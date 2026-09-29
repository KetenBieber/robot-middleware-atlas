# Fast DDS 架构地图：DDS Entity、RTPS Endpoint、History 与 Transport 是四层对象

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## 第一层：DDS façade

应用首先接触：

~~~text
DomainParticipantFactory
  ↓
DomainParticipant
  ├─ Publisher
  │  └─ DataWriter
  └─ Subscriber
     └─ DataReader
~~~

这层负责 DDS 标准语义：Topic、TypeSupport、QoS、Listener、Condition、WaitSet、Status。

但 DataWriter 本身并不是最终网络发送者。

## 第二层：Impl 对象

Fast DDS 广泛使用 façade + Impl：

~~~text
DomainParticipant
  ↕
DomainParticipantImpl

DataWriter
  ↕
DataWriterImpl

DataReader
  ↕
DataReaderImpl
~~~

Impl 对象负责真正资源：History、RTPS Endpoint、payload pool、QoS runtime state、deadline/lifespan timer 等。

这和 PImpl 设计思想相似：稳定公共 API 与高速变化的内部实现分开。

## 第三层：RTPS Endpoint

DataWriterImpl 最终关联一个 RTPSWriter：

~~~text
DataWriterImpl
  ↓
RTPSWriter
  ├─ StatefulWriter
  └─ StatelessWriter
~~~

Reader 侧：

~~~text
DataReaderImpl
  ↓
RTPSReader
  ├─ StatefulReader
  └─ StatelessReader
~~~

Stateful 并不是“对象更复杂”这么简单，而是它保存远端 endpoint 的协议状态。

Reliable Writer 需要知道每个 Reader 已确认到哪里，因此 StatefulWriter 维护 ReaderProxy。

Reliable Reader 需要知道每个 Writer 的 sequence / missing / heartbeat 状态，因此 StatefulReader 维护 WriterProxy。

## 第四层：History

Fast DDS 将 payload 生命周期显式放进 History：

~~~text
DataWriterImpl
  └─ WriterHistory
       ├─ CacheChange pool
       ├─ Payload pool
       └─ ordered changes

DataReaderImpl
  └─ ReaderHistory
       └─ received CacheChange_t
~~~

WriterHistory 不是单纯 owned messages。remove 一个 change 时，还要通知 Writer 可靠性状态、释放 payload、维护 sequence 与实例状态。

## Participant 还有一个 RTPSParticipantImpl

DomainParticipantImpl 启用时把 DDS QoS 转成 RTPSParticipantAttributes：

~~~cpp
fastdds::rtps::RTPSParticipantAttributes rtps_attr;
utils::set_attributes_from_qos(rtps_attr, qos_);
rtps_attr.participantID = participant_id_;

RTPSParticipant* part =
    RTPSDomain::createParticipant(
        domain_id_,
        false,
        rtps_attr,
        &rtps_listener_);
~~~

所以对象图继续向下：

~~~text
DomainParticipantImpl
    ↓
RTPSParticipant
    ↓
RTPSParticipantImpl
    ├─ BuiltinProtocols
    │  ├─ PDP
    │  ├─ EDP
    │  └─ WLP
    ├─ NetworkFactory
    ├─ Reader/Writer endpoints
    ├─ ResourceEvent / TimedEvent
    └─ FlowController
~~~

RTPSParticipantImpl 才是协议运行时的 ownership root。

## BuiltinProtocols 为什么独立存在

普通业务 Writer/Reader 发送用户 Topic。

Discovery 与 Liveliness 自己也需要 RTPS endpoint：

~~~text
PDP
Participant discovery

EDP
Publication / Subscription discovery

WLP
Writer Liveliness Protocol
~~~

把 builtin protocol 与 user endpoint 分开，可以让同一 Participant 内复用 transport、event resource 与 endpoint infrastructure，而不把 discovery 逻辑硬编码进每个 DataWriter。

## Transport 为什么再隔一层 NetworkFactory

RTPS Endpoint 不应该知道自己到底走 UDPv4、UDPv6、TCP、SHM 还是自定义 transport。

因此：

~~~text
RTPS endpoint
→ locator
→ NetworkFactory
→ TransportInterface
→ concrete transport
~~~

这个抽象和 YARP Carrier、eCAL transport gate、Cyclone DDS ddsi_tran 有同一个设计动机：协议层表达“我要把一组字节送到 locator”，系统层决定具体 I/O 实现。

## 一张 ownership 图

~~~text
DomainParticipantFactory
└─ DomainParticipantImpl
   └─ RTPSParticipantImpl
      ├─ BuiltinProtocols
      │  ├─ PDP
      │  └─ EDP
      ├─ PublisherImpl
      │  └─ DataWriterImpl
      │     ├─ WriterHistory
      │     └─ StatefulWriter
      │        └─ ReaderProxy[]
      ├─ SubscriberImpl
      │  └─ DataReaderImpl
      │     ├─ ReaderHistory
      │     └─ StatefulReader
      │        └─ WriterProxy[]
      ├─ FlowController
      ├─ Event resources
      └─ NetworkFactory / transports
~~~

后续文章就沿这张图逐层展开。
