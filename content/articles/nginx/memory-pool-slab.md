# Memory Pool / Slab：Lifetime Arena、Shared Zone 与两种完全不同的分配模型

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 里至少有三种都容易被口头叫成“pool”的东西：

~~~text
connection slot pool
ngx_pool_t
ngx_slab_pool_t
~~~

如果只从“减少 malloc”理解它们，会把三个完全不同的问题混在一起。

它们真正对应的是三种不同的生命周期：

~~~text
connection slot pool
  → 固定数量 control slots
  → storage 长期存在
  → logical generation 反复更换

ngx_pool_t
  → 一批对象共享一个 scope lifetime
  → small object 只 bump allocate
  → scope 结束时整体销毁

ngx_slab_pool_t
  → shared zone 中长期存在的独立对象
  → 每个对象可以单独 allocate/free
  → 多进程访问需要同步
~~~

前一篇 [Connection Pool Lifecycle：Stable Slot、Reusable Queue 与 Generation-safe Reuse](connection-pool-lifecycle.md) 已经处理了第一种。这一篇专门回答后两种：

> **为什么同一个 nginx runtime 既需要 arena，又需要 slab？**

答案不是“两个 allocator 谁更快”，而是：

> **对象是不是一起死，以及 allocator 有没有跨 owner / 跨进程共享。**

---

## 1. 先从对象生命周期，而不是 Allocator API 出发

设想一个 HTTP request。解析过程中可能创建：

~~~text
request
├── parsed headers
├── temporary strings
├── module contexts
├── buffer chain nodes
├── URI rewrite temporary state
└── cleanup records
~~~

这些对象的共同特点是：

~~~text
创建时间不同
大小不同
类型不同
但大多一起结束生命周期
~~~

如果每个对象都分别 `malloc/free`，allocator 就必须为根本不存在的业务需求付出代价：单对象 free metadata、free-list 管理、fragmentation、size-class 搜索、空洞合并和更多同步。

但 request 已经给出了更强的信息：

~~~text
这些对象基本上一起死
~~~

这意味着最自然的数据结构不是通用 heap，而是 arena，也就是 nginx 的 `ngx_pool_t`。

---

## 2. `ngx_pool_t` 的第一性原理：把逐对象回收从问题里删掉

普通 heap 要解决：

~~~text
alloc A
alloc B
free A
alloc C
free B
...
~~~

所以必须持续回答“哪里空闲、哪个空洞合适、要不要合并、并发修改怎么同步”。

arena 利用生命周期约束，把问题改写成：

~~~text
allocate A
allocate B
allocate C
...
destroy whole arena
~~~

于是 small allocation 最核心的状态只需要：

~~~text
last → 下一个可分配字节
end  → 当前 block 尾部
~~~

分配就变成：

~~~text
align(last)
+
bounds check
+
last += size
~~~

复杂的 free 问题直接消失了。

---

## 3. `ngx_pool_t` 本体到底保存什么

固定源码 `src/core/ngx_palloc.h`：

~~~c
typedef struct {
    u_char               *last;
    u_char               *end;
    ngx_pool_t           *next;
    ngx_uint_t            failed;
} ngx_pool_data_t;

struct ngx_pool_s {
    ngx_pool_data_t       d;
    size_t                max;
    ngx_pool_t           *current;
    ngx_chain_t          *chain;
    ngx_pool_large_t     *large;
    ngx_pool_cleanup_t   *cleanup;
    ngx_log_t            *log;
};
~~~

这些字段可以分成三组：

~~~text
block-local cursor:
  d.last / d.end / d.next / d.failed

arena allocation state:
  max / current / large

scope lifetime state:
  cleanup / chain / log
~~~

所以 `ngx_pool_t` 不只是“一块预分配内存”。它同时维护 small-object arena、large-allocation ownership 和 lifetime cleanup chain。

---

## 4. 创建 Pool 时，Header 和 Payload 在同一块内存里

固定源码 `ngx_create_pool()`：

~~~c
p = ngx_memalign(NGX_POOL_ALIGNMENT, size, log);
if (p == NULL) {
    return NULL;
}

p->d.last = (u_char *) p + sizeof(ngx_pool_t);
p->d.end = (u_char *) p + size;
p->d.next = NULL;
p->d.failed = 0;

size = size - sizeof(ngx_pool_t);
p->max = (size < NGX_MAX_ALLOC_FROM_POOL)
         ? size
         : NGX_MAX_ALLOC_FROM_POOL;

p->current = p;
p->chain = NULL;
p->large = NULL;
p->cleanup = NULL;
p->log = log;
~~~

内存布局是：

~~~text
root block
┌────────────────────────────────────────────┐
│ ngx_pool_t header                          │
├────────────────────────────────────────────┤
│                                            │
│ small allocations grow →                  │
│                                            │
│                           ← d.end          │
└────────────────────────────────────────────┘
  ^
  d.last initially
~~~

一次 aligned allocation 同时提供 arena metadata 和第一块 payload storage，避免再为 header 单独分配。

---

## 5. `pool->max` 是 Dispatch Threshold，不是总容量

固定头文件：

~~~c
#define NGX_MAX_ALLOC_FROM_POOL  (ngx_pagesize - 1)
~~~

创建时：

~~~c
p->max = (size < NGX_MAX_ALLOC_FROM_POOL)
         ? size
         : NGX_MAX_ALLOC_FROM_POOL;
~~~

所以 `pool->max` 的语义不是“pool 最多还能分多少字节”，而是：

~~~text
多大的单次 allocation
仍然走 small-object arena path
~~~

超过它就进入 large path。

---

## 6. Small Allocation 的 Fast Path 就是 Bump Pointer

固定源码 `ngx_palloc_small()`：

~~~c
p = pool->current;

do {
    m = p->d.last;

    if (align) {
        m = ngx_align_ptr(m, NGX_ALIGNMENT);
    }

    if ((size_t) (p->d.end - m) >= size) {
        p->d.last = m + size;

        return m;
    }

    p = p->d.next;

} while (p);

return ngx_palloc_block(pool, size);
~~~

当前 block 有空间时，只需要：

~~~text
m = align(last)
if end - m >= size:
    last = m + size
    return m
~~~

没有 free-list、tree、size-bin、coalescing，也没有 per-object metadata。

`ngx_palloc()` 与 `ngx_pnalloc()` 的差异也只在是否要求 alignment：

~~~text
ngx_palloc  → align = 1
ngx_pnalloc → align = 0
~~~

---

## 7. 为什么 Small Object 不支持通用单对象 Free

`ngx_pfree()` 并不会去 small blocks 里寻找某个对象，它只遍历 `pool->large`：

~~~c
for (l = pool->large; l; l = l->next) {
    if (p == l->alloc) {
        ngx_free(l->alloc);
        l->alloc = NULL;

        return NGX_OK;
    }
}

return NGX_DECLINED;
~~~

所以：

~~~text
small arena allocation
→ 不支持独立 free

tracked large allocation
→ 可以 pfree
~~~

这不是缺功能，而是 arena 的关键约束。small object 如果要任意释放，就要重新引入 free-chunk metadata、fragmentation handling 和 reuse policy，fast path 会变成另一种 allocator。

---

## 8. Block 满了以后为什么不是 Realloc 整个 Arena

找不到 small space 后会进入 `ngx_palloc_block()`。

新 block 大小与 root block 相同：

~~~c
psize = (size_t) (pool->d.end - (u_char *) pool);

m = ngx_memalign(NGX_POOL_ALIGNMENT, psize, pool->log);
~~~

最后挂到 block chain：

~~~c
p->d.next = new;
~~~

不能直接扩 root block 的关键原因是：业务代码已经拿到了内部 pointer。若扩容导致搬家，旧 pointer 就全部失效。

链新 block 保证：

~~~text
already-returned addresses stay stable
~~~

---

## 9. 为什么后续 Block 只需要更小的 Header

root block 拥有完整 `ngx_pool_t`，但后续 block 只真正使用 `ngx_pool_data_t`：

~~~c
new = (ngx_pool_t *) m;

new->d.end = m + psize;
new->d.next = NULL;
new->d.failed = 0;

m += sizeof(ngx_pool_data_t);
m = ngx_align_ptr(m, NGX_ALIGNMENT);
new->d.last = m + size;
~~~

因为 `max/current/large/cleanup/log` 都是整个 arena 的全局状态，只需要 root 保存一份；后续 block 只需自己的 cursor、end、next 和 failed。

---

## 10. `failed` + `current` 是轻量的自适应搜索 Hint

arena 增长后可能是：

~~~text
block0 → block1 → block2 → block3 → ...
~~~

如果每次从 block0 扫起，而前几个 block 都只剩很少尾部空间，就会重复失败。

固定源码：

~~~c
for (p = pool->current; p->d.next; p = p->d.next) {
    if (p->d.failed++ > 4) {
        pool->current = p->d.next;
    }
}
~~~

它没有维护精确 free-space index，而是记录“这个 block 最近经常失败”。失败足够多后，未来搜索起点向后移动。

这是一个很典型的工程权衡：

> **不为了精确最优建立复杂索引，只维护足以减少重复无效工作的历史 hint。**

---

## 11. Large Allocation 为什么直接走系统 Allocator

`ngx_palloc()`：

~~~c
if (size <= pool->max) {
    return ngx_palloc_small(pool, size, 1);
}

return ngx_palloc_large(pool, size);
~~~

large path 先：

~~~c
p = ngx_alloc(size, pool->log);
~~~

如果把一个很大的对象强行塞入普通 arena block，会让 block growth 和内部浪费被单个 oversized allocation 扭曲。

nginx 因而分成：

~~~text
small:
  arena blocks

large:
  independent system allocation
  +
  arena-owned tracking record
~~~

物理 storage 可以来自系统 allocator，但生命周期 ownership 仍归这个 pool。

---

## 12. `pool->large` 是 Ownership List，不是 Size-class Allocator

large metadata 很简单：

~~~c
struct ngx_pool_large_s {
    ngx_pool_large_t  *next;
    void              *alloc;
};
~~~

新 allocation 被记录：

~~~c
large->alloc = p;
large->next = pool->large;
pool->large = large;
~~~

destroy 时就能遍历并释放。

已经 `ngx_pfree()` 的 record 会出现：

~~~text
large->alloc == NULL
~~~

后续 large allocation 会尝试复用这样的 metadata node，但只做有限扫描：

~~~c
n = 0;

for (large = pool->large; large; large = large->next) {
    if (large->alloc == NULL) {
        large->alloc = p;
        return p;
    }

    if (n++ > 3) {
        break;
    }
}
~~~

这个细节说明：

> **metadata reuse 本身也不能变成热点路径里的无界 O(N) 搜索。**

---

## 13. Cleanup Chain 让 Arena 变成 Lifetime Scope

scope 中还可能拥有 fd、临时文件、SSL/context resource 或 module-specific external state。这些资源不会因为 arena bytes 被回收就自动完成语义销毁。

因此 `ngx_pool_t` 还有 cleanup list：

~~~c
struct ngx_pool_cleanup_s {
    ngx_pool_cleanup_pt   handler;
    void                 *data;
    ngx_pool_cleanup_t   *next;
};
~~~

`ngx_pool_cleanup_add()` 从 pool 自己分配 cleanup record，并挂到 `p->cleanup`：

~~~c
c = ngx_palloc(p, sizeof(ngx_pool_cleanup_t));

if (size) {
    c->data = ngx_palloc(p, size);
} else {
    c->data = NULL;
}

c->handler = NULL;
c->next = p->cleanup;
p->cleanup = c;
~~~

caller 再填入 `handler`。这样，非内存资源也被绑定到了同一个 lifetime scope。

---

## 14. Destroy 为什么必须 Cleanup → Large → Blocks

`ngx_destroy_pool()` 首先运行 cleanup：

~~~c
for (c = pool->cleanup; c; c = c->next) {
    if (c->handler) {
        c->handler(c->data);
    }
}
~~~

然后释放 tracked large allocations：

~~~c
for (l = pool->large; l; l = l->next) {
    if (l->alloc) {
        ngx_free(l->alloc);
    }
}
~~~

最后释放 block chain：

~~~c
for (p = pool, n = pool->d.next; /* void */; p = n, n = n->d.next) {
    ngx_free(p);

    if (n == NULL) {
        break;
    }
}
~~~

顺序不能反，因为 cleanup node、cleanup data 或 handler 需要的上下文本身就可能位于 pool 中。

因此 destroy 不是简单“把内存 free 掉”，而是明确的生命周期协议：

~~~text
run semantic finalizers
        ↓
release out-of-arena large storage
        ↓
release arena blocks
~~~

---

## 15. `ngx_reset_pool()` 与 Destroy 不是同一种操作

reset 会释放 tracked large allocations，并把 block cursor 倒回去：

~~~c
for (l = pool->large; l; l = l->next) {
    if (l->alloc) {
        ngx_free(l->alloc);
    }
}

for (p = pool; p; p = p->d.next) {
    p->d.last = (u_char *) p + sizeof(ngx_pool_t);
    p->d.failed = 0;
}

pool->current = pool;
pool->chain = NULL;
pool->large = NULL;
~~~

注意它没有运行 cleanup handlers。

所以：

~~~text
reset
→ 回收可分配空间，准备复用 arena storage

destroy
→ 结束完整 lifetime scope
~~~

当外部资源依赖 cleanup chain 时，不能把 reset 当成 destroy 的廉价替代。

---

## 16. Connection Slot 与 `c->pool` 是两层生命周期

accept path 先获得固定 control slot：

~~~c
c = ngx_get_connection(s, ev->log);

if (c == NULL) {
    ...
    return;
}
~~~

随后才创建 per-connection arena：

~~~c
c->pool = ngx_create_pool(ls->pool_size, ev->log);
if (c->pool == NULL) {
    ngx_close_accepted_connection(c);
    return;
}
~~~

peer address 和 log 再从该 arena 分配：

~~~c
c->sockaddr = ngx_palloc(c->pool, socklen);
log = ngx_palloc(c->pool, sizeof(ngx_log_t));
~~~

因此结构是：

~~~text
worker-lifetime stable connection slot
             │
             │ one logical generation
             ▼
        c->pool arena
             │
     ┌───────┼─────────┐
     │       │         │
 sockaddr   log   protocol state...
~~~

connection slot 可以服务很多代 logical connection，而某一代 `c->pool` 只服务当前连接的动态状态。

---

# Slab：为什么问题突然完全不同

`ngx_pool_t` 的设计依赖强前提：

~~~text
大多数对象一起死
~~~

shared-memory zone 不满足这个条件。

例如跨 worker 共享的 SSL session cache、limit_req / limit_conn state、upstream zone、file-cache shared metadata，里面的对象会独立创建和删除：

~~~text
worker A 创建 node X
worker B 读取 X
worker C 删除 X

同时 Y / Z 仍然存活
~~~

因此需求变成：

~~~text
独立 allocate/free
+
长期 reuse
+
控制碎片
+
多进程同步
~~~

这已经不是 arena 问题。

---

## 17. `ngx_slab_pool_t` 是 Shared Zone 内部的 Allocator State

固定源码：

~~~c
typedef struct {
    ngx_shmtx_sh_t    lock;

    size_t            min_size;
    size_t            min_shift;

    ngx_slab_page_t  *pages;
    ngx_slab_page_t  *last;
    ngx_slab_page_t   free;

    ngx_slab_stat_t  *stats;
    ngx_uint_t        pfree;

    u_char           *start;
    u_char           *end;

    ngx_shmtx_t       mutex;

    u_char           *log_ctx;
    u_char            zero;

    unsigned          log_nomem:1;

    void             *data;
    void             *addr;
} ngx_slab_pool_t;
~~~

这里已经出现 arena 中没有的长期 allocator 状态：

~~~text
page metadata
free page list
size-class statistics
shared mutex
shared-zone address identity
~~~

---

## 18. Slab Pool 本身就放在 Shared Mapping 开头

shared zone 初始化：

~~~c
sp = (ngx_slab_pool_t *) zn->shm.addr;
~~~

新 zone 设置：

~~~c
sp->end = zn->shm.addr + zn->shm.size;
sp->min_shift = 3;
sp->addr = zn->shm.addr;
~~~

创建 shared mutex 后初始化 slab：

~~~c
if (ngx_shmtx_create(&sp->mutex, &sp->lock, file) != NGX_OK) {
    return NGX_ERROR;
}

ngx_slab_init(sp);
~~~

大致布局：

~~~text
zn->shm.addr
    │
    ▼
┌───────────────────────────────────────────┐
│ ngx_slab_pool_t                           │
├───────────────────────────────────────────┤
│ size-class slot heads                     │
├───────────────────────────────────────────┤
│ stats                                     │
├───────────────────────────────────────────┤
│ ngx_slab_page_t metadata array            │
├───────────────────────────────────────────┤
│ page-aligned allocatable storage          │
│                                           │
└───────────────────────────────────────────┘
                               ▲
                               sp->end
~~~

allocator metadata 与被管理内存都位于同一个 shared zone。

---

## 19. 一个关键事实：这套 Shared Slab 不是 Position-independent

很多 shared-memory runtime 会采用：

~~~text
offset / relative pointer
~~~

让不同进程可以把同一 shared segment 映射到不同虚拟地址。

固定 nginx 实现走的是另一条路线。已有 zone 恢复时：

~~~c
sp = (ngx_slab_pool_t *) zn->shm.addr;

if (zn->shm.exists) {
    if (sp == sp->addr) {
        return NGX_OK;
    }

#if (NGX_WIN32)
    if (ngx_shm_remap(&zn->shm, sp->addr) != NGX_OK) {
        return NGX_ERROR;
    }

    sp = (ngx_slab_pool_t *) zn->shm.addr;

    if (sp == sp->addr) {
        return NGX_OK;
    }
#endif

    ngx_log_error(...,
                  "shared zone ... has no equal addresses: %p vs %p",
                  sp->addr, sp);
    return NGX_ERROR;
}
~~~

slab page/list structures保存普通 pointer，所以 nginx 建立的是：

~~~text
shared mapping must preserve compatible virtual addresses
~~~

而不是“任意 mapping address + relative pointer”。

这说明一个非常重要的 shared-memory 原理：

> **共享物理页不等于 raw pointer 自动跨进程有效。**

设计自己的 shared-memory middleware 时必须显式选择：

~~~text
equal/fixed mapping + raw pointer
或
relocatable mapping + offset/relative handle
~~~

---

## 20. 为什么 Slab API 必须带 Shared Mutex

外层 allocation：

~~~c
void *
ngx_slab_alloc(ngx_slab_pool_t *pool, size_t size)
{
    void *p;

    ngx_shmtx_lock(&pool->mutex);

    p = ngx_slab_alloc_locked(pool, size);

    ngx_shmtx_unlock(&pool->mutex);

    return p;
}
~~~

free 同样在 `pool->mutex` 下调用 `ngx_slab_free_locked()`。

根本原因是 ownership topology：

~~~text
worker-local state
→ one execution owner
→ ordinary mutation can be enough

shared-memory state
→ multiple worker processes
→ allocator metadata is shared mutable state
→ inter-process synchronization required
~~~

“要不要锁”首先由谁会并发修改这份状态决定，而不是由“是不是高性能代码”决定。

---

## 21. 为什么还要暴露 `*_locked` 版本

shared data structure 常常要完成一个更大的原子操作：

~~~text
lock shared zone
   ↓
lookup rbtree/hash
   ↓
remove old node
   ↓
free old allocation
   ↓
allocate new node
   ↓
insert new node
unlock
~~~

如果 free/alloc 自己再次获取相同 mutex，就无法自然嵌入这个临界区。

所以 nginx 同时提供：

~~~text
ngx_slab_alloc()
ngx_slab_free()

ngx_slab_alloc_locked()
ngx_slab_free_locked()
~~~

`*_locked` 不是“更快版本”，而是在 API 上声明：

~~~text
caller already owns pool->mutex
~~~

这是非常值得借鉴的 lock ownership contract。

---

## 22. Slab 怎样把 Request Size 映射到 Size Class

初始化：

~~~c
pool->min_size = (size_t) 1 << pool->min_shift;
~~~

固定 shared zone 初始化设置：

~~~c
sp->min_shift = 3;
~~~

最小 class 因而从 8 bytes 开始。

allocation 时：

~~~c
if (size > pool->min_size) {
    shift = 1;
    for (s = size - 1; s >>= 1; shift++) { /* void */ }
    slot = shift - pool->min_shift;

} else {
    shift = pool->min_shift;
    slot = 0;
}
~~~

本质上是：

~~~text
request size
   ↓
ceil(log2(size))
   ↓
power-of-two chunk size
   ↓
slot index
~~~

同一个 page 被绑定到某个 chunk size class，才能高效定位和复用独立对象。

---

## 23. 大于半页的对象直接按 Page Run 分配

初始化：

~~~c
ngx_slab_max_size = ngx_pagesize / 2;
~~~

分配入口：

~~~c
if (size > ngx_slab_max_size) {
    page = ngx_slab_alloc_pages(
        pool,
        (size >> ngx_pagesize_shift)
        + ((size % ngx_pagesize) ? 1 : 0));

    if (page) {
        p = ngx_slab_page_addr(pool, page);
    } else {
        p = 0;
    }

    goto done;
}
~~~

因此 allocator 分成：

~~~text
small/medium
→ chunk inside a page

large
→ whole page run
~~~

对象已经大到接近 page 时，再维持很多小 chunk metadata 没有意义。

---

## 24. SMALL / EXACT / BIG 是三种 Occupancy Encoding

固定源码：

~~~c
#define NGX_SLAB_PAGE_MASK   3
#define NGX_SLAB_PAGE        0
#define NGX_SLAB_BIG         1
#define NGX_SLAB_EXACT       2
#define NGX_SLAB_SMALL       3
~~~

它们不是业务类型，而是在回答：

~~~text
这个 page 的占用位图放在哪里最划算？
~~~

### SMALL

chunk 很小，一个 page 内 slot 很多，因此 bitmap 放在 page payload 开头：

~~~c
bitmap = (uintptr_t *) ngx_slab_page_addr(pool, page);
~~~

概念布局：

~~~text
page
┌──────────────────────┐
│ bitmap               │
├──────────────────────┤
│ small chunks         │
│ ...                  │
└──────────────────────┘
~~~

### EXACT

在特殊 chunk size 下，一个 machine word 的 bit 数正好可以表达 page 内所有 chunks，因此 `page->slab` 本身就是 occupancy bitmap。

### BIG

chunk 较大，一个 page 内 slot 较少，occupancy bits 编在 `page->slab` 的高位 map 区域。

共同目标是：根据 chunks-per-page 数量，选择更紧凑的 metadata layout。

---

## 25. `page->prev` 为什么还能顺便存 Page Type

固定宏：

~~~c
#define ngx_slab_page_type(page)     ((page)->prev & NGX_SLAB_PAGE_MASK)

#define ngx_slab_page_prev(page)     (ngx_slab_page_t *) ((page)->prev & ~NGX_SLAB_PAGE_MASK)
~~~

因为正常 pointer 的低位受 alignment 约束，低两位可以编码类型：

~~~text
page->prev
=
aligned previous pointer
|
2-bit page type
~~~

它和 epoll 的：

~~~text
connection pointer | instance bit
~~~

都使用 pointer tagging，但语义不同：

~~~text
epoll tag → logical generation
slab tag  → allocator page type
~~~

---

## 26. Full Page 为什么要从 Size-class List 中摘掉

如果一个 64-byte class page 已经没有空 chunk，却还留在 available list，后续 allocation 每次都会先碰到一个必然失败的候选。

所以状态关系是：

~~~text
partially free page
→ belongs to size-class available list

full page
→ detached from available list

free one chunk from formerly-full page
→ reinsert into available list
~~~

这和 arena 的 `failed/current` 虽然数据结构不同，但目标一致：

> **不要持续扫描已经知道无法满足请求的候选对象。**

---

## 27. Slab Free 为什么比 Arena Destroy 复杂得多

`ngx_slab_free_locked()` 首先检查 pointer 是否位于 pool：

~~~c
if ((u_char *) p < pool->start || (u_char *) p > pool->end) {
    ngx_slab_error(pool, NGX_LOG_ALERT,
                   "ngx_slab_free(): outside of pool");
    goto fail;
}
~~~

再从地址定位所属 page：

~~~c
n = ((u_char *) p - pool->start) >> ngx_pagesize_shift;
page = &pool->pages[n];
slab = page->slab;
type = ngx_slab_page_type(page);
~~~

随后按 SMALL / EXACT / BIG / PAGE 分别验证：

- pointer 是否符合 chunk/page alignment；
- occupancy bit 是否当前为 allocated；
- 是否 double free；
- 是否错误指向 page 中部；
- free 后 full page 是否重新获得 capacity；
- 最后一个 chunk free 后是否返还整页。

这就是 independent lifetime 的成本。

arena small allocation 不需要这些 metadata，因为它根本不允许任意对象独立离开生命周期。

---

## 28. 最后一个 Chunk Free 后为什么要归还整 Page

以 EXACT 为例：

~~~c
page->slab &= ~m;

if (page->slab) {
    goto done;
}

ngx_slab_free_pages(pool, page, 1);
~~~

当 occupancy 归零：

~~~text
这个 page 没有任何 live chunk
~~~

如果仍永久绑定在当前 size class，别的 class 就无法使用它。

所以 slab 有两级 reuse：

~~~text
Level 1:
同 size class 内复用空闲 chunk

Level 2:
page 完全空闲后回到 free-page allocator，
未来可被其他 size class / page-run request 使用
~~~

这是长期运行 allocator 控制碎片的重要机制。

---

## 29. Arena 与 Slab 的根本 Trade-off

`ngx_pool_t` 利用强生命周期假设：

~~~text
objects die together
~~~

所以换来：

~~~text
cheap small allocation
almost no per-object metadata
no independent small free
simple ownership
~~~

`ngx_slab_pool_t` 面对更弱的假设：

~~~text
objects die independently
~~~

因此必须支付：

~~~text
size classes
page metadata
occupancy bitmap
individual free validation
fragmentation management
shared locking
~~~

allocator 复杂度不是凭空出现的，它来自业务允许的生命周期自由度：

~~~text
lifetime 越统一
→ allocator 可以越简单

lifetime 越独立
→ allocator 必须记住越多状态
~~~

---

## 30. Shared Boundary 又额外引入 Synchronization 与 Address ABI

shared memory 比普通独立 lifetime allocator 又多两个问题。

第一是同步。多个 worker 可以同时修改：

~~~text
free page list
slot lists
page occupancy
stats
business shared indexes
~~~

第二是地址解释。共享物理页不保证不同地址空间使用相同 virtual address。

所以设计 shared-memory runtime 必须同时回答：

~~~text
who mutates allocator metadata?
→ mutex / owner / lock-free protocol

how are internal references represented?
→ raw pointer with mapping invariant
  or offset/relative handle
~~~

这两个问题都比“malloc 快多少”更基础。

---

## 31. 三种 Pool 放进同一张图

~~~text
                         object lifetime
                               │
          ┌────────────────────┼─────────────────────┐
          │                    │                     │
 fixed bounded slots      same-scope objects   independent objects
          │                    │                     │
          ▼                    ▼                     ▼
 connection pool          ngx_pool_t           ngx_slab_pool_t
          │                    │                     │
 stable storage           bump arena           size classes/pages
 generation reuse         bulk destroy         individual free
 worker-local owner       scope-local owner    shared mutex
 reusable policy          cleanup chain        shared-zone ABI
~~~

它们不是“谁更先进”的三种 allocator，而是在解决三个不同的问题。

---

## 32. 一个完整可运行的 C++ Arena 最小示例

下面不是 nginx 源码，而是用 C++17 复现 `ngx_pool_t` 最核心的同 scope bump allocation。为了保持示例完整，它只实现固定 backing buffer，不实现 nginx 的 block growth、large list 和 cleanup chain。

~~~cpp
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <new>
#include <vector>

class Arena {
public:
    explicit Arena(std::size_t bytes)
        : storage_(bytes), offset_(0) {}

    void* allocate(std::size_t size,
                   std::size_t alignment = alignof(std::max_align_t)) {
        const std::uintptr_t base =
            reinterpret_cast<std::uintptr_t>(storage_.data());

        const std::uintptr_t current = base + offset_;
        const std::uintptr_t aligned =
            (current + alignment - 1)
            & ~(static_cast<std::uintptr_t>(alignment) - 1);

        const std::size_t next =
            static_cast<std::size_t>(aligned - base) + size;

        if (next > storage_.size()) {
            throw std::bad_alloc{};
        }

        offset_ = next;
        return reinterpret_cast<void*>(aligned);
    }

    void reset() noexcept {
        offset_ = 0;
    }

    std::size_t used() const noexcept {
        return offset_;
    }

private:
    std::vector<std::byte> storage_;
    std::size_t offset_;
};

struct Scratch {
    int frame;
    double error;
};

int main() {
    Arena arena(1024);

    void* memory = arena.allocate(sizeof(Scratch), alignof(Scratch));
    auto* scratch = new (memory) Scratch{42, 0.125};

    assert(scratch->frame == 42);
    assert(scratch->error == 0.125);
    assert(arena.used() > 0);

    scratch->~Scratch();
    arena.reset();

    std::cout << "arena reset as one lifetime scope\n";
}
~~~

这个示例刻意没有 `arena.free(scratch)`，因为它表达的就是：

~~~text
scope ends
→ all scratch storage becomes reusable together
~~~

真正工程化时还必须处理非平凡析构对象的 destructor registration、block growth、oversized allocation、exception safety 和 owner/thread model。nginx cleanup chain 正是其中“scope finalizer”问题的一种 C 风格答案。

---

## 33. 在机器人系统里怎样选

### 一次控制周期的临时计算

如果 1 kHz 控制 loop 中的 scratch data 严格不跨周期：

~~~text
temporary matrix/views
temporary command objects
small trajectory scratch nodes
codec scratch
~~~

cycle-local arena 可以减少通用 heap 带来的不确定性。

但必须建立硬约束：

~~~text
arena pointer 不得泄漏到下一周期
~~~

### 设备 Session Control Block

如果最多 64 路设备 session，且断线后 slot 会复用，更接近：

~~~text
fixed slots + generation handle
~~~

也就是上一章的 connection pool 模型。

### 跨进程共享状态表

如果多个进程共同维护：

~~~text
sensor metadata registry
shared frame descriptors
multi-process health state
~~~

而 node 独立插入/删除，那么 arena 不够，需要真正支持 individual reuse 的 shared allocator、fixed object pool 或 slab。

同时必须选：

~~~text
raw pointer + equal/fixed mapping
或
offset/handle + relocatable mapping
~~~

### 大块图像 Payload

MB 级图像本体通常更适合：

~~~text
fixed frame slots
DMA / SHM buffers
loan / lease ownership
generation / sequence
~~~

而不是简单切成大量 slab chunks。allocator 还必须和数据面 ownership 一起设计。

---

## 34. 选型矩阵

| 问题 | Connection Slot Pool | `ngx_pool_t` | `ngx_slab_pool_t` |
| --- | --- | --- | --- |
| 主要对象 | runtime control slot | 同 scope 临时对象 | shared long-lived nodes |
| 数量模型 | 固定上限 | 动态增长 blocks | 固定 shared zone 内动态复用 |
| 单对象释放 | logical retirement 后 slot 回池 | small object 基本不支持 | 支持 |
| 地址语义 | slot 地址长期稳定 | pool lifetime 内已返回地址稳定 | 受 shared mapping contract 约束 |
| logical generation | 必须关注 | 通常由 scope 区分 | allocator 主要追踪 chunk/page occupancy |
| fast path | free-list pop | bump pointer | size-class bitmap |
| 碎片策略 | fixed slot | 接受 block 尾部浪费，整体释放 | chunk reuse + empty-page return |
| 并发模型 | worker-local owner | 通常 scope owner-local | shared mutex |
| overload / exhaustion | reusable victim reclaim | block growth / allocation fail | zone/free-page exhaustion |
| 生命周期附加机制 | multi-index retirement | cleanup chain | shared index + lock protocol |

---

## 35. 最终心智模型

只看 API：

~~~text
ngx_get_connection
ngx_palloc
ngx_slab_alloc
~~~

它们似乎都只是“拿一块内存”。

从 runtime 设计看：

~~~text
ngx_get_connection
→ 获取有限 control resource 的新 logical generation

ngx_palloc
→ 在共同 scope lifetime 中追加 transient object

ngx_slab_alloc
→ 在长期 shared zone 中建立可独立释放的对象
~~~

对应的数据结构自然变成：

~~~text
fixed slot + generation
bump arena + cleanup
page/bitmap + shared mutex
~~~

如果只记一条原则：

> **Allocator 设计首先是生命周期与 ownership 设计。对象是否一起销毁、是否需要独立 free、是否跨线程/进程共享、内部地址怎样跨地址空间解释，这些约束先决定数据结构；“malloc 快不快”反而是后面的问题。**
