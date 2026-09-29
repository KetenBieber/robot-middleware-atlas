# SharedMemory、ShmPointer 与 PointerOffset：跨进程真正能传的到底是什么

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## SharedMemory 只让页共享，不让虚拟地址共享

固定源码对 SharedMemory 的定义很直接：一个进程创建命名共享内存，其他进程可以打开它，从而共享 inter-process memory。

但每个进程的映射地址仍可不同。

因此不能把 producer 的 data pointer 直接塞进队列。

## ShmPointer 为什么有两个字段

固定源码：

~~~rust
pub struct ShmPointer {
    pub offset: PointerOffset,
    pub data_ptr: *mut u8,
}
~~~

这不是重复信息。

data_ptr：

~~~text
当前进程
直接读写 payload
~~~

PointerOffset：

~~~text
跨进程
识别共享 allocation
~~~

## 从绝对地址变成相对位置

Publisher：

~~~text
shared memory base A
+
offset
=
local pointer A
~~~

Subscriber：

~~~text
shared memory base B
+
same offset
=
local pointer B
~~~

所以：

~~~text
pointer
是 process-local view

offset
才是 cross-process identity
~~~

## DataSegment 把 SharedMemory 再封一层

Publisher 不直接拿 SharedMemory 到处使用。

DataSegment 内部：

~~~rust
enum MemoryType<Service> {
    Static(Service::SharedMemory),
    Dynamic(Service::ResizableSharedMemory),
}
~~~

并提供统一 allocate、grow、deallocate_bucket、bucket_size 等操作。

这让 payload storage 策略和 Publisher 算法分离。

## Static 与 Dynamic Segment

固定源码里的 DataSegmentType：

~~~rust
pub enum DataSegmentType {
    Dynamic,
    Static,
}
~~~

Static：

- 资源一次分配；
- 容量耗尽后不再自动扩展；
- 地址和容量行为更容易推理。

Dynamic：

- 可以扩展；
- 更灵活；
- receiver 需要处理新的 segment mapping；
- 生命周期和映射失败路径更复杂。

## Subscriber 如何恢复本地地址

接收端 DataSegmentView 调：

~~~rust
pub(crate) fn register_and_translate_offset(
    &self,
    offset: PointerOffset,
) -> Result<usize, SharedMemoryOpenError>
~~~

Static segment 下，逻辑接近：

~~~text
offset.offset()
+
memory.payload_start_address()
~~~

Dynamic segment 则可能需要先注册/映射相应 segment，再得到本地 pointer。

## 为什么 Dynamic 模式更难

Subscriber 源码中有专门的异常处理：如果 sender 对 dynamic data segment 做了 reallocation，并在 receiver 还没映射新 segment 前退出，receiver 可能失去该 chunk。

这说明 dynamic growth 引入新的 race：

~~~text
sender grows segment
↓
publishes offset
↓
receiver has not mapped new segment yet
↓
sender disappears
~~~

于是“可增长共享内存”并不是免费能力。

## PoolAllocator 的意义

DataSegment 创建 static segment 时配置 PoolAllocator bucket layout。

这意味着 runtime 不需要对每条消息重新走通用 heap allocator。

固定 bucket/pool 可以减少 malloc metadata、heap fragmentation 和 unpredictable allocation path，但容量规划也更重要。

## 这一章的核心

跨进程 zero-copy 不需要双方拥有同一个 pointer。

它需要：

~~~text
共同认可一个 shared allocation
+
一个跨地址空间稳定的 descriptor
+
一个本地 descriptor→pointer 的翻译机制
~~~

在 iceoryx2 中，这个 descriptor 的核心就是 PointerOffset。
