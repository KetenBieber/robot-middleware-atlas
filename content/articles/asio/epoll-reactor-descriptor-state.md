# epoll Reactor 与 descriptor_state：从 Readiness Bit 到用户 Completion 的完整状态机

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

Linux `epoll` 只告诉用户态一件事：

~~~text
某个 fd 的某类 readiness 发生了变化
~~~

它并不知道：

- 哪个 C++ handler 在等；
- 这是 `async_read_some`、`async_write_some` 还是 `async_wait`；
- 一个 fd 上排了几条 operation；
- 某个 operation 是否已经可以 speculative completion；
- cancel 后应该如何把 operation 转成 completion；
- readiness 被消耗以后，是否还值得继续 speculative syscall；
- handler 应该在哪个 executor 上执行；
- Runtime 的 outstanding work 应该怎样结算。

Asio 在 Linux Reactor 中间加了一层：

~~~text
epoll fd
    ↓
descriptor_state
    ↓
per-readiness operation queues
    ↓
reactor_op::perform()
    ↓
Scheduler completion
    ↓
handler_work
    ↓
user handler
~~~

这一层不是普通的“fd 元数据”。

`descriptor_state` 同时承担：

~~~text
kernel registration shadow
per-fd synchronization
pending operation ownership
readiness aggregation
Scheduler control operation
speculative-I/O policy
lifecycle / shutdown state
~~~

理解这一个对象，几乎就能把 Reactor 模型从“epoll API 使用法”提升到真正的 Runtime 设计层。

---

## 1. 最重要的事实：descriptor_state 自己就是一个 Scheduler Operation

定义：

~~~cpp
struct descriptor_state : operation
{
  descriptor_state* next_;
  descriptor_state* prev_;

  mutex mutex_;
  epoll_reactor* reactor_;
  int descriptor_;
  uint32_t registered_events_;

  op_queue<reactor_op>
    op_queue_[max_ops];

  bool try_speculative_[max_ops];
  bool shutdown_;
};
~~~

它继承：

~~~text
operation
~~~

这意味着：

> **一个 fd 的 readiness control block 本身可以被放进 Scheduler queue。**

所以 epoll 返回以后，Asio 不需要立刻执行用户 handler。

它可以先把：

~~~text
descriptor_state*
~~~

当成一个普通 Scheduler operation 发布。

---

## 2. Kernel Readiness 与 User Completion 被拆成两级调度

真实路径不是：

~~~text
epoll_wait
→ callback
~~~

而是：

~~~text
epoll_wait
        ↓
descriptor_state ready
        ↓
Scheduler queue
        ↓
descriptor_state::do_complete
        ↓
descriptor_state::perform_io
        ↓
reactor_op ready
        ↓
user operation completion
~~~

这多出来的一层非常关键。

它让：

- Reactor 只负责检测 readiness；
- descriptor_state 负责把 readiness 消费成 operation progress；
- Scheduler 负责执行 completion；
- associated executor 再负责最终 handler dispatch。

这是明显的职责分层。

---

## 3. 一个 fd 为什么不能只保存一个 callback

同一个 descriptor 可以同时存在：

~~~text
pending read
pending write
pending exceptional condition
~~~

所以 Reactor 定义：

~~~cpp
enum op_types
{
  read_op = 0,
  write_op = 1,
  connect_op = 1,
  except_op = 2,
  max_ops = 3
};
~~~

这里：

~~~text
connect_op == write_op
~~~

因为 non-blocking connect 的完成通常由：

~~~text
writability / error
~~~

表达。

---

## 4. 三条 Queue 对应三类 Readiness Contract

核心：

~~~cpp
op_queue<reactor_op>
  op_queue_[max_ops];
~~~

可理解成：

~~~text
op_queue_[0]
→ read-like operations

op_queue_[1]
→ write/connect-like operations

op_queue_[2]
→ exception/OOB operations
~~~

这样收到：

~~~text
EPOLLIN
~~~

时，不需要遍历所有 operation 判断类型。

直接处理：

~~~text
read queue
~~~

即可。

---

## 5. descriptor_state 是 User-space Shadow State

Kernel 里有：

~~~text
epoll interest set
fd readiness
~~~

用户态 `descriptor_state` 保存：

~~~text
descriptor_
registered_events_
pending operations
shutdown_
speculative policy
~~~

它本质上是：

> **Kernel fd registration 在用户态的控制块。**

这种模式在很多系统都存在：

~~~text
fd
→ connection object

DMA queue
→ queue state object

GPU stream
→ stream runtime state

CAN device
→ device session
~~~

OS handle 自己的信息通常不足以支撑上层 Runtime。

---

## 6. registered_events_ 是 Kernel Interest 的用户态镜像

普通 descriptor 注册时：

~~~cpp
ev.events =
    EPOLLIN
  | EPOLLERR
  | EPOLLHUP
  | EPOLLPRI
  | EPOLLET;

descriptor_data
  ->registered_events_
  = ev.events;
~~~

注意：

~~~text
初始没有 EPOLLOUT
~~~

这是故意的。

---

## 7. 为什么默认不监听 EPOLLOUT

大多数 socket 在大量时间里都是：

~~~text
writable
~~~

如果长期 level-trigger 监听 writable：

~~~text
会产生大量无意义 wakeup
~~~

即便 Asio 使用 ET：

~~~text
只有真的出现 pending write 时
才打开 EPOLLOUT interest
~~~

仍然更符合：

~~~text
interest follows demand
~~~

的设计。

---

## 8. 第一个 Pending Write 会动态扩展 epoll Interest

`start_op(write_op, ...)` 中，如果 speculative write 没完成：

~~~cpp
if ((registered_events_ & EPOLLOUT) == 0)
{
  ev.events =
    registered_events_ | EPOLLOUT;

  epoll_ctl(
    epoll_fd_,
    EPOLL_CTL_MOD,
    descriptor,
    &ev);

  registered_events_
    |= ev.events;
}
~~~

所以：

~~~text
application demand
        ↓
pending write exists
        ↓
kernel interest expands
~~~

这是一种 demand-driven registration。

---

## 9. Reactor Interest 不是静态配置

很多入门代码：

~~~text
epoll_ctl ADD once
then never modify
~~~

但成熟 Runtime 中 interest 往往是动态状态：

~~~text
read interest
write interest
error interest
timer interest
control wake interest
~~~

会随着 operation queue 变化。

因此：

~~~text
registered_events_
~~~

必须成为 Runtime state 的一部分。

---

## 10. descriptor_state 的 Mutex 保护什么

每个 descriptor_state 有：

~~~cpp
mutex mutex_;
~~~

它主要保护：

- `op_queue_[]`；
- `registered_events_`；
- `try_speculative_[]`；
- `shutdown_`；
- 与该 fd 相关的 Reactor transition。

所以 Asio 没有要求：

~~~text
所有 fd start/cancel
都抢一把 global reactor mutex
~~~

---

## 11. Per-descriptor Lock 是一种 Contention Partition

假设：

~~~text
Socket A
Socket B
Socket C
~~~

分别被不同线程操作。

理想情况：

~~~text
A transition → mutex A
B transition → mutex B
C transition → mutex C
~~~

而不是：

~~~text
A/B/C
→ one global lock
~~~

因此高连接数下竞争主要被限制到：

~~~text
same descriptor
~~~

这个局部。

---

## 12. 但 Reactor 仍有 Global State Lock

epoll Reactor 仍然有：

~~~text
reactor mutex_
registered_descriptors_mutex_
~~~

用于：

- timer queues；
- registry/object pool；
- whole-service state。

所以成熟并发设计并不是：

~~~text
“不要 global lock”
~~~

而是：

> **把不同一致性范围放到不同 lock domain。**

---

## 13. Reactive Descriptor Service 与 Reactor 是两层对象

`reactive_descriptor_service::implementation_type` 保存：

~~~cpp
int descriptor_;
descriptor_ops::state_type state_;
reactor::per_descriptor_data reactor_data_;
~~~

其中：

~~~text
descriptor_
→ native fd

state_
→ non-blocking / descriptor flags

reactor_data_
→ descriptor_state*
~~~

所以 public descriptor wrapper 并不直接包含所有 Reactor state。

---

## 14. reactor_data_ 是 Runtime Sidecar

可以理解为：

~~~text
public descriptor object
      |
      +-- fd
      +-- descriptor flags
      |
      +-- pointer
            ↓
       descriptor_state
            ↓
       Reactor-owned state
~~~

这是典型 sidecar control block。

---

## 15. 为什么不把所有字段直接塞进 public descriptor

因为 Reactor backend 可以变化：

- epoll；
- kqueue；
- select；
- io_uring；
- IOCP。

Public service 只需要一个抽象：

~~~text
per_descriptor_data
~~~

具体里面是什么，由 backend 决定。

这实现 backend isolation。

---

## 16. 注册 fd 时先分配 descriptor_state

`register_descriptor()`：

~~~text
allocate_descriptor_state
        ↓
initialize reactor pointer / fd
        ↓
shutdown_ = false
        ↓
try_speculative_[*] = true
        ↓
epoll_ctl ADD
~~~

也就是说：

~~~text
先建立 user-space control block
再建立 kernel registration
~~~

---

## 17. 为什么 Speculative 默认打开

一条 async operation 到来时，最便宜的路径不是：

~~~text
enqueue
epoll_wait
wake
perform syscall
~~~

而是：

~~~text
先试一次 non-blocking syscall
~~~

如果现在已经 ready：

~~~text
直接完成
~~~

整个 Reactor round trip 都可以省掉。

---

## 18. Speculative I/O 是 Readiness Bypass Fast Path

比如：

~~~text
async_read_some()
~~~

如果 socket receive buffer 已经有数据：

~~~text
non_blocking_read()
→ success
~~~

就不需要：

~~~text
EPOLLIN
~~~

再次告诉你“它可读”。

你已经通过 syscall 得到了更权威的答案。

---

## 19. Syscall Result 比 Readiness Hint 更权威

Readiness 只是：

~~~text
“现在值得尝试”
~~~

真正 operation 是否完成：

~~~text
由 read/write syscall 决定
~~~

因此 Reactor 的正确心智模型是：

~~~text
epoll readiness
→ permission/hint to retry non-blocking operation
~~~

不是：

~~~text
epoll readiness
→ operation guaranteed complete
~~~

---

## 20. reactor_op::perform() 把 Syscall 封进 Operation

`reactor_op`：

~~~cpp
enum status
{
  not_done,
  done,
  done_and_exhausted
};

status perform()
{
  return perform_func_(this);
}
~~~

每种具体 operation 提供自己的：

~~~text
perform_func_
~~~

比如：

~~~text
descriptor_read_op
descriptor_write_op
socket_recv_op
socket_send_op
connect_op
accept_op
~~~

---

## 21. Reactor 不需要知道具体 Syscall

`descriptor_state::perform_io()` 只调用：

~~~cpp
op->perform();
~~~

它不需要知道：

~~~text
recv
send
readv
writev
accept
connect
~~~

这种 type erasure 让：

~~~text
readiness scheduler
~~~

和：

~~~text
I/O operation implementation
~~~

解耦。

---

## 22. descriptor_read_op 的 perform 非常直接

逻辑：

~~~text
buffer sequence
        ↓
single buffer?
   /          \
 yes          no
  |            |
read1        readv-like
  |            |
  +-----+------+
        |
        v
non_blocking_read
        |
     success?
     /      \
   yes      would block
    |           |
   done      not_done
~~~

所以：

~~~text
perform()
~~~

实际上就是一次 I/O progress attempt。

---

## 23. Write Operation 同理

`descriptor_write_op`：

~~~text
non_blocking_write1
or
non_blocking_write
        ↓
done / not_done
~~~

因此 Asio 的 Reactor 不替代 syscall。

它只是决定：

~~~text
什么时候值得再试一次 syscall
~~~

---

## 24. start_op() 的第一层防御：descriptor_data 是否存在

~~~cpp
if (!descriptor_data)
{
  op->ec_ =
    asio::error::bad_descriptor;

  on_immediate(...);
  return;
}
~~~

这说明 invalid descriptor 也走：

~~~text
completion protocol
~~~

而不是随手 throw / delete operation。

---

## 25. shutdown_ 是 descriptor-level Retirement Bit

拿到 per-fd mutex 后：

~~~cpp
if (descriptor_data->shutdown_)
{
  on_immediate(...);
  return;
}
~~~

说明：

~~~text
这个 descriptor runtime state
不再接受新的 Reactor pending work
~~~

这是 lifecycle gate。

---

## 26. 为什么 Immediate Error 仍然是 Completion

异步 API 的承诺通常是：

~~~text
result delivered asynchronously
through handler/executor semantics
~~~

即使错误一开始就知道：

~~~text
bad descriptor
shutdown
unsupported
~~~

也仍然要走对应 completion machinery。

这维持一致的 execution model。

---

## 27. Speculative I/O 只有在 Queue 为空时才尝试

条件：

~~~cpp
if (op_queue_[op_type].empty())
~~~

原因很重要。

如果前面已经有 operation：

~~~text
A
B
~~~

新来的 C 不能跳过 A/B：

~~~text
直接 speculative syscall
~~~

否则会破坏：

~~~text
per-operation ordering
~~~

所以：

> **Fast path 不能绕过已经建立的 queue order。**

---

## 28. Fast Path 必须尊重 FIFO

这是很通用的原则。

优化可以绕过：

~~~text
slow mechanism
~~~

但不能绕过：

~~~text
semantic predecessor
~~~

否则“更快”会改变 API 行为。

---

## 29. Read Speculation 还有一个额外条件

源码：

~~~cpp
op_type != read_op
||
op_queue_[except_op].empty()
~~~

也就是：

~~~text
存在 exception/OOB waiter 时
普通 read 不允许 speculative
~~~

这是为了保护更细的 I/O ordering。

---

## 30. 为什么 OOB / Exception 必须优先

`perform_io()` 明确：

~~~text
Exception operations
must be processed first
~~~

循环：

~~~cpp
for (
  int j = max_ops - 1;
  j >= 0;
  --j)
~~~

即：

~~~text
except
→ write
→ read
~~~

异常/OOB 数据可能需要在普通数据之前被处理。

因此 speculative read 不能偷偷抢先消耗状态。

---

## 31. 这说明 Speculation 不是“想试就试”

正确 speculative optimization 必须满足：

~~~text
不会跳过已有 work
不会破坏 protocol ordering
不会破坏 priority relation
不会破坏 cancellation/lifetime invariant
~~~

否则 fast path 会改变语义。

---

## 32. perform() 返回 not_done 意味着什么

~~~text
这次 non-blocking progress attempt
仍然不能完成 operation
~~~

通常意味着：

~~~text
EAGAIN / would block
~~~

于是 operation 留在：

~~~text
descriptor op_queue
~~~

继续等待未来 readiness。

---

## 33. done 意味着 Operation 完成

~~~text
syscall/result 已经足以完成
~~~

于是：

~~~text
pop operation
→ completion list
~~~

稍后进入 Scheduler。

---

## 34. done_and_exhausted 比 done 多表达一层信息

它表示：

~~~text
当前 operation 完成
而且这次 I/O 结果表明当前 readiness
很可能已经被消耗干净
~~~

因此：

~~~text
继续盲目 speculative 同类 syscall
价值很低
~~~

Asio 会：

~~~cpp
try_speculative_[j] = false;
~~~

直到下一次对应 epoll readiness 到来。

---

## 35. try_speculative_ 是 Local Readiness Confidence

可以把：

~~~text
try_speculative_[read]
~~~

理解成：

> 当前是否值得在没有新 kernel edge 的情况下，再主动试一次该类 non-blocking I/O。

它不是：

~~~text
fd 是否 ready
~~~

而是一种 userspace optimization state。

---

## 36. 新 epoll Edge 会重新打开 Speculation

`perform_io(events)`：

~~~cpp
if (events & ...)
{
  try_speculative_[j] = true;
  ...
}
~~~

也就是说：

~~~text
kernel says readiness changed
        ↓
重新允许 speculative progress
~~~

这形成：

~~~text
edge
→ confidence reset
→ syscall attempts
→ exhausted
→ confidence disabled
→ wait next edge
~~~

---

## 37. 这是 EPOLLET 下非常自然的状态机

Edge-trigger 模式核心问题：

~~~text
Kernel 不会不断提醒“它还 ready”
~~~

所以 userspace 必须追踪：

~~~text
我是否已经把这次 readiness 尽量消费完
~~~

`try_speculative_` 就是这一状态的一部分。

---

## 38. EPOLLET 不等于“收到一次就只做一次 syscall”

恰恰相反。

收到 edge 后通常应该：

~~~text
持续尝试 progress
直到 would-block / exhausted
~~~

否则：

~~~text
数据还留在 kernel buffer
但没有新 edge
~~~

可能造成 stall。

---

## 39. perform_io() 会 Drain 同一类 Operation Queue

核心：

~~~cpp
while (
  reactor_op* op =
    op_queue_[j].front())
{
  status = op->perform();

  if (status)
  {
    pop;
    completed.push(op);

    if (done_and_exhausted)
    {
      try_speculative_[j] = false;
      break;
    }
  }
  else
    break;
}
~~~

所以一次 readiness 可以推动：

~~~text
多个同类 queued operations
~~~

而不只是第一条。

---

## 40. 为什么一次 Readiness 能完成多个 Operation

假设 receive buffer 有很多数据：

~~~text
read op A wants 100 B
read op B wants 100 B
read op C wants 100 B
~~~

Kernel 当前有：

~~~text
300 B
~~~

一次 `EPOLLIN` 后：

~~~text
A perform -> done
B perform -> done
C perform -> done
~~~

没必要三次返回 epoll_wait。

这就是 readiness batch consumption。

---

## 41. Readiness 是资源状态，Operation 是业务请求

一个 readiness edge：

~~~text
fd readable
~~~

可以服务多个：

~~~text
read requests
~~~

所以：

~~~text
one kernel event
!=
one user completion
~~~

这是 Reactor 设计必须明确的粒度差异。

---

## 42. 同一个 descriptor_state 一次 epoll batch 只入队一次

`epoll_reactor::run()`：

~~~cpp
if (!ops.is_enqueued(
      descriptor_data))
{
  descriptor_data
    ->set_ready_events(events);

  ops.push(descriptor_data);
}
else
{
  descriptor_data
    ->add_ready_events(events);
}
~~~

这是一种事件合并。

---

## 43. 多个 epoll Event 会 OR 到同一个 task_result_

`set_ready_events()`：

~~~cpp
task_result_ = events;
~~~

`add_ready_events()`：

~~~cpp
task_result_ |= events;
~~~

所以同一个 fd 如果当前 epoll 批次收到：

~~~text
EPOLLIN
EPOLLOUT
EPOLLERR
~~~

可以聚合成：

~~~text
one descriptor_state scheduler operation
+
combined event bitmask
~~~

---

## 44. task_result_ 被复用成 Event Mask Transport

普通 operation 的 `task_result_` 可以携带任务结果。

descriptor_state 用它保存：

~~~text
epoll events bitmask
~~~

之后 Scheduler：

~~~text
operation::complete(
  owner,
  ec,
  task_result)
~~~

进入：

~~~cpp
descriptor_state::do_complete(
    ...,
    bytes_transferred)
~~~

这里那个参数实际被解释成：

~~~text
uint32_t events
~~~

---

## 45. 这是 Type-erased Control Message 的典型技巧

Scheduler 只认识：

~~~text
operation*
~~~

它并不知道：

~~~text
task_result_ 对这个 operation
到底是 bytes、events 还是别的语义
~~~

由具体 operation 的 completion function 自己解释。

这使通用 Scheduler 无需知道 Reactor internals。

---

## 46. epoll_reactor::run() 为什么不直接 work_started()

源码注释：

~~~text
descriptor operation
doesn't count as work in and of itself
~~~

因为真正的 outstanding work 已经在：

~~~text
reactor_op 入 pending queue
~~~

时通过：

~~~cpp
scheduler_.work_started();
~~~

登记过了。

---

## 47. descriptor_state 只是 Progress Vehicle

它不是新的用户请求。

它只是：

~~~text
kernel readiness
→ drive existing user work forward
~~~

如果每次 fd readiness 都：

~~~text
work_started()
~~~

会人为制造额外 work debt。

---

## 48. 这会导致 Liveness Accounting 重复记账

假设一个 async read：

~~~text
start_op
→ work_started +1
~~~

如果每个 EPOLLIN 又：

~~~text
work_started +1
~~~

但最终只：

~~~text
handler completion -1
~~~

`outstanding_work_` 永远无法归零。

所以：

> **Readiness event 不是新的 logical work。**

---

## 49. descriptor_state 被 Scheduler 执行时会发生一个 Accounting 难题

Scheduler 的普通 operation 执行路径会创建：

~~~text
work_cleanup
~~~

在 operation completion 返回后：

~~~text
自动 work_finished()
~~~

但 descriptor_state 自己没有 work debt。

怎么办？

Asio 用了一个非常漂亮的 debt transfer。

---

## 50. perform_io_cleanup_on_block_exit 是 Work Accounting Bridge

结构：

~~~text
ops_
→ 本次 readiness 完成的 user operations

first_op_
→ 第一条要同步交给 Scheduler completion path 的 operation
~~~

析构时根据：

~~~text
有没有 first_op_
~~~

决定 accounting。

---

## 51. 如果本次 Readiness 完成了至少一条 User Operation

流程：

~~~text
descriptor_state scheduled
        ↓
perform_io
        ↓
completed ops:
A B C
        ↓
first_op = A
remaining = B C
        ↓
return A
~~~

`descriptor_state::do_complete()` 接着：

~~~cpp
A->complete(...)
~~~

---

## 52. 第一条 User Operation 借用了 descriptor_state 的 Scheduler Work Cleanup

外层 Scheduler 认为它刚执行的是：

~~~text
descriptor_state operation
~~~

因此退出时会：

~~~text
work_finished()
~~~

但 Asio 实际让这一次 decrement 对应：

~~~text
A 这条用户 operation
~~~

原先登记的 debt。

所以：

~~~text
descriptor_state 自己 0 debt
A 原本 +1 debt
outer cleanup -1
~~~

刚好配平。

---

## 53. 这是 Work Debt Transfer

可以写成：

\[
D_{\text{descriptor}} = 0
\]

\[
D_A = 1
\]

执行时并没有额外给 descriptor_state 新增 debt。

但 Scheduler 的固定 completion 机制一定会做一次：

\[
-1
\]

于是 Asio 让：

~~~text
A 成为 first_op
~~~

把这个 decrement 解释为：

~~~text
结算 A 的那一笔 debt
~~~

---

## 54. 剩余 B/C 为什么走 post_deferred_completions

因为它们原先也各自已经：

~~~text
work_started()
~~~

现在只需要：

~~~text
从 descriptor pending queue
移动到 Scheduler ready queue
~~~

不能再 work_started。

因此：

~~~cpp
post_deferred_completions(
  ops_)
~~~

只改变 location，不增加 debt。

---

## 55. 如果本次 Readiness 一个 User Operation 都没完成呢

例如：

~~~text
spurious-ish readiness
race
syscall returns would-block
~~~

则：

~~~text
first_op_ == nullptr
~~~

但外层 Scheduler 仍然会：

~~~text
work_finished()
~~~

这就会误减一笔不存在的 debt。

---

## 56. compensating_work_started() 用来抵消这个固定 Decrement

析构：

~~~cpp
if (!first_op_)
{
  scheduler_
    ->compensating_work_started();
}
~~~

它增加当前 thread 的：

~~~text
private_outstanding_work
~~~

使后面的 `work_cleanup` 不再执行：

~~~text
global work_finished()
~~~

---

## 57. 这里不是“凭空创建一笔 Work”

`compensating_work_started()` 的语义是：

> **补偿 Scheduler 固定 operation-completion 模板即将做出的错误 decrement。**

它只是 accounting correction。

不是新业务请求。

---

## 58. 这个 Trick 为什么需要理解 Scheduler 的 work_cleanup

`work_cleanup`：

~~~text
private_outstanding_work > 1
→ global += private - 1

private_outstanding_work < 1
→ work_finished()

private == 1
→ neither increment nor decrement
~~~

因此 `compensating_work_started()` 把：

~~~text
private 0
→ 1
~~~

正好让外层：

~~~text
不增
不减
~~~

---

## 59. 如果完成了一个 User Operation，不需要 Compensation

因为那次：

~~~text
work_finished()
~~~

正应该结算第一条用户 operation 的 debt。

所以：

~~~text
first_op exists
→ do not compensate
~~~

---

## 60. 这是一种“Control Operation 借用 User Debt”的设计

descriptor_state 是：

~~~text
control operation
~~~

用户 reactor_op 是：

~~~text
logical work
~~~

Asio 通过：

~~~text
first completed user op
~~~

把固定 Scheduler accounting 接口桥接起来。

这减少了 Scheduler 针对 Reactor 的特殊分支。

---

## 61. Generic Scheduler 因此不用知道 descriptor_state 不计 Work

Scheduler 仍然只执行：

~~~text
pop operation
complete
work_cleanup
~~~

特殊 accounting 被封装在：

~~~text
descriptor_state perform cleanup
~~~

内部。

这是很漂亮的 abstraction containment。

---

## 62. descriptor_state::do_complete 只在 owner 非空时 perform I/O

~~~cpp
if (owner)
{
  ...
}
~~~

如果 operation 被 teardown path：

~~~text
destroy()
~~~

`owner == nullptr`。

这时不能：

~~~text
继续碰 fd
继续 perform I/O
~~~

所以 control operation teardown 与 normal execution 也被统一在 type-erased operation contract 里。

---

## 63. Readiness Processing 运行在 per-descriptor Mutex 下

`perform_io()`：

~~~cpp
mutex_.lock();

scoped_lock descriptor_lock(
  mutex_,
  adopt_lock);
~~~

因此同一个 fd 的：

- start；
- cancel；
- readiness progress；
- deregister；

不会并发修改同一个 operation queue。

---

## 64. 但 User Handler 不在 descriptor Mutex 下执行

`perform_io()` 只把完成 operation 移出队列。

真正用户 handler：

~~~text
later through operation::complete
~~~

此时 descriptor lock 已经离开作用域。

这是非常重要的 reentrancy boundary。

---

## 65. 为什么不能拿着 fd Mutex 调 Handler

用户 handler 可能：

- 再发 async read；
- cancel；
- close；
- release descriptor；
- destroy owning object；
- post other work。

如果还持有同一 per-fd mutex：

~~~text
handler
→ reenter descriptor
→ deadlock
~~~

所以：

> **锁内决定状态，锁外执行用户代码。**

---

## 66. Reactor 与 Handler 之间至少隔着两个 Ownership Transfer

第一层：

~~~text
descriptor op_queue
→ completion list
~~~

第二层：

~~~text
operation internal state
→ handler_work / local binder
~~~

然后才：

~~~text
user upcall
~~~

这正是成熟 callback Runtime 常见的结构。

---

## 67. EPOLLERR / EPOLLHUP 会驱动所有相关 Queue

`perform_io()` 条件：

~~~cpp
events
&
(flag[j]
 | EPOLLERR
 | EPOLLHUP)
~~~

所以错误或 hangup：

~~~text
read queue
write queue
except queue
~~~

都可能被要求尝试 progress。

这是合理的。

因为 error/hangup 会影响整个 descriptor。

---

## 68. Error Readiness 不等于直接给所有 Operation 同一个 Error

Asio 仍然调用：

~~~text
op->perform()
~~~

让具体 syscall / operation：

~~~text
读取真实错误状态
决定 completion result
~~~

所以 Reactor 不粗暴地：

~~~text
EPOLLERR
→ all handlers get same ec
~~~

---

## 69. Kernel Event 只是触发 Concrete Operation Re-evaluation

这和很多 Runtime 的原则一致：

~~~text
Event
→ recheck authoritative state
~~~

不是：

~~~text
Event itself is final result
~~~

Kernel readiness 是提示。

Syscall outcome 才是 operation 结果。

---

## 70. register_descriptor 对 EPERM 的处理很值得学习

`epoll_ctl(ADD)` 可能：

~~~text
EPERM
~~~

典型如：

~~~text
regular file
~~~

这类 fd 不能注册到 epoll。

Asio 没有立即把 descriptor 判死。

---

## 71. EPERM 时 registered_events_ 被设为 0

~~~cpp
descriptor_data
  ->registered_events_ = 0;

return 0;
~~~

也就是说：

~~~text
public registration succeeds
但 Reactor backend 不可用
~~~

后续 operation 仍有机会先 speculative syscall。

---

## 72. 为什么 Regular File 仍值得接受

普通文件 read/write 一般不会像 socket 那样：

~~~text
等待 network readiness
~~~

如果 operation 在 speculative syscall 中就能完成：

~~~text
根本不需要 epoll
~~~

所以：

> **Backend capability failure 不必等于整个 async abstraction 立即失败。**

---

## 73. 只有真的需要 Reactor 时才 operation_not_supported

如果：

~~~text
registered_events_ == 0
~~~

而 speculative attempt 又不能完成：

~~~text
那才说明这条 operation
真的需要一个 backend wait
~~~

此时：

~~~cpp
op->ec_ =
  operation_not_supported;
~~~

这是一种 graceful capability fallback。

---

## 74. 设计原则：先用最小能力完成，再要求更强 Backend

不要因为：

~~~text
对象不支持高级机制 X
~~~

就立刻拒绝所有操作。

先问：

~~~text
当前 operation 是否真的依赖 X
~~~

Asio 的 regular-file fallback 就是这个思路。

---

## 75. start_op() 中 Queue Publication 与 Work Registration 在同一个 Lock Domain

最终 slow path：

~~~cpp
op_queue_[op_type].push(op);
scheduler_.work_started();
~~~

这两步都发生在：

~~~text
descriptor mutex still held
~~~

所以 cancel/readiness 无法看到：

~~~text
已经入 pending queue
但还没登记 work debt
~~~

的中间状态。

---

## 76. 这是 Register-before-Completion 的关键不变量

一旦 operation 对其他并发路径可见：

~~~text
它的 liveness obligation
也必须已经或同步建立
~~~

否则：

~~~text
cancel thread
可能先把它完成
~~~

而 accounting 还没建立。

会导致 underflow / early stop。

---

## 77. 为什么 work_started 放在 push 后仍然安全

因为：

~~~text
push
work_started
~~~

都在同一把 descriptor lock 里。

其他会 pop/complete 这条 operation 的路径：

~~~text
cancel
perform_io
deregister
~~~

也要先拿这把锁。

所以外界不能观察到两步之间的状态。

---

## 78. 并发协议不能只看源码行顺序

表面：

~~~text
push before work_started
~~~

似乎存在 race。

真正要分析的是：

~~~text
谁能同时观察这个状态？
观察者需要哪把锁？
~~~

这也是源码并发分析中最重要的能力之一。

---

## 79. Cancellation 与 Readiness 竞争的是同一个 Queue Ownership

`cancel_ops()`：

~~~text
lock descriptor
        ↓
pop pending operations
        ↓
ec = operation_aborted
        ↓
unlock
        ↓
post deferred completions
~~~

`perform_io()`：

~~~text
lock descriptor
        ↓
perform front operation
        ↓
if done:
  pop
        ↓
unlock later
~~~

所以：

~~~text
谁先从 queue 取得 operation
~~~

定义最终路径。

---

## 80. Cancel 不能回收已经离开 Queue 的 Operation

一旦 readiness 路径：

~~~text
pop operation
→ completion list
~~~

cancel 再来时：

~~~text
descriptor queue 已经看不到它
~~~

所以 cancellation 不是：

~~~text
erase future callback
~~~

而是：

~~~text
对仍处于 pending owner 下的 operation
进行状态转换
~~~

---

## 81. Per-operation Cancellation 用 cancellation_key_ 做筛选

`reactor_op` 保存：

~~~cpp
void* cancellation_key_;
~~~

`cancel_ops_by_key()`：

~~~text
scan one op queue
        ↓
matching key
→ operation_aborted completion

nonmatching
→ move to temporary queue
→ restore
~~~

这里 targeted cancellation 仍然围绕：

~~~text
descriptor-owned pending set
~~~

进行。

---

## 82. Targeted Cancel 不会抢占正在执行的 Handler

如果 operation 已经：

~~~text
scheduler ready
or
handler executing
~~~

它已经不在：

~~~text
descriptor pending queue
~~~

因此 cancellation key 无法 retroactively 抢占用户代码。

这必须由业务层合作完成。

---

## 83. deregister_descriptor 是更强的 Retirement

它做：

~~~text
stop kernel registration
        ↓
abort all pending operations
        ↓
descriptor_ = -1
shutdown_ = true
        ↓
post completions
~~~

这比：

~~~text
cancel_ops
~~~

多关闭了：

~~~text
未来 descriptor participation
~~~

---

## 84. closing 参数决定是否显式 EPOLL_CTL_DEL

如果 descriptor马上要被：

~~~text
close(fd)
~~~

Linux 会自动把它从 epoll set 移除。

因此：

~~~text
closing == true
~~~

时可以跳过显式 DEL。

否则：

~~~text
release fd without close
~~~

之类路径必须：

~~~text
EPOLL_CTL_DEL
~~~

---

## 85. Kernel Registration 与 OS fd Ownership 是不同生命周期

`release()`：

~~~text
deregister from reactor
        ↓
cleanup descriptor_state
        ↓
return native fd
~~~

但不：

~~~text
close fd
~~~

这说明：

~~~text
Reactor ownership
!=
OS handle ownership
~~~

API 必须明确区分。

---

## 86. close() 又是另一条路径

`close()`：

~~~text
deregister_descriptor
        ↓
descriptor_ops::close
        ↓
cleanup_descriptor_data
        ↓
reset implementation
~~~

所以：

~~~text
kernel reactor membership
OS descriptor lifetime
userspace descriptor_state lifetime
~~~

是三个阶段。

---

## 87. descriptor_state Cleanup 必须晚于 Deregistration

`deregister_descriptor()` 注释：

~~~text
Leave descriptor_data set
so subsequent cleanup_descriptor_data
can free it.
~~~

先：

~~~text
从可执行系统中 retire
~~~

再：

~~~text
物理 free control block
~~~

这是非常典型的：

~~~text
retire
→ reclaim
~~~

分离。

---

## 88. 为什么不能一 deregister 就直接 delete descriptor_state

epoll / Scheduler / operation lifecycle 之间可能还有：

~~~text
已经 publication 的控制状态
~~~

所以物理回收必须遵守 backend 定义的安全边界。

这和前面 LCM/libzmq 的 quiescence 思路是同一类问题。

---

## 89. epoll_reactor::run 会把 descriptor_state 指针发布给 Scheduler

因此 descriptor_state 生命周期至少要覆盖：

~~~text
epoll reports ptr
        ↓
Reactor local ops queue
        ↓
Scheduler queue
        ↓
descriptor_state completion
~~~

不能只按：

~~~text
fd has been closed
~~~

判断 control block 可释放。

---

## 90. 为什么 Reactor run 顶部强调 previous descriptor operations 已 dequeue

源码注释说明：

~~~text
scheduler queues reactor task
behind all descriptor operations
generated by this function
~~~

因此下一次进入 Reactor task 时：

~~~text
上一次返回的 descriptor_state operations
已经从 Scheduler queue 被 dequeue
~~~

于是这些 control block 可以安全再次：

~~~text
被放进当前 Reactor 的 ops queue
~~~

---

## 91. 这是 task_operation_ Queue Ordering 提供的 Quiescence Boundary

上一轮：

~~~text
descriptor A state
descriptor B state
task_operation_
~~~

因为 task sentinel 被放在这些 descriptor operation 后面：

~~~text
Scheduler 必须先 dequeue A/B
才会再次运行 Reactor task
~~~

因此 Reactor 可以依赖这个 ordering：

~~~text
不需要给 descriptor_state
额外分配一次性 event object
~~~

---

## 92. descriptor_state 本身被反复复用

它既是：

~~~text
long-lived fd control block
~~~

又临时扮演：

~~~text
Scheduler-ready control operation
~~~

而不是每个 epoll event：

~~~text
new EventObject()
~~~

这减少 allocation。

---

## 93. 但复用对象需要严格保证“不重复入队”

所以：

~~~cpp
ops.is_enqueued(
  descriptor_data)
~~~

非常关键。

否则同一个 intrusive node：

~~~text
同时出现在 queue 两次
~~~

会直接破坏链表结构。

---

## 94. Intrusive Node 的不变量比普通 Queue 更严格

普通：

~~~text
queue stores copies/pointers in separate nodes
~~~

重复 push 同一指针可能只是两个元素。

Intrusive queue：

~~~text
object itself owns next_
~~~

同一对象同时加入两次：

~~~text
next_ ownership冲突
~~~

所以必须检查：

~~~text
already enqueued?
~~~

---

## 95. Readiness Aggregation 正好解决重复 Intrusive Enqueue

如果已经在 local `ops`：

~~~text
不再 push
只 OR task_result_
~~~

所以既：

- 不丢 event bits；
- 不重复 intrusive enqueue；
- 不产生多余 Scheduler operation。

---

## 96. 这是一种 Coalescing Control Object

类似设计常见于：

~~~text
device dirty flag
network RX queue notification
GUI event coalescing
GPU completion poll token
actor mailbox scheduled bit
~~~

原则：

~~~text
state can accumulate
execution token only one
~~~

---

## 97. “一个 Token + 可累积状态”比“每个事件一个 Task”更省

高频 edge 下：

~~~text
N kernel events
~~~

不一定需要：

~~~text
N Scheduler tasks
~~~

如果语义允许合并：

~~~text
OR readiness bits
→ one execution token
~~~

可以显著降低调度开销。

---

## 98. 这和 Strand 的 locked_ 有相似结构

Strand：

~~~text
many submissions
→ one invoker ownership chain
~~~

descriptor_state：

~~~text
many readiness bits
→ one descriptor execution token
~~~

本质都是：

> **用一个 scheduled token 表示“这个对象需要处理”，把具体状态累积在对象内部。**

---

## 99. perform_io 中 Why Exception First

顺序：

~~~text
except
write
read
~~~

尤其 exception before read 是明确设计要求。

这不是数组编号偶然。

是协议 ordering。

---

## 100. Array Index 其实编码了 Priority

`op_queue_[3]` 看起来像：

~~~text
普通数组
~~~

但：

~~~text
index values
+
reverse iteration
~~~

共同编码了调度优先级：

~~~text
2 > 1 > 0
~~~

因此数据结构布局本身承载 policy。

---

## 101. 源码阅读时要警惕“数字只是下标”的错觉

很多 Runtime：

~~~text
enum
array
loop direction
~~~

组合起来就在表达：

- priority；
- state ordering；
- cleanup order；
- lock order。

不能只把它当容器技巧。

---

## 102. Speculative Read 为什么要看 except Queue，而 Write 不需要

因为：

~~~text
read-like normal data
~~~

和：

~~~text
OOB / exception data
~~~

可能存在消费顺序约束。

Write 路径不涉及同样的输入消费竞争。

所以 speculative policy 可以按 operation class 不同。

---

## 103. Fast Path Policy 本身是 Per-class State

~~~cpp
bool try_speculative_[max_ops];
~~~

不是一个：

~~~text
bool try_speculative
~~~

因为 read/write/except：

~~~text
当前是否值得主动 syscall
~~~

可以完全不同。

---

## 104. Per-class State 比 Global Ready Flag 更精确

一个 fd 可能：

~~~text
read side exhausted
write side still ready
~~~

所以：

~~~text
one global ready/exhausted flag
~~~

无法表达真实状态。

这就是为什么 Reactor state 往往要按 readiness class 拆分。

---

## 105. Connect 复用 Write Queue 也是语义归类

Non-blocking connect：

~~~text
EINPROGRESS
        ↓
wait writable/error
        ↓
check completion
~~~

所以 Asio 让：

~~~text
connect_op = write_op
~~~

不是代码偷懒，而是：

~~~text
它们共享 kernel readiness class
~~~

---

## 106. 设计 Data Structure 时应按驱动事件归类

不是问：

~~~text
API 名字是不是一样
~~~

而是问：

~~~text
它们由同一类 runtime event 驱动吗？
~~~

这会自然决定：

~~~text
是否共用 queue / state / scheduling path
~~~

---

## 107. Speculative Success 会走 Immediate Completion Path

如果：

~~~text
op->perform()
→ done
~~~

start_op：

~~~text
unlock descriptor
        ↓
on_immediate(...)
~~~

不会把 operation 先塞 pending queue。

因为：

~~~text
它已经不 pending
~~~

---

## 108. Immediate Completion 仍不等于直接调用 User Handler

`on_immediate` 通常会：

~~~text
通过 immediate handler work / executor semantics
安排 completion
~~~

而不是在 descriptor mutex 下直接调用业务代码。

因此：

~~~text
I/O immediately done
~~~

和：

~~~text
handler inline called arbitrarily
~~~

不是同一件事。

---

## 109. Asynchronous API 仍保留 Executor Contract

即便 syscall 当场成功：

~~~text
completion execution context
~~~

仍然由：

- associated immediate executor；
- handler work；
- Scheduler；

共同决定。

这避免 fast path 偷偷改变线程语义。

---

## 110. registered_events_ == 0 的 Descriptor 只靠 Speculation 生存

EPERM fallback 下：

~~~text
no kernel epoll path
~~~

所以 operation 只有两种可能：

~~~text
speculative completes
or
needs blocking readiness → unsupported
~~~

这是一个非常干净的 capability boundary。

---

## 111. “异步”不一定必须经过 Kernel Multiplexer

如果 operation 能：

~~~text
立即 non-blocking 完成
~~~

异步抽象仍可以通过：

~~~text
completion scheduling
~~~

保持异步 API 语义。

Kernel multiplexer 只是：

~~~text
等待真正会阻塞的 operation
~~~

所需工具。

---

## 112. Reactor 的真正价值是避免阻塞线程

不是：

~~~text
所有 I/O 都必须走 epoll
~~~

而是：

~~~text
当 syscall 当前不能 progress 时
用 readiness notification
代替 blocking thread
~~~

这才是 Reactor 的第一性原理。

---

## 113. One Descriptor, Many Pending Operations

一个 fd queue 可以：

~~~text
read A
read B
read C
~~~

这表示用户层提交速度可以快于：

~~~text
kernel I/O progress
~~~

Reactor 因此天然承担：

~~~text
backlog ownership
~~~

虽然真正 backpressure policy 还在更上层。

---

## 114. Reactor Queue 本身不是无限安全的 Backpressure 策略

能 queue 不代表应该无限 queue。

业务系统仍要决定：

- 最大 pending write；
- 最大 message backlog；
- 超时；
- drop；
- flow control。

Reactor 只提供机制。

---

## 115. write Queue 与应用 Send Queue 也不是同一层

常见网络库：

~~~text
application messages
→ serialize/output buffer
→ one/few async write operations
→ Reactor write queue
~~~

不要把：

~~~text
Reactor op_queue
~~~

误当成业务级 message queue。

它的颗粒度是 I/O operation。

---

## 116. descriptor_state shutdown_ 与 Reactor shutdown_ 是不同层

`descriptor_state::shutdown_`：

~~~text
这个 fd control block
已经 retire
~~~

`epoll_reactor::shutdown_`：

~~~text
整个 Reactor service
正在 teardown
~~~

局部 lifecycle 与全局 lifecycle 必须分开。

---

## 117. Whole Reactor Shutdown 不走正常 operation_aborted Handler Delivery

`epoll_reactor::shutdown()`：

~~~text
mark service shutdown
        ↓
walk registered descriptors
        ↓
move pending ops
        ↓
mark states shutdown
        ↓
free control blocks
        ↓
timer ops
        ↓
scheduler.abandon_operations
~~~

这里更接近：

~~~text
execution context teardown
~~~

而不是业务 graceful cancel。

---

## 118. abandon_operations 的目标是销毁 Handler Objects

整个 execution context 都在退出时：

~~~text
不再保证普通 completion delivery
~~~

而要确保：

~~~text
资源和 handler storage 正确释放
~~~

这与正常：

~~~text
cancel → operation_aborted callback
~~~

是不同语义。

---

## 119. Close / Cancel / Shutdown 三者必须分开

~~~text
cancel
→ pending ops become aborted completions
→ fd remains usable

close/deregister
→ retire fd from Reactor
→ pending ops aborted
→ OS fd closed separately

reactor shutdown
→ whole runtime teardown
→ abandon/destroy operations
~~~

这是三种生命周期级别。

---

## 120. registered_descriptors_ 为什么用 Object Pool

Reactor 长时间频繁：

~~~text
open fd
close fd
open fd
close fd
~~~

如果每个 descriptor_state 都走通用 heap：

~~~text
allocation contention
fragmentation
cache locality
~~~

都会受影响。

Object pool 更适合：

~~~text
同构、频繁、Runtime control objects
~~~

---

## 121. descriptor_state 还带 intrusive registry links

~~~cpp
descriptor_state* next_;
descriptor_state* prev_;
~~~

因此它同时是：

~~~text
object pool element
registry node
scheduler operation
fd control block
~~~

这是 Runtime 中常见的“多角色对象”。

---

## 122. 多角色对象能减少额外 Allocation，但提高 Invariant 密度

优点：

- 少分配；
- 少 pointer chasing；
- 更好 locality；
- 状态集中。

代价：

~~~text
同一个对象参与多个 ownership protocol
~~~

所以必须清楚：

~~~text
什么时候属于 registry
什么时候属于 Scheduler queue
什么时候可 free
~~~

---

## 123. 这种对象最怕 Lifetime 边界不清

如果只看到：

~~~text
fd close
~~~

就释放 descriptor_state，

但它仍是：

~~~text
Scheduler intrusive operation
~~~

就会 UAF。

因此源码作者必须画出：

~~~text
registry membership
queue membership
kernel registration
OS fd ownership
~~~

四种不同关系。

---

## 124. move_descriptor 为什么可以只转移 Pointer

~~~cpp
target_descriptor_data =
  source_descriptor_data;

source_descriptor_data = 0;
~~~

因为 Reactor state 并不嵌在 public wrapper 内。

移动 wrapper：

~~~text
只是转移 sidecar pointer ownership
~~~

descriptor_state 自己保持地址稳定。

这对 epoll：

~~~text
ev.data.ptr = descriptor_state
~~~

非常重要。

---

## 125. Stable Address 是 epoll data.ptr 的隐含要求

Kernel epoll registration 保存：

~~~text
user data pointer
~~~

如果 public C++ object move 导致 control block地址变化：

~~~text
kernel 仍持有旧 pointer
~~~

会出问题。

把 Reactor state 放进独立 pool object：

~~~text
wrapper 可 move
control block address stable
~~~

是非常合理的设计。

---

## 126. 这是 Pimpl/Sidecar 的另一个价值：Address Stability

不仅是 ABI 隐藏。

还可以提供：

~~~text
stable identity
across public object move
~~~

这在：

- kernel callback userdata；
- C callback userdata；
- intrusive registries；
- async completion；

都很重要。

---

## 127. epoll data.ptr 直接指向 descriptor_state

因此 event 到来时：

~~~text
kernel event
→ descriptor_state*
~~~

不需要再：

~~~text
fd → unordered_map lookup
~~~

省掉一次 registry search。

---

## 128. 用 Pointer Token 换 Lookup 成本

两种设计：

~~~text
epoll event returns fd
→ hash map lookup state
~~~

vs：

~~~text
epoll event returns state pointer
~~~

后者更快。

但要求：

~~~text
pointer lifetime absolutely correct
~~~

性能优化会把压力转移到 lifetime protocol。

---

## 129. 这是高性能 Runtime 的典型 Tradeoff

少一次 map lookup：

~~~text
更快
~~~

但必须保证：

~~~text
state address stable
state not freed while kernel may report it
~~~

因此性能与生命周期证明往往绑在一起。

---

## 130. Fork 后为什么要重建 epoll 并重新注册所有 descriptor_state

Child process 里：

~~~text
epoll fd / timerfd / interrupter
~~~

需要重新建立。

然后遍历：

~~~text
registered_descriptors_
~~~

按 `registered_events_`：

~~~text
重新 EPOLL_CTL_ADD
~~~

这再次说明用户态 shadow registry 的价值。

---

## 131. 没有 User-space Registry 就很难 Reconstruct Kernel State

如果 Runtime 只把 interest 丢进 kernel：

~~~text
自身不保留镜像
~~~

fork/reinit/diagnostics 时很难恢复。

所以：

~~~text
registered_events_
+
registered_descriptors_
~~~

也是可重建性的基础。

---

## 132. Kernel State 通常不应是唯一 Truth Source

用户态 Runtime 需要保留：

~~~text
desired state / logical state
~~~

Kernel 保存：

~~~text
enforced state
~~~

二者通过系统调用同步。

这和控制系统里的：

~~~text
desired config
vs
device actual config
~~~

非常类似。

---

## 133. Reactor 的真实状态机可以画成这样

~~~text
UNREGISTERED
    |
    | register_descriptor
    v
REGISTERED
    |
    | async operation
    v
SPECULATIVE TRY
   / \
done  would-block
 |        |
 v        v
READY   PENDING QUEUE
COMP       |
           | epoll edge
           v
      descriptor_state
      scheduled once
           |
           v
       perform_io
       /       \
   complete    still blocked
      |            |
      v            v
 Scheduler      remain pending
 completion
~~~

关闭：

~~~text
REGISTERED/PENDING
        |
        | deregister
        v
RETIRED
        |
        | abort pending
        v
COMPLETIONS OUT
        |
        | cleanup
        v
RECLAIMED
~~~

---

## 134. 一条 async_read_some 的完整路径

~~~text
application
    |
    v
reactive_descriptor_service
    |
    | allocate descriptor_read_op
    v
do_start_op
    |
    | ensure internal non-blocking
    v
epoll_reactor::start_op
    |
    +---------------------------+
    |                           |
speculative read works       EAGAIN
    |                           |
    v                           v
immediate completion       queue read op
                            work_started
                                 |
                                 v
                            epoll_wait
                                 |
                              EPOLLIN
                                 |
                                 v
                         descriptor_state
                          into Scheduler
                                 |
                                 v
                            perform_io
                                 |
                                 v
                       read op perform()
                                 |
                                 v
                             completion
                                 |
                                 v
                           handler_work
                                 |
                                 v
                           user handler
~~~

---

## 135. 一条 async_write_some 的完整路径

~~~text
allocate write operation
        |
        v
speculative write
   /           \
done           would block
 |                 |
 v                 v
completion     enable EPOLLOUT
                  |
                  v
              queue write op
                  |
                  v
              epoll_wait
                  |
               EPOLLOUT
                  |
                  v
             perform write
~~~

所以：

~~~text
EPOLLOUT interest
~~~

是 slow path 的一部分。

---

## 136. 一个 Event 可以同时驱动多个 Completion

假设：

~~~text
EPOLLIN | EPOLLOUT | EPOLLERR
~~~

descriptor_state：

~~~text
aggregates bits
        ↓
perform except queue
        ↓
perform write queue
        ↓
perform read queue
~~~

每条 queue 又可能完成多个 operation。

所以：

~~~text
1 kernel event batch
→ N user completions
~~~

这是 Reactor batching 的重要性能来源。

---

## 137. Scheduler 与 Reactor 的 Batch 边界是分开的

Reactor batch：

~~~text
epoll_wait returns up to 128 events
~~~

每个 descriptor 再：

~~~text
coalesce readiness
~~~

descriptor perform 又：

~~~text
drain multiple operations
~~~

最后 Scheduler：

~~~text
dispatch completions
~~~

因此系统是多层 batching。

---

## 138. 多层 Batching 的目标不是单纯吞吐

它还减少：

- mutex acquire；
- queue operations；
- epoll syscalls；
- wakeups；
- heap allocation；
- cross-thread handoff。

但 batching 太大也会影响：

~~~text
fairness / tail latency
~~~

所以它始终是权衡。

---

## 139. Asio 用 task sentinel 帮 Reactor 获得 Fairness Boundary

Reactor 完成当前批次后：

~~~text
descriptor ops
→ Scheduler queue

task_operation_
→ queue tail
~~~

让 completion 有机会先执行。

这避免 Reactor 无限自己循环 drain kernel events。

---

## 140. descriptor_state 也不直接无限执行用户 handler

它只：

~~~text
perform operation progress
move completed ops
~~~

再交回 Scheduler。

这让 execution fairness 仍由 Scheduler 主导。

---

## 141. Reactor 与 Scheduler 的边界本质是 Mechanism / Policy 分离

Reactor：

~~~text
what I/O can make progress?
~~~

Scheduler：

~~~text
which ready operation executes now?
~~~

Strand：

~~~text
which ready handlers may overlap?
~~~

Executor：

~~~text
where/how should handler execute?
~~~

这些问题最好不要塞进同一个类。

---

## 142. 一个自制网络 Runtime 最容易犯的错误

直接：

~~~text
epoll_wait
for each event:
  read socket
  call user callback
~~~

初期很简单。

很快就会遇到：

- callback reentrancy；
- fd close races；
- cancel；
- multiple pending ops；
- handler executor；
- buffer lifetime；
- shutdown；
- fairness；
- event coalescing；
- write readiness churn。

Asio 的层次就是这些问题演化出来的答案。

---

## 143. descriptor_state 是“把 fd 变成 Runtime Object”的关键一步

裸 fd：

~~~text
integer
~~~

Runtime object：

~~~text
identity
state
queues
locks
lifecycle
scheduling token
policy
~~~

这是从系统调用编程走向 Runtime 架构的分水岭。

---

## 144. 对机器人设备 Runtime 的直接迁移

比如 CAN fd：

~~~text
CAN socket
        ↓
CanChannelState
        |
        +-- RX pending ops
        +-- TX pending ops
        +-- error ops
        +-- current interests
        +-- shutdown
        +-- local mutex
~~~

Kernel readiness 只负责：

~~~text
驱动 CanChannelState progress
~~~

而不是直接进入任意业务模块。

---

## 145. 串口同样适用

~~~text
serial fd
        ↓
SerialSessionState
        |
        +-- pending reads
        +-- pending writes
        +-- timeout/deadline state
        +-- ownership
~~~

这样业务层可以保持：

~~~text
event-driven state machine
~~~

而不需要到处直接 `read()`/`write()`。

---

## 146. Device State 与 Operation State 要分开

`descriptor_state`：

~~~text
长期对象
~~~

`reactor_op`：

~~~text
一次请求
~~~

不要把两者混成：

~~~text
每个 request 自己保存完整 device state
~~~

否则：

- interest update；
- cancel all；
- close；
- error propagation；

都会变复杂。

---

## 147. Long-lived Control Block + Short-lived Operations 是通用结构

适用于：

- network connection；
- sensor device；
- CAN channel；
- GPU stream；
- file watcher；
- serial port；
- hardware queue。

长期 object 保存：

~~~text
共享状态
~~~

短期 operation 保存：

~~~text
单次请求
~~~

---

## 148. operation 的 Error/Bytes 属于请求，不属于 descriptor_state

`reactor_op` 保存：

~~~cpp
error_code ec_;
size_t bytes_transferred_;
~~~

因为：

~~~text
不同 operation 的 result
~~~

不能共享。

descriptor_state 只保存：

~~~text
fd-level state
~~~

这也是良好的 state partition。

---

## 149. cancellation_key_ 也属于 Operation

因为 targeted cancel 目标是：

~~~text
某一条请求
~~~

而不是：

~~~text
整个 descriptor
~~~

把 cancellation identity 放 operation 上更自然。

---

## 150. Cancel-all 与 Cancel-one 因此共享底层 Queue

~~~text
cancel all
→ drain whole queues

cancel by key
→ partition one queue
~~~

数据结构不需要两套。

---

## 151. try_speculative_ 属于 descriptor + readiness class

因为它描述的是：

~~~text
这一类 fd readiness
当前是否值得主动 retry
~~~

不是某一条 operation 独有。

所以它放：

~~~text
descriptor_state
~~~

而不是 reactor_op。

---

## 152. registered_events_ 同样属于 descriptor State

它是：

~~~text
fd 在 kernel 中当前注册什么 interest
~~~

多个 operation 共同影响它。

所以它不能放进某一条 write op。

---

## 153. shutdown_ 也是 Long-lived Control State

一旦 fd retire：

~~~text
后续 start_op
~~~

必须统一拒绝。

所以：

~~~text
shutdown_
~~~

属于 descriptor control block。

---

## 154. 正确 State Placement 是并发设计的核心

可以用一个问题判断字段放哪：

> 这个状态是“某一次请求”的，还是“这个资源整体”的？

请求级：

~~~text
ec
bytes
handler
cancellation key
~~~

资源级：

~~~text
fd
interest
pending queues
speculation confidence
shutdown
~~~

分对层，很多并发问题自然消失。

---

## 155. 为什么 descriptor_state 同时继承 operation 仍然不算混层

因为这个继承只是在表达：

~~~text
resource control block
可以临时成为一个 Scheduler token
~~~

不是说：

~~~text
descriptor_state 是某一条用户 I/O request
~~~

它是“可调度 control object”。

这是 intentional role composition。

---

## 156. Control Object 也可以是 Operation

Runtime Scheduler 的 operation abstraction 不必只装：

~~~text
用户 callback
~~~

还可以装：

- Reactor token；
- descriptor readiness token；
- timer dispatch token；
- shutdown control token。

只要 completion contract清楚即可。

---

## 157. 这就是 Asio Scheduler 为什么非常小

它不需要理解：

~~~text
socket
timer
strand
descriptor
~~~

只理解：

~~~text
operation*
~~~

复杂性被下沉到各种 typed operation。

这是 type erasure 的真正价值。

---

## 158. 为什么不用 std::variant<ReadOp, WriteOp, DescriptorState,...>

因为 Runtime 需要：

- 插件式 operation 类型；
- 低 allocation overhead；
- 头文件模板扩展；
- 自定义 allocator；
- backend-specific operation；
- intrusive queue。

函数指针 type erasure 比固定 variant 更开放。

---

## 159. 为什么不用 virtual complete()

Asio 更偏：

~~~text
function pointer
+
concrete typed allocation
~~~

避免：

- vtable requirement；
- 统一 inheritance hierarchy constraints；
- 某些额外 ABI/布局成本。

这属于低层 Runtime 常见风格。

---

## 160. Reactor_op 又在 Operation 上再加一层 perform()

因此有两个 function pointer 维度：

~~~text
perform_func_
→ I/O progress attempt

complete_func
→ completion / handler delivery
~~~

这是非常清晰的两阶段模型。

---

## 161. perform 与 complete 不能混成一个函数

`perform()` 可能：

~~~text
not_done
~~~

这时 operation 仍留在 Reactor queue。

`complete()` 则意味着：

~~~text
operation 生命周期进入完成阶段
~~~

将两者分开，状态机非常清楚。

---

## 162. 两阶段 Operation 是 Reactor 的核心抽象

~~~text
PENDING
  |
  | perform()
  |
  +-- not_done
  |     stay pending
  |
  +-- done
        leave reactor
        |
        v
      complete()
~~~

这比：

~~~text
event callback
~~~

更适合复杂 Runtime。

---

## 163. done_and_exhausted 是第三种“完成 + Readiness 信息”

所以 perform result 不只是：

~~~text
bool completed
~~~

还可以回传：

~~~text
资源状态 hint
~~~

让 control block更新：

~~~text
future speculative policy
~~~

这是一个小但很成熟的接口设计。

---

## 164. Operation Result 可以携带 Scheduling Hint

类似思想：

~~~text
task completed + reschedule immediately?
packet consumed + queue empty?
GPU kernel done + stream idle?
~~~

返回值可以同时表达：

~~~text
request result
+
scheduler hint
~~~

但必须保持语义清晰。

---

## 165. Edge-trigger Runtime 必须知道“什么时候停止 Drain”

`done_and_exhausted`：

~~~text
完成当前 op
但不要继续盲试后面的同类 op
~~~

`not_done`：

~~~text
当前 front op 都不能 progress
更不能继续后面的
~~~

`done`：

~~~text
当前 op 完成
可能还能继续下一条
~~~

这三态正好支撑 drain loop。

---

## 166. Queue Front Blocking 保证同类 Operation Order

如果 front：

~~~text
not_done
~~~

Asio：

~~~text
break
~~~

不会跳到后面的 operation。

否则：

~~~text
A blocked
B maybe complete
~~~

会破坏提交顺序。

所以 FIFO ordering 与 progress loop 是一致的。

---

## 167. 这意味着一个 Front Operation 可以形成 Head-of-line Blocking

这不是 bug。

它是：

~~~text
保持同类 async operation ordering
~~~

的代价。

上层若需要不同并发语义：

~~~text
应该拆不同 resource/queue
~~~

而不是让 Reactor随意越过 front。

---

## 168. Queue Design 永远包含 Ordering Tradeoff

~~~text
strict FIFO
→ predictable order
→ possible HOL blocking

out-of-order
→ more throughput
→ harder semantics
~~~

Asio 在同 readiness queue 内明显偏向 FIFO。

---

## 169. Reactor 不等于 Work-stealing Scheduler

它不会：

~~~text
挑任意可完成 operation
~~~

它严格围绕：

~~~text
fd + readiness class + queue order
~~~

工作。

这是 I/O protocol scheduler，不是 CPU task scheduler。

---

## 170. Descriptor Mutex 的 Critical Section 可能包含 Syscall

`perform_io()` 在 per-fd lock 内调用：

~~~text
op->perform()
→ nonblocking syscall
~~~

为什么可以？

因为这些 syscall 被要求：

~~~text
non-blocking
~~~

不会长期睡眠。

---

## 171. 这也是为什么 Internal Non-blocking 很重要

`reactive_descriptor_service::do_start_op()` 会确保：

~~~text
internal non-blocking
~~~

否则在 descriptor mutex 下：

~~~text
read/write 可能阻塞线程
~~~

整个 per-fd state machine 就会被卡住。

---

## 172. “锁内 syscall”是否合理取决于 Syscall Blocking Contract

不能简单说：

~~~text
锁里绝不能 syscall
~~~

而要看：

~~~text
这个 syscall 是否有严格 non-blocking 保证
~~~

Runtime 设计需要契约分析，而不是机械规则。

---

## 173. 但用户代码仍绝不能混进这个锁域

non-blocking syscall：

~~~text
受控、短、无任意重入
~~~

用户 handler：

~~~text
任意时长、任意调用图
~~~

两者风险完全不同。

所以：

~~~text
lock + syscall
~~~

有时可接受；

~~~text
lock + arbitrary user callback
~~~

通常危险。

---

## 174. Shutdown Race 也由 per-fd Lock 收束

~~~text
start_op
cancel
perform_io
deregister
~~~

都围绕 descriptor mutex。

因此 shutdown_ transition 可以形成：

~~~text
single serialization point
~~~

阻止新 work 与 retire 交错失控。

---

## 175. 这不代表业务对象 Lifetime 自动安全

per-fd lock 只保护：

~~~text
descriptor_state
reactor queues
~~~

handler 捕获的：

~~~text
Session*
buffer
application owner
~~~

仍要单独管理。

所以：

~~~text
Runtime state safety
!=
application object safety
~~~

---

## 176. descriptor_state Pointer Stable 也不保护 User Payload

Kernel 回来的 pointer：

~~~text
descriptor_state*
~~~

安全，只能说明 Reactor control block 还活着。

不能推导：

~~~text
async buffer 还活着
~~~

payload lifetime 仍由 async API contract要求。

---

## 177. 这几种 Lifetime 必须分别画

~~~text
OS fd
descriptor_state
reactor_op
handler
payload
application object
~~~

它们的起止时间不一样。

这是 Asio 系列文章一直反复出现的主线。

---

## 178. 一个典型 Close 时间线

~~~text
T0 async read pending
   reactor_op in descriptor queue

T1 close()
   deregister descriptor

T2 pending op moved out
   ec = operation_aborted

T3 fd closed

T4 descriptor_state cleanup

T5 aborted operation
   later dispatched

T6 user handler executes
~~~

所以：

~~~text
fd lifetime
<
completion lifetime
~~~

完全可能。

---

## 179. 这就是为什么 close() 返回后仍可能有 Handler

close 关闭的是：

~~~text
descriptor participation
~~~

不是：

~~~text
所有历史 completion execution
~~~

业务对象 reclaim 必须等：

~~~text
completion quiescence
~~~

---

## 180. epoll Reactor Page 与 Work Lifetime Page 的分工

这一篇回答：

~~~text
fd readiness
怎样变成 operation progress
~~~

[Work、Lifetime 与 Cancellation](work-lifetime-cancellation.md) 回答：

~~~text
这些 operation 的 work debt
和 handler lifetime
怎样收束
~~~

两者合起来才是完整 lifecycle。

---

## 181. 与 Scheduler Wakeup Page 的分工

[Scheduler 与 Reactor 唤醒协议](scheduler-reactor-wakeup.md) 回答：

~~~text
谁进入 epoll_wait
谁打断 epoll_wait
~~~

本篇回答：

~~~text
epoll_wait 返回以后
descriptor readiness 到底怎样被消费
~~~

上下游刚好接起来。

---

## 182. 与 Strand Page 的分工

Reactor 可以同时完成：

~~~text
read handler
timer handler
write handler
~~~

这些进入 Scheduler 后仍可能被多个 run thread 并发执行。

如果业务对象要求串行状态：

~~~text
再交给 Strand
~~~

所以：

~~~text
Reactor readiness ordering
≠
business-state serialization
~~~

---

## 183. 一条完整 Runtime Pipeline

~~~text
Kernel
  |
  | EPOLLIN
  v
epoll_reactor
  |
  | descriptor_state token
  v
Scheduler
  |
  | descriptor_state::perform_io
  v
reactor_op
  |
  | done
  v
Scheduler completion
  |
  | handler_work
  v
Strand / associated executor
  |
  v
User state machine
~~~

每一层解决不同问题。

---

## 184. 不要把“epoll 已经很高效”当成 Runtime 设计完成

epoll 只解决：

~~~text
大量 fd 的 readiness wait
~~~

它没有替你解决：

- object lifetime；
- queue ownership；
- handler execution；
- cancel；
- shutdown；
- work accounting；
- fairness；
- serialization；
- buffer lifetime。

真正困难的是 epoll 上面的 Runtime。

---

## 185. 对源码作者最重要的十条不变量

第一：

> **同一 readiness class 的 operation 不能被 speculative fast path 越序。**

第二：

> **operation 对 cancel/readiness 可见之前，work debt 必须在同一同步域内建立。**

第三：

> **descriptor_state 只是一枚 control token，不得重复计算 logical work。**

第四：

> **同一个 intrusive descriptor_state 在同一 local queue 中最多出现一次；重复 readiness 用 bitmask 合并。**

第五：

> **用户 handler 永远不能在 descriptor mutex 下执行。**

第六：

> **EPOLLET 下必须明确何时继续 drain、何时认为 readiness exhausted。**

第七：

> **Kernel registration lifetime、OS fd lifetime、userspace control-block lifetime必须分离。**

第八：

> **cancel、close/deregister、whole-runtime shutdown 是不同状态转换。**

第九：

> **Kernel event 是 re-evaluation hint，具体 syscall result 才是 operation truth。**

第十：

> **fast path 可以绕过 slow mechanism，但不能绕过 semantic ordering。**

---

## 186. 如果从零设计一个 Reactor Control Block

概念骨架：

~~~cpp
struct ChannelState : SchedulerOp
{
  Mutex mutex;

  int fd;
  EventMask interests;

  IntrusiveQueue<IoOp> read_ops;
  IntrusiveQueue<IoOp> write_ops;
  IntrusiveQueue<IoOp> error_ops;

  bool try_read;
  bool try_write;
  bool retired;

  EventMask ready_events;
};
~~~

但真正实现前必须继续回答：

- 谁拥有 `ChannelState`；
- kernel userdata 指针何时失效；
- operation work debt何时登记；
- close 与 queued completion 如何协调；
- event coalescing 怎样避免重复 intrusive enqueue；
- shutdown 怎样 drain；
- handler executor如何接入。

Asio 的价值就在于这些答案都在源码里。

---

## 187. 一个机器人 CAN Runtime 的映射

~~~text
CAN fd
  ↓
CanChannelState
  |
  +-- rx_queue
  +-- tx_queue
  +-- error_queue
  +-- epoll interests
  +-- try_rx / try_tx
  +-- retired
~~~

Kernel：

~~~text
EPOLLIN
~~~

只负责：

~~~text
CanChannelState becomes schedulable
~~~

然后：

~~~text
perform RX
→ decode frames
→ complete logical requests
→ post state-machine events
~~~

不要直接在 epoll loop 里调用所有控制模块。

---

## 188. 对高频传感器同样适用

例如：

~~~text
camera fd
lidar UDP socket
serial IMU
~~~

都可以先进入：

~~~text
resource-local control block
~~~

再把：

~~~text
raw readiness
~~~

变成：

~~~text
structured runtime completions
~~~

这能把 OS 细节隔离在最底层。

---

## 189. 对控制系统的一个重要启发：Readiness 与 State Transition 分层

底层：

~~~text
bytes available
~~~

并不等于：

~~~text
完整 packet available
~~~

完整 packet 也不等于：

~~~text
business state should transition
~~~

所以理想链条：

~~~text
OS readiness
→ transport progress
→ message completion
→ business event
→ state transition
~~~

不要跨层跳跃。

---

## 190. 分层越清楚，取消与故障越容易定义

如果全部在一个 callback 里：

~~~text
epoll
read
parse
mutate state
send response
~~~

cancel/close/error 会变得极其难拆。

分层以后：

~~~text
cancel transport op
close descriptor
drop message
retire session
~~~

可以分别定义。

---

## 191. 最终模型

~~~text
                    async operation
                          |
                          v
                  descriptor service
                          |
                          v
                   start_op()
                    /         \
                   /           \
          speculative works   would block
                |                 |
                v                 v
        immediate completion   per-fd op_queue
                                  |
                                  | work_started
                                  v
                              epoll wait
                                  |
                            readiness event
                                  |
                                  v
                        descriptor_state token
                                  |
                      +-----------+-----------+
                      |                       |
               already enqueued?             no
                      |                       |
                     yes                      v
                      |                 Scheduler local ops
                      v
                 OR event bits
                                  |
                                  v
                         Scheduler execution
                                  |
                                  v
                  descriptor_state::perform_io
                                  |
                 +----------------+----------------+
                 |                |                |
              except           write             read
                 |                |                |
                 v                v                v
              perform          perform          perform
                 |                |                |
        +--------+-------+        ...              ...
        |                |
     not_done           done
        |                |
        v                v
   remain pending   completion list
                         |
                +--------+--------+
                |                 |
             first op         remaining ops
                |                 |
                v                 v
        inline complete     deferred Scheduler
                |                 |
                +--------+--------+
                         |
                         v
                    handler_work
                         |
                         v
                 associated executor
                         |
                         v
                   user handler
~~~

这张图里最值得记住的不是 `epoll_ctl` 的参数。

而是：

> **Kernel readiness 只是 Runtime 的输入信号；真正成熟的 Reactor 会把它吸收到一个稳定的资源状态对象中，再通过 queue ownership、speculative progress、event coalescing、work accounting 和 completion protocol，把一个裸 fd 变成可组合的异步执行对象。**
