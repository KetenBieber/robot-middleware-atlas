# dds_write 主链：从 Typed Sample 到 RTPS Packet

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 第一层：API 先锁住 Writer

公开入口很短：

~~~c
dds_return_t dds_write(
  dds_entity_t writer,
  const void *data)
{
  dds_return_t ret;
  dds_writer *wr;

  if (data == NULL)
    return DDS_RETCODE_BAD_PARAMETER;

  if ((ret = dds_writer_lock(
         writer, &wr)) != DDS_RETCODE_OK)
    return ret;

  ret = dds_write_impl(
    wr, data, dds_time(), 0);

  dds_writer_unlock(wr);
  return ret;
}
~~~

Writer handle 必须经过实体生命周期保护；默认 timestamp 也在 API 入口生成。

## Typed Sample 先变成 Serdata

应用结构体不能直接成为 RTPS DATA payload。类型支持会把 sample 转为 ddsi_serdata，其中包含序列化表示、key/status/timestamp 等协议层需要的信息。

~~~text
application struct
-> ddsi_serdata
-> RTPS submessage payload reference
-> socket iovec
~~~

任何 zero-copy 讨论都必须说明是在这条链的哪一段减少拷贝。

## 网络与本地交付是两条支路

固定源码 deliver_data_any() 先建立 tkmap instance，然后依次尝试 network delivery 与 local delivery。同进程 Reader 不必为了模型统一而强制绕 UDP 回环，Cyclone DDS 有 local delivery fast path。

## 网络支路进入 ddsi_write_sample_gc

deliver_data_network() 调用 ddsi_write_sample_gc，随后进入 write_sample：分配 sequence number、根据 QoS 决定是否写 WHC、构造 DATA/DATAFRAG、处理 heartbeat，并最终交给 xpack。

## 大样本为什么会变成多个 xmsg

RTPS packet 受 fragment size 与 packet packing 限制。大 serdata 会拆成 DATAFRAG，并可能伴随 HEARTBEATFRAG。xmsg 表示需要保持关系的一组 RTPS submessage，xpack 再把多个 xmsg 聚合为一次 transport message。

这样 serialized sample 可以由 iovec 引用，而不必为了添加 packet header 再复制整份 payload。

## 默认同步模式一直跑到 sendmsg

官方 docs/dev/write-to-take.md 给出的主线是：

~~~text
APPLICATION
  dds_write
    -> ddsi_write_sample_gc
    -> transmit_sample
    -> ddsi_xpack_send_real
    -> sendmsg
~~~

默认状态下，发布线程自己承担序列化、WHC、packet build 与 socket send。发布 API 的 WCET 因而不能只看函数调用框架开销。

## Batching 改变 Flush 时机

deliver_data_network() 在 flush 为真时调用 ddsi_xpack_send。Writer batching 会改变每次 write 是否立即 flush，于是 throughput、packet packing 与端到端 latency 之间存在直接 trade-off。

## PSMX 让数据再多一条路径

若 Writer 配置了 PSMX endpoint，写路径还会处理 loan metadata 与 plugin write_with_key。一次 dds_write 可能同时涉及 RTPS network path、local reader delivery 与 PSMX shared-memory/plugin path。

## 控制系统中的实际问题

如果 1 kHz 控制线程直接调用可靠、同步、跨网络的 Writer，周期预算至少要考虑 serialization、WHC lock/allocation、xmsg、packet packing、socket send 与可靠性 backpressure。

如果不可接受，应从 QoS、asynchronous mode、消息大小、线程隔离、PSMX 或控制/遥测通道分离上重构，而不是仅提高线程优先级。
