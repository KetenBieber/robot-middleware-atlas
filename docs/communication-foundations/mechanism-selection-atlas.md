# Runtime Mechanism Atlas：先知道有哪些工具，再知道什么时候别用

这是一张横向工具地图。

它不替代深层机制页，而是回答一个更实际的问题：**当你看到一个工程约束时，候选方案空间到底有哪些？**

---

## 1. 状态共享：我要的是“最新值”还是“全部历史”

| 机制 | 语义 | 主要优点 | 主要代价 | 典型场景 | 不适合 |
| --- | --- | --- | --- | --- | --- |
| mutex-protected state | 读写同一份状态 | 简单、强一致临界区 | 锁等待、优先级反转 | 配置/低频状态 | 高频硬实时热路径 |
| atomic scalar | 单变量原子状态 | 极低开销 | 只能表达简单状态 | flag/counter/index | 复杂对象快照 |
| seqlock/versioned snapshot | Reader 重试直到读到一致版本 | Reader 无锁、适合一写多读 | Writer 不可长时间阻塞，Reader 可能重试 | 高频状态快照 | 每条更新都必须消费 |
| double/triple buffer | 读写不同 Buffer | ownership 清楚、适合大对象 | 需要 buffer 生命周期协议 | 图像/状态快照 | 命令历史 |
| latest-value mailbox | 新值覆盖旧值 | Data Age 低 | 丢历史 | localization/state | transaction/event |
| FIFO queue | 保存顺序历史 | 事件语义清晰 | backlog/data age | command/event/log | latest-state stream |

第一步不要先选 `std::queue` 或 ring，而要先问：

~~~text
旧数据还有业务价值吗？
每条更新都必须处理吗？
只要最新值可以吗？
~~~

如果答案不同，底层容器应该完全不同。

深入： [Ownership & Address Space](ownership-address-space.md)、[Queues & Backpressure](queues-backpressure.md)。

---

## 2. Queue Topology：Producer/Consumer 数量决定同步复杂度

| 拓扑 | 常见实现 | 优点 | 风险 | 典型场景 |
| --- | --- | --- | --- | --- |
| SPSC | bounded ring | 最容易做到低开销/可预测 | 只能一写一读 | driver→estimator |
| MPSC | mutex queue / sequence ring / per-producer queue | 多来源汇聚 | tail 热点、publication 复杂 | events→supervisor/logger |
| SPMC | work dispatch / fan-out cursor | 一源多消费 | 破坏性消费与广播语义要区分 | dispatcher→workers |
| MPMC | mutex/deque / lock-free bounded queue | 通用 | CAS/cache-line contention、reclamation | generic task pool |
| per-worker queues | local deque + stealing | 降低共享热点 | load imbalance、steal 复杂 | schedulers |
| sharded queues | hash/shard → local queues | 扩展性好 | ordering/fairness 更复杂 | high-rate notifications |
| point-to-point SPSC mesh | 每对 owner 间单向 SPSC | topology 简化、跨核共享最小 | N² 通道管理与显式路由 | shard-per-core runtime |

工业实现里，Folly 的 `ProducerConsumerQueue` 把 producer/consumer 各自状态放在独立 cache line，并缓存远端 cursor；`MPMCQueue` 则用全局 ticket + per-slot turn/generation 解决物理 slot 复用。Seastar 更进一步：两个 shard 之间天然只有一个 producer 和一个 consumer，因此直接使用点对点 SPSC request/completion queue，而不是全局 MPMC。这说明 topology 的变化会直接改变数据结构。

经验法则：

> 如果能通过拓扑设计把 MPMC 降成 SPSC/MPSC，就先改变拓扑，再谈 lock-free。

工业映射：Cyber 的 per-consumer buffer、Holoscan per-worker ready queue、UCX single-owner worker + MPSC submit。

深入： [Concurrent Queues & Progress Guarantees](concurrent-queues-progress.md)。

---

## 3. Overload / Backpressure：系统处理不过来时必须明确牺牲什么

| 策略 | 牺牲 | 保留 | 适用 | 危险点 |
| --- | --- | --- | --- | --- |
| block producer | 吞吐/上游实时性 | 完整性 | 必须无损任务 | 可能传播阻塞 |
| drop-new | 最新输入 | 已接受历史 | 已接受任务更重要 | 新状态长期进不来 |
| drop-old | 历史 | 新鲜度 | camera/perception | 中间事件可能消失 |
| latest-only | 全部历史 | 当前状态 | pose/state | 不适合命令 |
| reject/fail-fast | 可用性 | 系统边界 | capacity violation 应暴露 | 需要上层恢复 |
| rate limit | 峰值吞吐 | 稳定负载 | telemetry/API | 需要合理 token/window |
| admission control | 部分请求 | latency bound | expensive jobs | 需要代价模型 |
| quality degradation | 精度/质量 | 时限 | vision/AI pipeline | 策略复杂 |

关键不是“queue 满了怎么写代码”，而是：

~~~text
什么信息允许丢？
什么线程允许等？
deadline 过了以后处理还有意义吗？
~~~

工业映射：Holoscan queue policy/MemoryAvailableCondition、DDS History/ResourceLimits、Fast DDS FlowController。

---

## 4. Waiting / Wakeup：CPU 应该忙等、睡眠还是等事件

| 机制 | Wakeup latency | CPU 空闲成本 | 适用 | 典型问题 |
| --- | --- | --- | --- | --- |
| busy spin | 最低 | 很高 | dedicated RT core | 抢 CPU、功耗 |
| spin + backoff | 低 | 中 | 短临界等待 | 参数敏感 |
| periodic polling | 受周期影响 | 可控 | 简单设备/状态检查 | latency vs CPU |
| condition_variable | 低到中 | 低 | process-local queue | predicate/lost wakeup |
| semaphore | 低到中 | 低 | counting resources | count/state 一致性 |
| futex | 低 | 低 | 构建锁/同步原语 | API 较底层 |
| eventfd | 低 | 低 | Linux reactor/thread notification | 计数/合并语义 |
| armed queue + eventfd | 低 | 低 | 多 producer → 单 EventLoop | 必须正确处理 Empty/Armed/Non-empty 状态 |
| epoll | 低 | 低 | 多 fd event loop | edge/level trigger 语义 |
| libuv async | atomic pending + eventfd/pipe | 低 | 单 owner event loop 的跨线程唤醒 | notification 不是数据本身 |
| DDS WaitSet | 中间件级 | 低 | DDS condition aggregation | hidden runtime threads |
| interrupt | 很低 | 低 | device/ISR | ISR 约束 |

设计要点：**状态才是真值，notification 只是减少等待成本的优化。**

工业映射：libuv `uv_async_send`、Folly AtomicNotificationQueue/EventBase、Cyber Notifier、iceoryx2 Reactor、UCX worker arm+eventfd、Holoscan EventBasedScheduler。

---

## 5. Scheduler / Executor：Ready work 怎样获得 CPU

| 模型 | 数据结构 | 优点 | 风险 | 典型场景 |
| --- | --- | --- | --- | --- |
| dedicated thread | private state/queue | 最容易推理 | thread 数多 | control/driver |
| global task queue | MPMC queue | 简单通用 | hot queue contention | small pools |
| per-worker queue | local deque/ring | locality 好 | imbalance | high-throughput runtime |
| work stealing | local deque + victim scan | 利用率高 | locality/可预测性差 | irregular workloads |
| event loop | event queue/timer heap | thread 少 | callback 必须短 | network/control plane |
| libuv loop | fd registry + timer heap + phase queues | socket/timer/async 统一 | blocking work 必须隔离 | network/runtime |
| Asio scheduler | intrusive operation queue + reactor task | operation/executor/completion 分层 | async lifetime/cancellation 语义复杂 | C++ async runtime |
| strand / serial executor | waiting + ready 双队列 | 在 scheduler 层串行业务状态 | 长 handler 会阻塞同 lane | session/state machine |
| Folly CPU executor | pluggable blocking queue + LifoSem/ThrottledLifoSem | queue/wakeup/worker policy 可分离 | blocking task 可能饿死 CPU pool | CPU task execution |
| Seastar shard reactor | per-shard local queues + cooperative scheduling | mutable state 单 owner、跨核 cache bounce 少 | 长 task 必须主动让出 CPU | shard-per-core runtime |
| nginx worker loop | rbtree timer + posted queues + connection slots | 大量连接共享一个 worker execution owner | 单 callback 不能长期阻塞 | high-concurrency server |
| coroutine scheduler | ready list + context | 高并发 | 调试/栈语义复杂 | async I/O |
| fixed cyclic executive | static schedule | 高确定性 | 灵活性低 | hard RT periodic tasks |
| priority/deadline | priority queue/runqueue | 表达时限 | starvation/inversion | mixed-criticality |

然后还要叠加 Linux：

~~~text
SCHED_OTHER
SCHED_FIFO
SCHED_RR
CPU affinity
cgroup/cpuset
NUMA placement
~~~

工业映射：libuv loop、Asio scheduler/strand、Folly executor、Seastar reactor/scheduling group、nginx worker event loop、Cyber Scheduler/Processor、Orocos Activity、Holoscan EventBasedScheduler、ROS Executor。

### Timer 容器：同一个“最近 Deadline”问题也可以有不同实现

| 方案 | 最近 deadline | 任意删除 | 有序遍历 | 典型场景 |
| --- | --- | --- | --- | --- |
| min-heap | root O(1) | 需要 node/index 维护 | 不自然 | libuv dynamic timers |
| rbtree | 最左节点 | 内嵌 node 很自然 | 自然 | nginx connection timers |
| timer wheel | bucket 近似 O(1) | 依赖 wheel 结构 | 按桶 | 大量粗粒度 timeout |

Folly `HHWheelTimer` 使用 4 层 × 256 bucket 的 hierarchical timing wheel，并用 bitmap 快速跳过空的近端 bucket；nginx 还允许小于 300 ms 的 deadline 变化不重新插入 rbtree。容器选择因此不仅取决于 Big-O，还取决于 Timer 数量、删除模式、精度与 wakeup 策略。

---

## 6. Ownership / Lifetime：谁可以回收 Buffer

| 机制 | 适合 | 主要风险 |
| --- | --- | --- |
| copy | 小数据/边界清晰 | bandwidth/latency |
| move/unique ownership | 单消费者 | fan-out 困难 |
| shared_ptr/refcount | 多消费者 | atomic refcount、最后释放不可预测 |
| chained shared buffer | header/payload/scatter-gather | chain 与 storage lifetime 更复杂 |
| intrusive refcount | runtime object | 侵入对象布局 |
| object pool | 高频固定对象 | 回收条件必须正确 |
| operation object | 一次异步事务 | handler/payload/lifetime 边界复杂 |
| loan/reclaim | zero-copy message | 生命周期协议复杂 |
| generation handle | 可复用 slot | generation wrap/stale handle |
| connection slot + instance tag | fd/socket slot 复用 | stale kernel event | nginx epoll connection |
| hazard pointer | lock-free linked object | scan/management 成本 |
| epoch reclamation | 高频读 | stalled thread 延迟回收 |
| stream/event fence | GPU async buffer | fence 语义必须正确 |

zero-copy 的代价不是“没有代价”，而是：

> copy ownership 变成 lifetime ownership。

工业映射：Folly IOBuf 的 data-window / chain / SharedInfo、Asio scheduler_operation、nginx connection/request pool、iceoryx2 loan/sample、UCX Request/Buffer、Holoscan stream-aware deallocation。

---

## 7. IPC / Transport：边界越远，必须承担的语义越多

| 边界 | 常用方案 | 主要新增问题 |
| --- | --- | --- |
| 同线程 | direct call/reference | ownership |
| 同进程跨线程 | queue/mailbox | memory ordering/wakeup |
| 同主机跨进程 | shared memory / Unix socket / pipe | address/lifetime/crash recovery |
| 同机大 payload | SHM pool + descriptor | offset/generation/refcount |
| 跨主机小消息 | TCP/UDP/DDS/Zenoh/LCM | framing/reliability/discovery |
| 跨主机大 payload | UCX/RDMA | registration/rkey/progress |
| GPU 同机 | CUDA IPC / exported handle | stream/fence/device identity |
| GPU 跨主机 | UCX/RDMA/staged pipeline | memory domain + transport capability |

不要从“技术名词”选 transport；先从边界和 payload 性质选。

---

## 8. Memory Allocation：allocator 本身也会决定实时性

| 方案 | 优点 | 代价 | 适用 |
| --- | --- | --- | --- |
| malloc/new | 简单 | jitter/fragmentation | control plane |
| slab/object pool | 固定对象快 | 内部碎片 | request/message |
| fixed block pool | bounded/predictable | 配置要合理 | RT/GPU frames |
| arena/monotonic | 批量生命周期 | 不能单个 free | frame/request scope |
| pmr | C++ allocator 策略可注入 | 仍需设计后端 | generic runtime |
| stream-ordered allocator | GPU async lifetime | CUDA dependency | GPU pipeline |
| pinned host pool | DMA/H2D 有利 | 系统资源昂贵 | staging |

工业映射：nginx request arena / shared-memory slab、UCX request mpool、iceoryx2 chunk pool、Holoscan BlockMemoryPool/RMM。

---

## 9. Lookup / Registry：单 Owner、并发 Key Space 与 Read-mostly Snapshot 是三类问题

| 结构 | 并发模型 | 强项 | 代价 | 典型场景 |
| --- | --- | --- | --- | --- |
| owner-local `unordered_map` | 单线程/外部串行 | 简单、标准库 | cache locality 一般 | control plane |
| Folly F14 | 单 owner / 外部同步 | SIMD tag filter、紧凑 chunk、低 pointer chasing | 本身不是 concurrent map | hot lookup table |
| sharded ConcurrentHashMap | 多线程独立 key | 分片写锁、hazard-protected read | 跨 key 原子事务困难 | session/request registry |
| RCU immutable snapshot | 大量 Reader、少量 Writer | Reader 不阻止新版本发布 | grace period、旧版本暂时占内存 | config/routing/policy snapshot |
| serial owner + message passing | 任意 producer、单 mutable owner | 状态不变量最容易推理 | 所有更新经过 owner lane | supervisor/device state |
| shard-per-core ownership | 每核一份 local mutable state | 避免跨核锁与 cache-line bouncing | 跨 shard 事务必须显式消息化 | high-throughput multicore runtime |

这几种方案不能按“哪个更高级”排序。先问 mutable state 的 ownership：

~~~text
只有一个 owner，并且 lookup 很热？
→ F14 / 普通 owner-local map

多个线程真的要同时更新互不相关的 key？
→ sharded concurrent map

Reader 极多，而 Writer 可以构建完整新版本再发布？
→ RCU snapshot

更新之间存在复杂跨 key 不变量？
→ serial owner 或显式事务锁
~~~

Folly `ConcurrentHashMap` 进一步展示了两个不同问题：hazard pointer 负责“Reader 手里的 node 还不能回收”，seqlock 负责“bucket pointer 与 bucket count 是同一版本”；Folly RCU 则把保护粒度从具体 pointer 提升到整个 read-side epoch。Seastar 则给出另一条路线：如果 mutable state 能按 core/shard 切分，就先消除共享；只有跨 shard 协作时才通过 message passing 暴露边界。

---

## 10. 数据结构选型不是背 Big-O

应该同时记录：

~~~text
operation mix
frequency
producer/consumer topology
cache locality
allocation behavior
determinism requirement
boundedness
failure/shutdown semantics
~~~

例如 Folly SPSC 把 local cursor 和 remote cursor cache 放进 owner-specific cache line，是因为最频繁操作是“读写自己的进度、偶尔刷新对端进度”；MPMC 又改成 ticket + per-slot turn，因为多个 producer/consumer 已经破坏这种 single-owner 条件。Asio 的 `descriptor_state` 按 read/write/except 拆队列、nginx 用 rbtree、libuv 用 min-heap、Folly 用 timing wheel，也都来自不同 operation mix。数据结构选择来自访问模式，而不是统一偏好某种 STL。

---

## 11. 一个最小选型流程

面对新问题，先按以下顺序：

~~~text
1. Data semantics
   state / event / stream / task?

2. Topology
   SPSC / MPSC / SPMC / MPMC?

3. Time semantics
   deadline / max age / lossless?

4. Capacity
   bounded how? overload policy?

5. Ownership
   copy / move / loan / shared / pool?

6. Wakeup
   spin / block / event?

7. Execution
   dedicated / pool / event loop / RT?

8. Boundary
   thread / process / host / device?

9. Failure
   shutdown / crash / reconnect / stale handle?
~~~

最后才去选具体 API/框架。
