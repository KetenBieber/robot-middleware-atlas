# Asio 总览：把异步操作、执行位置与完成回调拆成三个独立问题

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`（Asio 1.38.2）。

Asio 的价值不只是封装 epoll/kqueue/IOCP。更重要的是把异步系统拆成三个正交问题：

~~~text
operation
= 要做什么

executor / execution context
= 在哪里执行

completion handler
= 完成以后调用什么
~~~

这比把 socket、线程、回调和状态全部塞进一个连接类更容易组合。

## io_context 背后真正重要的是 Scheduler

`io_context::run()` 本质上是让当前线程进入 scheduler，持续消费 ready operation。

Scheduler 管理：

- outstanding work；
- operation queue；
- idle-thread wakeup；
- reactor task；
- completion dispatch；
- stop/restart。

因此 queue 暂时为空并不等于 runtime 可以退出，因为 socket、timer 或其他异步操作可能仍在等待未来 completion。

## Reactor 与 Scheduler 是两层

Linux 下的数据路径可以压成：

~~~text
socket fd
↓
epoll_reactor
↓
descriptor_state
  read queue
  write queue
  except queue
↓
ready operation
↓
scheduler queue
↓
completion handler
~~~

Reactor 决定“什么时候可以尝试 I/O”，Scheduler 决定“哪个 ready completion 在哪个 run() thread 上执行”。

## Resource 与 Operation 分开

Socket 是长期资源；一次 async_read / async_write 是短期 operation。

这与 libuv 的 Handle / Request 划分非常接近：

~~~text
resource lifetime
≠
operation lifetime
≠
payload lifetime
~~~

## scheduler_operation 为什么只保存函数指针

底层 operation 基类保存：

~~~cpp
scheduler_operation* next_;
func_type func_;
unsigned int task_result_;
~~~

`next_` 让 operation 自己成为 intrusive queue node，`func_` 完成手工 type erasure。

这样 queue 只认识统一基类，但具体 operation 仍可以内嵌 handler、allocator、buffer state，而无需统一塞进 `std::function`。

## Strand 解决的是执行串行化

多个线程可以同时调用 `io_context::run()`，因此 handler 可以并行。

同一个 strand 上的 handler 则满足：

~~~text
may run on different OS threads
BUT
never overlap in execution
~~~

这是一种 logical execution ownership，而不是 thread affinity。

固定实现真正有价值的地方，是用 `locked_`、`waiting_queue_`、`ready_queue_` 和 `invoker` 把“并发提交”转换成“单逻辑 owner 批次执行”，并且让用户 handler 始终运行在 Strand 内部 mutex 之外。完整状态机见 [Strand：逻辑执行权、双队列交接与无锁用户回调](strand-serialization.md)。

## Work Count 是 Runtime Liveness

Scheduler 用 `work_started()` / `work_finished()` 维护 outstanding work。

当计数归零才会 stop。

因此 liveness 不是根据 ready queue 长度猜测，而是显式建模。

## 五条可迁移原则

1. Resource 与 Operation 分开。
2. Readiness detection 与 completion execution 分开。
3. Execution policy 可以对象化成 executor/strand。
4. Queue empty 不等于系统无工作，必须显式记录 outstanding work。
5. Handler、operation、payload、业务对象的 lifetime 必须分别设计。