# ZMTP 握手：TCP 连上以后为什么还不能立刻发送业务消息

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

TCP connect 成功，只能证明两端建立了一条可靠有序字节流。它没有告诉 libzmq：对端使用哪一版 ZMTP、双方选择什么安全机制、后续字节应该交给哪一版 encoder/decoder，以及这条连接是否已经允许业务消息进入。

因此真实链路不是：

~~~text
connect
  -> application message
~~~

而是：

~~~text
TCP connected
  -> greeting
  -> protocol version
  -> encoder / decoder
  -> security mechanism
  -> handshake commands
  -> READY
  -> normal message flow
~~~

这篇把这条状态机展开。

## 1. 为什么 connect 后直接发业务数据不够

考虑：

~~~text
peer A: ZMTP 3.1 + CURVE
peer B: ZMTP 3.1 + NULL
~~~

TCP 完全可能连接成功，但应用协议并不兼容。

如果没有独立握手层，错误会被拖到 decoder 或业务阶段才暴露，甚至可能把第一帧业务数据误解释为 protocol bytes。

因此 transport connectivity 与 protocol readiness 必须分开。

## 2. zmtp_engine_t 一开始就不是普通数据状态

构造时，Engine 先把两个状态函数指向 routing-id 阶段：

~~~cpp
_next_msg =
  static_cast<int
    (stream_engine_base_t::*) (msg_t *)> (
      &zmtp_engine_t::routing_id_msg);

_process_msg =
  static_cast<int
    (stream_engine_base_t::*) (msg_t *)> (
      &zmtp_engine_t::process_routing_id_msg);
~~~

可以把这两个成员理解成：

~~~text
_next_msg
  -> 当前状态下，下一条 outbound frame 怎样产生

_process_msg
  -> 当前状态下，收到一条 frame 怎样处理
~~~

握手推进时通过替换函数指针完成状态迁移，而不是让每一帧都穿过一个越来越大的 switch。

## 3. plug_internal：握手仍然跑在 Reactor 上

Engine 被挂到 I/O thread 后：

~~~cpp
void zmq::zmtp_engine_t::plug_internal ()
{
    set_handshake_timer ();

    _outpos = _greeting_send;
    _outpos[_outsize++] = UCHAR_MAX;

    put_uint64 (
      &_outpos[_outsize],
      _options.routing_id_size + 1);
    _outsize += 8;

    _outpos[_outsize++] = 0x7f;

    set_pollin ();
    set_pollout ();

    in_event ();
}
~~~

三个动作非常关键：

~~~text
set_handshake_timer
  -> 给协议建立过程一个截止条件

set_pollin / set_pollout
  -> 继续由 poller 驱动

in_event
  -> 立即消费可能已经到达的数据
~~~

握手没有为每条连接单独创建阻塞线程。它只是 Reactor 内的一组 per-connection state。

## 4. greeting 先判断“对端说哪一种 ZMTP”

handshake()：

~~~cpp
bool zmq::zmtp_engine_t::handshake ()
{
    zmq_assert (
      _greeting_bytes_read < _greeting_size);

    const int rc = receive_greeting ();
    if (rc == -1)
        return false;

    const bool unversioned = rc != 0;

    if (!(this->*select_handshake_fun (
          unversioned,
          _greeting_recv[revision_pos],
          _greeting_recv[minor_pos])) ())
        return false;

    if (_outsize == 0)
        set_pollout ();

    return true;
}
~~~

receive_greeting() 还要兼容更老的 wire format：

~~~cpp
if (_greeting_recv[0] != 0xff) {
    unversioned = true;
    break;
}

if (_greeting_bytes_read < signature_size)
    continue;

if (!(_greeting_recv[9] & 0x01)) {
    unversioned = true;
    break;
}

receive_greeting_versioned ();
~~~

也就是说“兼容旧版本”在运行时表现为一个真正的 parser decision：读到足够字节以后，才能判断这一段前缀到底是 greeting，还是旧协议中的 routing-id header。

## 5. 协议版本直接决定编码器和解码器

版本分派：

~~~cpp
switch (revision_) {
    case ZMTP_1_0:
        return &zmtp_engine_t::handshake_v1_0;

    case ZMTP_2_0:
        return &zmtp_engine_t::handshake_v2_0;

    case ZMTP_3_x:
        switch (minor_) {
            case 0:
                return &zmtp_engine_t::handshake_v3_0;
            default:
                return &zmtp_engine_t::handshake_v3_1;
        }

    default:
        return &zmtp_engine_t::handshake_v3_1;
}
~~~

例如 v2：

~~~cpp
_encoder =
  new (std::nothrow)
    v2_encoder_t (_options.out_batch_size);

_decoder =
  new (std::nothrow)
    v2_decoder_t (
      _options.in_batch_size,
      _options.maxmsgsize,
      _options.zero_copy);
~~~

v3.1：

~~~cpp
_encoder =
  new (std::nothrow)
    v3_1_encoder_t (
      _options.out_batch_size);

_decoder =
  new (std::nothrow)
    v2_decoder_t (
      _options.in_batch_size,
      _options.maxmsgsize,
      _options.zero_copy);
~~~

因此 encoder/decoder 属于协商后的 wire protocol，而不是普通的 TCP helper。

## 6. ZMTP 3.x 还要选择 mechanism

版本只是第一层。3.x greeting 中还带有安全机制名称。Engine 会把 peer 声明与本地配置比较，再实例化相应对象。

NULL：

~~~cpp
if (_options.mechanism == ZMQ_NULL
    && memcmp (
         _greeting_recv + 12,
         "NULL\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0",
         20) == 0) {

    _mechanism =
      new (std::nothrow)
        null_mechanism_t (
          session (),
          _peer_address,
          _options);
}
~~~

PLAIN：

~~~cpp
else if (_options.mechanism == ZMQ_PLAIN
         && memcmp (
              _greeting_recv + 12,
              "PLAIN\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0",
              20) == 0) {

    if (_options.as_server)
        _mechanism =
          new (std::nothrow)
            plain_server_t (
              session (),
              _peer_address,
              _options);
    else
        _mechanism =
          new (std::nothrow)
            plain_client_t (
              session (),
              _options);
}
~~~

CURVE 和 GSSAPI 也沿同一分层进入各自 client/server 实现。

如果机制不匹配，不会偷偷降级：

~~~cpp
else {
    socket ()->event_handshake_failed_protocol (
      session ()->get_endpoint (),
      ZMQ_PROTOCOL_ERROR_ZMTP_MECHANISM_MISMATCH);

    error (protocol_error);
    return false;
}
~~~

## 7. mechanism_t 是第二个小状态机

它的接口非常明确：

~~~cpp
class mechanism_t
{
  public:
    enum status_t
    {
        handshaking,
        ready,
        error
    };

    virtual int
      next_handshake_command (
        msg_t *msg_) = 0;

    virtual int
      process_handshake_command (
        msg_t *msg_) = 0;

    virtual int encode (msg_t *)
    {
        return 0;
    }

    virtual int decode (msg_t *)
    {
        return 0;
    }

    virtual status_t status () const = 0;
};
~~~

Engine 负责：

~~~text
什么时候读写
什么时候需要下一条 protocol command
什么时候可以进入普通消息阶段
~~~

Mechanism 负责：

~~~text
PLAIN / CURVE / GSSAPI 的具体状态与命令
认证/加解密相关 encode/decode
最终 ready / error
~~~

这使 transport Reactor 与认证协议可以独立变化。

## 8. 进入 mechanism 阶段以后，状态函数被换掉

3.x 选择 mechanism 后：

~~~cpp
_next_msg =
  &zmtp_engine_t::next_handshake_command;

_process_msg =
  &zmtp_engine_t::process_handshake_command;
~~~

之后 outbound 不再从 Session 拉业务消息，而是先向 mechanism 询问下一条握手 command。

## 9. Engine 怎样驱动 mechanism

stream_engine_base_t 的发送侧：

~~~cpp
int zmq::stream_engine_base_t::
next_handshake_command (msg_t *msg_)
{
    zmq_assert (_mechanism != NULL);

    if (_mechanism->status ()
        == mechanism_t::ready) {
        mechanism_ready ();
        return pull_and_encode (msg_);
    }

    if (_mechanism->status ()
        == mechanism_t::error) {
        errno = EPROTO;
        return -1;
    }

    const int rc =
      _mechanism->
        next_handshake_command (msg_);

    if (rc == 0)
        msg_->set_flags (msg_t::command);

    return rc;
}
~~~

接收侧：

~~~cpp
int zmq::stream_engine_base_t::
process_handshake_command (msg_t *msg_)
{
    zmq_assert (_mechanism != NULL);

    const int rc =
      _mechanism->
        process_handshake_command (msg_);

    if (rc == 0) {
        if (_mechanism->status ()
            == mechanism_t::ready)
            mechanism_ready ();
        else if (_mechanism->status ()
                 == mechanism_t::error) {
            errno = EPROTO;
            return -1;
        }

        if (_output_stopped)
            restart_output ();
    }

    return rc;
}
~~~

Mechanism 没有自己的线程。它只是被 I/O thread 在 readiness 事件中推进。

## 10. ready 是数据面的真正切换点

完整状态可以画成：

~~~text
TCP fd connected
      |
      v
send / receive greeting
      |
      v
select ZMTP version
      |
      v
select encoder / decoder
      |
      v
instantiate mechanism
      |
      v
handshake commands
      |
      +---- error ----> protocol failure
      |
      v
mechanism_t::ready
      |
      v
mechanism_ready()
      |
      v
pull_and_encode / decode_and_push
      |
      v
Session <-> Pipe <-> socket pattern
~~~

所以“TCP 已连接”和“ZeroMQ connection ready”是两个不同状态。

## 11. 为什么正常 Pipe 也应该等 Engine ready

Session 的 engine_ready() 只有在协议阶段完成后，才创建 Session-side 与 socket-side 的正常消息 Pipe。

否则可能出现：

~~~text
application send
   |
   v
normal pipe exists too early
   |
   v
application frame reaches Engine
   |
   v
peer still expects READY/authentication command
~~~

把 protocol readiness 作为 normal data channel 的门槛，业务数据和握手控制流就不会混在一起。

## 12. routing id 与 command 为什么继续复用 msg_t

协议控制消息没有另造一套 buffer hierarchy，而是继续使用 msg_t，并通过 flag 区分：

~~~text
command
routing_id
credential
ping / pong
subscribe / cancel
~~~

这让 Pipe、encoder、decoder 和 Session 仍围绕同一个 envelope 工作。

这也是 [msg_t 存储与引用计数](msg-storage-refcount.md) 的意义：它不仅承载业务 payload，也承载 Runtime 内部协议帧。

## 13. heartbeat 为什么放在 Engine 层

ZMTP 3.1 的 PING/PONG 也在 Engine 层处理。它验证的是：

~~~text
transport / protocol liveness
~~~

而不是：

~~~text
business callback health
~~~

如果 heartbeat 先进入应用线程再回复，应用高负载会被误判成网络死亡。反过来，Heartbeat 成功也不能证明机器人控制算法健康；它只证明协议层仍有交换。

## 14. 可以怎样复刻这种设计

不要把握手写成：

~~~cpp
void handshake(int fd) {
    blocking_read(...);
    blocking_write(...);
    authenticate(...);
    blocking_read(...);
}
~~~

更适合 Reactor 的结构是：

~~~text
ConnectionState
  greeting parser
  encoder
  decoder
  mechanism
  state

on_readable()
  consume available bytes
  advance state
  maybe emit command

on_writable()
  flush pending bytes
  advance state

on_timer()
  fail if handshake deadline exceeded
~~~

这样一个 I/O thread 可以推进许多连接，而每条连接只保留自己的协议状态。

下一篇沿 [TCP Connect 与重连](tcp-reconnect-state-machine.md) 看物理连接失败以后如何回到可恢复状态机；正常消息路径则回到 [Session 与 Stream Engine](session-stream-engine.md)。
