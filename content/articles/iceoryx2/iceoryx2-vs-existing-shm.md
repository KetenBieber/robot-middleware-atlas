# iceoryx2 vs eCAL SHM vs Fast DDS Data Sharing vs Cyclone PSMX：它们到底共享了什么

固定 iceoryx2 源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

比较重点放在共享内存机制的层级、所有权与数据路径，不进行整体优劣排序。

## 第一问：共享的是哪一层

### iceoryx2

核心就是：

~~~text
Publisher-owned SharedMemory Chunk
+
PointerOffset
+
ZeroCopyConnection
~~~

共享内存是数据面的基本形态。

### eCAL SHM

eCAL 是多 transport middleware。

SHM 是同机数据路径之一，仍与 registration、PubGate/SubGate、其他 transport 共存。

### Fast DDS Data Sharing

Fast DDS 仍以 DDS Entity、History、CacheChange、Reliability 为核心。

Data Sharing 优化的是同机 Writer/Reader history/payload delivery。

它和 SHM Transport 甚至是两层不同机制。

### Cyclone DDS PSMX

Cyclone DDS 仍有 DDSc/DDSI/RTPS 体系。

PSMX 是共享内存/本地 delivery 的扩展入口，和标准网络 RTPS data path 并存。

## 第二问：谁拥有 Payload

iceoryx2 最清楚：

~~~text
Publisher DataSegment
→ loan
→ shared Chunk
→ subscriber borrow
→ release
→ publisher reclaim
~~~

Fast DDS 围绕 CacheChange / payload pool / shared history。

Cyclone DDS 围绕 serialized data、loan、PSMX endpoint 与 history。

eCAL 的 SHM buffer rotation 又是另一套 ownership 模型。

所以不能因为都说 zero-copy，就认为内存生命周期相同。

## 第三问：Queue 里传什么

iceoryx2：

~~~text
PointerOffset
~~~

Fast DDS Data Sharing：

~~~text
共享 History / payload reference 语义
~~~

eCAL：

~~~text
SHM layer 自己的 buffer/sample metadata
~~~

Cyclone PSMX：

~~~text
PSMX endpoint 对 loaned sample/local delivery 的描述
~~~

对性能分析而言，这比“用了 shm”更具体。

## 第四问：是否天然跨主机

iceoryx2 的经典价值主要是同机 IPC。

eCAL / DDS 同时拥有 network transport，因此可以：

~~~text
local subscriber
走 shared memory

remote subscriber
走 network
~~~

这也意味着它们必须处理更复杂的兼容性和双路径 ownership。

## 第五问：Discovery 与 QoS 有多重

iceoryx2 也有 Service discovery、capacity contract、history、safe overflow 与 backpressure。

但它没有把目标扩展成 DDS 那样完整的标准化 QoS/RTPS interoperability。

因此 iceoryx2 的关注中心是：

~~~text
IPC mechanism first
~~~

DDS 的关注中心则是：

~~~text
distributed data bus semantics
+
local fast path
~~~

## 第六问：Crash Recovery

共享内存长期存在，因此不同实现都必须考虑 stale resource，只是暴露程度不同。

iceoryx2 把 NodeState::Dead 与 cleanup API 直接做成公开 runtime concept。

这使 shared-memory ownership 的失败路径在 API 层就可以被直接观察和验证。

## 四种实现放到同一机制层级

从底层机制到完整数据总线，可以整理为：

~~~text
Communication Foundations
↓
iceoryx2
纯共享内存 ownership

↓
eCAL
discovery + multi-transport

↓
Cyclone DDS / Fast DDS
标准 DDS 语义叠加 local fast path
~~~

这样 zero-copy 就不再是宣传词，而变成可追踪的数据结构和生命周期。
