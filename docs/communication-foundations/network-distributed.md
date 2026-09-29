# 跨主机通信：从“共享对象”变成“共享协议”

跨主机后，双方不再共享内存。

要让远端理解同一份数据，必须共享的是：

> 协议与数据表示。

## 对象必须变成字节表示

~~~text
C++ / Rust object
↓
serialization
↓
bytes
↓
framing
↓
transport
↓
bytes
↓
deserialization / zero-copy view
↓
remote object
~~~

因此跨主机通信天然增加 schema/version、byte order、alignment、serialization cost 和 packetization。

## UDP 与 TCP 不是“快 vs 稳”这么简单

UDP 提供 datagram：

- 保留消息边界；
- 不保证交付；
- 不保证顺序；
- 不替应用提供消息级可靠语义。

TCP 提供 byte stream：

- 可靠有序；
- 没有应用消息边界；
- 一个丢包会影响后续有序字节的可交付时间；
- socket send 成功只说明字节进入本地发送路径，不代表远端业务完成。

因此中间件通常还会在其上建立自己的 message id、sequence、fragment、heartbeat、ACK/NACK、retry 和 deadline。

## Reliability 有多个层级

~~~text
NIC transmitted
≠
remote kernel received
≠
middleware received
≠
application callback ran
≠
robot action executed
~~~

所以 Reliable DDS 或 TCP 都不能替代业务 ACK。

例如运动命令若要求确认，应有：

~~~text
request id
→ command
→ actuator validation
→ execution
→ business ACK
~~~

## Discovery 是控制面

数据面负责 payload。

发现系统负责：

~~~text
谁存在？
在哪里？
提供什么 topic/service/key？
支持什么 QoS/capability？
~~~

DDS SPDP/SEDP、eCAL registration、Zenoh scouting/declare、YARP Name Server 都是不同形式的控制面。

如果 discovery 出现 storm，业务数据面可能仍然很轻。

因此两者必须分开测。

## Routing 把通信从“一条连接”升级成图

在简单 pub/sub 中：

~~~text
A → B
~~~

到了 Zenoh 一类系统：

~~~text
A → Router 1 → Router 2 → B
               └──────→ storage
~~~

于是新的状态出现：route cache、face/session、subscription tree、path failure、reconnect、ACL 和 store-and-forward。

这已经进入分布式系统，而不只是 socket wrapper。

## Clock 是隐藏的分布式依赖

跨主机后，如果两个 timestamp 来自不同机器：

$$
t_B - t_A
$$

只有在时钟同步误差已知时才有明确意义。

机器人系统常需要 PTP、NTP、hardware timestamp 或 sensor-specific clock calibration。

所以端到端 latency 不能只拿两个未经同步的系统时钟相减。

## 从现有 Atlas 看五种答案

~~~text
LCM
最小协议 + UDP multicast

eCAL
discovery + SHM/UDP/TCP multi-transport

DDS
标准化 discovery + QoS + RTPS reliability

YARP
named ports + carrier negotiation

Zenoh
key space + routing + query/storage
~~~

后面比较中间件时，应先明确它主要解决的是哪一层，而不是只比较 API 长什么样。
