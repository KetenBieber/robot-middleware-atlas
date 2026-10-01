# ROS1 vs ROS2 控制链案例：30 Hz 视觉如何安全驱动 1 kHz 底盘执行环

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。案例是一条典型机器人链：相机约 30 Hz 输出目标水平位置，控制器据此修正底盘 yaw，而电机速度环运行在 1 kHz。

这个例子很适合暴露一个事实：**通信频率、控制频率和执行频率不是同一个东西。**

## 1. 先画清楚三个时钟

~~~text
camera / detector
     30 Hz
       |
       v
vision target state
       |
       v
yaw controller
   100~1000 Hz
       |
       v
chassis command
       |
       v
motor speed loop
     1 kHz
~~~

视觉每约 33 ms 才产生一次新观测，但执行器每 1 ms 都需要更新。

所以控制系统不能假设每个 1 kHz control tick 都会收到新视觉消息。

真正需要的是：

> 控制线程持续运行，并持有最近一次有效观测及其时间戳。

## 2. 错误设计：让视觉 callback 直接承担控制周期

如果写成：

~~~text
image callback
   |
compute yaw correction
   |
send motor command
~~~

控制更新频率就被锁死在相机/检测器频率附近。

更糟的是，detector 抖动会直接变成 actuator jitter。

这属于 execution architecture 问题，不是 ROS1/ROS2 transport 问题。

## 3. 更合理的设计：latest-state mailbox + fixed-rate controller

~~~text
vision callback
   |
update latest target state
   |
atomic/mutex protected state
   |
-----------------------------
fixed-rate control thread/timer
   |
read latest target + age
   |
compute yaw rate
   |
send chassis command
~~~

关键状态至少包括：

~~~text
target_x
confidence
capture_timestamp
receive_timestamp
valid
~~~

控制器每次运行都计算：

\[
Age=t_{now}-t_{capture}.
\]

如果 Age 超过阈值，则进入 stale-data policy。

## 4. ROS1 中最容易出现的陈旧数据问题

如果 subscription `queue_size` 太大，而视觉 callback 或控制逻辑来不及处理，旧帧会积压。

对于“篮筐当前水平位置”这种 state，旧样本价值快速衰减。

因此 ROS1 常见设计应倾向：

~~~text
small queue_size
fast callback
latest-state handoff
fixed-rate controller
~~~

而不是扩大 queue 去追求每一帧都处理。

## 5. ROS2 中要同时处理 History 与 Executor

ROS2 若使用 `KEEP_LAST depth=N`，样本先受 DDS History 管理。

然后还要经过：

~~~text
WaitSet
  -> Executor
  -> take
  -> callback
~~~

所以控制链有两个独立问题：

1. history 是否积压旧视觉数据；
2. Executor 是否让视觉 callback 或控制 timer 等太久。

仅把 depth 改成 1，并不能解决一个被长 callback 阻塞的 SingleThreadedExecutor。

## 6. 一个更稳妥的 ROS2 执行布局

~~~text
Vision subscription
  -> vision CallbackGroup
  -> latest-state buffer

Control timer
  -> dedicated MutuallyExclusive CallbackGroup
  -> high-priority executor/thread

Logging / diagnostics
  -> separate executor/process
~~~

如果控制 timer 与图像处理 callback 共用一个单线程 Executor，那么 20 ms 图像任务足以破坏 1 ms 控制周期。

## 7. QoS 应由视觉数据业务语义决定

目标框/目标水平偏差更像 latest state，而不是 event log。

常见设计目标：

~~~text
fresh > complete
~~~

因此重点不是“必须 RELIABLE”，而是：

- history 很浅；
- 旧样本快速淘汰；
- 允许检测器偶尔掉帧；
- 控制器能识别 stale input。

对于一个 30 Hz 目标观测，如果收到 300 ms 前的数据，即使它可靠到达，对控制也可能已经没有意义。

## 8. stale-data policy 才是闭环安全的关键

控制器不应无限使用最后一次观测。

可以定义：

~~~text
Age < 50 ms:
  normal visual correction

50 ms <= Age < 150 ms:
  decay command / limit yaw rate

Age >= 150 ms:
  stop visual correction
  hold or enter search mode
~~~

具体阈值应由相机帧率、检测器延迟、底盘动力学与安全要求共同确定。

这比“ROS 消息有没有丢”更接近真实控制问题。

## 9. ROS1 与 ROS2 在这个案例里的真正差异

ROS1：

~~~text
SubscriptionQueue
  -> CallbackQueue
  -> Spinner
  -> latest-state buffer
  -> control loop
~~~

ROS2：

~~~text
DDS History
  -> WaitSet
  -> Executor
  -> take
  -> latest-state buffer
  -> control loop
~~~

两者都不应该直接把消息到达频率当成控制频率。

ROS2 多出来的能力主要是 QoS contract、CallbackGroup、backend 可替换、intra-process / loan / DDS SHM，但这些能力不会替应用完成控制架构设计。

## 10. 真正值得测的指标

对这类视觉伺服链，建议测：

- camera capture -> publish latency；
- publish -> callback entry latency；
- callback jitter；
- target Age；
- control timer jitter；
- control computation WCET；
- command -> motor-loop latency；
- stale-data trigger 次数。

最后画成：

~~~text
capture
  |
  | perception delay
  v
publish
  |
  | middleware + scheduling delay
  v
callback
  |
  | state handoff
  v
control tick
  |
  | actuator path
  v
motor loop
~~~

这张时间链比单独比较 ROS1/ROS2 ping-pong latency 更能解释机器人为什么会“看起来慢半拍”。

## 11. 结论

ROS1 与 ROS2 的通信机制不同，但对 30 Hz 视觉 + 1 kHz 执行环，最关键的控制原则相同：

> 感知线程负责更新最新状态，控制线程按固定周期消费最新状态，并显式判断数据年龄。

中间件负责尽量低延迟地完成数据交付；闭环稳定性仍然取决于应用如何组织时钟、队列、线程与 stale-data policy。
