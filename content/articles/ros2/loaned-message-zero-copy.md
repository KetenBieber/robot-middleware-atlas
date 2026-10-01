# Loaned Message 与 Zero-copy：谁分配消息内存，谁负责归还

本篇主固定版本为 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，backend 核对 `rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e` 与 `rmw_fastrtps@da0c2d3120e9b5ea85cca251ea1f48ca8d0a93d7`。

## zero-copy 的真正问题不是“少一次 memcpy”

普通 publish 常见的所有权关系是：

~~~text
application allocates ROS message
        |
        v
middleware accepts/copies/serializes
        |
        v
writer/history/transport storage
~~~

如果 payload 是几 MB 的图像或点云，重复 allocation 和 copy 会明显增加 memory bandwidth 与 cache pressure。

Loaned Message 改变的第一件事不是网络协议，而是：

> 应用不再默认拥有最初那块 message storage，而是向 middleware 借一块。

## LoanedMessage 构造时先询问 capability

固定源码：

~~~cpp
if (pub_.can_loan_messages()) {
  void * message_ptr = nullptr;

  auto ret = rcl_borrow_loaned_message(
    pub_.get_publisher_handle().get(),
    rosidl_typesupport_cpp::
      get_message_type_support_handle<MessageT>(),
    &message_ptr);

  message_ = static_cast<MessageT *>(message_ptr);
} else {
  message_ = message_allocator_.allocate(1);
  new (message_) MessageT();
}
~~~

因此同一份用户代码可能有两种运行模式：

~~~text
middleware supports loan
      |
      v
borrow middleware memory

middleware does not support loan
      |
      v
fallback to local allocator
~~~

这说明一个很重要的边界：

> 使用 LoanedMessage API 不等于运行时一定发生 middleware zero-copy。

## 为什么 fallback 是必要的

如果 API 只在某一个 RMW 支持 loan 时才能工作，应用会被 backend 锁死。

fallback 允许用户保持同一套调用结构：

~~~cpp
auto loan = publisher->borrow_loaned_message();
loan.get().data = ...;
publisher->publish(std::move(loan));
~~~

但性能语义可能改变。

所以性能报告如果只写“使用 LoanedMessage”，却不写 RMW、消息类型和 `can_loan_messages()`，结论是不完整的。

## RAII 为什么特别适合 loan

LoanedMessage 析构时：

~~~cpp
if (pub_.can_loan_messages()) {
  rcl_return_loaned_message_from_publisher(
    pub_.get_publisher_handle().get(),
    message_);
} else {
  message_->~MessageT();
  message_allocator_.deallocate(message_, 1);
}

message_ = nullptr;
~~~

这里建立了明确的 lifetime contract：

~~~text
LoanedMessage alive
       |
message memory valid
       |
publish / destructor
       |
middleware receives it
or memory is returned
~~~

如果只返回裸指针而没有 RAII，异常路径很容易忘记 return loan。

对于有限 chunk pool，这会最终表现为资源耗尽，而不是普通的“小内存泄漏”。

## publish 时 ownership 真正在哪里转移

固定源码：

~~~cpp
if (this->can_loan_messages()) {
  this->do_loaned_message_publish(
    std::move(loaned_msg.release()));
} else {
  this->do_inter_process_publish(
    loaned_msg.get());
}
~~~

两条分支看起来只有几行，ownership 含义完全不同。

### 真正的 middleware loan

`release()` 后，LoanedMessage 不再负责这块内存。

~~~text
Application
   |
   | release ownership
   v
RMW / DDS backend
   |
   | recycle according to backend protocol
   v
memory pool
~~~

### fallback

没有调用 release。

普通 inter-process publish 读取本地对象；函数结束后 LoanedMessage 仍然拥有它，随后 destructor 用本地 allocator 回收。

所以调用代码相同，内存路径却可能完全不同。

## 为什么 Humble 不允许 loaned message 直接进入 rclcpp intra-process

Publisher 固定源码明确检查：

~~~cpp
if (intra_process_is_enabled_) {
  throw std::runtime_error(
    "storing loaned messages in intra process is not supported yet");
}
~~~

从 ownership 角度很容易理解这个冲突。

middleware loan 的 sample：

~~~text
lifetime controlled by RMW/backend
~~~

IntraProcessManager buffer：

~~~text
lifetime controlled by rclcpp shared/unique ownership
~~~

如果直接把 middleware-owned pointer 塞给本地多个 subscriber：

- 谁最后归还？
- shared subscriber 延长寿命怎么办？
- unique subscriber fan-out 如何复制？
- remote subscriber 同时存在怎么办？

没有统一 ownership protocol 时，最安全的选择就是拒绝这种组合。

## Cyclone RMW 的 loan 不是无条件能力

固定 Humble 源码把 loan path 放在：

~~~cpp
#ifdef DDS_HAS_SHM
...
#else
  RMW_SET_ERROR_MSG(
    "rmw_publish_loaned_message not implemented for rmw_cyclonedds_cpp");
  return RMW_RET_UNSUPPORTED;
#endif
~~~

同时还检查：

~~~cpp
if (!publisher->can_loan_messages) {
  return RMW_RET_UNSUPPORTED;
}
~~~

真正发布时：

~~~cpp
if (cdds_publisher->is_loaning_available) {
  auto d = new serdata_rmw(
    cdds_publisher->sertype,
    ddsi_serdata_kind::SDK_DATA);

  d->iox_chunk = ros_message;

  shm_set_data_state(
    d->iox_chunk,
    IOX_CHUNK_CONTAINS_RAW_DATA);

  if (dds_writecdr(cdds_publisher->enth, d) >= 0) {
    return RMW_RET_OK;
  }
}
~~~

这里已经能看到几个条件：

- build/runtime 有 SHM support；
- publisher advertises loan capability；
- message/type 满足 backend 的 loan 约束；
- sample 来自正确的共享内存 chunk。

所以实际 zero-copy capability 是条件交集：

\[
Z =
C_{RMW}
\cap
C_{type}
\cap
C_{SHM}
\cap
C_{lifetime}
\]

任一条件失败，都可能退化或直接 unsupported。

## Fast DDS RMW 也把 loan 作为 backend contract

固定实现：

~~~cpp
if (!publisher->can_loan_messages) {
  RMW_SET_ERROR_MSG("Loaning is not supported");
  return RMW_RET_UNSUPPORTED;
}

auto info =
  static_cast<CustomPublisherInfo *>(publisher->data);

if (!info->data_writer_->write(
    const_cast<void *>(ros_message)))
{
  return RMW_RET_ERROR;
}
~~~

上层看到的仍然是统一的 `rmw_publish_loaned_message`。

但 DataWriter 后面究竟走普通 History、Data Sharing 还是 SHM transport，要继续进入 Fast DDS 自己的数据面。

因此 RMW 统一的是接口契约，不保证 backend 的内存实现等价。

## Subscriber 侧的 loan 生命周期更严格

Executor 接收 loaned sample 的结构是：

~~~text
rcl_take_loaned_message
       |
       v
user callback
       |
       v
rcl_return_loaned_message_from_subscription
~~~

也就是说 callback 返回之后，Executor 就会把 sample 归还 middleware。

一个危险示例：

~~~cpp
const Image * g_ptr = nullptr;

void callback(const Image * msg)
{
  g_ptr = msg;   // callback return 后继续使用
}
~~~

如果 `msg` 指向 loaned storage，callback 返回后这块内存可能已经被回收或复用。

zero-copy 不是“更自由的 pointer”，恰恰相反，它通常意味着更严格的借用边界。

## Pool exhaustion 是一种真实 backpressure

共享内存/loaned sample 常来自有限 pool：

~~~text
borrow A
borrow B
borrow C
borrow D
(no one returns)
      |
      v
pool exhausted
~~~

下一次 borrow 可能失败。

所以系统必须推理：

- 最大同时 outstanding loan 数量；
- callback 最坏持有时间；
- 异常路径能否保证 return；
- history/fan-out 是否额外占用 chunk；
- pool 容量是否有明确上界。

这和普通 heap allocation 的思维不同：pool 是显式的有限通信资源。

## zero-copy 不应该是一个布尔标签

更正确的分析方式是逐段问：

~~~text
application -> publisher
    copy?

publisher -> writer/history
    copy?

writer/history -> transport
    copy?

process boundary
    same physical pages?

subscriber middleware -> callback
    copy?
~~~

一条 ROS2 path 可能只在其中两三段 zero-copy。

因此：

~~~text
zero-copy = true
~~~

的信息量远不如：

~~~text
Cyclone RMW
fixed-size message
SHM enabled
publisher loaned sample
subscriber loaned take
no application-level fan-out copy
~~~

## 为什么大图像/点云最值得使用 loan + SHM

假设图像 8 MB、30 Hz：

\[
8\text{ MB}\times30
=
240\text{ MB/s}
\]

如果完整复制两次：

\[
480\text{ MB/s}
\]

还没有计算 cache pollution 与其他 sensor streams。

所以对：

- RGB-D；
- point cloud；
- tensor；
- camera frame；

减少 full-payload copy 很有价值。

但对于几十字节的控制命令，主要成本往往来自：

- scheduling；
- synchronization；
- wakeup；
- stale data；
- reliability/history semantics。

不要为了几十字节消息引入复杂 loan lifetime，却忽略真正的调度瓶颈。

## 与 IntraProcessManager 的边界

两种机制可以清楚区分：

| 机制 | 目标 | 主要 owner |
|---|---|---|
| IntraProcessManager | 同进程绕过 middleware 数据面 | rclcpp |
| LoanedMessage | 使用 middleware 提供的 sample storage | RMW / DDS backend |
| DDS SHM/Data Sharing | 跨进程减少 payload 搬运 | DDS backend |

它们可能出现在同一个系统，但不是同义词。

## 一个框架作者真正要学的东西

Loaned Message 展示的是一种通用的 capability-based API：

~~~text
fast path available?
   /          \
 yes           no
 |              |
borrow       fallback
 |              |
strict         ordinary
lifetime       allocation
~~~

这种 API 既让高性能 backend 暴露能力，又避免应用代码完全依赖一个实现。

代价是：性能不再能从 API 表面推断，必须运行时核验 capability 和实际路径。

下一篇把 ROS1 与 ROS2 的 discovery、transport、execution、queue、intra-process 和 zero-copy 放进同一张架构迁移图。
