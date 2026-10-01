# SchedulingTerm 与 Event Wakeup：资源状态如何真正变成 READY Entity

Holoscan SDK 行为基线：v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

公开 GXF 源码基线：v3.2-1 `daf1810358301f642374dfb3d725be349bba5ec0`。

> Holoscan v4.6.0 与公开 GXF v3.2-1 不是同一个 backend 快照。这里用 Holoscan v4.6 的 Condition/Operator API 说明上层语义，用公开 GXF v3.2-1 的完整源码解释 SchedulingTerm、typed event、Dispatcher 与 EntityExecutor 之间的控制链；不把旧 GXF 的具体队列实现冒充成 v4.6 内部实现。

前面的几篇文章已经分别解释了 :doc:`Condition、Connector 与 Backpressure <conditions-connectors-backpressure>`、:doc:`GXF EventBasedScheduler 内部实现 <gxf-event-runtime-internals>` 和 :doc:`EntityExecutor 与 MessageRouter <gxf-entity-executor-router>`。真正容易断掉的一环是：

> 队列里突然来了一条消息、下游腾出了一个 slot、allocator 释放了一块内存、目标时间被修改，或者外部线程完成了异步工作以后，Scheduler 到底怎样知道“现在值得重新判断这个 Entity”？

答案不是“事件把 Entity 直接改成 READY”，而是：

~~~text
resource state changes
        |
        | typed event / notification
        v
Scheduler is asked to re-check
        |
        v
SchedulingTerm::check()
        |
        +--> update_state_abi()
        +--> check_abi()
        |
        v
EntityExecutor combines all terms
        |
        v
READY / WAIT / WAIT_TIME / WAIT_EVENT / NEVER
        |
        v
EventBasedScheduler::updateCondition()
        |
        v
ready queue / timer / waiting set
~~~

## 1. 第一性原理：状态与通知必须分开

假设 Consumer 只有一条条件：

~~~text
receiver has message
→ runnable
~~~

最粗糙的实现可以让 Producer 到消息时直接写：

~~~cpp
consumer.ready = true;
scheduler.enqueue(consumer);
~~~

但真实 Operator 通常还同时受约束：

~~~text
input available
AND downstream has free slot
AND GPU memory available
AND period has elapsed
AND control state permits
~~~

所以“消息到达”不等于“整个 Entity READY”。

runtime 必须区分两种信息：

~~~text
authoritative state:
  当前资源到底满足不满足执行条件

notification:
  某个可能影响状态的事实刚刚变化，
  值得 Scheduler 重新检查
~~~

GXF 的 event 主要承担第二种职责。它告诉 Scheduler：

> 这个 Entity 的世界可能变了，请重新计算 readiness。

而不是：

> 这个 Entity 一定 READY，直接执行。

## 2. SchedulingTerm 才是 readiness 的真值来源

公开 GXF 的 `SchedulingTerm` 把一次检查拆成两个阶段：

~~~cpp
virtual gxf_result_t update_state_abi(
    int64_t timestamp) {
  return GXF_SUCCESS;
}

virtual gxf_result_t check_abi(
    int64_t timestamp,
    SchedulingConditionType* type,
    int64_t* target_timestamp) const = 0;

Expected<SchedulingCondition> check(
    int64_t timestamp) {
  SchedulingConditionType status;
  int64_t target_timestamp = 0;

  gxf_result_t result =
      update_state_abi(timestamp);

  if (result != GXF_SUCCESS) {
    return Unexpected{result};
  }

  const gxf_result_t error =
      check_abi(
          timestamp,
          &status,
          &target_timestamp);

  return ExpectedOrCode(
      error,
      SchedulingCondition{
          status,
          target_timestamp});
}
~~~

这一段决定了整个 event-driven 协议的性质。

调用者看起来只是：

~~~cpp
term->check(now);
~~~

实际执行：

~~~text
重新观察底层资源
→ 更新 term 内部状态
→ 返回当前 SchedulingCondition
~~~

所以事件只需要把 Entity 送回“检查路径”，不需要让每一种事件自己理解完整的 scheduling logic。

## 3. MessageAvailable：通知后重新读 Receiver，而不是相信旧结论

`MessageAvailableSchedulingTerm` 初始化为 WAIT：

~~~cpp
gxf_result_t
MessageAvailableSchedulingTerm::initialize() {
  current_state_ =
      SchedulingConditionType::WAIT;
  last_state_change_ = 0;
  return GXF_SUCCESS;
}
~~~

真正决定状态的是：

~~~cpp
gxf_result_t
MessageAvailableSchedulingTerm::update_state_abi(
    int64_t timestamp) {
  const bool is_ready =
      checkMinSize() &&
      checkFrontStageMaxSize();

  if (is_ready &&
      current_state_ !=
          SchedulingConditionType::READY) {
    current_state_ =
        SchedulingConditionType::READY;
    last_state_change_ = timestamp;
  }

  if (!is_ready &&
      current_state_ !=
          SchedulingConditionType::WAIT) {
    current_state_ =
        SchedulingConditionType::WAIT;
    last_state_change_ = timestamp;
  }

  return GXF_SUCCESS;
}
~~~

而 `checkMinSize()` 直接读 Receiver：

~~~cpp
bool MessageAvailableSchedulingTerm::checkMinSize()
    const {
  return receiver_->back_size() +
             receiver_->size()
         >= min_size_;
}
~~~

因此真实逻辑是：

~~~text
message arrival
→ notification
→ term->check()
→ update_state_abi()
→ read current queue state
→ READY / WAIT
~~~

通知不携带“现在有几条消息”。Receiver 才是真值。

## 4. 为什么 event 只做 invalidation hint 更稳健

考虑：

~~~text
t0: message arrives
t1: notification emitted
t2: other state changes
t3: dispatcher finally re-checks
~~~

如果 notification 自身等价于一个 READY token，t3 可能执行一个过期结论。

GXF 的模式是：

~~~text
event
→ trigger re-evaluation
→ read current state
~~~

这和 condition_variable 的经典规则很像：

~~~text
notify
不是 predicate

wake
以后仍然重新检查 predicate
~~~

因此可以把 event 理解成：

> invalidation / wakeup hint。

SchedulingTerm 才是 authoritative state。

## 5. 消息到达事件真正由 MessageRouter 发出

`DoubleBufferReceiver::push_abi()` 只负责写 queue：

~~~cpp
if (!queue_->push(
        std::move(maybe.value()))) {
  return GXF_EXCEEDING_PREALLOCATED_SIZE;
}
return GXF_SUCCESS;
~~~

它没有直接操作 Scheduler。

真正的数据流边界在 `MessageRouter::syncOutbox()`。消息 distribute 给 receiver 后：

~~~cpp
while (tx->size() > 0) {
  Entity message =
      UNWRAP_OR_RETURN(tx->pop());

  RETURN_IF_ERROR(
      distribute(
          tx,
          message,
          receivers));
}

for (const Handle<Receiver>& receiver :
     receivers) {
  GxfEntityNotifyEventType(
      entity.context(),
      receiver->eid(),
      GXF_EVENT_MESSAGE_SYNC);
}
~~~

于是一次输出同时走两条路径：

~~~text
data plane:
Transmitter
→ Receiver queue

control plane:
GXF_EVENT_MESSAGE_SYNC
→ downstream Entity re-check
~~~

这是 streaming runtime 中非常关键的双平面设计。

## 6. 为什么必须先 push，再 notify

源码顺序是：

~~~text
publish new state
→ send notification
~~~

而不是：

~~~text
notify
→ later publish state
~~~

原因是 Dispatcher 醒来后马上会重新读取 queue。如果先通知，可能出现：

~~~text
dispatcher wakes
→ sees old empty queue
→ classifies WAIT
→ message arrives afterwards
~~~

若没有第二个事件，就会形成经典 lost wakeup。

所以一般原则是：

> 先使 predicate 对其他线程可观察，再发通知。

## 7. Backpressure 解除需要反向 notification

消息到达唤醒下游 Consumer；但上游 Producer 可能因为 downstream queue 满而 WAIT。

Consumer 取走消息后，`Receiver::receive()` 会反向通知连接的 transmitter：

~~~cpp
Expected<Entity> Receiver::receive() {
  gxf_uid_t uid;
  const gxf_result_t code =
      receive_abi(&uid);

  if (code == GXF_SUCCESS) {
    for (
      const Handle<Transmitter> transmitter :
      connected_transmitters_) {

      GxfEntityNotifyEventType(
          context(),
          transmitter->eid(),
          GXF_EVENT_MESSAGE_SYNC);
    }

    return Entity::Own(context(), uid);
  }

  return Unexpected{code};
}
~~~

所以 backpressure 控制方向与数据方向相反：

~~~text
data:
Producer  ------> Consumer

capacity wakeup:
Producer <------ Consumer
~~~

完整闭环：

~~~text
downstream fills
→ Producer condition becomes WAIT
→ Consumer drains one item
→ Receiver gains capacity
→ notify upstream transmitter Entity
→ DownstreamReceptive re-checks
→ Producer may become READY
~~~

## 8. DownstreamReceptive 重新算容量，不相信 MESSAGE_SYNC

事件只说明 queue relationship 发生了变化。

真正的容量判断仍在：

~~~cpp
for (const Handle<Receiver> receiver :
     receivers_) {
  const uint64_t required =
      receiver->back_size() +
      min_size_;

  const uint64_t available =
      receiver->capacity() -
      receiver->size();

  is_ready &=
      required <= available;
}
~~~

因此：

~~~text
GXF_EVENT_MESSAGE_SYNC
!=
Producer READY
~~~

更准确的是：

~~~text
GXF_EVENT_MESSAGE_SYNC
=
queue-related state changed,
please recompute
~~~

## 9. allocator free 也会成为 scheduling wakeup source

公开 GXF `Allocator::free()`：

~~~cpp
Expected<void> Allocator::free(byte* pointer) {
  Expected<void> result =
      ExpectedOrCode(
          free_abi(
              static_cast<void*>(pointer)));

  GxfEntityNotifyEventType(
      context(),
      eid(),
      GXF_EVENT_MEMORY_FREE);

  return result;
}
~~~

`MemoryAvailableSchedulingTerm` 再重新读 allocator：

~~~cpp
const bool is_ready =
    allocator_->is_available(min_bytes_);

if (is_ready &&
    current_state_ !=
        SchedulingConditionType::READY) {
  current_state_ =
      SchedulingConditionType::READY;
}
~~~

因此“释放一块内存”不只是 memory-management action，它还可能触发另一个执行单元重新变得 runnable。

## 10. 时间目标变化为什么也需要事件

`TargetTimeSchedulingTerm::setNextTargetTime()` 会：

~~~cpp
target_timestamp_ = target_timestamp;

GxfEntityNotifyEventType(
    context(),
    eid(),
    GXF_EVENT_TIME_UPDATE);
~~~

假设 Scheduler 原本准备等到 100 ms，再有人把 target 改成 20 ms。

如果没有 TIME_UPDATE，旧 timer 仍可能让 Entity 晚很多才被重新考虑。

所以 event source 不只有消息，还包括：

~~~text
message state
memory state
time state
control state
external async completion
~~~

## 11. BooleanSchedulingTerm：控制状态也进入同一协议

`enable_tick()`：

~~~cpp
auto retval =
    enable_tick_.set(true);

GxfEntityNotifyEventType(
    context(),
    eid(),
    GXF_EVENT_STATE_UPDATE);
~~~

`disable_tick()` 同样发 STATE_UPDATE。

于是外部控制路径可以修改 predicate，然后让 Scheduler 重新检查，而不需要 Scheduler 周期轮询一个 bool。

## 12. event 描述“原因”，SchedulingCondition 描述“结果”

公开 GXF 事件类型：

~~~cpp
typedef enum {
  GXF_EVENT_CUSTOM = 0,
  GXF_EVENT_EXTERNAL = 1,
  GXF_EVENT_MEMORY_FREE = 2,
  GXF_EVENT_MESSAGE_SYNC = 3,
  GXF_EVENT_TIME_UPDATE = 4,
  GXF_EVENT_STATE_UPDATE = 5,
} gxf_event_t;
~~~

这些名字描述的是：

~~~text
什么事实变了
~~~

而不是：

~~~text
最终 Entity 状态是什么
~~~

这是一条非常干净的分层：

~~~text
event = cause
condition = computed result
~~~

## 13. GxfEntityNotifyEventType 不直接依赖具体 Scheduler

公共调用：

~~~cpp
GxfEntityNotifyEventType(
    context,
    eid,
    event);
~~~

进入 Runtime 后再到 Program。`Program::entityEventNotify()` 会先检查 graph lifecycle：

~~~cpp
State state = state_.load();

if (state == State::DEINITALIZING ||
    state == State::ACTIVATING) {
  return Success;
}

if (state != State::RUNNING &&
    state != State::INTERRUPTING &&
    state != State::STARTING) {
  return Unexpected{
      GXF_INVALID_EXECUTION_SEQUENCE};
}

return system_group_->event_notify(
    eid,
    event);
~~~

这解决两个问题。

第一，deinitialize 时不会继续把新工作注入调度器。

第二，产生事件的组件只依赖 runtime event API，不需要保存 `EventBasedScheduler*`。

## 14. SystemGroup 是事件进入执行系统的统一门

`SystemGroup::event_notify_abi()`：

~~~cpp
for (size_t i = 0;
     i < systems_.size();
     i++) {
  const auto& system =
      systems_.at(i).value();

  const auto& result =
      system->event_notify_abi(
          eid,
          event);

  if (result != GXF_SUCCESS) {
    return result;
  }
}
~~~

所以控制路径是：

~~~text
component / external actor
→ GxfEntityNotifyEventType
→ Runtime
→ Program
→ SystemGroup
→ System::event_notify_abi
→ concrete Scheduler
~~~

这是一个很典型的 runtime control-plane fan-out。

## 15. EventBasedScheduler 对 EXTERNAL 与内部事件分流

公开 EBS：

~~~cpp
gxf_result_t
EventBasedScheduler::event_notify_abi(
    gxf_uid_t eid,
    gxf_event_t event) {

  auto itr = entities_.find(eid);

  if (itr == entities_.end()) {
    return GXF_SUCCESS;
  }

  if (event == GXF_EVENT_EXTERNAL) {
    std::unique_lock<std::mutex> lock(
        external_event_notification_mutex_);

    external_event_notified_->
        pushEvent(eid);

    external_event_notification_cv_.
        notify_one();
  } else {
    notifyDispatcher(eid);
  }

  return GXF_SUCCESS;
}
~~~

也就是说：

~~~text
MESSAGE_SYNC
MEMORY_FREE
TIME_UPDATE
STATE_UPDATE
       |
       v
internal dispatcher path

EXTERNAL
       |
       v
async event handler path
~~~

内部 event 在这个公开实现里最终都汇聚成同一件事：重新检查 eid。

## 16. notifyDispatcher 并没有把 Entity 直接放进 ready queue

实现：

~~~cpp
gxf_result_t
EventBasedScheduler::notifyDispatcher(
    gxf_uid_t eid) {

  std::unique_lock<std::mutex> lock(
      internal_event_notification_mutex_);

  internal_event_notified_->
      pushEvent(eid);

  internal_event_notification_cv_.
      notify_one();

  return GXF_SUCCESS;
}
~~~

这里没有：

~~~cpp
e->condition_ = READY;
~~~

也没有：

~~~cpp
ready_queue.push(eid);
~~~

只做：

~~~text
eid
→ re-evaluation queue
→ wake dispatcher
~~~

## 17. Dispatcher 醒来以后调用 checkEntity

Dispatcher 从 `internal_event_notified_` 取 eid：

~~~cpp
if ((eid != kNullUid) &&
    (!executor_->isEntityBusy(eid))) {

  std::shared_lock<
      std::shared_timed_mutex>
      lk_(e->ready_queue_sync_mutex_);

  if (e->is_present_in_ready_queue_
      == false) {
    lk_.unlock();
    dispatchEntity(e);
  }
}
~~~

`dispatchEntity()` 的关键动作：

~~~cpp
maybe_condition =
    executor_->checkEntity(
        e->eid_,
        now);

updateCondition(
    e,
    maybe_condition.value());
~~~

所以 Scheduler 真正做的是：

~~~text
event
→ wake
→ check current state
→ classify
→ route to proper queue/set
~~~

## 18. EntityExecutor 会把多个 term 做 AND 合成

`EntityItem::check()`：

~~~cpp
SchedulingCondition combined{
    SchedulingConditionType::READY,
    0};

for (size_t i = 0;
     i < terms.size();
     i++) {

  auto& term =
      terms.at(i).value();

  Expected<SchedulingCondition> result =
      term->check(timestamp);

  combined =
      AndCombine(
          combined,
          result.value());
}

return combined;
~~~

例如：

~~~text
MessageAvailable = READY
Periodic         = READY
MemoryAvailable  = WAIT
DownstreamSpace  = READY
~~~

最终只能是 WAIT。

因此 runtime 的 runnable predicate 本质上是资源约束的逻辑合成。

## 19. AndCombine 还决定“下一次怎么醒”

SchedulingCondition 不只是 bool。

公开 `AndCombine()` 的支配顺序：

~~~cpp
if (a.type == NEVER ||
    b.type == NEVER) {
  return {NEVER, 0};
}

if (a.type == WAIT_EVENT ||
    b.type == WAIT_EVENT) {
  return {WAIT_EVENT, 0};
}

if (a.type == WAIT ||
    b.type == WAIT) {
  return {WAIT, 0};
}
~~~

两个 WAIT_TIME：

~~~cpp
return {
  WAIT_TIME,
  std::max(
      a.target_timestamp,
      b.target_timestamp)
};
~~~

所以可以概括成：

~~~text
NEVER
  >
WAIT_EVENT
  >
WAIT
  >
WAIT_TIME
  >
READY
~~~

这里的“>”表示 AND 合成时谁支配最终 wake-up 机制。

## 20. 为什么两个 WAIT_TIME 取 max

如果：

~~~text
condition A:
ready after 10 ms

condition B:
ready after 14 ms
~~~

AND 要求：

~~~text
t >= 10
AND
t >= 14
~~~

等价于：

~~~text
t >= max(10, 14)
~~~

所以 Scheduler 只需要保留 14 ms 这一条最晚约束。

这是把逻辑条件降成 timer key。

## 21. updateCondition 把逻辑状态翻译成数据结构

公开 EBS：

~~~cpp
switch (next_condition.type) {
  case SchedulingConditionType::READY:
    ready_wait_time_jobs_[queue_index]
        ->insert(
            eid,
            target_timestamp,
            kMaxSlipNs,
            1);
    break;

  case SchedulingConditionType::WAIT_TIME:
    ready_wait_time_jobs_[queue_index]
        ->insert(
            eid,
            target_timestamp,
            kMaxSlipNs,
            1);
    break;

  case SchedulingConditionType::WAIT_EVENT:
    event_waiting_->pushEvent(eid);
    break;

  case SchedulingConditionType::WAIT:
    waiting_->pushEvent(eid);
    break;

  case SchedulingConditionType::NEVER:
    internal_event_notified_
        ->removeEvent(eid);
    break;
}
~~~

因此 condition state 的真正意义是：

> 下一次应该通过哪种机制重新考虑这个 Entity。

## 22. READY 与 WAIT_TIME 为什么可以共用 TimedJobList

从 timer queue 看：

~~~text
READY:
target_timestamp <= now

WAIT_TIME:
target_timestamp > now
~~~

它们都是：

~~~text
eid + target_timestamp
~~~

差别只是时间是否成熟。

因此可以由同一种 time-ordered structure 管理。

WAIT_EVENT 则没有确定 timestamp，必须等待 external/internal event；WAIT 也没有可排序的时间 key，所以需要不同结构。

## 23. Holoscan AsynchronousCondition 如何接入这套状态机

Holoscan 的 setter：

~~~cpp
void AsynchronousCondition::event_state(
    AsynchronousEventState state) {

  auto asynchronous_scheduling_term =
      get();

  auto gxf_event_state =
      holoscan_to_gxf_event_state(
          state);

  if (asynchronous_scheduling_term) {
    asynchronous_scheduling_term->
        setEventState(
            gxf_event_state);
  }

  event_state_ = state;
}
~~~

高层状态最后进入 GXF `AsynchronousSchedulingTerm`。

它的 check 映射：

~~~text
EVENT_NEVER
→ NEVER

EVENT_WAITING
→ WAIT_EVENT

WAIT
→ WAIT

READY / EVENT_DONE
→ READY
~~~

所以 Holoscan 的 execution-control API 不是旁路机制，它最终仍回到统一 SchedulingCondition 模型。

## 24. EVENT_DONE 为什么必须主动发 GXF_EVENT_EXTERNAL

公开实现：

~~~cpp
void
AsynchronousSchedulingTerm::setEventState(
    AsynchronousEventState state) {

  std::lock_guard<std::mutex> lock(
      event_state_mutex_);

  event_state_ = state;

  if (event_state_ ==
      AsynchronousEventState::EVENT_DONE) {

    GxfEntityEventNotify(
        context(),
        eid());
  }
}
~~~

`GxfEntityEventNotify()` 会使用 `GXF_EVENT_EXTERNAL`。

于是一个外部线程完成工作时：

~~~text
external thread
→ set EVENT_DONE
→ GXF_EVENT_EXTERNAL
→ external event queue
→ async event thread
→ dispatchEntityAsync
→ checkEntity
→ READY
→ worker
~~~

这是一条完整跨线程唤醒链。

## 25. 为什么外部事件单独走 async event queue

内部事件来源通常是 runtime 自己：

~~~text
MessageRouter
Allocator
SchedulingTerm state update
~~~

外部事件可能来自：

~~~text
camera SDK callback
device service thread
CUDA callback
inference service
user controller thread
~~~

让这些外部线程直接摸 ready queue，会把 scheduler 内部同步协议暴露出去。

公开 EBS 只让它们投递一个 eid，再由 async event handler 转交，职责边界更清楚。

## 26. Holoscan 给 Operator 自带 internal async condition

`Operator::initialize_async_condition()`：

~~~cpp
if (!internal_async_condition_) {
  internal_async_condition_ =
      fragment()->make_condition<
          holoscan::AsynchronousCondition>(
              "_internal_async_condition");

  add_arg(internal_async_condition_);
}
~~~

`stop_execution()`：

~~~cpp
internal_async_condition_->event_state(
    holoscan::AsynchronousEventState::
        EVENT_NEVER);
~~~

因此停止 Operator 可以通过 scheduling state 完成，而不是杀线程：

~~~text
control condition
→ NEVER
→ no future scheduling
~~~

## 27. busy 时来的 event 为什么不应该造成重入

Dispatcher 会先检查：

~~~cpp
!executor_->isEntityBusy(eid)
~~~

因为事件可能在当前 tick 还没结束时到达。

如果 event 一到就重复 enqueue：

~~~text
worker 0: Entity E compute()
worker 1: Entity E compute()
~~~

就破坏 Entity 的串行语义。

所以 event 的含义必须是：

~~~text
state changed,
re-evaluate at a safe execution boundary
~~~

而不是“强制启动另一个 tick”。

## 28. busy 时 event 被跳过，会不会丢 wakeup

公开 worker 在每次执行结束后都会：

~~~cpp
e->releaseOwnership();

if (state_ == State::kRunning) {
  notifyDispatcher(eid);
}
~~~

所以：

~~~text
event arrives while busy
→ cannot re-dispatch immediately
→ current tick completes
→ worker sends internal notification
→ dispatcher re-checks latest state
~~~

这里再次体现“state is truth, event is opportunity”。

## 29. 已经在 ready queue 时也不需要重复塞 eid

Dispatcher 还检查：

~~~cpp
e->is_present_in_ready_queue_ == false
~~~

如果一个高频相机在 Entity 已经 ready 时连续来多帧：

~~~text
MESSAGE_SYNC
MESSAGE_SYNC
MESSAGE_SYNC
...
~~~

不需要对应 N 个 ready-queue token。

因为 ready queue 表达：

~~~text
这个 Entity 至少值得被 worker 检查一次
~~~

不是：

~~~text
每个 event 必须对应一次 compute
~~~

消息计数仍然保存在 Receiver queue 中。

## 30. ready snapshot 过期也不会直接裸执行

Dispatcher check 与 worker 真正运行之间存在间隔：

~~~text
dispatcher check
→ enqueue
→ queue wait
→ worker gets eid
~~~

期间 downstream、memory、control state 都可能变化。

因此 `EntityExecutor::execute()` 会在真正 tick 前再次 check。

这相当于：

~~~text
dispatcher check
= admission candidate

worker-side check
= commit-time validation
~~~

与 optimistic concurrency control 很相似。

## 31. 一条消息到达后的完整 wake-up 链

~~~text
Producer compute()
    |
OutputContext emit
    |
Transmitter backstage
    |
Producer tick ends
    |
MessageRouter::syncOutbox()
    |
receiver->push(message)
    |
GXF_EVENT_MESSAGE_SYNC
    |
Program
    |
SystemGroup
    |
EventBasedScheduler
    |
notifyDispatcher(consumer_eid)
    |
internal_event_notified_
    |
Dispatcher
    |
checkEntity()
    |
SchedulingTerm::check()
    |
update_state_abi()
    |
read Receiver queue
    |
AndCombine()
    |
READY
    |
updateCondition()
    |
TimedJobList / ready queue
    |
worker
    |
executeEntity()
    |
re-check
    |
syncInbox()
    |
Operator::compute()
~~~

这就是从“数据到了”到“业务代码获得 CPU”的完整因果链。

## 32. 下游腾出空间后的反向 wake-up 链

~~~text
Consumer receive()
    |
pop message
    |
notify connected transmitter eid
    |
GXF_EVENT_MESSAGE_SYNC
    |
re-check upstream Producer
    |
DownstreamReceptive::update_state_abi()
    |
recompute free capacity
    |
READY
    |
Producer may run
~~~

这条反向控制链才让 bounded queue 的 backpressure 能自动解除。

## 33. GPU/CPU buffer 释放后的 wake-up 链

~~~text
Tensor/buffer lifetime ends
    |
Allocator::free()
    |
GXF_EVENT_MEMORY_FREE
    |
Scheduler re-check
    |
MemoryAvailableSchedulingTerm
    |
allocator_->is_available(...)
    |
READY
    |
waiting Operator becomes runnable
~~~

因此内存回收和执行调度不是两个完全独立的子系统。

## 34. Event-Based Scheduler 真正减少的是“无意义检查”

Event-based 不会让 condition check 消失。

每次通知后仍然要：

~~~text
update state
→ check
→ combine
~~~

它减少的是资源没有变化期间的反复扫描。

Polling：

~~~text
check
check
check
check
resource changes
check
~~~

Event-driven：

~~~text
sleep
resource changes
notify
check
sleep
~~~

核心收益是：

> 把“什么时候值得重新检查”的知识推给状态变化源。

## 35. 对机器人实时系统应该监控什么

一条 event-driven response path 可以拆成：

~~~text
T_event:
state change → notification

T_dispatch:
notification → dispatcher re-check

T_check:
SchedulingTerm update/check/combine

T_queue:
READY → worker gets eid

T_validate:
worker-side re-check

T_compute:
Operator execution
~~~

总延迟近似：

~~~text
T_response =
T_event
+ T_dispatch
+ T_check
+ T_queue
+ T_validate
+ T_compute
~~~

只测 compute latency，无法解释 sensor-to-actuator latency。

## 36. 为什么这对控制与具身系统特别重要

可能出现：

~~~text
compute = 0.3 ms
sensor-to-action = 8 ms
~~~

差值可能来自：

- notification producer 线程优先级；
- dispatcher 没及时获得 CPU；
- WAIT_TIME；
- downstream backpressure；
- allocator 暂时无 buffer；
- ready queue 拥塞；
- CPU affinity / migration；
- worker 二次验证后重新进入 WAIT。

所以实时分析应该画：

~~~text
physical event
→ runtime notification
→ condition re-check
→ ready queue
→ worker
→ compute
→ output
~~~

而不只是 Operator DAG。

## 37. 抽象成一个通用 runtime 模型

把 GXF 类名拿掉，仍可以得到：

~~~text
Resource
  owns authoritative state

Condition
  maps resource state
  to execution predicate

Event bus
  carries invalidation / wakeup hint

Dispatcher
  re-evaluates affected task

Condition algebra
  merges constraints

Ready/timer/event structures
  encode wake-up mechanism

Worker
  validates and executes
~~~

这个模型可以迁移到：

- ROS Executor guard condition；
- GPU inference pipeline；
- device-driver task graph；
- realtime controller runtime；
- VLA perception/action pipeline；
- distributed async service orchestration。

## 38. 五种 SchedulingCondition 应该怎样理解

不要把它们只记成五个 enum。

更准确的是：

~~~text
READY
  当前约束满足，
  可以进入 execution path

WAIT_TIME
  当前不能运行，
  但知道最早重新考虑时间

WAIT_EVENT
  当前不能运行，
  等明确的异步事件

WAIT
  当前不能运行，
  等某种状态变化后重新检查

NEVER
  不再参与后续 scheduling
~~~

它们描述的是：

> 下一次应该用什么机制再次考虑这个 Entity。

## 39. 最终闭环

~~~text
Message / Capacity / Memory / Time / Async completion
                       |
                       v
          GxfEntityNotifyEventType
                       |
                       v
              Program lifecycle gate
                       |
                       v
                  SystemGroup
                       |
                       v
             EventBasedScheduler
                       |
            +----------+----------+
            |                     |
       internal event        external event
            |                     |
       Dispatcher           Async event thread
            |                     |
            +----------+----------+
                       |
                       v
                checkEntity()
                       |
                       v
              SchedulingTerm::check
                       |
             update_state_abi()
                       |
                 check_abi()
                       |
                       v
            AndCombine(all terms)
                       |
                       v
 READY / WAIT_TIME / WAIT_EVENT / WAIT / NEVER
                       |
                       v
              updateCondition()
                       |
                       v
       ready queue / timer / wait set
                       |
                       v
                    worker
                       |
                       v
             executeEntity()
                       |
                 re-check
                       |
                       v
                   compute()
                       |
                       v
             notifyDispatcher()
~~~

Condition、Scheduler 和 EntityExecutor 到这里才真正成为一条闭合控制链。

最值得带走的设计原则是：

> **不要让事件本身成为执行许可；让事件只负责促使系统重新读取当前真值，再由统一的 condition algebra 决定是否真的可以执行。**
