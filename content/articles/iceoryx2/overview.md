# iceoryx2 总览：如果一开始就不搬 Payload，IPC 应该怎样设计

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

iceoryx2 最适合作为 Communication Foundations 的第一个完整实例，因为它不是“先设计网络 pub/sub，再加 shared memory 优化”，而是从同机 zero-copy IPC 出发组织 Node、Service、Port、SharedMemory、ZeroCopyConnection 与 crash recovery。

## 先从一个 20 MB 点云开始

最朴素的跨进程方案：

~~~text
Process A
PointCloud on heap
↓ copy
shared memory / socket buffer
↓
Process B
copy / deserialize
↓
PointCloud
~~~

如果 payload 很大，真正想要的是：

~~~text
shared data segment
      ↓ loan
Publisher 直接写 chunk
      ↓
只发送 PointerOffset
      ↓
Subscriber 把 offset 映射成本地地址
      ↓
直接读同一 chunk
      ↓
release offset
      ↓
Publisher reclaim
~~~

所以 iceoryx2 的核心问题不是“如何把字节传过去”，而是：

> 如何跨进程安全传递一块共享内存的访问权。

## 对象图

应用最先看到：

~~~text
Node
  ↓
ServiceBuilder
  ↓
PublishSubscribe Service
  ├─ Publisher
  └─ Subscriber
~~~

深入运行时：

~~~text
Publisher
  ├─ DataSegment
  │    ├─ SharedMemory
  │    └─ PoolAllocator
  ├─ ZeroCopyConnection::Sender[]
  └─ loaned Chunk

Subscriber
  ├─ ZeroCopyConnection::Receiver[]
  ├─ DataSegmentView[]
  └─ borrowed Sample
~~~

控制面还有 StaticStorage、DynamicStorage、PersistentDynamicStorage、Monitoring、Event、Reactor 与 Node stale-resource cleanup。

因此它不是一个简单的 shm ring。

## Payload 与控制消息彻底分开

固定源码里的 ZeroCopySender 接口直接说明了核心设计：

~~~rust
fn try_send(
    &self,
    ptr: PointerOffset,
    sample_size: usize,
    channel_id: ChannelId,
) -> Result<Option<PointerOffset>, ZeroCopySendError>;
~~~

真正通过 connection 传递的是 PointerOffset，而不是 payload bytes。

这正是大数据 zero-copy 的关键：

~~~text
large payload
留在 shared memory

small descriptor
进入 connection queue
~~~

## 为什么不能发送裸指针

同一 shared segment 在两个进程中的映射基址可能不同。

~~~text
Process A:
base = 0x70000000
payload = base + 0x1200

Process B:
base = 0x43000000
payload = base + 0x1200
~~~

0x70001200 对 B 没意义，但 0x1200 有意义。

因此 iceoryx2 的 ShmPointer 同时包含：

~~~rust
pub struct ShmPointer {
    pub offset: PointerOffset,
    pub data_ptr: *mut u8,
}
~~~

data_ptr 服务当前进程，PointerOffset 服务跨进程协议。

## Zero-copy 的难点是回收

Publisher 把 offset 发给多个 Subscriber 后，chunk 不能立即复用。

必须知道哪个 Subscriber 收到了、哪个仍借着 Sample、哪个已经 release、哪个进程异常死亡，以及什么时候 chunk 可以 reclaim。

因此 iceoryx2 还有 used-chunk tracking、borrow limit、dead node monitoring 和 stale resource cleanup。

## 这和 eCAL / DDS SHM 有什么不同

eCAL、Fast DDS、Cyclone DDS 都能走共享内存，但它们首先还是更大的通信系统：

~~~text
discovery
QoS
network fallback
shared-memory fast path
~~~

iceoryx2 更适合作为“同机 IPC 第一性原理”的研究对象：

~~~text
shared memory
offset
loan
borrow
release
reclaim
backpressure
crash recovery
~~~

把这些打透以后，再回去看 DDS Data Sharing / PSMX 会更容易分辨哪些是基础 IPC 机制，哪些是 DDS 语义。

## 本专题主线

~~~text
Node / Service
↓
Service trait 把机制注入
↓
Static / Dynamic Config
↓
Publisher 创建 DataSegment
↓
loan shared Chunk
↓
PointerOffset
↓
ZeroCopyConnection
↓
Subscriber translate offset
↓
borrow Sample
↓
release / reclaim
↓
backpressure / history
↓
dead process cleanup
~~~

这条链会贯穿整个专题。
