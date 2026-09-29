# 线程间通信：共享地址不等于共享时序

两个线程共享地址空间，所以“把一个指针交给另一个线程”看起来最简单。

真正困难的是：

> Consumer 怎么知道 Producer 已经把对象写完，而且自己看到的是写完后的内容？

## 最朴素的错误实现

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

问题不只是两个线程“可能同时执行”。

编译器和 CPU 都允许在不破坏单线程语义的前提下重排内存操作；普通 bool 也会形成 data race。

因此必须建立跨线程的 happens-before 关系。

## Mutex 同时解决互斥与可见性

~~~cpp
std::mutex m;
Frame frame;
bool ready = false;

void publish(Frame f) {
    std::lock_guard lock(m);
    frame = std::move(f);
    ready = true;
}
~~~

如果 consumer 用同一把 mutex 读取，就得到两个性质：

1. 同一时刻只有一方进入临界区；
2. unlock 到随后 lock 建立可见性顺序。

## Acquire / Release 为什么存在

如果状态很小，可以用原子变量发布数据：

~~~cpp
std::atomic<bool> ready{false};

// producer
frame = capture();
ready.store(true, std::memory_order_release);

// consumer
if (ready.load(std::memory_order_acquire)) {
    process(frame);
}
~~~

逻辑是：

~~~text
Producer:
写 frame
   ↓
release store ready=true

        synchronizes-with

Consumer:
acquire load ready==true
   ↓
随后读取 frame
~~~

release 不允许关键的先前写被观察成出现在发布点之后；acquire 不允许关键的后续读被观察成出现在接收点之前。

这不是简单的“强制刷新缓存”，而是在语言内存模型里建立可证明的跨线程顺序。

## Condition Variable：notify 不等于立即执行

典型 producer：

~~~cpp
{
    std::lock_guard lock(m);
    queue.push(item);
}
cv.notify_one();
~~~

consumer：

~~~cpp
std::unique_lock lock(m);
cv.wait(lock, [&] { return !queue.empty(); });
auto item = queue.front();
queue.pop();
~~~

真实时间线是：

~~~text
Producer
push
unlock
notify
   │
   ▼
Consumer 由 blocked 变成 runnable
   │
   ▼
OS scheduler 决定什么时候获得 CPU
   │
   ▼
重新拿 mutex
   │
   ▼
消费数据
~~~

因此 notify_one 只改变等待条件，不承诺 callback 立刻执行。

## Ring Buffer 为什么常见

如果 producer/consumer 模式固定，通用容器常常不是最合适的。

SPSC ring：

~~~text
slots[0 ... N-1]

producer owns head
consumer owns tail
~~~

在合理设计下，两边可以避免争抢同一个容器元数据。

但仍然要处理 head/tail 原子顺序、cache line false sharing、容量满、wrap around 与 shutdown。

## Latest-value Slot 与 FIFO 是两种语义

控制系统经常只想要最新状态：

~~~text
frame 100
frame 101
frame 102
~~~

如果 consumer 落后，FIFO 会让它继续处理 100、101，再看到 102。

但控制系统可能真正想要：

~~~text
直接覆盖成 102
~~~

于是需要 latest-value mailbox / double buffer，而不是 queue。

先问：

~~~text
业务需要每一条历史？
还是只需要最新状态？
~~~

这比“vector 还是 deque”更早。

## Lock-free 不等于 Wait-free

blocking、lock-free 和 wait-free 描述的是不同进展保证。

lock-free queue 仍可能让某一个线程反复 CAS 失败。

所以：

> 没有 mutex 不等于有确定 WCET。

## False Sharing

两个独立 atomic 如果落在同一 cache line：

~~~text
Core 0 writes head
Core 1 writes tail
~~~

即使逻辑完全独立，cache coherence 仍可能让 cache line 在核心间来回迁移。

高频 ring 常把 producer 与 consumer 的热点索引分开到不同 cache line。

## 对 Atlas 的映射

Cyber 的 Dispatcher/Notifier、LCM 接收 ring、Fast DDS FlowController、Cyclone DDS sendq 与 GXF operator queue 都可以用同一组并发问题分析：

~~~text
谁生产？
谁消费？
哪一个线程？
容器是什么？
谁唤醒谁？
队列满怎么办？
~~~
