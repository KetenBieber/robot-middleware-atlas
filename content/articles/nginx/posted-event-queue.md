# Posted Event Queue：Readiness、Phase Scheduling、Coalescing 与 Next-tick Barrier

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

很多 Reactor 教程把事件处理写成：

~~~text
epoll_wait
    ↓
fd ready
    ↓
handler(fd)
~~~

这对理解“非阻塞 I/O”够了，但对理解 nginx 不够。

nginx 在 OS readiness 与 protocol handler 之间又加了一层：

~~~text
kernel readiness
        ↓
ngx_event_t state update
        ↓
immediate handler
        or
posted-event queue
        ↓
worker phase scheduling
        ↓
handler
~~~

因此 nginx 的 worker 实际上不是：

~~~text
一个 epoll dispatcher
~~~

而是：

> **一个两级 scheduler：event backend 负责发现 readiness，posted queues 再决定已经 runnable 的 event 在哪个 phase、哪一轮进入 handler。**

固定源码里有三条全局 worker-local queue：

~~~c
ngx_queue_t ngx_posted_accept_events;
ngx_queue_t ngx_posted_next_events;
ngx_queue_t ngx_posted_events;
~~~

它们不是三种“优先级数字”。

它们分别表达：

~~~text
accept phase
normal deferred phase
next event-loop iteration
~~~

这章要把这三个 phase、intrusive queue、`posted` bit、event coalescing、self-repost、connection close 与 next-tick fairness全部串起来。

Timer deadline怎样在同一个 worker中先于/晚于 posted phase执行，见 [Timer Rbtree：Deadline Ordering、Lazy Update、Wrap-around 与 Shutdown Liveness](timer-rbtree.md)。Worker/epoll与 accept admission 的外层执行链见 [Worker / epoll / Accept：多进程 Reactor、Accept Ownership 与 Stale Event Generation](worker-epoll-accept.md)。

## 一、先区分 Readiness 与 Execution

Linux epoll告诉 nginx：

~~~text
fd may make progress
~~~

它不替 nginx决定：

- 立刻执行 callback？
- 先处理 accept？
- 先释放 accept mutex？
- 推迟到普通 posted phase？
- 推迟到下一个 event-loop iteration？
- callback之前是否还要完成内部状态修改？

所以：

~~~text
ready
~~~

和：

~~~text
run handler now
~~~

不是同一个事实。

## 二、`ngx_event_t` 同时是 Readiness State 与 Runnable Node

`ngx_event_s` 中既有：

~~~c
unsigned ready:1;
unsigned active:1;
unsigned posted:1;
~~~

也有：

~~~c
ngx_event_handler_pt handler;
ngx_queue_t queue;
~~~

这说明一个 event同时携带：

~~~text
OS/event-backend state
+
runtime scheduling state
+
callback entry
+
intrusive queue hook
~~~

## 三、为什么 Posted Queue 使用 Intrusive Doubly-linked Queue

`ngx_queue_t` 只有：

~~~c
prev
next
~~~

`ngx_event_t` 自己内嵌：

~~~text
ngx_queue_t queue
~~~

投递 event时不需要：

~~~text
malloc PostedTask
~~~

而是：

~~~text
event.queue
直接进入 worker queue
~~~

这非常适合：

- 高频；
-对象长期存在；
-状态 owner-local；
-需要 O(1) remove。

## 四、为什么需要 O(1) Remove

Posted event并不一定会顺利等到 handler执行。

Connection可能在此之前：

- timeout；
- error；
-主动 close；
-被 reusable pressure回收。

close路径必须：

~~~text
从 posted queue删除 read/write event
~~~

所以单向“只能 pop head”的结构不够方便。

## 五、`ngx_post_event` 的核心不是 Insert，而是 Membership Gate

宏：

~~~c
if (!(ev)->posted) {
    (ev)->posted = 1;
    ngx_queue_insert_tail(q, &(ev)->queue);
} else {
    ...
}
~~~

也就是说：

~~~text
posted = 0
→ 可以新建 queue membership

posted = 1
→ 不再重复插入
~~~

## 六、`posted` 是 Container Membership Bit

它不是：

~~~text
这个 event曾经被 post过
~~~

而是：

> **这个 event当前已经占据某一条 posted queue membership。**

这和 Timer 的：

~~~text
timer_set
~~~

非常类似。

## 七、一次 Event 只能同时占一个 Posted Queue

因为：

~~~text
ngx_event_t
~~~

只有一个：

~~~text
ngx_queue_t queue
~~~

hook。

所以它不能同时：

~~~text
在 posted_events
又在 posted_next_events
~~~

否则同一个 intrusive node会被两条链同时改 `prev/next`，

直接破坏两个 queue。

## 八、`posted` Bit 因而也是 Multi-membership 防线

如果某调用点想把 event从：

~~~text
当前 posted queue
~~~

迁移到：

~~~text
posted_next_events
~~~

必须先：

~~~text
ngx_delete_posted_event(ev)
~~~

再：

~~~text
ngx_post_event(ev,
               &ngx_posted_next_events)
~~~

## 九、OpenSSL 源码就这么做

当 SSL layer还有 buffered data，但希望：

~~~text
next event-loop iteration
继续 read
~~~

源码：

~~~c
if (c->read->posted) {
    ngx_delete_posted_event(c->read);
}

ngx_post_event(
    c->read,
    &ngx_posted_next_events);
~~~

这不是多余防御。

它是在：

~~~text
迁移 phase membership
~~~

## 十、为什么不能直接 `ngx_post_event(next_queue)`

因为：

~~~text
posted == 1
~~~

宏只会 log：

~~~text
update posted event
~~~

不会移动 node。

所以：

~~~text
目标 queue 参数
~~~

在 event已经 posted时不会生效。

## 十一、这暴露了一个重要 API Contract

`ngx_post_event(ev,q)` 不是：

~~~text
ensure event belongs to q
~~~

而是：

> **如果 event尚未 posted，则把它加入 q；否则只保持已有 membership。**

读调用点时必须清楚这个差异。

## 十二、Coalescing 的真实含义

假设同一个 event在执行前被触发：

~~~text
notification 1
notification 2
notification 3
~~~

调用三次：

~~~text
ngx_post_event(ev, posted_events)
~~~

queue里仍然只有：

~~~text
one node
~~~

所以：

~~~text
3 notifications
→ 1 pending execution
~~~

## 十三、为什么这叫 Event Coalescing

Posted queue表达：

~~~text
“这个 event至少需要再运行一次”
~~~

而不是：

~~~text
“发生了 N 次事件”
~~~

## 十四、什么时候 Coalescing 是正确的

如果 handler语义是：

~~~text
re-evaluate current state
~~~

例如：

- socket readable；
-buffered SSL data available；
-write side may progress；

多个 notification合并通常没问题。

因为 handler真正会检查：

~~~text
authoritative state
~~~

## 十五、什么时候 Coalescing 会丢业务语义

如果 notification本身代表：

~~~text
必须逐个计数的离散事件
~~~

例如：

-完成了 N 个独立 transaction；
-收到 N 个不可丢故障事件；
-N 次 credit return分别有数值意义；

只保留：

~~~text
one pending bit
~~~

就不够。

## 十六、所以 Posted Event 适合 State-triggered Execution

核心模式：

~~~text
state changed
        ↓
mark runnable
        ↓
run once
        ↓
drain/re-evaluate state
~~~

而不是：

~~~text
notification count itself is payload
~~~

## 十七、这和 Doorbell 模型很像

Posted bit像：

~~~text
doorbell already rung
~~~

Queue node只是告诉 owner：

~~~text
去看真实状态
~~~

多个生产原因可以合并成一个 execution opportunity。

## 十八、为什么 Handler 前先 `ngx_delete_posted_event`

`ngx_event_process_posted()`：

~~~c
while (!ngx_queue_empty(posted)) {
    q = ngx_queue_head(posted);
    ev = ngx_queue_data(
             q,
             ngx_event_t,
             queue);

    ngx_delete_posted_event(ev);

    ev->handler(ev);
}
~~~

顺序非常关键：

~~~text
remove membership
posted = 0
        ↓
handler
~~~

## 十九、第一原因：允许 Handler Self-repost

进入 handler时：

~~~text
posted == 0
~~~

所以 handler可以合法：

~~~text
ngx_post_event(ev, ...)
~~~

重新建立一个新的 future execution obligation。

## 二十、如果 Handler 前不清 `posted`

self-repost：

~~~text
posted仍为1
~~~

宏会认为：

~~~text
已经在 queue
~~~

于是不会插入。

handler想请求：

~~~text
再运行一次
~~~

会被错误吞掉。

## 二十一、第二原因：Callback 可以 Close 自己

handler可能：

~~~text
close connection
~~~

close逻辑会检查：

~~~text
if event->posted
    delete_posted_event
~~~

由于 process loop已经先删除：

~~~text
posted=0
~~~

close不会重复 remove当前 node。

这和 Timer expire：

~~~text
先 timer_set=0
再 handler
~~~

是同一条重入安全原则。

## 二十二、State-before-callback 再次出现

成熟 Runtime反复使用：

~~~text
authoritative scheduler state
        ↓
transition to callback-owned state
        ↓
invoke arbitrary handler
~~~

而不是：

~~~text
把内部 container仍留在半完成状态
就进入业务 callback
~~~

## 二十三、Self-repost 到 `posted_events` 会发生什么

注意 `ngx_event_process_posted()` 是：

~~~text
while queue not empty
~~~

它不是：

~~~text
snapshot current queue length
for N items
~~~

如果 handler执行时：

~~~text
repost itself to same posted_events
~~~

新 node插入 tail。

当前 while继续，

最终可能再次执行它：

~~~text
same posted-processing phase
~~~

## 二十四、所以普通 Posted Queue 不自动提供 “Next Tick”

这是一个非常重要的区别。

~~~text
post normal queue
~~~

只表示：

~~~text
deferred relative to current direct call
~~~

不一定表示：

~~~text
下一轮 event loop
~~~

## 二十五、为什么需要独立 `ngx_posted_next_events`

它专门表达：

> **至少跨过当前 event-loop iteration 的 phase boundary。**

这是一种：

~~~text
next-tick barrier
~~~

## 二十六、`posted_next` 在什么时候搬运

`ngx_process_events_and_timers()` 在调用 event backend前：

~~~c
if (!ngx_queue_empty(
        &ngx_posted_next_events))
{
    ngx_event_move_posted_next(cycle);
    timer = 0;
}
~~~

## 二十七、这里有两步动作

第一：

~~~text
move next queue
→ normal posted queue
~~~

第二：

~~~text
timer = 0
~~~

意味着：

~~~text
这轮不要阻塞等待 I/O
~~~

## 二十八、为什么 Move 发生在 Event-loop Iteration 开头

假设当前 normal posted handler中：

~~~text
post to posted_next
~~~

当前 iteration 已经错过：

~~~text
move_posted_next
~~~

所以它不会在当前 iteration再次被 normal posted phase处理。

直到：

~~~text
下一次进入 ngx_process_events_and_timers
~~~

才会被搬运。

这就建立了真正的：

~~~text
iteration boundary
~~~

## 二十九、为什么搬运后 `timer=0`

如果只把 node搬到：

~~~text
posted_events
~~~

然后仍：

~~~text
epoll_wait(INFINITE)
~~~

且网络没有新事件，

posted work可能长期饿死。

所以：

~~~text
pending next events
→ force nonblocking poll
~~~

确保当前 iteration 很快走到：

~~~text
process posted_events
~~~

## 三十、这是 Liveness Guarantee

Next-tick event不是：

~~~text
等下次恰好有 I/O时再跑
~~~

而是：

~~~text
主动触发额外 loop iteration
~~~

HTTP auth-delay源码甚至直接写注释：

~~~text
trigger an additional
event loop iteration
to ensure constant-time processing
~~~

## 三十一、这条调用非常有代表性

源码：

~~~c
ngx_post_event(
    r->connection->write,
    &ngx_posted_next_events);
~~~

作用不是：

~~~text
等 fd writable
~~~

而是：

~~~text
人为制造下一轮 execution opportunity
~~~

## 三十二、所以 Posted Queue 不是纯 I/O Mechanism

它可以承载：

- OS readiness；
-protocol continuation；
-software-generated event；
-fairness yield；
-reentrancy break。

这已经是：

~~~text
cooperative scheduler primitive
~~~

## 三十三、`ngx_event_move_posted_next()` 为什么先设置 `ready=1`

它遍历 next queue：

~~~c
ev->ready = 1;
ev->available = -1;
~~~

再整体 splice。

这告诉后续 handler：

~~~text
把它当成 runnable/ready continuation
~~~

而不是等待新的 backend readiness证明。

## 三十四、`available=-1` 表示什么

在通用非-kqueue语义中：

~~~text
available=-1
~~~

可理解为：

~~~text
ready，但准确可读/可写数量未知
~~~

这与软件 post的本质一致：

~~~text
请重新尝试 operation
~~~

而不是：

~~~text
kernel明确告诉你有 N bytes
~~~

## 三十五、Readiness 可以来自 Kernel，也可以来自 Runtime

这点非常重要。

统一 `ngx_event_t` 后：

~~~text
kernel-ready
~~~

与：

~~~text
software-ready/retry
~~~

可以进入同一个 handler ABI。

## 三十六、为什么整个 Next Queue 用 `ngx_queue_add` Splice

处理完 flags后：

~~~c
ngx_queue_add(
    &ngx_posted_events,
    &ngx_posted_next_events);

ngx_queue_init(
    &ngx_posted_next_events);
~~~

不是逐个：

~~~text
remove
insert_tail
~~~

而是直接双向链表 splice。

## 三十七、这使 Queue Transfer 近似 O(1)

除了前面为了更新：

~~~text
ready/available
~~~

必须遍历每个 event外，

真正容器迁移：

~~~text
head/tail pointer rewiring
~~~

可以一次完成。

## 三十八、为什么不能完全 O(1) Move 而不遍历

因为每个 event还要做：

~~~text
ready = 1
available = -1
~~~

所以：

~~~text
semantic state transition
~~~

仍是 O(N)。

Container splice只是避免额外每节点 remove/insert开销。

## 三十九、Container Cost 与 Semantic Cost 要分开

常见优化误区：

~~~text
我用了 splice，所以 move queue O(1)
~~~

但如果还要对每个 item更新业务状态，

整体仍是：

\[
O(N)
\]

正确分析要分：

~~~text
container topology mutation
vs
per-event semantic mutation
~~~

## 四十、三条 Queue 分别是什么

### `ngx_posted_accept_events`

用于：

~~~text
accept readiness
~~~

特别是在：

~~~text
accept mutex + NGX_POST_EVENTS
~~~

路径下。

### `ngx_posted_events`

普通 deferred runnable event。

### `ngx_posted_next_events`

保证至少推迟一个 loop iteration 的 continuation。

## 四十一、Accept Queue 为什么单独一条

Worker核心顺序：

~~~text
epoll/process backend
        ↓
posted_accept_events
        ↓
release accept mutex
        ↓
expire timers
        ↓
posted_events
~~~

accept handler需要：

~~~text
在持有 accept ownership期间
尽快完成 admission
~~~

所以不能和普通 event混成：

~~~text
同一 FIFO
~~~

## 四十二、如果 Accept 与普通 Event 混成一条 Queue

假设：

~~~text
1000 个普通 events
排在 accept前
~~~

worker可能：

~~~text
长时间持 accept mutex
~~~

其他 worker无法接管 shared listener。

所以 queue分层本质上是：

~~~text
phase-level scheduling policy
~~~

## 四十三、Accept Mutex 当前不是默认路径，但 Queue 仍有意义

固定版本：

~~~text
accept_mutex默认 off
~~~

Linux epoll多 worker常走：

~~~text
EPOLLEXCLUSIVE
~~~

但 Runtime仍保留：

~~~text
posted_accept_events
~~~

支持：

-显式 accept mutex配置；
-其他 event backend；
-统一 phase abstraction。

## 四十四、Backend 怎样选择 Immediate 还是 Posted

epoll：

~~~c
if (flags & NGX_POST_EVENTS) {
    queue =
      rev->accept
        ? &ngx_posted_accept_events
        : &ngx_posted_events;

    ngx_post_event(rev, queue);
} else {
    rev->handler(rev);
}
~~~

write side同理。

## 四十五、同一个 Backend Readiness 有两种 Dispatch Policy

~~~text
ready
  |
  +-- immediate handler
  |
  +-- posted execution
~~~

所以：

> **event backend 只负责“发生了什么”，worker policy 决定“现在执行还是排队执行”。**

## 四十六、为什么 Direct Handler 仍然存在

如果没有：

~~~text
需要延迟 callback 的 phase/lifetime原因
~~~

直接：

~~~text
rev->handler(rev)
~~~

省掉 queue操作。

nginx不是：

~~~text
所有 readiness都强制二次排队
~~~

而是按 phase需要选择。

## 四十七、这是一种 Conditional Two-level Scheduling

比：

~~~text
always direct
~~~

更可控，

比：

~~~text
always queue
~~~

更低开销。

## 四十八、Posted Queue 的 FIFO 是什么范围

`ngx_queue_insert_tail`：

~~~text
append tail
~~~

`process_posted`：

~~~text
take head
~~~

所以在单条 queue内部：

~~~text
按 post insertion order
~~~

近似 FIFO。

## 四十九、但不能把它解释成全局事件发生顺序

因为：

- accept与normal是不同 queue；
-next跨 iteration；
- direct handler根本不进 queue；
-Timer有独立 phase；
-多个 kernel event在同一 epoll batch中有自己的返回顺序。

所以 Runtime只定义：

~~~text
phase + local queue ordering
~~~

而不是：

~~~text
所有外部事件的绝对时间顺序
~~~

## 五十、Event Loop 本质上在构造一个 Deterministic Partial Order

可以写成：

~~~text
backend detection
    before
posted accept phase
    before
accept mutex release
    before
timer expiration
    before
normal posted phase
~~~

而同一 normal queue内部：

~~~text
FIFO-ish
~~~

这就是 Runtime可推理性来源。

## 五十一、为什么 Deferred Callback 可以降低重入复杂度

假设 handler A内部状态还在：

~~~text
TRANSITIONING
~~~

如果直接调用 B，

B又触发 A，

可能出现：

~~~text
A reenters before state commit
~~~

改成：

~~~text
A:
  mutate state
  post B
  return

worker:
  later run B
~~~

就建立了：

~~~text
state commit boundary
~~~

## 五十二、Posted Event 是一种 Poor-man's Continuation

它不保存：

~~~text
C++ coroutine stack
~~~

而是：

~~~text
event object
+
handler function pointer
+
owner state
~~~

下一次执行时 handler根据：

~~~text
object current state
~~~

继续状态机。

## 五十三、这很适合 C 风格 Runtime

无需：

- heap closure；
-future；
-coroutine frame。

而是：

~~~text
explicit state machine
+
software scheduling
~~~

## 五十四、代价是什么

Continuation state必须：

~~~text
显式存在对象字段
~~~

开发者要手工维护：

- handler切换；
-flags；
-buffer cursor；
-timer；
-posted membership。

这种代码性能高，

但状态机 discipline要求也高。

## 五十五、为什么 Posted Bit 不记录 Post Count

因为 nginx希望的是：

~~~text
at least once execution
~~~

而不是：

~~~text
exact notification accounting
~~~

这种 API会自然要求 handler：

~~~text
drain authoritative source until cannot progress
~~~

## 五十六、以 Socket Read 为例

多个 readiness：

~~~text
coalesced
~~~

没问题，

因为 handler应该：

~~~text
read until EAGAIN
~~~

或者根据 backend语义更新：

~~~text
ready/available
~~~

## 五十七、这与 Edge-triggered 设计理念相容

epoll clear/greedy模式本来就要求：

~~~text
consume state until no progress
~~~

Posted coalescing进一步强调：

~~~text
notification is hint
state is truth
~~~

## 五十八、但 `ready` 仍然是 Runtime Truth 的一部分

epoll ready后：

~~~c
rev->ready = 1;
rev->available = -1;
~~~

再 post。

所以 handler被延迟执行时，

readiness state已经先写入 event。

## 五十九、这也是 State-before-Notify

~~~text
backend:
  update ev->ready
  update ev->available
        ↓
  post event
~~~

handler之后读取：

~~~text
已经建立的 authoritative Runtime state
~~~

## 六十、为什么 Posted Queue 本身不需要锁

主要路径：

~~~text
worker-local event loop
~~~

Queue mutation也由同一 worker执行。

即使 readiness来自 kernel，

kernel只通过：

~~~text
epoll_wait return buffer
~~~

交给 worker，

不会另一个线程直接改 `ngx_queue_t`。

## 六十一、这再次说明 Owner Topology 比 Concurrent Container 更重要

nginx不需要：

~~~text
lock-free MPMC posted queue
~~~

因为设计上没有：

~~~text
many worker threads concurrent posting
~~~

到同一个 queue。

## 六十二、如果外部线程需要通知 Worker 怎么办

那会经过：

~~~text
eventfd / notify backend / dedicated handoff
~~~

再回到 worker owner context，

而不是随便让线程直接改 posted list。

## 六十三、Posted Queue 是 Owner-local Scheduler Queue

这和：

- Seastar task queue；
-libuv pending queue；

在设计上同属：

~~~text
owner loop local runnable set
~~~

## 六十四、Connection Close 为什么一定要删除 Posted Event

源码：

~~~c
if (c->read->posted) {
    ngx_delete_posted_event(c->read);
}

if (c->write->posted) {
    ngx_delete_posted_event(c->write);
}
~~~

然后：

~~~text
mark closed
remove reusable
free connection slot
~~~

## 六十五、Posted Membership 也是 Future Reachability

只要 event仍在：

~~~text
ngx_posted_events
~~~

未来：

~~~text
process_posted
~~~

就会：

~~~text
container_of(queue node)
→ ngx_event_t*
→ handler
~~~

所以 queue membership等价于：

~~~text
未来 callback admission
~~~

## 六十六、Slot Reuse 前不删除会怎样

错误链：

~~~text
old connection event posted
        ↓
old connection closes
        ↓
slot returned
        ↓
slot reused by new connection
        ↓
posted queue reaches same embedded node
        ↓
executes handler against new logical generation
~~~

这和 stale Timer是同一类：

~~~text
logical UAF despite valid storage address
~~~

## 六十七、所以 Close 要退出多个 Scheduling Domains

完整 connection close至少要处理：

~~~text
Timer tree membership
epoll/backend membership
posted queue membership
reusable queue membership
protocol state
~~~

这说明：

> **对象生命周期不是“谁 delete 内存”，而是“谁还拥有未来执行权”。**

## 六十八、Posted Queue 与 Epoll Generation 的区别

Posted queue完全由 nginx owner控制：

~~~text
close时可以主动 remove
~~~

所以主要使用：

~~~text
prevent stale callback
~~~

epoll ready list在 kernel里：

~~~text
close无法保证所有旧 ready item消失
~~~

所以使用：

~~~text
generation validate
~~~

## 六十九、Prevent 与 Detect 再次分工

Owned queue：

~~~text
remove membership
~~~

External queue：

~~~text
validate token/generation
~~~

这是一条很通用的 async lifecycle设计原则。

## 七十、为什么 `ngx_delete_posted_event` 先清 Bit 再 Remove

宏：

~~~c
(ev)->posted = 0;
ngx_queue_remove(&(ev)->queue);
~~~

从单 worker角度两步不会和其他线程竞争。

先清 bit意味着：

~~~text
logical membership retired
~~~

然后结构 unlink。

## 七十一、在 Debug Build 中 Queue Remove 还清 Link

`ngx_queue_remove` debug版本：

~~~text
prev = NULL
next = NULL
~~~

便于发现：

- double remove；
- stale queue node reuse。

和 Timer debug清 parent/child一样，

属于：

~~~text
intrusive membership misuse detection
~~~

## 七十二、Handler Self-repost 为什么不导致 Double-link

因为 `process_posted`：

~~~text
delete old membership
→ handler
~~~

handler看到：

~~~text
posted=0
~~~

再插入：

~~~text
same embedded node
~~~

属于新的、合法 membership generation。

## 七十三、这里也存在 Logical Generation，虽没有显式 Counter

每次：

~~~text
post
→ process/delete
→ repost
~~~

都可以看成：

~~~text
new execution obligation generation
~~~

只是 nginx用：

~~~text
membership bit + single-owner serial order
~~~

而不是整数 generation。

## 七十四、为什么 Next Queue 要先删除已有 Posted Membership

OpenSSL代码：

~~~c
if (c->read->posted) {
    ngx_delete_posted_event(c->read);
}

ngx_post_event(
  c->read,
  &ngx_posted_next_events);
~~~

它明确想改变：

~~~text
execution generation / phase
~~~

所以必须 retire当前 obligation，

再创建：

~~~text
next-iteration obligation
~~~

## 七十五、这是一种 Rescheduling，而不是 Duplicate Scheduling

不要把它理解成：

~~~text
再加一次 callback
~~~

而是：

~~~text
把当前 pending callback
迁移到更晚 phase
~~~

## 七十六、为什么 SSL 特别需要 Next-tick

SSL layer内部可能仍有：

~~~text
buffered plaintext/ciphertext
~~~

不完全对应：

~~~text
kernel fd readiness
~~~

Runtime需要：

~~~text
再给 SSL state machine一次 progress机会
~~~

但又不希望：

~~~text
当前调用链无限递归/忙循环
~~~

所以：

~~~text
posted_next
~~~

提供：

~~~text
cooperative yield
~~~

## 七十七、`posted_next` 可以看成 Scheduler Yield

当前 handler说：

> **我还可能有工作，但请让我先退出当前 execution slice，下一轮再继续。**

这和：

- coroutine `yield`；
-task reschedule；
-GUI `post()`；

非常类似。

## 七十八、为什么不是 Timer 0ms

也可以想象：

~~~text
add timer(now)
~~~

但那会：

-进入 rbtree；
-走 Timer semantic flags；
-增加 deadline scheduler操作；
-改变 timeout cause语义。

Posted-next更直接表达：

~~~text
next scheduler tick
~~~

而不是：

~~~text
time deadline
~~~

## 七十九、不同 Delay Mechanism 应表达不同语义

~~~text
posted normal
→ later this phase可能执行

posted next
→ next event-loop iteration

timer
→ not before deadline
~~~

不要用一个机制模拟所有延迟。

## 八十、HTTP Auth Delay 为什么同时 Timer + Posted-next

源码：

~~~text
write->delayed = 1
add_timer(auth_delay)
post write to posted_next
~~~

其中 Timer表达：

~~~text
真实 delay deadline
~~~

posted-next表达：

~~~text
再进入一次 event loop
以建立 constant-time processing path
~~~

二者职责不同。

## 八十一、这说明一个 Event 可以同时属于 Timer 与 Posted Queue

`ngx_event_t` 内嵌：

~~~text
timer node
queue node
~~~

正是为了支持：

~~~text
time obligation
+
runnable obligation
~~~

并存。

## 八十二、多 Membership 需要独立 Hook

如果只有一个 intrusive node，

就不可能同时：

~~~text
在 rbtree
和
posted queue
~~~

所以：

~~~text
one hook per independent container
~~~

是 intrusive design的重要原则。

## 八十三、这和 `pipe_t` 的多个 intrusive index slot相同

一个对象能参与多个 scheduler，

每个 scheduler必须有：

~~~text
独立 membership metadata
~~~

## 八十四、为什么 `ngx_event_move_posted_next` 不是直接处理 Handler

因为 next queue的语义是：

~~~text
跨 iteration
然后进入 normal posted phase
~~~

它不想再发明：

~~~text
fourth callback phase
~~~

所以先：

~~~text
normalize into posted_events
~~~

再由统一 processor执行。

## 八十五、这是 Queue Normalization

多种入口：

~~~text
next events
~~~

最终汇入：

~~~text
normal runnable queue
~~~

可以减少 handler executor分支。

## 八十六、Worker Phase 的完整 Runnable 流

~~~text
start iteration
    |
    +-- move posted_next → posted
    |     set timer=0
    |
    v
process backend / epoll
    |
    +-- direct handlers
    |
    +-- post accept events
    |
    +-- post normal events
    v
process posted_accept
    v
release accept mutex
    v
expire timers
    |   handlers may post normal/next
    v
process posted_events
    |   handlers may post normal/next
    v
iteration end
~~~

## 八十七、注意 Normal Posted Handler 可以在同一 Phase 继续扩张 Queue

由于：

~~~text
while !empty
~~~

如果 handler不断 repost normal events，

这一 phase理论上可以持续很久。

## 八十八、所以 `posted_next` 是一个 Fairness Escape Hatch

当 subsystem发现：

~~~text
我仍有工作
但不应该独占当前 loop
~~~

就应使用：

~~~text
next iteration
~~~

而不是：

~~~text
same queue immediate self-repost
~~~

## 八十九、这与 Cooperative Scheduler 的 Preemption Problem 相同

没有硬抢占，

公平性依赖任务自己：

~~~text
bounded work
+
yield/reschedule
~~~

nginx posted-next就是一种显式 yield primitive。

## 九十、如果 Handler 忘记 Yield 会怎样

一个 callback可以：

- 长循环；
-不断 self-post same queue；
-连续处理大量数据。

结果：

~~~text
Timer latency上升
other connection latency上升
accept fairness下降
~~~

所以单线程 Reactor性能依赖：

~~~text
handler discipline
~~~

## 九十一、事件驱动并不自动等于低延迟

如果 callback阻塞：

~~~text
epoll再高效也没用
~~~

Runtime还需要：

- bounded handler work；
- deferred continuation；
-offload blocking tasks。

## 九十二、Posted Queue 正是其中一个控制手段

它不能抢占已经执行的 handler，

但可以帮助设计者：

~~~text
把大工作拆成多个 slices
~~~

## 九十三、`ready=1` 不等于一定成功

Posted-next搬运时：

~~~text
ready=1
available=-1
~~~

handler之后尝试 I/O，

仍可能：

~~~text
EAGAIN
~~~

所以：

~~~text
ready
~~~

更像：

~~~text
runtime says “worth retrying”
~~~

而不是：

~~~text
operation guaranteed to complete
~~~

## 九十四、Readiness Event 是 Re-evaluation Hint

这个原则已经在多个 Runtime反复出现：

- epoll；
-Holscan event；
-libzmq activation；
-Cyber wake；
-nginx posted-next。

共同心智：

> **事件告诉 owner“条件可能已改变”，真正 action仍需重新检查 authoritative state。**

## 九十五、为什么 Posted Queue 不携带 Payload

因为 payload/state已经在：

~~~text
event owner object
~~~

例如 connection buffer、SSL state、request state。

Queue只携带：

~~~text
which event should execute
~~~

这也是 intrusive pointer queue足够的原因。

## 九十六、Queue Node 是 Scheduling Token，不是 Message

posted queue与消息队列不同：

~~~text
message queue:
  item carries data

posted queue:
  item identifies runnable state machine
~~~

## 九十七、这类设计适合 Stateful Protocol Engine

HTTP/SSL/stream connection本来就有：

~~~text
persistent object state
~~~

无需每次 callback都复制：

~~~text
continuation payload
~~~

## 九十八、但不适合 Stateless Worker Pool Job Queue

如果每个 work item本身是：

~~~text
独立 task data
~~~

则应使用真正：

~~~text
task/message object
~~~

而不是只 post一个 shared event bit。

## 九十九、为什么 Posted Queue 是 Doubly Linked

除了 O(1) remove，

还允许：

~~~text
event在执行前被任意 lifecycle path取消
~~~

这对：

- connection close；
- SSL phase migration；
- QUIC cleanup；

都很重要。

## 一百、取消不是“标记以后跳过”

nginx直接：

~~~text
unlink now
~~~

防止 queue继续持有 embedded-node reachability。

## 一百零一、这是 Eager Retirement

如果对象即将回收，

最好：

~~~text
立刻移除 owner-controlled scheduler membership
~~~

而不是：

~~~text
让 stale item留在 queue
执行时再判断
~~~

## 一百零二、为什么 Epoll 不能同样 Eager 完全清理

kernel ready list不是：

~~~text
用户态直接可遍历 queue
~~~

所以需要：

~~~text
instance generation
~~~

做 delayed stale detection。

不同 source采用不同 retirement策略。

## 一百零三、Connection Close 的完整 Scheduler Detach

固定源码可概括：

~~~text
if read timer_set
  del timer

if write timer_set
  del timer

unregister backend events

if read posted
  delete posted

if write posted
  delete posted

mark closed

remove reusable membership

free connection slot
~~~

## 一百零四、为什么顺序中 Timer 在 Backend 前、Posted 在 Backend 后

具体顺序受到：

~~~text
backend close semantics
~~~

影响。

核心 invariant不是机械固定全局顺序，

而是：

> **在 slot/storage可复用前，所有 owner-controlled future execution memberships都必须退出；外部 backend stale item则必须有 generation验证。**

## 一百零五、Posted Event Queue 本身不做 Generation Check

因为正确 lifecycle保证：

~~~text
slot reuse前
event已被 unlink
~~~

所以 normal posted processing可以直接：

~~~text
ngx_queue_data
→ handler
~~~

不用：

~~~text
instance comparison
~~~

## 一百零六、这使 Hot Path 更便宜

Prevent stale membership的成本发生在：

~~~text
close/cancel path
~~~

而正常 posted execution不需要额外 generation validation。

这是一个很好的：

~~~text
cost placement
~~~

选择。

## 一百零七、Callback Cancellation 与 Callback Quiescence

由于 worker单线程：

~~~text
如果 callback当前正在执行
~~~

close通常就在：

~~~text
同一调用栈/owner context
~~~

不会另一个线程同时删除 queue node。

所以：

~~~text
delete pending membership
~~~

足以解决“未来 callback”。

无需复杂：

~~~text
in-flight callback counter
~~~

## 一百零八、这是 Owner-thread 模型的巨大简化

在多线程 callback runtime中：

~~~text
unregister
~~~

往往必须：

- retire；
- wait in-flight；
- reclaim。

nginx worker local callback则很多时候自然串行化。

## 一百零九、但 Process/Thread 外部事件仍可能异步

如：

- AIO；
-thread pool；
-kernel readiness；
-signals。

它们通过特定 runtime机制回 worker，

而不是直接共享 posted queue。

这保持了局部 owner invariant。

## 一百一十、Posted Queue 还是一种 Phase Barrier

Accept queue：

~~~text
barrier before mutex release
~~~

Normal queue：

~~~text
barrier after timer expiration
~~~

Next queue：

~~~text
barrier across whole event-loop iteration
~~~

所以三条 queue真正表达的是：

~~~text
temporal placement
~~~

## 一百一十一、Phase Scheduling 比 Priority Scheduling 更准确

它们不是：

~~~text
priority 0 / 1 / 2
~~~

因为 `posted_next` 不是“低优先级”。

它有更强语义：

~~~text
not before next iteration
~~~

Accept queue也不是单纯“高优先级”。

它与：

~~~text
accept mutex ownership lifetime
~~~

绑定。

## 一百一十二、因此不能随便合并成 Priority Queue

一个整数 priority无法自然表达：

- current iteration vs next；
- before/after mutex release；
- before/after timer expiry。

Phase queue更贴合执行协议。

## 一百一十三、这是一种 Temporal Type System

可以把三条 queue理解成：

~~~text
event belongs to a temporal class
~~~

而不是：

~~~text
event has a numeric rank
~~~

## 一百一十四、机器人 Executor 也很适合 Phase Queue

例如一个控制周期：

~~~text
Phase 1
read sensors

Phase 2
update estimator

Phase 3
compute control

Phase 4
publish actuator commands

Phase next
deferred maintenance
~~~

不同 runnable event可以进入：

~~~text
phase-specific intrusive queue
~~~

而不是一个大 priority heap。

## 一百一十五、Phase Queue 的优势

- 执行顺序清晰；
-状态 invariant更容易保证；
-少 priority inversion解释；
-容易设置 barrier。

## 一百一十六、代价

- phase数目增加会复杂；
-跨 phase迁移要显式；
-不适合任意 deadline优先级。

所以 Timer仍然需要独立 rbtree。

## 一百一十七、为什么 Timer 与 Posted Queue 不合并

Timer回答：

~~~text
not before time T
~~~

Posted回答：

~~~text
already runnable, but execute in phase P
~~~

一个是：

~~~text
time eligibility
~~~

一个是：

~~~text
execution ordering
~~~

两个维度正交。

## 一百一十八、这也是成熟 Scheduler 的常见分层

~~~text
wait structure
→ determine eligibility

ready queue
→ choose execution order
~~~

OS scheduler也有类似：

- sleeping/wait queues；
-runnable queues。

nginx是事件 runtime版本。

## 一百一十九、Timer Expire 后直接 Handler，没有自动进 Posted Queue

nginx当前 Timer实现：

~~~text
expire
→ handler directly
~~~

所以 Timer phase本身就是：

~~~text
execution phase
~~~

而不是：

~~~text
eligibility only
~~~

这是当前源码具体选择。

## 一百二十、Handler 如果想转到 Normal Posted Phase

可以：

~~~text
ngx_post_event(...)
~~~

从 Timer callback显式把后续工作：

~~~text
defer
~~~

到普通 phase。

## 一百二十一、这提供显式 Control-flow Composition

~~~text
Timer
→ handler
→ post event
→ later handler
~~~

类似：

~~~text
deadline continuation
~~~

## 一百二十二、为什么 Posted Queue 不存 Handler Snapshot

Queue里只存：

~~~text
event pointer
~~~

执行时取：

~~~text
ev->handler
~~~

这意味着：

~~~text
post时的 handler
~~~

与：

~~~text
执行时 handler
~~~

理论上可以不同。

## 一百二十三、这是 Dynamic Dispatch State

协议代码可以：

~~~text
change event->handler
~~~

后再由已 posted event执行：

~~~text
current handler
~~~

而不是 post时绑定 closure。

## 一百二十四、这很强，也很危险

优点：

~~~text
状态机切换便宜
~~~

风险：

~~~text
handler pointer修改与 posted obligation顺序
必须非常清楚
~~~

否则 event可能进入：

~~~text
意外 state handler
~~~

## 一百二十五、因此 Event Object 本身就是 State-machine Control Block

它不是单纯：

~~~text
callback wrapper
~~~

而是：

- readiness；
-handler；
-timer；
-posted membership；
-lifecycle flags。

## 一百二十六、nginx 的 C 风格 Runtime 很依赖这种 Stable Control Block

这是为什么：

~~~text
preallocated event arrays
~~~

与：

~~~text
intrusive containers
~~~

结合得如此紧密。

## 一百二十七、Stable Address 减少了什么

- posted queue node无需 owner allocation；
-timer node无需 allocation；
-epoll data.ptr可直接指 connection；
-handler state可就地改。

代价则是：

~~~text
logical generation必须单独管理
~~~

## 一百二十八、Posted Queue 与 Instance Generation 分工

Posted queue：

~~~text
storage owner完全可控
→ close前 unlink
~~~

epoll：

~~~text
external queue可能残留
→ return时 validate generation
~~~

Timer：

~~~text
owner tree完全可控
→ close前 delete
~~~

三条 future execution source形成统一生命周期模型。

## 一百二十九、一个 Connection 的 Future Execution Graph

~~~text
ngx_connection_t
    |
    +-- read event
    |     +-- epoll registration
    |     +-- timer membership
    |     +-- posted membership
    |
    +-- write event
          +-- epoll registration
          +-- timer membership
          +-- posted membership
~~~

close不是：

~~~text
close(fd)
~~~

而是：

~~~text
cut every future-execution edge
~~~

## 一百三十、这就是 Quiescence 的图论视角

一个对象可安全复用/回收，

需要：

~~~text
no scheduler/registry can newly reach it
~~~

以及外部残留 source有：

~~~text
generation validation
~~~

## 一百三十一、为什么 Posted Queue 很适合做软件 Interrupt Bottom Half

可以类比：

~~~text
top half:
  detect minimal event
  update state
  post

bottom half:
  owner loop later runs heavier handler
~~~

虽然 nginx不是内核 interrupt机制，

设计思想相似。

## 一百三十二、机器人设备 Runtime 可以怎么用

硬件 callback / fd readiness：

~~~text
read sensor fd
update ring cursor
post sensor event
return quickly
~~~

owner loop later：

~~~text
parse packet
update state estimator
~~~

这样可以缩短：

~~~text
readiness dispatch critical section
~~~

## 一百三十三、但必须确认 Coalescing 是否安全

传感器如果：

~~~text
ring buffer保存所有 samples
~~~

post一个“有数据” event足够。

handler：

~~~text
drain ring
~~~

如果硬件 callback本身只有：

~~~text
notification count
~~~

却没有 queue/state保存 samples，

coalescing就会丢信息。

## 一百三十四、Notification 与 Payload 必须分离

安全模式：

~~~text
payload/state
→ authoritative storage

posted event
→ wake/runnable hint
~~~

这和 libzmq signaler、eventfd、condition notification是一致原则。

## 一百三十五、为什么 Queue 本身不需要 Backpressure

同一个 event最多一个 node，

所以重复通知不会：

~~~text
无限增加 queue length
~~~

这是天然的：

~~~text
per-event coalescing bound
~~~

## 一百三十六、Queue Length 上界接近 Event Object 数量

不是 notification数量。

这对：

~~~text
memory boundedness
~~~

很有价值。

## 一百三十七、但不同 Event 仍可大量堆积

如果 worker有：

~~~text
100k connections
~~~

理论上很多 event都可 posted。

所以：

~~~text
coalescing
~~~

不等于：

~~~text
全局 posted queue很小
~~~

只是避免同一个 event重复膨胀。

## 一百三十八、Fairness 仍依赖 Handler Work

FIFO只能保证：

~~~text
event entry ordering
~~~

如果某 handler一次做：

~~~text
巨大工作
~~~

后面的 event仍会延迟。

## 一百三十九、为什么 Next-tick 很有价值

它提供：

~~~text
cooperative work slicing
~~~

而不需要：

- thread preemption；
-coroutine runtime；
-task budget accounting。

## 一百四十、但 Next-tick 不是严格 CPU Time Slice

Runtime不会自动：

~~~text
执行 1ms后把 handler切走
~~~

开发者必须主动：

~~~text
post next and return
~~~

## 一百四十一、这是一种 Cooperative Preemption

和 Seastar：

~~~text
need_preempt / yield
~~~

思想相似，

实现层不同。

## 一百四十二、为什么 `timer=0` 是 Next-tick 的关键一半

只 splice queue还不够。

必须同时：

~~~text
prevent blocking sleep
~~~

否则 next work可能：

~~~text
ready but dormant
~~~

所以 next-tick协议完整是：

~~~text
move to runnable queue
+
force zero-time backend poll
~~~

## 一百四十三、这与 Lost Wakeup 思想类似

如果你只设置：

~~~text
software runnable state
~~~

却没有保证：

~~~text
event loop会被及时唤醒/不睡
~~~

就可能延迟 execution。

nginx通过：

~~~text
timer=0
~~~

避免这一点。

## 一百四十四、为什么不完全跳过 `ngx_process_events`

即便 timer=0，

仍然调用 backend：

~~~text
nonblocking poll
~~~

这样可以顺便收集：

~~~text
此刻已 ready 的 I/O
~~~

再统一进入后续 phases。

## 一百四十五、这保持 Event Loop Shape 稳定

不是：

~~~text
if posted-next then绕过 backend
~~~

而是：

~~~text
same loop
different timeout
~~~

减少特殊 control flow。

## 一百四十六、Runtime 常见技巧：调整 Wait Policy 而非重写 Loop

例如：

~~~text
pending internal work
→ timeout=0

no internal work
→ timeout=nearest deadline

no deadline
→ infinite
~~~

同一个 wait backend覆盖多种状态。

## 一百四十七、Posted Queue 与 Accept Mutex 的关系

当 accept mutex held：

~~~text
flags |= NGX_POST_EVENTS
~~~

epoll发现 accept event：

~~~text
post accept queue
~~~

worker回到 core后：

~~~text
process accept queue
→ release mutex
~~~

这样：

~~~text
mutex lifetime
~~~

由 worker phase显式控制。

## 一百四十八、为什么不在 epoll backend中直接 Unlock

因为 backend应该专注：

~~~text
event detection
~~~

accept mutex属于：

~~~text
worker-core scheduling policy
~~~

分层更清楚。

## 一百四十九、Posted Accept Queue 是 Decoupling Layer

它让：

~~~text
backend:
  “listen fd ready”

worker core:
  “在持锁 phase执行 accept”
~~~

互不侵入。

## 一百五十、这和 Driver Bottom-half 很像

低层只：

~~~text
record readiness
~~~

高层：

~~~text
在正确 policy context执行
~~~

## 一百五十一、为什么 Posted Queue 不是 Thread Pool

handler仍然：

~~~text
在同一个 worker thread执行
~~~

defer不会增加：

~~~text
CPU parallelism
~~~

它改变的是：

~~~text
temporal ordering / reentrancy
~~~

## 一百五十二、不要把“异步”自动等同“并行”

nginx大量 async flow只是：

~~~text
same thread
later phase
~~~

这能避免锁，

但不能加速 CPU-bound callback。

## 一百五十三、CPU-heavy Work 需要别的 Mechanism

例如：

- thread pool；
-process；
-offload service。

Posted queue只适合：

~~~text
cooperative event-state continuation
~~~

## 一百五十四、Posted Event 的 Owner 是谁

不是 queue node。

真正 owner通常仍是：

~~~text
connection/request/module object
~~~

Posted queue只持：

~~~text
intrusive reachability
~~~

所以：

~~~text
queue membership
~~~

不能被误认为：

~~~text
memory ownership reference count
~~~

## 一百五十五、这就是为什么 Close 必须显式 Unlink

没有：

~~~text
shared_ptr
~~~

自动延长 event storage。

Intrusive container的安全性完全依赖：

~~~text
lifecycle protocol
~~~

## 一百五十六、Intrusive Structure 交换了什么

获得：

- 零 node allocation；
-O(1) remove；
-stable object locality。

付出：

- 手工 membership纪律；
-对象不能在 membership期间搬家；
-close前必须 remove；
-debug难度更高。

## 一百五十七、什么时候值得这么做

高频 Runtime bookkeeping：

~~~text
yes
~~~

普通业务 application list：

~~~text
未必
~~~

需要根据：

- throughput；
-lifetime discipline；
-debug成本；

决定。

## 一百五十八、三条 Posted Queue 可以概括成一个 Temporal State Machine

Event可能经历：

~~~text
NOT_POSTED
    |
    | post normal
    v
POSTED_CURRENT
    |
    | process/delete
    v
EXECUTING
    |
    +-- repost normal → POSTED_CURRENT
    |
    +-- post next → POSTED_NEXT
    |
    +-- no repost → NOT_POSTED
~~~

另一路：

~~~text
NOT_POSTED
    |
    | post next
    v
POSTED_NEXT
    |
    | next iteration move
    v
POSTED_CURRENT
~~~

## 一百五十九、Phase Migration 需要 Delete + Repost

如果：

~~~text
POSTED_CURRENT
~~~

想变：

~~~text
POSTED_NEXT
~~~

必须：

~~~text
delete current membership
→ post next
~~~

因为只有一个 intrusive queue hook。

## 一百六十、这比一个 `enum phase` 更高效但更隐式

phase实际由：

~~~text
node在哪条 queue
~~~

编码。

对象只保存：

~~~text
posted bool
~~~

不保存：

~~~text
posted_queue_id
~~~

## 一百六十一、因此 API 无法直接问“我在哪条 Posted Queue”

要改变 queue：

~~~text
caller自己知道当前上下文
~~~

或先删除。

这是设计简洁换来的约束。

## 一百六十二、为什么不保存 Queue Pointer

每个 event多存一个 pointer会：

-增大 `ngx_event_t`；
-影响所有 connections；
-热结构 cache footprint增加。

nginx选择：

~~~text
不为少数迁移操作永久增加字段
~~~

## 一百六十三、又一次 Hot-path Cost Placement

rare path：

~~~text
delete + repost
~~~

换：

~~~text
every event少一个 queue-id pointer
~~~

成熟 Runtime经常做这种取舍。

## 一百六十四、Event Struct Size 非常重要

worker预分配：

~~~text
2 × worker_connections
~~~

个 read/write event。

每增加：

~~~text
8 bytes
~~~

都乘以大量 connection slot。

所以：

~~~text
small flags + intrusive hooks
~~~

很有价值。

## 一百六十五、Bitfield 不是“抠几字节”这么简单

它直接影响：

- cache lines；
-working set；
-preallocated memory footprint。

对数十万 connections，

差异会累积。

## 一百六十六、Posted Queue 的程序设计启发

设计一个 owner-thread Runtime时，可以拆：

~~~text
Detector
→ marks state

Scheduler
→ places runnable token into phase

Executor
→ removes membership, then callback
~~~

不要把三层揉成：

~~~text
detector直接 arbitrary callback
~~~

## 一百六十七、为什么这会更好测试

可以分别验证：

- readiness state是否正确；
-posted membership是否正确；
-phase ordering是否正确；
-handler state machine是否正确。

## 一百六十八、为什么更容易 Shutdown

因为 future execution obligation有显式：

~~~text
posted membership
~~~

close时可以：

~~~text
remove
~~~

而不是寻找：

~~~text
隐藏在递归调用栈/匿名 closure里的 callback
~~~

## 一百六十九、但匿名异步任务仍需要其他 Tracking

Posted queue只解决：

~~~text
ngx_event_t-based continuations
~~~

模块若创建：

- thread pool task；
-AIO；
-subrequest；
-external callback；

仍需独立 lifecycle tracking。

## 一百七十、不要把一个 Queue 当整个 Runtime 的 Quiescence Proof

真正对象可能有：

~~~text
many future-execution edges
~~~

每条都要被 retire/drain。

## 一百七十一、与 Timer 章节合起来的统一模型

~~~text
WAITING
  |
  +-- I/O readiness
  |      ↓
  |   READY
  |
  +-- Timer due
         ↓
      TIMEDOUT

READY
  |
  +-- direct handler
  |
  +-- posted current
  |
  +-- posted next
~~~

Timer决定：

~~~text
time eligibility
~~~

Posted决定：

~~~text
execution placement
~~~

## 一百七十二、与 Connection Pool 合起来的统一模型

~~~text
connection slot
    |
    +-- kernel reachability
    |     generation validated
    |
    +-- timer reachability
    |     explicit delete
    |
    +-- posted reachability
    |     explicit unlink
    |
    +-- reusable membership
          explicit remove
~~~

只有这些边全部处理好，

slot才能安全：

~~~text
new generation
~~~

## 一百七十三、源码作者必须守住的核心不变量

第一：

> **OS readiness 与 handler execution 是两个层次；posted queue 允许 worker-core policy重新安排 runnable event 的执行 phase。**

第二：

> **`ngx_event_t::posted` 表示当前 intrusive posted membership，同一个 event因为只有一个 queue hook而不能同时属于多条 posted queue。**

第三：

> **重复 `ngx_post_event()` 不累计 notification count，只保证 event至少有一个 pending execution；这种 coalescing要求 authoritative state保存在 queue之外。**

第四：

> **`ngx_event_process_posted()` 必须先 unlink并清 `posted`，再调用 handler，使 callback能够安全 close、repost或迁移 phase而不 double-remove。**

第五：

> **向普通 `ngx_posted_events` self-repost可能在同一 posted phase再次执行；真正的 next-iteration barrier必须使用 `ngx_posted_next_events`。**

第六：

> **next queue在下一次 worker iteration开头被转入 normal posted queue，同时将 backend timeout设为 0，既建立 iteration barrier又保证不会因无 I/O而睡眠饿死。**

第七：

> **从已有 posted phase迁移到 posted-next需要先 delete再 repost；已 posted event直接调用 `ngx_post_event` 不会改变 queue membership。**

第八：

> **accept、normal、next三条 queue表达 temporal phase而不是简单数值 priority；accept queue还与 accept-mutex ownership lifetime绑定。**

第九：

> **connection slot回池前必须退出 posted membership，因为 intrusive queue本身就是未来 callback reachability。**

第十：

> **Timer node、posted node、backend registration是独立 execution source，物理 reuse前必须分别 retire；外部 kernel stale event再由 instance generation检测。**

第十一：

> **posted-next 是 cooperative yield/reschedule primitive，不是硬抢占；handler仍需主动控制工作量。**

第十二：

> **intrusive queue节省 allocation与 removal成本，但把安全责任转移给 explicit lifecycle protocol。**

## 一百七十四、最终心智模型

~~~text
                KERNEL / SOFTWARE STATE
                         |
                         v
                    event ready
                         |
              update ngx_event_t state
                         |
              +----------+----------+
              |                     |
           immediate              posted
           handler                  |
                         +-----------+-----------+
                         |           |           |
                     accept       normal       next
                       phase        phase      iteration
                         |           |           |
                         |           |      next loop start
                         |           |           |
                         |           |      ready=1
                         |           |      timeout=0
                         |           |           |
                         |           +<----------+
                         |
                  before mutex release

posted executor:
    queue head
        ↓
    posted=0
    unlink node
        ↓
    handler
        |
        +-- finish
        +-- repost normal
        +-- repost next

connection close:
    delete timer
    unregister backend
    delete posted
    remove reusable
    free slot
~~~

如果只记一个结论：

> **nginx 的 posted event 不是“epoll callback 的临时队列”，而是 worker 内部的第二级 cooperative scheduler。`posted` bit把重复 readiness压成一个 runnable obligation，intrusive queue把调度成本压到 O(1)，handler 前先解除 membership保证重入安全，而 `posted_next_events + timer=0` 又提供真正的 next-tick yield。配合 accept phase、Timer 与 connection close，nginx 把“发现事件”和“什么时候执行事件”明确拆成了两层。**
