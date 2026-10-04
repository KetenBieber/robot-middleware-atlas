# Connection Pool Lifecycle：Stable Slot、Reusable Queue 与 Generation-safe Reuse

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 的 connection pool 很容易被理解成一种“为了少 malloc 几次而预分配对象”的优化。这个解释没有错，但它只碰到了最浅的一层。

真正困难的问题发生在复用之后。

一个 `ngx_connection_t` 的地址在整个 worker 生命周期中可以保持不变，但这个地址先后可能代表不同 socket、不同协议阶段，甚至不同类型的 endpoint。与此同时，旧连接留下的 epoll readiness、timer、posted event、reusable membership 或 fd index 都可能在未来重新把执行流带回这个地址。

所以 nginx 真正要解决的不是：

~~~text
怎样更快地拿到一块 ngx_connection_t 内存？
~~~

而是：

~~~text
怎样让一块稳定地址的 control slot
安全地经历很多代 logical connection，
并且保证上一代的 future execution
不会误伤下一代？
~~~

这篇文章沿一条完整生命周期来拆：

~~~text
worker 预分配 stable slots
        ↓
FREE slot
        ↓
ngx_get_connection(fd)
        ↓
新 generation 建立
        ↓
ACTIVE
        ↓
可能进入 REUSABLE
        ↓
资源压力触发 protocol-aware reclaim
        ↓
退出 timer / backend / posted / reusable / fd index
        ↓
ngx_free_connection()
        ↓
同一地址等待下一代
~~~

前面的三篇已经分别研究过这个生命周期中的几个局部机制：

- [Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation](worker-epoll-accept.md)
- [Timer Rbtree：Deadline Ordering、Lazy Update、Wrap-around 与 Shutdown Liveness](timer-rbtree.md)
- [Posted Event Queue：Readiness、Phase Scheduling、Coalescing 与 Next-tick Barrier](posted-event-queue.md)

这里要把它们重新拼成一个对象生命周期问题。

---

## 1. 先从一个最容易写错的朴素实现开始

假设我们自己写一个 reactor server，最直接的 connection 管理方式可能是：

~~~text
accept(fd)
  ↓
new Connection(fd)
  ↓
注册 epoll
  ↓
处理请求
  ↓
epoll_ctl(DEL)
close(fd)
delete Connection
~~~

如果连接数量不大，这个设计完全可以工作。

但在 nginx 这样的高并发 worker 里，会立刻碰到三个问题。

第一，connection control object 数量是高频变化的。每次 accept 都动态分配和释放一套 connection/event 元数据，会把 allocator 行为带进热点路径。

第二，server 通常希望对资源耗尽有明确边界，而不是“只要系统还能 malloc 就继续接”。如果 runtime 本身只允许 N 个 connection control slots，那么 overload policy 才能建立在一个可计算的容量上。

第三，也是最容易被忽略的一点：即使你执行了 `epoll_ctl(DEL)` 和 `close(fd)`，内核或其他调度结构中仍可能存在已经产生的旧执行义务。对象地址一旦被重新分配，旧事件就可能落到新对象上。

这第三个问题说明：

> **对象池最难的地方不是 free list，而是 logical lifetime。**

nginx 的设计因此把两个概念彻底分开：

~~~text
physical storage lifetime
        ≠
logical connection lifetime
~~~

---

## 2. worker 先建立固定数量的 Stable Control Slots

worker 初始化 event runtime 时，一次性分配三组数组。固定源码中的核心部分是：

~~~c
cycle->connections =
    ngx_alloc(sizeof(ngx_connection_t) * cycle->connection_n, cycle->log);
if (cycle->connections == NULL) {
    return NGX_ERROR;
}

cycle->read_events = ngx_alloc(sizeof(ngx_event_t) * cycle->connection_n,
                               cycle->log);
if (cycle->read_events == NULL) {
    return NGX_ERROR;
}

cycle->write_events = ngx_alloc(sizeof(ngx_event_t) * cycle->connection_n,
                                cycle->log);
if (cycle->write_events == NULL) {
    return NGX_ERROR;
}
~~~

随后每个 connection slot 固定绑定自己的 read/write event：

~~~c
i = cycle->connection_n;
next = NULL;

do {
    i--;

    c[i].data = next;
    c[i].read = &cycle->read_events[i];
    c[i].write = &cycle->write_events[i];
    c[i].fd = (ngx_socket_t) -1;

    next = &c[i];
} while (i);

cycle->free_connections = next;
cycle->free_connection_n = cycle->connection_n;
~~~

于是一个 worker 的基础对象关系是：

~~~text
connections[i]
   │
   ├── read  ──> read_events[i]
   │
   └── write ──> write_events[i]
~~~

这三个数组的 storage lifetime 接近整个 worker lifetime。

一个逻辑 TCP connection 却可能只活几秒钟。

因此同一个地址，例如：

~~~text
&connections[37]
~~~

可以先后表示：

~~~text
client A
  ↓ close
client B
  ↓ close
upstream C
  ↓ close
...
~~~

这里必须建立第一个核心不变量：

> **Stable address 只表示 storage 稳定，不表示 logical identity 稳定。**

---

## 3. `worker_connections` 真正限制的是什么

`cycle->connection_n` 最终对应 worker 的 connection capacity。

因此 `worker_connections` 更准确的理解不是：

~~~text
这个 worker 最多有 N 个客户端
~~~

而是：

~~~text
这个 worker 最多拥有 N 套
ngx_connection_t + read event + write event
runtime control slots
~~~

这两个说法差别很大。

因为 slot 不只会包装下游客户端，还可能包装：

- listening fd；
- upstream connection；
- resolver connection；
- channel/internal fd；
- 其他需要进入 event runtime 的 endpoint。

固定源码在初始化 listening socket 时也直接调用：

~~~c
c = ngx_get_connection(ls[i].fd, cycle->log);
~~~

所以一个 listening socket 本身也消耗 `ngx_connection_t` slot。

这就是为什么容量规划不能简单写成：

~~~text
worker_connections = 4096
→ 可以服务 4096 个客户端
~~~

如果一个业务连接还需要一条 upstream connection，那么一个请求链上可能同时占用多个 runtime slots。

更准确的容量关系是：

~~~text
OS fd capacity
        │
        ├── 约束 fd 是否还能创建
        │
worker connection slot capacity
        │
        ├── 约束 nginx 是否还有 control object
        │
per-connection/request memory
        │
        └── 约束每个活跃对象还能携带多少动态状态
~~~

三个边界不是一回事。

---

## 4. Free List 为什么可以直接借用 `c->data`

初始化时可以看到：

~~~c
c[i].data = next;
~~~

也就是说，FREE 状态下 `c->data` 被当成下一空闲 slot 的指针。

但 ACTIVE 状态下，`c->data` 往往承载上层 request/session/module state。

为什么一个字段可以这样复用？

因为两种语义严格依赖生命周期状态：

~~~text
FREE:
  c->data = next free slot

ACTIVE:
  c->data = protocol/application state
~~~

只要 FREE 和 ACTIVE 不重叠，这就是合法的 storage reuse。

对应的 pop/push 也非常直接。

`ngx_get_connection()` 取得 free slot：

~~~c
c = ngx_cycle->free_connections;

if (c == NULL) {
    ngx_log_error(NGX_LOG_ALERT, log, 0,
                  "%ui worker_connections are not enough",
                  ngx_cycle->connection_n);

    return NULL;
}

ngx_cycle->free_connections = c->data;
ngx_cycle->free_connection_n--;
~~~

`ngx_free_connection()` 放回 free list：

~~~c
c->data = ngx_cycle->free_connections;
ngx_cycle->free_connections = c;
ngx_cycle->free_connection_n++;
~~~

表面上它就是一个 singly-linked free list。

但不要因此把它理解成 lock-free stack。这里没有 CAS，也没有 ABA 处理。

原因不是 nginx 忘了做线程安全，而是这个结构的并发拓扑不同：

~~~text
one worker execution domain
        ↓
owns its connection pool
        ↓
ordinary pointer/counter mutation is enough
~~~

后面会再回到这个 owner model。

---

## 5. 从 FREE 到 ACTIVE：真正重要的是“换代”

拿到 free slot 后，nginx 不只是把 `fd` 填进去。

它先保留 read/write event 地址，再清空 connection 本体：

~~~c
rev = c->read;
wev = c->write;

ngx_memzero(c, sizeof(ngx_connection_t));

c->read = rev;
c->write = wev;
c->fd = s;
c->log = log;
~~~

随后处理 event state：

~~~c
instance = rev->instance;

ngx_memzero(rev, sizeof(ngx_event_t));
ngx_memzero(wev, sizeof(ngx_event_t));

rev->instance = !instance;
wev->instance = !instance;

rev->index = NGX_INVALID_INDEX;
wev->index = NGX_INVALID_INDEX;

rev->data = c;
wev->data = c;

wev->write = 1;
~~~

这里有两层动作。

第一层是 reset：

~~~text
上一代 connection/event 的 flags、
handler、index、协议状态不能泄漏到下一代
~~~

第二层是 generation 切换：

~~~text
rev->instance = !old_instance
wev->instance = !old_instance
~~~

所以一次 `ngx_get_connection()` 不只是“从对象池拿对象”，而是：

> **在稳定 storage 上启动一个新的 logical generation。**

---

## 6. 为什么 Stable Pointer 反而会制造 Logical UAF

如果对象每次都真正 `free()`，use-after-free 很容易被 ASan 之类的工具抓到。

对象池却更隐蔽。

假设旧逻辑对象 A 使用：

~~~text
address = 0x1000
~~~

A 结束后 slot 回池。

新逻辑对象 B 又使用：

~~~text
address = 0x1000
~~~

如果某个旧 callback、旧 readiness 或旧 handle 之后仍拿着 `0x1000`，它访问的是一块完全合法、仍映射在进程里的内存。

从 allocator 看，没有 UAF。

从 runtime 语义看，却已经访问了错误的一代对象。

这就是 **logical UAF**：

~~~text
physical pointer still valid
        +
logical generation already changed
        =
semantic use-after-free
~~~

因此对象池中的 identity 通常需要写成：

~~~text
storage address
+
generation
~~~

而不能只有 pointer。

---

## 7. nginx 怎样把 Generation 编进 epoll identity

在 Linux epoll backend 里，注册事件时不是只保存 connection pointer。

源码把 `ev->instance` 塞进 pointer 的最低位：

~~~c
ee.events = events | (uint32_t) flags;
ee.data.ptr = (void *) ((uintptr_t) c | ev->instance);

if (epoll_ctl(ep, op, c->fd, &ee) == -1) {
    ngx_log_error(NGX_LOG_ALERT, ev->log, ngx_errno,
                  "epoll_ctl(%d, %d) failed", op, c->fd);
    return NGX_ERROR;
}
~~~

为什么 pointer 最低位能拿来放 bit？

因为 `ngx_connection_t` 的正常对齐保证合法对象地址的最低位为 0。

所以一个 epoll user-data 实际表达：

~~~text
[ connection pointer | 1-bit instance ]
~~~

事件回来时再拆开：

~~~c
c = event_list[i].data.ptr;

instance = (uintptr_t) c & 1;
c = (ngx_connection_t *) ((uintptr_t) c & (uintptr_t) ~1);

rev = c->read;

if (c->fd == -1 || rev->instance != instance) {
    ngx_log_debug1(NGX_LOG_DEBUG_EVENT, cycle->log, 0,
                   "epoll: stale event %p", c);
    continue;
}
~~~

这里有两层 stale validation：

~~~text
c->fd == -1
  → 这个 slot 当前根本不代表 live fd

rev->instance != instance
  → slot 已经被复用成下一代
~~~

这正是 pointer reuse 场景里最关键的 generation check。

注意：nginx 的 1-bit generation 是特定实现技巧，不是“所有对象池都只需要一位”。

可迁移的原则是：

> **future completion 回来时，必须能够判断它对应的是不是当前 logical generation。**

在机器人 runtime 里，如果 slot 可能跨很多异步执行源复用，更常见的做法会是 32/64-bit generation counter。

---

## 8. Reusable 与 Free 完全不是一个状态

到这里还有一个很容易混淆的概念：

~~~text
free connection
~~~

和：

~~~text
reusable connection
~~~

不是同一回事。

FREE slot：

~~~text
已经没有当前 connection 语义
可以立即分配给新 fd
~~~

REUSABLE connection：

~~~text
仍然是一个 live connection
但协议层声明：
在资源紧张时可以优先牺牲它
~~~

例如 HTTP keepalive 正处于等待下一请求的 idle connection，就可能被标记为 reusable。

这个区分极其重要，因为 runtime 不能看到“资源紧张”就直接把任何 connection 回池。

它必须知道：

~~~text
哪些对象仍有业务价值
哪些对象当前可以被牺牲
~~~

这实际上是 protocol layer 与 resource manager 之间的一份 contract。

---

## 9. `ngx_reusable_connection()` 同时维护四类状态

固定源码：

~~~c
void
ngx_reusable_connection(ngx_connection_t *c, ngx_uint_t reusable)
{
    if (c->reusable) {
        ngx_queue_remove(&c->queue);
        ngx_cycle->reusable_connections_n--;

#if (NGX_STAT_STUB)
        (void) ngx_atomic_fetch_add(ngx_stat_waiting, -1);
#endif
    }

    c->reusable = reusable;

    if (reusable) {
        ngx_queue_insert_head(
            (ngx_queue_t *) &ngx_cycle->reusable_connections_queue, &c->queue);
        ngx_cycle->reusable_connections_n++;

#if (NGX_STAT_STUB)
        (void) ngx_atomic_fetch_add(ngx_stat_waiting, 1);
#endif
    }
}
~~~

一个 helper 同时维护：

~~~text
queue membership
reusable flag
reusable_connections_n
optional waiting statistics
~~~

这类设计很值得注意。

如果调用者可以随便：

~~~text
ngx_queue_remove(...)
~~~

却忘了同步更新 counter/flag，resource pressure policy 就会逐渐失真。

因此即使是 C 项目，没有 private member，也可以通过窄 helper 把一个 membership transition 封装起来。

这里还有一个顺序细节：

~~~c
ngx_queue_insert_head(...)
~~~

新的 reusable connection 插在队首。

后面 pressure reclaim 使用：

~~~c
ngx_queue_last(...)
~~~

从队尾选 victim。

于是这个队列自然形成一种近似“新对象靠前、旧对象靠后”的牺牲顺序。

它不是严格通用 LRU cache，但体现了相同的 recency 思路。

---

## 10. 资源快耗尽时，先 Reclaim，再拒绝 Admission

`ngx_get_connection()` 在真正 pop free slot 之前先调用：

~~~c
ngx_drain_connections((ngx_cycle_t *) ngx_cycle);
~~~

固定源码中的 pressure condition 是：

~~~c
if (cycle->free_connection_n > cycle->connection_n / 16
    || cycle->reusable_connections_n == 0)
{
    return;
}
~~~

换句话说，只有当：

~~~text
free_connection_n <= connection_n / 16
AND
reusable_connections_n > 0
~~~

才进入 reclaim。

一次 reclaim 也不是把所有 reusable connection 全清掉，而是：

~~~c
n = ngx_max(ngx_min(32, cycle->reusable_connections_n / 8), 1);
~~~

即：

~~~text
victim count =
max(min(32, reusable_connections_n / 8), 1)
~~~

这个设计体现两个目标。

第一，资源压力出现时主动释放低价值持有者，而不是立刻拒绝新连接。

第二，reclaim 工作量本身也必须有边界。

如果一次 admission 为了找一个 free slot 同步关闭几千个 idle connection，worker event loop 的延迟会突然爆炸。

所以 nginx 使用有限 batch。

可以把它理解成一条 degradation ladder：

~~~text
Level 0:
free slots 足够
→ 正常运行

Level 1:
free slots <= connection_n / 16
→ 有界地 reclaim reusable

Level 2:
仍然没有 free slot
→ admission failure
~~~

这比“无限扩容”或“瞬间清空所有 idle object”都更可控。

---

## 11. 为什么从 Queue Tail 选 Victim

真正取 victim 的代码是：

~~~c
q = ngx_queue_last(&cycle->reusable_connections_queue);
c = ngx_queue_data(q, ngx_connection_t, queue);
~~~

结合前面的：

~~~c
ngx_queue_insert_head(...)
~~~

可以得到：

~~~text
newly reusable
      ↓
queue head
      ...
older reusable
      ↓
queue tail
~~~

因此 tail 更接近“更早进入 reusable 状态”的对象。

这是一种低成本的近似 recency policy。

对机器人 runtime 来说，这个思想可以直接迁移到：

- idle device session；
- 可回收图像 buffer；
- 缓存的 RPC channel；
- 暂时不用的 GPU workspace；
- standby transport endpoint。

但有一个前提：

> **只有对象 owner 才知道它现在是否真的 safe-to-evict。**

资源管理器可以决定“什么时候需要牺牲”，却不应该猜“这个业务对象是否可以安全销毁”。

---

## 12. 为什么 Reclaim 不能直接 `ngx_free_connection(c)`

这是整个 connection pool 最值得学的一步。

pressure reclaim 没有写：

~~~text
ngx_free_connection(c)
~~~

而是：

~~~c
c->close = 1;
c->read->handler(c->read);
~~~

即 `c->close = 1` 先表达 close intent，然后通过 `c->read->handler` 回到对象自己的 protocol state machine。

原因很简单：

~~~text
resource manager 知道：
  “我需要释放 slot”

protocol handler 知道：
  “这个 connection 怎样关闭才是语义安全的”
~~~

例如 connection 可能正处于：

- HTTP keepalive；
- lingering close；
- TLS shutdown；
- QUIC/HTTP2 某个协议阶段；
- 还有模块 cleanup 要执行。

底层 pool 如果跳过这些状态直接把 slot 发布回 free list，就会把“storage reclaim”和“protocol retirement”混成一件事。

因此这里是典型的 **protocol-aware reclaim**：

~~~text
global/local resource pressure
        ↓
select victim
        ↓
set close intent
        ↓
invoke owner protocol handler
        ↓
protocol-safe retirement
        ↓
event runtime cleanup
        ↓
slot finally becomes FREE
~~~

这类分工可以概括成：

> **Global pressure chooses the victim; local semantics performs the safe retirement.**

---

## 13. 为什么同一个 Victim 有时会再触发一次 Handler

`ngx_drain_connections()` 后面还有一段很特别的逻辑：

~~~c
if (cycle->free_connection_n == 0 && c && c->reusable) {

    /*
     * if no connections were freed, try to reuse the last
     * connection again: this should free it as long as
     * previous reuse moved it to lingering close
     */

    c->close = 1;
    c->read->handler(c->read);
}
~~~

它揭示了一个重要事实：

~~~text
close request
≠
slot immediately FREE
~~~

第一次 handler 可能只是把 connection 从：

~~~text
KEEPALIVE / IDLE
~~~

推进到：

~~~text
LINGERING / FINALIZING
~~~

还没有真正释放 control slot。

因此生命周期更像：

~~~text
ACTIVE
  │
  ├──> REUSABLE
  │       │
  │       └── pressure close request
  │
  └──────────────> CLOSE_REQUESTED
                     │
                     ↓
                 RETIRING
                     │
                     ↓
                    FREE
~~~

REUSABLE 是一个 policy state，不是所有 connection 必经的状态。

ACTIVE connection 发生错误时也可以直接进入 retirement。

---

## 14. 真正的 Close 是“切断所有 Future Reachability”

`ngx_close_connection()` 比 `ngx_free_connection()` 长得多，因为前者做的是真正生命周期收尾。

固定源码的核心顺序：

~~~c
if (c->read->timer_set) {
    ngx_del_timer(c->read);
}

if (c->write->timer_set) {
    ngx_del_timer(c->write);
}

if (!c->shared) {
    if (ngx_del_conn) {
        ngx_del_conn(c, NGX_CLOSE_EVENT);

    } else {
        if (c->read->active || c->read->disabled) {
            ngx_del_event(c->read, NGX_READ_EVENT, NGX_CLOSE_EVENT);
        }

        if (c->write->active || c->write->disabled) {
            ngx_del_event(c->write, NGX_WRITE_EVENT, NGX_CLOSE_EVENT);
        }
    }
}

if (c->read->posted) {
    ngx_delete_posted_event(c->read);
}

if (c->write->posted) {
    ngx_delete_posted_event(c->write);
}

c->read->closed = 1;
c->write->closed = 1;

ngx_reusable_connection(c, 0);

ngx_free_connection(c);
~~~

把它画成对象图更容易看：

~~~text
                    ngx_connection_t
                           │
          ┌────────────────┼────────────────┐
          │                │                │
      read event       write event     reusable hook
          │                │                │
      ┌───┴────┐       ┌───┴────┐           │
      │        │       │        │           │
    epoll    timer    epoll    timer     reusable queue
      │        │       │        │
    posted   posted   posted   posted

另外还有：
fd → files[] → connection
protocol/request state → connection
~~~

只要其中任意结构仍然能在未来重新获得这个地址，就存在 future execution edge。

因此 close 的本质不是：

~~~text
destroy central object
~~~

而是：

~~~text
cut every future-reachability edge
then publish storage as reusable
~~~

这就是 **multi-index lifetime**。

---

## 15. Timer、Posted Queue 为什么必须在回池前退出

以 timer 为例。

`ngx_event_t` 内嵌自己的 rbtree node。只要 `timer_set` 仍为真，这个 event 仍然属于 timer tree。

如果 connection 已经回池，slot 又被新 connection 复用，而旧 timer node 还在 tree 中，未来 timeout 到来时：

~~~text
timer tree
   ↓
old embedded node
   ↓
same event address
   ↓
now belongs to new generation
~~~

这不是普通 dangling pointer。

它比 dangling pointer 更隐蔽，因为地址仍然合法。

posted queue 也是一样。

event 内部有 intrusive queue hook。只要 `posted` membership 没解除，未来 posted phase 就还能执行这个 event。

所以对象池里的核心不变量是：

> **intrusive hook 的 storage lifetime 必须覆盖它的 container membership；logical generation 结束前必须先结束旧 membership。**

也就是：

\[
Lifetime(storage) \supseteq Membership\ Interval
\]

但还不够。

因为 storage 会继续存在到下一代，所以真正要保证的是：

\[
Membership(old\ generation)
\cap
Execution(new\ generation)
=
\varnothing
\]

---

## 16. `files[]` 为什么使用 Compare-and-remove

`ngx_free_connection()` 还有一段容易被忽略的 secondary-index cleanup：

~~~c
if (ngx_cycle->files && ngx_cycle->files[c->fd] == c) {
    ngx_cycle->files[c->fd] = NULL;
}
~~~

它不是无条件：

~~~text
files[c->fd] = NULL
~~~

而是先检查：

~~~text
files[c->fd] == c
~~~

这相当于 compare-and-remove。

原因是 fd 本身也会复用。

如果某个 fd index 已经被更新成另一个 connection，旧对象的 cleanup 就不能无条件把新 mapping 抹掉。

因此：

~~~text
pointer reuse 需要 generation
fd reuse 也需要 identity validation
secondary index cleanup 需要确认“当前 entry 还是不是我”
~~~

这三个问题本质上都在处理：

> **物理标识符可复用时，旧生命周期不能覆盖新生命周期的状态。**

---

## 17. 为什么 `ngx_free_connection()` 应被看成 Publication

单看函数：

~~~c
void
ngx_free_connection(ngx_connection_t *c)
{
    c->data = ngx_cycle->free_connections;
    ngx_cycle->free_connections = c;
    ngx_cycle->free_connection_n++;

    if (ngx_cycle->files && ngx_cycle->files[c->fd] == c) {
        ngx_cycle->files[c->fd] = NULL;
    }
}
~~~

似乎只是“push 到 free list”。

从生命周期语义看，它实际上是在发布一条非常强的事实：

~~~text
this storage may now be reinterpreted
as a completely new logical connection
~~~

也就是说：

~~~text
retire old obligations
        ↓
publish free slot
        ↓
future ngx_get_connection() may reuse address
~~~

因此所有旧 generation 的 cleanup 都必须发生在 publication 之前。

在 nginx 单 worker owner 模型里，这个顺序不需要额外 release/acquire 来建立跨线程 happens-before，因为这些 control structures 不是多线程共享池。

如果把同样设计改成 multi-thread slot pool，publication 就会马上变成真正的并发内存模型问题：

- mutex；
- release/acquire；
- CAS；
- tagged pointer；
- generation counter；
- reclamation protocol。

所以不能看到 nginx 使用普通 pointer 就误以为“对象池天然不需要同步”。

真正起作用的是 ownership topology。

---

## 18. Owner Model：为什么这些 Counter 和 Queue 都不是 Atomic

`free_connection_n`、`reusable_connections_n`、free list 和 reusable queue 都是普通字段/链表操作。

它们能这样设计，是因为 connection lifecycle state 主要由当前 worker 自己推进。

可以把同步边界画成：

~~~text
shared / cross-worker boundary
        │
        └── listen accept ownership
            accept mutex / EPOLLEXCLUSIVE / reuseport
                    ↓
          accepted connection enters worker
                    ↓
worker-local ownership domain
        ├── connection slot
        ├── timer tree
        ├── posted queues
        ├── reusable queue
        └── protocol state
~~~

accept mutex 保护的也不是 connection pool。

它解决的是多个 worker 对共享 listening socket 的 accept admission。

一旦 connection 已进入某个 worker 的 runtime，其大部分 mutable lifecycle state 就由这个 owner 串行推进。

这与 Seastar 的 owner-shard 思路非常接近：

~~~text
nginx:
one worker event loop owns mutable connection state

Seastar:
one shard/core owns mutable local state
~~~

共同点不是“完全没有并发”，而是：

> **尽量让高频 mutable object 只有一个 execution owner，把同步集中在真正跨 owner 的边界。**

---

## 19. Prevent + Detect：为什么只做 Cleanup 仍然不够

到这里还剩一个问题。

Timer、posted queue、reusable queue、files index 等结构都由用户态 runtime 控制，可以在 close 时主动 remove。

但 epoll 已经产生的 ready item 并不一定能像普通 intrusive membership 一样逐条回收。

所以 nginx 对 stale edge 使用两类策略：

~~~text
Owner-controlled future edges
  timer
  posted
  reusable
  backend registration
  fd index

→ Prevent:
   close path 主动退出 membership
~~~

以及：

~~~text
External/kernel-side already-produced event
  epoll ready item

→ Detect:
   return 时检查 fd + instance generation
~~~

这是一个很通用的异步生命周期模式：

> **能撤销的 future edge 主动撤销；不能保证撤销的 external completion，在返回时用 token/generation 验证。**

只做 Prevent：

~~~text
可能漏掉已经产生的 kernel completion
~~~

只做 Detect：

~~~text
owner-controlled container 会长期残留无效 membership，
甚至直接破坏 intrusive container
~~~

两者必须结合。

---

## 20. Connection Pool 与 `ngx_pool_t` 不是同一种 Pool

nginx 里同时能看到 connection pool 和 `ngx_pool_t`，名字很容易把人带偏。

connection slot pool 解决：

~~~text
固定数量 control object
稳定地址
generation reuse
runtime capacity
multi-index retirement
~~~

`ngx_pool_t` 解决：

~~~text
同一 request/connection 生命周期内
大量小对象的 arena allocation
批量释放
~~~

因此一个 accepted connection 常常具有两层资源：

~~~text
Tier 1
stable ngx_connection_t control slot
        │
        └── worker lifetime storage

Tier 2
c->pool / request pools / buffers
        │
        └── logical connection or request lifetime
~~~

control slot 可以反复换代。

arena 则跟随某一代业务生命周期创建和销毁。

下一篇 [Memory Pool / Slab：Lifetime Arena、Shared-memory Allocator 与两种完全不同的 Pool](memory-pool-slab.md) 会专门拆这层 allocator 模型。

---

## 21. 用一个最小 C++ Runtime 复现“地址 + Generation”思想

下面不是 nginx 原始源码，而是一个完整可运行的教学程序。它只保留最关键的设计：固定 slot、free list、generation handle 和 stale-handle rejection。

~~~cpp
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <optional>
#include <vector>

struct Slot {
    int fd{-1};
    std::uint32_t generation{0};
    int next_free{-1};
    bool active{false};
};

struct Handle {
    std::size_t index{};
    std::uint32_t generation{};
};

class SlotPool {
public:
    explicit SlotPool(std::size_t n) : slots_(n) {
        for (std::size_t i = 0; i < n; ++i) {
            slots_[i].next_free =
                (i + 1 < n) ? static_cast<int>(i + 1) : -1;
        }
        free_head_ = n == 0 ? -1 : 0;
    }

    std::optional<Handle> acquire(int fd) {
        if (free_head_ == -1) {
            return std::nullopt;
        }

        const std::size_t index =
            static_cast<std::size_t>(free_head_);
        Slot& slot = slots_[index];

        free_head_ = slot.next_free;

        ++slot.generation;
        slot.fd = fd;
        slot.active = true;
        slot.next_free = -1;

        return Handle{index, slot.generation};
    }

    void release(Handle h) {
        Slot* slot = resolve(h);
        assert(slot != nullptr);

        slot->active = false;
        slot->fd = -1;
        slot->next_free = free_head_;
        free_head_ = static_cast<int>(h.index);
    }

    Slot* resolve(Handle h) {
        if (h.index >= slots_.size()) {
            return nullptr;
        }

        Slot& slot = slots_[h.index];

        if (!slot.active || slot.generation != h.generation) {
            return nullptr;
        }

        return &slot;
    }

private:
    std::vector<Slot> slots_;
    int free_head_{-1};
};

int main() {
    SlotPool pool(1);

    const Handle old_handle = *pool.acquire(10);
    assert(pool.resolve(old_handle) != nullptr);

    pool.release(old_handle);

    const Handle new_handle = *pool.acquire(22);

    assert(old_handle.index == new_handle.index);
    assert(old_handle.generation != new_handle.generation);

    assert(pool.resolve(old_handle) == nullptr);
    assert(pool.resolve(new_handle) != nullptr);

    std::cout << "stale handle rejected, new generation accepted\n";
}
~~~

运行时最关键的现象是：

~~~text
old_handle.index == new_handle.index
~~~

地址/slot 相同，但：

~~~text
old_handle.generation != new_handle.generation
~~~

所以旧异步完成无法仅凭 slot identity 命中新对象。

这比单纯解释“对象池减少 malloc”更接近 nginx `instance` bit 的真正价值。

---

## 22. 如果把这个模式迁移到机器人 Runtime

假设一个机器人进程最多同时管理 64 个设备 session。

可以预分配：

~~~text
DeviceSessionSlot[64]
~~~

每个 slot 持有：

~~~text
fd / handle
generation
read/write event hooks
timer hook
state pointer
reclaimable membership
~~~

设备断线重连时，同一个 slot 很可能快速被复用。

而上一代设备 session 仍可能残留：

- timer completion；
- async I/O completion；
- thread-pool result；
- DMA callback；
- CAN/serial receive task；
- timeout state machine；
- deferred cleanup。

如果这些 completion 只拿裸 pointer：

~~~text
Slot*
~~~

就有机会撞到新 session。

更稳妥的 handle 是：

~~~text
struct SessionHandle {
    DeviceSessionSlot* slot;
    std::uint32_t generation;
};
~~~

completion 返回时先验证：

~~~text
handle.generation == slot->generation ?
~~~

再决定是否继续。

同时，对于 runtime 自己拥有的 membership：

~~~text
timer queue
ready queue
reclaimable list
fd registry
~~~

应在 logical retirement 时主动 remove，而不是把所有安全责任都推给 generation check。

这就是 nginx 的 Prevent + Detect 思路在机器人系统中的直接迁移。

---

## 23. 资源回收策略也可以迁移，而不只是对象池

nginx 的 reusable queue 说明对象池不仅是 allocation mechanism，也可以承载 overload policy。

假设一个视觉/推理 runtime 的 GPU workspace 紧张。

Global allocator 知道：

~~~text
VRAM pressure
~~~

某个 model session owner 才知道：

~~~text
哪些 workspace 当前没有 in-flight CUDA work
哪些 buffer 已经可以牺牲
~~~

那么可以建立与 nginx 类似的 contract：

~~~text
owner marks reclaimable
        ↓
global manager observes pressure
        ↓
selects bounded victim batch
        ↓
asks owner to retire safely
        ↓
owner waits/cleans protocol-specific obligations
        ↓
storage returns to free pool
~~~

错误做法是 global manager 直接：

~~~text
cudaFree(anything old)
~~~

因为“旧”不等于“安全释放”。

对象也许仍然被 stream、event、graph capture 或其他引用持有。

所以真正可迁移的不是 nginx 的某个具体 queue API，而是：

> **resource pressure policy 与 object semantic retirement 分层。**

---

## 24. 把整个 Connection Lifecycle 再跑一遍

### 24.1 初始化

~~~text
worker starts
   ↓
allocate connections[]
allocate read_events[]
allocate write_events[]
   ↓
bind slot i ↔ read/write event i
   ↓
all slots linked into free_connections
~~~

### 24.2 新 fd 进入 runtime

~~~text
new fd
  ↓
ngx_get_connection(fd)
  ↓
check fd-index capacity
  ↓
ngx_drain_connections()
  ↓
free slot available?
  ├── no → admission failure
  └── yes
       ↓
pop free_connections
       ↓
reset connection/event state
       ↓
toggle instance generation
       ↓
bind fd/log/event->data
       ↓
ACTIVE
~~~

### 24.3 对象进入可牺牲状态

~~~text
protocol becomes idle/safe-to-evict
  ↓
ngx_reusable_connection(c, 1)
  ↓
insert head of reusable_connections_queue
  ↓
reusable flag/counter/stat updated
~~~

### 24.4 资源压力出现

~~~text
free_connection_n <= connection_n / 16
  ↓
choose bounded batch:
max(min(32, reusable_connections_n / 8), 1)
  ↓
ngx_queue_last(...)
  ↓
older reusable victim
  ↓
c->close = 1
  ↓
c->read->handler(...)
  ↓
protocol-aware retirement
~~~

### 24.5 真正关闭

~~~text
ngx_close_connection(c)
  ↓
remove read/write timers
  ↓
remove event backend registration
  ↓
remove posted events
  ↓
mark events closed
  ↓
remove reusable membership
  ↓
ngx_free_connection(c)
  ↓
compare-and-remove files[c->fd] == c
  ↓
invalidate/close fd
  ↓
slot is available for next generation
~~~

### 24.6 旧 epoll readiness 迟到

~~~text
old kernel event returns
  ↓
decode pointer + instance
  ↓
c->fd == -1 ?
or
rev->instance != instance ?
  ↓ yes
discard stale event
~~~

这样整个闭环才完整。

---

## 25. 最后把几个最容易混淆的概念压缩到一张表

| 概念 | 它是什么 | 不是什麽 |
| --- | --- | --- |
| stable slot | worker-lifetime control storage | 永久 logical connection identity |
| free slot | 已彻底退出当前连接语义、可立即换代 | idle keepalive |
| reusable connection | 仍 live，但 protocol 允许资源紧张时牺牲 | 已回 free list |
| generation / `instance` | 区分同地址不同逻辑生命期 | fd 本身 |
| `ngx_free_connection()` | 发布 slot 可再次分配 | 完整 protocol close |
| `ngx_close_connection()` | 退出多个 future-reachability index 后回池 | 单纯 `close(fd)` |
| reusable queue | pressure victim 候选集合 | thread-safe global pool |
| `ngx_pool_t` | 生命周期 arena allocator | connection control-slot pool |

---

## 26. 真正值得记住的设计原则

nginx 的 connection pool 最终可以压缩成六条原则。

第一，**bounded control storage**。固定数量的 stable slots 把 runtime capacity 变成明确边界，而不是依赖 allocator 一直成功。

第二，**physical storage 与 logical generation 分离**。同一个地址可以被多次复用，所以 pointer 不能独自承担 identity。

第三，**reclaimable 不等于 free**。可牺牲 live object 与已经结束生命周期的 storage 必须是两个状态。

第四，**multi-index retirement 先于 storage publication**。Timer、posted、backend、reusable、fd index 等 future-reachability edge 没切断之前，slot 不能进入 free list。

第五，**Prevent + Detect**。runtime 自己控制的旧 membership 主动撤销；无法保证撤销的外部 completion 用 generation/token 验证。

第六，**ownership topology 决定同步方式**。nginx 的 free list 不是因为“链表天然无锁”才不用 mutex/atomic，而是因为 worker-local single-owner sequencing 已经提供了结构性串行化。

如果只保留一句话：

> **nginx connection pool 的核心不是“省 malloc”，而是把有限 connection control resources 组织成 stable storage + logical generation + pressure-aware reclaim + multi-index retirement，使同一块地址可以在高频复用下仍然保持正确的异步生命周期。**
