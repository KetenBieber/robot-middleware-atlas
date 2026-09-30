# socket_base：Owner Thread、Command Routing 与 API Fast Path

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

`socket_base_t` 是应用线程看到的核心 ZeroMQ socket 对象。它既承载 send/recv/bind/connect 等 API，又参与 pipe 生命周期、context termination、monitor、reaper 与异步 command 处理。

真正重要的不是“socket 有一个 mailbox”，而是整个运行时遵守一个很强的边界：

~~~text
foreign thread
    |
    | command_t
    v
target owner mailbox
    |
    v
owner thread
    |
    v
target object state transition
~~~

这使大量 mutable state 可以维持单 owner，而不是每个字段都被多个线程直接锁着修改。

## object_t 为什么有 tid

`object_t` 保存：

~~~text
_ctx
_tid
~~~

`_tid` 不是 OS thread handle，而是 libzmq context 内部用于定位 owner execution slot 的 id。

发送 command 时：

~~~cpp
void object_t::send_command (const command_t &cmd_)
{
    _ctx->send_command (cmd_.destination->get_tid (), cmd_);
}
~~~

`ctx_t` 再执行：

~~~cpp
void ctx_t::send_command (uint32_t tid_,
                          const command_t &command_)
{
    _slots[tid_]->send (command_);
}
~~~

因此 command routing 的核心公式是：

~~~text
destination object
      |
      | get_tid()
      v
context slot[tid]
      |
      v
owner mailbox
~~~

sender 不需要知道目标线程当前睡在哪个 poller，也不需要持有目标对象内部锁。

## ctx_t::_slots 的角色

可以把 context 抽象成：

~~~text
ctx_t
 |
 +-- slot 0 -> administrative/reaper mailbox
 +-- slot 1 -> I/O thread mailbox
 +-- slot 2 -> I/O thread mailbox
 +-- slot 3 -> socket/application owner mailbox
 +-- slot 4 -> socket/application owner mailbox
 ...
~~~

不同对象通过 `tid` 映射到对应 mailbox。

所以 command path 不是：

~~~text
object pointer -> direct cross-thread function call
~~~

而是：

~~~text
object pointer
  -> object tid
  -> context slot
  -> mailbox
  -> owner execution point
  -> process_command()
~~~

这相当于给每个 execution owner 建立一个控制面 inbox。

## command_t 为什么同时需要 destination 和 type

一个 mailbox 里可以混合送往同一 owner thread 上不同对象的命令：

~~~text
command #1 -> pipe A activate_write
command #2 -> session B attach
command #3 -> socket C stop
command #4 -> pipe D term
~~~

因此 command 必须保存：

~~~text
destination
type
args
~~~

owner thread 从 mailbox dequeue 后：

~~~cpp
cmd.destination->process_command (cmd);
~~~

再由 `object_t::process_command()` 分派：

~~~cpp
switch (cmd_.type) {
    case command_t::activate_read:
        process_activate_read ();
        break;

    case command_t::activate_write:
        process_activate_write (
          cmd_.args.activate_write.msgs_read);
        break;

    case command_t::stop:
        process_stop ();
        break;

    ...
}
~~~

所以 mailbox 是 per-owner-thread，`destination` 是 per-object。这两个层次不能混淆。

## socket_base_t 自己为什么也需要 mailbox

I/O thread 有 mailbox 很容易理解，因为它在 poller 中睡眠。

应用-facing socket 也必须接收异步状态变化，例如：

~~~text
pipe becomes readable
pipe becomes writable
context terminated
new inproc peer binds
pipe terminates
monitor/reaper lifecycle changes
~~~

这些事件可能由别的 execution owner 发起，但最终会改变 socket 的：

~~~text
pipe containers
scheduler state
termination state
_ctx_terminated
derived socket pattern state
~~~

所以 application thread 在执行 socket API 时，必须周期性把 mailbox 中的 command 合并进自己的 owner-local state。

## socket 的 mailbox 有两种实现

构造函数根据 `thread_safe_` 选择：

~~~cpp
if (_thread_safe) {
    _mailbox =
      new mailbox_safe_t (&_sync);
}
else {
    mailbox_t *m = new mailbox_t ();
    ...
    _mailbox = m;
}
~~~

普通 socket：

~~~text
single application owner
+
mailbox_t
~~~

thread-safe socket：

~~~text
multiple API callers
+
socket-level _sync mutex
+
mailbox_safe_t
~~~

后者允许多个 API 调用者共享同一个 socket，因此 mailbox 也必须适配 receiver 不再唯一的情况。

## mailbox_safe_t 与 mailbox_t 的差异

普通 `mailbox_t` 的核心拓扑是：

~~~text
many senders
one receiver
~~~

所以它可以使用：

~~~text
sender-side mutex
+
SPSC ypipe
+
signaler
~~~

而 `mailbox_safe_t` 需要支持 thread-safe socket 的共享 API，因此：

~~~text
senders and receivers
share socket _sync
~~~

它内部使用：

~~~text
_cpipe
_cond_var
_sync pointer
_signalers vector
~~~

接收时：

~~~cpp
if (_cpipe.read (cmd_))
    return 0;

if (timeout_ == 0) {
    _sync->unlock ();
    _sync->lock ();
}
else {
    const int rc =
      _cond_var.wait (_sync, timeout_);
    ...
}
~~~

这里 condition variable 又重新出现，是因为 thread-safe socket 的调用线程不是固定的一只 event-loop receiver。

这说明同步原语由**等待边界和线程拓扑**决定，而不是“某个库永远只用某一种 primitive”。

## process_commands() 是 socket owner 的控制面入口

固定源码：

~~~cpp
int socket_base_t::process_commands (
  int timeout_,
  bool throttle_)
{
    ...
    command_t cmd;
    int rc = _mailbox->recv (&cmd, timeout_);

    if (rc != 0 && errno == EINTR)
        return -1;

    while (rc == 0 || errno == EINTR) {
        if (rc == 0)
            cmd.destination->process_command (cmd);

        rc = _mailbox->recv (&cmd, 0);
    }

    zmq_assert (errno == EAGAIN);

    if (_ctx_terminated) {
        errno = ETERM;
        return -1;
    }

    return 0;
}
~~~

和 `io_thread_t::in_event()` 一样，它会 drain 当前可用 command。

区别在于 socket owner 并不是一直运行一个内部 event loop。它通常在用户调用：

~~~text
send
recv
bind
connect
getsockopt
...
~~~

时获得 CPU，所以 command processing 必须嵌入这些 API 路径。

## send() 为什么先处理 command

发送路径需要先让 owner state 更新，再调用 socket-pattern-specific `xsend()`。

如果 mailbox 里已经有：

~~~text
activate_write
pipe_term
context stop
~~~

但 send 直接使用旧状态，可能得到错误的 writable 判断，甚至在已终止对象上继续操作。

因此：

~~~text
API call
  |
  v
merge pending control-plane state
  |
  v
operate on updated owner-local state
~~~

这是 command passing 模型的必要配套。

## 为什么不能每次 API 调用都完整 drain mailbox

如果应用高频调用 non-blocking send：

~~~text
send()
send()
send()
send()
...
~~~

每次都增加：

~~~text
mailbox check
atomic work
branch
possibly syscall
~~~

会给 fast path 增加固定成本。

所以 `process_commands(timeout=0, throttle=true)` 使用 TSC 做节流。

固定源码：

~~~cpp
const uint64_t tsc = clock_t::rdtsc ();

if (tsc && throttle_) {
    if (tsc >= _last_tsc
        && tsc - _last_tsc <= max_command_delay)
        return 0;

    _last_tsc = tsc;
}
~~~

`max_command_delay` 固定配置是：

~~~text
3,000,000 CPU ticks
~~~

源码注释给出的量级大约是：

~~~text
3 GHz CPU -> ~1 ms
1.5 GHz CPU -> ~2 ms
~~~

这不是实时 deadline，而是 throughput/command-latency trade-off。

## throttle 的代价必须明确

节流意味着：

~~~text
command 已经在 mailbox
!=
当前 API 调用一定马上处理
~~~

在持续高吞吐路径里，某些 command 可能延迟到下一次允许检查的时间点。

这是 API fast path 的工程选择：

~~~text
更少 control-plane polling overhead
        vs
更低 command latency
~~~

如果机器人控制 runtime 采用类似策略，最大 command latency 必须纳入控制 deadline，而不能只看平均吞吐。

## send 与 recv 为什么使用不同 command polling 策略

recv 热路径有：

~~~cpp
if (++_ticks == inbound_poll_rate) {
    if (process_commands (0, false) != 0)
        return -1;

    _ticks = 0;
}
~~~

固定配置：

~~~text
inbound_poll_rate = 100
~~~

也就是连续处理一批 inbound message 后，强制回到 control plane 看一次。

这是一种 fairness 机制：

~~~text
data plane cannot monopolize owner forever
~~~

否则高流量 recv 可以让：

~~~text
termination
pipe activation
ownership changes
~~~

长期得不到处理。

## blocking recv 为什么天然成为 command processing 点

如果当前没有业务消息，blocking recv 本来就需要等待。

阻塞路径可以抽象成：

~~~text
try xrecv
  |
  | no message
  v
process_commands(timeout)
  |
  v
state may change
  |
  v
try xrecv again
~~~

这与 condition variable 的：

~~~text
while (!predicate)
    wait
~~~

结构相似。

不同的是 predicate 可能由 pipe activation、context termination、peer lifecycle 等多个 command 改变。

## stop command 如何打断阻塞 API

`socket_base_t::stop()`：

~~~cpp
void socket_base_t::stop ()
{
    send_stop ();
}
~~~

源码说明它可以由另一个线程调用。

`send_stop()` 构造：

~~~text
destination = this
type = stop
~~~

再路由回 socket owner mailbox。

owner 执行：

~~~cpp
void socket_base_t::process_stop ()
{
    scoped_lock_t lock (_monitor_sync);
    stop_monitor ();

    _ctx_terminated = true;
}
~~~

之后 `process_commands()` 检查：

~~~cpp
if (_ctx_terminated) {
    errno = ETERM;
    return -1;
}
~~~

所以阻塞 API 被中断的链是：

~~~text
thread A calls ctx_term
      |
      v
stop command
      |
      v
socket owner mailbox wakes
      |
      v
process_stop()
      |
      v
_ctx_terminated = true
      |
      v
blocking send/recv returns ETERM
~~~

不是另一个线程直接终止当前 OS thread。

## attach_pipe 为什么必须在 socket owner 上执行

`socket_base_t::attach_pipe()`：

~~~cpp
pipe_->set_event_sink (this);
_pipes.push_back (pipe_);

xattach_pipe (
  pipe_,
  subscribe_to_all_,
  locally_initiated_);
~~~

这里会同时修改：

~~~text
base socket pipe registry
derived socket pattern scheduler/state
pipe event sink
~~~

如果 foreign thread 直接调用，就需要围绕多个容器和 pattern state 建复杂同步。

通过 `bind` command：

~~~text
foreign owner
  |
  | send_bind(socket, pipe)
  v
socket mailbox
  |
  v
socket_base_t::process_bind()
  |
  v
attach_pipe()
~~~

这些状态变化仍由 socket owner 串行完成。

## i_pipe_events 为什么再转给 x* 方法

`socket_base_t` 同时实现 `i_pipe_events`。

当 pipe 变 readable/writable 时，base class 接收事件，再交给派生 pattern：

~~~text
pipe event
   |
   v
socket_base_t
   |
   v
xread_activated / xwrite_activated
   |
   v
DEALER / ROUTER / PUB / SUB policy
~~~

职责边界：

~~~text
pipe:
  transport/channel state

socket pattern:
  routing/scheduling semantics
~~~

所以同一个 `pipe_t` 可以被不同 socket type 用完全不同的调度策略管理。

## owner-thread 模型不等于所有 socket API 都线程安全

普通 ZeroMQ socket 的 owner-thread 假设很强。

owner-thread command passing 解决的是**内部 execution ownership**，不是自动把任意 socket API 变成可并发调用。

thread-safe socket 类型会显式启用：

~~~text
_thread_safe
_sync
mailbox_safe_t
~~~

这说明“内部有 mailbox”与“公共对象支持多线程同时调用”是两个不同问题。

## sequence number 为什么出现在某些 command

`send_plug`、`send_own`、`send_attach`、`send_bind` 等路径可能先：

~~~text
destination->inc_seqnum()
~~~

再发 command。

目的不是给所有 command 排一个全局序号，而是让生命周期知道：

~~~text
还有异步 command 在路上
~~~

否则可能发生：

~~~text
owner thinks no outstanding work
-> begins destruction
-> previously sent attach/bind arrives
-> destination already dead
~~~

command passing 体系里，lifetime accounting 和 mailbox 同样重要。

## process_command() 为什么用 type + args，而不是通用 callback queue

`command_t` 采用：

~~~text
enum type
+
union args
+
destination pointer
~~~

owner 执行统一 switch，再进入 virtual handler。

这避免每条 command 都分配通用 callable object，也让 command layout、ownership 与 ABI 更可控。

代价是新增 command type 需要同时扩展：

~~~text
enum
args union
dispatch
handler
~~~

这是 runtime infrastructure 常见的封闭命令集合设计。

## context slot 与 Actor mailbox 的关系

相似点：

~~~text
owner-local mutable state
message/command passing
mailbox serialization
~~~

但 libzmq 不是纯 Actor 模型，因为还存在：

~~~text
shared pipe memory
atomic ypipe
sender-side mutex
socket API thread
poller callbacks
~~~

更准确的描述是：

~~~text
owner-thread runtime
+
command passing control plane
+
shared-memory data plane
~~~

## 对机器人 Runtime 的迁移

假设 CAN owner thread 内部拥有：

~~~text
fd/socket
tx queue
reconnect state
bus health
timer
device registry
~~~

其他线程不要直接：

~~~text
lock can_runtime
modify reconnect_state
erase device
close fd
~~~

可以改成：

~~~text
Control thread ------\
Estimator thread -----+--> Command mailbox --> CAN owner
Health thread -------/                         |
                                                +-- fd
                                                +-- timers
                                                +-- state machine
~~~

command 可以是：

~~~text
SendFrame
ResetBus
AddDevice
RemoveDevice
Shutdown
~~~

owner thread 串行执行状态迁移。

真正需要同步的边界收缩到：

~~~text
command publication
payload ownership transfer
shutdown/lifetime
~~~

而 runtime 内部大量 mutable state 可以继续是普通字段。

## 核心不变量

~~~text
1. every mutable runtime object has an execution owner
2. foreign threads do not directly mutate owner-local state
3. command carries destination + type + args
4. destination tid selects mailbox slot
5. owner drains commands at explicit scheduling points
6. object lifetime accounts for in-flight async commands
~~~

只做到前五条、不处理第六条，shutdown 时仍然可能出现 use-after-free。
