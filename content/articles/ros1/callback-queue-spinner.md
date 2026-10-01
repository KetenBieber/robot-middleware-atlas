# CallbackQueue 与 Spinner：收到消息以后，谁真正执行 ROS1 回调

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。

ROS1 通信最容易被忽略的一层不是 TCP，而是执行。网络线程把消息收进来以后，用户 callback 并不会“立即运行”。消息先进入订阅队列，再变成 CallbackQueue 中的可执行工作，最后由 Spinner 所在线程取得 CPU。

这也是为什么一个 ROS 节点即使网络吞吐足够，仍然可能出现明显 callback jitter。

## 1. 如果网络线程直接运行 callback 会发生什么

假设同一 node 有：

~~~text
/camera callback   35 ms
/imu callback       1 ms
/cmd callback       1 ms
~~~

如果 socket read thread 收到 camera 后直接跑视觉算法：

~~~text
Poll thread
  |
  +-- camera callback 35 ms
  |
  +-- during these 35 ms:
      other socket progress stalls
~~~

网络 I/O progress 被用户代码 WCET 绑住。ROS1 因此建立边界：

~~~text
I/O thread:
  make message work ready

Spinner thread:
  execute user work
~~~

## 2. SubscriptionQueue 是每个订阅 callback 的消息 FIFO

固定源码构造：

~~~cpp
SubscriptionQueue::SubscriptionQueue(
    const std::string& topic,
    int32_t queue_size,
    bool allow_concurrent_callbacks)
: topic_(topic)
, size_(queue_size)
, full_(false)
, queue_size_(0)
, allow_concurrent_callbacks_(
      allow_concurrent_callbacks)
{}
~~~

这里有两类状态：

~~~text
queue capacity:
  size_
  queue_size_
  full_

execution policy:
  allow_concurrent_callbacks_
~~~

同一个对象既管理输入样本 backlog，也管理这个订阅 callback 能否并行执行。

## 3. 满队列为什么丢 oldest

`push()`：

~~~cpp
boost::mutex::scoped_lock lock(queue_mutex_);

if (fullNoLock())
{
  queue_.pop_front();
  --queue_size_;

  full_ = true;

  if (was_full)
    *was_full = true;
}

queue_.push_back(i);
++queue_size_;
~~~

假设 100 Hz odometry，callback 只能 50 Hz：

~~~text
无限 FIFO:
  new data keeps arriving
  backlog keeps growing
  callback processes increasingly old poses

drop-old bounded FIFO:
  history is discarded
  newer samples survive
~~~

对控制回路来说，处理陈旧状态往往比少处理一些中间样本更危险。

但这不意味着 queue 越小永远越好。短时 CPU 抖动时，一个小 buffer 可以吸收 burst。它本质是 data-age 与 burst-tolerance 的工程权衡。

## 4. SubscriptionQueue::call 才真正 deserialize + 调用户函数

~~~cpp
CallbackInterface::CallResult
SubscriptionQueue::call()
{
  Item i;

  {
    boost::mutex::scoped_lock lock(queue_mutex_);

    if (queue_.empty())
      return CallbackInterface::Invalid;

    i = queue_.front();
    queue_.pop_front();
    --queue_size_;
  }

  VoidConstPtr msg =
      i.deserializer->deserialize();

  if (msg)
  {
    SubscriptionCallbackHelperCallParams params;

    params.event =
        MessageEvent<void const>(
          msg,
          i.deserializer->getConnectionHeader(),
          i.receipt_time,
          i.nonconst_need_copy,
          MessageEvent<void const>::CreateFunction());

    i.helper->call(params);
  }

  return CallbackInterface::Success;
}
~~~

所以收到 TCP bytes 与执行 callback 之间至少还有：

~~~text
queueing
OS scheduling
deserialization
callback dispatch
~~~

这些都属于 message age 的组成部分。

## 5. CallbackQueue 为什么还要再套一层

一个 Node 不只有 topic subscription。还可能有 timer、service 和内部 callback。ROS1 用 `CallbackInterface` 把这些不同工作统一成可执行任务。

`CallbackQueue::addCallback`：

~~~cpp
void CallbackQueue::addCallback(
    const CallbackInterfacePtr& callback,
    uint64_t removal_id)
{
  CallbackInfo info;
  info.callback = callback;
  info.removal_id = removal_id;

  {
    boost::mutex::scoped_lock lock(mutex_);

    if (!enabled_)
      return;

    callbacks_.push_back(info);
  }

  if (callback->ready())
    condition_.notify_one();
}
~~~

这相当于把不同事件源统一汇入一个 `D_CallbackInfo`，再让 Spinner 统一消费。

## 6. condition_variable 的语义只是“唤醒等待者”

`condition_.notify_one()` 不等于“callback 现在执行”。

真实过程：

~~~text
producer thread
  enqueue CallbackInfo
  notify_one
       |
       v
spinner thread becomes runnable
       |
       v
OS scheduler decides when it gets CPU
       |
       v
callOne/callAvailable
       |
       v
callback
~~~

所以 callback latency 还受 Spinner thread priority、CPU affinity、其他 runnable threads、当前 callback WCET 和 mutex contention 影响。

## 7. SingleThreadedSpinner 为什么最容易推理

~~~cpp
void SingleThreadedSpinner::spin(
    CallbackQueue* queue)
{
  if (!queue)
    queue = getGlobalCallbackQueue();

  ros::WallDuration timeout(0.1f);
  ros::NodeHandle n;

  while (n.ok())
  {
    queue->callAvailable(timeout);
  }
}
~~~

一个线程负责整个 queue。

优点：

~~~text
no user callback concurrency
simple ordering
simple shared-state reasoning
~~~

代价：

~~~text
one slow callback
    |
    v
head-of-line blocking
    |
    v
every later callback delayed
~~~

因此把高耗时 perception 和低延迟 control callback 放在同一个全局 queue，通常不是好执行结构。

## 8. callAvailable 为什么把 shared queue 批量搬到 TLS

固定源码：

~~~cpp
bool was_empty = tls->callbacks.empty();

tls->callbacks.insert(
    tls->callbacks.end(),
    callbacks_.begin(),
    callbacks_.end());

callbacks_.clear();

calling_ += tls->callbacks.size();
~~~

之后在线程本地 deque 里逐个执行。

可以把 ownership 理解成：

~~~text
shared queue
   |
   | batch handoff
   v
thread-local callback deque
   |
   v
user execution
~~~

全局锁保护的是“谁拿到哪些工作”，而不是包住整个用户 callback。这减少了执行用户代码时长期占用 global queue mutex 的机会。

## 9. MultiThreaded / AsyncSpinner 为什么使用 callOne

AsyncSpinner worker：

~~~cpp
void AsyncSpinnerImpl::threadFunc()
{
  disableAllSignalsInThisThread();

  CallbackQueue* queue =
      callback_queue_;

  bool use_call_available =
      thread_count_ == 1;

  WallDuration timeout(0.1);

  while (continue_ && nh_.ok())
  {
    if (use_call_available)
      queue->callAvailable(timeout);
    else
      queue->callOne(timeout);
  }
}
~~~

一个 worker 时，批量 drain 更简单；多个 worker 时每次 `callOne`，让工作能在多个线程之间竞争分配。

如果多个 worker 都一次把整个 shared queue 抢进自己的 TLS，最先抢到锁的线程可能垄断一大批工作。

## 10. 多线程 Spinner 不等于同一 Subscription 并发

`SubscriptionQueue::call`：

~~~cpp
boost::recursive_mutex::scoped_try_lock lock(
    callback_mutex_,
    boost::defer_lock);

if (!allow_concurrent_callbacks_)
{
  lock.try_lock();

  if (!lock.owns_lock())
  {
    return CallbackInterface::TryAgain;
  }
}
~~~

默认情况下，同一个 subscription callback 仍然串行。

因此：

~~~text
N Spinner workers
    !=
same subscription has N concurrent callbacks
~~~

并发 policy 存在两个层次：

~~~text
global CallbackQueue worker parallelism
+
per-Subscription serialization policy
~~~

## 11. TryAgain 为什么不是错误

如果 worker A 正在运行某 Subscription callback，worker B 又取到同一个 SubscriptionQueue，B 无法取得 `callback_mutex_`。

它返回 `TryAgain`。

CallbackQueue：

~~~cpp
if (result == CallbackInterface::TryAgain &&
    !info.marked_for_removal)
{
  boost::mutex::scoped_lock lock(mutex_);
  callbacks_.push_back(info);
  return TryAgain;
}
~~~

worker B 不阻塞死等，而是把工作重新排到 shared queue，让其他工作有机会推进。

这是一个简单的 cooperative scheduling 策略。

## 12. removal_id 为什么涉及 shared_mutex 和 TLS

一个 Subscriber 被销毁时，不能只从 shared deque 删除它的工作，因为 callback 可能已经：

~~~text
still in shared queue
or
moved into a Spinner thread's TLS queue
or
currently executing
~~~

`CallbackQueue::removeByID` 因此管理 `IDInfo`、shared mutex、marked_for_removal，并处理“在 callback 内部删除自己”这种递归场景。

生命周期问题本质是：**如何证明未来不会再有执行流进入已经逻辑销毁的对象？**

这和其他异步 Runtime 的 deferred destruction 是同一类问题。

## 13. SpinnerMonitor 为什么限制不兼容的 spinner

`SpinnerMonitor` 保存：

~~~cpp
std::map<ros::CallbackQueue*, Entry>
    spinning_queues_;

boost::mutex mutex_;
~~~

SingleThreadedSpinner 记录具体 thread id；multi-threaded spinner 用空 thread id 表示模式。

目的是阻止同一个 queue 被一个 single-thread spinner 和另一个线程上的不兼容 spinner 同时消费，从而破坏用户对串行顺序的预期。

## 14. ROS1 Spinner 为什么不是实时调度器

Spinner 没有定义：

~~~text
deadline scheduling
callback priority
WCET reservation
CPU budget
deterministic preemption
~~~

它主要解决：

~~~text
哪个普通 OS thread
从哪个 CallbackQueue
取哪一个 ready work
~~~

所以 `AsyncSpinner(4)` 并不自动等于实时控制。

1 kHz 控制回路还需要独立考虑 SCHED_FIFO/thread priority、CPU isolation/affinity、callback WCET、allocator/page fault、lock contention 和 message timestamp/data age。

## 15. 一个实际的共享锁阻塞场景

假设同一 CallbackQueue：

~~~text
camera callback:
  25 ms
  holds application mutex M

control callback:
  0.5 ms
  also needs M

MultiThreadedSpinner(4)
~~~

即使 control callback 已被另一个 worker 取到，也可能阻塞在 M 上等待 camera callback。

增加 Spinner 线程数并不会消除 application-level dependency。执行分析必须跨越中间件 queue 与业务共享状态。

## 16. 自定义 CallbackQueue 为什么比盲目加线程更有意义

ROS1 允许 NodeHandle 绑定不同 CallbackQueue。于是可以把：

~~~text
control callbacks
~~~

和：

~~~text
logging / perception / service callbacks
~~~

放到不同 queue，再使用不同线程消费。

这比单纯增加全局 AsyncSpinner 线程数更接近“执行域隔离”。

但它仍不自动赋予线程实时优先级，OS scheduling policy 仍要由应用设计。

## 17. CallbackQueue 给 ROS2 Executor 留下了什么问题

ROS1 已经把 I/O 和 callback execution 分开，但执行模型表达能力有限。

ROS2 进一步显式化：

~~~text
wait set
executor
callback group
mutually-exclusive / reentrant relation
waitable
guard condition
~~~

核心问题并没有改变：

> readiness 出现以后，哪个 execution context 在什么约束下运行用户代码？

ROS1 的 CallbackQueue + Spinner 是这个问题的一套较小、很适合读源码的答案。
