# Per-shard Allocator 与 Cross-CPU Free：为什么 Free 也必须回到内存 Owner

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

如果每个 Core 都有自己的 allocator metadata，那么一个非常现实的问题出现了：

~~~text
Core A allocates object
object moves to Core B
Core B is last user
who frees it?
~~~

最朴素方案让 B 直接修改 A 的 allocator free list。

这会重新引入跨核共享和锁。

Seastar 的做法是 deferred owner-side free。

## Local Free 是 Fast Path

`try_free_fastpath()` 先判断 pointer 是否属于当前 shard：

~~~cpp
is_local_pointer(ptr)
~~~

如果是本地 small-pool object，直接：

~~~text
local stats
→ local pool deallocate
~~~

这是最便宜路径。

## Pointer 本身编码/可恢复 Owner CPU

Seastar 的 memory layout 能从地址判断 allocation 属于哪个 CPU。

因此 slow path 不需要一个 global hash map 查 owner。

发现是 Seastar memory 但不属于当前 shard 后：

~~~cpp
free_cross_cpu(object_cpu_id(ptr), ptr);
~~~

## Cross-CPU Free 不直接调用 Owner Allocator

`free_cross_cpu()` 把待释放对象本身 reinterpret 成一个简单 intrusive node：

~~~cpp
struct cross_cpu_free_item {
    cross_cpu_free_item* next;
};
~~~

然后 CAS push 到 owner CPU 的：

~~~cpp
std::atomic<cross_cpu_free_item*> xcpu_freelist;
~~~

这本质上是一个跨核 MPSC ingress：

~~~text
many foreign cores
→ owner xcpu_freelist
→ one owner drains
~~~

## 为什么 Object Memory 自己可以当 Queue Node

对象已经逻辑死亡，不再需要保存原来的 payload。

所以它的首部内存可以临时写 `next` pointer。

无需为了“待 free 请求”再额外 malloc 一个 node。

这是典型 intrusive reclamation queue。

## Owner 怎样 Drain

Owner shard 周期执行：

~~~cpp
p = xcpu_freelist.exchange(nullptr, acquire);
while (p) {
    next = p->next;
    free(p);
    p = next;
}
~~~

一次 atomic exchange 把整个 remote list ownership 批量拿回本核。

之后真正 allocator free 都是 local operation。

又一次出现：

~~~text
shared ingress
→ detach batch
→ local processing
~~~

和 Folly AtomicNotificationQueue 的结构思想高度一致。

## 为什么 Reactor 里有专门 Poller

`drain_cross_cpu_freelist_pollfn` 被注册进 Reactor。

因此 remote free 不要求每次都立即 wake owner CPU。

源码注释明确说：free 本身通常没有副作用，所以如果 Reactor 正在睡，可以等它因为其他原因醒来时顺便 drain。

这是一种很有价值的性能判断：

> reclamation latency 并不总等于业务 latency。

如果资源压力不高，批量延迟回收比每个 free 都 IPI/wakeup 更划算。

## Memory Pressure 时又为什么主动 Drain

`maybe_reclaim()` 发现 free pages 低时，会主动先 drain cross-CPU freelist，再运行更昂贵的 reclaimers。

于是回收策略有两条路径：

~~~text
normal path
→ lazy periodic drain

memory pressure
→ eager drain before heavier reclaim
~~~

这就是 context-sensitive reclamation policy。

## 与 foreign_ptr 的区别

`foreign_ptr` 解决的是“复杂对象 destructor 必须回 owner shard执行”。

`xcpu_freelist` 解决的是“allocator-owned raw memory 最终必须回 owner allocator”。

二者本质相同，但协议层级不同：

~~~text
object semantic destruction
vs
memory block reclamation
~~~

## 对机器人程序的启发

如果你做 per-thread/per-core object pool：

不要为了支持 foreign free 就让所有 thread 共享同一个 pool mutex。

可以设计：

~~~text
allocation owner pool
+
remote-free MPSC list
+
owner-side batch drain
~~~

尤其适合固定线程拓扑、高频短生命周期对象。

## 可迁移原则

1. Per-core allocator 只有在 free path 也尊重 ownership 时才真正低共享。
2. Remote reclamation 可以先进入 MPSC ingress，再由 owner 批量处理。
3. 已死亡对象的内存本身可以作为 intrusive free-list node。
4. Reclamation latency 与 request latency 可以分开优化。
5. 正常路径 lazy drain、资源压力时 eager drain，是常见分层回收策略。