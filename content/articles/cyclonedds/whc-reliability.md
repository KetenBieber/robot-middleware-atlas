# Writer History Cache 与 Reliability：ACK/NACK 之前为什么不能丢样本

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## Reliable 的第一性原理

如果网络可能丢包，而 Reader 可以要求重传，那么 Writer 必须回答：

~~~text
你缺 sequence 4812，
我还能不能把 4812 再发一遍？
~~~

若 Writer 发完立刻释放样本，就没有重传数据。因此 Reliable 必然需要 writer-side history。

## WHC 的插入条件

发送主线中：

~~~c
if ((wr->reliable &&
     have_reliable_subs(wr)) ||
    wr_deadline ||
    wr->handle_as_transient_local)
{
  res = ddsi_whc_insert(
    wr->whc,
    ddsi_writer_max_drop_seq(wr),
    seq,
    exp,
    serdata,
    tk);
}
~~~

WHC 除可靠性之外还服务 deadline instance bookkeeping 与 transient-local history，因此不能简单定义成重传队列。

## 官方开发文档直接暴露了内部成本

docs/dev/write-to-take.md 对 ddsi_whc_insert 的说明包括：分配或复用 WHC node、插入 sequence-number hash、维护 sequence interval tree、keyed topic 时维护 instance-handle index，以及 history 满时可能淘汰旧样本。

Reliable write 的成本不仅是以后可能重发，写入当下就增加了索引和生命周期管理。

## 为什么需要 Sequence Interval

假设当前还保留：

~~~text
100 101 102 106 107 108
~~~

只存一个 min/max 无法表达 103-105 已被释放或不存在。按 sequence interval 管理可以紧凑表示连续区间，便于 ACK 后批量释放和重传查找。

## ACK 后释放不是简单 pop_front

不同 Reader 的确认进度可能不同：

~~~text
Reader A ack <= 108
Reader B ack <= 103
Reader C ack <= 106
~~~

Writer 能安全丢弃到哪个 sequence，需要同时看可靠 Reader 与 durability/history policy。最大可丢序号来自协议状态，而不是单消费者 FIFO head。

## HEARTBEAT 与 ACKNACK

~~~text
Writer
DATA #100 #101 #102
HEARTBEAT [100,102]
       |
       v
Reader
发现 #101 缺失
       |
ACKNACK bitmap
       |
       v
Writer
从 WHC 找 #101
retransmit
~~~

所以 Writer endpoint 必须保存 matched proxy readers、heartbeat state 与 WHC。

## WHC 是实时系统的重要内存热点

官方文档指出，小样本高吞吐时 WHC node 分配频率可能非常高，因此实现使用 cache 降低 allocator 成本；如果使用固定深度队列，预分配 circular array 也更容易做 WCET 与内存上界。

这是通用 DDS 语义与专用实时结构之间的典型权衡。

## Reliable 不等于业务成功

ACK/NACK 证明的是 RTPS Reader 的序列接收，不证明 ROS callback 已运行、控制器已应用命令、电机驱动已执行。命令型机器人消息若要求动作已生效，仍需要业务 request-id、result、timeout 与 idempotency 协议。
