# 实际案例：HoloHub Endoscopy Tool Tracking 的 GPU Streaming Runtime

案例源码基线：HoloHub `6817922b43f97d4d6f548dd255dcd4d59952a69c`。

Holoscan SDK 机制基线：v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

这个案例不是拿来学习“如何调用 TensorRT”。

真正值得看的，是一条医疗视频 AI pipeline 怎样把采集源、RDMA、GPU preprocess、LSTM inference、postprocess、visualization、recording、Buffer Pool 与 CUDA Stream 组织在同一个实时数据流里。

## 先画完整主链

Replay 模式下最核心路径：

~~~text
VideoStreamReplayer
        ↓
FormatConverter
        ↓
LSTM TensorRT Inference
        ↓
ToolTracking Postprocessor
        ↓
Holoviz / VTK
~~~

如果换成 AJA、Deltacast 或 Yuan，Source 变成实时采集设备；如果开启 overlay/recording，还会增加额外 branch。

这说明生产 pipeline 不是一条直线，而是：

~~~text
capture
├── AI branch
├── visualization branch
├── recorder branch
└── optional overlay feedback
~~~

fan-out 会直接影响 buffer lifetime。

## Source 为什么有多种实现

代码支持：

~~~text
replayer
aja
yuan
deltacast
~~~

这不是 UI 选择而已。

不同 source 对 memory path 的影响完全不同。

Replay 常从文件/host path 进入；capture card 则可能直接提供 DMA/RDMA-capable buffer。

所以同一个算法图，入口 memory domain 可能不同：

~~~text
file → CPU/system/pinned → GPU
vs
capture device → DMA/RDMA-capable path → GPU
~~~

## 为什么 source_block_size 是明确公式

AJA/Deltacast/Yuan 分支会根据 width/height 算：

~~~cpp
source_block_size = width * height * 4 * 4;
~~~

具体含义要结合 operator 输出格式理解，但工程思想很清楚：**pool block 不是拍脑袋写一个 64MB，而是由最大 frame representation 推导。**

Replay 分支也根据固定视频尺寸建立自己的 block size。

这和实时控制里根据最大 packet/frame size 预分配 buffer 是同一个原则。

## RDMA 为什么会影响 num_blocks

代码：

~~~cpp
source_num_blocks = use_rdma ? 3 : 4;
~~~

这个细节非常有价值。

它说明 transport path 改变以后，pipeline 的 buffer concurrency 需求也会变化。

没有 RDMA 时，额外 staging/copy path 可能需要更多同时在途 buffer；更直接的数据路径能减少一个阶段的 storage pressure。

不要把 RDMA 理解成：

~~~text
同样程序，只是网速更快
~~~

更准确的是：

~~~text
memory movement graph 发生变化
→ in-flight storage requirement 也变化
~~~

## 为什么每个 Stage 都有自己的 BlockMemoryPool

FormatConverter：

~~~cpp
Arg("pool") = make_resource<BlockMemoryPool>(
    "pool", 1, source_block_size, source_num_blocks)
~~~

LSTM inference 在源码里对应 `ops::LSTMTensorRTInferenceOp`。这一步不是一个黑盒“模型节点”，它同时持有 recurrent state、TensorRT engine、output tensor 与自己的 allocator 资源：

~~~cpp
const uint64_t lstm_inferer_block_size = 107 * 60 * 7 * 4;
const uint64_t lstm_inferer_num_blocks = 2 + 5 * 2;
~~~

Postprocessor：

~~~cpp
tool_tracking_postprocessor_num_blocks = 2 * 2;
~~~

为什么不全 app 共用一个巨大 pool？

per-stage pool 带来：

~~~text
容量归因清楚
block size 能贴合 stage output
一个 stage 泄漏/持有过久更容易发现
backpressure 可以局部出现
~~~

代价是总 reserved memory 可能更多、不同 stage 间不能自动借空闲 block。

这是 isolation 与 utilization 的交换。

## LSTM Pool 的 block 数为什么明显更多

这个 Operator 不只是单输入单输出。

配置里有：

~~~text
cellstate_in
hiddenstate_in
source_video
~~~

输出又包括：

~~~text
cellstate_out
hiddenstate_out
probs
scaled_coords
binary_masks
~~~

而 LSTM state 还要跨 tick 保留。

因此 pool 不能按“每 tick 一个输出”估算。

这是很典型的 Stateful Operator：

~~~text
pipeline concurrency
+
persistent recurrent state
→ memory requirement
~~~

## CudaStreamPool 为什么全链共享

代码创建：

~~~cpp
make_resource<CudaStreamPool>(
    "cuda_stream", 0, 0, 0, 1, 5);
~~~

随后 FormatConverter、LSTM inference、Holoviz 等共享这一个 stream pool。

这与“每个 Operator 自己 cudaStreamCreate”相比有几个好处：

- stream 资源集中管理；
- 最大并发流数量显式；
- 更方便做 stream propagation；
- 避免 operator lifecycle 与 CUDA stream lifetime 紧耦合。

`reserved_size=1`、`max_size=5` 也说明 stream 并发本身被当作可配置资源。

## 一个 Operator Graph 里为什么需要多个 CUDA Stream

如果全部 work 都放同一 stream：

~~~text
preprocess
→ infer
→ postprocess
→ render
~~~

天然串行。

当不同 branch 没有数据依赖时，多 stream 才可能让 GPU overlap。

但 stream 越多不一定越快。

还要看：

~~~text
GPU SM occupancy
copy engine
kernel resource usage
CUDA_DEVICE_MAX_CONNECTIONS
dependency events
~~~

所以 CudaStreamPool 是“允许并发”，不是“保证并发”。

## Fan-out 为什么会延长 Buffer Lifetime

Source 可能同时连到：

~~~text
FormatConverter
Holoviz
Recorder
Overlay path
~~~

如果它们 zero-copy 共享同一 underlying frame，Buffer 只有在所有 consumer 都放弃引用后才能回 pool。

于是：

~~~text
slow recorder
可能拖住
fast inference branch 的 buffer reuse
~~~

这就是 fan-out reference lifetime 问题。

在共享内存中它表现为 refcount；在 shared_ptr Tensor 中同样如此。

## Recorder 为什么经常需要额外 FormatConverter

Recorder 需要的像素格式可能与 inference/visualization 不同。

于是源码创建 recorder_format_converter 和独立 BlockMemoryPool。

这说明“旁路功能”不是免费的。

一条 recorder branch 会增加：

~~~text
一次 format conversion
一组 output buffer
一条 queue
一次磁盘 I/O
更多 buffer hold time
~~~

真实部署如果发现主链 latency 抖动，logging/recording branch 必须纳入分析。

## Overlay 为什么会形成反馈 Edge

Deltacast overlay 模式可能出现：

~~~text
source
→ AI/visualizer
→ render buffer
→ hardware transmitter/source overlay path
~~~

这不再是纯 DAG 直线。

反馈边会让生命周期和 shutdown 更复杂：

~~~text
谁是 root？
什么时候可以停止 source？
render buffer 是否仍在 hardware 使用？
~~~

图运行时必须比单纯 callback chain 更认真处理这些关系。

## 配置文件里的 RDMA 开关其实改变 Data Plane

YAML：

~~~yaml
external_source:
  rdma: true
~~~

这类配置不能只被理解成“performance=true”。

正确分析应该画：

~~~text
capture card
↓ DMA/RDMA?
host staging?
↓
GPU tensor
~~~

然后测：

~~~text
copy count
host memory bandwidth
CPU utilization
buffer slots
latency p99
~~~

## 这条 Pipeline 最容易出现哪些 Backpressure

### Capture faster than inference

~~~text
source queue grows
or
source blocks/drops
~~~

### Inference pool exhausted

前一批 GPU work 未完成，blocks 还没归还，新 tick 无法获得 buffer。

### Visualization slower

fan-out buffer 被 Holoviz 持有更久。

### Recorder I/O slower

旁路 branch 形成历史 backlog。

### CUDA stream pool exhausted

异步工作过多，无法继续分配独立 stream。

因此“实时性”不是只测 TensorRT inference time。

## 一个更有价值的 Profiling 表

建议为每一 stage 记录：

| Stage | Queue Age | Pool Free | CPU wait | GPU stream | p99 |
| --- | ---: | ---: | ---: | --- | ---: |
| Source | frame age | source blocks | capture wait | capture/none | ... |
| Converter | input age | converter blocks | scheduler wait | Sx | ... |
| Inference | tensor age | infer blocks | worker wait | Sy | ... |
| Postprocess | output age | post blocks | worker wait | Sz | ... |
| Holoviz | render age | render blocks | display wait | Sr | ... |

这样才能区分：

~~~text
算法慢
资源池太小
线程没拿到 CPU
还是下游 branch 拖住 buffer
~~~

## 对机器人视觉 Pipeline 的直接迁移

例如：

~~~text
RealSense / CSI camera
↓
GPU preprocess
↓
VLM encoder
↓
world model
↓
policy
~~~

完全可以借用相同设计：

~~~text
per-stage fixed pool
shared CudaStreamPool
explicit queue policy
stream-aware lifetime
optional recorder branch isolated
capture DMA path modeled separately
~~~

## 这个案例真正教的不是医疗 AI

它教的是一条真实 GPU streaming application 怎样把：

~~~text
graph topology
buffer capacity
GPU async execution
capture transport
fan-out
recording
visualization
~~~

变成同一个 runtime resource problem。

如果只看模型本身，就会漏掉真正决定工程稳定性的半个系统。
