# MPMCQueue：Ticket、Slot Turn 与 Futex 怎样解决多生产者多消费者复用

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

SPSC 已知谁写、谁读；MPMC 首先必须解决多个 producer 同时 push 时“谁属于下一个逻辑位置”。Folly 用 ticket。

## 全局 Ticket 只分配逻辑顺序

核心成员：

~~~cpp
Atom<uint64_t> pushTicket_;
Atom<uint64_t> popTicket_;
~~~

Producer 获取 push ticket，Consumer 获取 pop ticket。Ticket 回答“这次操作是第几个”，但它并没有保证映射到的物理 slot 已经可以复用。

## Ticket 映射到 Slot 与 Turn

设 capacity=C：

~~~text
slot index = f(ticket, C, stride)
turn       = ticket / C
~~~

同一个物理 slot 会服务 ticket `i`、`i+C`、`i+2C`。因此必须知道这个 slot 当前处于第几轮复用。

## SingleElementQueue 是每个 Slot 的状态机

每个 slot 内部是：

~~~cpp
aligned_storage_for_t<T> contents_;
TurnSequencer<Atom> sequencer_;
~~~

并规定 even turn 对应 enqueue，odd turn 对应 dequeue。

~~~text
empty generation N
→ producer owns
→ full generation N
→ consumer owns
→ empty generation N+1
~~~

Global ticket 解决 ordering；per-slot turn 解决 reuse safety。两层不能混为一谈。

## 为什么有 Ticket 还会阻塞

Producer 可能已经拿到 ticket=C，它映射回 slot 0，但 Consumer 还没有消费 ticket=0。于是 ticket 已分配，却不能覆盖 slot。

这时必须等对应 TurnSequencer 到达自己的 generation。

## TurnSequencer 为什么用 Futex

等待先走 adaptive spin；如果短时间没有轮到，就进入 futex：

~~~text
short wait → PAUSE / spin
long wait  → FUTEX_WAIT_BITSET
turn done  → FUTEX_WAKE
~~~

这避免了固定策略：永远 spin 会烧 CPU，永远 sleep 又会让短等待付出 syscall/scheduling 成本。

## Adaptive Spin Cutoff

Folly 会根据实际等待长度调整 spin threshold。频繁短等待时多 spin 一点，等待经常很长时更早睡眠。不同 CPU 的 PAUSE 成本差异也因此不会被一个硬编码循环次数绑死。

## 为什么 Push/Pop Ticket 还要分 Cache Line

Producer 群体高频写 `pushTicket_`，Consumer 群体高频写 `popTicket_`。如果它们共享 cache line，就会让所有参与核互相 invalidation。

所以物理内存布局本身也是并发算法的一部分。

## Stride 为什么存在

如果 ticket 直接映射到连续 slot，相邻逻辑操作会集中打同一组 cache line。Folly 通过 stride 打散物理访问位置，降低 slot 之间的 false sharing。

因此：

~~~text
logical order != physical adjacency
~~~

## Shutdown Sentinel

源码文档还建议 worker pool 可向 queue 插入 sentinel 来传播 shutdown。这提醒我们 queue 中不仅有业务 payload，也可能承载控制协议。

## 可迁移原则

1. Global ticket 负责顺序，per-slot generation 负责复用安全。
2. logical sequence 与 physical slot 必须分层。
3. cache-line placement 和 stride 是算法的一部分。
4. 短等待 spin、长等待 futex 是典型混合策略。
5. MPMC 的复杂度来自拓扑；能降级成 SPSC/MPSC 时优先改拓扑。