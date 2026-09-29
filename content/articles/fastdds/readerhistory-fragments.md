# StatefulReader 与 ReaderHistory：DATAFRAG、WriterProxy 和 CacheChange 怎样变成可读样本

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## 接收端不是“收到 UDP 就 push 到队列”

Fast DDS 的接收链至少经过：

~~~text
Transport Receiver
→ MessageReceiver
→ 找到目标 RTPSReader
→ StatefulReader / StatelessReader
→ WriterProxy
→ fragment assembly
→ ReaderHistory
→ DataReaderImpl
→ Listener / Condition / take
~~~

每一层都在补齐不同语义。

## MessageReceiver 负责协议拆包

RTPSParticipantImpl 为 locator 创建 ReceiverResource，并给它注册 MessageReceiver。

MessageReceiver::processCDRMsg() 先检查 RTPS header，再逐个处理 submessage。DATA / DATAFRAG 最后会通过 findAllReaders() 分发给目标 Reader。

所以一个 socket packet 可能同时包含：

~~~text
INFO_TS
DATA
HEARTBEAT
GAP
...
~~~

不能把 UDP datagram 和一个 DDS sample 一一对应。

## StatefulReader 为什么需要 WriterProxy

固定源码中 StatefulReader 保存：

~~~cpp
ResourceLimitedVector<WriterProxy*>
    matched_writers_;

ResourceLimitedVector<WriterProxy*>
    matched_writers_pool_;
~~~

WriterProxy 记录远端 Writer 的 sequence state、missing changes、heartbeat count、ACKNACK state、liveliness 与 fragment state。

Reliable Reader 不可能只靠 ReaderHistory 判断“网络上还缺什么”。

## DATA 先检查 Sequence 是否已收到

StatefulReader::process_data_msg()：

~~~cpp
std::unique_lock<RecursiveTimedMutex>
    lock(mp_mutex);

if (acceptMsgFrom(
        change->writerGUID,
        &pWP))
{
    if (!pWP ||
        !pWP->change_was_received(
            change->sequenceNumber))
    {
        ...
    }
}
~~~

这一步挡住重复 DATA，也把 remote writer 与本地 reliability state 关联起来。

## 为什么接收也需要 CacheChangePool

网络解析得到的 incoming CacheChange 不能直接长期塞进 ReaderHistory。

StatefulReader 会从自己的 change pool 申请真正受 Reader 管理的 CacheChange，再把 payload 拷贝或转移进去。

因此 receive hot path 的内存策略同样受 memory policy、change pool、payload pool 与 resource limits 影响。

## DATAFRAG 的两阶段完成

收到 fragment 时：

~~~cpp
work_change->add_fragments(
    change_to_add->serializedPayload,
    fragmentStartingNum,
    fragmentsInSubmessage);
~~~

只有：

~~~cpp
work_change->is_fully_assembled()
~~~

以后才：

~~~cpp
history_->completed_change(
    work_change,
    changes_up_to,
    rejection_reason);
~~~

随后 WriterProxy 标记 sequence received。

所以 fragment arrived 不等于 sample ready。大点云、图像在 packet loss 下会延长 CacheChange 与 fragment buffer 生命周期。

## ReaderHistory 仍然不是普通 FIFO

ReaderHistory::add_change() 要求已经绑定 Reader 与 mutex：

~~~cpp
if (mp_reader == nullptr ||
    mp_mutex == nullptr)
{
    ...
}
~~~

History 中 change 的插入/移除会和 Reader reliability、sample rejection、data available notification 等机制共同工作。

高层 DataReader 的 KEEP_LAST / KEEP_ALL 又会进一步管理 instance history。

## 为什么 Fast DDS 还要 ResourceLimitedVector

StatefulReader 的 WriterProxy 集合使用 ResourceLimitedVector，而不是无限 std::vector。

原因是 endpoint 数量本身也属于资源预算：

~~~text
initial
maximum
increment
~~~

既可以预留确定容量，也可以按配置增长。

这对“机器人系统最多会匹配多少 Publisher”提供显式上界。

## Data Sharing 也走 ReaderProxy 语义

同机 Data Sharing 虽然不走网络 RTPS DATA packet，但 Reliable Reader 仍需要 sequence/ACK 语义。

固定源码甚至有专门的 send_datasharing_ack()。

这说明绕过 Transport 不等于绕过 DDS reliability model。

## 接收端最坏情况来自哪里

需要关注：

- 大消息 fragment；
- 多 remote Writer；
- packet reordering；
- slow application；
- KEEP_ALL；
- reliable retransmission；
- Data Sharing history；
- callback 执行时间。

只测一个 publisher + 一个 subscriber + 64 字节 localhost，并不能验证真实感知数据链。
