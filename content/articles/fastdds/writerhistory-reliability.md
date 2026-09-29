# WriterHistory 与 StatefulWriter：Reliable 为什么需要 ReaderProxy、Heartbeat 与 TimedEvent

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## History Commit 后发生什么

WriterHistory 插入新 CacheChange 后，会通知绑定 Writer：

~~~cpp
void WriterHistory::notify_writer(
        CacheChange_t* a_change,
        const time_point& max_blocking_time)
{
    mp_writer
      ->unsent_change_added_to_history(
          a_change,
          max_blocking_time);
}
~~~

History 因而不是数据终点，而是 Writer protocol state 的入口。

## StatefulWriter 为什么必须为每个 Reader 建 Proxy

源码注释直接写：

> StatefulWriter maintains information of each matched Reader.

对象图：

~~~text
StatefulWriter
├─ WriterHistory
├─ ReaderProxy A
├─ ReaderProxy B
├─ ReaderProxy C
├─ periodic heartbeat event
├─ nack response event
└─ FlowController
~~~

每个 Reader 的 ACK 进度不同，不可能只用一个全局 sequence pointer。

## ReaderProxy 保存的不是 Reader 对象

它保存的是远端 Reader 的**本地协议镜像**：

- GUID；
- locator；
- requested/unsent changes；
- ACK state；
- NACK suppression；
- reliability timer state。

这和 Cyclone DDS 的 proxy_reader 概念本质相同，但 Fast DDS 用显式 C++ ReaderProxy 类表达。

## 新 Change 进入 Writer 后先处理 Data Sharing

StatefulWriter::unsent_change_added_to_history() 一开始锁 writer mutex，然后：

~~~cpp
if (is_datasharing_compatible())
{
    prepare_datasharing_delivery(
        change);
}
~~~

也就是说 Data Sharing 与网络可靠性不是后期平行模块，而是在一个 CacheChange 进入 writer 时共同决定后续 delivery。

## Heartbeat 为什么是 TimedEvent

StatefulWriter::init() 创建：

~~~cpp
periodic_hb_event_ =
    new TimedEvent(
      pimpl->getEventResource(),
      [&]() -> bool
      {
          return send_periodic_heartbeat();
      },
      times_.heartbeat_period);
~~~

并创建 nack_response_event_。

所以 Reliable 的时间行为不是 application thread 定时轮询，而是 Participant 共享 EventResource 驱动 TimedEvent。

## Heartbeat 与 ACKNACK 的第一性原理

Writer 发：

~~~text
DATA #100
DATA #101
DATA #102
HEARTBEAT first=100 last=102
~~~

Reader 发现 #101 缺失：

~~~text
ACKNACK bitmap:
100 received
101 missing
102 received
~~~

Writer 收到以后，ReaderProxy 记录请求状态，再从 WriterHistory 找到 #101 重发。

因此：

~~~text
WriterHistory
= 可重传 payload

ReaderProxy
= 谁还需要哪些 sequence

TimedEvent
= 什么时候主动宣告 / 响应
~~~

三者缺一不可。

## remove change 为什么也要协议判断

WriterHistory::remove_min_change() 不能只 pop_front。

Reliable Reader 若还没确认，旧 CacheChange 就可能仍然需要重传。

所以真正可释放的 change 取决于：

- ReaderProxy ACK state；
- durability；
- history QoS；
- resource limits；
- disable positive ACK 等策略。

## Fast DDS 对内存策略更显式

HistoryAttributes 带 memoryPolicy。固定源码中还能看到：

~~~cpp
if (m_att.memoryPolicy ==
    PREALLOCATED_MEMORY_MODE)
{
    if (payload.length >
        m_att.payloadMaxSize)
    {
        ...
    }
}
~~~

因此开发者可以在：

~~~text
预分配、可预测上界
vs
动态增长、适应大消息
~~~

之间做明确取舍。

这对机器人实时控制比“用了 reliable 所以会重传”更值得关注。
