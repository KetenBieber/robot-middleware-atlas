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
