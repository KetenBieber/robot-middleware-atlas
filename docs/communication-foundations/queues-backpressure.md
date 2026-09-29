# 队列与背压：吞吐不足时，系统必须明确选择“旧数据、阻塞还是丢失”

通信系统的稳定态通常不难。真正决定架构质量的是：

> Consumer 比 Producer 慢时怎么办？

## 无界队列只是把故障推迟

假设：

~~~text
Producer = 1000 msg/s
Consumer = 900 msg/s
~~~

每秒积压：
$$
1000 - 900 = 100
$$

如果消息平均 1 MB：

$$
100\ \text{MB/s}
$$

无界队列没有消除过载，只是把它转换成不断增长的 memory、latency 和 data age。

## 有界队列强迫系统做选择

队列满以后只能有少数几种策略。

### Block

~~~text
Producer
  ↓
queue full
  ↓
wait until consumer pops
~~~

优点是不丢。代价是上游线程被 backpressure 传播。

如果 producer 就是控制线程，阻塞时间必须进入 WCET。

### Drop New

~~~text
queue full
new message arrives
→ discard new
~~~

保留历史，但新状态无法及时进入系统。

适合某些必须完整处理旧任务的工作队列，不适合 latest-state 控制。

### Drop Old / Overwrite

~~~text
queue full
discard oldest
insert newest
~~~

牺牲完整历史，控制 data age。

感知与状态估计经常更关注最新状态。

### Retry

producer 主动反复尝试。

这不是免费方案：retry 会消耗 CPU，并可能形成 priority inversion 或 busy spin。

## Safe Overflow 与普通覆盖的区别

一个 shared-memory queue 若要覆盖旧 slot，必须保证旧 slot 没有仍被 consumer 借用。

因此“覆盖最旧数据”在 zero-copy 系统里往往需要更多 ownership tracking。

不能把 ring overwrite 直接等价成 zero-copy safe overflow。

## Queue Capacity 应该从时间预算推导

如果感知 producer 30 Hz，允许 consumer 最多落后 100 ms：

$$
30 \times 0.1 = 3
$$

那么容量 3～4 已经表达了业务时间预算。

如果随手配 depth = 1000，反而可能允许系统积累几十秒的旧数据。

## Data Age 比 Throughput 更重要

定义：

$$
\text{data age}
=
t_{\text{consume}}
-
t_{\text{sample}}
$$

一个 pipeline 即使吞吐稳定，只要队列太深，也可能产生“稳定但永远处理旧世界”的系统。

## 多级队列会叠加

典型链：

~~~text
application queue
→ middleware history
→ async send queue
→ socket buffer
→ NIC TX ring
→ network
→ NIC RX ring
→ receive queue
→ executor queue
~~~

每一层单独看只有几毫秒，叠加后可能形成很大的尾延迟。

所以分析一个 middleware 的 latency 时，要先画出所有排队点。

## Backpressure 传播方向

真正的 backpressure 是：

~~~text
Consumer slow
↓
queue full
↓
Publisher cannot commit/send
↓
upstream caller slows
~~~

如果中间层选择 drop，backpressure 就在该层被“截断”，代价转化成数据丢失。

没有绝对最好策略，只有业务语义不同。

## 控制、感知、日志通常不该共用同一种策略

控制命令：

~~~text
latest-only / bounded / fail-safe
~~~

感知帧：

~~~text
drop-old often acceptable
~~~

日志：

~~~text
prefer complete history
can buffer or write asynchronously
~~~

把三种流量全部塞进同一种 FIFO，是很多机器人运行时尾延迟问题的根源。
