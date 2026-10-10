# 真实案例：ROS 2 rmw_fastrtps 怎样把 Publisher、QoS 与 Executor WaitSet 映射到 Fast DDS

Fast DDS 本体固定到 39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

ROS 2 适配层固定到 ros2/rmw_fastrtps commit a88ce42dc66203a9b617a05daf0f5019870fc3c8。

这一页只回答一件事：

> rclcpp/rcl 下面的 ROS 2 语义到底怎样落到 Fast DDS 对象。

## 1. ROS Publisher 最终保存的是 Fast DDS DataWriter

rmw_fastrtps 的 CustomPublisherInfo 中保存 data_writer_。

创建 publisher 时，RMW 完成：

~~~text
ROS type support
→ Fast DDS TypeSupport
→ Topic
→ DataWriterQos
→ Publisher::create_datawriter
→ CustomPublisherInfo::data_writer_
~~~

所以 ROS Publisher 的 runtime 实体最终确实是 Fast DDS DataWriter。

## 2. rmw_publish 最终进入 write_w_timestamp

固定 rmw_publish.cpp：

~~~cpp
auto info =
  static_cast<
    CustomPublisherInfo*>(
      publisher->data);

Time_t stamp;
Time_t::now(stamp);

TRACETOOLS_TRACEPOINT(
  rmw_publish,
  publisher,
  ros_message,
  stamp.to_ns());

if (RETCODE_OK !=
    info->data_writer_
      ->write_w_timestamp(
        &data,
        HANDLE_NIL,
        stamp))
{
    RMW_SET_ERROR_MSG(
      "cannot publish data");

    return RMW_RET_ERROR;
}
~~~

于是完整链条是：

~~~text
rclcpp::Publisher::publish
→ rcl_publish
→ rmw_publish
→ Fast DDS DataWriter
→ DataWriterImpl
→ perform_create_new_change
→ PayloadPool / CacheChange
→ WriterHistory
→ StatefulWriter
→ FlowController / Transport
~~~

这也是为什么 Fast DDS write hot path 的锁、序列化与 History 会直接影响 ROS publish 调用时延。

## 3. RMW 为什么还包一层特殊数据对象

rmw_fastrtps 可以把 ROS message 指针和 type support implementation 包进 Fast DDS 认识的内部 data wrapper。

这样 Fast DDS TypeSupport 最终负责真正序列化 ROS message。

所以 Fast DDS DataWriter 并不是“天生认识 ROS struct”，而是 RMW type support 搭桥。

## 4. ROS History 怎样变成 Fast DDS History

固定 qos.cpp：

~~~cpp
switch (qos_policies.history)
{
case RMW_QOS_POLICY_HISTORY_KEEP_LAST:
  entity_qos.history().kind =
    KEEP_LAST_HISTORY_QOS;
  break;

case RMW_QOS_POLICY_HISTORY_KEEP_ALL:
  entity_qos.history().kind =
    KEEP_ALL_HISTORY_QOS;
  break;
}
~~~

随后 RMW 还保证 Fast DDS history depth 至少达到 ROS requested depth：

~~~cpp
if (qos_policies.depth !=
      RMW_QOS_POLICY_DEPTH_SYSTEM_DEFAULT &&
    static_cast<size_t>(
      entity_qos.history().depth)
      < qos_policies.depth)
{
    entity_qos.history().depth =
      static_cast<int32_t>(
        qos_policies.depth);
}
~~~

所以 ROS queue depth 最终会改变 Fast DDS History/Resource 行为。

## 5. Reliability 也是直接映射

~~~cpp
switch (qos_policies.reliability)
{
case RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT:
  entity_qos.reliability().kind =
    BEST_EFFORT_RELIABILITY_QOS;
  break;

case RMW_QOS_POLICY_RELIABILITY_RELIABLE:
  entity_qos.reliability().kind =
    RELIABLE_RELIABILITY_QOS;
  break;
}
~~~

这意味着 ROS 2 Reliable 最终会创建 StatefulWriter/Reader、Proxy、Heartbeat/ACKNACK 等 RTPS 机制。

不是 Executor 在做可靠性。

## 6. rmw_wait 不自己实现 Condition Variable

rmw_wait_set_t 的 data 指向一个真实 Fast DDS WaitSet：

~~~cpp
auto fastdds_wait_set =
  static_cast<
    eprosima::fastdds::dds::WaitSet*>(
      wait_set->data);
~~~

只有当当前没有 ready condition 时，RMW 才收集需要等待的 DDS Conditions。

## 7. Subscription Attach 的是什么

对于每个 subscription：

~~~cpp
attached_conditions.push_back(
  &custom_subscriber_info
    ->data_reader_
    ->get_statuscondition());
~~~

如果启用了 CPU/accelerator extra channel，还会把对应 DataReader StatusCondition 一并加入。

Service/Client 则 attach request/response reader 的 StatusCondition。

Event 同时可能加入 StatusCondition 与 GuardCondition。

## 8. GuardCondition 也进入同一个 Fast DDS WaitSet

ROS guard condition 被直接转为：

~~~cpp
eprosima::fastdds::dds::
  GuardCondition*
~~~

加入 attached_conditions。

因此 Executor 一次 wait 能同时等：

~~~text
subscription data
client response
service request
DDS event
ROS guard condition
~~~

这就是 DDS WaitSet 作为 RMW 底层 blocking primitive 的价值。

## 9. RMW 每轮 Wait 都 Attach / Detach

固定代码：

~~~cpp
for (auto& condition :
     attached_conditions)
{
    fastdds_wait_set
      ->attach_condition(
          *condition);
}
~~~

然后：

~~~cpp
ReturnCode_t ret_code =
  fastdds_wait_set->wait(
    triggered_conditions,
    timeout);
~~~

返回后：

~~~cpp
for (auto& condition :
     attached_conditions)
{
    fastdds_wait_set
      ->detach_condition(
          *condition);
}
~~~

源码 TODO 甚至写明：等上游支持批量 attach/detach 后再换 API。

这说明 RMW wait 本身也有一段容器构建和 attachment 管理成本。

## 10. 为什么先检查 has_triggered_condition

rmw_wait 一开始会：

~~~cpp
bool skip_wait =
  has_triggered_condition(
    subscriptions,
    guard_conditions,
    services,
    clients,
    events);
~~~

如果已经有 ready entity，就不必再 attach 一堆 condition 然后立刻被唤醒。

这是典型 fast path：

~~~text
先 cheap check
→ 没 ready 才进入完整 WaitSet setup
~~~

## 11. Wait 返回后为什么还要重新检查实体

Fast DDS WaitSet 只告诉 RMW“某些 Condition 触发”。

RMW 随后还会逐个检查：

~~~text
subscription_has_data
get_first_untaken_info
event guard/status
~~~

并把未 ready 的 ROS handle 置空。

所以 ROS Executor 得到的是经过 RMW 二次过滤后的 ready set。

## 12. listener_thread 说明 Graph Discovery 有自己的执行上下文

rmw_fastrtps_shared_cpp/src/listener_thread.cpp 还会自己创建 RMW waitset，等待 discovery subscription 和 guard condition。

这说明 ROS graph 维护并不完全依赖用户 Executor。

系统里至少有：

~~~text
Fast DDS receive threads
Fast DDS event/async threads
RMW graph listener thread
ROS Executor threads
application threads
~~~

做实时分析时必须分开。

## 13. StatusCondition 为什么有时被主动设为 none

publisher/client/service 创建代码里，会把某些 DataWriter StatusCondition enabled statuses 设为 none，避免不需要的状态意外触发 WaitSet。

这反映出一个原则：

> Condition 不是挂上去就完事，必须明确哪些 status 真正参与当前调度语义。

否则无关状态会制造虚假 wakeup。

## 14. 和 rmw_cyclonedds 对照

Fast DDS RMW：

~~~text
rmw_publish
→ DataWriter::write_w_timestamp

rmw_wait
→ Fast DDS StatusCondition /
  GuardCondition
→ WaitSet
~~~

Cyclone DDS RMW：

~~~text
rmw_publish
→ dds_write_ts

rmw_wait
→ DDS condition / waitset
~~~

两个 RMW 都最终依赖 DDS WaitSet，但 type support、ready check、attachment 数据结构和具体 endpoint implementation 不同。

## 15. 对机器人延迟分析意味着什么

真正完整的 ROS 2 接收链：

~~~text
NIC / SHM
→ Fast DDS Transport
→ MessageReceiver
→ StatefulReader
→ ReaderHistory
→ DataReader StatusCondition
→ Fast DDS WaitSet wake
→ rmw_wait
→ rcl Executor ready list
→ callback
~~~

所以只调 Executor 线程优先级，无法修复 network receive 堵塞、ReaderHistory backlog、fragment reassembly、RMW wait attach/detach 开销或 callback 前已有 stale data。

## 16. 这条真实案例最终把什么钉死了

~~~text
ROS Publisher
= Fast DDS DataWriter 的上层 façade

ROS Reliability/History
= Fast DDS QoS

ROS publish
= Fast DDS write hot path

ROS Executor blocking
= Fast DDS WaitSet

ROS Graph
= DDS discovery data + RMW listener
~~~

这样 Fast DDS 专题就从源码内核真正闭环到了 ROS 2。

## 17. Executor 调优为什么不能替代 DDS 调优

把 latency 分解后可以看到：

~~~text
T_total =
T_transport
+ T_rtps
+ T_history
+ T_wait
+ T_executor
+ T_callback
~~~

Executor priority/threads 只直接影响后两三项。如果大点云还在 DATAFRAG reassembly，
或 Reliable Writer 的 History/FlowController 已经产生 backlog，调高 Executor 优先级
不会让样本更早 ready。

## 18. ROS depth 与 Fast DDS capacity 不是一个孤立数字

RMW 会把 ROS History/depth 映射到 DDS QoS，但真正的运行时容量还要结合：

~~~text
History kind/depth
ResourceLimits
matched readers
Reliability
payload size
Data Sharing pool
~~~

因此 ROS 代码里写 QoS(10) 并不等于“系统永远只占 10 个消息的内存”。可靠性与多
instance、大 payload 仍会改变底层资源使用。

## 19. callback latency 应该从三个时间戳看

推荐在机器人链路同时记录：

~~~text
t_source   数据产生
t_receive  RMW/DDS ready
t_callback 用户 callback 开始
~~~

于是可以分开：

~~~text
middleware/data age = t_receive - t_source
executor delay      = t_callback - t_receive
~~~

只记录 callback 执行开始时间，会把 DDS backlog 和 Executor 排队混成一个数字。

## 20. shutdown 同样跨越 RMW 与 Fast DDS 两层

ROS context shutdown 以后，还要确保：

~~~text
Executor/wait threads wake
→ RMW handles stop being used
→ DDS endpoints detach
→ Fast DDS receiver/event/flow activity quiesces
→ Participant resources release
~~~

如果业务对象先析构而 DDS/RMW callback 仍可进入，就会把中间件内部的生命周期问题
扩散成用户对象 use-after-free。

## 21. 一份机器人 ROS 2 调优检查表

对于关键 topic，至少记录：

| 项目 | 问题 |
| --- | --- |
| Reliability | 丢一帧还是晚一帧更危险？ |
| History/depth | 允许保留多少旧数据？ |
| ResourceLimits | 内存上界是否明确？ |
| Publish mode | write 延迟还是 data age 优先？ |
| Transport | 同机/跨机实际走哪条路径？ |
| Loan/Data Sharing | 类型与 RMW 是否真的支持？ |
| WaitSet/Executor | ready 后多久进入 callback？ |
| Shutdown | 谁负责唤醒与 quiescence？ |

把这些问题一起回答，ROS 2 QoS 才从 API 参数变成可验证的系统合同。
