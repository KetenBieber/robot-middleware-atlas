# eProsima Fast DDS 总览：从 ROS 2 publish 一路追到 RTPS Writer

固定源码版本：Fast DDS v3.6.2，commit 39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## Fast DDS 真正复杂在哪里

如果只看 DDS API，Fast DDS 很像一组熟悉的对象：

~~~text
DomainParticipant
├─ Publisher
│  └─ DataWriter
└─ Subscriber
   └─ DataReader
~~~

但真正决定一条机器人消息延迟、缓存、可靠性与内存上界的，是更深一层的对象：

~~~text
DataWriter
  ↓
DataWriterImpl
  ↓
CacheChange_t
  ↓
WriterHistory
  ↓
StatefulWriter / StatelessWriter
  ↓
ReaderProxy / FlowController
  ↓
RTPSMessageGroup
  ↓
Transport
  ↓
UDP / TCP / SHM

远端：
Transport Receiver
  ↓
MessageReceiver
  ↓
StatefulReader / StatelessReader
  ↓
WriterProxy
  ↓
ReaderHistory
  ↓
DataReaderImpl
~~~

这里已经能看出 Fast DDS 与 Cyclone DDS 的实现风格差别：Cyclone DDS 把 API 层和 DDSI/RTPS 层明确分成 DDSc / DDSI；Fast DDS 则更直接地以 C++ 类图把 DDS Entity、RTPS Endpoint、History、Proxy 与 Transport 连起来。

## 本专题不重复 DDS 教科书

前一个 Cyclone DDS 专题已经解释了 SPDP、SEDP、Reliable、WHC/RHC、WaitSet 等 DDS/RTPS 基本机制。这里重点回答：

1. Fast DDS 用什么类和数据结构承载同样的协议语义？
2. DataWriter::write 到底在哪些地方可能分配、加锁、阻塞？
3. WriterHistory 与 ReaderHistory 为什么围绕 CacheChange_t 设计？
4. StatefulWriter 为什么必须维护每一个 ReaderProxy？
5. Discovery Server 如何改变“所有 Participant 互相发现”的拓扑？
6. Flow Controller 与 asynchronous publish 如何控制真正发送时机？
7. SHM Transport、Data Sharing、loan_sample 三者到底是不是一回事？
8. ROS 2 rmw_fastrtps 怎样把 Publisher、QoS 与 rmw_wait 映射到底层 Fast DDS？

## 最关键的写路径事实

固定源码中 DataWriterImpl::write() 只是入口：

~~~cpp
ReturnCode_t DataWriterImpl::write(
        const void* const data)
{
    if (writer_ == nullptr)
    {
        return RETCODE_NOT_ENABLED;
    }

    return create_new_change(ALIVE, data);
}
~~~

真正重量级的是 perform_create_new_change()。它会：

~~~text
获取 low-level writer mutex
→ 检查这是不是 loaned sample
→ 计算序列化大小
→ 从 payload pool 申请存储
→ serialize
→ 从 History 创建 CacheChange_t
→ 把 payload 移进 CacheChange
→ add_pub_change()
→ 更新 Deadline / Lifespan TimedEvent
~~~

因此“DDS write 只是把指针扔进后台线程”在 Fast DDS 里同样不是一个安全假设。

## CacheChange 是整个运行时的共同货币

Fast DDS 的很多核心类都围绕 CacheChange_t 工作：

~~~text
DataWriterImpl
    ↓ create
CacheChange_t
    ↓
WriterHistory
    ↓
StatefulWriter
    ↓
RTPS serialization / transport
    ↓
StatefulReader
    ↓
ReaderHistory
~~~

它包含 sequence number、writer GUID、serialized payload、fragment state、instance handle 等协议与数据生命周期信息。

所以后面读源码时，不要把 History 理解成 std::queue<message>。History 保存的是“RTPS 可跟踪、可重传、可分片、可按实例管理”的 CacheChange。

## Discovery 为什么值得单独拆

Fast DDS 有两条非常有代表性的发现路径：

~~~text
Simple Discovery
PDP Simple + EDP Simple

Discovery Server
PDP Client / PDP Server + server-side endpoint discovery
~~~

第二条尤其适合机器人群体系统：它把全互联 discovery traffic 改造成客户端到服务器的发现拓扑，减少 Participant 数量增长后的 discovery fan-out。

## 三种“同机优化”不要混在一起

Fast DDS 同时有：

~~~text
SHM Transport
Data Sharing
loan_sample
~~~

它们解决的是不同层级的问题。

SHM Transport 仍然是 Transport 层，把 RTPS message 通过共享内存传输；Data Sharing 则让 Writer/Reader 共享 history/payload pool，绕过常规 RTPS transport 数据路径；loan_sample 再进一步允许应用直接从 Writer 能管理的 pool 借出 sample memory。

判断这些机制是否真正降低成本，必须分别检查它们省掉的是哪一次 copy，以及 payload 在哪些条件下仍然需要 serialization。

## 一份消息的完整路径

用一份消息贯穿对象关系：

~~~text
Participant / Endpoint ownership
→ PDP / EDP discovery
→ QoS / matching
→ DataWriter / DataReader creation
→ DataWriter::write
→ CacheChange / WriterHistory
→ StatefulWriter reliability
→ Transport / MessageReceiver
→ StatefulReader / ReaderHistory
→ FlowController / async
→ Data Sharing / SHM / loan
→ WaitSet / Listener
→ ROS 2 rmw_fastrtps
~~~

专题结尾再和 Cyclone DDS 做对象与数据结构对照，而不是给两者打分。
