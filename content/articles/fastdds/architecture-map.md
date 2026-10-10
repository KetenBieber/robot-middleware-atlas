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

## 一张更准确的 ownership 图

DDS 对象树和 RTPS 运行时树不是一棵树。Publisher/Subscriber 属于
DomainParticipantImpl；RTPSParticipantImpl 是协议运行时根。二者通过
DataWriterImpl/DataReaderImpl 里的 low-level endpoint 指针连接：

~~~text
DomainParticipantFactory
└─ DomainParticipantImpl
   ├─ PublisherImpl
   │  └─ DataWriterImpl
   │     ├─ WriterHistory
   │     └─ writer_ ─────────────┐
   ├─ SubscriberImpl             │
   │  └─ DataReaderImpl          │
   │     ├─ ReaderHistory        │
   │     └─ reader_ ────────┐    │
   └─ RTPSParticipant ──────┼────┼─> RTPSParticipantImpl
                            │    │   ├─ BuiltinProtocols
                            │    │   │  ├─ PDP / EDP / WLP
                            │    │   ├─ StatefulReader / Writer
                            │    │   ├─ ReaderProxy / WriterProxy
                            │    │   ├─ FlowController
                            │    │   ├─ ResourceEvent
                            │    │   └─ NetworkFactory / ReceiverResource
                            │    │
                            └────┴── protocol endpoint handles
~~~

这个区分很重要：删除 Publisher 并不等于立刻销毁整个 RTPS Participant；
删除一个 RTPS Writer 也不应越权销毁 DDS Topic。理解两棵对象树之间的桥，
才能分析真正的 shutdown 顺序。

## 同一份样本如何穿过四层

发送端从 API 到字节流：

~~~text
DataWriter
  ↓
DataWriterImpl
  ↓
CacheChange_t + WriterHistory
  ↓
StatefulWriter / StatelessWriter
  ↓
RTPSMessageGroup
  ↓
NetworkFactory
  ↓
UDP / TCP / SharedMemTransport
~~~

接收端则反向补回协议与 DDS 语义：

~~~text
ReceiverResource
  ↓
MessageReceiver
  ↓
StatefulReader / StatelessReader
  ↓
WriterProxy + fragment/reorder state
  ↓
ReaderHistory
  ↓
DataReaderImpl
  ↓
Listener / Condition / take
~~~

因此 Fast DDS 的关键不是“有很多类”，而是每一层只负责一种约束：

| 层 | 主要问题 | 典型状态 |
| --- | --- | --- |
| DDS façade | 用户语义 | Topic、QoS、Status |
| Impl | DDS 与 RTPS 适配 | TypeSupport、payload pool、timer |
| RTPS endpoint | 协议状态机 | sequence、ReaderProxy、WriterProxy |
| History | 样本生命周期 | CacheChange、resource limits |
| Transport | 字节传输 | locator、receiver、socket/shared memory |

## 控制面和数据面在哪里汇合

PDP/EDP 先建立远端 endpoint 的代理对象；匹配完成以后，代理对象直接进入
StatefulWriter/StatefulReader 的可靠性状态机。也就是说 Discovery 的最终产物
不是一条“发现成功”的布尔值，而是数据面可使用的 ReaderProxy/WriterProxy。

~~~text
PDP / EDP
  ↓
remote endpoint metadata
  ↓
QoS compatibility
  ↓
ReaderProxy / WriterProxy
  ↓
DATA / HEARTBEAT / ACKNACK
~~~

## 为什么这一套对象分解适合机器人系统

机器人中间件同时面对小而急的控制消息和大而连续的感知数据。如果把它们都
压成“socket + callback”，无法分别回答延迟、内存和可靠性问题。Fast DDS 的
对象边界使这些问题可以分别定位：

- 控制命令尾延迟：先看 write、History、FlowController 与 publish mode；
- 点云内存：看 payload pool、fragment、History depth、Data Sharing；
- 启动抖动：看 PDP/EDP 与 endpoint matching；
- callback 延迟：看 receiver、ReaderHistory、WaitSet/Listener；
- 关闭卡顿：看 transport shutdown、TimedEvent 与 endpoint quiescence。

后续文章沿这张运行时地图逐层展开，而不是按源码目录顺序阅读。
