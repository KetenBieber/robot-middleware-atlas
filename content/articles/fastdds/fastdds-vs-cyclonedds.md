# Fast DDS vs Cyclone DDS：同一套 DDS/RTPS 语义，两套完全不同的实现分解

固定 Fast DDS：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

Cyclone DDS 对照基线：e54e991f75a3e67f8e628da3171122e36ea5b872。

这篇不评价“谁更好”，只比较源码设计。

## 1. 顶层对象模型

Fast DDS：

~~~text
DataWriter
→ DataWriterImpl
→ WriterHistory
→ StatefulWriter
→ ReaderProxy
~~~

Cyclone DDS：

~~~text
dds_writer
→ ddsi_writer
→ WHC
→ proxy_reader
~~~

Fast DDS 更显式 C++ object graph；Cyclone DDS 更强调 DDSc façade 与 DDSI protocol core 分层。

## 2. 样本容器

Fast DDS 的核心货币：

~~~text
CacheChange_t
~~~

Cyclone DDS 更偏：

~~~text
ddsi_serdata
+
WHC node
+
RHC sample
~~~

Fast DDS 把 writer GUID、sequence、payload、fragment state 聚合到 CacheChange；Cyclone DDS 更倾向把 serialized data 与各历史索引节点拆开。

## 3. 可靠性远端状态

Fast DDS：

~~~text
StatefulWriter
  └─ ReaderProxy[]

StatefulReader
  └─ WriterProxy[]
~~~

Cyclone DDS：

~~~text
ddsi_writer
  └─ proxy-reader match state

proxy_writer
  └─ reorder / reader matches
~~~

语义相似，类边界不同。

## 4. Writer History

Fast DDS WriterHistory 更像：

~~~text
CacheChange ownership
+ change pool
+ payload pool
+ writer notify
~~~

Cyclone DDS WHC 更突出：

~~~text
sequence hash
sequence interval tree
instance index
ACK-based removal
~~~

所以读 Fast DDS 时重点追 CacheChange ownership；读 Cyclone DDS 时重点追多索引 history structure。

## 5. Reader History

Fast DDS ReaderHistory 位于 RTPS reader 与高层 DataReaderImpl 之间，StatefulReader 完成 fragment assembly 后将 change commit。

Cyclone DDS 则有较清晰的：

~~~text
DDSI defrag/reorder
→ DDSc RHC
~~~

层次分隔。

## 6. Async Sending

Fast DDS 把 async scheduling 提升成 FlowController：

~~~text
writer queue
priority
bandwidth limit
period
~~~

Cyclone DDS async write 更偏 domain-global sendq：

~~~text
bounded queue
send thread
~~~

Fast DDS 因而在多 Writer QoS / bandwidth scheduling 上暴露更多调度对象。

## 7. Timed Events

Fast DDS：

~~~text
ResourceEvent
+ TimedEvent
+ sorted timers
~~~

Cyclone DDS：

~~~text
xevent queue
+ timed protocol events
~~~

两者都避免每个 timer 一个 OS thread。

## 8. 同机共享内存

Fast DDS：

~~~text
SHM Transport
Data Sharing
loan_sample
~~~

三层机制并存。

Cyclone DDS：

~~~text
PSMX
loan
local delivery
~~~

两者抽象边界不同，因此“都支持 shared memory”不是足够的性能比较。

## 9. WaitSet

Fast DDS：

~~~text
ConditionNotifier
→ WaitSetImpl
→ unordered_vector
→ condition_variable
~~~

Cyclone DDS：

~~~text
Entity observer
→ WaitSet
→ triggered-prefix array
→ condition variable
~~~

两者都由 application thread 等待，不是 socket receive thread 直接等 executor。

## 10. ROS 2 适配风格

rmw_fastrtps：

~~~text
rmw_publish
→ CustomPublisherInfo
→ Fast DDS
  DataWriter::write_w_timestamp

rmw_wait
→ collect StatusCondition /
  GuardCondition
→ Fast DDS WaitSet
~~~

rmw_cyclonedds：

~~~text
rmw_publish
→ dds_write_ts

rmw_wait
→ DDS read conditions /
  waitset
~~~

最终都把 ROS QoS/WaitSet 映射到 DDS，只是 type support、condition 组织与内部对象不同。

## 怎么用这两个专题

如果想理解 DDS 标准语义，可以先看 Cyclone DDS，因为 DDSc/DDSI 分层很清楚。

如果想理解 C++ 工程如何把 History、Pool、Proxy、Flow Controller 与 Transport 组合成显式对象系统，Fast DDS 更直观。

把两者对照读，才容易分清：

~~~text
哪些是 DDS/RTPS 必须存在的机制

哪些只是某个实现选择的数据结构
~~~

## 一张实现边界对照表

| 问题 | Fast DDS | Cyclone DDS |
| --- | --- | --- |
| 高层 façade | DataWriter/DataReader + Impl | DDSc entities |
| 协议 core | Stateful/Stateless Writer/Reader | DDSI writer/proxy entities |
| Writer 历史 | WriterHistory + CacheChange_t | WHC + serdata/index |
| Reader 重排 | WriterProxy + ReaderHistory | defrag/reorder + RHC |
| 远端状态 | ReaderProxy/WriterProxy 类 | match/proxy structures |
| 定时协议 | ResourceEvent/TimedEvent | xevent queue |
| 异步发送 | FlowController | send queue/thread |
| 同机优化 | SHM Transport + Data Sharing + loan | PSMX/local delivery/loan |

这个表的目的不是比较 feature 数，而是帮助定位“同一语义在哪个实现对象里”。

## 两种代码风格带来的阅读重点不同

Fast DDS 采用大量显式 C++ 对象与 Impl/Proxy/Pool 组合，适合沿 ownership graph 阅读：

~~~text
谁拥有谁
→ 谁持 mutex
→ 谁保存远端状态
→ 谁触发 timer
~~~

Cyclone DDS 更适合沿 DDSI 数据结构与索引关系阅读：

~~~text
serialized sample
→ WHC/RHC index
→ proxy match state
→ xevent / transport
~~~

如果只套用另一个实现的类名，很容易把标准机制和实现选择混在一起。

## Reliable 的共同不变量

无论实现如何不同，Reliable RTPS 都必须解决：

~~~text
sequence identity
missing detection
receiver acknowledgement
writer repair
history retention
late/stale state cleanup
~~~

Fast DDS 用 CacheChange_t + ReaderProxy/WriterProxy 显式表达；Cyclone DDS 用 WHC、
proxy/match 与 reorder 结构表达。这个“不变量”比类名更值得迁移到自己的 middleware
设计里。

## 机器人部署时不应该按品牌做结论

真正影响系统行为的是配置与 workload：

- payload 大小与频率；
- Reliability/History/ResourceLimits；
- 同机还是跨机；
- 是否启用共享内存/loan；
- publish mode；
- executor/waitset 调度；
- CPU affinity 与网络条件。

同一个实现换一组 QoS 和 transport，表现可能比两个实现之间的平均差异更大。

## 适合从 Fast DDS 学什么

Fast DDS 特别适合学习：

1. façade/Impl 如何隔离公共 API 与运行时；
2. CacheChange 如何成为跨 History/RTPS 的生命周期单位；
3. Proxy 对象如何把远端协议状态本地化；
4. FlowController 如何把发送调度对象化；
5. payload pool 如何给共享内存/loan 留扩展点；
6. discovery 与 user data 如何复用同一套 RTPS 基础设施。

把这些原则抽出来，比记住某个函数名更有价值。
