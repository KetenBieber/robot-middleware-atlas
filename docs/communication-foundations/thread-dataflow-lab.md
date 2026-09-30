# Thread Communication Lab：从一个 Queue 写到机器人多线程 Runtime

:::{contents} 本页目录
:depth: 3
:local:
:::


本页代码约定更严格：所有标成 ``cpp`` 的代码块都是单文件最小可运行程序，包含必要的 ``#include``、共享对象和 ``main()``；算法推导、错误写法和状态机草图统一使用 ``text``。这样读者不需要猜某个片段还缺哪些对象、线程入口或生命周期代码。
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

### 先运行一个完整版本

这一节不再只展示 `State` 结构体。先把 bounded、blocking、close、join 全部放进同一个最小程序。

编译运行：

~~~text
g++ -std=c++17 -O2 -pthread bounded_channel_demo.cpp -o bounded_channel_demo
./bounded_channel_demo
~~~

~~~cpp
#include <chrono>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <thread>

template<class T>
class BoundedChannel {
public:
    explicit BoundedChannel(std::size_t capacity)
        : capacity_(capacity) {}

    bool push(T value) {
        std::unique_lock<std::mutex> lock(m_);

        not_full_.wait(lock, [&] {
            return closed_ || queue_.size() < capacity_;
        });

        if (closed_) {
            return false;
        }

        queue_.push_back(std::move(value));

        lock.unlock();
        not_empty_.notify_one();
        return true;
    }

    bool pop(T& out) {
        std::unique_lock<std::mutex> lock(m_);

        not_empty_.wait(lock, [&] {
            return closed_ || !queue_.empty();
        });

        if (queue_.empty()) {
            return false;
        }

        out = std::move(queue_.front());
        queue_.pop_front();

        lock.unlock();
        not_full_.notify_one();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(m_);
            closed_ = true;
        }

        not_empty_.notify_all();
        not_full_.notify_all();
    }

private:
    const std::size_t capacity_;

    std::mutex m_;
    std::condition_variable not_empty_;
    std::condition_variable not_full_;
    std::deque<T> queue_;
    bool closed_ = false;
};

int main() {
    BoundedChannel<int> channel(2);

    std::thread consumer([&] {
        int value = 0;

        while (channel.pop(value)) {
            std::cout << "consume " << value << "\n";

            std::this_thread::sleep_for(
                std::chrono::milliseconds(10));
        }

        std::cout << "consumer exits\n";
    });

    std::thread producer_a([&] {
        for (int i = 0; i < 10; ++i) {
            if (!channel.push(100 + i)) {
                return;
            }
        }
    });

    std::thread producer_b([&] {
        for (int i = 0; i < 10; ++i) {
            if (!channel.push(200 + i)) {
                return;
            }
        }
    });

    producer_a.join();
    producer_b.join();

    channel.close();
    consumer.join();
}
~~~

对象关系只有一套：

~~~text
BoundedChannel<int> channel
        |
        +-- one mutex
        +-- one deque
        +-- one not_empty cv
        +-- one not_full cv
        +-- one closed flag
             ^
             |
      all producer/consumer threads
~~~

### 不变量

~~~text
0 <= queue.size() <= capacity

closed == true
→ 不再接受新消息

closed && queue.empty()
→ Consumer 可以退出
~~~

### 为什么需要两个 Condition Variable

`not_empty` 等“有数据”；`not_full` 等“有容量”。

Producer 在 full 时等待 Consumer 释放 slot；Consumer 在 empty 时等待 Producer 发布数据。这使 bounded queue 同时承担数据容器和 flow-control 边界。

### close() 为什么必须 notify_all

可能有多个 Producer 睡在 `not_full`，也可能有 Consumer 睡在 `not_empty`。只把 `closed_=true` 写进内存并不会自动让睡眠线程重新获得 CPU。

所以关闭协议是：

~~~text
lock
closed = true
unlock

notify_all(not_empty)
notify_all(not_full)
~~~

这个例子最重要的不是 Queue API，而是第一次把：

~~~text
capacity
backpressure
condition variable
shutdown
join
~~~

真正闭环起来。


## Project B：把 Sensor → Estimator 改成 SPSC Ring

### 场景

~~~text
IMU thread 1 kHz
     ↓
Estimator thread
~~~

只有一个 Producer、一个 Consumer。

这时一把全局 mutex 可以工作，但我们故意把拓扑约束利用起来。

### 先运行一个完整 SPSC Ring

这里明确利用拓扑约束：

~~~text
exactly one Producer
exactly one Consumer
~~~

编译运行：

~~~text
g++ -std=c++17 -O2 -pthread spsc_ring_demo.cpp -o spsc_ring_demo
./spsc_ring_demo
~~~

~~~cpp
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <thread>

template<class T, std::size_t N>
class SpscRing {
public:
    bool try_push(const T& value) {
        const auto head =
            head_.load(std::memory_order_relaxed);

        const auto tail =
            tail_.load(std::memory_order_acquire);

        if (head - tail == N) {
            return false;
        }

        slots_[head % N] = value;

        head_.store(
            head + 1,
            std::memory_order_release);

        return true;
    }

    bool try_pop(T& out) {
        const auto tail =
            tail_.load(std::memory_order_relaxed);

        const auto head =
            head_.load(std::memory_order_acquire);

        if (tail == head) {
            return false;
        }

        out = slots_[tail % N];

        tail_.store(
            tail + 1,
            std::memory_order_release);

        return true;
    }

private:
    std::array<T, N> slots_{};

    // 只有 Producer 写 head_。
    std::atomic<std::uint64_t> head_{0};

    // 只有 Consumer 写 tail_。
    std::atomic<std::uint64_t> tail_{0};
};

int main() {
    SpscRing<int, 8> ring;

    std::atomic<bool> done{false};
    std::atomic<int> dropped{0};

    std::thread producer([&] {
        for (int i = 1; i <= 100; ++i) {
            if (!ring.try_push(i)) {
                dropped.fetch_add(
                    1,
                    std::memory_order_relaxed);
            }

            std::this_thread::sleep_for(
                std::chrono::milliseconds(1));
        }

        done.store(
            true,
            std::memory_order_release);
    });

    std::thread consumer([&] {
        int value = 0;
        int consumed = 0;

        while (true) {
            if (ring.try_pop(value)) {
                ++consumed;

                std::this_thread::sleep_for(
                    std::chrono::milliseconds(3));

                continue;
            }

            if (done.load(
                    std::memory_order_acquire)) {
                break;
            }

            std::this_thread::yield();
        }

        std::cout
            << "consumed=" << consumed
            << " dropped=" << dropped.load()
            << "\n";
    });

    producer.join();
    consumer.join();
}
~~~

### 为什么这个 Ring 能不用一把全局 mutex

约定是：

~~~text
head = next write logical position
tail = next read logical position
~~~

并且：

~~~text
Producer only writes head
Consumer only writes tail
~~~

Producer 写 payload 后，用 release-store 发布新的 head；Consumer acquire-load head 后，才读取对应 payload。

反方向也是一样：Consumer 完成读取后 release-store tail；Producer acquire-load tail 后，才把对应 slot 当成可复用。

~~~text
Producer:
write payload
↓
release head

Consumer:
acquire head
↓
read payload
↓
release tail

Producer:
acquire tail
↓
reuse slot
~~~

这个程序故意让 Consumer 比 Producer 慢，所以 `try_push()` 会返回 false，`dropped` 会增长。这里的 overflow policy 是 **drop-new**；如果业务要求 drop-old、block 或 latest-only，Ring 协议必须相应改变，不能只改一个注释。

### 实验

再分别测试：

~~~text
capacity = 2 / 8 / 64
Consumer stall = 0 / 1 / 5 ms
~~~

记录：

~~~text
drop count
max lag
p99 age
CPU usage
~~~


## Project C：多 Callback 汇聚到一个 Supervisor：MPSC Queue

### 场景

~~~text
CAN RX thread --------\
camera health callback --\
planner callback ---------+--> Supervisor
watchdog callback -------/
~~~

多个 Producer 都想把 event 写给一个 Consumer。

### 第一版先写一个正确的 MPSC 基线

在研究 atomic reservation 前，先确认业务拓扑：

~~~text
many Producers
     |
     v
one shared queue
     |
     v
one Consumer
~~~

最容易证明正确的版本仍然是 mutex + condition variable。

编译运行：

~~~text
g++ -std=c++17 -O2 -pthread mpsc_baseline.cpp -o mpsc_baseline
./mpsc_baseline
~~~

~~~cpp
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct Event {
    int producer_id;
    int sequence;
};

class MpscQueue {
public:
    void push(Event e) {
        {
            std::lock_guard<std::mutex> lock(m_);
            queue_.push_back(e);
        }

        cv_.notify_one();
    }

    bool pop(Event& out) {
        std::unique_lock<std::mutex> lock(m_);

        cv_.wait(lock, [&] {
            return closed_ || !queue_.empty();
        });

        if (queue_.empty()) {
            return false;
        }

        out = queue_.front();
        queue_.pop_front();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(m_);
            closed_ = true;
        }

        cv_.notify_all();
    }

private:
    std::mutex m_;
    std::condition_variable cv_;
    std::deque<Event> queue_;
    bool closed_ = false;
};

int main() {
    MpscQueue queue;

    std::thread consumer([&] {
        Event e{};

        while (queue.pop(e)) {
            std::cout
                << "producer=" << e.producer_id
                << " seq=" << e.sequence
                << "\n";
        }
    });

    std::vector<std::thread> producers;

    for (int producer_id = 0;
         producer_id < 4;
         ++producer_id) {
        producers.emplace_back(
            [&, producer_id] {
                for (int seq = 0; seq < 10; ++seq) {
                    queue.push(
                        Event{producer_id, seq});
                }
            });
    }

    for (auto& producer : producers) {
        producer.join();
    }

    queue.close();
    consumer.join();
}
~~~

这个版本已经回答了最重要的问题：四个 Producer 访问的是**同一个** `MpscQueue queue`，所有 push 由同一把 `m_` 串行化，Consumer 只有一个。

### 为什么普通 head++ 不够

下面开始是**机制伪代码，不是完整可运行程序**：

~~~text
pos = head++
slots[pos % N] = event
~~~

两个 Producer 可以同时读到旧 head，于是都认为自己拿到了同一个 slot。

### 只有 fetch_add 也还不够

~~~text
pos = head.fetch_add(1)
slots[pos % N] = event
~~~

atomic fetch_add 可以让 Producer 拿到不同 ticket，但 ticket 只证明：

~~~text
reservation succeeded
~~~

它不证明对应 slot 的 payload 已经写完。

制造时序：

~~~text
P0 reserve slot 10
P0 被 OS 抢占

P1 reserve slot 11
P1 写完 slot 11

global head == 12
~~~

Consumer 不能因为 head==12 就假设 slot 10、11 都已经 READY。

### 所以需要 per-slot publication state

概念结构：

~~~text
Slot {
    sequence
    payload
}
~~~

状态机：

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

完整的 per-slot sequence / lock-free MPSC 推导放在 [并发队列与进展保证](concurrent-queues-progress.md)。本 Lab 到这里的目标不是让你复制一个半成品 lock-free queue，而是先看清：

~~~text
reservation
!=
publication
!=
reclamation
~~~

### 再测竞争

让 2、4、8 个 Producer 同时启动，对比：

~~~text
mutex baseline latency
CAS / reservation retry
enqueue latency
consumer lag
cache miss
~~~

这样“多 Producer 竞争一个热点状态”才会从名词变成可观察现象。


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
