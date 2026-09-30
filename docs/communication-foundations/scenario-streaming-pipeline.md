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
## 先建立四个基础量：Rate、Latency、Queueing Delay、Data Age

这类场景里最容易把“模型推理 25 ms”和“系统延迟 25 ms”混为一谈，其实完全不是一回事。

### Producer Rate / Consumer Rate

Camera 60 FPS 意味着平均到达间隔约：

~~~text
T_p = 1 / 60 s ≈ 16.7 ms
~~~

Perception 25 FPS 意味着平均服务时间约：

~~~text
T_c = 1 / 25 s = 40 ms
~~~

因为 40 ms > 16.7 ms，如果每一帧都必须进入同一个 FIFO，那么 Consumer 从长期平均意义上一定追不上 Producer。这不是优化问题，而是数学上必然积压。

### Processing Latency

某一帧真正执行算法花多久，例如 TensorRT inference = 24 ms。

### Queueing Delay

一帧在真正开始处理前，可能已经在 queue 里等待 80 ms。即使模型本身只跑 24 ms，端到端也至少已经 104 ms。

### Data Age

对机器人更重要的是：Consumer 真正使用这帧时，距离传感器采样已经过去多久。

~~~text
Data Age
=
capture buffering
+ queueing delay
+ scheduler delay
+ processing latency
+ downstream holding
~~~

所以“系统没 crash”远远不够。

---

## 什么叫 Backpressure

Backpressure 不是“队列满了”的同义词。

> **Backpressure 是下游处理能力不足时，把“我现在接不动更多工作”这个事实向上游传播。**

例如 Perception Queue 满时，系统可以选择：

~~~text
1. 阻塞 Camera
2. 拒绝新 Frame
3. 丢旧 Frame
4. 只保留最新
5. 降采样
6. 暂停调度昂贵算子
~~~

这些才是 backpressure policy。Queue capacity 只是触发它的一种状态量。

## Bounded Queue 与 Unbounded Queue

`std::queue` 默认没有容量上限。所谓 unbounded queue，不是“内存真的无限”，而是程序没有定义容量与过载策略，直到 allocator / OS 替你失败。

Bounded queue 则明确 `capacity=N`；达到 N 后必须决定 block、drop、overwrite 还是 reject。

实时系统最危险的往往不是显式报错，而是**延迟不断增长但表面仍然正常**。

---

## 一个可以直接运行的 Drop-Old Queue

下面模拟 Producer 每 10 ms 一帧、Consumer 每 40 ms 一帧、Queue capacity=3。队列满时丢最旧帧。

编译运行：

~~~text
g++ -std=c++17 -O2 -pthread drop_old_demo.cpp -o drop_old_demo
./drop_old_demo
~~~


~~~cpp
#include <chrono>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <thread>

struct Frame {
    int id;
    std::chrono::steady_clock::time_point created;
};

class DropOldQueue {
public:
    explicit DropOldQueue(std::size_t capacity)
        : capacity_(capacity) {}

    void push(Frame f) {
        {
            std::lock_guard<std::mutex> lock(m_);
            if (q_.size() == capacity_) {
                std::cout << "drop old frame "
                          << q_.front().id << "\n";
                q_.pop_front();
            }
            q_.push_back(std::move(f));
        }
        cv_.notify_one();
    }

    Frame pop() {
        std::unique_lock<std::mutex> lock(m_);
        cv_.wait(lock, [&] { return !q_.empty(); });
        Frame f = std::move(q_.front());
        q_.pop_front();
        return f;
    }

private:
    std::size_t capacity_;
    std::mutex m_;
    std::condition_variable cv_;
    std::deque<Frame> q_;
};

int main() {
    DropOldQueue q(3);

    std::thread producer([&] {
        for (int i = 0; i < 20; ++i) {
            q.push(Frame{i, std::chrono::steady_clock::now()});
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    });

    std::thread consumer([&] {
        for (int i = 0; i < 8; ++i) {
            Frame f = q.pop();
            auto age = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - f.created).count();
            std::cout << "consume frame " << f.id
                      << " age=" << age << " ms\n";
            std::this_thread::sleep_for(std::chrono::milliseconds(40));
        }
    });

    producer.join();
    consumer.join();
}
~~~

这里 mutex、condition_variable、deque 都属于同一个 `DropOldQueue` 对象。和普通 FIFO 的关键区别是：**容量有界 + overflow policy 明确**。

---

## Little's Law 到底在说什么

常见公式：

~~~text
L = λW
~~~

这里 `λ` 是平均到达率，`W` 是一个 item 平均在系统里停留的时间，`L` 是平均同时在系统中的 item 数。

例如 λ=60/s，W=0.025 s：

~~~text
L = 60 × 0.025 = 1.5
~~~

直觉就是：流量越大、每一项停留越久，同时在途的项自然越多。

这个 L 不只是 queue length，它可能包含 queue 中、CPU 正在处理、GPU in-flight、DMA in-flight、下游仍持有 buffer 的所有对象。因此用 Little's Law 估算 Buffer Pool 时，必须先定义“系统边界”。

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

其中 jitter 是“同一个阶段的处理时间或到达间隔会抖动而不是恒定”；burst 是“短时间内集中到达一批数据”；in-flight 表示对象已经离开 Queue、但仍被 CPU/GPU/NIC 或下游持有，暂时不能回收。

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

## Pool 容量继续沿用前面的 Little's Law

前面已经从 `L = λW` 推导过平均 in-flight。把系统边界扩大到 GPU/NIC 和下游持有以后，`W` 也必须包含这些阶段；实际 Pool 还要为 jitter、burst 和 fan-out 留出裕量。这里不再重复一次公式推导。

fan-out 指同一份输入同时送往多个下游，例如一帧图像同时进入 Perception、Recorder 和 Visualizer；最慢的分支可能延长 Buffer 生命周期。

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
→ 不能简单 drop-old，需要降输入 rate / 增算力 / admission control

如果 temporal model 依赖连续帧
→ latest-only 可能破坏模型语义，需要 sampling/window

如果 recorder 必须无损
→ 与实时 AI branch 分离，独立 queue/storage

如果跨主机传大 Tensor
→ 继续进入 UCX/RDMA/device data plane

admission control 的意思是“在工作进入昂贵阶段之前先判断系统是否还有容量”，容量不足就拒绝、延迟或限流，而不是先把任务塞进去再让 Queue 无限增长。
~~~

深入机制：

- [Queues & Backpressure](queues-backpressure.md)
- [Concurrent Queues](concurrent-queues-progress.md)
- [Heterogeneous Memory](heterogeneous-memory.md)
