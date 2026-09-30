# Atlas 映射表：把各类中间件放回同一张 Communication Mechanism Map

:::{contents} 本页目录
:depth: 3
:local:
:::

前面的通信基础不是独立知识点。它的用途就是把不同中间件从“API 名称比较”拉回到同一组底层机制。

同样一句：

~~~text
publish(message)
~~~

在不同系统里可能代表：

~~~text
borrow same object
enqueue pointer
loan shared chunk
serialize to UDP datagram
retain in reliable history
route through distributed key space
export device-memory descriptor
~~~

所以真正公平的比较单位不是“有没有 publish/subscribe”，而是**数据路径与所有权路径**。

还可以再往前一步：Atlas 的价值不应该只停在“选一个中间件”。

很多控制程序根本不需要完整 middleware：

~~~text
IMU thread
↓
SPSC ring
↓
Estimator
↓
latest-value mailbox
↓
Controller
~~~

或者：

~~~text
CAN RX
Camera health callback
Planner completion
Watchdog
   ↓
MPSC event queue
   ↓
Supervisor
~~~

这些程序仍然在解决和中间件完全相同的问题：

~~~text
ownership
queue semantics
publication
wakeup
backpressure
shutdown
progress guarantee
~~~

因此每拆一个中间件，都应该问一句：

> **如果我不用这个框架，只写自己的控制/感知 runtime，这个设计还能迁移出什么？**

## 先用六个维度看 Atlas

| 项目 | 主要通信边界 | Payload 机制 | 排队/历史 | 通知/调度 | 异常/过载机制 |
| --- | --- | --- | --- | --- | --- |
| LCM | 跨进程/跨主机 | serialization + UDP multicast | receive queue / fragment state | recv + notify pipe + handle | network loss / bounded receive behavior |
| eCAL | 同机 + 跨机 | SHM / UDP / TCP | transport-specific buffers | callbacks / registration | transport fallback / queue policy |
| Cyber RT | 线程 + 进程 + runtime | intra / SHM / RTPS | cache / pending queue | Dispatcher → Notifier → CRoutine | scheduler backlog / cache policy |
| YARP | 进程 + 跨主机 | Carrier transport | per-connection buffers | connection Unit / callback | carrier-specific flow policy |
| Zenoh | 分布式 | routed key space + transport | async channels / route state | async runtime | congestion control / reconnect |
| Cyclone DDS | 同机 + 跨机 | PSMX/local/RTPS | WHC / RHC / sendq | recv/delivery threads + WaitSet | QoS / reliability / resource limits |
| Fast DDS | 同机 + 跨机 | Data Sharing / SHM / RTPS | WriterHistory / ReaderHistory | receiver / FlowController / WaitSet | ResourceLimits / reliability / backpressure |
| iceoryx2 | 同机跨进程 | shared memory + PointerOffset | per-connection buffers / history | events / listener / reactor | discard/retry/safe overflow + dead-node cleanup |
| IgH / SOEM | 主机 ↔ EtherCAT device | process image + Ethernet frame | cyclic process data | RT application loop + NIC | WKC / slave state / cycle deadline |
| UCX | 线程/进程/跨机/异构 | memory-aware transport | endpoint/worker progress queues | polling/event progress | transport-specific flow / failure |

这张表最重要的不是背结论，而是让你知道下一次看源码应该去找什么对象。

## LCM：最适合看“网络数据面最小闭环”

LCM 的机制链很短：

~~~text
typed message
↓ generated encode
bytes
↓ provider
UDP multicast
↓ receive / reassembly
subscription match
↓ handle()
callback
~~~

因此它特别适合回答：

- serialization 在哪里发生；
- provider/vtable 怎样把 transport 抽掉；
- UDP fragmentation/reassembly 怎么做；
- callback 为什么跟 **handle()** 的调用线程绑定；
- receive queue 满了以后会发生什么。

对应页面：

- [LCM overview](../generated/lcm/overview.md)
- [UDP publish protocol](../generated/lcm/udpm-publish-protocol.md)
- [Receive / reassembly](../generated/lcm/receive-reassembly.md)
- [Subscription dispatch](../generated/lcm/subscription-dispatch.md)

## iceoryx2：最适合看“zero-copy 其实是 ownership protocol”

iceoryx2 的核心不是“用了 mmap”这么简单。

它真正值得看的链：

~~~text
shared-memory allocator/pool
↓
loan
↓
PointerOffset / relocatable identity
↓
zero-copy connection
↓
subscriber borrow
↓
reclaim
↓
dead-node cleanup
~~~

这正对应前面共享内存章节的每一个问题。

继续读：

- [Pool / allocator layout](../generated/iceoryx2/pool-allocator-layout.md)
- [Shared-memory pointer offset](../generated/iceoryx2/shared-memory-pointer-offset.md)
- [Publisher loan](../generated/iceoryx2/publisher-loan.md)
- [Subscriber reclaim](../generated/iceoryx2/subscriber-receive-reclaim.md)
- [Fan-out / backpressure](../generated/iceoryx2/fanout-backpressure-history.md)
- [Dead-node recovery](../generated/iceoryx2/dead-node-recovery.md)

## Fast DDS：最适合看“生产级通信为什么会长出很多状态”

Fast DDS 相比 LCM 多出来的复杂度，不只是“代码更多”。

它要同时维护：

~~~text
Participant / Endpoint lifecycle
Discovery
QoS matching
WriterHistory / ReaderHistory
Reliability
Fragmentation
FlowController
Data Sharing / SHM
WaitSet / Listener
~~~

这说明一个重要事实：

> 当中间件承诺更多语义，就必须保存更多协议状态。

从 Communication Foundations 看，它实际上把：

~~~text
queue
reliability cache
discovery control plane
multiple transport
shared-memory fast path
~~~

组合到一起。

对应：

- [Write / CacheChange](../generated/fastdds/write-cachechange.md)
- [WriterHistory reliability](../generated/fastdds/writerhistory-reliability.md)
- [FlowController](../generated/fastdds/flowcontroller-async.md)
- [Data Sharing vs SHM](../generated/fastdds/datasharing-vs-shm.md)
- [Loan / zero-copy](../generated/fastdds/loan-zero-copy.md)

## Cyclone DDS：可以把同一套 DDS 语义换一个实现重新验证

Cyclone DDS 很适合做“同协议、不同实现”的对照。

你可以继续追：

~~~text
write path
↓
WHC
↓
RTPS network
↓
receive / reorder
↓
RHC
↓
read/take
~~~

同时再看 PSMX/local path 是否绕过传统网络链。

对应：

- [Write path](../generated/cyclonedds/write-path.md)
- [WHC reliability](../generated/cyclonedds/whc-reliability.md)
- [RTPS network](../generated/cyclonedds/rtps-network.md)
- [Receive / reorder](../generated/cyclonedds/receive-reorder.md)
- [RHC read/take](../generated/cyclonedds/rhc-read-take.md)
- [PSMX loans](../generated/cyclonedds/psmx-loans.md)

## Cyber RT：重点不只在“传输”，还在运行时调度

Cyber 的独特之处是：

~~~text
communication
+
task runtime / coroutine scheduler
~~~

因此一条消息除了数据路径，还需要追：

~~~text
Receiver
↓
Dispatcher
↓
Notifier
↓
CRoutine becomes ready
↓
Scheduler
↓
callback/task executes
~~~

这正好验证线程章节里“notify 不等于 callback 立即运行”的结论。

如果目标是学习线程间程序组织，Cyber 目前是 Atlas 中最完整的一组源码案例，可以按：

~~~text
pending_queue_size / CacheBuffer
↓
DataDispatcher
↓
DataNotifier
↓
CRoutine wait/update
↓
ClassicContext condition variable
↓
Processor OS thread
~~~

连续阅读：

- [有界消息缓存与 ring](../generated/cyber/pending-queue-ring.md)
- [Dispatcher / Notifier](../generated/cyber/dispatcher-notifier.md)
- [CRoutine wakeup](../generated/cyber/croutine-wakeup.md)
- [Processor 与上下文切换](../generated/cyber/processor-context-switch.md)

这套链路很适合和 [Thread Communication Lab](thread-dataflow-lab.md) 对照着自己重写一遍。

## Zenoh：通信开始进入分布式路由

Zenoh 不是简单把 DDS transport 换掉。

它把抽象扩展成：

~~~text
key space
declaration
routing
pub/sub
query
storage
~~~

因此要额外分析：

~~~text
session
route state
subscription tree
router hop
reconnect
store/query path
~~~

这已经是“通信 + 分布式数据平面”。

## EtherCAT：它提醒我们通信不一定是 pub/sub

IgH / SOEM 的路径更像：

~~~text
application process image
↓
master frame construction
↓
NIC
↓
EtherCAT slaves
↓
working counter / state
↓
next cycle
~~~

这里最核心的语义不是 topic，而是：

~~~text
固定周期
process data image
deadline
WKC
slave state
~~~

这说明 Communication Foundations 不应该被 pub/sub API 限制。

## 具身系统里更有意义的比较场景

与其问：

~~~text
“哪个中间件最好？”
~~~

不如给出具体数据路径。

### 场景 A：同进程 1 kHz 状态到控制器

优先追：

~~~text
pointer/reference
queue semantics
cache line
scheduler
WCET
~~~

网络协议几乎不重要。

### 场景 B：同机多进程 30 FPS RGB-D

优先追：

~~~text
shared-memory pool
loan
fan-out
descriptor
notification
slow subscriber
crash recovery
~~~

### 场景 C：机器人到远端监控站

优先追：

~~~text
serialization
bandwidth
fragmentation
loss
reliability
routing
clock
reconnect
~~~

### 场景 D：GPU perception → VLA

优先追：

~~~text
GPU residency
host/device copy
buffer pool
CUDA event
cross-process GPU handle
device-aware transport
~~~

同一个中间件在四个场景里的价值可能完全不同。

### 场景 E：根本不使用 Middleware 的本地控制程序

假设是一块 ARM SoC 上的：

~~~text
device thread / ISR
↓
Estimator
↓
Controller
↓
Actuator
~~~

这时应该优先问：

~~~text
SPSC 还是 MPSC？
需要历史还是 latest-only？
固定容量多大？
Producer/Consumer 谁拥有 buffer？
数据提交后靠 polling、event 还是 fixed-rate loop 消费？
shutdown / fault 时谁唤醒谁？
目标架构上的 atomic 是否 lock-free？
DMA 与 CPU cache 的 ownership 怎样交接？
~~~

这说明 Communication Foundations 的终点不是 API，而是**跨平台的数据流设计能力**。

## 一个统一的源码阅读模板

以后进入任何新的通信系统，都按这张顺序图追：

~~~text
1. API entry
   publish / write / send

2. Payload representation
   typed object / serialized bytes / shared chunk / tensor

3. Ownership
   copy / move / borrow / loan

4. Queue/history
   bounded? latest? reliable cache?

5. Notification
   atomic / event / socket / scheduler

6. Transport
   intra / SHM / UDP / TCP / RDMA / device

7. Receive-side buffering
   reorder / history / callback queue

8. Delivery
   which thread/task actually runs?

9. Backpressure
   block / drop / retry / overwrite

10. Failure recovery
    reconnect / dead participant / stale resource
~~~

这十步不是“检查清单式写文档”，而是一条真实的数据生命周期。

当每一步都能在源码里找到对应对象、数据结构和状态转移时，中间件的 communication mechanism 才真正闭环。

## 从机制映射进入真正的工程源码

Atlas Mapping 解决的是“同一个机制在不同系统里叫什么”。

最后还差一步：看真实工程为什么在具体约束下选择这些机制，以及一个错误选择会怎样传导成 latency、stale data、锁竞争或 buffer exhaustion。

这些机制可以直接在跨项目工程案例中对照：

→ [Industry Runtime Cases](industry-runtime-cases.md)
