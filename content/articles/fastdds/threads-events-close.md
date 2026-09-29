# ResourceEvent、ReceiverResource 与关闭顺序：Fast DDS 的线程到底在做什么

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## Fast DDS 不是“每个 Writer 一个线程”

主要执行上下文包括：

~~~text
application threads
transport receiver threads
ResourceEvent thread
FlowController async threads
listener callback context
security / persistence related workers
~~~

真正线程数取决于 transport、publish mode 和配置。

## ResourceEvent 集中管理 TimedEvent

源码注释非常明确：

> This class centralizes all operations over timed events in the same thread.

ResourceEvent 构造：

~~~cpp
ResourceEvent::ResourceEvent()
  : thread_(
      new eprosima::thread())
{
}
~~~

它维护 pending_timers_、active_timers_、timers_count_、condition variables 与 steady clock current_time。

periodic heartbeat、NACK response、deadline、liveliness 等 TimedEvent 都可以共享这类 event resource。

## 为什么 Active Timers 要排序

固定源码：

~~~cpp
std::sort(
    active_timers_.begin(),
    active_timers_.end(),
    event_compare);
~~~

插入时使用 lower_bound 按 next_trigger_time 排序。

这样 event thread 只需要等待最早 timer，而不是为每个 endpoint 单独起 timer thread。

这是典型的：

~~~text
many logical timers
→ one scheduling thread
~~~

设计。

## Receive Thread 来自 Transport ReceiverResource

RTPSParticipantImpl 创建各 locator 的 ReceiverResource。

启用 Participant 后：

~~~cpp
for (auto& receiver :
     m_receiverResourcelist)
{
    receiver.Receiver
      ->RegisterReceiver(
          receiver.mp_receiver);
}
~~~

Transport 收到 bytes 后进入 MessageReceiver，再分发给 endpoint。

所以网络 packet parsing 与 application WaitSet thread 是完全不同执行上下文。

## FlowController Async Thread 是另一条执行线

异步 Writer 的 pending CacheChange 被 FlowController 调度。

这意味着：

~~~text
application thread
负责创建/commit CacheChange

async flow thread
负责选择 pending changes
并真正形成/send RTPS traffic
~~~

如果 FlowController backlog 增大，publish 调用可能仍快，但 data age 会增加。

## 关闭先关 Transport 再销毁 MessageReceiver

RTPSParticipantImpl teardown：

~~~cpp
m_network_Factory.Shutdown();
~~~

然后：

~~~cpp
for (auto& block :
     m_receiverResourcelist)
{
    block.Receiver
      ->UnregisterReceiver(
          block.mp_receiver);

    block.disable();
}
~~~

之后才销毁 security、MessageReceiver 与 participant resources。

这个顺序就是为了保证：

~~~text
不再有新 network callback
→ 等 receiver thread 退出
→ 才释放 callback target
~~~

## disableReader 为什么先从所有 MessageReceiver 移除 Endpoint

固定源码注释甚至点明：

~~~text
Avoid to receive PDPSimple reader
a DATA while calling ~PDPSimple
and EDP was destroy already.
~~~

disableReader() 会持 m_receiverResourcelistMutex，把 Reader 从每个 MessageReceiver removeEndpoint。

这是一个非常具体的 use-after-free 防线。

## 对 ROS 2 Executor 的意义

ROS 2 callback 延迟可拆成：

~~~text
network receiver
→ Fast DDS ReaderHistory
→ StatusCondition
→ rmw_wait / WaitSet
→ Executor wakes
→ callback scheduled
~~~

调 Executor priority 只能影响最后两段。

如果前面 FlowController、network receive、reassembly 或 History 已经排队，Executor 再快也拿不到尚未 ready 的 sample。

## 实时调度需要明确线程表

部署时至少记录：

| Thread | 工作 | 是否可阻塞 | 优先级/CPU |
| --- | --- | --- | --- |
| Control | controller compute | 应尽量否 | isolated |
| DDS write adapter | serialize/history | 可能 | measured |
| Async flow | network send | 是 | separate |
| Receiver | packet parse | 是 | network CPU |
| ResourceEvent | heartbeat/timers | 短任务 | bounded |
| Executor | callback | 取决于业务 | configured |

没有这张表，“Fast DDS 是实时 DDS”对系统设计没有足够信息。
