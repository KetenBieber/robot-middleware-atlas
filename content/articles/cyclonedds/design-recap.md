# Cyclone DDS 设计复盘：把发现、可靠性、缓存与线程重新连成一条因果链

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 一份消息真正经历了什么

完整链条可以压缩成：

~~~text
创建 Participant / Writer / Reader
  |
SPDP / SEDP
  |
QoS matching
  |
Writer endpoint knows readers
  |
dds_write
  |
typed sample -> serdata
  |
WHC if required
  |
RTPS DATA / DATAFRAG
  |
xmsg / xpack
  |
sendmsg
  |
recvmsg
  |
RTPS parse
  |
defrag / reorder
  |
RHC
  |
condition/status
  |
WaitSet or Listener
  |
application
~~~

只要中间任何一层有 queue、lock、allocation 或 retry，它都可能进入 latency、jitter 或 memory bound。

## 控制面与数据面不是完全独立

Discovery 创建 proxy endpoint；proxy endpoint 决定 DATA 到来后使用哪套 reorder、哪些 readers 匹配、往哪个 dqueue 交付。

QoS matching 决定 endpoint 是否相遇；Reliability 又决定 write 时是否写 WHC。

因此：

~~~text
control plane state
直接改变
data plane hot path
~~~

## 最关键的数据结构对应关系

| 问题 | 数据结构/机制 | 原因 |
| --- | --- | --- |
| Entity children | AVL tree | 动态对象、稳定地址、O(log n) |
| Endpoint lookup | entity index/hash | GUID 高频查找 |
| Writer history | seq hash + intervals | ACK/NACK 与稀疏序号 |
| Reader instances | hash table | keyed instance lookup |
| 非空 Reader instances | circular list | 避免全表扫描 |
| WaitSet ready set | array + triggered prefix | 小集合、低配置频率 |
| Async send | bounded linked queue + cond | 显式背压 |
| Receive temp objects | bump allocator | common path 少分配 |

这比背诵类名更重要，因为它揭示源码作者在优化什么访问模式。

## 谁执行哪段代码

~~~text
application thread
  create / read / take / default synchronous write

receive thread
  socket receive + RTPS parse

delivery queue thread
  optional asynchronous user-data delivery

xevent thread
  timed protocol events

sendq thread
  optional asynchronous final packet send

GC request thread
  deferred reclamation
~~~

这张表仍是近似模型；local delivery、Listener、PSMX 与具体配置可能改变调用上下文。

## 三个最容易形成错误直觉的地方

第一，Reliable 不是“把 UDP 变 TCP”。它是 RTPS 自己的 sequence、heartbeat、ACKNACK、WHC 与 retransmit 协议。

第二，Keep Last 1 不是全链只有一个 sample。它主要约束 Reader/Writer history policy，socket、reorder、executor 仍可能产生排队。

第三，Asynchronous Write 不是“publish 永不阻塞”。固定实现 sendq 只有 200 个 xpack，满后 producer 会等待。

## 与 LCM/eCAL/Zenoh 对照时看什么

LCM 把可靠性和发现大量留给应用，Cyclone DDS 则把 endpoint discovery、QoS compatibility 与可靠性状态纳入协议内核。

eCAL 更强调 registration + 多 transport 选择；Cyclone DDS 更强调 DDS data model 与 RTPS interoperability。

Zenoh 把 key expression、query 与 routing data space 放在中心；Cyclone DDS 则围绕 Topic/Type/Entity/QoS 构建实时发布订阅。

没有哪个“更高级”的统一结论，真正区别是它们把哪些复杂性放进 middleware，哪些留给应用。

## 进入 ROS 2 以后再多一层

ROS 2 不直接把 rclcpp Publisher 当作 DDS Writer。中间还有 RMW：

~~~text
rclcpp
-> rcl
-> rmw
-> rmw_cyclonedds
-> Cyclone DDS DDSc
-> DDSI / RTPS
~~~

所以分析 ROS 2 通信延迟时，必须区分 Executor scheduling、RMW adapter 与 DDS network path。

下一阶段的真实案例会分别用官方 ddsperf 和 ROS 2 rmw_cyclonedds 验证这些机制如何进入实际工程代码。
