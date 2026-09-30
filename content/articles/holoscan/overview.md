# Holoscan 总览：当“消息中间件”已经不足以描述 GPU Streaming Runtime

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

如果一套机器人感知程序只是 sensor → callback → publish，用“中间件”理解它通常够用。

现代具身/视觉 AI pipeline 很快会变成：

~~~text
Camera / DMA
    ↓
format conversion
    ↓ CUDA stream
GPU inference
    ↓
post-process
    ↓
visualization / control / network
~~~

这里真正决定性能的已经不只是“消息怎么发”，还包括：哪个 Operator 此刻满足执行条件、ready Operator 放在哪个队列、哪条 OS worker thread 真正执行它、worker 是否绑定 CPU core、Tensor 当前在哪个 memory domain、Buffer 从固定 pool 取还是动态分配、GPU kernel 完成之前 Buffer 能不能归还、下游 queue 满时上游是否还应该继续算，以及 graph 被切成多个 Fragment 后哪条边要变成 UCX connection。

Holoscan 值得研究，正因为它把这些问题放进同一个 runtime。

## 从一个单线程实现开始

假设要做一条 60 FPS 相机推理链：

~~~cpp
while (running) {
    auto frame = camera.read();
    auto converted = preprocess(frame);
    auto result = infer(converted);
    auto overlay = postprocess(result);
    display(overlay);
}
~~~

因果关系很清楚，但所有阶段被锁在一条执行序列里。即使 CPU、GPU 与不同 pipeline stage 本来可以重叠，也完全利用不到。

于是自然会拆成：

~~~text
Source ──> Preprocess ──> Inference ──> Postprocess ──> Holoviz
               │              │
             CUDA           CUDA
~~~

此时问题从“函数怎么调用”变成：

~~~text
Preprocess 什么时候 READY？
Inference 的输入到了没有？
下游还有没有空 slot？
Allocator 还有没有可用 Buffer？
哪个 worker thread 执行？
~~~

这就是 runtime 出现的动机。

## Holoscan 可以先压成四层

~~~text
┌──────────────────────────────┐
│ Graph / Semantic Layer       │
│ Fragment, Operator, IOSpec   │
├──────────────────────────────┤
│ Scheduling Layer             │
│ Condition, Scheduler, Pool   │
├──────────────────────────────┤
│ Data / Memory Layer          │
│ Connector, Allocator, CUDA   │
├──────────────────────────────┤
│ Transport / OS Layer         │
│ UCX, pthread, Linux, NIC     │
└──────────────────────────────┘
~~~

### Graph 层只回答“谁依赖谁”

`add_flow(A, B)` 表示逻辑数据依赖。

它没有承诺 A/B 各占一个线程、B 紧跟 A 执行，也没有承诺 payload 一定发生一次 copy。

### Scheduling 层回答“谁现在可以执行”

Operator 不因为存在于图里就能运行。

它可能同时受 MessageAvailable、DownstreamMessageAffordable、Periodic、MemoryAvailable、CUDA event/stream 或外部异步 Condition 约束。

### Data / Memory 层回答“payload 到底在哪里”

一条边可能使用 DoubleBuffer、AsyncBuffer 或 UCX；payload 可能来自 BlockMemoryPool、RMM、StreamOrderedAllocator，也可能本身就是 GPU-resident Tensor。

所以“有一条 flow edge”只描述逻辑依赖，不描述物理搬运。

### OS / Transport 层回答“最终谁获得硬件”

即使 runtime 已经决定 Operator READY，仍然要经历：

~~~text
READY
↓
ready queue
↓
worker wake
↓
Linux runqueue
↓
CPU scheduling
↓
compute()
~~~

GPU 工作还会继续进入 CUDA stream 和 GPU execution。

因此端到端 latency 是多层 scheduler 叠加出来的。

## 一条本地消息真正经过什么

对同一 Fragment，可以先画成：

~~~text
Operator A compute()
↓
OutputContext::emit()
↓
GXF Entity / Tensor component
↓
Transmitter
↓
Receiver queue
↓
MessageAvailableCondition changes
↓
Scheduler reevaluates Operator B
↓
ready queue
↓
worker thread
↓
Operator B compute()
~~~

当 B 在另一个 Fragment：

~~~text
A
↓
UcxTransmitter
↓
serialization / tensor metadata
↓
UCX
↓
UcxReceiver
↓
B
~~~

所以 Holoscan 非常适合继续使用 Communication Foundations 的“数据线 + 控制线 + 所有权线”。

## Operator 不等于线程

这是第一个必须固定的概念。

错误理解：

~~~text
Operator A = thread A
Operator B = thread B
~~~

实际关系更接近：

~~~text
Operator
  ↓ READY
Scheduler
  ↓ selects runnable work
Worker Thread
  ↓
compute()
~~~

同一个 worker 可以先后执行多个 Operator。

所以分析一个 Operator 的 mutable state 时，不能仅凭“它是一个节点”推断线程安全，必须继续确认 scheduler/thread-pool 语义。

## Fragment 也不是 POSIX process 的同义词

Fragment 是图分区和部署边界。

它可以把 Application 拆成：

~~~text
Fragment A
  Source
  Preprocess

Fragment B
  Inference
  Postprocess
~~~

跨 Fragment flow 再由 runtime 具体化为 distributed connection。

因此源码分析要追的是：

~~~text
graph partition
→ connection spec
→ UcxReceiver / UcxTransmitter
→ runtime process / host
~~~

而不是把 Fragment 直接翻译成“进程”。

## Condition 为什么是关键抽象

普通线程池经常是 task submitted → runnable。

Streaming runtime 不够。

一个 Inference Operator 可能需要：

~~~text
input message >= 1
AND
downstream free slot >= 1
AND
allocator free block >= 1
AND
CUDA dependency satisfied
~~~

所以 ready 本身是逻辑谓词：

~~~text
Ready(op) = C1 AND C2 AND ... AND Cn
~~~

Condition 把 queue state、memory capacity、timer、external event、CUDA completion 统一转换成 Scheduler 能理解的执行状态。

这不是 API 糖，而是调度模型的核心。

## 从 Polling 到 Event-Based 为什么是 OS 问题

旧 MultiThreadScheduler 需要周期性检查 waiting Operator。

假设 polling interval 是 Delta：

- Delta 大：ready event 到来后可能等待接近一个 interval；
- Delta 小：polling thread 长时间占 CPU。

EventBasedScheduler 把它换成：

~~~text
state/event changes
→ notify dispatcher
→ reevaluate scheduling state
→ ready queue
~~~

这和 epoll/eventfd、condition variable、interrupt-driven I/O 背后的共同思想一样：

> 没事时不要持续问“有事了吗”，而让状态变化产生 wakeup。

## v4.6.0 已经暴露出工业 Runtime 的典型结构

固定版本官方代码与文档明确提供：

~~~text
per-worker private ready queue
deterministic queue assignment
optional work stealing
internal notification sharding
batch drain
wait-state sharding
worker post-check fast path
CPU pinning
dispatcher pinning
SCHED_FIFO / SCHED_RR
performance counters
~~~

这已经是在直接处理 hot queue contention、load imbalance、wake-up latency、dispatcher bottleneck 和 Linux scheduling jitter。

## 为什么 FlowGraph 的 STL 容器值得单独学

FlowGraph 同时使用：

~~~cpp
std::unordered_map
std::map
std::list
std::set
std::vector
std::optional
std::unordered_set
~~~

例如：

~~~cpp
std::unordered_map<NodeType,
                   std::map<NodeType, EdgeDataType, NodeTypeCompare>>
    succ_;
~~~

外层按 node 快速找 adjacency；内层又要求 deterministic insertion-order traversal。

另外用：

~~~cpp
std::list<NodeType> ordered_nodes_;
std::unordered_map<std::string, NodeType> name_map_;
~~~

分别保存插入顺序和 name index。

这正适合训练“按访问模式选容器”，后面会单独拆。

## 为什么 Allocator 也是通信系统的一部分

如果一帧 GPU Tensor 占 16 MB，60 FPS 就对应约 960 MB/s 的 payload 生成速率。

如果 pipeline 每层都动态分配、复制、释放，allocator 和 memory movement 都会进入主路径。

Holoscan 因而把 Allocator 建模成 Resource，并提供：

- UnboundedAllocator；
- BlockMemoryPool；
- StreamOrderedAllocator；
- RMMAllocator。

更关键的是 MemoryAvailableCondition 可以做到：

~~~text
allocator 资源不足
→ Operator 不进入 READY
~~~

内存容量因此直接参与调度语义，而不是等分配失败后才发现过载。

## 为什么 HoloHub 案例比 Hello World 更重要

本专题会继续拆两个固定 HoloHub 工程。

Endoscopy Tool Tracking：

~~~text
Video source
→ FormatConverter
→ LSTM TensorRT inference
→ ToolTracking postprocess
→ Holoviz
~~~

源码显式配置多个 BlockMemoryPool、CudaStreamPool、可选 AJA/Deltacast/Yuan capture、RDMA 与 TensorRT state。

Ultrasound Segmentation：

~~~text
AJA / Replay
→ format conversion
→ TensorRT inference
→ segmentation postprocess
→ Holoviz
~~~

其配置明确写着：

~~~yaml
input_on_cuda: true
output_on_cuda: true
transmit_on_cuda: true
~~~

这正是研究 Tensor 长时间驻留 GPU 时，Buffer Pool 与 CUDA Stream 如何取代传统 CPU message copy 的好案例。

## 本专题阅读顺序

~~~text
overview
↓
architecture-map
↓
flowgraph-containers
↓
event-based-scheduler
↓
conditions-connectors-backpressure
↓
allocator-cuda-memory
↓
distributed-ucx-runtime
↓
HoloHub real cases
~~~

每篇都继续问：

~~~text
owner 是谁？
容器是什么？
哪个线程修改？
哪里阻塞？
谁负责 wakeup？
payload 在哪个 memory domain？
资源什么时候可复用？
过载以后发生什么？
shutdown 怎样收束？
~~~

这才是 Holoscan 对具身智能真正有价值的部分。
