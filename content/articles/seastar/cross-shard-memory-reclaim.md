# Per-shard Allocator 与 Cross-CPU Free：地址编码、MPSC Ingress 与 Owner-side Reclaim

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Shard-per-core 的设计如果只做到：

~~~text
allocation local
task local
socket local
~~~

还不够。

真正困难的是：

> **对象最终可能在别的 CPU 上结束生命周期。**

假设：

~~~text
Shard A
allocates object P
        |
        | ownership / result moves
        v
Shard B
becomes last user
        |
        | free(P)
        v
?
~~~

最直接的实现是：

~~~text
B 直接进入 A 的 allocator metadata
~~~

这会立刻把 per-shard allocator 重新变成：

~~~text
shared allocator
+
lock / atomic metadata
+
cross-core cache-line bouncing
~~~

Seastar 选择完全不同的路径：

> **foreign CPU 只提交“这个地址该释放了”这一事实，真正 allocator mutation 仍回到 allocation owner。**

于是跨核 free 变成：

~~~text
foreign caller
    ↓
address → owner CPU
    ↓
MPSC intrusive ingress
    ↓
owner Reactor detach batch
    ↓
owner local allocator free
~~~

这篇要把这条链从地址布局、fast path、原子内存序、Reactor poller、内存压力一直拆到生命周期边界。

对象级的同一原则已经在 [Sharded 与 foreign_ptr：Owner-Shard、跨核调用与析构执行域](sharded-foreign-ptr.md) 里看到：`foreign_ptr` 把对象析构送回 owner；这里继续下沉一层，看 allocator 怎样把**裸内存块的最终回收**送回 owner。

---

# 一、先明确：Seastar Allocator 本身就是 Share-nothing 设计

`memory.cc` 顶部直接说明：

~~~text
This is a share-nothing allocator
(memory allocated on one cpu
 must be freed on the same cpu)
~~~

这句话不是性能建议。

它是 allocator 的核心 correctness contract。

---

## 1. 为什么 Per-shard Allocator 这么有吸引力

如果一个 allocator 只被一个 Reactor thread 修改：

- small-pool freelist 不需要跨核锁；
- buddy/span metadata 不需要通用并发保护；
- allocation/free 热路径可大量使用普通 load/store；
- allocator metadata 留在本核 cache；
- statistics 可以 thread-local；
- local object pool 可以极轻。

---

## 2. 但现实对象会跨 Shard

例如：

~~~text
Shard 1
creates result buffer
        ↓
submit_to(0)
        ↓
Shard 0 consumes result
        ↓
Shard 0 drops last owner
~~~

所以：

~~~text
allocation owner
~~~

与：

~~~text
last user
~~~

天然可能不同。

---

# 二、关键设计：Owner CPU 不是查表查出来的

很多 allocator 会维护：

~~~text
global map:
address range → allocator arena
~~~

Seastar 当前固定源码不是这样。

它直接利用虚拟地址空间布局。

---

# 三、源码里的地址规划

注释给出：

~~~text
0x0000'sccc'vvvv'vvvv

s   → system allocator / base area
ccc → cpu number
v   → virtual address within cpu
~~~

当前源码：

~~~cpp
static constexpr unsigned cpu_id_shift = 36;
static constexpr unsigned max_cpus = 256;
~~~

---

## 3. 每个 CPU 拿一段固定 Virtual Address Region

初始化：

~~~cpp
auto base =
    mem_base()
    + (size_t(cpu_id) << cpu_id_shift);
~~~

也就是：

~~~text
CPU 0:
[mem_base + 0 << 36, ...]

CPU 1:
[mem_base + 1 << 36, ...]

CPU 2:
[mem_base + 2 << 36, ...]
~~~

---

# 四、于是 Pointer 自带 Owner 信息

源码：

~~~cpp
unsigned object_cpu_id(const void* ptr) {
    return
      (reinterpret_cast<uintptr_t>(ptr)
       >> cpu_id_shift) & 0xff;
}
~~~

所以：

~~~text
pointer address bits
        ↓
owner cpu id
~~~

不需要额外对象 header。

---

# 五、这是 Address-space Encoding

逻辑 ownership metadata：

~~~text
owner = CPU k
~~~

没有存在：

~~~text
unordered_map<void*, owner>
~~~

而是存在：

~~~text
virtual address bits
~~~

---

# 六、这种设计的价值

foreign free 时：

~~~text
O(1)
~~~

就能得到 owner。

而且没有：

- global lock；
- hash lookup；
- metadata pointer chase。

---

# 七、代价也很明确

地址布局变成 allocator ABI 的一部分。

它依赖：

- 进程虚拟地址空间够大；
-每个 CPU 能预留稳定区域；
- CPU 数量上限受编码位数限制；
- allocator 自己控制 mapping。

---

# 八、所以这不是通用 malloc 都能照搬的技巧

如果：

- 地址空间不受你控制；
- allocation 来自多个 external allocator；
- NUMA arena 动态迁移；
-进程是 32-bit；

这种方案可能不成立。

---

# 九、Local Pointer 判断也不需要 Owner 查表

源码：

~~~cpp
bool cpu_pages::is_local_pointer(void* ptr) {
    return
      (reinterpret_cast<uintptr_t>(ptr)
       & cpu_id_and_mem_base_mask)
      == local_expected_cpu_id;
}
~~~

其中：

~~~text
local_expected_cpu_id
~~~

在线程初始化时由：

~~~text
mem_base
+
cpu_id bits
~~~

预先构成。

---

# 十、Fast Path 因此非常短

逻辑：

~~~text
address prefix
==
current shard prefix
~~~

即可判断：

~~~text
local allocation
~~~

---

# 十一、Free 热路径先做什么

公共 free 最终先走：

~~~cpp
cpu_pages::try_free_fastpath(obj)
~~~

目标不是覆盖所有情况。

目标是覆盖：

> **最常见、最便宜、最值得 inline 的情况。**

---

# 十二、Fast Path 有三个条件

源码注释明确：

~~~text
1. pointer belongs to current shard
2. pointer comes from small pool
3. pool is not sampled
~~~

代码：

~~~cpp
if (is_local_pointer(ptr)) {
    auto pool =
      get_cpu_mem().to_page(ptr)->pool;

    if (pool
        && !pool->is_sampled_pool()) {
        ...
        pool->deallocate(ptr);
        return true;
    }
}
~~~

---

# 十三、为什么 Fast Path 只照顾 Small Pool

small-object allocation/free 通常是最高频路径。

它可以直接：

~~~text
pointer
→ page metadata
→ pool
→ deallocate
~~~

无需进入更复杂的大对象/span/reclaimer逻辑。

---

# 十四、为什么 sampled pool 不走 Fast Path

Heap profiling 下，

sampled allocation 还要维护：

~~~text
allocation_site
sampled live set
~~~

这些 bookkeeping 使它不再是简单：

~~~text
pool->deallocate
~~~

所以 deliberately 退出 ultra-fast path。

---

# 十五、Hot Path 的设计原则

不是：

~~~text
让所有分支都 inline
~~~

而是：

> **把 90% 的廉价 case 做得极短，把剩余语义移到 noinline slow path。**

---

# 十六、Fast Path 失败后进入 `free_slowpath`

逻辑：

~~~text
if still local
    → full local free
else
    → do_foreign_free
~~~

---

# 十七、为什么 Local 也可能走 Slow Path

可能是：

- large allocation；
- sampled small allocation；
- sized free 需要不同 bookkeeping；
- 非 small pool。

所以：

~~~text
fast-path miss
!=
foreign pointer
~~~

---

# 十八、Slow Path 第二次判断 Locality 是必要的

`try_free_fastpath()` 只回答：

~~~text
能否用最便宜方式完成
~~~

而 `free_slowpath()` 再回答：

~~~text
真正 owner 是不是当前 shard
~~~

两个问题不同。

---

# 十九、`do_foreign_free()` 先区分 Allocator Domain

foreign pointer 还有两类：

~~~text
A. Seastar allocator memory
B. non-Seastar memory
~~~

---

# 二十、为什么不能所有 Foreign Pointer 都进 xcpu_freelist

如果 pointer 来自：

~~~text
glibc/system allocator
~~~

地址里的 CPU bits没有 Seastar owner 语义。

这时应该：

~~~text
original_free_func(ptr)
~~~

而不是：

~~~text
object_cpu_id(ptr)
~~~

---

# 二十一、所以第一层判断是 `is_seastar_memory(ptr)`

逻辑：

~~~text
not Seastar memory
→ return to original allocator

Seastar memory
→ decode owner CPU
→ cross-CPU reclaim protocol
~~~

---

# 二十二、Allocator Domain 与 CPU Domain 是两个维度

一个 pointer 可能是：

~~~text
current reactor
+
foreign allocator memory
~~~

也可能：

~~~text
alien thread
+
Seastar allocator memory
~~~

所以不要把“foreign”理解成单一概念。

---

# 二十三、源码统计也明确区分它们

统计类型包含：

~~~text
cross_cpu_frees
foreign_mallocs
foreign_frees
foreign_cross_frees
~~~

说明作者本身就在区分：

~~~text
cross-shard Seastar memory
~~~

和：

~~~text
non-Seastar allocator memory
~~~

---

# 二十四、Reactor Thread 与 Alien Thread 也不同

统计更新：

~~~text
Reactor thread
→ thread-local stats

alien thread
→ atomic alien_stats
~~~

---

# 二十五、为什么统计路径也要分层

如果 Reactor 本地统计每次都 atomic：

~~~text
高频 malloc/free
→ 不必要成本
~~~

所以 local common case：

~~~text
plain thread-local increment
~~~

只有 alien thread：

~~~text
atomic fetch_add
~~~

---

# 二十六、这再次体现同一个经济原则

> **本地快路径不为少数跨域情况支付成本。**

---

# 二十七、现在进入真正的 Cross-CPU Free

foreign Seastar pointer：

~~~cpp
free_cross_cpu(
    object_cpu_id(ptr),
    ptr);
~~~

---

# 二十八、这里没有 `smp::submit_to()`

这是非常重要的区别。

对象级 `foreign_ptr`：

~~~text
需要执行 destructor / refcount semantics
→ submit task to owner shard
~~~

allocator raw block：

~~~text
只需要告诉 owner：
“这个块可以 free 了”
~~~

不需要创建完整 Future/RPC work item。

---

# 二十九、所以 Memory Reclaim 用更轻的 Dedicated Data Structure

源码：

~~~cpp
struct cross_cpu_free_item {
    cross_cpu_free_item* next;
};
~~~

以及：

~~~cpp
alignas(cache_line_size)
std::atomic<cross_cpu_free_item*>
    xcpu_freelist;
~~~

---

# 三十、待释放对象自己就是 Queue Node

~~~cpp
auto p =
    reinterpret_cast<
      cross_cpu_free_item*>(ptr);
~~~

意味着对象 payload 已经：

~~~text
logically dead
~~~

所以首字节可以改写为：

~~~text
next pointer
~~~

---

# 三十一、这叫 Intrusive Reclamation Queue

不额外分配：

~~~text
FreeRequest node
~~~

而直接复用：

~~~text
dead object's memory
~~~

---

# 三十二、为什么这是非常合理的

如果为了 free 一个对象还要：

~~~text
malloc request node
~~~

会产生荒谬循环：

~~~text
free
→ malloc metadata
→ later free metadata
~~~

intrusive node 避免了它。

---

# 三十三、它要求什么前提

只有当对象已经：

~~~text
不再被业务读取
不再被业务写
生命周期逻辑结束
~~~

才能覆盖前几个字节。

---

# 三十四、因此 Cross-CPU Freelist 不是 Deferred Destructor Queue

它接收的应该是：

~~~text
已经完成 semantic destruction
只剩 allocator storage reclaim
~~~

---

# 三十五、复杂 Destructor 为什么不能直接用这一层替代

如果对象 destructor 还要：

-关闭 fd；
-修改 container；
-释放子对象；
-执行 callback；

这些语义必须先正确执行。

`xcpu_freelist` 只关心：

~~~text
raw storage
~~~

---

# 三十六、Producer 拓扑是什么

对 owner CPU A：

~~~text
CPU B
CPU C
CPU D
alien thread
...
~~~

都可能 free A-owned block。

所以：

~~~text
many producers
→ one owner consumer
~~~

---

# 三十七、因此不是 Pair-wise SPSC

SMP RPC：

~~~text
A → B
~~~

固定 pair 可使用：

~~~text
one request SPSC
~~~

Allocator：

~~~text
any CPU → owner B
~~~

更适合：

~~~text
MPSC ingress
~~~

---

# 三十八、Topology 先决定 Data Structure

这是这一章最值得迁移的原则之一：

> **先问“有几个 producer、几个 consumer、谁是 owner”，再选 queue。**

---

# 三十九、Producer Push 是 Treiber-style CAS

源码：

~~~cpp
auto old =
    list.load(
      std::memory_order_relaxed);

do {
    p->next = old;
} while (
    !list.compare_exchange_weak(
        old,
        p,
        std::memory_order_release,
        std::memory_order_relaxed));
~~~

---

# 四十、这不是通用 Queue，而是 Lock-free Stack Ingress

每个 producer：

~~~text
new dead block P
        ↓
P.next = current head
        ↓
CAS head = P
~~~

所以链表顺序是：

~~~text
LIFO-ish
~~~

---

# 四十一、为什么 Free Reclaim 不在乎 FIFO

业务消息常常需要：

~~~text
order
~~~

但 allocator free request：

~~~text
P1 before P2
~~~

通常没有业务语义。

所以可以放弃：

~~~text
FIFO guarantee
~~~

换更轻的 lock-free push。

---

# 四十二、Data Structure 应服从 Semantics

如果没有顺序需求，

不要为了“队列”这个名字默认选择：

~~~text
FIFO MPSC queue
~~~

---

# 四十三、Release Memory Order 发布什么

Producer 在 CAS 前：

~~~cpp
p->next = old;
~~~

然后 CAS 使用：

~~~text
memory_order_release
~~~

发布新的 head。

---

# 四十四、Consumer 用 Acquire Detach

Owner：

~~~cpp
auto p =
    xcpu_freelist.exchange(
      nullptr,
      std::memory_order_acquire);
~~~

于是：

~~~text
producer writes p->next
happen-before
consumer traverses p->next
~~~

---

# 四十五、为什么 Head 的初次 Load 可以 Relaxed

Producer 只需要：

~~~text
拿一个候选旧 head
~~~

真正成功发布由：

~~~text
release CAS
~~~

保证。

如果 CAS 失败，

`compare_exchange_weak` 会更新 expected `old`，

重新：

~~~text
p->next = old
~~~

再试。

---

# 四十六、Consumer 为什么先 Relaxed Load 再 Exchange

源码：

~~~cpp
if (!xcpu_freelist.load(relaxed)) {
    return false;
}
~~~

这是：

~~~text
cheap empty fast check
~~~

---

# 四十七、这个 Relaxed Load 可以看到旧值吗

可以。

如果错误看到：

~~~text
null
~~~

最多：

~~~text
这一次 poll 不 drain
~~~

之后还会再次 poll / memory pressure drain。

---

# 四十八、所以它不是 Correctness Gate

真正 detach：

~~~text
exchange(acquire)
~~~

才是 authoritative ownership transfer。

---

# 四十九、这是典型 Speculative Fast Check

~~~text
relaxed hint
→ avoid expensive RMW when likely empty

authoritative atomic operation
→ actual state transition
~~~

---

# 五十、Owner 为什么使用 `exchange(nullptr)` 而不是逐节点 CAS Pop

因为它想一次拿走：

~~~text
当前整个 remote batch
~~~

---

# 五十一、一次 Exchange 建立清晰 Ownership Transfer

之前：

~~~text
atomic head owns list
~~~

exchange 后：

~~~text
owner local variable p owns detached list
atomic head = null
~~~

之后新的 producer：

~~~text
继续 push 新 batch
~~~

不会和 owner 遍历旧 batch冲突。

---

# 五十二、Shared Ingress 与 Local Processing 被分离

~~~text
atomic cross-core phase:
exchange one pointer

local phase:
walk linked list
free each block
~~~

---

# 五十三、这和 SMP Batch 的思路高度一致

SMP queue：

~~~text
cross-core handoff
→ local array processing
~~~

Allocator：

~~~text
cross-core atomic head
→ detach whole list
→ local allocator processing
~~~

---

# 五十四、跨核同步面应该尽量小

最贵的共享操作只有：

~~~text
producer CAS head
consumer exchange head
~~~

后续真正 allocator work：

~~~text
全部 owner-local
~~~

---

# 五十五、Consumer 处理顺序

源码：

~~~cpp
while (p) {
    auto n = p->next;
    increment local frees;
    free(p);
    p = n;
}
~~~

---

# 五十六、为什么必须先保存 `n`

一旦：

~~~text
free(p)
~~~

完成，

`p` storage 可能立即回 allocator：

- 放进 local pool；
-合并 span；
-未来被重新分配。

所以不能之后再：

~~~text
p->next
~~~

---

# 五十七、这是 Intrusive Reclaim 的标准 Traversal Rule

~~~text
read linkage
before
reclaim node storage
~~~

---

# 五十八、这里的 `free(p)` 已经是 Owner-local Free

注意：

~~~text
drain_cross_cpu_freelist()
~~~

运行于 owner shard。

所以内部调用：

~~~cpp
free(p);
~~~

会走正常 owner allocator bookkeeping。

---

# 五十九、Cross-CPU Path 不是另一套 Allocator

它只是：

~~~text
把 foreign free 延迟回 local free path
~~~

---

# 六十、最终所有 Metadata Mutation 仍集中到 Owner

例如：

- small_pool freelist；
- page/span state；
- buddy merge；
- local free counters；

都在 owner context 更新。

---

# 六十一、为什么 `xcpu_freelist` 要 Cache-line Align

声明：

~~~cpp
alignas(seastar::cache_line_size)
std::atomic<cross_cpu_free_item*>
    xcpu_freelist;
~~~

这个 head 是：

~~~text
跨 CPU write-hot
~~~

如果和 owner allocator 的其他热字段共享 cache line，

foreign producer 的 CAS 会连带把：

~~~text
不相关 metadata
~~~

一起在核间抖动。

---

# 六十二、Cache-line Isolation 是并发协议的一部分

不仅要减少：

~~~text
logical locks
~~~

还要减少：

~~~text
false sharing
~~~

---

# 六十三、为什么 MPSC Head 本身仍会 Contend

多个 foreign cores 同时 free 给同一 owner：

~~~text
all CAS same head
~~~

会产生 contention。

但相比：

~~~text
直接共享整个 allocator metadata
~~~

竞争被压缩到：

~~~text
一个极小 ingress pointer
~~~

---

# 六十四、这是 Contention Funnel

共享状态没有消失。

它被集中成：

~~~text
one atomic ingress
~~~

而不是扩散到：

- dozens of pool lists；
- page metadata；
- buddy tree/list；
- statistics。

---

# 六十五、什么时候这个 Funnel 也可能成为瓶颈

如果 workload 是：

~~~text
大量对象在 A 分配
绝大多数都在其他核 free
~~~

那么 owner A 的 `xcpu_freelist` 会成为热点。

---

# 六十六、所以 Cross-CPU Free 应是 Exception Path，不是默认数据流

Shard-per-core 最优模式仍然是：

~~~text
allocate local
use local
free local
~~~

---

# 六十七、架构应该降低 Ownership Migration

如果系统长期出现：

~~~text
cross_cpu_frees / frees 很高
~~~

它可能是性能信号：

~~~text
对象 ownership partition 不合理
~~~

---

# 六十八、统计项因此非常有价值

`memory::statistics` 暴露：

~~~text
mallocs
frees
cross_cpu_frees
...
~~~

可以观察：

\[
R_{cross}
=
\frac{cross\_cpu\_frees}
{frees}
\]

---

# 六十九、这个 Ratio 能回答什么

高比例意味着：

-对象经常跨 shard 最后释放；
-任务分区与数据 owner 不一致；
-返回值 ownership 经常逃离 origin；
-allocator locality 价值被削弱。

---

# 七十、不要只看吞吐，要看 Ownership Topology 指标

这类 runtime 指标往往比：

~~~text
CPU utilization
~~~

更能揭示架构问题。

---

# 七十一、Cross-CPU Free 的 Lifecycle 边界

源码先：

~~~cpp
if (!live_cpus[cpu_id].load(relaxed)) {
    // Thread was destroyed; leak object
    return;
}
~~~

---

# 七十二、这段代码非常值得研究

如果 owner CPU 已经销毁：

~~~text
无法再把 raw block交回 owner allocator
~~~

源码选择：

~~~text
leak
~~~

而不是：

~~~text
foreign CPU 强行 free
~~~

---

# 七十三、为什么 Leak 反而比 Wrong-owner Free 更安全

Wrong-owner free 可能：

-破坏 allocator metadata；
-访问已销毁 `cpu_pages`；
-造成 UAF；
-双重释放；
-内存结构损坏。

测试 teardown 下 leak：

~~~text
局部资源损失
~~~

但不会破坏已退出 runtime。

---

# 七十四、这说明 Reclamation Protocol 也有 Liveness Preconditions

必须满足：

~~~text
owner allocator still alive
~~~

---

# 七十五、所以 Shutdown 顺序非常重要

正常系统应避免：

~~~text
owner shard destroyed
但 foreign holders 还可能释放 owner memory
~~~

---

# 七十六、这和 `foreign_ptr` 的 Lifecycle 原则一致

先：

~~~text
drain foreign references / work
~~~

再：

~~~text
destroy owner execution domain
~~~

---

# 七十七、Owner Runtime 退出本身就是 Quiescence Barrier

只有当未来不再可能产生：

~~~text
remote release to owner
~~~

时，

owner allocator 才适合消失。

---

# 七十八、`live_cpus` 只是最后防线

它不是：

~~~text
完整 shutdown coordination protocol
~~~

只是在异常/测试 teardown case：

~~~text
避免把请求送入死 owner
~~~

---

# 七十九、Memory Ownership 生命周期也要区分

~~~text
logical object dead
~~~

与：

~~~text
owner allocator alive
~~~

是不同条件。

---

# 八十、现在看 Reactor 为什么注册专门 Poller

源码：

~~~cpp
class drain_cross_cpu_freelist_pollfn
    : public simple_pollfn<true> {
public:
    bool poll() override {
        return
          memory::drain_cross_cpu_freelist();
    }
};
~~~

---

# 八十一、Cross-CPU Reclaim 被纳入 Reactor Progress Loop

这意味着：

~~~text
free queue
~~~

不是某个独立 background thread 处理。

Owner Reactor 自己：

~~~text
周期 drain
~~~

---

# 八十二、为什么 Owner Reactor 最适合做 Consumer

因为它本来就是：

~~~text
owner allocator 的唯一正常执行线程
~~~

---

# 八十三、这避免新增 Reclaimer Lock

如果另开：

~~~text
allocator cleanup thread
~~~

它就会和 Reactor 同时修改 allocator，

又需要：

~~~text
lock
~~~

---

# 八十四、Owner-thread Polling 保持 Single-writer Metadata

cross-core producers：

~~~text
只写 atomic ingress
~~~

owner Reactor：

~~~text
唯一写 allocator internals
~~~

---

# 八十五、为什么 Producer 不 Wake Owner

源码注释：

~~~text
Other cpus can queue items for us to free;
and they won't notify us about them.

But it's okay to ignore those items,
freeing them doesn't have side effects.

We'll take care of those items
when we wake up for another reason.
~~~

---

# 八十六、这和 SMP Message Queue 完全不同

SMP request：

~~~text
可能是业务 latency-critical work
~~~

所以 target sleep 时：

~~~text
需要 maybe_wakeup
~~~

Cross-CPU free：

~~~text
只是回收容量
~~~

通常没有立即可观察业务副作用。

---

# 八十七、因此可以牺牲 Reclaim Latency

换取：

-少 eventfd write；
-少 IPI；
-少 wakeup；
-更大 batch。

---

# 八十八、Latency Class 应该分类

不是所有异步事件都值得：

~~~text
立即唤醒 CPU
~~~

---

# 八十九、可以分成

~~~text
business latency critical
→ wake

control/lifecycle critical
→ usually wake

reclamation only
→ may defer
~~~

---

# 九十、这对机器人 Runtime 很重要

例如：

~~~text
motor emergency-stop
~~~

绝不能 lazy。

但：

~~~text
release old image buffer
~~~

完全可以晚几百微秒甚至更久。

---

# 九十一、Reclamation Latency 与 Business Latency 应分离优化

这是文章主原则之一。

---

# 九十二、但一直 Lazy 会不会内存吃光

会。

所以 Seastar 还有第二条路径：

~~~text
memory pressure
→ eager reclaim
~~~

---

# 九十三、`maybe_reclaim()` 的顺序

源码：

~~~cpp
if (nr_free_pages
    < current_min_free_pages) {

    drain_cross_cpu_freelist();

    if (still low)
        run_reclaimers(sync,...);

    if (still low)
        schedule_reclaim();
}
~~~

---

# 九十四、为什么第一步先 Drain Cross-CPU List

这些内存其实：

~~~text
已经逻辑 free
~~~

只差 owner bookkeeping。

相比执行更昂贵 reclaimer：

~~~text
先收回“已经属于我的钱”
~~~

最便宜。

---

# 九十五、Reclaim Cost Hierarchy

可以理解为：

~~~text
Level 0:
owner-local freelist already free

Level 1:
cross-CPU logically free
→ detach + local free

Level 2:
sync reclaimers
→ ask caches/subsystems release memory

Level 3:
schedule broader async reclaim
~~~

---

# 九十六、Memory Pressure 改变 Reclaim Urgency

正常：

~~~text
lazy drain
~~~

压力高：

~~~text
eager drain
~~~

同一资源根据系统状态采用不同 latency policy。

---

# 九十七、这叫 Context-sensitive Reclamation

reclaim policy 不应该固定为：

~~~text
always immediate
~~~

或：

~~~text
always lazy
~~~

---

# 九十八、Memory Pressure 是 Feedback Signal

可以看成：

~~~text
free pages
        ↓
pressure threshold
        ↓
increase reclaim aggressiveness
~~~

---

# 九十九、这和控制系统有相似结构

低压力：

~~~text
不做昂贵动作
~~~

越过阈值：

~~~text
逐级增强控制输入
~~~

---

# 一百、为什么不是每次 Allocation 都 Drain

那会把：

~~~text
cross-CPU free bookkeeping
~~~

重新放到每次 allocation hot path。

---

# 一百零一、只有达到 `current_min_free_pages`

才把它提升为：

~~~text
allocation critical dependency
~~~

---

# 一百零二、正常路径与压力路径解耦

这是性能 Runtime 很常见的策略：

~~~text
steady-state:
optimize throughput

pressure-state:
optimize survival / capacity
~~~

---

# 一百零三、`drain_cross_cpu_freelist()` 返回 Bool

接口：

~~~text
true
→ did work

false
→ nothing detached
~~~

这非常适合 Reactor poller。

---

# 一百零四、Poller 要回答的是 Progress，而不是 Queue Length

Reactor 关心：

~~~text
这一轮是否推进了系统状态
~~~

而不是：

~~~text
free list 精确还有几项
~~~

---

# 一百零五、为什么不维护 Atomic Length

如果每个 foreign free 都：

~~~text
head CAS
+
counter atomic increment
~~~

会多一条跨核热点。

如果 runtime 不需要精确 queue length，

就没必要维护。

---

# 一百零六、Minimal Shared Metadata

cross-CPU ingress 只共享：

~~~text
head pointer
~~~

这是很干净的设计。

---

# 一百零七、统计 `cross_cpu_frees` 在 Producer Side 增加

push 成功后：

~~~text
increment cross_cpu_frees
~~~

统计的是：

~~~text
发生了一次 cross-CPU deallocation request
~~~

---

# 一百零八、而 `frees` 在 Owner Drain 时增加

consumer：

~~~text
increment_local(frees)
~~~

然后真正：

~~~text
free(p)
~~~

所以两个计数代表不同阶段。

---

# 一百零九、Admission 与 Completion 再次分开

~~~text
cross_cpu_frees
→ remote release admitted

frees
→ owner actually processes reclaim
~~~

---

# 一百一十、这让指标有 Protocol Semantics

不能把两个 counter 都粗暴理解成：

~~~text
free() 调用次数
~~~

---

# 一百一十一、为什么 `live_objects = mallocs - frees`

如果 remote free 已经 push，

但 owner 尚未 drain：

~~~text
frees 尚未增加
~~~

对象仍算：

~~~text
not physically reclaimed
~~~

---

# 一百一十二、这更符合 Allocator Perspective

从 allocator 容量看：

~~~text
queued remote free
~~~

还没有重新变成可分配资源。

---

# 一百一十三、单元测试专门覆盖 Cross-CPU Live-object Accounting

测试：

~~~text
Shard 1 分配大量 unique_ptr
返回到另一个 shard
clear vector
→ cause cross-cpu free
~~~

然后检查：

~~~text
live_objects
~~~

不会因为计数下溢变成巨大值。

---

# 一百一十四、这说明 Stats 也是 Reclaim Protocol 的一部分

尤其：

~~~text
remote free request
~~~

和：

~~~text
owner physical free
~~~

有时间差。

---

# 一百一十五、Foreign System Allocation 是另一条路线

如果：

~~~text
!is_seastar_memory(ptr)
~~~

代码直接：

~~~text
original_free_func(ptr)
~~~

---

# 一百一十六、为什么不需要 Owner Shard

因为 glibc/system allocator：

~~~text
有自己的线程安全 contract
~~~

不属于 Seastar per-shard share-nothing allocator。

---

# 一百一十七、不要把 Runtime Ownership Rule 错套到 External Resource

每个 allocator/domain 都有自己的：

~~~text
free contract
~~~

---

# 一百一十八、同一个 `free()` API 背后可能有多个 Ownership Protocol

~~~text
Seastar local pointer
→ local allocator

Seastar foreign pointer
→ owner MPSC ingress

system pointer
→ original_free_func
~~~

---

# 一百一十九、API Uniformity 不代表 Implementation Uniformity

这是 malloc interposition 类系统常见点。

---

# 一百二十、Alien Thread 为什么也能 Free Seastar Pointer

`do_foreign_free()` 不要求：

~~~text
caller 必须是 Reactor shard
~~~

它可以 decode：

~~~text
owner cpu
~~~

并 CAS 到 owner ingress。

---

# 一百二十一、所以 MPSC Producers 不只来自 Other Reactors

还可能来自：

~~~text
std::thread
library callback thread
alien execution context
~~~

---

# 一百二十二、这进一步证明 Pair-wise SPSC 不合适

Producer topology 是开放的 many-to-one。

---

# 一百二十三、Alien Stats 为什么要 Atomic

alien thread 没有：

~~~text
current shard-local stats
~~~

所以放入：

~~~text
global array of atomic stats buckets
~~~

---

# 一百二十四、统计本身也避免一个全局 Counter Hotspot

它不是：

~~~text
one global atomic counter
~~~

而是：

~~~text
array buckets indexed by thread hash
~~~

降低 contention。

---

# 一百二十五、从这段小代码也能看出 Seastar 的一致思路

~~~text
avoid shared hot state
shard/bucket it
~~~

不仅业务对象如此，

统计也如此。

---

# 一百二十六、现在讨论这个 Lock-free Stack 的 ABA

经典 Treiber Stack 的危险常来自：

~~~text
CAS pop
读取 head->next
head 被移除/重用
head 地址再次出现
旧 CAS 误成功
~~~

---

# 一百二十七、这里的 Consumer 不是 CAS Pop

Owner 直接：

~~~text
exchange(nullptr)
~~~

把整个 list 原子摘走。

它不做：

~~~text
load head
load head->next
CAS head=head->next
~~~

---

# 一百二十八、Producer 也只做 Push

Producer 不 dereference：

~~~text
old head payload
~~~

只把旧 head pointer 写入：

~~~text
new node's next
~~~

---

# 一百二十九、所以经典 Pop-side ABA 风险被大幅缩小

更准确地说：

> **这个协议没有通用 Treiber stack 的 CAS-pop 读改写序列，因此不依赖 hazard pointer 来保护 consumer 对 old head 的解引用。**

---

# 一百三十、但不要泛化成“所有 Lock-free Stack 都不需要 ABA 防护”

这里只能成立于当前特殊拓扑：

~~~text
producers:
push only

consumer:
atomic whole-list detach

detached nodes:
only owner traverses/free
~~~

---

# 一百三十一、这再次说明并发算法必须结合具体操作集分析

仅看到：

~~~text
atomic pointer + CAS
~~~

不能直接套：

~~~text
Treiber stack textbook conclusions
~~~

---

# 一百三十二、为什么 Consumer Detach 后 Producer 不会写 Detached Node

Producer CAS 的共享对象只有：

~~~text
atomic head
~~~

它不会修改 old head。

只修改自己新提交的：

~~~text
p->next
~~~

---

# 一百三十三、Producer CAS 失败时

`compare_exchange_weak`：

~~~text
old
~~~

更新为当前 head，

producer 再：

~~~text
p->next = old
~~~

自己的 node 尚未被发布成功，

所以可安全重写 `next`。

---

# 一百三十四、Publish Point 非常清楚

只有：

~~~text
CAS success
~~~

之后，

node 才属于 shared ingress。

---

# 一百三十五、Consumer Exchange 是 Reclaim Ownership Point

只有：

~~~text
exchange returns list
~~~

之后，

detached nodes 才完全归 owner local drain。

---

# 一百三十六、可以画成 Ownership State Machine

~~~text
BUSINESS_LIVE
    |
    | logical lifetime ends
    v
PRODUCER_LOCAL_FREE_NODE
    |
    | release CAS success
    v
SHARED_XCPU_INGRESS
    |
    | owner exchange(acquire)
    v
OWNER_LOCAL_RECLAIM_BATCH
    |
    | allocator free
    v
ALLOCATOR_AVAILABLE
~~~

---

# 一百三十七、每个 Transition 都改变 Ownership

这比单纯说：

~~~text
链表 push/pop
~~~

更准确。

---

# 一百三十八、为什么 Release/Acquire 放在两次 Ownership Handoff 上

第一处：

~~~text
producer local node
→ shared ingress
~~~

用 release。

第二处：

~~~text
shared ingress
→ owner local batch
~~~

用 acquire。

---

# 一百三十九、Memory Order 与 Ownership Model 是一回事

不是背口诀：

~~~text
CAS 应该用 release
exchange 应该用 acquire
~~~

而是问：

> **谁在发布哪些普通内存写？谁在接管后需要看到它们？**

---

# 一百四十、这里需要可见的普通写就是 `p->next`

consumer 必须看到正确 list linkage。

---

# 一百四十一、业务 Payload 不需要同步给 Consumer

因为业务 payload 已经：

~~~text
逻辑死亡
~~~

owner reclaim 只需要：

-指针；
- allocator metadata；
- next linkage。

---

# 一百四十二、这让 Synchronization Contract 极小

同步的不是真正对象状态，

而只是：

~~~text
reclaim linkage
~~~

---

# 一百四十三、为什么对象首部可以覆盖

因为 logical lifetime 已结束，

C++ object semantics 已经不再需要保存：

~~~text
原 payload bytes
~~~

---

# 一百四十四、但必须注意 Destructor Boundary

对于非-trivial C++ object：

~~~text
析构必须已经发生
~~~

才能把 storage 当：

~~~text
cross_cpu_free_item
~~~

---

# 一百四十五、所以 Object Destruction 与 Storage Deallocation 必须分开理解

~~~text
T::~T()
~~~

和：

~~~text
operator delete(storage)
~~~

是两层。

---

# 一百四十六、foreign_ptr 更偏前一层

它保证：

~~~text
最后 shared pointer release / object destructor
~~~

回 owner domain。

---

# 一百四十七、xcpu_freelist 更偏后一层

它保证：

~~~text
raw allocator storage bookkeeping
~~~

回 owner allocator。

---

# 一百四十八、实际 C++ delete 可能串起两层

~~~text
delete T
→ destructor
→ operator delete
~~~

如果整个过程发生在正确 owner，

两层自然都正确。

---

# 一百四十九、但 allocator interception 让 Free 层自己也必须能处理 Foreign Caller

所以即使 higher-level code 没用 foreign_ptr，

底层 allocator 仍提供：

~~~text
cross-CPU safety net
~~~

---

# 一百五十、Safety Net 不等于推荐 Ownership 模式

能安全：

~~~text
foreign free
~~~

不代表应该设计成：

~~~text
所有对象都频繁 foreign free
~~~

---

# 一百五十一、为什么 Owner-side Batch 可能更 Cache-friendly

一批 remote free：

~~~text
detach once
~~~

然后 owner 连续：

~~~text
to_page
pool deallocate
span update
~~~

减少来回跨核 metadata mutation。

---

# 一百五十二、但链表本身是 LIFO，地址 locality 未必理想

批量的主要收益不是：

~~~text
严格顺序 locality
~~~

而是：

~~~text
amortize shared synchronization
keep allocator mutation local
~~~

---

# 一百五十三、Batch Size 是自适应的

不像固定：

~~~text
batch_size=16
~~~

这里 batch 大小等于：

~~~text
自上次 drain 后累计的 free 数
~~~

---

# 一百五十四、这是时间驱动的自然 Batch

如果 owner 忙：

~~~text
积累更多
→ 一次 detach 更多
~~~

如果流量低：

~~~text
少量对象
→ 下次 poll处理
~~~

---

# 一百五十五、这也解释了为什么不 Wake

不 wake 会自然：

~~~text
提高 batch size
~~~

降低每个 remote free 的同步成本。

---

# 一百五十六、代价是 Temporarily Higher Memory Footprint

queued free：

~~~text
已经业务死亡
但还没有重新进入 allocator free capacity
~~~

所以 memory footprint 暂时偏高。

---

# 一百五十七、Memory Pressure Path 正是补偿这个 Tradeoff

一旦 free pages 不足：

~~~text
立即 drain
~~~

缩短 reclaim latency。

---

# 一百五十八、这形成一个闭环

~~~text
low pressure
→ defer
→ batch
→ throughput better

high pressure
→ eager drain
→ capacity recovered
→ avoid allocation failure
~~~

---

# 一百五十九、这就是 Adaptive Deferred Reclamation

不是固定延迟。

---

# 一百六十、为什么 `maybe_reclaim()` 先同步 Reclaimer，后 Schedule Reclaim

如果 cross-CPU free 仍不够：

~~~text
run_reclaimers(sync)
~~~

先尝试立即可回收的 subsystem。

还不够：

~~~text
schedule_reclaim()
~~~

让更广泛回收异步继续。

---

# 一百六十一、Reclaimer 是更高成本机制

它可能要求：

- cache shrink；
- data structure eviction；
- application callback；
- page release。

所以应该在：

~~~text
free remote dead blocks
~~~

之后再做。

---

# 一百六十二、Cheap Reclaim First

这是通用内存系统原则：

> **先回收已经逻辑死亡、只差 bookkeeping 的资源，再请求活跃 subsystem 做语义性 eviction。**

---

# 一百六十三、为什么 Cross-CPU Free 没有 Future/ACK

Allocator caller 调：

~~~text
free(ptr)
~~~

通常不需要知道：

~~~text
owner 什么时候真的把 block 放回 pool
~~~

---

# 一百六十四、它的 completion semantics 是 Weak

~~~text
free()
returns
→ caller may no longer use pointer
~~~

但不保证：

~~~text
memory immediately reusable on owner
~~~

---

# 一百六十五、这和 `foreign_ptr::destroy()` 不同

explicit destroy：

~~~text
Future ready
→ owner-side semantic destruction completed
~~~

allocator free：

~~~text
fire-and-forget reclaim request
~~~

---

# 一百六十六、不同资源需要不同 Completion Strength

不要所有 cleanup 都设计成：

~~~text
Future + ACK
~~~

那会增加巨大开销。

---

# 一百六十七、Free 的关键 Contract 只需要 Caller-side Finality

一旦 `free(ptr)`：

~~~text
caller 永远不能再访问 ptr
~~~

owner 什么时候真正 recycling，

对 caller 无需可见。

---

# 一百六十八、这就是为什么可以使用无通知 Deferred Reclaim

# 一百六十九、如果业务真的需要 Capacity Barrier 怎么办

例如：

~~~text
释放一大批 buffer
然后必须确保下一次 allocation 能拿到
~~~

这时可能需要显式：

~~~text
memory pressure drain
owner synchronization
~~~

而不能把普通 free 当 completion barrier。

---

# 一百七十、API Contract 要区分

~~~text
lifetime ends
~~~

与：

~~~text
capacity reclaimed
~~~

---

# 一百七十一、为什么 `live_cpus` 用 Relaxed

它在这里更多是：

~~~text
best-effort lifecycle guard
~~~

而不是用来发布：

~~~text
复杂 owner allocator state
~~~

初始化/销毁有更大的 runtime ordering contract。

---

# 一百七十二、不能把 Relaxed `live_cpus` 当完整 Lifetime Synchronization

源码自身注释说明该 leak path主要是：

~~~text
boost unit-tests
~~~

正常 shutdown 依赖更高层生命周期正确。

---

# 一百七十三、这是源码阅读的重要方法

看到 atomic：

~~~text
不要自动推断它承担所有生命周期同步
~~~

必须看：

-谁写；
-谁读；
-它保护什么；
-更高层是否已有 quiescence。

---

# 一百七十四、`all_cpus[cpu_id]` 为什么能直接取 Owner Allocator

初始化：

~~~text
all_cpus[cpu_id] = this
~~~

然后 foreign free：

~~~text
all_cpus[owner]
→ xcpu_freelist
~~~

---

# 一百七十五、这里确实存在 Global Directory

但它不是：

~~~text
per-allocation ownership hash
~~~

而只是：

~~~text
CPU id → cpu_pages*
~~~

固定小数组。

---

# 一百七十六、Address Decoding 把查找粒度从 Object 降到 Owner

~~~text
pointer
→ CPU id via address
→ cpu_pages via fixed array
~~~

这是两级轻量 lookup。

---

# 一百七十七、这比 Object→Arena Hash Map 更稳定

尤其 hot path：

-无 allocation metadata lookup；
-无 string/key；
-无 lock。

---

# 一百七十八、CPU 数量上限体现 Encoding Tradeoff

源码：

~~~text
max_cpus = 256
~~~

因为当前 owner extraction只取：

~~~text
8 bits
~~~

---

# 一百七十九、这提醒我们：Bit-field Ownership Encoding 是 Capacity Contract

设计时要提前考虑：

~~~text
未来最大 shard 数
~~~

---

# 一百八十、不能把 Layout Magic Number 当纯实现细节

`cpu_id_shift=36`：

~~~text
FIXME: make dynamic
~~~

已经说明作者知道：

~~~text
地址布局与平台/规模耦合
~~~

---

# 一百八十一、NUMA 又是什么关系

CPU owner：

~~~text
负责 allocator metadata
~~~

NUMA node：

~~~text
决定物理内存 locality
~~~

两者相关但不是同一个维度。

---

# 一百八十二、Shard-per-core 通常希望

~~~text
CPU owner
≈
NUMA-local allocator owner
~~~

减少：

-remote memory access；
-remote free metadata；
-cross-socket coherence。

---

# 一百八十三、Cross-CPU Free 不是解决 NUMA Data Placement

它只解决：

~~~text
正确释放 owner allocation
~~~

如果数据长期在错误 NUMA node 被消费，

仍有性能问题。

---

# 一百八十四、Ownership Correctness 与 Placement Optimization 分开

# 一百八十五、与 Folly MPSC Queue 的对照

Folly 通用 queue 要考虑：

-业务 payload；
-order；
-consumer blocking/wakeup；
-arbitrary lifetime；
-reclamation。

Seastar xcpu free list：

~~~text
payload already dead
no ordering requirement
single owner consumer
no immediate wakeup
node storage already available
~~~

所以能极度简化。

---

# 一百八十六、Special-purpose Concurrent Structure 往往比 Generic Queue 更便宜

这是源码设计很重要的一点。

---

# 一百八十七、不要一看到“跨线程请求”就用 `std::queue + mutex`

先问：

~~~text
payload还活着吗？
需要 FIFO 吗？
需要 wakeup 吗？
consumer固定吗？
node能 intrusive 吗？
~~~

---

# 一百八十八、这个场景的答案

~~~text
payload: dead
FIFO: no
wakeup: usually no
consumer: one owner
intrusive: yes
~~~

自然得到：

~~~text
atomic intrusive MPSC stack
~~~

---

# 一百八十九、机器人 Object Pool 的直接迁移

假设视觉线程：

~~~text
camera buffer pool owner = Thread A
~~~

buffer 传给：

- detector B；
- tracker C；
- logger D。

最后 consumer D 释放。

---

# 一百九十、错误做法

所有线程直接：

~~~text
lock pool mutex
return buffer
~~~

---

# 一百九十一、Owner-return 模式

~~~text
D
→ push buffer to A.remote_free
→ A batch drains
→ local pool return
~~~

---

# 一百九十二、尤其适合固定尺寸 Buffer Pool

因为 dead buffer 可以直接：

~~~text
复用 header 作为 next pointer
~~~

---

# 一百九十三、但如果 Buffer 还被 DMA 使用就不能这样做

必须先确保：

~~~text
device completion
~~~

否则逻辑生命周期还没结束。

---

# 一百九十四、CPU Reclaim 与 Device Reclaim 是两层

~~~text
GPU/NIC DMA complete
        ↓
semantic/resource lifetime ends
        ↓
remote-free enqueue
        ↓
owner pool reclaim
~~~

---

# 一百九十五、不要把 “last C++ reference dropped” 自动等同 “device done”

异构系统尤其要明确：

- stream/event；
- fence；
- completion callback。

---

# 一百九十六、然后才进入 allocator ownership protocol

# 一百九十七、Cross-shard Memory Reclaim 与 Backpressure

free ingress 本身没有显式 capacity bound。

这是因为：

~~~text
node storage = object storage
~~~

每个 pending request 已经占用自己的内存。

---

# 一百九十八、所以 Queue Node Allocation 不会 OOM

它不需要额外：

~~~text
malloc queue node
~~~

---

# 一百九十九、但 Pending Memory 本身仍会积累

如果 owner 长时间不运行：

~~~text
many dead blocks
→ list grows
→ usable free pages not recovered
~~~

---

# 二百、系统级 Backpressure 通过 Memory Pressure 间接出现

不是：

~~~text
xcpu queue full
~~~

而是：

~~~text
nr_free_pages low
→ maybe_reclaim
→ eager drain
~~~

---

# 二百零一、这是一种 Resource-pressure Backpressure

和 bounded queue 不同：

~~~text
queue capacity
~~~

不是直接 signal。

底层资源量：

~~~text
free pages
~~~

才是反馈。

---

# 二百零二、这说明 Backpressure 不一定长成 Semaphore

可以来自：

- HWM；
- credit；
- memory pressure；
- token bucket；
- queue capacity；
- deadline budget。

---

# 二百零三、Reclaim Poller 为什么是 `simple_pollfn<true>`

它是 Reactor progress source的一部分。

关键不是 template 参数本身，

而是：

~~~text
poll returns whether progress happened
~~~

并持续纳入 event loop。

---

# 二百零四、这让内存回收变成 Runtime Scheduler 的普通一环

Reactor 不只是 I/O：

- tasks；
- timers；
- SMP；
- syscall；
- memory reclaim。

---

# 二百零五、所以“Reactor = epoll loop”是不完整的

Seastar Reactor 更接近：

> **每 shard 的完整 CPU runtime。**

---

# 二百零六、Allocator Progress 也是 Reactor Progress

这点在普通 event-loop 教程里很少讲。

---

# 二百零七、如果 Reclaim Poller 占用太久怎么办

一次 detached list如果极长：

~~~text
while(p)
~~~

可能增加 Reactor latency。

源码这里选择直接 drain whole detached batch。

---

# 二百零八、这是 Throughput 与 Tail Latency 的潜在 Tradeoff

如果 workload 产生巨大 remote-free burst：

~~~text
one poll
→ many frees
~~~

可能形成长 CPU slice。

---

# 二百零九、为什么源码仍可接受这种选择

可能基于：

- remote free 应为少数；
- local allocator free 较便宜；
-批量回收压力大时本来就应该尽快释放；
-避免复杂 partial-list state。

---

# 二百一十、设计自己的 Runtime 时要根据 Budget 决定

可以选择：

~~~text
drain all
~~~

或：

~~~text
drain at most N
reschedule remainder
~~~

---

# 二百一十一、控制系统通常更关心 Worst-case Latency

如果是 1 kHz real-time loop，

你可能更偏向：

~~~text
bounded reclaim budget per tick
~~~

而不是一次清空几十万 node。

---

# 二百一十二、但不要擅自说 Seastar 当前源码做了 Bounded Drain

当前固定源码是：

~~~text
while(p)
→ drain detached list fully
~~~

必须按源码说。

---

# 二百一十三、这就是“机制”与“可迁移改造”要区分

源码事实：

~~~text
full batch drain
~~~

设计建议：

~~~text
real-time runtime may impose budget
~~~

不能混写。

---

# 二百一十四、为什么 Owner Drain 后没有把链表反转

因为顺序无语义。

Producer stack顺序：

~~~text
last pushed first processed
~~~

完全可接受。

---

# 二百一十五、如果 Reclaim 有 Priorities 呢

例如：

~~~text
large buffers
small objects
device resources
~~~

一个单 stack 可能不够。

可改成：

- size-class ingress；
-priority queues；
- separate reclaim domains。

---

# 二百一十六、Seastar 当前机制追求最小开销

它不是通用 resource manager。

---

# 二百一十七、为什么 `free_cross_cpu` 先检查 `live_cpus`

如果 owner 已不存在，

`all_cpus[cpu_id]` 指向的 runtime state可能已经不可用。

---

# 二百一十八、顺序是

~~~text
check owner liveness
→ get owner ingress
→ CAS publish
~~~

---

# 二百一十九、但这不是严格的 Lock-free Lifetime Reservation

理论上：

~~~text
check live
~~~

和：

~~~text
owner destruction
~~~

之间如果没有更高层 quiescence，

仍可能存在 lifetime race。

---

# 二百二十、正常正确性依赖更高层 Runtime Shutdown Protocol

这正说明：

> **一个 relaxed liveness flag 不能替代系统级 quiescence。**

---

# 二百二十一、源码注释说该异常路径主要用于 Unit Test

所以不要把它包装成：

~~~text
通用 safe concurrent owner retirement algorithm
~~~

---

# 二百二十二、跨核 Reclaim 的真正 Lifetime Contract

应该是：

~~~text
所有可能 foreign-free 的 allocation
都在 owner allocator shutdown 前归还
~~~

---

# 二百二十三、这与 Thread Pool Shutdown 一样

不能：

~~~text
销毁 worker queue
然后 producer 还能 enqueue
~~~

---

# 二百二十四、Registry/Queue Lifetime 总要有 Submission Quiescence

# 二百二十五、为什么这章与前面 Callback Quiescence 一脉相承

都需要分：

~~~text
stop future submissions
drain already-admitted work
destroy receiver/owner state
~~~

---

# 二百二十六、Cross-CPU Free 只省略了“业务 completion”

但 owner lifecycle 仍要管理 ingress。

---

# 二百二十七、完整四类 Free Path

可以整理成：

~~~text
free(ptr)
   |
   v
try_free_fastpath
   |
   +-- local small unsampled
   |      ↓
   |   pool->deallocate
   |
   +-- miss
          ↓
      free_slowpath
          |
          +-- local Seastar
          |      ↓
          |   full local free
          |
          +-- foreign
                 ↓
           do_foreign_free
                 |
          +------+------+
          |             |
 non-Seastar ptr    Seastar ptr
          |             |
 original_free      object_cpu_id
                        |
                        v
                  free_cross_cpu
                        |
                        v
                  owner xcpu list
~~~

---

# 二百二十八、Owner 侧

~~~text
Reactor poll
or
memory pressure
       |
       v
drain_cross_cpu_freelist
       |
       v
exchange(nullptr, acquire)
       |
       v
local detached chain
       |
       v
free each block locally
~~~

---

# 二百二十九、这形成双阶段 Reclaim

Caller：

~~~text
logical deallocation submission
~~~

Owner：

~~~text
physical allocator reclamation
~~~

---

# 二百三十、这种分离可以显著降低共享

但也增加：

- deferred capacity；
-shutdown obligation；
- owner progress dependency；
- observability need。

---

# 二百三十一、Owner Progress Dependency 很关键

如果 owner Reactor 被：

~~~text
长时间 CPU-bound task
~~~

阻塞，

cross-CPU list也不会 drain。

---

# 二百三十二、所以 Cooperative Runtime 要控制 Task Slice

这又回到：

~~~text
need_preempt
scheduling group
task budget
~~~

---

# 二百三十三、Memory Reclaim 与 CPU Scheduler 是耦合的

如果 Scheduler 不给 Reclaim Poller progress，

内存容量可能迟迟回不来。

---

# 二百三十四、Runtime 子系统不是孤岛

~~~text
scheduler fairness
→ poller progress
→ memory reclaim
→ allocation availability
→ task progress
~~~

是一条反馈环。

---

# 二百三十五、这也是为什么研究源码不能只看单个类

# 二百三十六、与 `foreign_ptr` 的完整对照

`foreign_ptr`：

~~~text
unit:
C++ ownership object

transport:
smp::submit_to

completion:
optional Future

owner work:
refcount/destructor

cost:
full cross-shard work item
~~~

---

# 二百三十七、`xcpu_freelist`

~~~text
unit:
raw memory block

transport:
intrusive MPSC atomic stack

completion:
none to caller

owner work:
allocator free/bookkeeping

cost:
one CAS push
~~~

---

# 二百三十八、为什么不能所有资源都用一种机制

复杂对象需要：

~~~text
semantic destruction
~~~

raw block只需要：

~~~text
storage reclaim
~~~

抽象层不同。

---

# 二百三十九、机制成本应该匹配语义强度

> **越底层、越高频、语义越简单的路径，越应该避免通用 RPC/Promise 开销。**

---

# 二百四十、与 SMP Request Queue 的拓扑对照

SMP：

~~~text
specific A → B
specific origin completion B → A
~~~

所以：

~~~text
pair-wise SPSC × 2
~~~

Allocator：

~~~text
A/B/C/... → owner X
no reverse completion
~~~

所以：

~~~text
one MPSC ingress
~~~

---

# 二百四十一、Topology Matrix

| 场景 | Producers | Consumer | Completion | 合适机制 |
|---|---:|---:|---|---|
| A→B RPC | 1 logical pair | B | 回 A | pair-wise SPSC |
| callback registry | many | many/readers | depends | snapshot/RCU/locks |
| cross-CPU free | many | one owner | 无 | intrusive MPSC stack |
| owner object operation | many callers | one owner | Future | message passing |

---

# 二百四十二、这张表比“lock-free 更快”有价值

数据结构由：

~~~text
concurrency topology
+
semantic requirements
~~~

共同决定。

---

# 二百四十三、Memory Order 也由 Ownership Transfer 推导

Producer：

~~~text
prepare node
→ release publish
~~~

Consumer：

~~~text
acquire detach
→ consume linkage
~~~

---

# 二百四十四、不需要 Seq-Cst

因为这里没有要求：

~~~text
所有 CPU 对所有原子操作建立单一全序
~~~

只需要：

~~~text
per-node publication visibility
~~~

---

# 二百四十五、这就是为什么 Release/Acquire 足够表达协议

# 二百四十六、Relaxed Statistics 又为什么合理

统计值不参与：

~~~text
allocator correctness decision
~~~

只是 observability。

所以：

~~~text
atomic relaxed
~~~

通常足够。

---

# 二百四十七、不要把“atomic”都用同一种内存序

每个 atomic 应问：

~~~text
它是否发布数据？
是否用于 ownership transfer？
还是只计数？
~~~

---

# 二百四十八、三种 Atomic 在同一文件里用途完全不同

~~~text
xcpu head CAS
→ publication / ownership

live_cpus
→ lifecycle hint/guard

stats
→ observability counter
~~~

---

# 二百四十九、这正适合学习 Memory Model

# 二百五十、一个机器人图像 Buffer Pool 设计示例

假设：

~~~text
Camera thread A owns 256 buffer pool
~~~

图像流：

~~~text
A captures
→ B detector
→ C tracker
→ D logger
~~~

最后 ref 在 D 消失。

---

# 二百五十一、可以设计

Buffer header：

~~~cpp
struct Buffer {
    Buffer* remote_next;
    ...
};
~~~

Owner：

~~~text
owner_id = A
~~~

---

# 二百五十二、D 最后释放

~~~text
if current == owner
→ local pool return

else
→ CAS push owner.remote_free
~~~

---

# 二百五十三、A 每轮合适时机

~~~text
exchange remote_free
→ local batch
→ return buffers to pool
~~~

---

# 二百五十四、如果 Camera Pool 快耗尽

~~~text
eager drain remote_free
~~~

对应 Seastar：

~~~text
memory pressure → drain first
~~~

---

# 二百五十五、如果 A 睡眠，要不要 Wake

取决于：

~~~text
buffer availability 是否业务 critical
~~~

相机实时 pipeline可能要比 Seastar generic malloc更激进。

---

# 二百五十六、所以可迁移的是原则，不是原样照搬 Policy

机制：

~~~text
owner-side reclaim
~~~

可以复用。

wakeup 策略：

~~~text
根据业务 latency
~~~

重新设计。

---

# 二百五十七、对于 1 kHz 控制系统

不要在 hard-ish control tick里：

~~~text
无上限 drain arbitrary remote list
~~~

可考虑：

~~~text
bounded N reclaim per cycle
+
background full drain
~~~

---

# 二百五十八、对于大吞吐网络 Runtime

更可能接受：

~~~text
full batch drain
~~~

换吞吐。

---

# 二百五十九、这就是 Scheduling Policy 与 Reclaim Mechanism 分离

# 二百六十、Cross-CPU Free 还说明一个设计习惯

当对象已经死亡：

~~~text
它本身就是最便宜的消息载体
~~~

---

# 二百六十一、类似技巧还有

- intrusive ready queue；
- slab freelist；
- lock-free retire list；
- object pool return list；
- deferred delete list。

---

# 二百六十二、但必须防止 Double Free

一旦 block 已经进入：

~~~text
xcpu_freelist
~~~

业务必须永远不再提交第二次。

---

# 二百六十三、MPSC Stack 不会替你 Detect Duplicate Node

如果同一 pointer push 两次：

~~~text
list topology
~~~

甚至可能形成：

- cycle；
-self-loop；
-double reclaim。

---

# 二百六十四、所以 Memory Safety Preconditions 仍然来自上层 Ownership

Lock-free data structure不是：

~~~text
double-free sanitizer
~~~

---

# 二百六十五、为什么 Per-shard Allocator 让 Bugs 更容易局部化

正常路径：

~~~text
owner-only metadata mutation
~~~

若出现 pool corruption，

搜索范围主要是：

- owner local allocation/free；
-cross-CPU ingress duplicate/wrong pointer；
- lifecycle bugs。

比全局并发 allocator更容易推理。

---

# 二百六十六、但地址编码错误会很致命

如果错误 pointer 被误识别成 Seastar memory：

~~~text
object_cpu_id
~~~

可能指向错误 owner。

所以：

~~~text
is_seastar_memory
~~~

是重要 domain gate。

---

# 二百六十七、Null Pointer 又单独处理

~~~cpp
if (!ptr)
    return;
~~~

避免把 0：

~~~text
误 decode
~~~

成某个 owner。

---

# 二百六十八、Free API 的输入分类顺序本身就是 Safety Protocol

~~~text
null
→ no-op

non-Seastar
→ system free

Seastar
→ owner-decode path
~~~

---

# 二百六十九、不要在 Domain 判断前先读 Allocator Metadata

foreign pointer可能根本不属于你的 allocator。

---

# 二百七十、这在 Custom Allocator 集成中非常重要

# 二百七十一、为什么 `object_size(ptr)` 可以直接找 Owner cpu_pages

源码：

~~~cpp
cpu_pages::all_cpus[
    object_cpu_id(ptr)]
  ->object_size(ptr);
~~~

同样依赖：

~~~text
address owner encoding
~~~

---

# 二百七十二、Address Encoding 不只是 Free 优化

它成为：

~~~text
allocator-wide routing primitive
~~~

---

# 二百七十三、这是“Pointer as Capability Metadata”的一个弱形式

pointer bits除了定位数据，

还隐含：

~~~text
owner domain
~~~

---

# 二百七十四、但它不是 Security Capability

不要混淆。

任何知道地址的人并没有因此获得：

~~~text
权限认证
~~~

这里只是 runtime metadata encoding。

---

# 二百七十五、与 Tagged Pointer 的区别

Tagged pointer常利用：

~~~text
alignment low bits
~~~

Seastar这里利用：

~~~text
预留 virtual address region high bits
~~~

语义不同。

---

# 二百七十六、为什么 Virtual Address 是很有价值的 Runtime Resource

现代 64-bit 系统地址空间极大。

可以拿来编码：

- shard；
- arena；
- region type；
- object class。

---

# 二百七十七、代价是 Layout Constraints

任何地址空间随机化、外部 mapping、超大规模 shard都要考虑冲突。

---

# 二百七十八、这一点与 Shared-memory Offset 的思路相似

地址/offset不仅定位数据，

也能帮助恢复：

~~~text
ownership / segment metadata
~~~

---

# 二百七十九、最终统一：Seastar 的“少共享”不等于“没有跨核原子”

仍然有：

~~~text
xcpu_freelist atomic CAS
SMP queue atomics
sleep/wakeup atomics
~~~

---

# 二百八十、真正目标是把共享缩小到边界

内部主体：

~~~text
owner-local
~~~

边界：

~~~text
small explicit atomic/message channel
~~~

---

# 二百八十一、这比“全部 lock-free”更准确

Seastar 的架构核心：

> **share-nothing inside, explicit synchronization at ownership boundaries。**

---

# 二百八十二、对 C++ Runtime 设计的最终建议

如果一个对象池/allocator有稳定 owner：

第一步先问：

~~~text
foreign free 是常态还是异常？
~~~

如果异常：

~~~text
owner-local fast path
+
remote-return ingress
~~~

通常比：

~~~text
全局 thread-safe pool
~~~

更合理。

---

# 二百八十三、第二问：Remote Free 是否需要顺序

不需要：

~~~text
stack
~~~

就够。

需要：

~~~text
考虑 MPSC queue
~~~

---

# 二百八十四、第三问：是否要立即 Wake Owner

如果 release本身没有用户可见副作用：

~~~text
可 lazy
~~~

如果释放直接决定：

- buffer availability；
-deadline；
-safety；

可能要 wake。

---

# 二百八十五、第四问：是否需要 Completion ACK

普通 storage reclaim：

~~~text
通常不需要
~~~

复杂 resource shutdown：

~~~text
可能需要 Future
~~~

---

# 二百八十六、第五问：Owner 如何定位

可选：

- object header；
- arena metadata；
- pointer region；
- handle table；
- shard id field。

Seastar 选择：

~~~text
virtual-address encoding
~~~

---

# 二百八十七、第六问：Owner 什么时候可以销毁

必须先建立：

~~~text
submission quiescence
~~~

确保未来不会再出现 remote return。

---

# 二百八十八、第七问：Pressure 时怎么改变 Policy

低压力：

~~~text
batch/defer
~~~

高压力：

~~~text
eager drain
~~~

---

# 二百八十九、这七个问题比“用不用 lock-free”更重要

# 二百九十、完整源码路径总结

~~~text
operator delete / free
        |
        v
try_free_fastpath
        |
        +-- local small pool
        |      |
        |      v
        |   direct deallocate
        |
        v
free_slowpath
        |
        +-- local
        |     |
        |     v
        |  local full free
        |
        v
do_foreign_free
        |
        +-- null
        |     → return
        |
        +-- system pointer
        |     → original_free_func
        |
        +-- Seastar foreign pointer
              |
              v
        object_cpu_id(address)
              |
              v
        free_cross_cpu(owner,p)
              |
              v
   release-CAS intrusive push
              |
              v
       owner xcpu_freelist
              |
       +------+------+
       |             |
 periodic poll   memory pressure
       |             |
       +------+------+
              v
  exchange(nullptr, acquire)
              |
              v
      detached local list
              |
              v
      owner-local free()
~~~

---

# 二百九十一、和前一章组成的完整 Ownership Stack

~~~text
Mutable service state
→ sharded<Service>
→ computation returns owner

C++ smart ownership
→ foreign_ptr
→ final ref/destructor returns owner

Raw allocator storage
→ xcpu_freelist
→ physical free returns owner
~~~

---

# 二百九十二、三个层级不能互相替代

`sharded<T>` 不负责：

~~~text
raw malloc block reclaim
~~~

`foreign_ptr` 不负责：

~~~text
arbitrary remote method synchronization
~~~

`xcpu_freelist` 不负责：

~~~text
C++ semantic destructor
~~~

---

# 二百九十三、分层越清楚，生命周期越容易证明

# 二百九十四、源码作者要守住的核心不变量

第一：

> **Seastar allocator 的正常 metadata mutation 属于 allocation owner shard；foreign caller 只提交 release request。**

第二：

> **Owner CPU 可以由 allocator-controlled virtual-address bits直接恢复，不需要 per-object global map。**

第三：

> **Free fast path只覆盖 local + small-pool + unsampled 的高频廉价 case，剩余语义明确退到 slow path。**

第四：

> **跨核 free 的拓扑是 many-producer / one-owner-consumer，因此使用 intrusive MPSC stack，而不是 pair-wise SPSC 或通用 MPMC queue。**

第五：

> **dead object storage本身可以作为 reclaim node，但前提是 C++/业务生命周期已经真正结束。**

第六：

> **producer 的 release CAS 与 owner 的 acquire exchange构成 ownership/publication handoff；relaxed pre-check只是优化，不是正确性边界。**

第七：

> **owner 用 `exchange(nullptr)` 一次摘走当前整批 remote frees，把共享同步压缩成一次原子 ownership transfer，后续 allocator work全部本地执行。**

第八：

> **reclamation 不要求 FIFO，因此 LIFO intrusive stack 是合理降成本，而不是语义缺陷。**

第九：

> **Cross-CPU free 通常不唤醒 owner，因为 reclaim latency 与业务 latency可以分离；memory pressure 再把 lazy policy切换成 eager drain。**

第十：

> **free request 被接受与 memory 真正恢复为 allocator capacity 是两个阶段，stats 也分别反映 cross-CPU admission 与 owner-side physical free。**

第十一：

> **owner allocator shutdown 前必须建立 foreign-release quiescence；`live_cpus` 只是异常防线，不是完整并发生命周期协议。**

第十二：

> **foreign_ptr 的对象级析构与 xcpu_freelist 的 storage-level reclaim 是同一 owner-computes 原则的不同层级，不能混为一种机制。**

---

# 二百九十五、最终心智模型

如果只看一张图：

~~~text
             SHARE-NOTHING ALLOCATOR
                      |
         +------------+------------+
         |                         |
   local deallocation          foreign deallocation
         |                         |
         v                         v
 local fast/full free       decode owner from address
                                   |
                                   v
                           owner atomic ingress
                                   |
                                   | no immediate wake
                                   v
                            owner Reactor poll
                                   |
                    +--------------+--------------+
                    |                             |
                normal load                  memory pressure
                    |                             |
                 lazy drain                    eager drain
                    |                             |
                    +--------------+--------------+
                                   v
                         exchange whole batch
                                   |
                                   v
                         local allocator free
~~~

如果只记一个结论：

> **Seastar 的 per-shard allocator 并不是“每个核一份 malloc”这么简单；真正让 share-nothing 成立的是 free 路径也尊重 owner。Pointer 的地址直接编码 allocation owner，foreign CPU 只用 release-CAS 把已经死亡的 storage 放进 owner 的 MPSC ingress，owner Reactor 再用 acquire exchange 整批接管并在本地修改 allocator metadata。正常时可以延迟回收换取批量和更少 wakeup，内存压力出现时再主动 drain——这是一套从地址布局、并发拓扑到调度策略都围绕 ownership 设计的完整协议。**
