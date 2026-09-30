# ProducerConsumerQueue：SPSC 为什么可以做到几乎只写自己的 Cache Line

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

`ProducerConsumerQueue<T>` 是固定容量、单生产者单消费者队列。它最值得学的不是 ring buffer 本身，而是 2026 版实现对 ownership、cache line 和 remote cursor cache 的处理。

## 从最朴素的 Ring Buffer 开始

~~~text
write_index
read_index
buffer[N]
~~~

Producer 需要判断 full，Consumer 需要判断 empty，所以两边都会读取对方进度。如果两个 index 落在同一 cache line，两个核会不断互相 invalidate，这就是 false sharing。

## Folly 直接按 Side 拆 Ownership

源码核心：

~~~cpp
struct Side {
  T* const records{};
  uint32_t const mask{};
  uint32_t const size{};
  std::atomic<uint64_t> localIndex{};
  uint64_t remoteIndexCache{};
};

alignas(hardware_destructive_interference_size) Side producer_;
alignas(hardware_destructive_interference_size) Side consumer_;
~~~

Producer 热路径只改 producer side；Consumer 热路径只改 consumer side。两边各占自己的 destructive-interference boundary。

## remoteIndexCache 为什么重要

Producer 判断是否 full 时，先看自己的 `remoteIndexCache`。只有缓存显示“可能满了”，才真正 acquire-load Consumer 的 `localIndex`。Consumer 判断 empty 同理。

于是稳定状态下：

~~~text
Producer mostly touches producer cache line
Consumer mostly touches consumer cache line
~~~

跨核读取只在接近 empty/full 边界时发生。

这是一种非常通用的优化：remote state 如果只用于保守判断，可以先缓存，只有触碰资源边界时再 refresh。

## 为什么 Cursor 单调递增

存储的 logical cursor 不在 `0..N-1` 中循环，而是持续增长。真正访问 slot 时才做：

~~~cpp
records[index & mask]
~~~

因此 occupancy 可以直接表达为：

~~~text
write_cursor - read_cursor
~~~

0 表示 empty，capacity 表示 full，不需要传统 ring buffer 故意浪费一个 slot。

## Acquire / Release 的意义

Producer：构造 slot 内容 → release-store producer cursor。Consumer：acquire-load producer cursor → 读取 slot。由此建立 slot construction 到 consumer access 的 happens-before。

Consumer 释放 slot 后也 release 更新自己的 cursor，Producer acquire refresh 后才知道该 slot 可以安全复用。

## 为什么它能做到 Wait-free SPSC

Producer 不和其他 producer 抢 ticket，Consumer 也不和其他 consumer 抢 ticket；full/empty 时直接返回 false，不等锁、不等同角色线程。

Topology 本身消除了大部分同步复杂度。

## sizeGuess 为什么只承诺近似

任意线程读取 producer/consumer cursor 时并没有一次原子快照，所以 size 只能是 approximation。Folly 明确接受这一点，而不是为了 metrics 引入全局同步。

这也是成熟 API 设计：如果用途只是监控，不要为了绝对精确破坏 hot path。

## 机器人程序里的直接映射

~~~text
driver RX thread → estimator thread
camera capture thread → processing thread
logger producer → disk writer
~~~

只要确实是 1 producer + 1 consumer，就应该优先利用这个拓扑，而不是为了“通用性”直接上 MPMC。

## 可迁移原则

1. 先利用拓扑约束，再选择同步原语。
2. 热路径状态按 owner 拆 cache line。
3. remote progress 可保守缓存，边界时再 refresh。
4. logical sequence 与 physical slot index 分开。
5. 监控指标不一定值得付出精确同步成本。