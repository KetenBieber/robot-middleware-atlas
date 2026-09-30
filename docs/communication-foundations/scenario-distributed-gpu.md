# 场景设计六：VLM/VLA 的 GPU Tensor 怎样跨模块、跨进程甚至跨主机

> **知识依赖：** 本场景建立在大对象 IPC 的 Payload / Descriptor / Notification、Offset 和 Loan 之上。进入 GPU/NPU memory domain 后，最重要的两个问题仍然是“数据位于哪类内存”和“哪个完成信号证明 Buffer 已经可以进入下一生命周期阶段”。

## 场景

现代具身 pipeline：

~~~text
Camera
↓
GPU Vision Encoder
↓
Visual Features / Tokens
↓
VLA Policy / Reasoning
↓
Action
~~~

最危险的默认设计是：

~~~text
GPU Tensor
↓ D2H
CPU vector
↓ serialize
middleware
↓ deserialize
CPU vector
↓ H2D
GPU
~~~

如果上下游本来都在 GPU，这些 copy 很可能完全是软件边界制造的。

---

## 第一步：把 Memory Domain 写进接口

传统接口：

~~~text
send(void* data, size_t bytes);
~~~

对异构系统不够。

这里的 `Memory Domain` 指“这块内存由谁分配、谁可以直接访问、通过什么总线或 API 访问”。例如普通 Host RAM、Pinned Host、CUDA Device、DMA-BUF 背后的设备内存都属于不同 domain。

`Pinned Host` 是被固定在物理内存中的 Host Buffer，避免被 OS 换出，常用于 DMA；`DMA` 是设备不经 CPU 逐字节搬运、直接访问内存的机制；`RDMA` 则把这种直接访问能力扩展到远端主机的注册内存。

至少要知道：

~~~text
Host/System
Pinned Host
CUDA Device
CUDA Managed
NPU memory
DMA buffer
RDMA registered memory
~~~

因为“地址”只在特定访问者/设备上下文里有意义。

---

## Tensor Handle 应该包含什么

下面给一个可以独立编译的“元数据模型”。它不调用 CUDA，只把异构 Buffer 接口中必须显式携带的信息建模出来：

这段程序**不依赖 CUDA**，只是先把 Handle 元数据建模清楚，因此任何 C++17 编译器都可以运行：

~~~text
g++ -std=c++17 -O2 tensor_handle_demo.cpp -o tensor_handle_demo
./tensor_handle_demo
~~~


~~~cpp
#include <array>
#include <cstddef>
#include <cstdint>
#include <iostream>

enum class MemoryDomain {
    Host,
    PinnedHost,
    CudaDevice,
    DmaBuffer,
    RdmaRegistered
};

struct TensorHandle {
    MemoryDomain domain = MemoryDomain::Host;
    int device_id = -1;
    std::uint64_t buffer_id = 0;
    std::size_t bytes = 0;
    std::array<std::size_t, 4> shape{};
    std::uint64_t generation = 0;
    std::uint64_t completion_token = 0;
};

int main() {
    TensorHandle h;
    h.domain = MemoryDomain::CudaDevice;
    h.device_id = 0;
    h.buffer_id = 42;
    h.bytes = 224 * 224 * 3 * sizeof(float);
    h.shape = {1, 3, 224, 224};
    h.generation = 7;
    h.completion_token = 1024;

    std::cout
        << "buffer=" << h.buffer_id
        << " bytes=" << h.bytes
        << " generation=" << h.generation
        << " completion_token=" << h.completion_token
        << "\n";
}
~~~

这里 `buffer_id` 不是裸指针，而是 Runtime 可以解释的稳定标识；`generation` 用来区分 Buffer 复用前后的不同实例；`completion_token` 只是教学版占位符，真实系统里可能映射到 CUDA event、timeline semaphore、fence fd 或其他完成信号。

消息控制面传 handle/schema；大 payload 留在原 memory domain。

---

## 同进程 GPU：最简单的是共享 Device Pointer，但仍需要 Stream 依赖

A 在 streamA 写 Tensor。

B 在 streamB 读。

不能因为 A 的 CPU compute() 返回就认为数据 ready。

需要：

~~~text
cudaEventRecord(streamA)
↓
cudaStreamWaitEvent(streamB)
↓
B kernel
~~~

这叫 GPU-side happens-before。

它和 CPU release/acquire 是不同层的同步。

---

## 同机跨进程 GPU：Pointer 不能直接传

不同 CUDA context/process 不能默认共享普通 device pointer。

候选：

~~~text
CUDA IPC memory handle
DMA-BUF/exported handle
runtime-specific GPU IPC
~~~

仍然需要：

~~~text
device identity
handle export/import
lifetime
completion synchronization
crash cleanup
~~~

所以它与 SHM offset pointer 是同一个思想在 GPU domain 的版本。

---

## 跨主机 GPU：必须决定 Direct 还是 Staging

候选路径：

### Direct-capable

~~~text
GPU memory
↓ GPUDirect/RDMA-capable path
NIC
↓
remote GPU/registered destination
~~~

### Staged

~~~text
GPU
↓ D2H
Pinned Host Pool
↓ network
remote pinned
↓ H2D
GPU
~~~

Direct 不可用时，staging pipeline 仍然可以通过多 buffer overlap copy/network。

---

## 为什么 UCX 适合做这一层

UCX 把：

~~~text
memory type
endpoint lanes
transport capability
message size
registration
protocol selection
~~~

> **第三遍/专题阅读内容：** Rendezvous、registration、GPUDirect/RDMA 属于高速异构传输机制。只想先建立具身 Runtime 基础时，可以在这里停下；前面“Memory Domain + Handle + Completion”的模型已经足够。


纳入同一个 data-plane planner。

同一个 send 逻辑可以根据环境选择 shared memory、TCP、RDMA、CUDA-aware path 等。

它不是 VLA framework，但它提供了很合适的低层 heterogeneous transport substrate。

---

## Rendezvous 为什么比 Eager 更适合大 Tensor

小消息可以：

~~~text
header + payload 一起发
~~~

大 Tensor 更合理：

~~~text
先交换 metadata / address / rkey
↓
选择 GET/PUT/direct/staged plan
↓
真正搬 payload
~~~

控制面与数据面分离。

详见： [UCX Rendezvous & GPU Pipeline](../generated/ucx/rendezvous-gpu-pipeline.md)。

---

## Registration Cache 为什么重要

RDMA/某些 device path 对 buffer 需要 registration/export。

如果每个 frame：

~~~text
malloc
register
send
deregister
free
~~~

控制开销可能抵消 zero-copy 收益。

更合理：

~~~text
long-lived tensor/buffer pool
↓
register/export once
↓
reuse many transfers
~~~

这就是 pool 与高速 transport 天然配套的原因。

---

## Completion 到底证明什么

必须区分：

~~~text
GPU kernel completion
local NIC DMA completion
transport send completion
remote receive completion
business consumption completion
~~~

它们不是同一个时刻。

Buffer 什么时候可以复用，取决于**最后一个仍可能访问它的参与者**。

---

## Fan-out 更复杂

Visual feature 同时给：

~~~text
Policy
Recorder
Visualizer
Telemetry
~~~

如果共用一块 GPU buffer，最慢 consumer 决定回收时间。

可能需要：

~~~text
refcount
separate pool
copy for slow branch
drop branch
deadline-based cancellation
~~~

不要让 telemetry 拖住 control-critical tensor pool。

---

## Backpressure 应该发生在哪一层

至少三层：

~~~text
business queue capacity
GPU buffer pool capacity
transport pending/request capacity
~~~

UCX 没有 NO_RESOURCE 不代表业务没积压。

Holoscan queue 没满也不代表 GPU pool 有空 block。

所以 telemetry 必须分别暴露。

---

## 工业案例：Holoscan Ultrasound

配置直接使用：

~~~yaml
input_on_cuda: true
output_on_cuda: true
transmit_on_cuda: true
~~~

意义是 Operator 软件边界不强迫 Tensor 回 CPU。

同时每个 stage 配自己的 BlockMemoryPool/CudaStreamPool。

详见： [Ultrasound Segmentation](../generated/holoscan/case-study-ultrasound-segmentation.md)。

---

## 工业案例：Holoscan Distributed Fragment

跨 Fragment 后：

~~~text
Graph edge
→ UcxTransmitter
→ serialization/control metadata
→ UCX
→ UcxReceiver
→ remote Scheduler
~~~

逻辑 edge 膨胀成完整 distributed data path。

shutdown 也必须允许 queued/in-flight UCX 消息 drain。

---

## 一个 VLA Data Plane 设计

~~~text
Camera DMA Buffer Pool
↓
Vision Encoder
  output: GPU TensorHandle
↓
Policy admission check
  downstream capacity?
↓
local:
  CUDA handle + event

remote:
  UCX/RDMA if direct
  otherwise pinned staging pipeline
↓
Policy GPU
↓
action
~~~

控制面只传：

~~~text
frame id
timestamp
deadline
shape/dtype
buffer generation
route
~~~

---

## 什么时候 Copy 反而合理

如果 Tensor 很小，或者边界要求强隔离/持久化：

~~~text
copy/serialize
~~~

可能比复杂 GPU handle lifetime 更划算。

所以 zero-copy 仍然不是宗教。

需要测：

~~~text
payload size × frequency
copy bandwidth
registration/export overhead
buffer lifetime complexity
failure recovery
~~~

---

## 指标

~~~text
GPU pool free blocks
in-flight tensors
tensor age
CUDA event wait
D2H/H2D bytes
registration cache hit
UCX pending count
network completion latency
end-to-end action age
~~~

最终机器人关心的不是 transport GB/s，而是 action 基于多旧的 observation。

---

## 设计检查表

~~~text
[ ] payload 当前在哪个 memory domain？
[ ] 下游计算最终在哪个 domain？
[ ] 能否避免不必要 D2H/H2D？
[ ] handle/export 的 lifetime 谁负责？
[ ] 哪个 completion 允许 buffer reuse？
[ ] pool 容量如何从 in-flight concurrency 推导？
[ ] direct path 不可用时 fallback 是什么？
[ ] slow branch 是否会拖住 control-critical buffer？
[ ] shutdown 如何 drain GPU/network in-flight work？
~~~

深入机制：

- [Heterogeneous Memory](heterogeneous-memory.md)
- [UCX 实现专题](../generated/ucx/index.rst)
- [Holoscan 实现专题](../generated/holoscan/index.rst)
