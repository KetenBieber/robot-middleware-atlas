# Folly 总览：不是工具函数合集，而是一套工业 C++ Runtime 数据结构观

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`（Folly v2026.09.28.00）。

Folly 最值得研究的地方，不是它提供了多少容器，而是它把高性能 C++ Runtime 中反复出现的问题拆成了一组可以复用的底层部件：

~~~text
线程间通信
→ ProducerConsumerQueue / MPMCQueue / AtomicNotificationQueue

网络 payload
→ IOBuf

大量 Timer
→ HHWheelTimer

事件循环
→ EventBase

CPU 任务执行
→ CPUThreadPoolExecutor
~~~

这些类围绕同一组工程约束：cache line 怎样避免互相打架；producer/consumer 怎样减少共享写；blocking 应该先 spin 还是直接睡；wakeup 应该和 payload 分离还是合并；buffer ownership 怎样跨协议层移动；大量 timeout 为什么不一定用 heap；thread pool 怎样把 queue policy 和 worker policy 分开。

## 一条统一分析线

对 Folly 可以沿三条线同时读：

- 数据线：payload 放在哪里、是否复制、queue 是否 bounded、buffer 是否 chain；
- 控制线：谁通知谁、何时 eventfd/futex/semaphore wakeup、是否批处理；
- 所有权线：slot、buffer、callback、thread 分别什么时候可复用或销毁。

真正复杂的 bug 往往发生在这些生命周期交叉处，而不是 API 调用本身。

## 为什么先 SPSC，再 MPMC

`ProducerConsumerQueue` 已知只有一个 producer、一个 consumer，因此根本不需要万能 MPMC 算法。`MPMCQueue` 则必须额外解决多个生产者、多个消费者、slot reuse、ordering 与 blocking。

因此第一条程序设计原则是：

> producer/consumer topology 本身就是优化信息。

先问“我真的需要 MPMC 吗”，再谈 lock-free。

## 这组专题真正要带走的结构

~~~text
single owner
cached remote progress
monotonic ticket
per-slot generation / turn
intrusive linkage
bounded batch
armed / disarmed notification
hierarchical timer bucket
queue policy injected into executor
~~~

这些结构可以迁移到网络服务器、CAN 接收线程、相机流水线、planner worker、logger、GPU completion queue、supervisor 和控制 Runtime。

本专题顺序：SPSC cache-line/cursor → MPMC ticket/turn → EventBase notification → IOBuf ownership → HHWheelTimer → CPUThreadPoolExecutor。