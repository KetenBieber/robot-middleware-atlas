# Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 的并发模型经常被一句话概括成：

~~~text
master
+
multiple workers
+
epoll
~~~

这句话没有错，但信息密度太低。

真正值得拆的是下面这条完整执行链：

~~~text
master / worker process topology
        ↓
worker-local event loop
        ↓
listening socket admission
        ↓
accept ownership arbitration
        ↓
accept4()
        ↓
preallocated connection slot
        ↓
epoll registration
        ↓
read/write handler execution
        ↓
close / slot reuse
        ↓
generation-bit stale-event rejection
~~~

这条链同时回答了几个经典 Runtime 问题：

- 多 worker 都能看到同一个 listening socket，谁来 accept？
- 为什么这个固定版本默认并不使用 accept mutex？
- `EPOLLEXCLUSIVE`、accept mutex、`SO_REUSEPORT` 分别解决哪一层共享？
- accept 成功以后，为什么 nginx 不是直接 `malloc(new connection)`？
- connection slot 被复用后，Linux epoll 队列里残留的旧 ready event 怎么办？
- graceful shutdown 为什么必须先停止新的 accept，再 drain 旧连接？

这几个问题共同构成 nginx 的 owner / admission / generation 模型。

---

## 一、先看 nginx 的 Concurrency Topology

nginx 不是：

~~~text
one process
+
global MPMC task queue
+
many worker threads
~~~

典型 Unix 部署更接近：

~~~text
master process
    |
    +-- worker 0
    |      |
    |      +-- single-thread event loop
    |
    +-- worker 1
    |      |
    |      +-- single-thread event loop
    |
    +-- worker 2
           |
           +-- single-thread event loop
~~~

每个 worker 进程有自己的：

- `ngx_cycle_t` runtime state；
- connection slot array；
- read/write event array；
- timer tree；
- posted event queue；
- epoll fd；
- module-local state。

---

## 二、Worker Local State 为什么大量不需要 Mutex

worker 的主循环：

~~~c
for ( ;; ) {
    ...
    ngx_process_events_and_timers(cycle);
    ...
}
~~~

绝大多数：

- socket readiness；
- connection handler；
- request handler；
- timer expiry；
- posted event；

都在同一个 worker execution context 中推进。

所以一个 connection 的：

~~~text
read event
write event
buffer
request state
timer state
reusable state
~~~

通常不需要多个 CPU 同时直接修改。

---

## 三、真正共享的热点被尽量缩小到边界

例如：

~~~text
listening socket accept ownership
~~~

才需要跨 worker 协调。

而不是：

~~~text
每个已建立 connection 都挂一把跨进程锁
~~~

这和前面 Seastar 的思路非常像：

> **先靠 ownership topology 缩小共享面，再给边界选择同步机制。**

---

## 四、`ngx_process_events_and_timers()` 是 Worker Reactor 的中心

源码：

~~~cpp
void
ngx_process_events_and_timers(ngx_cycle_t *cycle)
{
    if (ngx_timer_resolution) {
        timer = NGX_TIMER_INFINITE;
        flags = 0;

    } else {
        timer = ngx_event_find_timer();
        flags = NGX_UPDATE_TIME;
    }

    ...
    ngx_process_events(cycle, timer, flags);
    ...
}
~~~

默认 epoll backend 下：

~~~text
ngx_process_events
→ ngx_epoll_process_events
→ epoll_wait
~~~

---

## 五、Timer 与 I/O 共用同一个 Sleep Point

event loop 先计算：

~~~text
nearest timer deadline
~~~

再把：

~~~text
timeout
~~~

传给：

~~~text
epoll_wait
~~~

所以不是：

~~~text
一条 timer thread
+
一条 network thread
~~~

而是：

~~~text
one owner loop
→ sleep until I/O or timer deadline
~~~

---

## 六、Worker Loop 的真实 Phase Order

固定源码可以概括成：

~~~text
find nearest timer
        ↓
accept admission handling
        ↓
move posted_next if needed
        ↓
epoll_wait
        ↓
posted accept events
        ↓
release accept mutex if held
        ↓
expire timers
        ↓
posted normal events
~~~

这个顺序不是偶然。

---

## 七、为什么 Accept Posted Event 要先于普通 Posted Event

如果当前 worker 临时获得了：

~~~text
accept right
~~~

它应尽快：

~~~text
consume accept readiness
~~~

然后释放共享 accept ownership。

不能先跑一大堆普通 request callback，

让其他 worker 长时间拿不到新连接 admission。

---

## 八、Accept Ownership 是 Connection Ownership 的入口

建立连接以后：

~~~text
connection belongs to one worker
~~~

后续 read/write state 基本局部。

所以最关键的跨 worker 分流时刻恰恰是：

~~~text
accept
~~~

---

## 九、多 Worker 为什么会竞争同一个 Listening Socket

如果 master 创建一个 listening fd，

然后多个 worker fork 继承：

~~~text
Worker 0 ──┐
Worker 1 ──┼── same listening socket
Worker 2 ──┘
~~~

新连接进入 listen backlog 时，

多个 worker 理论上都可能等待：

~~~text
same readiness condition
~~~

这会带来：

- thundering herd；
-无效 wakeup；
- accept competition；
- cache/调度浪费。

---

## 十、nginx 有三种不同层级的解决办法

当前固定源码可以看到：

~~~text
1. accept mutex
2. EPOLLEXCLUSIVE
3. SO_REUSEPORT
~~~

它们不是“三个版本的同一个函数”。

它们分别改变不同层级的 ownership。

---

## 十一、先纠正一个常见历史印象：Accept Mutex 当前默认关闭

配置初始化：

~~~cpp
ngx_conf_init_value(
    ecf->accept_mutex, 0);
~~~

所以这个固定版本默认：

~~~text
accept_mutex off
~~~

---

## 十二、什么时候 Accept Mutex 才真正启用

worker 初始化：

~~~cpp
if (ccf->master
    && ccf->worker_processes > 1
    && ecf->accept_mutex)
{
    ngx_use_accept_mutex = 1;
}
~~~

需要同时满足：

~~~text
master mode
+
worker_processes > 1
+
config accept_mutex on
~~~

---

## 十三、因此 Accept Mutex 是显式配置路径，不是当前 Linux 默认主路径

这点很重要。

学习 nginx 历史时经常会看到：

~~~text
nginx 用 accept mutex 解决惊群
~~~

但看当前固定源码必须补一句：

> **它仍存在，但默认关闭；Linux epoll 多 worker 更倾向利用内核的 `EPOLLEXCLUSIVE`。**

---

## 十四、Accept Mutex 真正锁的是什么

不是：

~~~text
connection table
~~~

也不是：

~~~text
HTTP request
~~~

它锁的是：

> **哪个 worker 当前注册/拥有 listening read event 的 accept 资格。**

---

## 十五、拿到 Mutex 后发生什么

`ngx_trylock_accept_mutex()`：

~~~cpp
if (ngx_shmtx_trylock(&ngx_accept_mutex)) {

    if (ngx_enable_accept_events(cycle)
        == NGX_ERROR) {
        ...
    }

    ngx_accept_events = 0;
    ngx_accept_mutex_held = 1;

    return NGX_OK;
}
~~~

---

## 十六、`enable_accept_events()` 不是简单设一个 Bool

它遍历：

~~~text
cycle->listening
~~~

对每个 inactive listen read event：

~~~cpp
ngx_add_event(
    c->read,
    NGX_READ_EVENT,
    0);
~~~

也就是：

~~~text
真正把 accept readiness 注册进 event backend
~~~

---

## 十七、拿不到 Mutex 后会怎样

如果 worker 之前持有 accept right：

~~~text
ngx_accept_mutex_held == true
~~~

但这轮抢锁失败：

~~~cpp
ngx_disable_accept_events(cycle, 0);
ngx_accept_mutex_held = 0;
~~~

所以：

~~~text
loss of ownership
→ remove listening events
~~~

---

## 十八、这是 Event Registration Ownership

accept mutex 控制的不是：

~~~text
谁先调用 accept()
~~~

而更早：

~~~text
谁当前有资格收到 accept readiness
~~~

---

## 十九、为什么 Mutex Held 时开启 `NGX_POST_EVENTS`

事件循环：

~~~cpp
if (ngx_accept_mutex_held) {
    flags |= NGX_POST_EVENTS;
}
~~~

这会让 epoll ready 不立即递归执行 accept handler，

而是先放入：

~~~text
ngx_posted_accept_events
~~~

---

## 二十、这样 Worker 可以明确控制临界区

顺序：

~~~text
hold accept mutex
        ↓
epoll_wait
        ↓
collect accept readiness
        ↓
process posted accept
        ↓
unlock mutex
~~~

---

## 二十一、如果没拿到 Mutex，为什么缩短 epoll timeout

源码：

~~~cpp
if (timer == NGX_TIMER_INFINITE
    || timer > ngx_accept_mutex_delay)
{
    timer = ngx_accept_mutex_delay;
}
~~~

---

## 二十二、这是“重新竞争 Ownership”的 Deadline

如果 loser 直接：

~~~text
epoll_wait forever
~~~

它可能很久没有机会重新尝试：

~~~text
accept mutex
~~~

所以：

~~~text
accept_mutex_delay
~~~

不仅是配置参数，

也是：

~~~text
ownership rescheduling latency
~~~

---

## 二十三、但当前 Linux epoll 默认更重要的是 `EPOLLEXCLUSIVE`

worker 初始化 listening event 时：

~~~cpp
if ((ngx_event_flags & NGX_USE_EPOLL_EVENT)
    && ccf->worker_processes > 1)
{
    ngx_use_exclusive_accept = 1;

    ngx_add_event(
        rev,
        NGX_READ_EVENT,
        NGX_EXCLUSIVE_EVENT);
}
~~~

而：

~~~text
NGX_EXCLUSIVE_EVENT
→ EPOLLEXCLUSIVE
~~~

---

## 二十四、`EPOLLEXCLUSIVE` 把惊群抑制下推给 Kernel

共享 listen fd仍然存在：

~~~text
one listening socket
shared by workers
~~~

但 epoll registration告诉内核：

~~~text
不要因为同一个 event
把所有等待者都唤醒
~~~

---

## 二十五、与 Accept Mutex 的最大区别

Accept Mutex：

~~~text
userspace decides
which worker currently registers accept event
~~~

EPOLLEXCLUSIVE：

~~~text
all relevant workers may register
kernel chooses wakeup
~~~

---

## 二十六、一个是 User-space Ownership Arbitration

另一个是：

~~~text
Kernel Wakeup Arbitration
~~~

两者虽然都缓解惊群，

机制边界完全不同。

---

## 二十七、为什么 EPOLLEXCLUSIVE 路径不需要每轮 Lock

因为：

~~~text
accept readiness competition
~~~

被交给：

~~~text
kernel epoll wakeup semantics
~~~

用户态不再需要：

~~~text
每轮 trylock / enable / disable
~~~

---

## 二十八、第三条路径：SO_REUSEPORT

这比 EPOLLEXCLUSIVE 更进一步。

`ngx_clone_listening()` 会为 reuseport 情况建立多份 listening entry。

每份记录：

~~~text
ls[i].worker
~~~

---

## 二十九、Worker 初始化时只处理自己的 Socket

源码：

~~~cpp
if (ls[i].reuseport
    && ls[i].worker != ngx_worker)
{
    continue;
}
~~~

---

## 三十、所以 Topology 变成

~~~text
Worker 0
→ listening socket 0

Worker 1
→ listening socket 1

Worker 2
→ listening socket 2
~~~

每个 fd 都：

~~~text
bind same address/port
+
SO_REUSEPORT
~~~

---

## 三十一、流量分配交给 Kernel Socket Selection

此时不只是：

~~~text
共享一个 fd
但少唤醒一些 worker
~~~

而是：

~~~text
kernel 在多个 listen socket 中选一个
~~~

---

## 三十二、所以 Reuseport 更接近 Ownership Partition

EPOLLEXCLUSIVE：

~~~text
shared resource
+
kernel arbitration
~~~

Reuseport：

~~~text
per-worker resource
+
kernel flow distribution
~~~

---

## 三十三、三种策略可以画成层级

~~~text
Accept Mutex
    ↓
one shared socket
userspace owns accept right

EPOLLEXCLUSIVE
    ↓
one shared socket
kernel arbitrates wakeup

SO_REUSEPORT
    ↓
multiple listen sockets
worker-local accept ownership
kernel distributes flows
~~~

---

## 三十四、这是“优化锁”与“消灭共享”的差别

如果能把：

~~~text
one shared resource
~~~

拆成：

~~~text
N owned resources
~~~

通常比不断优化共享锁更彻底。

---

## 三十五、为什么 Reuseport 路径绕过 Accept Mutex Disable

`ngx_disable_accept_events()` 里：

~~~cpp
if (ls[i].reuseport && !all) {
    continue;
}
~~~

原因：

~~~text
这个 listen socket 本来就是当前 worker 自己的
~~~

accept mutex 不应该把它禁掉。

---

## 三十六、不同 Ownership Mechanism 可以同时存在于同一 Runtime

共享 sockets：

~~~text
可能受 mutex/exclusive 管理
~~~

reuseport sockets：

~~~text
per-worker local ownership
~~~

所以不能写成：

~~~text
nginx 全局只使用一种 accept 模式
~~~

---

## 三十七、现在进入真实 Accept Handler

`ngx_event_accept()` 大致：

~~~text
read-ready on listening socket
        ↓
accept4/accept
        ↓
new fd
        ↓
ngx_get_connection(fd)
        ↓
allocate request/connection pool
        ↓
initialize connection
        ↓
call listening handler
~~~

---

## 三十八、为什么优先 `accept4(... SOCK_NONBLOCK)`

如果 kernel 支持：

~~~text
accept4
~~~

可以在新 fd 建立时一次完成：

~~~text
accept
+
nonblocking flag
~~~

减少：

~~~text
accept
→ fcntl/set nonblocking
~~~

的额外 syscall/竞态窗口。

---

## 三十九、如果 Kernel 返回 `ENOSYS`

源码会：

~~~text
use_accept4 = 0
~~~

以后退回普通：

~~~text
accept()
~~~

这是典型 feature-probe fallback。

---

## 四十、`EAGAIN` 的语义很明确

listen readiness 被消费到：

~~~text
当前没有更多 connection
~~~

就：

~~~text
return
~~~

不是错误。

---

## 四十一、`EMFILE/ENFILE` 为什么特殊处理

如果进程/系统 fd耗尽：

~~~text
accept()
~~~

无法继续建立新连接。

nginx 会：

~~~text
disable accept events
~~~

---

## 四十二、如果正在使用 Accept Mutex

它还会：

~~~text
unlock accept mutex
ngx_accept_disabled = 1
~~~

让别的 worker有机会接管新连接。

---

## 四十三、如果没使用 Accept Mutex

则给 listening event：

~~~text
add timer(accept_mutex_delay)
~~~

暂时退避，

避免无休止地：

~~~text
ready → accept → EMFILE
~~~

busy loop。

---

## 四十四、Admission Control 不只是“有新连接就接”

它还要考虑：

- fd capacity；
- connection slot capacity；
- worker load；
- allocator/pool；
- OS errors。

---

## 四十五、`ngx_accept_disabled` 是一个 Load-shedding Heuristic

accept 成功后：

~~~cpp
ngx_accept_disabled =
    ngx_cycle->connection_n / 8
    - ngx_cycle->free_connection_n;
~~~

---

## 四十六、当 Free Slot 很少时

这个值变正，

event loop 后续：

~~~text
跳过若干轮 accept mutex competition
~~~

相当于：

~~~text
当前 worker已经偏满
→ 少抢一些新连接
~~~

---

## 四十七、这是非常轻量的 Admission Feedback

它不是精确 load balancer。

而是：

~~~text
free slot pressure
→ reduce accept aggressiveness
~~~

---

## 四十八、Accept 成功并不等于 Connection Runtime 已就绪

拿到新 socket fd：

~~~text
s
~~~

后还必须：

~~~text
allocate one ngx_connection_t slot
~~~

---

## 四十九、nginx Connection 对象不是每次 `malloc`

worker 初始化时一次分配：

~~~text
connections[connection_n]
read_events[connection_n]
write_events[connection_n]
~~~

并建立：

~~~text
free_connections
~~~

单链表。

---

## 五十、Connection Capacity 是显式有界资源

~~~text
worker_connections
~~~

不只是配置数字。

它最终控制：

~~~text
固定 connection slot 数
~~~

---

## 五十一、`ngx_get_connection()` 先尝试 Drain Reusable Connections

源码：

~~~cpp
ngx_drain_connections(cycle);
~~~

然后才：

~~~text
pop free_connections
~~~

---

## 五十二、这意味着 Admission 与 Reclamation 是一条链

如果 free slots紧张：

~~~text
先回收 reusable/idle connection
~~~

再尝试给新 fd分配 slot。

---

## 五十三、`free_connections` 是 Intrusive Free List

free slot 时：

~~~cpp
c->data = ngx_cycle->free_connections;
ngx_cycle->free_connections = c;
~~~

也就是借用：

~~~text
connection.data
~~~

作为 next pointer。

---

## 五十四、Connection Slot 空闲时没有业务对象语义

所以可以复用字段：

~~~text
data
~~~

承载 free-list linkage。

这和前面 Seastar dead storage 复用为 reclaim node 是同一类思想。

---

## 五十五、Slot 重新取出以后

~~~cpp
c = free_connections;
free_connections = c->data;
free_connection_n--;
~~~

然后恢复：

~~~text
read event pointer
write event pointer
fd
log
~~~

---

## 五十六、真正关键：Slot Reuse 会翻转 `instance`

源码：

~~~cpp
instance = rev->instance;

ngx_memzero(rev, ...);
ngx_memzero(wev, ...);

rev->instance = !instance;
wev->instance = !instance;
~~~

每次 connection slot重新分配：

~~~text
generation bit toggles
~~~

---

## 五十七、为什么只用 1 Bit 就有价值

假设 slot C：

~~~text
Generation 0
fd = 10
~~~

它在 epoll里有 ready event。

随后：

~~~text
fd 10 close
slot C free
slot C immediately reused
new fd = 25
Generation 1
~~~

---

## 五十八、Kernel Event Queue 可能仍带着旧 Event

event loop下一次拿到：

~~~text
data.ptr = C + generation 0
~~~

但 C 现在已经代表：

~~~text
generation 1
~~~

如果只看 pointer：

~~~text
地址完全一样
~~~

会把旧 fd的 readiness错误送给新连接。

---

## 五十九、这就是 Slot Reuse / ABA-like Hazard

地址：

~~~text
C
~~~

出现两次。

但逻辑对象不是同一代。

---

## 六十、nginx 把 Generation Bit 编进 `epoll_event.data.ptr`

注册事件：

~~~cpp
ee.data.ptr =
    (void *) (
      (uintptr_t)c | ev->instance);
~~~

---

## 六十一、为什么 Pointer 低位可借用

`ngx_connection_t*` 对齐保证：

~~~text
最低 bit = 0
~~~

所以可以塞：

~~~text
instance ∈ {0,1}
~~~

---

## 六十二、Kernel 返回后先拆 Pointer

~~~cpp
instance = (uintptr_t)c & 1;

c =
  (ngx_connection_t*)
  ((uintptr_t)c & ~1);
~~~

---

## 六十三、然后验证

~~~cpp
rev = c->read;

if (c->fd == -1
    || rev->instance != instance)
{
    continue;
}
~~~

---

## 六十四、这两个条件分别防什么

`c->fd == -1`：

~~~text
slot currently free / connection closed
~~~

`rev->instance != instance`：

~~~text
slot地址相同
但 generation 已变
~~~

---

## 六十五、所以 `data.ptr` 不是简单 Callback Context Pointer

它实际编码：

~~~text
connection slot identity
+
logical generation
~~~

---

## 六十六、这是一种 Tiny Tagged Pointer

不用：

~~~text
额外 malloc event wrapper
~~~

也不用：

~~~text
global generation map
~~~

---

## 六十七、为什么 1 Bit 看起来也够

它不是想建立：

~~~text
永不重复的 64-bit generation number
~~~

而是防：

~~~text
close/reuse 与当前 epoll batch残留事件
~~~

---

## 六十八、它依赖一个更窄的时间窗口

事件 stale 检测只需要区分：

~~~text
上一代
vs
当前代
~~~

而不是永久历史。

---

## 六十九、所以 Generation Width 应由 Stale Window 推导

如果系统可能同时保留：

~~~text
多代旧引用
~~~

1 bit就不够。

nginx 当前 event backend 的 stale risk 边界允许：

~~~text
toggle bit
~~~

这个极小方案。

---

## 七十、不要机械复制 1-bit Generation

可迁移的是：

> **可复用 storage address 不能单独作为逻辑对象 identity；必须加入足以覆盖 stale-reference window 的 generation。**

---

## 七十一、Connection Slot 与 FD 也不是同一 Identity

fd 数字本身也会被 OS 快速复用。

所以：

~~~text
fd == 10
~~~

更不能作为：

~~~text
stable connection identity
~~~

---

## 七十二、nginx 同时面对两层 Reuse

~~~text
OS fd number reuse
+
nginx connection slot reuse
~~~

因此需要：

~~~text
event generation validation
~~~

---

## 七十三、这与 ABA 问题非常接近

经典 ABA：

~~~text
A
→ B
→ A
~~~

pointer value 回到原值，

观察者误以为：

~~~text
什么都没变
~~~

nginx：

~~~text
slot C old connection
→ free
→ slot C new connection
~~~

pointer仍是 C，

但：

~~~text
instance toggled
~~~

暴露了 generation change。

---

## 七十四、Connection Slot 的生命周期

可以画成：

~~~text
FREE
   |
   | ngx_get_connection(fd)
   | toggle instance
   v
ASSIGNED
   |
   | initialize protocol state
   v
ACTIVE
   |
   | close
   v
CLOSING
   |
   | ngx_free_connection
   v
FREE
~~~

---

## 七十五、Event Slot 的生命周期与 Connection 同步

每个 connection预绑定：

~~~text
read event slot
write event slot
~~~

不是每个 fd动态创建 event object。

---

## 七十六、这使得 Slot Identity 非常稳定

内存地址：

~~~text
固定
~~~

只有：

~~~text
generation
~~~

变化。

这正适合 tagged-pointer stale check。

---

## 七十七、Preallocation 带来的不仅是性能

也把资源上限变得非常明确：

~~~text
no free slot
→ cannot admit connection
~~~

---

## 七十八、而不是让 malloc 一直增长到 OOM

这是一种 Runtime Admission Boundary。

---

## 七十九、`ngx_get_connection()` Slot 不足时怎么办

源码：

~~~text
worker_connections are not enough
→ return NULL
~~~

accept handler随后：

~~~text
close newly accepted socket
~~~

---

## 八十、也就是说 Kernel 已经 Accept 成功

但 Runtime Admission 可以失败。

要区分：

~~~text
TCP accept success
~~~

和：

~~~text
nginx runtime capacity admission success
~~~

---

## 八十一、这与 Middleware Queue HWM 非常相似

底层：

~~~text
resource exists
~~~

不代表上层：

~~~text
runtime willing/able to own it
~~~

---

## 八十二、Connection Slot 紧张时 nginx 还会主动 Drain Reusable Connection

`ngx_drain_connections()` 从：

~~~text
reusable_connections_queue
~~~

尾部挑 connection。

---

## 八十三、它不是直接 `free(c)`

而是：

~~~cpp
c->close = 1;
c->read->handler(c->read);
~~~

---

## 八十四、为什么通过 Handler 关闭

connection 可能属于：

- HTTP keepalive；
- upstream idle connection；
-其他 protocol module。

真正 close 需要模块自己的：

~~~text
cleanup / timer / pool / state
~~~

---

## 八十五、Runtime 只发“请关闭”信号

~~~text
c->close = 1
~~~

再回到正常事件处理链。

---

## 八十六、这是 Policy 与 Mechanism 分层

Runtime policy：

~~~text
free slot紧张
→牺牲 reusable connection
~~~

Protocol mechanism：

~~~text
read handler真正执行安全关闭
~~~

---

## 八十七、为什么不绕过模块直接回收 Slot

因为：

~~~text
connection object lifecycle
~~~

不仅是：

~~~text
ngx_connection_t free-list membership
~~~

还包括模块状态与 request pool。

---

## 八十八、Accept Handler 后面还创建 Connection Pool

~~~cpp
c->pool =
    ngx_create_pool(
      ls->pool_size,
      ev->log);
~~~

说明：

~~~text
connection slot
~~~

与：

~~~text
connection-scoped memory pool
~~~

是两个资源层。

---

## 八十九、Slot 是固定 Runtime Metadata

Pool 是：

~~~text
该连接生命周期内动态对象
~~~

---

## 九十、Preallocated Control Object + Per-lifetime Arena

这是非常实用的组合：

~~~text
stable control slot
+
dynamic lifetime-local arena
~~~

---

## 九十一、Accept Event 本身也占一个 Connection Slot

worker 初始化 listening socket 时：

~~~cpp
c =
  ngx_get_connection(
    ls[i].fd,
    cycle->log);
~~~

所以 listen fd 也使用：

~~~text
ngx_connection_t
+
read event
~~~

统一 event backend模型。

---

## 九十二、统一 Event Context 的好处

epoll返回：

~~~text
connection pointer
~~~

无论它代表：

- listening fd；
- client socket；
- upstream socket；

都可以沿：

~~~text
c->read / c->write
~~~

统一分发。

---

## 九十三、Accept Event 用 `rev->accept = 1` 标识

所以 epoll ready后如果需要 post：

~~~cpp
queue =
  rev->accept
    ? &ngx_posted_accept_events
    : &ngx_posted_events;
~~~

---

## 九十四、一个 Bit 改变 Runtime Phase

`accept` bit不是业务 metadata。

它决定：

~~~text
ready event进入哪条调度队列
~~~

---

## 九十五、Readiness Detection 与 Handler Execution 是分层的

epoll做：

~~~text
detect readiness
~~~

nginx再决定：

~~~text
immediate handler
or
posted queue
~~~

---

## 九十六、为什么 Posted Event 很重要

如果 kernel event直接无条件 callback：

~~~text
backend
→ arbitrary module handler
~~~

Runtime 很难插入：

- accept mutex release；
- timer phase；
- fairness；
- batch order。

---

## 九十七、Posted Queue 是 Runtime Scheduling Layer

这和：

- libuv pending；
- Seastar task queue；
- libzmq activation；

属于同一大类：

> **OS readiness 只是输入；用户 callback 何时执行仍由 Runtime 自己调度。**

---

## 九十八、EPOLLERR / EPOLLHUP 为什么被 OR 成 IN / OUT

源码：

~~~cpp
if (revents
    & (EPOLLERR|EPOLLHUP))
{
    revents |=
      EPOLLIN|EPOLLOUT;
}
~~~

---

## 九十九、原因不是“错误等于可读可写”

而是：

> **确保至少一个已经 active 的 read/write handler 获得执行机会，进入统一错误处理路径。**

---

## 一百、Backend 不想再发明一套 Error Callback ABI

于是把：

~~~text
error readiness
~~~

映射回：

~~~text
normal active handlers
~~~

---

## 一百零一、这是 Error Normalization

底层 OS event：

~~~text
EPOLLERR/HUP
~~~

被转换成 Runtime已有的：

~~~text
read/write event processing
~~~

---

## 一百零二、Graceful Shutdown 先关闭什么

worker收到：

~~~text
ngx_quit
~~~

第一次进入 graceful shutdown：

~~~cpp
ngx_exiting = 1;
ngx_set_shutdown_timer(cycle);
ngx_close_listening_sockets(cycle);
ngx_close_idle_connections(cycle);
ngx_event_process_posted(...);
~~~

---

## 一百零三、第一原则：先 Stop Admission

~~~text
close listening sockets
~~~

意味着：

~~~text
不再接新连接
~~~

---

## 一百零四、然后 Drain Existing Work

已建立 connection：

~~~text
继续 event loop
~~~

直到：

~~~text
no non-cancelable timers left
~~~

worker才退出。

---

## 一百零五、这就是经典 Retire / Drain / Exit

~~~text
RETIRE
stop new accepts

DRAIN
finish existing connections/timers

EXIT
destroy worker process
~~~

---

## 一百零六、为什么不能先 Kill Event Loop

因为已有：

- keepalive；
- active response；
- timer；
- upstream；
- buffered write；

需要完成。

---

## 一百零七、Master/Worker Reload 也是同样思路

新 worker：

~~~text
开始接新流量
~~~

旧 worker：

~~~text
停止 admission
继续 drain
~~~

---

## 一百零八、因此 Process Replacement 也可以看成 Generation Handover

~~~text
old worker generation
→ retiring

new worker generation
→ admitting
~~~

---

## 一百零九、这和 ROUTER Handover 很像

共同模型：

~~~text
future work
→ new owner

already-admitted work
→ old owner drains
~~~

只是一个发生在：

~~~text
connection routing
~~~

一个发生在：

~~~text
process generation
~~~

---

## 一百一十、Accept Ownership 与 Connection Ownership 必须区分

Accept ownership：

~~~text
谁拿下一个 connection
~~~

Connection ownership：

~~~text
拿到以后谁维护该 connection
~~~

---

## 一百一十一、nginx 的答案

前者可以：

- mutex；
- EPOLLEXCLUSIVE；
- reuseport；

后者几乎总是：

~~~text
accepted-by worker
→ remains worker-local
~~~

---

## 一百一十二、所以跨 Worker Migration 不是默认机制

连接不会每个 request：

~~~text
重新丢到另一个 worker
~~~

这避免：

-共享 connection state；
-cross-process handoff；
-cache cold migration。

---

## 一百一十三、代价是 Load Imbalance 只能在入口缓解

例如：

~~~text
某 worker有很多长连接
~~~

nginx不会简单把已经建立的 fd迁给另一个 worker。

---

## 一百一十四、所以 Accept 分配策略很重要

因为：

~~~text
initial placement
~~~

会影响 connection 全生命周期 locality。

---

## 一百一十五、Reuseport 是更强的 Initial Placement 机制

kernel对新 flow进行：

~~~text
socket selection
~~~

相当于：

~~~text
connection placement
~~~

---

## 一百一十六、这对机器人多设备 Runtime 的迁移

假设：

~~~text
4 个执行线程
4 组 sensor/device channels
~~~

可以选择：

~~~text
shared admission queue + mutex
~~~

也可以：

~~~text
per-thread channel ownership
upstream/kernel/dispatcher分流
~~~

---

## 一百一十七、后者通常更符合 Cache Locality

前提是：

~~~text
workload可被合理 partition
~~~

---

## 一百一十八、Accept Mutex 对应什么通用模式

~~~text
shared ingress
+
one temporary owner
~~~

---

## 一百一十九、EPOLLEXCLUSIVE 对应什么

~~~text
shared ingress
+
kernel/runtime arbitration
~~~

---

## 一百二十、Reuseport 对应什么

~~~text
partitioned ingress
+
per-owner queue
~~~

---

## 一百二十一、三种设计不是简单性能排序

它们的适用性取决于：

-平台支持；
-公平性；
-connection distribution；
-配置；
-观测需求；
-兼容性。

---

## 一百二十二、不要把“无锁”自动当最优

Reuseport也会把：

~~~text
connection distribution policy
~~~

交给 kernel。

这可能与应用层负载指标不完全一致。

---

## 一百二十三、如果 Worker Workload 不等价

例如某些 worker还承担额外任务，

纯 kernel hash分流：

~~~text
未必等于业务最优负载均衡
~~~

---

## 一百二十四、Runtime 仍需选择合适 Ownership Boundary

## 一百二十五、Connection Slot Generation 最值得迁移到对象池

假设机器人控制器有：

~~~text
ControllerSlot[128]
~~~

slot重复用于不同 device session。

如果 async callback 保存：

~~~text
ControllerSlot*
~~~

旧 session完成后 callback晚到，

slot已经给新 session。

---

## 一百二十六、只比较 Pointer 不够

正确做法类似：

~~~text
slot pointer
+
generation
~~~

callback返回时：

~~~text
if generation != current
    drop stale callback
~~~

---

## 一百二十七、这适用于

- timer callback；
-GPU completion；
-DMA completion；
-network async result；
-thread-pool result；
-camera frame processing；
-device reconnect。

---

## 一百二十八、任何 Reusable Slot 都应问 ABA-like 问题

> **旧异步事件晚到时，地址是否可能已经代表另一个逻辑对象？**

如果答案是：

~~~text
yes
~~~

就需要：

- generation；
- unique token；
- reference counting；
- quiescence；
-禁止早复用。

---

## 一百二十九、nginx 选择的是 Generation Validation

因为：

~~~text
事件后端可能留下 stale readiness
~~~

但又希望：

~~~text
connection slot快速复用
~~~

---

## 一百三十、Connection Object 并不是 OS FD 的 Owner Type

它更像：

~~~text
runtime slot
~~~

在一个 generation中绑定：

~~~text
fd
+
read event
+
write event
+
module state
~~~

---

## 一百三十一、Slot Reuse 把 Memory Allocation 与 Logical Identity 分离

内存对象地址：

~~~text
长期存在
~~~

逻辑 connection：

~~~text
反复创建/销毁
~~~

---

## 一百三十二、这也是 Object Pool 的根本语义

Pool不是：

~~~text
对象永远不死
~~~

而是：

~~~text
storage不死
logical object反复重建
~~~

---

## 一百三十三、所以 Pool 必须特别重视 Generation

普通 heap allocation：

~~~text
地址也可能复用
~~~

但 pool：

~~~text
地址复用频率更高
~~~

stale-reference风险更明显。

---

## 一百三十四、`ngx_connection_t` Free List 也体现了 Intrusive Design

Free时：

~~~text
c->data
~~~

不再是 module context，

而是：

~~~text
next free slot
~~~

---

## 一百三十五、字段语义随 Lifecycle State 改变

ACTIVE：

~~~text
c->data
→ protocol/module state
~~~

FREE：

~~~text
c->data
→ free-list next
~~~

---

## 一百三十六、这是 Union-like Lifecycle Reuse

没有显式 C union，

但运行时协议保证：

~~~text
同一时刻只有一种语义有效
~~~

---

## 一百三十七、这类代码必须有强状态边界

否则：

~~~text
业务还认为 slot active
但 runtime 已把 data 改成 next pointer
~~~

会立刻内存错误。

---

## 一百三十八、为什么 Preallocation 特别适合 nginx

connection object：

-大小固定；
-数量有明确上限；
-高频创建/销毁；
-生命周期与 fd强绑定。

所以非常适合：

~~~text
preallocated arrays + free list
~~~

---

## 一百三十九、但 Request Body 等可变数据不适合固定 Slot

所以 nginx又使用：

~~~text
pool / buffers
~~~

不同资源选不同 allocator模型。

---

## 一百四十、不要把“一种 Allocation Strategy”用遍 Runtime

常见合理组合：

~~~text
control object
→ fixed pool

request-local objects
→ arena

shared state
→ slab

large buffer
→ separate allocator
~~~

---

## 一百四十一、Accept Admission 的完整状态链

~~~text
listen readiness
        ↓
accept ownership allowed?
        |
        +-- mutex?
        +-- exclusive?
        +-- reuseport?
        ↓
accept4()
        ↓
new fd
        ↓
runtime slot pressure?
        ↓
ngx_get_connection()
        |
        +-- maybe drain reusable
        ↓
slot available?
        |
       no
        ↓
close fd
        |
       yes
        ↓
toggle event generation
        ↓
create connection pool
        ↓
initialize protocol state
        ↓
ls->handler(c)
~~~

---

## 一百四十二、这比“accept() 后创建对象”多了很多 Runtime Policy

## 一百四十三、Accept 失败类别也对应不同处理

`EAGAIN`：

~~~text
queue temporarily empty
→ stop this accept loop
~~~

`ECONNABORTED`：

~~~text
client connection died
→ may continue accepting
~~~

`EMFILE/ENFILE`：

~~~text
local/system fd resource exhausted
→ disable admission / backoff
~~~

---

## 一百四十四、Error Class 决定 Scheduler Action

这和前面：

~~~text
EAGAIN vs EHOSTUNREACH
~~~

一样。

成熟 Runtime不是：

~~~text
所有 error log然后 return
~~~

而是：

~~~text
error semantic
→ recovery policy
~~~

---

## 一百四十五、`multi_accept` 又控制什么

如果 event config允许：

~~~text
accept many
~~~

一个 readiness callback可以循环：

~~~text
accept until EAGAIN
~~~

---

## 一百四十六、这也是 Batch vs Fairness Tradeoff

一次多 accept：

~~~text
提高 accept throughput
~~~

但可能：

~~~text
单个 listening event占用更长 CPU slice
~~~

---

## 一百四十七、Runtime 常见三种 Batch

这里已经看到：

- one readiness → many accepts；
- one epoll_wait → many ready events；
- posted queue → batch callbacks。

---

## 一百四十八、Batching 减少 syscall / scheduling overhead

但会增加：

~~~text
单轮 execution burst
~~~

---

## 一百四十九、对实时机器人 Runtime 要重新设 Budget

网络服务器倾向吞吐。

1 kHz控制 loop可能更需要：

~~~text
bounded work per iteration
~~~

---

## 一百五十、`ngx_accept_disabled` 是另一种轻量 Fairness 调整

worker slot越紧张：

~~~text
少抢 accept
~~~

从而让其他 worker更容易获取新连接。

---

## 一百五十一、它不是精确 Global Load Balancer

因为每个 worker只知道自己的：

~~~text
free_connection_n
~~~

---

## 一百五十二、这是一种 Decentralized Feedback

没有 central scheduler维护：

~~~text
worker load table
~~~

每个 worker根据本地压力调整：

~~~text
admission aggressiveness
~~~

---

## 一百五十三、这很好地符合 shared-nothing 架构

## 一百五十四、但 Accept Mutex 才需要 Shared Memory Lock

跨进程共享：

~~~text
ngx_accept_mutex
~~~

是刻意保留的少量全局协调点。

---

## 一百五十五、为什么它值得被单独命名

因为这种共享点应该：

~~~text
数量少
语义窄
容易推理
~~~

---

## 一百五十六、不是“多进程就完全无共享”

而是：

> **把共享控制在极少数边界上。**

---

## 一百五十七、EPOLLEXCLUSIVE 进一步把这个共享协调点下推 Kernel

从用户态代码角度：

~~~text
更少 cross-worker lock protocol
~~~

---

## 一百五十八、Reuseport 再把 Shared Socket 本身拆掉

三者展示了一个很漂亮的架构演进：

~~~text
shared resource + userspace lock
        ↓
shared resource + kernel arbitration
        ↓
partitioned resources
~~~

---

## 一百五十九、这条演进可以用于很多系统

例如：

~~~text
one global work queue
→ one queue + futex arbitration
→ per-core queues + work distribution
~~~

---

## 一百六十、但 Partition 之后要面对 Rebalancing

资源局部化后：

~~~text
负载倾斜
~~~

变成新的问题。

---

## 一百六十一、所以 Ownership Partition 与 Dynamic Balance 是一对 Tradeoff

## 一百六十二、nginx Worker 模型为什么仍然成功

典型 Web workload：

-连接相对独立；
-request state相对局部；
-共享 cache可以另做 shared memory；
-新连接入口可由 kernel分发。

非常适合：

~~~text
connection ownership partition
~~~

---

## 一百六十三、但某些强共享应用可能不适合照搬

例如：

~~~text
所有请求都更新同一超热点 mutable structure
~~~

多进程 worker会导致：

- IPC；
-shared memory synchronization；
-cache coherence；

成本上升。

---

## 一百六十四、架构必须从 State Topology 出发

不是从：

~~~text
nginx 很快，所以我要 master/worker
~~~

出发。

---

## 一百六十五、这一章与下一章 Connection Pool 的边界

这里重点是：

~~~text
new connection
如何进入 worker
+
slot generation如何保证 OS event安全
~~~

下一章 `connection-pool-lifecycle.md` 会继续深入：

- reusable queue；
- drain policy；
- close ordering；
- timer/post event 清理；
- slot归还；
- stale reference 生命周期。

---

## 一百六十六、最终把整个 Worker Accept 模型压缩成四层

第一层：

~~~text
PROCESS OWNERSHIP
master / workers
~~~

第二层：

~~~text
ADMISSION OWNERSHIP
accept mutex / EPOLLEXCLUSIVE / reuseport
~~~

第三层：

~~~text
RESOURCE OWNERSHIP
worker-local connection slots
~~~

第四层：

~~~text
LOGICAL IDENTITY
slot pointer + instance generation
~~~

---

## 一百六十七、四层分别解决什么

Process：

~~~text
谁执行 connection lifecycle
~~~

Admission：

~~~text
谁拿下一条新连接
~~~

Resource：

~~~text
是否还有 runtime capacity
~~~

Generation：

~~~text
这个地址现在还是原来的逻辑 connection 吗
~~~

---

## 一百六十八、任何高性能 Runtime 都可以问同样四个问题

## 一百六十九、源码作者必须守住的核心不变量

第一：

> **建立连接后，connection mutable state基本由一个 worker event loop拥有；跨 worker共享应尽量停留在 admission/shared-memory边界。**

第二：

> **当前固定版本 `accept_mutex` 默认关闭；启用需要显式配置和多 worker 条件。Linux epoll 多 worker路径会使用 `EPOLLEXCLUSIVE` 把共享 listen socket 的唤醒仲裁交给 kernel。**

第三：

> **`SO_REUSEPORT` 不是另一种 mutex，而是把 listening socket拆成 per-worker资源，从 topology上减少共享。**

第四：

> **Accept ownership 的关键不是保护 `accept()` 函数本身，而是决定哪个 worker当前具有 listening readiness admission资格。**

第五：

> **`ngx_get_connection()` 消耗的是固定 worker-local connection slot；TCP accept 成功与 Runtime capacity admission 成功是两个阶段。**

第六：

> **connection storage地址会反复复用，因此 epoll registration必须把 pointer和 generation绑定，不能只靠 pointer识别逻辑对象。**

第七：

> **`instance` 每次 slot分配时翻转，epoll返回后先验证 `fd != -1` 且 generation匹配，旧 generation ready event直接丢弃。**

第八：

> **free-list字段复用只在 FREE state合法；同一个 `c->data` 在 active和free状态拥有不同语义，生命周期边界必须明确。**

第九：

> **资源紧张时 nginx优先通过已有 protocol handler回收 reusable connection，而不是绕过模块强行回收 slot。**

第十：

> **graceful shutdown 必须先关闭 listening admission，再 drain已有 connection/timer，最后退出 worker。**

第十一：

> **OS readiness 与 callback execution不是同一层；posted event queue让 Runtime保留自己的 phase ordering。**

第十二：

> **可复用对象池必须分析 stale asynchronous event / ABA-like hazard，并给 storage address增加足够的 generation identity。**

---

## 一百七十、最终心智模型

~~~text
                       MASTER
                         |
              fork / signal / reload
                         |
          +--------------+--------------+
          |                             |
       Worker A                      Worker B
          |                             |
     local epoll                    local epoll
          |                             |
          +---------- listen -----------+
                     ownership
                /       |        \
          mutex    EPOLLEXCLUSIVE  reuseport
                     |
                     v
                  accept4
                     |
                     v
             runtime admission
                     |
              free connection?
                 /       \
               no         yes
               |           |
           close fd     pop slot
                           |
                     toggle instance
                           |
                     init connection
                           |
                     epoll data.ptr
                  = pointer + generation
                           |
                     later ready event
                           |
                 generation still same?
                   /             \
                 no               yes
                 |                 |
             stale drop         handler
~~~

如果只记一个结论：

> **nginx 的 worker/epoll 性能并不只是来自“事件驱动”，而是来自一整套 ownership 分层：master/worker切开进程状态，accept mutex/EPOLLEXCLUSIVE/reuseport决定入口归属，worker-local固定 connection slot限制资源容量，而 `pointer + instance generation` 又让高频 slot复用不会被旧 epoll ready event误伤。真正值得学习的是“先设计 owner、admission和logical identity，再选择 event API”。**
