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
## Condition Variable：先把“两个线程 + 三个共享对象”说清楚

这一节先不谈抽象结论，先看一个**完整、可直接编译运行**的 Producer / Consumer 小程序。

### 先回答最容易混淆的问题：这到底是不是两个线程？

是。

下面这个程序里：

- `producer_thread` 是一个独立线程；
- `consumer_thread` 是另一个独立线程；
- 两个线程运行在同一个进程中；
- 它们共享同一个 `SharedQueue shared;` 对象；
- 因而也共享其中**同一份** `mutex`、`condition_variable` 和 `queue`。

完整程序：

~~~cpp
#include <condition_variable>
#include <chrono>
#include <iostream>
#include <mutex>
#include <queue>
#include <thread>

struct Frame {
    int id;
};

struct SharedQueue {
    std::mutex m;
    std::condition_variable cv;
    std::queue<Frame> q;
};

void producer(SharedQueue& shared) {
    for (int i = 0; i < 5; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        {
            std::lock_guard<std::mutex> lock(shared.m);
            std::cout << "[producer] push frame " << i << "\n";
            shared.q.push(Frame{i});
        }

        shared.cv.notify_one();
    }
}

void consumer(SharedQueue& shared) {
    for (int i = 0; i < 5; ++i) {
        std::unique_lock<std::mutex> lock(shared.m);

        shared.cv.wait(lock, [&] {
            return !shared.q.empty();
        });

        Frame f = shared.q.front();
        shared.q.pop();

        std::cout << "[consumer] take frame " << f.id << "\n";
    }
}

int main() {
    SharedQueue shared;

    std::thread producer_thread(producer, std::ref(shared));
    std::thread consumer_thread(consumer, std::ref(shared));

    producer_thread.join();
    consumer_thread.join();
}
~~~

Linux / GCC 可以直接：

~~~bash
g++ -std=c++17 -O2 -pthread producer_consumer.cpp -o producer_consumer
./producer_consumer
~~~

典型输出：

~~~text
[producer] push frame 0
[consumer] take frame 0
[producer] push frame 1
[consumer] take frame 1
...
~~~

这里最关键的是先把对象关系画出来：

~~~text
                    同一个进程

              SharedQueue shared
             /        |         \
            /         |          \
           v          v           v
     shared.m     shared.cv     shared.q
      mutex       condition      queue
                    variable

          ^                         ^
          |                         |
  producer_thread            consumer_thread
          |                         |
          +------ 都持有 shared 的引用 ------+
~~~

所以不是 Producer 有自己的 `cv`、Consumer 又有自己的 `cv`，而是：

~~~text
Producer 和 Consumer 都访问同一个 shared.cv
Producer 和 Consumer 都访问同一个 shared.m
Producer 和 Consumer 都访问同一个 shared.q
~~~

如果分别创建两份 `SharedQueue`，那两个线程就根本没有通过这组对象通信。

---

## Mutex 在这里到底保护什么？

先暂时把 condition variable 拿掉，只看 queue。

`std::queue` 本身不保证多个线程并发访问安全，所以我们规定：

> 任何线程只要想读写 `shared.q`，都必须先拿到同一把 `shared.m`。

因此：

~~~cpp
std::lock_guard<std::mutex> lock(shared.m);
shared.q.push(...);
~~~

不是说 `mutex` 和 `queue` 在 C++ 语法上天然绑定。真正的关系来自我们的设计约定：

~~~text
shared.m protects shared.q
~~~

mutex 保护的是一组共享状态的不变量。以后若增加 `closed`、`dropped_frames`，而它们与 queue 必须保持一致，也可以由同一把 mutex 一起保护。

---

## lock_guard 是什么？

`std::lock_guard<std::mutex>` 是一个 RAII 锁对象。

这句：

~~~cpp
std::lock_guard<std::mutex> lock(shared.m);
~~~

可以粗略理解成：

~~~cpp
shared.m.lock();

// 临界区

shared.m.unlock();
~~~

区别是 RAII 版本会在 `lock` 离开作用域时自动 unlock，所以即使中途 return 或抛异常也不容易忘记释放。

因此我们常故意多写一层花括号：

~~~cpp
{
    std::lock_guard<std::mutex> lock(shared.m);
    shared.q.push(...);
}

shared.cv.notify_one();
~~~

执行顺序：

~~~text
进入 {
    lock(shared.m)
    push
离开 }
    lock_guard 析构
    unlock(shared.m)

notify_one()
~~~

---

## Condition Variable 到底解决什么问题？

如果 Consumer 只写成不断检查 queue：

~~~cpp
while (true) {
    std::lock_guard<std::mutex> lock(shared.m);

    if (!shared.q.empty()) {
        Frame f = shared.q.front();
        shared.q.pop();
        process(f);
    }
}
~~~

queue 为空时，Consumer 会不断 `lock -> check -> unlock`，这叫 busy polling，会白白占 CPU。

我们真正需要的是：

> queue 为空时，让 Consumer 睡眠；Producer 改变 queue 状态后，再把 Consumer 唤醒。

这就是 condition variable 的职责。

它不是数据容器，不保存 Frame，也不代表“queue 一定非空”。它只是一个**线程等待 / 唤醒设施**。

真实状态仍然是：

~~~cpp
!shared.q.empty()
~~~

这就是 predicate：当前是否具备继续执行的条件。

---

## 为什么 condition_variable 一定要和 mutex 配合？

因为必须解决一个具体竞态：

> **检查条件** 和 **进入睡眠** 之间不能出现缝隙。

错误伪代码：

~~~cpp
if (shared.q.empty()) {
    sleep_until_notified();
}
~~~

可能出现：

~~~text
Consumer                        Producer
--------                        --------

检查 q.empty()
结果：true

                               push(frame)
                               notify_one()

Consumer 还没真正 sleep

Consumer 现在才开始 sleep
~~~

Producer 的 notify 已经发生完，Consumer 却在 notify 之后才睡下。如果之后再没有新 Frame，它可能永远睡着。

这就是 lost wakeup。

---

## wait() 真正关键的是“原子地 unlock + sleep”

`condition_variable::wait` 配合 `std::unique_lock<std::mutex>`，就是为了把：

~~~text
1. 释放 mutex
2. 把当前线程登记为 waiter 并进入睡眠
~~~

正确连接起来。

Consumer 调用：

~~~cpp
shared.cv.wait(lock, predicate);
~~~

可以按下面理解：

~~~text
Consumer 当前持有 shared.m
        |
        v
检查 predicate
        |
        +-- true --> 直接继续
        |
        +-- false
              |
              v
     进入等待转换：
       登记为 waiter
       +
       unlock(shared.m)
       +
       block/sleep
~~~

这里的关键是：condition variable 与 mutex 的协议不会在“释放锁”和“真正进入等待”之间留下一个让 Producer 的通知永久丢失的竞态窗口。

为什么要先持有 mutex？因为 Producer 修改 predicate 所依赖的状态 `shared.q` 时，也必须持有**同一把** mutex。于是 Producer 与 Consumer 的状态转换被同一个同步协议串起来。

---

## Producer 为什么是“改状态，再 notify”？

Producer：

~~~cpp
{
    std::lock_guard<std::mutex> lock(shared.m);
    shared.q.push(Frame{i});
}

shared.cv.notify_one();
~~~

顺序是：

~~~text
1. lock mutex
2. 修改共享状态 q
3. unlock mutex
4. notify
~~~

真正重要的是：

~~~text
q: empty -> non-empty
~~~

这才是事实。

`notify_one()` 只是告诉一个等待线程：

> 你等待的条件**可能**已经变化了，醒来重新检查一下。

所以 condition variable 的完整语义不是：

~~~text
notify = 发送一条消息
~~~

而是：

~~~text
shared state changes
        +
notification wakes waiter to re-check state
~~~

---

## notify_one() 为什么“不等于立即执行 callback”？

这里并不存在 callback。`notify_one()` 操作的是等待线程的阻塞状态，而不是调用某个函数对象。

`notify_one()` 更准确的含义是：

> 让等待这个 condition variable 的某一个线程有资格离开等待状态。

它不会：

- 直接调用 Consumer 函数；
- 把 CPU 立即切给 Consumer；
- 保证 Consumer 下一条指令马上执行；
- 保证 Consumer 已经拿到 mutex。

完整时间线：

~~~text
Producer thread
    |
    | q.push()
    |
    | unlock(shared.m)
    |
    | shared.cv.notify_one()
    v

condition-variable waiter
    |
    | 从 blocked/waiting 状态被唤醒
    v

OS scheduler
    |
    | 什么时候真正调度 Consumer？
    | 不确定
    v

Consumer thread 开始运行
    |
    | 尝试重新 lock(shared.m)
    v

拿到 mutex
    |
    | 再检查 !shared.q.empty()
    v

pop
~~~

所以：

~~~text
notify_one()
!=
立即执行 Consumer
~~~

它只是让等待者重新进入“可以被调度、并继续争取 mutex”的流程。

---

## condition_variable 和 mutex 到底有什么区别？

### mutex：解决“现在谁可以碰共享状态？”

Producer 正在 push 时，Consumer 不能同时 pop。

它解决的是：

> **互斥 + 内存同步。**

### condition_variable：解决“条件不成立时，我怎么高效等待？”

queue 为空时，Consumer 没必要一直 `lock + check + unlock`。

它解决的是：

> **阻塞等待 + 唤醒。**

一句话记忆：

> **mutex 保护事实，condition_variable 等待事实发生变化。**

---

## 为什么 wait() 用 unique_lock，而不是 lock_guard？

`lock_guard` 的语义很简单：

~~~text
构造 -> lock
析构 -> unlock
~~~

它不能在生命周期中主动“暂时 unlock，再重新 lock”。

但 `condition_variable::wait` 必须完成：

~~~text
unlock mutex
sleep
wake
lock mutex again
~~~

所以它需要一个能够被暂时释放、随后重新获得 mutex 的锁对象：

~~~cpp
std::unique_lock<std::mutex>
~~~

因此：

~~~cpp
std::unique_lock<std::mutex> lock(shared.m);
shared.cv.wait(lock, predicate);
~~~

不是 API 随便要求一种 wrapper，而是 `wait` 的语义本身需要它。

---

## wait(lock, predicate) 实际上等价于什么？

这句：

~~~cpp
shared.cv.wait(lock, [&] {
    return !shared.q.empty();
});
~~~

逻辑上基本等价于：

~~~cpp
while (shared.q.empty()) {
    shared.cv.wait(lock);
}
~~~

注意是 `while`，不是 `if`。

### 原因一：spurious wakeup

condition variable 允许线程在没有对应 notify 的情况下醒来。

所以：

~~~text
醒了
!=
queue 一定非空
~~~

### 原因二：多个 Consumer 会竞争同一个状态

假设 Consumer A、B 都在等，Producer 只 push 一个 Frame，然后 `notify_all()`。

A 和 B 都可能醒。A 先拿到 mutex 并 pop 唯一一个 Frame；随后 B 才拿到 mutex，此时 q 又空了。

所以 B 必须重新检查 predicate。

安全思维模型永远是：

~~~text
wake up
   |
   v
re-lock mutex
   |
   v
re-check predicate
   |
   v
predicate true 才继续
~~~

---

## 一个更完整的可运行版本：观察线程 ID 和关闭协议

~~~cpp
#include <condition_variable>
#include <chrono>
#include <iostream>
#include <mutex>
#include <queue>
#include <thread>

struct Frame {
    int id;
};

struct Channel {
    std::mutex m;
    std::condition_variable cv;
    std::queue<Frame> q;
    bool closed = false;
};

void producer(Channel& ch) {
    std::cout << "producer thread = "
              << std::this_thread::get_id() << "\n";

    for (int i = 0; i < 3; ++i) {
        std::this_thread::sleep_for(std::chrono::seconds(1));

        {
            std::lock_guard<std::mutex> lock(ch.m);
            ch.q.push(Frame{i});
            std::cout << "producer pushed frame " << i << "\n";
        }

        ch.cv.notify_one();
    }

    {
        std::lock_guard<std::mutex> lock(ch.m);
        ch.closed = true;
    }

    ch.cv.notify_all();
}

void consumer(Channel& ch) {
    std::cout << "consumer thread = "
              << std::this_thread::get_id() << "\n";

    while (true) {
        std::unique_lock<std::mutex> lock(ch.m);

        ch.cv.wait(lock, [&] {
            return ch.closed || !ch.q.empty();
        });

        std::cout << "consumer woke and owns mutex again\n";

        if (ch.q.empty() && ch.closed) {
            break;
        }

        Frame f = ch.q.front();
        ch.q.pop();

        std::cout << "consumer popped frame " << f.id << "\n";
    }
}

int main() {
    Channel ch;

    std::thread t1(producer, std::ref(ch));
    std::thread t2(consumer, std::ref(ch));

    t1.join();
    t2.join();
}
~~~

这个程序最值得观察的三个事实：

1. Producer 和 Consumer 的 thread ID 不同；
2. `ch.m / ch.cv / ch.q / ch.closed` 都只有一份；
3. `wait` 返回时，Consumer 已经重新持有 `ch.m`。

---

## 两个 Consumer 时，同一个 cv 怎么工作？

完全可以：

~~~cpp
std::thread c1(consumer, std::ref(ch));
std::thread c2(consumer, std::ref(ch));
~~~

Producer 调：

~~~cpp
ch.cv.notify_one();
~~~

表示唤醒其中一个等待者，不保证是哪一个。

而：

~~~cpp
ch.cv.notify_all();
~~~

表示所有等待者都离开 condition-variable wait，尝试重新获得 mutex。

但 mutex 仍然只有一把，所以最后仍然是一个一个进入临界区。

这正好说明：

> condition_variable 决定谁从“睡眠等待”中出来；mutex 决定谁此刻真正拥有共享状态访问权。

---

## 最后压成一条完整时序

假设 queue 一开始为空：

~~~text
Consumer thread                          Producer thread
---------------                          ---------------

lock(m)

check:
q.empty() == true

cv.wait(lock, predicate)
    |
    | register waiter
    | unlock(m)
    | block
    v

                                        lock(m)

                                        q.push(frame)

                                        unlock(m)

                                        cv.notify_one()
                                                |
                +-------------------------------+
                |
                v
Consumer becomes runnable

OS scheduler eventually runs Consumer

Consumer tries lock(m)

lock(m) succeeds

re-check predicate:
!q.empty() == true

cv.wait(...) returns

q.front()
q.pop()

unlock(m)
~~~

三套职责必须分开：

~~~text
queue / closed / predicate
    = 共享事实

mutex
    = 谁可以读写这些事实

condition_variable
    = 条件不满足时如何睡眠，以及条件可能变化后如何唤醒
~~~

一旦这三层分开，condition variable 就不再神秘：

> **它是在 mutex 保护的共享状态之上，实现“无忙等条件等待”的机制。**

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

但这里不能停在“列出这些名词”。真正的复杂度来自两个原本在 SPSC 中被单写者约束自动解决的问题：

~~~text
reservation:
谁拿到逻辑位置 p？

publication:
拿到 p 的 Producer 是否已经真的把 payload 写完？
~~~

例如两个 Producer：

~~~text
P0 reserve slot 10
P0 被抢占

P1 reserve slot 11
P1 写完
~~~

此时全局 enqueue position 即使已经走到 12，也不能推出 slot 10 已经可读。

所以成熟 bounded MPSC/MPMC 结构经常给**每个物理 slot 再附一个 sequence/generation**：

~~~text
slot.sequence == p
    → 这一轮可由 Producer 写

Producer 写 payload
    ↓
release-store sequence = p + 1
    → 正式发布

Consumer acquire-load sequence == p + 1
    → 才能读 payload

Consumer 完成
    ↓
sequence = p + capacity
    → 物理槽归还给下一轮
~~~

于是 queue 不再只是“数组 + 两个 index”，而变成：

> **reservation state + per-slot ownership + publication protocol + reclaim protocol。**

这一整套从 CAS、per-slot sequence、ABA 到 memory reclamation 的推导放在下一篇
[并发队列与进展保证](concurrent-queues-progress.md) 中完整展开。

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

还要再补一层：

~~~text
Safety:
结构有没有被破坏？

Progress:
竞争发生时谁能完成？
~~~

lock-free 只保证“系统整体持续有人完成操作”，并不保证某一个线程有完成时间上界；wait-free 才试图给每个参与者提供有限步骤上界。

因此一条控制线程真正需要问的是：

~~~text
CAS 最多失败多少次？
是否会因为别的核心长期竞争而 starvation？
满队列时 spin 还是返回？
payload 析构会不会落在实时线程？
atomic 在目标 MCU/SoC 上是否真的 lock-free？
~~~

这些问题比“库主页写着 lock-free”更接近实际实时性。

## 先用拓扑减少共享，再谈无锁优化

如果程序有：

~~~text
16 Producers
    ↓
one global MPMC queue
~~~

最自然的反应往往是继续优化 CAS。

但程序组织层还有更重要的一步：

~~~text
Producer group A → queue A
Producer group B → queue B
Producer group C → queue C
                 ↓
              merge
~~~

也就是 sharding / per-worker queue。

这体现一条非常通用的规律：

> **最便宜的共享状态，是根本不共享。**

中间件、线程池、控制程序、GPU pipeline 都能使用这个思路。先通过 ownership 和 topology 把共享范围缩小，再决定剩下的共享状态要不要做 lock-free。

## “Queue 类型”与“业务语义”必须分开

SPSC/MPSC/MPMC 只告诉你：

~~~text
有几个 Producer
有几个 Consumer
~~~

它没有告诉你：

~~~text
FIFO 还是 latest-only？
能不能丢？
满了 block 还是 drop？
每个 Consumer 都要看到同一条消息吗？
需要 priority / deadline 吗？
~~~

例如：

~~~text
1 Producer
4 Consumers
~~~

不代表应该使用 destructive SPMC queue。

如果四个消费者都必须看见同一条状态，更合理的模型可能是：

~~~text
one payload history
├── cursor A
├── cursor B
├── cursor C
└── cursor D
~~~

或者像 Cyber 一样，给每个 DataVisitor 独立的 bounded ring。

所以并发容器设计的顺序应该是：

~~~text
业务数据语义
↓
Producer/Consumer topology
↓
ownership
↓
capacity / overflow
↓
publication
↓
wakeup
↓
最后才是 mutex / CAS / atomic
~~~

## 线程通信能力怎样迁移到非 x86 平台

真正可迁移的不是某一条 x86 指令，而是：

~~~text
single-writer ownership
bounded storage
sequence / generation
publish-before-notify
data path / control path separation
explicit overflow policy
lifetime state machine
~~~

在 Linux 上它们可能落成：

~~~text
std::atomic
condition_variable
eventfd
epoll
~~~

在 RTOS / MCU 上可能变成：

~~~text
短临界区
IRQ mask
semaphore / event flag
ISR → task ring
static pool
DMA completion
~~~

因此不要把“线程通信”理解成桌面 C++ 专属技巧。即使没有虚拟内存、没有 pthread、没有 x86 TSO，Producer/Consumer、slot ownership、发布时序、容量和回收状态机仍然存在。

对应的可运行项目路线见
[Thread Communication Lab](thread-dataflow-lab.md)。

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

## 从“同步正确”走向“队列到底承诺什么”

到这里，我们已经可以证明：

~~~text
publish happens-before consume
~~~

但这只解决**并发正确性**，没有决定系统语义。

如果 Producer 比 Consumer 快，接下来必须先回答：

~~~text
必须保存所有历史吗？
只保留最新值可以吗？
满了以后 block、drop-old、drop-new 还是 fault？
允许积压多久？
~~~

这些问题应该先于“到底用 SPSC ring 还是 MPMC queue”。

所以接下来先定义队列与过载契约：

→ [Queues & Backpressure](queues-backpressure.md)
