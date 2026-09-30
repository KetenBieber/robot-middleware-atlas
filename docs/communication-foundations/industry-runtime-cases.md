# 工程案例：同样的数据流问题，在真实 Runtime 里怎样落地

:::{contents} 本页目录
:depth: 3
:local:
:::

前面的 Communication Foundations 很容易被误解成“自己造中间件时才需要学的底层知识”。

真实工程恰好相反。

真正复杂的系统不会只出现一条 queue、一个 mutex 或一个 socket。它们会把不同的数据结构和执行策略叠在一起：

~~~text
业务对象
↓
线程间 queue / latest-value state
↓
runtime scheduler
↓
IPC / DDS / UCX / EtherCAT
↓
Linux scheduler / NIC / GPU / device
~~~

这一页选择几类已经在 Atlas 中固定源码版本的工程，再加一个现代 GPU streaming runtime，观察同一组问题如何反复出现。

重点不是比较谁“更先进”，而是看设计约束如何逼出不同的数据结构。

---

## 案例一：Apollo Planning——为什么“输入都到了”仍然不是一个原子快照

Apollo Planning 同时存在高频触发输入、低频状态输入、Cyber Reader/Writer、CRoutine、OS worker thread，以及算法内部自己的 LocalView 与 Frame。

完整案例见 {doc}`Apollo Planning 如何把 Cyber RT 变成规划流水线 <../generated/cyber/case-study-apollo-planning>`。

### 先看业务需求

一次规划需要 Prediction、Chassis、Localization 等主输入，同时还会读取 PlanningCommand、TrafficLight、Story 等辅助状态。

最粗糙的实现会想：

~~~text
每个 topic 都来一次 callback
→ 放到全局变量
→ Proc() 直接读
~~~

问题是这些 callback 与 Proc() 并不处于同一执行序列。于是工程里出现：

~~~text
Reader callback
  ↓
mutex
  ↓
copy into component-owned state

Planning Proc
  ↓
mutex
  ↓
copy selected state into LocalView
  ↓
unlock
  ↓
RunOnce()
~~~

这里最值得学习的不是 `std::mutex` 本身，而是**快照语义**。

固定源码里多个辅助字段使用同一把 mutex，但 Proc() 分多个临界区逐段复制。于是：

~~~text
PlanningCommand version 100
↓ unlock

Story callback updates version 101

↓ lock again
Story version 101
~~~

最终一个 LocalView 可以合法地包含“命令 100 + 场景 101”。

> **mutex 能保证没有 data race，却不会自动给业务建立跨 topic transaction。**

如果业务需要强一致快照，还要引入 sequence/version、共同临界区或显式 snapshot protocol。

### 数据结构为什么不是全部用 queue

辅助状态很多时候只需要“下一次规划看到最新值”。如果每次 Routing/Story 更新都进入无限 FIFO，producer faster than planner 时会把历史状态越积越多。

所以真实机器人 runtime 经常同时使用：

~~~text
事件流       → queue
最新状态     → mailbox / protected latest object
算法周期对象 → local snapshot
~~~

这就是为什么“所有通信统一成 queue”通常不是好抽象。

---

## 案例二：ETH RSL soem_interface——EtherCAT 周期数据为什么先 stage，再统一发

ETH Zurich Robotic Systems Lab 的 `soem_interface` 是 SOEM 在真实机器人软件里的工程封装。

Atlas 已经固定并拆解该案例：{doc}`ETH RSL soem_interface <../generated/soem/case-study-leggedrobotics-soem-interface>`。

其核心对象可以概括成：

~~~text
EthercatBusManagerBase
  ↓ owns / manages
EthercatBusBase
  ↓ contains
EthercatSlaveBase...
~~~

Slave 不直接在任意时刻调用网卡发送，而是先把 typed PDO stage 到统一 IOmap，所有 Slave 完成以后再由 Bus 一次提交 process data。

这本质上是：

~~~text
业务对象修改逻辑状态
        ↓
连续 process image
        ↓
一个周期统一提交
~~~

它很像数据库 prepare/commit，也很像 GPU command buffer。

### WKC 为什么是数据有效性的门

工程不是“SOEM receive 完就让所有 Slave 读内存”，而是先判断 Working Counter。WKC 不可信时，不把 stale/partial process image 当成新设备状态。

这个原则可以直接迁移到传感器 pipeline：

~~~text
transport received bytes
!=
business data is valid
~~~

CRC、sequence、timestamp、generation、WKC 都是在定义“这一批数据能不能进入下一层”。

### 一个 coarse mutex 的真实代价

案例使用较粗的 Context 互斥边界协调周期过程数据、SDO、诊断和关闭。

优点是 ownership 简单；代价是慢 SDO/diagnosis 可能延长实时数据面的锁等待。

因此进一步的生产化问题自然出现：control-plane 和 data-plane 是否应该分线程、分 queue、甚至分锁域？

---

## 案例三：ROS 2 RMW——Executor 等待之前已经叠了多少层容器

ROS 2 很容易让人把所有延迟都归因到 Executor。实际上 RMW → DDS 已经构成一个完整 runtime。

Atlas 中可以对照：

- {doc}`rmw_cyclonedds <../generated/cyclonedds/case-study-rmw-cyclonedds>`
- {doc}`rmw_fastrtps <../generated/fastdds/case-study-rmw-fastrtps>`

### Cyclone DDS RMW 的数据结构选择

`CddsWaitset` 一层就能看到：

~~~text
std::vector
  subscriptions / guard conditions / services / clients / events

std::vector<dds_attach_t>
  triggers

std::unordered_set<dds_entity_t>
  deduplicate event entities
~~~

为什么这里可以接受 STL 容器，而 DDS 内核自己又会使用 AVL/hash/intrusive list 等更细的数据结构？因为访问模式不同。

RMW WaitSet 的集合规模通常是“当前 Executor 这一轮要等的 entity 数量”，更重视清晰重建 attachment set、与 ROS handle 映射和 ready filtering；DDS 协议层维护长期 endpoint/history/proxy 状态，热路径约束完全不同。

### Fast DDS RMW 为什么每轮 attach/detach

Fast DDS RMW 会 collect Conditions → attach to WaitSet → wait → detach → re-check ROS entities。

所以所谓“一次 Executor wait”背后已经包含 container build、condition mapping、DDS wait primitive 和 ready filtering。

### OS 线程图比 ROS Node 图更重要

真实进程里可能同时存在 DDS receive thread、DDS event/async writer thread、RMW graph listener thread、ROS Executor worker 和 application-owned thread。

做实时分析时应该画 thread、priority、CPU affinity、blocking primitive、queue/history owner 与 wake-up edge，而不是只画 ROS node/topic topology。

---

## 案例四：NVIDIA Holoscan——现代 GPU Pipeline 已经变成 Scheduler + Memory Runtime

Holoscan SDK v4.6.0 提供了一个完整的 event-driven GPU runtime 实例。Atlas 本地源码固定在：

~~~text
source-audit/holoscan-sdk
tag: v4.6.0
commit: 66a9609ac37515405561b9b8dbdee8e57f41ab11
~~~

Holoscan 表面是 Operator Graph，但往下一层会同时出现 FlowGraph、Operator/Resource、Condition、Scheduler、ThreadPool、Allocator、CUDA Stream、DoubleBuffer/AsyncBuffer、UCX Transmitter/Receiver。

### Event-Based Scheduler 为什么值得单独研究

官方 v4.6.0 文档已经把旧 MultiThreadScheduler 标记为 legacy，并推荐多线程 pipeline 使用 EventBasedScheduler。

它公开的性能设计非常典型：

~~~text
dispatcher
↓
internal notification shards
↓
per-worker private ready queue
↓
worker threads

worker queue empty
↓
optional work stealing
↓
scan peer queues
~~~

这意味着设计者已经明确处理一个全局 ready queue 的锁争用、dispatcher notification 热点、worker 负载不均和重复调度路径。

### Sharding 为什么是数据结构问题

如果 N 个 worker 都往一条 internal notification queue 写，会形成 MPSC hot queue 和同一批 cache line/lock contention。

Holoscan 的 EventBasedScheduler 可以把 notification 拆成多个 shard，再由 dispatcher 批量取出事件。这种结构把单一共享队列拆成 sharded queues，并通过 batched drain 降低热点与唤醒开销。

### Linux Scheduler 终于进入应用 Runtime

Event-Based Scheduler 的 dispatcher 可以配置 CPU core，并支持 SCHED_FIFO/SCHED_RR；用户 ThreadPool 还能把特定 Operator 放到指定 core 和实时策略。

因此真实执行链是：

~~~text
condition/event
→ dispatcher bookkeeping
→ ready queue
→ worker wake
→ Linux runqueue
→ CPU scheduling
→ Operator compute
~~~

这正好把 middleware/runtime 与操作系统连起来。

---

## 案例五：Endoscopy / Ultrasound——为什么 Buffer Pool 与 CUDA Stream 也属于通信设计

NVIDIA HoloHub 的参考应用把 Holoscan 放进 Endoscopy Tool Tracking、Multi-AI Ultrasound、Ultrasound Segmentation 等持续视频/Tensor pipeline。

这些应用会显式使用 BlockMemoryPool、CudaStreamPool、GPU inference、post-process 和 visualization。

这说明 GPU pipeline 的容量往往已经变成“多少预分配 buffer slot、多少 CUDA stream、多少 frame 同时 in-flight”，而不只是 `std::queue::size()`。

### 为什么固定 pool 是实时系统常见设计

Unbounded allocator 很方便，但允许 runtime 在负载高峰继续申请资源。固定 BlockMemoryPool 则把系统容量提前确定：

~~~text
capacity known in advance
↓
resource exhaustion exposes backpressure
↓
不会把处理不过来伪装成无限内存增长
~~~

对于具身系统，“延迟越来越大”往往比“明确丢一帧”更危险。

---

## 案例六：rosidl::Buffer——为什么 Accelerator IPC 最后落到 FD、Generation 与 Fence

2026-09-21 的 Isaac ROS 5 把多条 NITROS 数据链迁向 rosidl::Buffer 与 CUDA buffer backend。迁移背景见 {doc}`Isaac ROS 5：NITROS → rosidl::Buffer <../generated/rosidlbuffer/case-study-isaac-ros-5-migration>`；底层 IPC 闭环见 {doc}`Accelerator IPC：CUDA VMM 与 DMA-BUF <../generated/rosidlbuffer/accelerator-ipc-protocols>`。

这次迁移特别适合放进 Communication Foundations，因为它不是简单换 API，而是在重新画**消息语义、memory domain 与 transport**的边界。

旧思路更接近：

~~~text
NVIDIA-specific adapted type
├── GPU representation
├── type negotiation
├── transport
└── builders/views
~~~

新思路则把它拆成：

~~~text
standard message semantics
        ↓
rosidl::Buffer
        ↓
CPU / CUDA / other storage backend
        ↓
per-endpoint capability + fallback
~~~

### 为什么这对数据结构学习很有价值

CUDA backend 并不是“把 vector 换成 device pointer”。固定源码里同时出现：

~~~text
std::map<size, vector<VmmBlock*>>
  → size-class lower_bound + cache-friendly bucket scan

unordered_map<(pid, block_id), CachedImport>
  → imported VMM mapping cache

unordered_map<GID, EndpointInfo>
  → host endpoint locality cache

deque<PendingWork>
  → asynchronous GPU cleanup queue
~~~

同一个项目里四种容器分别服务 allocator、IPC cache、control-plane lookup 与 deferred cleanup；这正好说明“用哪个 STL”只能从访问模式推出来。

### 为什么这也是一个 Linux OS 案例

跨进程 CUDA VMM path 会继续下沉到：

~~~text
CUDA exported POSIX FD
↓
AF_UNIX
↓
sendmsg / SCM_RIGHTS
↓
epoll dispatcher
↓
eventfd shutdown wakeup
↓
subscriber VMM import
~~~

因此 accelerator zero-copy 的底层并没有脱离操作系统，反而更依赖明确的 FD ownership、Reactor、共享内存 metadata 与 shutdown protocol。

Qualcomm dma-buf backend 给出了同一个问题的另一种实现：process-wide FdBroker 使用 `unordered_map<uid, Entry>` 做 capability lookup，用 `deque<uid>` 维护 64-entry rolling window，再通过 `dup + SCM_RIGHTS + mmap` 把同一 dma-buf 映射进 Subscriber。CUDA backend 则利用稳定 VMM block、`(pid, block_id)` import cache、generation UID 与 CUDA IPC event 来摊薄长期 GPU pipeline 的 setup 成本。

两条实现的共同点不是某个 API，而是：

~~~text
stable resource identity
+ generation
+ capability transfer
+ local import/mapping
+ ownership lease
+ completion fence
+ bounded retention
~~~

这套结构可以直接迁移到 Camera DMA、NPU buffer、RDMA registered memory 或自研 VLA Tensor Runtime。

### 最关键的架构变化

真正被标准化的是：

~~~text
message semantic surface
+
Buffer backend contract
~~~

而不是强迫所有 endpoint 使用同一种物理 data path。

同一个 GPU frame 可以对不同消费者分别走 direct CUDA ownership、same-host VMM IPC、CPU fallback 或网络序列化。所谓 zero-copy 因而从“全局模式”变成 **per-endpoint capability decision**。

---

## 跨系统运行时对照

| 系统 | 核心共享状态 | 典型数据结构 | OS / runtime 边界 | 真正要防的问题 |
| --- | --- | --- | --- | --- |
| Apollo Planning | latest auxiliary state + planning snapshot | mutex-protected object、shared_ptr、LocalView | CRoutine → Processor OS thread | data race 消失但跨 topic 快照不一致 |
| ETH soem_interface | process image + bus context | contiguous IOmap、slave collection、mutex | cyclic thread → raw socket/NIC | SDO/diagnosis 阻塞实时周期 |
| ROS 2 RMW/DDS | entity set + History + Conditions | vector、unordered_set、WaitSet、DDS History | DDS threads + RMW listener + Executor | 把所有延迟错怪给 Executor |
| Holoscan EBS | ready operators + event notifications | private ready queues、shards、batch drain | dispatcher/worker → Linux scheduler | hot queue contention、worker imbalance |
| GPU streaming app | in-flight frame/tensor resources | fixed buffer pool、CUDA stream pool | CPU thread + GPU stream/event | allocator jitter、buffer exhaustion、Data Age |
| rosidl::Buffer accelerator backends | VMM/dma-buf + endpoint capability + generation/fence | map<size, vector>、unordered_map cache/table、deque recycler/window | CUDA VMM / dma-buf + shm + AF_UNIX/SCM_RIGHTS/epoll | stale descriptor、过早 recycle、capability miss、fallback 抖动 |
| libuv | fd watcher + timers + async notifications + write requests | fd→watcher 数组、dirty watcher queue、min-heap、intrusive work queue | epoll/eventfd/condition_variable/thread pool | callback starvation、write backlog、close/use-after-free |
| nginx | connection/event slots + request state | free list、reusable queue、timer rbtree、posted intrusive queues、arena/slab | master/worker + epoll + shared mutex/reuseport | stale event、connection exhaustion、slow handler、graceful drain |

这些系统最终都落到同样的运行时问题：状态在哪里、谁拥有、用什么容器、谁修改、谁等待、如何唤醒、资源满了以后如何传播压力，以及关闭时怎样收束在途工作。

---

## libuv：为什么 Event Loop 不是一个 epoll_wait() 循环

libuv 把长期事件源和一次异步动作拆成 Handle/Request，并把 socket readiness、Timer、跨线程 wakeup 与 blocking work 放进不同执行边界。

~~~text
socket / timer / async handle
        ↓
      uv_loop
        ↓
pending → prepare → epoll → check → close → timers

blocking file/DNS/custom work
        ↓
global worker pool
        ↓
loop-local completion queue
        ↓
uv_async_send / eventfd
~~~

fd 这种稠密整数 ID 直接索引 watcher；desired interest 与 kernel-applied interest 分开并延迟批量 `epoll_ctl`；Timer 用 min-heap 把最近 deadline 合并成 poll timeout；跨线程 completion 先发布共享状态，再用 eventfd 只负责唤醒 owner loop。

`uv_write()` 允许未完成 Request 留在 per-stream write queue，`write_queue_size` 统计排队字节，但 pause producer、high-water mark、drop 或断开慢连接仍然属于业务 backpressure policy。

`uv_close()` 也不是立即 free：它只进入 CLOSING 并挂入 closing list，真正回收必须等 event-loop closing phase 与 close callback。这把资源关闭与内存回收分成两个时刻，避免 callback 栈和 pending operation 继续访问已销毁对象。

---

## nginx：为什么大量连接不等于大量线程

nginx 把连接首先按 worker process 分片。每个 worker 内的 connection、read/write event、Timer 与 posted event 主要由一个 event loop thread 修改，因此最常见的 connection-local state 不需要跨线程 mutex。

~~~text
master
├→ worker 0 → epoll + timers + posted events
├→ worker 1 → epoll + timers + posted events
└→ worker N → epoll + timers + posted events
~~~

每个 worker 预分配固定数量的 `ngx_connection_t` 与 read/write event。新 socket 通过 O(1) free-list pop 获得 slot；连接关闭后 slot 回到 free list。当 free slot 低于阈值时，nginx 会从 `reusable_connections_queue` 中选择较老的 keepalive 连接进入协议自己的 close handler，为新连接主动腾资源。

connection slot 被复用时，read/write event 的 `instance` bit 翻转；epoll data.ptr 同时编码 pointer 与这个 generation bit。旧 fd generation 的 ready event 即使迟到，也会因为 instance 不一致而被丢弃。这是 generation handle 在网络 Reactor 中的极简实现。

Timer 则使用 rbtree，而不是 libuv 的 min-heap。更进一步，当新旧 deadline 差不到 `NGX_TIMER_LAZY_DELAY=300 ms` 时，nginx 甚至不重新调整 rbtree，用允许的 timeout 误差换更少的 tree mutation。

内存同样按生命周期拆成两类：request/config 小对象使用 bump-pointer arena，几乎不做单独 free；共享内存里的长期独立对象使用带锁 slab。Allocator 选择因此来自对象 lifetime 与 sharing boundary，而不是只比较 malloc 性能。

---

## 统一运行时剖面

~~~text
1. Long-lived owners
   Runtime / Context / Bus / Participant / Fragment / EventLoop

2. Hot data structures
   ring / deque / map / pool / history / ready queue / timer heap

3. Execution contexts
   OS thread / coroutine / worker / callback / event loop

4. Blocking points
   mutex / condvar / eventfd / epoll / waitset / socket

5. Resource bounds
   queue depth / history depth / pool slots / worker count / write bytes

6. Overload policy
   block / reject / drop-old / latest-only / fault / disconnect

7. Shutdown order
   stop input → drain in-flight → close handles → reclaim resources
~~~

这七项共同决定系统的延迟、可预测性、背压传播和资源生命周期。
