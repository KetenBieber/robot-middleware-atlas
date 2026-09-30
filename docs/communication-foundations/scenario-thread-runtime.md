# 场景设计四：从“一组件一线程”到真正的多线程 Runtime

## 场景

机器人程序逐渐长成：

~~~text
Camera
LiDAR
IMU
Localization
Perception
Planning
Control
Network
Logger
Supervisor
~~~

最直接的组织方式是：

~~~text
一个模块一个 std::thread
~~~

小系统里完全可行。

问题在模块数量、任务频率和 blocking pattern 增长以后出现。


## 先把 Runtime 里的五个基本名词分开

### Component

Component 是业务模块，例如 Perception、Planning、Localization、Logger。它是逻辑概念，**Component 不等于 Thread**。

### OS Thread

`std::thread` 最终对应操作系统可调度执行实体。线程有自己的 stack、register context、scheduler state、priority 与 CPU affinity。操作系统真正调度的是 thread，不是你的 Perception 类。

### Task

Task 是“一次可执行工作”，例如处理一帧图像、执行一次 timer callback、处理一次 network completion、写一个 log batch。

一个 Component 可以不断产生很多 Task。

### Worker

Worker 是专门从任务队列中取 Task 并执行的线程。

~~~text
Task Queue
   |
   +--> Worker 0
   +--> Worker 1
   +--> Worker 2
~~~

### Execution Context

Execution Context 表示：**这段工作在哪一类执行环境中运行，它受到什么线程、调度、优先级和串行化约束。**

~~~text
Control execution context
    = dedicated 1 kHz RT thread

Network execution context
    = event-loop owner thread

Background execution context
    = shared worker pool
~~~

所以“十个 Component”并不意味着“十个 Thread”。

---


## 为什么线程太多会有成本

每个线程都要维护自己的 stack 和调度状态；当 runnable threads 明显多于 CPU cores 时，OS 还要频繁决定谁运行。

Context switch 时需要保存和恢复寄存器、program counter、stack pointer 等状态。若线程工作集不同，还可能引入 cache/TLB 扰动。

因此“一模块一线程”不是错误，只是在模块数和 workload 增长后，不一定再是最经济的映射。

---


## Naive 方案：一组件一线程

优点：

~~~text
ownership 清楚
调试直观
模块互不抢同一个 task queue
~~~

代价：

~~~text
thread 数不断增长
context switch 增加
每个线程都需要 stack
CPU affinity/priority 难统一
大量线程其实长期 sleep
~~~

如果 50 个逻辑组件只偶尔执行一次 callback，50 个 OS thread 可能并不划算。

---


## 第一步：先区分 Execution Context，而不是先数模块

把工作分成：

### 固定周期、强时限

例如：

~~~text
1 kHz controller
motor bus
safety monitor
~~~

更适合 dedicated RT thread / static cyclic execution。

### 高频事件驱动计算

例如：

~~~text
perception callbacks
message processing
network completion
~~~

适合 worker pool / executor。

### 低频 control plane

例如：

~~~text
configuration
diagnostics
service calls
~~~

可走普通 event loop / shared pool。

不要让三类任务共享完全相同的执行策略。

---


## MPMC、MPSC、SPSC 到底是什么

它们描述 Queue 的 producer/consumer topology：

~~~text
SPSC = Single Producer / Single Consumer
MPSC = Multiple Producer / Single Consumer
SPMC = Single Producer / Multiple Consumer
MPMC = Multiple Producer / Multiple Consumer
~~~

例如 Camera Thread → Inference Thread 是典型 SPSC；多个 sensor callback → 一个 Supervisor 更像 MPSC。

Topology 会直接决定 Queue 需要承担多少同步复杂度。

---


## Global Task Queue 为什么是第一个自然抽象

~~~text
Producer callbacks
      ↓
global MPMC queue
      ↓
W0 W1 W2 W3
~~~

优点：

~~~text
thread 数固定
负载可以共享
实现简单
~~~

当 worker 少、任务粒度大时，这个方案通常很好。

---


## 一个可以直接运行的最小 Worker Pool

下面不是工业 ThreadPool，而是把对象关系讲清楚的最小版本。

编译运行：

~~~text
g++ -std=c++17 -O2 -pthread thread_pool_demo.cpp -o thread_pool_demo
./thread_pool_demo
~~~


~~~cpp
#include <atomic>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>

class ThreadPool {
public:
    explicit ThreadPool(int n) {
        for (int i = 0; i < n; ++i) {
            workers_.emplace_back([this, i] {
                worker_loop(i);
            });
        }
    }

    void post(std::function<void()> task) {
        {
            std::lock_guard<std::mutex> lock(m_);
            tasks_.push(std::move(task));
        }
        cv_.notify_one();
    }

    ~ThreadPool() {
        {
            std::lock_guard<std::mutex> lock(m_);
            stopping_ = true;
        }
        cv_.notify_all();
        for (auto& t : workers_) {
            t.join();
        }
    }

private:
    void worker_loop(int id) {
        while (true) {
            std::function<void()> task;

            {
                std::unique_lock<std::mutex> lock(m_);
                cv_.wait(lock, [&] {
                    return stopping_ || !tasks_.empty();
                });

                if (stopping_ && tasks_.empty()) {
                    return;
                }

                task = std::move(tasks_.front());
                tasks_.pop();
            }

            {
                // 只保护终端输出，避免多个 worker 的字符交错。
                // 它不是 task queue 的同步锁。
                std::lock_guard<std::mutex> output_lock(output_m_);
                std::cout << "worker " << id << " runs task\n";
            }
            task();
        }
    }

    std::mutex m_;
    std::mutex output_m_;
    std::condition_variable cv_;
    std::queue<std::function<void()>> tasks_;
    std::vector<std::thread> workers_;
    bool stopping_ = false;
};

int main() {
    std::atomic<int> completed{0};

    {
        ThreadPool pool(3);
        for (int i = 0; i < 8; ++i) {
            pool.post([&completed] {
                completed.fetch_add(1, std::memory_order_relaxed);
            });
        }
    }

    std::cout << "completed=" << completed.load() << "\n";
}
~~~

这里 `m_ / cv_ / tasks_` 都只有一份，三个 Worker 线程共享它们，所以这就是一个最小的 global MPMC task queue。`output_m_` 只为了让教学程序的终端输出不交错，不属于任务调度协议。

---


## 什么叫 Contention 和 Cache-Line Bouncing

假设四个 Worker 都操作同一把 mutex：

~~~text
W0 --\
W1 ----> mutex -> global queue
W2 --/
W3 -/
~~~

同一时刻只能一个进入临界区，其余线程等待、重试或睡眠，这叫 lock contention。

换成 atomic/CAS 也不等于竞争消失。如果所有核心都反复修改同一个 atomic head，它所在的 cache line 会在核心之间频繁转移 ownership，这就是 cache-line bouncing。

CAS 是 Compare-And-Swap / Compare-And-Exchange：只有当共享变量仍等于“我刚才观察到的旧值”时才把它改成新值；如果别人先改过，就失败并重试。它能避免某些 mutex，但高竞争下仍可能产生大量 retry 和 cache-coherence 流量。

> **lock-free 不等于没有硬件级共享成本。**

Per-worker queue / shard-per-core 的价值之一，就是减少共享写热点。

---



## Global Queue 什么时候变成热点

worker 数增加后：

~~~text
all workers
→ same head/tail
→ same mutex/atomic/cache line
~~~

产生：

- mutex contention；
- CAS retry；
- cache-line bouncing；
- wakeup herd。

这时可以考虑 sharding/per-worker queue。

---


## Per-Worker Queue：用局部性换负载均衡难度

~~~text
Dispatcher
├→ Q0 → W0
├→ Q1 → W1
├→ Q2 → W2
└→ Q3 → W3
~~~

优点：

~~~text
worker 多数时候只碰自己的 queue
cache locality 更好
共享热点减少
~~~

新问题：

~~~text
Q0 overloaded
Q2 empty
~~~

所以进一步出现 work stealing。

---


## Work Stealing 到底“偷”的是什么

Per-worker Queue 以后，每个 Worker 主要从自己的 Queue 取任务。如果 W0 的 Q0 很满，而 W1 的 Q1 为空，W1 可以从 Q0 取走**尚未开始执行**的 Task。

它不会中断 W0 当前正在执行的 callback。

因此：

> **Work Stealing 解决的是待执行工作负载不均，不是实时抢占。**

---


## Work Stealing 什么时候值得

不规则任务：

~~~text
some inference task = 2 ms
some task = 20 ms
~~~

静态分配容易不均。

空闲 worker 可以从其他 queue 偷任务。

适合：

~~~text
throughput-oriented
任务耗时变化大
任务可在不同 worker 执行
~~~

不适合：

~~~text
严格 thread affinity
cache-local state 很重
hard RT fixed schedule
~~~

Holoscan EventBasedScheduler 的 per-worker queue + optional stealing 就是工业实例。

---


## Runtime 不能只设计 Queue，还要设计 Wakeup

最差方式：

下面只是控制流伪代码，故意不是可编译 C++：

~~~text
while (!stop) {
    if (queue.try_pop(task)) run(task);
}
~~~

没任务时持续烧 CPU。

另一极端：

~~~text
sleep 10 ms
~~~

会制造额外调度延迟。

候选：

~~~text
condition_variable
semaphore
eventfd/epoll
futex
runtime-specific notifier

这些 primitive 的等待边界不同：condition_variable 等待受 mutex 保护的 predicate 变化；semaphore 维护可消费计数；eventfd + epoll 把跨线程 wakeup 变成 fd readiness，适合并入 Event Loop；futex 是 Linux 的低层“用户态原子值 + 必要时进入内核睡眠”机制，很多 mutex/condition-variable 实现会在更底层借助它。
~~~

核心不变量：

~~~text
publish work
before
notify sleeping worker
~~~

并且 Consumer 醒来后仍要检查共享 predicate。

---


## Dispatcher 为什么经常独立存在

如果 readiness 不是简单 queue non-empty，而是：

~~~text
input ready
AND output capacity
AND timer
AND memory available
~~~

需要一个 control-plane actor 统一重新评估任务状态。

于是出现：

~~~text
events
↓
dispatcher
↓
ready queues
↓
workers
~~~

Holoscan 是典型。

Cyber 则使用 DataNotifier → Scheduler → Processor/CRoutine 路线。

---


## Coroutine 为什么会出现

如果 task 经常：

~~~text
wait message
wait timer
yield
resume
~~~

每个逻辑任务都占一个 pthread 会浪费 OS thread。

Coroutine 可以：

~~~text
many logical execution flows
share fewer OS workers
~~~

但它不是免费：

- context/stack 管理；
- scheduler complexity；
- blocking syscall 会阻塞底层 worker；
- thread-local assumptions 可能失效。

Cyber CRoutine 正好是这类设计。

---


## ROS Executor 为什么常被误解

ROS Node/Callback Group 是逻辑组织。

真正执行还要经过：

~~~text
DDS receive/history
↓
RMW readiness
↓
WaitSet
↓
Executor
↓
worker
~~~

所以 callback latency 不能只怪 Executor。

需要画完整线程图。

---


## OS Scheduler 是 Runtime 的最后一层

Runtime 选中 task 后，还要：

~~~text
worker runnable
↓
Linux runqueue
↓
CPU core
~~~

关键参数：

~~~text
SCHED_OTHER / SCHED_FIFO / SCHED_RR
priority
CPU affinity
cpuset/cgroup
NUMA
~~~

SCHED_OTHER 是普通分时调度；SCHED_FIFO / SCHED_RR 是 Linux 实时调度类，前者同优先级按 FIFO，后者同优先级再加时间片轮转。CPU affinity 是“线程允许在哪些 CPU core 上运行”；NUMA 是 Non-Uniform Memory Access，多 socket/多 NUMA node 机器上，CPU 访问本地内存通常比访问远端 node 内存更便宜。

Runtime scheduler 与 OS scheduler 是两级调度。

---


## 一个错误 RT 配置

~~~text
Control thread: FIFO 90
Logger: OTHER

Control needs mutex
Logger holds mutex
~~~

高优先级 thread 仍可能被低优先级 owner 卡住。

所以 priority 设置必须和：

~~~text
mutex graph
blocking I/O
allocation
shared resources
~~~

一起分析。

---


## 工业案例：Cyber

~~~text
transport callback
↓
Dispatcher / CacheBuffer
↓
Notifier
↓
Scheduler
↓
Processor OS thread
↓
CRoutine resume
↓
Component::Proc
~~~

它展示了逻辑 task、coroutine 与 OS worker 分离。

---


## 工业案例：Holoscan

~~~text
Condition/event
↓
EventBasedScheduler dispatcher
↓
per-worker ready queue
↓
worker
↓
Linux scheduler
~~~

并加入 sharding、batch drain、work stealing、CPU pinning 和 RT policy。

详见： [Holoscan Event-Based Scheduler](../generated/holoscan/event-based-scheduler.md)。

---


## 一个合理的混合 Runtime

~~~text
CPU2:
  EtherCAT/control dedicated RT thread

CPU3:
  safety/supervisor

CPU4-6:
  perception worker pool

CPU7:
  network/logging/event loop
~~~

数据流：

~~~text
driver → SPSC → estimator
events → MPSC → supervisor
perception tasks → per-worker queues
logs → low-priority MPSC batch writer
~~~

不是所有 task 都必须进入统一 Executor。

---


## 设计检查表

~~~text
[ ] 哪些工作必须 dedicated thread？
[ ] 哪些工作可以 pool 化？
[ ] global queue 会不会成为热点？
[ ] task 是否允许跨 worker 执行？
[ ] 是否需要 work stealing？
[ ] idle worker 用什么 primitive 睡眠？
[ ] Runtime scheduler 与 Linux priority/affinity 如何对应？
[ ] 是否存在 blocking syscall 卡住 worker？
[ ] shutdown 谁 close queue、谁唤醒 sleeping workers？
~~~

深入机制：

- [Concurrent Queues](concurrent-queues-progress.md)
- [Thread Communication Lab](thread-dataflow-lab.md)
- [Industry Runtime Cases](industry-runtime-cases.md)
