# socket_base：应用线程为什么也要处理 command

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

前面 Mailbox 章节主要看 I/O thread。现在换到 application-facing socket。一个容易忽略的问题是：**socket_base_t 自己也有 mailbox，也要处理异步 command。**

## command 从哪里来

`object_t` 提供大量 `send_*()`：stop、plug、attach、bind、activate_read、activate_write、pipe_term 等。它们都会构造 `command_t`，填入 destination，再交给 context：

~~~cpp
cmd.destination = destination_;
cmd.type = command_t::activate_write;
cmd.args.activate_write.msgs_read = msgs_read_;
send_command (cmd);
~~~

`ctx_t::send_command()` 本身很薄：

~~~cpp
void ctx_t::send_command (uint32_t tid_, const command_t &command_)
{
    _slots[tid_]->send (command_);
}
~~~

真正重要的是 `_slots[tid_]`：每个 execution owner 都对应自己的 mailbox slot。

## 为什么 application thread 不能永远不看 mailbox

如果 application thread 正在不断调用 send/recv，而另一个线程触发 context termination、pipe state 改变或其他控制事件，那么这些 command 必须进入 socket owner 的执行序列。

`socket_base_t::process_commands()` 做的就是这件事。

~~~cpp
command_t cmd;
int rc = _mailbox->recv (&cmd, timeout_);

while (rc == 0 || errno == EINTR) {
    if (rc == 0)
        cmd.destination->process_command (cmd);
    rc = _mailbox->recv (&cmd, 0);
}
~~~

这段和 `io_thread_t::in_event()` 很像：一次机会尽量 drain 当前 command。

## 为什么还有 throttle

源码里 non-blocking 路径会用 TSC 判断距离上一次 command processing 是否太近。原因很现实：如果每次极短的 send/recv 都检查 mailbox，会把 fast path 的固定成本拉高。

因此 socket runtime 在做折中：

~~~text
command latency
    vs
hot-path overhead
~~~

源码注释甚至指出 command delay 大约随 CPU 主频落在毫秒量级。这里不是绝对实时保证，而是吞吐与控制响应之间的工程折中。

## owner thread + command passing 的关键收益

假设 pipe 的状态 `_out_active` 本应由 socket owner 修改。另一个线程不直接改这个 bool，而是发送 activate_write command。这样 mutable state 的 owner 不变。

~~~text
foreign thread
   |
command_t
   v
owner mailbox
   |
owner executes process_command
   |
modify owner-local state
~~~

这比“每个字段都套 mutex”更容易维护对象不变量。

## 但 command passing 并不等于没有同步

发送 command 仍然需要 mailbox 的 sender-side mutex、atomic ypipe boundary 和 signaler；对象生命周期还需要 sequence/termination 协议。

所以正确结论不是“消息传递没有锁”，而是：**把同步成本集中在边界，让大部分对象内部状态维持单 owner。**

## 对机器人 Runtime 的直接映射

例如网络管理线程想让控制通信 owner 重连设备，不要直接从外部线程改 `connection_state`、fd、timer、pending queue。更稳妥的方式是发送 `ReconnectCommand`，由 owner thread 在自己的 event loop 中执行状态迁移。
