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

真正关键的不只是“mailbox 有一个可 poll 的 fd”，而是 writer publish、reader sleep registration 与 wake decision 被 `ypipe::_c` 的 CAS 串成同一个 lost-wakeup-safe handoff；多个 sender 再由 `_sync` 串成一个逻辑 writer。完整协议见 [Mailbox：跨线程 Command、Lost Wakeup 与 Owner-Thread 执行模型](mailbox-command-wakeup.md)。

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

Mailbox、yqueue/ypipe、Pipe/HWM、socket owner、I/O Reactor、Session/Engine 与 socket pattern 共同组成这套消息 Runtime：底层负责所有权、发布、唤醒和容量，上层再叠加公平调度、显式路由与订阅匹配语义。

最外层的生命周期闭环由 Context/Reaper 完成：logical tid 首先只是 `_slots[tid] → mailbox` 的控制面地址；socket close 后地址仍暂时保留给晚到 command，直到 Reaper 完成 internal quiescence、poller detach 和 registry erase 后才允许 slot reuse，最后以 DONE 唤醒 Context terminate。见 [Context 与 Reaper：Slot 地址空间、Ownership Handoff 与最终销毁屏障](context-reaper-lifecycle.md)。

其中 DEALER 可以直接理解成 `FQ + LB`；ROUTER 则把输出选择改成 `routing-id → pipe_t*` 的精确索引，并用 `_current_in/_current_out` 把已经开始的 multipart 与后来发生的 disconnect/handover 隔离。见 [DEALER / ROUTER：显式路由、Routing-ID 生命周期与 Multipart 粘性](dealer-router-routing.md)。
