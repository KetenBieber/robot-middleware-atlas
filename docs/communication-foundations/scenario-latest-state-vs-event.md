# 场景设计一：最新状态、事件流与历史——先别急着选 Queue

## 场景

机器人里最常见的数据流之一：

~~~text
Estimator 200 Hz
      ↓
Controller 1000 Hz
~~~

Controller 每个周期需要机器人当前状态。

第一反应很容易是：

~~~cpp
std::queue<State> q;
~~~

但在决定容器之前，必须先问业务语义。

---

## 第一步：这到底是 State、Event 还是 History

三个问题：

~~~text
1. 新值到达以后，旧值还有独立业务意义吗？
2. 每一次更新都必须被 Consumer 处理吗？
3. Consumer 需要的是当前世界，还是发生过的完整序列？
~~~

如果答案是：

~~~text
旧值很快失效
不要求每个 estimate 都执行一次 control
只需要当前状态
~~~

那么它是 **latest state**，不是 event queue。

反过来：

~~~text
ENABLE
START
STOP
FAULT_ACK
~~~

通常是 event/command，每一条的顺序与存在本身可能有意义。

日志/轨迹则属于 history。

---

## Naive 方案一：一个共享 State

~~~cpp
State state;
~~~

Estimator 写，Controller 读。

多线程以后立刻产生 data race。

最简单修复：

~~~cpp
std::mutex m;

Estimator:
  lock
  state = new_state
  unlock

Controller:
  lock
  local = state
  unlock
~~~

如果 State 更新频率不高、copy 成本小，这可能已经够好。

不要因为看到 mutex 就自动认为设计落后。

---

## 但 Mutex 只解决 Race，不自动解决 Snapshot

假设状态由多个来源组成：

~~~text
pose
velocity
battery
mode
~~~

如果它们由不同 callback 分别加锁更新，而 Controller 分多次加锁读取：

~~~text
read pose version 100
unlock

velocity callback updates version 101

lock
read velocity version 101
~~~

最终 local state 是混合版本。

这和 Apollo Planning 里辅助输入逐段复制到 LocalView 的问题完全一致。

所以：

> **没有 data race，不等于获得业务级原子快照。**

如果要求同一采样时刻的一致状态，要增加 version/snapshot protocol。

---

## 候选方案空间

| 方案 | Reader 成本 | Writer 成本 | 历史 | 一致快照 | 适合 |
| --- | --- | --- | --- | --- | --- |
| mutex + copy | lock + copy | lock + copy | 否 | 可以 | 简单状态 |
| atomic scalar | 极低 | 极低 | 否 | 单字段 | flag/counter |
| double buffer | 指针/索引切换 | 写非当前 buffer | 否 | 可以 | 大 State |
| seqlock/version | 可能重试 | 单 writer 简单 | 否 | 可以 | 高频一写多读 |
| latest mailbox | 读最新 | 覆盖旧值 | 否 | 取决实现 | state stream |
| FIFO | pop | push | 是 | 单 item | event/history |

---

## Double Buffer 为什么自然

如果 State 很大：

~~~text
buffer A: Reader currently using
buffer B: Writer updating
~~~

Writer 完成以后发布 current index。

~~~text
write B completely
↓
release publish current=B
↓
Reader acquire current
↓
read B
~~~

核心不是“两块内存”，而是：

> **写入中的对象与已发布对象物理分离。**

这样 Reader 不会看到半更新对象。

---

## Seqlock / Versioned Snapshot 什么时候更合适

一种典型模式：

~~~text
sequence = even
↓
Writer sequence++ → odd
↓
write fields
↓
sequence++ → even
~~~

Reader：

~~~text
read seq0
if odd → retry
copy state
read seq1
if seq0 != seq1 → retry
~~~

优点是 Reader 不阻塞 Writer。

适合：

~~~text
一个 Writer
很多 Reader
Writer 临界区短
Reader 可以偶尔重试
~~~

不适合：

~~~text
Writer 很慢
Reader 必须固定步骤完成
多 Writer 很复杂
~~~

---

## 为什么 FIFO 在 Latest State 场景可能是错误设计

Estimator 200 Hz，Controller 100 Hz。

无界 FIFO：

~~~text
每秒生产 200
每秒消费 100
→ backlog +100/s
~~~

10 秒以后 Controller 可能处理几秒前的状态。

程序没有丢包，也没有崩，但控制语义已经错误。

这就是 Data Age 比 queue loss 更重要的典型场景。

---

## 工业案例：Apollo Planning

Apollo Planning 的一些辅助输入不是“每条都必须触发一次 Planning”，而更像下一次规划要读取的 latest state。

因此工程使用 component-owned state + mutex，再在 Proc() 里构造 LocalView。

这个案例最值得学习的是：

~~~text
event trigger
和
latest auxiliary state
可以在同一个 Component 里使用不同通信语义
~~~

不要为了统一框架把所有输入都塞进同一种 queue。

详见： [工程案例页](industry-runtime-cases.md)。

---

## 工业案例：Holoscan AsyncBuffer

Holoscan/GXF 的 AsyncBuffer 使用 four-slot/latest-style 异步交换思想，目标不是可靠保存每条历史，而是让两个执行流尽量独立、读取一致已发布值。

这和普通 FIFO connector 是不同语义。

详见： [Condition、Connector 与 Backpressure](../generated/holoscan/conditions-connectors-backpressure.md)。

---

## 一个机器人 Runtime 的合理组合

~~~text
IMU samples
→ SPSC ordered ring

Estimated RobotState
→ latest/versioned snapshot

Mode Change / Fault Event
→ bounded MPSC event queue

Logs
→ MPSC batch queue
~~~

同一个程序同时存在四种数据结构完全正常。

---

## 什么时候应该换方案

如果出现以下变化，就重新选型：

~~~text
“每次更新都必须处理”
→ latest state 改 event/history queue

“Reader 数量暴增”
→ mutex 可能改 versioned/RCU-style

“State 变得很大”
→ copy snapshot 改 double buffer/handle

“需要跨进程”
→ 裸 pointer 改 SHM descriptor/generation
~~~

---

## 设计检查表

在写第一行容器代码前回答：

~~~text
[ ] 数据是 State / Event / History 哪一种？
[ ] 旧值在新值出现后还有意义吗？
[ ] 每条更新必须处理吗？
[ ] 是否需要跨字段一致快照？
[ ] Producer/Consumer 数量？
[ ] Reader 可以重试吗？
[ ] 对象 copy 成本多大？
[ ] 最大允许 Data Age？
~~~

深入机制：

- [Ownership & Address Space](ownership-address-space.md)
- [Threads & Memory Order](threads-memory-order.md)
- [Queues & Backpressure](queues-backpressure.md)
