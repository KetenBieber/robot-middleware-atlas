# Context、Session 与 Router：rmw_zenoh 的 Runtime 根对象

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇只研究 Runtime 根：`rmw_context_t`、`rmw_context_impl_s::Data`、共享 `zenoh::Session`、`NodeData` 与 Router 的关系。

## 1. 为什么 Context 是正确的 Session 粒度

`rmw_context_impl_s::Data` 在构造阶段创建：

~~~text
zenoh::Session
GraphCache
graph GuardCondition
serialization BufferPool
optional SHM context
BufferBackendContext
nodes_ map
~~~

而不是让每个 Node 创建一个 Session。

源码中的核心成员关系可以压缩成：

~~~text
Data
 |
 +-- session_: shared_ptr<zenoh::Session>
 +-- graph_cache_: shared_ptr<GraphCache>
 +-- nodes_: unordered_map<rmw_node_t*, shared_ptr<NodeData>>
 +-- next_entity_id_: atomic<size_t>
 +-- is_shutdown_: atomic<bool>
~~~

这形成一个天然 ownership root。

## 2. NodeData 为什么保存在 Context 的 map 里

`create_node_data()` 在 Context mutex 下：

1. 检查 node 是否已存在；
2. 检查 Session 是否仍有效；
3. 获取新的 entity id；
4. 构造 `NodeData::make(...)`；
5. 插入 `nodes_`。

因此 NodeData 生命周期由 Context 集中追踪。

这不是为了方便查找，而是保证 RMW C handle 只是一层 type-erased façade，真实 C++ 对象仍由 Runtime ownership tree 管理。

## 3. 一个 Node 的 entity id 为什么来自 Context

`get_next_entity_id()` 使用 atomic fetch-add。

原因是同一 Session 下所有 graph entity 最终都需要在 liveliness key 中具有可区分 identity。

~~~text
Context owns identity namespace
    |
Node id
    |
Publisher / Subscription / Service / Client id
~~~

如果每个 Node 自己从 0 开始编号，而 key 又缺少足够父级信息，就可能产生 graph identity collision。

## 4. Session 初始化为什么先读取 Router 状态

Data 构造完成 Zenoh Session 后，会检查 Router 是否可达。

默认设计依赖 Router 帮助 Sessions 发现彼此，尤其是跨主机环境。

但源码在 Router 不可达时并不一定直接 abort；根据配置，它可以多次尝试后继续初始化，并警告其他 peer 暂时无法发现。

这说明 Router connectivity 属于 network convergence capability，而不是 C++ object existence precondition。

## 5. 初始化 GraphCache 为什么先做一次 liveliness_get

只订阅未来 liveliness update 会漏掉“在本 Context 启动之前已经存在”的实体。

因此初始化阶段先：

~~~text
liveliness_get(all existing)
   |
parse_put(...)
   |
GraphCache initial snapshot
~~~

随后才通过 liveliness subscriber 接收增量变化。

这是经典的 snapshot + incremental updates 模式。

## 6. 为什么初始 liveliness_get 使用阻塞 FIFO

源码注释明确解释：初始化阶段希望按顺序收完整 reply，并避免过小 bounded channel 因生产/消费调度关系导致 starvation 或 deadlock。

所以这里选择 blocking FIFO 并不是随手写法，而是初始化阶段的执行模型决定的。

初始化线程本来就在等待 graph snapshot，不需要改成 busy polling。

## 7. SHM 为什么也挂在 Context 根上

当 Zenoh shared-memory transport optimization 启用时，Data 会初始化共享的 SHM 相关状态。

这说明 SHM 不是 Publisher 私有资源，而是整个 Session/Context 下多个 endpoint 共享的 transport capability。

判断某 Publisher 是否真正低复制时，必须同时看 Context transport config、message type、payload size、backend capability 与 receiver path。

## 8. shutdown 为什么用 atomic once

`Data::shutdown()` 先通过 `compare_exchange_strong` 把 `is_shutdown_` 从 false 置 true。

这建立了：

> 关闭操作只能由一个执行上下文赢得。

之后才 undeclare graph subscriber、关闭 buffer backend、close Session。

显式 `rmw_shutdown` 与析构都可能抵达这里，因此 once-only state transition 比“析构里直接 close”更可靠。

## 9. Session close 为什么是全局屏障

共享 Session 被所有 NodeData 和 endpoint 使用。

Session close 不是“关闭一个 socket”，而是 Context 数据面的全局生命周期屏障。

固定版本源码依赖 Zenoh close 等待 in-flight callbacks 的语义，再释放 Context 自己的 Session 引用。

## 10. 可迁移的对象图

任何自研中间件若存在“一个 Runtime 下多个逻辑节点共享 transport”的需求，都可以采用类似结构：

~~~text
RuntimeContext
  owns
Session/Transport
Graph/Registry
Allocator/Pool
Node registry
Shutdown state
~~~

这样连接复用、身份空间、关闭顺序和共享资源都集中在一个明确根对象。
