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

## 用 copy ledger 区分三条路径

讨论“零拷贝”最可靠的方法，是逐段记录 payload 是否重新构造：

| 路径 | 应用对象→middleware | endpoint 间 | 接收应用 |
| --- | --- | --- | --- |
| UDP/TCP | 通常 serialize/copy | transport bytes | deserialize/read |
| SHM Transport | 通常 serialize/copy | 共享内存搬运 RTPS message | deserialize/read |
| Data Sharing | 取决于 loan/type | 共享 history/payload | 可进一步 loan/view |

所以 SHM Transport 省掉的是 kernel/network transport path；Data Sharing 省掉的是一部分
RTPS endpoint 间的数据搬运。二者不能用同一个“zero-copy=yes/no”字段概括。

## Data Sharing 需要 Writer/Reader 兼容

Data Sharing 是 endpoint 级决策：只有同机且双方配置、类型与资源条件满足时才能走
shared history。一个 Writer 同时面对本机 Reader 和远端 Reader 时，仍可能同时维护：

~~~text
local Data Sharing delivery
+ remote RTPS transport delivery
~~~

因此启用 Data Sharing 并不意味着整个 Writer 可以删除 RTPS/History/reliability 状态。

## 为什么共享内存仍然需要生命周期协议

共享地址空间只能解决“在哪里放 bytes”，不能回答：

- Writer 什么时候可以复用 slot；
- Reader 什么时候读完；
- 进程异常退出如何回收；
- Reliable ACK 什么时候成立；
- History depth 满时覆盖谁。

DataSharingPayloadPool、WriterPool/ReaderPool 与 shared history 就是在解决这些生命周期
问题。真正困难的是 ownership，不是 mmap 本身。

## 混合拓扑是最容易误判的场景

例如一台机器人上相机节点同时被本机感知算法和远端调试机订阅：

~~~text
Camera Writer
  ├─ local Reader  -> Data Sharing
  └─ remote Reader -> UDP/TCP RTPS
~~~

此时 payload 的可回收时机受两条 delivery 语义共同影响。只测本机 subscriber 得到的
“零拷贝性能”不能代表远端 Reader 存在时的真实系统成本。

## 加密与数据表示也可能让 fast path 失效

一旦安全插件、representation 转换、类型不满足共享约束或 RMW 层主动复制，某一段
copy 可能重新出现。部署前应画出实际 copy ledger，而不是根据配置文件里出现
SharedMemTransportDescriptor 就下结论。

## 机器人系统的选择顺序

建议先按拓扑回答：

~~~text
同进程？
  → 优先考虑上层 intraprocess

同主机跨进程？
  → 评估 Data Sharing / SHM Transport

跨主机？
  → RTPS transport
~~~

再按类型与生命周期检查 loan、serialization、History、可靠性和安全要求。这样才能
知道优化真正发生在哪一段。
