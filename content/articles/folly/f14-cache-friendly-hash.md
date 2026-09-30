# F14 Hash Table：为什么高性能 Hash Map 先比较 1-Byte Tag，而不是先追完整 Key

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

`std::unordered_map` 的问题不只是平均复杂度也是 O(1)。在热查找路径里，更关键的是：一次 lookup 要触碰多少 cache line、发生多少 pointer chasing、做多少次完整 key comparison。

Folly F14 的设计目标就是把“昂贵的完整比较”尽可能推迟。

## 从 Node-based Hash Table 的 Cache Miss 开始

典型 chained hash：

~~~text
hash(key)
→ bucket pointer
→ node
→ next node
→ next node
~~~

即使算法复杂度是 O(1)，每次链式跳转都可能触发 cache miss。

F14 反过来把多个 slot 组织成连续 `Chunk`，先在小而紧凑的 metadata 上过滤候选。

## 一个 Chunk 为什么大约是 14 个 Slot

源码：

~~~cpp
static constexpr unsigned kCapacity = sizeof(Item) == 4 ? 12 : 14;
~~~

常见情况下一个 Chunk 管 14 个 item；4-byte item 特殊优化为 12，使整个 chunk 更贴近 cache-line 布局。

Chunk 头部最重要的是一组 byte tag：

~~~text
tags_[0..]
control_
outboundOverflowCount_
then item storage
~~~

Tag 为 0 表示 empty；真实 entry 使用 1..255。

## Hash 被拆成“位置 + Fingerprint”

完整 hash 不只用来决定 preferred chunk，还会提取一个 1-byte tag。

于是 lookup 分成两级：

~~~text
hash(key)
↓
preferred chunk + 8-bit tag
↓
SIMD scan tags
↓
only candidate slots
↓
full key equality
~~~

源码注释明确指出，正确 hash 下单个 tag false match 概率约为 `1/255`。

所以绝大多数不相关 slot 根本不会触发完整 key comparison。

## SIMD 为什么适合 Metadata，而不是直接比较所有 Key

x86 路径会把 tag vector 一次加载进 SIMD register：

~~~cpp
auto tagV = _mm_load_si128(tagVector());
auto eqV = _mm_cmpeq_epi8(tagV, needleV);
uint32_t mask = _mm_movemask_epi8(eqV);
~~~

ARM 路径则使用 NEON/SVE。

一次向量比较可以产生“哪些 slot 的 tag 相等”的 bitmask。

这比依次读 14 个完整 key 更符合 cache 和 SIMD 硬件特征。

## Metadata Scan 与 Payload Access 分离

这是一条很通用的设计原则：

~~~text
small dense metadata
→ filter
→ sparse expensive payload access
~~~

类似思想还可以出现在：

- packet classifier；
- entity/component lookup；
- object pool slot state；
- GPU descriptor table；
- ECS archetype metadata。

## outboundOverflowCount 为什么能提前终止 Probe

开放寻址最麻烦的问题是：当前 chunk 没找到时，到底还要不要继续 probe？

F14 为 chunk 保存 `outboundOverflowCount_`。

如果它为 0，含义是：

> 没有本来想放在这个 chunk 的 key 因为满而被迫溢出到后续 chunk。

因此 miss lookup 可以立即结束。

如果大于 0，才继续按 probe delta 查看后续 chunk。

这相当于给 probe chain 保存了一份紧凑的结构性证明。

## 为什么还有 hostedOverflowCount

F14 同时记录一个 chunk 当前承载了多少 overflow item。

insert/erase 时维护这些计数，可以在不保存完整 probe history 的情况下恢复正确 termination 条件。

这再次说明 hash table 的 metadata 不只是 occupancy bitmap，还可以编码查找算法需要的证明信息。

## prehash + prefetch 为什么值得暴露

F14 提供 `prehash()` 与 `prefetch()`，允许把批量 lookup 拆成：

~~~text
compute hash for key i
prefetch chunk for key i
do other work
later finish lookup
~~~

这样 CPU 可以在等待 memory latency 时执行其他指令，形成软件 pipeline。

对于批量路由表、feature lookup、dictionary lookup，这比只盯着单次 API latency 更有意义。

## F14 不是 ConcurrentHashMap

F14 的重点是单个 hash table 的 cache efficiency；它并没有因此自动获得多线程写安全。

如果对象由一个 EventLoop/strand 单 owner 管理，F14 很合适；如果多个线程真正并发读写，则需要外部同步或专门的 ConcurrentHashMap。

这也是一个重要选型原则：

> cache-friendly container 和 concurrent container 解决的是两个不同问题。

## 可迁移原则

1. O(1) 不等于低延迟；cache miss 和 pointer chasing 往往更重要。
2. 先扫描紧凑 metadata，再访问昂贵 payload。
3. Hash 可以拆成 address information 与 fingerprint。
4. 数据结构可以保存“何时可停止搜索”的辅助证明状态。
5. 批量 hot lookup 可以显式暴露 prehash/prefetch，利用 memory-level parallelism。