# EventBase 与 AtomicNotificationQueue：跨线程 Post 为什么需要 Queue + Armed State + Wakeup

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

一个 event loop 经常需要让任意线程提交函数给 loop thread 执行。最朴素方案是 mutex queue + 每次 push 都写 eventfd，但高频提交会制造大量重复 wakeup。

Folly 的核心思想是：payload queue 和 wakeup state 要协同。

## EventBase 的入口语义

`runInEventBaseThread(fn)` 如果当前已经在 EventBase thread，可以走当前 loop callback 路径；跨线程时则进入 notification queue。`AlwaysEnqueue` 则无条件排队。

这区分了两种语义：允许本线程快速路径，和严格异步 enqueue。对 reentrancy-sensitive 代码非常重要。

## AtomicNotificationQueue 的三个状态

~~~text
Empty
Armed
Non-empty
~~~

Armed 不需要单独对象。源码把 `kQueueArmedTag = 1` 作为 atomic head 的特殊 pointer tag，也就是一个不指向真实 `Node` 的 sentinel。

~~~text
head == nullptr        → Empty
head == armed tag      → Armed
head == real Node*     → Non-empty
~~~

## Armed 的意义

Consumer 在确认 queue 为空后 arm，相当于声明：

> 我准备睡眠；下一次有人 push 时必须叫醒我。

第一个 producer 把 Armed 变为 Non-empty，并得到“需要 wake”这个结果。后续 producer 看到 queue 已经 Non-empty，就不必重复 wake。

这就是 notification coalescing。

## 为什么 Producer 侧先形成反向链表

Producer 使用 CAS 把 Node push 到 atomic head，本质上接近 Treiber stack。这样多 producer 热路径只做原子头插。

Consumer 一次 exchange 出整条链，再执行 reverse，把 LIFO 链恢复为 FIFO。

也就是说：

> producer 热路径追求最小共享写；排序恢复成本放到 single consumer。

## 为什么 Atomic Queue 与 Local Queue 分两层

结构同时存在 atomic ingress 和 consumer-local queue。

Atomic ingress 负责多 producer 并发提交；local queue 由 Consumer 独占，之后只做普通指针操作。

~~~text
shared ingress
→ batch detach
→ single-owner local queue
~~~

这个模式非常值得迁移。

## maxReadAtOnce 不是 Capacity

EventBase 每轮不能无限处理 notification queue，否则 socket、timer 和其他事件会被饿死。所以它限制单轮最多消费多少任务。

Queue capacity 控制资源边界；maxReadAtOnce 控制 scheduler fairness。两者完全不同。

## Notification 与 Payload 分离

eventfd/pipe 只表达“有工作”，真正的 Task 在 queue 中。这对应一个非常重要的不变量：

> data/state 是真值；notification 只是减少等待成本。

通知可以合并，payload 不能因此丢。

## 机器人 Runtime 的映射

~~~text
CAN RX thread
planner thread
diagnostic thread
timer thread
      ↓
atomic ingress
      ↓
one supervisor EventLoop
~~~

可以直接采用 Empty/Armed/Non-empty + eventfd + local batch，而不是每次 push 都机械唤醒。

## 可迁移原则

1. Queue data 与 wakeup signal 分开建模。
2. Consumer 睡前需要 armed handshake，避免 lost wakeup。
3. 多 producer 热路径可以只做 atomic prepend，FIFO 恢复留给单 consumer。
4. Shared ingress 后尽快切换到 single-owner local queue。
5. Event loop 需要单轮 work budget 保证公平性。