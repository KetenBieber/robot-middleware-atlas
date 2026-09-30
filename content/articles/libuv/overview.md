# libuv 总览：一个 Event Loop 怎样组织 Socket、Timer、ThreadPool 与跨线程唤醒

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

libuv 最值得研究的不是“跨平台异步 I/O”这个标签，而是：**一个单线程事件循环怎样同时管理长期资源、一次性异步操作、定时任务、跨线程通知和阻塞工作。**

## 从阻塞服务器开始

~~~c
for (;;) {
  int client = accept(server_fd, ...);
  ssize_t n = read(client, buf, sizeof(buf));
  process(buf, n);
  write(client, response, len);
  close(client);
}
~~~

一个 client 阻塞，整条线程就停住。另一种方案是一连接一线程，但连接数增长后，thread stack、context switch、调度和共享状态都会一起增长。

libuv 把问题拆成两类 execution domain：

~~~text
non-blocking socket / timer / signal
        ↓
     uv_loop_t
        ↓
 epoll / kqueue / IOCP

blocking file/DNS/custom work
        ↓
 global worker thread pool
        ↓
 completion queue
        ↓
 uv_async_send()
        ↓
 loop thread callback
~~~

阻塞操作没有消失，而是被隔离到允许阻塞的线程域。

## Handle 与 Request 是两种生命周期

长期资源属于 Handle：

~~~text
uv_tcp_t
uv_udp_t
uv_timer_t
uv_async_t
uv_signal_t
~~~

一次异步动作属于 Request：

~~~text
uv_write_t
uv_connect_t
uv_shutdown_t
uv_fs_t
uv_work_t
~~~

例如一条 TCP connection 同时存在三种生命周期：

~~~text
uv_stream_t
  connection lifetime

uv_write_t
  one operation lifetime

payload memory
  valid until completion
~~~

长期资源和一次操作不应该塞进一个巨型对象。

## Event Loop 是一个 Phase Scheduler

固定源码中的 uv_run() 明确分 phase：

~~~text
pending
→ idle
→ prepare
→ I/O poll
→ bounded pending drain
→ check
→ closing handles
→ timers
~~~

Timer、close callback、I/O callback 和 deferred callback 的顺序由 phase 明确定义，而不是“哪个 callback 先被调用就算哪个”。

## Timer 与 I/O 共用一次 Sleep

Timer 放在最小堆里，heap root 是最近 deadline。

~~~text
nearest timer deadline
        ↓
calculate poll timeout
        ↓
epoll_wait(timeout)
~~~

I/O 先到就提前醒；没有 I/O 就由 timer timeout 唤醒。因此一万个 Timer 不需要一万个 sleeping thread。

## 跨线程通知为什么不是共享 Queue 就够了

Worker thread 即使已经把结果写进共享 queue，loop thread 仍可能睡在 kernel：

~~~text
epoll_wait(..., -1)
~~~

因此还需要：

~~~text
shared state
+
eventfd / pipe / EVFILT_USER
~~~

Linux 下 libuv 使用 eventfd 唤醒 loop。**数据状态和 wakeup channel 是两个不同问题。**

## 为什么 Loop Thread 适合作为状态 Owner

连接状态、timer state、协议状态和 close state 尽量只在 loop thread 修改，可以大幅减少 mutex。

这是通过限制 ownership topology 降低同步复杂度，而不是简单的“单线程比多线程快”。

## libuv 不替业务解决 Backpressure

uv_write() 可以积累 write_queue 和 write_queue_size。如果业务生产速度长期高于 socket drain rate，queue 仍然会增长。

libuv 提供 mechanism；业务仍必须决定暂停生产、限流、drop、断开慢连接或设置自己的 high-water mark。

## 六条可迁移原则

1. 长期 Resource 与一次 Operation 分开。
2. I/O readiness 与 blocking work 分开。
3. Timer deadline 与 I/O wait 合并。
4. 共享状态与跨线程 wakeup 分开。
5. 尽量让一组 mutable state 只有一个 execution owner。
6. close request 与 memory reclamation 分开。

这些原则可以直接迁移到机器人 Runtime、设备管理器、网络网关、日志服务和自研中间件。
