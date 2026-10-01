# TCPROS：TCP 字节流怎样变成 ROS 消息通道

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。

TCPROS 的价值不在“ROS 使用 TCP”这句话，而在于它如何把一个没有消息边界的 TCP byte stream 变成有类型、有 framing、能被 PollManager 异步推进的消息通道。

如果只看到 `TransportTCP`，会误以为 ROS1 的通信层只是 socket wrapper；真正的协议状态分散在 `TransportTCP`、`Connection`、`TransportPublisherLink` 和 `Publication` 之间。把这几层拆开，才看得见复制、等待、锁和错误恢复发生在哪里。

## 1. TCP 最大的问题：它没有消息边界

应用连续发送：

~~~text
message A = 120 bytes
message B = 80 bytes
~~~

接收端调用 `recv()` 可能观察到：

~~~text
50 bytes
70 bytes
150 bytes
30 bytes
~~~

TCP 只保证字节顺序，不承诺一次 `read` 对应一次 `write`。

ROS1 因此在每条 payload 前放一个 32-bit 长度：

~~~text
+--------------------+---------------------------+
| uint32 payload_len | serialized payload bytes  |
+--------------------+---------------------------+
~~~

Subscriber 侧的循环：

~~~cpp
connection_->read(
  4,
  boost::bind(
    &TransportPublisherLink::onMessageLength,
    this,
    boost::placeholders::_1,
    boost::placeholders::_2,
    boost::placeholders::_3,
    boost::placeholders::_4));

uint32_t len = *((uint32_t*)buffer.get());

connection_->read(
  len,
  boost::bind(
    &TransportPublisherLink::onMessage,
    this,
    boost::placeholders::_1,
    boost::placeholders::_2,
    boost::placeholders::_3,
    boost::placeholders::_4));
~~~

读完 payload 后，又回到“先读 4 字节长度”的状态。

## 2. 为什么 TransportTCP 和 Connection 不能合成一个类

`TransportTCP` 负责 OS 层事实：

~~~text
socket fd
connect
read/write syscall
TCP_NODELAY
poll readiness
disconnect
~~~

`Connection` 负责协议推进需要的异步操作状态：

~~~text
current read target size
bytes already filled
read completion callback

current write size
bytes already sent
write completion callback
~~~

初始化时：

~~~cpp
transport_ = transport;
header_func_ = header_func;
is_server_ = is_server;

transport_->setReadCallback(
    boost::bind(
      &Connection::onReadable,
      this,
      boost::placeholders::_1));

transport_->setWriteCallback(
    boost::bind(
      &Connection::onWriteable,
      this,
      boost::placeholders::_1));

transport_->setDisconnectCallback(
    boost::bind(
      &Connection::onDisconnect,
      this,
      boost::placeholders::_1));
~~~

这让依赖方向非常明确：

~~~text
TransportTCP
  knows OS readiness

Connection
  knows "I need N bytes then call X"

Topic Link
  knows "those N bytes mean ROS header/message"
~~~

如果 `TransportTCP` 自己知道 topic、md5sum、message length 和 callback，就会把通用 socket 状态与 ROS 协议状态耦合在一起。

## 3. Connection::read 是注册一个读取目标

~~~cpp
void Connection::read(
    uint32_t size,
    const ReadFinishedFunc& callback)
{
  if (dropped_ || sending_header_error_)
    return;

  {
    boost::recursive_mutex::scoped_lock lock(read_mutex_);

    ROS_ASSERT(!read_callback_);

    read_callback_ = callback;
    read_buffer_ =
        boost::shared_array<uint8_t>(new uint8_t[size]);

    read_size_ = size;
    read_filled_ = 0;
    has_read_callback_ = 1;
  }

  transport_->enableRead();
  readTransport();
}
~~~

调用者表达的是：接下来需要恰好 `size` 个字节；凑齐后执行 callback。当前 fd 已可读时立即推进；否则留给 PollSet 后续 readiness event。

## 4. partial read 怎样被隐藏在 Connection 内

`readTransport()`：

~~~cpp
uint32_t to_read = read_size_ - read_filled_;

int32_t bytes_read =
    transport_->read(
      read_buffer_.get() + read_filled_,
      to_read);

read_filled_ += bytes_read;

if (read_filled_ == read_size_ && !dropped_)
{
  ReadFinishedFunc callback;
  uint32_t size;
  boost::shared_array<uint8_t> buffer;

  callback = read_callback_;
  size = read_size_;
  buffer = read_buffer_;

  read_callback_.clear();
  read_buffer_.reset();
  read_size_ = 0;
  read_filled_ = 0;
  has_read_callback_ = 0;

  callback(shared_from_this(), buffer, size, true);
}
~~~

`read_filled_` 就是异步读取操作的进度计数。假设要读 1024 bytes：

~~~text
read #1 -> 200    read_filled = 200
read #2 -> 500    read_filled = 700
read #3 -> 324    read_filled = 1024
                  completion callback
~~~

这样上层 Link 不需要关心内核每次实际返回多少字节。

## 5. socket readiness 怎样进入 roscpp

TCP socket 被加入 `PollSet`：

~~~cpp
poll_set_->addSocket(
    sock_,
    boost::bind(
      &TransportTCP::socketUpdate,
      this,
      boost::placeholders::_1),
    shared_from_this());
~~~

执行链：

~~~text
kernel socket readiness
       |
       v
PollSet
       |
       v
TransportTCP::socketUpdate
       |
       +--> Connection::onReadable
       |
       +--> Connection::onWriteable
~~~

`PollManager` 自己有专门线程：

~~~cpp
void PollManager::start()
{
  shutting_down_ = false;
  thread_ = boost::thread(
      &PollManager::threadFunc, this);
}
~~~

所以网络 progress 与用户 `ros::spin()` 是两层不同的执行职责。

## 6. Connection Header 为什么是 TCPROS 的第一层协议

TCP connect 只确认 host/port 可建立连接。还不知道 topic、message type、schema compatibility 和 latency hint。

Subscriber 先写：

~~~cpp
M_string header;

header["topic"] = parent->getName();
header["md5sum"] = parent->md5sum();
header["callerid"] = this_node::getName();
header["type"] = parent->datatype();
header["tcp_nodelay"] =
    transport_hints_.getTCPNoDelay() ? "1" : "0";

connection_->writeHeader(header, ...);
~~~

Publisher 的 `Publication::validateHeader` 检查这些字段。完整状态是：

~~~text
TCP connect
   |
   v
subscriber header
   |
   v
publisher validation
   |
   v
publisher response header
   |
   v
message length/payload loop
~~~

“socket connected”与“ROS channel ready”是两个不同状态。

## 7. 为什么 md5sum 能阻止危险的错误解释

两端可以都声称 type 是 `my_pkg/State`，但字段定义不同。只比较 type 字符串可能让双方对 wire layout 产生不同理解。

ROS1 用 message definition 的 MD5 identity 在 connection setup 时阻止这种连接。它不在每帧携带完整 schema，而是在连接建立前验证一次 compatibility。

## 8. tcp_nodelay 是 latency/efficiency 取舍

TCP 默认 Nagle 算法倾向合并小写入，减少小包数量。对高频小消息，这可能增加等待。

Subscriber 通过 header 的 `tcp_nodelay` 表达偏好，Publisher 最终调用 `TransportTCP::setNoDelay`。

对于 IMU、控制状态等小包，降低聚合等待可能重要；对于大图像，瓶颈更多来自 payload 大小、序列化、内存带宽和网络吞吐。TCP_NODELAY 应结合消息大小和控制周期理解。

## 9. write side 同样是 partial progress 状态机

TCP `write()` 也不保证一次写完整 buffer。`Connection::writeTransport()` 保存：

~~~text
write_buffer_
write_size_
write_sent_
~~~

每次 socket writable：

~~~cpp
uint32_t to_write =
    write_size_ - write_sent_;

int32_t bytes_sent =
    transport_->write(
      write_buffer_.get() + write_sent_,
      to_write);

write_sent_ += bytes_sent;
~~~

只有 `write_sent_ == write_size_` 才触发 completion callback。ROS1 没有假设一次 TCP write 等于一条原子消息发送。

## 10. Publication 为什么站在 Connection 之上做 fan-out

Publisher 并不直接持一堆 raw fd。它持有 SubscriberLink：

~~~text
Publication
   |
   +-- TransportSubscriberLink -> Connection A
   +-- TransportSubscriberLink -> Connection B
   +-- IntraProcessSubscriberLink -> local subscriber
~~~

`Publication::getPublishTypes` 先决定是否需要 serialized bytes、是否存在 nocopy subscriber，再统一把逻辑消息交给 links。

这使 topic fan-out policy 与具体 TCP write state 分离。

## 11. TCP 的可靠性为什么不等于控制实时性

TCP 提供有序可靠 byte stream，但机器人关心的常常是：

~~~text
sample age
deadline
jitter
latest-value semantics
overload behavior
~~~

慢 subscriber 时，数据可能积压在：

~~~text
Publication queue
Connection write buffer
kernel TCP send buffer
network congestion
kernel TCP recv buffer
SubscriptionQueue
CallbackQueue
OS run queue
~~~

所以“TCP 没丢包”完全可能与“控制器正在处理很久以前的状态”同时成立。

## 12. 一个具体的过载反例

假设：

~~~text
sensor = 200 Hz
message = 2 KB
callback = 8 ms
single-threaded spinner
~~~

callback 最大吞吐约 125 Hz。即使网络瞬间送达，SubscriptionQueue 仍会持续过载。queue 大则积累历史，queue 小则更早丢旧。

问题不在 TCP，而在**生产速率、消费 WCET 与队列 policy**不匹配。

## 13. TCPROS 的设计骨架

~~~text
OS transport:
  fd / connect / read / write / readiness

async connection:
  partial read/write
  read-exactly-N
  header/framing

topic link:
  ROS identity
  type compatibility
  lifecycle

publication/subscription:
  fan-out/fan-in
  queue policy

callback runtime:
  execution scheduling
~~~

每一层承担不同变化压力：换 transport 不必重写 callback queue；换执行策略不必重写 TCP framing；增加 intra-process link 也不必改变 Publisher API。
