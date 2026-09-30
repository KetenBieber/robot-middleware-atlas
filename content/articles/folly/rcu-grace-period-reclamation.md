# RCU：Reader 几乎不等 Writer，代价是把“删除”推迟到 Grace Period 之后

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

很多系统状态具有非常明显的访问偏斜：

~~~text
read: millions
write: occasional
~~~

例如：

- routing/config snapshot；
- feature flags；
- device registry snapshot；
- static-ish topology；
- policy table。

如果每次 Reader 都拿 shared_mutex，读路径仍然需要同步访问同一把锁状态。

RCU 的思路是把成本转移给 Writer 和 reclamation。

## 第一性原理：Reader 真正需要的是什么

Reader 不一定需要阻止 Writer 发布新版本。

它只需要保证：

> 我正在读的旧版本，在我退出 critical section 前不要被 free。

于是 Writer 可以：

~~~text
build new object
↓
atomic publish new pointer
↓
retire old object
↓
wait until pre-existing readers finish
↓
reclaim old object
~~~

发布新状态和回收旧内存被拆成两个时间点。

## rcu_domain 是 Read-side Lifetime Domain

Folly `rcu_domain` 保存：

~~~text
ThreadCachedReaders counters_
global version_
work_
TurnSequencer turn_
syncMutex_
retire queue q_
two epoch queues
executor
~~~

Reader 进入：

~~~cpp
counters_.increment(version_.load(std::memory_order_acquire));
~~~

退出：

~~~cpp
counters_.decrement();
~~~

这比对每一个具体对象发布 hazard pointer 更接近“当前线程处于一个 read-side epoch”。

## 为什么 `synchronize()` 要推进两个 Epoch

源码目标：

~~~cpp
target = curr + 2;
~~~

原因不是神秘的“两轮更安全”，而是 Reader 进入时读取 version 与登记 counter 之间存在时间窗口。

只推进一个 epoch，可能漏掉一个“已经读取旧 version、但还没完成 reader registration”的 late reader。

经过两个 epoch，旧 Reader 集合才可以被可靠隔离。

## half_sync 做什么

每次 half sync：

1. 把新 retire callback 收集进第一阶段 queue；
2. 检查下一 epoch 对应 Reader counter 是否归零；
3. 已经过一个 epoch 的 callback 移到 finished；
4. 新 callback 从 queue0 移到 queue1；
5. 发布新的 global version；
6. 用 TurnSequencer 唤醒等待 synchronize 的线程。

因此 callback 必须经历两个阶段才真正可执行。

## Grace Period 不是固定时间

Grace period 的定义不是“等 10ms”。

它是：

> 所有在更新前已经进入的 Reader 都已经退出。

如果某个 Reader 在 read-side critical section 里停 2 秒，那么旧对象至少 2 秒不能回收。

所以 RCU 的黄金规则之一就是：read-side critical section 必须短。

## 为什么 retire Callback 在 syncMutex 外执行

Folly 先把已经安全的 callback 收集到 `finished`，释放 `syncMutex_` 后才交给 executor。

因为 deleter 可能很慢、重新进入其他 subsystem，甚至触发复杂 destructor。

和前面 scheduler 的原则一致：

> 内部同步锁只保护 Runtime metadata，不要覆盖任意用户代码。

## 为什么 `retire()` 不能直接 Full Synchronize

调用 `retire()` 的线程自己可能正处于 RCU read-side critical section。

如果它同步等待“所有 Reader 退出”，也包括自己，就可能永远等不到。

因此普通 retire 路径只能尝试 non-blocking `half_sync(false)`；显式 `synchronize()` 则要求当前线程不能持有 RCU reader lock。

这是 API contract 和 deadlock proof 直接相关的例子。

## RCU 与 Hazard Pointer 的区别

两者都解决：

~~~text
object removed
but reader may still hold pointer
~~~

但保护粒度不同。

| 机制 | Reader 表达 | 更适合 |
| --- | --- | --- |
| Hazard Pointer | 我正在保护这个具体 pointer | lock-free linked traversal、动态 pointer graph |
| RCU | 我现在处于这个 domain 的 read epoch | 大量读、少量发布新 snapshot |

Hazard pointer 的 Reader 会发布具体地址；RCU Reader 更像声明“我进入了旧版本可能仍被访问的时代”。

## RCU 为什么常与 Immutable Snapshot 搭配

如果 Writer 发布新 pointer 后还原地修改旧对象，Reader 仍会遭遇 data race。

最容易推理的结构是：

~~~text
old immutable snapshot
↓ reader keeps using

writer builds new snapshot
↓ atomic publish

old snapshot retire after grace period
~~~

这就是 copy-update-publish-reclaim。

## 机器人系统中的直接场景

例如 Planner 高频读取：

~~~text
RobotConfig
CostPolicy
MapMetadata
SafetyLimitSnapshot
~~~

后台配置线程偶尔更新。

如果这些对象可以作为 immutable snapshot 发布，RCU 可以让 hot reader path 避免与 writer 锁竞争。

但高频 mutable control state 不适合直接套 RCU。

## 可迁移原则

1. Publication 与 reclamation 是两个不同事件。
2. Reader 只需要旧对象继续存活，不一定要阻止新版本发布。
3. Grace period 是 reader completion 条件，不是固定 wall-clock delay。
4. RCU 适合 read-mostly immutable snapshot，不是通用共享状态魔法。
5. Memory reclamation strategy 必须和数据结构访问模式一起设计。