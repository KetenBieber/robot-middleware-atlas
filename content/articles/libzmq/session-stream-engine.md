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
