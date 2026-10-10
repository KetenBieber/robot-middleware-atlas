# FlowController 与 Asynchronous Publish：谁决定 CacheChange 什么时候真正上网

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## 异步发送不是一个 Bool 就结束了

Fast DDS 的 asynchronous publish 把 application write 和 network send 分开。

但两者之间不是一个无策略 FIFO，而是 FlowController。

## FlowControllerImpl 的核心数据结构

固定源码中能看到：

~~~cpp
using map_writers =
    std::unordered_map<
      BaseWriter*,
      std::tuple<
        FlowQueue,
        int32_t,
        uint32_t,
        uint32_t>>;

using map_priorities =
    std::map<
      int32_t,
      std::vector<BaseWriter*>>;

map_writers writers_queue_;
map_priorities priorities_;
~~~

### Writer → Queue 用 unordered_map

调度时经常从 BaseWriter* 找自己的 pending queue。

指针 key 精确、查找频繁，不需要有序，哈希表平均 O(1) 合适。

### Priority → Writers 用 map

调度要按 priority 顺序遍历。

std::map 自带有序 key，因此避免每轮重新 sort priority。

### 同优先级 Writers 用 vector

同一个 priority 下 Writer 数量通常不大，连续内存遍历 cache locality 更好；调度过程以线性扫描为主。

这就是“数据结构由访问模式决定”的具体例子。

## Bandwidth-limited Async 模式

固定源码：

~~~cpp
max_bytes_per_period =
    descriptor
      ->max_bytes_per_period;

period_ms =
    std::chrono::milliseconds(
      descriptor->period_ms);
~~~

FlowControllerLimitedAsyncPublishMode 还保存 sent_bytes_limitation_。

因此调度器可以表达：

~~~text
每个 period
最多发送 N bytes
~~~

而不只是“晚点发”。

## 为什么机器人系统需要 Flow Control

假设：

~~~text
Camera 30 Hz × 8 MB
LiDAR 10 Hz × 4 MB
Control 1 kHz × 64 B
~~~

如果所有 Writer 都立即抢 Transport，大图像 burst 很容易扩大 control message 排队。

FlowController 可以把带宽、priority 与 fragment scheduling 显式纳入 participant runtime。

## Async Publish 仍然做了什么

application thread 仍然需要完成：

~~~text
Writer mutex
→ serialization / loan ownership
→ CacheChange
→ WriterHistory
→ reliability bookkeeping
→ enqueue / schedule
~~~

它只是把最终 message send 延后。

所以 asynchronous publish 不等于 application thread O(1)。

## Sync 与 Async 的真正权衡

同步：

~~~text
优点：
发送时机更直接
少一层 queue

代价：
Transport send 延迟进入 write()
~~~

异步：

~~~text
优点：
把网络 I/O 从生产者线程隔离
可统一限流/priority

代价：
多一层 queue
增加数据年龄
过载时需要背压策略
~~~

对闭环控制，不能因为 async “不会卡控制线程”就默认更好；queue depth 同样可能把旧命令延迟发送。

## 为什么 Priority 不能代替 Capacity

高优先级队列如果无限增长，照样会造成内存膨胀、command stale，最终系统迟早崩溃。

实时设计必须同时回答：

~~~text
priority
capacity
drop / overwrite
max blocking time
data age
~~~

FlowController 只解决其中一部分。

## FlowController 调度的是 Change，不是 Topic 名字

进入异步路径以后，真正被调度的是已经存在于 WriterHistory 中、等待发送的
CacheChange/fragment 工作。也就是说 serialization 与 History commit 已经发生。

~~~text
application write
  ↓
CacheChange committed
  ↓
writer pending work
  ↓
FlowController
  ↓
selected fragment/change
  ↓
RTPS message + Transport
~~~

因此 async 主要隔离的是“何时发送”，不是“何时拥有样本”。

## 带宽限制可以直接换算成最小排空时间

若配置 max_bytes_per_period=B、period=T，一个长度为 S 的 burst 在没有其他 Writer
竞争时，理论上也至少需要大约：

~~~text
ceil(S / B) × T
~~~

才能被调度完。实际时间还要加上优先级竞争、fragment overhead 和 transport 成本。

例如相机突发 8 MB，而控制器每 10 ms 只允许 1 MB，那么仅限流就能引入至少 80 ms
量级的发送展开时间。此时“异步发布不阻塞生产者”并不能说明数据是新鲜的。

## priority 解决先后，capacity 解决过载

FlowController 的 priority 可以保证高优先级 Writer 更早被考虑，但如果生产速率长期
大于消费速率，任何 priority 都无法消灭 backlog。

过载策略必须额外明确：

~~~text
queue capacity
History depth
drop / overwrite
max blocking time
deadline
data-age alarm
~~~

对于速度指令，晚 200 ms 送达的“可靠旧命令”可能比直接丢弃更危险。

## Async sender 仍与可靠性共享状态

同一个 CacheChange 既受 FlowController 发送调度影响，也受 StatefulWriter /
ReaderProxy 的 ACK、NACK、重传状态影响。因此不能把 flow queue 当成独立于 WriterHistory
的普通生产者消费者队列。

当 Reader 很慢时，发送调度已经完成的 Change 仍可能因为可靠性要求留在 History；
这就是为什么“网络已 send”与“内存可回收”是两件事。

## 一个更适合机器人部署的观察面

异步模式至少应记录：

- 每个 Writer pending changes；
- 最老 pending sample 的 data age；
- 每周期实际发送字节；
- WriterHistory 占用；
- 重传量；
- write() timeout；
- receiver 端 sample lost/rejected。

只看链路带宽利用率无法判断控制消息是否正在被大流量 topic 挤成旧数据。
