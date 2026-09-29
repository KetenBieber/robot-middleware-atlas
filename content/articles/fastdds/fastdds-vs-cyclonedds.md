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
