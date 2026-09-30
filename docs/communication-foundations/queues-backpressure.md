# 队列与背压：吞吐不足时，系统必须明确选择“等待、丢失还是变旧”

:::{contents} 本页目录
:depth: 3
:local:
:::

通信系统在负载很低时通常都显得很优秀。真正能区分设计质量的时刻，是：

> Producer 持续比 Consumer 快时，系统到底怎么办？

这不是一个“性能优化细节”，而是通信语义本身。

如果这个问题没有明确答案，队列就会替系统积累债务，最终以延迟、内存、丢包或控制失效的形式爆出来。

## 先用最简单的速率模型看清“积压”

设 Producer 到达速率为：

$$
\lambda = 1000\ \text{msg/s}
$$

Consumer 服务速率为：

$$
\mu = 900\ \text{msg/s}
$$

只要长期满足：

$$
\lambda > \mu
$$

积压就会持续增长。

每秒增长：

$$
\lambda - \mu = 100\ \text{msg/s}
$$

如果每条消息 1 MB，相当于：

$$
100\ \text{MB/s}
$$

的内存债务。

所以“无界队列不丢消息”并不是免费可靠性，而是：

> 把过载从数据丢失转换成无限增长的 memory 与 latency。

## Queue depth 其实就是“允许系统落后多久”

假设相机 30 Hz，业务允许感知最多落后 100 ms：

$$
30 \times 0.1 = 3
$$

那么 queue depth 约 3～4 条就已经表达了这个时间预算。

反过来，如果随手配置：

~~~text
depth = 1000
~~~

理论上系统可能在负载异常时堆积几十秒旧帧。

这就是为什么实时机器人系统里，**queue depth 不应该只按“越大越安全”来配**。

## Data Age：控制系统通常比吞吐更在乎这个量

定义一条样本从采样到真正被消费的年龄：

$$
\text{data age}
=
t_{consume}
-
t_{sample}
$$

一个 pipeline 完全可能满足：

~~~text
没有丢帧
吞吐稳定
CPU 也没爆
~~~

但控制器一直在处理 500 ms 以前的世界。

这叫：

> 稳定地处理旧数据。

因此对 perception/control，除了 throughput，还应持续观测：

- queue depth；
- oldest sample age；
- end-to-end data age；
- drop count；
- p95/p99 latency。

## 队列满以后，系统事实上只有几种选择

在讨论 Block / Drop / Overwrite 之前，先把两个经常混在一起的维度分开：

~~~text
并发拓扑：
SPSC / MPSC / SPMC / MPMC

业务过载语义：
block / drop-new / drop-old / latest-only / retry
~~~

它们是正交的。

例如一个 MPSC queue 可以选择：

~~~text
多个 Producer
↓
有界容量
↓
满时立即失败
~~~

也可以选择：

~~~text
多个 Producer
↓
有界容量
↓
Producer 阻塞等待
~~~

所以“换成 lock-free MPSC”不会自动解决 backpressure；它只改变**竞争与同步实现**，不替你决定满载语义。

这也是程序组织中一个非常重要的分层：

> **Queue algorithm 解决“并发访问怎样正确”，Queue policy 解决“系统过载时应该牺牲什么”。**

### Block：把压力向上游传播

~~~text
Producer
  ↓
queue full
  ↓
wait
  ↓
Consumer pops
  ↓
Producer continues
~~~

优点：

- 不主动丢数据；
- 生产速率最终会被消费速率约束。

代价：

- Producer 线程被阻塞；
- backpressure 可能一路传播到采集/控制线程；
- 阻塞时间必须进入 WCET。

如果 Publisher 正处在 1 kHz 控制循环里，这个策略就要非常谨慎。

### Drop New：保留历史，拒绝新数据

~~~text
queue = [100, 101, 102]
new = 103
↓
drop 103
~~~

它适合“旧任务必须完成”的 work queue，但对于状态流可能很糟糕：系统会继续处理旧世界，却把最新状态扔掉。

### Drop Old：牺牲历史，保留新鲜度

~~~text
queue = [100, 101, 102]
new = 103
↓
drop 100
queue = [101, 102, 103]
~~~

这更符合很多感知流：

~~~text
旧图像错过就错过
最新图像更有价值
~~~

### Overwrite / Latest-only：队列退化成 mailbox

控制状态常常更极端：

~~~text
100
101
102
103
~~~

Consumer 醒来只需要 **103**。

这时最自然的数据结构不是深 FIFO，而是：

~~~text
single latest slot
double buffer
versioned mailbox
~~~

### Retry：没有阻塞，但会消耗 CPU

~~~text
try enqueue
failed
try again
failed
...
~~~

如果 retry 是 busy spin，它会把“队列满”转换成 CPU 占用和 cache contention。

因此 retry policy 必须和退避策略一起看。

## Backpressure 的定义不是“队列满了”

真正的 Backpressure 是压力沿调用链传播：

~~~text
Consumer slow
↓
receive queue full
↓
middleware cannot accept more
↓
publisher send/commit slows
↓
upstream producer slows
~~~

如果中间某层选择 drop：

~~~text
Consumer slow
↓
queue full
↓
drop-old
↓
Publisher continues normally
~~~

backpressure 就在这一层被截断，代价转化成数据丢失。

因此 **drop** 和 **backpressure** 是两种不同的过载语义。

## 多级队列为什么会制造尾延迟

一条真实通信链往往不止一个 queue：

~~~text
application queue
→ middleware history
→ async send queue
→ kernel socket buffer
→ NIC TX ring
→ network
→ NIC RX ring
→ kernel receive buffer
→ middleware receive queue
→ executor queue
~~~

假设每层只允许排队 5 ms，十层叠加就可能出现 50 ms。

更糟的是，很多层的排队并不是固定 5 ms，而是随着 burst 和调度抖动变化。

所以排查延迟不能只在 API 两端打 timestamp；必须把中间所有 queue point 画出来。

## Little's Law 为什么值得记住

稳定系统里，一个非常实用的关系是：

$$
L = \lambda W
$$

其中：

- **L**：系统中平均存在的任务数；
- **lambda**：平均到达速率；
- **W**：平均停留时间。

它给一个很直观的工程关系：

> 队列里平均堆得越多，平均等待时间就越长。

所以看到“queue depth 经常保持在 20”时，不要只把它当一个容量数字，它已经在暗示时延。

## Zero-copy 系统里的 Drop Old 更难

普通 copied queue 想丢最旧数据很简单：

~~~text
pop oldest
destroy
~~~

但 shared-memory loan 里，“最旧 descriptor”对应的 payload 可能仍被 Consumer 借用。

~~~text
queue says oldest = Chunk X
Consumer still reading Chunk X
~~~

这时直接覆盖 X 会破坏内存安全。

因此 production zero-copy 的 **safe overflow** 必须把 queue policy 与 ownership tracking 结合起来。

这也是为什么共享内存系统里“overwrite”不是一个简单 ring index 操作。

同样的困难在线程内 zero-copy 也存在。如果 queue 里保存的是对象指针、pool handle 或 DMA buffer descriptor，那么“丢掉 descriptor”之前必须确认：

~~~text
还有没有 Consumer 持有 payload？
槽位是否已经归还 pool？
是否存在异步 GPU/DMA 操作仍在使用它？
~~~

所以从普通 std::deque 迁移到 pool/ring 后，backpressure policy 会直接和 ownership state machine 耦合。

## DDS 的 History / ResourceLimits 本质上就在表达容量语义

DDS 看起来有很多 QoS 名词，但放进队列视角会直观很多。

例如：

~~~text
KEEP_LAST(depth=N)
~~~

本质上是在说：

> 每个 instance/history 最多保留有限数量样本。

而：

~~~text
KEEP_ALL
~~~

并不意味着真正无限；仍然要受 ResourceLimits 和实现资源约束。

再叠加 Reliability、FlowController、WriterHistory、ReaderHistory，就会形成：

~~~text
应用写入
↓
Writer history
↓
可靠性保留 / 重传
↓
发送调度
↓
Reader history
↓
应用 take/read
~~~

因此 DDS 性能问题经常不是“UDP 慢”，而是 history 与可靠性状态让数据在多个层级被保留。

可以继续看 Fast DDS 的 [WriterHistory reliability](../generated/fastdds/writerhistory-reliability.md) 与 [FlowController](../generated/fastdds/flowcontroller-async.md)。

## Credit-based Flow Control：背压也可以显式表达“你还能发多少”

除了“队列满了再阻塞”，还可以让 Consumer 或下游明确告诉上游可用额度：

~~~text
Consumer grants 8 credits
↓
Producer may send 8 items
↓
credits consumed
↓
Consumer releases capacity
↓
new credits returned
~~~

这种机制的好处是，容量限制在发送之前就可见，不必等到某一层 buffer 已经塞满。

网络 transport、RDMA、流式 runtime 中经常能看到类似思想。

## 控制、感知、规划、日志不应该共用一种 Queue Policy

### 控制状态

目标是低 data age：

~~~text
latest-only
bounded
drop-old
fail-safe
~~~

### 感知帧

通常允许丢部分帧，但不希望延迟不断堆积：

~~~text
small bounded queue
drop-old
monitor age
~~~

### 规划任务

可能更像 work queue：

~~~text
有限任务
明确 cancel
可能需要每个请求都有结果
~~~

### 日志

更关注完整性：

~~~text
larger buffer
asynchronous disk writer
batching
backpressure or loss accounting
~~~

把这些流量都塞进一个“默认 FIFO depth=1000”，几乎一定会掩盖业务语义。

## Queue Capacity 应该从 deadline 反推，而不是拍脑袋

一个实用设计过程：

假设：

~~~text
sensor rate = 50 Hz
max acceptable age = 80 ms
consumer worst temporary stall = 40 ms
~~~

80 ms 里最多产生：

$$
50 \times 0.08 = 4
$$

条样本。

于是初始容量可以围绕 4～5 设计，再结合 burst、调度抖动和测量调整。

这比“内存很多，先给 1024”更接近实时系统逻辑。

## 真正应该监控哪些指标

如果一个中间件只告诉你“发送成功多少条”，信息远远不够。

通信队列至少应该能观察：

~~~text
current depth
high-water mark
enqueue failures
drops by reason
oldest sample age
time spent blocked
retry count
consumer lag
pool free chunks
p50/p95/p99 latency
~~~

这些指标能把“偶尔卡一下”拆成具体机制：

~~~text
是 queue 深？
是 Consumer 慢？
是 scheduler 抖？
是 pool 耗尽？
是可靠性重传？
~~~

## 一句话把队列问题串起来

队列不是“装消息的容器”，而是一份延迟和过载契约。

真正需要确定的是：

~~~text
哪些消息必须完整保留？
哪些消息过期后就没价值？
系统允许落后多久？
队列满时谁承担代价？
压力要不要传播给上游？
~~~

回答完这些问题以后，才轮到 **std::deque、ring buffer、lock-free queue、DDS History、shared-memory descriptor queue** 这些具体实现。

如果还需要继续回答：

~~~text
SPSC 为什么能只有单写 head/tail？
MPSC 为什么需要 reservation 与 publication 分离？
MPMC 的 per-slot sequence 在解决什么？
lock-free 为什么仍可能 starvation？
wait-free 为什么难？
ABA 与 memory reclamation 为什么是无锁链表的核心？
~~~

继续读 [并发队列与进展保证](concurrent-queues-progress.md)；如果希望把这些机制真正写成程序，再进入 [Thread Communication Lab](thread-dataflow-lab.md)。

## 从“队列契约”走向“怎样把契约做成并发数据结构”

这一篇先决定了语义：

~~~text
FIFO / latest
bounded / unbounded
block / drop / overwrite / fault
~~~

下一步才轮到实现问题。

一旦 Producer/Consumer 数量从 SPSC 变成 MPSC/MPMC，就必须进一步处理：

~~~text
reservation 与 publication 为什么分开？
head/tail 谁能写？
per-slot sequence 在防什么？
CAS 失败意味着什么？
lock-free 与 wait-free 到底保证谁能前进？
ABA 和 reclamation 为什么会出现？
~~~

这些问题进入下一篇：

→ [Concurrent Queues & Progress Guarantees](concurrent-queues-progress.md)
