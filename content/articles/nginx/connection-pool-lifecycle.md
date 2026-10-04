# Connection Pool 与 Reusable Queue：为什么 nginx 不为每个 Socket 动态 new 一个对象

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

高并发 server 的连接对象具有非常典型的生命周期：创建频率高、对象大小固定、最大并发数明确、关闭后可立即复用。

nginx 没有让每次 accept 都 malloc/free 一组 `ngx_connection_t + ngx_event_t`，而是把它们预先组织成有界池。

## Connection Slot 是固定资源

worker 拥有：

~~~text
connections[]
read_events[]
write_events[]
free_connections
free_connection_n
~~~

`worker_connections` 因而不是一个抽象配置数字，而是 runtime object capacity。

## Free List 为什么直接复用 `c->data`

`ngx_free_connection()`：

~~~c
c->data = ngx_cycle->free_connections;
ngx_cycle->free_connections = c;
ngx_cycle->free_connection_n++;
~~~

空闲 connection 不再需要原本业务 data，于是 nginx 直接借 `c->data` 字段作为 singly-linked free-list pointer。

这是典型的对象状态复用：

~~~text
ACTIVE:
  c->data = request/session state

FREE:
  c->data = next free connection
~~~

同一个字段在互斥生命周期阶段承担不同角色。

## `ngx_get_connection()` 为什么不分配内存

~~~c
c = ngx_cycle->free_connections;
if (c == NULL) {
  ... worker_connections are not enough ...
  return NULL;
}

ngx_cycle->free_connections = c->data;
ngx_cycle->free_connection_n--;
~~~

获取连接本质是 free-list pop。

这带来：

- O(1) 获取；
- 无 allocator jitter；
- 最大连接对象数量有界；
- 资源耗尽可以明确进入 overload policy。

## 为什么每次复用都要把 Connection 清零

~~~c
rev = c->read;
wev = c->write;

ngx_memzero(c, sizeof(ngx_connection_t));

c->read = rev;
c->write = wev;
~~~

connection slot 会跨完全不同的 socket 重复使用。

如果上一代的 flags、handler、protocol pointer 泄漏到下一代，就是典型 stale state bug。

所以复用前 reset 是生命周期协议的一部分。

## 最精彩的细节：`instance` Bit

read/write event 在复用时：

~~~c
instance = rev->instance;

ngx_memzero(rev, sizeof(ngx_event_t));
ngx_memzero(wev, sizeof(ngx_event_t));

rev->instance = !instance;
wev->instance = !instance;
~~~

每次 connection slot 被重新分配，instance bit 翻转。

这不是 connection pool 自己孤立的技巧。epoll 注册时真正写入内核的是 `connection pointer | instance`；ready event 返回后先拆出 instance，并验证 `c->fd != -1` 且当前 read event generation 仍匹配。也就是说，slot pool 与 event backend 共同组成 stale-event 防线。完整 accept/epoll 执行链见 [Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation](worker-epoll-accept.md)。

## 为什么只要 1 Bit 就有用

epoll ready list 里可能已经存在旧 socket generation 的事件。

时间线：

~~~text
slot C / fd 10 / instance 0
↓ epoll marks readable
close fd 10
↓
slot C reused
fd 22 / instance 1
↓
old epoll event finally returned
~~~

如果只拿 pointer `C`，runtime 会把旧 event 当成新连接事件。

nginx 把 event_list.data.ptr 的最低 bit 存 instance：

~~~text
pointer | instance
~~~

poll 返回后比较：

~~~c
instance = (uintptr_t) c & 1;
c = (ngx_connection_t *) ((uintptr_t) c & ~1);

if (c->fd == -1 || rev->instance != instance)
  continue;  /* stale */
~~~

这就是 generation handle 的极简版本。

## 为什么 Pointer 低位可以拿来存 Bit

`ngx_connection_t*` 至少按机器字节对齐，合法 pointer 最低位本来就是 0。

因此可以 pointer tagging：

~~~text
aligned pointer bits
+
1-bit generation tag
~~~

这在 GC、VM、lock-free runtime、object handle 中很常见。

## Reusable Queue 与 Free List 不是一回事

Free list：

~~~text
已经无连接语义
可以立即分配给新 fd
~~~

Reusable queue：

~~~text
仍然是 active connection
但业务允许在资源紧张时主动关闭
~~~

`ngx_reusable_connection(c, 1)` 把 connection 插到 intrusive queue 头部。

## 为什么 Resource Exhaustion 时不直接拒绝新连接

`ngx_drain_connections()` 在 free slot 很少时尝试回收 reusable connection。

触发条件：

~~~text
free_connection_n <= connection_n / 16
AND
reusable_connections_n > 0
~~~

每次回收数量：

~~~c
n = ngx_max(ngx_min(32, reusable_connections_n / 8), 1);
~~~

也就是有限批量回收，而不是一次清空所有 keepalive。

## 为什么取 Queue 尾部

~~~c
q = ngx_queue_last(&cycle->reusable_connections_queue);
~~~

新的 reusable connection 插在 head，因此 tail 更接近“更老的 reusable connection”。

nginx 实际形成一种近似 LRU 的牺牲顺序。

## 回收不是直接 `free(c)`

nginx：

~~~c
c->close = 1;
c->read->handler(c->read);
~~~

它仍然通过 connection 自己的 protocol/read handler 进入正确关闭路径。

这非常关键：

> allocator 发现资源紧张，不代表 allocator 可以跳过业务状态机直接摧毁对象。

## 为什么 Connection Close 顺序很长

`ngx_close_connection()` 会：

其中“delete read/write timer”是 storage-reuse correctness 的前置条件：Timer tree 保存的是嵌在 `ngx_event_t` 里的 node，只要 membership 仍在，未来 expire 就能重新得到这个 event 地址。connection slot 回池前必须先切断这条 future reachability。Timer 的 intrusive membership、lazy update 与 shutdown liveness 见 [Timer Rbtree：Deadline Ordering、Lazy Update、Wrap-around 与 Shutdown Liveness](timer-rbtree.md)。

~~~text
delete read timer
delete write timer
unregister backend events
remove posted events
mark read/write closed
remove reusable status
return slot to free list
close fd
~~~

资源对象同时挂在 timer tree、posted queue、epoll backend、reusable queue 等多个索引中。

关闭就是从所有索引里解除 membership。

其中 posted queue 不是普通“callback list”：同一个 event只有一个 intrusive queue hook，`posted` bit会 coalesce 重复投递，executor 在 callback 前先 unlink，而 close 路径必须在 slot reuse 前取消 pending posted membership。完整 phase/lifetime 协议见 [Posted Event Queue：Readiness、Phase Scheduling、Coalescing 与 Next-tick Barrier](posted-event-queue.md)。

## 这是一种 Multi-index Object 设计

同一个 connection/event 可能同时存在于：

~~~text
fd / epoll identity
timer rbtree
posted event queue
reusable queue
protocol state
~~~

因此对象生命周期的真正复杂度来自：

> 一个对象同时属于多少个数据结构。

## 对普通 Runtime 的迁移

固定容量 session/task/device slot 可以设计：

~~~text
free list
+
active indexes
+
reclaimable/reusable queue
+
generation tag
~~~

适合：

- TCP session；
- device channel；
- fixed task slot；
- request object pool；
- shared-memory slot。

## 设计结论

1. 固定上限资源适合预分配 + O(1) free list。
2. slot reuse 必须配 generation/stale-event 防护。
3. “可回收”与“已空闲”是两个不同状态。
4. resource pressure 可以触发有策略的旧对象回收，而不是无限扩容。
5. 对象销毁必须解除它在所有 secondary index 中的 membership。
