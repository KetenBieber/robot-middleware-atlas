# FlowGraph 容器设计：为什么一张 DAG 同时需要 unordered_map、map、list、set 和 cache

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

这一页不讲“怎么调用 add_flow”。我们直接站在源码作者角度问：如果要自己实现一张 Operator Graph，究竟应该用什么数据结构？

最朴素的答案可能是：

~~~cpp
std::unordered_map<Node, std::vector<Node>> graph;
~~~

但固定 `FlowGraphImpl` 同时使用 `unordered_map`、`map`、`list`、`set`、`vector`、`unordered_set` 与 `optional`。这正好是一个“为什么不能所有问题都用一种 STL”的真实案例。

## Edge 到底存什么

Operator graph 的 edge data 类型是：

~~~cpp
using OperatorEdgeDataElementType =
    std::unordered_map<
        std::string,
        std::set<std::string, std::less<>>>;
~~~

语义是：

~~~text
source output port name
        ↓
set of destination input port names
~~~

所以一条 A → B 的逻辑 edge 可以同时携带多个 port mapping。Graph 的 node adjacency 与 port connectivity 是两层关系。

## 为什么 EdgeData 外层是 unordered_map，value 又是 set

给定 source output port name，常见操作是立即找到所有 destination input ports，因此 hash lookup 很自然。

`std::set` 则表达“一组不重复 input port”。固定 `add_flow()` 合并已有 edge 时直接使用：

~~~cpp
datadict->at(key).merge(value);
~~~

如果改成 vector，每次 merge 都必须自己做 duplicate check。

这里不是“set 比 vector 高级”，而是 set 更直接表达 edge port mapping 的不变量。

## adjacency 为什么同时保存 succ_ 与 pred_

真实定义：

~~~cpp
std::unordered_map<
    NodeType,
    std::map<NodeType, EdgeDataType, NodeTypeCompare>>
    succ_;

std::unordered_map<
    NodeType,
    std::map<NodeType, EdgeDataType, NodeTypeCompare>>
    pred_;
~~~

理论上一份 successor graph 已经能表示完整有向图，但 runtime 同时会问：

~~~text
A 后面是谁？
B 前面是谁？
B 的某个 input indegree 是多少？
A 的某个 output outdegree 是多少？
谁是 root？
谁是 leaf？
~~~

如果只保存 succ_，查询 B 的 predecessor 需要扫描全图。保存双向索引后可以分别从 `succ_[A]` 和 `pred_[B]` 直接进入局部 adjacency。

这是经典的：**用额外内存换反向查询速度。**

## 外层为什么是 unordered_map

NodeType 本身是：

~~~cpp
using OperatorNodeType = std::shared_ptr<Operator>;
~~~

常见路径是“给定这个 Operator object，找到它的邻接表”。直接用 NodeType 做 hash key，避免额外维护连续整数 NodeId。

另一种设计当然可以把 Node 映射成整数，然后使用 `vector<vector<NodeId>>`。那会获得更好的连续内存局部性，但要支付 ID 管理和动态删除复杂度。

这没有绝对答案，取决于 graph 是否频繁修改、规模多大、是否需要稳定 ID。

## 内层为什么偏偏是 std::map

源码注释直接说明目的：

~~~cpp
// Use std::map values so that nodes returned by
// get_root_nodes() and get_next_nodes()
// are in a deterministic order (by insertion order).
~~~

如果 adjacency 也使用 unordered_map，遍历顺序可能随 hash 状态变化。日志、测试、生成配置乃至 shutdown traversal 都会更难复现。

所以组合实际上是：

~~~text
外层：
按 node 快速定位 adjacency
→ unordered_map

内层：
邻居 traversal 需要 deterministic
→ ordered map
~~~

## NodeTypeCompare：排序标准不是名字，而是 insertion order

内层 map 使用自定义 comparator：

~~~cpp
struct NodeTypeCompare {
  const std::list<NodeType>* ordered_nodes;

  bool operator()(const NodeType& lhs,
                  const NodeType& rhs) const {
    auto lhs_it = std::find(ordered_nodes->begin(),
                            ordered_nodes->end(), lhs);
    auto rhs_it = std::find(ordered_nodes->begin(),
                            ordered_nodes->end(), rhs);
    return std::distance(ordered_nodes->begin(), lhs_it)
         < std::distance(ordered_nodes->begin(), rhs_it);
  }
};
~~~

这意味着 traversal 顺序对应用户 compose graph 时的添加顺序，而不是 pointer 地址或名字字典序。

## 这个 comparator 其实并不“算法最优”

`std::find` 在 list 上是线性扫描。假设图里有 V 个节点，一次比较最坏就是 O(V)；而 `std::map` 的 insert/find 又需要多次 comparison。

因此大图构建时，这个 comparator 的渐进复杂度并不漂亮。

可以想象另一种实现：

~~~cpp
std::unordered_map<NodeType, uint64_t> insertion_index;
~~~

然后 comparator 只比较两个 index。

为什么当前实现仍然合理？关键在于 **FlowGraph 是配置/control plane，而不是每帧必经 hot path**。

图通常在 compose/init 阶段建立一次，运行阶段真正高频的是 Receiver queue、Condition、ready queue、worker dispatch。

这正好说明：**数据结构不能脱离调用频率谈最优。**

## ordered_nodes_ 为什么单独存在

源码：

~~~cpp
std::list<NodeType> ordered_nodes_;
~~~

它保存 node 的添加顺序，同时给 NodeTypeCompare 提供顺序真值。

list 的优势包括插入后 iterator/reference 稳定、erase 不搬移后续元素；缺点是 cache locality 差、线性搜索慢。

这里的设计明显更偏向“稳定顺序与实现清晰”，而不是最大化 graph-build throughput。

读工业源码时不应该把每个容器选择神化成理论最优，而应该问它服务什么约束。

## name_map_：为什么同一个 Node 还要第二套索引

源码还有：

~~~cpp
std::unordered_map<std::string, NodeType> name_map_;
~~~

Graph 已能通过 NodeType 找节点，但配置与用户接口经常只有字符串，例如 `segmentation_inference` 或 `holoviz`。

没有 name_map_ 时，`find_node(name)` 只能全表扫描；现在可以直接 hash lookup。

这就是数据库式二级索引：

~~~text
primary object identity:
NodeType

secondary lookup:
name → NodeType
~~~

代价是 add/remove 必须同步维护索引一致性。

因此 add_node() 同时做：

~~~cpp
ordered_nodes_.push_back(node);
name_map_[node->name()] = node;
~~~

并且 duplicate name 直接报错。name 一旦成为唯一索引，它就不再只是 UI 标签。

## cycle detection 为什么使用 optional<vector> cache

源码：

~~~cpp
mutable std::optional<std::vector<NodeType>>
    cached_cyclic_roots_;
~~~

如果只用空 vector 表示 cache，会无法区分：

~~~text
从没算过
vs
算过了，而且确实无环
~~~

`optional<vector<...>>` 则可以区分三态：

~~~text
nullopt       = not computed
vector{}      = computed, no cycle
vector{...}   = computed, cycle roots
~~~

而 add_node/add_flow/remove_node 都会执行：

~~~cpp
cached_cyclic_roots_.reset();
~~~

这才是一套完整 cache protocol：**cached result + invalidation condition**。

## DFS 临时状态为什么又换回 unordered_map/unordered_set

cycle detection 内部使用：

~~~cpp
std::unordered_map<NodeType, VisitState> visit_states;
std::unordered_set<NodeType> seen_cyclic_roots;
~~~

因为这里不需要对 membership state 做 deterministic ordered traversal，只需要高效 lookup。

同一算法里：

~~~text
persistent adjacency:
deterministic traversal matters
→ map

temporary DFS state:
membership lookup matters
→ unordered_map / unordered_set
~~~

这就是按访问模式选容器，而不是按个人偏好选容器。

## 为什么导出 connectivity 时又构造 map<string, vector<string>>

对外输出时，源码重新创建：

~~~cpp
std::map<std::string, std::vector<std::string>>
    input_to_output_map;

std::map<std::string, std::vector<std::string>>
    output_to_input_map;
~~~

vector 还会 sort + unique。

原因是内部表示针对 mutation/query，外部表示针对 deterministic inspection/YAML/debug。

同一个逻辑关系完全可以有两种 representation：

~~~text
internal representation
  optimize lookup/mutation

external representation
  optimize stable inspection
~~~

强迫整个系统只使用一种数据结构，通常反而让每种访问模式都变差。

## Graph container 与 Scheduler ready queue 是两类问题

FlowGraph 的特点：

~~~text
变化慢
规模相对小
配置阶段访问
强调稳定顺序与 topology query
~~~

Scheduler ready queue 的特点：

~~~text
变化极快
多个 worker 高频 push/pop
关注 contention、wakeup、cache line
~~~

所以 FlowGraph 使用 std::map 并不意味着 ready queue 也应该用 map；ready queue 追求并发也不意味着 Graph configuration 必须 lock-free。

这就是 **control plane 与 hot data path 分离**。

## 如果自己重写一版

一种不同取舍是先给节点分配稠密 NodeId：

~~~cpp
using NodeId = uint32_t;

class FlowGraph {
  std::vector<std::shared_ptr<Operator>> nodes_;
  std::unordered_map<std::string, NodeId> name_to_id_;
  std::vector<std::vector<NodeId>> succ_;
  std::vector<std::vector<NodeId>> pred_;
  std::optional<CycleResult> cycle_cache_;
};
~~~

这样邻接表 cache locality 更好，但动态 remove、稳定 ID 和 object↔ID 映射都会更复杂。

没有一种实现脱离使用场景以后还能被称为“绝对最优”。

## 对具身 Runtime 最可迁移的三件事

第一，同一份状态可以维护多套索引。TaskId、name、priority、deadline 都可能需要不同视图。

第二，cache 一定和 invalidation 一起设计。只说“缓存起来”不是完整方案。

第三，任何复杂度判断都必须带上调用频率：control plane 可以接受的成本，不代表 1 kHz control loop 或 60 FPS ready path 也能接受。

真正专业的源码阅读不是问“为什么不用更高级的数据结构”，而是问：**这个容器处在什么访问模式、什么频率、什么并发边界里？**
