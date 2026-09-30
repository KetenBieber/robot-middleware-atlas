# GXF EntityExecutor 与 MessageRouter：一次 tick 前后到底发生了什么

Holoscan SDK 行为基线：v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

公开 GXF 源码基线：v3.2-1 `daf1810358301f642374dfb3d725be349bba5ec0`。

前一篇已经追到 Scheduler 怎么决定“哪个 Entity 可以被 worker 拿走”。

但 worker 真正调用 `executeEntity()` 后，业务 Codelet 并不是立刻裸跑。

中间还有一整套：

~~~text
SchedulingTerm check
↓
Entity lifecycle state
↓
execution_mutex
↓
Router::syncInbox
↓
Codelet tick
↓
SchedulingTerm::onExecute
↓
Router::syncOutbox
↓
downstream event notification
~~~

这条链是理解 GXF/Holoscan 数据流语义的关键。

## EntityExecutor 为什么不是 Scheduler 的一部分

如果 Scheduler 直接调用每个 Codelet：

~~~text
Scheduler
├── knows queue state
├── knows lifecycle
├── knows Codelet list
├── knows Router
├── knows statistics
└── executes business code
~~~

它会变成一个巨大对象。

GXF 把责任拆成：

~~~text
Scheduler
  when / where to run

EntityExecutor
  whether this Entity can run
  how one Entity tick is executed

MessageRouter
  how messages cross transmitter/receiver boundary
~~~

这与 OS 中：

~~~text
scheduler
vs
process execution state
vs
I/O subsystem
~~~

是相似的职责分离。

## EntityItem 自己就是一个运行时状态机

`EntityExecutor::EntityItem` 会维护生命周期：

~~~text
NOT_STARTED
↓
START_PENDING
↓
STARTED
↓
TICK_PENDING
↓
TICKING
↓
IDLE
↓
...
↓
STOP_PENDING
↓
NOT_STARTED
~~~

执行前源码会拒绝非法状态：

~~~cpp
if (status_ == GXF_ENTITY_STATUS_START_PENDING) { ... }

if (status_ == GXF_ENTITY_STATUS_TICK_PENDING ||
    status_ == GXF_ENTITY_STATUS_TICKING) {
  return GXF_INVALID_EXECUTION_SEQUENCE;
}
~~~

这不是多余防御。

Runtime 一旦允许：

~~~text
同一 Entity
在上一个 tick 未结束时
再次进入 tick
~~~

那么成员状态、Receiver、Transmitter 和算法内部缓存都会失去基本串行语义。

## 为什么已经有 atomic ownership，还要 execution_mutex_

Scheduler 的 `ScheduleEntity::ownership_` 防止两个 worker 同时拿到 execution token。

EntityExecutor 里仍然：

~~~cpp
std::unique_lock<std::mutex> lock(execution_mutex_);
~~~

这看起来重复。

但两层保护的职责不同：

~~~text
atomic ownership:
  scheduler-level claim
  非常短
  防止重复 worker 执行

execution_mutex:
  Entity execution critical section
  保护生命周期、check、tick、stop
~~~

如果未来 Scheduler 实现变化、调用路径不是来自同一个 ownership gate，EntityExecutor 仍有自己的安全边界。

这就是 layered synchronization。

## check() 为什么要先把 SchedulingTerm 合并

公开源码：

~~~cpp
SchedulingCondition combined{
    SchedulingConditionType::READY, 0};
~~~

先处理 `SchedulingTermCombiner`，再处理剩余 term：

~~~cpp
combined = AndCombine(combined, result.value());
~~~

所以普通 Entity readiness 可以理解成：

~~~text
C = C1 AND C2 AND C3 ...
~~~

例如：

~~~text
MessageAvailable
AND
DownstreamReceptive
AND
MemoryAvailable
AND
Periodic
~~~

这把资源约束真正变成 executable predicate。

## 为什么 Combiner 需要先从普通 terms 集合里剔除

`activate()` 会找：

~~~cpp
entity.findAll<SchedulingTerm>()
entity.findAll<SchedulingTermCombiner>()
~~~

然后收集 combiner 里已经包含的 term cid，再从普通 `terms` vector 里移除。

原因非常直接：

如果一个 term：

~~~text
已经参与 OR / custom combiner
又被外层 AND 一次
~~~

调度逻辑会被重复计算，甚至改变布尔语义。

这是一种 runtime graph normalization。

## MessageAvailableSchedulingTerm 真正看的是两个 stage

公开源码：

~~~cpp
return receiver_->back_size() +
       receiver_->size() >= min_size_;
~~~

也就是说它统计：

~~~text
main-stage messages
+
back-stage messages
~~~

为什么？

因为消息即使还没 `sync()` 到当前可消费 main stage，也已经实际到达 Receiver。

Scheduler 判断“有没有新输入”时必须看到整个逻辑 queue occupancy，而不是只看当前 tick snapshot。

## 但 front_stage_max_size 又只看 size()

另一个检查：

~~~cpp
return !maybe || receiver_->size() <= *maybe;
~~~

这里使用 main stage `size()`。

所以同一个 SchedulingTerm 同时关注：

~~~text
总可用消息量
与
当前消费 stage 的积压
~~~

这说明 queue state 并不是一个单一整数。

工业 runtime 里最好明确区分：

~~~text
arrived
staged
visible to current tick
consumed
~~~

## ExpiringMessageAvailableSchedulingTerm 是很实用的 batching policy

它表达的不是“凑够 N 条才跑”。

而是：

~~~text
如果数量够 max_batch_size
→ 立即 READY

否则只要至少有一条
→ 等到 oldest_message_age 达 max_delay
→ READY
~~~

源码：

~~~cpp
if (receiver_size >= max_batch_size_) {
  *type = READY;
  return GXF_SUCCESS;
}

const int64_t expiring_timestamp =
    oldest_message_ts + max_delay_ns_;

if (expiring_timestamp <= timestamp) {
  *type = READY;
} else {
  *type = WAIT_TIME;
}
~~~

这就是经典：

~~~text
throughput batching
vs
latency deadline
~~~

二者同时满足。

AI inference batching、网络 packet batching、数据库 group commit 都会用到同样思想。

## 一个具体例子

假设：

~~~text
max_batch_size = 8
max_delay = 5 ms
~~~

情况 A：

~~~text
2 ms 内已经来 8 条
→ 立即运行
~~~

情况 B：

~~~text
5 ms 只来 3 条
→ deadline 到
→ 用 3 条运行
~~~

如果只按 batch size，会在低负载时延迟无限增长。

如果只按每条立即处理，高负载时吞吐又上不去。

这就是 batching scheduler 的第一性原理。

## execute() 为什么又重新 check 一次

Dispatcher 已经 check 过 Entity。

Worker 拿到后 `executeEntity()` 又：

~~~cpp
const auto maybe_condition = check(timestamp);
~~~

为什么重复？

因为：

~~~text
dispatcher check
↓
enqueue
↓
queue wait
↓
worker actually runs
~~~

中间状态可能已经变化。

比如：

~~~text
downstream queue 原本有空间
↓
别的 producer 先占满
↓
当前 worker 真正运行时
条件已经不成立
~~~

所以 scheduler decision 是 snapshot，不是永久授权。

这类似 optimistic validation：

> 真正执行前再验证一次。

## WAIT_TIME 为什么在 execute() 里还能变 READY

源码：

~~~cpp
if (condition.type == WAIT_TIME) {
  const int64_t target = condition.target_timestamp;
  if (target <= timestamp) {
    condition = {READY, target};
  } else {
    return {WAIT_TIME, target};
  }
}
~~~

TimedJobList 负责把 worker 大致在合适时间唤醒。

EntityExecutor 再做最终 timestamp check。

所以：

~~~text
timer queue
负责 wakeup scheduling

executor
负责 semantic validation
~~~

这是很稳健的分层。

## 真正 tick 之前先 syncInbox

关键源码：

~~~cpp
code = router->syncInbox(entity);
~~~

然后才：

~~~cpp
tickCodelet(...);
~~~

这就是 DoubleBuffer 语义真正落地的位置。

Receiver 新消息先进入 backstage。

Entity tick 开始前：

~~~text
backstage
↓ syncInbox
mainstage
↓
Codelet sees stable current-stage messages
~~~

所以 `compute()/tick()` 不是在一个不断被 producer 改写的 queue 上随便读取。

## tick 结束后才 syncOutbox

Codelet 运行完：

~~~cpp
for (...) {
  term->onExecute(timestamp);
}

code = router->syncOutbox(entity);
~~~

因此输出语义也是分阶段的：

~~~text
Codelet emit/push
→ transmitter backstage
→ tick completes
→ syncOutbox
→ distribute downstream
~~~

这给一次 Entity tick 提供了接近 transaction boundary 的感觉。

不是严格数据库事务，但至少：

~~~text
当前 tick 输入先固定
业务逻辑执行
输出在 tick 尾部统一传播
~~~

比“业务函数中间 push 一半输出就让下游立刻看到”更容易推理。

## MessageRouter 为什么缓存 receivers_ / transmitters_

`addRoutes()` 会提前：

~~~cpp
receivers_[entity.eid()].insert(rx);
transmitters_[entity.eid()].insert(tx);
~~~

tick 热路径里 `syncInbox(entity)` 不需要每次：

~~~text
扫描 Entity components
找 Receiver
~~~

而是直接从 cache 取。

这又是 control-plane build → data-plane cache 的典型设计。

图激活阶段多做一点工作，换运行阶段更少动态发现。

## Router 为什么同时保存正向和反向路由

连接：

~~~cpp
routes_[tx].insert(rx);
routes_reversed_[rx].insert(tx);
~~~

与 FlowGraph 的 `succ_ / pred_` 完全同样。

正向查询：

~~~text
Transmitter → all Receivers
~~~

反向查询：

~~~text
Receiver → all Transmitters
~~~

用额外索引换 query 效率。

这类模式在 runtime 中反复出现：

~~~text
graph adjacency
route table
service discovery
resource ownership
~~~

都经常需要双向索引。

## syncOutbox() 是消息发布的真正 commit 点

源码：

~~~cpp
RETURN_IF_ERROR(tx->sync());

if (tx->size() > 0) {
  const auto receivers =
      getConnectedReceivers(tx);

  while (tx->size() > 0) {
    Entity message = tx->pop();
    distribute(tx, message, receivers);
  }
}
~~~

然后：

~~~cpp
GxfEntityNotifyEventType(
    entity.context(),
    receiver->eid(),
    GXF_EVENT_MESSAGE_SYNC);
~~~

这里把 data path 和 control path 清楚分开：

~~~text
data:
message copied/shared into receiver backstage

control:
notify downstream Entity state changed
~~~

这正是 Communication Foundations 强调的两条线。

## distribute() 为什么看起来简单得惊人

公开本地 MessageRouter：

~~~cpp
for (const Handle<Receiver>& receiver : receivers) {
  receiver->push(message);
}
~~~

它只是把 `Entity` handle 推给每个 Receiver。

这里大 payload 并没有自动复制一份字节数组。

`Entity` 本身有引用计数语义。

所以 fan-out 更接近：

~~~text
one message object/storage
→ multiple receiver references
~~~

而不是：

~~~text
N subscribers
→ N full deep copies
~~~

当然，Entity 内具体 component 的 ownership 还要继续看 Tensor/allocator。

## DoubleBufferReceiver 的 queue 真正是什么

类型：

~~~cpp
using queue_t =
    gxf::staging_queue::StagingQueue<Entity>;
~~~

不是 `std::queue`。

`StagingQueue` 内部：

~~~cpp
std::vector<T> items_;
size_t begin_;
size_t num_mainstage_;
size_t num_backstage_;
std::mutex mutex_;
~~~

它是一个**预分配的双阶段 ring buffer**。

构造时：

~~~cpp
items_(2 * capacity, null)
~~~

为什么是 `2 * capacity`？

因为 main stage 和 backstage 各自最多允许 capacity 个元素。

这用固定内存换掉运行时 vector 扩容。

## Ring Buffer 的物理布局

概念：

~~~text
| O | X | X | X | B | B | O | O |
      ^           ^
    begin      backstage
~~~

`X` 是当前 mainstage。

`B` 是新到达 backstage。

`push()` 只写 backstage。

`pop()/peek()` 只看 mainstage。

`sync()` 把 backstage 逻辑并入 mainstage。

## 为什么 ring 底层用 vector，不用 deque

固定容量已知，而且只需要 circular index。

`vector<T>` 提供：

~~~text
连续内存
一次性分配
简单 modulo indexing
cache locality 更好
~~~

如果用 deque，虽然前后插入方便，但会引入 segmented storage，且这个结构本来就自己维护 begin index。

所以 fixed ring + vector 很合理。

## kPop overflow 在 backstage 满时为什么要移动元素

当 backstage 自己已经 capacity 满：

~~~cpp
for (size_t i = begin_backstage + 1;
     i < backstage_end; ++i) {
  at(i - 1) = std::move(at(i));
}
~~~

然后把新 item 放最后。

这相当于：

~~~text
drop oldest backstage
keep newest arrivals
~~~

注意这里是 O(N) 移动。

为什么没做更复杂的双 ring index？

因为实现优先简单，而且 overflow 本就不应该是健康 steady state。

这又提醒：

> 异常/过载路径的复杂度可以和正常路径不同。

如果系统长期每帧都触发这个 O(N) overflow，那真正问题是 capacity/policy 配错了。

## sync() 时 overflow 和 push() 时 overflow 是两个不同阶段

即使 backstage 自己没满，也可能发生：

~~~text
main = 3/4
back = 2/4
~~~

一调用 sync：

~~~text
total would be 5 > capacity 4
~~~

此时同样要执行 pop/reject/fault policy。

这就是为什么 StagingQueue 的 overflow 不是一个单纯 `push()` 边界。

双阶段 queue 有两个容量检查点。

## MessageAvailableTerm 为什么看 main + back，现在就完全说通了

因为 Producer push 后：

~~~text
message already exists in backstage
but current Codelet cannot consume it yet
~~~

Scheduler 需要知道“有输入到了”，所以看：

~~~text
size + back_size
~~~

真正 tick 开始，`syncInbox()` 再把它变成可消费 mainstage。

这是一套完整因果链：

~~~text
Producer push
↓
Receiver backstage grows
↓
SchedulingTerm changes
↓
notify Scheduler
↓
Entity READY
↓
worker execute
↓
syncInbox
↓
Codelet sees message
~~~

如果把这些文件分开看，很难建立这条链；串起来后整个 runtime 才清楚。

## MemoryAvailableTerm 又说明 Scheduler 不是只看消息

公开实现：

~~~cpp
const bool is_ready =
    allocator_->is_available(min_bytes_);
~~~

如果用 `min_blocks`：

~~~cpp
min_bytes_ = allocator_->block_size() * min_blocks;
~~~

于是：

~~~text
queue has input
but allocator has no output storage
→ Entity remains WAIT
~~~

这比“先执行再 allocation fail”成熟得多。

## Boolean / Async Term 为什么主动 notify runtime

例如 BooleanSchedulingTerm：

~~~cpp
enable_tick_.set(true);
GxfEntityNotifyEventType(
    context(), eid(),
    GXF_EVENT_STATE_UPDATE);
~~~

AsynchronousSchedulingTerm 在 EVENT_DONE 时：

~~~cpp
GxfEntityEventNotify(context(), eid());
~~~

所以 condition state 改变时不能只改一个 bool。

必须同时：

~~~text
update state
+
wake scheduler
~~~

否则 Scheduler 可能永远睡着。

这和条件变量程序里：

~~~cpp
predicate = true;
cv.notify_one();
~~~

必须成对出现一样。

## 这套设计和工业数据流系统有什么共性

Kafka/stream processor、GPU graph runtime、robot middleware 都在反复处理：

~~~text
1. 输入先 staging
2. readiness 根据输入+资源判断
3. worker 执行一个原子逻辑 step
4. 输出统一 commit
5. downstream 被唤醒
~~~

差别只是 storage 和 transport 不同。

## 对自研具身 Runtime 的一个直接模板

可以定义：

~~~cpp
struct NodeRuntime {
    Inbox inbox;
    Outbox outbox;
    std::vector<Condition*> conditions;
    std::mutex execution_mutex;
};
~~~

Scheduler：

~~~text
event
→ check all conditions
→ READY / WAIT / WAIT_TIME / WAIT_EVENT
~~~

Worker：

~~~text
lock execution
→ inbox.sync()
→ node.compute()
→ conditions.on_execute()
→ outbox.sync()
→ notify downstream
~~~

这里最重要的是 tick boundary。

只要 tick 边界定义清楚，输入 snapshot、输出 visibility、资源归还和 profiling 才有稳定语义。

## 本篇应该真正记住的六个不变量

1. Scheduler 的 READY 只是候选，真正执行前仍需重新验证条件。
2. 同一 Entity 不能 re-enter；调度 ownership 与 execution mutex 可以分层。
3. 输入先 staging，tick 前统一 sync；输出 tick 后统一 sync。
4. data route 与 wakeup notification 是两条不同路径。
5. queue occupancy、memory availability、timer 和 async event 都是同一 readiness predicate 的组成部分。
6. 一个 runtime tick 最重要的不是“调用 compute”，而是定义 compute 前后哪些状态对谁可见。
