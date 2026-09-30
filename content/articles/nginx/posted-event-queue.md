# Posted Event Queue：为什么 epoll Ready 以后不一定立刻调用 Handler

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

在最简单 Reactor 中：

~~~text
epoll_wait
↓
event ready
↓
handler(event)
~~~

nginx 允许把 ready event 先放进 posted queue。

## 三条 Posted Queue

固定源码定义：

~~~c
ngx_queue_t ngx_posted_accept_events;
ngx_queue_t ngx_posted_next_events;
ngx_queue_t ngx_posted_events;
~~~

它们不是同一个优先队列，而是不同 phase 的 intrusive FIFO。

## `ngx_post_event` 为什么先检查 posted bit

~~~c
if (!(ev)->posted) {
  ev->posted = 1;
  ngx_queue_insert_tail(q, &ev->queue);
}
~~~

同一个 event 被重复 post 时不会插入多次。

因此 queue 中表达的是：

> 这个 event 至少需要再执行一次。

而不是累计 notification 次数。

这是 event coalescing。

## Posted Queue 为什么是 Intrusive

`ngx_event_t` 自己内嵌 `ngx_queue_t queue`。

所以 post/delete：

~~~text
不需要 malloc queue node
不需要额外 owner object
O(1) remove
~~~

适合高频 runtime bookkeeping。

## Worker Phase 为什么先处理 Accept Posted Events

`ngx_process_events_and_timers()`：

~~~text
epoll
↓
posted_accept_events
↓
release accept mutex
↓
expire timers
↓
posted_events
~~~

当 accept mutex 生效时，持锁 worker 先把 accept readiness post 起来，再在统一阶段处理。

这样可以控制 accept 与普通 connection event 的执行边界。

## posted_next_events 又解决什么

某些 callback 希望：

> 不要在当前 event-loop iteration 继续执行，而是至少推迟到下一轮。

`ngx_posted_next_events` 在下一轮开始前被整体 move 到普通 posted queue，并把 poll timeout 设为 0。

这相当于：

~~~text
defer to next tick
~~~

GUI/message loop、JavaScript micro/macro task、游戏 frame scheduler 都有相似概念。

## 为什么 Deferred Callback 很重要

### 避免深层递归

handler A 如果直接触发 B，B 又触发 A，调用栈和状态重入会很复杂。

### 保证内部状态先完成

可以先：

~~~text
update connection state
update queue ownership
release mutex
~~~

再执行用户/上层 handler。

### 提供 Fairness Boundary

把“现在 ready”变成“稍后同一 loop 执行”，可以把执行顺序纳入 runtime phase。

## epoll module 怎么选择 Post 还是 Immediate

~~~c
if (flags & NGX_POST_EVENTS) {
  queue = rev->accept ? &ngx_posted_accept_events
                      : &ngx_posted_events;
  ngx_post_event(rev, queue);
} else {
  rev->handler(rev);
}
~~~

同一个底层 readiness 可以有两种 dispatch policy。

这说明：

> readiness detection 与 handler scheduling 是两个不同层。

## 和 libuv Pending Queue 对照

libuv：

~~~text
pending queue
check phase
closing phase
~~~

nginx：

~~~text
posted accept
posted normal
posted next
~~~

两者都不是把 kernel readiness 直接等同于业务 execution。

## 适合迁移到普通程序的模式

~~~text
interrupt / socket / device callback
↓
update minimal state
↓
post lightweight event object
↓
owner loop later executes heavy logic
~~~

对于串口、CAN gateway、设备驱动 wrapper、GUI、机器人 supervisor 都很实用。

## 关键原则

1. Notification 是否允许 coalesce 要由业务语义决定。
2. Readiness detector 和 execution scheduler 应分层。
3. Intrusive queue 很适合 runtime-owned event object。
4. “下一轮再执行”是解决重入的一种简单而强大的工具。
