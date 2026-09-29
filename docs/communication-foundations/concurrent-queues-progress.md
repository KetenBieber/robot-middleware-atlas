# 并发队列与进展保证：从 SPSC 到 MPMC，真正读懂 Lock-free / Wait-free

:::{contents} 本页目录
:depth: 3
:local:
:::

很多并发教程在这里会突然开始堆术语：

~~~text
SPSC
MPSC
MPMC
CAS
lock-free
wait-free
ABA
memory reclamation
~~~

如果只记名字，几乎无法迁移到真实程序设计。真正应该建立的是一套更底层的分析方法：

> 一个并发数据结构，本质上是在多个执行流之间维护一台共享状态机。设计者必须同时证明 **状态不会坏、数据不会读错、对象不会过早释放，以及每个参与者能否继续前进**。

所以分析任何 queue、mailbox、ring、work-stealing deque 或 shared state，都先拆成四条线：

~~~text
1. State
   哪些字段共同定义容器状态？

2. Ownership
   哪个线程可以修改哪些字段和槽位？

3. Publication
   payload 写完以后，靠什么让另一个线程“合法地看到”？

4. Progress
   竞争发生时，谁可能等待多久？
~~~

这四条线比“用了没有 mutex”更重要。

## 先把 Safety 和 Progress 分开

并发算法常把两个完全不同的问题混在一起。

Safety 关心“绝不能发生什么”：

~~~text
不能读到半写入 payload
不能两个 producer 占用同一个 slot
不能把仍在读取的对象复用
不能让 head/tail 进入不可能状态
不能 use-after-free
~~~

Progress 关心“竞争时还能不能完成”：

~~~text
某线程会不会睡眠？
某线程会不会一直 CAS 失败？
系统整体是否至少有人前进？
每一次操作是否都有有限步骤上界？
~~~

一个算法完全可能 Safety 正确，但某个线程永久饥饿。也可能单线程测试一直成功，但跨线程存在 data race。

因此：

> **正确性不等于实时性，非阻塞也不等于有界延迟。**

## Linearizability：并发操作到底“在哪一刻算发生了”

假设两个 Producer 几乎同时 enqueue：

~~~text
P0: enqueue(A)
P1: enqueue(B)
C : dequeue()
~~~

机器内部可能经历很多 load、CAS、slot write。对调用者来说，我们希望每个操作都像在某一个瞬间原子发生，并且这个虚拟顺序不违背真实时间上的先后约束。

这个性质称为 **linearizability**。

常见 linearization point：

~~~text
mutex queue:
  成功修改受锁容器的临界区

SPSC enqueue:
  发布 head 的 release-store

CAS stack push:
  成功把 top 从 old 改为 new 的 CAS

sequence-slot queue:
  slot.sequence 从“可写”改成“可读”的发布 store
~~~

找不到 linearization point，通常说明你还没有真正理解这个数据结构什么时候对其他线程生效。

## Blocking、Obstruction-free、Lock-free、Wait-free 到底差什么

| 机制 | 能否阻塞 OS 线程 | 保证谁能前进 | 单操作是否有步骤上界 |
| --- | --- | --- | --- |
| blocking / lock-based | 可以 | 取决于锁持有者和调度 | 通常没有 |
| obstruction-free | 不一定 | 单独运行足够久的线程 | 不保证竞争下完成 |
| lock-free | 不要求 mutex 阻塞 | **系统整体**持续有人完成操作 | 不保证某一个线程 |
| wait-free | 不要求等待其他线程完成 | **每个线程**都能完成 | 有算法步骤上界 |

这里最容易误解的是 lock-free。

假设三个 Producer 不断 CAS：

~~~text
P0 CAS fail
P1 CAS success

P0 CAS fail
P2 CAS success

P0 CAS fail
P1 CAS success
...
~~~

整个系统一直在前进，所以仍然满足 lock-free；但 P0 可以一直失败。

因此：

> lock-free 保证 global progress，不保证 per-thread latency。

这也是为什么严格实时线程不能仅凭“这个 queue 是 lock-free”就认为 WCET 已经解决。

### Wait-free 为什么更难

如果核心循环是：

~~~cpp
while (!state.compare_exchange_weak(expected, desired)) {
    recompute();
}
~~~

竞争次数没有固定上界，它通常不满足 wait-free。

真正的 wait-free 算法往往需要：

- 给每个参与者固定 slot；
- operation descriptor；
- bounded retry；
- helping：一个线程帮助另一个线程完成已公布操作；
- 更复杂的版本/epoch 状态。

所以工程上不是“wait-free 一定高级”，而是只有当业务真的需要 per-operation progress bound 时，才值得支付这种复杂度。

## Mutex Queue 并不低级

先建立一个容易证明的基准设计：

~~~cpp
template<class T>
class BoundedQueue {
public:
    bool push(T value) {
        std::unique_lock lock(m_);
        not_full_.wait(lock, [&] {
            return closed_ || queue_.size() < capacity_;
        });

        if (closed_) return false;

        queue_.push_back(std::move(value));
        not_empty_.notify_one();
        return true;
    }

    bool pop(T& out) {
        std::unique_lock lock(m_);
        not_empty_.wait(lock, [&] {
            return closed_ || !queue_.empty();
        });

        if (queue_.empty()) return false;

        out = std::move(queue_.front());
        queue_.pop_front();
        not_full_.notify_one();
        return true;
    }

private:
    std::mutex m_;
    std::condition_variable not_empty_;
    std::condition_variable not_full_;
    std::deque<T> queue_;
    std::size_t capacity_;
    bool closed_{false};
};
~~~

这个设计拥有几个很强的工程性质：

~~~text
状态集中
不变量容易写
关闭语义容易实现
异常路径容易推理
可以直接用 ThreadSanitizer 检查
~~~

如果临界区很短、竞争低，它可能比复杂 MPMC lock-free queue 更合适。

程序设计不是为了消灭 mutex，而是为了找到：

> **最小但足够的同步机制。**

## CAS：Lock-free 结构最常见的竞争仲裁原语

Compare-And-Swap 可以抽象为：

~~~text
if memory == expected:
    memory = desired
    return success
else:
    expected = memory
    return failure
~~~

C++：

~~~cpp
std::size_t expected = old_pos;

if (head.compare_exchange_weak(
        expected,
        new_pos,
        std::memory_order_acq_rel,
        std::memory_order_acquire)) {
    // reservation succeeded
}
~~~

CAS 失败时，C++ 会把实际读到的值写回 expected，所以经典循环可以基于更新后的状态重新计算。

compare_exchange_weak 允许 spurious failure，即比较值相等也允许失败一次。循环里通常没问题，因为下一轮继续尝试。strong 更适合“不准备重复很多次”的场景。

不要把 weak 理解成“不安全”。它仍然是原子操作，只是允许更多失败结果。

## 为什么多个 Producer 不能只共享一个普通 head++

SPSC 中只有 Producer 写 head，所以 Producer 不需要和另一个 Producer 争 head。

MPSC 中：

~~~text
P0 ----\
P1 -----+--> head
P2 ----/
~~~

如果写成：

~~~cpp
auto pos = head;
head = head + 1;
~~~

两个 Producer 可能同时读取旧值 7，然后都认为自己拿到了 slot 7。

因此 MPSC 的第一个新增问题就是：

> **reservation 必须原子化。**

最直接可以用：

~~~cpp
auto pos = head.fetch_add(1, std::memory_order_relaxed);
~~~

每个 Producer 会得到不同 ticket。

但这还没有解决完整问题。

## “拿到 ticket”不等于“slot 已经可读”

假设：

~~~text
P0 reserve slot 10
P1 reserve slot 11

P0 被 OS 抢占
P1 很快写完 slot 11
~~~

如果 Consumer 只看全局 head：

~~~text
head == 12
~~~

它可能错误地认为 10、11 都已经发布。

这暴露一个关键区别：

~~~text
reservation position
!=
publication state
~~~

因此多 Producer ring 常把状态下沉到每一个 slot。

## Per-slot Sequence：把“槽位身份”编码进数据结构

设容量 N=4。每个 slot 不只有 payload，而是：

~~~cpp
template<class T>
struct Slot {
    std::atomic<std::uint64_t> sequence;
    T payload;
};
~~~

初始化：

~~~text
slot 0 sequence = 0
slot 1 sequence = 1
slot 2 sequence = 2
slot 3 sequence = 3
~~~

Producer 拿到逻辑位置 p 后，只能在：

~~~text
slot[p % N].sequence == p
~~~

时写入。

写完 payload 后：

~~~cpp
slot.sequence.store(p + 1, std::memory_order_release);
~~~

这一步才是：

> **这个 slot 对 Consumer 正式发布。**

Consumer 要读逻辑位置 p，先用 acquire-load 检查：

~~~text
slot.sequence == p + 1
~~~

消费完成后把槽位归还给下一圈 Producer：

~~~cpp
slot.sequence.store(p + N, std::memory_order_release);
~~~

于是同一个物理槽的 sequence 会经历：

~~~text
slot 0:

0      可写给 logical position 0
1      position 0 已发布，可读
4      可写给 logical position 4
5      position 4 已发布，可读
8      可写给 logical position 8
...
~~~

sequence 同时编码：

1. 当前物理槽属于哪一轮；
2. Producer 是否已经真正写完；
3. Consumer 是否已经释放这一轮；
4. wrap-around 后旧状态不能被误认为新状态。

这就是为什么很多高性能 bounded queue 不只保存 head/tail，而会给每个 slot 加 generation/sequence。

## MPSC：多生产者、单消费者可以简化哪一半

MPSC：

~~~text
Producer 0 --\
Producer 1 ---+--> queue --> one Consumer
Producer 2 --/
~~~

竞争主要发生在 enqueue reservation。

Consumer 只有一个，所以 dequeue 位置可以是 Consumer 私有变量，不需要和其他 Consumer CAS 抢读取位置。

概念化流程：

~~~text
Producer:
  1. 原子获得 ticket p
  2. 找 slot[p % N]
  3. 确认 slot 属于这一轮且可写
  4. 写 payload
  5. release 发布 sequence = p + 1

Consumer:
  1. 检查 slot[c % N].sequence == c + 1
  2. acquire
  3. 读 payload
  4. release sequence = c + N
  5. c++
~~~

这种模型适合：

~~~text
多个 sensor callback
多个 event source
多个 worker completion
             ↓
      一个 supervisor thread
~~~

## MPMC：Consumer 端也要竞争 reservation

MPMC：

~~~text
P0 --\
P1 ---+--> queue --> C0
P2 --/           --> C1
                  --> C2
~~~

现在两个方向都有竞争：

~~~text
enqueue position
dequeue position
~~~

Consumer 也必须通过 CAS、fetch-add 或其他原子协议取得唯一 ticket。

这时 per-slot sequence 更重要，因为全局 enqueue_pos / dequeue_pos 只说明逻辑位置被“认领”到哪里，不能单独证明 payload 已完成发布或已完成回收。

### MPMC 的真实成本来自哪里

热点通常包括：

- 多核反复修改 enqueue counter；
- 多核反复修改 dequeue counter；
- CAS failure；
- 同一 cache line bouncing；
- per-slot sequence load；
- 满/空时 spin/backoff；
- payload 构造/析构；
- NUMA remote access。

竞争高时，一把设计良好的分片锁或 per-core queue 可能反而更稳定。

## Sharding：很多时候最好的 MPMC 是少共享

如果 16 个 Producer 都竞争一只全局 queue：

~~~text
16 cores
   ↓
one enqueue cache line
~~~

不一定要继续优化 CAS。

另一条路是：

~~~text
Producer group A -> queue A
Producer group B -> queue B
Producer group C -> queue C
~~~

Consumer 再做 merge。

核心思想：

> **最便宜的共享状态，是根本不共享。**

这也是程序组织能力的重要部分：先改变 topology，再优化 atomic。

## ABA：值“看起来没变”，身份已经换了一轮

假设 lock-free stack：

~~~text
top = A
~~~

Thread 0 读到 A 后被暂停。期间 Thread 1：

~~~text
pop A
pop B
push A
~~~

top 又变成 A。

Thread 0 醒来，CAS 看到 expected 仍等于 A，于是可能成功，但状态已经不是原来那一轮。

这就是 ABA。

常见解决方向：

~~~text
pointer + version counter
tagged pointer
per-slot generation
hazard pointer
epoch
不复用节点直到安全期结束
~~~

bounded ring 的 sequence number 本质上也在解决一种“物理槽复用后的身份混淆”。

## Memory Reclamation：Lock-free 最容易被低估的部分

固定数组 ring 的 slot 生命周期通常等于 queue 生命周期，所以 reclamation 相对简单。

但 linked lock-free queue：

~~~text
Node A -> Node B -> Node C
~~~

Consumer 把 A 从链上摘掉后，不能立即 delete A，因为另一个线程可能刚刚读到 A 的地址，尚未完成后续 CAS。

所以必须回答：

> **什么时候可以证明再也没有线程握着旧节点地址？**

常见方法：

### Hazard Pointer

线程先公布“我正在访问 Node A”。回收者只有确认所有 hazard slot 都不指向 A，才释放。

优点是回收及时；代价是每次访问要维护共享 metadata，并扫描 hazard set。

### Epoch-based Reclamation

线程宣布自己当前处于某个 epoch。节点退休后不立即 free，等所有可能见过旧节点的线程都离开旧 epoch，再统一释放。

热路径较轻，但长期不 quiescent 的线程会拖住回收。

### Reference Counting

思路直接，但频繁原子 inc/dec 会带来 contention，而且“有引用计数”不代表整套 lock-free 生命周期协议自动正确。

所以：

> 写出 CAS 链表只是 lock-free queue 的前半部分；安全回收才是后半部分。

## Memory Order：不要从 x86 的表现反推语言保证

很多错误无锁代码在 x86 上“跑了几年没问题”，原因之一是 x86 TSO 比一些架构提供更强的硬件顺序。

但 C++ 程序应该首先按 C++ memory model 证明：

~~~text
payload write
↓
release publish
↓
acquire observe
↓
payload read
~~~

编译器再把它映射到：

~~~text
x86-64
ARMv8
RISC-V
其他支持 C++ atomics 的平台
~~~

因此：

> 不要写“在我的 x86 上没重现”的并发协议；要写能在语言内存模型里证明的协议。

## Atomic 不保证“硬件一定一条指令”

std::atomic<uint64_t> 保证的是原子语义，不自动保证：

~~~text
lock-free
单条 CPU 指令
固定时延
~~~

可以检查：

~~~cpp
std::atomic<std::uint64_t>::is_always_lock_free
~~~

某些 32-bit MCU 上，64-bit atomic 可能需要库函数或临界区。

所以跨平台设计时要把两层分开：

~~~text
语言语义正确
        ↓
目标平台实现成本
~~~

## 从 x86 到 ARM / MCU：哪些思想真正能迁移

真正可迁移的不是某条 LOCK CMPXCHG 指令，而是：

~~~text
single-writer ownership
bounded storage
monotonic sequence
generation counter
publish-before-notify
data path / control path separation
explicit overflow policy
lifetime state machine
~~~

这些设计思想可以落在不同底层原语上。

### Linux / x86 或 ARM

可以使用：

~~~text
std::atomic
futex
eventfd
epoll
pthread mutex
~~~

只要按 C++ happens-before 设计，编译器会在具体架构上产生对应的 acquire/release 或 barrier。

### 单核 MCU：ISR → Main Loop

一个 UART ISR 生产字节，主循环消费：

~~~text
ISR
  ↓
SPSC ring
  ↓
main loop
~~~

仍然有：

~~~text
one producer
one consumer
bounded capacity
head/tail
overflow policy
~~~

但同步可能变成短临界区、禁中断窗口或架构原子指令，而不是 condition_variable。

### DMA → CPU

DMA engine 不是普通 C++ thread，它可能绕开 CPU cache 或需要 cache maintenance / device memory barrier。

但更高层的不变量仍然一样：

~~~text
DMA owns buffer while filling
↓
completion event
↓
CPU acquires ownership
↓
CPU processes
↓
buffer returned to DMA pool
~~~

最有价值的能力不是背一个并发库，而是识别：

> **谁是 Producer，谁拥有 slot，哪个状态转移代表发布，什么时候才能复用。**

## Volatile 为什么不是线程同步工具

volatile 主要表达“这个访问不能被当作普通可消除内存访问”。

它不提供 C++ 线程间：

- atomicity；
- happens-before；
- mutex；
- publication ordering。

所以 volatile bool ready 不能替代 std::atomic<bool> ready。

在 memory-mapped I/O 里 volatile 可能有设备寄存器语义，但设备访问顺序还可能需要平台 barrier；这是另一个层级的问题。

## False Sharing：无锁算法也可能被 cache coherence 拖垮

假设：

~~~cpp
struct QueueState {
    std::atomic<std::uint64_t> enqueue;
    std::atomic<std::uint64_t> dequeue;
};
~~~

两个字段逻辑独立，但若位于同一 cache line，Producer cores 和 Consumer cores 会反复让同一 cache line 在核心间迁移。

因此常见布局会把热点状态分开。C++17 还提供 std::hardware_destructive_interference_size 来表达这种意图；实际平台仍应测量。

## Backoff：CAS 失败以后“立刻再撞一次”不一定合理

高竞争循环：

~~~cpp
while (!try_push(x)) {
}
~~~

可能造成：

~~~text
cache line bouncing
branch pressure
CPU 100%
其他线程更难完成
~~~

常见策略包括短 pause/yield、指数退避、超过阈值后 sleep、分片 queue。

实时系统又不能无脑 sleep，因为尾延迟会扩大。所以 backoff 本身也是调度策略。

## Queue Type 不只由“几个线程”决定

SPSC/MPSC/MPMC 只描述并发拓扑：

~~~text
SPSC: 1 Producer, 1 Consumer
MPSC: many Producers, 1 Consumer
SPMC: 1 Producer, many Consumers
MPMC: many Producers, many Consumers
~~~

还必须再加业务语义：

~~~text
FIFO?
latest-only?
lossless?
drop-old?
blocking?
bounded?
priority?
deadline?
fan-out?
~~~

例如一个 Producer、四个 Consumer 并不一定应该使用 destructive SPMC queue。如果四个 Consumer 都必须看到同一条状态，更像：

~~~text
Producer
   ├── consumer A cursor
   ├── consumer B cursor
   ├── consumer C cursor
   └── consumer D cursor
~~~

这就是 multi-reader ring / fan-out buffer 的问题。

Cyber 的每 DataVisitor 独立 ring 正是在避免 destructive competition。

## Seqlock / Versioned Mailbox：状态流不一定需要 Queue

如果控制器永远只关心最新姿态：

~~~text
position
velocity
timestamp
~~~

FIFO 可能不是最自然结构。

一个典型思想是 versioned snapshot：

~~~text
version odd   -> writer 正在修改
version even  -> snapshot 稳定
~~~

Reader：

~~~text
v0 = version
if odd: retry

copy state

v1 = version
if v0 != v1: retry
~~~

Writer 是单写者时，这类结构可以用很低成本提供“读到一致 snapshot”的能力。

它不保存历史，而是让 Reader 要么读到一个完整版本，要么重试。

适合：

~~~text
latest robot state
latest calibration
latest control target
~~~

但不适合必须保留每个事件的业务。

## 一个实用选择矩阵

| 场景 | 首选思路 | 为什么 |
| --- | --- | --- |
| 一个 sensor thread → 一个算法 thread | SPSC bounded ring | 单写 head / 单写 tail，协议最简单 |
| 多 callback → 一个 supervisor | MPSC bounded queue | Producer 竞争，Consumer 简化 |
| 多 worker → 多 worker task pool | MPMC / sharded queues | 双向竞争，需要更强协议 |
| 控制器只读最新状态 | mailbox / double buffer / seqlock | 避免历史排队 |
| 必须完整处理任务 | bounded blocking queue | 语义清晰，天然 backpressure |
| 多事件源唤醒一个线程 | eventfd/epoll/reactor | payload 与 notification 分离 |
| 严格实时核心 | 尽量单写者、固定容量、预分配 | 降低竞争和动态资源不确定性 |

这张表不是性能排名，而是提醒：

> **先选择正确的数据流拓扑，再选择并发原语。**

## 把这些机制映射回 Atlas 的真实源码

### Cyber：Ring + Dispatcher + Notifier

- [有界消息缓存：pending queue ring](../generated/cyber/pending-queue-ring.md)
- [Dispatcher / Notifier](../generated/cyber/dispatcher-notifier.md)
- [CRoutine wakeup](../generated/cyber/croutine-wakeup.md)

重点看它为何选择：

~~~text
每 Consumer 独立 ring
+
短 mutex 临界区
+
通知与 payload 分离
+
Scheduler wakeup
~~~

这是一种很现实的“正确性优先，再控制共享范围”的设计。

### Fast DDS：多 Writer 的异步调度

- [FlowController 与异步发送](../generated/fastdds/flowcontroller-async.md)

这里看 unordered_map<Writer*, Queue> 与 map<Priority, vector<Writer*>> 如何把访问模式翻译成 STL 数据结构，而不是只看 DDS API。

### iceoryx2：Event / Reactor

- [Thread Safety、Event 与 Reactor](../generated/iceoryx2/thread-safety-event-reactor.md)
- [Event Notifier / Listener](../generated/iceoryx2/event-notifier-listener.md)

它很好地展示：

~~~text
payload ownership
!=
event wakeup
!=
scheduler
~~~

### UCX：Progress Engine

- [UCX Progress Engine](../generated/ucx/progress-engine.md)
- [Backpressure 与 thread safety](../generated/ucx/backpressure-thread-safety.md)

这里要问：

~~~text
谁调用 progress？
busy poll 还是 event-driven？
MULTI thread mode 的同步成本是什么？
推理线程占满 CPU 会不会饿死通信 progress？
~~~

## 最后把问题收束成“设计一条数据流”

当你不是在写 middleware，而只是写一个控制程序时，仍然可以照搬这套方法：

~~~text
Sensor thread
↓
SPSC ring / latest mailbox
↓
Estimator thread
↓
versioned state
↓
Controller thread
↓
MPSC event queue
↓
Supervisor
~~~

每一条边都明确：

~~~text
Producer 数量
Consumer 数量
payload ownership
capacity
overflow
publication point
wakeup mechanism
shutdown
progress guarantee
~~~

这时学到的已经不再是“通信中间件知识”，而是**如何组织并发程序的数据流**。
