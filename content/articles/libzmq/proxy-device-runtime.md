# Proxy / Device：为什么“消息转发器”也必须理解 Backpressure

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

ZeroMQ 的 socket pattern 解决的是单个 socket 怎样路由消息，但工程里经常还需要一个更高层组件：

~~~text
clients
   |
   v
frontend socket
   |
   v
proxy
   |
   v
backend socket
   |
   v
workers
~~~

它看起来像最简单的：

~~~cpp
while (true) {
    recv(frontend, msg);
    send(backend, msg);
}
~~~

但真正实现马上遇到四类问题：

- multipart message 不能被拆坏；
- backend 满时不能继续无脑 recv frontend；
- request 与 reply 两个方向都可能同时有流量；
- capture、pause/resume、terminate、statistics 不能破坏主数据面。

libzmq 的 proxy.cpp 很适合用来理解：**一个“业务层转发循环”怎样复用底层 socket 的 backpressure，却仍然需要自己设计 poller 状态机。**

在 XSUB/XPUB 组合中，Proxy 还会承接一条反向控制链：业务数据向订阅者方向流，而 subscribe/cancel interest 反向传播。为什么 XSUB 必须保存可重放的本地 Trie、XPUB 为什么维护 `prefix → Pipe set`，以及 forwarding device 为什么不能简单吞掉 duplicate subscriptions，见 [PUB / SUB：订阅 Trie、反向控制面与 Distributor](pubsub-trie-distributor.md)。

## 1. Proxy 不是新的 Transport

Proxy 没有自己实现：

~~~text
TCP framing
ZMTP greeting
Pipe
routing trie
network I/O thread
~~~

这些已经由 frontend/backend socket 各自承担。

Proxy 只是站在 socket API 之上：

~~~text
recv from socket A
send to socket B

recv from socket B
send to socket A
~~~

所以它更像 Runtime 内置的“应用级消息泵”。

这说明中间件里的高级能力不一定继续往底层增加新 transport；很多能力可以组合已有稳定抽象。

## 2. forward() 为什么一次转完整 Multipart Message

核心函数：

~~~cpp
while (true) {
    int rc =
      from_->recv (
        msg_,
        ZMQ_DONTWAIT);

    ...

    rc = from_->getsockopt (
      ZMQ_RCVMORE,
      &more,
      &moresz);

    ...

    rc = to_->send (
      msg_,
      more
        ? ZMQ_SNDMORE
        : 0);

    if (more == 0)
        break;
}
~~~

假设一条逻辑消息是：

~~~text
frame A [more]
frame B [more]
frame C [last]
~~~

Proxy 不能：

~~~text
recv A
switch direction
recv reply X
send A
...
~~~

否则 multipart 边界和上层 socket pattern 语义会被打乱。

所以最内层 while 保证：

> 一旦开始转发一条 multipart message，就把它所有 frame 完整转完。

## 3. 为什么 forward 使用 Non-blocking Recv

源码：

~~~cpp
int rc =
  from_->recv (
    msg_,
    ZMQ_DONTWAIT);
~~~

外层 poller 已经决定“此时存在可推进工作”。

如果 forward 内部再做阻塞 recv：

~~~text
poller says input ready
  |
  v
recv one message
  |
  v
block waiting for another independent message
~~~

整个 proxy loop 会被单一方向卡住。

因此职责分工是：

~~~text
poller
  -> decide when progress is possible

forward
  -> drain currently available bounded work
~~~

## 4. proxy_burst_size 为什么是一个显式预算

config.hpp：

~~~cpp
proxy_burst_size = 1000
~~~

forward：

~~~cpp
for (unsigned int i = 0;
     i < proxy_burst_size;
     i++) {
    ...
}
~~~

如果每轮只转一条：

~~~text
poll
recv one
send one
poll
recv one
send one
~~~

poller 往返成本会增大。

如果一旦可读就无限 drain：

~~~text
frontend continuously busy
  |
  v
loop never returns
  |
  v
reply/control direction starves
~~~

所以 burst size 是一个公平性预算：

~~~text
larger burst
  -> fewer poll transitions
  -> better throughput
  -> potentially worse fairness

smaller burst
  -> more scheduler opportunities
  -> more event-loop overhead
~~~

这和 NAPI budget、event loop batch limit、scheduler quantum 是同一类机制。

## 5. 为什么 EAGAIN 要结合 i 判断

源码：

~~~cpp
if (rc < 0) {
    if (likely (
          errno == EAGAIN
          && i > 0))
        return 0;

    return -1;
}
~~~

如果 i > 0，说明本轮已经成功转发过消息，随后当前 queue 被 drain 空：

~~~text
normal end of burst
~~~

如果 i == 0，则 caller 刚根据 readiness 进入 forward，却立即没有可读数据，需要向上层暴露这个状态变化。

因此 EAGAIN 不是脱离上下文就能解释的“固定错误”。

## 6. Capture 为什么使用 msg_t::copy

capture()：

~~~cpp
zmq::msg_t ctrl;

int rc = ctrl.init ();

rc = ctrl.copy (*msg_);

rc = capture_->send (
  &ctrl,
  more_
    ? ZMQ_SNDMORE
    : 0);
~~~

随后原消息继续送往 destination：

~~~cpp
to_->send (
  msg_,
  more
    ? ZMQ_SNDMORE
    : 0);
~~~

结构：

~~~text
                   +--> capture socket
                   |
from -> msg -------+
                   |
                   +--> destination
~~~

前面的 [msg_t 存储与引用计数](msg-storage-refcount.md) 已经证明，大消息 copy 可以通过引用计数共享 payload，而不是必然复制整份大 buffer。

所以 capture 可以做 traffic recorder / debug feed，但仍然会增加：

~~~text
msg representation
reference ownership
queue pressure
send work
~~~

## 7. Capture 为什么必须复制 SNDMORE

源码：

~~~cpp
capture_->send (
  &ctrl,
  more_
    ? ZMQ_SNDMORE
    : 0);
~~~

原消息：

~~~text
[route][header][payload]
       one multipart message
~~~

如果 capture 丢掉 more 标志：

~~~text
[route]
[header]
[payload]
       three messages
~~~

观察语义就被破坏。

所以“复制消息”不仅是复制 bytes，还包括复制 frame boundary。

## 8. Statistics 里的 count 是什么单位

forward 每收到一个 frame：

~~~cpp
size_t nbytes =
  msg_->size ();

recving.count += 1;
recving.bytes += nbytes;

...

sending.count += 1;
sending.bytes += nbytes;
~~~

因此 count 是 message part / frame 数，不一定等于完整 multipart business message 数。

工程指标必须明确单位：

~~~text
packet
frame
message
request
sample
~~~

否则吞吐分析会得到完全不同的结论。

## 9. 为什么永久 Poll POLLOUT 会烧 CPU

多数 socket 在没有 backpressure 时长期 writable。

如果一直：

~~~text
poll:
  frontend POLLIN | POLLOUT
  backend  POLLIN | POLLOUT
~~~

即便没有输入，POLLOUT 也可能持续立即返回：

~~~text
poll -> writable
poll -> writable
poll -> writable
~~~

fallback 实现的注释明确指出，把 POLLIN/POLLOUT 一起用于阻塞 poll 会因为 POLLOUT 大多数时间立即 ready 而拉高 CPU。

因此一个非常通用的 Reactor 原则是：

> ZMQ_POLLOUT 不是应该永远订阅的“状态更新”；只有曾经因为不可写而阻塞时，等待重新可写才真正有意义。

## 10. 新 Poller 版本为什么建立多套 Wait Set

源码建立：

~~~text
poller_all
poller_in
poller_receive_blocked
poller_send_blocked
poller_both_blocked
poller_frontend_only
poller_backend_only
~~~

这不是为了展示 API，而是在预编码不同 backpressure 状态下“下一次真正值得阻塞等待什么”。

正常：

~~~text
wait for input
~~~

如果 frontend 有 input，但 backend 不可写：

~~~text
suppress frontend input as blocking wake source
wait for backend output capacity
keep useful control/opposite events live
~~~

如果两个方向都被堵：

~~~text
wait for whichever output side can make progress
~~~

它把 backpressure 变成 wait-set selection。

## 11. 为什么底层有 HWM，Proxy 仍要处理 Backpressure

底层 HWM 最终会表现为：

~~~text
destination socket cannot currently accept more
~~~

如果 Proxy 仍不断从 source recv：

~~~text
source queue drains
   |
   v
proxy must buffer messages somewhere
   |
   v
intermediate memory grows
~~~

Proxy 没有建立无限中间队列，而是：

~~~text
destination not writable
  |
  v
stop treating source input as useful wakeup
  |
  v
wait for output capacity
~~~

这就是更高一层的 backpressure propagation。

## 12. 两个方向为什么分别维护 Readiness

主循环维护：

~~~cpp
frontend_in
frontend_out
backend_in
backend_out
~~~

request 方向需要：

~~~text
frontend input
AND
backend output capacity
~~~

reply 方向需要：

~~~text
backend input
AND
frontend output capacity
~~~

两个方向可以独立堵塞，所以不能用一个全局 writable flag。

## 13. frontend == backend 为什么必须特殊处理

代码明确检查：

~~~cpp
frontend_equal_to_backend =
  frontend_ == backend_;
~~~

API 形式上有两个参数，并不代表对象一定不同。

如果把同一个 socket 当两个独立 event source：

~~~text
same readiness
  -> interpreted twice
  -> duplicate processing risk
~~~

因此实现减少对应 poller，并保证同一 socket 的 event 只沿一个逻辑分支解释。

这就是 aliasing-aware API implementation。

## 14. Control Socket 为什么仍然使用普通消息

steerable proxy 支持：

~~~text
PAUSE
RESUME
TERMINATE
STATISTICS
~~~

handle_control 只是：

~~~cpp
rc = control_->recv (
  &cmsg,
  ZMQ_DONTWAIT);
~~~

然后根据 payload 改状态：

~~~cpp
if (PAUSE)
    state = paused;

else if (RESUME)
    state = active;

else if (TERMINATE)
    state = terminated;
~~~

控制面继续走 socket message，而不是共享原子变量。

优点：

~~~text
跨线程安全
可跨进程
可录制/测试
与 poller 统一
~~~

代价：

~~~text
control command obeys message scheduling
not a hard-real-time interrupt
~~~

## 15. PAUSE 停止的只是 Forwarding

主循环只有：

~~~cpp
if (state == active) {
    forward(...);
}
~~~

才转业务消息。

PAUSE 并不：

~~~text
close frontend
close backend
destroy proxy
disconnect peers
~~~

而是：

~~~text
connections remain
queues remain
control remains responsive
forwarding suspended
~~~

所以 paused 是调度状态，不是生命周期终止状态。

## 16. STATISTICS 为什么返回 8 个 Frame

数据结构：

~~~cpp
struct stats_socket
{
    uint64_t count;
    uint64_t bytes;
};

struct stats_endpoint
{
    stats_socket send;
    stats_socket recv;
};

struct stats_proxy
{
    stats_endpoint frontend;
    stats_endpoint backend;
};
~~~

维度：

~~~text
(frontend, backend)
x
(recv, send)
x
(count, bytes)
~~~

所以 2 × 2 × 2 = 8 个 uint64：

~~~text
frontend recv count
frontend recv bytes
frontend send count
frontend send bytes
backend recv count
backend recv bytes
backend send count
backend send bytes
~~~

每个值作为一个 multipart frame 返回。

## 17. REP Control 为什么必须回一个空 Reply

源码检查 control socket type：

~~~cpp
if (type == ZMQ_REP) {
    cmsg.init_size (0);

    rc = control_->send (
      &cmsg,
      0);
}
~~~

因为 REP 自身有 request/reply 状态机：

~~~text
recv
  -> send
  -> next recv
~~~

即便 PAUSE/RESUME 没有返回值，也必须完成 REP duty。

这说明 socket pattern 的协议约束不会因为它被拿来做“内部控制通道”就消失。

## 18. Proxy 为什么不适合放在实时控制主线程

Proxy 一次 loop 可能执行：

~~~text
poll
up to 1000-message burst
multipart inner loop
capture copy
stats accounting
control handling
~~~

执行时间依赖 traffic，不具备固定 WCET。

因此机器人系统里更合理的是：

~~~text
1 kHz control thread
  -> bounded latest state / command

network proxy thread
  -> forwarding / routing / telemetry
~~~

高性能不等于硬实时。

## 19. Proxy 和持久 Broker 的边界

libzmq Proxy 没有：

~~~text
durable log
persistent offsets
consumer group
disk queue
transaction
cluster consensus
~~~

它是 transient forwarding device。

适合：

~~~text
worker broker
fan-in/fan-out bridge
topology adapter
traffic capture point
~~~

不应和 Kafka/RabbitMQ 一类持久 broker 的语义混为一谈。

## 20. 可以迁移出的通用算法

抽象：

~~~text
while not terminated:
    wait for:
      useful input
      blocked output becoming writable
      control

    process control

    if active:
      for each direction:
        if input && output_capacity:
          forward bounded burst

        if input && !output_capacity:
          stop using that input
          as a blocking wake source
~~~

核心不变量：

~~~text
preserve multipart boundary
no unbounded intermediate queue
POLLOUT matters after blocking
control stays live while paused
bounded burst preserves fairness
~~~

前面的 [Pipe 与 HWM](pipe-hwm-backpressure.md) 解释单连接内部怎样产生 backpressure；这一篇说明上层 forwarding loop 怎样继续传播这个压力。下一篇 [Context 与 Reaper](context-reaper-lifecycle.md) 收束整个 Context 的 socket slot、reaping 与最终退出。
