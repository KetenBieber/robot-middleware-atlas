# ROS1 与 ROS2 通信 Runtime 对照：哪些问题被重新分层了

本篇以 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 为主固定版本，并对照 Atlas 已完成的 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b` 专题。目的不是给两代系统打分，而是逐项看通信职责被怎样重新切分。

## 总体结构先放在一起

### ROS1

~~~text
               ROS Master
                  |
            XML-RPC graph
                  |
 Publisher ---------------- Subscriber
              TCPROS
                  |
          SubscriptionQueue
                  |
           CallbackQueue
                  |
              Spinner
~~~

### ROS2

~~~text
          DDS distributed discovery
                    |
               ROS GraphCache
                    |
 Publisher -> rcl -> rmw -> DDS -> Reader
                    |
                 WaitSet
                    |
                Executor
                    |
              CallbackGroup
                    |
                 callback
~~~

变化不是“把 TCP 换成 DDS”这么简单，而是 discovery、transport、QoS、execution 和 memory ownership 都被重新分层。

## 1. ROS Master：中心目录变成 distributed discovery + local graph cache

ROS1：

~~~text
registerPublisher
registerSubscriber
lookup / publisherUpdate
        |
        v
ROS Master
~~~

中心 Master 提供明确的 graph state。

ROS2：

~~~text
DDS Participant discovery
DDS Endpoint discovery
        |
        v
RMW GraphCache
        |
ParticipantEntitiesInfo
~~~

这里没有中心 ROS Master，但每个参与者需要维护和同步自己的 graph view。

### 真正的设计变化

“去中心化”并不会消灭 registry。

它把：

~~~text
one global registry
~~~

变成：

~~~text
many local caches
      +
synchronization/discovery protocol
~~~

所以故障模型也变了：ROS1 要关心 Master 可达性，ROS2 要关心 discovery 网络、Domain、endpoint matching 和 graph convergence。

## 2. TCPROS：固定 transport 变成 RMW contract

ROS1 数据面主线：

~~~text
roscpp
  |
TCPROS header/framing
  |
TransportTCP
  |
socket
~~~

ROS2：

~~~text
rclcpp
  |
rcl
  |
rmw
  |
+------------------+
| Cyclone DDS      |
| Fast DDS         |
| other RMW        |
+------------------+
~~~

RMW 把 ROS API 与具体 middleware 解耦。

### 得到什么

同一个 rclcpp API 可以落到不同 backend。

ROS2 上层可以依赖：

~~~text
rmw_publish
rmw_take
rmw_wait
rmw QoS
~~~

而不直接依赖 Cyclone/Fast DDS 类型。

### 付出什么

诊断路径更长。

ROS1 出现发布阻塞，可以较快追：

~~~text
Publication -> Connection -> TransportTCP -> socket
~~~

ROS2 则要继续问：

- 当前 RMW 是谁？
- DDS Writer 同步还是异步路径？
- History 是否满？
- Reliability 是否等待 reader？
- SHM/Data Sharing 是否参与？
- FlowController 是否限流？

抽象能力更强，但性能结论必须固定实现版本。

## 3. queue_size：局部 backlog 变成 QoS contract

ROS1 常见：

~~~cpp
subscribe(topic, queue_size, callback);
~~~

queue size 主要描述本地积压。

ROS2 需要同时描述：

~~~text
History
Depth
Reliability
Durability
Deadline
Liveliness
Lifespan
~~~

其中 Reliability、Durability、Deadline、Liveliness 还是 offered/requested contract，配置不兼容时 endpoints 可以根本不建立有效数据关系。

### 对控制系统的意义

ROS1 容易把问题归结成“queue 太大/太小”。

ROS2 必须同时区分：

~~~text
network delivery semantics
        |
DDS History semantics
        |
Executor scheduling semantics
~~~

把 depth 调大并不自动提高控制质量，可能只是保留更多旧样本。

## 4. Spinner：CallbackQueue 变成 WaitSet + Executor

ROS1：

~~~text
Transport/Poll
    |
SubscriptionQueue
    |
CallbackQueue
    |
Spinner
~~~

ROS2：

~~~text
RMW readiness
    |
rcl_wait_set
    |
Executor
    |
CallbackGroup
    |
take
    |
callback
~~~

ROS2 的 WaitSet 可以统一等待：

- subscription；
- timer；
- service；
- client；
- guard condition；
- middleware event。

这让 execution plane 与 middleware readiness 的边界更统一。

### 但统一不等于实时

SingleThreadedExecutor 仍可能：

~~~text
long vision callback
       |
blocks
       |
short control callback
~~~

MultiThreadedExecutor 仍然受：

- CallbackGroup；
- application locks；
- OS scheduler；
- shared cache；
- worker priority；

影响。

所以 ROS2 把执行模型暴露得更明确，但不会自动产生确定性。

## 5. CallbackQueue 与 DDS History 不是同一个东西

ROS1 数据积压比较直观：

~~~text
network receive
   |
SubscriptionQueue
   |
CallbackQueue
~~~

ROS2 常见路径：

~~~text
DDS Reader History
   |
RMW ready
   |
WaitSet
   |
take one sample
   |
callback
~~~

Executor 并不会把 DDS History 中所有样本提前复制成自己的 callback queue。

所以分析 backlog 时要问：

> 样本现在堆在 DDS History，还是 callback 正在 CPU 上排队？

这两种延迟的解决方法不同。

## 6. Nodelet：容器式同进程优化变成 rclcpp intra-process

ROS1 Nodelet：

~~~text
Nodelet Manager
    |
pluginlib-loaded Nodelets
    |
roscpp intra-process links
~~~

ROS2：

~~~text
Composable Nodes
    |
rclcpp Publisher/Subscription
    |
IntraProcessManager
~~~

共同原理是：

> 用同地址空间换取对象级传递，避免普通跨进程序列化路径。

ROS2 的 IntraProcessManager 进一步把 subscriber 分成 shared ownership 与 unique ownership 两类，并在 fan-out 时显式处理 copy/move。

这让“同进程 no-copy”变成一个 ownership 问题，而不仅是 transport 问题。

## 7. zero-copy：Nodelet object sharing 之后又多了一层 middleware loan

ROS1 最经典低复制路径是同进程 Nodelet。

ROS2 除了 intra-process，还提供 RMW loan contract：

~~~text
application
    |
borrow middleware sample
    |
construct in place
    |
publish
    |
backend owns/recycles
~~~

同时 DDS backend 还可能提供 SHM/Data Sharing。

所以 ROS2 的低复制能力分成至少三层：

~~~text
rclcpp intra-process
RMW loan
DDS shared-memory/data-sharing transport
~~~

它们不是同一种机制。

### 最重要的源码阅读规则

看到：

~~~cpp
borrow_loaned_message()
~~~

不能直接写“ROS2 实现了 zero-copy”。

还必须验证：

- 当前 RMW 是否支持；
- message type 是否适合 loan；
- SHM 是否启用；
- publish/take 哪一段使用同一 storage；
- fan-out 是否产生 copy。

API 只是 capability 请求，不是性能证明。

## 8. Serialization：从集中可见变成 backend/type-support responsibility

ROS1 roscpp 的 serialization 链很集中：

~~~text
Serializer<T>
    |
SerializedMessage
    |
TCPROS frame
~~~

ROS2 ordinary typed publish 可以把 ROS object 一路交到 RMW/backend。

然后由具体 type support / DDS writer path 决定如何进入 CDR 或共享内存 sample。

这样更灵活，但也意味着复制点和 serialization point 更依赖 implementation。

所以 ROS2 做性能审计时，必须把：

~~~text
ROS distribution
RMW implementation
DDS version
transport config
message type
~~~

一起固定。

## 9. discovery failure 的故障模式改变了

ROS1 常见：

~~~text
Master unreachable
wrong ROS_MASTER_URI
stale registration
publisher XMLRPC unreachable
TCPROS connection failure
~~~

ROS2 常见：

~~~text
wrong ROS_DOMAIN_ID
multicast/unicast discovery blocked
Participant not discovered
QoS incompatible
RMW/config mismatch
GraphCache not converged
DDS transport/SHM configuration issue
~~~

因此迁移诊断方法不能只是把 `rosnode list` 换成 `ros2 node list`。

系统层问题已经改变。

## 10. “topic exists” 在两代系统都不等于控制链健康

ROS1：

~~~text
Master sees publisher/subscriber
          !=
TCPROS healthy
          !=
callback timely
~~~

ROS2：

~~~text
Graph sees endpoint
      !=
QoS compatible
      !=
sample fresh
      !=
Executor timely
~~~

真正的控制链是：

\[
Sensor
\rightarrow
Transport
\rightarrow
History/Queue
\rightarrow
Executor
\rightarrow
Controller
\]

任何一层都可能让数据过期。

所以对于机器人控制，最重要的指标往往不是“Topic Hz 看起来正常”，而是：

\[
Age=t_{control}-t_{sample}
\]

以及它的 worst-case/jitter。

## 11. 从框架作者视角看两代设计

ROS1 给出几个很清晰的基础原则：

- discovery 与 data plane 可以分离；
- receive 与 callback execution 必须解耦；
- queue 必须有容量策略；
- 同进程大 payload 值得独立优化。

ROS2 在这些问题上继续拆层：

- RMW 隔离 middleware；
- DDS 提供 discovery 与 QoS；
- GraphCache 恢复 ROS node 语义；
- WaitSet 抽象 readiness；
- Executor 抽象 callback scheduling；
- IntraProcessManager 管同进程 ownership；
- Loan API 暴露 middleware memory capability。

代价是对象更多、状态更多、调用链更长。

## 一张职责迁移表

| 职责 | ROS1 | ROS2 |
|---|---|---|
| Graph discovery | ROS Master / XML-RPC | DDS discovery + RMW GraphCache |
| Topic transport | TCPROS / UDPROS | RMW backend + DDS/RTPS/SHM |
| Type/data conversion | roscpp Serializer | rosidl type support + DDS backend |
| Queue/history | SubscriptionQueue 等 | DDS History + rclcpp local buffers |
| Callback scheduling | CallbackQueue + Spinner | WaitSet + Executor + CallbackGroup |
| Same-process path | Nodelet + roscpp intraprocess | Composable Nodes + IntraProcessManager |
| Low-copy memory | same-process object sharing | intra-process + RMW loan + DDS SHM |
| Reliability semantics | transport-oriented | QoS offered/requested contract |

## DDS 专题应该从哪里接进来

ROS2 层读到：

~~~text
rmw_cyclonedds -> dds_write
rmw_fastrtps  -> DataWriter::write
~~~

就不应该继续在 ROS2 文章里重复 DDS 内部细节。

直接跳到 Atlas 已有的 DDS 主线：

~~~text
Writer
  |
History
  |
Reliability
  |
RTPS
  |
UDP / SHM
~~~

反方向从 DDS 回到机器人 callback，则回到：

~~~text
Reader
  |
RMW readiness
  |
WaitSet
  |
Executor
  |
take
  |
callback
~~~

这样 ROS2 与 DDS 形成一张连续知识图，而不是两套重复教程。

## 最后的架构定位

ROS1 更像一套自带发现和传输协议的机器人消息 Runtime。

ROS2 更像一个分层集成框架：

~~~text
ROS application semantics
          |
       rcl/rmw
          |
replaceable middleware runtime
          |
DDS/network/shared-memory
          |
Executor-based application scheduling
~~~

理解这层差异以后，再讨论 ROS2 的实时性、商用适配或是否值得采用，才有可以验证的技术对象，而不是停留在 API 或生态印象上。
