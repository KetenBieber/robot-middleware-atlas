# Scheduling Group：同一个 Reactor 只有一条线程，为什么还需要 VRuntime 与 Shares

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Shard-per-core 消除了很多跨线程锁，但没有消除一个问题：

> 同一个 Core 上有多个业务类别时，谁应该先拿 CPU？

例如：

~~~text
network request
background compaction
metrics
control-plane task
~~~

它们都在同一个 Reactor thread 上 cooperative execution。

如果只有一个 FIFO，CPU-heavy backlog 仍然可能把 latency-sensitive task 淹没。

## 每个 Scheduling Group 有自己的 Task Queue

`task_queue` 保存：

~~~cpp
int64_t _vruntime;
float _shares;
int64_t _reciprocal_shares_times_2_power_32;
circular_buffer<task*> _q;
~~~

这不是 OS runqueue，而是 Reactor 内部的 userspace scheduling domain。

## Shares 的意义

Runtime 被记账为：

~~~cpp
scaled = runtime * reciprocal(shares)
vruntime += scaled
~~~

因此同样真实运行 1 ms：

~~~text
shares 大
→ vruntime 增长慢
→ 更容易再次被选中

shares 小
→ vruntime 增长快
→ 更晚再次获得 CPU
~~~

这和 Linux CFS 的 vruntime 思想非常接近。

## 为什么不用 Priority=High/Low 两档

严格优先级容易造成 starvation：

~~~text
high queue never empty
→ low queue never runs
~~~

shares/vruntime 更适合表达：

~~~text
service A should get ~4x CPU weight of service B
~~~

而不是“B 永远不能在 A 前面”。

## Active Queue 为什么按 VRuntime 排序

`indirect_compare` 本质比较：

~~~cpp
tq1->_vruntime < tq2->_vruntime
~~~

Reactor 从 active queues 中拿最应该获得 CPU 的 group。

运行一批 task 后，根据实际 elapsed runtime 做 `account_runtime()`，再把仍有 backlog 的 queue 插回 active list。

所以调度单位不是单个 task，而是：

~~~text
scheduling group
→ run a cooperative batch
→ account elapsed CPU time
→ reinsert by vruntime
~~~

## Task Quota 的作用

即使当前 group 还有大量 task，`need_preempt()` 到来后，Reactor 也应该切回 scheduler。

否则 vruntime 公平性只存在于纸面：一个 group 永不返回控制权，就没人能执行调度算法。

因此：

~~~text
fair scheduling policy
+
cooperative preemption points
~~~

缺一不可。

## Shares 不是 Real-time Deadline

`shares=1000` 比 `shares=100` 获得更多长期 CPU 比例，不表示任务有：

- deadline guarantee；
- WCET guarantee；
- fixed-priority preemption；
- SCHED_FIFO semantics。

它解决的是 throughput/fairness，不是 hard real-time。

机器人系统里不能把 scheduling group shares 当成控制线程实时优先级。

## 机器人 Runtime 的映射

一个单核 perception/control-plane reactor 可以划：

~~~text
sensor ingress SG      shares 1000
state publication SG   shares 800
diagnostics SG         shares 100
background cleanup SG  shares 50
~~~

这样低价值后台任务不会完全饿死，但也不会轻易淹没前台路径。

真正的硬实时 servo loop 仍应该放到独立 RT execution context。

## 可迁移原则

1. Single-thread runtime 仍然需要内部 CPU fairness。
2. 每类业务独立 queue 比一个 global FIFO 更容易表达 policy。
3. Weight/share 适合比例公平，不等于实时 deadline。
4. Scheduler policy 只有在 task 主动归还控制权时才有效。
5. Runtime 应对实际 elapsed CPU time 记账，而不是只数 task 个数。