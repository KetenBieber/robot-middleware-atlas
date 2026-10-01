# QoS 不是参数表：它是 Publisher 与 Subscriber 的通信契约

本篇主固定版本为 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，兼容性规则核对 `rmw_dds_common@e26ba1079886c5598614172db0b27526aa6af07d`，DDS 映射参考 `rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e`。Fast DDS 对应机制可与 Atlas 的 Fast DDS QoS 专题对照。

## 为什么 ROS2 不能只有 queue_size

ROS1 用户很熟悉：

~~~cpp
nh.subscribe("/odom", 10, callback);
~~~

queue size 很重要，但它主要描述“本地来不及处理时能积多少条”。

分布式系统还要回答更多问题：

- 丢一个样本能不能接受？
- late joiner 是否需要旧样本？
- publisher 多久至少应产生一次样本？
- endpoint 多久不活跃算失效？
- 历史样本保留多少？

于是 ROS2 把这些要求放进 QoS contract。

## rclcpp::QoS 最终只是一个 rmw_qos_profile_t

固定源码：

~~~cpp
QoS::QoS(
  const QoSInitialization & qos_initialization,
  const rmw_qos_profile_t & initial_profile)
: rmw_qos_profile_(initial_profile)
{
  rmw_qos_profile_.history = qos_initialization.history_policy;
  rmw_qos_profile_.depth = qos_initialization.depth;
}
~~~

例如：

~~~cpp
QoS &
QoS::keep_last(size_t depth)
{
  rmw_qos_profile_.history =
    RMW_QOS_POLICY_HISTORY_KEEP_LAST;
  rmw_qos_profile_.depth = depth;
  return *this;
}

QoS &
QoS::reliable()
{
  return this->reliability(
    RMW_QOS_POLICY_RELIABILITY_RELIABLE);
}
~~~

所以 C++ builder API 只是把策略写进统一的 RMW profile。真正把它翻译成 DDS QoS 的是 backend。

## QoS 的第一层作用：决定 endpoint 能不能匹配

最容易忽略的事实是：

> QoS 不兼容时，不是“连上以后表现差”，而是 endpoint 可以根本不形成数据关系。

`rmw_dds_common::qos_profile_check_compatible` 明确编码了 compatibility。

### Reliability 是 offered/requested 关系

固定源码：

~~~cpp
if (
  publisher_qos.reliability ==
    RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT &&
  subscription_qos.reliability ==
    RMW_QOS_POLICY_RELIABILITY_RELIABLE)
{
  *compatibility = RMW_QOS_COMPATIBILITY_ERROR;
}
~~~

直觉上：

~~~text
Publisher offers: best effort
Subscriber requests: reliable
            |
            X
Publisher 无法满足要求
~~~

反过来：

~~~text
Publisher offers: reliable
Subscriber requests: best effort
            |
            OK
~~~

这不是对称比较，而是 capability 与 requirement 的比较。

## Durability 也是能力契约

固定源码：

~~~cpp
if (
  publisher_qos.durability ==
    RMW_QOS_POLICY_DURABILITY_VOLATILE &&
  subscription_qos.durability ==
    RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL)
{
  *compatibility = RMW_QOS_COMPATIBILITY_ERROR;
}
~~~

Transient Local subscriber 希望 late join 时还能拿到历史状态，但 Volatile publisher 没有承诺保留这类状态，所以无法满足。

这适合区分两种机器人数据：

~~~text
高速传感器帧
  -> 通常只关心当前/未来样本

地图、静态状态、配置类数据
  -> late join 可能需要最近历史
~~~

## Deadline 为什么也会影响 compatibility

如果 subscriber 要求每 10 ms 至少一条数据，而 publisher 只承诺 100 ms 一条，那么通信即使“能发包”，也不能满足 contract。

固定规则的核心是：

~~~cpp
if (sub_deadline < pub_deadline) {
  *compatibility = RMW_QOS_COMPATIBILITY_ERROR;
}
~~~

可以把它理解成：

\[
T_{publisher\ offer}
\le
T_{subscriber\ request}
\]

才能满足订阅端要求。

这里 deadline 不是线程调度 deadline，也不是网络包的绝对截止时间；它是数据产生/接收节奏的 QoS 约束。

## Liveliness 解决的是“端点还活着吗”

通信系统里“没有新消息”可能有几种含义：

~~~text
传感器静止
网络断开
publisher 卡死
节点退出
系统只是暂时没有数据
~~~

Liveliness 给 middleware 一个可观察的活性契约。

固定 compatibility 中，Automatic publisher 无法满足要求 Manual By Topic 的 subscriber：

~~~cpp
if (
  publisher_qos.liveliness ==
    RMW_QOS_POLICY_LIVELINESS_AUTOMATIC &&
  subscription_qos.liveliness ==
    RMW_QOS_POLICY_LIVELINESS_MANUAL_BY_TOPIC)
{
  *compatibility = RMW_QOS_COMPATIBILITY_ERROR;
}
~~~

因为 subscriber 请求的是更强的“这个 topic 主动声明自己仍活着”的能力。

## History/Depth 与 Reliability 不要混淆

下面两个配置回答不同问题：

~~~text
Reliability:
  丢失的样本是否尝试恢复？

History / Depth:
  本端需要保留多少历史样本？
~~~

`KEEP_LAST(1) + RELIABLE` 完全合法。

这意味着：对当前仍在交付范围内的样本使用可靠通信，但应用并不想堆很深的历史。

控制系统经常需要这种“只关心最新状态”的语义。

## QoS 如何进入具体 DDS

Cyclone RMW 对 reliability 的映射非常直接：

~~~cpp
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
~~~

RMW profile 到这里被翻译成 Cyclone DDS 的原生 QoS。

Fast DDS 会做同类转换，只是落到自己的 QoS object 与 DataWriter/DataReader 配置。

因此：

~~~text
rclcpp QoS API
      |
rmw_qos_profile_t
      |
RMW backend mapper
      |
DDS native QoS
      |
Writer / Reader / History / Reliability
~~~

## 为什么“可靠”不等于“适合控制”

考虑 1 kHz 状态流。

### 方案 A：深历史 + reliable

如果下游暂时卡住，middleware 可能保留更多旧样本并尝试恢复。数据完整性变好，但数据年龄可能增长。

### 方案 B：KEEP_LAST(1) + best effort

旧样本很快被新样本替代。偶发丢样本可以接受，但控制器更容易拿到最新状态。

对于控制系统，真正要优化的经常不是：

\[
P(\text{every sample delivered})
\]

而是：

\[
Age(t)=t-t_{\text{sample}}
\]

所以 QoS 选型要从控制语义出发，而不是默认“Reliable 一定更高级”。

## 一个具体的机器人 QoS 设计问题

### 里程计 / IMU 高频状态

通常关心最新值：

~~~text
KEEP_LAST small depth
BEST_EFFORT or carefully tested RELIABLE
VOLATILE
~~~

重点是数据年龄和 bounded backlog。

### 地图 / 静态配置

可能需要 late join：

~~~text
TRANSIENT_LOCAL
RELIABLE
small meaningful history
~~~

重点是新节点加入后能恢复最近状态。

### 控制命令

必须额外考虑：

- 旧命令是否应被保留；
- deadline miss 是否需要触发 failsafe；
- publisher 消失时怎样检测；
- reliable 重传旧命令是否可能比丢掉它更危险。

“可靠传输”与“控制安全”不是同义词。

## QoS 与 Executor 是两层问题

即使 DDS 层已经可靠、deadline compatibility 完全正确：

~~~text
DDS sample ready
      |
      v
Executor 被长 callback 占住
      |
      v
control callback late
~~~

QoS 无法替代 CPU 调度。

这就是下一篇为什么要进入 WaitSet 和 Executor：通信契约解决的是 endpoint 与数据面，callback 何时执行属于另一个 Runtime。
