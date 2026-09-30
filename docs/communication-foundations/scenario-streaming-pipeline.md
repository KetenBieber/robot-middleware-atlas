# 场景设计二：高频 Sensor → 慢速 Perception，怎样避免“系统还在跑但看到过去”

## 场景

~~~text
Camera: 60 FPS
   ↓
Perception: 25 FPS
~~~

这是机器人视觉里最典型的生产速率 > 消费速率。

真正的问题不是 queue API，而是：**过载以后要保什么，丢什么，如何限制 Data Age。**

---

## Naive 方案：Unbounded FIFO

~~~cpp
std::queue<Frame> frames;
~~~

Producer 每秒 +60，Consumer 每秒 -25。

净积压：

~~~text
35 frames/s
~~~

10 秒就是 350 frames。

如果每帧间隔约 16.7 ms，Consumer 已经看到数秒前的环境。

这是一类危险故障：

~~~text
程序活着
CPU/GPU 还在算
没有 crash
但信息已经过期
~~~

---

## 第一约束不是“无损”，而是 Max Data Age

实时感知通常应该先定义：

~~~text
max acceptable age = A_max
~~~

而不是：

~~~text
queue capacity = 100
~~~

如果 60 FPS：

~~~text
100 frames ≈ 1.67 s history
~~~

对避障/控制可能已经不可接受。

所以容量必须从时间语义推导。

---

## 候选 Overload Policy

### Block Producer

~~~text
Perception 慢
→ Camera producer 被堵住
~~~

优点：不丢。

风险：capture/driver 线程可能不能阻塞，硬件 buffer 仍会溢出。

### Drop New

保留 backlog，拒绝新帧。

这会继续处理旧世界，通常不适合实时视觉。

### Drop Old

队列满时丢最旧帧。

目标是让 Consumer 尽量追近现在。

### Latest Only

始终只有最新一帧。

适合只关心当前观察、不需要 temporal continuity 的阶段。

### Sample / Decimate

例如 60 FPS 只送 20 FPS 到昂贵模型。

这是主动 rate adaptation，而不是被动 overflow。

---

## Queue Capacity 应从延迟预算反推

假设允许 queueing delay 最多 50 ms，Camera 60 FPS。

帧周期约：

~~~text
16.7 ms
~~~

理论上 queue 中最多只应容纳约 3 帧量级，而不是 100。

实际还要结合：

~~~text
processing jitter
burst
GPU in-flight
capture buffering
~~~

但核心原则不变：

> **容量是时延预算的结果，不是拍脑袋的常量。**

---

## Pool Size 又是另一套容量

Queue depth 不等于 Buffer Pool depth。

一帧可能已经从 queue pop 出，但 GPU kernel 仍在使用对应 Buffer。

所以总在途资源约等于：

~~~text
queued frames
+
currently executing frames
+
GPU/NIC in-flight frames
+
downstream-held frames
~~~

Holoscan 的 BlockMemoryPool 正好把这种资源显式出来。

---

## Little's Law 可以给 Pool 一个第一估算

假设：

~~~text
arrival rate λ = 60/s
GPU stage latency W = 25 ms
~~~

平均 in-flight：

~~~text
L ≈ λ × W
  ≈ 1.5
~~~

只配 1 block 几乎肯定太紧。

再加 jitter、fan-out、下游持有，实际通常需要更高裕量。

---

## 为什么 Backpressure 最好发生在昂贵计算之前

错误路径：

~~~text
run expensive inference
↓
try publish
↓
downstream full
↓
drop result
~~~

GPU 工作已经白做。

更合理：

~~~text
check downstream capacity
AND
check allocator capacity
↓
only then schedule inference
~~~

Holoscan 的 DownstreamMessageAffordableCondition 与 MemoryAvailableCondition 就是在做这种调度级 backpressure。

---

## Scheduler 也会制造 Queueing Delay

即使数据 queue 很短，ready Operator 还可能在 worker queue 里等。

完整 Data Age：

~~~text
capture age
+ queue wait
+ scheduler ready wait
+ CPU dispatch
+ GPU execution
+ downstream hold
~~~

所以 profile 只测模型 inference time 会严重低估系统延迟。

---

## 工业案例：Holoscan Endoscopy

真实 HoloHub pipeline 使用：

~~~text
Video source
→ FormatConverter
→ TensorRT/LSTM
→ Postprocess
→ Holoviz
~~~

并为不同 stage 配独立 BlockMemoryPool 和共享 CudaStreamPool。

RDMA 开关甚至会改变 source num_blocks。

这说明 transport path 会改变 in-flight storage requirement。

详见： [Endoscopy Tool Tracking](../generated/holoscan/case-study-endoscopy-tool-tracking.md)。

---

## 工业案例：DDS History 为什么不是无限缓存

DDS 的 History、ResourceLimits、Reliability 本质上也是在回答：

~~~text
最多保留多少历史？
Reader 慢了以后 Writer 怎么办？
可靠性和资源上限如何共同作用？
~~~

“Reliable”从来不意味着“无限内存保存全部历史”。

---

## 一个合理的视觉 Pipeline 设计

~~~text
Camera driver
  bounded capture buffers
      ↓
SPSC ring capacity 2~4
  drop-old / overwrite by semantics
      ↓
Preprocess
  fixed GPU pool
      ↓
Inference
  schedule only when output capacity exists
      ↓
Latest result / small bounded queue
~~~

如果 recorder 需要完整历史，应单独走独立存储支路，而不是逼主实时链无限排队。

---

## 该记录什么指标

至少：

~~~text
capture timestamp
queue depth
oldest data age
drop count
pool free blocks
READY-to-run latency
GPU execution latency
end-to-end p50/p99/max
~~~

FPS 只是其中一个指标。

---

## 什么时候应该换方案

~~~text
如果业务要求每帧都处理
→ 不能简单 drop-old，需要降输入 rate / 增算力 / admission

如果 temporal model 依赖连续帧
→ latest-only 可能破坏模型语义，需要 sampling/window

如果 recorder 必须无损
→ 与实时 AI branch 分离，独立 queue/storage

如果跨主机传大 Tensor
→ 继续进入 UCX/RDMA/device data plane
~~~

深入机制：

- [Queues & Backpressure](queues-backpressure.md)
- [Concurrent Queues](concurrent-queues-progress.md)
- [Heterogeneous Memory](heterogeneous-memory.md)
