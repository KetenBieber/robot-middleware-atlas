# Strand：逻辑执行权、双队列交接与无锁用户回调

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

很多人第一次接触 Asio Strand，会把它理解成：

~~~text
一个“自动帮你加 mutex”的 executor
~~~

这个理解只能覆盖表面语义。

真正值得学习的是：

> Asio 没有在每次业务状态访问前加锁，而是把“谁有资格执行这一批 handler”本身做成了一个显式的调度状态机。

固定实现中的核心对象只有几个字段：

~~~cpp
bool locked_;
bool shutdown_;

op_queue<scheduler_operation>
  waiting_queue_;

op_queue<scheduler_operation>
  ready_queue_;
~~~

看起来很简单，但这四个字段实际上构成了一个完整的：

~~~text
multi-producer
        ↓
logical single-owner
        ↓
batch handoff
        ↓
serial execution
~~~

协议。

理解它以后，你会发现 Strand 解决的并不是“怎么避免两个线程同时写一个变量”这么局部的问题，而是：

~~~text
怎样把一个业务对象的 mutable state
收束到一个逻辑执行域里
~~~

这对连接状态机、机器人设备 Session、控制命令串行化、网络协议状态和异步资源管理都非常有价值。

## 1. 先区分 Thread Ownership 与 Logical Ownership

假设：

~~~text
Thread A
Thread B
Thread C
~~~

都在执行：

~~~cpp
io_context.run();
~~~

同一个 TCP Session 同时收到：

- read completion；
- write completion；
- timeout；
- reconnect；
- 外部 command。

如果这些 handler 直接进入普通 executor：

~~~text
read handler  -> Thread A
write handler -> Thread B
timer handler -> Thread C
~~~

它们可能并行访问：

~~~cpp
Session::state_
Session::send_queue_
Session::retry_count_
Session::socket_
~~~

最直接的解决方案是：

~~~text
每个 handler
lock(session_mutex)
mutate
unlock
~~~

Strand 选择另一条路：

~~~text
所有改变 Session state 的操作
        ↓
先进入同一个 serial execution domain
        ↓
Runtime 保证这些 handler 不重叠
~~~

因此 Session 本身的许多状态不再需要每次访问都加业务 mutex。

## 2. Strand 不绑定某一个 OS Thread

Strand 的公共语义是：

~~~text
handlers may run on different OS threads
BUT
handlers from the same strand
do not execute concurrently
~~~

因此：

~~~text
serial execution
!=
thread affinity
~~~

今天：

~~~text
Handler A -> Thread 1
~~~

下一次：

~~~text
Handler B -> Thread 3
~~~

完全可以。

只要：

~~~text
A 与 B 不重叠执行
~~~

就满足 Strand 的核心约束。

## 3. strand 对象本身不是 serialization state

公开 `strand<Executor>` 保存：

~~~cpp
Executor executor_;
implementation_type impl_;
~~~

其中：

~~~cpp
implementation_type
=
shared_ptr<strand_impl>
~~~

真正的 serialization state 在：

~~~text
strand_impl
~~~

里。

所以复制 Strand：

~~~cpp
auto s2 = s1;
~~~

不是创建一个新的串行域。

因为：

~~~text
s1.impl_
==
s2.impl_
~~~

二者仍然引用同一个 ordered non-concurrent state。

## 4. Equality 比较的也是 serialization identity

源码：

~~~cpp
friend bool operator==(
    const strand& a,
    const strand& b) noexcept
{
  return a.impl_ == b.impl_;
}
~~~

因此两个 Strand 是否“相同”，不是比较：

~~~text
underlying thread
executor address
socket
~~~

而是比较：

~~~text
是否共享同一个 strand_impl
~~~

这是一种很干净的 execution-domain identity。

## 5. strand_impl 是真正的状态机

固定实现：

~~~cpp
class strand_impl
{
  slim_mutex mutex_;

  bool locked_;
  bool shutdown_;

  op_queue<scheduler_operation>
    waiting_queue_;

  op_queue<scheduler_operation>
    ready_queue_;

  strand_impl* next_;
  strand_impl* prev_;

  strand_executor_service* service_;
};
~~~

可以把这些字段分成三类。

第一类：

~~~text
mutex_
~~~

保护跨线程 submit 的共享元数据。

第二类：

~~~text
locked_
shutdown_
waiting_queue_
~~~

决定谁当前拥有逻辑执行权，以及谁还只能排队等待。

第三类：

~~~text
ready_queue_
~~~

属于当前逻辑 owner。

这就是 Strand 最重要的结构分层。

## 6. locked_ 不是 mutex 是否被锁

源码注释已经说得很明确：

~~~text
locked_ == true
~~~

表示：

~~~text
有 handler upcall 正在执行

或者

strand 本身已经被调度，
准备执行 pending handlers
~~~

所以：

~~~text
locked_
~~~

真正表达的是：

> **这个 serialization domain 的执行权已经被占用。**

不是：

~~~text
mutex_.try_lock() succeeded
~~~

这种 OS 同步状态。

## 7. 为什么需要同时存在 mutex_ 与 locked_

`mutex_` 解决：

~~~text
多个 producer 同时 submit 时
谁可以修改 shared metadata
~~~

`locked_` 解决：

~~~text
当前是否已经存在一个 logical owner
~~~

所以：

~~~text
mutex
=
短临界区的数据结构保护

locked_
=
跨越整个 handler batch 的逻辑所有权
~~~

这两个概念不能互换。

## 8. mutex 生命周期很短，logical ownership 生命周期很长

一次 submit 可能：

~~~text
lock mutex
check locked_
push queue
unlock mutex
~~~

只持续几十条指令。

但 `locked_ == true` 可以一直持续到：

~~~text
这一批 ready handler
全部执行结束
~~~

所以：

~~~text
critical section
~~~

和：

~~~text
serialization interval
~~~

是不同长度的时间窗口。

## 9. enqueue() 是 Strand 的核心线性化点

固定源码：

~~~cpp
bool strand_executor_service::enqueue(
    const implementation_type& impl,
    scheduler_operation* op)
{
  impl->lock_mutex();

  if (impl->shutdown_)
  {
    impl->unlock_mutex();
    op->destroy();
    return false;
  }
  else if (impl->locked_)
  {
    impl->waiting_queue_.push(op);
    impl->unlock_mutex();
    return false;
  }
  else
  {
    impl->locked_ = true;
    impl->unlock_mutex();

    impl->ready_queue_.push(op);
    return true;
  }
}
~~~

这段代码值得逐行读。

## 10. 第一种状态：已经 shutdown

~~~text
shutdown_ == true
~~~

新 operation 不再进入 Strand。

源码直接：

~~~cpp
op->destroy();
~~~

注意不是：

~~~text
调用用户 function
~~~

而是销毁 operation object。

这和 execution context shutdown 的语义一致：

~~~text
资源收尾时允许释放 pending handler object
而不保证进行用户 upcall
~~~

## 11. 第二种状态：locked_ == true

这意味着：

~~~text
已有 logical owner
~~~

新 operation：

~~~cpp
waiting_queue_.push(op);
~~~

然后 submitter 返回。

它不会再去 schedule 一个新的 invoker。

因为当前 owner 或已经被 schedule 的 invoker，最终会负责接管这些 waiting work。

## 12. 为什么不能每个 submit 都 post 一个 invoker

假设 100 个线程都向同一 Strand 提交 work。

如果每次都：

~~~text
enqueue handler
post strand invoker
~~~

底层 executor 会看到：

~~~text
100 个重复 invoker
~~~

它们最终还必须再次竞争：

~~~text
谁真正获得 strand execution ownership
~~~

Asio 反过来只让：

~~~text
第一个获得 logical lock 的 submitter
~~~

负责 schedule Strand。

其他 submitter：

~~~text
只把 work 放 waiting_queue
~~~

避免重复调度。

## 13. 第三种状态：locked_ == false

第一个 submitter：

~~~cpp
impl->locked_ = true;
~~~

这一步就是：

> 当前线程为这批 work 抢到了 Strand 的逻辑执行权。

但注意：

它并不会立刻执行用户 handler。

它只是获得：

~~~text
“负责建立下一次 invoker 执行”的资格
~~~

## 14. 最反直觉的一行：先 unlock，再 push ready_queue

源码：

~~~cpp
impl->locked_ = true;
impl->unlock_mutex();

impl->ready_queue_.push(op);
~~~

很多人看到这里会问：

> mutex 都释放了，为什么 `ready_queue_` 还能无锁 push？

答案就是 Strand 的设计核心。

## 15. ready_queue_ 不是普通共享队列

当：

~~~text
locked_ == true
~~~

之后，其他线程进入 `enqueue()`：

~~~text
lock mutex
see locked_ == true
→ only touch waiting_queue_
~~~

不会有人再碰：

~~~text
ready_queue_
~~~

所以刚刚那个抢到 logical ownership 的线程，虽然已经释放 mutex，但它拥有：

~~~text
ready_queue_ exclusive access
~~~

这是一种 **逻辑所有权代替数据结构锁** 的设计。

## 16. ready_queue_ 的安全来自 protocol，不来自容器本身

`op_queue` 只是一个普通 intrusive FIFO：

~~~cpp
Operation* front_;
Operation* back_;
~~~

没有内部 mutex。

它的线程安全完全取决于：

~~~text
who is allowed to touch it
~~~

所以一个容器是否线程安全，不能只看容器类本身。

必须看：

~~~text
ownership protocol
~~~

## 17. waiting_queue_ 才是真正的多 producer 汇聚点

多个外部线程：

~~~text
Producer A
Producer B
Producer C
~~~

都可能：

~~~text
lock impl mutex
push waiting_queue
unlock
~~~

因此 waiting queue 的访问必须在 mutex 保护下。

可以把结构理解成：

~~~text
many producers
    |
    v
waiting_queue
    |
    | batch handoff
    v
ready_queue
    |
    v
single logical owner
~~~

## 18. Strand 实际上是一个 MPSC → single-owner 转换器

虽然底层 `op_queue` 本身不是一个 lock-free MPSC queue，但在架构语义上：

~~~text
MPSC submit domain
        ↓
mutex protected waiting queue
        ↓
batch transfer
        ↓
single-owner ready queue
~~~

非常像一个 execution-domain adapter。

## 19. first == true 才会真正 schedule invoker

例如 `post()`：

~~~cpp
bool first = enqueue(impl, p.p);

if (first)
{
  asio::post(
      ex,
      allocator_binder<
        invoker<Executor>,
        Allocator>(
          invoker<Executor>(impl, ex),
          a));
}
~~~

所以：

~~~text
first == true
~~~

不只是：

~~~text
我是队首
~~~

而是：

> **我取得了 logical lock，并承担让 Strand 真正进入 underlying executor 的责任。**

## 20. Strand 调度给底层 executor 的不是用户 Handler，而是 invoker

实际进入底层 executor 的对象：

~~~text
invoker<Executor>
~~~

不是每一个用户 function。

这意味着：

~~~text
underlying executor
看到的是 Strand batch runner
~~~

而不是：

~~~text
每个用户 handler 都单独竞争底层 scheduler
~~~

这就是 Strand 的聚合执行模型。

## 21. invoker 自己持有 strand_impl 的 shared_ptr

`invoker` 保存：

~~~cpp
implementation_type impl_;
~~~

即：

~~~text
shared_ptr<strand_impl>
~~~

所以即使外部最后一个 `strand` 对象被销毁：

~~~text
已经 schedule 的 invoker
仍然拥有 strand_impl
~~~

不会出现：

~~~text
invoker 正准备运行
impl 已经 free
~~~

这是 execution object 对 serialization state 的显式 lifetime ownership。

## 22. invoker 还持有 underlying executor 的 work lease

新 Execution 模型：

~~~cpp
executor_(
  asio::prefer(
    ex,
    execution::outstanding_work.tracked))
~~~

TS executor 路径则：

~~~cpp
executor_work_guard<Executor>
  work_;
~~~

因此 scheduled Strand batch 本身会：

~~~text
keep underlying executor alive
~~~

直到 invoker 生命周期结束。

这与前一篇 [Work、Lifetime 与 Cancellation](work-lifetime-cancellation.md) 的 liveness 模型完全衔接。

## 23. 为什么 Strand 不能只保存一个 raw Executor*

因为一个已经被 schedule 的 batch 需要同时证明：

~~~text
strand_impl still alive

underlying execution context
still treats this invocation as work
~~~

所以：

~~~text
state lifetime
+
runtime liveness
~~~

是两笔不同的 ownership。

## 24. invoker::operator() 的主体非常短

~~~cpp
void operator()()
{
  on_invoker_exit on_exit = { this };

  impl_->service_
    ->run_ready_handlers(impl_);
}
~~~

真正复杂的逻辑被拆成：

~~~text
run ready batch

+

RAII exit handoff
~~~

这是一种非常漂亮的 Runtime 结构。

## 25. on_invoker_exit 是批次交接协议

析构：

~~~cpp
~on_invoker_exit()
{
  if (push_waiting_to_ready(
      this_->impl_))
  {
    ...

    execute/post(
      std::move(*this_));
  }
}
~~~

因此无论：

~~~text
ready batch 正常返回
~~~

还是：

~~~text
某个用户 handler 抛异常
~~~

只要 stack unwinding 发生，RAII guard 都会尝试完成：

~~~text
waiting → ready
~~~

以及：

~~~text
schedule next invoker
~~~

## 26. 这说明异常不能破坏 Strand 的 handoff invariant

如果没有 RAII：

~~~text
run ready handlers
handler throws
skip handoff
locked_ remains true
waiting work never scheduled
~~~

Strand 会永久卡死。

`on_invoker_exit` 的作用就是保证：

~~~text
batch completion protocol
~~~

不依赖所有用户代码正常返回。

## 27. run_ready_handlers() 为什么完全不加 Strand mutex

源码：

~~~cpp
void run_ready_handlers(
    implementation_type& impl)
{
  call_stack<strand_impl>
    ::context ctx(impl.get());

  while (
    scheduler_operation* o =
      impl->ready_queue_.front())
  {
    impl->ready_queue_.pop();

    o->complete(
      impl.get(),
      success_ec_,
      0);
  }
}
~~~

它直接无锁 drain：

~~~text
ready_queue_
~~~

因为此时 logical ownership 已经建立。

## 28. 用户 Handler 也不在 Strand mutex 下执行

这一点非常关键。

真实路径：

~~~text
enqueue side:
  mutex only protects metadata

invoker side:
  ready queue owned exclusively
  no strand mutex

user handler:
  no strand mutex
~~~

因此 handler 可以：

- 再次 post；
- dispatch；
- defer；
- 销毁业务状态；
- 调用其他异步 API。

不会因为“Strand 自己的 mutex”而产生最直接的 self-deadlock。

## 29. Strand 与 eCAL callback-lock 模型正好相反

之前 eCAL 固定实现的问题之一是：

~~~text
internal mutex
    |
    v
user callback
~~~

容易形成：

~~~text
callback
→ remove callback
→ same mutex
→ self-deadlock
~~~

Asio Strand 的核心规则则是：

~~~text
mutex protects queue transition
but never wraps arbitrary user callback
~~~

这是非常值得迁移的设计边界。

## 30. call_stack 才是“当前正在这个 Strand 中执行”的判据

`run_ready_handlers()` 一开始：

~~~cpp
call_stack<strand_impl>
  ::context ctx(impl.get());
~~~

这是一个 thread-local call stack。

它把当前：

~~~text
strand_impl*
~~~

压入当前线程的逻辑执行栈。

## 31. running_in_this_thread() 不检查 thread id

源码：

~~~cpp
return !!call_stack<strand_impl>
  ::contains(impl.get());
~~~

所以判定标准是：

~~~text
当前调用栈上
是否存在这个 serialization domain
~~~

不是：

~~~text
std::this_thread::get_id()
==
owner_thread
~~~

这再次说明 Strand 是 logical ownership。

## 32. 同一个 Strand 可以跨不同 run() Thread

例如：

~~~text
Batch 1
invoker runs on Thread A

Batch 2
invoker runs on Thread C
~~~

都没有问题。

因为每个时刻只有：

~~~text
one active logical owner
~~~

## 33. dispatch 的 inline 语义建立在 call_stack 上

`dispatch()`：

~~~cpp
if (running_in_this_thread(impl))
{
  function_type tmp(...);
  tmp();
  return;
}
~~~

如果当前已经位于这个 Strand：

~~~text
不重新排队
~~~

而是直接执行。

## 34. 这会产生受控的 Reentrancy

假设：

~~~text
Handler A
  |
  +-- strand.dispatch(B)
~~~

由于：

~~~text
A 已经在 strand call stack 中
~~~

B 可以直接：

~~~text
inline execute
~~~

于是：

~~~text
A begin
  B begin
  B end
A end
~~~

仍然没有并发。

但已经出现：

~~~text
nested execution
~~~

## 35. Strand 保证 Non-Concurrency，不保证“永不重入”

这是非常重要的区别。

~~~text
non-concurrent
~~~

表示：

~~~text
不会有两个线程同时执行
~~~

而：

~~~text
non-reentrant
~~~

表示：

~~~text
当前 handler 未返回前
不会再次进入同一状态机
~~~

Strand 默认只保证前者。

`dispatch()` 可以引入后者。

## 36. 对状态机代码，Reentrancy 可能比 Data Race 更隐蔽

例如：

~~~cpp
void Session::OnRead()
{
  phase_ = Parsing;

  strand_.dispatch(
    [this] {
      OnTimeout();
    });

  phase_ = Idle;
}
~~~

如果 `OnTimeout()` 假设：

~~~text
phase_ == Idle
~~~

就会出错。

即使：

~~~text
没有任何 data race
~~~

逻辑仍然可能不正确。

## 37. 所以 Strand 不能替代状态机设计

它解决：

~~~text
concurrent mutation
~~~

但不会自动解决：

~~~text
nested transition
illegal state transition
long handler
starvation
~~~

Runtime 只能提供 execution ordering。

业务 invariant 仍然要自己设计。

## 38. post() 为什么即使在当前 Strand 内也不 inline

`post()` 没有：

~~~text
running_in_this_thread()
~~~

快捷分支。

它永远：

~~~text
allocate operation
enqueue
~~~

如果当前 Strand 正 locked：

~~~text
→ waiting_queue_
~~~

所以：

~~~text
A
  post(B)
~~~

通常得到：

~~~text
A completes
        |
        v
current ready batch completes
        |
        v
handoff waiting -> ready
        |
        v
next invoker batch
        |
        v
B
~~~

## 39. post() 是打破当前调用栈的工具

因此当你明确希望：

~~~text
当前状态转换结束以后
再执行下一步
~~~

`post()` 比 `dispatch()` 更接近：

~~~text
enqueue-next-turn
~~~

这对 event-driven state machine 很重要。

## 40. defer() 也不在当前 Strand 内 inline

固定实现中 `defer()` 同样：

~~~text
allocate op
enqueue
~~~

首个 owner 再通过：

~~~cpp
asio::defer(ex, invoker)
~~~

交给 underlying executor。

它保留底层 executor 对 continuation/defer 的调度语义。

但 Strand 层本身仍然遵循：

~~~text
current locked batch
→ new work goes waiting
~~~

## 41. execute() 的 inline 条件更严格

新的 Execution API：

~~~cpp
if (
  query(ex, blocking)
    != blocking.never
  &&
  running_in_this_thread(impl))
{
  function();
  return;
}
~~~

如果 underlying executor 被要求：

~~~text
blocking.never
~~~

即使当前已经在 Strand 中，也不能直接内联。

这是 Strand 对 executor property 的尊重。

## 42. Strand 不应该偷偷违反 underlying executor contract

如果调用者显式要求：

~~~text
blocking.never
~~~

那么 Strand 不能说：

~~~text
“反正我现在已经是 owner，
直接调用就好了”
~~~

它仍要保持 executor 的 execution property。

这体现：

~~~text
serialization policy
~~~

与：

~~~text
blocking policy
~~~

是两个正交维度。

## 43. ready_queue 与 waiting_queue 的批次边界很关键

假设当前 ready batch：

~~~text
A
B
C
~~~

执行 A 时，其他线程又提交：

~~~text
D
E
F
~~~

由于：

~~~text
locked_ == true
~~~

D/E/F 只能进入：

~~~text
waiting_queue_
~~~

不会直接追加到当前：

~~~text
ready_queue_
~~~

所以当前批次仍然只执行：

~~~text
A B C
~~~

## 44. 这阻止外部 producer 无限延长当前 batch

如果所有新 work 都直接追加到 ready queue：

~~~text
A running
producer adds D
B running
producer adds E
C running
producer adds F
...
~~~

一个高频 producer 可能让：

~~~text
current invoker
永远 drain 不完
~~~

waiting/ready 分层形成一个明确的：

~~~text
batch boundary
~~~

## 45. push_waiting_to_ready() 就是 batch handoff

源码：

~~~cpp
impl->lock_mutex();

impl->ready_queue_
  .push(impl->waiting_queue_);

bool more_handlers =
  impl->locked_ =
    !impl->ready_queue_.empty();

impl->unlock_mutex();

return more_handlers;
~~~

可以拆成：

~~~text
freeze producer-visible waiting set
        |
        v
splice waiting batch
into owner-ready queue
        |
        v
decide whether logical lock remains held
~~~

## 46. op_queue::push(other_queue) 是 O(1) 拼接

`op_queue` 保存：

~~~cpp
front_
back_
~~~

拼接整个 queue：

~~~text
old ready.back -> waiting.front
ready.back = waiting.back

waiting.front = null
waiting.back = null
~~~

不需要逐节点搬运。

因此：

~~~text
batch handoff
~~~

本身非常便宜。

## 47. 这也是 Intrusive Queue 的价值

每个 operation 自己带：

~~~text
next_
~~~

所以 Strand 不需要额外：

~~~text
std::list node
shared allocation
queue wrapper object
~~~

`executor_op` 本身就是：

~~~text
scheduler operation
+
handler storage
+
queue node
~~~

这种 intrusive 设计很适合 Runtime fast path。

## 48. waiting → ready 之后为什么继续 locked_ = true

只要：

~~~text
ready_queue 非空
~~~

说明新的 batch 已经获得 logical ownership。

所以：

~~~text
locked_ = true
~~~

会跨越：

~~~text
current invoker exit
→ next invoker schedule
→ next invoker start
~~~

中间即使暂时没有 OS thread 正在执行用户 handler，serialization domain 也不能被别的 submitter 抢走。

## 49. locked_ 覆盖“scheduled but not executing”窗口

这和生命周期问题中的 pre-entry window 非常相似。

如果只在 handler 真正开始时才：

~~~text
locked_ = true
~~~

那么：

~~~text
invoker 已 schedule
但尚未执行
~~~

期间可能有另一个 submitter也认为 Strand free。

Asio 选择：

~~~text
在 schedule responsibility 被取得时
就占住 logical lock
~~~

因此：

~~~text
execution eligibility
~~~

比：

~~~text
actual execution
~~~

更早被纳入 serialization protocol。

## 50. 这和 libzmq seqnum 的思路有共性

libzmq：

~~~text
future command eligibility
        ↓
reserve target lifetime
        ↓
later actual execution
~~~

Asio Strand：

~~~text
future batch execution responsibility
        ↓
set locked_
        ↓
later invoker actual execution
~~~

两者都说明：

> 并发系统必须保护“已经获得未来执行资格”的阶段，而不只是“当前正在 CPU 上执行”的阶段。

## 51. on_invoker_exit 为什么不是直接继续 while(waiting)

一种更简单的写法似乎是：

~~~text
run ready
lock
move waiting -> ready
unlock
goto run ready
~~~

Asio 没这么做。

它选择：

~~~text
run current batch
        ↓
handoff
        ↓
repost invoker
~~~

这会把执行权重新交给 underlying executor。

## 52. 重新 post 给底层 executor 有公平性意义

如果 Strand 永远自己：

~~~text
waiting -> ready -> continue
~~~

高负载单 Strand 可能一直霸占一个 worker。

重新 schedule：

~~~text
给 underlying executor
重新进行一次调度决策
~~~

让其他 ready work 有机会获得 CPU。

所以：

~~~text
serialization
~~~

和：

~~~text
fair scheduling
~~~

仍然分层处理。

## 53. 但 current ready batch 本身可能很大

注意批次边界只把：

~~~text
执行期间新来的 work
~~~

隔离到 waiting。

如果 invoker 真正开始之前，已经有很多 work 被归入 ready batch，当前 batch 仍可能很大。

因此 Strand 不是严格 time-slice scheduler。

它只是：

~~~text
serial executor
~~~

## 54. 长 Handler 仍然会阻塞整个 Strand

如果：

~~~text
A takes 100 ms
~~~

那么同 Strand 的：

~~~text
B C D E
~~~

都必须等。

所以：

~~~text
Strand
!=
parallel execution accelerator
~~~

它用串行化换取状态简单性。

CPU-heavy work 应该：

~~~text
offload to worker pool
        ↓
completion post back to strand
~~~

## 55. Strand 适合“短状态转换”

典型 handler：

~~~text
read completion
→ parse header
→ update protocol state
→ schedule next read

timer
→ check deadline
→ transition state

command
→ mutate mode
→ enqueue output
~~~

这些工作应尽量短。

真正大计算：

~~~text
图优化
神经网络推理
大矩阵
压缩
长时间磁盘操作
~~~

不应该堵在 Strand 上。

## 56. Strand 并不保证所有外部访问都安全

只有通过：

~~~text
same strand
~~~

提交的 mutation 才被串行化。

如果另一个线程绕过 Strand：

~~~cpp
session.state_ = Closed;
~~~

那仍然会和 Strand handler 形成 data race。

所以执行所有权必须是一条架构规则：

~~~text
all mutable state transitions
must enter via owner executor
~~~

而不是局部“偶尔用 strand”。

## 57. Logical Owner 模型要求 API 也围绕 ownership 设计

更合理的 Session API：

~~~cpp
void RequestClose()
{
  asio::post(
    strand_,
    [self = shared_from_this()] {
      self->DoClose();
    });
}
~~~

而不是：

~~~cpp
void Close()
{
  state_ = Closed;
}
~~~

前者明确：

~~~text
caller submits intent
owner performs mutation
~~~

后者让任何线程都可以直接修改 state。

## 58. 这和 Actor 模型很接近

一个 Strand + State Object 可以看成：

~~~text
actor mailbox-ish submit path
+
single logical execution owner
+
mutable private state
~~~

区别是：

- mailbox 结构隐藏在 executor/operation queue 内；
- handler 可以来自 I/O completion；
- execution thread 不固定；
- `dispatch` 允许受控 inline reentrancy。

所以它不是严格 Actor，但思路高度相似。

## 59. Strand copy 共享 state，很适合把 execution capability 传给不同模块

比如：

~~~text
TCP reader
Timer
Command interface
Retry manager
~~~

都可以持有同一个 Strand 的 copy。

这些对象彼此不共享 mutex。

它们共享的是：

~~~text
同一个 serialization capability
~~~

这是 capability-oriented design。

## 60. serialization capability 比暴露 mutex 更容易验证

如果对象暴露：

~~~cpp
std::mutex& mutex();
~~~

调用者必须自己记住：

~~~text
哪些字段需要锁
锁顺序是什么
callback 内能不能重入
~~~

如果只暴露：

~~~text
PostToOwner(...)
~~~

系统 invariant 更集中。

## 61. Strand service 还维护所有 strand_impl 的 raw linked list

service 字段：

~~~cpp
strand_impl* impl_list_;
~~~

每个 impl 保存：

~~~cpp
next_
prev_
service_
~~~

这张链表的主要用途之一是：

~~~text
execution_context shutdown
~~~

时枚举所有 Strand。

## 62. 为什么 service list 用 raw pointer，而 strand 用 shared_ptr

真正 ownership：

~~~text
strand copies
invoker
~~~

由：

~~~text
shared_ptr<strand_impl>
~~~

负责。

service 的：

~~~text
impl_list_
~~~

更像：

~~~text
non-owning registry
~~~

用于：

~~~text
shutdown enumeration
~~~

因此：

~~~text
registry membership
!=
memory ownership
~~~

这也是很重要的 Runtime 数据结构设计。

## 63. strand_impl 析构会主动从 registry 摘除自己

析构：

~~~cpp
lock(service_->mutex_);

if (service_->impl_list_ == this)
  service_->impl_list_ = next_;

if (prev_)
  prev_->next_ = next_;

if (next_)
  next_->prev_ = prev_;
~~~

所以：

~~~text
shared ownership reaches zero
~~~

时，impl 会同步维护 service registry。

## 64. Service Shutdown 与普通 Handler Cancel 语义不同

`strand_executor_service::shutdown()`：

~~~cpp
lock(service mutex)

for each impl:
  lock impl
  shutdown_ = true
  ops.push(waiting_queue_)
  ops.push(ready_queue_)
  unlock impl
~~~

局部：

~~~text
ops
~~~

离开函数后，其 `op_queue` 析构会：

~~~text
destroy pending operations
~~~

而不是调用用户 function。

## 65. shutdown_ 同时关闭新的 enqueue

之后新 submit：

~~~text
enqueue
→ see shutdown_
→ op->destroy()
~~~

所以 shutdown 做了两件事：

~~~text
RETIRE
  reject new work

RECLAIM PENDING HANDLER OBJECTS
  destroy waiting/ready operations
~~~

但这属于 execution_context 销毁协议。

不能和业务层：

~~~text
cancel one Session
~~~

混为一谈。

## 66. strand shutdown 不是业务 graceful shutdown

它不会保证：

~~~text
每个 handler 收到 operation_aborted
~~~

而是：

~~~text
execution context is going away
pending function objects are destroyed
~~~

业务若需要：

~~~text
close protocol
flush
ack
final state callback
~~~

应在 execution context teardown 之前完成。

## 67. run_ready_handlers() 为什么传 impl.get() 作为 owner

~~~cpp
o->complete(
  impl.get(),
  success_ec_,
  0);
~~~

`owner != nullptr` 告诉 `executor_op::do_complete()`：

~~~text
这是正常 execution
应该进行 user upcall
~~~

如果：

~~~text
op->destroy()
~~~

则内部传：

~~~text
owner == nullptr
~~~

handler object 被释放，但不调用用户 function。

这是 Asio operation type erasure 的统一 destroy/complete contract。

## 68. executor_op 会先释放 operation storage，再调用用户 function

`executor_op::do_complete()`：

~~~text
move/copy handler out
        ↓
release operation allocation
        ↓
if owner != nullptr
  invoke handler
~~~

所以 Strand 自己的 operation node 在真正用户回调开始时可以已经不存在。

这减少：

~~~text
内部 allocation
跨越用户 callback
~~~

的生命周期。

## 69. Strand serialization 的对象不是 operation node，而是 execution right

因此即使：

~~~text
executor_op 已释放
~~~

当前 handler 仍然处于：

~~~text
strand call_stack context
~~~

且：

~~~text
locked_ == true
~~~

serialization invariant 仍然成立。

这再次说明：

~~~text
memory object lifetime
!=
logical execution ownership
~~~

## 70. 为什么 ready_queue 可以完全无锁 drain

因为真正 invariant 是：

~~~text
locked_ == true
AND
exactly one invoker owns ready_queue
~~~

不是：

~~~text
ready_queue uses thread-safe container
~~~

这是“通过 ownership 消除同步”的经典设计。

## 71. 这比给 ready_queue 自己加 mutex 更强

如果每次：

~~~text
front
pop
~~~

都锁 mutex：

- 增加 cache-line contention；
- handler 间多一次同步；
- 让用户回调附近更难推断锁状态。

Asio 把锁集中在：

~~~text
producer-to-owner handoff
~~~

然后 owner fast path：

~~~text
lock-free from protocol perspective
~~~

## 72. 真正的性能优化来自减少共享，而不是更快的 mutex

Strand 的性能思想可以压成：

~~~text
shared phase:
  short critical section
  enqueue waiting

owner phase:
  exclusive ready queue
  no shared lock per handler
~~~

这比单纯换：

~~~text
spin mutex
futex
adaptive mutex
~~~

更重要。

## 73. slim_mutex 只是实现层优化

固定构建支持：

~~~text
std atomic wait
or futex
~~~

时，每个 `strand_impl` 可以有自己的：

~~~text
slim_mutex
~~~

否则 Asio 会从：

~~~text
num_mutexes = 193
~~~

的共享 mutex pool 中选一个。

但这只是：

~~~text
metadata handoff lock implementation
~~~

不是 Strand serial execution 的本质。

## 74. 如果多个 Strand 共用一把 pooled mutex，会不会变成同一个 Strand

不会。

共享 mutex 只意味着：

~~~text
enqueue metadata critical section
~~~

偶尔互相竞争。

真正 serialization identity 仍然是：

~~~text
strand_impl
locked_
queues
~~~

所以：

~~~text
same mutex
!=
same strand
~~~

## 75. ready/waiting queue 的 FIFO 应该怎样理解

`op_queue::push()` 是尾插 FIFO。

因此在一个确定的 enqueue linearization order 下：

~~~text
先进入 queue 的 operation
先被 drain
~~~

但多个线程并发提交时：

~~~text
谁先获得 impl mutex
~~~

定义了实际 linearization order。

不能把 wall-clock 上“几乎同时调用”误认为一个更强的全局顺序保证。

## 76. dispatch inline 还能改变直觉上的 FIFO

假设 waiting 中已有：

~~~text
B
~~~

当前 handler A 内：

~~~cpp
strand.dispatch(C);
~~~

因为 A 已在 Strand 内：

~~~text
C inline executes now
~~~

于是执行顺序：

~~~text
A start
C
A end
...
B later
~~~

所以：

> Strand 的核心契约是 non-concurrency，不应把它简单理解成所有 submission 的严格全局 FIFO。

## 77. 如果业务要求严格“下一事件”语义，应偏向 post

状态机里希望：

~~~text
A 完整结束
然后 B
~~~

而不是：

~~~text
A 中间重入 B
~~~

更适合：

~~~text
post
~~~

让 B 进入下一批。

这是一条非常实用的工程规则。

## 78. Strand 与 Mutex 的真正差异

Mutex 模型：

~~~text
Thread A:
  lock
  mutate X
  unlock

Thread B:
  lock
  mutate X
  unlock
~~~

Strand 模型：

~~~text
Thread A:
  submit intent A

Thread B:
  submit intent B

Runtime:
  choose logical owner
  execute A
  execute B
~~~

前者同步的是：

~~~text
memory access
~~~

后者同步的是：

~~~text
execution order
~~~

## 79. Execution Ordering 往往更适合状态机

状态机真正关心：

~~~text
Event 1
then
Event 2
then
Event 3
~~~

而不是：

~~~text
字段 foo 每次写之前要 lock
~~~

Strand 把并发问题提升成：

~~~text
event sequencing
~~~

这和事件驱动系统本身的语义更一致。

## 80. 机器人 MotorSession 是非常典型的映射

例如一个电机 Session：

~~~text
CAN RX
control command
timeout
fault clear
mode switch
~~~

如果都能影响：

~~~text
mode_
last_feedback_
pending_command_
fault_state_
~~~

可以统一：

~~~text
all events
    |
    v
MotorSession strand
    |
    v
state machine
~~~

## 81. CAN 接收线程不需要直接修改 MotorSession

接收线程：

~~~text
decode frame
        |
        v
post feedback event
to session strand
~~~

然后：

~~~text
Session owner
updates state
~~~

这样：

~~~text
CAN RX thread
control planner thread
timeout thread
~~~

不需要共同持有 Session mutex。

## 82. 控制实时性仍然要单独设计

Strand 并不保证：

~~~text
hard real-time latency
~~~

它只保证：

~~~text
serialization
~~~

如果某个 handler：

~~~text
malloc
blocking I/O
large computation
long logging
~~~

会拖延后续事件。

所以机器人 Runtime 中常见更合理的分层：

~~~text
hard RT loop
  fixed ownership / lock-free data path

async management runtime
  strand / executor serialized state
~~~

不要把 Strand 当成硬实时调度器。

## 83. Strand 解决“谁修改状态”，不是“什么时候必须执行”

Deadline / priority / RT scheduling：

~~~text
属于 scheduler policy
~~~

Strand：

~~~text
属于 serialization policy
~~~

两者是不同轴。

## 84. 复杂系统里可以同时存在多个 Strand

例如：

~~~text
Motor A strand
Motor B strand
Telemetry strand
Network Session strand
~~~

这样：

~~~text
每个对象内部串行
对象之间仍可并行
~~~

比一把：

~~~text
global mutex
~~~

拥有更好的并发结构。

## 85. Strand 粒度太粗也会退化

如果整个机器人 Runtime 全放到：

~~~text
one global strand
~~~

那么：

~~~text
所有业务逻辑
完全串行
~~~

性能和延迟都可能恶化。

正确问题是：

> 哪一组 mutable state 必须共享同一个 logical owner？

这决定 Strand 的粒度。

## 86. Strand 粒度实际上就是 State Ownership Boundary

如果两个状态必须原子地共同变化：

~~~text
A.state
B.state
~~~

可能需要：

~~~text
同一个 execution owner
~~~

如果彼此独立：

~~~text
不同 strand
~~~

可以并行。

因此 Strand 设计本质上是：

~~~text
state partitioning
~~~

问题。

## 87. 一个好的模块接口会把 owner boundary 显式化

例如：

~~~cpp
class MotorSession
{
public:
  void PostCommand(Command c);

private:
  void DoCommand(Command c);

  Strand strand_;
  State state_;
};
~~~

公开 API：

~~~text
submit
~~~

私有 API：

~~~text
mutate
~~~

这比暴露一个 mutex 更容易维护。

## 88. 如何验证一段 Strand 代码

第一问：

~~~text
所有 mutable state mutation
是否真的都经过同一 Strand？
~~~

第二问：

~~~text
有没有 handler 内 dispatch
导致意外 reentrancy？
~~~

第三问：

~~~text
有没有 blocking / long CPU work
堵住整个 serialization domain？
~~~

第四问：

~~~text
对象 lifetime 是否覆盖
已排队的 handler？
~~~

第五问：

~~~text
shutdown 时还有没有新 work
可以继续进入？
~~~

## 89. Strand 不自动解决业务对象 lifetime

即使 execution 被完全串行：

~~~text
queued lambda
captures raw this
~~~

如果对象本身先析构：

~~~text
handler later runs
→ dangling this
~~~

仍然会 UAF。

所以还必须设计：

- `shared_from_this()`；
- owner-lifetime join；
- weak_ptr validation；
- explicit drain；
- generation token。

Strand 只解决 execution overlap。

## 90. invoker shared_ptr 只保护 strand_impl，不保护业务对象

这是很容易混淆的一层。

~~~text
invoker.impl_
~~~

保证：

~~~text
strand serialization state alive
~~~

但不保证：

~~~text
Session alive
~~~

如果 handler 捕获的是：

~~~text
Session*
~~~

业务对象 lifetime 仍要单独证明。

## 91. Work Guard 也只保护 Runtime Liveness

同理：

~~~text
invoker.work_
~~~

保证：

~~~text
underlying executor stays logically alive
~~~

它不拥有：

~~~text
socket payload session
~~~

所以 Asio 中至少有三种 ownership：

~~~text
strand_impl ownership
runtime work ownership
application object ownership
~~~

不能混在一起。

## 92. Shutdown 的安全也依赖执行上下文整体生命周期

`strand_executor_service::shutdown()` 是 execution context destruction protocol 的一部分。

`execution_context` 文档要求派生 context 析构时：

~~~text
shutdown services
then
destroy services
~~~

所以 Strand service shutdown 不应该被理解成普通热路径 API。

它属于：

~~~text
whole runtime teardown
~~~

## 93. 这和普通 Session Close 的设计完全不同

Session Close 应该：

~~~text
retire new application work
cancel/finish I/O
drain handlers
release Session
~~~

而 Strand service shutdown：

~~~text
整个 execution context 正在消失
直接 destroy pending executor_ops
~~~

是更外层的系统边界。

## 94. Strand 的核心不变量可以写成五条

第一条：

\[
locked = false
\Rightarrow
\text{no current/scheduled logical owner}
\]

第二条：

\[
locked = true
\Rightarrow
\text{new external submissions enter waiting}
\]

第三条：

~~~text
ready_queue
is accessed only by current logical owner
~~~

第四条：

~~~text
waiting_queue
is modified only under impl mutex
~~~

第五条：

~~~text
at most one invoker chain
owns the strand at a time
~~~

这五条足以解释绝大多数实现。

## 95. 为什么 locked_ 可以是普通 bool

因为所有跨线程对 `locked_` 的检查和修改都发生在：

~~~text
impl mutex
~~~

之下。

运行 ready handlers 时：

~~~text
不需要读取/修改 locked_
~~~

只依赖已经建立好的 owner invariant。

因此不需要：

~~~text
atomic<bool> locked_
~~~

把所有同步都推到 memory order 层。

## 96. 这是“锁保护 ownership transition，无锁执行 owned state”

可以把 Strand 设计压缩成：

~~~text
shared transition
  under mutex

exclusive phase
  no mutex

shared handoff
  under mutex
~~~

这是一种非常通用的并发结构。

## 97. 类似模式在很多 Runtime 中都能看到

例如：

~~~text
event loop posted queue
reactor ready list
actor mailbox drain
work-stealing local deque
batch consumer
single-writer database shard
~~~

只要满足：

~~~text
共享提交
→ 获取 owner
→ owner 私有执行
→ handoff
~~~

都可以使用类似设计。

## 98. 和“每个字段加 mutex”相比，它更接近第一性原理

第一性问题不是：

> 这个变量应该用哪一把锁？

而是：

> 为什么这么多线程都需要直接修改这个状态？

如果答案是：

~~~text
其实不需要
只需要把事件发送给唯一 owner
~~~

那最好的锁可能就是：

~~~text
不让共享写发生
~~~

Strand 就是在 execution layer 做这个事情。

## 99. 一张完整时序图

~~~text
Producer A                    Producer B

post(A)
  |
  | lock impl
  | locked == false
  | locked = true
  | unlock
  | ready.push(A)
  |
  +---- schedule invoker ----+
                             |
                             v
                        Executor Worker
                             |
                             | call_stack push
                             |
                        run ready:
                             A
                             |
                             | while A runs:
                             |
Producer B                   |
post(B)                      |
  |                          |
  | lock impl                |
  | locked == true           |
  | waiting.push(B)          |
  | unlock                   |
  |                          |
return                       |
                             |
                        A returns
                             |
                        ready empty
                             |
                        on_invoker_exit
                             |
                        lock impl
                             |
                        splice waiting
                          -> ready
                             |
                        locked = true
                             |
                        unlock
                             |
                        repost invoker
                             |
                             v
                        next batch:
                             B
                             |
                             v
                        ready empty
                             |
                        handoff
                             |
                        waiting empty
                             |
                        locked = false
~~~

## 100. 如果 A 内部 dispatch(C)

时序会变成：

~~~text
A begin
  |
  +-- dispatch(C)
        |
        | call_stack contains impl
        v
      C executes inline
        |
      C returns
  |
A continues
  |
A returns
~~~

所以：

~~~text
C
~~~

不会进入 waiting batch。

这正是 reentrancy 语义。

## 101. 如果 A 内部 post(C)

则：

~~~text
A begin
  |
  +-- post(C)
        |
        | locked == true
        v
      waiting.push(C)
  |
A returns

current ready batch finishes
        |
        v
handoff
        |
        v
next invoker runs C
~~~

所以：

~~~text
post
~~~

天然形成 next-turn boundary。

## 102. 对业务状态机的实际建议

如果一个函数只是：

~~~text
“允许立即嵌套执行也没问题”
~~~

可以接受 `dispatch` / inline execute。

如果一个状态转换必须：

~~~text
当前 transaction 完整结束后
才进入下一事件
~~~

更适合 `post`。

不要机械地把它们当成性能 API 的差别。

它们会改变状态机时序。

## 103. 这和协程中的 continuation scheduling 也非常像

协程恢复策略常常要决定：

~~~text
inline resume

or

enqueue resume
~~~

inline：

~~~text
低延迟
但可能深递归 / reentrancy
~~~

enqueue：

~~~text
多一次调度
但形成明确边界
~~~

Strand 的 dispatch/post 差异就是类似问题。

## 104. 一个更现代的 SerialExecutor 也可以沿用这套模型

概念骨架：

~~~cpp
class SerialExecutor
{
  Mutex mutex;
  bool owned = false;

  IntrusiveQueue waiting;
  IntrusiveQueue ready;

  void Submit(Operation* op)
  {
    bool first = false;

    {
      Lock l(mutex);

      if (owned)
        waiting.push(op);
      else
      {
        owned = true;
        first = true;
      }
    }

    if (first)
    {
      ready.push(op);
      ScheduleInvoker();
    }
  }
};
~~~

这只是概念骨架。

真实实现还要处理：

- shutdown；
- allocator；
- executor work；
- exception；
- inline dispatch；
- lifetime；
- fairness。

## 105. 真正重要的是 protocol proof

写这种代码时，必须能证明：

~~~text
为什么 unlock 后还能写 ready_queue？

为什么不会存在两个 invoker？

为什么异常不会让 locked_ 永远 true？

为什么 new work 不会丢？

为什么 shutdown 后 handler 不会继续入队？
~~~

如果这些问题说不清，代码即使“测试跑了很久没出错”也不代表正确。

## 106. Strand 的 Proof Sketch

### 唯一 owner

只有看到：

~~~text
locked_ == false
~~~

的 submitter 能设置：

~~~text
locked_ = true
~~~

而检查与设置都在同一 mutex 下。

所以不可能两个线程同时从 false 获取 owner。

### 新提交隔离

owner 存在时：

~~~text
locked_ == true
~~~

所有 producer 只能：

~~~text
waiting.push
~~~

不会修改 ready。

### owner 批次独占

ready queue 仅 owner/invoker 链访问。

所以 drain 不需要 mutex。

### handoff 原子化

waiting → ready 与：

~~~text
locked_ next state
~~~

在同一 impl mutex critical section 内完成。

因此不会出现：

~~~text
waiting 已有 work
但 locked_ 错误变 false
~~~

### 异常安全

`on_invoker_exit` RAII 负责 handoff/repost。

因此用户 handler 抛异常不会直接破坏 owner protocol。

## 107. 这套证明比“用了 mutex 所以线程安全”强得多

“有 mutex”只能证明：

~~~text
某些临界区互斥
~~~

真正要证明的是：

~~~text
所有跨阶段状态转换
在每种 interleaving 下
都维持不变量
~~~

Strand 是一个很好的小型并发协议教材。

## 108. 和前几篇生命周期专题放在一起

eCAL：

~~~text
内部锁覆盖 user callback
→ reentrancy deadlock
~~~

LCM C++：

~~~text
callback eligibility 已建立
但 userdata owner 回收过早
→ UAF
~~~

libzmq：

~~~text
future command eligibility
先建立 seqnum reservation
→ lifetime quiescence
~~~

Asio Work：

~~~text
pending async operation
先建立 outstanding work debt
→ completion liveness
~~~

Asio Strand：

~~~text
future serial execution responsibility
先设置 locked_
→ logical ownership serialization
~~~

它们实际上都在研究同一个更大的问题：

> **一个动作在真正执行之前，系统如何记录“它已经获得未来执行资格”，并让其他线程尊重这个事实？**

## 109. 对机器人中间件最可迁移的结论

第一：

> Mutable state 不一定需要共享锁；可以把 mutation 权收束到 logical owner。

第二：

> “当前没有线程正在执行”不代表 execution domain 空闲；scheduled-but-not-running 也必须算 owned。

第三：

> 外部 producer 与内部 owner 最好使用不同队列/不同访问规则。

第四：

> 用户代码应在 Runtime 内部锁之外执行。

第五：

> Inline dispatch 与 queued post 是状态机语义差异，不只是性能差异。

第六：

> Serial executor 不等于业务对象 lifetime manager。

第七：

> 批次 handoff 可以减少共享锁开销，并给底层 scheduler 留出公平调度机会。

## 110. 最终模型

可以把 Asio Strand 压成下面这张图：

~~~text
              concurrent submitters
          A         B         C
           \        |        /
            \       |       /
             v      v      v
          +-------------------+
          | impl mutex        |
          |                   |
          | locked_ ?         |
          +---------+---------+
                    |
         +----------+----------+
         |                     |
     locked=false           locked=true
         |                     |
         v                     v
   acquire logical       waiting_queue_
      ownership
         |
         v
    ready_queue_
         |
         v
 schedule one invoker
         |
         v
  call_stack marks
  current strand owner
         |
         v
 drain ready without
 strand mutex
         |
         v
 user handlers
         |
         v
 RAII on_invoker_exit
         |
         v
 lock impl mutex
         |
         v
 waiting -> ready
         |
         v
 more work?
   |             |
  yes            no
   |             |
   v             v
repost       locked_=false
invoker
~~~

Strand 的精髓不是：

~~~text
“它有一把锁”
~~~

而是：

> **那把锁只负责所有权交接；真正执行阶段依靠已经建立的逻辑所有权，从共享状态退化成单 owner 状态。**

这才是它比“每个 callback 自己 lock mutex”更值得学习的地方。
