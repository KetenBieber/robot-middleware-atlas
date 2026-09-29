# Fan-out、Backpressure 与 History：一个慢 Subscriber 会怎样影响共享内存池

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 一对多让 Zero-copy 从“一个指针”变成 Ownership 图

单 Publisher、单 Subscriber 很简单：

~~~text
Publisher
  ↓ offset
Subscriber
~~~

但真实系统常是：

~~~text
              ┌→ Subscriber A
Publisher X ──┼→ Subscriber B
              └→ Subscriber C
~~~

同一个 chunk 被多个 Subscriber 借用时，Publisher 不能因为其中一个已经 release 就立刻重用。

## 每个连接都有自己的发送状态

Sender 保存 connections[]，每个 Connection 对应一个 receiver_port_id 与 ZeroCopySender。

发送时会逐连接交付同一个 chunk 的 offset。

因此：

~~~text
payload fan-out
不是 N 次复制 payload

而是
N 条 connection
分别接收同一 shared chunk 的 descriptor
~~~

## Queue Full 时必须做选择

BackpressureStrategy 明确提供两类基础行为。

### RetryUntilDelivered

receiver queue 满时继续等待或重试，直到能交付。

~~~text
不轻易丢
但 Publisher 可能被慢 Subscriber 反向阻塞
~~~

如果 Publisher 运行在关键控制线程，这个阻塞必须计入时间预算。

### DiscardData

底层 try_send 在 receiver buffer 满时允许当前数据对该 receiver 不成功交付。

~~~text
保护 Producer 进度
接受 Subscriber 丢样本
~~~

对高频感知 latest-state 流可能更合理。

## Backpressure Handler 比二选一更细

Sender 还可以配置用户 backpressure handler。

底层把 retry count、elapsed time、service id、sender port id 与 receiver port id 交给 handler。

于是策略可以从静态枚举升级为运行时决策。

但这也意味着回调本身进入发送路径，不能写成不可控的重任务。

## Safe Overflow 的意义

Service 还可以启用 safe overflow。

其目标语义是：

~~~text
subscriber buffer full
→ oldest data can be replaced by newest
~~~

关键字是 safe。

如果旧样本已经被应用借出，就不能直接覆盖它正在读的共享内存。

所以 safe overflow 需要结合 used-chunk/borrow tracking，而不是普通 ring 的无条件 overwrite。

## History 是“新 Subscriber 加入时的过去样本”

Service Builder 的 history_size 定义的是 Subscriber 在连接时最多可以请求多少历史 sample。

Subscriber Builder 还有 history_request。

所以：

~~~text
history
解决 late joiner

buffer
解决已连接 Subscriber 的在线排队
~~~

## 容量为什么必须一起规划

Publisher 可 loan 的样本数、Subscriber buffer、history、max borrowed samples、fan-out 数量共同决定最坏共享内存占用。

因此不能只配 subscriber buffer = 8 就认为总共只需要 8 个 chunk。

更接近的思考是：

~~~text
在线 queued
+
应用 borrowed
+
publisher loaned-but-not-sent
+
history retained
+
fan-out still referenced
~~~

共同占用 pool。

## Data Age 仍然是机器人系统的核心指标

如果 queue 深度很大，Subscriber 可以做到“一条不丢”，但一直处理旧样本。

$$
\text{data age}
=
t_{\text{consume}}
-
t_{\text{sample}}
$$

对于 perception→policy pipeline，很多时候 bounded latest data 比无限完整历史更重要。

## 设计问题最终不是“要不要丢包”

真正的问题是：

~~~text
当 Consumer 不够快时
你希望系统牺牲什么？

吞吐？
Publisher latency？
历史完整性？
内存上界？
数据新鲜度？
~~~

iceoryx2 把这些选择显式暴露出来，而不是隐藏在无限 queue 里。
