# ROS2 Communication Runtime 总览：从 Publisher API 到 DDS，再回到 Executor

本专题固定阅读 ROS 2 Humble 的 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并配合 `rcl@cbaee7c9`、`rmw@566d17a9`、`rmw_dds_common@e26ba107`、`rmw_cyclonedds@e370e09c` 与 `rmw_fastrtps@da0c2d31`。DDS 内部机制继续复用 Atlas 已有的 Cyclone DDS 与 Fast DDS 专题；这里研究的是 ROS2 自己怎样把应用层语义接到这些 Runtime 上。

## 一个 publish 为什么要经过这么多层

假设相机节点执行：

~~~cpp
image_pub_->publish(image);
~~~

最容易想到的实现是：

~~~text
Publisher::publish
    |
    v
socket.send(bytes)
~~~

ROS1 的 TCPROS 已经证明这种模型可以工作。但 ROS2 同时希望处理同进程优化、DDS 实现替换、QoS、统一 WaitSet/Executor，以及部分中间件提供的 loaned message / shared-memory 能力。

真正的数据路径更像：

~~~text
rclcpp::Publisher
      |
      | API / ownership policy
      v
rcl
      |
      | language-neutral C boundary
      v
rmw
      |
      | middleware contract
      +--------------------+
      |                    |
      v                    v
rmw_cyclonedds       rmw_fastrtps
      |                    |
      v                    v
 Cyclone DDS            Fast DDS
      |                    |
      +---------+----------+
                |
           RTPS / SHM
~~~

第一条必须建立的认识是：

> RMW 不是一套新的传输协议。它是一道稳定接口，把 ROS2 上层对象与具体中间件实现隔开。

## rclcpp 先决定消息走哪条路

`Publisher::publish` 并不会无条件进入 DDS。以 unique_ptr 版本为例：

~~~cpp
publish(std::unique_ptr<T, ROSMessageTypeDeleter> msg)
{
  if (!intra_process_is_enabled_) {
    this->do_inter_process_publish(*msg);
    return;
  }

  bool inter_process_publish_needed =
    get_subscription_count() > get_intra_process_subscription_count();

  if (inter_process_publish_needed) {
    auto shared_msg =
      this->do_intra_process_ros_message_publish_and_return_shared(std::move(msg));
    this->do_inter_process_publish(*shared_msg);
  } else {
    this->do_intra_process_ros_message_publish(std::move(msg));
  }
}
~~~

因此同一次 publish 可能得到三种路径：

~~~text
只有本进程 subscriber
    -> IntraProcessManager

只有进程外 subscriber
    -> rcl -> rmw -> DDS

两者同时存在
    -> intra-process
    -> inter-process
~~~

ROS2 的“通信中间件”因此不能只等价为 DDS；rclcpp 自己就拥有一条本进程数据面。

## inter-process 路径很快收敛到 rcl

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

这里没有自己实现序列化器、RTPS Writer 或 socket。模板化 C++ Publisher 被压缩为一个 `rcl_publisher_t` handle 和消息地址。

rcl 的核心同样很薄：

~~~c
rcl_ret_t
rcl_publish(
  const rcl_publisher_t * publisher,
  const void * ros_message,
  rmw_publisher_allocation_t * allocation)
{
  if (rmw_publish(
      publisher->impl->rmw_handle,
      ros_message,
      allocation) != RMW_RET_OK)
  {
    return RCL_RET_ERROR;
  }
  return RCL_RET_OK;
}
~~~

于是跨语言和跨 middleware 的边界被拆成：

~~~text
C++ template world
      |
      v
rcl C ABI
      |
      v
rmw implementation ABI
~~~

## 同一个 rmw_publish 可以落到两套不同实现

Cyclone RMW：

~~~cpp
auto pub = static_cast<CddsPublisher *>(publisher->data);

if (dds_write(pub->enth, ros_message) >= 0) {
  return RMW_RET_OK;
}
return RMW_RET_ERROR;
~~~

Fast DDS RMW：

~~~cpp
auto info = static_cast<CustomPublisherInfo *>(publisher->data);

rmw_fastrtps_shared_cpp::SerializedData data;
data.is_cdr_buffer = false;
data.data = const_cast<void *>(ros_message);
data.impl = info->type_support_impl_;

if (!info->data_writer_->write(&data)) {
  return RMW_RET_ERROR;
}
~~~

RMW 的价值不是把底层做成一样，而是让上层只依赖相同的语义入口。

## 接收方向不是 publish 的简单镜像

发布可以由用户线程同步调用。订阅端则通常先等待“什么东西 ready 了”：

~~~text
DDS / rmw readiness
      |
      v
rcl_wait()
      |
      v
Executor::wait_for_work()
      |
      v
get_next_ready_executable()
      |
      v
take_type_erased()
      |
      v
rcl_take()
      |
      v
rmw_take_with_info()
      |
      v
user callback
~~~

所以必须区分：

- 数据已经到达 middleware；
- Executor 已经被唤醒；
- subscription 被选中；
- 消息已经 take；
- callback 真正获得 CPU。

对于控制链，数据年龄可以粗略写成：

\[
T_{age}
=
T_{transport}
+
T_{reader}
+
T_{wait}
+
T_{executor}
+
T_{callback\ queue}
\]

DDS latency 只是其中一部分。

## Executor 是执行平面，不是网络线程

`Executor::wait_for_work` 会收集 subscription、timer、client、service、guard condition 和 event，然后填充 `rcl_wait_set_t`。底层等待返回以后，Executor 才挑选 `AnyExecutable`。

因此：

~~~text
DDS receive thread / kernel readiness
             !=
ROS callback execution thread
~~~

如果同一个 Executor 里存在一个 20 ms 的视觉 callback 和一个 1 ms 周期控制 callback，网络即使准时到达，也不能自动保证控制 callback 准时执行。

## QoS 是运行时契约

`rclcpp::QoS` 最终保存 `rmw_qos_profile_t`。例如 reliable：

~~~cpp
QoS &
QoS::reliable()
{
  return this->reliability(
    RMW_QOS_POLICY_RELIABILITY_RELIABLE);
}
~~~

这个 profile 由 RMW 映射到 DDS QoS。它会影响 endpoint matching、History、重传、样本积压以及 deadline/liveliness 事件。

所以 QoS 不是一组“网络参数”，而是通信双方的协议契约。

## 没有 Master，不代表没有 Graph

DDS 能发现 Participant、Writer、Reader，但 ROS 用户还会查询 Node name、namespace、topic/service ownership。`rmw_dds_common::GraphCache` 负责维护这些 ROS 层映射。

~~~text
DDS discovery
    |
    | participant / writer / reader
    v
RMW
    |
    | map to ROS identities
    v
ROS GraphCache
~~~

ROS1 Master 被删除后，中心 graph service 消失了，但 graph 这个数据结构和一致性问题没有消失。

## Intra-process 与 Loaned Message 不是同一件事

Intra-process 的目标是同进程时绕过 DDS 数据面，核心对象是 `IntraProcessManager`。

Loaned Message 的目标是让应用直接在 middleware 提供的内存上构造消息，核心对象是 `LoanedMessage` 和 RMW loan API。

Humble 固定源码甚至明确禁止 loaned message 直接进入 rclcpp intra-process：

~~~cpp
if (intra_process_is_enabled_) {
  throw std::runtime_error(
    "storing loaned messages in intra process is not supported yet");
}
~~~

所以“ROS2 支持 zero-copy”必须继续追问：哪条 path、哪种 RMW、哪类消息、哪种 transport？

## 本专题的完整地图

~~~text
                         Application
                             |
                  rclcpp Publisher/Subscription
                    /                    \
                   /                      \
          intra-process                 inter-process
               |                             |
       IntraProcessManager                   v
                                      rcl publish/take
                                             |
                                             v
                                           rmw
                                   /                    \
                          rmw_cyclonedds            rmw_fastrtps
                                |                        |
                           Cyclone DDS                Fast DDS
                                   \                    /
                                    RTPS / SHM / UDP

receive:
DDS/RMW readiness
      -> rcl_wait
      -> Executor
      -> take
      -> CallbackGroup
      -> user callback
~~~

后续文章就沿这张图逐层拆开，而不是按照 ROS2 包名顺序阅读。
