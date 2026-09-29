# 真实案例：ROS 2 rmw_cyclonedds 如何把 Publisher、QoS 与 Executor WaitSet 落到 Cyclone DDS

Cyclone DDS 本体固定到 e54e991f75a3e67f8e628da3171122e36ea5b872。ROS 2 适配层固定到 ros2/rmw_cyclonedds commit 19478b0a9aa523af62023812d05bfcb4295e8efb（4.2.1）。

## 为什么这一篇比 ROS 2 Talker/Listener 更关键

Talker/Listener 只能告诉你 rclcpp 的接口。真正要回答的是：

~~~text
rclcpp Publisher
在哪里变成 DDS Writer？

ROS QoS
在哪里变成 DDS QoS？

Executor wait
在哪里进入 DDS WaitSet？

DDS DATA_AVAILABLE
怎样重新变成 ROS ready subscription？
~~~

rmw_cyclonedds 正好是这层桥。

## 第一层：Context 建立 DDS Participant

固定 rmw_node.cpp 初始化时：

~~~cpp
this->ppant =
  dds_create_participant(
    this->domain_id,
    ppant_qos.get(),
    nullptr);

if (this->ppant < 0) {
  ...
}
~~~

接着它还创建 DDS builtin-topic readers，用来观察 Participant、Publication、Subscription discovery。

这说明 ROS graph discovery 并不是凭空出现；RMW 会消费 DDS discovery information，再组织成 ROS graph 语义。

## 为什么还创建共享 Publisher / Subscriber

同一 context 中，rmw_cyclonedds 创建 DDS Publisher/Subscriber 容器，后续 ROS publisher/subscription 的 DDS Writer/Reader 会挂到这些容器下。

因此不能简单说：

~~~text
一个 ROS Node = 一个 DDS Participant
~~~

对象复用策略是 RMW 实现的一部分，而不是 DDS 标准强制的 ROS 映射。

## ROS Publisher 怎样变成 DDS Writer

创建 publisher 的核心片段：

~~~cpp
qos = create_readwrite_qos(
  qos_policies,
  *type_support->get_type_hash_func(
    type_support),
  false,
  "");

if ((pub->enth =
       dds_create_writer(
         dds_pub,
         topic,
         qos,
         listener)) < 0)
{
  RMW_SET_ERROR_MSG(
    "failed to create writer");
  goto fail_writer;
}
~~~

这里有三层对象：

~~~text
rmw_publisher_t
  -> CddsPublisher
      -> pub->enth
          = dds_entity_t Writer
~~~

所以 rclcpp Publisher 的数据最终会进入 Cyclone DDS Writer。

## rmw_publish 最终究竟调用什么

固定提交最直接的一段：

~~~cpp
auto pub =
  static_cast<CddsPublisher *>(
    publisher->data);

const dds_time_t tstamp =
  dds_time();

TRACETOOLS_TRACEPOINT(
  rmw_publish,
  (const void *)publisher,
  ros_message,
  tstamp);

if (dds_write_ts(
      pub->enth,
      ros_message,
      tstamp) >= 0)
{
  return RMW_RET_OK;
}
~~~

主线于是完全闭合：

~~~text
rclcpp::Publisher::publish
-> rcl publish
-> rmw_publish
-> dds_write_ts
-> Cyclone DDS write path
-> WHC / RTPS / UDP or local/PSMX path
~~~

这就是为什么前面分析 dds_write 是否同步、WHC 是否阻塞，会直接影响 ROS 2 publish 调用。

## ROS Message 为什么能直接传给 dds_write_ts

rmw_cyclonedds 为 ROSIDL type support 构造 Cyclone DDS 的 sertype/topic type support。

于是 dds_write_ts 收到的 void pointer 虽然指向 ROS message object，Cyclone DDS 序列化层已经知道如何把它转换成 serdata。

这里不是 DDS 认识所有 C++ struct，而是 RMW 在类型系统之间搭了桥。

## ROS QoS 到 DDS QoS：History

create_readwrite_qos() 中：

~~~cpp
switch (qos_policies->history) {
  case RMW_QOS_POLICY_HISTORY_SYSTEM_DEFAULT:
  case RMW_QOS_POLICY_HISTORY_KEEP_LAST:
    if (qos_policies->depth ==
        RMW_QOS_POLICY_DEPTH_SYSTEM_DEFAULT)
    {
      dds_qset_history(
        qos,
        DDS_HISTORY_KEEP_LAST,
        1);
    } else {
      dds_qset_history(
        qos,
        DDS_HISTORY_KEEP_LAST,
        static_cast<int32_t>(
          qos_policies->depth));
    }
    break;

  case RMW_QOS_POLICY_HISTORY_KEEP_ALL:
    dds_qset_history(
      qos,
      DDS_HISTORY_KEEP_ALL,
      DDS_LENGTH_UNLIMITED);
    break;
}
~~~

ROS depth 因而会进入 DDS RHC/WHC 行为，而不只影响 Executor。

## ROS Reliability 到 DDS Reliability

~~~cpp
switch (qos_policies->reliability) {
  case RMW_QOS_POLICY_RELIABILITY_RELIABLE:
    dds_qset_reliability(
      qos,
      DDS_RELIABILITY_RELIABLE,
      DDS_INFINITY);
    break;

  case RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT:
    dds_qset_reliability(
      qos,
      DDS_RELIABILITY_BEST_EFFORT,
      0);
    break;
}
~~~

所以 ROS 中一个 QoS mismatch，最终就是前面 ddsi_qosmatch.c 中 Requested/Offered 的 compatibility 问题。

## Durability 为什么还要设置 Durability Service

Transient Local 分支除了：

~~~cpp
dds_qset_durability(
  qos,
  DDS_DURABILITY_TRANSIENT_LOCAL);
~~~

还读取当前 history 并设置 durability service。

源码注释解释了原因：Cyclone DDS 把 reliability window 与 historical-data retention 分开管理。

这是一个很好例子：两个中间件即使都叫 TRANSIENT_LOCAL，内部 history 实现也不一定完全同构；RMW 必须做语义适配。

## ROS Subscription 怎样变成 DDS Reader

创建 subscription 时：

~~~cpp
sub->enth =
  dds_create_reader(
    dds_sub,
    topic,
    qos,
    listener);
~~~

随后立刻建立 read condition：

~~~cpp
sub->rdcondh =
  dds_create_readcondition(
    sub->enth,
    DDS_ANY_STATE);
~~~

所以 Executor 等待的不是“直接等 socket”，而是最终等待 Reader/Condition 的可读状态。

## rmw_wait 如何组装 WaitSet

rmw_wait() 会收集：

- subscriptions；
- guard conditions；
- services；
- clients；
- events。

对 subscription：

~~~cpp
dds_waitset_attach(
  ws->waitseth,
  x->rdcondh,
  nelems++);
~~~

service/client 也把内部 subscription read-condition attach 进去。

这正好解释 ROS 2 Executor 为什么能用一个 wait primitive 同时等多种 entity。

## RMW 自己也有重要的数据结构

CddsWaitset 使用 C++ 容器：

~~~text
std::vector
保存 subscription / guard /
service / client / event 列表

std::vector<dds_attach_t>
保存 triggers

std::unordered_set<dds_entity_t>
去重 event entities

std::sort
把 waitset 返回的 trigger index 排序
~~~

这与 Cyclone DDS C 内核的 AVL/hash/circular list 是不同层的工程选择。

RMW 的集合规模通常由一个 Executor waitset 当前管理的实体数决定；这里优先开发便利和清晰映射，而不是协议层长期高频索引。

## 为什么一个 WaitSet 不允许并发 rmw_wait

固定代码：

~~~cpp
std::lock_guard<std::mutex>
  lock(ws->lock);

if (ws->inuse) {
  RMW_SET_ERROR_MSG(
    "concurrent calls to rmw_wait "
    "on a single waitset is not supported");

  return RMW_RET_ERROR;
}

ws->inuse = true;
~~~

一个 waitset 是一次 Executor 等待操作的可变工作区，内部 vectors、attachments 与 trigger indices 会被重建。允许多个线程并发操作同一个对象会让这些状态失去单一 owner。

## 真正阻塞的位置

最终：

~~~cpp
const dds_return_t ntrig =
  dds_waitset_wait(
    ws->waitseth,
    ws->trigs.data(),
    ws->trigs.size(),
    timeout);
~~~

返回后：

~~~cpp
ws->trigs.resize(ntrig);

std::sort(
  ws->trigs.begin(),
  ws->trigs.end());
~~~

随后按 trigger index 把没有 ready 的 ROS handles 置空。

Executor 上层得到的就是“这轮哪些实体 ready”。

## 一条 ROS 2 接收链最终是什么

~~~text
NIC / UDP
-> Cyclone receive thread
-> RTPS parser
-> proxy writer
-> defrag / reorder
-> Reader RHC
-> ReadCondition triggered
-> DDS WaitSet observer
-> dds_waitset_wait wakes
-> rmw_wait returns ready subscription
-> rcl / Executor
-> user callback
~~~

所以 Executor latency 只占最后一段。若 sample 很晚才进入 RHC，上层调 executor 优先级不能修复前面已经产生的数据年龄。

## Discovery Listener 还有另一套 WaitSet

rmw_cyclonedds 自己的 discovery info listener thread 也创建 WaitSet，然后无限等待 DDS builtin-topic readers。

这说明 RMW 并不是把所有 DDS work 都交给 Executor；ROS graph 维护本身也有专门的 discovery execution context。

## Loaning 为什么还要经过 RMW Capability

publisher/subscription 创建后会记录 is_loaning_available，具体判断依赖 Cyclone DDS shared-memory/loan capability 与 ROS message 是否 self-contained。

所以：

~~~text
Cyclone DDS 支持 loan
!= 任意 ROS message 都自动 loan
!= rclcpp publish 一定 zero-copy
~~~

还要同时满足 type、RMW API、backend 与 topology。

## 实时机器人应怎样利用这条链

对一条 joint state -> controller -> command 链，不要只记录 callback timestamp。更有价值的 tracing 点是：

~~~text
sensor sample timestamp
rmw receive ready
executor callback start
controller compute start/end
rmw_publish entry
dds_write exit
actuator receive timestamp
~~~

再结合 Cyclone statistics / PCAP / perf scheduler trace，才能分辨是 DDS network、RMW wait、Executor queue 还是业务 compute 导致尾延迟。

## 这个案例最终验证了什么

它把 ROS 2 与 Cyclone DDS 之间最重要的抽象桥全部钉住了：

~~~text
ROS Publisher -> DDS Writer
ROS Subscription -> DDS Reader + ReadCondition
ROS QoS -> DDS QoS
ROS publish -> dds_write_ts
ROS wait -> dds_waitset_wait
ROS graph -> DDS builtin discovery data
~~~

读完这一篇以后，再分析 rclcpp Executor 与 Callback Group，就可以明确哪些问题属于 ROS 调度层，哪些已经发生在 DDS/RMW 层。
