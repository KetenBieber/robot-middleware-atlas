# Buffer Backend Contract：统一消息语义，而不是统一内存实现

固定源码版本：`d7cd9642d77a1d64fd85f25ba0bf96e108401900`。

如果业务 message 直接把 payload 固定成 `std::vector<T>`，那 CPU storage 就成了全系统 canonical representation。每加入一种 CUDA/NPU/共享内存后端，就容易继续制造 `CudaImage`、`SharedMemoryImage`、`NitrosImage` 之类平行类型。

更可扩展的做法是固定逻辑消息语义，而把 storage 交给 backend：

~~~text
Image semantic fields
+
rosidl::Buffer<uint8_t>
        ↓
CPU backend
or
CUDA backend
or
other accelerator backend
~~~

## Backend 真正需要提供什么

可以把职责拆成四组。

### Storage

~~~text
allocate
resize
clear
clone
convert to CPU
~~~

### Descriptor

把 backend-specific storage 表示成可传输的小 metadata，而不是重新复制 payload。

CUDA descriptor 需要表达的不是完整 Tensor，而是类似：

~~~text
device id
publisher pid
block id
block size
socket identity
generation uid
CUDA completion event
~~~

### Endpoint capability

回答：

~~~text
这个 remote endpoint 是否能安全理解当前 backend？
~~~

### Import

Subscriber 根据 descriptor：

~~~text
validate
→ import backing storage
→ reconstruct local BufferImpl
→ attach deleter/lifetime
→ attach completion dependency
~~~

所以 backend 是 storage protocol，不只是 allocator plugin。

## 为什么必须保留 CPU fallback

一个 Publisher 可以同时连接：

~~~text
same-process GPU consumer
same-host GPU consumer
remote CPU consumer
debug/recording tool
~~~

如果 CUDA optimized path 是 message correctness 的必要条件，那么最弱 endpoint 会让整个 topic 不可用。

更好的关系是：

~~~text
semantic correctness:
always available

optimized storage path:
conditional
~~~

因此 capability negotiation 与 fallback 是 backend contract 的一部分。

## Descriptor 为什么必须小

正确关系：

~~~text
Descriptor = reference/capability metadata
Payload    = external backing storage
~~~

如果 descriptor 再携带完整 bytes，所谓 zero-copy 就已经消失。

这与共享内存 offset descriptor、DMA-BUF fd、RDMA key 是同一种思想。

## 为什么 Descriptor 不能只带 virtual address

同一 VMM allocation 在两个进程中可能映射为不同 VA：

~~~text
publisher VA = A
subscriber VA = B
~~~

稳定身份必须来自：

~~~text
allocation/block identity
+
exportable handle
+
generation
~~~

而不是 raw pointer value。

## Endpoint discovery 是 control plane

`on_creating_endpoint()` 与 `on_discovering_endpoint()` 不搬运大 payload。

它们建立的是：

~~~text
endpoint exists
↓
backend support
↓
locality/device/user facts
↓
optimized-path decision
~~~

真正 publish/import 是 data plane。

因此每帧不应该重新扫描共享 registry 做能力发现。

## 为什么有两层 GID cache

HostEndpointManager 有：

~~~cpp
std::unordered_map<GidKey, CachedEndpointInfo, GidKeyHash>
    local_cache_;
~~~

CUDA backend 自己又有：

~~~cpp
std::unordered_map<GidKey, bool, GidKeyHash>
    ipc_decision_cache_;
~~~

前者缓存**事实**：

~~~text
locality / device / uid / ipc capable
~~~

后者缓存**策略结论**：

~~~text
最终是否走 CUDA IPC
~~~

即使 key 相同，两者也不应该机械合并，因为失效条件和职责不同。

## same-host 为什么仍然不够

当前 CUDA IPC path 至少要求：

~~~text
same host
AND
same CUDA device
AND
same Linux uid
AND
remote supports cuda backend
AND
local pool supports VMM IPC
~~~

这是一个 AND capability predicate。

每个条件都在消除一种无效假设：

- 跨主机不能使用本机 FD/shm；
- cross-device path 当前没有实现；
- 不同 Linux 用户涉及权限/安全边界；
- 不支持 backend 的对端无法解释 descriptor；
- 驱动/硬件不支持 VMM IPC 时必须回退。

## 一个 topic 可以 per-endpoint 使用不同 data path

~~~text
Publisher
├─ A: intra-process CUDA
├─ B: same-host CUDA VMM IPC
└─ C: CPU fallback / serialization
~~~

逻辑 topic 不要求物理传输路径一致。

这和 DDS transport selection、UCX lane/protocol selection 的思想是一致的。

## 通用接口为什么仍然需要 backend-specific escape hatch

CPU 与 CUDA storage 不可能完全等价。

CPU：

~~~text
host pointer directly dereferenceable
serialization straightforward
~~~

CUDA：

~~~text
host cannot directly dereference
stream/event dependencies
cross-process VMM import
~~~

因此通用 Buffer 接口负责 interoperability；高性能 CUDA 算法仍需要获得 backend-specific ReadHandle/WriteHandle。

如果 abstraction 强行隐藏 memory domain，成本只会变成隐式 copy/synchronize。

## to_cpu() 是一个重要的显式边界

CUDA `to_cpu()` 需要：

~~~text
allocate CPU storage
↓
ReadHandle waits producer dependency
↓
cudaMemcpyAsync D2H
↓
cudaStreamSynchronize
↓
return CPU Buffer
~~~

这里同步和 copy 都是有成本的。

把 conversion 显式化比“看起来是普通 data()，内部偷偷 D2H”健康得多。

## clone() 则可以保持在 Device

CUDA clone 可以：

~~~text
allocate new CUDA Buffer
↓
ReadHandle source
↓
WriteHandle destination
↓
cudaMemcpyAsync D2D
~~~

所以“复制”并不必然意味着跨 memory domain。

## resize() 为什么也变成异步 ownership 问题

容量变化需要：

~~~text
new VMM block
↓
D2D copy old payload
↓
replace backing storage
↓
old storage waits outstanding CUDA work before recycle
~~~

普通容器操作一旦进入 accelerator domain，就会附带 execution dependency。

## 一个可迁移到自研 VLA runtime 的接口

~~~cpp
class BufferBackend {
public:
    virtual MemoryDomain domain() const = 0;
    virtual Buffer allocate(size_t bytes) = 0;
    virtual Buffer clone(const Buffer&) = 0;

    virtual Descriptor export_for(
        Endpoint endpoint) = 0;

    virtual Buffer import(
        const Descriptor&) = 0;

    virtual CpuBuffer to_cpu(
        const Buffer&) = 0;
};
~~~

业务 message 只保存：

~~~text
shape
dtype
semantic metadata
Buffer
~~~

而不是把 CUDA pointer 暴露成整个系统的唯一事实。

## 最终必须守住的六个不变量

无论后端将来换成 DMA-BUF、ROCm、NPU 或 RDMA：

~~~text
1. logical payload semantics 保持一致
2. descriptor 不能指向已回收 storage
3. consumer 必须等待 producer completion
4. storage reuse 必须等待所有 consumer completion
5. capability 不满足时必须有正确 fallback
6. stale descriptor 必须可检测
~~~

这些才是 Buffer Backend Contract 的核心。
