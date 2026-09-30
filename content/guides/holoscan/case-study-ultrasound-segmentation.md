# 实际案例：Ultrasound Segmentation——GPU Residency、有限 Pool 与 Fan-out

案例源码基线：HoloHub `6817922b43f97d4d6f548dd255dcd4d59952a69c`。

Holoscan SDK 机制基线：v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

这个案例比 Endoscopy 更适合看一个问题：**如果输入、推理输出和 Operator 间传输都尽量留在 CUDA device memory，软件 Graph 的每一条边还意味着一次 CPU copy 吗？**

答案是否定的。

这也是现代 embodied/VLM/VLA 数据面需要建立的基本直觉：

~~~text
software stage boundary
!=
memory-domain boundary
~~~

## 先画主链

Replay 模式：

~~~text
VideoStreamReplayer
        ├──────────────→ Holoviz
        │
        └→ Segmentation Preprocess
              ↓
           Inference
              ↓
        Segmentation Postprocess
              ↓
           Holoviz
~~~

AJA 实时采集模式还会增加一个 drop-alpha FormatConverter：

~~~text
AJA Source
  ├──────────────→ Holoviz
  └→ Drop Alpha
        ↓
     Preprocess
        ↓
     Inference
        ↓
   Postprocess
        ↓
      Holoviz
~~~

一开始就能看到一个重要事实：Source 输出会 fan-out 到 visualization 与 AI branch。

## Fan-out 让“引用数”成为数据面的一部分

如果 Source frame 通过共享引用被两个 branch 消费：

~~~text
Frame F
├── visualizer owns reference
└── preprocess owns reference
~~~

这个 buffer 只有在两边都释放以后才真正可复用。

因此主 AI branch 即使很快，慢 visualization 仍可能延长 Source buffer lifetime。

这就是 fan-out 系统中常见的 hidden coupling。

在共享内存里它表现为 loan/refcount；在 C++ shared_ptr Tensor 中是引用计数；在 GPU pool 中最终体现为 block 长时间不归还。

## AJA 分支为什么先单独做 Drop Alpha

AJA 输入是 1920×1080、4 channel。

代码显式计算：

~~~cpp
const int width = 1920;
const int height = 1080;
const int n_channels = 4;
const int bpp = 4;

uint64_t drop_alpha_block_size =
    width * height * n_channels * bpp;

uint64_t drop_alpha_num_blocks = 2;
~~~

这不是“随便给 converter 一个 pool”。

它把最坏单帧 output storage 按图像维度直接推导出来，再给两个同时在途 block。

如果一个 block 是：

~~~text
1920 × 1080 × 4 × 4 bytes
≈ 33.2 MB
~~~

两个 block 就已经约 66 MB。

这能直观看出为什么大型图像 pipeline 的 pool 参数不能靠感觉。

## Preprocessor 为什么又是另一套 Pool

Preprocess 使用：

~~~cpp
width_preprocessor = 1264;
height_preprocessor = 1080;
preprocessor_block_size =
    width_preprocessor * height_preprocessor * 4 * 4;
preprocessor_num_blocks = 3;
~~~

它和 drop-alpha 不共用相同 block size。

原因是两个 stage 的最大 output representation 不同。

如果整个应用强行共用一个最大块 allocator：

~~~text
small stage
也被迫占用 huge block
~~~

会造成明显内部碎片。

per-stage pool 用更多管理对象换来更贴近真实 tensor shape 的容量配置。

## Inference Pool 为什么只有 256×256×2×4

推理输出被压缩到：

~~~text
256 × 256 × 2 channels × 4 bytes
~~~

代码：

~~~cpp
const uint64_t inference_block_size =
    256 * 256 * 2 * 4;
const uint64_t inference_num_blocks = 2;
~~~

与原始 1080p frame 相比小很多。

这提醒我们：

> 一条 pipeline 的 bandwidth/memory pressure 不能只按入口帧大小估算；每个 stage 的 tensor shape 都可能改变一个数量级。

## Postprocess 又为什么只要 256×256 bytes

SegmentationPostprocessor 输出最终 mask：

~~~cpp
postprocessor_block_size = 256 * 256;
postprocessor_num_blocks = 2;
~~~

从 1080p RGBA 输入到 256×256 mask，数据规模不断变化。

所以真正正确的 data-plane 图应该在每条 edge 上标：

~~~text
shape
dtype
memory domain
pool
queue capacity
~~~

而不是只画 Operator 名称。

## YAML 中三行 CUDA 配置为什么特别重要

推理配置：

~~~yaml
input_on_cuda: true
output_on_cuda: true
transmit_on_cuda: true
~~~

可以分别理解成：

~~~text
input_on_cuda
  Inference 期望输入已经在 CUDA memory

output_on_cuda
  推理输出留在 CUDA memory

transmit_on_cuda
  向下游发送时继续保持 CUDA-side data
~~~

因此主链不需要：

~~~text
GPU
↓ D2H
CPU message
↓ H2D
GPU
~~~

Operator 边界并不会自动把 tensor 拉回 CPU。

这就是现代 GPU-resident pipeline 最重要的性能来源之一。

## 这和传统 Middleware 思维最大的不同

传统消息抽象很容易让人默认：

~~~text
Publisher owns a serialized CPU buffer
↓
Subscriber gets another CPU object
~~~

GPU pipeline 更合理的是：

~~~text
message metadata
  shape/dtype/stream/id

+

shared/managed device buffer
~~~

真正大的 payload 留在 GPU。

因此以后设计 VLA runtime 时，消息系统应该允许“控制面和 data plane 分离”。

## 一个共享 CudaStreamPool 为什么贯穿多个 Stage

代码：

~~~cpp
auto cuda_stream_pool =
    make_resource<CudaStreamPool>(
        "cuda_stream", 0, 0, 0, 1, 5);
~~~

DropAlpha、Preprocessor、Holoviz 等都可以引用同一个 pool。

这带来一个非常实际的资源模型：

~~~text
Operator count
!=
CUDA stream count
~~~

多个 Operator 可以从统一 stream pool 获取执行流。

所以应用级并发度最终受：

~~~text
worker threads
CUDA streams
GPU hardware
buffer blocks
queue capacity
~~~

共同限制。

只把 worker_thread_number 调大，并不会让 GPU pipeline 无限加速。

## 为什么 Visualizer 使用 UnboundedAllocator，而主链大量使用 BlockMemoryPool

代码里 Holoviz：

~~~cpp
Arg("allocator") =
    make_resource<UnboundedAllocator>("pool")
~~~

而 preprocess/inference/postprocess 都偏向固定 BlockMemoryPool。

这是一处很好的工程对比。

主实时计算链更看重：

~~~text
bounded memory
predictable allocation
clear backpressure
~~~

可视化层更偏向功能完整与弹性，允许动态 allocator。

这并不证明 UnboundedAllocator 适合所有生产部署；恰恰说明不同 branch 的实时要求不同。

如果 display branch 后续变成 hard real-time consumer，allocator 策略也应重新评估。

## UnboundedAllocator 为什么可能掩盖过载

假设 visualizer 变慢。

固定 pool：

~~~text
blocks exhausted
→ pressure immediately visible
~~~

Unbounded allocator：

~~~text
keep allocating
→ memory grows
→ latency/backlog grows
→ only later hit system pressure
~~~

所以“不会 allocation fail”不等于系统更健康。

实时系统真正关心的是：

~~~text
bounded latency
bounded memory
bounded Data Age
~~~

而不是“尽量不报错”。

## Source 同时送 AI 与 Visualization 有什么隐患

逻辑上：

~~~text
Source
├→ Display
└→ AI
~~~

如果两边使用同一 frame reference，而 display branch 在 GPU 上还有异步工作，frame 释放时机取决于：

~~~text
AI done?
Display done?
deallocation stream correct?
~~~

只看 C++ shared_ptr refcount 不够。

因为 CPU refcount 降到 0 时，GPU stream 可能仍在访问底层 memory。

这正是 Holoscan stream-aware deallocation 存在的原因。

## 什么时候会需要第三个 Preprocess Block

为什么 preprocess 配 3 blocks，而不是 1？

异步 pipeline 可以出现：

~~~text
frame N
  GPU preprocess still running

frame N+1
  already scheduled

frame N+2
  arrives / waits
~~~

只要 compute() 返回早于 GPU completion，就可能同时持有多个 output buffer。

因此 pool depth 应按 in-flight concurrency 配，而不是按“函数局部只创建一个 Tensor”配。

## 这条 Pipeline 的 Backpressure 链

可以画成：

~~~text
Source rate
↓
source connector capacity
↓
preprocessor pool blocks
↓
inference input capacity
↓
inference output blocks
↓
postprocess blocks
↓
visualizer consumption
~~~

任何一段变慢，都可能向上游传播。

如果没有 DownstreamMessageAffordable/MemoryAvailable 一类条件，过载往往只会在最末端 allocation 或 queue overflow 时暴露。

更好的 runtime 是尽量把资源不足前移为 scheduling state。

## 一个有意义的性能实验应该怎么做

不要只比较：

~~~text
TensorRT inference = X ms
~~~

至少做四组实验。

### 1. GPU residency on/off

对比 GPU→CPU→GPU staging 与全 CUDA residency。

看：

- CPU utilization；
- PCIe/NVLink traffic；
- latency p50/p99；
- memory bandwidth。

### 2. Pool depth

分别设 1/2/3/4 blocks。

观察：

- allocation stalls；
- dropped frames；
- queue age；
- GPU utilization。

### 3. Visualization branch on/off

检查 fan-out 是否拖长 source buffer lifetime。

### 4. Stream pool size

从 1 到多个 CUDA streams，看 overlap 是否真的增加，而不是只增加调度复杂度。

## 与具身 VLA 的直接对应

把这条医疗影像链换个名字：

~~~text
Camera
↓
Vision Preprocess
↓
Visual Encoder
↓
Feature Postprocess
↓
Policy / Visualization
~~~

核心问题完全一样。

尤其 Visual Encoder 输出可能是大 feature tensor，如果每层都回 CPU，data movement 很可能比模型本身更浪费。

因此 VLA runtime 应优先考虑：

~~~text
GPU-resident tensor
explicit Buffer Pool
stream/event dependency
bounded queues
fan-out lifetime
resource-aware scheduling
~~~

## 本案例最值得带走的四个结论

1. 软件 Operator 边界不等于 memory-domain 边界；GPU Tensor 可以跨多个 stage 保持 device residency。
2. 每个 stage 的 shape/dtype 不同，pool 应按真实输出尺寸和 in-flight 数量设计。
3. fan-out 会延长 buffer lifetime，慢支路可以间接拖住快支路。
4. Unbounded allocation 能掩盖容量问题；真正的实时工程更关心 bounded memory 与 Data Age。
