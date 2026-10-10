# WaitSet、Condition 与 Listener：Fast DDS 怎样唤醒应用线程

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## WaitSet façade 很薄

公开 WaitSet::wait() 只是：

~~~cpp
ReturnCode_t WaitSet::wait(
    ConditionSeq& active_conditions,
    const Duration_t timeout) const
{
    return impl_->wait(
        active_conditions,
        timeout);
}
~~~

真正同步逻辑在 WaitSetImpl。

## Attachment 用 unordered_vector

attach_condition()：

~~~cpp
std::lock_guard<std::mutex>
    guard(mutex_);

was_there =
    entries_.remove(
      &condition);

entries_.emplace_back(
    &condition);
~~~

entries_ 是 unordered_vector 风格集合。

原因很直接：

~~~text
Condition 数量通常小
不需要排序
attach/detach 频率低
wait 时需要连续扫描
~~~

所以连续存储比树结构更简单。

## Condition 为什么还有 Notifier

新 Condition attach 时：

~~~cpp
condition
  .get_notifier()
  ->attach_to(this);
~~~

Condition 状态变化后 Notifier 可以反向调用 WaitSet::wake_up()。

这避免 WaitSet 用固定周期轮询所有 Reader。

## 真正阻塞是 condition_variable

WaitSetImpl::wait()：

~~~cpp
std::unique_lock<std::mutex>
    lock(mutex_);

auto fill_active_conditions =
    [&]()
    {
        active_conditions.clear();

        for (const Condition* c :
             entries_)
        {
            if (c
                ->get_trigger_value())
            {
                active_conditions
                  .push_back(
                    const_cast<
                      Condition*>(c));
            }
        }

        return !active_conditions
                  .empty();
    };
~~~

无限等待：

~~~cpp
cond_.wait(
    lock,
    fill_active_conditions);
~~~

有限等待则 wait_for。

所以 application thread 最终睡在 std::condition_variable 上，而不是 socket。

## 一个 WaitSet 不允许两个并发 wait

~~~cpp
if (is_waiting_)
{
    return
      RETCODE_PRECONDITION_NOT_MET;
}
~~~

原因是 active_conditions、condition variable 和等待状态本来就是单 wait operation 的 mutable workspace。

## wake_up 很简单

~~~cpp
void WaitSetImpl::wake_up()
{
    std::lock_guard<std::mutex>
      guard(mutex_);

    cond_.notify_one();
}
~~~

真正复杂的是“什么时候 Condition trigger”，例如 DataReader data available、StatusCondition、GuardCondition 和 event listener condition。

## Listener 与 WaitSet 的线程语义不同

WaitSet 是 application-owned thread：

~~~text
application
→ wait()
→ sleep
→ middleware condition notify
→ application thread resumes
~~~

Listener 则由 middleware delivery/status path 调用。

因此 Listener callback 中不能默认当前一定是 executor thread，也不能默认可以无限阻塞。

必须追具体 callback source。

## 与 Cyclone DDS 的对照

Cyclone DDS WaitSet 也使用 mutex + condition variable，但内部维护 triggered prefix，并把 entity observer 作为 attachment。

Fast DDS 则更直接：

~~~text
Condition
→ ConditionNotifier
→ WaitSetImpl
→ condition_variable
~~~

两者语义相同，内部 ready-set 数据结构不同。

## WaitSet 是 level-triggered 思维，而不是消息队列

Condition 的 trigger value 表示“当前条件是否成立”。WaitSet 被唤醒后仍要重新扫描
attached Condition，而不是假设一次 notify 就严格对应一个事件。

这与 condition_variable 的典型用法一致：

~~~cpp
cond_.wait(lock, predicate);
~~~

predicate 才是事实来源，notify 只是提示“状态可能变化”。因此即使出现 spurious wakeup，
WaitSet 也会重新检查条件，而不会凭一次唤醒伪造数据。

## Listener 与 WaitSet 是两种不同的 execution ownership

~~~text
Listener:
middleware thread enters user callback

WaitSet:
middleware only changes condition + notify
application thread wakes and handles work
~~~

这对机器人程序非常关键。Listener callback 如果直接执行重计算、锁住业务 mutex 或
阻塞 I/O，就可能拖慢 receiver/status 路径；WaitSet 则把业务工作留在应用自己控制的
线程里。

## Notifier 为什么比轮询更重要

朴素实现可以每 1 ms 扫描所有 Reader：

~~~text
while running:
    for condition in all_conditions:
        check()
    sleep(1ms)
~~~

它会产生固定 CPU 开销，并把唤醒延迟量化到 polling period。ConditionNotifier 让状态
变化主动唤醒 WaitSet，只在真正发生变化时竞争 mutex/condition_variable。

## rmw_wait / ROS 2 Executor 在这条链的后半段

使用 rmw_fastrtps 时，可以把回调延迟拆成：

~~~text
network receive
→ ReaderHistory becomes ready
→ DDS condition/status
→ Fast DDS WaitSet / rmw_wait
→ ROS 2 Executor chooses callback
→ user callback
~~~

调 Executor 线程数和 callback group 只能改变后半段；如果前面的 fragment reassembly
或 ReaderHistory 尚未 ready，Executor 无法提前执行。

## 一个容易出现的锁反转风险

Listener 模式下，如果 middleware callback 持有内部路径需要的锁，而用户 callback 又
拿业务锁；另一个业务线程反过来持业务锁调用 DDS API，就可能形成锁顺序冲突。

因此 Listener callback 更适合做短小的状态转移或投递，不适合承载复杂控制算法。
需要明确线程归属的机器人程序通常更容易用 WaitSet/Executor 建立稳定的执行边界。

## 关闭 WaitSet 也需要唤醒睡眠线程

任何 shutdown 设计都必须考虑：如果应用线程正在无限 wait，谁负责改变 condition 或
GuardCondition 并 notify，使它能够退出。只设置一个 running=false 而不唤醒
condition_variable，会得到经典的“退出标志已改但线程永远睡着”问题。
