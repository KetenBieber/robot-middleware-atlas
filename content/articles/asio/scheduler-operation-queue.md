# Scheduler 与 Operation Queue：为什么 Asio 把可执行任务做成 Intrusive Operation

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

Asio 的 scheduler 可以看成 completion runtime：

~~~text
reactor / timer / post
↓
scheduler_operation
↓
op_queue
↓
run() threads
↓
complete()
~~~

## scheduler_operation 是最小可调度单元

核心字段：

~~~cpp
scheduler_operation* next_;
func_type func_;
unsigned int task_result_;
~~~

`next_` 直接服务 intrusive queue，因此 push/remove 不需要额外分配 list node。

## 为什么不用 std::function 统一 Handler

`std::function` 会把所有 completion 都先变成同一种高层 callable abstraction。

Asio 反过来保留具体 operation 类型，只在基类放一个函数指针：

~~~text
typed operation object
+
one erased completion function
~~~

这样 handler allocator、buffer state、error state 都可以跟 operation 存在同一个对象中。

## task_operation_ 是 Control Message

Scheduler queue 中有一个特殊 `task_operation_`。

取到它时并不调用用户 callback，而是进入 reactor task：

~~~cpp
task_->run(timeout, this_thread.private_op_queue);
~~~

所以一条 queue 同时承载：

~~~text
user completion
+
runtime control sentinel
~~~

这和 libuv threadpool 的 slow-work sentinel 属于同一种设计。

## do_run_one() 为什么先 Pop、再 Unlock、最后 Complete

顺序是：

~~~text
lock scheduler
↓
pop operation
↓
decide whether another worker should wake
↓
unlock
↓
call user completion
~~~

Scheduler global mutex 绝不能覆盖用户 handler。

否则一个阻塞 handler 就能让所有 worker 无法 dequeue；handler 重新 post/cancel 时还可能形成 reentrancy deadlock。

## 多个 run() Thread 怎么共享工作

当前线程拿走一个 operation 后，如果 queue 还有更多 work，会唤醒另一个 idle scheduler thread。

但这只解决 worker 并行，并不保证同一业务对象串行，因此还需要 strand。

Strand 不是再给每个 handler 套一把 mutex，而是在调度层建立 logical owner：首个 submitter 设置 `locked_`，外部并发提交进入 `waiting_queue_`，当前 owner 无锁 drain `ready_queue_`，再通过 RAII invoker 完成批次交接。详见 [Strand：逻辑执行权、双队列交接与无锁用户回调](strand-serialization.md)。

## outstanding_work_ 为什么不是 Queue Size

异步 read 进入 reactor 后，scheduler ready queue 可以为空，但系统仍有未来 completion。

所以 outstanding work 记录的是：

> runtime 仍承诺未来可能产生 completion 的逻辑工作数量。

## Cancellation 的边界

必须区分：

~~~text
queued operation
kernel-pending I/O
already executing handler
~~~

前两者可以请求取消，最后一种只能由业务代码合作结束。

这里最容易漏掉的一层，是“取消以后 work debt 怎么结算”：Reactor 并不会直接 delete pending operation，而是把它改写成 `operation_aborted` completion 再交回 Scheduler；Scheduler 又要等 completion/upcall 边界结束后才能释放对应 outstanding work。完整链路见 [Work、Lifetime 与 Cancellation](work-lifetime-cancellation.md)。

## 可迁移的最小骨架

~~~cpp
struct Operation {
  Operation* next;
  void (*complete)(Operation*, Error);
};

class Scheduler {
  IntrusiveQueue<Operation> ready;
  Mutex mutex;
  Event wakeup;
  AtomicCount outstanding;
};
~~~

关键不变量：scheduler lock 只保护调度数据结构；用户代码锁外执行；liveness 单独计数。