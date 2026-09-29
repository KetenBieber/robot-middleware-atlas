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
