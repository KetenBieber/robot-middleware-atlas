# Fast DDS QoS：从 API Policy 一路影响 History、Reliability 与 Matching

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## QoS 在 Fast DDS 里有三种作用

第一类决定 endpoint 是否能匹配：

~~~text
Reliability
Durability
Ownership
Data Representation
Partition
...
~~~

第二类决定本地存储结构：

~~~text
History
Resource Limits
Durability Service
Memory Policy
~~~

第三类直接改变运行时 timer/发送行为：

~~~text
Deadline
Lifespan
Liveliness
Publish Mode
Disable Positive ACKs
Data Sharing
~~~

所以 QoS 不是一张静态配置表。

## DataWriter 创建以后并非所有 QoS 都能改

DataWriterImpl::set_qos() 会检查：

~~~cpp
if (!can_qos_be_updated(
        qos_,
        qos_to_set))
{
    return RETCODE_IMMUTABLE_POLICY;
}
~~~

这说明一些 policy 在创建 endpoint 时已经“编译”进内部对象、History、Transport 或 matching state，运行时不能无成本改变。

## Reliability 为什么还带 max_blocking_time

Fast DDS 的 write hot path 会从 QoS 取：

~~~cpp
auto max_blocking_time =
    steady_clock::now() +
    microseconds(
      TimeConv::Time_t2MicroSecondsInt64(
        qos_.reliability()
            .max_blocking_time));
~~~

这个值会影响 strict-realtime writer mutex 获取，也会进入 History 插入/资源等待。

因此 Reliable QoS 里的 max_blocking_time 不只是“网络确认超时”，而是应用写调用可以承受多长阻塞的一部分 runtime contract。

## History 与 Resource Limits 必须一起读

典型配置：

~~~text
History:
  KEEP_LAST depth = N

ResourceLimits:
  max_samples
  max_instances
  max_samples_per_instance
~~~

History 描述语义，Resource Limits 给出可用资源上界。

当 History 想保留的数据超过 Resource Limits 时，系统必须选择：

- 淘汰；
- 等待；
- 返回 OUT_OF_RESOURCES / TIMEOUT；
- 覆盖旧样本。

不理解 Resource Limits，就无法分析 write 的最坏时间与内存上限。

## Deadline 为什么进入 TimedEvent

DataWriterImpl 完成 History 插入后，会：

~~~cpp
history_->set_next_deadline(...);

deadline_timer_->cancel_timer();
deadline_timer_->restart_timer();
~~~

Lifespan 同样更新 timer。

这说明 QoS 会创建长期 runtime event，并参与 EventResource 调度，而不是只在 discovery 参数里传播一次。

## Data Sharing 也是 QoS

官方 delivery_mechanisms 示例明确写：

~~~cpp
writer_qos.data_sharing().automatic();
reader_qos.data_sharing().automatic();
~~~

如果不使用，则：

~~~cpp
writer_qos.data_sharing().off();
reader_qos.data_sharing().off();
~~~

因此同机数据是否进入 Data Sharing fast path，本质上也是 endpoint compatibility / QoS 决策的一部分。

## ROS 2 最终还是映射到这些对象

rmw_fastrtps 会把：

~~~text
RMW_QOS_POLICY_RELIABILITY_RELIABLE
RMW_QOS_POLICY_HISTORY_KEEP_LAST
RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL
deadline / lifespan / liveliness
~~~

转换成 Fast DDS DataWriterQos / DataReaderQos。

所以 ROS 2 的 QoS mismatch、publisher blocking、history memory growth，最后都能沿源码落回 Fast DDS 这里。
