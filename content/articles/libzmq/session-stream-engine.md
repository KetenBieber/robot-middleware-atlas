# Session 与 Stream Engine：消息语义怎样落到 TCP 字节流

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

到了这一层，需要明确两个世界：

~~~text
libzmq 内部：msg_t / multipart / pipe
网络内核：byte stream / fd readiness
~~~

`session_base_t` 和 `stream_engine_base_t` 就是这两个世界之间的桥。

## Session 持有什么

构造状态中可以看到：`_pipe`、`_engine`、`_socket`、`_io_thread`、address、linger timer。它既知道内部消息通道，又知道传输侧 engine。

`attach_pipe()` 会保存 pipe，并把自己设成 pipe event sink：

~~~cpp
_pipe = pipe_;
_pipe->set_event_sink (this);
~~~

这意味着 pipe 的 read/write activation 最终会回到 Session。

## pull_msg：Engine 从内部 Runtime 拿一条消息

~~~cpp
if (!_pipe || !_pipe->read (msg_)) {
    errno = EAGAIN;
    return -1;
}
~~~

对 Engine 来说，Session 提供的是消息级接口，而不是让 Engine 自己知道 ypipe/HWM 的所有细节。

## push_msg：网络收完一条消息后塞回内部 Pipe

~~~cpp
if (_pipe && _pipe->write (msg_)) {
    const int rc = msg_->init ();
    errno_assert (rc == 0);
    return 0;
}
~~~

这条路径把 network-decoded `msg_t` 重新交回消息 Runtime。

## Engine 才真正拥有 fd

`stream_engine_base_t` 构造后会把 socket 设为 non-blocking。`plug()` 时：

~~~cpp
io_object_t::plug (io_thread_);
_handle = add_fd (_s);
~~~

也就是说真实网络 fd 注册在 I/O thread 的 poller 上。

## 为什么 Session 和 Engine 要分开

如果直接把 TCP read/write、ZMTP framing、pipe/HWM、reconnect、socket pattern 全塞进一个类，会形成巨型状态机。

分层以后：

~~~text
Session
  管内部 message flow / pipe lifecycle

Engine
  管 transport fd / encoder / decoder / handshake
~~~

这让同一 Session 语义可以挂不同 transport engine，也让 transport framing 不必侵入上层 socket pattern。

## TCP 是字节流，ZeroMQ 是消息

TCP 不知道：

~~~text
message A 在哪里结束
multipart 是否还有下一帧
routing id 是什么
handshake frame 是什么
~~~

所以 Engine 需要 encoder/decoder，把 msg_t 变成 wire bytes，再从 byte stream 恢复 message boundary。

这也是为什么“用了 TCP”完全不等于“已经有消息中间件”。TCP 只给可靠有序字节流，消息语义、framing、routing、backpressure 和 lifecycle 仍然需要 Runtime 自己实现。

## readiness 与 message flow 怎样闭环

~~~text
socket fd readable
   |
poller
   |
stream engine decoder
   |
session.push_msg
   |
pipe
   |
socket pattern

socket pattern wants send
   |
pipe
   |
session.pull_msg
   |
engine encoder
   |
socket fd writable
~~~

这里可以看到：poller 只负责“什么时候能继续 I/O”；Session/Pipe 决定“有没有消息和容量”；Engine 决定“怎样变成网络字节”。三个职责不能混为一层。

## 一条连接中的对象实例与线程归属

一条已经进入正常消息阶段的 TCP 连接，可以压缩成下面这张对象图：

~~~text
Application / socket owner thread
    |
    +-- socket_base_t
    |      |
    |      +-- socket-side pipe_t endpoint
    |
    |      cross-thread message channel
    |========================================
                                           |
I/O thread                                 |
    |                                      |
    +-- session_base_t                     |
    |      |                               |
    |      +-- session-side pipe_t endpoint+
    |      |
    |      +-- stream_engine_base_t
    |              |
    |              +-- decoder / encoder
    |              +-- network fd
    |
    +-- poller_t
           |
           +-- network fd readiness
~~~

这里不是“多个对象共同使用同一个 pipe_t”。`pipepair()` 会创建两个 `pipe_t` endpoint，它们分别属于两边 execution owner，底层再由两条单向 ypipe 互联。

Session 侧 endpoint 由 Session 保存；socket 侧 endpoint 通过 command 交给 socket owner。这一点决定了很多成员为什么可以保持普通字段而不是原子变量。

## engine_ready()：消息通道为什么在协议就绪后才建立

固定源码中的 `engine_ready()`：

~~~cpp
void session_base_t::engine_ready ()
{
    if (!_pipe && !is_terminating ()) {
        object_t *parents[2] = {this, _socket};
        pipe_t *pipes[2] = {NULL, NULL};

        const bool conflate =
          get_effective_conflate_option (options);

        int hwms[2] = {
          conflate ? -1 : options.rcvhwm,
          conflate ? -1 : options.sndhwm
        };

        bool conflates[2] =
          {conflate, conflate};

        const int rc =
          pipepair (parents, pipes, hwms, conflates);
        errno_assert (rc == 0);

        pipes[0]->set_event_sink (this);
        _pipe = pipes[0];

        pipes[0]->set_endpoint_pair (
          _engine->get_endpoint ());
        pipes[1]->set_endpoint_pair (
          _engine->get_endpoint ());

        send_bind (_socket, pipes[1]);
    }
}
~~~

这里的对象归属很清楚：

~~~text
pipes[0]
  parent = Session
  event sink = Session
  owner = Session / I/O thread

pipes[1]
  parent = socket
  owner = socket/application thread
~~~

因此 TCP connect 完成不等于 application message channel 已经建立。若 Engine 还有 greeting、mechanism 或 authentication handshake，Session 可以等到 engine 真正 ready 后才创建正常 message pipe。

## send_bind() 为什么不能替换成直接 attach_pipe()

Session 所在线程不能直接调用 socket owner 的：

~~~text
socket.attach_pipe(pipes[1])
~~~

否则 socket 的 pipe registry、pattern-specific scheduler 和其他 owner-local 状态会被 foreign thread 修改。

实际路径是：

~~~text
Session / I/O thread
     |
     | send_bind(socket, pipes[1])
     v
socket mailbox
     |
     v
socket owner thread
     |
     v
process_bind()
     |
     v
attach_pipe()
~~~

这与前面的 owner-command 机制完全一致：跨线程只传 command 和 endpoint 引用，真正状态迁移仍由目标 owner 执行。

## outbound：应用消息怎样走向网络

完整方向：

~~~text
Application send
    |
    v
socket pattern
    |
    v
socket-side pipe.write
    |
    v
session-side pipe becomes readable
    |
    v
Session.read_activated
    |
    v
Engine.restart_output
    |
    v
Session.pull_msg
    |
    v
mechanism / encoder
    |
    v
network write
~~~

其中 Session 只提供消息级接口：

~~~cpp
int session_base_t::pull_msg (msg_t *msg_)
{
    if (!_pipe || !_pipe->read (msg_)) {
        errno = EAGAIN;
        return -1;
    }

    _incomplete_in =
      (msg_->flags () & msg_t::more) != 0;

    return 0;
}
~~~

这里没有 TCP read/write，也没有 epoll。它只回答：当前内部 pipe 是否有下一帧 `msg_t`。

## _incomplete_in 为什么必须存在

ZeroMQ 支持 multipart message：

~~~text
frame A [more]
frame B [more]
frame C [last]
~~~

如果 Engine 已经从内部 pipe 拉出了 frame A、frame B，而连接在 frame C 之前断开，Session 需要知道当前逻辑消息仍未结束。

`pull_msg()` 每次保存：

~~~cpp
_incomplete_in =
  (msg_->flags () & msg_t::more) != 0;
~~~

错误恢复时，`clean_pipes()` 会把旧连接遗留的 multipart 尾部消费掉，避免它与重连后的新消息拼接。

## clean_pipes() 为什么两个方向做不同处理

固定源码：

~~~cpp
void session_base_t::clean_pipes ()
{
    zmq_assert (_pipe != NULL);

    _pipe->rollback ();
    _pipe->flush ();

    while (_incomplete_in) {
        msg_t msg;

        int rc = msg.init ();
        errno_assert (rc == 0);

        rc = pull_msg (&msg);
        errno_assert (rc == 0);

        rc = msg.close ();
        errno_assert (rc == 0);
    }
}
~~~

`rollback()` 处理尚未完成、尚未作为完整 message 发布的 outbound multipart 尾部。

`while (_incomplete_in)` 则清掉 Engine 已经开始读取但还未读完的 inbound multipart。

因此 connection error 不能只关闭 fd；message framing state 也必须回到一致边界。

## read_activated 为什么会启动网络输出

Session 是自己 pipe endpoint 的 `i_pipe_events` sink。

固定源码：

~~~cpp
void session_base_t::read_activated (pipe_t *pipe_)
{
    if (unlikely (
          pipe_ != _pipe
          && pipe_ != _zap_pipe)) {
        zmq_assert (
          _terminating_pipes.count (pipe_) == 1);
        return;
    }

    if (unlikely (_engine == NULL)) {
        if (_pipe)
            _pipe->check_read ();
        return;
    }

    if (likely (pipe_ == _pipe))
        _engine->restart_output ();
    else
        _engine->zap_msg_available ();
}
~~~

对于正常 message pipe：

~~~text
pipe readable
   |
   v
Session can pull outbound msg
   |
   v
Engine.restart_output()
~~~

这里“pipe 的 read side”对应“网络的 output side”，因为 Session 正站在两个方向相反的通道中间。

## restart_output() 为什么先重新订阅 POLLOUT，再立即尝试发送

Engine：

~~~cpp
void stream_engine_base_t::restart_output ()
{
    if (unlikely (_io_error))
        return;

    if (likely (_output_stopped)) {
        set_pollout ();
        _output_stopped = false;
    }

    out_event ();
}
~~~

这里同时做两件事：

~~~text
long-term readiness:
  re-enable POLLOUT

fast path:
  speculative out_event now
~~~

如果 TCP send buffer 此刻正好有空间，直接发送可以省掉一次 epoll round trip；如果不能全部写完，后续再由 POLLOUT readiness 驱动。

## out_event() 为什么有自己的 byte batch

`out_event()` 不会每拉一条 message 就立即做一次 syscall。

它会先尽量填充 byte batch：

~~~cpp
_outpos = NULL;
_outsize =
  _encoder->encode (&_outpos, 0);

while (_outsize
       < static_cast<size_t> (
           _options.out_batch_size)) {

    if ((this->*_next_msg) (&_tx_msg)
        == -1)
        break;

    _encoder->load_msg (&_tx_msg);

    unsigned char *bufptr =
      _outpos + _outsize;

    const size_t n =
      _encoder->encode (
        &bufptr,
        _options.out_batch_size - _outsize);

    _outsize += n;
}
~~~

因此存在两个不同批次概念：

~~~text
message batch
  多个 msg_t

byte batch
  encoder 合并后的连续字节
~~~

`out_batch_size` 限制的是 byte batch，不是“最多发送多少条消息”。

## partial write 为什么必须保存 _outpos / _outsize

非阻塞 socket 可能出现：

~~~text
want write: 64 KB
kernel accepts: 12 KB
remaining: 52 KB
~~~

Engine 不能重做整个编码，也不能丢掉剩余字节。

所以：

~~~cpp
_outpos += nbytes;
_outsize -= nbytes;
~~~

下一次 POLLOUT 继续从当前 byte offset 写。

这说明 network progress 与 message progress 是两个状态机：

~~~text
message already encoded
but bytes not fully written
~~~

在高性能网络 Runtime 中非常常见。

## 没有 outbound message 时为什么 reset_pollout()

如果 Session 没有下一条消息：

~~~text
_outsize == 0
~~~

Engine：

~~~cpp
_output_stopped = true;
reset_pollout ();
return;
~~~

TCP socket 在大量时间里都可能处于 writable。如果一直订阅 POLLOUT：

~~~text
epoll returns writable
-> no message
-> return
-> epoll returns writable again
-> ...
~~~

就会形成 busy loop。

所以“是否订阅 writable event”必须由应用数据可用性驱动，而不是永久打开。


## inbound：内部 HWM 怎样反向停止网络读取

反方向的数据链是：

~~~text
network fd readable
  -> Engine.in_event
  -> read bytes
  -> decoder
  -> complete msg_t
  -> mechanism.decode
  -> Session.push_msg
  -> session-side pipe.write
  -> socket-side endpoint
  -> application recv
~~~

这里最关键的边界不是 socket read，而是 `Session.push_msg()` 可能返回 `EAGAIN`。

## TCP read 返回值为什么不能直接当成消息

`in_event_internal()` 先向 decoder 取得 buffer，再把当前可读字节放进去：

~~~cpp
size_t bufsize = 0;
_decoder->get_buffer (&_inpos, &bufsize);

const int rc = read (_inpos, bufsize);
~~~

一次 TCP read 可能得到半个 frame、一个完整 frame，也可能一次拿到多个 frame。TCP 只保证可靠有序 byte stream，并不提供 ZeroMQ message boundary。

因此后续必须循环 decoder：

~~~cpp
while (_insize > 0) {
    rc = _decoder->decode (
      _inpos, _insize, processed);

    _inpos += processed;
    _insize -= processed;

    if (rc == 0 || rc == -1)
        break;

    rc = (this->*_process_msg) (
      _decoder->msg ());

    if (rc == -1)
        break;
}
~~~

所以至少存在三个不同层次的状态：

~~~text
fd readable
  -> kernel bytes available

decoder complete
  -> one protocol message available

pipe writable
  -> downstream has capacity
~~~

`EPOLLIN` 只表示 read 可能取得进展，不表示已经得到一条完整业务消息。

## decode_and_push() 是网络与内部 Pipe 的交界点

正常消息阶段：

~~~cpp
int stream_engine_base_t::decode_and_push (
  msg_t *msg_)
{
    if (_mechanism->decode (msg_) == -1)
        return -1;

    if (_session->push_msg (msg_) == -1) {
        if (errno == EAGAIN)
            _process_msg =
              &stream_engine_base_t::
                push_one_then_decode_and_push;
        return -1;
    }

    return 0;
}
~~~

路径是：

~~~text
wire framing complete
  -> mechanism decode
  -> normal msg_t
  -> Session.push_msg
  -> internal pipe capacity check
~~~

mechanism 处理协议和安全转换；Session 负责内部 runtime handoff。

## push_msg() 成功时为什么重新 init 原 msg_t

固定源码：

~~~cpp
if (_pipe && _pipe->write (msg_)) {
    const int rc = msg_->init ();
    errno_assert (rc == 0);
    return 0;
}

errno = EAGAIN;
return -1;
~~~

`pipe->write(msg_)` 成功以后，payload ownership 已经进入内部 pipe，因此原 Engine-side `msg_t` 被重新初始化为空。

~~~text
before:
  Engine msg_t owns payload

successful write:
  Pipe/runtime owns payload

msg_->init():
  source handle becomes empty
~~~

如果 write 失败，原 `msg_t` 仍保持有效，Engine 才能稍后重试。

## HWM 满为什么是 EAGAIN 而不是 connection error

内部消费者太慢意味着：

~~~text
message valid
connection may be healthy
downstream temporarily has no capacity
~~~

因此返回 `EAGAIN`，而不是 `EPROTO` 或 `ECONNRESET`。

Runtime 必须区分：

~~~text
capacity unavailable
protocol invalid
transport failed
timeout
~~~

因为四种状态对应的动作分别可能是 pause、terminate、reconnect 或 retry。

## EAGAIN 怎样真正停止 POLLIN

`in_event_internal()` 在 downstream 返回 EAGAIN 后：

~~~cpp
if (rc == -1) {
    if (errno != EAGAIN) {
        error (protocol_error);
        return false;
    }

    _input_stopped = true;
    reset_pollin (_handle);
}
~~~

因果链是：

~~~text
application too slow
  -> Pipe reaches HWM
  -> Session.push_msg returns EAGAIN
  -> Engine cannot hand off current msg
  -> _input_stopped = true
  -> reset_pollin(network fd)
  -> stop actively reading socket
~~~

这一步让 backpressure 真正越过内部 queue，进入 OS I/O 层。

## 为什么不能继续 read 到另一个用户态 buffer

如果 Pipe 满后仍然不断 recv：

~~~text
Pipe full
  -> keep socket read
  -> append bytes elsewhere
  -> another buffer grows
~~~

只是把积压从 Pipe 转移到 Engine 内存，数据年龄和内存占用仍会继续增长。

TCP 下暂停 read 会逐步产生：

~~~text
Engine stops read
  -> kernel recv buffer fills
  -> advertised receive window shrinks
  -> remote sender eventually slows
~~~

这才是端到端反压。

UDP 没有同样的可靠流控；本地不读更可能导致 kernel datagram drop，因此 UDP overload policy 必须单独设计。

## 被 HWM 挡住的完整消息不能丢

假设 decoder 已经产生 M42：

~~~text
M42 complete
Session.push_msg(M42) -> EAGAIN
~~~

此时不能继续 decode M43，否则可能破坏顺序或复用当前 decoder message storage。

源码将处理函数切换为：

~~~cpp
_process_msg =
  &stream_engine_base_t::
    push_one_then_decode_and_push;
~~~

也就是把状态改为：

~~~text
NORMAL_DECODE
  -> downstream full
RETRY_CURRENT_MESSAGE
  -> success
NORMAL_DECODE
~~~

被挡住的 M42 成为必须先完成的 continuation。


## 空间恢复以后怎样重新开启 network input

Application 从 socket-side endpoint 消费消息以后，Pipe 的 HWM/LWM 协议会在达到 progress reporting boundary 时向 Session side 发送：

~~~text
activate_write(msgs_read)
~~~

Session 收到：

~~~cpp
void session_base_t::write_activated (
  pipe_t *pipe_)
{
    if (_pipe != pipe_) {
        zmq_assert (
          _terminating_pipes.count (pipe_) == 1);
        return;
    }

    if (_engine)
        _engine->restart_input ();
}
~~~

所以内部容量恢复并不是 Engine 自己轮询发现的，而是由 Pipe control plane 主动通知。

## restart_input() 为什么先重试旧消息

固定源码开头：

~~~cpp
bool stream_engine_base_t::restart_input ()
{
    zmq_assert (_input_stopped);
    zmq_assert (_session != NULL);
    zmq_assert (_decoder != NULL);

    int rc =
      (this->*_process_msg) (
        _decoder->msg ());

    if (rc == -1) {
        if (errno == EAGAIN)
            _session->flush ();
        else {
            error (protocol_error);
            return false;
        }
        return true;
    }

    ...
}
~~~

第一件事不是重新打开 POLLIN，而是重新处理当前 decoder 已经保存的那条 pending message。

这样维持：

~~~text
M42 blocked
  -> retry M42
  -> M42 succeeds
  -> only then continue M43
~~~

顺序不会被容量暂停打乱。

## push_one_then_decode_and_push() 只完成一个 continuation

固定源码：

~~~cpp
int stream_engine_base_t::
push_one_then_decode_and_push (
  msg_t *msg_)
{
    const int rc =
      _session->push_msg (msg_);

    if (rc == 0)
        _process_msg =
          &stream_engine_base_t::
            decode_and_push;

    return rc;
}
~~~

它不做新 read，也不 decode 下一条消息。它只尝试完成之前未完成的 handoff。

成功后才把 `_process_msg` 恢复为 normal decoder path。

这是一个很清晰的状态机编码方式：

~~~text
function pointer
  == current continuation state
~~~

而不是堆多个容易组合爆炸的 bool。

## restart_input() 为什么最后重新 set_pollin

当前 pending msg 与 decoder 已经缓存的 bytes 都能继续推进以后：

~~~cpp
_input_stopped = false;
set_pollin ();
_session->flush ();
~~~

这里三个动作分别表示：

~~~text
_input_stopped = false
  local state says input path active

set_pollin()
  Reactor once again watches network readable

Session.flush()
  publish any internal messages produced during recovery
~~~

状态恢复与 poller subscription 必须同步变化。

## 恢复后为什么立刻 speculative read

源码随后再次调用：

~~~cpp
if (!in_event_internal ())
    return false;
~~~

当 Pipe 容量刚恢复时，kernel receive buffer 很可能已经积累了新 bytes。

如果只执行：

~~~text
set_pollin
return
wait next epoll
~~~

会多一次 Reactor round trip。

因此 input 和 output 两边都采用：

~~~text
enable readiness
+
try immediate progress
+
fall back to poller if needed
~~~

这是统一的低延迟优化模式。

## Session.flush() 为什么通常在 decode batch 之后

一次 network read 可能得到：

~~~text
M1 bytes
M2 bytes
M3 bytes
M4 bytes
~~~

decoder 可能在一轮 `in_event_internal()` 中连续恢复多个 message。

Session 对 pipe 的多次 write 可以先发生，最后：

~~~cpp
_session->flush ();
~~~

统一推进 ypipe publication boundary。

于是可能形成：

~~~text
4 x pipe.write
1 x pipe.flush
~~~

这减少原子 publication 和跨线程 activation 的频率。

这也是 ypipe 中 `write() != publish` 在上层 Runtime 的真实用途。

## Inbound Backpressure 的完整闭环

~~~text
Remote peer
    |
    | TCP bytes
    v
kernel recv buffer
    |
    | EPOLLIN
    v
StreamEngine.in_event
    |
    v
decoder -> complete msg_t
    |
    v
Session.push_msg
    |
    +-- Pipe write success
    |       |
    |       v
    |   Session.flush
    |       |
    |       v
    |   socket-side endpoint
    |
    +-- Pipe HWM full
            |
            v
          EAGAIN
            |
            v
      _input_stopped = true
            |
            v
      reset_pollin(fd)
            |
            v
      network read paused
            |
            | application consumes
            v
      Pipe LWM progress
            |
            v
      activate_write
            |
            v
      Session.write_activated
            |
            v
      Engine.restart_input
            |
            v
      retry pending msg
            |
            v
      set_pollin(fd)
~~~

这一条链把 application consumption rate 反馈到了 TCP read rate。

## Outbound Wakeup 的完整闭环

~~~text
Application send
    |
    v
socket-side Pipe write
    |
    v
Pipe flush
    |
    | Session reader was passive
    v
activate_read command
    |
    v
Session.read_activated
    |
    v
Engine.restart_output
    |
    +-- set POLLOUT if stopped
    |
    +-- speculative out_event
            |
            v
      Session.pull_msg
            |
            v
      mechanism.encode
            |
            v
         encoder
            |
            v
       network write
~~~

因此 Pipe 的两个 activation event 分别对应：

~~~text
read_activated
  -> outbound data appeared
  -> restart network output

write_activated
  -> inbound capacity recovered
  -> restart network input
~~~

## 为什么两个方向名字看起来相反

从 Session 的 Pipe endpoint 观察：

~~~text
read_activated:
  Session can read message from Pipe

write_activated:
  Session can write message into Pipe
~~~

而 Session 处在 network 与 application 之间，所以映射自然变成：

~~~text
Pipe read
  -> Network output

Pipe write
  -> Network input
~~~

方向没有反，只是观察坐标系不同。

## 四种 progress condition 必须分开

一条连接中同时存在：

~~~text
fd readable
  OS says bytes may be read

decoder complete
  protocol parser has a full msg_t

Pipe writable
  downstream has capacity

socket has_in
  application-visible message exists
~~~

输出侧同样有：

~~~text
Pipe readable
encoded bytes pending
fd writable
application has sendable data
~~~

这些状态不能合并成一个 `ready`。每一层只对自己能证明的 progress condition 负责。


## Handshake ready 与 TCP connected 不是同一个状态

Engine input path 在进入正常 decoder 之前先检查：

~~~cpp
if (unlikely (_handshaking)) {
    if (handshake ()) {
        _handshaking = false;

        if (_mechanism == NULL
            && _has_handshake_stage) {
            _session->engine_ready ();

            if (_has_handshake_timer) {
                cancel_timer (
                  handshake_timer_id);
                _has_handshake_timer = false;
            }
        }
    }
    else
        return false;
}
~~~

一条 TCP connection 建立以后，还可能需要：

~~~text
greeting
ZMTP version negotiation
security mechanism
authentication
metadata exchange
~~~

因此：

~~~text
fd connected
!=
application message path ready
~~~

这也是 `engine_ready()` 为什么不简单绑定到 socket creation 的原因。

## decoder 与 mechanism 不是同一层

两者都处理“网络数据”，但职责不同：

~~~text
decoder / encoder
  byte framing
  partial input/output
  wire message boundary

mechanism
  protocol/security transformation
  authentication metadata
  command semantics

Session
  internal msg_t handoff
  Pipe lifecycle/backpressure
~~~

如果把 framing、安全机制、内部 queue 与 routing 都塞进同一个 parser，错误恢复路径会迅速变成巨型状态机。

## mechanism_ready() 如何切换正常数据路径

当安全/协议 mechanism 完成：

~~~cpp
_next_msg =
  &stream_engine_base_t::pull_and_encode;

_process_msg =
  &stream_engine_base_t::write_credential;
~~~

后续再进入正常 `decode_and_push`。

这里的成员函数指针实际上在编码 Engine 当前阶段：

~~~text
handshake
authentication
credential delivery
normal data
blocked downstream retry
~~~

状态改变时切换处理函数，比每个 byte 都重新检查大量条件更紧凑。

## Engine error 为什么必须回到 Session

网络错误不是 Engine 单独删除自己就结束。

Session 还需要处理：

~~~text
unfinished multipart
Pipe cleanup
disconnect/hiccup notification
reconnect policy
linger/termination
~~~

`engine_error()` 先把：

~~~text
_engine = NULL
~~~

然后清理 Pipe 中半条消息，再按错误类别决定 reconnect 或 terminate。

这说明：

~~~text
transport lifetime
!=
logical endpoint lifetime
~~~

连接可以断开并重建，而 socket/application endpoint 仍继续存在。

## connection_error、timeout_error、protocol_error 为什么分开

Session 收到的错误理由至少有：

~~~text
connection_error
timeout_error
protocol_error
~~~

它们表达的恢复语义不同。

连接错误或超时在主动连接场景可能进入：

~~~text
reconnect
~~~

协议错误更可能意味着：

~~~text
peer data invalid
handshake invalid
do not continue same protocol flow
~~~

Runtime 如果只保存一个 `failed=true`，就无法做正确恢复策略。

## reconnect 前为什么必须 clean_pipes()

假设旧连接中存在：

~~~text
outbound multipart:
  A [more]
  B [more]

inbound partially consumed:
  X [more]
  Y [more]
~~~

直接复用这些状态到新连接会让：

~~~text
old B + new C
or
old Y + new Z
~~~

被错误拼成逻辑消息。

所以连接重建前必须恢复到完整 message boundary。

## linger 最终如何落到 Session 与 Pipe

Session termination 中：

~~~cpp
if (linger_ > 0) {
    add_timer (
      linger_,
      linger_timer_id);

    _has_linger_timer = true;
}

_pipe->terminate (
  linger_ != 0);
~~~

如果 linger 超时：

~~~cpp
void session_base_t::timer_event (
  int id_)
{
    zmq_assert (
      id_ == linger_timer_id);

    _has_linger_timer = false;

    zmq_assert (_pipe);
    _pipe->terminate (false);
}
~~~

因此 API 里的 linger 最终变成：

~~~text
deadline timer
+
delayed Pipe termination
+
timeout后的 forced drop
~~~

它不是“sleep 一段时间再 close”。

## pipe_terminated() 为什么才是真正的引用释放点

请求 terminate 以后，Pipe 两端还要完成 delimiter/term/ack 协议。

Session 只有收到：

~~~text
pipe_terminated(pipe)
~~~

以后才清：

~~~text
_pipe = NULL
~~~

并取消对应 linger timer。

所以异步关闭必须区分：

~~~text
shutdown requested
quiescing
termination handshake
reclamation complete
~~~

把这四步压成一个 `closed` bool，通常会留下 use-after-free 或丢在途消息。

## _terminating_pipes 为什么维护旧 Pipe 集合

重连、detach、hiccup 或 shutdown 时，Session 可能同时存在：

~~~text
current _pipe
old detached Pipe waiting for async termination
_zap_pipe
~~~

旧 Pipe 的终止事件可能晚于新 Pipe 创建。

`_terminating_pipes` 使 Session 能判断某个迟到 callback 属于：

~~~text
current channel
or
old channel still being reclaimed
~~~

这是异步对象生命周期中常见的 detached-but-not-yet-dead 状态。

## Session / Engine 分层后的状态责任

| 层 | 主要状态 |
| --- | --- |
| Poller / I/O thread | fd registration、timer、callback scheduling |
| Stream Engine | byte buffer、encoder/decoder、handshake、POLLIN/POLLOUT |
| Session | engine/pipe bridge、reconnect、linger、partial-message cleanup |
| Pipe | HWM/LWM、activation、message ownership、termination |
| Socket pattern | routing、fair queue、load balance、pub-sub policy |

这些层会互相反馈，但每层只直接修改自己拥有的核心状态。

## 机器人网络 Runtime 中最值得迁移的设计

设想可靠网络相机：

~~~text
remote camera
  -> TCP
  -> decoder
  -> bounded frame channel
  -> perception thread
~~~

如果 perception 变慢，系统必须明确：

~~~text
capacity boundary
ownership on failed enqueue
pause/resume trigger
transport semantics
drop/retry policy
shutdown behavior
~~~

对于 TCP，可以通过停止 read 逐步把压力反馈到发送端；对于 UDP，则通常需要明确 drop/latest-state policy。

真正可迁移的不是 ZeroMQ API，而是：

~~~text
bounded internal channel
+
owner-thread I/O
+
readiness subscription as flow-control actuator
+
explicit retry continuation
+
lifecycle state machine
~~~

## Session / Engine 的核心不变量

~~~text
1. socket-side and session-side Pipe endpoints are different objects
2. each endpoint follows its execution owner
3. Session bridges msg_t flow; Engine owns transport readiness
4. TCP byte boundaries do not equal message boundaries
5. successful push transfers payload ownership into Pipe
6. failed push retains current decoded message for retry
7. Pipe HWM can disable network POLLIN
8. LWM/activate_write can re-enable network input
9. Pipe read activation restarts network output
10. blocked old message completes before newer input advances
11. handshake readiness differs from transport connection readiness
12. connection failure cleans partial multipart state
13. linger becomes timer plus delayed/forced Pipe termination
14. shutdown request and physical reclamation are different phases
15. readiness means permission to make progress, not business completion
~~~

这些不变量把 Poller、Engine、Session、Pipe 和 Socket 接成了一条完整消息 Runtime。
