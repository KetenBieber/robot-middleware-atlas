# QoS Matching：Reliable、Durability、Deadline 为什么会改变连接关系

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## QoS 不只是本地参数

一部分 DDS QoS 属于 Requested/Offered compatibility。Reader 请求的能力超过 Writer 提供能力时，双方不能匹配。

ddsi_qosmatch.c 对 Reliability 的判断是：

~~~c
if ((mask & DDSI_QP_RELIABILITY) &&
    rd_qos->reliability.kind >
      wr_qos->reliability.kind)
{
  *reason =
    DDS_RELIABILITY_QOS_POLICY_ID;
  return false;
}
~~~

Reader 要 Reliable，而 Writer 只提供 Best Effort，不能靠接收端自己补出可靠性。

## Durability 也是强度关系

~~~c
if ((mask & DDSI_QP_DURABILITY) &&
    rd_qos->durability.kind >
      wr_qos->durability.kind)
{
  *reason =
    DDS_DURABILITY_QOS_POLICY_ID;
  return false;
}
~~~

Reader 请求的历史可见性高于 Writer 提供能力时，同样不兼容。

## Deadline 为什么比较方向不同

~~~c
if ((mask & DDSI_QP_DEADLINE) &&
    rd_qos->deadline.deadline <
      wr_qos->deadline.deadline)
{
  *reason =
    DDS_DEADLINE_QOS_POLICY_ID;
  return false;
}
~~~

Reader 要求最多 10 ms 必须有一次，而 Writer 只承诺 20 ms，显然满足不了。这里不是枚举值相等判断，而是 policy 的物理语义决定比较方向。

## Topic、Partition 与 Data Representation

固定源码先比较 Topic 名与 Partition，再比较 RxO policy。Writer 选择的 data representation 还必须出现在 Reader 可接受列表中。所以类型名相同不等于线上的序列化表示一定兼容。

## Reliability 会直接改变热路径

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

QoS 会改变内存占用、数据结构维护、写调用阻塞风险、ACK 后释放时机和 heartbeat/retransmit 工作量。

## WHC 与 RHC 不是同一个 queue

~~~text
WHC
= writer-side protocol history
= reliability / retransmit / transient-local

RHC
= reader-side application history
= read/take / sample state / resource limits
~~~

后面的 rmw_cyclonedds 固定案例会看到 ROS 2 QoS 被逐项映射为 DDS QoS，因此它最终会进入这里的 compatibility 与数据路径。
