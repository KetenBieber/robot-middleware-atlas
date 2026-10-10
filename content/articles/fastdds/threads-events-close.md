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

## 一张完整 execution graph

Fast DDS 运行时更适合画成执行上下文图，而不是“DDS 有几个线程”：

~~~text
Application write thread
   └─ serialize / History commit
            │
            ├─ sync publish ──> Transport send
            │
            └─ async publish ─> FlowController thread ─> Transport

Transport receiver thread
   └─ MessageReceiver
        └─ StatefulReader
             └─ ReaderHistory
                  ├─ Listener callback
                  └─ Condition notify
                         └─ application WaitSet thread

ResourceEvent thread
   └─ heartbeat / nack / deadline / liveliness timers
~~~

同一个 Topic 的端到端延迟可能跨越四种线程，因此不能只给 ROS 2 Executor 提升优先级
就宣称完成实时调度。

## TimedEvent 集中化的收益与代价

把很多逻辑 timer 放进一个 ResourceEvent thread，避免“一个 timer 一个线程”的巨大
上下文切换成本，也让下一触发时间可以统一排序。代价是：某个 timer callback 如果做
过重工作，会延迟同线程上的其他 timer。

因此 heartbeat/nack/deadline callback 应保持短小，把长任务留给其他执行上下文。

## shutdown 是逆向依赖图

创建顺序大致是：

~~~text
Participant
→ transports/receivers/events
→ builtin protocols
→ user endpoints
→ histories/pools
~~~

关闭则必须优先停止会继续“主动执行”的来源：

~~~text
stop new application work
→ stop/detach endpoint protocol activity
→ shutdown network ingress
→ disable/join receiver work
→ cancel timed/async work
→ release histories/pools/listeners
→ destroy participant
~~~

真正目标不是机械反序，而是确保每一层释放前，其潜在 caller 已经 quiescent。

## 一个典型 use-after-free 时序

错误顺序：

~~~text
t0 destroy EDP target
t1 receiver thread already has packet
t2 MessageReceiver dispatches DATA to old endpoint pointer
t3 callback enters freed object
~~~

disableReader/removeEndpoint 这类步骤正是在 t0 前切断 t2 的 dispatch route。关闭协议
本身就是内存安全设计的一部分。

## 线程优先级必须和数据年龄一起设计

给 receiver thread 很高优先级，如果应用消费跟不上，只会更快地把数据堆进 History；
给 FlowController 很高优先级，如果 WriterHistory 已经积累旧命令，也可能更快发送过时
数据。实时系统的目标不是“线程越快越好”，而是从产生到消费的 data age 有界。

建议把监控指标和线程对应起来：

| 执行上下文 | 最值得监控 |
| --- | --- |
| write thread | publish WCET、timeout |
| FlowController | pending age、bytes/period |
| receiver | packet/fragment backlog |
| ResourceEvent | timer lateness |
| WaitSet/Executor | wake-to-callback delay |

这样才能定位抖动到底来自协议、I/O 还是业务调度。
