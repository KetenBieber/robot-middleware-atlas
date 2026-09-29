# 异构内存通信：具身 AI 为什么把“消息传输”升级成 Memory Domain 问题

:::{contents} 本页目录
:depth: 3
:local:
:::

传统机器人中间件通常默认一件事：

~~~text
message payload ≈ CPU-addressable bytes
~~~

但现代具身系统越来越不像这样。

相机数据可能从 DMA buffer 开始，预处理和视觉编码长期留在 GPU，VLA 也在 GPU 上推理，只有最终 action 才回到 CPU 控制环。

于是通信系统真正面对的是：

> 数据如何在不同 Memory Domain 之间移动、共享和同步？

## 先看一个最常见的浪费路径

1920×1080 RGB8 一帧大小约为：

$$
1920 \times 1080 \times 3
=
6{,}220{,}800\ \text{bytes}
$$

约 6.2 MB。

30 FPS 原始数据量约：

$$
6.2 \times 30
\approx
186\ \text{MB/s}
$$

假设 pipeline 是：

~~~text
Camera
→ Rectify
→ Depth
→ Detector
→ Vision Encoder
→ VLA
~~~

如果每一级都走：

~~~text
GPU
→ CPU
→ middleware
→ CPU
→ GPU
~~~

即使单次 copy 看起来不大，整条链也会不断制造：

- PCIe/NVLink 传输；
- staging；
- allocator；
- stream synchronization；
- cache pollution；
- CPU 调度。

因此具身 runtime 的性能瓶颈可能根本不在模型 FLOPs，而在 memory-domain crossing。

## Memory Domain 比“进程”更贴近真实数据路径

可以把系统看成：

~~~text
CPU pageable RAM
CPU pinned RAM
GPU 0 VRAM
GPU 1 VRAM
NPU memory
camera DMA buffer
RDMA registered memory
remote host memory
~~~

对每一个 domain 都问：

~~~text
谁能直接 load/store？
谁只能通过 DMA？
哪个 allocator 创建？
完成事件是什么？
能不能跨进程导出？
生命周期由谁管理？
~~~

这比只画“进程 A / 进程 B”更能解释现代 AI pipeline。

## 必须把各种 Copy 分开说

一句“我们支持 zero-copy”信息远远不够。

可能省掉的是：

~~~text
application object → middleware buffer
middleware buffer → shared memory
CPU pageable → pinned staging
host → GPU
GPU → host
process A mapping → process B mapping
GPU 0 → GPU 1
host A → host B
~~~

任何一项被消除都可以叫某个局部 zero-copy，但端到端效果完全不同。

所以文档里更准确的说法应该是：

> 在哪两个 memory domain 之间，没有发生 payload copy？

## Pinned Memory：为什么 GPU 通信总会遇到它

普通 pageable host memory 可以被操作系统换页。

而设备 DMA 需要稳定的物理页，所以 host-to-device 传输通常更喜欢 pinned/page-locked memory。

直观路径：

~~~text
pageable host buffer
↓ possible staging
pinned memory
↓ DMA
GPU VRAM
~~~

如果应用直接使用预分配 pinned pool，就能减少临时 staging 与 allocation。

但 Pinned Memory 也不是越多越好：

- 它占用不可随意换出的物理页；
- 大量 pinned allocation 会影响系统内存管理；
- allocation/free 本身可能昂贵；
- 更适合预分配和池化。

这和 shared-memory pool 的思路完全一致：

> 高频数据面尽量预分配，运行时只做借用与归还。

## CUDA Stream：GPU 上“函数返回”并不代表工作已经完成

CPU 代码：

~~~cpp
launch_kernel<<<grid, block, 0, stream>>>(buffer);
~~~

kernel launch 通常是异步的。

所以 Producer 线程继续往下执行时：

~~~text
CPU 已经返回
≠ GPU 已经写完 buffer
~~~

如果另一个 Consumer 立即使用这个 allocation，就需要明确同步关系。

GPU 上对应 CPU happens-before 的机制包括：

- stream ordering；
- CUDA event；
- event wait；
- explicit synchronize；
- external semaphore/fence。

因此 GPU zero-copy 也必须有“发布完成事件”。

## CUDA IPC：跨进程共享 GPU Allocation 的核心仍然是 Descriptor

如果 Process A 在 GPU 上分配了一个 tensor：

~~~text
Process A
GPU allocation
↓
export IPC handle
~~~

Process B 不应该要求 A：

~~~text
GPU → CPU copy
send bytes
CPU → GPU copy
~~~

更合理的是：

~~~text
Process A exports descriptor/handle
↓
Process B imports handle
↓
maps the same device allocation
~~~

这和 CPU SHM 的关系非常像：

~~~text
CPU SHM:
segment id + offset
→ resolve shared physical pages

CUDA IPC:
allocation handle
→ resolve shared device allocation
~~~

跨进程传递的是“如何重新定位同一块资源”，而不是资源内容本身。

## 但是共享到同一块 GPU Memory 还不够：必须同步

假设：

~~~text
Process A stream writes Tensor X
Process B imports Tensor X
~~~

B 能获得 device pointer，不代表它可以立即读。

需要一个完成协议：

~~~text
A launches kernel
↓
A records CUDA event E
↓
export/share synchronization primitive
↓
B waits E
↓
B kernel reads Tensor X
~~~

这就是异构场景里的 ownership transition：

~~~text
A owns mutable X
↓ writes finished
publish completion
↓
B gains readable X
~~~

抽象结构与 CPU release/acquire 非常相似，只是同步原语换成了 device event/fence。

## Camera DMA Buffer：传感器其实也有自己的 Memory Domain

现代相机并不一定先“生成一个 std::vector<uint8_t>”。

更真实的链路可能是：

~~~text
sensor
↓
PCIe / CSI
↓
driver-managed DMA buffer
↓
application imports buffer
↓
GPU preprocess
~~~

如果驱动和 GPU runtime 支持合适的 buffer export/import，理想路径是让 GPU 直接消费 camera buffer 或其映射。

这时中间件真正需要传的可能只有：

~~~text
buffer handle
shape
stride
format
timestamp
fence
~~~

而不是每帧几 MB 的像素。

## Metadata 与 Payload 分离在异构系统里更加重要

高带宽数据面可以保持在 device memory：

~~~text
Tensor payload → stays on GPU
~~~

CPU 侧只传一个很小的 descriptor：

~~~cpp
struct TensorDescriptor {
    uint64_t handle;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t format;
    uint64_t timestamp;
};
~~~

控制面再附带同步信息：

~~~text
event/fence
ownership token
generation
lifetime
~~~

这实际上就是 shared-memory descriptor queue 在 GPU 世界里的延伸。

## Unified Memory 为什么不能简单等价成“问题不存在了”

Unified/managed memory 让编程模型更统一，但它不意味着数据移动成本消失。

页面仍可能在 host/device 之间迁移，访问模式不合适时会产生 page fault、migration 和不可预测延迟。

因此实时 pipeline 仍然需要知道：

~~~text
数据当前 residency 在哪里？
访问会不会触发迁移？
是否应该 prefetch？
是否需要固定 placement？
~~~

抽象更方便，不代表物理成本不存在。

## RDMA：把“远端内存”也拉进同一张图

传统跨主机路径：

~~~text
GPU
↓
host staging
↓
kernel network stack
↓
NIC
↓
network
↓
remote kernel
↓
remote host
↓
remote GPU
~~~

RDMA 试图让 NIC 更直接地访问已注册内存，减少 CPU 参与与中间 copy。

进一步的 GPUDirect RDMA 则希望：

~~~text
GPU memory
↔ NIC
~~~

更直接协作。

于是跨主机通信也可以继续沿用同一个问题：

~~~text
allocation 在哪？
谁注册？
谁持有 key/descriptor？
NIC 能不能 DMA？
完成事件是什么？
remote side 什么时候可以消费？
~~~

## UCX 为什么适合放在具身通信体系里理解

UCX 的意义不只是“又一个网络库”。

它尝试在统一抽象下选择不同 transport/memory 路径：

~~~text
same process
shared memory
TCP
RDMA
CUDA-aware paths
device transports
~~~

这类系统的价值在于：

> 上层表达“把这一块 memory 发给那个 endpoint”，底层根据 memory type 和拓扑选择更合适的路径。

这和传统只面向 CPU byte buffer 的中间件思路已经明显不同。

## 一个 GPU Pipeline 真正应该怎么画

只画 node：

~~~text
Camera → Perception → VLA → Control
~~~

信息太少。

更有用的是：

~~~text
Camera DMA Buffer
 [driver/device memory]
        │
        ▼
Preprocess
 [GPU 0 VRAM]
        │ no host copy
        ▼
Vision Features
 [GPU 0 VRAM]
        │ descriptor + event
        ▼
VLA
 [GPU 0 VRAM]
        │
        ▼
Action Tensor
 [GPU]
        │ small D2H
        ▼
Control Command
 [CPU]
~~~

然后对每条边标：

~~~text
copy?
DMA?
import/export handle?
stream/event?
queue?
lifetime?
~~~

这才是具身运行时的数据路径。

## Memory Pool 在异构系统里同样重要

如果每帧都：

~~~text
cudaMalloc
kernel
cudaFree
~~~

allocator 自身就可能产生明显开销和同步。

更合理的系统通常会：

~~~text
pre-allocate pool
↓
loan buffer
↓
GPU writes
↓
consumer uses
↓
release
↓
reuse
~~~

你会发现，这个状态机和 iceoryx2 shared-memory pool 几乎同构。

也就是说：

> CPU SHM、GPU buffer pool、RDMA registered memory，本质上都在解决“昂贵 allocation 如何被长期持有并安全复用”。

## 对具身中间件真正重要的接口不一定是 publish(bytes)

一个面向 AI pipeline 的通信抽象，越来越需要表达：

~~~text
buffer/tensor handle
memory type
device id
shape / stride / dtype
ownership
completion event
lifetime
routing target
~~~

而不是只接受：

~~~text
void* data, size_t size
~~~

因为后者往往默认“这是 CPU 可读的一段连续 bytes”。

GXF/Holoscan、UCX、CUDA IPC、DLPack 风格 tensor handle 等技术的共同趋势，就是让“内存本身的属性”成为通信契约的一部分。

## 最后用一句话概括

传统中间件把问题描述成：

> 消息怎样从 Node A 发到 Node B？

具身 AI runtime 更准确的问题是：

> 一块高带宽数据现在属于哪个 memory domain，哪个执行单元拥有它，怎样在不做无意义 staging 的前提下，把访问权和完成事件交给下一阶段？

当问题改成这样以后，zero-copy、shared memory、CUDA IPC、RDMA、UCX、GPU-aware runtime 才真正连成一条技术主线。
