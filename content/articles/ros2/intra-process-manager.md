# IntraProcessManager：同一进程里为什么还需要一套消息 Runtime

本篇固定 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`。这里研究的是 rclcpp 自己拥有的数据面：当 Publisher 与 Subscription 位于同一个进程时，消息可以绕过 RMW/DDS 的跨进程链路。

## 为什么同进程还走 DDS 会显得浪费

假设两个 composable nodes 被装进同一个进程：

~~~text
camera component
      |
      v
detector component
~~~

如果仍然走完整 inter-process path：

~~~text
ROS object
  |
DDS/type support
  |
writer/history
  |
reader
  |
ROS object
~~~

即使底层 DDS 最终能使用共享内存，仍然需要 endpoint、History、type support 与 middleware 状态。

rclcpp 因此提供另一条本地数据面：

~~~text
Publisher
   |
IntraProcessManager
   |
subscription buffer
   |
Executor
   |
callback
~~~

这条链不进入 `rcl_publish -> rmw_publish -> DDS Writer`。

## Manager 的第一项工作：登记本进程 endpoints

固定接口：

~~~cpp
uint64_t
add_subscription(
  SubscriptionIntraProcessBase::SharedPtr subscription);

uint64_t
add_publisher(
  rclcpp::PublisherBase::SharedPtr publisher);
~~~

每个 endpoint 获得一个进程内 unique id。

核心容器：

~~~cpp
using SubscriptionMap =
  std::unordered_map<
    uint64_t,
    SubscriptionIntraProcessBase::WeakPtr>;

using PublisherMap =
  std::unordered_map<
    uint64_t,
    rclcpp::PublisherBase::WeakPtr>;

using PublisherToSubscriptionIdsMap =
  std::unordered_map<
    uint64_t,
    SplittedSubscriptions>;
~~~

为什么 registry 保存 weak_ptr？

因为 Manager 负责“索引”而不是“拥有”。如果这里保存强引用，那么一个 Publisher 即使业务对象已经释放，也可能仅因为 registry 里还有 shared_ptr 而继续存活。

weak_ptr 让生命周期仍由真实 owner 决定：

~~~text
Node/Publisher owner dies
       |
weak_ptr expires
       |
registry can clean stale entry
~~~

## 为什么 publisher -> subscriptions 要提前建索引

最朴素的本进程 publish 可以写成：

~~~cpp
for (auto & sub : all_subscriptions) {
  if (compatible(pub, sub)) {
    deliver(sub);
  }
}
~~~

但这会把 topic、type、QoS 匹配放进每一次 publish 的热路径。

如果系统有 \(P\) 个 publisher、\(S\) 个 subscription，而每个 publisher 都高频发布，反复扫描会浪费大量控制面工作。

IntraProcessManager 在 endpoint 注册/删除时维护：

~~~text
publisher id
    |
    +-> matching subscription id
    +-> matching subscription id
    +-> ...
~~~

数据面直接查预计算关系。

这是常见的 Runtime 设计原则：

> 把变化较慢的拓扑计算放到控制面，把高频热路径压缩成索引查找。

## 为什么 matched subscriptions 还要分成两组

固定数据结构：

~~~cpp
struct SplittedSubscriptions
{
  std::vector<uint64_t> take_shared_subscriptions;
  std::vector<uint64_t> take_ownership_subscriptions;
};
~~~

两组不是性能标签，而是 ownership contract 不同。

### shared subscriptions

接收端可以接受：

~~~text
shared_ptr<const T>
~~~

多个 subscription 可以共同观察同一个不可变对象。

### ownership subscriptions

接收端希望获得：

~~~text
unique_ptr<T>
~~~

一个对象不可能同时被 move 给两个 owner。

因此 fan-out 必须解决：

> 一个输入 unique_ptr 怎样服务多个需要独占所有权的 consumer？

## 没有 ownership consumer 时：直接提升为 shared_ptr

固定源码：

~~~cpp
if (sub_ids.take_ownership_subscriptions.empty()) {
  std::shared_ptr<MessageT> msg = std::move(message);

  this->template add_shared_msg_to_buffers<
    MessageT, Alloc, Deleter, ROSMessageType>(
      msg,
      sub_ids.take_shared_subscriptions);
}
~~~

这里没有复制 payload 对象本身。

所有 shared subscriber 指向同一个消息实例，只增加 shared ownership bookkeeping。

## 多个 ownership consumer 时：为什么复制不可避免

固定源码在向 ownership subscribers 分发时：

~~~cpp
for (auto it = subscription_ids.begin();
     it != subscription_ids.end(); it++)
{
  if (std::next(it) == subscription_ids.end()) {
    subscription->provide_intra_process_data(
      std::move(message));
    break;
  } else {
    auto ptr = MessageAllocTraits::allocate(
      allocator, 1);

    MessageAllocTraits::construct(
      allocator, ptr, *message);

    subscription->provide_intra_process_data(
      MessageUniquePtr(ptr, deleter));
  }
}
~~~

最后一个 subscriber 可以拿走原始 unique_ptr。

前面的 subscriber 必须各自得到独立对象。

如果有 \(N\) 个 consumer 都要求独占可变对象，那么至少需要 \(N\) 份独立状态。这个 copy 不是“实现不够聪明”，而是 ownership 语义本身要求的。

## shared 与 ownership 混合时为什么还要再复制一份

源码的另一条分支：

~~~cpp
auto shared_msg =
  std::allocate_shared<MessageT, MessageAllocatorT>(
    allocator, *message);

add_shared_msg_to_buffers(
  shared_msg,
  sub_ids.take_shared_subscriptions);

add_owned_msg_to_buffers(
  std::move(message),
  sub_ids.take_ownership_subscriptions,
  allocator);
~~~

原始 unique object 留给 ownership path。

另外复制一份形成 shared object，供 shared subscribers 使用。

所以“同进程通信 = zero-copy”是不准确的。

真正决定 copy 数量的是：

- Publisher 传入 const ref 还是 unique_ptr；
- subscriber 需要 shared 还是 unique ownership；
- fan-out 数量；
- TypeAdapter 是否发生转换；
- 本地与远端 subscriber 是否同时存在。

## shared_timed_mutex 为什么在这里

内部 registry 与 pub-to-sub 关系会在 endpoint 创建/销毁时修改，而 publish 热路径需要读取。

固定类中：

~~~cpp
mutable std::shared_timed_mutex mutex_;
~~~

发布路径使用 shared lock：

~~~cpp
std::shared_lock<std::shared_timed_mutex> lock(mutex_);
~~~

直觉是：

~~~text
many concurrent publishers/readers
        |
     shared lock

topology update
        |
   exclusive lock
~~~

拓扑变化远低于 publish 频率时，这比所有 publish 都抢独占锁更合理。

但它仍然不是 lock-free；高并发系统仍需把 registry contention 纳入性能分析。

## 本地与远端 subscriber 同时存在怎么办

Publisher 源码会比较：

~~~cpp
bool inter_process_publish_needed =
  get_subscription_count() >
  get_intra_process_subscription_count();
~~~

如果远端 subscriber 存在，一次 publish 可以分成：

~~~text
             message
            /       \
           /         \
local IntraProcess   rcl/rmw/DDS
      |                   |
local subscriber     remote subscriber
~~~

也就是说，开启 intra-process 并不意味着这个 Publisher 从此不再进入 DDS。

它只是为本地匹配 endpoint 增加一条旁路。

## 这和 ROS1 Nodelet 的共同点

ROS1 Nodelet 的核心目标同样是：

> 把组件加载进同一进程，避免 TCPROS serialize/deserialize 和进程间搬运。

可以对应成：

~~~text
ROS1
Nodelet Manager
    |
Nodelet plugins
    |
roscpp intraprocess links

ROS2
Composable Nodes
    |
rclcpp Publisher/Subscription
    |
IntraProcessManager
~~~

共同本质都是：

~~~text
same address space
      |
object ownership transfer
      |
avoid network serialization path
~~~

ROS2 的差别是它必须同时和 RMW、QoS、Executor、CallbackGroup、TypeAdapter 等机制组合。

## 同进程通信仍然不是“免费性能”

减少了 transport/serialization，不代表代价归零。

可能仍然存在：

- allocation；
- fan-out copy；
- shared_ptr refcount；
- shared_timed_mutex；
- TypeAdapter conversion；
- Executor scheduling；
- cache contention。

更完整的成本模型是：

\[
C =
C_{serialization}
+
C_{copy}
+
C_{allocation}
+
C_{ownership}
+
C_{sync}
+
C_{scheduling}
\]

intra-process 主要减少其中几项，而不是让 \(C=0\)。

## 对图像和点云为什么特别有价值

假设单帧点云 5 MB，20 Hz：

\[
5\text{ MB}\times20
=
100\text{ MB/s}
\]

如果 pipeline：

~~~text
driver -> filter -> detector -> planner
~~~

每跳都 serialize/copy，会快速消耗 memory bandwidth。

对于这类大 payload，同进程 object sharing 的收益明显。

但对于几十字节的控制命令，延迟通常更多受 scheduling、锁和数据年龄影响，而不是 memcpy。

## 一个框架设计者应该记住的原则

IntraProcessManager 展示了一个很典型的设计套路：

1. 控制面提前建立匹配关系；
2. 热路径只查预计算索引；
3. ownership 语义直接决定数据结构与复制策略；
4. registry 不拥有业务对象，因此用 weak_ptr；
5. topology read 多、write 少，因此使用 shared/exclusive lock；
6. 本地优化不能破坏远端通信。

它不是一个“ROS 专用技巧”，而是一套可以迁移到其他 Runtime 的对象模型。

下一篇继续研究另一种完全不同的低复制机制：Loaned Message。它不是绕开 middleware，而是让 middleware 直接提供 sample storage。
