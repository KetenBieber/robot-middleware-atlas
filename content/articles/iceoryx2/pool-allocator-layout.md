# PoolAllocator：为什么 Zero-copy Runtime 更愿意管理固定 Bucket，而不是在周期路径上 malloc

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

如果已经决定让 Publisher 直接在共享内存里写最终 payload，那么下一个问题不是 API，而是：

> 这块共享内存到底怎样分配，才能既快、又可回收、又能跨进程表示？

iceoryx2 的一个关键答案是 PoolAllocator。

## 第一性原理：为什么普通 Heap 不理想

通用 heap allocator 要处理：

- 不同大小的 allocation；
- split / merge；
- free list；
- fragmentation；
- metadata；
- 多线程竞争。

这些能力很通用，但对固定消息形态的实时 IPC 并不一定划算。

如果系统已经知道：

~~~text
每个 sample 最大尺寸
每个 sample 对齐要求
最大并发 chunk 数量
~~~

那么可以把问题简化成：

~~~text
一大片连续内存
→ 切成 N 个等尺寸 bucket
→ allocation 只需要找到一个空闲 index
~~~

## 底层 PoolAllocator 的核心字段

固定源码 iceoryx2-bb/memory/src/pool_allocator.rs：

~~~rust
pub struct PoolAllocator {
    buckets: UniqueIndexSet,
    bucket_size: usize,
    bucket_alignment: usize,
    start: SyncPointer<u8>,
    size: usize,
    is_memory_initialized: AtomicBool,
}
~~~

关键字段是：

~~~text
buckets: UniqueIndexSet
~~~

allocator 把“哪块内存空闲”转换成“哪个整数 index 空闲”。

## Allocation 实际做什么

固定源码：

~~~rust
match unsafe { self.buckets.acquire_raw_index() } {
    Ok(v) => Ok(unsafe {
        NonNull::new_unchecked(
            self.start
                .as_ptr()
                .cast_mut()
                .add(v as usize * self.bucket_size),
        )
    }),
    Err(_) => {
        fail!(from self,
            with AllocationError::OutOfMemory,
            "No more buckets available ...");
    }
}
~~~

所以一次 allocation 的核心路径是：

~~~text
UniqueIndexSet
↓ acquire index i
start + i * bucket_size
↓
返回该 bucket 的地址
~~~

没有在 payload 区域里遍历可变大小 free block。

## Deallocation 为什么同样简单

固定源码先把 pointer 反算成 index：

~~~rust
((position - self.start.as_ptr() as usize)
    / self.bucket_size) as u32
~~~

然后：

~~~rust
self.buckets
    .release_raw_index(
        index,
        ReleaseMode::Default);
~~~

因此 pool 的逻辑身份其实是 bucket index，而不是 pointer。

这一设计与 PointerOffset 的相对地址语义一致：

~~~text
进程内 allocator
关注 bucket index

跨进程 IPC
关注 relative offset
~~~

两者都尽量避免把绝对虚拟地址当成全局身份。

## CAL 层为什么还包一层 Shm PoolAllocator

iceoryx2-cal/src/shm_allocator/pool_allocator.rs 又定义：

~~~rust
pub struct PoolAllocator {
    allocator:
        iceoryx2_bb_memory::
        pool_allocator::PoolAllocator,
    base_address: usize,
    max_supported_alignment_by_memory: usize,
    number_of_used_buckets: AtomicUsize,
}
~~~

这一层把普通 pointer allocator 适配成：

~~~text
Allocate<PointerOffset>
~~~

核心转换：

~~~rust
let chunk =
    self.0.allocator.allocate(layout)?;

Ok(PointerOffset::new(
    chunk.as_ptr() as usize
        - self.0.allocator.start_address()
            as usize,
))
~~~

这一步非常重要。

allocator 最终交给 IPC 层的不是：

~~~text
0x7f123456...
~~~

而是：

~~~text
chunk_address - segment_start
=
PointerOffset
~~~

## 为什么 Grow 不一定搬内存

底层固定 bucket 已经决定最大 bucket size。

如果 new layout 仍然装得进当前 bucket，grow 可以保持同一个 pointer。

固定源码写得很直接：

~~~rust
if self.bucket_size < new_layout.size() {
    return OutOfMemory;
}

...

Ok(ptr)
~~~

CAL 的 shm allocator 也类似：

~~~rust
if new_layout.size()
    > self.0.bucket_size()
{
    return OutOfMemory;
}

...

Ok(offset)
~~~

所以这里的 grow 更像：

~~~text
当前 bucket 本来就足够大
只是扩大“有效 payload 解释范围”
~~~

而不是通用 realloc。

## AllocationStrategy 为什么存在

当 Dynamic DataSegment 真的装不下时，CAL 层的 resize_hint 会根据策略决定新容量。

三种典型策略：

~~~text
Static
不增长

BestFit
刚好满足当前需求

PowerOfTwo
按 2 的幂扩容
~~~

这对应三个工程目标：

| 策略 | 目标 |
| --- | --- |
| Static | 最容易推理、容量固定 |
| BestFit | 降低额外内存浪费 |
| PowerOfTwo | 降低频繁扩容次数 |

## 为什么固定 Bucket 对机器人实时链很友好

假设相机帧最大 4 MB，最多允许 8 个在途 chunk。

可以直接预留：

$$
4\text{ MB} \times 8 = 32\text{ MB}
$$

系统从一开始就知道：

~~~text
最多占多少 payload memory
最多有多少 outstanding samples
第 9 个 loan 会怎样失败
~~~

这比“希望 heap 到时还有空间”更容易做容量分析。

## 代价也很明确

固定 bucket 的缺点是 internal fragmentation。

例如 bucket 4 MB，而普通 sample 只有 500 KB：

~~~text
实际 payload    0.5 MB
bucket          4.0 MB
内部浪费        3.5 MB
~~~

因此 bucket size 必须围绕真实消息上界设计。

## 从 STL 角度怎么类比

如果用 C++ 思考，它更接近：

~~~text
fixed-capacity object pool
+
free-index set
~~~

而不是：

~~~text
std::vector<std::byte>
每次 resize
+
new/delete
~~~

核心设计目标不是 API 美观，而是：

> 把动态内存问题转化成有限资源调度问题。

这正是实时系统里更容易推理的形式。
