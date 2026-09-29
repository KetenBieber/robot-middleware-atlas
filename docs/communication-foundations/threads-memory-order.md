# 线程间通信：共享地址不等于共享时序

:::{contents} 本页目录
:depth: 3
:local:
:::

跨线程是最容易被低估的一层。两个线程共享同一个进程、同一个虚拟地址空间，甚至可以拿到同一根指针，于是它看起来不像“真正的通信”。

但只要 Producer 和 Consumer 不再处于同一条执行序列里，问题就从“地址能不能访问”变成：

> Consumer 凭什么知道 Producer 已经写完，而且自己读到的是那次写之后的内容？

这就是线程通信的核心：**共享对象 + 跨线程时序证明**。

## 第一层问题：C++ 源码顺序不等于另一个核心看到的顺序

先看一个很自然但错误的版本：

~~~cpp
Frame frame;
bool ready = false;

// producer
frame = capture();
ready = true;

// consumer
if (ready) {
    process(frame);
}
~~~

人眼会按顺序理解成：

~~~text
先写 frame
再写 ready=true
Consumer 看见 ready=true
于是 frame 肯定写完
~~~

但 C++ 内存模型不允许你这样推理。

这里至少有三层可能改变观察结果：

1. 编译器可以在不破坏单线程语义时重排普通内存访问；
2. CPU core 之间通过 cache coherence 协调，但这并不等于所有普通 load/store 自动形成语言层面的同步；
3. 对 **ready** 的并发普通读写本身就是 data race，程序行为已经失去合法定义。

因此正确问题不是“CPU 会不会刷新缓存”，而是：

> 哪一个同步操作建立了 happens-before？

## Mutex 为什么同时解决互斥和可见性

最直接的版本是 mutex：

~~~cpp
std::mutex m;
Frame frame;
bool ready = false;

void publish(Frame f) {
    std::lock_guard<std::mutex> lock(m);
    frame = std::move(f);
    ready = true;
}

Frame consume() {
    std::lock_guard<std::mutex> lock(m);
    return frame;
}
~~~

mutex 做了两件不同的事。

### Mutual exclusion

同一时间只有一个线程可以进入临界区。

### Synchronization

Producer 的 unlock 与随后成功获得同一把 mutex 的 Consumer lock 之间形成同步关系。

于是可以画成：

~~~text
Producer
write frame
write ready
unlock(m)
    │
    │ synchronizes-with
    ▼
lock(m)
read ready
read frame
Consumer
~~~

这条边存在以后，Consumer 才有语言层面依据认为自己看到的是 Producer 发布后的状态。

## Condition Variable：通知只负责“唤醒候选者”

Producer/Consumer 更常见的是：

~~~cpp
std::mutex m;
std::condition_variable cv;
std::queue<Frame> q;

void publish(Frame f) {
    {
        std::lock_guard<std::mutex> lock(m);
        q.push(std::move(f));
    }
    cv.notify_one();
}
~~~

Consumer：

~~~cpp
Frame take() {
    std::unique_lock<std::mutex> lock(m);

    cv.wait(lock, [&] {
        return !q.empty();
    });

    Frame f = std::move(q.front());
    q.pop();
    return f;
}
~~~

这里最重要的不是 **notify_one()** 这个 API，而是时间线：

~~~text
Producer
push
unlock
notify
  │
  ▼
Consumer 从 blocked 变成 runnable
  │
  ▼
OS scheduler 决定它什么时候真正运行
  │
  ▼
Consumer 重新竞争 mutex
  │
  ▼
再次检查 predicate
  │
  ▼
pop
~~~

因此：

> notify 不等于立即执行 callback。

如果系统有严格的 1 ms deadline，那么线程唤醒、run queue、优先级和 mutex 竞争都必须进入延迟预算。

### 为什么 wait 必须带 predicate

condition variable 允许 spurious wakeup，而且 notify 也可能发生在 Consumer 真正 wait 之前。

所以逻辑不能写成：

~~~cpp
cv.wait(lock);
return q.front();
~~~

而应该始终围绕共享状态：

~~~text
“只要 queue 非空，我就能继续。”
~~~

condition variable 是“减少无意义等待”的机制，**queue 是否为空才是事实来源**。

## Acquire / Release：给普通数据建立发布边

如果一个对象只由 Producer 写一次，Consumer 在发布后只读，可以用原子变量建立发布关系：

~~~cpp
Frame frame;
std::atomic<bool> ready{false};

// producer
frame = capture();
ready.store(true, std::memory_order_release);

// consumer
if (ready.load(std::memory_order_acquire)) {
    process(frame);
}
~~~

可以把它理解成一扇门：

~~~text
Producer side
----------------------
write frame
write metadata
release-store ready=true
            │
            │ synchronizes-with
            ▼
acquire-load ready==true
read metadata
read frame
----------------------
Consumer side
~~~

release 的含义不是“立刻把所有缓存刷到内存”，acquire 也不是“强制重新读 DRAM”。

更准确的理解是：

> release/acquire 在 C++ 内存模型里给一组读写建立可证明的顺序边。

当 Consumer 的 acquire 确实读到了 Producer release 写出的那个值时，发布点之前的写就 happens-before Consumer 在 acquire 之后的读。

## 为什么 relaxed 不够做“发布数据”

把上面的 ready 改成：

~~~cpp
ready.store(true, std::memory_order_relaxed);
~~~

relaxed 仍能保证这个 atomic 自身的读改写原子性，但不负责把普通的 **frame** 写入与 ready 的发布绑定成跨线程顺序。

它适合计数器、统计量等只关心 atomic 自身值的场景，却不能单独承担“对象已经初始化完毕”的发布语义。

## Queue 其实是“数据结构语义 + 同步语义”

谈线程通信时，很容易把讨论简化成：

~~~text
vector 还是 deque？
mutex 还是 lock-free？
~~~

但队列首先要回答业务语义。

### FIFO：每条都要保留

~~~text
100 → 101 → 102 → 103
              ↑
         Consumer 逐个处理
~~~

适合命令任务、日志、事务型工作。

### Latest-value：旧数据一旦过期就没有价值

控制器读状态时，Consumer 如果落后：

~~~text
pose 100
pose 101
pose 102
~~~

它可能真正想要的是：

~~~text
直接读 pose 102
~~~

这时一个深 FIFO 反而制造 data age。

### Bounded work queue：保留有限历史

感知 pipeline 可能允许最多缓存 2～3 帧，超过以后 drop-old。

这种语义必须先定，再决定容器。

## SPSC Ring：为什么单生产者单消费者可以做得很轻

Single Producer Single Consumer ring 的关键结构非常简单：

~~~text
slots[0 ... N-1]

Producer owns head
Consumer owns tail
~~~

Producer：

~~~cpp
auto next = (head + 1) % N;
if (next == tail.load(std::memory_order_acquire)) {
    // full
}

slots[head] = item;
head.store(next, std::memory_order_release);
~~~

Consumer：

~~~cpp
if (tail == head.load(std::memory_order_acquire)) {
    // empty
}

auto item = slots[tail];
tail.store((tail + 1) % N, std::memory_order_release);
~~~

这里设计成立的原因是：

- 只有 Producer 修改 head；
- 只有 Consumer 修改 tail；
- 双方读取对方索引来判断 full/empty；
- payload 写入和 head 发布之间需要正确的 release/acquire。

所谓“lock-free ring”并不是把同步消失了，而是把：

~~~text
一个共享 mutex
~~~

拆成了：

~~~text
两个单写者索引 + 有方向的内存顺序
~~~

## MPSC / MPMC 为什么复杂度突然上升

一旦多个 Producer 同时抢同一个 enqueue 位置：

~~~text
Producer A ─┐
Producer B ─┼→ next slot?
Producer C ─┘
~~~

就需要对“谁获得哪个 slot”做竞争仲裁。

常见机制包括：

- CAS；
- ticket/sequence number；
- per-slot state；
- linked nodes；
- sharded queues；
- central lock。

所以看到一个 MPMC queue 时，不要只看“没有 mutex”，还要看它是否在高冲突下反复 CAS，以及 memory reclamation 怎么做。

## Lock-free 不等于 Wait-free，更不等于固定 WCET

几个概念必须分开：

- **blocking**：线程可能因为锁、条件变量、系统调用睡眠；
- **lock-free**：系统整体保证持续前进，但某一个线程可能一直失败；
- **wait-free**：每个操作都能在有界步骤内完成。

一个 lock-free CAS loop：

~~~cpp
while (!state.compare_exchange_weak(old, desired)) {
    // retry
}
~~~

没有 mutex，但某个线程仍可能反复失败。

因此：

> “没有锁”不能直接推出“实时性更好”。

实时系统更关心的是最坏执行路径、调度优先级、缓存行为和 contention 上界。

## False Sharing：逻辑上互不相关，物理 cache line 却在打架

假设 head 和 tail 恰好落在同一 cache line：

~~~text
cache line
+-------------------------------+
| head | tail | other metadata  |
+-------------------------------+
  ↑      ↑
 Core0  Core1
 write  write
~~~

Producer 频繁写 head，Consumer 频繁写 tail。

虽然它们从不写同一个变量，但 cache coherence 以 cache line 为粒度，line 可能在两个核心间反复迁移。

因此高频 ring 常会把热点状态分开：

~~~cpp
struct alignas(64) ProducerState {
    std::atomic<size_t> head;
};

struct alignas(64) ConsumerState {
    std::atomic<size_t> tail;
};
~~~

这就是为什么“数据结构布局”本身会影响通信性能。

## 线程唤醒是一个完整调度链，不是一个函数调用

哪怕 queue 和 memory order 都正确，Consumer 仍可能在睡眠。

真实链路经常是：

~~~text
Producer commits data
↓
event / futex / condition variable
↓
kernel marks Consumer runnable
↓
scheduler chooses a core
↓
context switch
↓
Consumer resumes
↓
cache working set warms up
↓
callback runs
~~~

对于 10 Hz 的 UI，这些成本无所谓；对于 1 kHz 控制环，它们可能是主要抖动来源。

因此需要区分：

~~~text
busy polling
blocking wait
hybrid spin-then-sleep
event-driven callback
fixed-rate polling
~~~

每一种都是延迟、CPU 占用、能耗和可预测性的交换。

## Priority inversion：高优先级线程也可能被低优先级资源持有者拖住

假设：

~~~text
Low priority thread
holds mutex
    ↓
High priority control thread blocks on mutex
    ↓
Medium priority thread keeps running
~~~

高优先级线程最终会被一个低优先级锁持有者间接阻塞。

因此实时系统会关心 priority inheritance、priority ceiling，或者通过单写者数据结构减少共享锁。

这也是“线程通信机制”最终会和调度策略连起来的原因。

## Intra-process zero-copy 的真正条件

同进程最容易做 zero-copy，但也不是“传个 shared_ptr 就结束”。

至少要回答：

~~~text
Producer 发布后还能不能修改？
多个 Consumer 是只读还是可写？
最后一个引用何时释放？
allocator 是否产生抖动？
callback 是否可能把对象长期持有？
~~~

一个合理模型往往是：

~~~text
mutable producer-owned object
↓ publish
immutable shared object
↓ fan-out read
last reader releases
↓ recycle to pool
~~~

这已经和跨进程 shared-memory loan 的状态机非常接近。

## 把 Atlas 中的线程机制放回这张图

读具体中间件时，可以直接用这一页的框架：

- Cyber：Dispatcher / Notifier / CRoutine Scheduler 的数据提交与唤醒；
- LCM：receive queue、notify pipe、handle 线程；
- Fast DDS：FlowController、receiver thread、WaitSet；
- Cyclone DDS：receive/delivery thread、sendq、WaitSet；
- iceoryx2：event/listener/reactor 与 zero-copy queue。

具体实现名字不同，但都在回答同一组问题：

~~~text
谁生产？
谁消费？
数据结构是什么？
同步边在哪里？
谁负责唤醒？
回调运行在哪条执行流？
队列满了怎么办？
~~~

一旦这些问题能从源码里逐一指出来，线程间 communication 才算真正拆完。
