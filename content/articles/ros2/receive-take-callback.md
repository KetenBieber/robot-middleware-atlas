# 从 Reader 到用户 Callback：ROS2 为什么先 wait，再 take，再 execute

本篇主固定版本为 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，跨层核对 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194` 与 `rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e`。

## “收到消息”至少有五种不同含义

在 ROS2 里，下列事件不是同一时刻：

~~~text
1. network / SHM data arrives
2. DDS Reader has sample
3. RMW marks subscription ready
4. Executor takes sample
5. user callback starts
~~~

如果把这些阶段全部称为“收到”，分析 latency 和 jitter 时会非常混乱。

## Executor 先根据 readiness 选中 Subscription

当 `AnyExecutable` 中包含 subscription：

~~~cpp
if (any_exec.subscription) {
  execute_subscription(any_exec.subscription);
}
~~~

这里还没有直接调用用户 callback。`execute_subscription` 先决定消息怎样被取出。

## 普通 inter-process path 会先准备 message storage

固定源码：

~~~cpp
std::shared_ptr<void> message =
  subscription->create_message();

take_and_do_error_handling(
  "taking a message from topic",
  subscription->get_topic_name(),
  [&]() {
    return subscription->take_type_erased(
      message.get(), message_info);
  },
  [&]() {
    subscription->handle_message(
      message, message_info);
  });

subscription->return_message(message);
~~~

这段代码清楚分成三步：

~~~text
allocate/get storage
      |
      v
take middleware sample
      |
      v
invoke user handler
~~~

callback 并不是 DDS receive thread 直接调用的。

## 为什么 wait ready 后 take 仍可能失败

`take_and_do_error_handling` 里有一段很关键的逻辑：

~~~cpp
bool taken = false;

try {
  taken = take_action();
} catch (...) {
  // error handling
}

if (taken) {
  handle_action();
} else {
  // middleware may have interrupted wait spuriously
}
~~~

这和 condition_variable 很类似：

~~~cpp
cv.wait(lock);
if (condition) {
  // proceed
}
~~~

正确模型是“被唤醒后重新检查”，而不是“被唤醒即等价于条件成立”。

## rclcpp 的 type-erased take 为什么存在

Subscription 模板层知道 MessageT，但 Executor 必须统一处理很多不同 message type。

因此执行层使用：

~~~text
void * message_out
~~~

作为 type-erased 存储，再通过 subscription 内部的 type support 还原类型语义。

如果没有这层 type erasure，Executor 就会被迫了解每一个业务消息类型：

~~~cpp
if (type == Image) ...
else if (type == Odometry) ...
else if (type == LaserScan) ...
~~~

这显然无法扩展。

## rcl_take 再把路径压到 RMW

固定源码：

~~~c
rcl_ret_t
rcl_take(
  const rcl_subscription_t * subscription,
  void * ros_message,
  rmw_message_info_t * message_info,
  rmw_subscription_allocation_t * allocation)
{
  bool taken = false;

  rmw_ret_t ret = rmw_take_with_info(
    subscription->impl->rmw_handle,
    ros_message,
    &taken,
    message_info_local,
    allocation);

  if (ret != RMW_RET_OK) {
    return rcl_convert_rmw_ret_to_rcl_ret(ret);
  }

  if (!taken) {
    return RCL_RET_SUBSCRIPTION_TAKE_FAILED;
  }

  return RCL_RET_OK;
}
~~~

与 publish 一样，rcl 自己不实现 DDS Reader queue。

## Cyclone RMW 再进入自己的 take 实现

对外入口：

~~~cpp
extern "C" rmw_ret_t rmw_take_with_info(
  const rmw_subscription_t * subscription,
  void * ros_message,
  bool * taken,
  rmw_message_info_t * message_info,
  rmw_subscription_allocation_t * allocation)
{
  static_cast<void>(allocation);

  return rmw_take_int(
    subscription,
    ros_message,
    taken,
    message_info);
}
~~~

下一层才处理 Cyclone DDS sample/type support。

接收主线因此是：

~~~text
DDS Reader
   |
rmw_take_with_info
   |
rcl_take
   |
SubscriptionBase::take_type_erased
   |
Executor
   |
user callback
~~~

## Serialized、Loaned、普通消息是三条不同接收 path

Executor 固定源码先判断：

~~~text
is_serialized?
    |
    +-> take serialized buffer

can_loan_messages?
    |
    +-> take loaned sample
        callback
        return loan

otherwise
    |
    +-> create local message
        take/copy into it
        callback
~~~

这说明“Subscriber 收到一条消息”不能自动推导出复制次数。

### 普通 message

middleware 可能需要把 sample materialize/copy 到 ROS message storage。

### serialized message

上层拿的是序列化 buffer，不要求立即构造成 typed ROS object。

### loaned message

middleware 提供 sample memory，callback 用完后归还。

## Loaned subscription 的寿命边界就在 callback 外面

Executor 对 loaned message 的结构是：

~~~text
rcl_take_loaned_message
        |
        v
handle_loaned_message(callback)
        |
        v
rcl_return_loaned_message_from_subscription
~~~

因此 callback 获得的 loan memory 不是可以无限保存的普通对象。

这也是 zero-copy API 必须显式表达 lifetime 的原因：减少 copy 的代价，是 ownership contract 更严格。

## MessageInfo 为什么和 payload 一起 take

`rmw_message_info_t` 带有和 sample 来源、时间、publisher GID 等相关的 metadata。

把 metadata 与 payload 同一次 take 获取，可以保证用户看到的 message info 对应当前这一个 sample。

如果 metadata 通过另一条异步查询路径取得，就可能出现 sample A 和 metadata B 错配。

## DDS History 与 Executor queue 不是同一个队列

ROS1 常看到：

~~~text
TCP data
  |
SubscriptionQueue
  |
CallbackQueue
~~~

ROS2 里数据积压可能首先发生在 DDS Reader History。

Executor WaitSet 描述的是“这个 subscription 有工作 ready”，并不把每一个 DDS sample 全部搬成独立的 Executor task。

因此 queue depth 和 callback backlog 要分层看：

~~~text
DDS Reader History
       |
     take
       |
Executor callback execution
~~~

这也是为什么 QoS History 与 Executor 调度必须分开分析。

## 一个具体的 stale-data 场景

假设 100 Hz odometry，每 10 ms 新样本一条。

控制 callback 因另一个任务阻塞 50 ms：

~~~text
0ms   sample 0
10ms  sample 1
20ms  sample 2
30ms  sample 3
40ms  sample 4
50ms  executor finally runs
~~~

此时实际取到哪条、积压多少条，受 Reader History/QoS 与 take 行为影响。

如果控制器真正关心的是最新状态，就应该同时设计：

- 小而有界的 History；
- 合理 Reliability；
- 不被长 callback 阻塞的 Executor；
- callback 内检查 message timestamp。

仅仅把 depth 调大，可能只是让旧数据保存得更久。

## 接收链的工程结论

ROS2 的 end-to-end latency 至少要拆成：

\[
T_{e2e}
=
T_{transport}
+
T_{reader}
+
T_{wait}
+
T_{take}
+
T_{executor}
+
T_{callback}
\]

其中任何一层都可能造成 jitter。

性能测试不应该只 ping DDS，也不应该只测 callback 周期；要沿这条链分别打 timestamp 才能知道瓶颈在哪里。
