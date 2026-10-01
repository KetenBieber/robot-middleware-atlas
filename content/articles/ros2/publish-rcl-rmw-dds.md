# 一次 publish 的真实路径：rclcpp → rcl → rmw → DDS

本篇主固定版本为 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，跨层核对 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`、`rmw@566d17a97e87bc5336ce33fb025ce8e952a3326a`、`rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e` 与 `rmw_fastrtps@da0c2d3120e9b5ea85cca251ea1f48ca8d0a93d7`。

## publish 返回时到底发生了什么

用户写：

~~~cpp
publisher->publish(msg);
~~~

常见的两个误解分别是“这里只是入一个 ROS 队列，所以一定立即返回”和“返回时对端 callback 已经消费完成”。

ROS2 的真实行为介于两者之间。上层会同步调用到 RMW，具体 DDS backend 再决定 serialization、Writer History、flow control 和 transport 行为。rcl 的接口文档甚至明确把 `rcl_publish()` 标记为 potentially blocking。

因此真正要问的是：

> 同步边界在哪里？消息所有权在哪里变化？哪一层开始依赖具体 DDS？

## 第一层：rclcpp 决定 ownership

普通引用版本：

~~~cpp
publish(const T & msg)
{
  if (!intra_process_is_enabled_) {
    return this->do_inter_process_publish(msg);
  }

  auto unique_msg = this->duplicate_ros_message_as_unique_ptr(msg);
  this->publish(std::move(unique_msg));
}
~~~

没有 intra-process 时，引用直接向下传，避免 rclcpp 先复制。启用 intra-process 后，rclcpp 需要获得一份可转移所有权的对象，所以构造 unique_ptr。

如果用户本来就交出 unique_ptr，则可以避免这一步额外复制。

## intra + inter 同时存在为什么需要 shared ownership

固定源码：

~~~cpp
bool inter_process_publish_needed =
  get_subscription_count() > get_intra_process_subscription_count();

if (inter_process_publish_needed) {
  auto shared_msg =
    this->do_intra_process_ros_message_publish_and_return_shared(
      std::move(msg));
  this->do_inter_process_publish(*shared_msg);
}
~~~

朴素实现如果这样写：

~~~cpp
intra_publish(std::move(msg));
dds_publish(*msg);   // ownership 已经被移走
~~~

第二步没有合法对象可以读。

所以 shared_ptr 在这里不是装饰，而是在“本进程 ownership transfer”和“随后跨进程只读发送”之间建立一个共同寿命。

## 第二层：rclcpp 到 rcl

~~~cpp
void
do_inter_process_publish(const ROSMessageType & msg)
{
  TRACEPOINT(rclcpp_publish, nullptr, static_cast<const void *>(&msg));
  auto status = rcl_publish(publisher_handle_.get(), &msg, nullptr);

  if (RCL_RET_OK != status) {
    rclcpp::exceptions::throw_from_rcl_error(
      status, "failed to publish message");
  }
}
~~~

模板化 C++ Publisher 到这里收敛成：

~~~text
rcl_publisher_t *
const void * message
~~~

这就是 rcl 可以保持 C ABI 的原因。

## 第三层：rcl 几乎不处理 payload

~~~c
rcl_ret_t
rcl_publish(
  const rcl_publisher_t * publisher,
  const void * ros_message,
  rmw_publisher_allocation_t * allocation)
{
  if (!rcl_publisher_is_valid(publisher)) {
    return RCL_RET_PUBLISHER_INVALID;
  }

  if (rmw_publish(
      publisher->impl->rmw_handle,
      ros_message,
      allocation) != RMW_RET_OK)
  {
    RCL_SET_ERROR_MSG(rmw_get_error_string().str);
    return RCL_RET_ERROR;
  }

  return RCL_RET_OK;
}
~~~

rcl 负责 handle validity、参数检查、trace 和错误码翻译；它并不知道 Cyclone DDS 还是 Fast DDS。

如果 rclcpp/rclpy 都直接适配每一种 middleware，组合复杂度会接近客户端数量乘 backend 数量。rcl + rmw 把它重新拆成两条独立扩展轴。

## 第四层：RMW contract 开始分叉

### Cyclone DDS backend

~~~cpp
auto pub = static_cast<CddsPublisher *>(publisher->data);
TRACEPOINT(rmw_publish, ros_message);

if (dds_write(pub->enth, ros_message) >= 0) {
  return RMW_RET_OK;
}

RMW_SET_ERROR_MSG("failed to publish data");
return RMW_RET_ERROR;
~~~

`publisher->data` 在这里被解释成 Cyclone 专用的 `CddsPublisher`，最终调用 `dds_write`。

### Fast DDS backend

~~~cpp
auto info = static_cast<CustomPublisherInfo *>(publisher->data);

rmw_fastrtps_shared_cpp::SerializedData data;
data.is_cdr_buffer = false;
data.data = const_cast<void *>(ros_message);
data.impl = info->type_support_impl_;

if (!info->data_writer_->write(&data)) {
  RMW_SET_ERROR_MSG("cannot publish data");
  return RMW_RET_ERROR;
}
~~~

这里同一个 rmw handle 被解释成 Fast DDS 专用对象，最终进入 `DataWriter::write`。

RMW 统一的是 ROS 需要的 contract，而不是强迫底层实现相同的数据结构。

## 序列化到底在哪发生

普通 typed publish 向下传的是 ROS message 对象地址，不是必然已经序列化好的 byte buffer。

Fast DDS RMW 对 typed 和 serialized path 甚至使用不同标记：

~~~cpp
SerializedData data;
data.is_cdr_buffer = false;
data.data = const_cast<void *>(ros_message);
data.impl = info->type_support_impl_;
~~~

如果用户直接 publish serialized message，则它构造 CDR view：

~~~cpp
eprosima::fastcdr::FastBuffer buffer(
  reinterpret_cast<char *>(serialized_message->buffer),
  serialized_message->buffer_length);

SerializedData data;
data.is_cdr_buffer = true;
data.data = &ser;
data.impl = nullptr;
~~~

所以“ROS2 在哪一层序列化”没有脱离 backend 和 API path 的唯一答案。

## publish 返回不等于 subscriber 已经执行

应用线程看到：

~~~text
user thread
   |
Publisher::publish
   |
rcl_publish
   |
rmw_publish
   |
DDS Writer
   |
return
~~~

而 subscriber callback 是另一条链：

~~~text
transport receive
   |
Reader / History
   |
rmw readiness
   |
Executor wakeup
   |
take
   |
callback
~~~

因此 publish return 不能证明对端已经收到、已经 take 或 callback 已经执行。

Reliable QoS 也不是“业务 callback completion ack”。

## 背压不再只是 TCP send buffer

DDS 路径中可能存在：

~~~text
Application
  |
RMW
  |
DDS Writer
  |
Writer History
  |
Flow Controller
  |
Transport / SHM
~~~

阻塞或延迟可能来自 History resource limit、reliable reader 未确认样本、异步 flow controller、共享内存 chunk 不足或 transport buffer。

所以 1 kHz 控制线程里直接调用 publish 是否安全，不能仅凭“网络很快”判断。

更稳妥的结构通常是：

~~~text
control loop
    |
    | bounded handoff
    v
communication worker
    |
    v
ROS2 publisher
~~~

除非已经对当前 RMW、QoS 和负载做过 worst-case 验证。

## 与 ROS1 的本质差异

ROS1 的主线很短：

~~~text
roscpp -> TCPROS -> TCP socket
~~~

ROS2 则是：

~~~text
rclcpp -> rcl -> rmw -> DDS
                     |
                     +-> History / Reliability / RTPS / SHM
~~~

ROS2 牺牲了调用链的简单性，换来 middleware 可替换、QoS、分布式 discovery 和多种数据面能力。
