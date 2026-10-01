# 从 ROS1 到 ROS2：哪些限制来自通信架构，而不是 API 风格

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。这里比较的是通信职责如何重组，不把 ROS2 简化成某一个固定 DDS 实现；ROS2 的 RMW 层可以承载不同 middleware backend。

ROS2 并不是因为 ROS1 的 C++ API“不够现代”才出现。更根本的变化来自 ROS1 在 discovery、QoS、execution、type support 和部署边界上的结构性限制。

## 1. 先把 ROS1 的完整链压成一张图

~~~text
ROS Master
  registration graph
       |
       | XML-RPC publisherUpdate
       v
Subscriber
       |
       | requestTopic
       v
Publisher XML-RPC API
       |
       | TCPROS host/port
       v
TCP connection
       |
       | connection header
       v
serialized message stream
       |
       v
SubscriptionQueue
       |
       v
CallbackQueue
       |
       v
Spinner
       |
       v
user callback
~~~

这个架构的优点是简单、透明、容易抓包和定位对象。

但每个简单机制也定义了自己的能力边界。

## 2. 中央 Master 让 graph 简单，也引入 discovery 单点

Master 不参与 payload，所以故障时已有 TCPROS 可以继续。

但是：

~~~text
Master unavailable

existing TCPROS:
  may continue

new Publisher:
  cannot normally register

new Subscriber:
  cannot normally discover graph

future graph changes:
  cannot converge through Master
~~~

ROS2 的常见 DDS-based RMW 把 Participant/Endpoint discovery 下沉到 middleware；RMW Zenoh 等后端则用自己的分布式发现/路由模型。

共同点是：ROS2 client library 不再把一个中央 rosmaster 作为唯一 graph truth service。

## 3. TCPROS 有可靠字节流，但没有统一 QoS contract

ROS1 topic 最常见的语义来自：

~~~text
TCP ordered reliable stream
+
queue_size
+
latching
+
tcp_nodelay
~~~

机器人通信还经常需要表达：

- Best Effort 还是 Reliable；
- 保留一个最新样本还是一段 History；
- late joiner 是否要收到旧数据；
- 多久未更新算 deadline missed；
- endpoint 是否仍 alive；
- writer cache/resource limit 多大。

ROS1 这些行为分散在 TCP、queue、latching 和应用策略中。

DDS-based ROS2 把 History、Reliability、Durability、Deadline、Liveliness 等变成 QoS policy，并在 endpoint matching / runtime 中参与行为决定。

## 4. queue_size=1 为什么仍然不是端到端 latest-value guarantee

ROS1 Subscriber queue 可以只保留一个样本，但真实链路还有：

~~~text
publisher-side buffering
Connection write state
kernel TCP send buffer
network queue
kernel TCP receive buffer
SubscriptionQueue
CallbackQueue
OS runnable queue
~~~

某一层设置 depth=1，不能自动证明整个系统处理的永远是最新状态。

ROS2 QoS 同样不会魔法般提供硬实时，但它至少把更多 queue/history/reliability 语义提升为显式通信 contract。

## 5. Spinner 已经分离通信与执行，但 execution policy 仍很有限

ROS1 做对了一件重要的事：

> PollManager 不直接执行 user callback。

但 Spinner 主要提供：

~~~text
SingleThreaded
MultiThreaded
Async
custom CallbackQueue
~~~

它没有完整表达：

~~~text
callback dependency groups
mutually exclusive group
reentrant group
waitable
guard condition
executor policy
~~~

ROS2 把执行链变成更显式的：

~~~text
middleware readiness
   |
   v
rcl wait set
   |
   v
rclcpp Executor
   |
   v
Callback Group
   |
   v
user callback
~~~

这不意味着默认 Executor 自动满足实时性，只意味着“readiness 怎样映射到 execution”被提升为独立层。

## 6. ROS1 的 MD5 type identity 为什么简单但不够扩展

TCPROS header 用：

~~~text
type
md5sum
~~~

判断两端消息定义是否兼容。

优点：

~~~text
simple
cheap
connection-time check
~~~

边界：

~~~text
ROS-specific schema identity
limited schema evolution semantics
tight coupling to generated ROS1 messages
~~~

ROS2 使用 rosidl/type support，把 IDL、语言绑定和 middleware serialization/type system 分层得更明确。

从架构角度，是从：

~~~text
ROS message generation + MD5
~~~

走向：

~~~text
ROSIDL schema
  -> language type support
  -> middleware type support
~~~

## 7. Nodelet 为什么说明“部署边界就是性能边界”

ROS1 为避免大消息 transport serialization，常把多个 component 放进同一个 Nodelet manager。

于是：

~~~text
zero-copy opportunity
    <=>
same address space
~~~

代价是：

~~~text
shared failure domain
~~~

现代 ROS2/中间件生态进一步探索：

~~~text
same-process composition
intra-process optimization
loaned messages
DDS shared-memory transport
data sharing
iceoryx-style shared memory
GPU-backed buffer
~~~

目标是把“减少 copy”和“必须同进程”逐渐解耦。

## 8. RMW 为什么是 ROS2 里非常重要的一层

ROS1 roscpp 直接实现大量 graph、transport 和 callback runtime。

ROS2 把 middleware backend 抽成 RMW：

~~~text
rclcpp
   |
   v
rcl
   |
   v
rmw
   |
   +--> Fast DDS
   |
   +--> Cyclone DDS
   |
   +--> Zenoh RMW
   |
   +--> other implementations
~~~

优点是 client library 不再永久绑定某一个 transport/runtime。

代价也明显：一次 publish 的调用链更深，性能和故障归因更难。

所以理解 ROS2 必须明确：

> 现在看到的机制属于 rclcpp、rcl、rmw，还是具体 middleware backend？

## 9. ROS1 与 ROS2 应按职责对照

~~~text
ROS1                         ROS2
-------------------------------------------------------------
Master graph                 RMW/middleware discovery graph

registerPublisher            publisher/entity creation
registerSubscriber           subscription/entity creation

publisherUpdate              backend graph convergence

requestTopic                 backend endpoint/transport setup

TCPROS/UDPROS                backend data path
                             (RTPS/UDP/SHM/Zenoh/...）

type + md5sum                rosidl/type support/backend type

SubscriptionQueue            middleware history
+ local callback queue       + client-side execution queues

CallbackQueue/Spinner        wait set + Executor

Nodelet                      composition / intra-process
                             + optional loan/SHM paths
~~~

这不是严格的类名一一映射，而是“谁承担了同一种系统职责”的映射。

## 10. ROS2 没有消灭哪些问题

不论 backend 是 DDS 还是其他 RMW，以下问题仍然存在：

- discovery 与 payload data path 需要区分；
- I/O readiness 不应该直接等同于 user callback execution；
- queue/history 必须有有界 overload policy；
- type compatibility 必须在通信关系建立时解决；
- same-process 与 cross-process memory ownership 不同；
- async shutdown 仍要证明晚到 work 不访问已释放对象；
- 线程数增加不等于 deadline guarantee。

因此 ROS2 更复杂，不是因为这些问题消失了，而是因为更多问题被显式抽象出来。

## 11. 一条 ROS2 publish 应该怎样拆

后续 ROS2 专题应该沿真正源码链：

~~~text
rclcpp::Publisher::publish
        |
        v
rcl_publish
        |
        v
rmw_publish
        |
        v
RMW implementation
        |
        +--> Fast DDS writer
        |
        +--> Cyclone DDS writer
        |
        +--> other backend
        |
        v
transport / history / reliability
~~~

接收侧：

~~~text
backend receive/history
        |
        v
RMW readiness
        |
        v
rcl wait set
        |
        v
rclcpp Executor
        |
        v
Callback Group
        |
        v
user callback
~~~

这条链能避免把“ROS2 慢”“DDS 重”“Executor 抖动”混成一句无法验证的话。

## 12. ROS1 为什么仍然值得研究

ROS1 的 Runtime 相对小，很多机制能从公开 API 一路追到底层：

~~~text
Master registration
XML-RPC negotiation
TCP socket
framing
serialization
callback queue
spinner
intra-process path
~~~

它提供了一个很好的最小样本，让我们先建立三条分析坐标：

~~~text
data line:
  payload 在哪里，copy 了几次

control line:
  谁发现谁，谁通知谁

ownership/execution line:
  谁拥有对象，谁能执行 callback，何时安全销毁
~~~

再用同样坐标研究 ROS2，就能看出哪些复杂度来自 DDS/RMW，哪些来自 Executor，哪些是任何机器人 middleware 都绕不开的系统问题。
