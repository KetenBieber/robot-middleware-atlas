# SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Seastar 的 shard-per-core 架构很容易被一句话简化成：

> 每个 Core 跑一个 Reactor。

但这只描述了线程布局，没有解释真正决定并发模型的部分：

> **如果 mutable state 只允许 owner shard 修改，那么其他 shard 想操作它时，计算怎样跨 Core 移动？请求怎样返回？谁持有 work item？队列满了怎么办？目标 Reactor 已经睡眠时怎样避免 lost wakeup？**

这些问题集中在：

~~~text
smp::submit_to()
        ↓
smp_message_queue
        ↓
request SPSC
        ↓
target Reactor task
        ↓
completion SPSC
        ↓
origin promise
~~~

所以 `smp_message_queue` 不是一个普通“跨线程队列”。

它更准确地说是：

> **一个 pair-wise cross-shard RPC transport：origin shard 负责创建和最终销毁 work item，target shard 负责执行 computation，request/completion 各走一条单向 SPSC channel，service-group semaphore 则限制完整 round-trip outstanding。**

---

# 一、先从 Shard-per-core 的真正约束出发

假设有：

~~~text
Shard 0
Shard 1
Shard 2
Shard 3
~~~

每个 shard 都有自己的：

- Reactor；
- scheduling groups；
- task queues；
- timers；
- allocator；
- socket/network state；
- service instance。

核心设计目标不是：

~~~text
让 4 个线程同时访问所有对象
~~~

而是：

~~~text
让绝大多数 mutable object
只被一个 shard 直接修改
~~~

---

## 1. 为什么这样做

如果对象被所有 Core 直接共享，

通常需要：

- mutex；
- atomic；
- cache-line bouncing；
- concurrent container；
- complex reclamation。

Shard-per-core 先从架构上减少这些问题：

~~~text
state ownership
→ one shard
~~~

然后跨核访问改成：

~~~text
move computation
to owner shard
~~~

---

# 二、`submit_to()` 不是“远程函数直接调用”

API：

~~~cpp
smp::submit_to(target, func)
~~~

表面像：

~~~text
在另一个 Core 调一个函数
~~~

实际至少要完成：

~~~text
1. 创建 work_item
2. 获得 cross-shard credit
3. 把 request 发布给 target
4. 唤醒 target Reactor
5. target 把 work_item 转成普通 task
6. target 执行 func
7. 保存 result / exception
8. 把 completion 发回 origin
9. origin resolve promise
10. origin 归还 credit
11. origin delete work_item
~~~

这是完整 RPC。

---

# 三、Local submit 与 Remote submit 是两条不同路径

源码：

~~~cpp
if (t == this_shard_id()) {
    ...
    return futurize<ret_type>::invoke(...);
} else {
    return _qs[t][this_shard_id()]
        .submit(t, options, ...);
}
~~~

---

## 2. 本地调用为什么不走 Queue

如果 target 就是自己：

~~~text
origin == target
~~~

则不存在：

- cache ownership transfer；
- cross-core wakeup；
- completion 回源；
- service-group remote credit。

所以直接执行更便宜。

---

## 3. 这是一条很重要的 API 设计原则

同一个高层 API：

~~~text
submit_to(shard, func)
~~~

可以隐藏：

~~~text
local fast path
+
remote protocol path
~~~

但二者的语义必须一致：

~~~text
最终都返回 future
最终都把异常放进 future
~~~

---

# 四、为什么 Local Path 还专门处理 Func Lifetime

源码注释明确说：

~~~text
temporary func 的移动
以及最终 destruction
都发生在 calling core
~~~

对于同步立即返回的 callable：

~~~text
可以直接 invoke
~~~

但如果 callable 返回 future，

它的 continuation 可能延迟完成。

---

## 4. Rvalue Func 为什么要被额外保存

源码：

~~~cpp
auto w =
  std::make_unique<std::decay_t<Func>>(
      std::move(func));

auto ret =
  futurize<ret_type>::invoke(*w);

return ret.finally(
  [w = std::move(w)] {});
~~~

目的：

~~~text
func object
必须活到它产生的 future 完成
~~~

---

# 五、Execution Ownership 不只是“在哪运行”

还包括：

~~~text
callable 在哪销毁
result 在哪兑现
work item 在哪 delete
~~~

Seastar 在这些地方都显式区分 origin 与 target。

---

# 六、Remote submit 从 `smp_message_queue::submit()` 开始

~~~cpp
auto wi =
  std::make_unique<
    async_work_item<Func>>(
      *this,
      options.service_group,
      std::forward<Func>(func));

auto fut = wi->get_future();

submit_item(
  target,
  options.timeout,
  std::move(wi));

return fut;
~~~

---

# 七、为什么先拿 Future，再把 Work Item 送走

因为：

~~~text
promise
~~~

被保存于 work item 内，

调用者需要先获得与它配套的：

~~~text
future
~~~

然后 work item 的物理执行权才转移。

---

# 八、`work_item` 为什么继承 `task`

源码：

~~~cpp
struct work_item : public task
~~~

这是整个设计最关键的一层。

跨核 request 到达 target 后，

不是在 SMP polling loop 里直接执行用户函数。

而是：

~~~cpp
void work_item::process()
{
    schedule(this);
}
~~~

---

# 九、Queue Arrival 与 User Execution 被分成两阶段

~~~text
cross-core queue
        ↓
arrival detected
        ↓
schedule(task)
        ↓
target Reactor scheduler
        ↓
run_and_dispose()
~~~

---

# 十、为什么不在 `process_incoming()` 里直接跑 Func

如果直接执行：

~~~text
poll SMP queue
→ user function
~~~

那么用户 computation 会绕过：

- scheduling group；
- vruntime；
- preemption point；
- Reactor task accounting。

这会破坏 Seastar 的 CPU scheduling policy。

---

# 十一、SMP Queue 只负责“把 Task 引入 Owner Reactor”

所以：

> **message transport 与 CPU scheduler 分层。**

这与很多成熟 Runtime 相同：

~~~text
I/O readiness
≠
handler execution policy
~~~

---

# 十二、每对 Shard 之间为什么天然是 SPSC

假设一条方向：

~~~text
Shard A → Shard B
~~~

对于 A→B 的 request queue：

~~~text
producer = A
consumer = B
~~~

只有一个 producer 和一个 consumer。

---

# 十三、所以没有必要上 MPMC

源码：

~~~cpp
using lf_queue_base =
  boost::lockfree::spsc_queue<
      work_item*,
      boost::lockfree::capacity<128>>;
~~~

容量：

~~~text
128 pointer slots
~~~

---

# 十四、架构先把并发拓扑简化

不是：

~~~text
先选最强的并发容器
~~~

而是：

~~~text
先把所有通信拆成 pair-wise channels
→ 每条 channel 天然 SPSC
→ 再使用最简单结构
~~~

---

# 十五、N 个 Shard 并不意味着一个 N-way MPMC

逻辑上：

~~~text
N shards
~~~

可以构成：

~~~text
N × N pair-wise channel matrix
~~~

每个有向边仍然是：

~~~text
one producer
one consumer
~~~

---

# 十六、`_qs[target][origin]` 为什么看起来反直觉

remote submit：

~~~cpp
_qs[t][this_shard_id()]
~~~

可以理解成：

~~~text
queue object
代表 origin → target 这条 pair
~~~

其两个内部方向：

~~~text
_pending
origin → target

_completed
target → origin
~~~

---

# 十七、一个 `smp_message_queue` 实际包含两条 SPSC

构造：

~~~cpp
smp_message_queue(
  reactor* from,
  reactor* to)
    : _pending(to)
    , _completed(from)
{}
~~~

---

# 十八、为什么 Request 和 Completion 分两条 Queue

一次 remote call 本质是：

~~~text
request
+
response
~~~

如果把两种方向都放同一个并发结构，

会变成双 producer/consumer 关系。

拆成两条：

~~~text
origin --pending--> target

origin <--completed-- target
~~~

每条都保持 SPSC。

---

# 十九、这叫“方向分解”

一个双向协议：

~~~text
A ↔ B
~~~

可以实现成：

~~~text
A → B
+
B → A
~~~

两个单向 ownership channel。

---

# 二十、和 libzmq Pipe 的结构很像

libzmq：

~~~text
two ypipe
→ one bidirectional Pipe pair
~~~

Seastar：

~~~text
pending SPSC
+
completed SPSC
→ one cross-shard RPC queue
~~~

共同思想：

> **双向逻辑不一定需要双向并发容器。**

---

# 二十一、为什么还要有 Local FIFO

发送端不是每次 submit 都直接：

~~~text
push shared SPSC ring
~~~

而是先：

~~~cpp
std::deque<work_item*>
    pending_fifo;
~~~

---

# 二十二、Remote Submit 的第一站其实是 Origin-local Memory

~~~text
submit_to
    ↓
service-group admission
    ↓
local pending_fifo
~~~

只有达到：

~~~text
batch_size = 16
~~~

才调用：

~~~text
move_pending()
~~~

---

# 二十三、Local FIFO 的目的

尽量把：

~~~text
频繁 submit
~~~

留在：

~~~text
origin core local cache
~~~

然后批量触碰：

~~~text
cross-core shared cache lines
~~~

---

# 二十四、Batching 优化的是 Cache Coherence 成本

每次跨核共享 ring 操作可能涉及：

- head/tail cache line；
- memory ordering；
- remote cache invalidation；
- wakeup 判断。

如果每 request 一次：

~~~text
local code
→ shared ring
~~~

跨核协调频率很高。

---

# 二十五、Batch Size 固定为 16

~~~cpp
static constexpr size_t
batch_size = 16;
~~~

不是说：

~~~text
一次必须刚好发送 16 个
~~~

而是：

~~~text
pending_fifo >= 16
→ opportunistically flush
~~~

---

# 二十六、不够 16 个会不会永远不发

不会。

Reactor 的 SMP poll path 会：

~~~text
flush_request_batch()
~~~

把未满 batch 的本地 FIFO 推出去。

---

# 二十七、所以 Batch 有两个触发条件

~~~text
size trigger
→ pending >= 16

poll trigger
→ Reactor cycle flush
~~~

---

# 二十八、这是 Latency / Throughput 折中

如果只等 size：

~~~text
低负载请求可能长期滞留
~~~

如果每条立即 flush：

~~~text
高负载 coherence 开销变大
~~~

所以：

~~~text
threshold
+
periodic/event-loop flush
~~~

兼顾两者。

---

# 二十九、`move_pending()` 做什么

核心：

~~~cpp
auto begin =
    pending_fifo.cbegin();

auto end =
    pending_fifo.cend();

end =
    _pending.push(begin, end);
~~~

SPSC ring 尽可能接受一段。

---

# 三十、Ring 可能一次放不完

如果共享 ring 当前只剩部分容量：

~~~text
push(begin,end)
~~~

返回：

~~~text
实际推进到的新 iterator
~~~

所以 local FIFO 中未发布部分继续保留。

---

# 三十一、为什么不能“push 失败就丢”

work item 已经：

- 获得 service-group credit；
- 拥有 promise；
- 代表调用者 future。

丢掉意味着：

~~~text
future 永远不完成
~~~

所以它必须留在 origin local backlog。

---

# 三十二、成功发布后做什么

~~~cpp
_pending.maybe_wakeup();

pending_fifo.erase(
    begin,
    published_end);
~~~

顺序非常重要：

~~~text
publish first
→ wake target
→ remove local ownership record
~~~

---

# 三十三、Queue Publication 与 Wakeup 是两个不同问题

只把 pointer 写进 SPSC：

~~~text
data visible
~~~

不代表：

~~~text
target CPU will execute
~~~

如果 target Reactor 已经睡眠，

还需要：

~~~text
wakeup protocol
~~~

---

# 三十四、`maybe_wakeup()` 为什么不是无条件 Eventfd Write

源码：

~~~cpp
remote->wakeup();
~~~

真正 `reactor::wakeup()` 先：

~~~cpp
if (!_sleeping.load(...))
    return;
~~~

如果目标没睡：

~~~text
不做 syscall
~~~

---

# 三十五、这避免把每个跨核 request 都变成 Kernel Wakeup

高负载时 Reactor 本来就在跑：

~~~text
queue publication 足够
~~~

只有低负载进入 sleep 时才需要：

~~~text
eventfd signal
~~~

---

# 三十六、Sleep/Wakeup 最大的风险是什么

经典 Lost Wakeup：

~~~text
Target:
poll queue empty

Origin:
push request

Target:
go sleep

Origin:
thought target awake
does not wake
~~~

结果：

~~~text
queue nonempty
CPU asleep
~~~

---

# 三十七、Seastar 的 Sleep Protocol

进入 interrupt mode：

~~~cpp
_sleeping.store(true,
    memory_order_relaxed);

try_systemwide_memory_barrier();

if (poll()) {
    _sleeping.store(false, ...);
    return false;
}
~~~

---

# 三十八、为什么设置 Sleeping 后还要再 Poll 一次

这是典型：

~~~text
prepare-to-sleep
→ final condition check
→ commit sleep
~~~

---

# 三十九、如果 Request 恰好在准备睡眠期间到达

最终 `poll()` 能观察到：

~~~text
queue nonempty
~~~

于是：

~~~text
cancel sleep
~~~

---

# 四十、Producer 侧 `maybe_wakeup()` 的注释非常关键

源码：

~~~text
Called after lf_queue_base::push()

This is read-after-write,
which wants memory_order_seq_cst,
but barrier is inserted using
systemwide_memory_barrier()
~~~

并保留：

~~~cpp
atomic_signal_fence(
    memory_order_seq_cst);
~~~

---

# 四十一、这里不是普通 Atomic Flag 教科书

它在解决：

~~~text
shared queue state
+
remote sleeping flag
+
kernel eventfd wakeup
~~~

三者之间的 ordering。

---

# 四十二、为什么不能只说“用了 SPSC，所以线程安全”

SPSC 只证明：

~~~text
pointer publication
~~~

不证明：

~~~text
sleeping consumer 一定被唤醒
~~~

因此：

> **Queue Correctness 与 Wait Protocol Correctness 是两套证明。**

---

# 四十三、libzmq Mailbox 也是同一个主题

libzmq：

~~~text
ypipe passive marker
+
signaler
~~~

Seastar：

~~~text
SPSC ring
+
_sleeping
+
systemwide barrier
+
eventfd
~~~

机制不同，问题相同：

> **不能在“检查为空”与“真正睡眠”之间丢掉生产者事件。**

---

# 四十四、Target 收到 Request 后为什么不立即运行

`process_incoming()`：

~~~cpp
process_queue(
  _pending,
  [] (work_item* wi) {
      wi->process();
  });
~~~

而：

~~~cpp
work_item::process()
{
    schedule(this);
}
~~~

---

# 四十五、所以 Request Arrival 只产生 Runnable Task

~~~text
SMP ingress
→ schedule task
~~~

真正 CPU 时间仍由：

~~~text
target Reactor scheduler
~~~

决定。

---

# 四十六、Work Item 继承哪个 Scheduling Group

构造：

~~~cpp
task(
  current_scheduling_group())
~~~

也就是说：

~~~text
work item task identity
~~~

会携带 origin-side 当前 scheduling group。

---

# 四十七、同时还有 SMP Service Group

`work_item` 还保存：

~~~cpp
smp_service_group ssg;
~~~

这不是同一个东西。

---

# 四十八、Scheduling Group 与 SMP Service Group 分工

可以粗略区分：

~~~text
Scheduling Group
→ CPU scheduling / task fairness

SMP Service Group
→ cross-shard outstanding admission
~~~

---

# 四十九、一个是 CPU 时间策略，一个是网络化 Credit

即使都叫 group，

也不能混淆。

---

# 五十、Service Group 为什么必要

如果 shard A 可以无限：

~~~text
submit_to(B)
~~~

而 B 处理较慢，

A 可以制造无限：

- work_item allocation；
- local pending_fifo；
- future/promise；
- completion debt。

---

# 五十一、固定 Ring 128 并不足以提供完整 Backpressure

因为 Ring 满时：

~~~text
request 仍可以堆在 origin pending_fifo
~~~

所以：

~~~text
ring capacity
~~~

不是业务 admission bound。

---

# 五十二、真正 Admission Gate 是 Semaphore

`submit_item()`：

~~~cpp
auto& sem =
  get_smp_service_groups_semaphore(
      ssg_id, target);

get_units(
    sem,
    1,
    timeout)
~~~

---

# 五十三、只有拿到 Unit 才进入 Pending FIFO

成功：

~~~text
credit acquired
→ pending_fifo.push_back(item)
~~~

失败：

~~~text
item->fail_with(exception)
→ future 失败
~~~

---

# 五十四、这个 Credit 什么时候归还

很多人会猜：

~~~text
request push 到 target 后就归还
~~~

但源码不是。

---

# 五十五、Credit 一直占到 Completion 回 Origin

`process_completions()`：

~~~cpp
wi->complete();

get_smp_service_groups_semaphore(
    ssg_id, target)
    .signal();

delete wi;
~~~

---

# 五十六、所以 Credit 覆盖完整生命周期

~~~text
admitted
→ waiting local batch
→ request ring
→ target task queued
→ target func running
→ async future pending
→ response local batch
→ completion ring
→ origin completion
~~~

直到这里才：

~~~text
signal credit
~~~

---

# 五十七、这限制的是 Outstanding RPC，不是 Queue Length

更准确：

\[
Outstanding_{A\to B}
\le Credit_{service\ group}
\]

---

# 五十八、为什么这样比限制 Ring 更有意义

因为真正消耗资源的并不只是：

~~~text
ring slot
~~~

还包括：

- target task；
- async operation；
- result storage；
- promise；
- remote memory；
- completion backlog。

---

# 五十九、Round-trip Credit 是 End-to-end Backpressure

它覆盖：

~~~text
请求从产生
直到调用者拿到完成
~~~

而不是只覆盖某一个中间 buffer。

---

# 六十、这和 TCP Window/Distributed Credit 很像

概念：

~~~text
sender owns limited credit
→ send request
→ receiver processes
→ completion/ack returns
→ sender credit restored
~~~

---

# 六十一、为什么 Service Group 是 Pair-wise 的

源码：

~~~text
clients
→ one client per server shard
~~~

创建 service group 时：

~~~text
每个 server shard
为每个 client shard
建立 semaphore
~~~

---

# 六十二、Global Max Nonlocal Requests 被拆成 Per-client Credit

源码：

~~~cpp
per_client =
    max_nonlocal_requests /
    (smp::count - 1);
~~~

---

# 六十三、为什么这样拆

避免一个 origin shard：

~~~text
吃光所有 target capacity
~~~

同时也让：

~~~text
每对 shard
有独立 admission state
~~~

---

# 六十四、这是资源隔离

类似：

- per-tenant queue quota；
- per-peer credit；
- per-client connection window。

---

# 六十五、Default Service Group 为什么几乎无限

默认 group 初始化：

~~~text
max_counter()
~~~

意味着：

~~~text
不主动施加有限 outstanding 限制
~~~

用户需要：

~~~text
create_smp_service_group()
~~~

配置有界控制。

---

# 六十六、为什么 API 要提供 Timeout

`get_units()` 使用：

~~~text
options.timeout
~~~

如果长期拿不到 credit，

future 可以：

~~~text
fail
~~~

而不是无限在本地排队。

---

# 六十七、Admission Wait 本身也是 Future

Seastar 不：

~~~text
block OS thread
~~~

而是：

~~~text
wait semaphore asynchronously
→ continuation
~~~

---

# 六十八、这保持 Reactor Non-blocking

即使 backpressure，

也不会：

~~~text
mutex wait
condition_variable block
~~~

占住 shard thread。

---

# 六十九、Target 执行 Func 后发生什么

`run_and_dispose()`：

~~~cpp
futurator::invoke(_func)
  .then_wrapped([this](auto f) {
      save result or exception;
      _queue.respond(this);
  });
~~~

---

# 七十、名字叫 `run_and_dispose()`，为什么不 Delete

源码明确：

~~~text
We don't delete the task here
as creator will delete it
on origin shard.
~~~

---

# 七十一、这说明 Task Execution Owner 与 Memory Reclaim Owner 不同

target：

~~~text
execution owner
~~~

origin：

~~~text
allocation / final destruction owner
~~~

---

# 七十二、为什么要回 Origin Delete

Work item：

- 在 origin 分配；
- promise 属于 origin-side future chain；
- callable 生命周期设计要求回 calling core；
- allocator 可能 shard-local。

所以最终：

~~~text
delete wi
~~~

回 origin 更符合 ownership。

completion 回到 origin 后，真正唤醒后续控制流的仍然是 Promise/Future 子系统：Promise 的结果存储怎样在 promise、future 与 continuation 之间迁移，以及 pending dependency 为什么最终物化成 Reactor task，见 [Future / Continuation：状态迁移、Task 化 Continuation 与异步控制流](future-continuation-task.md)。
---

# 七十三、这与 `foreign_ptr` 是同一个大原则

对象可以：

~~~text
跨 shard 被使用
~~~

但：

~~~text
destructor execution domain
~~~

仍然必须明确。

详情见 [Sharded 与 foreign_ptr：对象可以跨 Core 移动，但析构责任不能随便移动](sharded-foreign-ptr.md)。

---

# 七十四、Target Completion 为什么也先进入 Local FIFO

`respond()`：

~~~cpp
_completed_fifo.push_back(item);
~~~

达到：

~~~text
batch_size
~~~

或：

~~~text
engine stopped
~~~

才：

~~~text
flush_response_batch()
~~~

---

# 七十五、Request 与 Completion 两边都 Batch

链：

~~~text
origin local request FIFO
→ shared request ring

target local completion FIFO
→ shared completion ring
~~~

---

# 七十六、这形成对称结构

~~~text
LOCAL
  ↓ batch
SHARED
  ↓ detach
LOCAL
~~~

两个方向都尽量减少跨核共享访问。

---

# 七十七、为什么 Reactor 停止时 Completion 要立即 Flush

~~~cpp
if (_completed_fifo.size()
        >= batch_size
    || engine().stopped())
~~~

shutdown 时不能再依赖：

~~~text
未来正常 poll 周期
~~~

去把 completion 推回 origin。

---

# 七十八、这是 Shutdown Progress 优先于 Batching Efficiency

运行时：

~~~text
等 batch
~~~

退出时：

~~~text
尽快 flush completion
~~~

---

# 七十九、`process_queue()` 为什么先搬到 Stack Array

源码注释：

~~~text
copy batch to local memory
in order to minimize time
cross-cpu data is accessed
~~~

---

# 八十、具体步骤

先：

~~~cpp
q.pop(wi)
~~~

拿第一项。

再：

~~~text
prefetch first work item
~~~

然后：

~~~cpp
q.pop(items)
~~~

批量把 pointer 搬到本地：

~~~text
work_item* items[]
~~~

---

# 八十一、之后处理时不再频繁触碰 Shared Ring

~~~text
shared ring
→ extract pointer batch
→ local stack
→ work on objects
~~~

---

# 八十二、这是 Ownership Handoff 的经典优化

共享结构只负责：

~~~text
transfer ownership/reference
~~~

业务处理尽量在：

~~~text
local cache domain
~~~

完成。

---

# 八十三、为什么还要 Prefetch Work Item

pointer 刚从 remote producer 交过来，

指向的 work item 内存很可能：

~~~text
仍在 origin cache domain
~~~

target 访问 `_func`/state 可能产生 cache miss。

所以提前：

~~~text
prefetch
~~~

用处理当前项的时间覆盖下一项 memory latency。

---

# 八十四、Batch + Prefetch 是两层优化

Batch：

~~~text
减少共享 queue 元数据访问
~~~

Prefetch：

~~~text
减少 dereference remote-created object 的等待
~~~

---

# 八十五、Metrics 为什么也要 Cache-line 隔离

`smp_message_queue` 中：

~~~cpp
struct alignas(cache_line_size) {
   sender-side stats...
};

metrics::metric_groups _metrics;

struct alignas(cache_line_size) {
   receiver-side stats...
};
~~~

源码注释甚至强调：

~~~text
在两个 stats structure 之间
故意留至少一个 cache line
避免 hw prefetcher accidental prefetch
另一 CPU 正在写的 line
~~~

---

# 八十六、False Sharing 不只来自业务字段

即使：

~~~text
statistics
~~~

如果两个 CPU 高频写同一 cache line，

也会制造：

~~~text
MESI bouncing
~~~

---

# 八十七、Instrumentation 也必须服从 Hot-path Ownership

这是很多性能系统会忽略的问题：

~~~text
加 metrics
→ 算法没变
→ 性能却明显下降
~~~

原因可能就是 cache coherence。

---

# 八十八、为什么 Queue Length 是 128，但 Batch 是 16

两个常数服务不同目的。

~~~text
queue_length=128
→ cross-core shared ring burst absorption

batch_size=16
→ amortize shared publication / wakeup
~~~

---

# 八十九、不能把二者解释成同一个 Backpressure 参数

真正 request admission 上限：

~~~text
service group semaphore
~~~

shared ring：

~~~text
transport staging capacity
~~~

local FIFO：

~~~text
batch staging
~~~

三个层级不同。

---

# 九十、SMP Pipeline 的三种 Queueing State

一次 request 可能在：

~~~text
1. service-group semaphore waiters

2. origin pending_fifo

3. shared pending SPSC

4. target Reactor task queue

5. target async operation

6. target completed_fifo

7. shared completed SPSC

8. origin completion processing
~~~

---

# 九十一、所以“队列长度”不是一个数字

真正 latency 分析必须分：

~~~text
admission delay
publication batching delay
transport delay
target scheduling delay
execution delay
completion batching delay
origin scheduling delay
~~~

---

# 九十二、这对机器人多核 Pipeline 很重要

如果控制请求：

~~~text
Core 0 planner
→ Core 3 controller
~~~

观测到 200 µs latency，

不能只盯：

~~~text
SPSC ring
~~~

还要看：

- service group；
- target scheduling group；
- batch；
- Reactor sleep；
- continuation chain。

---

# 九十三、`process_completions()` 为什么先 Complete 再 Signal Credit

源码顺序：

~~~text
wi->complete()
→ sem.signal()
→ delete wi
~~~

---

# 九十四、为什么不是先归还 Credit

如果先：

~~~text
signal credit
~~~

另一个 request 可以立刻被 admission，

但上一个 request 的 origin-side completion 尚未兑现。

---

# 九十五、当前顺序把 Credit 语义定义得更强

更接近：

> **调用者的 promise 已经完成，才算一个 outstanding slot 真正释放。**

---

# 九十六、虽然 Delete 在 Signal 后

此时 promise 已经完成，

剩余只是：

~~~text
work-item object reclaim
~~~

其 lifetime 不再代表调用未完成。

---

# 九十七、Work Item Failure 也必须完成 Future

如果 service-group admission 本身失败：

~~~cpp
item->fail_with(exception);
~~~

不会进入 remote queue。

---

# 九十八、为什么还更新 Completion Stats

源码：

~~~text
++_compl
++_last_cmpl_batch
~~~

说明从 API 视角：

~~~text
remote request 生命周期已经终结
~~~

即使根本没跨核执行。

---

# 九十九、Transport Admission Failure 也属于 Completion

这有利于统一 future semantics：

~~~text
success
remote exception
admission timeout
~~~

最终都是：

~~~text
future ready
~~~

---

# 一百、Remote Exception 如何回来

target：

~~~text
f.failed()
→ _ex = f.get_exception()
~~~

origin：

~~~text
complete()
→ _promise.set_exception(_ex)
~~~

---

# 一百零一、异常对象跨核本身也有 Ownership 复杂度

源码明确留下一个尚未跨 shard 支持的边界：

~~~text
_ex was allocated on another cpu
~~~

这正说明：

> **跨 shard 移动 value 很容易，跨 shard 移动带 allocator/lifetime 语义的复杂对象更难。**

---

# 一百零二、Shard-per-core 不是“完全没有跨核内存”

work item 本身：

~~~text
origin allocates
target accesses
origin deletes
~~~

仍会跨 cache domain。

---

# 一百零三、它优化的是“共享写 ownership”

不是：

~~~text
所有字节永远不跨核
~~~

而是：

~~~text
长期 mutable state
尽量单 owner

跨核 interaction
变成 bounded handoff
~~~

---

# 一百零四、Why Message Passing Beats Shared Mutable State

Shared-state 方案：

~~~text
A lock object
B lock object
modify
unlock
~~~

会要求：

- lock coherence；
- object cache bouncing；
- lifetime synchronization。

Message-passing 方案：

~~~text
A packages work
→ B executes on its local state
→ result returns
~~~

把共享点压缩到 queue。

---

# 一百零五、复杂度没有消失，而是集中在 Boundary

跨 shard boundary 必须解决：

- queue;
- backpressure；
- wakeup；
- ownership；
- completion；
- shutdown。

这正是 `smp_message_queue` 的价值。

---

# 一百零六、Reactor 如何周期处理所有 SMP Queue

源码：

~~~cpp
for (unsigned i = 0;
     i < count;
     ++i) {

  if (this_shard_id() != i) {
      ...
  }
}
~~~

当前 shard 与每个其他 shard 都有 pair channel。

---

# 一百零七、对于 Incoming Pair

~~~cpp
auto& rxq =
  _qs[this_shard_id()][i];

rxq.flush_response_batch();

rxq.process_incoming();
~~~

---

# 一百零八、为什么先 Flush Response

这条 queue object 表示：

~~~text
i → current
~~~

当前 shard可能此前执行了来自 i 的 work，

completion 暂存在：

~~~text
rxq._completed_fifo
~~~

所以先尝试推回 i。

---

# 一百零九、然后 Process New Incoming

~~~text
request from i
→ schedule target-local task
~~~

---

# 一百一十、对于 Outgoing Pair

~~~cpp
auto& txq =
  _qs[i][this_shard_id()];

txq.flush_request_batch();

txq.process_completions(i);
~~~

---

# 一百一十一、因此一个 Poll Cycle 同时推进四件事

对每个 peer i：

~~~text
1. flush responses current→i
2. process requests i→current
3. flush requests current→i
4. process responses i→current
~~~

---

# 一百一十二、这是真正的 Full Duplex Progress Engine

而不是单纯：

~~~text
read queue
~~~

---

# 一百一十三、`pure_poll_queues()` 为什么也 Flush Local Batch

即使只是判断：

~~~text
有没有 SMP work
~~~

它也：

~~~text
flush_response_batch
flush_request_batch
~~~

---

# 一百一十四、Readiness Probe 可以做 Progress

这和很多 Event Loop 一样：

~~~text
poll
~~~

不是纯 const query。

它可能：

~~~text
publish deferred batch
~~~

然后再判断共享 ring 是否有工作。

---

# 一百一十五、为什么这很合理

如果 local FIFO 里有请求，

但尚未 publish：

~~~text
共享 ring 看起来空
~~~

只检查 ring 会错误判定：

~~~text
系统 idle
~~~

---

# 一百一十六、所以 Deferred Local State 必须先 Materialize

~~~text
local pending work
→ shared visible work
→ readiness decision
~~~

---

# 一百一十七、`has_unflushed_responses()` 为什么单独存在

completion 可能暂存：

~~~text
_completed_fifo
~~~

它还没进入 shared ring，

但仍然意味着：

~~~text
系统有进展义务
~~~

所以 idle 判断不能忽略。

---

# 一百一十八、Idle Detection 是全协议状态的函数

不是：

~~~text
one queue.empty()
~~~

而是：

~~~text
shared requests
OR
shared completions
OR
local unflushed requests
OR
local unflushed completions
~~~

---

# 一百一十九、这和 Shutdown Quiescence 很像

前面 libzmq 已经看到：

~~~text
queue empty
≠
system quiescent
~~~

因为可能还有：

- local staging；
- async child；
- scheduled callback；
- completion debt。

Seastar 同样如此。

---

# 一百二十、Wakeup 为什么要看 Reactor `_sleeping`

Reactor 在活跃时：

~~~text
会不断 poll SMP queues
~~~

所以：

~~~text
request publication
~~~

自然很快被看到。

---

# 一百二十一、只有 Sleep 才需要 Interrupt

因此：

~~~text
fast path
→ userspace only

slow idle path
→ eventfd syscall
~~~

---

# 一百二十二、这是 Adaptive Waiting

和：

- spin-then-park；
- futex；
- eventfd doorbell；

属于同一类设计：

> **高负载避免 syscall，低负载允许 CPU sleep。**

---

# 一百二十三、为什么 System-wide Barrier 看起来很重

因为 Lost Wakeup 跨的是：

~~~text
两个 CPU
+
shared queue
+
sleep flag
+
kernel blocking boundary
~~~

---

# 一百二十四、Seastar 选择把昂贵 Barrier 放在 Sleep Transition

高负载：

~~~text
几乎不睡
→ barrier 很少
~~~

低负载：

~~~text
吞吐不敏感
→ 可以承担 barrier 成本
~~~

---

# 一百二十五、把昂贵同步放在 Cold Path

这是 Runtime 优化常见原则：

~~~text
hot path
→ relaxed / local / batched

cold transition
→ strong synchronization
~~~

---

# 一百二十六、Queue 与 Wakeup 的不变量

需要保证：

> 如果 target 最终真的进入 sleep，那么它在 commit sleep 前已经观察不到任何 request；任何 sleep commit 之后的 request publication 又一定能观察 sleeping 并触发 wakeup。

---

# 一百二十七、这就是 Wait Protocol 的双向证明

Consumer：

~~~text
prepare sleep
→ barrier
→ final poll
~~~

Producer：

~~~text
publish
→ ordering
→ maybe wake
~~~

---

# 一百二十八、只证明 Producer 不够

即使 producer 总会看 `_sleeping`，

consumer 若：

~~~text
先 check queue
后 set sleeping
~~~

仍可能 lost wakeup。

---

# 一百二十九、只证明 Consumer 也不够

consumer prepare-to-sleep 正确，

producer 若：

~~~text
publish后不 wake
~~~

它还是会长期睡。

---

# 一百三十、Wait Protocol 是双方共同协议

这和 condition variable 的正确模式相同：

~~~text
predicate
+
publication
+
wait registration
+
notification
~~~

必须一起看。

---

# 一百三十一、`lf_queue` Destructor 为什么 Drain + Delete Remaining Item

~~~cpp
consume_all([](work_item* ptr) {
    delete ptr;
});
~~~

说明 queue destruction 假定：

~~~text
剩余 ownership 最终归这个 queue cleanup path
~~~

---

# 一百三十二、但正常路径不靠 Destructor 回收

正常：

~~~text
request
→ target
→ completion
→ origin delete
~~~

Destructor drain 是：

~~~text
terminal cleanup
~~~

不是 hot path ownership model。

---

# 一百三十三、为什么 Work Item 用 Raw Pointer 进 Queue

queue 只传：

~~~text
work_item*
~~~

ownership protocol由状态机决定：

~~~text
origin allocates
pending owns transit
target owns execution
completed owns return transit
origin deletes
~~~

---

# 一百三十四、Raw Pointer 不等于“没有 Ownership”

真正关键是：

~~~text
每个阶段谁负责下一步
~~~

而不是 C++ 类型是否写了 `unique_ptr`。

---

# 一百三十五、在边界入口先用 `unique_ptr`

`submit()` 创建：

~~~text
unique_ptr<work_item>
~~~

只有在 admission 成功准备进入 queue 后：

~~~cpp
item.release();
~~~

---

# 一百三十六、这是一种 Exception-safe Ownership Handoff

在 `release()` 之前若失败：

~~~text
unique_ptr 自动清理
~~~

之后代码注明：

~~~text
no exceptions from this point
~~~

---

# 一百三十七、Ownership Release 必须和 No-fail Region 对齐

非常值得迁移：

> **裸指针 handoff 前保持 RAII；只有跨过最后一个可能失败点后才 release ownership。**

---

# 一百三十八、Credit Unit 的 Lifetime 也很特别

`get_units()` 返回 RAII units。

但成功后代码：

~~~cpp
units_fut.get().release();
~~~

这里也主动放弃 RAII 自动归还。

---

# 一百三十九、为什么要 Release Semaphore Unit

因为这个 credit 不能在：

~~~text
submit continuation 返回
~~~

时自动归还。

它必须跨越：

~~~text
整个远端 round-trip
~~~

---

# 一百四十、所以 Credit Ownership 被“转移”给 Work Item Protocol

之后不是靠 units destructor，

而是靠：

~~~text
process_completions().signal()
~~~

显式归还。

---

# 一百四十一、这是资源 Token Handoff

起初：

~~~text
semaphore units object
~~~

证明你拿到了一个 token。

进入 protocol 后：

~~~text
token debt
~~~

由 work-item lifecycle 隐式携带。

---

# 一百四十二、为什么这种设计容易出错

如果任何路径：

- work item 丢失；
- completion 不返回；
- exception 没转换；
- shutdown 漏 flush；

就可能：

~~~text
credit leak
~~~

随后该 service-group 永久失去容量。

---

# 一百四十三、所以 Completion Path 是 Backpressure Protocol 的一部分

不是：

~~~text
只是为了返回 result
~~~

还承担：

~~~text
release admission credit
~~~

---

# 一百四十四、Request/Response 与 Flow-control 深度耦合

完整模型：

~~~text
credit
  ↓
request
  ↓
execute
  ↓
completion
  ↓
credit return
~~~

---

# 一百四十五、这和网络 Credit-based Flow Control 完全同构

例如：

- RDMA credits；
- PCIe tags；
- NoC virtual-channel credit；
- distributed RPC concurrency limit。

---

# 一百四十六、机器人跨核任务也可以这样设计

假设：

~~~text
Vision shard
→ Planning shard
~~~

不要只放一个：

~~~text
unbounded MPSC
~~~

而是：

~~~text
N outstanding credits
→ submit frame processing
→ completion returns
→ credit restored
~~~

---

# 一百四十七、这样自然限制 Data Age

如果 vision 产生速度高于 planning，

有界 outstanding 会：

~~~text
在 admission 处施加压力
~~~

而不是：

~~~text
让 backlog 无限老化
~~~

---

# 一百四十八、如果任务语义是 Latest-state

更进一步可以：

~~~text
credit=1
+
replace pending state
~~~

而不是排队所有历史 frame。

---

# 一百四十九、如果任务语义是 Event

则不能随便覆盖，

应：

~~~text
bounded queue
+
backpressure / explicit drop policy
~~~

---

# 一百五十、SMP Service Group 是 Mechanism，不是业务 Policy

它只给：

~~~text
maximum nonlocal outstanding
~~~

不决定：

- drop old；
- drop new；
- priority；
- deadline；
- latest-only。

这些仍由上层设计。

---

# 一百五十一、为什么 Target Func 可以返回 Future

`futurize::invoke()` 把：

~~~text
T
future<T>
void
future<>
~~~

统一成 future abstraction。

---

# 一百五十二、所以 Remote Work 不要求同步完成

target 可以：

~~~text
启动 async I/O
→ return future
~~~

Work item 不会立刻 respond。

---

# 一百五十三、Completion 等到 Target Future Ready

这意味着 credit 覆盖：

~~~text
远端 async operation 整个生命周期
~~~

而不是只覆盖：

~~~text
CPU callback 执行时间
~~~

---

# 一百五十四、这让 Service-group Credit 更接近 Concurrency Limit

如果 target func 发起磁盘/网络异步操作：

~~~text
100 outstanding
~~~

就可以限制：

~~~text
远端系统级并发
~~~

---

# 一百五十五、这和 Semaphore around Async RPC 很像

伪代码：

~~~text
await credit
await remote_call
credit++
~~~

只是 Seastar 把它集成进 SMP transport。

---

# 一百五十六、为什么 `waiting_task()` 返回 nullptr

源码对这一能力明确标注为未实现边界：

~~~text
waiting_task across shards
not implemented
unsynchronized task access unsafe
~~~

这是很有价值的边界声明。

---

# 一百五十七、并不是所有 Future Debug/Task Dependency 都能透明跨 Shard

如果要返回：

~~~text
另一个 shard 的 task*
~~~

并直接检查，

就重新引入：

~~~text
unsynchronized cross-core object access
~~~

所以宁可：

~~~text
不实现
~~~

也不暴露错误抽象。

---

# 一百五十八、Shard Boundary 应该是 API Boundary

跨 shard 时尽量交换：

- immutable value；
- moved callable；
- message；
- future result；
- foreign wrapper。

而不是：

~~~text
远端 mutable task pointer
~~~

---

# 一百五十九、这一原则和 `sharded<T>` 相同

业务层：

~~~text
invoke_on(owner, lambda)
~~~

而不是：

~~~text
T* remote = ...
remote->mutate()
~~~

---

# 一百六十、跨 Shard 的真正成本应该显式

Seastar 不把它伪装成：

~~~text
普通函数调用
~~~

`submit_to()` 返回 future，

这已经提示调用者：

~~~text
这是异步边界
~~~

---

# 一百六十一、如果 API 返回普通 T 会有什么问题

调用者容易误以为：

- 低延迟；
- 本地访问；
- 无调度；
- 无 backpressure；
- 无 failure。

Future 让边界成本显式化。

---

# 一百六十二、一个完整 Remote Submit 时序

~~~text
Origin Shard A
    |
    | smp::submit_to(B, func)
    v
create async_work_item
    |
    | obtain service-group credit
    v
A-local pending_fifo
    |
    | batch / poll flush
    v
shared SPSC pending
    |
    | maybe_wakeup(B)
    v
Target Reactor B
    |
    | process_incoming
    v
work_item::process
    |
    | schedule(task)
    v
B scheduling group
    |
    | run_and_dispose
    v
invoke func
    |
    | async/sync future completes
    v
save result/exception
    |
    | respond()
    v
B-local completed_fifo
    |
    | batch / stop flush
    v
shared SPSC completed
    |
    | maybe_wakeup(A)
    v
Origin Reactor A
    |
    | process_completions
    v
promise.set_value / exception
    |
    +--> return service-group credit
    |
    +--> delete work_item
~~~

---

# 一百六十三、这条链里有四种 Ownership

第一：

~~~text
State Ownership
→ target shard owns mutable service state
~~~

第二：

~~~text
Execution Ownership
→ target Reactor scheduler runs func
~~~

第三：

~~~text
Work-item Allocation/Reclaim Ownership
→ origin shard
~~~

第四：

~~~text
Flow-control Token Ownership
→ outstanding request protocol
~~~

---

# 一百六十四、这四种 Ownership 不能混为一谈

“对象属于 B”不意味着：

~~~text
所有相关内存都必须由 B 分配/释放
~~~

“task 在 B 执行”也不意味着：

~~~text
promise 必须在 B 完成
~~~

---

# 一百六十五、Seastar 的优势来自明确拆分这些责任

而不是：

~~~text
everything local
~~~

---

# 一百六十六、SMP Queue 与 Cross-CPU Free 的共同结构

另一篇 [Per-shard Allocator 与 Cross-CPU Free](cross-shard-memory-reclaim.md)：

~~~text
foreign CPUs
→ shared MPSC ingress
→ owner batch drain
→ local free
~~~

SMP queue：

~~~text
origin local batch
→ SPSC ingress
→ target local task
~~~

共同原则：

> **共享结构只做交接，真正工作回 owner-local domain。**

---

# 一百六十七、为什么 Cross-CPU Free 用 MPSC，而 SMP Request 用 SPSC

因为 topology 不同。

SMP A→B：

~~~text
producer only A
consumer only B
~~~

Remote frees to B：

~~~text
A/C/D/... all may free B-owned memory
→ one B consumer
~~~

所以自然：

~~~text
MPSC
~~~

---

# 一百六十八、数据结构选择来自 Producer/Consumer Topology

不是来自：

~~~text
哪个 lock-free 名字更高级
~~~

---

# 一百六十九、SPSC 的优势

相对 MPMC：

- 少 CAS；
- 少 per-slot metadata；
- 更低 cache coherence；
- producer/consumer cursor ownership清晰；
- 更容易 batch。

---

# 一百七十、代价

N shards 需要：

~~~text
O(N²) pair relationships
~~~

---

# 一百七十一、为什么 Seastar 仍愿意这么做

它优化的是：

~~~text
固定小/中等核数
+
极高频消息
+
明确 affinity
~~~

---

# 一百七十二、O(N²) Metadata 不等于 O(N²) Traffic

大多数 queue：

~~~text
可能空闲
~~~

只是存在 pair channel object。

真正成本取决于：

~~~text
actual communication graph
~~~

---

# 一百七十三、什么时候这种结构不合适

如果：

- shard 数极大；
- topology 动态；
- many arbitrary producers；
- affinity 很弱；

则 pair-wise SPSC matrix 可能过重。

---

# 一百七十四、Actor Runtime 也面临同样取舍

可以选：

~~~text
per-destination MPSC
~~~

减少 queue 数，

但增加：

~~~text
multi-producer contention
~~~

Seastar 选择：

~~~text
更多 queue
换更简单 hot path
~~~

---

# 一百七十五、为什么批量 Publication 很适合 Pair-wise Queue

一个 origin→target 有自己独立：

~~~text
pending_fifo
~~~

所以不用与其他 producer 协调 batch。

---

# 一百七十六、如果是 MPSC Batch 会更复杂

多个 producer：

~~~text
谁拥有 staging buffer？
谁决定 flush？
~~~

通常需要额外 synchronization。

---

# 一百七十七、Pair Ownership 又一次减少协议复杂度

同一个 design pattern：

~~~text
single owner
→ local mutable state
→ explicit handoff
~~~

贯穿 Seastar。

---

# 一百七十八、SMP Queue 的 Hot Shared State 到底有哪些

真正跨核共享的核心只需要：

~~~text
SPSC producer/consumer indices
work_item pointer slots
_sleeping flag
eventfd wakeup path
~~~

---

# 一百七十九、业务 Service State 不在这里共享

这是最重要的结构价值。

---

# 一百八十、如果自己设计类似 Runtime

建议先画：

~~~text
Shard A owns:
- services
- scheduler
- allocator
- local pending buffer

Shard B owns:
- services
- scheduler
- allocator
- local response buffer

Shared A→B:
- bounded pointer ring
- wakeup metadata
~~~

---

# 一百八十一、然后再问每个字段谁写谁读

例如：

~~~text
A→B ring producer cursor
→ A writes

consumer cursor
→ B writes
~~~

这比先写 mutex 更容易推理。

---

# 一百八十二、Cache-line Layout 也从 Ownership 推导

如果：

~~~text
A-writes fields
~~~

尽量与：

~~~text
B-writes fields
~~~

分 cache line。

---

# 一百八十三、metrics 也遵循这个规则

源码刻意：

~~~text
sender stats
[cache-line gap]
receiver stats
~~~

就是例子。

---

# 一百八十四、Cross-shard Runtime 的五层设计

可以抽象：

~~~text
1. Ownership
   who owns mutable state?

2. Transport
   how work crosses shard?

3. Scheduling
   when target executes it?

4. Flow Control
   how many may be outstanding?

5. Reclamation
   where is wrapper/payload destroyed?
~~~

---

# 一百八十五、Seastar 给出的答案

~~~text
Ownership
→ shard-per-core

Transport
→ pair-wise SPSC

Scheduling
→ task / scheduling group

Flow Control
→ SMP service-group semaphore

Reclamation
→ origin/owner-specific protocol
~~~

---

# 一百八十六、这比“一条 lock-free queue”完整得多

只替换：

~~~text
mutex queue
→ lock-free queue
~~~

没有回答：

- queue 满怎么办；
- consumer 睡怎么办；
- callback 在哪跑；
- result 怎么回；
- object 谁 free。

---

# 一百八十七、Lock-free 只是一个局部机制

Runtime 正确性来自：

~~~text
topology
+
ownership
+
backpressure
+
wakeup
+
lifetime
~~~

共同作用。

---

# 一百八十八、对机器人多核 Runtime 的一个具体映射

假设：

~~~text
Core 0
Sensor ingest

Core 1
Localization

Core 2
Planner

Core 3
Control
~~~

---

# 一百八十九、错误设计

所有模块共享：

~~~text
GlobalState
std::mutex
std::vector
~~~

高频修改：

~~~text
pose
map
trajectory
control state
~~~

---

# 一百九十、Seastar-like 设计

~~~text
Localization state
owned by Core 1

Planner state
owned by Core 2

Control state
owned by Core 3
~~~

跨模块：

~~~text
submit immutable input/computation
~~~

---

# 一百九十一、Planning 想读取 Localization State

不要：

~~~text
Core 2
lock Core 1 state
~~~

而是：

~~~text
Core 2
submit_to(Core 1)
→ snapshot required state
→ completion back
~~~

或者：

~~~text
publish immutable snapshot
~~~

---

# 一百九十二、哪种更好取决于访问模式

低频 query：

~~~text
RPC submit_to
~~~

高频 latest-state：

~~~text
immutable snapshot / replicated state
~~~

---

# 一百九十三、不要把所有跨核访问都做 RPC

Shard-per-core 不是教条。

如果 1 kHz 控制循环每个 scalar read 都：

~~~text
submit_to
~~~

开销也会很高。

---

# 一百九十四、正确问题是

> 哪些 mutable state 值得保持单 owner，哪些 read-mostly/latest state 值得复制？

---

# 一百九十五、SMP Service-group 对机器人 Pipeline 的借鉴

例如 Vision→Planner：

~~~text
max_nonlocal_requests = 2
~~~

可以表达：

~~~text
最多同时有 2 帧 planning work
~~~

超过：

~~~text
producer future waits
~~~

---

# 一百九十六、这比无限队列更健康

无限 queue：

~~~text
throughput < input rate
→ latency不断增长
→ robot acts on stale state
~~~

bounded outstanding：

~~~text
压力尽早显现
~~~

---

# 一百九十七、但控制系统通常还需要 Drop Policy

Seastar semaphore 默认：

~~~text
wait
~~~

机器人 latest-state 可能更适合：

~~~text
drop/replace old
~~~

所以要在业务层组合。

---

# 一百九十八、Runtime Mechanism 与 Control Semantics 不能混

Mechanism：

~~~text
有界 credit
~~~

Policy：

~~~text
wait / drop / overwrite / deadline
~~~

---

# 一百九十九、Wakeup 机制对实时性有什么影响

目标 Reactor：

~~~text
busy
→ userspace queue polling

idle
→ eventfd wake
~~~

所以 latency 分布会有：

~~~text
hot-path low latency
+
sleep wakeup tail
~~~

---

# 二百、实时系统必须关注 Tail Latency

平均值低不代表：

~~~text
sleep transition race
+
scheduler delay
+
batch delay
~~~

不存在。

---

# 二百零一、Batch Size 也影响 Tail

高负载：

~~~text
batch improves throughput
~~~

低负载：

~~~text
poll-cycle flush prevents indefinite wait
~~~

这正是 event-loop batching 的常见模式。

---

# 二百零二、真正要测的不是 Queue Push ns

如果做工程评估，应看：

~~~text
submit_to()
→ target func begins

submit_to()
→ origin future ready
~~~

两种 latency。

---

# 二百零三、前者是 Dispatch Latency

包括：

- admission；
- batching；
- wakeup；
- target scheduling。

---

# 二百零四、后者是 Round-trip Latency

再包括：

- function execution；
- completion batching；
- origin scheduling。

---

# 二百零五、Service-group Backpressure 影响的是哪一段

主要在：

~~~text
admission
~~~

它可以让调用在进入 transport 之前等待。

---

# 二百零六、为什么这是好位置

越晚背压：

~~~text
更多对象已经分配
更多 queue 已占用
更多 cache 已污染
~~~

越早 admission：

~~~text
资源增长更可控
~~~

---

# 二百零七、Early Admission Control 是普遍原则

例如：

- connection accept limit；
- request semaphore；
- GPU inflight count；
- frame queue depth；
- RPC concurrency limit。

---

# 二百零八、`scoped_critical_alloc_section` 又说明什么

`submit()` 在 work item allocation 周围：

~~~cpp
memory::scoped_critical_alloc_section _;
~~~

说明跨核 submission 的 allocation 本身：

~~~text
属于 runtime-sensitive path
~~~

---

# 二百零九、这里只需要抓住一个概念

即使 message passing 消除了共享业务锁，

它仍然需要：

~~~text
work-item allocation
~~~

所以 allocator design 仍是系统性能的一部分。

---

# 二百一十、Seastar 为什么把 Allocator 也 Per-shard

如果所有 submit 都争：

~~~text
global malloc metadata
~~~

又会重新制造共享瓶颈。

---

# 二百一十一、这就连接到 Cross-shard Memory Reclaim

分配 per-shard 后，

foreign free 又需要：

~~~text
return to owner
~~~

所以：

~~~text
CPU ownership
~~~

会贯穿：

- task；
- service；
- allocator；
- destructor。

---

# 二百一十二、这不是一个孤立 Queue 设计

`smp_message_queue` 是 shard-per-core architecture 的连接组织。

---

# 二百一十三、最终统一图

~~~text
                         Origin Shard A
                 +--------------------------+
                 |                          |
                 |  application task        |
                 |       |                  |
                 |  submit_to(B)            |
                 |       |                  |
                 |  acquire SMP credit      |
                 |       |                  |
                 |  pending_fifo            |
                 +-------|------------------+
                         |
                         | batch
                         v
                +--------------------+
                | A → B SPSC pending |
                +--------------------+
                         |
                         | wake if sleeping
                         v
                 +--------------------------+
                 |      Target Shard B      |
                 |                          |
                 | process_incoming         |
                 |       |                  |
                 | work_item::process       |
                 |       |                  |
                 | schedule(task)           |
                 |       |                  |
                 | scheduling group         |
                 |       |                  |
                 | run_and_dispose          |
                 |       |                  |
                 | func / async future      |
                 |       |                  |
                 | completed_fifo           |
                 +-------|------------------+
                         |
                         | batch
                         v
                +----------------------+
                | B → A SPSC completed |
                +----------------------+
                         |
                         v
                 +--------------------------+
                 |      Origin Shard A      |
                 |                          |
                 | process_completions      |
                 |       |                  |
                 | promise ready            |
                 | credit return            |
                 | delete work_item         |
                 +--------------------------+
~~~

---

# 二百一十四、源码作者需要守住的十二条不变量

第一：

> **跨 shard mutable state 优先移动 computation 到 owner，而不是让 foreign shard 直接修改远端对象。**

第二：

> **N 个 shard 不自动意味着 MPMC；pair-wise topology 可以把每条链降为 SPSC。**

第三：

> **request 与 completion 分成两条反向 SPSC，可以保持每条通道单 producer / single consumer。**

第四：

> **shared ring 只负责 ownership handoff，真正处理应尽快回到 local memory / local scheduler。**

第五：

> **queue arrival 不应绕过 target Reactor scheduling policy；work item 先 schedule 成 task。**

第六：

> **local staging + batch publication 可以显著减少跨核 cache-line interaction，但必须有低负载 flush path。**

第七：

> **SPSC 的线程安全不等于 sleeping consumer 的 wakeup 正确；queue publication 与 wait protocol 必须共同证明。**

第八：

> **SMP service-group credit 应覆盖完整 remote round-trip，才能真正约束 outstanding work，而不只是 ring occupancy。**

第九：

> **credit 的归还路径与 completion path 是同一协议；漏 completion 就等于漏 capacity。**

第十：

> **执行 shard 与 reclaim shard 可以不同，必须显式记录最终 destructor/reclaim responsibility。**

第十一：

> **hot path 的 producer-written 与 consumer-written stats 也要避免 false sharing；metrics 不是“免费”的。**

第十二：

> **跨核 Runtime 的正确性不是 lock-free queue 一个组件提供的，而是 ownership、admission、publication、wakeup、scheduling、completion 与 reclamation 的组合证明。**

---

# 二百一十五、最终心智模型

不要把：

~~~text
smp::submit_to()
~~~

理解成：

~~~text
跨线程调用 lambda
~~~

更准确的是：

~~~text
owner-shard RPC
~~~

它包含：

~~~text
bounded admission
+
origin-local batching
+
pair-wise SPSC publication
+
lost-wakeup-safe notification
+
target-local task scheduling
+
async result capture
+
reverse completion channel
+
origin-side promise fulfillment
+
credit return
+
origin-side reclaim
~~~

如果只记一句：

> **Seastar 的跨核高性能不是“用了 lock-free queue”，而是先用 shard-per-core 把 mutable state 变成单 owner，再把不可避免的跨核协作压缩成有界、成对、可批量、可唤醒、可回源的显式消息协议。**
