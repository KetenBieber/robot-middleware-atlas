# GXF EventBasedScheduler 内部实现：TimedJobList、EventList、Worker 与 Dispatcher

Holoscan SDK 行为基线：v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

公开 GXF 源码基线：v3.2-1 `daf1810358301f642374dfb3d725be349bba5ec0`。

> 这两个版本不是同一个 backend 快照。Holoscan v4.6 的 EventBasedScheduler 已经公开了 per-worker private ready queue、notification sharding、batch drain、work stealing 等新能力；公开 GXF v3.2-1 则保留了更早一代可完整阅读的核心实现。本文用后者解释底层数据结构与线程协议，再把它和 v4.6 的行为演进对照，绝不把旧源码冒充成新版本实现。

这一页终于可以回答前一篇留下的问题：

~~~text
READY 到底放在哪个容器？
WAIT_TIME 为什么不能和 WAIT_EVENT 用同一个结构？
worker 睡在哪里？
dispatcher 怎样被唤醒？
同一个 Entity 为什么不会被两个 worker 同时执行？
~~~

## 先看完整线程拓扑

公开 GXF v3.2-1 的 `EventBasedScheduler::runAsync_abi()` 会创建：

~~~text
1 × dispatcher thread
1 × async event handler thread
N × worker threads
0/1 × max-duration thread
~~~

源码：

~~~cpp
dispatcher_thread_ = std::thread([this] {
  dispatcherThreadEntrance();
});

async_threads_.emplace_back([this] {
  asyncEventThreadEntrance();
});

for (const auto& thread_pool_ptr : thread_pool_set_) {
  for (const auto& thread_it : thread_pool_ptr->get()) {
    async_threads_.emplace_back([=] {
      workerThreadEntrance(thread_pool_ptr,
                           thread_it.second.uid);
    });
  }
}
~~~

因此这里不是一个“线程池”对象包办全部执行。

它其实是三种责任完全不同的 OS thread：

~~~text
Dispatcher
  决定 Entity 当前属于 READY / WAIT / WAIT_TIME / WAIT_EVENT / NEVER

Worker
  真正执行 Entity

Async-event thread
  处理来自外部 actor 的异步事件
~~~

这种职责拆分比简单的 producer/consumer 线程池更接近一个 runtime scheduler。

## 第一张核心数据结构图

Scheduler 里最关键的成员：

~~~cpp
std::unordered_map<gxf_uid_t,
                   std::shared_ptr<ScheduleEntity>>
    entities_;

std::vector<std::unique_ptr<TimedJobList<gxf_uid_t>>>
    ready_wait_time_jobs_;

std::unique_ptr<UniqueEventList<gxf_uid_t>>
    event_waiting_;

std::unique_ptr<UniqueEventList<gxf_uid_t>>
    waiting_;

std::unique_ptr<UniqueEventList<gxf_uid_t>>
    external_event_notified_;

std::unique_ptr<UniqueEventList<gxf_uid_t>>
    internal_event_notified_;
~~~

这组成员已经把 SchedulingCondition 的五个状态映射成了不同容器。

## 为什么 READY 和 WAIT_TIME 共用 TimedJobList

READY 可以被看成：

~~~text
target_timestamp <= now
~~~

WAIT_TIME 则是：

~~~text
target_timestamp > now
~~~

它们都天然带“目标执行时间”。

因此 GXF 把二者放进：

~~~cpp
TimedJobList<gxf_uid_t>
~~~

差别只是 target_timestamp。

这比维护：

~~~text
ready_queue
+
timer_queue
~~~

两套完全独立容器更统一。

## TimedJobList 内部为什么是 priority_queue

真正定义：

~~~cpp
std::priority_queue<
    Item,
    std::vector<Item>,
    ItemPriorityCmp>
    queue_;
~~~

Item 包含：

~~~cpp
JobT job;
int64_t target_time;
int64_t slack;
int priority;
~~~

所以它不是普通 FIFO。

最重要查询是：

> 下一次最早应该运行哪个 job？

这是典型 min-deadline/earliest-time ordering。

`priority_queue` 的插入复杂度约为：

~~~text
O(log N)
~~~

取最早项则是 O(1)。

这比每次从 vector 全扫描最小 timestamp 更适合 timer-like workload。

## Comparator 为什么同时看 deadline 和 priority

核心逻辑：

~~~cpp
const int64_t a_time = a.target_time + a.slack;
const int64_t b_time = b.target_time + b.slack;

if (std::abs(a_time - b_time) < kTimeFudge
    && a.priority != b.priority) {
  return a.priority < b.priority;
} else {
  return a_time > b_time;
}
~~~

即：

~~~text
正常情况：
earlier target/slack first

时间几乎一样：
priority breaks tie
~~~

这正是实时调度里常见的二级排序。

## 为什么 TimedJobList 还需要 unordered_set

成员：

~~~cpp
std::unordered_set<JobT> items_;
~~~

插入前：

~~~cpp
if (items_.find(job) != items_.end()) {
  return false;
}
~~~

它不是为了遍历，而是为了防止同一个 Entity 重复进入 timed queue。

如果只用 priority_queue，要判断“eid 42 是否已经排队”需要扫描内部 heap。

于是：

~~~text
priority_queue
  解决：谁最早执行

unordered_set
  解决：谁已经存在
~~~

这是同一个 job set 的两个索引。

## pending_ 为什么用 std::list

TimedJobList 还有：

~~~cpp
std::list<Item> pending_;
~~~

当 target time 到达时：

~~~cpp
pending_.push_back(top_item);
queue_.pop();
~~~

然后从 pending_ 找一个可执行项。

这相当于把 job 分成：

~~~text
future heap
  target time 还没到

pending list
  时间已经成熟，等待 worker 取走
~~~

两个阶段。

为什么不直接从 heap pop 出来返回？

因为同一个 TimedJobList 可以被多个 worker wait；pending_ 作为“已成熟但尚未真正领取”的集合，让时间成熟和 worker 获取这两个动作分开。

## waitForJob() 是 OS 睡眠点

核心：

~~~cpp
if (wait_duration > 0) {
  queue_cv_.wait_for(
      lock,
      std::chrono::nanoseconds(wait_duration));
} else {
  queue_cv_.wait(lock);
}
~~~

这里终于把 runtime 映射到 OS blocking primitive。

没有任务：

~~~text
condition_variable wait
→ thread blocks
→ leaves CPU runqueue
~~~

未来有 timer：

~~~text
wait_for(until next ETA)
~~~

有新 job insert：

~~~cpp
queue_cv_.notify_one();
~~~

所以 Event-Based 的“event”不是抽象概念，最终就是条件变量、timer wakeup 与线程调度。

## 为什么 WAIT 和 WAIT_EVENT 不进 TimedJobList

WAIT 没有确定的 wake-up timestamp。

WAIT_EVENT 更明确：必须等某个外部事件。

因此它们进入：

~~~cpp
UniqueEventList<gxf_uid_t>
~~~

这类容器更像“等待集合”而不是“按时间排序的 queue”。

## UniqueEventList 为什么同时用 list + unordered_map

内部：

~~~cpp
std::list<T> list_;
std::unordered_map<
    T,
    typename std::list<T>::iterator>
    items_;
~~~

push：

~~~cpp
if (items_.find(item) != items_.end()) {
  return false;
}
list_.push_back(item);
items_.insert({item, --list_.end()});
~~~

remove：

~~~cpp
list_.erase(items_.at(item));
items_.erase(item);
~~~

这套组合非常经典。

`list` 提供：

~~~text
稳定 iterator
顺序 pop_front
O(1) erase by iterator
~~~

`unordered_map` 提供：

~~~text
item → iterator
均摊 O(1) membership/remove lookup
~~~

如果只用 list，removeEvent(eid) 要 O(N) 扫描。

如果只用 unordered_set，又没有 FIFO-like event order。

因此两者组合是：

> 顺序容器 + 哈希索引。

这和 LRU cache 常见的 list + hash map 是同一种结构。

## updateCondition() 就是状态到容器的路由器

固定源码：

~~~cpp
switch (next_condition.type) {
  case READY:
    ready_wait_time_jobs_[queue_index]->insert(...);
    break;

  case WAIT_EVENT:
    event_waiting_->pushEvent(eid);
    break;

  case WAIT_TIME:
    ready_wait_time_jobs_[queue_index]->insert(...);
    break;

  case WAIT:
    waiting_->pushEvent(eid);
    break;

  case NEVER:
    internal_event_notified_->removeEvent(eid);
    break;
}
~~~

这段代码非常值得记。

SchedulingCondition 不是一个 enum 打日志而已。

它直接决定 Entity 被放进哪一类数据结构。

## Dispatcher 为什么只做 check，不做 execute

dispatcher 收到内部 notification 后：

~~~cpp
if (!executor_->isEntityBusy(eid)) {
  if (!e->is_present_in_ready_queue_) {
    dispatchEntity(e);
  }
}
~~~

`dispatchEntity()` 内部：

~~~cpp
maybe_condition = executor_->checkEntity(eid, now);
updateCondition(e, maybe_condition.value());
~~~

所以 Dispatcher 做：

~~~text
event
→ check scheduling terms
→ classify state
→ put into correct waiting/ready structure
~~~

它不直接运行业务 Codelet。

这保持 control plane 与 execution plane 分离。

## Worker 的核心路径

Worker：

~~~cpp
ready_wait_time_jobs_[mapped_queue]->waitForJob(eid);
~~~

拿到 eid 后：

~~~cpp
if (!e->tryToAcquire()) {
  continue;
}

auto maybe_condition =
    executor_->executeEntity(eid, now);

e->releaseOwnership();

notifyDispatcher(eid);
~~~

链路：

~~~text
waitForJob
↓
atomic acquire entity ownership
↓
execute
↓
release ownership
↓
notify dispatcher to re-check next state
~~~

这就是一个完整 event-driven scheduler tick。

## 为什么 ScheduleEntity 还要一个 atomic ownership

成员：

~~~cpp
std::atomic<EntityOwnership> ownership_;
~~~

acquire：

~~~cpp
EntityOwnership free = kFree;
ownership_.compare_exchange_strong(
    free, kAcquired);
~~~

这是一个非常小的 CAS state machine：

~~~text
kFree
  ↓ CAS winner
kAcquired
  ↓ execute done
kFree
~~~

目的不是保护所有 Entity 数据。

它只解决：

> 两个 worker 是否同时“拥有执行权”。

真正 Entity 内部执行仍有 `EntityItem::execution_mutex_` 作为另一层保护。

这说明工业 runtime 很常见：

~~~text
atomic
保护轻量调度 ownership

mutex
保护较大临界区/复杂对象状态
~~~

不是所有同步都强行 lock-free。

## 为什么还有 ready_queue_sync_mutex_

`ScheduleEntity` 还包含：

~~~cpp
std::shared_timed_mutex ready_queue_sync_mutex_;
bool is_present_in_ready_queue_;
~~~

Dispatcher 和 Worker 都会访问 `is_present_in_ready_queue_`。

它的语义是防止：

~~~text
同一 Entity
同时因为多个 notification
重复插入 ready queue
~~~

所以这里存在两套不同状态：

~~~text
is_present_in_ready_queue_
  container membership

ownership_
  execution ownership
~~~

把两者混成一个 bool 会让状态机变得模糊。

## thread_queue_mapping_ 在旧版本里怎样表达 pinning

默认 ThreadPool 中所有 worker：

~~~cpp
thread_queue_mapping_[worker_id] = 0;
~~~

即共享第 0 个 TimedJobList。

如果某 Entity 绑定到用户 ThreadPool 的特定 thread：

~~~cpp
if (!thread_queue_mapping_.contains(thread_id)) {
  ready_wait_time_jobs_.emplace_back(
      std::make_unique<TimedJobList<...>>(...));
  thread_queue_mapping_[thread_id] =
      ready_wait_time_jobs_.size() - 1;
}
~~~

于是 pinned thread 会获得自己的 queue。

这说明旧公开实现是：

~~~text
default pool:
many workers → one shared ready/timed queue

pinned/custom threads:
thread → dedicated queue
~~~

## 这也解释了 v4.6 为什么继续演化

旧结构在 worker 数增加时，默认 pool 的所有 worker 都会竞争：

~~~text
TimedJobList::queue_cv_mutex_
priority_queue
items_
pending_
~~~

这很容易形成 hot lock。

Holoscan v4.6 文档已经变成：

~~~text
per-worker private ready queue
+
optional work stealing
+
notification sharding
+
batch drain
~~~

可以把这看成一个非常自然的扩展路径：

~~~text
共享 queue 简单正确
↓ worker 数量上升
lock/cache contention 增加
↓
queue sharding/private queue
↓
负载不均
↓
work stealing
~~~

这就是从源码里学习“为什么架构会演进”，而不是背新版参数。

## Dispatcher 自己睡在哪里

公开 GXF：

~~~cpp
internal_event_notification_cv_.wait(
    lock,
    [&] {
      return !internal_event_notified_->empty()
          || state_ != State::kRunning;
    });
~~~

事件产生：

~~~cpp
internal_event_notified_->pushEvent(eid);
internal_event_notification_cv_.notify_one();
~~~

这就是：

~~~text
UniqueEventList
+
mutex
+
condition_variable
~~~

组成的 event channel。

## 为什么 notification list 自己也要去重

假设 Entity 42 很短时间内收到 100 个重复 state-update event。

如果全部进入 dispatcher queue：

~~~text
42,42,42,42,...
~~~

Dispatcher 会重复 check 同一 Entity。

`UniqueEventList` 的 hash index 让重复 eid 不会被插入。

因此 notification 语义其实是：

> “这个 Entity 需要重新检查一次”，而不是“累计 100 个事件次数”。

这是 **edge-trigger-like coalescing** 思维。

很多 GUI event loop、reactor、dirty-flag system 都有类似做法。

## Async event 为什么单独一条 thread

`GXF_EVENT_EXTERNAL` 不直接走普通 internal notification：

~~~cpp
external_event_notified_->pushEvent(eid);
external_event_notification_cv_.notify_one();
~~~

async thread 再：

~~~text
wait external event
↓
dispatchEntityAsync
↓
check ending criteria
~~~

它把“外部 actor 唤醒”从普通 worker→dispatcher feedback path 中拆出来。

这对设备 callback、外部 async completion 很重要。

## Deadlock 判断为什么必须先等一个 grace timeout

Dispatcher 发现：

~~~text
no ready/timed work
no waiting event that can advance?
no running worker?
~~~

可能想判 deadlock。

但外部 event 可能马上到。

所以公开实现会：

~~~cpp
internal_event_notification_cv_.wait_for(
    ..., stop_on_deadlock_timeout_, ...);
~~~

timeout 后再检查一次 ending criteria。

这不是“多等一会儿”的随意补丁。

它是在解决异步系统的经典不可判定边界：

> 当前没有本地可运行工作，不代表未来不会有外部 progress。

## stopAllJobs() 如何让阻塞 worker 退出

每个 TimedJobList：

~~~cpp
ready_wait_time_jobs_[i]->stop();
~~~

`TimedJobList::stop()`：

~~~cpp
is_running_.store(false);
queue_cv_.notify_all();
~~~

Worker 原本睡在 `waitForJob()`。

stop 后：

~~~text
atomic running flag = false
+
notify_all
→ every worker wakes
→ leaves loop
~~~

这就是一个正确 shutdown protocol 的最小形态。

只改 stop flag、不 notify 会导致线程永远睡着。

只 notify、不改 state 又可能醒来后继续等待。

## ThreadPool 数据结构本身为什么只是 map

公开 GXF `ThreadPool`：

~~~cpp
std::map<gxf_uid_t, Thread> thread_pool_;
~~~

Thread 结构甚至只有：

~~~cpp
struct Thread {
  gxf_uid_t uid;
};
~~~

这说明 ThreadPool 在这个版本里更像**执行资源描述**，而不是真的持有 `std::thread` 对象。

真正 `std::thread` 由 Scheduler 创建。

所以：

~~~text
ThreadPool resource
  describes membership / identity / priority

Scheduler
  materializes actual OS threads
~~~

再次体现 declaration 与 execution 的分离。

## 把旧 GXF 与 Holoscan 4.6 放在一起看

| 问题 | 公开 GXF v3.2-1 | Holoscan 4.6 行为契约 |
| --- | --- | --- |
| ready/timed work | shared TimedJobList for default pool | per-worker private ready queues + timed handling |
| event notification | UniqueEventList + CV | notification sharding + batch drain |
| load balance | default workers share queue | optional work stealing |
| pinned work | thread→dedicated TimedJobList | user ThreadPool + affinity/policy |
| worker sleep | condition_variable in TimedJobList | event-based worker queues |
| duplicate schedule | hash membership + queue flag + atomic owner | newer implementation still必须维护 equivalent invariants |

真正应该学的是 invariants，不是容器类名。

未来内部实现即使把 `std::priority_queue` 换成 intrusive heap，把 mutex queue 换成 MPMC ring，这几个不变量仍然存在：

~~~text
Entity 不重复排队
Entity 不并发执行
定时任务按时间成熟
事件等待与时间等待语义不同
worker 无工作时必须阻塞
状态变化必须能唤醒 dispatcher
shutdown 必须唤醒所有 sleeper
~~~

这才是源码拆解最终要提炼出的东西。
