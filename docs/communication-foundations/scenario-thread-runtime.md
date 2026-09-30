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

~~~cpp
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
