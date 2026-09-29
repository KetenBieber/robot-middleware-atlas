# Data Sharing 与 SHM Transport：同样叫共享内存，为什么是两条完全不同的数据路径

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

这是 Fast DDS 最容易被讲错的一块。

## SHM Transport 仍然是 Transport

官方 delivery_mechanisms 示例在 SHM 模式下：

~~~cpp
auto shm_transport =
    std::make_shared<
      SharedMemTransportDescriptor>();

shm_transport->segment_size(
    shm_transport
      ->max_message_size()
      * max_samples);

pqos.transport()
    .user_transports
    .push_back(shm_transport);
~~~

这里改变的是 Participant 的 transport 列表。

数据模型仍是：

~~~text
CacheChange
→ RTPS message
→ SHM Transport
→ remote ReceiverResource
→ MessageReceiver
→ Reader
~~~

所以 SHM Transport 可以理解为“用共享内存替换 UDP/TCP 运送 RTPS packet”。

## Data Sharing 改变的是 History / Payload 所有权

同一个官方示例还单独设置：

~~~cpp
writer_qos
  .data_sharing()
  .automatic();

reader_qos
  .data_sharing()
  .automatic();
~~~

这不是 transport descriptor。

StatefulWriter 在新 change 进入时：

~~~cpp
if (is_datasharing_compatible())
{
    prepare_datasharing_delivery(
        change);
}
~~~

prepare_datasharing_delivery() 会把 change 加入 shared history。

因此 Data Sharing 更接近：

~~~text
Writer shared history/payload
        ↓
Reader directly observes shared change
~~~

而不是重建一份 RTPS packet 再交给 transport。

## 为什么两者都可能使用共享内存

它们只是优化层级不同：

~~~text
SHM Transport
优化 transport copy / kernel path

Data Sharing
优化 endpoint 间 payload/history movement
~~~

名字相似不代表同一个机制。

## 官方示例为什么 DATA_SHARING 仍然配置 SHM Transport

delivery_mechanisms 示例对 SHM 和 DATA_SHARING 都添加 SharedMemTransportDescriptor。

这并不意味着 Data Sharing 必须通过 SHM Transport 发送用户数据。

它保证 Participant 仍然有适合本机通信的 transport/discovery 能力，同时 Data Sharing QoS 决定业务 sample 是否走 shared history fast path。

因此读配置时要分清：

~~~text
Participant transport capability

vs

Writer / Reader data_sharing compatibility
~~~

## DataSharingPayloadPool 的 Release 很特别

固定源码：

~~~cpp
bool
DataSharingPayloadPool::
release_payload(
    SerializedPayload_t& payload)
{
    payload.length = 0;
    payload.pos = 0;
    payload.max_size = 0;
    payload.data = nullptr;
    payload.is_serialized_key = false;
    payload.payload_owner = nullptr;
    return true;
}
~~~

这里 release 更像“解除当前 SerializedPayload view”，真正共享段 ownership 由 WriterPool / ReaderPool 和 descriptor/history 管理。

这和普通 heap payload pool 的 free 语义不同。

## Reliable Data Sharing 仍需要 ACK

StatefulReader 中甚至存在 send_datasharing_ack()。

因为 Transport 可以绕开，DDS reliable sequence 语义不能绕开。

所以 Data Sharing 不等于 Best Effort shortcut。

## 对图像/点云的实际意义

大消息适合先问：

~~~text
同进程？
→ intraprocess

同主机不同进程？
→ Data Sharing / SHM

跨主机？
→ UDP / TCP
~~~

但性能结论还必须测：

- serialize 是否仍发生；
- payload pool 是否真正复用；
- Reader 是否 loan/take；
- 是否同时存在网络 Reader；
- History depth；
- sample 生命周期。

共享内存只是存储位置，不自动保证端到端 zero-copy。
