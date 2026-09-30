# Condition、Connector 与 Backpressure：Queue 为什么直接决定 Operator 能不能被调度

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

如果只看 Graph，会觉得：

~~~text
A → B
~~~

意味着 A 算完以后 B 就算。

真实 streaming runtime 不是这样。

B 能不能运行取决于 Receiver queue；A 能不能继续运行又可能取决于 B 的 queue 有没有空间；如果 A 需要 GPU buffer，还可能取决于 allocator 有没有空 block。

所以 Holoscan 最有价值的一点是：**队列容量、内存容量与异步事件，不只是错误处理，它们直接进入 scheduling condition。**

## 先看最常见的 Receiver：DoubleBufferReceiver

固定 header 直接描述了两级 queue：

~~~text
front stage
main stage
~~~

消息首先进入 front stage。

当 Operator 被选中执行时，front-stage messages 在 `compute()` 之前移动到 main stage；然后 compute 期间 front stage 又可以继续接收新消息。

可以画成：

~~~text
Producer
   ↓
front stage
   ↓ swap/stage before compute
main stage
   ↓
Consumer compute()

同时：
Producer 可以继续写新的 front stage
~~~

这就是“double buffer”名字真正要表达的东西：接收新数据与当前 tick 消费数据之间有 staging boundary。

## 为什么不直接一个 std::queue + mutex

单 queue 模型：

~~~text
Producer push
Consumer pop
~~~

如果 Consumer 需要在一次 compute 内看一批稳定输入，而 Producer 同时持续 push，就必须不断协调同一容器。

双 stage 后：

~~~text
front:
负责下一轮到达

main:
负责当前轮消费
~~~

当前 tick 的输入集合与下一批到达被逻辑分开。

这和游戏引擎 double buffering、网络 RX batch、RCU snapshot 都有相似动机：

> **把“正在读的视图”和“正在写的新状态”分开。**

## MessageAvailableCondition：输入队列变成调度谓词

固定 wrapper：

~~~cpp
spec.param(receiver_, "receiver", ...);
spec.param(min_size_, "min_size", ..., 1UL);
~~~

含义是：

~~~text
receiver available message count >= min_size
→ condition permits execution
~~~

因此 Consumer 不需要自己写：

~~~cpp
while (queue.empty()) {
    sleep(...);
}
~~~

Scheduler 直接把 queue state 纳入 readiness。

这把 blocking responsibility 从业务 compute 中移到了 runtime。

## front_stage_max_size：为什么“太多消息”也可能禁止执行

MessageAvailableCondition 还可以配置 `front_stage_max_size`。

这说明 readiness 不一定只有“至少有一个消息”。

某些 codelet/Operator 不会每次 tick 清空 front stage，于是 runtime 还需要限制 front-stage accumulation。

本质上 condition 可以表达一个窗口：

~~~text
min_size <= queue_state <= front_stage_max_size
~~~

这比简单 `queue.empty()` 更接近真实 pipeline。

## DownstreamMessageAffordableCondition：Backpressure 被前移到 compute 之前

最朴素 Producer：

~~~cpp
auto result = expensive_inference();
queue.push(result);
~~~

如果 push 时才发现 downstream queue 满：

~~~text
GPU 计算已经做完
结果却放不进去
~~~

计算资源已经浪费。

Holoscan 的 `DownstreamMessageAffordableCondition` 直接检查 downstream receiver 是否至少有指定 free slots。

固定源码参数：

~~~cpp
transmitter_
min_size_
~~~

源码描述的是：

~~~text
receiver connected to transmitter
has at least min_size free slots
in its back/front buffer capacity
~~~

于是可以做到：

~~~text
downstream full
→ Producer not READY
→ expensive compute does not start
~~~

这就是**调度级 backpressure**。

## Backpressure 与“queue 满了以后处理”不是一个层级

事后策略：

~~~text
compute
↓
publish
↓
queue full
↓
drop / reject / fault
~~~

调度级策略：

~~~text
check downstream capacity
↓
not enough
↓
do not schedule Producer
~~~

后者把资源约束传播到了上游执行决策。

## Queue Policy：pop、reject、fault 在表达三种系统哲学

DoubleBufferReceiver/Transmitter 都暴露：

~~~text
capacity
policy
~~~

固定源码说明 policy：

~~~text
0: pop
1: reject
2: fault
~~~

### pop

队列满时通过丢掉已有内容为新消息腾空间。

更像：

~~~text
freshness > completeness
~~~

适合实时视觉/状态流中“旧帧价值快速衰减”的场景。

### reject

保留已有 queue，新来的消息进不去。

更像：

~~~text
already accepted work > newest work
~~~

### fault

把容量耗尽视为设计/运行时错误。

适合理论上不应该 overflow、希望尽快暴露 capacity misconfiguration 的 pipeline。

选择 policy 其实是在定义过载语义。

## Capacity=1 为什么不是“小得离谱”

默认 queue capacity 可以很小。

对于实时 streaming，目标往往不是保存全部历史，而是维持低 Data Age。

假设 Producer 60 Hz、Consumer 30 Hz，queue 无界：

~~~text
1 s 后积 30 frames
10 s 后积 300 frames
~~~

系统 FPS 看起来还在跑，但 Consumer 看到的是越来越旧的世界。

因此机器人 pipeline 的正确指标不是只有 throughput，还必须看：

~~~text
queue depth
oldest item age
latest item age
drop count
producer blocked/not-ready time
~~~

## MemoryAvailableCondition：Buffer Pool 也可以产生 Backpressure

如果 Operator 每次运行需要一个 GPU block：

~~~text
READY
→ compute
→ allocate
→ allocation fails
~~~

已经太晚。

Holoscan v4.6.0 提供 MemoryAvailableCondition：

~~~cpp
allocator_
min_bytes_
min_blocks_
~~~

并且强制 `min_bytes` 与 `min_blocks` 二选一。

对 BlockMemoryPool 可以直接表达：

~~~text
free blocks >= 1
→ Operator may execute
~~~

于是 allocator capacity 成为 scheduler input。

这是现代异构 runtime 很重要的一步：

> **内存不是 compute() 内部才出现的实现细节，而是 runnable state 的一部分。**

## 一个完整 Producer readiness 可以是什么

例如 GPU Preprocess：

~~~text
input receiver has frame
AND
downstream inference receiver has free slot
AND
device BlockMemoryPool has free block
AND
period/deadline condition allows
~~~

只有全部满足才进入 READY。

这样 Scheduler 看到的不是抽象“task”，而是当前资源约束下真正可推进的工作。

## Condition Graph 其实是隐藏的第二张图

用户看到的业务图：

~~~text
Camera → Preprocess → Infer → Display
~~~

但执行时还存在资源依赖图：

~~~text
Camera
  depends on capture event

Preprocess
  depends on input queue
  depends on GPU block
  depends on downstream space

Infer
  depends on input queue
  depends on TensorRT/CUDA resources

Display
  depends on input queue
~~~

所以真正的 runtime graph 是：

~~~text
data dependency
+
resource/scheduling dependency
~~~

这也是为什么只画 Operator DAG 不足以分析 latency。

## AsyncBuffer：当需求从 FIFO 变成“独立并行 + 最新状态”

固定 v4.6.0 header 说明 AsyncBuffer 使用 **Simpson's four-slot buffer** 来实现 lockless asynchronous communication。

官方 ping example 中，TX 与 RX 通过 kAsyncBuffer 后可以在两个 worker thread 上独立并行；RX 可能观察到“旧消息路径”标签。

这里的设计目标和普通 bounded FIFO 不同。

FIFO 强调：

~~~text
item 1
then item 2
then item 3
~~~

four-slot/latest-style asynchronous exchange 更强调：

~~~text
Producer 不因为 Consumer 暂时没读就停下来
Consumer 获得一个一致的已发布值
双方尽量不争同一个 slot
~~~

它适合状态传播，而不适合“每条消息都必须恰好处理一次”的命令流。

## 为什么 Four-Slot 会用四个 slot，而不是两个

如果只有两个 buffer：

~~~text
Reader 正在读 A
Writer 写 B
~~~

Writer 发布 B 后，如果很快又要写下一次，它可能想重新覆盖 A；但 Reader 可能仍没读完。

经典 four-slot 思想通过两个 pair × 每 pair 两个 slot，让 Reader 与 Writer 各自拥有可切换空间，避免两方同时触碰正在被另一方使用的 cell。

这里最重要的不是背 Simpson 算法，而是看到一个原则：

> **要做到无锁异步覆盖，必须给“正在读”和“下一次写”留出独立物理状态。**

当前 Holoscan checkout 公开的是 AsyncBuffer wrapper/header，真正 GXF four-slot 原子实现位于底层 GXF 组件，因此本文不会伪造其具体 atomic memory_order。

## AsyncBuffer 为什么限制连接拓扑

固定 `Fragment::add_flow()` 会对 AsyncBuffer 做额外检查：某些目标 input 已经存在其他 upstream 连接时，会拒绝再把它改成 AsyncBuffer。

这是因为 lock-free/latest-style connector 往往依赖更严格的 producer/consumer topology。

通用 MPMC queue 可以接受很多 producer，但代价是更复杂同步。

专用 SPSC/latest buffer 则用拓扑限制换更简单、更强的进展保证。

这和我们在 concurrent queue 章节得到的原则一致：

> **先限制 topology，再优化 synchronization。**

## DoubleBuffer 与 AsyncBuffer 应该怎么选

| 维度 | DoubleBuffer | AsyncBuffer |
| --- | --- | --- |
| 主要语义 | staged queue | asynchronous/latest-style exchange |
| 历史消息 | 可以排队到 capacity | 不应理解成可靠 FIFO 历史 |
| backpressure | capacity/policy/condition | 重点是独立并行与覆盖语义 |
| 典型用途 | pipeline stage message flow | 异步状态、不同速率执行流 |
| 拓扑 | 常规 Holoscan port | 连接限制更严格 |

不要因为 AsyncBuffer 名字里有 async 就默认它“更先进”。

Connector 的选择首先是消息语义选择。

## 真实机器人怎么组合

一个合理组合可能是：

~~~text
camera frame
→ bounded DoubleBuffer

latest localization state
→ latest/async state channel

control command
→ bounded ordered queue + sequence/deadline

diagnostic state
→ async/latest
~~~

同一个进程使用不同 channel type 很正常。

## 把 Queue 与 Scheduler 联起来以后，Backpressure 才完整

完整路径：

~~~text
Receiver queue state
        ↓
MessageAvailableCondition
        ↓
Consumer READY/WAIT

Downstream free capacity
        ↓
DownstreamMessageAffordableCondition
        ↓
Producer READY/WAIT

Allocator free blocks
        ↓
MemoryAvailableCondition
        ↓
GPU Producer READY/WAIT
~~~

所以 backpressure 不是一个 `if(queue.full())`。

它已经成为**图上资源状态向 Scheduler 传播的控制信号**。

## 诊断过载时应该记录什么

至少分三层：

~~~text
Queue:
  capacity
  current depth
  drop/reject/fault count
  oldest data age

Scheduler:
  WAIT duration
  READY-to-run latency
  worker queue wait

Memory:
  free blocks
  allocation wait/fail
  buffer hold time
~~~

如果只看平均 FPS，就无法判断系统是 compute 慢、queue 堵、worker 没 CPU，还是 GPU buffer 没归还。

## 本篇真正要带走什么

第一，Queue 是调度状态，不只是数据容器。

第二，Backpressure 最有效的位置往往是昂贵 compute 开始之前。

第三，FIFO、double-stage queue 与 latest/async buffer 是不同消息语义，不应该用一个通用 `std::queue` 抽象掉。

第四，资源容量应该显式进入 ready predicate，而不是等异常发生以后再处理。
