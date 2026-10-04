# Timer Rbtree：Deadline Ordering、Lazy Update、Wrap-around 与 Shutdown Liveness

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 的 Timer 机制很容易被压缩成一句：

~~~text
把 timeout 放进红黑树，
取最小 deadline 作为 epoll_wait timeout。
~~~

但如果只看到这里，真正值得学习的部分几乎都被漏掉了。

固定源码中的 Timer Runtime 同时在解决：

- 大量连接事件的 deadline 排序；
- 已知 Timer 的频繁删除与重设；
- 同一毫秒重复 deadline；
- 32-bit 毫秒时钟约 49 天回绕；
- 高频连接活动导致的 tree update churn；
- Timer 到期时 scheduler state 与 callback state 的切换；
- connection/event storage 释放前的 Timer membership 清理；
- graceful shutdown 时“哪些 Timer 还必须阻止 worker 退出”。

所以更准确的心智模型应该是：

> **nginx Timer 不是一棵“存时间的树”，而是 worker-local event ownership 的一个 deadline index。`ngx_event_t` 本身内嵌 tree node，deadline 可以有意保留 300 ms 内的旧值，时间比较使用 signed-delta 处理 wrap-around，而 Timer membership、`timer_set/timedout/cancelable` 又直接参与 callback、connection close 和 worker quiescence。**

Worker 主循环、epoll readiness 与 graceful admission 已在 [Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation](worker-epoll-accept.md) 中展开。这里继续看同一个 worker execution domain 内，Timer 怎样成为另一条 scheduler input。

## 一、Timer 并不是独立线程

nginx 没有：

~~~text
network event loop
+
timer thread
~~~

worker 主循环先调用：

~~~text
ngx_event_find_timer()
~~~

得到最近 deadline 距离当前时间还有多久，再把它作为：

~~~text
ngx_process_events(cycle, timer, flags)
~~~

的 wait timeout。

epoll backend 最终得到：

~~~text
epoll_wait(..., timer)
~~~

所以 Timer 与 I/O 共用一个 sleep point：

~~~text
nearest timer deadline
        \
         +--> worker sleep until first thing happens
        /
I/O readiness
~~~

这让：

- connection state；
- read/write callback；
- Timer tree；
- timeout callback；

都继续保持 worker-local owner 模型。

## 二、为什么这个 Owner 模型很重要

如果 Timer 由另一线程管理，连接事件可能出现：

~~~text
worker thread:
  closing connection

timer thread:
  expiring read timeout

both touch same ngx_event_t / ngx_connection_t
~~~

那就需要：

- 锁；
-引用计数；
- callback quiescence；
- cross-thread timer cancellation。

nginx 的主要路径把 Timer tree 放在 worker event loop中，因此：

~~~text
connection mutation
timer insertion/deletion
timer expiration
handler execution
~~~

基本都在同一个 owner context 串行推进。

这不是“完全没有并发”。

而是：

> **先用进程/worker ownership 消除最热对象上的细粒度并发。**

## 三、全局名字不等于跨 Worker 全局共享

源码：

~~~c
ngx_rbtree_t
    ngx_event_timer_rbtree;

static ngx_rbtree_node_t
    ngx_event_timer_sentinel;
~~~

变量从 C 符号层面看是 global。

但 worker 是：

~~~text
separate process
~~~

fork 后每个 worker都有自己的地址空间副本。

因此更准确：

~~~text
one timer rbtree per worker process
~~~

而不是：

~~~text
all workers share one timer tree
~~~

这是读 nginx 源码时经常需要补上的“进程语义”。

## 四、`ngx_event_t` 本身就是 Timer Node Owner

`ngx_event_s` 直接内嵌：

~~~c
ngx_rbtree_node_t timer;
~~~

还同时内嵌：

~~~c
unsigned timedout:1;
unsigned timer_set:1;
unsigned cancelable:1;
~~~

也就是说 Timer 并不是：

~~~text
Timer*
  → Event*
~~~

的独立 heap object。

而是：

~~~text
ngx_event_t
    |
    +-- scheduler state
    +-- handler
    +-- timer flags
    +-- embedded rbtree node
~~~

## 五、这是典型 Intrusive Container

普通容器：

~~~text
tree node
    |
    +-- pointer to event
~~~

intrusive 结构则是：

~~~text
event
    |
    +-- tree node is part of event storage
~~~

优点：

- 无额外 Timer node allocation；
- 删除时已知 node 地址；
- node 与业务 event 生命周期天然相关；
- 更少 pointer indirection；
- 更适合高频 add/delete。

代价：

- 一个 node 同时只能安全属于预定 container membership；
- owner object回收前必须先退出 tree；
- container invariant 会进入 object lifecycle。

## 六、从 Node 怎么找回 Event

Timer 到期时拿到的是：

~~~text
ngx_rbtree_node_t*
~~~

源码使用：

~~~c
ev = ngx_rbtree_data(
        node,
        ngx_event_t,
        timer);
~~~

这本质上是：

~~~text
container_of(node,
             ngx_event_t,
             timer)
~~~

通过成员 offset 反推外层对象地址。

所以 Tree 保存的不是：

~~~text
opaque callback object
~~~

而是直接嵌入：

~~~text
event storage
~~~

的 intrusive scheduler node。

## 七、Timer Membership 因而是 Storage Reachability

只要：

~~~text
ev->timer
~~~

仍然挂在：

~~~text
ngx_event_timer_rbtree
~~~

未来：

~~~text
ngx_event_expire_timers()
~~~

就可能通过 node 重新得到：

~~~text
ngx_event_t*
~~~

因此：

> **Tree membership 就是一条未来可达引用。**

这对 connection pool 尤其重要。

## 八、Connection Close 为什么先删 Timer

`ngx_close_connection()` 中明确：

~~~c
if (c->read->timer_set) {
    ngx_del_timer(c->read);
}

if (c->write->timer_set) {
    ngx_del_timer(c->write);
}
~~~

之后才继续：

-从 event backend 删除 fd；
-归还 connection slot；
-close socket。

这不是 housekeeping。

它是在切断：

~~~text
timer tree
→ embedded event node
→ connection generation storage
~~~

这条未来 reachability。

## 九、如果反过来会怎样

错误顺序：

~~~text
connection slot returned to free list
        ↓
same slot reused
        ↓
old event timer still in tree
        ↓
deadline expires
        ↓
container_of(old node)
        ↓
callback runs against new generation
~~~

这会比普通 stale pointer 更危险，因为：

~~~text
storage地址仍合法
~~~

但逻辑对象已经换代。

所以前一章的：

~~~text
epoll instance generation
~~~

与这里的：

~~~text
timer membership removal
~~~

解决的是两条不同 stale-event 来源。

## 十、OS Ready Event 与 User-space Timer 的 Stale 防线不同

epoll ready list：

~~~text
nginx无法直接删除所有已由 kernel排队的旧 ready item
~~~

所以使用：

~~~text
pointer + instance generation
~~~

返回时验证。

Timer tree：

~~~text
完全由 worker owner控制
~~~

所以 close 时可以：

~~~text
直接删除 intrusive node
~~~

两种 stale source 应采用不同策略。

## 十一、Timer Tree 初始化

源码：

~~~c
ngx_rbtree_init(
    &ngx_event_timer_rbtree,
    &ngx_event_timer_sentinel,
    ngx_rbtree_insert_timer_value);
~~~

注意第三个参数：

~~~text
ngx_rbtree_insert_timer_value
~~~

而不是通用：

~~~text
ngx_rbtree_insert_value
~~~

nginx 为 Timer 提供了专门的 key comparison。

## 十二、为什么不能只用普通 Unsigned `<`

Timer key 本质：

~~~text
absolute millisecond timestamp
~~~

如果：

~~~text
ngx_msec_t
~~~

使用 32-bit，

大约每：

~~~text
2^32 ms
≈ 49.7 days
~~~

会回绕。

例如接近回绕时：

~~~text
old now      = 0xFFFFFFF0
future timer = 0x00000020
~~~

普通 unsigned 比较会说：

~~~text
0x00000020 < 0xFFFFFFF0
~~~

看起来 future timer反而“更小”。

## 十三、nginx 使用 Signed Difference

Timer 插入：

~~~c
p = ((ngx_rbtree_key_int_t)
        (node->key - temp->key) < 0)
      ? &temp->left
      : &temp->right;
~~~

nearest timer：

~~~c
timer =
  (ngx_msec_int_t)
  (node->key - ngx_current_msec);
~~~

expire 判断：

~~~c
if ((ngx_msec_int_t)
      (node->key - ngx_current_msec) > 0)
{
    return;
}
~~~

核心不是：

~~~text
比较绝对 unsigned timestamp
~~~

而是：

~~~text
比较 modular difference
再解释为 signed interval
~~~

## 十四、为什么 Signed-delta 能处理 Wrap

只要两个相关时间点的距离没有大到超过 signed range，

例如 32-bit：

~~~text
|deadline - now| < 2^31 ms
≈ 24.8 days
~~~

那么：

~~~text
(deadline - now) interpreted as signed
~~~

仍能表达：

- 正数 → deadline在未来；
- 0/负数 → deadline已到期或过去。

这类技巧常见于：

- TCP sequence number；
- embedded tick counter；
- ring generation；
- monotonic timer wheel。

## 十五、Wrap-safe 比较依赖 Horizon Contract

不能把它理解成：

~~~text
任意两个 32-bit timestamp 都可正确排序
~~~

它成立的前提是：

~~~text
相关 deadline彼此不会跨越半个计数空间
~~~

nginx源码注释也强调：

~~~text
timer values
usually spread in a small range,
usually several minutes
~~~

所以 signed-delta 是基于真实 workload horizon 的协议。

## 十六、这是一个很重要的数据类型设计原则

遇到环形计数器时，不要问：

~~~text
哪个 unsigned 数字更大？
~~~

而要问：

> **在允许的最大时间/序号距离内，A 相对 B 是向前还是向后？**

## 十七、为什么 Timer Tree 允许 Duplicate Keys

源码明确注释：

~~~text
the event timer rbtree
may contain duplicate keys
~~~

因为：

~~~text
同一毫秒
~~~

完全可能有很多：

- keepalive timeout；
- read timeout；
- upstream timeout。

Timer scheduler 不要求：

~~~text
deadline key globally unique
~~~

## 十八、Duplicate Deadline 的正确语义

如果：

~~~text
A deadline = 1000
B deadline = 1000
C deadline = 1000
~~~

Runtime 只需要保证：

~~~text
deadline <= now
的所有 event最终被 expire
~~~

不要求：

~~~text
A/B/C之间有业务上的唯一排序
~~~

## 十九、Comparator 如何放 Duplicate

Timer insert comparator：

~~~text
signed difference < 0
→ left

otherwise
→ right
~~~

所以相等：

~~~text
go right
~~~

这给树提供确定插入路径，

但不赋予 duplicate业务顺序语义。

## 二十、最近 Deadline 怎样找到

`ngx_event_find_timer()`：

~~~c
if (root == sentinel) {
    return NGX_TIMER_INFINITE;
}

node =
    ngx_rbtree_min(root, sentinel);

timer =
    (ngx_msec_int_t)
    (node->key - ngx_current_msec);

return timer > 0
       ? timer
       : 0;
~~~

## 二十一、Empty Tree 的语义

没有 Timer：

~~~text
NGX_TIMER_INFINITE
~~~

event backend可以：

~~~text
无限等待 I/O
~~~

除非还有：

- accept mutex delay；
- posted-next；
- timer resolution；
- signal；

改变 sleep policy。

## 二十二、Nearest Deadline 已过期时为什么返回 0

如果：

~~~text
node->key <= now
~~~

则：

~~~text
wait timeout = 0
~~~

意味着：

~~~text
不要再睡
马上完成 event-loop iteration
然后 expire timers
~~~

## 二十三、为什么不直接在 `find_timer()` 执行 Callback

因为：

~~~text
compute sleep deadline
~~~

与：

~~~text
perform scheduler mutation + callback
~~~

是不同 phase。

Worker loop保持明确顺序：

~~~text
find timer
→ wait/process I/O
→ process posted accepts
→ expire timers
→ process posted events
~~~

## 二十四、Phase Order 让 Runtime 行为可推理

如果 nearest timer 查询顺手执行 callback，

一个看似：

~~~text
pure-ish query
~~~

会突然：

- close connection；
- post events；
- add/delete timer；
- mutate protocol state。

nginx避免这种隐藏重入。

## 二十五、Min 查询不是 Heap Root O(1)

红黑树最小节点通过：

~~~c
while (node->left != sentinel) {
    node = node->left;
}
~~~

所以是：

~~~text
O(tree height)
≈ O(log N)
~~~

nginx没有额外缓存：

~~~text
current minimum pointer
~~~

## 二十六、为什么仍然合理

Timer 操作模式不只有：

~~~text
find min
~~~

还包括大量：

- delete known timer；
- reschedule known timer；
- ordered traversal；
- duplicate deadline；
- graceful-exit global property scan。

所以数据结构选择不能只优化一个 operation。

## 二十七、Rbtree 与 Heap 的真正比较维度

只写：

~~~text
heap O(logN)
tree O(logN)
~~~

几乎没信息。

应展开：

| 操作 | Heap | Intrusive Rbtree |
|---|---|---|
| min | root O(1) | leftmost O(log N) |
| insert | O(log N) | O(log N) |
| delete known node | 需维护 heap index | 已有 node，O(log N) |
| ordered traversal | 不自然 | 自然 |
| intrusive membership | 可实现 | 很自然 |
| duplicate key | 支持 | 支持 |
| wrap-aware comparator | 需定制 | 当前源码已定制 |
| shutdown全局扫描 | 不自然 | `next()` 自然 |

nginx真实 workload让 rbtree 成为合理选择。

## 二十八、`ngx_event_t` 内嵌 Node 让 Delete 特别自然

删除：

~~~c
ngx_rbtree_delete(
    &ngx_event_timer_rbtree,
    &ev->timer);
~~~

不用：

~~~text
search tree by deadline
~~~

也不用：

~~~text
deadline → timer object map
~~~

因为 membership handle：

~~~text
就是 ev->timer 地址
~~~

## 二十九、这是一种 Intrusive Handle

业务 event 自己携带：

~~~text
scheduler removal handle
~~~

这种模式很适合：

- timers；
- ready queues；
- LRU；
- intrusive lists；
- reactor registries。

## 三十、代价：对象不能随便 Move

如果一个对象：

~~~text
node地址
~~~

已经被 tree保存，

把整个 object memcpy/move 到另一个地址会破坏：

- parent pointer；
- child pointer；
-外部 container node address。

所以 intrusive membership 通常要求：

~~~text
storage address stable
until removed
~~~

## 三十一、nginx Connection/Event Preallocation 正好满足这个要求

`read_events[]` / `write_events[]`：

~~~text
地址稳定
~~~

logical generation反复复用，

但 storage本身不在 active lifetime中搬家。

这和 timer intrusive node非常契合。

## 三十二、现在进入最有价值的优化：300 ms Lazy Update

头文件：

~~~c
#define NGX_TIMER_LAZY_DELAY 300
~~~

新增/刷新 Timer：

~~~c
key = ngx_current_msec + timer;

if (ev->timer_set) {
    diff =
      (ngx_msec_int_t)
      (key - ev->timer.key);

    if (ngx_abs(diff)
        < NGX_TIMER_LAZY_DELAY)
    {
        return;
    }

    ngx_del_timer(ev);
}

ev->timer.key = key;
ngx_rbtree_insert(...);
ev->timer_set = 1;
~~~

## 三十三、它优化的不是“Timer 到期处理”

而是：

~~~text
Timer Refresh Churn
~~~

网络连接很常见：

~~~text
每次收到新数据
→ read timeout = now + 60s
~~~

如果流量频繁：

~~~text
t=0 ms   deadline=60000
t=20 ms  deadline=60020
t=40 ms  deadline=60040
t=60 ms  deadline=60060
...
~~~

严格 scheduler每次都：

~~~text
delete old node
+
insert new node
~~~

## 三十四、但这些变化对业务意义很小

如果 timeout本身是：

~~~text
60 seconds
~~~

而新旧 deadline只差：

~~~text
20 ms
~~~

通常没有必要为这 20 ms：

- 做 tree delete；
-做 tree rebalance；
-做 tree insert；
-再次 cache-touch node ancestry。

## 三十五、nginx 直接保留旧 Deadline

如果：

~~~text
|new_deadline - old_deadline| < 300ms
~~~

则：

~~~text
do nothing
~~~

注意：

> **不是“更新 key 但不重排”。**

它是真的：

~~~text
继续使用旧 key
~~~

## 三十六、这意味着 Timer 有意不精确

例如旧 deadline：

~~~text
60000 ms
~~~

新 deadline：

~~~text
60250 ms
~~~

diff：

~~~text
250 ms
~~~

小于 300，

所以真正 scheduler 仍将在：

~~~text
60000 ms
~~~

把 Timer视为到期。

## 三十七、Lazy Window 可以让 Timer 稍早，也可以稍晚

diff 是 signed：

~~~text
new - old
~~~

判断：

~~~text
abs(diff) < 300
~~~

因此无论新 deadline：

~~~text
比旧值稍晚
~~~

还是：

~~~text
比旧值稍早
~~~

都可能保留旧 key。

所以误差是：

~~~text
bounded approximation
~~~

而不是只向某一个方向偏。

## 三十八、为什么这种 Approximation 合理

很多 nginx Timer 是：

- idle timeout；
- keepalive；
- read inactivity；
- write inactivity；
- upstream timeout。

它们通常是：

~~~text
failure/idle detection policy
~~~

不是：

~~~text
硬实时 actuator deadline
~~~

所以可以用：

~~~text
<300ms deadline drift
~~~

换：

~~~text
显著减少 tree maintenance
~~~

## 三十九、Approximation 是业务 Contract 的一部分

如果你把同样机制照搬到：

~~~text
1 kHz motor current loop
~~~

300 ms误差当然不可接受。

所以可迁移原则不是：

~~~text
Timer 就应该 lazy 300ms
~~~

而是：

> **先定义允许的 deadline error budget，再用 error budget减少 scheduler维护成本。**

## 四十、机器人系统可以怎么迁移

例如：

~~~text
telemetry heartbeat
timeout = 5s
tolerance = 50ms
~~~

可以 lazy update。

但：

~~~text
safety watchdog
deadline = 20ms
~~~

可能要求：

~~~text
exact/bounded much tighter
~~~

Timer precision应该按语义分类。

## 四十一、这是一种 Approximate Scheduler

数据结构本身仍是精确红黑树。

近似发生在：

~~~text
是否把新 deadline写进结构
~~~

也就是：

~~~text
admission/update policy
~~~

而不是：

~~~text
tree算法
~~~

## 四十二、这点很值得区分

很多“近似数据结构”不一定需要：

~~~text
特殊 approximate container
~~~

也可以是：

~~~text
精确 container
+
近似 update policy
~~~

## 四十三、Timer Refresh 为什么先检查 `timer_set`

如果 event还没有 Timer：

~~~text
没有旧 deadline可复用
~~~

必须真正：

~~~text
insert
~~~

只有：

~~~text
already scheduled
~~~

才有 lazy comparison意义。

## 四十四、`timer_set` 是 Membership Bit

它的语义不是：

~~~text
这个 event 曾经设置过 Timer
~~~

而更接近：

> **当前 `ev->timer` 是否属于 timer rbtree。**

所以它是：

~~~text
container membership state
~~~

## 四十五、`ngx_event_del_timer()` 的顺序

~~~c
ngx_rbtree_delete(...);

#if DEBUG
  clear node links
#endif

ev->timer_set = 0;
~~~

删除后 membership bit才清零。

## 四十六、为什么 Debug 模式清 Node Links

删除后：

~~~text
left/right/parent = NULL
~~~

可以让：

-重复删除；
-stale traversal；
-use-after-delete；

更容易在 debug中暴露，

而不是继续残留看似合法的树关系。

## 四十七、Timer 到期的 Expire Loop

核心：

~~~text
loop:
  if tree empty
      return

  node = minimum

  if node deadline still future
      return

  recover event

  delete node from tree
  timer_set = 0
  timedout = 1

  handler(event)
~~~

## 四十八、为什么只看 Min 就能停止

红黑树保持 deadline order。

如果最小 deadline：

~~~text
> now
~~~

那么其余节点：

~~~text
>= min
> now
~~~

都不可能到期。

因此：

~~~text
one min check
~~~

就能结束当前 expiration pass。

## 四十九、一次 Expire 可以执行多个 Callback

如果：

~~~text
10 个 Timer都 <= now
~~~

loop 会连续：

~~~text
delete
→ callback
→ next minimum
~~~

直到：

- tree空；
-最小 deadline转为未来。

这是一种：

~~~text
due-timer batch
~~~

## 五十、Batch 的 Tail-latency Tradeoff

如果大量 Timer 同一毫秒到期，

一个 event-loop iteration可能执行：

~~~text
many handlers
~~~

吞吐高，

但单轮 CPU burst变长。

nginx当前源码没有在这个函数中设置：

~~~text
max_expire_callbacks_per_round
~~~

## 五十一、这对硬实时 Runtime 不一定适合

控制系统若要求：

~~~text
bounded loop execution time
~~~

可考虑：

- 每轮最多 expire N个；
-按 priority分类；
- timer wheel bucket budget；
- separate soft timer executor。

但这是可迁移设计建议，

不是 nginx 当前源码事实。

## 五十二、Expire Callback 前为什么先删 Tree Membership

源码顺序：

~~~text
tree delete
timer_set = 0
timedout = 1
handler()
~~~

不是：

~~~text
handler()
then delete
~~~

## 五十三、第一原因：防 Callback 重入 Scheduler 时看到旧 Membership

Timer handler可能：

~~~text
重新 add timer
~~~

如果旧 node仍在 tree，

callback调用：

~~~text
ngx_add_timer(ev,...)
~~~

会认为：

~~~text
timer_set == true
~~~

然后可能：

- lazy return；
- delete old timer；
-重复操作当前正在 expire 的 node。

先摘除以后：

~~~text
callback看到一个干净 unscheduled event
~~~

## 五十四、第二原因：Callback 可以关闭 Owner

handler可能：

~~~text
close connection
~~~

甚至让：

~~~text
event storage回到 pool
~~~

如果 tree仍然拥有 embedded node，

callback返回前就可能形成 dangling tree membership。

## 五十五、所以这是经典 Rule

> **在进入任意用户/协议 callback 前，先把 scheduler 自己的 ownership/membership state恢复到一致状态。**

这和：

- libzmq先更新容量 truth再 activation；
- callback registry先建立 snapshot；
- queue pop先转移 ownership；

是同一类系统规则。

## 五十六、`timedout = 1` 为什么也在 Handler 前

handler需要知道：

~~~text
我是因为 timeout被调用
~~~

所以 callback execution context 应先完整建立：

~~~text
timer_set = false
timedout = true
~~~

再进入 handler。

## 五十七、这叫 State-before-Notify

~~~text
authoritative state transition
        ↓
invoke callback
~~~

而不是：

~~~text
callback
        ↓
希望 callback自己猜状态
~~~

## 五十八、`timedout` 与 `timer_set` 是两个不同维度

可能：

~~~text
timer_set = 0
timedout = 0
~~~

表示：

~~~text
没有 active timer，也不是 timeout触发
~~~

也可能：

~~~text
timer_set = 0
timedout = 1
~~~

表示：

~~~text
Timer刚到期，callback正在/已经处理 timeout
~~~

所以不能把：

~~~text
timer_set=false
~~~

等同于：

~~~text
never timed out
~~~

## 五十九、Timer Callback 可以重新 Arm

handler里可能：

~~~text
clear/interpret timedout
perform protocol transition
ngx_add_timer(ev,new_timeout)
~~~

新的 Timer是：

~~~text
new scheduler membership
~~~

而不是延续旧 node state。

## 六十、Timer Event 与 I/O Event 共用同一个 Handler ABI

`ngx_event_t`：

~~~c
ngx_event_handler_pt handler;
~~~

无论 callback来源：

- read readiness；
- write readiness；
- timeout；

最终都可以进入：

~~~text
event handler
~~~

区别由 event flags表达：

~~~text
ready
timedout
eof
error
...
~~~

## 六十一、这减少 Backend-specific Callback 类型

上层模块写：

~~~text
one event state machine
~~~

根据：

~~~text
event flags
~~~

处理：

- I/O；
- timeout；
- close。

这让协议状态机统一。

## 六十二、Timer 为什么不是单独 `std::function`

nginx C runtime强调：

~~~text
stable event object
+
function pointer handler
~~~

而不是每次 Timer add都：

~~~text
allocate closure
~~~

这与固定 slot / intrusive timer node设计一致。

## 六十三、Timer Node Key 是 Absolute Deadline

新增：

~~~c
key = ngx_current_msec + timer;
~~~

这里 `timer` 是：

~~~text
relative delay
~~~

tree中存：

~~~text
absolute tick deadline
~~~

## 六十四、为什么 Absolute Deadline 方便

如果所有 node存：

~~~text
relative delta
~~~

每次时间推进都要更新大量节点。

absolute key使：

~~~text
time progress
~~~

只体现在：

~~~text
ngx_current_msec
~~~

变化，

tree node key无需每 tick修改。

## 六十五、这是 Event Scheduler 常见模式

~~~text
store absolute deadline
compare to monotonically advancing now
~~~

而不是：

~~~text
decrement every timer every millisecond
~~~

## 六十六、`ngx_current_msec` 为什么是 Runtime Time Cache

event loop统一更新时间，

模块大量使用：

~~~text
cached current millisecond
~~~

减少频繁系统时钟调用。

这和 Timer tree一起构成：

~~~text
cached clock
+
absolute deadlines
~~~

的 runtime设计。

## 六十七、Timer 插入 Comparator 为什么不是普通 Rbtree Comparator

通用：

~~~c
node->key < temp->key
~~~

Timer：

~~~c
(ngx_rbtree_key_int_t)
(node->key - temp->key) < 0
~~~

专门解决：

~~~text
millisecond wrap
~~~

因此：

> **同一种 container algorithm 可以通过 domain-specific comparator获得完全不同的正确性语义。**

## 六十八、数据结构模板化不一定要 C++ Template

nginx C代码通过：

~~~text
tree->insert callback
~~~

把：

~~~text
tree balancing
~~~

与：

~~~text
key ordering policy
~~~

分离。

这本质上就是：

~~~text
strategy function pointer
~~~

## 六十九、Rbtree Node 为什么存 Key 而不是让 Comparator访问 Event

因为：

~~~text
tree core
~~~

只需要知道：

- key；
-parent/left/right；
-color。

它不需要依赖：

~~~text
ngx_event_t
~~~

业务对象类型。

这使同一 rbtree可用于别的索引。

## 七十、Intrusive Generic Container 的典型结构

~~~text
generic node embedded in domain object
        +
generic balancing algorithm
        +
domain-specific ordering callback
~~~

这是非常值得学习的 C Runtime 设计。

## 七十一、现在看 `ngx_event_no_timers_left()`

graceful worker退出前：

~~~c
if (ngx_exiting) {
    if (ngx_event_no_timers_left()
        == NGX_OK)
    {
        ngx_worker_process_exit(cycle);
    }
}
~~~

所以 Timer tree还承担：

~~~text
worker liveness accounting
~~~

## 七十二、函数不是只看 Tree Empty

源码：

~~~text
if tree empty
→ OK

else traverse ordered tree:
    if any !ev->cancelable
        → NGX_AGAIN

if only cancelable timers remain
→ OK
~~~

## 七十三、Cancelable Timer 的真正语义

它不是：

~~~text
这个 Timer会自动 cancel
~~~

而是：

> **这个 Timer 不应该单独阻止 graceful worker退出。**

这是一种：

~~~text
liveness classification
~~~

## 七十四、Non-cancelable Timer 相当于 Outstanding Work Hold

只要存在：

~~~text
!cancelable
~~~

worker就认为：

~~~text
仍有必须等待的 asynchronous obligation
~~~

所以：

~~~text
timer membership
+
cancelable bit
~~~

共同决定：

~~~text
process quiescence
~~~

## 七十五、Timer 因而不只是 Deadline Scheduler

它同时成为：

~~~text
shutdown liveness registry
~~~

这比“红黑树管理超时”深一层。

## 七十六、为什么需要遍历整棵 Tree

最近 Timer：

~~~text
可能是 cancelable
~~~

但树深处仍可能存在：

~~~text
non-cancelable timer
~~~

所以不能只看：

~~~text
minimum
~~~

必须检查全局属性：

~~~text
exists non-cancelable?
~~~

## 七十七、这正是 Rbtree Ordered Traversal 的额外用途

源码：

~~~c
for (node = ngx_rbtree_min(...);
     node;
     node = ngx_rbtree_next(...))
{
    ...
}
~~~

Heap 对：

~~~text
“是否有任意 node满足 predicate”
~~~

没有排序优势，

通常仍需扫描底层数组。

rbtree的 next traversal则是自然结构。

## 七十八、但这里复杂度仍是 O(N)

不要因为用了 tree就误以为：

~~~text
no_timers_left = O(logN)
~~~

它可能遍历所有 Timer。

## 七十九、为什么 Shutdown 时 O(N) 可以接受

这是：

~~~text
rare control path
~~~

而不是：

~~~text
每次 request hot path
~~~

系统设计允许：

~~~text
rare path用更直接的全局扫描
~~~

不必为它增加：

~~~text
实时维护 non_cancelable_count
~~~

这种额外 hot-path状态。

## 八十、这是“把复杂度放到冷路径”

如果每次 add/del Timer都维护：

~~~text
global non-cancelable counter
~~~

正常运行每个 Timer mutation都会多一份 bookkeeping。

nginx选择：

~~~text
shutdown rare scan
~~~

保持 steady-state简单。

## 八十一、这与 Lazy Timer 一样体现 Runtime 经济学

共同原则：

> **不要为了稀有路径的理论最优，给高频路径永久加成本。**

## 八十二、Cancelable Classification 必须在 Owner Event 上

因为：

~~~text
worker exit decision
~~~

需要知道：

~~~text
这个 Timer背后的 work 是否必须 drain
~~~

只有 deadline本身不够。

## 八十三、同一个 Deadline 可以有不同 Shutdown Semantics

例如两个 Timer：

~~~text
T1 deadline=10s, cancelable=true
T2 deadline=10s, cancelable=false
~~~

时间排序完全相同，

但 graceful exit含义不同。

所以：

~~~text
scheduling metadata
~~~

与：

~~~text
liveness metadata
~~~

是两个维度。

## 八十四、这对机器人 Runtime 的迁移

可以把 Timer 分成：

~~~text
must-drain:
  safety transaction
  actuator shutdown ACK
  persistent storage flush

cancelable:
  metrics flush
  debug heartbeat
  cache refresh
~~~

shutdown时：

~~~text
只等待 must-drain
~~~

而不是所有 Timer一视同仁。

## 八十五、但不要只靠一个 Bool 就解决复杂 Shutdown

nginx这里的：

~~~text
cancelable
~~~

适合它的 event model。

更复杂系统可能需要：

- gate；
- in-flight counter；
- cancellation token；
- scope/task group。

可迁移的是：

~~~text
liveness classification
~~~

而不是具体 bit。

## 八十六、Connection Close 与 Timer Cancel 的顺序再次体现 Quiescence

对 connection而言：

~~~text
future timer callback
~~~

就是：

~~~text
future admitted execution
~~~

所以关闭连接必须：

~~~text
retire timer membership
~~~

再回收 slot。

这和：

- unregister callback；
- remove poller fd；
- cancel queued work；

本质一致。

## 八十七、Timer Delete 并不需要等待另一个 Timer Thread

因为当前主要模型是：

~~~text
single worker owner
~~~

所以：

~~~text
rbtree_delete
~~~

完成以后，

未来 Timer traversal就不会再新发现该 event。

## 八十八、这让 Timer Quiescence 很便宜

没有：

~~~text
cross-thread timer callback currently running?
~~~

的典型竞态。

Callback本身就在：

~~~text
same owner loop
~~~

串行执行。

## 八十九、但 Handler 内部仍可能启动异步子系统

Timer callback退出不等于：

~~~text
所有 downstream async work完成
~~~

Timer tree只管理：

~~~text
Timer callback admission
~~~

不自动管理：

- thread-pool task；
- file AIO；
- upstream RPC。

不同 subsystem仍有自己的 lifetime contract。

## 九十、不要把 Timer Removal 当全局 Object Quiescence

它只证明：

~~~text
timer tree
不会再因为这个 membership触发 callback
~~~

不证明：

~~~text
其他 callback/source不再持有 event/connection
~~~

这条边界必须明确。

## 九十一、Timer Tree 与 Posted Event Queue 是两个 Execution Source

一个 event可能：

~~~text
timer_set=true
posted=true
~~~

不同 source可能同时把同一逻辑 event推向未来执行。

close/lifecycle要分别处理：

- Timer membership；
- posted membership；
- OS backend active state。

## 九十二、这正是下一章 Posted Event Queue 的重点

Timer tree解决：

~~~text
when should event become runnable?
~~~

Posted queue解决：

~~~text
ready event什么时候进入 handler execution phase?
~~~

两者都属于：

~~~text
worker-local scheduler
~~~

但数据结构和语义不同。

Posted queue 这一侧还有一个与 Timer 对称的生命周期协议：`posted` 是 intrusive queue membership bit，executor 会先 delete membership 再进入 handler，而 connection close也会在 slot 回池前删除 read/write posted event。next queue则提供 Timer 所不表达的“下一轮再执行”语义。见 [Posted Event Queue：Readiness、Phase Scheduling、Coalescing 与 Next-tick Barrier](posted-event-queue.md)。

## 九十三、为什么 Timer 使用 Rbtree，而 Posted 使用 Intrusive Queue

Timer需要：

~~~text
ordered by deadline
~~~

Posted callback只需要：

~~~text
ready-order / phase queue
~~~

因此：

~~~text
Rbtree
vs
FIFO-ish intrusive queue
~~~

是由查询模式决定，

不是作者风格不统一。

## 九十四、数据结构应从 Query 反推

Timer的问题：

~~~text
who expires first?
delete this known event
iterate all for shutdown
~~~

Posted queue的问题：

~~~text
what is next runnable event?
append/remove event
~~~

自然得到不同容器。

## 九十五、为什么 Timer Node 与 Posted Queue Node 都嵌在 `ngx_event_t`

event结构同时有：

~~~c
ngx_rbtree_node_t timer;
ngx_queue_t       queue;
~~~

同一个 event可以同时拥有：

~~~text
one timer membership
+
one posted-queue membership
~~~

因为它有：

~~~text
两个独立 intrusive node
~~~

## 九十六、这与 libzmq Pipe 多个 `array_item_t<ID>` 很相似

一个逻辑对象可同时参加多个 container，

但每个 container需要：

~~~text
独立 membership hook
~~~

不能复用同一个 node字段。

## 九十七、Intrusive Multi-membership 是成熟 Runtime 的常见模式

例如：

~~~text
Connection:
  timer node
  ready queue node
  LRU node
  hash node
~~~

每个 membership代表：

~~~text
一种未来 reachability / scheduling relation
~~~

## 九十八、因此 Object Reclaim 前必须退出所有 Membership

不是只：

~~~text
free memory
~~~

而是：

~~~text
remove timer
remove posted queue
remove poller
remove reusable list
remove registry
then reclaim
~~~

这就是 Runtime lifecycle 的核心。

## 九十九、Timer Tree 的红黑性质真正保障什么

它保证树高：

~~~text
O(log N)
~~~

避免最坏：

~~~text
完全退化链表
~~~

使：

- insert；
-delete；
-min left traversal；
-next traversal局部操作；

拥有稳定复杂度边界。

## 一百、为什么不用普通 BST

网络 workload deadline可能非常有序：

~~~text
不断插入 now + constant timeout
~~~

普通 BST可能接近：

~~~text
sorted insertion
→ degenerate chain
~~~

红黑树必须重新平衡。

## 一百零一、Timer Deadline 很可能天然近似递增

例如：

~~~text
每个新 connection:
  timeout = now + 60s
~~~

随着 now增加，

key也大体递增。

这正是普通 BST最危险的输入模式之一。

## 一百零二、Balanced Tree 因而不是学术装饰

它直接防止：

~~~text
connection arrival order
~~~

把 Timer scheduler拖成 O(N)。

## 一百零三、为什么 Duplicate Key 放右侧也不会破坏平衡

BST insertion决定初始位置，

随后红黑：

- recolor；
- rotate；

继续维护 balance invariant。

所以大量相同 deadline也不会变成普通链。

## 一百零四、Timer Lazy Update 还会减少 Rotation Frequency

每次 delete/reinsert都可能：

-改 parent/child；
-rotate；
-recolor。

保留旧 key意味着：

~~~text
整个 tree topology不变
~~~

因此 lazy update省的不只是：

~~~text
两次函数调用
~~~

而是可能的一串 pointer mutation。

## 一百零五、Pointer Mutation 为什么对 Cache 不友好

Rbtree维护会访问：

~~~text
node
parent
grandparent
uncle
rotated child
~~~

如果 tree很大，

这些 node可能分散在：

~~~text
多个 cache line
~~~

高频刷新会制造 cache miss。

## 一百零六、Lazy Update 是 Cache-locality 优化

它通过接受：

~~~text
small deadline error
~~~

减少：

~~~text
pointer-chasing structural mutation
~~~

这比只看 Big-O 更贴近真实性能。

## 一百零七、这就是为什么“数据结构成本”不等于比较次数

真实成本还有：

- cache miss；
- branch；
-pointer chasing；
-write traffic；
- allocator；
- synchronization。

nginx Timer是一个很好的例子。

## 一百零八、为什么 300ms 是常数而不是比例

源码固定：

~~~text
NGX_TIMER_LAZY_DELAY = 300
~~~

这是一条工程 heuristic。

它不是：

~~~text
timeout * 1%
~~~

也不是动态调节。

## 一百零九、因此不同 Timer 共享同一个 Lazy Threshold

一个：

~~~text
60s keepalive
~~~

和：

~~~text
500ms timeout
~~~

都经过同一 300ms比较。

这提醒我们：

> **源码实现的通用 heuristic 不一定是所有业务领域的理论最优。**

## 一百一十、设计自己的 Runtime 时可以做分类策略

例如：

~~~text
soft timer:
  lazy tolerance 50ms

control timer:
  exact

telemetry timer:
  coalescing bucket 100ms
~~~

但复杂度也会上升。

nginx选择：

~~~text
one simple global tolerance
~~~

是另一种工程取舍。

## 一百一十一、Timer Coalescing 与 Lazy Update 不完全相同

Coalescing：

~~~text
主动把多个 deadline对齐到共同 bucket
~~~

nginx Lazy Update：

~~~text
如果某 event已有 deadline
且新 deadline足够接近
→沿用自己的旧 deadline
~~~

它不主动把不同 event归一到统一 bucket。

## 一百一十二、因此不要把它误称为 Timing Wheel

底层仍然是：

~~~text
exact-key rbtree
~~~

只是 update admission是近似的。

## 一百一十三、`ngx_event_find_timer()` 与 Lazy Update 的组合

event loop只相信：

~~~text
tree中实际保存的 key
~~~

不会知道：

~~~text
某 module曾希望 deadline稍微后移
~~~

因为 lazy return根本没更新 key。

所以近似策略完全封装在：

~~~text
add_timer
~~~

入口。

## 一百一十四、这是很好的 Abstraction Boundary

scheduler core不需要：

~~~text
每次考虑 tolerance
~~~

它只维护：

~~~text
当前 authoritative tree state
~~~

## 一百一十五、模块调用者也不需要自己判断 300ms

所有：

~~~text
ngx_add_timer
~~~

统一获得相同 policy。

这减少重复 heuristic。

## 一百一十六、但 Module 仍负责何时 Del Timer

例如 connection close：

~~~text
if timer_set
→ del timer
~~~

scheduler不会自动知道：

~~~text
owner object马上要回收
~~~

因此 lifetime boundary仍由 owner/module显式维护。

## 一百一十七、Scheduler 与 Owner 各自职责

Timer scheduler：

~~~text
order
expire
membership
~~~

owner lifecycle：

~~~text
when timer no longer semantically valid
~~~

不能互相替代。

## 一百一十八、如果忘记 Delete Timer，会发生什么

可能不是立刻 crash。

因为 slot内存稳定，

可能出现更隐蔽的：

~~~text
old Timer fires on reused event generation
~~~

这类 bug比普通 UAF更难看出。

## 一百一十九、Object Pool 会放大“逻辑 UAF”

storage仍然有效，

sanitizer未必看到：

~~~text
freed heap memory
~~~

但语义 owner已经变了。

所以：

~~~text
membership cleanup
+
generation identity
~~~

尤其重要。

## 一百二十、Timer Node 本身没有 Instance Bit

epoll data.ptr带：

~~~text
instance
~~~

Timer tree node没有额外 generation tag。

原因是：

~~~text
worker完全控制 membership
~~~

正确 close协议应该：

~~~text
在 slot reuse前删 timer
~~~

因此无需在 expire时再 generation check。

## 一百二十一、这体现“Prevent vs Detect”两种策略

Timer：

~~~text
prevent stale event
→ remove membership before reuse
~~~

epoll：

~~~text
cannot fully prevent kernel-stale ready
→ detect generation mismatch on return
~~~

## 一百二十二、选哪种取决于 Ownership

如果 Runtime拥有 queue：

~~~text
prefer retire/remove
~~~

如果外部系统可能保留已提交 work：

~~~text
需要 generation/token validate
~~~

## 一百二十三、Timer Expiration Handler 为什么可能立即 Close Connection

因为 timeout本身常是：

~~~text
liveness failure
~~~

例如：

- client header timeout；
-upstream connect timeout；
-write timeout。

所以 callback可直接改变 connection lifecycle。

## 一百二十四、这再次要求 Tree State 先一致

如果 handler close时：

~~~text
ngx_close_connection
→ sees timer_set?
~~~

由于 expire已经：

~~~text
timer_set=0
~~~

close不会尝试第二次 delete当前 node。

这防止 double-remove。

## 一百二十五、这是一条非常具体的重入安全链

~~~text
expire:
  delete node
  timer_set=0
  timedout=1
  handler

handler:
  close connection

close:
  if timer_set
     del timer
~~~

由于 timer_set已经 0：

~~~text
no double delete
~~~

## 一百二十六、如果顺序反过来

~~~text
handler first
~~~

handler close看到：

~~~text
timer_set=1
~~~

就会：

~~~text
del timer
~~~

返回 expire函数后再：

~~~text
del timer again
~~~

直接破坏 rbtree。

所以当前顺序是 correctness protocol。

## 一百二十七、Flag 不只是状态描述，也防重入

`timer_set` 在这里同时服务：

- membership query；
- lifecycle cleanup；
- reentrant callback correctness。

这类 bit常常承载多个相关 invariant。

## 一百二十八、`timedout` 为什么不能在 callback后设

handler通常会：

~~~text
if (rev->timedout) ...
~~~

如果 callback后才设：

~~~text
业务看不到 timeout原因
~~~

所以必须：

~~~text
establish cause before dispatch
~~~

## 一百二十九、Timer Cause 与 I/O Readiness 可能同时出现

现实中可能：

~~~text
fd ready
和
deadline到期
~~~

发生得很接近。

Worker phase ordering决定：

~~~text
哪个 handler先观察什么 state
~~~

所以 Runtime phase本身就是语义的一部分。

## 一百三十、不要假设 OS 与 Timer 存在全局真实同时顺序

event loop只能根据：

- wait return；
- current cached time；
- phase order；

建立自己的：

~~~text
deterministic processing order
~~~

## 一百三十一、这就是 Event Loop 的本质之一

它把并发发生的外部事件：

~~~text
I/O
clock
signal
~~~

线性化为：

~~~text
某个可重复推理的单线程处理顺序
~~~

## 一百三十二、Timer 与 Posted Event 的组合会继续深化这个问题

Timer callback可能：

~~~text
post another event
~~~

posted handler可能：

~~~text
add timer
~~~

所以 worker loop中的 phase order构成：

~~~text
small cooperative scheduler
~~~

## 一百三十三、Timer Tree 的 Sentinel 为什么重要

Rbtree使用：

~~~text
sentinel
~~~

表示 leaf null。

这样核心算法不用每次：

~~~text
if child == NULL
~~~

而可以让：

~~~text
sentinel behaves as black node
~~~

简化红黑修复逻辑。

## 一百三十四、Sentinel 也是 Data-structure Invariant

源码：

~~~text
a sentinel must be black
~~~

红黑树 delete fixup依赖：

~~~text
leaf/sentinel color
~~~

正确。

## 一百三十五、这是容器实现细节，但影响 Runtime 健壮性

Timer Runtime依赖：

~~~text
generic rbtree invariant
~~~

所以 debug tree corruption最终可能表现成：

- timer错序；
-crash；
-infinite loop。

## 一百三十六、为什么 nginx 不自己写“Timer 专用链表”

因为 Timer需要：

~~~text
动态任意 deadline
~~~

不是：

~~~text
严格按插入顺序到期
~~~

链表若保持有序：

~~~text
insert O(N)
~~~

大量 connection Timer不合适。

## 一百三十七、为什么不直接用 Timing Wheel

Timing wheel通常用：

~~~text
bounded resolution
+
bucketed deadline range
~~~

换 O(1) 或接近 O(1) 操作。

nginx选择 rbtree表明其设计更偏：

~~~text
通用 arbitrary deadlines
+
简单成熟 intrusive tree
~~~

同时再通过：

~~~text
300ms lazy update
~~~

削减现实 churn。

## 一百三十八、这是一种“精确容器 + 近似刷新”的折中

不是：

~~~text
最理论极致的 Timer 算法
~~~

而是：

~~~text
implementation simplicity
+
good enough performance
+
明确 workload heuristic
~~~

## 一百三十九、工程设计不应该只比较论文算法复杂度

还要考虑：

-已有 core container；
- debugging；
-portability；
-maintenance；
-object model；
-common operation mix。

## 一百四十、nginx 已经有通用 Intrusive Rbtree

Timer复用：

~~~text
same balancing core
+
timer-specific comparator
~~~

降低实现表面积。

## 一百四十一、`ngx_rbtree_next()` 为什么值得注意

它让：

~~~text
in-order traversal
~~~

无需：

~~~text
额外 stack/recursive traversal
~~~

给定当前 node：

-有右子树 → 取右子树最小；
-否则沿 parent上升。

这适合：

~~~text
shutdown scan
~~~

等稀有顺序遍历。

## 一百四十二、Tree Node Parent Pointer 的额外价值

除了平衡删除，

它还支持：

~~~text
successor traversal
~~~

这也是 Rbtree 对 Timer shutdown scan的自然支持。

## 一百四十三、Timer Key Overflow 为什么只在 Comparator 处理还不够

nearest deadline与 expiration也必须：

~~~text
同样使用 signed difference
~~~

否则 insertion顺序 wrap-safe，

但 expire判断不 wrap-safe，

系统仍会错误。

nginx在三处保持一致：

- insert comparator；
-find timer；
-expire due check。

## 一百四十四、这是 Cross-layer Arithmetic Invariant

所有对 Timer key的“before/after”判断必须遵循同一：

~~~text
modular signed-delta semantics
~~~

不能某处：

~~~text
unsigned <
~~~

某处：

~~~text
signed difference
~~~

## 一百四十五、时间比较最好封装统一语义

nginx当前在相关代码中重复相同 cast模式。

设计新系统时可以考虑：

~~~cpp
deadline_before(a,b)
deadline_due(deadline,now)
~~~

减少误用。

## 一百四十六、但必须清楚 Wrapper 的 Horizon Preconditions

不要把：

~~~text
wrap-safe
~~~

封装后忘记：

~~~text
max interval < half range
~~~

这仍是 API contract。

## 一百四十七、为什么 64-bit Time 会让 Wrap 不那么实际

64-bit ms wrap周期极大，

但 nginx源码仍保留兼容：

~~~text
32-bit millisecond case
~~~

体现低层 runtime常要考虑：

- 32-bit platform；
- typedef宽度；
-portability。

## 一百四十八、对嵌入式机器人尤其重要

MCU tick常见：

~~~text
uint32_t milliseconds
~~~

每 49.7天回绕。

如果写：

~~~c
if (now > deadline)
~~~

长期运行一定出错。

正确模式通常是：

~~~c
(int32_t)(deadline - now)
~~~

或等价 helper，

并限制最大 timeout horizon。

## 一百四十九、这是 nginx Timer 最直接可迁移的基础技巧之一

而且比：

~~~text
红黑树本身
~~~

更常用。

## 一百五十、Timer 精度与 Clock 精度也要区分

`NGX_TIMER_LAZY_DELAY=300ms`：

~~~text
scheduler policy precision
~~~

而：

~~~text
ngx_current_msec
~~~

来自 runtime time update：

~~~text
clock observation precision
~~~

是两个不同误差来源。

## 一百五十一、不要只调高 Clock Resolution 就以为 Timer 更精确

如果 add_timer策略本身允许：

~~~text
300ms stale deadline
~~~

更频繁读时钟并不会消除这个 policy error。

## 一百五十二、System Precision 是多层组合

\[
E_{\text{total}}
\approx
E_{\text{clock}}
+
E_{\text{scheduler policy}}
+
E_{\text{event-loop latency}}
+
E_{\text{handler queueing}}
\]

实际 timeout触发误差不只有 Tree key。

## 一百五十三、Event Loop Busy 也会让 Timer 晚执行

即使 tree中 key绝对准确，

如果某 handler执行：

~~~text
500ms blocking work
~~~

Timer只能：

~~~text
500ms后才被检查
~~~

所以单线程 Reactor要求：

~~~text
callback不能长期阻塞
~~~

## 一百五十四、Timer Correctness 依赖 Scheduler Progress

这和 Seastar reclaim poller类似：

~~~text
owner loop不获得 CPU
→ subsystem不 progress
~~~

## 一百五十五、因此 “Timer 设置成功” 不等于硬 deadline guarantee

它只表达：

~~~text
once worker regains event-loop progress,
due timer will be observed
~~~

## 一百五十六、nginx Timer 是 Soft Real-time Mechanism

非常适合网络 timeout，

不应拿它当：

~~~text
hard real-time scheduler
~~~

## 一百五十七、对控制系统迁移时必须明确 Worst-case Callback Time

如果要在同一 loop中运行：

- network；
-control；
-timer；

必须预算：

~~~text
max handler execution
+
max batch
+
timer scan
~~~

否则 deadline semantics不可控。

## 一百五十八、Timer 到期 Batch 也应计入 WCET

如果一轮可能到期：

~~~text
N timers
~~~

且 handler最大成本：

~~~text
C
~~~

最坏 burst：

\[
N \cdot C
\]

nginx并不为实时场景做这个 bound。

## 一百五十九、这正是“借鉴设计而非照抄实现”

可借鉴：

- owner-local Timer；
-intrusive membership；
-wrap-safe time arithmetic；
-state-before-callback；
-liveness classification。

实时系统则可能改：

- bounded expiry batch；
-priority timer；
-tighter lazy threshold。

## 一百六十、Timer 与 Connection Pool 的完整生命周期

建立连接：

~~~text
connection slot allocated
        ↓
read/write event initialized
        ↓
module adds timers
        ↓
tree references embedded event nodes
~~~

关闭连接：

~~~text
retire timers
        ↓
retire backend events
        ↓
module cleanup
        ↓
free connection slot
        ↓
slot may be reused
~~~

## 一百六十一、顺序的核心是“先切 Future Reachability”

只要一个异步 source还能：

~~~text
未来发现这个对象
~~~

就不能把 storage解释成：

~~~text
new generation
~~~

Timer tree是一个 source，

epoll queue是另一个 source。

## 一百六十二、Timer 可以 Prevent Stale，Epoll 需要 Detect Stale

再压缩一次：

~~~text
owned scheduler:
  remove before reuse

external kernel queue:
  validate generation on return
~~~

这个区别非常值得迁移到：

- GPU completion queue；
-DMA ring；
-thread-pool callback；
-message broker。

## 一百六十三、Graceful Shutdown 是 Timer Liveness 最终闭环

worker收到 graceful quit：

~~~text
stop accepting new connections
close idle connections
continue loop
~~~

之后每轮：

~~~c
if (ngx_exiting
    && ngx_event_no_timers_left()
       == NGX_OK)
{
    exit worker;
}
~~~

## 一百六十四、所以 Worker 退出不是只看 Connection Count

Timer本身可能代表：

~~~text
必须完成的 outstanding obligation
~~~

只要有：

~~~text
non-cancelable timer
~~~

worker还不能退出。

## 一百六十五、为什么不直接等待 Tree Empty

因为有些 Timer：

~~~text
可以丢弃
~~~

如果为了：

~~~text
metrics/cache/soft background timeout
~~~

再等几十秒，

graceful shutdown会无谓拖长。

所以 `cancelable` 提供：

~~~text
shutdown policy classification
~~~

## 一百六十六、Quiescence 不等于“所有 Queue 都完全空”

更准确：

> **所有会影响正确性的 outstanding obligations 已经消失；可安全放弃的工作可以仍存在。**

这就是 cancelable Timer 的意义。

## 一百六十七、这个原则比 Timer 本身更通用

shutdown可以分类：

~~~text
must finish
may cancel
must rollback
may drop
~~~

而不是：

~~~text
一律 drain everything
~~~

## 一百六十八、为什么 `ngx_event_no_timers_left()` 叫这个名字略有误导

它返回 OK时：

~~~text
tree可能并不空
~~~

只要：

~~~text
remaining timers all cancelable
~~~

所以真实语义更接近：

~~~text
no shutdown-blocking timers left
~~~

源码名是历史/简洁命名，

阅读时应根据实现理解 contract。

## 一百六十九、读源码不能只靠函数名

类似：

- `has_out()` 在不同 libzmq scheduler语义不同；
- `free()` 可能只是 deferred reclaim admission；
- `no_timers_left()` 可能仍有 cancelable timer。

真正 contract由：

~~~text
implementation + caller
~~~

共同定义。

## 一百七十、最终把 Timer Runtime 压缩成五层

第一层：

~~~text
CLOCK
ngx_current_msec
~~~

第二层：

~~~text
DEADLINE INDEX
intrusive rbtree
~~~

第三层：

~~~text
UPDATE POLICY
300ms lazy tolerance
~~~

第四层：

~~~text
EXECUTION TRANSITION
remove → flags → handler
~~~

第五层：

~~~text
LIFECYCLE / QUIESCENCE
timer_set / cancelable / owner storage
~~~

## 一百七十一、源码作者必须守住的核心不变量

第一：

> **Timer tree 是 worker-local scheduler index；正常 connection/event Timer mutation由同一个 worker owner执行。**

第二：

> **`ngx_event_t` 内嵌 `ngx_rbtree_node_t timer`，因此 Timer membership直接引用 event storage；owner storage回收前必须先退出 tree。**

第三：

> **Timer key不是用普通 unsigned `<` 排序，而使用 signed difference；find/expire也必须使用同一 wrap-aware arithmetic。**

第四：

> **Signed-delta wrap handling依赖 deadline interval小于半个计数空间这一 horizon contract。**

第五：

> **重复 deadline合法；Timer scheduler只要求正确 deadline ordering，不要求 key唯一。**

第六：

> **`timer_set` 是当前 rbtree membership state，不是“曾经设置过 Timer”的历史标志。**

第七：

> **Timer expire必须先从 tree删除、清 `timer_set`、设 `timedout`，再进入任意 handler；否则 callback重入/close可能 double-delete或访问仍被 scheduler拥有的 node。**

第八：

> **`NGX_TIMER_LAZY_DELAY=300` 是明确的近似更新策略：新旧 deadline差小于阈值时继续使用旧 key，以 bounded precision换取更少 tree mutation。**

第九：

> **Lazy update优化的是高频 refresh churn，而不是 Timer expiration本身。**

第十：

> **connection close必须先删除 read/write Timer，再归还可复用 slot；否则旧 tree membership可能在 slot新 generation上触发逻辑 UAF。**

第十一：

> **`cancelable` Timer不会单独阻止 graceful worker退出；non-cancelable Timer则代表仍需 drain 的 liveness obligation。**

第十二：

> **Timer removal只关闭 Timer这一条 future-execution source，不等于对象在所有 subsystem中已经 quiescent。**

## 一百七十二、最终心智模型

~~~text
module / connection event
        |
        | ngx_add_timer(delay)
        v
absolute deadline
        |
        | if existing and |Δ| < 300ms
        |        keep old key
        |
        v
embedded rbtree node
        |
        v
worker-local timer tree
        |
        +------------------------------+
        |                              |
        | find minimum                 | graceful exit scan
        v                              v
epoll_wait timeout               any non-cancelable?
        |                              |
        v                              v
current time advances           yes → keep worker alive
        |
        v
minimum deadline <= now?
        |
       yes
        v
delete node
        |
timer_set = 0
timedout = 1
        |
        v
event handler
        |
        +--> may close owner
        +--> may re-arm timer
        +--> may post other work
~~~

如果只记一个结论：

> **nginx 的 Timer 设计不是“红黑树代替最小堆”这么简单。它把 deadline index直接嵌进 `ngx_event_t` 生命周期，用 signed-delta 保证毫秒计数回绕时仍能排序，用 300 ms lazy refresh 把软 timeout 的精度预算兑换成更少 rbtree churn，并在 callback 前先完成 tree membership 与 timeout state 转移；最后 `cancelable` 又把 Timer 从 deadline scheduler提升成 graceful shutdown 的 liveness registry。**
