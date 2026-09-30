# 跨主机通信：从“共享对象”变成“共享协议”

:::{contents} 本页目录
:depth: 3
:local:
:::

同进程里，两个模块可以共享 C++ 对象；同主机跨进程，还可以借助共享物理页。

一旦跨机器，这两种能力都消失了。

两台主机真正能够共享的，只剩下：

> 一套协议：双方都同意“这些字节表示什么、怎么分帧、怎么确认、怎么发现对方、失败以后怎么办”。

因此网络通信的核心不是“socket API”，而是把本地对象逐步变成一个分布式协议状态机。

## 第一层：对象必须变成可传输的数据表示

本地对象：

~~~cpp
struct Pose {
    double x;
    double y;
    std::string frame_id;
};
~~~

不能直接把对象内存镜像发走。

因为其中包含：

- 编译器/ABI 相关布局；
- padding；
- 本地指针；
- 容器内部状态；
- endian/alignment 差异。

因此路径通常是：

~~~text
typed object
↓
schema-aware serialization
↓
byte sequence
↓
framing
↓
transport
↓
byte sequence
↓
deserialize / zero-copy view
↓
remote typed object
~~~

LCM 在这一层用生成代码把消息编码成 wire bytes；可以对照 [LCM overview](../generated/lcm/overview.md) 的 publish 路径继续读。

## Serialization 不只是“CPU 花了一点时间”

serialization 同时定义：

~~~text
field order
field width
endianness
string/array encoding
optional fields
schema evolution
version compatibility
~~~

所以它既是性能问题，也是协议兼容问题。

真正工程化时必须问：

~~~text
旧版本 Subscriber 能否读新版本消息？
未知字段怎么处理？
可变长度数组是否需要额外 allocation？
能否直接在 receive buffer 上做 zero-copy view？
~~~

## 第二层：有了 bytes，还要解决 Framing

TCP 是 byte stream：

~~~text
[byte][byte][byte][byte]...
~~~

它没有“这一条消息到这里结束”的天然概念。

因此应用层通常要自己定义：

~~~text
magic
version
message length
message type/channel
payload
checksum
~~~

UDP 虽然保留 datagram 边界，但一个大型消息仍可能超过安全 MTU，需要应用自己分片，或者依赖 IP fragmentation。

所以“把消息写进 socket”之前，中间件往往已经做了自己的 frame/fragment protocol。

LCM 的 [UDP publish protocol](../generated/lcm/udpm-publish-protocol.md) 与 [receive/reassembly](../generated/lcm/receive-reassembly.md) 就是一个很干净的例子。

## UDP 与 TCP 不是“快 vs 稳”这么简单

### UDP

提供 datagram：

- 保留消息边界；
- 可能丢；
- 可能乱序；
- 可能重复；
- 不提供应用级 ACK；
- multicast 很方便。

适合 LCM 这类“低状态、允许应用接受丢包”的设计。

### TCP

提供可靠有序 byte stream：

- 字节不会凭空丢掉而不被连接语义发现；
- 但没有应用消息边界；
- 丢失的 TCP segment 会阻塞后续有序字节向应用交付；
- send() 成功只说明进入本机发送路径。

因此 TCP 的“可靠”仍然不等于：

~~~text
远端 callback 已执行
~~~

更不等于：

~~~text
机器人已经执行命令
~~~

## Reliability 必须分层理解

一条命令可能经历：

~~~text
Application write
↓
middleware accepted
↓
local kernel accepted
↓
NIC transmitted
↓
remote NIC received
↓
remote kernel accepted
↓
middleware reassembled
↓
callback dispatched
↓
actuator validated
↓
physical action executed
~~~

每一层都可能有自己的“成功”。

所以：

> 传输层可靠性、middleware 可靠性、业务可靠性不能混成一个词。

DDS RTPS 的可靠模式会引入 sequence、Heartbeat、AckNack 与重传状态；可以继续看 Cyclone DDS 的 [WHC reliability](../generated/cyclonedds/whc-reliability.md) 与 Fast DDS 的 [WriterHistory reliability](../generated/fastdds/writerhistory-reliability.md)。

但如果业务要求“电机已经接受并执行命令”，仍然需要 **business ACK**。

## 一个可靠消息协议为什么会自然长出 Sequence Number

假设 Sender 连续发：

~~~text
seq=100
seq=101
seq=102
~~~

Receiver 实际收到：

~~~text
100
102
~~~

如果没有 sequence number，它甚至无法知道 101 丢了。

所以可靠协议自然会出现：

~~~text
sequence number
received bitmap / gap list
ACK / NACK
retransmission cache
timeout / heartbeat
~~~

然后 Sender 还必须暂存未确认样本。

于是“可靠性”会直接回到上一章的 queue/history 与 memory pressure。

## 第三层：Discovery 是控制面，不是 Payload 数据面

一个 pub/sub 系统在发送数据之前，通常先要回答：

~~~text
谁在线？
对方地址是什么？
订阅了什么 topic/key？
类型兼容吗？
QoS 兼容吗？
~~~

这些消息和真正的大 payload 不是一类流量。

可以把系统拆成：

~~~text
Control plane
discovery / matching / liveness / route metadata

Data plane
payload / fragments / ACK-NACK / flow
~~~

DDS 的 SPDP/SEDP、eCAL registration、Zenoh scouting/declare、YARP Name Server 都属于控制面。

这一区分很重要，因为一个系统可能：

~~~text
业务数据非常轻
但 discovery storm 很重
~~~

如果只测 publish latency，很容易漏掉启动和拓扑变化时的控制面成本。

## 第四层：Routing 让通信从连接变成一张图

最简单的 pub/sub：

~~~text
A ─────────→ B
~~~

有路由节点以后：

~~~text
A
│
▼
Router 1
├────────→ B
└→ Router 2 → C
             └→ Storage
~~~

此时系统会出现新的状态：

- route table；
- subscription tree；
- session/face；
- next hop；
- reconnect；
- path failover；
- ACL；
- store-and-forward。

这已经不再是“socket wrapper”，而是一个小型分布式系统。

Zenoh 的价值也恰恰在这里：它把 key space、routing、query/storage 放在同一体系里。

## 网络 Backpressure 为什么比本机 Queue 更复杂

本机 queue 满了，你至少能立即看到一个明确状态。

网络上，压力可能藏在多层：

~~~text
middleware send queue
↓
kernel send buffer
↓
NIC TX ring
↓
switch/router
↓
remote receive buffer
↓
remote middleware queue
~~~

Sender 的 publish() 返回时，远端 Consumer 可能还完全没有处理。

因此“异步发送”必须明确它把 payload 复制/持有到哪里，以及 congestion 时怎么限速。

DDS FlowController、TCP congestion control、Zenoh congestion policy 都在不同层回答这一问题。

## Multicast 与 Unicast：Fan-out 在哪一层发生

一个 Publisher 发给 10 个 Subscriber。

### Application-level unicast

~~~text
Publisher
├→ B
├→ C
├→ D
...
~~~

Sender 可能重复发送多份。

### Network multicast

~~~text
Publisher
↓ one multicast datagram
network fabric
├→ B
├→ C
└→ D
~~~

Fan-out 被下沉到网络。

这也是 LCM 选择 UDP multicast 后架构能保持很小的原因之一：它把订阅分发的一部分复杂度交给了网络。

代价则是可靠性、跨网段部署、网络设备策略等限制。

## Failure Detection：连接断了和进程死了不是同一个事实

分布式系统里，超时只能说明：

~~~text
“我在规定时间内没有观察到对方。”
~~~

它可能是：

- 对端进程崩溃；
- 主机断电；
- 网络隔离；
- packet loss；
- scheduler 长时间卡顿；
- stop-the-world；
- 交换机故障。

因此 liveness 通常是基于 lease/heartbeat/timeout 的判断，而不是绝对“知道对方死了”。

这就是为什么分布式通信要接受“不确定性”。

## Clock 是网络延迟测量里的隐藏依赖

如果 Sender 在主机 A 记录：

~~~text
t_send = clock_A()
~~~

Receiver 在主机 B 记录：

~~~text
t_recv = clock_B()
~~~

那么：

$$
t_{recv} - t_{send}
$$

只有在两个时钟同步误差已知时才有意义。

机器人系统常见方案：

- PTP；
- NTP；
- hardware timestamp；
- sensor clock calibration；
- 单端 RTT 测量；
- 同机 monotonic clock 分段测量。

所以不要拿两个未经同步的 wall clock 直接声称“网络延迟 2.3 ms”。

## 幂等与重复：可靠重试可能制造第二个问题

当协议支持 retry：

~~~text
send command id=42
↓
ACK lost
↓
sender retries id=42
~~~

Receiver 可能实际收到了两次。

如果命令是：

~~~text
“把计数器加 1”
~~~

重复执行会产生错误结果。

因此关键业务协议经常需要 request id / message id 与去重：

~~~text
id=42 already executed
→ return cached result
→ do not execute again
~~~

这已经超出“网络传输”本身，进入分布式语义。

## 用五种中间件看不同取舍

### LCM

~~~text
schema-generated bytes
+ UDP multicast
+ very small control state
~~~

优先保持运行时简单。

### DDS

~~~text
discovery
+ QoS matching
+ RTPS reliability
+ histories
+ multiple transports
~~~

用更多状态换取更强的可配置语义。

### eCAL

~~~text
registration/discovery
+ SHM/UDP/TCP multi-transport
~~~

强调同机与跨机结合。

### YARP

~~~text
named ports
+ carrier negotiation
~~~

把连接与传输策略放到 port/carrier 模型。

### Zenoh

~~~text
key space
+ routing
+ pub/sub
+ query/storage
~~~

把问题继续扩展到分布式数据平面。

## 最后回到一条消息：跨主机到底发生了什么

一条普通 publish 可以被展开成：

~~~text
typed object
↓ encode
wire bytes
↓ frame / fragment
transport queue
↓ socket
kernel buffer
↓ NIC
network
↓ remote NIC
kernel receive
↓ parse / reassemble
reliability state
↓ receive queue
scheduler
↓ callback
typed object / view
~~~

真正的机制分析，就是在这张链上逐点回答：

~~~text
哪里 copy？
哪里排队？
哪里可能丢？
谁负责重传？
谁发现对方？
谁负责路由？
谁产生 backpressure？
业务成功在哪一层确认？
~~~

一旦这些问题清楚，所谓“DDS 很重”“UDP 很快”“Zenoh 是路由中间件”才会从印象变成可验证的工程事实。

## 网络还不是最后一层：现代 AI Pipeline 连“CPU 可直接访问”都不能默认

跨主机之后，我们已经失去了共享地址和共享物理页。

但传统网络中仍常默认 payload 最终落在 CPU-addressable buffer。

GPU/NPU、Camera DMA、RDMA registration 出现以后，这个假设也会失效：

~~~text
bytes 到了本机
≠
目标计算单元可以零成本访问它
~~~

所以下一篇继续把 memory domain 纳入通信契约：

→ [Heterogeneous Memory](heterogeneous-memory.md)
