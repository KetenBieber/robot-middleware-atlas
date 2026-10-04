# Future / Continuation：异步等待为什么可以变成 Task，而不是阻塞 Thread

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

普通同步程序：

~~~text
call read()
↓
thread blocks
↓
kernel wakes thread
↓
continue function
~~~

Seastar 要在一个 shard 上用一个 Reactor thread 承载大量并发，因此不能让这个 thread 为某个 request 阻塞。

核心办法是把“以后继续执行什么”显式对象化。

## continuation_base 直接继承 task

跨 shard 的 `work_item` 也直接继承 `task`：SMP queue 只负责把 work item 送到 owner shard，真正的用户函数不会在 queue poller 内直接执行，而是先 `schedule(this)`，重新进入目标 Reactor 的 scheduling-group 语义。见 [SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure](smp-message-queue.md)。

源码：

~~~cpp
template <typename T>
class continuation_base : public task {
    future_state _state;
};
~~~

这是一条非常关键的连接：

~~~text
asynchronous continuation
=
reactor schedulable task
~~~

Future 不只是一个结果盒子，它最终连接到 Reactor task scheduler。

## Future 未 Ready 时发生什么

调用 `.then(func)` 一类 API 时会创建 continuation object。

如果前置 future 尚未完成：

~~~text
future
↔ promise
stores continuation task
~~~

当前 C++ 调用链结束，把 CPU 还给 Reactor。

未来 promise 获得 value/exception 后，continuation 才进入 runnable path。

## 为什么 Ready Future 有 Fast Path

如果 future 已经 ready，理论上可以立即继续。

Seastar 会尽量避免无必要 schedule round-trip。

但 cooperative scheduler 还必须尊重 `need_preempt()`：

如果当前 task quota 已耗尽，即使结果 ready，也应该 yield，让其他 scheduling group/I/O 有机会运行。

所以：

~~~text
data dependency ready
!=
CPU policy says continue immediately
~~~

数据依赖和调度公平是两个独立维度。

## continuation::run_and_dispose 为什么自删除

Continuation 是一次性的状态机节点：

~~~cpp
_wrapper(promise, func, state);
delete this;
~~~

执行完成后没有继续存在的意义。

这和 Asio 的 operation object 很像：一次异步事务本身就是一个临时 Runtime object。

## Promise/Future 为什么互相保存连接

未 ready 的 future 与 promise 需要知道：

- state 在哪里；
- future 是否仍存在；
- continuation task 是谁。

`future_base::schedule()` 会把 continuation task 安装到 promise side，并把 state 指向 continuation 自身的 state storage。

这样 result 到来时不需要查全局表。

这是直接 object-to-object continuation linkage。

## Future 为什么标记 nodiscard

异步链如果被静默丢弃，会造成：

- exception 无人观察；
- outstanding resource 无人追踪；
- concurrency 无界；
- shutdown 时不知还有多少 background work。

所以源码文档强调 background work 应通过 gate/semaphore 等机制跟踪。

这和前面 Asio outstanding-work 的思想一致：

> 异步任务必须有显式 liveness ownership。

## Future Chain 不是 OS Thread Stack

同步函数的控制流保存在 thread stack。

Seastar async chain 则把控制流拆成多个 continuation object：

~~~text
Task A
→ future pending

[thread free to do other work]

completion
→ Task B continuation
→ Task C continuation
~~~

这就是 stackless async state machine 的本质。

## 对机器人程序的启发

如果一个 supervisor 要等待：

~~~text
network reply
sensor configuration ACK
disk completion
timer
~~~

不必为每个逻辑 request 分配一个阻塞线程。

可以把后续动作变成 continuation/coroutine task。

但 CPU-heavy 算法仍不能长时间占 Reactor。

## 可迁移原则

1. Async waiting 可以保存 continuation，而不是保存 blocked thread。
2. Continuation 本身可以成为 scheduler task。
3. Ready dependency 不代表应该无条件 inline 执行，还要服从 fairness/preemption policy。
4. 一次性 async operation/continuation 很适合自包含 state + callback + lifetime。
5. Background async chain 仍必须有 gate/semaphore/outstanding-work 之类的 liveness accounting。