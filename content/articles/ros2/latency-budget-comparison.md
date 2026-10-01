# ROS1 vs ROS2 延迟预算：一条机器人消息究竟在哪些地方变老

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。目标不是给两代系统做“谁更快”的结论，而是建立一张能用于排查机器人实时链路的延迟预算表。

Supporting source: `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。ROS2 接收侧必须把 DDS Reader History 中的数据等待与 Executor 调度等待分开计算。


## 1. 端到端延迟是一串等待

对一帧相机消息，真正关心的是：

\[
T_{e2e}=T_{publish}+T_{encode}+T_{queue}+T_{transport}+T_{receive}+T_{ready}+T_{schedule}+T_{callback}.
\]

ROS1 典型跨进程路径：

~~~text
publisher thread
  -> serialize
  -> Publication / SubscriberLink
  -> TCP socket
  -> subscriber receive
  -> SubscriptionQueue
  -> CallbackQueue
  -> Spinner
  -> callback
~~~

ROS2 典型路径：

~~~text
publisher thread
  -> rclcpp
  -> rcl_publish
  -> rmw_publish
  -> DDS writer/history/transport
  -> DDS reader/history
  -> RMW readiness
  -> rcl_wait
  -> Executor
  -> take
  -> callback
~~~

路径更长不等于必然更慢，但意味着等待被分散到更多独立机制中。

## 2. ROS1 的等待点：SubscriptionQueue 与 Spinner

`SubscriptionQueue::push` 使用有界队列；当 `fullNoLock()` 为真时会先 `pop_front()`，再放入新样本。

源码锚点：

~~~text
ros_comm/clients/roscpp/src/libros/subscription_queue.cpp
  SubscriptionQueue::push()
  SubscriptionQueue::call()
  SubscriptionQueue::fullNoLock()
~~~

Spinner 周期性调用：

~~~cpp
queue->callAvailable(timeout);
~~~

因此 ROS1 常见延迟链是：

~~~text
消息到达
  -> SubscriptionQueue 等待
  -> CallbackQueue 等待
  -> Spinner 获得 CPU
~~~

“网络正常”不能推出“控制器看到的是新数据”。

## 3. ROS2 多了一层 middleware history

ROS2 `Executor::wait_for_work()` 等的是 readiness，并不会先把 DDS History 中所有样本搬进 Executor。

~~~text
rclcpp/src/rclcpp/executor.cpp
  Executor::wait_for_work()
  Executor::get_next_ready_executable()
  Executor::execute_any_executable()
~~~

subscription ready 后，执行路径才进入：

~~~text
take_type_erased
  -> rcl_take
  -> rmw_take_with_info
  -> DDS reader take
~~~

因此 ROS2 的数据年龄至少要拆成：

~~~text
DDS History 等待
+
Executor scheduling 等待
~~~

这两个问题不能靠同一个参数解决。

## 4. callback 很快，端到端仍可能很慢

假设视觉 callback 每帧只执行 2 ms，但另一个 callback 偶尔占用同一执行线程 50 ms：

~~~text
camera sample ready
        |
        | waits behind 50 ms callback
        v
camera callback executes 2 ms
~~~

profiler 看到的是“视觉 callback 只有 2 ms”；控制器看到的却是“输入已经老了 50 ms”。

ROS1 对应 CallbackQueue/Spinner 争用；ROS2 对应 Executor/CallbackGroup/OS scheduling。

所以至少要记录：

~~~text
message source timestamp
callback entry timestamp
callback exit timestamp
~~~

## 5. publish() 的耗时也不是传输耗时

ROS1 发布端可能经历 serialization、fan-out、socket write/backpressure。

ROS2 发布端还可能经历：

- type adaptation；
- `rcl_publish`；
- RMW serialization；
- DDS Writer History；
- reliability 状态；
- async writer / flow controller；
- SHM / Data Sharing 分支。

所以 `publish()` 很快返回，不代表 reader 已经拿到样本；`publish()` 很慢，也不一定是 serialization 慢。

## 6. 控制系统最重要的是数据年龄

设控制周期 1 kHz，视觉只有 30 Hz。真正危险的是：

~~~text
视觉频率正常
吞吐正常
CPU 正常
但 controller 读到 100 ms 前的 frame
~~~

核心指标应该是：

[
Age=t_{consume}-t_{sensor}.
]

而不是只看 throughput。

## 7. 工程化延迟预算

| 阶段 | ROS1 | ROS2 | 主要风险 |
|---|---|---|---|
| 发布 | Publication | Publisher/rcl/RMW | serialization、loan fallback |
| 中间件积压 | socket queue | DDS Writer/Reader History | reliability、history |
| 本地接收 | SubscriptionQueue | RMW take | backlog |
| readiness | CallbackQueue | WaitSet | wakeup latency |
| 调度 | Spinner | Executor/CallbackGroup | long callback、优先级 |
| 用户处理 | callback | callback | WCET、锁、内存抖动 |

看到 80 ms 延迟时，先确认 80 ms 属于哪一段，再决定改 transport、queue、QoS 还是 execution。

## 8. 最低限度观测

关键控制链至少记录：

~~~text
sensor timestamp
publish timestamp
middleware receive/take timestamp
callback entry timestamp
callback exit timestamp
actuator command timestamp
~~~

ROS1 与 ROS2 架构不同，但调试原则相同：**先定位消息在哪里等待，再优化对应层。**
