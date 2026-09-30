# Allocator、CUDA Stream 与 Tensor 生命周期：GPU Pipeline 为什么不能只看显存够不够

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

在 CPU 单线程程序里，内存管理很容易被理解成：

~~~text
new/malloc
↓
use
↓
delete/free
~~~

GPU streaming runtime 里，这个模型不够。

一个 `compute()` 返回时，GPU kernel 可能还在使用它刚刚处理的 Tensor。此时 CPU 侧对象生命周期与 GPU 侧真实访问生命周期已经分叉。

这正是 Holoscan 把 Allocator、CudaStreamPool、CUDA event/stream metadata 都提升为 runtime Resource 的原因。

## 先从最危险的错误实现开始

~~~cpp
void compute() {
    void* p = cudaMalloc(...);
    kernel<<<..., stream>>>(p);
    cudaFree(p);
}
~~~

CUDA kernel launch 是异步的。CPU 调用返回，并不意味着 GPU 已经停止访问 `p`。

所以必须区分：

~~~text
CPU API lifetime
!=
GPU access lifetime
~~~

这与 UCX request、DMA buffer、共享内存 loan 的本质完全一样：**调用结束不等于异步使用结束。**

## Holoscan 的 Allocator 层次在解决什么

固定 SDK 暴露：

- `Allocator`：通用同步 allocate/free；
- `CudaAllocator`：增加 `allocate_async/free_async` 与 pool size；
- `BlockMemoryPool`：固定大小、固定块数；
- `StreamOrderedAllocator`：CUDA stream-ordered allocation；
- `RMMAllocator`：基于 RAPIDS memory manager 的 device + pinned-host pool；
- `UnboundedAllocator`：方便原型验证的动态分配。

它们并不是“几种 malloc 替代品”，而是在不同 workload 假设下提供不同资源语义。

## BlockMemoryPool：把容量从隐含变成显式

固定源码参数：

~~~cpp
storage_type
block_size
num_blocks
dev_id
~~~

并且明确：一次 allocation 如果小于 block_size，仍然占用完整 block；超过一个 block 则不能由该 block 满足。

可以把 pool 画成：

~~~text
slot 0  [ block_size bytes ]
slot 1  [ block_size bytes ]
slot 2  [ block_size bytes ]
...
slot N
~~~

这带来两个重要性质。

第一，运行时最大内存占用基本可以提前估算：

~~~text
pool capacity ≈ block_size × num_blocks
~~~

第二，过载不会被“再 malloc 一点”掩盖。

~~~text
all blocks in use
→ allocation unavailable
→ MemoryAvailableCondition can hold Operator
~~~

这比无限 heap growth 更适合实时系统。

## 为什么固定块会浪费内存，却仍然值得

假设每帧实际只需 6 MB，但 block_size 配成 8 MB。

四个 in-flight buffer：

~~~text
实际 payload: 24 MB
pool reservation: 32 MB
~~~

看起来浪费 8 MB。

但换来的东西是：

- allocation 时间更稳定；
- 不需要每帧 cudaMalloc/cudaFree；
- 不容易产生通用 allocator fragmentation；
- capacity 显式；
- exhaustion 可直接转化为 backpressure。

实时系统经常愿意用空间换可预测性。

## Storage Type：Host 与 System 不是一回事

BlockMemoryPool 支持：

~~~text
kHost
kDevice
kSystem
kCudaManaged
~~~

固定 header 特别说明：

~~~text
Host   = pinned CPU memory (cudaMallocHost)
System = ordinary C++ new memory
Device = CUDA device memory
Managed = CUDA managed memory
~~~

因此“都在 CPU RAM”并不代表性能等价。

Pinned host memory 允许 GPU/NIC DMA 更直接地访问，但它是更昂贵、更有限的系统资源。

所以分析 memory pool 必须把 memory domain 写清楚，不能只记一个 byte size。

## StreamOrderedAllocator：为什么 allocate/free 也要进 CUDA Stream

传统 allocator：

~~~text
CPU calls allocate
CPU calls free
~~~

但 GPU 的真实依赖关系存在 stream 中。

StreamOrderedAllocator 使用 CUDA stream-ordered allocation，使：

~~~text
allocation
kernel use
free/reuse
~~~

都能按照 stream dependency 排序。

固定 wrapper 参数包括：

~~~text
device_memory_initial_size
device_memory_max_size
release_threshold
dev_id
~~~

`release_threshold` 控制 pool 保留多少 memory 才尝试归还底层系统。

这体现 allocator 的另一层 tradeoff：

~~~text
cache more memory
→ future allocation faster
→ resident memory higher

release aggressively
→ resident memory lower
→ more allocation/release overhead
~~~

## RMMAllocator：为什么同时有 Device Pool 和 Pinned Host Pool

真实 GPU pipeline 不只需要 device memory。

还常有：

~~~text
camera/CPU input
↓
pinned staging memory
↓ H2D
device tensor
~~~

RMMAllocator 因而维护 device pool 与 pinned-host pool 两类资源。

这和 UCX Memory Domain 的思想非常接近：

> 不同 memory domain 需要不同 allocator 与访问路径。

## CudaStreamPool：Stream 本身也是有限资源

固定 `CudaStreamPool` 参数：

~~~text
dev_id
stream_flags
stream_priority
reserved_size
max_size
cuda_green_context
nvtx_identifier
~~~

它内部创建的 GXF CudaStream 最终来自 `cudaStreamCreateWithPriority`。

`reserved_size` 是初始预留数，`max_size` 是最大 pool size；0 表示 runtime 层不人为设上限，但硬件并发能力仍然有限。

因此 CUDA stream 也不能被理解成“无限免费的轻量线程”。

## Stream Priority 与 CPU RT Priority 是两套调度系统

CPU 侧：

~~~text
Linux thread priority
SCHED_FIFO / RR
CPU affinity
~~~

GPU 侧：

~~~text
CUDA stream priority
GPU scheduler
kernel dependencies
~~~

它们互不等价。

即使一个 Operator 在高优先级 CPU thread 上很快 launch kernel，也不代表该 kernel 一定立刻抢占所有 GPU work。

完整实时分析必须同时画 CPU 和 GPU execution timeline。

## receive_cuda_stream 做的不是“拿到上游 stream 然后同步 CPU”

官方 v4.6.0 文档描述的推荐模式是：

~~~text
receive input
↓
receive_cuda_stream()
↓
get/reuse operator internal stream
↓
record CUDA event on upstream stream
↓
cudaStreamWaitEvent on internal stream
↓
launch this operator work
~~~

关键是：

~~~text
cudaEventRecord
cudaStreamWaitEvent
~~~

都不需要阻塞 CPU 等 GPU 完成。

CPU 可以继续返回 scheduler，真正依赖由 GPU stream graph 保证。

## 为什么不用 cudaDeviceSynchronize

如果每个 Operator 都：

~~~cpp
cudaDeviceSynchronize();
~~~

整个 pipeline 会被强制串行化：

~~~text
CPU waits GPU A
↓
launch B
↓
CPU waits GPU B
~~~

而 stream/event 可以做到：

~~~text
CPU quickly queues work

GPU:
A work ──event──>
               B waits → B work
~~~

CPU thread 可以同时去执行别的 Operator。

这正是 GPU streaming runtime 和普通同步函数链最大的差异。

## OutputContext::emit() 为什么要知道 CUDA Stream

固定 `io_context.cpp` 在发出 Tensor 时会取得 output stream。

源码注释直接说明：

~~~text
stream-aware deallocation
allows allocators like BlockMemoryPool
to defer memory reuse
until GPU operations complete
~~~

也就是说 stream metadata 同时承担两个职责：

~~~text
1. 告诉下游 GPU dependency
2. 告诉 allocator 最后谁还在使用 memory
~~~

这就是 ownership 与 synchronization 合流的地方。

## shared_ptr<Tensor> 的 zero-copy 为什么会制造新的 race

官方 CUDA stream 文档明确提醒：Tensor emit 可以通过 `shared_ptr<Tensor>` 共享同一底层 memory。

例如 diamond graph：

~~~text
        B
      ↗
A ───
      ↘
        D
~~~

B 和 D 都拿到同一 underlying Tensor memory。

如果两边都 read-only，没有问题。

如果 B 原地修改：

~~~text
B writes tensor
D reads tensor
~~~

就会形成数据竞争。

所以 zero-copy 的真正含义是：

> copy 被省掉以后，ownership/read-write discipline 必须更严格。

这是所有 zero-copy middleware 的共同规律。

## Sink Operator 是一个非常容易忽视的生命周期陷阱

Producer/transform Operator 通常还会 emit output，因此 runtime 可以把当前 stream metadata 继续传播。

Sink 不 emit。

假设：

~~~text
upstream Tensor last marked with stream A
↓
sink receives
↓
sink launches async kernel on stream B
↓
sink compute() returns
↓
Tensor refcount reaches zero
↓
allocator thinks A is last user
↓
memory reused
↓
stream B kernel still reading
~~~

这就是典型 use-after-recycle。

官方文档要求 sink 在这类情况下显式设置 deallocation stream，例如：

~~~cpp
tensor->set_deallocation_stream(cuda_stream);
~~~

这一步本质上是在更新：

~~~text
Buffer last-use fence
~~~

与 DMA fence、Vulkan fence、UCX completion 的作用完全同类。

## 为什么异步 GPU 会让 Pool 需求比你直觉更大

假设每个 tick 逻辑上“只需要一个输出 Tensor”。

同步程序里：

~~~text
tick0 allocate
work done
free
tick1 allocate
~~~

所以一个 block 似乎够了。

异步 GPU 中：

~~~text
tick0 launch GPU using block0
compute returns

tick1 already scheduled
needs another output
but block0 still in use
~~~

所以至少可能需要 block1。

官方文档明确提醒：异步执行可能使 pool 需要 2x 或更多 in-flight capacity。

Buffer 数量应该从**pipeline concurrency**推导，而不是从单次函数局部变量数量推导。

## 一个简单 Pool Size 推导

假设：

~~~text
input rate = 60 FPS
GPU stage latency = 25 ms
~~~

Little's Law 的直觉给出平均 in-flight：

~~~text
L ≈ lambda × W
  ≈ 60/s × 0.025s
  ≈ 1.5
~~~

所以只配 1 block 显然危险；2 是理论下界附近，实际还要给 jitter、下游持有和 pipeline overlap 留余量。

因此 pool capacity 应该和：

~~~text
arrival rate
stage latency
fan-out
downstream hold time
jitter
~~~

一起估算。

## HoloHub Endoscopy 是非常真实的 Pool 配置案例

固定 HoloHub case 会计算：

~~~cpp
source_block_size = width * height * ...;
source_num_blocks = use_rdma ? 3 : 4;
~~~

并给不同 stage 单独建 BlockMemoryPool。

LSTM inference 的 pool block 数还显式包含多个 state/output buffer。

这说明真实工程不是：

~~~text
给整个 app 一个巨大 GPU allocator
~~~

而常常是：

~~~text
per-stage pool
明确 block size
明确 num_blocks
明确 sharing
~~~

这样更容易定位究竟哪一段耗尽。

## HoloHub Ultrasound 又展示另一种 GPU-resident Pipeline

其 inference 配置明确：

~~~yaml
input_on_cuda: true
output_on_cuda: true
transmit_on_cuda: true
~~~

并且 preprocess/inference/postprocess 分别有自己的 BlockMemoryPool。

这让大部分中间 Tensor 不需要因为 Operator 边界回 CPU。

Operator 边界是软件边界，**不是 memory-domain 边界**。

这句话对具身大模型尤其重要。

## MemoryAvailableCondition 如何把 allocator 和 scheduler 接起来

只靠 pool 还不够。

如果 pool 没 block 时才在 compute 内失败，系统已经开始执行一次无效工作。

MemoryAvailableCondition 可以提前表达：

~~~text
free blocks >= K
or
free bytes >= B
~~~

不满足则 Operator 不 READY。

于是：

~~~text
memory pressure
→ scheduling pressure
~~~

这就是异构 runtime 中的真正 backpressure。

## 这套思路怎样迁移到 VLA/具身数据面

可以设计一个统一 BufferHandle：

~~~cpp
struct BufferHandle {
    MemoryDomain domain;
    DeviceId device;
    void* address_or_handle;
    size_t bytes;
    Generation generation;
    CompletionFence last_use;
};
~~~

Pool 管理 storage，queue 传 handle，Scheduler 看 resource condition，GPU/NIC completion 更新 fence。

这样：

~~~text
CPU image
GPU feature
NPU tensor
RDMA registered buffer
~~~

都能放进同一个 ownership 模型。

## 本篇真正要记住的不是 Allocator 类名

而是五条原则：

1. 异步调用返回，不等于 Buffer 已经没人使用。
2. Pool capacity 是 pipeline concurrency 的一部分。
3. Memory domain 决定可访问者和传输路径。
4. CUDA stream/event 不只是性能工具，也是 lifetime fence。
5. allocator exhaustion 应该向 Scheduler 传播成 backpressure，而不是等 malloc/cudaMalloc 失败后处理。
