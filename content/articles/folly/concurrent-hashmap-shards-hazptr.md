# ConcurrentHashMap：分片写锁 + Hazard Pointer，为什么比一把全局 Mutex 更可扩展

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

一个最简单的线程安全 map 是：

~~~cpp
std::mutex m;
std::unordered_map<K, V> map;
~~~

每次 find/insert/erase 都拿同一把锁。

正确，但所有 key 都共享一个 synchronization domain。

Folly `ConcurrentHashMap` 的第一步不是发明神奇无锁算法，而是先做 **sharding**。

## 默认 8 个 Shard Bits = 256 个 Segment

模板默认：

~~~cpp
uint8_t ShardBits = 8;
static constexpr uint64_t NumShards = (1 << ShardBits);
~~~

Hash 的低位先选择 segment：

~~~cpp
return h & (NumShards - 1);
~~~

Segment 内再用剩余 hash bits 选择 bucket。

所以：

~~~text
global key space
↓ hash low bits
256 synchronization domains
↓ remaining bits
bucket inside segment
~~~

两个落在不同 segment 的 writer 不需要竞争同一把 mutex。

## Segment 为什么 `alignas(64)`

源码：

~~~cpp
class alignas(64) ConcurrentHashMapSegment
~~~

分片只是逻辑隔离还不够；如果两个 segment 的热状态落在同一 cache line，仍可能发生 false sharing。

所以并发拓扑和物理布局必须一起设计。

## Segment 为什么 Lazy Allocate

顶层先只保存：

~~~cpp
Atom<SegmentT*> segments_[NumShards];
~~~

某个 shard 第一次真正 insert 时，`ensureSegment()` 才 allocate，并通过 CAS 安装。

如果两个线程同时创建同一 segment：

~~~text
both allocate
→ one CAS wins
→ loser destroys its temporary segment
~~~

这样 256 shards 不意味着启动时必须创建 256 份完整 hash table。

## Writer 并不是完全 Lock-free

Segment 内 insert/erase/rehash 会拿自己的 `m_`。

这非常值得注意：

> 工业高性能并发容器并不等于“所有操作都必须 lock-free”。

更现实的策略是：

~~~text
shard the lock
+
make read path cheap
+
move reclamation outside lock
~~~

锁粒度和访问拓扑往往比“有没有 mutex”这个二元问题更重要。

## Reader 为什么不直接拿 Segment Mutex

`find()` 会使用 hazard pointers 保护：

- 当前 bucket array；
- 当前 node；
- next node。

Iterator 内甚至直接持有 3 个 hazard pointer slot。

这样 Reader 在遍历链表时，即使 Writer 已经把 node 从 bucket 中 unlink，内存也不会立刻被 free。

这就是并发容器最容易被忽略的第二问题：

~~~text
unlink from structure
!=
safe to reclaim memory
~~~

## Rehash 时为什么还需要 Seqlock

Reader 必须同时看到一组一致的：

~~~text
bucket_count
buckets pointer
~~~

Writer rehash 时更新两者。

源码使用 `seqlock_`：Writer 在切换表前后各递增一次；Reader 读取 version → count → hazard-protect buckets → 再读 version。

只有 version 未变化且为偶数，才说明拿到一致快照。

于是：

~~~text
hazard pointer
= protect object lifetime

seqlock
= validate multi-field snapshot
~~~

它们解决的不是同一个问题。

## 旧 Bucket Array 为什么 `retire()` 而不是立即 Delete

Rehash 发布新 buckets 后，某个 Reader 可能还在旧表上。

所以旧 buckets 进入 hazard-pointer retirement，等没有 Reader 再保护它以后才回收。

同理，erase 后 node 的 `release()` 放在 segment mutex 外执行，避免 deleter/destructor 在锁内制造长临界区或 reentrancy。

## 为什么 `contains()` 被直接删除

源码故意：

~~~cpp
bool contains(const KeyType& k) const = delete;
~~~

因为：

~~~text
if (map.contains(k))
    use(map[k]);
~~~

在并发环境中存在经典 TOCTOU：contains 返回后，key 可能马上被另一个线程 erase。

Concurrent API 必须让“找到对象”和“保持对象有效”属于同一个保护协议。

因此 `find()` 返回携带 hazard protection 的 iterator，比一个裸 bool 更安全。

## 何时应该 Shard，何时不应该

Sharding 适合 key 独立、访问可按 hash 分区的状态。

不适合需要跨多个 key 原子事务的场景，因为：

~~~text
key A in shard 3
key B in shard 91
~~~

此时全局一致操作仍需更高层协议。

所以 sharding 用局部并发换取全局事务能力的下降。

## 机器人 Runtime 的映射

例如多线程服务维护：

~~~text
device_id → DeviceSession
request_id → PendingRPC
stream_id → StreamState
~~~

如果 key 之间独立，sharded map 很合理。

如果整个控制状态本来由单一 supervisor owner 线程管理，直接用普通 F14/map 反而更简单。

## 可迁移原则

1. 先分 synchronization domain，再优化单把锁。
2. Logical sharding 还要配合 cache-line physical isolation。
3. Reader safety 包括查找正确性和内存回收安全两个问题。
4. Hazard pointer 保护 lifetime；seqlock 验证 snapshot consistency。
5. 并发 API 应避免诱导 TOCTOU，用类型承载保护生命周期。