# Scheduler 与 Reactor 唤醒协议：Worker Sleep、task_operation_ 与 epoll Interrupter

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

在异步 Runtime 里，“线程没活干就睡眠，有活了再叫醒”听起来很简单。

真正写到源码层，问题会立刻变成：

~~~text
线程到底睡在哪里？

睡的是 Scheduler worker，
还是 Reactor 的 epoll_wait？

新 completion 到来时，
该 signal condition variable，
还是 interrupt epoll？

如果多个 run() thread 都在，
应该叫醒几个？

如果 queue 里还有 ready handler，
为什么不能让 Reactor 继续阻塞？

task_operation_ 为什么要作为 sentinel
塞进和普通 completion 同一条队列？

stop() 为什么既要 signal_all，
又要 interrupt Reactor？
~~~

Asio 的回答不是一个 wakeup API，而是一整套分层协议：

~~~text
scheduler::wakeup_event_
scheduler::task_operation_
scheduler::task_interrupted_
scheduler::wake_one_thread_and_unlock()
scheduler_task::run()
scheduler_task::interrupt()
epoll_reactor::run()
epoll_reactor::interrupt()
eventfd / pipe interrupter
~~~

这套机制最值得学习的地方在于：

> **Scheduler wait 与 Reactor wait 是两个不同阻塞域，唤醒逻辑必须知道当前 work 应该把哪一层从睡眠中拉回来。**

---

## 1. 先画出 Asio 中的两个阻塞点

一个 `io_context::run()` thread 进入 Scheduler 后，可能处于两种完全不同的睡眠状态。

第一种：

~~~text
Scheduler ready queue empty
        |
        v
wakeup_event_.wait(...)
~~~

第二种：

~~~text
dequeue task_operation_
        |
        v
reactor.run(...)
        |
        v
epoll_wait(...)
~~~

前者睡在：

~~~text
pthread_cond / event
~~~

后者睡在：

~~~text
kernel epoll wait queue
~~~

这两个阻塞点需要不同的唤醒机制。

---

## 2. Scheduler Event 与 Reactor Interrupter 不是同一个东西

Scheduler 有：

~~~cpp
event wakeup_event_;
~~~

Linux + pthread 构建下：

~~~text
event
→ posix_event
→ pthread_cond_t
~~~

Reactor 有：

~~~cpp
select_interrupter interrupter_;
~~~

Linux epoll 路径通常落到：

~~~text
eventfd_select_interrupter
~~~

所以：

~~~text
wakeup_event_
负责唤醒 Scheduler worker

interrupter_
负责打断 epoll_wait
~~~

不能混成一个“wake up”。

---

## 3. 为什么一个 Runtime 需要两种 Sleep Domain

Scheduler 的职责是：

~~~text
有没有 ready completion 可以执行？
~~~

Reactor 的职责是：

~~~text
kernel fd / timer 有没有变成 ready？
~~~

所以两种睡眠表达不同的问题。

Scheduler wait：

~~~text
我现在没有 ready user work
~~~

Reactor wait：

~~~text
我正在等待 kernel readiness
~~~

因此一个新的 `post()` handler 到来时：

~~~text
不需要 kernel fd readiness
~~~

它只需要让某个线程回到 Scheduler。

如果恰好所有可运行线程中只有一个卡在 `epoll_wait()`：

~~~text
又必须把 Reactor 那个线程打断
~~~

这就是唤醒协议的复杂性来源。

---

## 4. task_operation_ 是 Reactor 在 Scheduler Queue 中的代理

Scheduler 定义：

~~~cpp
struct task_operation : operation
{
  task_operation()
    : operation(0)
  {
  }
} task_operation_;
~~~

它不是普通用户 completion。

更像：

~~~text
“现在轮到 Reactor 跑一次”
~~~

的 control sentinel。

所以 Scheduler queue 同时承载：

~~~text
user completions
+
runtime task sentinel
~~~

这是一个 control-plane 与 data-plane 共用 intrusive queue 的设计。

---

## 5. init_task() 把 Reactor Sentinel 插入主队列

初始化：

~~~cpp
task_ = get_task_(context);
op_queue_.push(&task_operation_);
wake_one_thread_and_unlock(lock);
~~~

默认 Linux 下：

~~~text
task_
≈ epoll_reactor
~~~

因为 Reactor 实现了：

~~~cpp
class scheduler_task
{
  virtual void run(
      long usec,
      op_queue<scheduler_operation>& ops) = 0;

  virtual void interrupt() = 0;
};
~~~

所以 Scheduler 不需要知道：

~~~text
epoll
kqueue
select
io_uring
~~~

它只认识统一的：

~~~text
scheduler_task
~~~

---

## 6. task_operation_ 不是“一次性任务”

普通 completion：

~~~text
pop
execute
destroy
~~~

`task_operation_` 不同。

每次 Reactor `run()` 返回以后，`task_cleanup` 会：

~~~cpp
op_queue_.push(
    this_thread_->private_op_queue);

op_queue_.push(
    &scheduler_->task_operation_);
~~~

也就是：

~~~text
Reactor sentinel
被重新插回队尾
~~~

因此它代表一个长期存在的 Runtime service。

---

## 7. 为什么 Sentinel 要重新插到队尾

假设 queue：

~~~text
task_operation_
A
B
C
~~~

Scheduler 先取到 task。

如果 Reactor 返回以后又把 task 插回队头：

~~~text
task
A
B
C
~~~

它可能反复优先跑 Reactor，拖延已有 completion。

插到队尾：

~~~text
A
B
C
task
~~~

更接近：

~~~text
先处理已经 ready 的 completion
再回 kernel 查新的 readiness
~~~

这是一个很自然的 fairness 结构。

---

## 8. do_run_one() 如何识别两种 Operation

核心：

~~~cpp
operation* o = op_queue_.front();
op_queue_.pop();

if (o == &task_operation_)
{
  ...
  task_->run(...);
}
else
{
  ...
  o->complete(...);
}
~~~

所以 Scheduler 的 hot path 先做：

~~~text
sentinel discrimination
~~~

然后进入两个完全不同的执行域。

---

## 9. 取到普通 Handler 时，Scheduler 不进入 Reactor

普通 operation：

~~~text
pop
        |
        v
maybe wake another worker
        |
        v
unlock scheduler mutex
        |
        v
user completion
~~~

因此已有 ready work 时：

~~~text
优先消耗 completion
~~~

不必每次都先去 `epoll_wait()`。

---

## 10. 取到 task_operation_ 时，Scheduler 进入 Reactor

路径：

~~~text
pop task_operation_
        |
        v
decide timeout
        |
        v
unlock scheduler mutex
        |
        v
task_->run()
        |
        v
epoll_reactor::run()
        |
        v
epoll_wait()
~~~

这里 Reactor 可能阻塞。

所以此时这个 run() thread 已经离开：

~~~text
Scheduler condition-variable wait domain
~~~

进入：

~~~text
kernel epoll wait domain
~~~

---

## 11. task_usec_ 决定 Reactor 可以阻塞多久

源码：

~~~cpp
task_->run(
    more_handlers ? 0 : task_usec_,
    this_thread.private_op_queue);
~~~

如果：

~~~text
Scheduler queue 还有更多 ready handler
~~~

则：

~~~text
usec = 0
~~~

也就是：

~~~text
Reactor 只能 poll，
不能 block
~~~

这是一个非常重要的不变量。

---

## 12. 为什么有 Ready Handler 时绝不能让 Reactor Blocking

假设：

~~~text
queue:
A
B
task
~~~

取到 task 时还有：

~~~text
A / B
~~~

等待。

如果 Reactor：

~~~text
epoll_wait(-1)
~~~

无限阻塞，那么：

~~~text
明明 ready queue 有业务 work
线程却睡在 kernel
~~~

这是调度错误。

所以：

~~~text
more_handlers
→ reactor timeout = 0
~~~

保证 Reactor 只快速收割一轮 readiness，然后让 Scheduler继续。

---

## 13. task_interrupted_ 是什么

Scheduler 字段：

~~~cpp
bool task_interrupted_;
~~~

它不是：

~~~text
Reactor thread currently running?
~~~

更准确地说，它记录：

~~~text
Reactor task 是否已经被要求从 blocking wait 中返回
~~~

当准备进入 task：

~~~cpp
task_interrupted_ =
    more_handlers
    || task_usec_ == 0;
~~~

如果 Reactor 本来就不会 block：

~~~text
无需再 interrupt
~~~

因此标记为：

~~~text
already effectively interrupted
~~~

---

## 14. 为什么需要避免重复 interrupt

如果每一个 `post()` 都无条件：

~~~text
write eventfd
or
epoll_ctl MOD
~~~

在高并发下会产生大量：

- 系统调用；
- cache contention；
- kernel wakeup；
- 重复通知。

所以 Scheduler 用：

~~~text
task_interrupted_
~~~

抑制：

~~~text
已经要求 Reactor 醒来以后
又不断重复 interrupt
~~~

---

## 15. wake_one_thread_and_unlock() 是整个协议的决策中心

源码：

~~~cpp
void scheduler::wake_one_thread_and_unlock(
    mutex::scoped_lock& lock)
{
  if (wait_usec_ == 0
      || !wakeup_event_
            .maybe_unlock_and_signal_one(lock))
  {
    if (!task_interrupted_ && task_)
    {
      task_interrupted_ = true;
      task_->interrupt();
    }

    lock.unlock();
  }
}
~~~

这段函数非常值得拆开。

---

## 16. 第一优先级：先叫醒 Scheduler Waiter

核心：

~~~cpp
wakeup_event_
  .maybe_unlock_and_signal_one(lock)
~~~

如果确实存在：

~~~text
condition-variable waiter
~~~

那么：

~~~text
signal one Scheduler thread
~~~

通常就足够处理新 ready completion。

---

## 17. 为什么只 Signal One，不是 Signal All

一个新 handler 到来：

~~~text
只需要一个 worker
~~~

如果每次：

~~~text
broadcast
~~~

会产生：

~~~text
thundering herd
~~~

多个 worker 同时：

- 被唤醒；
- 争 Scheduler mutex；
- 发现只有一个 operation；
- 其余再次睡眠。

所以 fast path：

~~~text
one work item
→ wake one worker
~~~

---

## 18. 如果没有 Scheduler Waiter，就要考虑 Reactor

`maybe_unlock_and_signal_one()` 返回 false 时：

~~~text
当前没有 event waiter
~~~

但 Runtime 可能仍有 thread：

~~~text
睡在 epoll_wait
~~~

于是：

~~~cpp
if (!task_interrupted_ && task_)
  task_->interrupt();
~~~

这是第二层 wakeup。

---

## 19. 这就是“先 Scheduler，后 Reactor”的 Wake Routing

可以画成：

~~~text
new ready operation
        |
        v
Scheduler queue push
        |
        v
is there idle Scheduler waiter?
        |
   +----+----+
   |         |
  yes        no
   |         |
   v         v
signal one   Reactor may be blocked
worker       |
             v
         task_->interrupt()
~~~

这不是优化细节。

这是正确把 work 路由到：

~~~text
当前真正能被叫醒的执行域
~~~

---

## 20. 为什么 Scheduler Waiter 存在时通常不必 Interrupt Reactor

假设：

~~~text
Thread A:
  wakeup_event_.wait()

Thread B:
  epoll_wait()
~~~

新 handler 到来。

只要：

~~~text
Thread A
~~~

被叫醒，就可以执行这个 handler。

没有必要同时让：

~~~text
Thread B
~~~

离开 kernel wait。

否则会造成不必要的 Reactor churn。

---

## 21. more_handlers 时为什么主动 Wake Another Scheduler Thread

普通 completion 分支：

~~~cpp
if (more_handlers && !one_thread_)
  wake_one_thread_and_unlock(lock);
else
  lock.unlock();
~~~

当前 worker已经取走：

~~~text
一个 operation
~~~

如果 queue 后面还有：

~~~text
更多 ready operation
~~~

就尝试再叫醒一个 worker。

这让多 `run()` thread 可以并行消费 ready queue。

---

## 22. 这是逐步扩张，不是一次 Broadcast

队列：

~~~text
A B C D
~~~

Thread 1 取 A：

~~~text
发现 more_handlers
→ wake Thread 2
~~~

Thread 2 取 B：

~~~text
发现 more_handlers
→ wake Thread 3
~~~

形成：

~~~text
按需要逐步唤醒 worker
~~~

避免一次性唤醒所有线程。

---

## 23. one_thread_ 为什么可以跳过这些 Wake

配置：

~~~cpp
one_thread_ =
  concurrency_hint == 1;
~~~

如果 Runtime 明确只有一个 Scheduler execution thread：

~~~text
没有其他 worker 可叫醒
~~~

因此很多：

~~~text
wake another thread
~~~

分支可以跳过。

这减少：

- event signaling；
- shared queue handoff；
- thread wakeup bookkeeping。

---

## 24. one_thread_ 不是“整个程序只有一个线程”

它表达的是：

~~~text
Scheduler execution concurrency
被配置为 1
~~~

其他线程仍然可能：

~~~text
post work
start async I/O
~~~

只是不会有多个 worker 同时调用同一个 Scheduler 的 handler path。

---

## 25. one_thread_ 与 private_op_queue 有强关系

continuation fast path 可以：

~~~text
post to current thread
private_op_queue
~~~

减少主 Scheduler mutex 竞争。

单线程模式下：

~~~text
nested poll/run
~~~

还需要把外层 private queue 合并回主队列，避免内部嵌套调用看不到已有 work。

所以 single-thread optimization 不是简单：

~~~text
关闭 mutex
~~~

而是会改变 queue ownership 策略。

---

## 26. Scheduler Idle Wait 的状态机

当：

~~~text
op_queue_.empty()
~~~

`do_run_one()` 进入：

~~~cpp
wakeup_event_.clear(lock);

if (wait_usec_ > 0)
  wakeup_event_.wait_for_usec(
      lock, wait_usec_);
else
  wakeup_event_.wait(lock);
~~~

这是一套：

~~~text
clear
then wait
~~~

协议。

---

## 27. 为什么 Wait 前必须 Clear Event

`wakeup_event_` 不是单纯的：

~~~text
pthread_cond_wait
~~~

它还维护：

~~~text
signaled state
~~~

如果之前一次 signal 还留着：

~~~text
state.signaled = true
~~~

那么下一次 wait 应该直接通过，而不是睡下去。

所以：

~~~text
clear
~~~

显式宣布：

> 我准备进入新的 idle wait generation。

---

## 28. posix_event 的 state_ 同时编码 Signaled 与 Waiter Count

实现：

~~~text
state_ bit 0
= signaled

state_ / 2
≈ waiter count
~~~

等待：

~~~cpp
while ((state_ & 1) == 0)
{
  state_ += 2;
  pthread_cond_wait(...);
  state_ -= 2;
}
~~~

因此同一个整数同时记录：

~~~text
event state
+
waiter presence
~~~

---

## 29. maybe_unlock_and_signal_one() 为什么能知道有没有 Waiter

源码：

~~~cpp
state_ |= 1;

if (state_ > 1)
{
  lock.unlock();
  pthread_cond_signal(...);
  return true;
}

return false;
~~~

如果：

~~~text
state_ > 1
~~~

说明：

~~~text
waiter count 非零
~~~

于是 signal 一个。

否则：

~~~text
只把 event 标成 signaled
~~~

让未来 waiter 不会错过这次通知。

---

## 30. 这是一种 Lost-Wakeup Protection

经典错误：

~~~text
Producer:
  check waiter absent

Consumer:
  about to sleep

Producer:
  signal lost

Consumer:
  sleeps forever
~~~

Asio 的 event 把：

~~~text
signaled state
+
waiter registration
~~~

放在 Scheduler mutex 同一个保护域里。

因此：

~~~text
clear
waiter count
signal state
queue mutation
~~~

被协议化。

---

## 31. pthread_cond 本身不保存历史 Signal

裸 `pthread_cond_signal()`：

~~~text
没有 waiter
→ signal 直接丢失
~~~

Asio `posix_event` 用：

~~~text
state_ bit 0
~~~

补出：

~~~text
sticky signal
~~~

语义。

所以它更像：

~~~text
condition variable
+
manual bookkeeping event
~~~

---

## 32. 为什么 wakeup_event_ 要和 Scheduler mutex 配合

如果 queue push 与：

~~~text
event signal
~~~

不在同一状态机里，就容易出现：

~~~text
worker checks queue empty
producer pushes work
worker sleeps
producer misses wake
~~~

Scheduler 的做法：

~~~text
queue mutation
+
wait state transition
+
signal decision
~~~

都围绕同一 mutex 发生。

这正是避免 lost wakeup 的关键。

---

## 33. wake_one_thread_and_unlock() 把 Unlock 也纳入协议

名字不是：

~~~text
wake_one_thread()
~~~

而是：

~~~text
wake_one_thread_and_unlock()
~~~

因为：

> **unlock 的时机本身就是 wakeup correctness 的一部分。**

不能随便：

~~~text
unlock
then inspect waiter state
then signal
~~~

否则 waiter 可能在中间改变。

---

## 34. unlock_and_signal_one 为什么先记录 Signal 再 Unlock

`posix_event`：

~~~cpp
state_ |= 1;
bool have_waiters = state_ > 1;

lock.unlock();

if (have_waiters)
  pthread_cond_signal(...);
~~~

关键是：

~~~text
signaled state
在 mutex 保护下先建立
~~~

unlock 后即使 waiter 被调度：

~~~text
也能看到 signal state
~~~

这是一种标准条件变量协议。

---

## 35. Reactor 的 epoll_wait 是第二套 Sleep Protocol

`epoll_reactor::run()`：

~~~cpp
int num_events =
    epoll_wait(
      epoll_fd_,
      events,
      128,
      timeout);
~~~

这里 Scheduler mutex 已经释放。

因此 Reactor 等待期间：

~~~text
其他线程完全可以继续 post work
~~~

问题变成：

> 新 work 与当前 epoll interest 无关时，怎样打断这个 `epoll_wait()`？

---

## 36. scheduler_task::interrupt() 把答案抽象掉

Scheduler 只调用：

~~~cpp
task_->interrupt();
~~~

不关心：

~~~text
epoll uses eventfd
kqueue uses user event
select uses pipe
io_uring uses wake mechanism
~~~

这是一种非常干净的 abstraction boundary。

Scheduler 只表达：

~~~text
“你的阻塞 wait 应该尽快返回”
~~~

由具体 Reactor 决定怎么做到。

---

## 37. Linux epoll Reactor 的 interrupt() 非常特殊

固定源码：

~~~cpp
void epoll_reactor::interrupt()
{
  epoll_event ev = {0, {0}};
  ev.events =
      EPOLLIN
      | EPOLLERR
      | EPOLLET;

  ev.data.ptr = &interrupter_;

  epoll_ctl(
      epoll_fd_,
      EPOLL_CTL_MOD,
      interrupter_
        .read_descriptor(),
      &ev);
}
~~~

注意：

~~~text
这里没有 write(eventfd)
~~~

这与很多 Reactor 实现完全不同。

---

## 38. Interrupter 在 Reactor 构造时先被置为 Readable

构造函数：

~~~text
add interrupter fd to epoll
        |
        v
interrupter_.interrupt()
~~~

而 `eventfd_select_interrupter::interrupt()`：

~~~cpp
uint64_t counter(1);
write(write_descriptor_,
      &counter,
      sizeof(counter));
~~~

所以初始化以后：

~~~text
interrupter fd 处于 readable 状态
~~~

---

## 39. epoll Reactor 故意不 Reset Interrupter

处理 event：

~~~cpp
if (ptr == &interrupter_)
{
  // no reset
}
~~~

源码解释：

~~~text
leave descriptor ready-to-read
and rely on edge-triggered notification
when epoll registration is updated
~~~

所以它没有：

~~~text
read eventfd counter back to zero
~~~

---

## 40. 为什么一个一直 Readable 的 fd 还能再次 Wake epoll

因为这里使用：

~~~text
EPOLLET
~~~

普通情况下：

~~~text
fd 一直 readable
→ 不会不断重复 edge notification
~~~

当需要主动 interrupt 时，Asio执行：

~~~text
EPOLL_CTL_MOD
~~~

重新修改 interrupter 的 epoll registration。

对于这个已经 ready 的 fd：

~~~text
registration update
→ epoll 再次报告 readiness
~~~

从而把阻塞的 `epoll_wait()` 拉回来。

---

## 41. 这是一种“Persistent Ready + Re-arm by MOD”策略

传统策略：

~~~text
need wake
→ write eventfd

epoll returns
→ read/reset eventfd
~~~

Asio epoll 策略：

~~~text
startup:
  make fd permanently readable

need wake:
  EPOLL_CTL_MOD registration

epoll returns:
  keep fd readable
~~~

因此 runtime wake fast path 避免每次：

~~~text
write + read
~~~

一对 system call。

---

## 42. eventfd_select_interrupter 仍保留通用 Write/Reset 能力

这个 interrupter 类型也服务：

- select；
- 其他 reactor。

所以它本身提供：

~~~text
interrupt() -> write

reset() -> read/drain
~~~

只是：

~~~text
epoll_reactor
~~~

选择了一种更特殊的使用协议：

~~~text
write once
keep ready
re-arm with EPOLL_CTL_MOD
~~~

说明：

> 一个 primitive 的 API 不等于每个 backend 都必须按同一种协议使用。

---

## 43. 为什么 Interrupter fd 也注册 EPOLLET

如果没有 edge-trigger：

~~~text
interrupter permanently readable
~~~

会导致：

~~~text
epoll_wait()
每次立刻返回
~~~

Runtime 进入 busy loop。

EPOLLET 让：

~~~text
readable state
~~~

只在：

~~~text
edge / re-arm
~~~

时通知。

---

## 44. Interrupter 本质上是 Control FD

普通 descriptor：

~~~text
socket fd
timer fd
~~~

代表外部 I/O readiness。

Interrupter：

~~~text
runtime control fd
~~~

代表：

~~~text
“不要继续睡，回 Scheduler 看一下”
~~~

所以 epoll interest set 中同时存在：

~~~text
data-plane fd
control-plane fd
~~~

---

## 45. timerfd 是第三种 Wake Source

Linux 下 Reactor 还可能注册：

~~~text
timer_fd_
~~~

因此 `epoll_wait()` 可以因为三类事件返回：

~~~text
I/O descriptor ready

timerfd ready

interrupter control event
~~~

这三个来源最后都会被 Reactor 翻译成：

~~~text
scheduler operations
or
timer checks
~~~

---

## 46. 为什么有 timerfd 时 Reactor Timeout 可以更简单

如果：

~~~text
timer_fd_ != -1
~~~

timer deadline 已经由 kernel timerfd 表达。

所以：

~~~text
epoll_wait
~~~

不必每次把最近 timer deadline折算成 timeout。

Timer 到期：

~~~text
timerfd becomes readable
~~~

自然唤醒 epoll。

---

## 47. 没有 timerfd 时，需要把 Timer Deadline 转成 epoll Timeout

源码：

~~~text
usec
→ milliseconds
→ compare timer queue earliest deadline
→ epoll_wait(timeout)
~~~

因此 Reactor blocking timeout 同时受：

~~~text
Scheduler task budget
+
nearest timer deadline
~~~

约束。

---

## 48. schedule_timer() 为什么可能需要 Update Timeout

新增 timer：

~~~cpp
bool earliest =
  queue.enqueue_timer(...);

scheduler_.work_started();

if (earliest)
  update_timeout();
~~~

只有新 timer 成为：

~~~text
earliest deadline
~~~

才需要调整 Reactor wakeup。

否则：

~~~text
当前 epoll wait deadline
已经更早
~~~

不需要打断。

---

## 49. 这是“只在调度边界变早时 Wake”的优化

新增：

~~~text
10 秒后 timer
~~~

但原来已有：

~~~text
1 秒后 timer
~~~

没必要唤醒 Reactor。

新增：

~~~text
100 微秒后 timer
~~~

才必须：

~~~text
update timeout
~~~

这是一种非常典型的 deadline scheduler 优化。

---

## 50. 有 timerfd 时 update_timeout 直接改 Kernel Timer

~~~cpp
timerfd_settime(...)
~~~

然后返回。

无需：

~~~text
interrupt epoll
~~~

因为 timerfd 本身已经在 epoll interest set 中。

Kernel 会在新 deadline 到来时：

~~~text
标记 timerfd readable
→ wake epoll
~~~

---

## 51. 没有 timerfd 时 update_timeout 必须 Interrupt

否则：

~~~text
Reactor 已经 epoll_wait(old_timeout)
~~~

新更早 timer 虽然进了 userspace queue：

~~~text
kernel 不知道
~~~

所以必须：

~~~text
interrupt()
→ epoll_wait returns
→ recompute timeout
~~~

这是 userspace timer wheel 与 kernel wait 的同步问题。

---

## 52. wakeup_event 与 timerfd/interrupter 解决不同层

~~~text
Scheduler wakeup_event
  wakes a thread waiting for ready work

Reactor interrupter
  aborts kernel readiness wait

timerfd
  makes a future deadline visible to kernel epoll
~~~

三者不能简单互换。

---

## 53. task_cleanup 是 Reactor → Scheduler 的 Return Bridge

Reactor：

~~~text
epoll_wait returns
        |
        v
collect descriptor states / timers
        |
        v
append ops to
this_thread.private_op_queue
~~~

然后 `task_cleanup` 析构：

~~~text
lock Scheduler
        |
        v
merge private_op_queue
into global op_queue_
        |
        v
reinsert task_operation_
~~~

这是：

~~~text
Reactor result publication
~~~

回 Scheduler 的边界。

---

## 54. 为什么 Reactor 不直接在 epoll loop 里调用用户 Handler

如果 Reactor直接：

~~~text
fd ready
→ invoke handler
~~~

就把：

- readiness detection；
- execution scheduling；
- handler serialization；
- executor policy；

全部耦合起来。

Asio 保持：

~~~text
Reactor
only produces ready operations

Scheduler
decides execution
~~~

这使 Strand、executor、work accounting 都能继续组合。

---

## 55. Reactor 返回的 Operation 先进入 Thread-private Queue

`task_->run()` 参数：

~~~cpp
this_thread.private_op_queue
~~~

所以 Reactor不需要一边遍历 epoll events，一边不断争 Scheduler mutex。

它先：

~~~text
locally collect ready operations
~~~

然后由：

~~~text
task_cleanup
~~~

一次性 splice 回主 queue。

这是 batch publication。

---

## 56. Batch Publication 减少 Scheduler Mutex Traffic

假设一次 `epoll_wait()` 返回：

~~~text
64 个 ready fd
~~~

如果每个 completion 都：

~~~text
lock scheduler
push one
unlock
~~~

会产生大量锁操作。

现在：

~~~text
collect 64 ops locally
        |
        v
one scheduler lock
        |
        v
splice whole queue
~~~

这与 Strand：

~~~text
waiting → ready batch handoff
~~~

是相似的设计思想。

---

## 57. task_cleanup 为什么必须 RAII

`task_->run()` 注释明确：

~~~text
may throw
~~~

如果 Reactor task 抛异常后：

~~~text
task_operation_ 不重新插回 queue
~~~

Runtime 以后就再也不会进入 Reactor。

所以 `task_cleanup` 保证：

~~~text
normal return
or exception unwind
~~~

都会：

- merge Reactor completions；
- restore task sentinel；
- restore accounting state。

---

## 58. task_cleanup 还负责 private_outstanding_work 结算

Reactor 或内部 completion path 可能把：

~~~text
private_outstanding_work
~~~

积累在当前 thread。

退出 task 时：

~~~text
transfer to global outstanding_work_
~~~

然后清零。

因此 task cleanup 同时完成：

~~~text
queue publication
+
work accounting publication
+
task sentinel re-arm
~~~

---

## 59. task_interrupted_ 在 task_cleanup 里重新置 true

源码：

~~~cpp
scheduler_->task_interrupted_ = true;
~~~

含义是：

~~~text
当前 task 已经返回
~~~

所以不存在一个仍在 kernel block 的 Reactor task需要被 interrupt。

下一次 Scheduler重新取到：

~~~text
task_operation_
~~~

会重新设置是否可能 block。

---

## 60. task_interrupted_ 其实是 Reactor Blocking-State Hint

可以更准确地理解为：

~~~text
false
≈ Reactor task may currently require an interrupt

true
≈ no useful interrupt is currently needed
~~~

它不是严格 thread state enum。

而是 wakeup optimization state。

---

## 61. stop_all_threads() 为什么既 Signal All 又 Interrupt Task

源码：

~~~cpp
stopped_ = true;

wakeup_event_.signal_all(lock);

if (!task_interrupted_ && task_)
{
  task_interrupted_ = true;
  task_->interrupt();
}
~~~

因为 stop() 要让：

~~~text
所有 run() thread
尽快返回
~~~

这些线程可能分布在两个 wait domain。

---

## 62. Signal All 只覆盖 Scheduler Waiters

~~~text
wakeup_event_.signal_all
~~~

可以叫醒：

~~~text
所有 condition-variable waiters
~~~

但叫不醒：

~~~text
epoll_wait
~~~

所以仍需要：

~~~text
task_->interrupt()
~~~

---

## 63. Interrupt Reactor 也不能替代 Signal All

反过来：

~~~text
epoll interrupt
~~~

只保证那个正在 Reactor task里的线程回来。

其他线程如果睡在：

~~~text
wakeup_event_.wait
~~~

仍然不会醒。

所以 stop 必须双管齐下。

---

## 64. 这就是 Multi-Wait-Domain Shutdown

一个 Runtime shutdown 前必须枚举：

~~~text
有哪些地方线程可能阻塞？
~~~

然后：

~~~text
每一个阻塞域
都必须有明确退出信号
~~~

否则 shutdown 很容易 hang。

---

## 65. 常见阻塞域不只 Condition Variable

大型 Runtime 还可能有：

- `epoll_wait`；
- `poll`；
- `recv`；
- futex；
- semaphore；
- eventfd；
- GPU stream wait；
- condition variable；
- blocking device ioctl；
- completion port。

shutdown 设计必须逐个回答：

~~~text
怎么叫醒？
~~~

Asio 是一个很好的示范。

---

## 66. 为什么 post() 既 Push Queue 又 Wake

`post_immediate_completion()` 非 continuation 路径：

~~~text
work_started
lock Scheduler
push op
wake_one_thread_and_unlock
~~~

队列 publication 与 wake notification 是同一个 protocol。

不能只：

~~~text
push queue
~~~

然后指望 sleeping worker“迟早自己醒”。

---

## 67. Queue 与 Wakeup 是一对不可分割的机制

很多自制 thread pool 的 bug 就来自：

~~~text
ConcurrentQueue 很正确
~~~

但：

~~~text
sleep/wakeup protocol 错了
~~~

比如：

~~~text
lost wakeup
too many wakeups
worker starvation
shutdown hang
~~~

所以：

> 一个并发队列从来都不是完整的线程调度器。

---

## 68. continuation Fast Path 为什么可能不 Wake

当前 Scheduler thread 内：

~~~text
continuation
→ private_op_queue
~~~

此时：

~~~text
当前线程本来就在执行 Scheduler
~~~

不需要另外叫醒一个 worker。

而且 continuation 常常希望：

~~~text
保持 locality
~~~

因此可以延迟到当前 completion结束时再统一发布。

---

## 69. 这是“知道执行上下文”后的 Wake Elision

如果 caller 已知：

~~~text
我就在 Runtime worker 内
~~~

就可以避免：

~~~text
global queue push
event signal
thread wakeup
~~~

这比单纯优化 mutex 更有效。

---

## 70. 但 continuation Fast Path 仍必须维持 Work Accounting

所以 private queue 同时配：

~~~text
private_outstanding_work
~~~

否则：

~~~text
work 被隐藏到 thread-local queue
但 global liveness 看不到
~~~

Runtime 可能误判退出条件。

---

## 71. Scheduler Queue Empty 不代表 Reactor 没 Work

这里与前一篇 Work Lifetime 完全一致。

例如：

~~~text
async_read pending
~~~

此时：

~~~text
Scheduler op_queue_
可能只有 task_operation_

真正 read op
还在 descriptor_state::op_queue_
~~~

所以：

~~~text
ready queue inventory
~~~

不能代表系统总工作量。

---

## 72. task_operation_ 让 Scheduler 在 Empty-ready 状态仍有 Reactor 入口

如果完全没有 sentinel：

~~~text
Scheduler ready queue empty
→ workers sleep
~~~

那 kernel fd readiness 到来时：

~~~text
谁在 epoll_wait？
~~~

所以至少需要一个执行单元：

~~~text
负责进入 Reactor
~~~

`task_operation_` 就是把：

~~~text
Reactor responsibility
~~~

显式嵌入 Scheduler queue。

---

## 73. 这和 Dedicated I/O Thread 不同

另一种 Runtime 设计可能：

~~~text
Thread 0
专门 epoll_wait

Worker 1..N
只执行 handler
~~~

Asio Scheduler task 模型更灵活：

~~~text
任意 run() worker
取到 task_operation_
就暂时成为 Reactor runner
~~~

因此 Reactor owner 可以在不同 OS thread 之间迁移。

---

## 74. Reactor Ownership 也是 Logical，不是固定 Thread Affinity

这和 Strand 类似。

Strand：

~~~text
logical handler owner
can move across threads
~~~

Reactor task：

~~~text
whoever dequeues task_operation_
becomes current Reactor runner
~~~

所以系统没有强制：

~~~text
固定 thread 0 == epoll thread
~~~

---

## 75. 一个 task_operation_ 保证同时最多一个 Reactor Runner

因为：

~~~text
主 queue 中只有一个 sentinel instance
~~~

某个线程 pop 后：

~~~text
sentinel 暂时不在 queue
~~~

直到 task_cleanup：

~~~text
重新插回
~~~

所以不会两个 Scheduler worker 同时拿到同一个 Reactor task。

这是一种非常简单的 single-owner token。

---

## 76. Sentinel 本身就是 Ownership Token

可以把：

~~~text
task_operation_ 在 queue 中
~~~

理解为：

~~~text
Reactor execution right currently available
~~~

被某线程 pop：

~~~text
execution right acquired
~~~

task cleanup reinsert：

~~~text
execution right released / republished
~~~

这和锁非常不同，但完成了类似的 ownership transfer。

---

## 77. 为什么不额外放一个 reactor_running mutex

因为：

~~~text
queue membership
~~~

本身已经能表达：

~~~text
Reactor execution token 是否可获得
~~~

不需要再维护一份：

~~~text
bool reactor_running
~~~

避免双重状态不一致。

---

## 78. 这是“用队列中的唯一对象表达执行权”

这类模式非常值得迁移。

例如：

~~~text
single database flush token
single hardware bus poll token
single GC phase token
single control-loop maintenance task
~~~

只要：

~~~text
token instance unique
~~~

queue ownership 就可以自然保证单执行者。

---

## 79. epoll_wait 返回以后不直接 Complete Descriptor Operation

Reactor 对普通 descriptor event：

~~~text
descriptor_state*
~~~

先加入：

~~~text
ops
~~~

而不是立刻：

~~~text
read/write handler complete
~~~

`descriptor_state` 本身也是一个 operation-like task。

它并不只是“fd 的一些元数据”：同一个对象还负责聚合 epoll readiness bit、持有 READ/WRITE/EXCEPT 三类 pending operation queue，并以 Scheduler operation 的身份把 kernel readiness 推进成用户 completion。完整状态机见 [epoll Reactor 与 descriptor_state](epoll-reactor-descriptor-state.md)。

后面 Scheduler再调用它的 perform I/O logic，把真正 ready operation 推出来。

这进一步维持：

~~~text
Reactor detection
≠
user completion
~~~

分层。

---

## 80. Reactor Event Coalescing 为什么检查 is_enqueued

一次 `epoll_wait()` 返回的 events 可能让同一个 descriptor：

~~~text
READ
WRITE
ERROR
~~~

多种 readiness 聚合。

如果 descriptor_state 已经进 local ops queue：

~~~text
只 add_ready_events
~~~

而不是重复 enqueue。

所以：

~~~text
one descriptor state
→ one scheduler entry
→ bitmask carries multiple readiness
~~~

减少重复调度。

---

## 81. 这也是 Control Plane Aggregation

类似 Strand 批处理：

~~~text
多个 producer event
→ aggregate state
→ single scheduled object
~~~

Asio 大量使用这种：

~~~text
合并状态
而不是生成无限重复任务
~~~

的设计。

---

## 82. Interrupter 不承载业务 Payload

它只表达：

~~~text
wake reason:
runtime state changed
~~~

真正新 work 在：

~~~text
Scheduler queue
timer queue
descriptor registry
~~~

里。

因此 wake fd 只是一枚：

~~~text
doorbell
~~~

不是消息通道。

---

## 83. Doorbell Pattern 很常见

典型：

~~~text
shared queue
+
eventfd/pipe
~~~

producer：

~~~text
push real data
ring doorbell
~~~

consumer：

~~~text
wake
drain shared state
~~~

Doorbell 不复制业务数据。

Asio epoll interrupter 就是更极端的：

~~~text
persistent-ready doorbell
~~~

---

## 84. 为什么 Doorbell 与 Queue Publication 顺序重要

正确：

~~~text
publish work
        |
        v
ring doorbell
~~~

错误：

~~~text
ring doorbell
        |
        v
publish work
~~~

consumer 可能：

~~~text
wake
see nothing
sleep again
~~~

然后 producer 才 push，造成 lost wakeup。

Scheduler 的：

~~~text
op_queue_.push(op)
→ wake_one_thread_and_unlock
~~~

就是严格的 publish-before-notify。

---

## 85. Timer Update 同样遵循“先改状态，再唤醒”

schedule timer：

~~~text
lock timer queue
insert timer
work_started
determine earliest
        |
        v
update_timeout
~~~

所以 Reactor 被叫醒后：

~~~text
新的 timer 状态已经可见
~~~

不会出现：

~~~text
醒来但 deadline 还没更新
~~~

---

## 86. Stop 同样是“先改状态，再广播”

~~~text
stopped_ = true
        |
        v
signal_all Scheduler waiters
        |
        v
interrupt Reactor
~~~

线程醒来以后：

~~~text
检查 stopped_
→ exit
~~~

这也是经典 shutdown ordering。

---

## 87. Wakeup Signal 本身不代表 Work

Scheduler worker 被 signal 后：

~~~text
仍然必须重新检查
op_queue_
stopped_
~~~

因为：

- signal 可被合并；
- 多线程竞争；
- stop 也会 signal；
- spurious wake 可能存在。

因此：

> wakeup 是“状态可能变化”的提示，不是状态本身。

---

## 88. 这与 Holoscan Event Wakeup 的结论一致

事件通知：

~~~text
不是 READY permission
~~~

Scheduler wakeup：

~~~text
也不是“你一定有 handler”
~~~

真正状态仍然必须重新读取：

~~~text
queue / stopped / timer / descriptor
~~~

这是事件驱动系统的共同原则。

---

## 89. 一个正确 Event Loop 应把 Wakeup 当 Hint

不要设计：

~~~text
每个 wake byte
严格对应一条 task
~~~

除非协议明确这样保证。

更稳健的是：

~~~text
wakeup
→ re-check authoritative state
→ drain what is currently ready
~~~

Asio正是这样做的。

---

## 90. 为什么 Scheduler Event 需要 Sticky State，而 Reactor Interrupter 不必承载计数

Scheduler event：

~~~text
可能发生 signal-before-wait
~~~

所以要保存：

~~~text
signaled bit
~~~

epoll interrupter：

~~~text
fd 本身永久 readable
~~~

readiness 就已经是 sticky kernel state。

因此两个 wait domain 采用了不同的“防丢通知”策略。

---

## 91. Sticky User-space Event 与 Sticky Kernel Readiness

Scheduler：

~~~text
state_ bit0
~~~

是 userspace sticky notification。

Reactor：

~~~text
eventfd readable
~~~

是 kernel sticky readiness。

它们本质都在解决：

~~~text
notification arrives before waiter sleeps
~~~

不能丢。

---

## 92. EPOLLET 又防止 Sticky Readiness 变成 Busy Loop

如果 fd 永远 readable，level-trigger：

~~~text
epoll_wait
immediately returns forever
~~~

加 EPOLLET：

~~~text
只在 edge / registration update 时重新通知
~~~

形成：

~~~text
sticky state
+
explicit re-arm
~~~

组合。

---

## 93. 这是一种很巧妙的 Wakeup Coalescing

多次：

~~~text
interrupt request
~~~

在 Reactor 还没重新 block 前可以被：

~~~text
task_interrupted_
~~~

合并。

Kernel 侧 fd 又保持：

~~~text
readable
~~~

不需要每次递增 eventfd counter。

所以 wakeup protocol 同时在：

~~~text
Scheduler state
+
epoll registration state
~~~

两层做 coalescing。

---

## 94. Wakeup 过多与 Wakeup 过少都是 Bug

过少：

~~~text
work queued
thread sleeps forever
~~~

这是 correctness failure。

过多：

~~~text
大量空唤醒
system call storm
cache contention
context switch
~~~

是性能 failure。

成熟 Runtime 要同时解决：

~~~text
no lost wakeup
+
avoid redundant wakeup
~~~

---

## 95. Asio 的核心策略可以总结成三步

第一：

~~~text
publish authoritative state
~~~

第二：

~~~text
select correct wait domain
~~~

第三：

~~~text
coalesce redundant wakeups
~~~

这比“用 eventfd 唤醒 epoll”完整得多。

---

## 96. 机器人设备 Runtime 可以怎样迁移

假设有：

~~~text
CAN I/O thread
Command Scheduler workers
Timer/deadline manager
~~~

不要直接写：

~~~text
有新命令
→ notify_all()
~~~

应该先画：

~~~text
Worker 可能睡在哪？

CAN thread 可能睡在哪？

Timer 可能睡在哪？
~~~

再为每层设计：

~~~text
authoritative state
+
wake primitive
+
shutdown wake
~~~

---

## 97. CAN Poll Thread 与 Scheduler Worker 很像这两层

例如：

~~~text
Control command queue
        |
        v
worker condvar wait

CAN socket
        |
        v
epoll/select wait
~~~

一个外部 command 可能只需要：

~~~text
叫醒 worker
~~~

但：

~~~text
shutdown
~~~

可能同时要：

~~~text
wake worker
+
break epoll wait
~~~

这就是 Asio 的双域模型。

---

## 98. 不要用固定 1 ms Polling 逃避 Wakeup Protocol

很多机器人程序为了简单：

~~~text
while(running)
{
  poll queue
  poll socket
  sleep(1ms)
}
~~~

这避免了 lost wakeup，但代价是：

- 固定延迟；
- CPU 空转；
- 时延抖动；
- 不必要 syscall；
- 电源消耗。

真正成熟的 Runtime：

~~~text
无 work 时阻塞
有 work 时精准唤醒
~~~

---

## 99. 但精准 Wakeup 需要更严格的不变量

你必须证明：

~~~text
work publication 与 wakeup 顺序

waiter registration 与 signal 顺序

shutdown state 与 broadcast 顺序

kernel wait 与 interrupter 协议

timer deadline 更新与 timeout 更新
~~~

这就是 event loop 实现真正难的地方。

---

## 100. 一个最小双域 Runtime 骨架

概念：

~~~cpp
class Runtime
{
  Mutex mutex;
  Queue ready;
  Event worker_event;

  Reactor reactor;

  void Post(Task* t)
  {
    Lock lock(mutex);

    ready.push(t);

    if (!worker_event.SignalOne(lock))
      reactor.Interrupt();
  }
};
~~~

这个骨架并不完整。

但它已经比：

~~~text
queue + notify
~~~

多表达了：

~~~text
两个不同 wait domain
~~~

---

## 101. Reactor Token 也可以做成 Queue Sentinel

概念：

~~~cpp
Task reactor_token;
~~~

Scheduler：

~~~text
pop reactor_token
→ temporarily become I/O poller
→ collect completions
→ requeue reactor_token
~~~

这样不需要固定 I/O thread。

Asio 的 `task_operation_` 就是这个设计的成熟版本。

---

## 102. 什么时候更适合 Dedicated I/O Thread

如果系统需要：

- 严格 thread affinity；
- NIC polling busy loop；
- CPU pinning；
- RT priority separation；
- io_uring SQPOLL 特殊模型；
- 设备驱动 API 要求固定线程；

专门 I/O thread 可能更合适。

Asio 的模式更偏：

~~~text
general-purpose cooperative runtime
~~~

---

## 103. Scheduler Task 模式的优点

- 不需要固定 Reactor thread；
- Reactor 与 completion worker 共用 thread pool；
- sentinel 自然表达 single Reactor ownership；
- workload 轻时线程利用率更高；
- backend 可以通过 scheduler_task abstraction 替换。

代价是：

~~~text
wakeup protocol
更复杂
~~~

---

## 104. Wakeup Event 的 Waiter Count 为什么是性能信息

`posix_event::state_ > 1`：

~~~text
有 waiter
~~~

所以 producer 能决定：

~~~text
signal condvar
~~~

还是：

~~~text
不必 signal
~~~

它不是为了业务语义。

而是让 wakeup path 避免无意义 syscall。

---

## 105. Scheduler Lock 同时保护 Queue 与 Wake State

这是非常关键的设计选择。

如果：

~~~text
queue mutex
~~~

和：

~~~text
wait-state mutex
~~~

分开，协议会复杂很多。

需要解决：

~~~text
queue empty
→ switch mutex
→ register waiter
~~~

之间的 race。

统一 mutex 让：

~~~text
queue observation
+
wait registration
~~~

线性化。

---

## 106. 这是为什么 Condition Variable 通常要和 Predicate Lock 一起设计

正确模型：

~~~text
while (!predicate)
    cond.wait(lock)
~~~

Asio 只是把 predicate 从单变量扩展成：

~~~text
op_queue / stopped / event state
~~~

但原理完全相同。

---

## 107. Event Signal 不应该替代 Predicate

错误：

~~~text
收到 signal
→ 我一定能 pop
~~~

正确：

~~~text
收到 signal
→ acquire lock
→ re-check queue/stopped
~~~

这使 spurious wake 或合并通知都安全。

---

## 108. Reactor Interrupter 也只是 Hint

`epoll_wait()` 因 interrupter 返回：

~~~text
不代表一定有 user handler
~~~

可能是：

- timer deadline 改了；
- stop；
- Scheduler新 work；
- registration state 变化。

Reactor返回后再：

~~~text
重新进入 Scheduler
~~~

读取权威状态。

---

## 109. 这形成“Hint → Authoritative State”通用结构

~~~text
event / signal / interrupter
        |
        v
wake execution context
        |
        v
re-read authoritative queues/state
        |
        v
decide actual work
~~~

这是任何高可靠 Event Runtime 都应该有的模型。

---

## 110. 最终完整时序：外部 Thread Post 一个 Handler

~~~text
Producer Thread
    |
    | work_started()
    v
lock Scheduler
    |
    | op_queue.push(H)
    v
wake_one_thread_and_unlock
    |
    +-------------------------------+
    |                               |
Scheduler waiter exists       no Scheduler waiter
    |                               |
    v                               v
signal one pthread-cond      task_interrupted_ ?
    |                               |
    v                          +----+----+
Worker wakes                  |         |
    |                        yes        no
    |                         |         |
    |                         |         v
    |                         |    task_->interrupt()
    |                         |         |
    |                         |         v
    |                         |     EPOLL_CTL_MOD
    |                         |         |
    |                         |         v
    |                         |     epoll_wait returns
    |                         |         |
    +-------------------------+---------+
                              |
                              v
                      Scheduler sees H
                              |
                              v
                         complete H
~~~

---

## 111. 最终完整时序：Kernel Socket Readiness

~~~text
Socket packet arrives
        |
        v
kernel marks fd ready
        |
        v
epoll_wait returns
        |
        v
epoll_reactor::run
        |
        v
descriptor_state
into private_op_queue
        |
        v
task_cleanup
        |
        | lock Scheduler
        | splice private ops
        | requeue task_operation_
        v
Scheduler queue
        |
        v
descriptor perform
        |
        v
actual read completion
        |
        v
user handler
~~~

这里：

~~~text
不需要外部 producer signal
~~~

因为 kernel readiness 本身就是 wake source。

---

## 112. 最终完整时序：stop()

~~~text
Control Thread
    |
    v
scheduler.stop()
    |
    | lock Scheduler
    v
stopped_ = true
    |
    +----------------------------+
    |                            |
signal_all wakeup_event      task_->interrupt()
    |                            |
    v                            v
all idle workers            Reactor epoll_wait
wake                         returns
    |                            |
    +--------------+-------------+
                   |
                   v
          each loop sees stopped_
                   |
                   v
               run() exits
~~~

这才叫完整 shutdown wakeup。

---

## 113. 与前几篇 Asio 专题的关系

[Scheduler 与 Operation Queue](scheduler-operation-queue.md) 解释：

~~~text
ready operation
怎样被 worker 消费
~~~

本篇解释：

~~~text
worker 没有 work 时
怎样睡眠和被叫醒
~~~

[epoll Reactor 与 descriptor_state](epoll-reactor-descriptor-state.md) 解释：

~~~text
fd readiness 如何映射成 operation state
~~~

本篇解释：

~~~text
Scheduler 如何把执行权交给 Reactor，
Reactor blocking wait 又如何被控制面打断
~~~

[Work、Lifetime 与 Cancellation](work-lifetime-cancellation.md) 解释：

~~~text
Runtime 为什么不能因为 queue 暂时为空而退出
~~~

本篇解释：

~~~text
既然不能退出，
线程在没有 ready completion 时到底睡在哪里
~~~

[Strand](strand-serialization.md) 解释：

~~~text
多个 ready handler
怎样进一步形成 logical serial owner
~~~

这几篇合起来才构成完整 execution runtime。

---

## 114. 对 Runtime 作者最重要的八条不变量

第一：

> **Work publication 必须发生在 wake notification 之前。**

第二：

> **Wait registration 与 predicate observation 必须共享一个可证明的同步协议。**

第三：

> **不同阻塞域必须有不同的退出手段。**

第四：

> **Wakeup 是状态变化提示，不是业务状态本身。**

第五：

> **已有 ready work 时，Reactor 不应长期 blocking。**

第六：

> **只有一个执行者可以持有 Reactor task token。**

第七：

> **Reactor completion 应先本地聚合，再批量发布给 Scheduler。**

第八：

> **Shutdown 必须唤醒所有可能阻塞的执行域。**

---

## 115. 最值得迁移到机器人系统的一句话

机器人控制系统里常见：

~~~text
CAN RX
serial port
UDP
timer
command queue
state machine
~~~

真正成熟的设计不应只是：

~~~text
开几个 thread
加几个 mutex
~~~

而应该明确画出：

~~~text
每个 thread 在哪里阻塞？

谁拥有权威 queue？

哪种状态变化需要叫醒哪一层？

哪些 wake 可以合并？

shutdown 怎么保证所有 blocking point 都退出？
~~~

Asio 的 Scheduler/Reactor 唤醒协议提供了一个非常完整的参考答案。

---

## 116. 最终模型

~~~text
                    Producer
                       |
                       | publish ready op
                       v
                Scheduler op_queue
                       |
                       v
          wake_one_thread_and_unlock
                 /             \
                /               \
       idle worker exists      none
              |                  |
              v                  v
       wakeup_event         task interrupt
              |                  |
              v                  v
      Scheduler worker      epoll Reactor
          wakes               wakes
              \                  /
               \                /
                +------v-------+
                       |
                 Scheduler loop
                       |
         +-------------+-------------+
         |                           |
   normal operation             task_operation_
         |                           |
         v                           v
   user completion             reactor.run()
                                     |
                                     v
                                epoll_wait()
                                     |
                        +------------+------------+
                        |            |            |
                     socket        timerfd    interrupter
                        |            |            |
                        +------------+------------+
                                     |
                                     v
                              local op batch
                                     |
                                     v
                               task_cleanup
                                     |
                                     v
                          merge into Scheduler
                                     |
                                     v
                          requeue task_operation_
~~~

这套结构最核心的不是 epoll，也不是 condition variable。

而是：

> **把“等待什么”与“如何被唤醒”明确分层，再用队列状态、唯一执行 token 和精确 wake routing 把这些层重新连接起来。**
