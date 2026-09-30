# TCP Connect 与重连：非阻塞连接怎样变成可恢复状态机

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

很多网络程序把 connect 当成初始化阶段的一次函数调用。但长期运行的机器人 Runtime 不能假设远端一定已经启动、网络永不闪断、服务永不重启，也不能让一个 I/O worker 因为某一条连接建立过程而阻塞几秒。

libzmq 把“连接”拆成一组事件：

~~~text
start
  -> nonblocking connect
  -> writable event / timeout
  -> SO_ERROR confirm
  -> tune fd
  -> create Engine

failure
  -> close
  -> reconnect timer
  -> retry
~~~

## 1. 为什么阻塞 connect 会破坏 Reactor

假设一个 I/O thread 管很多连接：

~~~text
I/O thread
  |
  +-- connection A
  +-- connection B
  +-- connection C
  +-- heartbeat timers
  +-- mailbox
~~~

如果 A 在 connect() 中阻塞 3 秒，同一个 owner 上其他 fd readiness、timer 和跨线程 command 都无法及时处理。

所以 tcp_connecter_t 的 open() 先创建 fd，再将其改为 non-blocking，然后才调用系统 connect。

## 2. EINPROGRESS 不是连接失败

源码：

~~~cpp
_s = tcp_open_socket (
  _addr->address.c_str (),
  options,
  false,
  true,
  _addr->resolved.tcp_addr);

if (_s == retired_fd) {
    LIBZMQ_DELETE (
      _addr->resolved.tcp_addr);
    return -1;
}

unblock_socket (_s);

int rc = ::connect (
  _s,
  tcp_addr->addr (),
  tcp_addr->addrlen ());

if (rc == 0)
    return 0;
~~~

在非阻塞 fd 上，connect 经常无法立刻完成。Unix 上如果被 EINTR 打断也被转为异步连接语义；Windows 对应错误则统一转换。

最终调用者看到：

~~~text
rc == 0
  -> immediately connected

rc == -1 && errno == EINPROGRESS
  -> connection establishment is in progress

other error
  -> this attempt failed
~~~

“还没完成”和“已经失败”必须是两个状态。

## 3. start_connecting() 把结果映射成事件

核心逻辑：

~~~cpp
void zmq::tcp_connecter_t::
start_connecting ()
{
    const int rc = open ();

    if (rc == 0) {
        _handle = add_fd (_s);
        out_event ();
    }

    else if (rc == -1
             && errno == EINPROGRESS) {
        _handle = add_fd (_s);
        set_pollout (_handle);

        _socket->event_connect_delayed (
          make_unconnected_connect_endpoint_pair (
            _endpoint),
          zmq_errno ());

        add_connect_timer ();
    }

    else {
        if (_s != retired_fd)
            close ();

        add_reconnect_timer ();
    }
}
~~~

状态图：

~~~text
open()
  |
  +-- connected immediately
  |      |
  |      v
  |   out_event()
  |
  +-- EINPROGRESS
  |      |
  |      +-- register fd
  |      +-- poll POLLOUT
  |      +-- optional connect timeout
  |
  +-- immediate error
         |
         +-- close
         +-- reconnect timer
~~~

整个过程没有 sleep，也没有阻塞等待。

## 4. 为什么 POLLOUT 还不能直接等于“连接成功”

非阻塞 connect 完成后，poller 往往通过 writable event 唤醒对象。但 writable 既可能表示成功，也可能表示异步错误已经可读取。

所以 out_event() 里调用的 connect() 并不是再次执行系统 ::connect，而是读取 socket error：

~~~cpp
const int rc = getsockopt (
  _s,
  SOL_SOCKET,
  SO_ERROR,
  reinterpret_cast<char *> (&err),
  &len);
~~~

如果 err 非零，就返回 retired_fd；只有 SO_ERROR 为 0，才真正把 fd 交出去。

这也是阅读网络源码时很重要的一条规则：**函数名必须放回状态机上下文解释。** tcp_connecter_t::connect() 的语义是“确认异步 connect 的最终结果”。

## 5. out_event()：成功进入 Engine，失败回到重连

完整骨架：

~~~cpp
void zmq::tcp_connecter_t::out_event ()
{
    if (_connect_timer_started) {
        cancel_timer (connect_timer_id);
        _connect_timer_started = false;
    }

    rm_handle ();

    const fd_t fd = connect ();

    if (fd == retired_fd
        && ((options.reconnect_stop
             & ZMQ_RECONNECT_STOP_CONN_REFUSED)
            && errno == ECONNREFUSED)) {

        send_conn_failed (_session);
        close ();
        terminate ();
        return;
    }

    if (fd == retired_fd
        || !tune_socket (fd)) {
        close ();
        add_reconnect_timer ();
        return;
    }

    create_engine (
      fd,
      get_socket_name<tcp_address_t> (
        fd,
        socket_end_local));
}
~~~

这里有三条结果：

~~~text
configured stop-on-connection-refused
  -> tell Session
  -> terminate Connecter

ordinary failure
  -> close current attempt
  -> reconnect timer

success
  -> tune socket
  -> create Engine
~~~

## 6. poller handle 为什么先移除

out_event() 一开始就 rm_handle()。

因为此时“正在建立连接”这个 fd 的 poller 注册已经完成使命。成功后，fd 会转移到新的 Engine，由 Engine 重新以正常网络连接的身份加入 I/O runtime；失败后则关闭并等待下一轮。

如果继续保留 Connecter 自己的 poller handle，就会出现两个对象同时声称对同一个 fd readiness 负责。

## 7. create_engine() 是一次 ownership handoff

公共基类的成功路径：

~~~cpp
void zmq::stream_connecter_base_t::
create_engine (
  fd_t fd_,
  const std::string &local_address_)
{
    const endpoint_uri_pair_t endpoint_pair (
      local_address_,
      _endpoint,
      endpoint_type_connect);

    i_engine *engine;

    if (options.raw_socket)
        engine =
          new (std::nothrow)
            raw_engine_t (
              fd_,
              options,
              endpoint_pair);
    else
        engine =
          new (std::nothrow)
            zmtp_engine_t (
              fd_,
              options,
              endpoint_pair);

    alloc_assert (engine);

    send_attach (_session, engine);

    terminate ();

    _socket->event_connected (
      endpoint_pair,
      fd_);
}
~~~

对象职责迁移：

~~~text
before success

Connecter
  owns:
    connecting socket
    connect timer
    POLLOUT wait
    retry policy

after success

Engine
  owns:
    established fd
    protocol handshake
    encoder / decoder
    read/write readiness

Connecter
  terminates
~~~

这是典型的“阶段对象”设计：连接尝试对象只活在连接建立阶段。

## 8. connect timeout 与 reconnect interval 不是同一个概念

connect timeout 回答：

> 单次异步 connect 最多等多久？

~~~cpp
void zmq::tcp_connecter_t::
add_connect_timer ()
{
    if (options.connect_timeout > 0) {
        add_timer (
          options.connect_timeout,
          connect_timer_id);

        _connect_timer_started = true;
    }
}
~~~

超时：

~~~cpp
if (id_ == connect_timer_id) {
    _connect_timer_started = false;
    rm_handle ();
    close ();
    add_reconnect_timer ();
}
~~~

而 reconnect interval 回答：

> 本轮失败以后，多久再发起下一轮 connect？

两个参数必须独立，否则就无法同时做到：

~~~text
single attempt:
  do not hang too long

repeated failure:
  do not hammer peer continuously
~~~

## 9. reconnect interval 怎样计算

stream_connecter_base_t 保存 _current_reconnect_ivl。

配置 reconnect_ivl_max 时：

~~~cpp
if (options.reconnect_ivl_max > 0) {
    int candidate_interval = 0;

    if (_current_reconnect_ivl == -1)
        candidate_interval =
          options.reconnect_ivl;
    else if (_current_reconnect_ivl
             > std::numeric_limits<int>::
                 max () / 2)
        candidate_interval =
          std::numeric_limits<int>::max ();
    else
        candidate_interval =
          _current_reconnect_ivl * 2;

    if (candidate_interval
        > options.reconnect_ivl_max)
        _current_reconnect_ivl =
          options.reconnect_ivl_max;
    else
        _current_reconnect_ivl =
          candidate_interval;

    return _current_reconnect_ivl;
}
~~~

也就是指数增长并封顶。

没有 reconnect_ivl_max 时：

~~~cpp
if (_current_reconnect_ivl == -1)
    _current_reconnect_ivl =
      options.reconnect_ivl;

const int random_jitter =
  generate_random ()
  % options.reconnect_ivl;

const int interval =
  _current_reconnect_ivl
      < std::numeric_limits<int>::max ()
          - random_jitter
    ? _current_reconnect_ivl
        + random_jitter
    : std::numeric_limits<int>::max ();

return interval;
~~~

也就是 base interval 加随机 jitter。

## 10. 为什么需要 jitter

假设 100 台机器人同时依赖一个服务。服务重启后，如果所有客户端都严格每 100 ms 重试：

~~~text
t=100 ms    100 connects
t=200 ms    100 connects
t=300 ms    100 connects
~~~

失败本身会把客户端“同步”起来，形成 thundering herd。

随机 jitter 把重试时刻打散；指数退避则在长时间故障时进一步减少连接压力。

## 11. reconnect timer 仍由原 Reactor 驱动

添加重连 timer：

~~~cpp
void zmq::stream_connecter_base_t::
add_reconnect_timer ()
{
    if (options.reconnect_ivl > 0) {
        const int interval =
          get_new_reconnect_ivl ();

        add_timer (
          interval,
          reconnect_timer_id);

        _socket->event_connect_retried (
          make_unconnected_connect_endpoint_pair (
            _endpoint),
          interval);

        _reconnect_timer_started = true;
    }
}
~~~

到期后：

~~~cpp
void zmq::stream_connecter_base_t::
timer_event (int id_)
{
    zmq_assert (
      id_ == reconnect_timer_id);

    _reconnect_timer_started = false;
    start_connecting ();
}
~~~

自动重连不是另开一个 while + sleep 线程，而是：

~~~text
poller timer
   |
   v
same I/O owner
   |
   v
start_connecting()
~~~

线程 ownership 因此仍然清晰。

## 12. Session 是长期 endpoint，Connecter/Engine 是阶段对象

连接断开后，Session 的 reconnect 路径会 reset 当前 transport 状态，然后：

~~~cpp
if (options.reconnect_ivl > 0)
    start_connecting (true);
else {
    std::string *ep =
      new (std::string);

    _addr->to_string (*ep);
    send_term_endpoint (
      _socket,
      ep);
}
~~~

start_connecting(true) 再根据 transport 创建新的 Connecter。

整体关系：

~~~text
Session
  |
  | create
  v
Connecter
  |
  | connection succeeds
  v
Engine
  |
  | attach
  v
Session
  |
  | connection error
  +--------------------+
                       |
                       v
                 new Connecter
~~~

Session 表示“这个逻辑 endpoint 还存在”；Connecter 表示“正在尝试建立一次物理连接”；Engine 表示“某一轮已经建立的连接”。

这比一个巨大 Connection 类同时保存 Connecting / Established / Reconnecting 的所有资源更容易界定 ownership。

## 13. delayed_start 把首次连接和重连区分开

基类 process_plug：

~~~cpp
void zmq::stream_connecter_base_t::
process_plug ()
{
    if (_delayed_start)
        add_reconnect_timer ();
    else
        start_connecting ();
}
~~~

首次连接可以立即尝试；重连则可以先等待 interval，避免错误发生后立刻形成 tight retry loop。

## 14. 终止时必须取消未来事件

Connecter 关闭：

~~~cpp
void zmq::stream_connecter_base_t::
process_term (int linger_)
{
    if (_reconnect_timer_started) {
        cancel_timer (
          reconnect_timer_id);
        _reconnect_timer_started =
          false;
    }

    if (_handle)
        rm_handle ();

    if (_s != retired_fd)
        close ();

    own_t::process_term (linger_);
}
~~~

TCP 子类还会先取消 connect-timeout timer。

顺序很关键：

~~~text
cancel timers
remove poller handle
close fd
then continue object termination
~~~

否则可能出现：

~~~text
object freed
   |
old timer fires
   |
timer_event(this)
   |
use-after-free
~~~

所以安全 shutdown 的第一步往往不是 free，而是先撤销所有未来还能把控制流带回对象的事件源。

## 15. 对机器人系统真正意味着什么

ZeroMQ 的自动重连解决的是 transport availability，不会自动解决控制系统的 stale-data 问题。

例如远程视觉服务断线：

~~~text
network reconnecting
       |
       +-- local control loop may continue
       +-- observation age keeps increasing
       +-- fallback policy may activate
~~~

上层仍需定义：

- 旧观测是否还可用于控制；
- 重连后是否要用 generation/epoch 隔离旧命令；
- 指令是否允许重放；
- actuator command 的最大允许数据年龄；
- 断线多久进入 degraded / fail-safe mode。

因此正确理解应是：

~~~text
transport reconnect
  !=
control recovery
~~~

物理 TCP 连上以后还必须经过 [ZMTP 握手](zmtp-handshake-mechanism.md) 才进入正常消息面；最终关闭则进入 [Linger 与终止协议](linger-termination-protocol.md)。
