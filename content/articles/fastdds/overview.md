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


## Runtime 视角下的三个核心边界

Fast DDS 的源码复杂度主要来自三个边界：

~~~text
Application boundary
        |
        v
DDS Entity boundary
        |
        v
RTPS protocol boundary
        |
        v
Transport boundary
~~~

其中：

- DataWriter/DataReader 负责 DDS 语义；
- StatefulWriter/Reader 负责 RTPS 协议状态；
- History/CacheChange 负责数据生命周期；
- Transport 负责跨进程或跨机器传输。

因此分析任何 Fast DDS 问题时，应先定位它属于哪个边界，而不是直接从 API 入口向下追所有调用。

## 与机器人系统的对应关系

对于机器人场景，可以把不同数据流映射到不同 QoS 与运行路径：

~~~text
control command
    -> small payload
    -> low latency
    -> bounded history

camera / point cloud
    -> large payload
    -> SHM/DataSharing preferred
    -> freshness over retransmission

state estimation
    -> reliable when loss is unacceptable
    -> deadline monitoring important
~~~

Fast DDS 的工程价值不只是实现 DDS 标准，而是提供了一组可以调节实时性、可靠性、带宽和内存占用之间权衡的运行时机制。

## 从 ROS 2 publish 到远端 callback 的完整闭环

把前面的对象拼起来，一次 ROS 2 消息可以按执行上下文重放：

~~~text
rclcpp publisher thread
  ↓
rmw_fastrtps
  ↓
DataWriterImpl
  ↓ serialize/loan
CacheChange + WriterHistory
  ↓
StatefulWriter + ReaderProxy
  ↓
FlowController or synchronous send
  ↓
NetworkFactory / Transport
=========================== process/host boundary
ReceiverResource
  ↓
MessageReceiver
  ↓
StatefulReader + WriterProxy
  ↓
ReaderHistory
  ↓
StatusCondition / Listener
  ↓
Fast DDS WaitSet
  ↓
rmw_wait
  ↓
ROS 2 Executor
  ↓
user callback
~~~

这条链里没有一个单独的“DDS 线程”负责所有事情。应用 write thread、异步发送线程、
transport receiver、ResourceEvent timer thread、RMW/Executor thread 分别承担不同阶段。

## 一条消息为什么可能同时占用多个状态容器

同一个 sample 在不同阶段会同时被不同结构描述：

~~~text
application object
→ SerializedPayload_t
→ CacheChange_t
→ WriterHistory
→ ReaderProxy delivery state
→ RTPS fragment
→ WriterProxy receive state
→ ReaderHistory
→ DataReader sample
~~~

这不是重复设计。每个结构回答的问题不同：payload 回答“字节在哪里”，CacheChange
回答“这是哪个序列样本”，Proxy 回答“远端知道到哪里”，History 回答“什么时候还能
重传/读取”。

## Runtime 设计时最重要的五个问题

面对任何 Fast DDS 性能或正确性问题，优先回答：

1. 当前样本由谁拥有，什么时候能回收？
2. 当前代码运行在哪个线程，是否可能阻塞？
3. 当前队列/History 的容量是多少，过载策略是什么？
4. Reliable 状态是否仍然要求保留旧样本？
5. 当前延迟是 processing latency 还是 data age？

最后一个尤其重要。异步发送可能让 write() 很快返回，但消息在 FlowController 中已经
变旧；Executor callback 很快，也不能挽救此前在 reassembly/History 中积压的旧数据。

## 运行时机制之间的依赖关系

这些机制按运行时依赖可以连成：

~~~text
architecture-map
→ participant-endpoint-lifecycle
→ writer-reader-creation
→ write-cachechange
→ writerhistory-reliability
→ readerhistory-fragments
→ discovery-pdp-edp / discovery-server
→ qos-matching
→ transport-network
→ flowcontroller-async
→ datasharing-vs-shm / loan-zero-copy
→ waitset-listener
→ threads-events-close
→ rmw_fastrtps case
→ Fast DDS vs Cyclone DDS
~~~

这样每出现一个新对象时，前置的 ownership、协议和线程语义都已经建立。
