# libzmq 总览：Socket API 背后为什么需要消息 Runtime

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

libzmq 最值得研究的不是 `zmq_send()` / `zmq_recv()` 的表面 API，而是：**一个看起来像 socket 的对象，为什么内部需要 pipe、mailbox、I/O thread、session、engine、poller 与 HWM。**

## 从最朴素的 send 开始

~~~text
application thread
      |
      v
   send()
      |
      v
kernel socket
~~~

这种模型默认调用线程自己负责网络 I/O。ZeroMQ 还必须处理多 peer、消息边界、慢消费者、重连、跨线程控制和 socket pattern，因此内部链条更接近：

~~~text
Application thread
      |
      v
socket_base_t
      |
      v
   pipe_t
      |
      v
session_base_t
      |
      v
stream/ZMTP engine
      |
      v
 OS socket fd
~~~

控制线则是：

~~~text
thread A -> command_t -> mailbox_t -> ypipe + signaler -> owner thread
~~~

## 数据线与控制线分开

数据线主要搬 `msg_t`；控制线搬 stop、plug、attach、bind、activate_read、activate_write、pipe_term 等 `command_t`。这意味着 libzmq 尽量避免线程 A 直接修改线程 B 所拥有对象的内部状态，而是把动作变成 command，交回 owner thread。

## Pipe 不是 OS pipe

`pipepair()` 创建两个 endpoint，并用两条单向 ypipe 连接：

~~~cpp
typedef ypipe_t<msg_t, message_pipe_granularity> upipe_normal_t;

pipes_[0] = new pipe_t (..., upipe1, upipe2, ...);
pipes_[1] = new pipe_t (..., upipe2, upipe1, ...);
~~~

逻辑上：

~~~text
pipe A -- ypipe1 --> pipe B
pipe A <-- ypipe2 -- pipe B
~~~

两条单向 channel 让每个方向都形成 one logical writer / one logical reader，为 SPSC 数据结构创造清晰 ownership。

## Mailbox 解决跨线程控制

`io_thread_t` 自己拥有 mailbox 和 poller；构造时把 mailbox fd 加进 poller：

~~~cpp
_mailbox_handle = _poller->add_fd (_mailbox.get_fd (), this);
_poller->set_pollin (_mailbox_handle);
~~~

于是同一个 I/O owner 可以同时等待网络 fd、timer 和跨线程 command。

## Session 是消息世界与 transport 世界的边界

`session_base_t` 一边读写内部 pipe，一边挂接 transport engine。Engine 才真正拥有 OS fd、non-blocking I/O、encoder/decoder、handshake、heartbeat 与 framing。

## HWM 是状态机，不只是 queue size

`pipe_t::check_write()` 在 HWM 达到后会把 `_out_active` 置为 false。Backpressure 因而直接改变 pipe 的运行状态，而不是等内存爆掉后再补救。

## 总图

~~~text
application
   |
socket_base_t
   |
pipe_t / ypipe / HWM
   |
session_base_t
   |
stream engine
   |
OS socket

control: object_t -> command_t -> mailbox -> poller -> owner thread
~~~

后续按 Mailbox → yqueue/ypipe → Pipe/HWM → socket_base → io_thread/poller → session → engine → socket patterns 的顺序继续拆。
