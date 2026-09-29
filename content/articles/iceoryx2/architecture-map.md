# iceoryx2 架构地图：Node、Service、Port 与 CAL 为什么要分层

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 顶层不是一个全局 Broker

iceoryx2 的入口是 Node：

~~~text
Node<Service>
  ↓
service_builder(name)
  ↓
Service Builder
  ↓
open / create / open_or_create
  ↓
PortFactory
~~~

Node 是当前进程进入 iceoryx2 runtime 的 ownership root。

源码注释直接说明：Node 是整个 infrastructure 的入口，并拥有通过它创建的 entities。

## Service 是通信契约

一个 PublishSubscribe Service 不是 Publisher 本身。

它先固定 service name、payload type、user header type、max publishers/subscribers、subscriber buffer size、history size、safe overflow 以及资源与同步机制。

随后才从 PortFactory 创建 Publisher / Subscriber。

因此：

~~~text
Service
= 一组可互操作 endpoint 的共享契约
~~~

而不是“一个 socket”。

## PortFactory 把 Service 与 Endpoint 隔开

PublishSubscribe PortFactory 提供：

~~~text
publisher_builder()
subscriber_builder()
static_config()
dynamic_config()
~~~

所以 Service contract 与每个 endpoint 的本地策略分开。

Publisher 自己还有 max_loaned_samples、backpressure_strategy、allocation_strategy、port name 与 degradation handler。

Subscriber 则有 buffer_size、history_request、degradation handler 与 port name。

## 数据面核心对象

Publisher 侧：

~~~text
Publisher
  ↓
PublisherSharedState
  ↓
Sender
  ├─ DataSegment
  ├─ connections[]
  ├─ segment_states
  ├─ loan_counter
  └─ backpressure_strategy
~~~

Subscriber 侧：

~~~text
Subscriber
  ↓
SubscriberSharedState
  ↓
Receiver
  ├─ connection_storage
  ├─ DataSegmentView
  ├─ borrow tracking
  └─ channel state
~~~

这已经能看出它没有“把 payload 塞进 queue”。

queue 里主要传递的是共享 chunk 的位置。

## CAL 是什么

仓库里的 iceoryx2-cal 可以理解为 Communication Abstraction Layer。

上层 Service trait 不把底层写死，而是通过 associated type 选择：

~~~text
StaticStorage
DynamicStorage
SharedMemory
ResizableSharedMemory
ZeroCopyConnection
Event
Monitoring
Reactor
~~~

于是 pub/sub 算法和具体 OS/IPC primitive 解耦。

## 为什么 DataSegment 独立存在

Publisher 需要一块专门承载 payload 的共享区域。

DataSegment 可以是 Static 或 Dynamic，并统一暴露 allocate / grow / deallocate / bucket_size 等操作。

所以 Publisher 不直接操作 POSIX shm API。

## 为什么 Connection 与 SharedMemory 分开

这是 iceoryx2 最重要的架构分离之一。

~~~text
SharedMemory
负责 payload storage

ZeroCopyConnection
负责 PointerOffset delivery / return
~~~

如果把二者耦合，任何 queue/backpressure 变化都会影响 allocator 和 memory mapping。

拆开之后可以分别替换共享内存实现、connection queue、monitoring、event 与 reactor。

## 运行时总图

~~~text
Application
   │
   ▼
Node
   │
   ▼
Service / PortFactory
   │
   ├──────── Publisher
   │            │
   │            ├─ DataSegment
   │            │     └─ SharedMemory
   │            │
   │            └─ ZeroCopySender[]
   │
   └──────── Subscriber
                │
                ├─ DataSegmentView[]
                └─ ZeroCopyReceiver[]

control plane:
StaticStorage / DynamicStorage /
Monitoring / Event / Reactor
~~~

后续各机制都可以定位到这张对象图中的具体边界：Service 负责契约，Port 负责端点，DataSegment 负责 payload，Connection 负责 descriptor 交付。
