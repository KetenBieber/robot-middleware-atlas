# rosidl::Buffer 总览：把“消息是什么”和“payload 存在哪里”拆开

固定源码版本：`d7cd9642d77a1d64fd85f25ba0bf96e108401900`。

传统消息类型经常把**消息语义**和**存储实现**绑死。例如图像字段如果直接定义为 `std::vector<uint8_t>`，就已经默认 payload 属于 CPU heap、使用 host pointer 访问、生命周期服从普通 C++ 容器。对 CPU-only 程序这很自然，但在 Camera DMA → CUDA preprocess → TensorRT/VLM encoder → GPU postprocess 的链路里，它会逼着 accelerator 数据反复回到 CPU canonical representation。

更合理的分层是：

~~~text
Message semantics
  shape / dtype / metadata
        ↓
Logical Buffer
        ↓
Storage backend
  CPU / CUDA / accelerator memory
~~~

`rosidl::Buffer<T>` 的价值就在这里：上层仍然表达“有 N 个 T”，而 storage backend 决定这 N 个元素具体属于哪一个 memory domain。

## CUDA backend 不只是 cudaMalloc 包装

固定源码中的 CUDA backend 实际包含三类长期 runtime：

~~~text
CudaMemoryPool
  CUDA VMM allocation / reuse / block identity

CudaVmmIPCManager
  Unix-domain socket / FD passing / import cache

HostEndpointManager
  endpoint locality / device / uid / capability registry
~~~

这意味着 backend 的职责已经是：

~~~text
storage
+ ownership
+ capability negotiation
+ cross-process import
+ async completion
+ fallback
~~~

而不是单纯 allocator。

## 为什么不能跨进程直接传 device pointer

一个 CUDA pointer 只是当前进程 CUDA virtual address space 里的地址。另一个进程不能拿到相同整数后直接解引用。

跨进程真正需要共享的是 allocation identity 与可导出的内核资源：

~~~text
cuMemCreate
↓
cuMemAddressReserve
↓
cuMemMap
↓
cuMemExportToShareableHandle
↓
POSIX FD
~~~

Subscriber 再在自己的进程里 import 并映射，因此同一个物理 allocation 在两个进程里可以拥有不同 VA。

这和 Communication Foundations 里的基本结论完全一致：

> shared storage 不等于 shared virtual address。

## 为什么 GPU IPC 最后会落到 Unix-domain socket

POSIX FD 是进程局部整数。Publisher 中的 fd=17 与 Subscriber 中的 fd=17 没有天然关系。

所以 exported VMM FD 必须通过：

~~~text
AF_UNIX socket
+ sendmsg/recvmsg
+ SCM_RIGHTS
~~~

由内核复制到另一个进程的 descriptor table。

GPU runtime 因而直接落到了 Linux IPC 原语。

## 为什么还需要 host-wide endpoint registry

即使能传 FD，系统仍要先判断优化路径是否成立：

~~~text
same host?
same CUDA device?
same Linux uid?
remote supports cuda backend?
local pool IPC-capable?
~~~

固定 `HostEndpointManager` 维护：

~~~text
RMW GID
→ locality
→ device id
→ uid
→ IPC capability
~~~

因此 zero-copy 不是一个全局 bool，而是 **per-endpoint capability decision**。

## 稳定 block identity 为什么是必要的

如果每条消息都重新 export/import CUDA allocation，会反复支付 FD handshake、VMM import 和 mapping 成本。

固定实现给每个 pool block 一个稳定：

~~~text
(pid, block_id)
~~~

Subscriber 用它做 import cache key。

第一次：

~~~text
cache miss
→ receive FD
→ import/mapping
→ cache
~~~

后续同一个 block：

~~~text
cache hit
→ reuse imported mapping
~~~

所以 pool 同时减少 allocation 和 IPC setup。

## 稳定 block_id 又会带来 stale-handle 问题

block 被回收再利用以后，旧 descriptor 可能迟到：

~~~text
block_id=3, uid=100  → frame A
recycle
block_id=3, uid=101  → frame B

late descriptor still says uid=100
~~~

只检查 block_id 会产生典型 ABA/stale reference。

固定共享 metadata 因而还有：

~~~cpp
std::atomic<uint64_t> uid;
~~~

descriptor 同时携带 expected UID。Subscriber import 时比较 generation，不一致就丢弃 stale descriptor。

## 为什么 atomic refcount 还不够

共享 metadata 还包含：

~~~cpp
std::atomic<int32_t> refcount;
std::atomic<uint64_t> publish_timestamp_us;
~~~

Subscriber import 增加 refcount，最后一个本地 owner 释放时减少 refcount。

但 Publisher 即使看到 refcount==0，也不会立刻认为 block 安全，因为 descriptor 可能已经发出而 Subscriber 尚未来得及 import/refcount++。

因此回收条件实际上是：

~~~text
refcount == 0
AND
publish grace window elapsed
AND
generation UID still matches
~~~

这是一个跨进程 lifetime protocol，不是 shared_ptr 能单独解决的问题。

## zero-copy 还必须共享 completion dependency

共享同一 GPU allocation 并不代表 Producer 已经写完。

因此 descriptor 还携带 CUDA IPC event handle：

~~~text
Producer stream writes
↓
records CUDA event
↓
descriptor carries event handle
↓
Subscriber imports event
↓
reader stream waits event
↓
safe read
~~~

zero-copy 真正需要共享两件东西：

~~~text
storage
+
completion/fence
~~~

只有 pointer 没有 fence，仍然会产生数据竞争。

## ReadHandle / WriteHandle 为什么比裸 pointer 更重要

裸 `uint8_t*` 无法表达：

- 当前访问是读还是写；
- 哪条 CUDA stream 在访问；
- 访问何时结束；
- 是否允许第二个 writer；
- Buffer 什么时候可以 recycle。

固定 backend 用 move-only RAII handle：

~~~text
WriteHandle
  mutable pointer
  finalize/destructor records producer event

ReadHandle
  waits producer event
  const pointer
  destructor records reader event
~~~

把访问权限与 async lifetime 绑定在同一个对象协议里。

## C++ 析构为什么仍然不能直接 free

CPU 最后一个 Buffer owner 消失时，GPU kernel 可能还在运行。

因此固定 `CudaBuffer` 还有后台 recycler：

~~~text
CudaBuffer destructor
↓
collect outstanding CUDA events
↓
enqueue PendingWork
↓
BufferRecycler thread
↓
cudaEventSynchronize
↓
return underlying block
~~~

CPU object lifetime 和 GPU execution lifetime 被显式桥接。

## 和 Atlas 已有专题怎样串起来

~~~text
iceoryx2
  CPU shared-memory ownership
        ↓
UCX
  heterogeneous transport / memory movement
        ↓
rosidl::Buffer CUDA backend
  standard message semantics + pluggable GPU storage/IPC
        ↓
Holoscan
  graph scheduling + queue + allocator + CUDA/UCX runtime
~~~

四者解决不同层级的问题，却共享 ownership、descriptor、completion、backpressure 这些底层不变量。

这套 data plane 可以按四层理解：Buffer contract 定义逻辑语义；VMM/DMA-BUF pool 管理物理 storage；Unix socket、SCM_RIGHTS 与 shared metadata 负责跨进程 capability/lifetime；CUDA event 与 Read/Write Handle 负责异步 completion。Endpoint locality 再决定这些优化是否成立，不成立时必须显式 fallback。
