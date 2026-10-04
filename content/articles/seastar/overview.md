# Seastar 总览：与其把共享并发容器做得越来越复杂，不如先让状态只属于一个 Core

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`（Seastar 25.05.0）。

前面 Folly 展示了一条工业 C++ 路线：SPSC、MPMC、hazard pointer、RCU、sharded map，把共享数据结构做得足够高效。

Seastar 提供另一种更激进的答案：

> 尽量不要共享 mutable state。

它把程序组织成多个 shard，每个 shard 通常对应一个逻辑 CPU，并拥有自己的 Reactor、任务队列、Timer、网络状态和 allocator。

跨 shard 协作不靠“大家一起拿锁访问同一对象”，而靠 message passing。

~~~text
Core 0 / Shard 0
  Reactor
  local state
  local allocator
       │
       │ smp message
       ▼
Core 1 / Shard 1
  Reactor
  local state
  local allocator
~~~

## 这和“单线程 Event Loop”有什么不同

单线程 Event Loop 只解决一个线程里的并发组织。

Seastar 进一步把整台多核机器拆成：

~~~text
one event loop per shard
+
one mutable-state ownership domain per shard
+
explicit cross-shard messages
~~~

所以多核扩展不是把一个 global event loop 变成多线程，而是复制多个相对独立的 runtime island。

## Reactor 是每个 Shard 的 CPU Runtime

Reactor 不只等 socket readiness。

它同时处理：

- scheduling-group task queues；
- pollers；
- timers；
- SMP messages；
- kernel I/O completions；
- cross-CPU free list；
- signal/syscall work；
- idle/sleep。

因此 Reactor 更接近一个 userspace cooperative scheduler，而不是普通 Reactor pattern 的薄封装。

## Future 不是阻塞句柄

Seastar 的 continuation 直接继承 `task`。

一个 future 未 ready 时，不会让 OS thread 阻塞等待。

而是：

~~~text
register continuation task
↓
current code returns to reactor
↓
event completes
↓
continuation becomes runnable task
↓
reactor executes it
~~~

这让一个 shard 可以只用一个 Reactor thread 承载大量逻辑并发。

## 跨 Shard 调用为什么必须走 SMP Queue

`smp::submit_to()` 的远端路径不是“把 lambda 丢进一个共享队列”这么简单：它先受 SMP service-group credit 限制，再经过 origin-local batch、pair-wise SPSC request queue、target Reactor task scheduling、reverse completion queue，最后回 origin shard resolve promise、归还 credit 并回收 work item。完整链见 [SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure](smp-message-queue.md)。

如果 Shard 0 直接访问 Shard 1 的 mutable object：

- ownership 被破坏；
- cache line 在核间迁移；
- local allocator/reference count 假设可能失效；
- shutdown/lifetime 变得难以推理。

所以 Seastar 用 `smp::submit_to()` 把函数作为 work item 发送给 owner shard。

真正的数据原则是：

> move computation to data owner，而不是让所有 core 共享 data。

这个原则在对象层进一步分成两条：`sharded<Service>` 把 computation 发到目标 shard 的本地 Service，而 `foreign_ptr<Ptr>` 允许 ownership handle 跨 shard 移动、却把最终 release/destructor 送回资源 owner。完整对象与析构执行域见 [Sharded 与 foreign_ptr：Owner-Shard、跨核调用与析构执行域](sharded-foreign-ptr.md)。

## 这条原则甚至延伸到内存释放

Seastar 的 allocator 是 per-shard 的。

如果 Core B free 了 Core A 分配的对象，它不会直接修改 A 的 allocator metadata。

而是：

~~~text
foreign free on B
↓
push pointer to A.xcpu_freelist
↓
A reactor later drains list
↓
A local allocator performs real free
~~~

也就是说连 free 都遵守 owner-computes。

## 与 Folly 的关系

两者不是互斥的。

可以把它们理解成两个优化层次：

~~~text
Architecture first:
Can mutable state be sharded?
→ Seastar-style ownership

If state really must be shared:
choose the right concurrent structure
→ Folly-style queue / CHM / RCU
~~~

## 本专题顺序

1. Reactor shard-per-core：先看一个 Core 内部怎样 cooperative scheduling。
2. Scheduling Group：同一 Reactor 内不同业务如何按 shares 分 CPU。
3. SMP Message Queue：跨 Core 如何批量传递 work item 与 completion。
4. Future/Continuation：异步控制流如何变成 Reactor task。
5. Sharded/foreign_ptr：对象 ownership 怎样绑定 shard。
6. Cross-core memory reclaim：为什么跨核 free 也必须回 owner。

最终目标不是学一套 Seastar API，而是掌握一个更一般的程序设计选择：什么时候应该优化锁，什么时候应该直接消除共享。