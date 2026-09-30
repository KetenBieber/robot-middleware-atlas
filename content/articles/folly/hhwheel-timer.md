# HHWheelTimer：大量 Timeout 为什么不一定要用 Min-Heap

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

前面已经看到 libuv 用 min-heap、nginx 用 rbtree。Folly 的 HHWheelTimer 给出第三种答案：hierarchical timing wheel。

这正好说明 Timer 数据结构没有统一最优。

## Heap 的模型

Min-heap 适合：

~~~text
insert O(log N)
get earliest O(1)
pop earliest O(log N)
~~~

但连接数巨大、timeout 频繁 reschedule/cancel 时，heap 调整会持续发生。

## Timing Wheel 的核心思想

把时间轴离散成 tick，再把 Timer 放进 bucket。

Folly 使用 4 层，每层 256 个 bucket：

~~~cpp
WHEEL_BUCKETS = 4
WHEEL_BITS = 8
WHEEL_SIZE = 256
~~~

近未来 Timer 放低层，远未来 Timer 放高层。时间推进到边界后，再 cascade 到更细粒度的 bucket。

## 为什么分层

很远的 Timer 没必要现在就精确排到最近 tick。

可以先粗粒度归档：

~~~text
far deadline
→ high-level bucket
→ cascade later
→ lower-level bucket
→ final expiration
~~~

这把大量远期 timeout 的维护成本摊薄。

## Bucket 为什么用 Intrusive List

Callback 自己继承 intrusive list hook，所以 Timer 进入 bucket 不需要额外 new 一个 list node。

这很适合 schedule/cancel 高频而 callback 对象长期存在的场景。

## Folly 并不是恒定 Tick

传统 timing wheel 容易让人以为必须每 1ms 唤醒一次。

Folly 会计算真正的 next wakeup，只 schedule 下一次必要的 timeout。空闲时不会为了“维护 wheel”持续醒来。

## Bitmap 是 Level-0 的二级索引

Level-0 bucket 还有 bitmap。若附近大量 bucket 为空，可以直接找下一位 set bit，而不是每个 tick 顺序扫描。

这是：

~~~text
bucket hierarchy
+
bitset index
~~~

的组合。

## Cascade 到底在做什么

高层 bucket 到期时，里面的 Timer 未必现在执行，而是按剩余时间重新放到更低层。

~~~text
L3 → L2 → L1 → L0 → callback
~~~

它把远期粗分类逐步精化为近期精确调度。

## Callback Reentrancy 比排序更危险

Timer callback 可能在执行过程中 cancel、reschedule，甚至销毁整个 wheel。

源码因此使用 `processingCallbacksGuard_` 和临时 `timeoutsToRunNow_`，先把待运行 callback 从 bucket 中抽离，再逐个调用。

这避免遍历 bucket 时 callback 反向修改正在遍历的数据结构。

## Heap / RBTree / Wheel 怎么选

| 结构 | 强项 | 代价 |
| --- | --- | --- |
| min-heap | 快速得到最近 deadline | 任意删除、频繁调整较麻烦 |
| rbtree | 有序、任意节点删除自然 | 每次操作 O(log N) |
| timing wheel | 大量 timeout、分层时间范围 | cascade 与精度设计更复杂 |

Timer 容器选择来自 workload，不来自“哪个容器更高级”。

## 机器人系统中的映射

Wheel 适合大量 soft timeout，例如 sensor heartbeat、RPC deadline、device watchdog、stale-state cleanup。

但 1 kHz hard real-time control tick 不应该依赖这种通用 timeout abstraction 保证。

## 可迁移原则

1. Timer 容器按 deadline workload 选型。
2. 大量 Timer 可以用时间分桶换维护成本。
3. Intrusive callback 适合频繁 schedule/cancel。
4. Bitmap 可以给稀疏 bucket 建二级索引。
5. Timer callback 的 reentrancy/lifetime 与时间排序同样重要。