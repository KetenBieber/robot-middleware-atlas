# 线程、Asynchronous Write 与关闭：后台线程多不等于实时边界清楚

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## Domain 启动时有哪些线程

ddsi_start() 的固定源码依次启动：

~~~c
ddsi_gcreq_queue_start(
  gv->gcreq_queue);

ddsi_dqueue_start(
  gv->builtins_dqueue);

ddsi_dqueue_start(
  gv->user_dqueue);

ddsi_xeventq_start(
  gv->xevents,
  NULL);

setup_and_start_recv_threads(gv);
~~~

配置需要时还会有 TCP listen thread；第一个 async writer 创建后还会启动 sendq。

这是一组职责分离的 worker，不是一个统一 event loop。

## 默认 Write 为什么仍然会占用调用线程

官方开发文档说明默认 synchronous write 会一直执行到 socket send。后台 receive/event/GC 线程的存在，不会自动把 publish 变成异步。

所以控制线程是否会执行 sendmsg，要看 Writer 模式，而不是看进程里有没有 DDS worker。

## Async Write 到底做了什么

ddsi_xpack_send()：

~~~c
if (!xp->async_mode)
  ddsi_xpack_send_real(xp);
else
{
  struct ddsi_xpack *xp1 =
    ddsrt_malloc(sizeof(*xp));

  memcpy(xp1, xp, sizeof(*xp1));

  ...
  enqueue to sendq
}
~~~

异步模式把最终 transport send 转移给 sendq，但在移交前仍然已经完成了 serialization、WHC、RTPS/xpack 构建，并且要复制 xpack 管理结构和 iovec。

因此：

~~~text
async write
!= zero work on publisher thread
~~~

## SENDQ_MAX=200 揭示了真实背压

固定 ddsi_xmsg.c：

~~~c
#define SENDQ_MAX 200
~~~

入队时：

~~~c
while (
  gv->sendq_length >= SENDQ_MAX)
{
  ddsrt_cond_wait(
    &gv->sendq_cond,
    &gv->sendq_lock);
}
~~~

队列满以后，调用发布的线程会等待。

所以：

~~~text
asynchronous
!= unbounded
!= never blocks
~~~

反而正因为 queue 有边界，系统才能避免无限内存增长；代价是过载最终反馈到 producer。

## Receive Thread 与 Delivery Thread 为什么分开

Network receive 的关键目标是尽快继续收包、维护 RTPS protocol state。慢业务 callback 如果总在 receive thread 里执行，会扩大 socket backlog 与 packet loss 风险。

所以 Cyclone DDS 提供 delivery queue，把“网络解析”和“用户数据交付”在需要时分开。

不过为了低延迟与顺序保证，某些配置又允许 synchronous delivery。性能优化永远不是无条件的，需要结合锁和顺序语义看。

## GC Thread 不是 Java 式 GC

官方 write-to-take 文档特别解释：这里的 garbage collector 更接近垃圾车，负责延迟释放已经逻辑删除但仍可能被并发读者短暂观察的数据结构。

这让一些 lookup hot path 不必频繁 reference-count 每个瞬时引用，但要求删除路径经过 grace/deferred-free 协议。

## 关闭为什么先 stop I/O

ddsi_term_prep() 先把 rtps_keepgoing 清零，再 trigger receive threads。正确顺序是：

~~~text
禁止继续产生新协议工作
-> 唤醒 receive/send/event worker
-> join workers
-> 回收 transport
-> 回收 endpoint/entity
~~~

如果先 free entity，再停 receive thread，刚到的 packet 仍可能查找到正在释放的对象。

## 实时线程设计建议

对机器人控制进程，至少区分：

~~~text
hard/firm realtime control loop
DDS publish/subscribe adapter
telemetry/logging
discovery/recovery
~~~

是否把 DDS API 直接放进最高优先级线程，必须由测量后的 WCET、QoS、queue bound 与 socket 行为决定，而不是依赖“DDS 是实时中间件”这个标签。
