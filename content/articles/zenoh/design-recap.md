# Zenoh 设计总结：从数据空间到可复刻路由内核

本篇沿本地固定源码 `9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 归纳 [Session 生命周期](session-runtime.md)、[数据转发](publisher-routing.md)、[路由缓存](resource-route-cache.md)、[Query 完成协议](query-lifecycle.md)以及[背压和关闭](backpressure-close.md)。源码事实与自行设计的改进建议在正文中分别讨论，不能把后者当成该提交的现成功能。

Zenoh 的核心不是某一个发布函数，而是一组相互制约的结构：Session 提供应用级实体与生命周期，Primitives 隔离 API 和路由内核，Face 表示一条逻辑通信边界，Resource tree 维护 key-expression 关系，Route cache 把昂贵计算移出高频数据路径，Transport 最终承担链路交付。

把这些结构按因果关系连接起来，可以得到一幅完整的实现图：

```text
Application
  |
  | builder: declare / put / get
  v
Session
  |  entity registries, qid/id allocation, local callbacks,
  |  QueryState, timeout tasks, close linearization
  v
Primitives
  v
Mux -- ingress policy --> Face / Gateway / Routing Tables
                         | Resource tree
                         | keyexpr match graph
                         | versioned data/query route cache
                         | pending query fan-out/fan-in
                         v
                  destination Faces
                         |
                  egress policy -- DeMux
                         v
               Transport / remote node
```

## 适用场景与系统定位

Zenoh 适合需要跨越进程、设备和网络边缘统一访问数据的系统。相同的 key-expression 空间可以承载发布订阅、查询/回复与存储相关能力，因此上层无需为实时流和按需读取维护完全独立的命名体系。

典型场景包括：

- 机器人本体、边缘计算盒和云端之间的状态与指令交换；
- 需要通配表达式选择一组设备或资源；
- 同一数据既要实时订阅，也要在新节点上线时按需查询；
- 网络拓扑变化，但高频数据路径仍需要复用路由结果；
- 一个进程中有本地实体，同时还要透明访问远端实体。

它不是天然替代所有实时总线。若系统只需要单机固定拓扑的极低延迟共享内存，Zenoh 的表达式匹配、动态路由和异步生命周期会带来额外复杂度；若需要严格硬实时上界，也必须逐层验证分配、锁、队列与传输实现。

## 数据路径的关键不变量

Publisher 路径可以浓缩成六个不变量：

1. Builder 只收集配置，真正的合法性与状态检查在执行点完成；
2. 同一 key expression 的声明可以在 Session 内聚合，远端不必看到每个本地 handle；
3. WireExpr 用 scope id 加 suffix 压缩重复字符串，但进入路由前必须还原为可匹配表达式；
4. ingress policy 在路由前执行，egress policy 在每个目标 Face 发送前执行；
5. Route 是不可变共享快照，拓扑变化通过失效而不是原地修改旧快照；
6. 路由表锁只保护查找与快照建立，不包围网络发送或用户 callback。

Query 路径另外增加四个不变量：

1. Reply 数量不能代表完成，Final 必须是独立控制信号；
2. 本地与远端是两个生产路径，`Locality::Any` 必须等两路 Final；
3. 每个 Face 使用自己的 qid 空间，pending map 负责双向改写；
4. timeout、Face close 和正常 Final 并发竞争时，只有成功取走 pending 状态的一方负责完成。

## 控制面与数据面的分离

声明、拓扑变化和 key-expression 匹配属于控制面；Put/Push 和 Reply 转发属于数据面。Zenoh 把更多计算放到控制面：

```text
控制面
  建立 Resource
  计算 matches
  聚合 declarations
  推进 route versions

数据面
  cache lookup
  clone Arc<Route>
  按目标 Face 重写 WireExpr
  发送
```

这是一种“读多写少”优化。拓扑稳定时，高频数据只承担缓存查找和目的地遍历；声明抖动或大量通配符会把成本重新推回控制面，并通过失效导致数据路径首次访问抖动。

## 最值得提取的设计模式

### Builder 与执行点分离

Builder 让 API 易于组合，但不应成为状态真相来源。Session 和 Runtime 在执行点重新验证 closed 状态、配置一致性与所有权。该模式适合异步 API，因为 builder 创建和真正执行之间可能已经发生关闭或拓扑变化。

### Facade 与 Primitives 边界

Session 面向应用实体，Primitives 面向协议动作。上层只表达“声明、推送、请求、回复”，下层可以由本地路由器、远端 Face 或插件 Runtime 实现。这是端口与适配器思想：稳定接口隔离变化较快的传输与路由实现。

### RAII 完成协议

`QueryInner::drop` 将最后一个共享 Query 的释放转成 Final；`Arc<Query>` 的最后一个 pending 方向转成上游 Final。它把生命周期与协议完成绑定，减少手工计数，但要求所有异常路径都能释放引用。

### 不可变路由快照

`Arc<Route>` 创建后只读。更新发生时让旧快照失效并计算新对象，而不是在发送线程正在遍历时修改 vector。这类似 copy-on-write/RCU 的工程取向：读路径简单，写路径承担重建成本。

### 版本化惰性失效

全局拓扑变化只推进 version，缓存下次访问时自行发现过期。它把一次潜在 `O(R)` 的停顿分散到真实访问集合，但也可能在拓扑变化后的第一波流量中形成重算尖峰。

### 锁外执行外部代码

Session callback、实体析构、网络发送都不应在核心状态锁内执行。做法是锁内复制所需句柄或移出容器，释放锁后再调用。这是避免回入死锁和长尾锁等待的基础规则。

### 所有权驱动的关闭

Session 只终止自己拥有的实体和任务；拥有 static Runtime 时才继续关闭 Transport 与 routing tables；共享 DynamicRuntime 时只撤销该 Session。关闭权限跟随所有权，而不是跟随“谁先调用 close”。

## Rust 机制与 C++ 对应关系

理解设计不要求照搬语言。下表给出可迁移的实现对应：

| Rust 机制 | 设计含义 | C++ 可选实现 |
|---|---|---|
| `Arc<T>` | 跨任务共享只读或内部同步对象 | `std::shared_ptr<T>` |
| `Weak<T>` | 非拥有索引，打破引用环 | `std::weak_ptr<T>` |
| `RwLock<T>` | 高频读、低频写状态保护 | `std::shared_mutex` |
| `Drop` | 作用域结束自动清理/发 Final | 析构函数与 RAII guard |
| trait object | 运行时可替换接口 | 纯虚基类或 type erasure |
| async task + token | 协作式后台任务取消 | coroutine/task + `std::stop_token` |
| bounded channel | 有界生产者消费者队列 | mutex/CV ring 或成熟队列库 |

迁移时要复制不变量，不要机械翻译类型。C++ 析构函数不能安全地等待任意异步网络操作，因此“析构触发 Final”可设计为同步入队，由受控 I/O worker 真正发送；显式 `close()` 仍负责等待队列刷空。

## 数据结构选择与性能结论

| 数据结构 | 选择原因 | 性能优势 | 主要风险 |
|---|---|---|---|
| Resource chunk tree | 共享 key 前缀 | 精确查找约 `O(depth)` | 深树与节点分配 |
| 双向 `Weak` matches | 预计算表达式相交 | 局部失效无需全树扫描 | 通配符密集时边数膨胀 |
| RegionMap + `Vec<Option<T>>` | NodeId 较稠密时直接索引 | 命中近似 `O(1)` | 稀疏 id 浪费空间 |
| `Arc<Route>` | 多线程共享不可变目的集合 | 命中只做引用计数 | 旧快照延迟释放 |
| HashMap qid/entity registry | 动态在途状态 | 平均 `O(1)` 定位 | hash 抖动与峰值内存 |
| bounded FIFO/Ring | 明确内存上界 | 可预测容量 | 阻塞或丢弃必须二选一 |

吞吐测试必须与控制面 churn 分开。稳定拓扑下的 cache-hit 吞吐，不能代表大量设备上线、声明变化和 wildcard 路由重算时的表现。

## 优点

- Session 与 Runtime 分层清晰，既支持普通独占运行时，也支持插件共享运行时；
- 数据、查询与表达式路由共享统一资源模型，减少多套命名与发现机制；
- Route cache 同时提供精确局部清除与常数成本全局失效；
- Query 用 Final、RAII 和 pending map 明确表达多回复、多方向完成；
- 用户 callback 和发送动作位于核心锁外，降低回入死锁与锁长尾；
- TaskController 使后台任务归属和关闭 barrier 可见。

## 缺点与工程风险

- 公开 builder 的 `await` 外观不等于所有初始化都天然可取消，调用者仍需理解执行点；
- Resource 清理对具体 `Arc` 临时引用布局敏感，维护时容易因多一个 clone 改变行为；
- wildcard-heavy 工作集会同时放大 match graph、缓存失效和 route compute；
- Query 有请求端 timeout、每方向 timeout、取消 token、Session close 和迟到消息，多路径竞态增加诊断难度；
- 全局惰性失效把重算延后，拓扑突变后的第一波请求可能出现尾延迟尖峰；
- Rust 内存安全不自动证明协议生命周期正确，pending 引用泄漏仍会阻止 Final 与资源回收；
- 完整互操作还依赖 transport、protocol codec、各 Hat 路由策略和拦截器链，不能只复刻 Session 层。

## 从零实现的依赖顺序

建议按底层不变量向公开 API 反向构建：

1. **任务层**：TaskController、取消 token、tracked/abortable task 和 terminate barrier；
2. **资源层**：chunk tree、精确 key、通配相交、Weak match edges 与清理；
3. **缓存层**：不可变 Route、版本号、局部和全局失效；
4. **路由层**：Face、Mux/DeMux、WireExpr 解析、ingress/egress policy；
5. **查询层**：qid、pending query、Reply、Final 与 fan-out/fan-in；
6. **运行时层**：Gateway、Transport callback、mode orchestrator 和 scouting；
7. **Session 层**：entity registries、本地快速路径、timeout/consolidation 和 close；
8. **Builder 层**：公开声明、Put、Get 和 handler 适配接口。

Builder 放在最后，是因为它只负责收集参数和触发执行。真正决定正确性的状态位于 Session registry、routing tables、pending maps 与 task shutdown 中。

## 最小可运行内核的模块边界

第一版不必实现完整 Zenoh wire compatibility，可以先固定以下接口：

```cpp
struct KeyExpr {
  bool intersects(const KeyExpr&) const;
  bool includes(const KeyExpr&) const;
};

struct Route {
  std::vector<std::shared_ptr<Face>> destinations;
};

class ResourceTree {
 public:
  ResourceHandle declare(KeyExpr);
  std::shared_ptr<const Route> data_route(Source, KeyExpr);
  void invalidate_intersections(const KeyExpr&);
};

class Face {
 public:
  void send_push(Push);
  void send_query(Request);
  void send_reply(Response);
  void send_final(ResponseFinal);
};

class Session {
 public:
  Publisher declare_publisher(KeyExpr);
  Queryable declare_queryable(KeyExpr, QueryCallback);
  ReplyReceiver get(Selector, QueryOptions);
  Task<void> close();
};
```

随后用本地 loopback Face 验证完整状态机，再接入真实 Transport。这样可以把路由与生命周期错误同协议编码错误分离。

## 实现完成的判定标准

一个“能发送 Hello World”的原型还没有覆盖中间件核心。至少应证明：

- 同一 Session 的多个相同 Publisher 只产生正确数量的远端声明；
- 本地与远端 Query 同时命中时，只有两路 Final 都完成后 receiver 才关闭；
- 某个下游不返回 Final 时，per-direction timeout 能释放 pending entry；
- 新增相交通配声明后，旧 Route 不再被复用；
- callback 中取消自身实体不会死锁；
- 慢消费者分别呈现预期的 FIFO 阻塞或 Ring 淘汰行为；
- 连续两次 close 只有一次底层副作用；
- close 返回后没有活动 task、Face、pending query 或可达 Resource 子树。

达到这些条件，才说明复刻的不只是 API 形状，而是 Zenoh 最关键的路由、聚合与生命周期设计。
