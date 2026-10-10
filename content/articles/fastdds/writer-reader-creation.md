# DataWriter / DataReader 创建：Fast DDS 怎样把 Topic、TypeSupport、History 与 RTPS Endpoint 装配起来

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## PublisherImpl 只是第一道门

创建 Writer 时：

~~~cpp
DataWriterImpl* impl =
    create_datawriter_impl(
        type_support,
        topic,
        qos,
        listener,
        payload_pool);

return create_datawriter(
    topic,
    impl,
    mask);
~~~

create_datawriter_impl() 实际就是：

~~~cpp
return new DataWriterImpl(
    this,
    type,
    topic,
    qos,
    listener,
    payload_pool);
~~~

真正复杂的资源装配进入 DataWriterImpl 构造/enable。

## Writer 创建阶段要解决五件事

~~~text
1. TypeSupport / serialization
2. PayloadPool
3. WriterHistory
4. RTPSWriter attributes
5. Discovery-visible WriterProxyData
~~~

这几件事如果放到每次 write 才做，发送热路径会非常昂贵。

所以 endpoint creation 本质上是配置编译。

## 为什么 DataWriterImpl 同时知道 DDS 与 RTPS

DataWriterImpl 保存 DDS QoS、Topic、Listener；同时内部 writer_ 指向 RTPSWriter。

它是两层语义的桥：

~~~text
DDS:
DataWriterQos
Topic
TypeSupport
Status

RTPS:
WriterAttributes
WriterHistory
StatefulWriter / StatelessWriter
Locator / FlowController
~~~

## Reader 对称地装配 ReaderHistory

SubscriberImpl：

~~~cpp
DataReaderImpl* impl =
    create_datareader_impl(
        type_support,
        topic,
        qos,
        listener,
        payload_pool);
~~~

DataReaderImpl 最终创建 RTPSReader，并让 ReaderHistory 与 low-level reader 绑定。

## PayloadPool 为什么是可注入对象

create_datawriter() / create_datareader() 接口允许传：

~~~cpp
std::shared_ptr<
    fastdds::rtps::IPayloadPool>
    payload_pool
~~~

这非常重要。

Payload 的分配策略并没有硬编码成 malloc/free。普通 RTPS、Data Sharing、用户自定义 pool 都可以通过统一接口改变 payload ownership。

因此 Fast DDS 的零拷贝能力不是后期 hack，而是对象模型一开始就给 payload storage 留了抽象边界。

## WriterHistory 与 RTPSWriter 为什么必须绑定 Mutex

History 必须在：

- application write；
- reliability ACK/NACK；
- async sender；
- lifespan/deadline；
- dispose/remove；

之间保持一致。

WriterHistory 保存 mp_writer 与 mp_mutex，remove/change 等操作都依赖 low-level writer。

这让 History 不只是一个 passive container，而是 Writer protocol state 的一部分。

## Stateful 与 Stateless 如何选择

Best Effort 不需要为每个 remote Reader 保存确认进度，可以使用更轻的 stateless 逻辑。

Reliable 则需要：

~~~text
remote ReaderProxy[]
per-reader ACK state
heartbeat
NACK response
unsent / requested changes
~~~

因此会落到 StatefulWriter。

Reader 同理：Reliable Reader 需要 WriterProxy 去跟踪远端 Writer sequence 空洞。

## endpoint enable 之后才真正参与 discovery

DDS 对象“new 出来”与“网络上可见”是两回事。

enable 完成之后，RTPS endpoint 才进入 Participant 的 builtin discovery / matching 体系。

这也是为什么 QoS、Type 与 resource limits 应在 enable 之前尽可能确定。

## 创建阶段本质上是在“编译配置”

Writer/Reader 创建时做的工作，可以理解为把高层 QoS 与类型信息编译成几组运行时
对象：

~~~text
Topic + TypeSupport
       ↓
serialization contract

HistoryQos + ResourceLimits
       ↓
HistoryAttributes + pools

Reliability + PublishMode
       ↓
Stateful/Stateless endpoint + FlowController relation

Locator + Transport config
       ↓
RTPS endpoint routing capability
~~~

把这些决策前移到创建阶段的意义，是让 hot path 少做重复结构性判断；代价是某些
QoS 在 enable 后不能随意修改。

## IPayloadPool 是内存策略的插槽

IPayloadPool 把“样本的协议对象”与“payload 到底存在哪里”解耦。普通序列化、
Data Sharing、用户提供的 pool 都可以复用 CacheChange/History 这一套协议状态机。

~~~text
CacheChange_t
  └─ SerializedPayload_t
       └─ payload_owner -> IPayloadPool
~~~

这里最重要的不是多态本身，而是 release 的责任有明确归属。History 删除一个
CacheChange 时，不必知道 payload 来自 heap、共享内存还是特殊 allocator。

## Writer/Reader 创建时为什么要知道资源上界

如果 endpoint 直到运行时才发现“最多允许多少 samples / instances / matched peers”，
就无法对内存和锁等待做任何可靠预算。Fast DDS 把 resource limits、History depth、
proxy 容量等信息提前进入对象配置，就是为了让容量成为协议运行时的一部分。

对机器人控制链，典型目标不是“能缓存越多越好”，而是：

~~~text
small bounded History
+ predictable pool
+ explicit overwrite/drop policy
+ bounded matched endpoints
~~~

感知链则可能接受更大的 pool，以换取点云/图像突发时不立刻失败。

## enable 之后，配置开始产生外部可观察状态

Writer/Reader enable 后进入 EDP，远端开始看到它的 Topic、Type、QoS 和 locator，
并可能立刻建立 ReaderProxy/WriterProxy。此后修改会牵涉：

- discovery database；
- 已匹配远端 endpoint；
- History 与 reliability state；
- timers；
- Data Sharing compatibility。

因此“创建完成”不是 C++ new 返回，而是对象、资源和协议状态都建立后进入一个可被
远端观察的稳定状态。

## 一个创建失败的反例

假设控制模块创建 Writer 时配置 KEEP_ALL，却把资源上限设得很小；创建本身可能成功，
但运行后可靠 Reader 暂时不 ACK，History 很快占满。此时 write() 才暴露 TIMEOUT 或
OUT_OF_RESOURCES，问题表面像“网络不稳定”，根因其实是创建阶段的资源模型与运行负载
不匹配。

所以 Endpoint creation 不是样板代码，而是系统容量设计。
