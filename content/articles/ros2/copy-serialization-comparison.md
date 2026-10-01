# ROS1 vs ROS2 消息复制与序列化：一帧图像到底被搬了几次

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。这里不使用“ROS1 会复制、ROS2 zero-copy”这种过度简化的说法，而是把对象 ownership、serialization 与 transport 分开。

Supporting backends: `rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e`，`rmw_fastrtps@da0c2d3120e9b5ea85cca251ea1f48ca8d0a93d7`。ROS2 同进程对象分发由 `IntraProcessManager` 负责，而跨进程路径继续进入 RMW/DDS。


## 1. copy 至少有三种

“复制”可能指：

1. C++ 对象拷贝；
2. serialize 到字节 buffer；
3. 内核或网络栈搬运。

所以一条路径可能没有 C++ 对象 copy，却仍然发生 serialization；也可能没有网络 copy，却在 fan-out 时复制对象。

## 2. ROS1 跨进程最终必须进入 TCPROS bytes

roscpp 的 `Publication::getPublishTypes()` 会询问 subscriber link 是否需要 serialized representation、是否可以 no-copy。

~~~text
ros_comm/clients/roscpp/src/libros/publication.cpp
  Publication::getPublishTypes()

ros_comm/clients/roscpp/src/libros/topic_manager.cpp
  TopicManager::publish()
~~~

典型跨进程路径：

~~~text
C++ message
   |
Serializer<T>
   |
SerializedMessage
   |
TCPROS frame
   |
kernel socket buffer
~~~

关键不是“ROS1 永远先复制一次”，而是跨进程 TCPROS 的 wire representation 必须是 serialized bytes。

## 3. ROS1 Nodelet：no-copy 来自同地址空间

Nodelet 把多个组件装进同一个进程，再利用 roscpp intra-process link 直接传对象 ownership。

~~~text
shared_ptr<Message>
     |
same process
     |
intra-process subscriber link
     |
callback
~~~

因此 ROS1 的低复制核心条件是 publisher 与 subscriber 共享地址空间。

## 4. ROS2 inter-process：复制点由 backend 决定

ROS2 普通路径：

~~~text
rclcpp message
  -> rcl_publish
  -> rmw_publish
  -> DDS implementation
~~~

Cyclone DDS RMW 的 `rmw_publish` 进入 `dds_write`；Fast DDS RMW 则进入 `DataWriter::write` 路径。

这意味着从 rclcpp 层看不到所有 copy。还必须继续确认：

- type support 是否生成 CDR；
- DDS Writer 是否保留 history sample；
- transport 是 UDP、SHM 还是 Data Sharing；
- reader take 是否重新构造 ROS object。

## 5. ROS2 intra-process：第一层是 ownership transfer

`Publisher::publish` 在 intra-process 启用时进入 `do_intra_process_publish()`。

~~~text
rclcpp/include/rclcpp/publisher.hpp
rclcpp/include/rclcpp/experimental/intra_process_manager.hpp
~~~

核心对象包括：

~~~cpp
std::unique_ptr<MessageT, Deleter>
std::shared_ptr<const MessageT>
~~~

所以同进程路径的本质不是“共享内存 transport”，而是对象已经处于同一 address space，运行时只需要设计 ownership。

## 6. 同时存在本地与远端 subscriber

publisher 源码明确计算：

~~~cpp
bool inter_process_publish_needed =
  get_subscription_count() > get_intra_process_subscription_count();
~~~

因此一次 publish 可以同时：

~~~text
同进程 subscriber
   <- intra-process object path

远端 subscriber
   <- inter-process RMW/DDS path
~~~

启用 intra-process 不意味着整条发布路径完全没有 serialization。

## 7. LoanedMessage：第二层优化

`LoanedMessage` 允许应用从 middleware 借 storage：

~~~text
borrow
  -> construct in place
  -> publish
  -> middleware recycle
~~~

但是否真的 zero-copy 还取决于：

- `can_loan_messages()`；
- RMW backend；
- message type；
- DDS transport；
- reader 端是否继续 loan；
- fan-out 是否需要额外 storage。

borrow 成功不等于 producer 到所有 consumers 全链 zero-copy。

## 8. DDS SHM/Data Sharing：第三层优化

ROS2 低复制至少有三层：

~~~text
Layer 1: rclcpp intra-process ownership transfer
Layer 2: RMW loan contract
Layer 3: DDS shared-memory/data-sharing transport
~~~

三者不能合并成一个“zero-copy 开关”。

## 9. 4 MB 图像的四条路径

ROS1 普通跨进程：

~~~text
object -> serialize -> TCP/kernel -> receive -> deserialize
~~~

ROS1 Nodelet：

~~~text
shared_ptr<Image> -> callback
~~~

ROS2 普通 DDS：

~~~text
object -> RMW/type support -> DDS history -> transport -> take -> object
~~~

ROS2 optimized：

~~~text
intra-process unique_ptr/shared_ptr
or
loaned DDS sample + SHM/Data Sharing
~~~

不存在固定的“ROS2 复制两次”答案。正确做法是沿当前 backend 的实际 storage ownership 逐段证明。

## 10. 性能审计该记录什么

对图像、点云、tensor，至少固定：

- payload size；
- 是否同进程；
- intra-process 是否开启；
- RMW implementation；
- DDS transport；
- loan support；
- serialization CPU time；
- memory bandwidth；
- subscriber fan-out 数。

“zero-copy”不是一个标签，而是一条需要逐段证明的 ownership path。
