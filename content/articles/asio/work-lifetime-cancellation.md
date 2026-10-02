# Work、Lifetime 与 Cancellation：为什么 Queue 空了不代表异步系统已经结束

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

异步 I/O 最容易产生的一类误解，是把下面几件事当成同一件事：

~~~text
ready queue 为空
没有 pending I/O
io_context::run() 可以返回
descriptor 已关闭
handler 不会再执行
operation object 已销毁
payload 可以释放
业务对象可以 delete
~~~

它们并不等价。

Asio 的实现之所以值得源码级阅读，正是因为它把这些生命周期拆成不同层次，再用几套不同的机制把它们连接起来：

~~~text
scheduler::outstanding_work_
executor_work_guard
reactor per-descriptor op_queue
scheduler op_queue
handler_work
descriptor deregistration
operation_aborted completion
scheduler shutdown
~~~

本篇不把 cancellation 理解成一个 API，而是把问题改写成：

> 一个异步 operation 从“被接受”到“用户 handler 最终不可能再被调用”，中间究竟经过哪些 ownership、liveness 与 completion 边界？

这条链一旦看清，`run()` 为什么会退出、`cancel()` 为什么不是强杀、`close()` 为什么还会产生 callback、`stop()` 为什么不是 cancel，以及为什么捕获了 handler 仍不能自动延长 buffer 生命周期，都会自然得到答案。

## 1. 先建立五种完全不同的 lifetime

讨论 Asio 时至少要分开五层。

第一层是 **Runtime liveness**：

~~~text
io_context::run()
是否应该继续活着等待未来 completion？
~~~

第二层是 **Operation lifetime**：

~~~text
某个 async_read / async_write
对应的内部 operation object
是否还存在？
~~~

第三层是 **Descriptor lifetime**：

~~~text
fd 是否仍注册在 epoll
OS descriptor 是否仍然 open？
~~~

第四层是 **Handler lifetime**：

~~~text
completion handler 对象本身
是否仍被 Runtime 持有？
~~~

第五层是 **Payload / application object lifetime**：

~~~text
buffer 指向的内存
handler 捕获的 this
业务 Session 对象
是否还活着？
~~~

这五层可能同时存在，也可能先后结束。

一个常见错误就是：

~~~text
Asio 还保存 handler
⇒ handler 引用的所有东西都安全
~~~

这是错的。

Runtime 最多只能管理它真正拥有或显式计数的对象。

## 2. Scheduler 的 outstanding_work_ 不是 ready queue 长度

`scheduler` 中最核心的 liveness 状态：

~~~cpp
void work_started()
{
  ++outstanding_work_;
}

void work_finished()
{
  if (--outstanding_work_ == 0)
    stop();
}
~~~

这里统计的不是：

~~~text
scheduler op_queue 里有几个节点
~~~

而是：

~~~text
Runtime 仍承诺未来要完成多少份逻辑 work
~~~

因此：

\[
W_{\text{outstanding}}
\neq
|\text{ready queue}|
\]

ready queue 可以暂时为空，但 `outstanding_work_` 仍然大于零。

## 3. 为什么 ready queue 为空时 run() 仍然可能不能退出

一个典型异步读可能处于：

~~~text
async_read submitted
        |
        v
epoll_reactor::descriptor_state
        |
        v
op_queue_[read_op]
        |
        | waiting kernel readiness
        v
scheduler ready queue currently empty
~~~

此时 completion 还没有进入 scheduler queue。

如果只看：

~~~text
scheduler queue empty
~~~

就让 `run()` 返回，那么 Runtime 会在真正的 fd readiness 到来之前退出。

所以必须有另一套状态告诉 scheduler：

~~~text
虽然现在没 ready handler，
但未来仍可能产生 handler。
~~~

这就是 outstanding work。

## 4. run() 的第一道门就是 outstanding_work_

固定源码：

~~~cpp
std::size_t scheduler::run(
    asio::error_code& ec)
{
  ec = asio::error_code();

  if (outstanding_work_ == 0)
  {
    stop();
    return 0;
  }

  ...
}
~~~

因此 `run()` 的存活前提不是：

~~~text
queue currently non-empty
~~~

而是：

~~~text
there exists logical outstanding work
~~~

这是一种 **liveness lease**。

## 5. executor_work_guard 就是显式 Liveness Lease

经典 TS executor 路径中：

~~~cpp
explicit executor_work_guard(
    const executor_type& e)
  : executor_(e),
    owns_(true)
{
  executor_.on_work_started();
}
~~~

析构：

~~~cpp
~executor_work_guard()
{
  if (owns_)
    executor_.on_work_finished();
}
~~~

`reset()`：

~~~cpp
void reset() noexcept
{
  if (owns_)
  {
    executor_.on_work_finished();
    owns_ = false;
  }
}
~~~

它的语义很直接：

~~~text
guard alive
    |
    v
Runtime must assume
future work may still arrive
~~~

即使这一瞬间：

~~~text
ready queue empty
reactor has no event
~~~

`run()` 也不能因为“眼前没活”就结束。

## 6. Work Guard 不是“线程保持器”

它不是：

~~~text
keep this OS thread alive forever
~~~

更准确的是：

~~~text
keep execution context logically alive
until this lease is released
~~~

真正调用 `run()` 的线程仍然由应用控制。

Work guard 只改变：

~~~text
io_context 是否认为还有未完成工作
~~~

## 7. copy、move、reset 展示了 liveness lease 的所有权语义

`executor_work_guard` copy：

~~~cpp
executor_work_guard(
    const executor_work_guard& other)
  : executor_(other.executor_),
    owns_(other.owns_)
{
  if (owns_)
    executor_.on_work_started();
}
~~~

copy 会复制一份独立 lease。

move：

~~~cpp
executor_work_guard(
    executor_work_guard&& other)
  : executor_(...),
    owns_(other.owns_)
{
  other.owns_ = false;
}
~~~

move 转移 lease。

所以可以把 guard 看成：

~~~text
RAII token
representing one outstanding liveness obligation
~~~

这和普通资源所有权非常像，只不过资源不是 fd 或 memory，而是：

~~~text
“Runtime 现在还不能自然退出”这一事实
~~~

## 8. 新 Execution 模型中的 tracked outstanding_work

较新的 executor 路径不一定直接调用：

~~~text
on_work_started()
on_work_finished()
~~~

`executor_work_guard` 会构造：

~~~cpp
asio::prefer(
    executor_,
    execution::outstanding_work.tracked)
~~~

并把这个 tracked executor 保存到 `work_` storage。

其析构就释放这个 tracked-work 对象。

所以 API 形式发生了变化，但核心语义没变：

~~~text
tracked executor object alive
⇒ outstanding work lease alive
~~~

## 9. 一个真正的 async operation 何时获得 work debt

以 Linux `epoll_reactor` 为例。

operation 无法立即完成时，会进入 per-descriptor queue：

~~~cpp
descriptor_data
  -> op_queue_[op_type]
      .push(op);

scheduler_.work_started();
~~~

关键不是这两行本身，而是它们执行时：

~~~text
descriptor_data->mutex_
仍然持有
~~~

因此另一个线程不能在：

~~~text
operation 已经进入 descriptor queue
但 work debt 还没登记
~~~

这个窗口里把 operation cancel 掉。

## 10. 这是典型的 Register-Before-Completion 协议

理想时序：

~~~text
lock descriptor
    |
    v
publish operation into pending queue
    |
    v
register outstanding work
    |
    v
unlock descriptor
~~~

cancel/readiness 必须先拿同一把 descriptor mutex。

因此一旦另一个线程能够观察到 pending operation，它对应的 liveness debt 已经存在。

这和所有正确异步协议的共同原则一致：

> completion 变得可能之前，必须先把未来要等待的 obligation 登记好。

## 11. Speculative completion 为什么走另一条记账路径

`start_op()` 先尝试 speculative I/O。

如果非阻塞 read/write 已经直接完成：

~~~text
operation 从未进入 descriptor pending queue
~~~

这时走：

~~~text
on_immediate(...)
~~~

而不是：

~~~text
push op
work_started
~~~

为什么不会漏记？

因为 immediate completion 进入 scheduler 时使用：

~~~cpp
post_immediate_completion(...)
~~~

该函数自己负责：

~~~cpp
work_started();
op_queue_.push(op);
~~~

因此两条分支分别是：

~~~text
Pending branch:
descriptor queue
→ work_started
→ later deferred completion

Immediate branch:
operation completes now
→ post_immediate_completion
→ work_started + scheduler queue
~~~

不管哪条路径，ready completion 一旦进入 scheduler，work debt 都已经建立。

## 12. immediate 与 deferred 的区别首先是“谁已经付过账”

`scheduler` 明确区分：

~~~cpp
post_immediate_completion(...)
~~~

和：

~~~cpp
post_deferred_completion(...)
~~~

前者的注释语义是：

~~~text
work_started() has not yet been called
~~~

后者则假设：

~~~text
work_started() was previously called
~~~

所以 deferred completion 不应该再次增加 outstanding work。

否则会出现：

~~~text
one operation
→ work_started twice
→ work_finished once
→ run() 永远认为还有工作
~~~

## 13. Cancellation 的本质不是 delete operation

`epoll_reactor::cancel_ops()`：

~~~cpp
mutex::scoped_lock
  descriptor_lock(
    descriptor_data->mutex_);

op_queue<operation> ops;

for (...)
{
  while (reactor_op* op =
      descriptor_data
        ->op_queue_[i].front())
  {
    op->ec_ =
      asio::error::operation_aborted;

    descriptor_data
      ->op_queue_[i].pop();

    ops.push(op);
  }
}

descriptor_lock.unlock();

scheduler_
  .post_deferred_completions(ops);
~~~

这段源码非常关键。

cancel 做的是：

~~~text
pending reactor operation
        |
        | remove from descriptor queue
        | mark operation_aborted
        v
ready completion
        |
        v
scheduler queue
~~~

不是：

~~~text
pending operation
→ delete
~~~

## 14. operation_aborted 是一种 Completion Result

所以：

~~~text
cancel()
~~~

更准确的语义是：

> 把仍由 Reactor 持有的 pending operation 转换成一个带 `operation_aborted` 结果的 completion。

用户 handler 仍然可能被调用。

例如：

~~~cpp
async_read(...,
  [](error_code ec, size_t n) {
    if (ec == asio::error::operation_aborted)
      ...
  });
~~~

这正是 cancellation 是状态机，而不是“让 callback 消失”的体现。

## 15. 为什么 cancel 之后仍必须 post_deferred_completion

operation 原先已经：

~~~text
scheduler_.work_started()
~~~

这笔 debt 必须有结算点。

如果 cancel 直接销毁 operation：

~~~text
outstanding_work_
永远少不了这一笔
~~~

如果只减少 work count，但不生成 completion：

~~~text
用户又失去了异步 API
承诺的 completion 语义
~~~

因此 cancel 需要把 operation 从：

~~~text
waiting-for-I/O
~~~

转换为：

~~~text
ready-with-operation_aborted
~~~

## 16. post_deferred_completions 为什么不 work_started

因为 debt 已经在 operation 第一次进入 reactor pending queue 时登记：

~~~text
start_op
  |
  +-- op_queue.push
  +-- scheduler.work_started
~~~

cancel 只是改变 work 的位置：

~~~text
descriptor queue
→ scheduler queue
~~~

并没有创造新的 work。

所以：

~~~text
move work
!=
create work
~~~

这是 runtime accounting 非常重要的边界。

## 17. Cancel 与 Readiness 的竞争由 descriptor mutex 决定

假设 fd readiness 和 `cancel()` 同时发生。

两边都要处理：

~~~text
descriptor_data->op_queue_
~~~

因此核心竞争是：

~~~text
谁先从 pending queue
取得这个 reactor_op
~~~

如果 cancel 先拿到：

~~~text
op.ec = operation_aborted
→ scheduler
~~~

如果 readiness 路径已经先把 operation 完成并移出 descriptor queue：

~~~text
cancel_ops
已经看不到它
~~~

因此 cancellation 不是“历史回滚”。

## 18. 为什么已经 ready 的 operation 不能被 retroactively cancel

一旦 operation 已经从：

~~~text
descriptor pending queue
~~~

移动到：

~~~text
scheduler ready queue
~~~

它不再属于 `cancel_ops()` 扫描的容器。

这意味着：

~~~text
cancel request
发生得晚
~~~

时，用户仍可能收到已经完成的正常结果。

正确心智模型是：

~~~text
cancel pending work
~~~

而不是：

~~~text
erase every future callback
regardless of current phase
~~~

## 19. selective cancellation 也是“从 pending set 中筛选”

`epoll_reactor` 还有：

~~~cpp
cancel_ops_by_key(
    ...,
    void* cancellation_key)
~~~

它遍历指定 `op_type` queue：

~~~text
matching key
→ operation_aborted
→ scheduler

non-matching key
→ push back
~~~

所以 per-operation cancellation 的底层问题仍然是：

~~~text
从一个 owner-local pending set 中
选择哪些 operation 转移到 aborted completion
~~~

而不是抢占某条正在执行的 C++ handler。

## 20. Close 比 Cancel 多做了一件事：关闭未来 I/O 入口

`reactive_descriptor_service::cancel()`：

~~~cpp
reactor_.cancel_ops(
    impl.descriptor_,
    impl.reactor_data_);
~~~

fd 本身仍然 open。

而 `close()`：

~~~cpp
reactor_.deregister_descriptor(
    impl.descriptor_,
    impl.reactor_data_,
    ...);

descriptor_ops::close(
    impl.descriptor_,
    impl.state_,
    ec);

reactor_.cleanup_descriptor_data(
    impl.reactor_data_);
~~~

所以：

~~~text
cancel
  pending I/O -> aborted completion
  descriptor remains usable

close
  stop descriptor participation
  pending I/O -> aborted completion
  close OS fd
  release descriptor bookkeeping
~~~

## 21. deregister_descriptor 的顺序非常值得研究

固定源码逻辑：

~~~text
lock descriptor state
        |
        v
detach / stop epoll registration
        |
        v
move all pending ops out
and mark operation_aborted
        |
        v
descriptor_ = -1
shutdown_ = true
        |
        v
unlock descriptor state
        |
        v
post deferred completions
~~~

之后 service 才执行：

~~~text
OS close
cleanup descriptor_data
~~~

这形成一个非常清楚的阶段划分。

## 22. Descriptor state 可以先死，Operation 仍然继续活

当 pending op 被移动到 scheduler queue 后：

~~~text
descriptor_state
不再拥有该 op
~~~

operation object 自己保存完成所需状态。

例如 `descriptor_read_op_base` 内部保存：

~~~cpp
int descriptor_;
MutableBufferSequence buffers_;
~~~

派生 operation 还保存：

~~~cpp
Handler handler_;
handler_work<...> work_;
~~~

因此：

~~~text
descriptor registration lifetime
<
operation completion lifetime
~~~

完全可能成立。

## 23. Close 返回并不意味着 handler 不会再执行

这是最容易误判的地方之一。

close 可以先完成：

~~~text
epoll deregistration
fd close
descriptor_data cleanup
~~~

但之前 pending operation 已经被转换成：

~~~text
scheduler ready completion
~~~

因此稍后仍可能：

~~~text
handler(operation_aborted, ...)
~~~

所以：

~~~text
fd closed
!=
completion queue drained
~~~

## 24. Scheduler 真正执行 completion 时先释放内部锁

`do_run_one()` 中：

~~~cpp
operation* o = op_queue_.front();
op_queue_.pop();

...

lock.unlock();

work_cleanup on_exit =
  { this, &lock, &this_thread };

o->complete(
    this,
    ec,
    task_result);
~~~

这说明 user completion path 不在 scheduler mutex 内执行。

这是非常重要的 reentrancy 设计。

## 25. 为什么不能在 scheduler mutex 下执行用户 handler

用户 handler 可以做几乎任何事：

- 再次 `async_read`；
- `post()` 新 work；
- cancel 其他 operation；
- close socket；
- reset work guard；
- 销毁业务对象；
- 抛异常。

如果 scheduler 仍持有核心 queue mutex：

~~~text
user handler
→ reenter scheduler
→ same mutex
~~~

就很容易变成自死锁。

因此成熟 runtime 的一个基本规则是：

> 内部锁负责维护 Runtime invariant，不负责包住任意用户代码。

## 26. work_cleanup 为什么包住整个 o->complete()

源码注释：

~~~text
Ensure the count of outstanding work
is decremented on block exit.
~~~

`work_cleanup` 在：

~~~text
o->complete()
~~~

之前构造。

它的析构发生在：

~~~text
completion returns
or throws
~~~

之后。

所以当前 operation 的 liveness debt 覆盖到：

~~~text
completion processing boundary
~~~

而不是在“从 ready queue pop 出来”时就释放。

## 27. dequeue 不是 completion

这和前面很多 runtime 原理一致：

~~~text
queued
→ dequeued
→ handler body
→ completed
~~~

只有 dequeue 并不能证明：

~~~text
user-visible completion finished
~~~

所以 work accounting 不能在 pop queue 时结束。

## 28. Handler 执行时产生新 continuation 怎么记账

Asio 对同一个 scheduler thread 上的 continuation 做了优化。

`post_immediate_completion()` 如果确认自己正位于 scheduler-owned thread：

~~~cpp
++this_thread
   ->private_outstanding_work;

this_thread
  ->private_op_queue
  .push(op);
~~~

它暂时不立刻修改 global `outstanding_work_`。

这是为了减少：

~~~text
global atomic / shared counter churn
+
main queue contention
~~~

## 29. work_cleanup 实际上在做“债务转移”

`work_cleanup`：

~~~cpp
if (private_outstanding_work > 1)
{
  increment(
    scheduler_->outstanding_work_,
    private_outstanding_work - 1);
}
else if (private_outstanding_work < 1)
{
  scheduler_->work_finished();
}
~~~

假设当前 operation 原本占用一笔 global work debt。

handler 内产生 `k` 个 private continuation。

如果：

\[
k=0
\]

没有后继 work：

~~~text
当前 debt 结束
→ work_finished()
~~~

如果：

\[
k=1
\]

不增不减：

~~~text
当前 operation 的那 1 笔 debt
直接转移给唯一 continuation
~~~

如果：

\[
k>1
\]

只需要补：

\[
k-1
\]

笔 global debt。

因为当前 operation 原来的那一笔可以继续代表其中一个 child continuation。

## 30. 这是一个很漂亮的 Work Conservation 模型

可以写成：

\[
W_{\text{after}}
=
W_{\text{before}}
-1
+k
\]

但实现不一定真的执行：

~~~text
-1
then +k
~~~

而是利用当前已有的一笔 debt：

~~~text
k = 0:
  -1

k = 1:
   0

k > 1:
  +(k-1)
~~~

语义相同，但减少共享状态修改。

## 31. private_op_queue 不只是性能优化，也影响 lifetime accounting

因此：

~~~text
private queue
~~~

和：

~~~text
private_outstanding_work
~~~

必须成对理解。

如果只搬 operation，不搬对应 work debt：

~~~text
run() 可能过早 stop
~~~

如果只搬 debt，不搬 operation：

~~~text
run() 可能永不退出
~~~

数据结构与 lifecycle accounting 必须一致。

## 32. scheduler_operation 本身是 type-erased ownership capsule

基类：

~~~cpp
class scheduler_operation
{
public:
  void complete(
      void* owner,
      const error_code& ec,
      size_t bytes)
  {
    func_(owner, this, ec, bytes);
  }

  void destroy()
  {
    func_(0, this, error_code(), 0);
  }

private:
  func_type func_;
};
~~~

没有虚函数。

具体 operation 用 function pointer 恢复真实类型。

所以一个 operation node 同时是：

~~~text
intrusive queue node
+
type erasure header
+
completion dispatch hook
+
resource ownership capsule
~~~

## 33. complete 与 destroy 为什么必须区分

`complete(...)`：

~~~text
normal runtime completion path
~~~

而：

~~~cpp
destroy()
~~~

会传：

~~~text
owner = nullptr
~~~

具体 operation 可以据此：

~~~text
释放 handler / operation memory
但不 upcall 用户
~~~

这对 execution context teardown 非常关键。

## 34. Scheduler shutdown 不等于把所有 handler 调成 operation_aborted

`scheduler::shutdown()` 最终会：

~~~cpp
while (!op_queue_.empty())
{
  operation* o =
    op_queue_.front();

  op_queue_.pop();

  if (o != &task_operation_)
    o->destroy();
}
~~~

这是：

~~~text
destroy queued handler objects
~~~

而不是：

~~~text
invoke every user handler
with operation_aborted
~~~

所以：

~~~text
descriptor cancel/close
~~~

与：

~~~text
execution context shutdown
~~~

是不同语义。

## 35. stop() 又是第三种完全不同的机制

`scheduler::stop()`：

~~~cpp
mutex::scoped_lock lock(mutex_);
stop_all_threads(lock);
~~~

`stop_all_threads()`：

~~~cpp
stopped_ = true;
wakeup_event_.signal_all(lock);

if (!task_interrupted_ && task_)
{
  task_interrupted_ = true;
  task_->interrupt();
}
~~~

它没有：

~~~text
遍历 descriptor op queue
设置 operation_aborted
销毁 operation
~~~

因此：

> `io_context::stop()` 是停止 dispatch loop，不是取消所有 I/O。

## 36. stop / cancel / close / shutdown 必须分开

可以整理成：

~~~text
work_guard.reset()
  释放一笔 Runtime liveness lease

io_context.stop()
  请求 run/poll loop 停止 dispatch

descriptor.cancel()
  pending descriptor ops
  -> operation_aborted completions
  descriptor stays open

descriptor.close()
  detach descriptor
  abort pending ops
  close OS fd
  completion may still run later

scheduler / execution context shutdown
  destroy remaining queued operation objects
  may skip user upcall
~~~

如果把这五者混成“停止异步任务”，代码一定会出现边界错误。

## 37. restart() 为什么存在

`restart()` 只是：

~~~cpp
stopped_ = false;
~~~

它不会重新创建全部 runtime state。

所以当 `run()` 因：

~~~text
stop()
or work exhausted
~~~

返回后，如果还要再次运行 context，需要显式 restart。

这再次说明：

~~~text
stopped state
~~~

是 scheduler dispatch 状态，不是资源全部销毁状态。

## 38. descriptor_read_op 展示了 Operation 真正拥有什么

`descriptor_read_op_base` 保存：

~~~cpp
int descriptor_;
MutableBufferSequence buffers_;
~~~

派生类保存：

~~~cpp
Handler handler_;
handler_work<Handler, IoExecutor> work_;
~~~

所以 Runtime operation 的确拥有：

- handler 对象；
- buffer sequence 对象；
- executor/work bookkeeping；
- error / transferred bytes 状态。

但这里仍然有一个关键区别。

## 39. Buffer Sequence 对象不等于 Buffer Storage

`asio::mutable_buffer` / `const_buffer` 本质上描述：

~~~text
pointer + length
~~~

因此 operation 保存：

~~~text
buffers_
~~~

通常只意味着：

~~~text
保存了 buffer view / sequence
~~~

不意味着：

~~~text
把用户 payload bytes 深拷贝进 operation
~~~

所以：

~~~cpp
std::string msg = make_message();

async_write(
    socket,
    asio::buffer(msg),
    handler);
~~~

如果 `msg` 在 async operation 完成前销毁：

~~~text
operation 仍活着
buffer view 仍活着
underlying bytes 已死
~~~

这仍然是 dangling payload。

## 40. Payload Lifetime 的正确不变量

对于需要异步访问的 buffer：

\[
T_{\text{payload}}
\ge
T_{\text{async access}}
\]

不是：

\[
T_{\text{payload}}
\ge
T_{\text{async call}}
\]

因为 `async_write()` 返回只表示：

~~~text
operation accepted
~~~

不表示：

~~~text
kernel/runtime 已经完全不再读这块 memory
~~~

## 41. handler 被 Runtime 保存，也不自动保护 handler 捕获的 this

比如：

~~~cpp
async_read(
  socket,
  buffer_,
  [this](error_code ec, size_t n) {
    on_read(ec, n);
  });
~~~

operation 的确保存这个 lambda。

但 lambda 内部的：

~~~text
this
~~~

只是一个普通指针。

如果业务对象先被销毁：

~~~text
handler object alive
this pointee dead
~~~

completion 稍后执行时仍然可能 UAF。

## 42. Runtime Owns Handler ≠ Runtime Owns Captured Object

这是 C++ 异步编程最值得反复强调的一条：

~~~text
handler lifetime
!=
captured pointee lifetime
~~~

安全策略可以是：

- `shared_from_this()`；
- owner thread 保证 session 在 completion drain 前不析构；
- explicit in-flight count；
- cancellation + completion drain；
- coroutine frame 持有状态；
- generation / weak_ptr 验证。

选择哪一种取决于系统 ownership。

## 43. descriptor_read_op 为什么在 upcall 前先复制 handler

`do_complete()`：

~~~cpp
descriptor_read_op* o =
  static_cast<
    descriptor_read_op*>(base);

ptr p = {
  addressof(o->handler_),
  o,
  o
};
~~~

然后把 operation 里的 work 移出来：

~~~cpp
handler_work<...> w(
  static_cast<
    handler_work<...>&&>(
      o->work_));
~~~

再构造本地 binder：

~~~cpp
binder2<
  Handler,
  error_code,
  size_t>
handler(
  o->handler_,
  o->ec_,
  o->bytes_transferred_);
~~~

最后：

~~~cpp
p.reset();
~~~

先释放 operation memory。

之后才真正 upcall：

~~~cpp
w.complete(
  handler,
  handler.handler_);
~~~

## 44. 为什么要“先复制 handler，再释放 operation，再 upcall”

源码注释直接给出了动机：

~~~text
handler 的某个 sub-object
可能才是真正拥有 allocator memory 的对象
~~~

因此不能简单写：

~~~text
free operation
then use handler reference inside it
~~~

正确顺序是：

~~~text
capture handler state locally
        |
        v
move work/executor bookkeeping out
        |
        v
free operation storage
        |
        v
invoke user handler
~~~

这是一种非常成熟的 callback lifetime 模式。

## 45. 用户 handler 执行时，operation object 本身已经可以不存在

所以：

~~~text
operation object lifetime
~~~

和：

~~~text
handler execution lifetime
~~~

也不完全一样。

Asio 可以做到：

~~~text
internal operation memory
freed before upcall
~~~

同时：

~~~text
local handler copy
still alive
~~~

这降低了 reentrancy 时内部资源仍被占用的范围。

## 46. handler_work 又是另一层 execution lifetime

operation 完成时把：

~~~text
o->work_
~~~

move 到局部 `w`。

这保证即使 operation storage 已经释放：

~~~text
associated executor / outstanding work
~~~

仍然覆盖用户 handler dispatch。

所以 Asio 并没有把所有 lifetime 塞进一个对象。

它是逐层“接力”：

~~~text
operation
  |
  +-- handler
  +-- handler_work
        |
        v
local completion state
        |
        v
user upcall
~~~

## 47. Cancellation 的完成 handler 为什么仍应正常走 handler_work

因为：

~~~text
operation_aborted
~~~

仍然是 completion。

从 scheduler 角度：

~~~text
success completion
~~~

和：

~~~text
cancelled completion
~~~

都必须经过同一个 ownership / executor / work accounting 边界。

差别主要在：

~~~text
error_code
~~~

而不是：

~~~text
有没有 completion lifecycle
~~~

## 48. Cancel 后立即析构业务对象为什么仍危险

考虑：

~~~text
Thread A:
  socket.cancel()
  delete session

Runtime:
  operation_aborted completion
  already posted to scheduler
  handler captures session this
~~~

那么：

~~~text
cancel succeeded
~~~

并不能证明：

~~~text
handler will never run
~~~

恰恰相反，cancel 往往是在确保 handler 以 aborted 状态进入 completion path。

所以 teardown 需要：

~~~text
cancel
+
drain/join completion boundary
~~~

而不是只有 cancel。

## 49. 这就是 Cancellation 与 Quiescence 的区别

Cancellation：

~~~text
请求 pending operation
停止正常 I/O 目标
~~~

Quiescence：

~~~text
证明不会再有任何 completion
进入目标业务对象
~~~

前者是状态转换。

后者是生命周期证明。

## 50. 一个安全 Session teardown 需要回答四个问题

第一：

~~~text
还能不能提交新的 async operation？
~~~

第二：

~~~text
已经 pending 的 operation
是否已经 cancel / close？
~~~

第三：

~~~text
已经 ready 的 completion
是否已经 drain？
~~~

第四：

~~~text
handler 捕获的业务对象
是否在上述边界之后才 reclaim？
~~~

如果只回答第二个问题，生命周期仍然是不完整的。

## 51. 为什么 stop() 不能拿来代替 teardown drain

`io_context::stop()` 可能让：

~~~text
run()
~~~

很快返回。

但它没有保证：

~~~text
descriptor op queues empty
scheduler completion queues empty
业务 handler 已经执行
业务 handler 永远不会再执行
~~~

如果随后 `restart()` + `run()`：

~~~text
之前仍保留的 work
~~~

可能继续被处理。

所以：

~~~text
run returned
~~~

也不能脱离返回原因单独理解。

## 52. “run 返回”至少有两类原因

一类：

~~~text
outstanding_work_ == 0
~~~

这是自然耗尽。

另一类：

~~~text
stopped_ == true
~~~

这是 stop 状态。

这两个结论对 teardown 的意义不同。

自然耗尽更接近：

~~~text
Runtime 没有仍被计账的 work
~~~

而 stop 更接近：

~~~text
先暂停 dispatch
~~~

## 53. Work Guard 也不能用来保护 payload

另一个常见误解：

~~~text
我有 executor_work_guard
所以异步对象不会被销毁
~~~

work guard 只承诺：

~~~text
io_context liveness
~~~

它不拥有：

~~~text
socket
buffer
Session
handler-captured object
~~~

因此：

~~~text
Runtime still alive
~~~

和：

~~~text
your memory still alive
~~~

是两件事。

## 54. Work Guard 甚至不代表当前一定有 I/O

你可以创建：

~~~text
work_guard
~~~

但一个 async op 都不发。

此时：

~~~text
outstanding work > 0
ready queue empty
descriptor queue empty
~~~

`run()` 仍可保持等待。

这正好说明：

~~~text
outstanding_work
是 liveness contract
而不是 work inventory
~~~

## 55. Operation Count 也不是 Descriptor Count

一个 descriptor 可以同时有：

~~~text
read op
write op
except op
~~~

每个 operation 都可能拥有各自 work debt。

一个 Runtime 对象的生命周期不能只按：

~~~text
fd count
~~~

来推断。

## 56. 三层 Queue 是理解 Asio 的关键

Linux Reactor 路径至少要看到三层。

第一层：

~~~text
descriptor_data::op_queue_[type]
~~~

表示：

~~~text
等待 fd readiness
~~~

第二层：

~~~text
scheduler::op_queue_
~~~

表示：

~~~text
completion ready for dispatch
~~~

第三层：

~~~text
thread_info::private_op_queue
~~~

表示：

~~~text
当前 scheduler worker
产生的 continuation
~~~

同一个 operation 生命周期会跨不同容器。

## 57. “Queue 空了”必须先问“哪一个 Queue”

所以：

~~~text
queue empty
~~~

在异步 Runtime 中几乎不是一个完整句子。

必须明确：

~~~text
descriptor pending queue empty?
scheduler ready queue empty?
thread-private continuation queue empty?
application send queue empty?
kernel socket buffer empty?
~~~

每个结论都不同。

## 58. Close 的 teardown 顺序本质是 Retire → Complete → Reclaim

把 `deregister_descriptor + close + cleanup` 抽象一下：

~~~text
RETIRE
  prevent descriptor from
  accepting/producing normal readiness work

COMPLETE
  convert already pending ops
  into aborted completions

RECLAIM descriptor state
  release OS fd and
  per-descriptor bookkeeping
~~~

但是：

~~~text
operation / handler completion
~~~

可能继续晚于 descriptor reclaim。

因此这还不是业务对象的最终 reclaim。

## 59. 业务层还要再加一个 Completion Quiescence

如果 handler 捕获业务对象：

~~~text
descriptor retire
        |
        v
pending ops become aborted completions
        |
        v
scheduler dispatches them
        |
        v
all handlers leave business object
        |
        v
business object reclaim
~~~

这才是完整链。

## 60. Asio 与“立即 delete on cancel”的设计差异

立即 delete pending operation 看起来简单：

~~~text
cancel
→ erase
→ delete
~~~

但会失去：

- completion notification；
- associated executor semantics；
- work accounting；
- allocator / handler cleanup discipline；
- consistent error delivery。

Asio 选择的是：

~~~text
cancel changes outcome
but preserves completion protocol
~~~

这是更加组合化的设计。

## 61. 一个异步 API 最重要的 Contract 不是函数返回值

`async_read()` 返回时：

~~~text
operation accepted
~~~

`cancel()` 返回时：

~~~text
cancellation request applied
to operations still in the cancellable phase
~~~

`close()` 返回时：

~~~text
descriptor-level resource retired
~~~

真正的业务完成仍然由：

~~~text
completion boundary
~~~

表达。

这也是 async API 与 sync API 最大的心智差异。

## 62. 在机器人 Runtime 中可以怎样迁移

假设你有一个 CAN bus thread：

~~~text
Control thread
Estimator thread
Planner thread
        |
        v
CAN Runtime
~~~

一条异步 transaction 可以有：

~~~text
Operation
  request payload view
  completion callback
  deadline
  associated execution owner
~~~

Runtime 可以分别维护：

~~~text
pending bus queue
ready completion queue
outstanding transaction count
shutdown state
~~~

这样你就不会把：

~~~text
队列空
~~~

误当成：

~~~text
所有 transaction 已终止
~~~

## 63. 一个机器人设备 Session 的 cancel 也应该是状态转换

例如：

~~~text
PendingCommand
    |
    +-- ACK arrived
    |     -> SuccessCompletion
    |
    +-- timeout
    |     -> TimeoutCompletion
    |
    +-- cancel
          -> CancelledCompletion
~~~

而不是：

~~~text
cancel
→ free command object
~~~

completion protocol 统一后：

- logging；
- promise/future；
- coroutine resume；
- metrics；
- cleanup

都可以共享同一条收尾路径。

## 64. Deadline、Cancellation、Close 应该是不同事件

控制系统中经常把它们写成同一个 bool：

~~~text
stopped = true
~~~

更合理的是：

~~~text
deadline expired
cancel requested
transport closed
runtime stopping
~~~

分别映射到不同 completion reason。

这样诊断延迟与故障才有意义。

## 65. 对源码作者最有用的六条不变量

第一条：

> operation 进入任何可被其他线程观察的 pending 容器前，必须先建立或同步建立对应 lifecycle obligation。

第二条：

> 从 pending queue 转移到 ready queue，不应重复创建 work debt。

第三条：

> dequeue ready operation 不等于 completion finished，liveness debt 应覆盖 completion boundary。

第四条：

> cancel 应改变 pending operation 的状态与去向，不应假设能够抢占已经执行的用户代码。

第五条：

> descriptor resource lifetime、operation lifetime、handler lifetime 与 payload lifetime必须分别证明。

第六条：

> user code 不应在 Runtime 核心锁域内执行。

## 66. 一张完整状态图

~~~text
async_* initiation
        |
        v
construct operation
(handler + buffers + handler_work)
        |
        v
try speculative I/O
   |                |
 success            not ready
   |                |
   v                v
post immediate    descriptor op_queue
completion          |
   |                | work_started
   |                v
   |          wait epoll readiness
   |                |
   |       +--------+---------+
   |       |                  |
   |    readiness           cancel/close
   |       |                  |
   |       |             ec = operation_aborted
   |       |                  |
   +-------+------------------+
                   |
                   v
          scheduler ready queue
                   |
                   v
             dequeue op
                   |
             unlock mutex
                   |
                   v
         operation::complete()
                   |
                   +-- move handler_work
                   +-- copy handler state
                   +-- free operation memory
                   |
                   v
             user upcall
                   |
                   v
             work_cleanup
                   |
          +--------+---------+
          |                  |
   no continuation      k continuations
          |                  |
   work_finished      transfer/add debt
          |                  |
          +--------+---------+
                   |
                   v
          outstanding_work
          may reach zero
                   |
                   v
                stop
~~~

这张图里最重要的不是函数名，而是：

~~~text
ownership 什么时候转移
work debt 什么时候建立
work debt 什么时候释放
user code 在哪一个锁域之外运行
~~~

## 67. 最终结论

Asio 的 `outstanding_work_`、`executor_work_guard` 和 cancellation 不是三块独立知识。

它们共同回答一个问题：

> Runtime 如何在 operation 还可能产生 completion 时保持活着，又如何在 cancel/close 发生后仍然完整交付或销毁这些 completion，最后在真正没有 lifecycle obligation 时才允许 event loop 自然结束？

把这套设计压缩成一句话：

~~~text
Work tracks future completion obligations.
Cancellation changes completion outcome.
Close retires the descriptor.
Completion settles the obligation.
Only quiescence permits reclamation.
~~~

也就是：

> **Work 管“未来是否仍欠 completion”，Cancellation 管“completion 以什么结果结束”，Close 管“资源是否还接受新 I/O”，而对象能否回收必须等这些边界真正收束。**

这比“异步函数 + callback”更接近一个 Runtime 作者真正需要设计的东西。
