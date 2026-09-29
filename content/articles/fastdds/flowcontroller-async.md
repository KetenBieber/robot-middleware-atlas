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
