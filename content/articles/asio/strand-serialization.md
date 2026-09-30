# Strand：怎样在 Scheduler 层保证业务状态串行，而不是到处加 Mutex

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

一个 TCP Session 可能同时收到 read completion、write completion、timer 和外部 command。

多线程 `io_context::run()` 下，它们可能并行修改同一份 Session state。

Strand 的语义是：

~~~text
handlers may run on different OS threads
BUT
handlers in the same strand never overlap
~~~

## 双队列结构

Strand implementation 维护：

~~~cpp
bool locked_;
op_queue<operation> waiting_queue_;
op_queue<operation> ready_queue_;
~~~

语义：

~~~text
ready_queue
= 当前 logical owner 可以连续执行的 handlers

waiting_queue
= 其他线程并发提交的 handlers
~~~

waiting queue 需要 mutex；ready queue 在 strand owner 执行期间可以无锁访问。

## do_post() 的状态机

如果 `locked_ == true`：

~~~text
another handler owns strand
→ push waiting_queue
~~~

否则：

~~~text
locked = true
→ push ready_queue
→ post strand itself to io_context
~~~

调度器看到的是 strand 这个聚合执行单元，而不是每次 submit 都直接与全局 scheduler 竞争。

## Handler 完成后的 Handoff

当前批次结束时：

~~~text
lock strand mutex
↓
move waiting_queue → ready_queue
↓
locked = !ready_queue.empty()
↓
unlock
↓
if more work: repost strand
~~~

这是并发 producer 到单 owner 的 batch handoff。

## Strand 与 Mutex 的区别

Mutex：

~~~text
every state access
→ lock
→ mutate
→ unlock
~~~

Strand：

~~~text
all mutations become scheduled handlers
→ runtime serializes handlers
→ business state can often remain lock-free
~~~

它把并发安全提升到 execution policy 层。

## 不适合的场景

如果一个 handler 执行 100 ms CPU work，同 strand 后续事件全部被堵住。

长任务应该移到 worker pool，再把 completion post 回 strand。

## 机器人程序中的映射

例如 MotorSession：

~~~text
CAN RX
trajectory update
timeout
mode change
↓
same serial executor / strand
↓
MotorSession state machine
~~~

比四个 callback 各自加锁更容易建立状态不变量。

## 可迁移原则

1. 并发安全可以通过 execution ordering，而不是共享数据锁解决。
2. Mutable state 最好有明确 logical owner。
3. 双队列可以分离外部并发 submit 与内部无锁执行。
4. Serial executor 适合 event-driven state machine，不适合 blocking work。
5. 串行执行不等于固定在某个 OS thread。