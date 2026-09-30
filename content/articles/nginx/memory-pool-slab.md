# Memory Pool 与 Slab：为什么 nginx 同时需要两套内存分配器

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

看到 nginx 同时有 `ngx_pool_t` 和 `ngx_slab_pool_t` 时，最重要的问题不是“哪个 allocator 更快”，而是：**它们服务完全不同的生命周期与共享边界。**

## `ngx_pool_t` 解决的是同生命周期批量对象

典型场景：

~~~text
one HTTP request
├─ parsed headers
├─ temporary strings
├─ module contexts
├─ buffer chain nodes
└─ cleanup callbacks
~~~

这些对象大多随着 request 一起销毁。

因此没必要逐个 free。

## Small Allocation 是 Bump Pointer

这条 fast path 对应固定源码里的 `ngx_palloc_small()`：从 `pool->current` 开始扫描 block，用 `last` 指针完成对齐和 bump。

每个 pool block 有：

~~~text
last → next free byte
end  → block end
next → next block
failed
~~~

分配：

~~~c
m = p->d.last;
m = ngx_align_ptr(m, NGX_ALIGNMENT);

if (p->d.end - m >= size) {
  p->d.last = m + size;
  return m;
}
~~~

核心成本接近：

~~~text
align + bounds check + pointer increment
~~~

不需要 free-list 搜索。

## 为什么 Small Object 基本不支持单独 Free

如果允许任意 free，pool 就必须维护：

- free block metadata；
- fragmentation；
- coalescing；
- size classes。

而 request arena 的业务语义根本不需要这些。

所以 nginx 选择：

> 小对象只分配，不单独回收；整个 pool 一次销毁。

用更弱的 API 换更简单的实现。

## Block 满了以后为什么链一个新 Block

`ngx_palloc_block()` 创建同样大小的新 pool block，并挂到 linked list。

这让 pool 可以增量增长，而不需要移动已经返回给业务代码的 pointer。

## `failed` 字段为什么很有意思

分配时如果某个 block 连续无法满足请求：

~~~c
if (p->d.failed++ > 4) {
  pool->current = p->d.next;
}
~~~

`current` 不永远从第一个 block 开始扫描。

老 block 多次失败以后，后续 allocation 直接从更靠后的 block 搜索。

这是一个很轻量的自适应 hint：

~~~text
历史失败次数
→ 调整未来搜索起点
~~~

## Large Allocation 为什么走系统 malloc

超过 `pool->max`：

~~~text
ngx_alloc(size)
→ record in pool->large list
~~~

因为把大对象塞进固定 pool block 会制造严重内部碎片。

所以 small/large 两条路径分开。

## Large List 为什么只复用前几个空 Entry

`ngx_palloc_large()` 搜索已有 `large->alloc == NULL` slot 时，只看前几个节点：

~~~c
if (n++ > 3)
  break;
~~~

这又是一个工程化折中：

> 不为了复用一个 metadata node 做长链表扫描。

扫描成本超过阈值就直接分配一个新 metadata node。

## Cleanup List 把资源生命周期绑定到 Pool

`ngx_pool_cleanup_add()` 允许注册：

~~~text
handler + data
~~~

destroy pool 时先执行所有 cleanup，再释放 memory。

所以 pool 不只是 allocator，还是一个 scope-lifetime manager。

文件 descriptor、临时文件、module resource 都可以挂 cleanup。

## 为什么 Shared Memory 不能直接用 `ngx_pool_t`

共享区域需要：

- 多进程可见；
- 任意对象可能不同时间释放；
- 长期反复 allocate/free；
- 必须控制碎片；
- 需要进程间锁。

这些需求与 request arena 完全不同。

因此 nginx 另有 slab allocator。

## Slab 如何按 Size Class 组织 Page

`ngx_slab_pool_t` 按 `min_shift` 形成 slots。

请求 size 被 round 到对应 power-of-two class。

小对象在 page 内使用 bitmap；exact/big 类型把 occupancy bits 编进 page metadata。

这里的设计是：

~~~text
size class
→ page list
→ bitmap/bitset finds free slot
~~~

## 为什么 Slab Allocation 自带 Shared Mutex

~~~c
ngx_shmtx_lock(&pool->mutex);
p = ngx_slab_alloc_locked(pool, size);
ngx_shmtx_unlock(&pool->mutex);
~~~

因为 slab 常用于 shared-memory zone，不再假设单 worker ownership。

这和 request pool 的 lock-free-by-ownership 完全不同。

## `page->prev` 为什么偷低 Bits 存 Type

源码：

~~~c
#define NGX_SLAB_PAGE_MASK 3
#define NGX_SLAB_PAGE      0
#define NGX_SLAB_BIG       1
#define NGX_SLAB_EXACT     2
#define NGX_SLAB_SMALL     3
~~~

`prev` pointer 的低对齐位用于存 page type。

这是 pointer tagging 的另一例。

前面 nginx connection 用 pointer 低位存 instance generation；这里用低两位存 slab type。

## Arena 与 Slab 的选型矩阵

| 维度 | ngx_pool_t | ngx_slab_pool_t |
| --- | --- | --- |
| 典型 scope | request/config | shared long-lived objects |
| free 模式 | 整体销毁为主 | 独立 allocate/free |
| 小对象 | bump pointer | size class + bitmap |
| 并发 | owner-local | shared mutex |
| 碎片管理 | 基本不回收 | page/slot reuse |
| 元数据复杂度 | 很低 | 较高 |

## 可迁移到普通程序的原则

### Phase Lifetime → Arena

如果一批对象天然一起死：

~~~text
parse one packet
one RPC request
one planner cycle scratch data
one configuration load
~~~

优先考虑 arena/monotonic allocation。

### Independent Lifetime → Pool/Slab

如果对象跨多个 phase 独立销毁，就需要真正 free/reuse protocol。

### Shared Boundary 会改变 Allocator

单线程 owner-local allocator 可以非常简单；跨进程共享则需要同步、offset/relative representation 与 crash recovery。

## 最重要的结论

Allocator 的第一问题不是性能，而是：

> 对象的生命周期模式是什么？谁可以同时操作 allocator？

只有先回答这两个问题，才能决定 bump arena、object pool、slab、buddy 或通用 heap。
