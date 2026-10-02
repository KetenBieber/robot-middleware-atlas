# CRoutine 状态机与 Event Latch：DATA_WAIT、IO_WAIT、Lost Wakeup 和 Memory Order

本文固定到 Apollo Cyber RT 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。

:doc:`调度唤醒 <croutine-wakeup>` 已经解释了 DataNotifier 怎样把 channel 更新变成 `NotifyProcessor()`，以及 Processor 为什么仍要经过 `NextRoutine() → UpdateState() → Resume()` 才真正重新执行 CRoutine。这里继续追一个更底层的问题：

> **当“事件已经发生”和“协程准备进入等待”发生在不同线程时，Runtime 怎样保证不会丢掉这次唤醒？**

一个可靠的协程等待协议至少要同时处理三件事：

```text
state
  routine 当前允许怎样被调度

event memory
  某个异步事件是否已经发生但尚未消费

payload publication
  与事件对应的数据何时对 consumer 可见
```

Cyber RT 固定实现已经具备几个关键机制：

- `lock_` 防止多个 Processor 同时执行同一只 CRoutine；
- `updated_` 试图把异步更新记成一位 event latch；
- `notify_grp_ + condition_variable` 负责把睡眠中的 OS worker 叫醒；
- DataVisitor ring 保存真正的消息，event latch 不负责记录消息数量。

真正需要判断的是：这些机制在不同等待路径中，是否组成了同一个可证明的握手协议。

## 1. 先区分三种“醒”

“唤醒”在 Runtime 中至少有三层含义。

第一层是 **payload 已经存在**。例如相机帧已经写入 `CacheBuffer`。第二层是 **CRoutine 已经满足 READY 条件**。第三层才是 **承载它的 Linux worker thread 已经从 condition variable 返回并再次获得 CPU**。

三者不能互相替代：

```text
message in ring
    !=
CRoutine READY
    !=
Processor thread running
```

消息进 ring，不代表 task 已经 READY；task READY，也不代表 Processor 已经获得 CPU；`notify_one()` 只是让某个 OS worker 有机会重新扫描。

## 2. RoutineState 不是完整的当前执行状态

固定源码：

```cpp
enum class RoutineState {
  READY,
  FINISHED,
  SLEEP,
  IO_WAIT,
  DATA_WAIT
};
```

没有 `RUNNING`。RoutineFactory 在真正执行 `f(msg)` 之前就先把状态写成 `DATA_WAIT`：

```cpp
CRoutine::GetCurrentRoutine()->
    set_state(RoutineState::DATA_WAIT);

if (dv->TryFetch(msg)) {
  f(msg);
  CRoutine::Yield(RoutineState::READY);
}
```

因此 routine 明明正在 CPU 上运行用户函数时，`state_` 仍可能等于 `DATA_WAIT`。

所以 `RoutineState` 更接近：

> 下一次 Scheduler 重新评估这只 routine 时，应按什么等待规则处理。

它不是一张精确的“此刻是否正在执行”的状态表。

## 3. 真正的执行所有权由 lock_ 表达

`CRoutine` 的执行权由一只 `atomic_flag` 控制：

```cpp
std::atomic_flag lock_ = ATOMIC_FLAG_INIT;

inline bool CRoutine::Acquire() {
  return !lock_.test_and_set(
      std::memory_order_acquire);
}

inline void CRoutine::Release() {
  lock_.clear(
      std::memory_order_release);
}
```

`ClassicContext::NextRoutine()` 先 `Acquire()`，确认 `UpdateState() == READY` 后才把 CRoutine 交给 Processor。`Processor::Run()` 在 `Resume()` 返回后再 `Release()`。

这建立了一个重要不变量：

```text
同一时刻最多一个 Processor
拥有这只 CRoutine 的执行权
```

而且前一 Processor 的普通内存写，在 `Release(release)` 后，可以被后续成功 `Acquire(acquire)` 的 Processor 看见。

所以不能简单说“`state_` 不是 atomic，因此所有 state 访问都不安全”。**Processor 与 Processor 之间有 `lock_` 这一层 ownership synchronization。**

真正的问题出在外部通知线程。

## 4. NotifyProcessor 读取 state_ 时没有进入 CRoutine ownership

Classic 策略：

```cpp
if (cr->state() == RoutineState::DATA_WAIT ||
    cr->state() == RoutineState::IO_WAIT) {
  cr->SetUpdateFlag();
}

ClassicContext::Notify(cr->group_name());
```

`state()` 只是：

```cpp
inline RoutineState CRoutine::state() const {
  return state_;
}
```

通知线程没有：

- `cr->Acquire()`；
- state mutex；
- atomic `state_`；
- 或任何与 routine 写 `state_` 共用的同步点。

`id_cr_lock_` 只保护 task-id → CRoutine 的 registry，不保护 CRoutine 内部字段。

因此 routine 线程写 `state_` 与 transport/poller 线程读 `state_` 可以形成真正的 C++ data race。

## 5. updated_ 不是 READY，而是一位“尚未消费事件”

固定成员：

```cpp
std::atomic_flag updated_ = ATOMIC_FLAG_INIT;
```

构造函数先把它设为 true：

```cpp
updated_.test_and_set(std::memory_order_release);
```

通知方：

```cpp
inline void CRoutine::SetUpdateFlag() {
  updated_.clear(std::memory_order_release);
}
```

Scheduler 扫描方：

```cpp
if (!updated_.test_and_set(
        std::memory_order_release)) {
  if (state_ == RoutineState::DATA_WAIT ||
      state_ == RoutineState::IO_WAIT) {
    state_ = RoutineState::READY;
  }
}
```

所以它的实际语义是：

```text
updated_ == true
  当前没有待消费更新

updated_ == false
  至少发生过一次更新，尚未消费
```

变量名很容易让人反着理解。

## 6. 一位 latch 为什么允许多个事件合并

假设相机连续到达 A、B、C 三帧。

`updated_` 只有一位，三次 `clear()` 仍然只是 false。这并不天然错误，因为真实消息数量保存在 ring：

```text
CacheBuffer:
  A
  B
  C

updated_:
  “至少发生过一次变化”
```

也就是说：

> payload 保存业务事实；event latch 只保存“需要重新检查”的义务。

这是一种很常见的 Runtime 设计。

## 7. notify_grp_ 又是另一层 event memory

ClassicContext 的 worker 唤醒不是只调用 `notify_one()`，而是先记录计数：

```cpp
mtx_wq_[group_name].Mutex().lock();
notify_grp_[group_name]++;
mtx_wq_[group_name].Mutex().unlock();

cv_wq_[group_name].Cv().notify_one();
```

等待侧：

```cpp
std::unique_lock<std::mutex> lk(
    mtx_wrapper_->Mutex());

cw_->Cv().wait_for(
    lk,
    std::chrono::milliseconds(1000),
    [&]() {
      return notify_grp_[current_grp] > 0;
    });

if (notify_grp_[current_grp] > 0) {
  notify_grp_[current_grp]--;
}
```

于是 Cyber 有两级 latch：

```text
task-level:
  updated_

worker-group-level:
  notify_grp_
```

前者决定具体 CRoutine 是否需要重新评估，后者只让某个 Processor worker 再扫描一次。

## 8. group wakeup 不能替代 task wakeup

假设：

```text
CRoutine.state = DATA_WAIT
updated_       = no pending event
notify_grp     = 1
```

Processor 会经历：

```text
NextRoutine:
  state still DATA_WAIT
  -> not READY

Wait:
  consume notify_grp=1
  -> immediately return

NextRoutine:
  still DATA_WAIT

Wait:
  now really sleeps
```

所以 `notify_grp_` 只能表示“再扫描一次”，不能替代 `updated_` 把某只等待态 CRoutine 变成 READY。

# DataVisitor：为什么它比较接近正确的等待握手

## 9. RoutineFactory 先宣布 DATA_WAIT，再检查 ring

固定源码：

```cpp
for (;;) {
  CRoutine::GetCurrentRoutine()->
      set_state(RoutineState::DATA_WAIT);

  if (dv->TryFetch(msg)) {
    f(msg);
    CRoutine::Yield(RoutineState::READY);
  } else {
    CRoutine::Yield();
  }
}
```

顺序是：

```text
prepare DATA_WAIT
        |
        v
check real queue
        |
        +-- data exists --> process --> Yield READY
        |
        +-- empty -------> Yield, keep DATA_WAIT
```

这是典型的 **prepare-to-wait before final condition check**。

## 10. 为什么这个顺序重要

错误顺序是：

```text
check queue -> empty
producer writes message + notify
consumer sets DATA_WAIT
consumer yields
```

事件发生在最终检查与真正睡眠之间，就会产生 lost wakeup。

DataVisitor 先写 `DATA_WAIT`，再读 ring，正是为了缩小这个窗口。

## 11. 更早到达的事件还能被 payload 本身兜住

假设 producer 在 routine 写 `DATA_WAIT` 之前就写入消息。

通知线程可能看到旧状态 `READY`，于是没有 `SetUpdateFlag()`。但消息已经留在 `CacheBuffer`。

routine 随后：

```text
state = DATA_WAIT
TryFetch()
```

仍然会看到 payload。

所以 DataVisitor 有两层信息来源：

```text
persistent payload
+
event hint
```

event hint 偶尔没留下，并不一定立刻等价于丢消息。

## 12. 但 state_ 的跨线程 data race 让这套证明不完整

理想推理会说：

```text
producer 要么看到 READY
要么看到 DATA_WAIT
```

但固定源码里 `state_` 是普通枚举，routine 写与通知线程读没有共同同步。

一旦读写真正重叠，程序就进入 C++ data race / undefined behavior 范畴。

因此 DataVisitor 的等待顺序是合理的，但 `state_` 的跨线程 publication 机制并不完整。

# TaskManager：更直接的 check-before-sleep

## 13. Consumer 先 Dequeue，再 HangUp

固定 `TaskManager` worker：

```cpp
while (!stop_) {
  std::function<void()> task;

  if (!task_queue_->Dequeue(&task)) {
    auto routine =
        croutine::CRoutine::
            GetCurrentRoutine();

    routine->HangUp();
    continue;
  }

  task();
}
```

而 `HangUp()`：

```cpp
inline void CRoutine::HangUp() {
  CRoutine::Yield(RoutineState::DATA_WAIT);
}
```

顺序变成：

```text
check queue
  empty
        |
        v
later state = DATA_WAIT
        |
        v
Yield
```

它与 DataVisitor 恰好相反。

## 14. Producer 先 Enqueue，再 NotifyTask

固定生产侧：

```cpp
task_queue_->Enqueue(
    [task]() { (*task)(); });

for (auto& task : tasks_) {
  scheduler::Instance()->
      NotifyTask(task);
}
```

`Enqueue -> Notify` 本身是正确方向。

问题在 consumer 尚未真正写入等待态时。

## 15. 一条完整 lost-wakeup 交错

假设 worker：

```text
Dequeue()
  -> false

// 此时还没有 HangUp
```

producer 恰好：

```text
Enqueue(task X)
NotifyTask(worker)
```

Scheduler 看到当前状态仍像 `READY`，因此：

```text
does not clear updated_
but increments notify_grp_
```

随后 worker：

```text
HangUp()
  state = DATA_WAIT
  Yield
```

此时：

```text
queue      = contains task X
state      = DATA_WAIT
updated_   = no pending event
notify_grp = 1
```

Processor 第一次扫描发现 task 不 READY；`Wait()` 消耗那一个 group notification 后立即返回；第二次扫描仍不 READY；随后真正睡下。

结果可能是：

> queue 明明非空，但 CRoutine 停在 DATA_WAIT，直到未来另一个 NotifyTask 偶然到达。

这就是协议层 lost wakeup，不只是 data race。

## 16. DataVisitor 与 TaskManager 的一行差别

```text
DataVisitor:
  announce wait
  -> check condition
  -> sleep

TaskManager:
  check condition
  -> announce wait
  -> sleep
```

并发等待协议里，这一行顺序差异非常关键。

# PollHandler：EPOLLONESHOT 下的 Register→Yield 窗口

## 17. Block 先 Register，再 Yield(IO_WAIT)

固定代码：

```cpp
Fill(timeout_ms, is_read);

if (!Poller::Instance()->
        Register(request_)) {
  is_blocking_.store(false);
  return false;
}

routine_->Yield(RoutineState::IO_WAIT);
```

也就是：

```text
arm epoll
    |
    v
event may complete
    |
    v
later state = IO_WAIT
    |
    v
Yield
```

## 18. ResponseCallback 只在已经看到 IO_WAIT 时通知

固定代码：

```cpp
response_ = rsp;

if (routine_->state() ==
    RoutineState::IO_WAIT) {
  scheduler::Instance()->
      NotifyTask(routine_->id());
}
```

如果 epoll event 在 `Yield(IO_WAIT)` 之前到达：

```text
Poller:
  response_ = rsp
  sees state == READY
  no NotifyTask

Routine:
  later Yield(IO_WAIT)
```

这又是一条“事件先发生，等待态后提交”的窗口。

## 19. EPOLLONESHOT 使问题更明显

`Fill()` 设置：

```cpp
request_.events =
    EPOLLET | EPOLLONESHOT;
```

Poller 交付 response 后还会：

```cpp
search->second->timeout_ms = -1;
search->second->callback(response);
```

一次 one-shot readiness 已经被消费，只有下一次重新 Register/MOD 才会 re-arm。

但下一次 Register 必须等当前 routine 恢复以后才能执行。

因此如果这次 callback 没有留下 task-level event：

```text
event delivered
routine later IO_WAIT
updated_ no pending event
timeout disabled
```

就没有显然的后续机制自动把它重新变 READY。

这不能依赖“fd 反正还 ready，epoll 下次还会告诉我”来修复，因为 `EPOLLONESHOT` 正是要求应用重新 arm。

# updated_ 的 memory order：原子不等于 payload publication

## 20. producer 和 consumer 两端都用了 release

producer：

```cpp
updated_.clear(
    std::memory_order_release);
```

consumer：

```cpp
updated_.test_and_set(
    std::memory_order_release);
```

这保证 atomic flag 自身操作是原子的，但 `memory_order_release` 不具备 acquire 语义。

## 21. 发布其他普通内存需要 happens-before

经典发布：

```text
producer:
  write payload
  flag.store(true, release)

consumer:
  flag.load(acquire)
  read payload
```

只有 consumer 的 acquire 观察到 producer 的 release publication，才能用这只 atomic 为其他普通数据建立 happens-before。

固定 `test_and_set(memory_order_release)` 不提供这个 acquire 部分。

## 22. DataVisitor 为什么仍有独立 payload synchronization

DataDispatcher 写 ring 时：

```text
lock CacheBuffer mutex
Fill(payload)
unlock
```

DataVisitor 读取：

```text
lock same CacheBuffer mutex
Fetch(payload)
```

因此消息内容的 publication 由 `CacheBuffer` mutex 保证。

`updated_` 只负责“请重新检查”，不承担消息内存可见性。

这是职责分离正确的一面。

## 23. PollHandler 的 response_ 没有同类 mutex

成员：

```cpp
PollResponse response_;
std::atomic<bool> is_read_;
std::atomic<bool> is_blocking_;
CRoutine* routine_;
```

Poller thread 写：

```cpp
response_ = rsp;
```

routine 恢复后读：

```cpp
if (response_.events &
    target_events) {
  result = true;
}
```

`response_` 不是 atomic，也没有自己的 mutex。

正常情况下，如果 Processor 恰好睡在 `ClassicContext::Wait()`，group mutex 的释放/重获可以提供额外同步。但 Processor 不一定正在 Wait，它可能正在扫描其他 task。

因此如果 Runtime 希望依靠 task event latch 发布 `response_`，consumer 侧就需要一个真正的 acquire edge。

## 24. 一个 event latch 若还发布 event data，需要 atomicity + ordering

理想结构：

```text
Poller:
  response_ = rsp
  pending IO event (release)
        |
        v
Processor:
  consume IO event (acquire)
  state -> READY
  Resume
        |
        v
routine:
  read response_
```

这时才有清晰的 C++ 内存模型证明。

# NotifyProcessor 为什么最好不要先读 state_

## 25. 当前协议让 producer 参与 consumer 状态机

通知线程必须先判断：

```text
consumer 已经在 WAIT 吗？
```

于是 producer 与 consumer 共同操作一套 phase：

```text
routine/Processor:
  writes state_

transport/poller:
  reads state_
  decides whether event counts
```

复杂性由此出现。

## 26. 更简单的协议：事件到达就无条件记 pending

更容易证明的语义：

```cpp
void NotifyProcessor(...) {
  cr->SignalEvent();
  NotifyGroup(...);
}
```

producer 不读 `state_`，只表达：

> “某件事变化了，请重新检查。”

是否从 WAIT 转成 READY，由拥有 CRoutine execution ownership 的 Scheduler 决定。

## 27. stale event 最坏只是一次 spurious recheck

事件在 routine 正忙时到达，也可以先留下 pending bit。

如果对应 work 后来已经被消费，Scheduler 可能多唤醒一次：

```text
wake
-> re-check queue
-> empty
-> sleep again
```

这只是 spurious wakeup。

相比 lost wakeup 导致 work 永久卡住，这种 false positive 通常更容易接受。

# 把 state transition 收回 CRoutine ownership 域

## 28. producer 只写 PendingEvents

理想 ownership：

```text
external producer threads:
  may update pending_events

Processor owning CRoutine:
  may read/write state_
  may read/write wake_time_
  may advance coroutine context
```

这样 `state_` 甚至可以继续是普通字段，因为它只在 `lock_` 串行化的执行域中访问。

## 29. UpdateState 成为 WAIT→READY 的统一入口

教学骨架：

```cpp
RoutineState CRoutine::UpdateState() {
  if (state_ == SLEEP &&
      deadline_reached()) {
    state_ = READY;
  }

  const bool event =
      pending_event_.exchange(
          false,
          std::memory_order_acquire);

  if (event &&
      (state_ == DATA_WAIT ||
       state_ == IO_WAIT)) {
    state_ = READY;
  }

  return state_;
}
```

重点不是 API 名字，而是：

```text
producer records event
consumer owns state transition
```

## 30. 正向 pending bool 比反向 atomic_flag 更容易理解与验证

当前语义：

```text
clear = pending
set   = empty
```

更直观的形式：

```cpp
std::atomic<bool>
    event_pending{false};

// producer
event_pending.store(
    true,
    std::memory_order_release);

// consumer
bool event =
    event_pending.exchange(
        false,
        std::memory_order_acquire);
```

变量值直接对应含义。

## 31. 如果事件原因不同，可以用 bitmask

例如：

```cpp
enum EventBits {
  DATA_EVENT = 1 << 0,
  IO_EVENT   = 1 << 1,
  TASK_EVENT = 1 << 2,
  STOP_EVENT = 1 << 3
};
```

producer：

```cpp
pending_events_.fetch_or(
    IO_EVENT,
    std::memory_order_release);
```

consumer：

```cpp
auto events =
    pending_events_.exchange(
        0,
        std::memory_order_acquire);
```

仍然保持 producer 记录 cause、consumer 推进 state。

# 三条等待路径应该怎样修

## 32. DataVisitor

它已经有正确的 prepare-to-wait 骨架：

```text
state = DATA_WAIT
-> final TryFetch
-> Yield
```

需要补的是：

- producer 不再跨线程读普通 `state_`；
- pending event 使用清晰 release/acquire；
- Scheduler 在 ownership 下消费 event；
- payload 继续由 ring mutex 发布。

## 33. TaskManager

必须关闭：

```text
Dequeue empty
<--- event may arrive here --->
HangUp
```

典型做法是：

```text
prepare wait
-> final queue check
-> commit sleep
```

或使用 sequence/futex/parking-lot 类协议。

更简单的 producer 侧仍然应该是：**Enqueue 后无条件记录 pending event**。

## 34. PollHandler

I/O completion 不应先问“routine 已经 IO_WAIT 吗”。

更稳健的 callback：

```text
write response
-> publish IO event
-> notify scheduler group
```

事件即使比 `Yield(IO_WAIT)` 更早，也会留下事实。

如果 I/O runtime 更复杂，还可以用 generation/token：

```text
Block generation 42
arm fd with generation 42

Poller completes generation 42

Routine only consumes completion 42
```

这样 timeout、re-arm、unregister、close 和旧 callback 都更容易证明。

# SLEEP 与外部事件不同

## 35. SLEEP 由 deadline 重新计算

固定 `UpdateState()`：

```cpp
if (state_ == RoutineState::SLEEP &&
    std::chrono::steady_clock::now()
        > wake_time_) {
  state_ = RoutineState::READY;
  return state_;
}
```

它不依赖外部 producer；真实 condition 是“deadline 是否已经过去”。

`wake_time_` 在 routine 执行域写入，未来 Processor 通过 `lock_` acquire 后读取，因此它与外部通知线程直接读 `state_` 的边界不同。

# force_stop_ 也暴露同类跨域状态

## 36. Stop 写普通 bool，Resume 读普通 bool

固定成员：

```cpp
bool force_stop_ = false;
```

写：

```cpp
void CRoutine::Stop() {
  force_stop_ = true;
}
```

读：

```cpp
if (cyber_unlikely(force_stop_)) {
  state_ = RoutineState::FINISHED;
  return state_;
}
```

## 37. Stop 可以与已经 Acquire 的 Processor 并发

一种交错：

```text
Processor:
  Acquire(cr)
  return from NextRoutine

remove thread:
  Stop()
  force_stop_ = true

Processor:
  Resume()
  read force_stop_
```

`force_stop_` 既不是 atomic，也没有共同 mutex。

后面 `RemoveCRoutine()` 虽然会等待 `Acquire()` 成功再 erase，但那个 quiescence barrier 发生在 `Stop()` 写之后，不能追溯性地修复前面的普通读写 race。

所以 STOP 也适合被纳入统一 control event，或至少使用明确 atomic/synchronization。

# 一个统一的 CRoutine runtime state 模型

## 38. 把三类信息拆开

```text
SchedulingState:
  READY
  DATA_WAIT
  IO_WAIT
  SLEEP
  FINISHED

PendingEvents:
  DATA
  IO
  TASK
  STOP

ExecutionOwnership:
  free
  owned by Processor
```

分别回答：

```text
state:
  consumer 如何被调度

events:
  producer 已经发生什么

ownership:
  谁能修改 consumer-local state
```

## 39. 外部线程只操作 PendingEvents

教学结构：

```cpp
void Signal(Event e) {
  pending_events_.fetch_or(
      e,
      std::memory_order_release);

  scheduler_group_->Notify();
}
```

不读 `state_`，也不直接写 READY。

## 40. Scheduler 在 Acquire 后消费 event

```cpp
if (!cr->Acquire()) {
  continue;
}

auto events = cr->TakeEvents();
cr->AdvanceState(events);

if (cr->state() == READY) {
  return cr;
}

cr->Release();
```

这样所有 `SchedulingState` 迁移都收回一个 ownership domain。

## 41. event 仍然不是业务事实

收到 `DATA_EVENT` 不代表 queue 现在一定非空；收到 `IO_EVENT` 也不代表系统调用一定成功。

所以 routine 恢复后仍要重新检查：

- ring；
- task queue；
- fd readiness；
- timeout；
- shutdown predicate。

event 只是 re-evaluation signal。

# Memory order 应围绕真实数据流设计

## 42. 先画 happens-before，再选 memory_order

应该先回答：

```text
producer 写了哪些普通数据？

consumer 在哪里读？

哪一个 atomic 或 mutex
连接这两端？
```

而不是先问“这里用 acquire 还是 relaxed”。

## 43. Data path

```text
producer:
  CacheBuffer mutex
  Fill(payload)
  unlock
        |
        v
consumer:
  lock same mutex
  Fetch(payload)
```

所以 payload visibility 由 mutex 负责。

## 44. PollResponse path

如果不用 mutex：

```text
Poller:
  response_ = rsp
  pending.store(IO_EVENT, release)
        |
        v
Scheduler:
  pending.exchange(0, acquire)
  READY
  Resume
        |
        v
routine:
  read response_
```

这才是完整的 publication chain。

## 45. atomic modification order 不等于其他字段的 happens-before

即使所有线程都一致地观察 `updated_` 的 false→true 顺序，也不能自动同步 `response_.events` 或其他普通字段。

atomic 对象自己的 modification order 与其他内存的 visibility 是不同层次。

# 三条现有等待路径的对照

## 46. DataVisitor

真实 condition：

```text
CacheBuffer / fusion ring
```

优点：

```text
prepare DATA_WAIT before TryFetch
```

主要缺口：

```text
NotifyProcessor reads state_
outside CRoutine ownership
```

## 47. TaskManager

真实 condition：

```text
BoundedQueue<function<void()>>
```

producer：

```text
Enqueue -> NotifyTask
```

缺口：

```text
Dequeue empty -> later HangUp
```

存在 check-before-sleep。

## 48. PollHandler

真实 event data：

```text
PollResponse response_
```

缺口：

```text
Register -> later Yield(IO_WAIT)

ResponseCallback only notifies
after observing IO_WAIT

updated_ consumer has no acquire edge
```

这是三条路径中握手要求最强的一条。

# 为什么机器人 Runtime 特别怕 lost wakeup

## 49. 这类故障往往“数据明明到了，任务就是不跑”

在机器人链路中可能表现为：

```text
camera frame arrived
but perception routine sleeps

socket ready
but control session stalls

async task queued
but worker remains parked
```

典型特点：

- 低概率；
- 对时序敏感；
- 压测才出现；
- 加日志以后可能消失；
- 很难靠普通功能测试稳定复现。

## 50. 一秒 wait_for 不是 task 正确性的兜底

ClassicContext 的 `wait_for(..., 1000ms)` 只会让 OS worker 每秒重新扫描。

如果 task 仍是：

```text
DATA_WAIT
+
no pending event
```

再扫描也不会自动 READY。

timeout 不能修复 task-level event 丢失。

对视觉伺服、底盘控制、避障、融合和控制网络而言，1 秒也远超合理 latency budget。

# 一个通用源码检查表

## 51. 每个等待点问五个问题

```text
1. real condition 在哪里？
   queue / ring / fd / deadline

2. waiter 何时宣布 prepare-to-wait？

3. producer 在哪里记录 event？

4. event 比 sleep 更早发生时，
   是否会留下持久状态？

5. wake 后是否重新检查 real condition？
```

## 52. 每个跨线程 payload 再问三问

```text
1. producer 写普通数据在哪里？

2. consumer 读普通数据在哪里？

3. 哪个 release/acquire 或 mutex
   建立 happens-before？
```

“中间有 atomic”不是完整答案。

# 最终把 Cyber 的四层状态固定下来

## 53. Payload state

```text
CacheBuffer
BoundedQueue
PollResponse
```

保存真实业务条件。

## 54. Task event state

```text
updated_
```

它的目标语义应该是：

```text
this CRoutine needs re-evaluation
```

而不应该依赖 producer 正确猜测当前 RoutineState。

## 55. Worker wake state

```text
notify_grp_
+
condition_variable
```

表示：

```text
some worker in this scheduling group
should scan again
```

它不指定具体 task。

## 56. Execution ownership

```text
lock_
```

表示：

```text
only one Processor may
advance this CRoutine
```

最清晰的状态机应该让 SchedulingState 只在这个 ownership 域中迁移。

# 固定源码的边界总结

## 57. 已经具备的正确机制

```text
lock_
  prevents multi-Processor reentry

CacheBuffer mutex
  publishes message payload

notify_grp + condition_variable
  remembers worker-level wake

updated_ atomic flag
  coalesces task-level event

DataVisitor ordering
  prepares DATA_WAIT before TryFetch
```

这些都是正确方向。

## 58. 仍未统一的并发契约

```text
NotifyProcessor
  reads non-atomic state_
  outside CRoutine ownership

TaskManager
  Dequeue empty
  then HangUp

PollHandler
  Register
  then Yield(IO_WAIT)

ResponseCallback
  only NotifyTask when
  it already observes IO_WAIT

updated_
  producer release
  consumer release
  no acquire publication edge

force_stop_
  plain bool crosses
  remove / Resume domains
```

所以问题不是“Cyber 没有 event mechanism”。

真正的问题是：

> 已经存在的 event latch、worker counter、execution lock 和 persistent payload，还没有被所有等待路径统一成同一个可证明的状态机协议。

# 从零实现时的推荐骨架

## 59. Producer

```cpp
void CRoutine::Signal(
    uint32_t events) {

  pending_events_.fetch_or(
      events,
      std::memory_order_release);

  scheduler_group_->Notify();
}
```

producer 不读 `RoutineState`。

## 60. Scheduler / consumer

```cpp
RoutineState
CRoutine::UpdateState() {

  const auto events =
      pending_events_.exchange(
          0,
          std::memory_order_acquire);

  if (events & STOP_EVENT) {
    state_ = RoutineState::FINISHED;
    return state_;
  }

  if ((events & DATA_EVENT) &&
      state_ == RoutineState::DATA_WAIT) {
    state_ = RoutineState::READY;
  }

  if ((events & IO_EVENT) &&
      state_ == RoutineState::IO_WAIT) {
    state_ = RoutineState::READY;
  }

  if (state_ == RoutineState::SLEEP &&
      DeadlineReached()) {
    state_ = RoutineState::READY;
  }

  return state_;
}
```

这是教学骨架，不是 Apollo 源码。

核心原则是：

```text
events cross threads
state transitions stay local
```

## 61. Wait protocol

所有 waiter 都应该满足：

```text
prepare wait
-> final condition check
-> commit sleep
```

或者用 generation、futex、parking-lot 等协议实现等价握手。

不能：

```text
check condition
-> later announce waiting
```

# 九条可迁移原则

```text
1. state is not event memory

2. event memory is not payload

3. worker wakeup is not task readiness

4. execution ownership should own
   state transitions

5. producer should record events,
   not guess consumer phase

6. prepare-to-wait must happen
   before the final condition check

7. release/acquire must follow
   the real payload publication path

8. spurious wakeup is often acceptable;
   lost wakeup usually is not

9. atomic state can remove a data race,
   but cannot repair a broken handshake
```

对于机器人 Runtime，可靠唤醒的核心不是“看到 WAIT 就 notify”。

而是：

> **事件无论发生在等待之前、等待过程中还是等待之后，都必须留下一个可被下一次状态评估观察到的事实；等待者恢复以后，再重新检查真实 condition。**

这才是从 `DATA_WAIT / IO_WAIT` 这些枚举值走向可证明并发状态机的关键。
