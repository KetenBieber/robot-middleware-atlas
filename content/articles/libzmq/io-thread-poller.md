# io_thread / poller：网络 fd、Mailbox 与 Timer 怎样进入同一个 Reactor

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

libzmq 的 `io_thread_t` 不是“一个只负责 socket recv/send 的线程”。它真正拥有的是一套 Reactor：poller 等待 fd readiness 和 timer，mailbox 把跨线程 command 也转换成 poller 可见事件，具体 engine、listener、connector 再以 `i_poll_events` 对象挂到这只 Reactor 上。

~~~text
io_thread_t
  |
  +-- mailbox_t
  |     |
  |     +-- signaler fd
  |
  +-- poller_t
        |
        +-- mailbox signaler fd -> io_thread_t
        +-- TCP fd              -> stream_engine
        +-- listener fd         -> listener object
        +-- connector fd        -> connector object
        +-- timers              -> corresponding io_object
~~~

I/O thread 的本质是：

~~~text
one thread
+
one poller
+
many event sources
+
owner-local callbacks
~~~

## poller_t 不是固定等于 epoll

`poller.hpp` 根据编译平台选择：

~~~text
kqueue
epoll
devpoll
pollset
poll
select
~~~

Linux 常见路径：

~~~cpp
typedef epoll_t poller_t;
~~~

上层只依赖统一 poller concept：

~~~text
add_fd
rm_fd
set_pollin
reset_pollin
set_pollout
reset_pollout
add_timer
cancel_timer
start
stop
~~~

OS-specific multiplexing 被限制在 poller implementation。

## io_thread_t 构造时为什么先注册 mailbox fd

固定源码：

~~~cpp
io_thread_t::io_thread_t (ctx_t *ctx_,
                          uint32_t tid_) :
    object_t (ctx_, tid_),
    _mailbox_handle (
      static_cast<poller_t::handle_t> (NULL))
{
    _poller = new poller_t (*ctx_);

    if (_mailbox.get_fd () != retired_fd) {
        _mailbox_handle =
          _poller->add_fd (
            _mailbox.get_fd (),
            this);

        _poller->set_pollin (
          _mailbox_handle);
    }
}
~~~

mailbox signaler fd 的 event sink 是：

~~~text
this == io_thread_t
~~~

因此 signaler readable 时，poller 回调：

~~~text
io_thread_t::in_event()
~~~

这只 fd 只是 doorbell。真正的 command 保存在 `mailbox_t::_cpipe`，而是否需要 ring doorbell 由 `ypipe::flush()` 对 reader passive state 的 CAS 结果决定；因此 poller wakeup 与 command publication 并不是两个独立动作。完整交接协议见 [Mailbox：跨线程 Command、Lost Wakeup 与 Owner-Thread 执行模型](mailbox-command-wakeup.md)。

普通网络 fd 的 event sink 则通常是：

~~~text
stream_engine
listener
connector
~~~

同一 poller 中，不同 fd 可以映射到不同 C++ 对象。

## add_fd(fd, events) 真正保存了什么

epoll 实现内部：

~~~cpp
struct poll_entry_t
{
    fd_t fd;
    epoll_event ev;
    i_poll_events *events;
};
~~~

`add_fd()`：

~~~cpp
poll_entry_t *pe =
  new poll_entry_t;

pe->fd = fd_;
pe->ev.events = 0;
pe->ev.data.ptr = pe;
pe->events = events_;

epoll_ctl (
  _epoll_fd,
  EPOLL_CTL_ADD,
  fd_,
  &pe->ev);
~~~

内核返回事件以后，libzmq 通过 `data.ptr` 找回：

~~~text
poll_entry_t
  |
  +-- fd
  +-- interested events
  +-- i_poll_events* callback target
~~~

这是 fd 到对象 callback 的映射层。

## i_poll_events 为什么只有三个主要入口

接口核心：

~~~text
in_event()
out_event()
timer_event(id)
~~~

它们覆盖：

~~~text
fd readable / error
fd writable
timer expired
~~~

具体对象自己解释事件含义。

例如：

~~~text
mailbox readable
-> io_thread_t::in_event
-> drain command

TCP readable
-> stream_engine::in_event
-> read/decode bytes

listener readable
-> listener::in_event
-> accept connection
~~~

poller 不理解 ZMTP、pipe、socket pattern 或 command type。

## epoll loop 的执行顺序

固定源码主循环：

~~~cpp
while (true) {
    const int timeout =
      static_cast<int> (
        execute_timers ());

    if (get_load () == 0) {
        if (timeout == 0)
            break;
        continue;
    }

    const int n =
      epoll_wait (
        _epoll_fd,
        &ev_buf[0],
        max_io_events,
        timeout ? timeout : -1);

    ...

    for (int i = 0; i < n; i++) {
        ...
    }

    ...
}
~~~

运行时阶段：

~~~text
execute due timers
        |
        v
derive next timeout
        |
        v
epoll_wait
        |
        v
dispatch ready fds
        |
        v
reclaim retired poll entries
        |
        v
repeat
~~~

Timer 与 fd readiness 共享同一个 owner thread，不需要额外 timer thread 直接修改 engine state。

## max_io_events 为什么是有界 batch

固定配置：

~~~text
max_io_events = 256
~~~

epoll 每轮最多取有限事件：

~~~cpp
epoll_event ev_buf[max_io_events];
~~~

高负载下下一轮会继续处理，但 Reactor 的工作天然被分成 batch。

这与 mailbox drain、stream output batch 一起说明 libzmq 多处都在做：

~~~text
batch enough for throughput
but keep scheduling boundaries
~~~

## EPOLLERR / EPOLLHUP 为什么进入 in_event()

事件循环：

~~~cpp
if (ev.events & (EPOLLERR | EPOLLHUP))
    pe->events->in_event ();

if (ev.events & EPOLLOUT)
    pe->events->out_event ();

if (ev.events & EPOLLIN)
    pe->events->in_event ();
~~~

错误/HUP 统一先进入具体对象的 input/error path。poller 不负责解释连接状态，也不直接关闭业务对象。

## 为什么每个 callback 后都重新检查 retired_fd

循环中多次：

~~~cpp
if (pe->fd == retired_fd)
    continue;
~~~

callback 可能直接执行：

~~~text
rm_fd(handle)
~~~

例如 `in_event()` 发现协议错误后可能触发 engine teardown。

如果同一 epoll event 随后继续调用 `out_event()`，就可能访问已经逻辑删除的 event source。

因此 callback 不是纯函数；它可以修改当前 Reactor registration。

## rm_fd() 为什么不立即 delete poll_entry_t

`rm_fd()`：

~~~cpp
epoll_ctl (
  _epoll_fd,
  EPOLL_CTL_DEL,
  pe->fd,
  &pe->ev);

pe->fd = retired_fd;
_retired.push_back (pe);
~~~

没有立即 delete。

原因是当前 `epoll_wait()` 返回的 `ev_buf` 中可能仍然持有：

~~~text
ev.data.ptr == pe
~~~

立即 free 会形成：

~~~text
current local event batch
still references pe
-> use-after-free
~~~

所以采用两阶段回收：

~~~text
logical retire now
physical delete after current dispatch batch
~~~

## 为什么这里不需要 hazard pointer

event dispatch 与 `_retired` 回收都发生在同一个 I/O owner thread。

生命周期风险来自当前本地 event batch，而不是多个 CPU 并发持有 poll entry。

所以单线程 owner + batch boundary 已经足够形成安全 reclamation point。

## check_thread() 的价值

`epoll_t::add_fd/rm_fd/set_pollin/...` 都会检查调用线程。

poller registration state 假设由对应 I/O thread 管理。

foreign thread 如果想改变 I/O 状态，不应该直接：

~~~text
set_pollout(handle)
rm_fd(handle)
~~~

而应该：

~~~text
send command
-> owner handler
-> owner calls poller API
~~~

这与 pipe/socket 的 owner-thread 模型完全一致。

## io_object_t 是 I/O owner 的适配层

`io_object_t` 内部只保存：

~~~text
poller_t *_poller
~~~

`plug(io_thread)`：

~~~cpp
_poller =
  io_thread_->get_poller ();
~~~

之后派生对象可以调用：

~~~text
add_fd
rm_fd
set_pollin
reset_pollin
set_pollout
reset_pollout
add_timer
cancel_timer
~~~

而不需要每个 engine/listener 都手动维护 poller 实现细节。

这层抽象表达的是：

~~~text
this object lives on this Reactor
~~~

不是“这个对象拥有 poller”。

## plug / unplug 为什么支持 execution migration

`io_object_t::unplug()`：

~~~cpp
_poller = NULL;
~~~

源码注释允许：

~~~text
unplug from old I/O thread
migrate object
plug to new I/O thread
~~~

迁移前必须先正确撤销旧 fd/timer registration。否则对象虽然换了 owner 指针，内核事件仍可能回调旧 Reactor。

## stream_engine 怎样注册真实网络 fd

`stream_engine_base_t::plug()`：

~~~cpp
_session = session_;
_socket = _session->get_socket ();

io_object_t::plug (io_thread_);

_handle = add_fd (_s);
_io_error = false;

plug_internal ();
~~~

于是：

~~~text
TCP socket fd
   |
   v
same poller
   |
   v
stream_engine_base_t
~~~

engine 的 `in_event/out_event` 由该 I/O thread 串行调用。

这意味着：

~~~text
decoder
encoder
handshake state
input/output buffer
poll flags
timers
~~~

都可以主要保持 owner-local。

## mailbox fd 与 network fd 的 callback owner 不同

同一个 poller：

~~~text
mailbox fd
  -> io_thread_t object

network fd
  -> stream_engine object
~~~

所以 event loop 不是一个巨大 fd switch，而是：

~~~text
fd registration stores event-sink object
event-sink object handles own event
~~~

这是一种对象化 Reactor。

## io_thread_t::in_event() 如何处理控制面

固定源码：

~~~cpp
command_t cmd;
int rc =
  _mailbox.recv (&cmd, 0);

while (rc == 0 || errno == EINTR) {
    if (rc == 0)
        cmd.destination->process_command (cmd);

    rc =
      _mailbox.recv (&cmd, 0);
}
~~~

控制面也只是 Reactor 的一种 event source。

一轮 epoll 可能返回：

~~~text
mailbox ready
TCP A readable
TCP B writable
~~~

随后同一 I/O thread 依次执行：

~~~text
io_thread.in_event()
  -> process commands

engine A.in_event()
  -> read/decode

engine B.out_event()
  -> encode/write
~~~

不应假设 mailbox 一定先于网络 I/O，或网络 I/O 一定先于 mailbox。

## process_stop() 如何让 Reactor 收敛

`io_thread_t::process_stop()`：

~~~cpp
_poller->rm_fd (_mailbox_handle);
_poller->stop ();
~~~

epoll implementation 的退出还会结合 load 与 timer 状态。

所以停止不是粗暴 cancel OS thread，而是让 registered event sources 和生命周期协议逐步收敛到退出条件。

## get_load() 为什么能用于 choose_io_thread()

`ctx_t::choose_io_thread()`：

~~~cpp
const int load =
  _io_threads[i]->get_load ();

if (selected == NULL || load < min_load)
    selected = _io_threads[i];
~~~

epoll：

~~~text
add_fd -> adjust_load(+1)
rm_fd  -> adjust_load(-1)
~~~

这里的 load 更接近注册 event source 的负载指标，而不是 CPU utilization。

它很便宜，适合在新连接/对象选择 I/O owner 时做近似平衡。

## Reactor 为什么能减少锁

假设 engine 的字段：

~~~text
_decoder
_encoder
_input_stopped
_output_stopped
_insize
_outsize
_handle
handshake timers
~~~

都只由同一 I/O thread callback 修改。

这些字段就不需要跨线程 mutex。

其他线程想改变 engine/session 状态时：

~~~text
foreign thread
  -> command mailbox
  -> owner callback
~~~

所以性能收益不只来自 epoll；更重要的是：

~~~text
event loop serializes mutable state transitions
~~~

## Reactor 的代价：callback 不能做无界阻塞工作

如果某个 callback 执行：

~~~text
CPU-heavy work 20 ms
blocking filesystem call
long user callback
~~~

同一 I/O thread 上其他：

~~~text
network fd
timer
mailbox command
~~~

都会被延迟。

所以 Reactor 隐含一个关键约束：

~~~text
event callback must make bounded progress
~~~

这也是为什么 I/O Runtime 和任意业务算法通常需要不同 execution boundary。

## speculative I/O 为什么与 poller 并存

Event Loop 不意味着所有 I/O 都必须先等 readiness callback。

当 engine 知道新消息刚到，可以直接尝试 write：

~~~text
message available
-> restart_output()
-> speculative out_event()
~~~

成功时减少一次：

~~~text
enable EPOLLOUT
-> epoll_wait
-> callback
~~~

失败再回到 normal readiness path。

这是：

~~~text
optimistic fast path
+
Reactor fallback
~~~

## Timer 为什么应该和 fd 同 owner

handshake timeout、heartbeat、reconnect timer 如果由独立 timer thread 直接修改 engine：

~~~text
timer thread modifies engine
I/O thread modifies engine
~~~

又会重新引入锁。

把 timer callback 放到 poller owner：

~~~text
timer expiry
-> same I/O thread
-> timer_event(id)
-> mutate owner-local state
~~~

可以继续维持单 owner。

## 与 libuv Event Loop 的共性

两者都有：

~~~text
fd readiness
timer
cross-thread wakeup
owner-thread callbacks
deferred lifecycle
~~~

libzmq Reactor 更服务于自身消息 Runtime：

~~~text
mailbox command
pipe activation
session/engine
socket pattern
~~~

libuv 更像通用异步 I/O Runtime。

共同原则仍然是：

~~~text
unify wait boundary
serialize state transitions
make cross-thread handoff explicit
~~~

## 机器人网络 Runtime 的直接结构

~~~text
NetworkThread
  |
  +-- epoll
  |    +-- telemetry socket
  |    +-- command socket
  |    +-- serial/event fd
  |    +-- wakeup eventfd
  |
  +-- timer heap
  |    +-- reconnect
  |    +-- heartbeat
  |    +-- watchdog
  |
  +-- owner-local connection state
~~~

其他线程只通过：

~~~text
MPSC command queue
+
eventfd wakeup
~~~

进入 NetworkThread。

这样可以避免：

~~~text
planner locks network object
logger locks same object
health thread closes fd
network thread concurrently epoll/read
~~~

这种难以推理的跨线程状态共享。

## Reactor 的核心不变量

~~~text
1. poller registration is owner-thread state
2. each fd maps to one event-sink object
3. callback may mutate registrations
4. fd removal is logical first, physical reclamation after batch
5. mailbox wakeup is another pollable event source
6. timers execute on the same owner thread
7. foreign-thread state changes enter through commands
8. long/blocking callbacks violate the latency model
~~~

这些不变量比“使用 epoll”本身更有迁移价值。
