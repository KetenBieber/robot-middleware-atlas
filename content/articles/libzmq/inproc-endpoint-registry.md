# inproc Endpoint Registry：同一 Context 内两个 Socket 怎样真正连起来

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

inproc:// 很容易被误解成“更快的 TCP”。实际上它根本没有 TCP、fd、listener、connecter、ZMTP greeting，也不需要 I/O thread 去驱动网络。

它做的是另一件事：

> 在同一个 ctx_t 内，把两个 socket_base_t 通过一对 pipe_t 直接接起来。

因此 inproc 的核心问题不是“怎样收发网络包”，而是：

- 谁保存名字到 socket 的映射；
- bind 先发生与 connect 先发生时怎样统一；
- 两边 Pipe 属于哪个线程；
- HWM 怎样根据两端配置合并；
- 为什么 endpoint lookup 必须参与 seqnum 生命周期屏障；
- Context 退出时为什么还要处理尚未匹配的 pending connection。

这套逻辑非常值得读，因为它把“服务发现 + 本地 IPC 建链 + 并发生命周期”压缩到了几百行代码里。

## 1. 最朴素的 inproc 实现为什么不够

假设自己写：

~~~cpp
std::unordered_map<std::string, Socket*> endpoints;

void bind(std::string name, Socket* s) {
    endpoints[name] = s;
}

void connect(std::string name, Socket* me) {
    Socket* peer = endpoints.at(name);
    me->peer = peer;
}
~~~

它立刻留下几个问题。

第一，connect 如果先于 bind：

~~~text
Thread A                 Thread B

connect("camera")
  |
  +-- endpoint missing

                         bind("camera")
~~~

是直接失败、阻塞等待，还是保存待连接状态？

第二，peer 是另一个线程拥有的 socket。当前线程不能直接修改 peer 的 Pipe registry，否则就破坏了 libzmq 一直坚持的 owner-thread state mutation。

第三，查到 peer 指针之后，在真正发送 bind command 前，peer 可能开始销毁。

所以“全局 map 查一个指针”只是第一步。

## 2. endpoint_t 为什么连 options 也一起复制

ctx.hpp 中：

~~~cpp
struct endpoint_t
{
    socket_base_t *socket;
    options_t options;
};
~~~

Context registry 保存的不是只有 socket 指针，而是：

~~~text
logical address
    |
    v
endpoint_t
    |- socket*
    |- options snapshot
~~~

注释直接说明原因：peer 可以读取对端配置，而不需要额外 synchronization / handshaking。

这点对 inproc 特别重要，因为它没有网络握手阶段。

TCP 可以在 ZMTP greeting/mechanism 过程中逐步交换连接信息；inproc 建链时两端都在同一个进程里，直接把 bind 时的 options snapshot 放进 registry 更简单。

## 3. 为什么 endpoints 用 map，而 pending connections 用 multimap

ctx_t 保存：

~~~cpp
typedef std::map<std::string, endpoint_t>
  endpoints_t;

endpoints_t _endpoints;

typedef std::multimap<
  std::string,
  pending_connection_t>
  pending_connections_t;

pending_connections_t _pending_connections;
~~~

这是一个很自然的数据结构选择。

一个 bind address 只能有一个注册者：

~~~text
inproc://camera
  -> exactly one bound endpoint
~~~

所以 endpoints 用 map；重复 bind 会变成 EADDRINUSE。

但一个还未 bind 的地址可能先收到多个 connect：

~~~text
client A --\
client B ----> inproc://camera   [not bound yet]
client C --/
~~~

所以 pending 需要允许同一个 key 对应多条记录，multimap 正好表达：

~~~text
address -> 0..N pending connections
~~~

## 4. bind(inproc) 只做 registry，不创建 listener

socket_base_t::bind：

~~~cpp
if (protocol == protocol_name::inproc) {
    const endpoint_t endpoint =
      {this, options};

    rc = register_endpoint (
      endpoint_uri_,
      endpoint);

    if (rc == 0) {
        connect_pending (
          endpoint_uri_,
          this);

        _last_endpoint.assign (
          endpoint_uri_);

        options.connected = true;
    }

    return rc;
}
~~~

和 TCP bind 对比：

~~~text
TCP bind
  -> choose I/O thread
  -> create listener
  -> create fd
  -> bind/listen kernel socket
  -> poller

inproc bind
  -> register logical endpoint
  -> attach any pending connectors
~~~

所以 inproc 是 Context-local connection fabric，不是 socket transport 的一种轻量包装。

## 5. register_endpoint() 为什么必须持 _endpoints_sync

ctx_t：

~~~cpp
int zmq::ctx_t::register_endpoint (
  const char *addr_,
  const endpoint_t &endpoint_)
{
    scoped_lock_t locker (
      _endpoints_sync);

    const bool inserted =
      _endpoints
        .ZMQ_MAP_INSERT_OR_EMPLACE (
          std::string (addr_),
          endpoint_)
        .second;

    if (!inserted) {
        errno = EADDRINUSE;
        return -1;
    }

    return 0;
}
~~~

这把：

~~~text
check whether address exists
+
insert endpoint
~~~

变成一个原子临界区。

否则两个应用线程同时 bind 同一个地址时，都可能先观察到“不存在”，然后各自插入，破坏 single binder 语义。

## 6. connect(inproc) 为什么一开始就创建 Pipe

connect_internal()：

~~~cpp
const endpoint_t peer =
  find_endpoint (endpoint_uri_);

const int sndhwm =
  peer.socket == NULL
    ? options.sndhwm
    : options.sndhwm != 0
        && peer.options.rcvhwm != 0
      ? options.sndhwm
          + peer.options.rcvhwm
      : 0;

const int rcvhwm =
  peer.socket == NULL
    ? options.rcvhwm
    : options.rcvhwm != 0
        && peer.options.sndhwm != 0
      ? options.rcvhwm
          + peer.options.sndhwm
      : 0;
~~~

随后马上：

~~~cpp
object_t *parents[2] = {
  this,
  peer.socket == NULL
    ? this
    : peer.socket
};

pipe_t *new_pipes[2] =
  {NULL, NULL};

rc = pipepair (
  parents,
  new_pipes,
  hwms,
  conflates);
~~~

即便 bind 还没出现，connector 也先创建自己的双向 Pipe pair。

原因是 application-side socket 已经需要一个本地 endpoint 来表达这条 logical connection；等 binder 出现时，再把 remote endpoint 的 ownership 转过去。

## 7. 为什么 inproc HWM 是两端 HWM 的组合

如果 peer 已经存在：

~~~text
connector sndhwm
+
binder rcvhwm
~~~

共同决定 connector -> binder 方向允许积压的总消息数。

源码：

~~~cpp
const int sndhwm =
  options.sndhwm != 0
    && peer.options.rcvhwm != 0
  ? options.sndhwm
      + peer.options.rcvhwm
  : 0;
~~~

反方向同理。

这和 TCP 情况不同。网络连接两边各自有用户态 queue、内核 socket buffer 和远端 runtime；inproc 的两个 endpoint 直接共享同一对 Pipe，因此容量语义必须从两端配置合成。

如果任一 HWM 为 0，ZeroMQ 语义里代表无限，因此总 HWM 也变成 0，而不是有限值相加。

## 8. bind 已存在时，find_endpoint() 为什么顺便 inc_seqnum

ctx_t::find_endpoint：

~~~cpp
endpoint_t endpoint =
  it->second;

endpoint.socket->inc_seqnum ();

return endpoint;
~~~

单看“查 map”很奇怪：为什么查一下地址还要增加对端 command sequence？

因为返回的是一个裸 socket 指针，而调用者稍后会异步发送 bind command：

~~~text
find peer socket
    |
    | time gap
    v
send_bind(peer, pipe)
~~~

如果 peer 在这段间隙完成 termination 并释放，send_bind 就会指向悬空对象。

inc_seqnum 的含义是：

> 我已经取得对这个对象的一个未来 command obligation；在对应 bind command 被处理之前，你不能完成销毁。

随后发送 bind 时设置 inc_seqnum=false，避免同一 obligation 计数两次。

这正好和 [Linger 与终止协议](linger-termination-protocol.md) 中的 sent/processed seqnum barrier 对上。

## 9. bind 已存在：远端状态仍由远端 owner 修改

peer 已存在时：

~~~cpp
send_bind (
  peer.socket,
  new_pipes[1],
  false);
~~~

当前 socket 只直接 attach 自己这一端：

~~~cpp
attach_pipe (
  new_pipes[0],
  false,
  true);
~~~

于是线程边界仍然是：

~~~text
connector owner thread
  |
  +-- attach local pipe endpoint directly
  |
  +-- send command ----------------------+
                                       |
                                       v
                              binder owner thread
                                       |
                                       +-- attach remote pipe endpoint
~~~

inproc 虽然没有网络线程，但不代表可以跨 application thread 直接修改另一个 socket 的状态。

## 10. connect 先于 bind：为什么要先发 routing id

当 peer.socket == NULL 时，当前还不知道未来 binder 的 recv_routing_id 配置。

源码采取保守策略：

~~~cpp
send_routing_id (
  new_pipes[0],
  options);

const endpoint_t endpoint =
  {this, options};

pend_connection (
  std::string (endpoint_uri_),
  endpoint,
  new_pipes);
~~~

先把 routing id 写进 Pipe；以后 binder 出现时，如果发现 binder 根本不需要 routing id，再把这条预写消息消费掉。

这是一种典型的：

~~~text
unknown future capability
  -> encode enough information now
  -> discard later if unnecessary
~~~

因为 connect 时还没有 binder options，无法提前精确分支。

## 11. pend_connection() 如何处理“bind 恰好在此刻发生”

pend_connection 仍持有 _endpoints_sync：

~~~cpp
const endpoints_t::iterator it =
  _endpoints.find (addr_);

if (it == _endpoints.end ()) {
    endpoint_.socket->inc_seqnum ();

    _pending_connections
      .ZMQ_MAP_INSERT_OR_EMPLACE (
        addr_,
        pending_connection);
}
else {
    connect_inproc_sockets (
      it->second.socket,
      it->second.options,
      pending_connection,
      connect_side);
}
~~~

这段代码解决典型 TOCTOU：

~~~text
T0  connector find_endpoint -> missing
T1  binder registers endpoint
T2  connector stores pending
~~~

如果 find 和 pending insert 之间没有再次在同一 registry lock 下检查，binder 可能已经调用完 connect_pending，而 connector 之后才把自己挂到 pending 表，结果永远没人来处理。

所以 pend_connection 再查一次 endpoint：

~~~text
still absent
  -> store pending

appeared meanwhile
  -> connect immediately
~~~

## 12. pending_connection_t 为什么同时保存两个 Pipe endpoint

结构：

~~~cpp
struct pending_connection_t
{
    endpoint_t endpoint;
    pipe_t *connect_pipe;
    pipe_t *bind_pipe;
};
~~~

因为 connect 先发生时，Pipe pair 已经创建了：

~~~text
connect_pipe
  owner eventually = connector

bind_pipe
  owner eventually = binder
~~~

但 binder 线程 ID 尚未知。

等 binder 出现，connect_inproc_sockets() 再执行：

~~~cpp
pending_connection_.bind_pipe
  ->set_tid (
    bind_socket_->get_tid ());
~~~

也就是把尚未落到最终 owner 的 Pipe endpoint 完成归属迁移。

## 13. connect_pending() 为什么一次处理同名的所有连接

binder 注册成功以后：

~~~cpp
const auto pending =
  _pending_connections
    .equal_range (addr_);

for (auto p = pending.first;
     p != pending.second;
     ++p)
    connect_inproc_sockets (
      bind_socket_,
      _endpoints[addr_].options,
      p->second,
      bind_side);

_pending_connections.erase (
  pending.first,
  pending.second);
~~~

这正对应 multimap 语义：

~~~text
one binder
  |
  +-- pending connector A
  +-- pending connector B
  +-- pending connector C
~~~

bind 是一个“一次满足该 address 下全部等待者”的事件。

## 14. connect_inproc_sockets() 为什么重新计算 HWM

connect 先发生时，当时不知道 binder options，只能按 connector 自己的 HWM 初始化。

binder 真正出现以后：

~~~cpp
connect_pipe->set_hwms_boost (
  bind_options_.sndhwm,
  bind_options_.rcvhwm);

bind_pipe->set_hwms_boost (
  connector_options.sndhwm,
  connector_options.rcvhwm);

connect_pipe->set_hwms (...);
bind_pipe->set_hwms (...);
~~~

也就是在 capability 已知后补全真正双端容量语义。

这说明 pending Pipe 不是“建好后完全不动”，而是一个 provisional connection state。

## 15. bind_side 可以直接 process_command 的原因

如果当前调用来自 binder 自己的 bind()：

~~~cpp
if (side_ == bind_side) {
    command_t cmd;
    cmd.type = command_t::bind;
    cmd.args.bind.pipe =
      pending_connection_.bind_pipe;

    bind_socket_->process_command (cmd);
}
~~~

此时已经处于 bind socket 自己的 application thread，上下文正确，所以可以直接处理 bind command。

如果来自 connect_side，binder 属于另一个线程：

~~~cpp
pending_connection_.connect_pipe
  ->send_bind (
    bind_socket_,
    pending_connection_.bind_pipe,
    false);
~~~

同一个状态迁移，根据当前 execution owner 选择：

~~~text
same-thread
  -> direct process_command

cross-thread
  -> mailbox command
~~~

这是 owner-thread architecture 很典型的优化。

## 16. unregister_endpoints() 为什么必须早于 Pipe teardown

socket_base_t::process_term：

~~~cpp
unregister_endpoints (this);

for (...) {
    _pipes[i]
      ->send_disconnect_msg ();

    _pipes[i]
      ->terminate (false);
}
~~~

先把 Context registry 中所有指向当前 socket 的 logical names 移除，再开始 Pipe teardown。

否则可能出现：

~~~text
socket A starts termination

thread B:
  find_endpoint("inproc://x")
  -> still returns A
  -> creates new pipe toward dying socket
~~~

因此 shutdown 的一个基本不变量是：

> 一旦进入 closing，就先关闭所有“发现我并创建新关系”的入口。

## 17. Context terminate 为什么主动补接 pending inproc

这是最反直觉的一段：

~~~cpp
pending_connections_t copy =
  _pending_connections;

for (auto p = copy.begin ();
     p != copy.end ();
     ++p) {

    socket_base_t *s =
      create_socket (ZMQ_PAIR);

    zmq_assert (s);

    s->bind (
      p->first.c_str ());

    s->close ();
}
~~~

注释直接说：

~~~text
Connect up any pending inproc connections,
otherwise we will hang.
~~~

pending connector 在等待一个完整 bind / termination 协议闭环，它的 Pipe 和 seqnum obligation 已经存在。

如果 Context 直接进入销毁，而这些 pending connection 永远没有 binder 出现，相关 Pipe/command 生命周期就可能无法退场。

所以 terminate 临时创建 PAIR socket 去 bind 每个 pending address：

~~~text
pending logical connection
   |
   v
temporary binder appears
   |
   v
normal inproc attach/term path completes
   |
   v
temporary socket closes
~~~

这不是为了传业务消息，而是为了让已有生命周期债务进入正常状态机并被清算。

## 18. inproc 的本质不是共享内存，而是共享 Runtime

inproc 并没有建立：

~~~text
POSIX shm
mmap
shared page
cross-process zero-copy
~~~

它只能在同一个 Context、同一进程内使用。

真正共享的是：

~~~text
ctx_t registry
pipe_t objects
message ownership rules
application-thread command fabric
~~~

所以把 inproc 称作“共享内存 transport”并不准确。

更准确的理解是：

> 它绕开 kernel network transport，直接把两个 socket pattern 接到同一套 Pipe Runtime 上。

## 19. 对机器人软件架构的启发

同一进程内如果有：

~~~text
camera frontend
perception worker
local planner
telemetry adapter
~~~

可以选择：

~~~text
direct C++ function call
shared queue
inproc message fabric
~~~

inproc 的价值不只是“快”，而是仍然保留 ZeroMQ 的：

~~~text
socket pattern
multipart
routing
HWM/backpressure
monitoring
统一关闭协议
~~~

代价是它依然有消息 envelope、Pipe 与调度语义，不会像普通函数调用那样零抽象成本。

因此选择时应先问：

~~~text
是否需要组件解耦与消息语义？
    yes -> inproc 有价值

只是同线程函数组合？
    -> 不必为了“中间件一致性”强行套 socket
~~~

接下来可读 [Context 与 Reaper](context-reaper-lifecycle.md)，看这些 application socket 最终如何从原线程移交给专用 Reaper 并让整个 Context 安全退出；如果关注运行时可观测性，则进入 [Monitor Event](monitor-event-observability.md)。
