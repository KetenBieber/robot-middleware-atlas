# Worker / epoll / Accept：nginx 怎样把多进程与单线程 Reactor 组合起来

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 的并发不是一个巨型 MPMC worker pool，而是：

~~~text
master
├─ worker process 0 → event loop
├─ worker process 1 → event loop
├─ worker process 2 → event loop
└─ worker process 3 → event loop
~~~

每个 worker 内大部分 connection state 由单线程 event loop 拥有。

## Worker Loop 为什么几乎不需要锁

`ngx_worker_process_cycle()` 每轮只调用：

~~~text
ngx_process_events_and_timers(cycle)
~~~

read/write handler、timer callback、posted event 大多都在当前 worker 的同一 execution context 中执行。

因此 connection-local mutable state 不需要每次访问都加 mutex。

这是一种 shared-nothing 倾向。

## `ngx_process_events_and_timers()` 怎样计算 Sleep

先：

~~~text
timer = ngx_event_find_timer()
~~~

再把 timer 交给 event backend：

~~~text
epoll_wait(..., timer)
~~~

Timer rbtree 与 I/O wait 共用同一阻塞点。

## epoll 返回后为什么不总是直接 Handler

如果 flags 包含 `NGX_POST_EVENTS`：

~~~text
readiness
→ posted_accept_events / posted_events
~~~

否则直接调用 handler。

readiness detection 与 execution scheduling 因而分层。

## Stale epoll Event 怎么识别

epoll data.ptr 中同时编码：

~~~text
connection pointer
+
1-bit instance generation
~~~

poll 返回：

~~~c
instance = (uintptr_t) c & 1;
c = (ngx_connection_t *) ((uintptr_t) c & ~1);

rev = c->read;

if (c->fd == -1 || rev->instance != instance) {
  continue;
}
~~~

这防止 connection slot 已被复用后，旧 fd generation 的 ready event误伤新连接。

## 为什么 EPOLLERR/HUP 要强制转成 IN/OUT

源码：

~~~c
if (revents & (EPOLLERR|EPOLLHUP)) {
  revents |= EPOLLIN|EPOLLOUT;
}
~~~

目标是至少让一个 active read/write handler 获得执行机会，进入统一的 connection error handling。

否则 error readiness 可能没有对应用户 handler 被调用。

## Accept 在多 Worker 下为什么会有竞争

多个 worker 共享 listening socket 时，同一个新连接可能让多个 worker 被唤醒。

历史上这会造成 thundering herd。

nginx 有几种策略：

- accept mutex；
- EPOLLEXCLUSIVE；
- SO_REUSEPORT per-worker listening socket。

它们解决的是同一个问题，但层级不同。

## Accept Mutex 做了什么

启用后，worker 尝试获取共享 mutex。

持锁 worker 才 enable accept events，并让本轮 epoll readiness 进入 posted accept queue。

处理完 accept posted events 后释放 mutex。

所以 mutex 保护的不是每条 connection state，而是：

> 哪个 worker 当前拥有 accept 权。

## 为什么拿不到 Mutex 时会限制 Poll Timeout

如果当前 worker 没拿到 accept mutex：

~~~text
timer = min(timer, accept_mutex_delay)
~~~

它不能永久睡眠，否则未来可能一直没有机会重新竞争 accept 权。

所以 accept scheduling 与 event-loop sleep deadline 被连接起来。

## SO_REUSEPORT 是另一种 Ownership 设计

`ngx_clone_listening()` 可以为每个 worker 创建自己的 listening entry。

概念：

~~~text
worker 0 owns listen socket 0
worker 1 owns listen socket 1
...
kernel distributes flows
~~~

这比共享一把 accept mutex 更接近 ownership partitioning。

用 topology 规避共享，比优化锁本身更彻底。

## Graceful Shutdown 为什么先关闭 Listening Socket

收到 graceful quit：

~~~text
set ngx_exiting
↓
set shutdown timer
↓
close listening sockets
↓
close idle connections
↓
continue processing active work/timers
↓
exit when non-cancelable timers gone
~~~

它先停止新输入，再 drain 已有连接。

这与线程池 close producer → drain queue → stop consumers 完全同构。

## Master/Worker 为什么适合网络 Server

优点：

- worker crash 隔离；
- 每 worker 局部 connection state；
- reload 可以由 master 协调新旧 worker 交替；
- CPU affinity 可以按 worker 绑定；
- 很少需要跨 worker 共享热点。

代价：

- 跨 worker 共享 cache/state 更复杂；
- 需要 shared memory/IPC；
- 一个连接固定在某个 worker 上。

## 与 libuv 的关键对照

| 维度 | libuv | nginx |
| --- | --- | --- |
| 定位 | embeddable async library | complete server runtime |
| concurrency | caller decides threads/loops | master + worker processes |
| event owner | uv_loop thread | worker process event loop |
| timer | min-heap | rbtree + lazy update |
| deferred event | pending/check/closing phases | posted event queues |
| blocking work | global thread pool | module-specific mechanisms/thread pools |
| accept ownership | application-dependent | mutex/exclusive/reuseport |

## 对机器人程序的迁移

当模块天然可以分片时，可以考虑：

~~~text
Process/Thread A owns device group A
Process/Thread B owns device group B
~~~

而不是：

~~~text
all workers
→ one global map
→ one mutex
~~~

nginx 的重要启发不是“网络服务器要多进程”，而是：

> 先用 ownership partition 降低共享，再讨论同步原语。
