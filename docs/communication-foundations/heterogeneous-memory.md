# 异构内存通信：具身 AI 为什么把“消息传输”变成 Memory Domain 问题

传统机器人消息系统默认：

~~~text
message payload
≈
CPU-addressable bytes
~~~

但现代感知与 VLA pipeline 中，主要数据可能长期存在 GPU VRAM，而不应该频繁回到 CPU RAM。

## 一张图像为什么不应该反复回 CPU

假设 1920×1080 RGB8：

$$
1920 \times 1080 \times 3
=
6{,}220{,}800\ \text{bytes}
$$

约 6.2 MB。

30 FPS 原始吞吐约：

$$
6.2 \times 30
\approx
186\ \text{MB/s}
$$

如果 pipeline：

~~~text
Camera
→ Rectify
→ Depth
→ Detection
→ Segmentation
→ Vision Encoder
→ VLA
~~~

每一级都经历 GPU → CPU → middleware → CPU → GPU，那么真正浪费的可能不是神经网络算力，而是 memory-domain crossing。

## 必须区分不同 Copy

“zero-copy”必须写清楚省掉的是哪一段：

~~~text
application object → middleware buffer
middleware buffer → shared memory
host RAM → device RAM
device RAM → host RAM
process A mapping → process B mapping
host A GPU → host B GPU
~~~

只省掉其中一项，不等于端到端 zero-copy。

## Pinned Memory 为什么重要

普通 pageable host memory 在 GPU DMA 时往往需要额外 staging。

Pinned host memory 可以让设备 DMA 更直接，但代价是：

- 占用不可分页的物理内存；
- 过多 pinned allocation 会伤害系统整体内存管理；
- allocation 本身应尽量池化。

因此 GPU pipeline 同样需要 memory pool。

## CUDA IPC 的本质

同主机多进程 GPU 通信不应该先把 VRAM 数据拷回 CPU。

典型思路：

~~~text
Process A
CUDA allocation
↓ export handle/descriptor

Process B
import descriptor
↓
map same device allocation
↓
kernel consumes
~~~

这里跨进程发送的是 descriptor，而不是整块 tensor。

它和 shared-memory IPC 的 offset 思想高度相似：

~~~text
CPU SHM:
offset identifies shared allocation

GPU IPC:
descriptor/handle identifies device allocation
~~~

## GPU 同步同样是 Ownership 协议

即使 B 能映射 A 的 GPU memory，也不能立刻读。

必须知道：

~~~text
A 的 kernel 是否已经写完？
B 的 stream 何时可以开始？
~~~

所以需要 CUDA event、stream dependency、fence 或显式 synchronization。

这和 CPU acquire/release 本质上回答同一个问题：

> Consumer 获得这块内存的访问权时，Producer 对它的写入是否已经完成？

## RDMA / GPUDirect 把边界继续推远

跨主机时，传统路径可能是：

~~~text
GPU
→ host memory
→ kernel network stack
→ NIC
→ remote host memory
→ GPU
~~~

RDMA / GPUDirect 目标是减少中间 staging，让 NIC 与注册内存甚至 GPU memory 更直接协同。

UCX 的统一通信抽象正覆盖这类 memory-domain 与 transport 组合：

~~~text
same-process
shared memory
TCP
CUDA IPC
RDMA
GPUDirect
~~~

可以被统一到同一通信抽象下。

## 对具身系统真正该画的图

分析 VLA runtime 时，仅画 node/topic 不足以解释数据搬运成本，还需要标注 memory domain：

~~~text
Camera
↓
Perception
↓
VLA
↓
Action
~~~

而要画：

~~~text
Camera DMA buffer
   [device / pinned]
↓
Preprocess
   [GPU]
↓
Feature Tensor
   [GPU]
↓
VLM/VLA
   [GPU]
↓
Action
   [CPU small payload]
↓
control loop
~~~

Memory domain 本身就是系统架构。
