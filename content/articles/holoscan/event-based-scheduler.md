# Event-Based Scheduler：从轮询、Ready Queue 到 Linux 实时调度

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

这一页是 Holoscan 专题的核心。

因为一旦把 Operator 看成“任务”，真正的问题就不再是 API，而是经典操作系统问题：**谁判断任务 ready、ready work 放哪、哪个 worker 拿走、没工作时怎么睡、有工作时怎么醒、多个 worker 如何避免争同一个热点队列、最后 Linux 又怎样把线程放到 CPU 上。**

## 从最简单的轮询 Scheduler 开始

假设有 N 个 Operator，每个都有 `is_ready()`：

~~~cpp
while (!stop) {
    for (auto& op : operators) {
        if (op->is_ready()) {
            op->compute();
        }
    }
}
~~~

优点是简单。

但它把 CPU 时间大量浪费在：

~~~text
not ready
not ready
not ready
not ready
...
~~~

如果为了省 CPU 加 sleep：

~~~cpp
sleep_for(check_interval);
~~~

新的 ready event 又可能在 sleep 刚开始时发生，于是等待接近整个 interval。

这形成典型 tradeoff：

~~~text
poll fast
→ latency lower
→ CPU burn higher

poll slow
→ CPU lower
→ wake-up latency higher
~~~

Holoscan 旧 MultiThreadScheduler 正是 polling-based。官方 v4.6.0 文档明确将其保留为 legacy，新多线程场景推荐 EventBasedScheduler。

## Event-Based 的第一性原理

如果 readiness 来自状态变化，那么最合理的方式不是持续检查状态，而是让状态变化产生 notification。

概念链变成：

~~~text
Receiver receives message
or
Timer expires
or
Memory becomes available
or
Async event fires
        ↓
state transition
        ↓
notify dispatcher
        ↓
re-evaluate scheduling terms
        ↓
READY
        ↓
enqueue worker-ready work
~~~

这和操作系统从 busy polling 转 interrupt/event-driven I/O 的逻辑一致。

## Dispatcher 与 Worker 为什么要分开

一种粗糙多线程 scheduler 可以让所有 worker 自己扫描所有 Operator：

~~~text
W0 ─┐
W1 ─┼→ global operator set
W2 ─┤
W3 ─┘
~~~

这会产生：

- 重复 readiness check；
- shared graph/state contention；
- 谁负责 timed wait 不清楚；
- 同一 Operator 的调度竞争更复杂。

Event-Based 模型把职责拆开：

~~~text
state notifications
       ↓
   Dispatcher
       ↓
assign READY work
       ↓
worker ready queues
       ↓
 Workers
~~~

Dispatcher 更像 control plane；worker 更像 execution data plane。

## 为什么不是一个 Global Ready Queue

最自然的线程池设计：

~~~text
all producers
     ↓
global ready queue
     ↓
W0 W1 W2 W3 ...
~~~

worker 越多，所有线程越集中争夺：

~~~text
queue head/tail
mutex
condition variable
cache lines
~~~

吞吐开始被一条 shared queue 限制。

Holoscan v4.6.0 文档明确描述：默认 pool 中**每个 worker 拥有 private ready queue**，job 在 graph launch 时做固定、确定的 queue assignment。

因此默认结构更像：

~~~text
Dispatcher
 ├─> Q0 → Worker0
 ├─> Q1 → Worker1
 ├─> Q2 → Worker2
 └─> Q3 → Worker3
~~~

这首先减少了 worker 之间对一个 hot queue 的竞争。

## Private Queue 的新问题：Load Imbalance

如果固定分配以后：

~~~text
Q0: heavy, heavy, heavy
Q1: light
Q2: empty
Q3: empty
~~~

会出现：

~~~text
Worker0 overloaded
Worker2 idle
Worker3 idle
~~~

这就是 sharding/private queue 的典型副作用。

于是引入 work stealing。

## Work Stealing 为什么是“局部性 vs 利用率”的交换

Holoscan 参数：

~~~text
enable_queue_stealing
steal_scan_limit
~~~

当 worker 自己的 queue 为空时，可以扫描别的 worker queue 并偷一项 ready work。

概念上：

~~~text
Worker2
  own Q2 empty
      ↓
 scan Q0/Q1/Q3
      ↓
 steal from Q0
~~~

收益：

~~~text
减少 idle core
缓解静态分配不均
~~~

代价：

~~~text
访问别人的 queue
更多同步/cache traffic
thread affinity 被削弱
执行位置更难预测
~~~

所以 work stealing 默认并未强制开启；它是 workload-dependent tradeoff。

实时控制分支甚至可能不希望“别人来偷”，因为 cache locality 和固定 CPU placement 比平均吞吐更重要。

## steal_scan_limit 为什么存在

如果有 64 个 worker，而每次 steal 都扫描另外 63 个 queue：

~~~text
own queue empty
→ 63 victim probes
→ fail
→ repeat
~~~

空闲状态反而制造大量共享元数据访问。

`steal_scan_limit` 就是在限制一次 steal attempt 的探测成本。

这是典型的数据结构工程：算法上“扫描全部”最容易找到工作，系统上却可能污染 cache、增加同步。

## Dispatcher 自己也会成为热点

把所有 readiness event 都送到一个 Dispatcher，并不代表问题消失。

假设 32 个 worker/外部路径都频繁产生 notification：

~~~text
producer 0 ─┐
producer 1 ─┤
...        ─┼→ one internal event queue → dispatcher
producer31 ─┘
~~~

这条 notification queue 又成为 MPSC hotspot。

v4.6.0 因而提供：

~~~text
internal_event_shard_count
dispatcher_internal_pop_batch_size
~~~

官方文档说明 internal notifications 可以按 shard 分开；0 表示自动按 worker 数量设置 shard。

概念结构：

~~~text
W0/W1 → shard 0 ─┐
W2/W3 → shard 1 ─┤
W4/W5 → shard 2 ─┼→ Dispatcher
...              ─┘
~~~

这和高并发日志、网络 acceptor、metrics aggregator 常见的 sharded queue 完全同构。

## 为什么还要 Batch Pop

假设 dispatcher 每拿一条 notification 都重新走完整 bookkeeping：

~~~text
lock/acquire
pop 1
unlock
process
repeat
~~~

固定成本会放大。

`dispatcher_internal_pop_batch_size` 默认 32，意味着一次从 shard drain 多条 notification。

批处理思想：

~~~text
一次同步成本
摊给多条 item
~~~

但 batch 太大又可能让某个 shard 独占 dispatcher 太久，影响公平性。

所以 batch size 同样是：

~~~text
throughput
vs
fairness / tail latency
~~~

的交换。

## WAIT、WAIT_EVENT、WAIT_TIME 为什么不能塞进一种容器

不同 waiting state 的唤醒条件不同。

### WAIT_TIME

已知未来具体时间：

~~~text
now = t0
ready_at = t1
~~~

最自然的数据结构是按时间排序的 timed structure，例如 heap/timer list。

### WAIT_EVENT

不知道什么时候发生，只能等外部 event。

需要 event tracking / notification structure。

### WAIT

某个 condition 暂时不满足，但未必有确定时间。

也需要重新检查路径。

Holoscan v4.6.0 公开 `wait_state_shard_count`，专门控制 WAIT_EVENT / WAIT tracking lists；文档明确说 WAIT_TIME 由 separate timed job list 管理。

这是非常好的设计信号：

> 不同 wake-up key 应该使用不同数据结构。

不能为了统一接口，把定时器、外部事件和普通 queue wait 全都塞进一个 FIFO。

## Worker Post-Check Fast Path：为什么执行完还要马上再看一次

正常路径：

~~~text
Worker executes Operator
↓
notify Dispatcher
↓
Dispatcher re-checks
↓
still READY
↓
enqueue again
~~~

如果 Operator 是一个持续有数据的高吞吐 stage，这个 dispatcher round trip 很固定。

因此 v4.6.0 提供可选：

~~~text
enable_worker_postcheck_fastpath
~~~

worker 执行完以后直接 re-check 当前 entity；如果仍 READY，可以直接更新/重入自己的 queue，绕过一次 dispatcher 往返。

本质是：

~~~text
centralized control path
→
local fast path
~~~

这是 runtime 常见优化。

## Fast Path 为什么默认仍关闭

越绕过中央协调点，越容易引入边界状态问题。

例如：

- worker 判断 not-ready 的瞬间，外部 event 到达；
- dispatcher 是否会得到通知；
- real-time priority 下，某 worker 是否长期自循环导致其他状态饥饿。

所以 Holoscan 又提供 fallback notification 参数：

~~~text
postcheck_fallback_notify_interval
postcheck_fallback_notify_min_workers
postcheck_fallback_notify_min_period_ns
~~~

这是很典型的工业优化风格：

> 快路径不是删除正确性路径，而是在正确性路径旁边加一个可退回的局部优化。

## Event-Based 并不等于“没有线程”

Event-driven 经常被误解成“异步，所以不占线程”。

实际上至少有：

~~~text
dispatcher thread
worker threads
可能的 transport/network threads
application-owned threads
CUDA runtime threads
~~~

Event-based 只是改变“等待 ready 的方式”，没有消灭执行上下文。

## CPU Pinning 为什么进入 Scheduler API

如果 OS 可以任意迁移 worker：

~~~text
tick 0: Worker0 on CPU2
tick 1: Worker0 on CPU7
tick 2: Worker0 on CPU3
~~~

会产生 cache migration、NUMA locality 和 jitter。

Holoscan EventBasedScheduler 提供 `pin_cores` 给 default worker pool；dispatcher 还可以通过 `GXF_EBS_DISPATCHER_CPU_CORE` 单独绑核。

这说明 runtime 已经承认：

> 线程在哪个 CPU 上跑，是 pipeline 性能模型的一部分。

## SCHED_FIFO / SCHED_RR：Runtime 最终还是要服从 Linux

Dispatcher 可以通过环境变量配置 Linux real-time scheduling policy：

~~~text
GXF_EBS_DISPATCHER_SCHED_POLICY
GXF_EBS_DISPATCHER_SCHED_PRIORITY
~~~

支持 `SCHED_FIFO` / `SCHED_RR`。

用户定义 ThreadPool 也可以把 Operator 放入实时调度线程。

但这并不意味着设置 FIFO 就“自动实时”。

如果一个高优先级 FIFO worker：

~~~text
while(true) {
  always-ready operator
}
~~~

它可能长期压制低优先级线程。

因此必须同时分析：

~~~text
priority
blocking points
mutex ownership
condition wait
CPU affinity
WCET
~~~

否则只是在制造更难调试的 starvation / priority inversion。

## Dispatcher 与 Worker 的优先级关系要怎么想

如果 worker 很高优先级，而 Dispatcher 过低：

~~~text
event arrives
↓
dispatcher迟迟拿不到 CPU
↓
READY work 无法入队
↓
worker反而没活干
~~~

如果 Dispatcher 过高，又可能抢占真正做 compute 的 worker。

所以一个完整 RT 配置需要考虑：

~~~text
notification producer priority
dispatcher priority
worker priority
driver/network thread priority
~~~

而不是只调一个线程。

## 一个具身机器人线程拓扑示例

假设：

~~~text
CPU2: sensor ingest
CPU3: scheduler dispatcher
CPU4: control-critical worker
CPU5-6: perception workers
CPU7: logging/network
~~~

可以把 control-critical Operator 放入专用 ThreadPool，普通视觉 branch 留给 default pool。

这样 graph topology 与 execution topology 分开：

~~~text
same DAG
but
different CPU placement / priority
~~~

这就是 runtime 的价值。

## 为什么 Scheduler Metrics 很重要

v4.6.0 `log_perf_stats` 可以输出：

- dispatcher loop/notification statistics；
- per-worker wait/execute time；
- work-steal attempt/success；
- post-check fast-path/fallback hit rate。

这些指标比单纯测 FPS 更能定位问题。

例如：

~~~text
FPS low
~~~

可能是：

~~~text
worker compute 慢
dispatcher notification 太多
queue assignment 不均
steal 大量失败
Condition 长时间不满足
allocator 没 buffer
~~~

没有内部状态指标，只能把所有问题归因于“GPU 慢”。

## 源码继续下沉：公开 GXF 可以看到旧一代 EBS 的真实内部结构

Holoscan SDK v4.6.0 这一份 checkout 只直接包含 `holoscan::EventBasedScheduler` wrapper 与参数绑定；但 NVIDIA-ISAAC-ROS 还公开了一份 GXF v3.2-1 源码，固定基线为 `daf1810358301f642374dfb3d725be349bba5ec0`。

它不是 Holoscan v4.6 当前 backend 的同版本源码，因此不能拿它去“证明”新版 private ready queue、sharding、work stealing 的具体容器实现；但它完整展示了 Event-Based Scheduler 的上一代内部骨架：

~~~text
TimedJobList
  = priority_queue + unordered_set + pending list
  + mutex + condition_variable

UniqueEventList
  = list + unordered_map<eid, iterator>
  + mutex

ScheduleEntity
  = atomic execution ownership
  + ready-queue membership state

threads
  = dispatcher
  + async-event handler
  + worker pool
  + optional max-duration thread
~~~

下一篇 {doc}`GXF EventBasedScheduler 内部实现 <gxf-event-runtime-internals>` 会直接沿这份源码把容器、锁、condition variable、worker wakeup 与 Entity ownership 全部拆开。

再下一篇 {doc}`GXF EntityExecutor 与 MessageRouter <gxf-entity-executor-router>` 则继续追到一次 tick 前后的 `syncInbox → Codelet → syncOutbox → downstream notify`。

因此这一篇保留“Holoscan 4.6 的行为与演进”，后两篇负责“公开 GXF 代码里能够逐行证明的底层机制”，两层不会混写。

## 把 Event-Based Scheduler 映射回 Communication Foundations

它几乎把前面的并发章节全部串起来：

| Foundations 概念 | Holoscan 机制 |
| --- | --- |
| polling vs blocking/event | MultiThread vs EventBased |
| MPSC hotspot | internal notification queue |
| sharding | internal_event_shard_count |
| batching | dispatcher_internal_pop_batch_size |
| per-consumer queue | per-worker ready queue |
| load balance | work stealing |
| fast path | worker post-check |
| wakeup ownership | dispatcher |
| OS scheduling | pin_cores / FIFO / RR |
| observability | log_perf_stats |

所以 Holoscan 的 scheduler 不是 AI 领域特例。

它就是现代并发 runtime 在 GPU streaming 场景里的一个工业实例。
