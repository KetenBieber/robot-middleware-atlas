# CUDA VMM Pool：size-class free list、generation 与安全回收

固定源码版本：d7cd9642d77a1d64fd85f25ba0bf96e108401900。

CudaMemoryPool 最值得研究的不是 CUDA API，而是它怎样把异步、跨进程的 GPU allocation 变成可重复使用、可验证 generation、可等待远端 reader 的 pool block。

核心成员可以压成：

~~~cpp
std::map<size_t, std::vector<VmmBlock *>> free_blocks_;
std::vector<std::unique_ptr<VmmBlock>> all_blocks_;
std::unique_ptr<CudaVmmIPCManager> ipc_manager_;
mutable std::mutex mutex_;
~~~

这四个字段已经把 size class、长期 ownership、IPC 服务和并发保护分开。

## 为什么 free_blocks_ 是 map<size, vector<Block*>>

allocation 请求大小并不固定，而且 CUDA VMM 还要求按 granularity 对齐。

Pool 真正要做的查询是：

~~~text
给定 aligned request
→ 找 first bucket size >= request
~~~

源码直接使用：

~~~cpp
auto it = free_blocks_.lower_bound(aligned);
~~~

所以 ordered map 的价值不是“输出有序”，而是提供 lower_bound range query。

如果换成 unordered_map，精确 size lookup 很快，但“下一个足够大的 size class”只能扫描所有 bucket。

## 为什么 bucket value 是 vector，不是 list

同一个 size bucket 里并不是所有 block 都立刻可复用。

某些 block 可能仍然满足：

~~~text
remote refcount > 0
or
publish grace period not elapsed
~~~

因此代码需要扫描 bucket，找到第一个 ready block。

源码特别解释：bucket 内顺序无意义，因此命中以后不使用 vector.erase，而是：

~~~cpp
vec[i] = vec.back();
vec.pop_back();
~~~

这就是经典 swap-and-pop。

普通 erase 会搬移后续元素，最坏 O(N)；swap-and-pop 在不保序场景是 O(1)。

源码还明确比较了 list：list 也能 O(1) erase，但节点分散在 heap，顺序扫描时 cache locality 更差。

这是一处非常真实的数据结构选择，不是“vector 永远最好”。

## 为什么愿意线性扫描 bucket

理论上可以维护 ready/busy 两套集合，但 readiness 会随跨进程 refcount 和时间变化。

如果每次远端 refcount 改变都要主动移动 block 分类，系统又需要更多跨进程通知和锁。

当前设计选择：

~~~text
size-class selection: O(log K)
+
within-bucket scan: O(M)
~~~

K 是 size class 数，M 是单 bucket block 数。

对于典型 GPU pipeline 中有限数量的 in-flight block，这往往比更复杂的状态维护更值得。

## VMM granularity 为什么会造成内部浪费

请求会先：

~~~text
requested bytes
→ round up to VMM allocation granularity
→ actual block size
~~~

所以一个 5 MB Tensor 不一定只占 5 MB allocation。

做显存预算时必须区分：

~~~text
logical payload bytes
reserved VMM bytes
~~~

否则只按 shape × dtype 会低估实际 footprint。

## create_block 的资源链

固定实现依次：

~~~text
cuMemCreate
↓
cuMemAddressReserve
↓
cuMemMap
↓
cuMemSetAccess
↓
optional cuMemExportToShareableHandle
↓
create shared IPCMetadata
~~~

这和操作系统虚拟内存模型非常相似：physical-like allocation handle、VA reservation、mapping、permission 被拆成独立步骤。

## VmmBlock 为什么不是一个 pointer

一个 block 同时保存：

~~~text
CUDA allocation handle
CUDA virtual address
aligned size
exported POSIX fd
stable block_id
shared IPCMetadata pointer
shared-memory fd/name
current generation UID
~~~

这些字段分别属于 CUDA VMM、Linux fd table、共享内存控制面和 generation protocol。

所以生产级“GPU buffer”本质上已经是一组跨层资源。

## owner container 与 free index 为什么分开

长期 owner：

~~~cpp
std::vector<std::unique_ptr<VmmBlock>> all_blocks_;
~~~

空闲索引：

~~~cpp
std::map<size_t, std::vector<VmmBlock *>> free_blocks_;
~~~

all_blocks_ 决定 metadata 什么时候真正销毁；free_blocks_ 只表示“哪些 block 当前逻辑上被归还”。

这是很值得迁移的 C++ 设计：

~~~text
one owning container
+
multiple non-owning indexes/views
~~~

如果 free list 也持有 shared ownership，就容易把“对象存在”和“对象空闲”混成一个概念。

## free() 实际不是 cuMemFree

固定 free 只是：

~~~text
push block pointer back into free_blocks_[size]
~~~

真正 VMM allocation 由 pool 持有到析构。

所以 steady-state 不反复进入 driver allocation/free。

## 逻辑 free 不等于物理可复用

block 返回 free list 以后，allocate 再通过 is_block_ready 检查：

~~~text
IPC refcount
publish timestamp
grace period
~~~

这意味着 free list 存的是“候选可复用 block”，而不是“保证立即可用 block”。

这是异步/跨进程 allocator 和普通 malloc free-list 最大的差异之一。

## IPCMetadata 是一个跨进程状态机

共享 metadata：

~~~cpp
std::atomic<int32_t> refcount;
std::atomic<uint64_t> uid;
std::atomic<uint64_t> publish_timestamp_us;
~~~

三者分别回答：

~~~text
refcount:
还有多少 remote user

uid:
这个 slot 当前是哪一代逻辑 payload

publish timestamp:
当前 generation 何时发布
~~~

三个变量不能互相替代。

## block_id 与 UID 为什么必须分离

block_id 是稳定物理 slot identity。

UID 是逻辑 generation。

时间线：

~~~text
slot 3 / uid 100 → frame A
recycle
slot 3 / uid 101 → frame B
~~~

晚到的旧 descriptor 如果只有 slot 3，会错误引用 frame B。

加入 generation：

~~~text
descriptor uid 100
shared uid 101
→ stale
~~~

这和 object pool handle、ECS entity generation、lock-free ABA counter 是同一种思想。

## refcount==0 为什么仍然不够

Publisher 发出 descriptor 后，Subscriber 可能尚未真正 import。

在这个窗口中：

~~~text
descriptor in flight
remote refcount still 0
~~~

如果马上 reuse，就会过早换代。

所以固定实现还有 grace period。

正确理解：

~~~text
refcount:
已知 active importer

grace:
覆盖尚未完成 import 的传输窗口

UID:
最终 stale detection
~~~

这是三层防护。

## 为什么 publish_timestamp 用 steady_clock

grace period 关心的是经过时长，而不是现实世界日期。

使用 monotonic steady time 可以避免 NTP/手工调时造成 wall clock 跳变。

实时系统里 timeout/deadline 逻辑一般都应该先问：这里需要 wall time 还是 monotonic duration？

## pool destructor 为什么先 drain

析构不会直接 unmap/release。

它先循环检查所有 block 是否 ready；未 ready 就 sleep 1 ms 后继续。

全部安全后才：

~~~text
munmap shared metadata
close shm fd
unlink shm
close exported fd
cuMemUnmap
cuMemAddressFree
cuMemRelease
~~~

这说明 allocator teardown 本身也是 ownership protocol 的一部分。

## 这种 polling shutdown 的代价

优点是简单、正确路径清楚。

代价是 shutdown latency 受远端 consumer 影响，而且没有更复杂 peer-liveness 机制时可能等待很久。

如果把它升级到更强的机器人 runtime，可以继续加入：

~~~text
peer lease
dead-process detection
timeout diagnostics
forced reclaim policy
~~~

这正好与 iceoryx2 dead-node cleanup 对照。

## 具体 size-class 例子

假设：

~~~text
4 MB:  [A, B]
8 MB:  [C, D]
16 MB: [E]
~~~

请求 6 MB。

ordered map lower_bound 找到 8 MB bucket。

如果 C 仍有远端引用，D ready：

~~~text
scan C → skip
scan D → choose
swap D with bucket tail
pop_back
~~~

请求获得 8 MB block。

牺牲 2 MB 内部空间，换来 VMM allocation identity 与 import cache 的复用。

## 为什么不做 block split/merge

当前 pool 更接近 size-bucket reuse，而不是 buddy allocator。

不 split 的一个实际好处是 block identity 稳定：

~~~text
(pid, block_id)
~~~

可长期作为 subscriber import cache key。

如果频繁 split/merge，跨进程 cache/invalidation protocol 会复杂很多。

所以 allocator 的目标不是单纯最高 memory utilization，还包括 IPC identity stability。

## 对自研 VLA Tensor Pool 的迁移

可以抽象：

~~~cpp
struct Block {
    AllocationHandle allocation;
    size_t bytes;
    uint32_t slot;
    uint64_t generation;
    SharedRefCount * remote_refs;
    CompletionFence last_use;
};
~~~

然后使用：

~~~text
owning vector of block metadata
+
ordered size-class free index
+
completion/lifetime eligibility
~~~

memory primitive 可以替换成 CUDA VMM、DMA-BUF、NPU handle 或 registered RDMA memory。

## 本篇最重要的五点

1. ordered map 的核心价值可以是 lower_bound，不只是“保持顺序”。
2. unordered bucket 用 vector + swap-pop 能兼顾局部性与 O(1) removal。
3. owning container 与 free/index view 应分离。
4. 异步 allocator 的 free 只是“归还候选”，真正 reuse 还要过 completion/lifetime gate。
5. 稳定 block identity 能降低 IPC setup，但必须配 generation 与 stale detection。
