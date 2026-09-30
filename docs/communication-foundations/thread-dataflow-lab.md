# Thread Communication Lab：从一个 Queue 写到机器人多线程 Runtime

:::{contents} 本页目录
:depth: 3
:local:
:::

这一页不再重复 API，而是通过一组逐层增加约束的小项目，把线程通信真正写成程序组织能力。

最终目标是一条可以迁移到控制、感知、日志、设备驱动甚至嵌入式平台的数据流：

~~~text
Sensor / ISR / Driver
        ↓
bounded channel
        ↓
Estimator
        ↓
latest state
        ↓
Controller
        ↓
event / command channel
        ↓
Supervisor / Actuator
~~~

每个实验都必须回答同样八个问题：

~~~text
1. Producer 有几个？
2. Consumer 有几个？
3. 谁拥有 payload？
4. 哪个变量定义 empty/full/ready？
5. publication point 在哪里？
6. 满了以后 block/drop/overwrite 哪一种？
7. shutdown 时怎么保证没有线程永远睡眠？
8. 怎样证明没有 data race 和 use-after-free？
~~~

如果这八个问题回答不出来，程序“跑起来了”也不能算设计完成。

## Project A：先写一个真正能关闭的 Bounded Blocking Channel

### 场景

~~~text
Logging producers
      ↓
bounded queue
      ↓
disk writer thread
~~~

最容易写出的版本只有：

~~~text
mutex
condition_variable
deque
~~~

但真正值得学的是 **close protocol**。

### 状态与不变量

~~~cpp
struct State {
    std::deque<Message> queue;
    std::size_t capacity;
    bool closed;
};
~~~

共享不变量：

~~~text
0 <= queue.size() <= capacity

closed == true
→ 不再接受新消息

closed && queue.empty()
→ Consumer 可以退出
~~~

### 为什么需要两个条件变量

~~~text
not_empty
not_full
~~~

Producer 在 full 时等待容量，Consumer 在 empty 时等待数据。

这已经让 queue 成为双向 flow-control primitive：

~~~text
Consumer 通过“释放 slot”
把 backpressure 反馈给 Producer
~~~

### close() 应做什么

正确方向：

~~~text
lock
closed = true
unlock
notify_all(not_empty)
notify_all(not_full)
~~~

为什么是 notify_all？

因为可能同时存在：

~~~text
多个 Producer 睡在 not_full
多个 Consumer 睡在 not_empty
~~~

如果关闭只改 bool 不唤醒，它们不会自动获得 CPU 检查新状态。

### 实验

故意制造：

1. capacity=2；
2. Producer 每 1 ms 产生一条；
3. Consumer 每 10 ms 处理一条；
4. 运行 1 s 后调用 close；
5. 记录 Producer blocked time；
6. 验证所有线程都能 join。

这里第一次把：

~~~text
capacity
backpressure
condition variable
shutdown
~~~

真正连起来。

## Project B：把 Sensor → Estimator 改成 SPSC Ring

### 场景

~~~text
IMU thread 1 kHz
     ↓
Estimator thread
~~~

只有一个 Producer、一个 Consumer。

这时一把全局 mutex 可以工作，但我们故意把拓扑约束利用起来。

### 数据结构

~~~cpp
template<class T, std::size_t N>
class SpscRing {
    std::array<T, N> slots_;
    std::atomic<std::uint64_t> head_{0};
    std::atomic<std::uint64_t> tail_{0};
};
~~~

约定：

~~~text
head = next write logical position
tail = next read logical position
~~~

只有 Producer 写 head，只有 Consumer 写 tail。

### Producer

~~~cpp
bool try_push(const T& value) {
    auto head = head_.load(std::memory_order_relaxed);
    auto tail = tail_.load(std::memory_order_acquire);

    if (head - tail == N) {
        return false;
    }

    slots_[head % N] = value;

    head_.store(head + 1, std::memory_order_release);
    return true;
}
~~~

### Consumer

~~~cpp
bool try_pop(T& value) {
    auto tail = tail_.load(std::memory_order_relaxed);
    auto head = head_.load(std::memory_order_acquire);

    if (tail == head) {
        return false;
    }

    value = slots_[tail % N];

    tail_.store(tail + 1, std::memory_order_release);
    return true;
}
~~~

### 为什么 memory order 这样放

Producer：

~~~text
write payload
↓
release head
~~~

Consumer：

~~~text
acquire head
↓
read payload
~~~

Consumer 不能在 Producer 正式发布 head 之前读那个 slot。

反方向：

~~~text
Consumer read complete
↓
release tail

Producer acquire tail
↓
知道 slot 可以复用
~~~

这是一套完整 ownership handoff。

### 实验

分别测试：

~~~text
capacity = 1 / 2 / 8 / 64
Consumer stall = 0 / 1 / 5 ms
~~~

记录：

~~~text
drop count
max lag
p99 age
CPU usage
~~~

再把 head 和 tail 故意放在同一 cache line，然后用 padding 分开，比较高频下的差异。

## Project C：多 Callback 汇聚到一个 Supervisor：MPSC Queue

### 场景

~~~text
CAN RX thread --------\
camera health callback --\
planner callback ---------+--> Supervisor
watchdog callback -------/
~~~

多个 Producer 都想把 event 写给一个 Consumer。

### 先写错误版本

~~~cpp
auto pos = head++;
slots[pos % N] = event;
~~~

问自己：

> 两个 Producer 同时读 head 怎么办？

然后自然逼出 atomic reservation。

### 第二版：只有 fetch_add 仍然不够

~~~cpp
auto pos = head.fetch_add(1);
slots[pos % N] = event;
~~~

再制造：

~~~text
P0 reserve 10
P0 被抢占

P1 reserve 11
P1 写完
~~~

Consumer 能不能直接相信 head==12？

不能，因为 slot 10 还没有真正发布。

### 第三版：每槽 sequence

~~~cpp
struct Slot {
    std::atomic<std::uint64_t> sequence;
    Event event;
};
~~~

把 queue 分成：

~~~text
reservation
payload write
publication
reclaim
~~~

四个阶段。

最重要的产物不是“一个最快的 MPSC”，而是一张状态机：

~~~text
FREE_FOR_p
   ↓ producer owns
WRITING_p
   ↓ release publish
READY_p
   ↓ consumer owns
READING_p
   ↓ release recycle
FREE_FOR_(p+N)
~~~

只要这张图能讲清楚，之后去读任何 bounded concurrent queue 都有抓手。

### 再测竞争

让 2、4、8 个 Producer 同时经过 barrier 后 enqueue，记录：

~~~text
CAS / reservation retry
enqueue latency
consumer lag
cache miss
~~~

这会让“多 Producer 竞争一个热点状态”从抽象概念变成可观察现象。

## Project D：Controller 根本不想排队——Versioned Latest Mailbox

### 场景

~~~text
Estimator 200 Hz
    ↓
Controller 1 kHz
~~~

Controller 每周期只关心：

~~~text
“现在最新的状态是什么？”
~~~

所以 FIFO queue 不是自然语义。

### 从双缓冲开始

~~~text
buffer[0]
buffer[1]
active_index
~~~

Writer 永远写 inactive buffer，写完以后再原子切 active index。

进一步再做 versioned snapshot / seqlock：

~~~text
version odd
→ writer 正在改

version even
→ snapshot 稳定
~~~

Reader：

~~~text
v0 = version
if odd: retry

copy state

v1 = version
if v0 != v1: retry
~~~

### 要观察的现象

比较：

~~~text
深 FIFO
latest mailbox
double buffer
versioned snapshot
~~~

在 Estimator 短暂卡顿与 Controller 高频读取下的：

~~~text
data age
读取一致性
历史保留
retry 次数
~~~

你会非常直观地看到：

> “不丢消息”并不是控制系统的最高目标。

## Project E：Data Channel 和 Wakeup Channel 分离

前几个项目如果 Consumer 不 busy-poll，就需要等待。

这一项目在 Linux 上使用：

~~~text
eventfd
epoll
timerfd
~~~

建立：

~~~text
payload:
SPSC/MPSC queue

notification:
eventfd

periodic deadline:
timerfd

shutdown:
another eventfd
~~~

最终一个线程：

~~~text
epoll_wait()
   ├── sensor ready
   ├── command ready
   ├── timer tick
   └── shutdown
~~~

### 为什么 payload 不直接塞进 eventfd

eventfd 只负责：

~~~text
“有事情发生了”
~~~

真正 payload 仍在 queue/ring/mailbox。

这就是：

~~~text
data path
!=
control / notification path
~~~

Cyber 的 Dispatcher + Notifier、iceoryx2 的 payload + Event/Reactor 都是同一个思想。

### 必须实验的 Lost Wakeup

故意写一个错误流程：

~~~text
Consumer check queue empty
        ↓
Producer enqueue + notify
        ↓
Consumer 才开始 sleep
~~~

看它如何错过事件。

然后用：

~~~text
predicate / counter
arm-before-sleep
~~~

消除 race。

这一步会直接帮助理解 condition_variable 和 UCX worker_arm + epoll_wait 为什么都有固定顺序。

## Project F：Mini Executor：从 Queue 进入任务调度

到这里已经有：

~~~text
data storage
notification
~~~

再加入：

~~~text
task ready queue
worker threads
~~~

形成最小 Executor。

### 第一版

~~~text
MPSC ready queue
↓
one worker
~~~

### 第二版

~~~text
MPMC ready queue
↓
N workers
~~~

观察：

~~~text
多个 worker 为什么开始争 dequeue
CAS contention 从哪里来
~~~

### 第三版：Per-worker Queue + Stealing

~~~text
Worker0 -> local deque
Worker1 -> local deque
Worker2 -> local deque

idle worker
   ↓
steal from others
~~~

这会让你理解为什么大型 runtime 往往不坚持“一只全局 MPMC queue”。

### 再加 priority

不要直接把 priority queue 当答案。

要追问：

~~~text
高优先级 task 已 ready
但 worker 正执行低优先级长任务
怎么办？
~~~

如果 callback 不可抢占：

~~~text
priority 只能影响“下一个选谁”
~~~

这正是读 Cyber Scheduler 时必须区分的地方。

## Project G：把前面所有结构组装成机器人 Dataflow

最终做一个没有 ROS/DDS 依赖的小 runtime：

~~~text
                    ┌──────────── Logger
                    │               ▲
Camera thread ─SPSC─┤               │ MPSC telemetry
                    ▼               │
              Perception thread     │
                    │               │
                    │ latest object │
                    ▼               │
               Planner thread ──────┘
                    │
                    │ latest command
                    ▼
              Control thread 1 kHz
                    │
                    ▼
               Actuator mock

IMU thread ─SPSC──────────────► Estimator
                                  │
                                  └─ versioned RobotState
                                         │
                                         ▼
                                    Control thread
~~~

这里不要追求功能复杂，而要把每一条边写出协议。

| 边 | 数据结构 | 容量语义 | 满时策略 | 唤醒 |
| --- | --- | --- | --- | --- |
| Camera → Perception | SPSC ring | 2～3 frames | drop-old | eventfd |
| IMU → Estimator | SPSC ring | bounded history | drop-old / count loss | polling/event |
| Estimator → Controller | latest mailbox | 1 snapshot | overwrite | Controller fixed-rate read |
| Planner → Controller | latest command | 1 command | overwrite + deadline | fixed-rate read |
| Any → Logger | MPSC queue | large but bounded | block/drop with accounting | condition variable |
| Any → Supervisor | MPSC event queue | bounded | critical event 不可静默丢失 | eventfd |

这张表本身就是程序架构。

## 在项目里故意制造失败

并发程序最容易因为“正常运行”而产生错误信心。

### Producer 过快

~~~text
lambda > mu
~~~

观察：

~~~text
block
drop
age
~~~

### Consumer 卡住

~~~text
sleep 50 ms
~~~

观察恢复后是补历史、跳最新，还是整个上游一起阻塞。

### Shutdown 正好发生在等待窗口

重复：

~~~text
100000 次 start/stop
~~~

寻找：

~~~text
lost wakeup
join hang
use-after-free
~~~

### 多 Producer 同时冲击

barrier 后同时 enqueue，放大 CAS contention。

### CPU affinity

把 Producer/Consumer：

~~~text
绑同核
绑不同核
跨 NUMA
~~~

观察 cache-coherence 与 scheduler 差异。

## 工具不是答案，但应该用来证伪

Linux/C++ 项目至少可以使用：

~~~text
ThreadSanitizer
AddressSanitizer
perf
perf stat
perf sched
~~~

关注：

~~~text
context switches
cache misses
cycles
CAS retry count
queue high-water mark
wakeup latency
~~~

不要只 benchmark messages/s。

机器人系统更关心：

~~~text
p99 / max data age
deadline miss
shutdown correctness
~~~

## 如何映射回已经拆解的中间件源码

### Project B ↔ Cyber CacheBuffer

- [pending queue ring](../generated/cyber/pending-queue-ring.md)

对比自己写的 SPSC ring 与 Cyber 的“每 DataVisitor 独立 ring + mutex”。重点思考为什么 Cyber 没必要为了“无锁”把状态机复杂化。

### Project E ↔ Cyber Notifier / iceoryx2 Reactor

- [Cyber Dispatcher / Notifier](../generated/cyber/dispatcher-notifier.md)
- [Cyber CRoutine wakeup](../generated/cyber/croutine-wakeup.md)
- [iceoryx2 Thread Safety / Reactor](../generated/iceoryx2/thread-safety-event-reactor.md)

这里验证：

~~~text
先提交数据
再通知
通知只表达需要重新检查
~~~

### Project F ↔ Fast DDS / Cyber Scheduler

- [Fast DDS FlowController](../generated/fastdds/flowcontroller-async.md)
- [Cyber Processor/context switch](../generated/cyber/processor-context-switch.md)

看 runtime 如何组织：

~~~text
pending work
priority
worker
wake
shutdown
~~~

### Project E/F ↔ UCX Progress Engine

- [UCX Progress Engine](../generated/ucx/progress-engine.md)

比较：

~~~text
dedicated communication thread
业务线程主动 progress
busy polling
event-driven sleep
~~~

这里会发现“通信”最后仍然落回 CPU 调度与程序组织。

## 跨平台迁移时保留什么，替换什么

应该保留：

~~~text
Producer/Consumer topology
slot ownership
bounded capacity
sequence/generation
overflow policy
publication-before-notify
shutdown state machine
~~~

可以替换：

~~~text
std::condition_variable
→ RTOS semaphore / event flag

eventfd/epoll
→ RTOS queue set / event group

std::atomic
→ 架构原子 / 短临界区

Linux thread
→ RTOS task / ISR + task

heap payload
→ static pool / DMA buffer
~~~

于是同一套设计能力可以从桌面 Linux 一直迁移到 ARM SoC、RTOS 甚至部分裸机数据路径。

真正跨平台的不是 API，而是：

> **状态机、所有权、容量和时序。**

## 地址空间一分开，刚刚成立的假设会再次失效

到这里，同一进程内的线程通信已经基本闭环：

~~~text
ownership
→ memory ordering
→ queue semantics
→ concurrent container
→ wakeup/executor
~~~

但一旦把 Estimator 与 Controller 拆成两个进程，最关键的假设消失了：

~~~text
同一个指针值
不再天然指向同一个对象
~~~

allocator、裸指针、对象构造、崩溃回收都要重新设计。

所以下一层边界进入：

→ [Processes & Shared Memory](processes-shared-memory.md)
