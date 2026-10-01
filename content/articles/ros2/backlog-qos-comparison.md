# ROS1 Queue vs ROS2 History/QoS：积压、丢旧样本和可靠性不是同一个旋钮

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。重点回答一个常见问题：ROS1 的 `queue_size` 能不能简单理解成 ROS2 的 `depth`？

Supporting source: `rmw_dds_common@e26ba1079886c5598614172db0b27526aa6af07d`，用于核对 QoS compatibility 与 RMW history contract。


只能做局部类比。

## 1. ROS1 queue_size 管本地 delivery backlog

`SubscriptionQueue::fullNoLock()` 判断：

~~~cpp
(size_ > 0) && (queue_size_ >= size_)
~~~

满时 `push()` 会先 `pop_front()`，再追加新样本。也就是典型的丢最旧、保留较新。

因此 ROS1 subscriber 侧 queue size 直接影响 callback 来不及处理时可以积压多少旧样本。

## 2. ROS2 depth 首先属于 History policy

ROS2 `rclcpp::QoS` 内部保存 `rmw_qos_profile_t`。

`keep_last(depth)` 设置：

~~~text
history = KEEP_LAST
depth = N
~~~

`keep_all()` 则切换为 KEEP_ALL。

所以 depth 的第一层含义是 middleware history contract，而不是 Executor callback queue 长度。

## 3. ROS2 backlog 可能存在于多个层

~~~text
DDS Writer History
        |
transport
        |
DDS Reader History
        |
RMW readiness
        |
Executor
        |
take
        |
callback
~~~

callback 处理不及时，样本通常首先积压在 reader history，而不是预先复制成 N 个 Executor callback item。

因此“积压在哪里”比“depth 是多少”更重要。

## 4. Reliability 与 depth 是正交维度

ROS2 还有：

~~~text
RELIABLE
BEST_EFFORT
~~~

Reliability 回答丢包时 middleware 的传输语义；History/Depth 回答本地历史保留多少。

例如：

~~~text
KEEP_LAST(1) + RELIABLE
KEEP_LAST(10) + BEST_EFFORT
~~~

两组配置分别表达了不同的 history 与 transport 组合。

## 5. Durability 又是第三个维度

ROS1 对后来加入的 subscriber 是否看到旧数据主要依赖 latch。

ROS2 则拆出：

~~~text
VOLATILE
TRANSIENT_LOCAL
~~~

因此 ROS1 比较集中的 queue/latch 概念，在 ROS2 被拆成 History、Depth、Reliability、Durability、Lifespan、Deadline、Liveliness 等策略。

## 6. QoS compatibility 会让不通信成为配置结果

ROS2 QoS 参与 offered/requested compatibility。

调试顺序应该是：

~~~text
1. endpoints matched?
2. QoS compatible?
3. data entering history?
4. executor taking fast enough?
~~~

而不是看到没数据就直接怀疑网络。

## 7. latest-state 控制应该优先防旧数据堆积

目标位置、速度估计、视觉目标框、当前姿态都更接近 state，而不是 event。

若业务只关心最新值，应优先：

- 小 history/depth；
- 明确丢旧策略；
- callback 足够快；
- 日志与控制链分开。

盲目把 depth 从 1 改成 100，可能只是把偶尔掉帧变成持续处理旧数据。

## 8. event stream 与 state stream 需要不同策略

| 数据 | 更像 | 核心要求 |
|---|---|---|
| 当前目标框 | state | 新鲜度 |
| 关节状态 | state | 新鲜度/周期 |
| 故障事件 | event | 不丢失 |
| 日志记录 | event/stream | 完整性 |

中间件配置应该由业务语义驱动，而不是从 Reliable 还是 BestEffort 开始倒推需求。

## 9. queue_size 与 depth 的正确对应

~~~text
ROS1 queue_size
  -> roscpp callback delivery backlog

ROS2 depth
  -> RMW/DDS history policy parameter
~~~

可以把两者都看成有界历史思想，但不能视为完全等价。真正做延迟优化时，先确定样本在哪个容器里排队，再调那个层的策略。
