# Monitor Event：运行时为什么把“可观测性”也做成消息流

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

一个消息 Runtime 真正进入工程以后，仅仅“能 send/recv”远远不够。

你还必须知道：

- TCP 什么时候真的 connected；
- connect 是否 delayed；
- 是否正在 retry；
- listener 是否 bind 成功；
- accept 是否失败；
- ZMTP handshake 是协议失败还是认证失败；
- connection 什么时候 disconnected；
- Pipe 当前积压了多少消息。

很多库会把这些状态做成 callback：

~~~cpp
on_connected(...);
on_error(...);
on_retried(...);
~~~

libzmq 选择了另一条路线：**把运行时事件也编码成 ZeroMQ message，通过一个内部 monitor socket 发给观察者。**

这使 monitor 不再是“随便打几行日志”，而是一个独立的 observability data plane。

## 1. 为什么不直接在内部 printf

最简单的第一版当然是：

~~~cpp
printf("connected %s\n", endpoint);
~~~

但它很快失去工程价值。

日志有几个天然问题：

~~~text
文本格式不稳定
难以结构化消费
无法按 socket 实例隔离
无法自然做多进程/线程转发
没有 backpressure/lifecycle 契约
事件和值类型容易丢失
~~~

如果改成 callback：

~~~cpp
socket.on_event = user_callback;
~~~

又会引入更危险的问题：

> 回调到底在哪个线程执行？

连接成功可能发生在 I/O thread；bind 失败可能发生在 application thread；handshake 事件来自 Engine。若这些内部线程直接执行任意用户 callback，就会把用户代码注入 libzmq 的 owner-thread 状态机。

Monitor socket 避免了这一点。

内部只负责：

~~~text
runtime event
   |
   v
serialize into messages
   |
   v
monitor socket
~~~

用户自己的线程再：

~~~text
recv monitor message
   |
   v
parse / log / metrics / alarm
~~~

执行上下文因此解耦。

## 2. monitor() 为什么只允许 inproc endpoint

socket_base_t::monitor：

~~~cpp
std::string protocol;
std::string address;

if (parse_uri (
      endpoint_,
      protocol,
      address)
    || check_protocol (protocol))
    return -1;

if (protocol !=
      protocol_name::inproc) {
    errno = EPROTONOSUPPORT;
    return -1;
}
~~~

这意味着 monitor channel 本身不是一个外部网络服务。

结构是：

~~~text
monitored socket
      |
      | event messages
      v
internal monitor socket
      |
      | inproc://...
      v
observer socket in same Context
~~~

这样做有两个重要效果。

第一，产生 monitor event 不需要再走 TCP connect、ZMTP handshake、network retry 等复杂链，否则“用于观察网络错误的 monitor 自己也依赖网络”会形成递归问题。

第二，monitor 默认只是一条进程内控制/观测通道。如果要把指标送到远端，应该由业务 observer 收到后再决定怎样导出。

## 3. 为什么 monitor socket 是一个真实 ZeroMQ socket

源码不是维护某个特殊 callback queue，而是：

~~~cpp
_monitor_socket =
  zmq_socket (
    get_ctx (),
    type_);
~~~

支持的 type 限制为：

~~~cpp
case ZMQ_PAIR:
case ZMQ_PUB:
case ZMQ_PUSH:
    break;
default:
    errno = EINVAL;
    return -1;
~~~

这些都是适合作为单向事件输出的 socket 类型。

随后：

~~~cpp
rc = zmq_bind (
  _monitor_socket,
  endpoint_);
~~~

因此 monitor 完全复用了已有：

~~~text
Context
inproc endpoint registry
msg_t
multipart
Pipe
socket send
lifecycle
~~~

这是一种很有代表性的框架设计：

> 内部观测机制优先复用已经稳定的数据面抽象，而不是再造一套平行队列。

## 4. 为什么 monitor socket 的 LINGER 强制设为 0

源码：

~~~cpp
int linger = 0;

int rc =
  zmq_setsockopt (
    _monitor_socket,
    ZMQ_LINGER,
    &linger,
    sizeof (linger));
~~~

注释：

~~~text
Never block context termination
on pending event messages
~~~

这体现了 observability 与业务数据的优先级差异。

假设 Context 正在退出，但 monitor observer 已经不消费：

~~~text
monitor queue
  [CONNECTED]
  [DISCONNECTED]
  [CLOSED]
  ...
~~~

如果 monitor 默认无限 linger，Context 可能为了“把诊断事件完整送达”而永远不能退出。

libzmq 明确选择：

~~~text
shutdown correctness
   >
monitor event delivery completeness
~~~

所以 monitor 是 best-effort observability channel，不应成为 Runtime 的 shutdown dependency。

这个原则在机器人系统里同样非常重要：日志、metrics、trace 不应该阻塞急停、控制线程退出或进程回收。

## 5. _monitor_sync 在保护什么

monitor() 一开始：

~~~cpp
scoped_lock_t lock (
  _monitor_sync);
~~~

event() 也会：

~~~cpp
scoped_lock_t lock (
  _monitor_sync);

if (_monitor_events & type_)
    monitor_event (...);
~~~

被保护的核心状态包括：

~~~text
_monitor_socket
_monitor_events
monitor event version
stop/restart monitor lifecycle
~~~

事件可能从不同内部执行上下文产生，因此 monitor send 不能假设永远只有一个 caller。

注意这并不意味着“整个 socket 是线程安全的”。这里只是 monitor 子系统对自己的 shared state 建立了独立 mutex。

## 6. event wrapper 为什么先做 bitmask 过滤

具体事件函数都很薄。

例如连接成功：

~~~cpp
void socket_base_t::event_connected (
  const endpoint_uri_pair_t &endpoint,
  fd_t fd)
{
    uint64_t values[1] = {
      static_cast<uint64_t> (fd)
    };

    event (
      endpoint,
      values,
      1,
      ZMQ_EVENT_CONNECTED);
}
~~~

重连：

~~~cpp
void socket_base_t::
event_connect_retried (
  const endpoint_uri_pair_t &endpoint,
  int interval)
{
    uint64_t values[1] = {
      static_cast<uint64_t> (
        interval)
    };

    event (
      endpoint,
      values,
      1,
      ZMQ_EVENT_CONNECT_RETRIED);
}
~~~

真正发送前统一：

~~~cpp
if (_monitor_events & type_)
    monitor_event (...);
~~~

这让未订阅事件的热路径只付出：

~~~text
mutex
+
bit test
~~~

而不需要每次都构造完整 multipart event message。

## 7. 为什么 connected / retried / handshake 都在同一协议里

从源码可以看到：

~~~text
ZMQ_EVENT_CONNECTED
ZMQ_EVENT_CONNECT_DELAYED
ZMQ_EVENT_CONNECT_RETRIED
ZMQ_EVENT_LISTENING
ZMQ_EVENT_BIND_FAILED
ZMQ_EVENT_ACCEPTED
ZMQ_EVENT_ACCEPT_FAILED
ZMQ_EVENT_CLOSED
ZMQ_EVENT_CLOSE_FAILED
ZMQ_EVENT_DISCONNECTED
ZMQ_EVENT_HANDSHAKE_FAILED_NO_DETAIL
ZMQ_EVENT_HANDSHAKE_FAILED_PROTOCOL
ZMQ_EVENT_HANDSHAKE_FAILED_AUTH
ZMQ_EVENT_HANDSHAKE_SUCCEEDED
...
~~~

它们来自不同内部对象：

~~~text
Listener
Connecter
Engine
Session
Socket
Pipe stats
~~~

但最终统一投影成：

~~~text
event id
values
endpoint information
~~~

这就是 observability schema。

对用户来说，不需要知道 event 是哪个内部类产生的，只需要知道：

~~~text
what happened
to which endpoint
with what numeric values
~~~

## 8. monitor event v1 为什么只有两帧

v1：

~~~cpp
const uint16_t event =
  static_cast<uint16_t> (event_);

const uint32_t value =
  static_cast<uint32_t> (
    values_[0]);
~~~

第一帧：

~~~text
uint16 event
+
uint32 value
~~~

第二帧：

~~~text
endpoint URI string
~~~

发送：

~~~cpp
zmq_msg_send (
  &msg,
  _monitor_socket,
  ZMQ_SNDMORE);

zmq_msg_send (
  &msg,
  _monitor_socket,
  0);
~~~

这是早期简单 ABI：

~~~text
frame 0:
  fixed event/value pair

frame 1:
  address
~~~

因此 monitor() 会拒绝 v1 无法表示的高位 event mask：

~~~cpp
if (event_version_ == 1
    && events_ >> 16 != 0) {
    errno = EINVAL;
    return -1;
}
~~~

## 9. v2 为什么改成 multipart schema

v2 第一帧：

~~~text
event_ : uint64
~~~

第二帧：

~~~text
values_count_ : uint64
~~~

然后每一个 value 独立一帧：

~~~cpp
for (uint64_t i = 0;
     i < values_count_;
     ++i) {
    ...
    zmq_msg_send (
      &msg,
      _monitor_socket,
      ZMQ_SNDMORE);
}
~~~

最后：

~~~text
local endpoint URI
remote endpoint URI
~~~

可以压成：

~~~text
[event]
[count]
[value0]
[value1]
...
[local URI]
[remote URI]
~~~

为什么不是把整个结构体 memcpy 一次？

因为 multipart schema 更容易扩展：

~~~text
values_count can grow
URI length is variable
local/remote address can be independent
~~~

同时继续复用 ZeroMQ 自己的 frame boundary，而不需要再定义内部 TLV parser。

## 10. 这和 msg_t 的设计怎样对上

前面 [msg_t 存储与引用计数](msg-storage-refcount.md) 已经看到 libzmq 把“消息 envelope”作为整个 Runtime 的统一传输单位。

Monitor 继续复用同样机制：

~~~text
runtime state transition
   |
   v
construct zmq_msg_t
   |
   v
multipart send
   |
   v
ordinary observer recv
~~~

因此 observability 不是在 Runtime 外面贴一层日志，而是把内部状态转换成一种普通消息产品。

## 11. stop_monitor() 为什么也要发 MONITOR_STOPPED

关闭：

~~~cpp
if ((_monitor_events
     & ZMQ_EVENT_MONITOR_STOPPED)
    && send_monitor_stopped_event_) {

    uint64_t values[1] = {0};

    monitor_event (
      ZMQ_EVENT_MONITOR_STOPPED,
      values,
      1,
      endpoint_uri_pair_t ());
}

zmq_close (_monitor_socket);

_monitor_socket = NULL;
_monitor_events = 0;
~~~

这提供了一个明确的 stream terminator：

~~~text
event stream ...
event stream ...
MONITOR_STOPPED
~~~

observer 可以区分：

~~~text
temporarily no events
~~~

与：

~~~text
this monitor source is gone
~~~

但这个终止事件仍然受前面的 LINGER=0 约束，因此不能把它当成绝对可靠的持久化记录。

## 12. 为什么重新 monitor 前先 stop 旧 monitor

源码：

~~~cpp
if (_monitor_socket != NULL)
    stop_monitor (true);
~~~

一个 socket 同时只维护一个内部 monitor socket。

这样 _monitor_events + _monitor_socket + version 构成单一一致配置，不需要支持：

~~~text
observer A wants event subset X
observer B wants subset Y
observer C wants version 2
~~~

这种多订阅 registry。

如果业务确实需要多路消费，可以让 monitor type 选择 PUB，然后在外部 fan-out，而不是把复杂度放进 socket_base_t。

## 13. Pipe stats 为什么不能直接从 application thread 读两边

query_pipes_stats() 注释非常重要：

~~~text
There are 2 pipes per connection,
and the inbound one must be queried
from the I/O thread.
~~~

一条连接两端的 Pipe state 属于不同 execution owner。

所以 application thread 不能直接：

~~~cpp
peer_pipe->queue_size()
~~~

完整过程是：

~~~text
application socket
   |
   | send_stats_to_peer()
   v
outbound Pipe
   |
   | command
   v
peer / I/O thread
   |
   | read peer-side stats
   | combine inbound + outbound
   v
command back to socket
   |
   v
process_pipe_stats_publish()
   |
   v
ZMQ_EVENT_PIPES_STATS
~~~

源码最终：

~~~cpp
uint64_t values[2] = {
  outbound_queue_count_,
  inbound_queue_count_
};

event (
  *endpoint_pair_,
  values,
  2,
  ZMQ_EVENT_PIPES_STATS);
~~~

这是一条很好的通用原则：

> 可观测性不能为了“读指标方便”而破坏原本的数据所有权边界。

统计也要通过 owner-safe command path 汇聚。

## 14. monitor 与 logging 的职责有什么区别

可以把两者区分成：

~~~text
log
  -> 面向人类阅读
  -> 可包含自由文本
  -> 适合解释原因

monitor event
  -> 面向程序消费
  -> 稳定 event schema
  -> 适合 metrics / state machine / test assertion
~~~

例如机器人网络异常：

日志可以写：

~~~text
camera uplink reconnecting after peer restart
~~~

monitor 则提供：

~~~text
CONNECT_DELAYED
CONNECT_RETRIED(interval=...)
CONNECTED(fd=...)
HANDSHAKE_SUCCEEDED
~~~

两者不是互斥关系。

## 15. 为什么不能把 monitor event 当成业务状态

CONNECTED 只说明 transport 建立完成；HANDSHAKE_SUCCEEDED 说明 ZMTP/security protocol 已成功。

它们都不能证明：

~~~text
远端感知算法已经 ready
当前检测结果仍新鲜
控制命令已经执行
远端机器人处于安全状态
~~~

因此健康状态应分层：

~~~text
transport health
  <- libzmq monitor

protocol health
  <- handshake monitor

application health
  <- heartbeat / status message

control safety
  <- age / watchdog / state machine
~~~

不要把低层 event 直接提升成高层安全结论。

## 16. 对机器人运行时最有价值的用法

一个实际监控线程可以维护：

~~~text
endpoint state:
  disconnected
  connecting
  transport_connected
  protocol_ready

last_transition_time
reconnect_interval
pipe_in_depth
pipe_out_depth
handshake_fail_reason
~~~

然后业务层只消费派生状态：

~~~text
vision_link_degraded
telemetry_backlogged
remote_control_unavailable
~~~

这样网络库事件不会直接渗透到所有业务模块。

## 17. 为什么 monitor 本身也应有 bounded work

Monitor socket 虽然解耦了内部线程与 observer，但事件生产仍不是完全“免费”。

event() 仍需要：

~~~text
lock _monitor_sync
construct messages
send monitor frames
~~~

因此高频 event 不应该变成每个业务 message 的 tracing 通道。

它更适合：

~~~text
connection lifecycle
error
retry
queue snapshot
protocol state transition
~~~

而不是每帧点云都发一次 monitor event。

高频 tracing 应该使用更低开销、批量化或者采样式设施。

## 18. 这套设计可以怎样迁移到自研 Runtime

可以抽象一个统一 EventRecord：

~~~text
EventRecord
  event_id
  timestamp
  endpoint_local
  endpoint_remote
  values[]
~~~

内部 owner 线程只做：

~~~text
state transition
  -> emit record
~~~

然后交给：

~~~text
bounded MPSC event channel
  -> metrics thread
  -> log exporter
  -> diagnostic UI
~~~

重要约束包括：

~~~text
observability failure
must not block shutdown

observer work
must not run inside I/O owner

stats collection
must respect object ownership
~~~

libzmq monitor 最值得借鉴的不是具体事件编号，而是这三个边界。

下一篇可以读 [Proxy / Device](proxy-device-runtime.md)，看一套更高层的转发服务怎样完全建立在普通 socket、poller 与 multipart message 之上；如果关心 socket 最终退出过程，则进入 [Context 与 Reaper](context-reaper-lifecycle.md)。
