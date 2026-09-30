# SMP Message Queue：跨 Core 不共享对象，而是把计算发给 Owner Shard

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Shard-per-core 架构真正成立的关键，不是每核都有一个 Reactor，而是：

> 跨 shard 协作必须有明确的数据与控制通道。

Seastar 的 `smp_message_queue` 就是这条通道。

## 每对 Shard 之间使用 SPSC Queue

源码核心：

~~~cpp
using lf_queue_base = boost::lockfree::spsc_queue<
    work_item*,
    boost::lockfree::capacity<128>>;
~~~

为什么不是 MPMC？

因为对一条 A→B 的 queue 来说：

~~~text
producer = shard A
consumer = shard B
~~~

天然就是 SPSC。

这和 Folly 第一篇 SPSC 的结论完全一致：

> 先通过架构把 topology 简化，再选择最简单的数据结构。

## 为什么同时有 Pending 与 Completed 两条 Queue

一次 `submit_to(B, func)` 实际是 request/response：

~~~text
A
  pending queue ──────→ B
                       run func
A
  completed queue ←─── B
  fulfill local promise
~~~

`_pending` 承载远端 work item，`_completed` 把结果生命周期送回 origin shard。

## Work Item 为什么继承 Task

`work_item : public task`。

目标 shard 收到 work item 后，并不是在 SMP poller 内直接任意执行用户函数。

`process()` 会把它纳入目标 Reactor 的 task/scheduling 语义。

这样跨核消息到达和真正业务 execution 仍然分层。

## Origin Shard 为什么负责最终 Delete

`async_work_item::run_and_dispose()` 明确不删除自己。

目标 shard 执行函数并保存 result/exception 后：

~~~text
respond(this)
↓
completed queue back to origin
↓
origin complete() promise
↓
delete work_item
~~~

这使 work item 的 allocator/lifetime owner 仍然回到创建它的 shard。

## 为什么先 Pending FIFO，再批量 Flush 到 SPSC Ring

发送侧有：

~~~text
std::deque<work_item*> pending_fifo
↓ batch_size = 16
boost SPSC ring
~~~

不是每 submit 一个 item 都立刻触碰跨核共享 ring。

先在本核 local deque 聚合，达到 batch 或 poll flush 时再批量 push。

这是：

~~~text
local accumulation
→ amortized cross-core publication
~~~

## process_queue 为什么先搬到 Local Array

源码注释直接说明：

> copy batch to local memory in order to minimize time in which cross-cpu data is accessed

Consumer 先从跨核 SPSC ring 取一批 pointer 到本地 stack array，然后才逐个 process。

并且会提前 prefetch 后续 work item。

这体现一个非常强的多核原则：

> 共享 cache line 只负责交接 ownership，交接完尽快回到本地内存工作。

## Service Group Semaphore 是 Backpressure

`submit_item()` 在真正进入 queue 前要从目标 shard 对应的 SMP service-group semaphore 获取 unit。

这限制 outstanding 跨核请求。

所以跨核 message passing 也不是无限 queue：

~~~text
admission control
→ submit
→ target execute
→ completion returns
→ signal capacity back
~~~

## maybe_wakeup 为什么不能只 Push Queue

如果目标 Reactor 已经睡眠，仅修改 SPSC ring 不能让 CPU 自动醒来。

所以 publish 后需要 `remote->wakeup()`。

源码特别强调 push 与 wakeup 之间的 memory-order/barrier 关系，避免：

~~~text
consumer checks queue empty
→ decides to sleep
producer pushes
→ wakeup visibility race
~~~

Queue state 与 wakeup protocol 必须一起证明。

## 为什么 Statistics 也要隔 Cache Line

发送侧与接收侧统计结构都 `alignas(cache_line_size)`，中间甚至故意放 `metric_groups` 拉开距离，避免硬件 prefetcher 顺带把另一个 CPU 正在写的 line 拉进来。

这说明 false sharing 不只发生在“核心算法变量”，metrics 也能破坏 hot path。

## 可迁移原则

1. N 个 shard 不代表需要一个 N-way MPMC；点对点通道可以是 SPSC。
2. Cross-core queue 只负责 ownership handoff，消费后尽快转入 local batch。
3. Request 与 completion 可以各有一条单向 queue。
4. 跨核 outstanding 数量需要 backpressure/admission control。
5. Queue publication 与 CPU wakeup 是两个必须共同证明的协议。