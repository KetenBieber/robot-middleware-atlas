# Reader History Cache：为什么 Reader 侧不是一个普通 FIFO

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 网络层完成以后，Reader 仍需要自己的状态机

RTPS 层完成 packet 解析、defrag 和 reorder，只能说明一个样本已经满足协议层交付条件。DDS Reader 还必须实现 History、Resource Limits、Sample State、Instance State、View State、Ownership、Deadline 和 Time-Based Filter 等应用语义。

所以数据还要进入 Reader History Cache，简称 RHC。

## 默认 RHC 的核心结构

固定 src/core/ddsc/src/dds_rhc_default.c 中：

~~~c
struct dds_rhc_default {
  struct dds_rhc common;
  struct ddsrt_hh *instances;
  struct ddsrt_circlist nonempty_instances;
  struct lwregs registrations;

  int32_t max_instances;
  int32_t max_samples;
  int32_t max_samples_per_instance;

  uint32_t n_instances;
  uint32_t n_nonempty_instances;
  uint32_t n_vsamples;
  uint32_t n_vread;

  ddsrt_mutex_t lock;
  ...
};
~~~

这里已经能看出 RHC 不是一条 queue：

~~~text
instance hash
+ nonempty circular list
+ writer registrations
+ resource counters
+ per-instance sample history
+ one RHC mutex
~~~

## 为什么要有 Instance Hash

DDS keyed topic 允许一条 Topic 上存在多个 logical instances。Reader 收到样本时首先要根据 instance handle 找到对应 instance。

哈希表适合这类访问：

~~~text
key / instance handle
  -> O(1) average lookup
  -> per-instance history
~~~

如果使用 std::vector 风格顺序扫描，instance 数量增长后，接收热路径会退化成 O(n)。

## 为什么还有 Circular List

读取 API 并不总是指定某个 key，它经常要找到“有哪些 instance 当前非空”。

因此 RHC 维护 nonempty_instances circular list。instance 从 empty 变 nonempty 时加入，从 nonempty 变 empty 时移除。

这避免每次 read/take 都扫描整个 hash table。

## 一个 Instance 为什么预留一个 Sample

rhc_instance 中存在 a_sample，作为单样本预分配位置。对 KEEP_LAST depth=1 这类常见机器人状态流，很多情况下可以避免为每个新 sample 再分配一个独立 sample node。

这是一个很典型的 fast path 设计：

~~~text
常见情况
single instance + depth 1
-> 尽量复用预留槽

复杂情况
depth > 1 / multiple samples
-> 扩展为动态 sample history
~~~

## Sample State 与 Instance State 为什么要分开

DDS 读取返回的不只是 payload，还要给 sample info：

~~~text
sample_state
READ / NOT_READ

view_state
NEW / NOT_NEW

instance_state
ALIVE
NOT_ALIVE_DISPOSED
NOT_ALIVE_NO_WRITERS
~~~

所以 dispose/unregister 即使没有普通 payload，也可能需要向应用暴露 invalid sample 来表示状态变化。

这就是 RHC 比 ring buffer 复杂的根本原因：它存的是 DDS data model，而不是“最近 N 个字节块”。

## read 与 take 的本质差别

~~~text
read
读取满足条件的 sample
但历史中的 sample 仍然存在
并更新 read state

take
读取并把 sample 从 RHC 中移走
可能进一步使 instance 变 empty
甚至触发 instance/registration 回收
~~~

因此 take 的回收路径会比普通只读遍历复杂。

## Resource Limits 是确定内存上界的重要入口

RHC 保存：

~~~text
max_instances
max_samples
max_samples_per_instance
history_depth
~~~

这些 QoS 决定 sample 到来时是接受、过滤、覆盖还是拒绝。对机器人系统来说，真正需要问的是：

> 当消费者持续慢于生产者时，RHC 最终采取什么动作？

KEEP_LAST 1、KEEP_LAST N、KEEP_ALL 的延迟与内存失败方式完全不同。

## 为什么 ROS 2 里 Keep Last 1 很常见

状态估计、关节状态、定位等流通常只关心最新状态。如果应用最终只处理最新状态，保留大量旧样本只会增加 data age。

但 Keep Last 1 也不意味着整个链路只有一个 buffer：network socket、defrag/reorder、RHC、RMW/Executor 和业务层都可能存在额外排队。分析 freshness 要追完整链，而不是只看一个 QoS 字段。
