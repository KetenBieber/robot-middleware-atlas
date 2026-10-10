# 真实案例：Fast DDS 官方 delivery_mechanisms 怎样把 UDP、SHM、Data Sharing 与 Loan 放进同一份程序

Fast DDS 固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a，官方示例目录 examples/cpp/delivery_mechanisms。

这份案例的价值不在于“能发消息”，而在于它用同一套 Publisher/Subscriber 业务代码切换多个 delivery mechanism，因此特别适合验证前面源码里讨论的抽象边界。

## 1. 同一程序先切 Participant Transport

示例根据命令行配置不同 delivery mechanism。

普通 UDP/TCP 模式使用 builtin transports；SHM 与 DATA_SHARING 分支则显式创建：

~~~cpp
std::shared_ptr<
    SharedMemTransportDescriptor>
    shm_transport =
        std::make_shared<
          SharedMemTransportDescriptor>();

shm_transport->segment_size(
    shm_transport->max_message_size()
      * max_samples);

pqos.transport()
    .user_transports
    .push_back(shm_transport);
~~~

这一步只改变 Participant transport capability。

## 2. Data Sharing 还要额外改 Writer/Reader QoS

Publisher：

~~~cpp
if (DeliveryMechanismKind::
      DATA_SHARING ==
    config.delivery_mechanism)
{
    writer_qos
      .data_sharing()
      .automatic();
}
else
{
    writer_qos
      .data_sharing()
      .off();
}
~~~

PubSub 版本中 Reader 也对应设置。

所以官方示例自己就证明：

~~~text
SHM Transport
和
Data Sharing QoS
是两层独立配置
~~~

如果只配置 SHM transport，不会自动等价于 Data Sharing。

## 3. 为什么还要配置 History / Resource Limits

示例使用 Transient Local、KEEP_LAST，并根据 max_samples 设置：

~~~cpp
writer_qos.history().depth =
    max_samples;

writer_qos.resource_limits()
    .max_samples_per_instance =
    max_samples;

writer_qos.resource_limits()
    .max_samples =
      writer_qos.resource_limits()
        .max_instances
      * max_samples;
~~~

Data Sharing 不是无限共享一块内存。

共享 history 的 slot 数量仍然受 History/Resource Limits 决定。

这正对应 WriterPool / ReaderPool 中 shared history descriptor 的容量概念。

## 4. Publisher 为什么使用 loan_sample

真正发消息时：

~~~cpp
void* sample = nullptr;

if (!is_stopped() &&
    RETCODE_OK ==
      writer_->loan_sample(sample))
{
    DeliveryMechanisms* msg =
      static_cast<
        DeliveryMechanisms*>(
          sample);

    msg->index() =
      ++index_of_last_sample_sent_;

    memcpy(
      msg->message().data(),
      "Delivery mechanisms",
      sizeof("Delivery mechanisms"));

    ret =
      RETCODE_OK ==
      writer_->write(sample);
}
~~~

注意应用没有：

~~~text
构造本地 Message
→ write(&message)
→ middleware 再 serialize/copy
~~~

而是：

~~~text
Writer loan
→ 直接填 payload-backed object
→ write loan back
~~~

这和 DataWriterImpl::check_and_remove_loan() 的源码完全对应。

## 5. Loan 并不自动意味着 Data Sharing

同一份示例在不同 delivery mechanism 下都可以调用 loan_sample。

所以 Loan 解决的是 application → Writer payload 这一段 copy。

Data Sharing 解决的是 Writer history → Reader history 同机交付路径。

SHM Transport 解决的是 RTPS packet transport 走共享内存而不是 kernel network。

三者可以组合，但不能互相替代。

## 6. 为什么 SHM 与 DATA_SHARING 都创建 SharedMemTransportDescriptor

官方示例的 DATA_SHARING 分支仍然添加 SHM transport。

原因不是“Data Sharing 就是 SHM Transport”。

Participant 仍然需要：

- discovery；
- fallback transport；
- 其他不满足 Data Sharing compatibility 的 endpoint；
- builtin traffic。

因此 Participant transport 和 endpoint data-sharing policy 必须同时存在。

## 7. PubSubApp 为什么值得看

PubSubApp 在一个进程里同时建 Publisher 与 Subscriber。

它让我们可以比较：

~~~text
INTRAPROCESS
SHM
DATA_SHARING
UDP/TCP
~~~

在同样 Topic/Type 下的不同路径。

这里尤其要避免一个误区：

> 单进程最快路径不能代表双进程、跨主机结果。

Fast DDS 的 intraprocess delivery、本机 Data Sharing 与 network transport 各自有独立 fast path。

## 8. 这个官方案例可以怎么做性能实验

固定程序后建议只改一个变量：

| 实验 | Transport | Data Sharing | Loan |
| --- | --- | --- | --- |
| A | UDP | off | off |
| B | UDP | off | on |
| C | SHM Transport | off | on |
| D | SHM + Data Sharing | automatic | on |

再分别测：

~~~text
writer call latency
reader data age
CPU
memory bandwidth
context switches
payload size sensitivity
history depth sensitivity
~~~

这样才能真正看到三种机制分别省掉了哪一段成本。

## 9. 这个案例验证了什么

它把前面几篇源码结论直接落到官方代码：

- Transport 是 Participant 级；
- Data Sharing 是 endpoint QoS；
- loan_sample 是 payload ownership；
- History/Resource Limits 决定共享池容量；
- 同一业务代码可以在多种 delivery path 上运行；
- zero-copy 是多层条件同时成立的结果。

因此它比自写 HelloWorld 更适合作为 Fast DDS 同机传输机制的真实基线。

## 10. 用同一案例做 copy ledger

性能实验前先记录每个模式的理论数据路径：

~~~text
UDP:
application
→ serialize
→ WriterHistory
→ RTPS packet
→ kernel/network
→ ReaderHistory
→ application

SHM Transport:
application
→ serialize
→ WriterHistory
→ RTPS packet
→ shared-memory transport
→ ReaderHistory
→ application

Data Sharing + loan:
writer pool loan
→ application fill
→ shared history/payload
→ reader view
~~~

然后用 profiler/trace 验证实际是否出现预期 copy，而不是用吞吐结果反推内部路径。

## 11. 混合 Reader 实验比纯本机实验更重要

真实机器人常见拓扑不是“只有一个本地订阅者”，而是：

~~~text
camera writer
  ├─ local perception reader
  └─ remote debugging reader
~~~

建议在 Data Sharing 实验里加入一个远端 Reader，观察：

- WriterHistory 占用是否变化；
- CPU/serialization 是否重新出现；
- 本地 Reader latency 是否受远端 Reliable Reader 影响；
- payload 回收时机是否变化。

这样才能验证 mixed topology 下的真实 ownership，而不是只验证最佳路径。

## 12. 过载实验要测 data age

除了平均 latency，还应主动制造：

~~~text
producer rate > consumer/network capacity
~~~

并测量：

- 最老 pending sample age；
- History occupancy；
- write timeout；
- sample lost/rejected；
- retransmission；
- RSS / shared-memory pool 占用。

对机器人控制而言，旧数据持续可靠到达通常比“偶尔丢一个最新值”更危险，因此 data age
必须作为一等指标。

## 13. 实验结论必须限定条件

最终报告至少记录：

~~~text
Fast DDS commit
delivery mechanism
payload size
History/ResourceLimits
Reliability
publish mode
number/location of readers
CPU affinity
transport descriptor
~~~

缺少这些条件，“SHM 比 UDP 快多少”或“loan 实现 zero-copy”都无法迁移到其他机器人。
