# nginx 总览：Master/Worker、Event Loop 与预分配资源怎样组成高并发服务器 Runtime

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 的价值不只是 HTTP server。它展示了另一种非常成熟的程序组织方式：**少量 worker process，各自运行单线程事件循环；连接对象、事件对象和 request pool 尽量局部拥有；Timer、posted event、epoll readiness 和连接回收全部围绕 worker-local state 组织。**

## 先看最重要的进程边界

~~~text
master process
├─ parse/config/reload
├─ manage listening sockets
├─ spawn workers
└─ signal / graceful restart

worker process 0
  └─ event loop

worker process 1
  └─ event loop

...
~~~

这和 libuv 作为“可嵌入 library loop”不同。nginx 自己定义整个 server process topology。

## Worker 的主循环非常薄

~~~c
for (;;) {
  if (ngx_exiting && ngx_event_no_timers_left() == NGX_OK)
    ngx_worker_process_exit(cycle);

  ngx_process_events_and_timers(cycle);

  if (ngx_terminate)
    ngx_worker_process_exit(cycle);

  if (ngx_quit) {
    ngx_exiting = 1;
    ngx_close_listening_sockets(cycle);
    ngx_close_idle_connections(cycle);
    ngx_event_process_posted(cycle, &ngx_posted_events);
  }
}
~~~

真正的复杂度都在 event/timer/connection data structures 里，而不是在 worker loop 本身。

## 一轮 Worker Event Loop 的主线

~~~text
find nearest timer
↓
possibly try accept mutex
↓
move posted_next → posted
↓
epoll/kqueue/... wait
↓
process posted accept events
↓
release accept mutex
↓
expire timers
↓
process posted normal events
~~~

这是一套明确 phase order。

需要注意当前固定源码版本的实际默认：`accept_mutex` 配置默认是关闭的；Linux epoll + 多 worker 时会优先给共享 listening event 加 `EPOLLEXCLUSIVE`，而 `SO_REUSEPORT` 则进一步把 listening socket 克隆成 per-worker 资源。三种入口 ownership、accept admission、connection slot 分配和 epoll stale-event generation 的完整链见 [Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation](worker-epoll-accept.md)。

## 为什么 nginx 倾向单线程 Worker

一个 connection 的 read/write event、request state、buffer chain、timer 大多由同一 worker 修改。

于是大量状态不需要跨线程 mutex。

并发来自：

~~~text
many connections per worker
+
many worker processes
~~~

而不是一个进程内几十条 worker thread 共同修改同一 connection table。

## Timer 为什么不是最小堆

nginx 使用全局 worker-local rbtree。

libuv 用 min-heap；nginx 用 rbtree。

两者都能找到 nearest deadline，但 nginx Timer 还需要大量动态 delete/reinsert，并且 `ngx_event_t` 直接内嵌 rbtree node。

更有意思的是 nginx 还有 300 ms lazy update：新旧 deadline 差异很小时根本不重排 rbtree。

这说明容器选择还会和“允许多大时间近似”结合。

## Posted Event 为什么不是直接 Callback

底层 epoll 发现 event ready 后，可以：

~~~text
immediate handler()
or
post to intrusive queue
~~~

posted 模式让 nginx 先完成 accept mutex、timer 等 runtime bookkeeping，再统一执行 callback。

这和 libuv pending queue 属于同一类 deferred execution。

## Connection 为什么来自 Free List

nginx 不为每个新 socket 动态 malloc 一整套 connection/event object。

worker 初始化时已经有固定数量：

~~~text
connection_n
connections[]
read_events[]
write_events[]
~~~

`ngx_get_connection()` 从 free_connections 单链表取一项；close 后再归还。

这把最大连接数直接变成显式资源边界。

这里还有一个与固定对象池强相关的正确性问题：slot 地址会高频复用，而内核 epoll ready list 可能残留上一代 fd 的事件。nginx 每次重新分配 slot 都翻转 `ngx_event_t::instance`，并把这一位编码进 `epoll_event.data.ptr`；事件返回时先比较 generation 再执行 handler。

## Connection 紧张时为什么会主动回收 Reusable Connection

当 free connection 数太低时：

~~~text
reusable_connections_queue
↓
pick old reusable connection
↓
set c->close = 1
↓
invoke read handler
~~~

这不是 allocator trick，而是 admission/backpressure policy：优先牺牲可复用/空闲连接，为新连接腾运行时 slot。

## Memory Pool 为什么不是通用 malloc 替代品

`ngx_pool_t` 服务的是“同生命周期对象批量申请、整体销毁”。

小对象 bump allocation；大对象单独 malloc 并挂 large list；pool destroy 时统一 cleanup/free。

它非常适合 request/config 这种 phase lifetime，不适合需要任意独立 free 的长期对象。

共享内存区域则使用另一套 slab allocator。

## nginx 第一批应该带走的程序设计原则

1. 用 process/thread ownership 限制共享状态。
2. runtime 的资源容量最好显式有界。
3. event callback 可以 deferred，而不是所有 readiness 立即递归执行。
4. timer/data structure 可以利用业务允许的时间误差减少维护成本。
5. lifecycle 相同的短命对象适合 arena/pool，而不是逐对象 free。
6. stale OS event 必须通过 generation/instance 重新验证。
