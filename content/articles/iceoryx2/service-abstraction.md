# Service Trait：iceoryx2 如何把存储、连接、监控与等待机制做成可替换策略

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 为什么不能把 IPC 写死成 POSIX SHM + futex

如果上层 Publisher 直接调用 shm_open、mmap、pthread mutex 或 eventfd，那么 process-local 模式、跨平台适配、单线程策略、测试和 crash monitoring 都会被硬编码进同一实现。

iceoryx2 把这些变化压力集中到 Service trait。

## 固定源码中的 Service Associated Types

Service trait 包含：

~~~rust
type StaticStorage: StaticStorage;

type PersistentDynamicStorage<T>: DynamicStorage<T>;

type Bag: BagFamily;

type DynamicStorage<T>: DynamicStorage<T>;

type SharedMemory: SharedMemoryForPoolAllocator;

type ResizableSharedMemory:
    ResizableSharedMemoryForPoolAllocator<
        Self::SharedMemory>;

type Connection: ZeroCopyConnection;

type Event: Event<RelocatableCountingBitSet>;

type Monitoring: Monitoring;

type Reactor: Reactor;
~~~

这一段几乎就是整个 IPC runtime 的 dependency injection 表。

## StaticStorage 与 DynamicStorage 为什么分开

创建后不应随意改变的：

~~~text
name
messaging pattern
type details
capacity contract
history/safe-overflow capability
~~~

适合 StaticStorage。

运行中会改变的：

~~~text
active publisher list
active subscriber list
endpoint details
ownership records
~~~

适合 DynamicStorage。

这样 discovery 不需要每次重写完整静态配置。

## PersistentDynamicStorage 为什么单独存在

有些动态信息即使进程 crash 也必须留下足够线索供其他进程恢复。

因此 dynamic 不等于 process-private temporary state。

生产级 IPC 必须考虑 abnormal termination。

## SharedMemory 与 Connection 是两个独立协议

Service trait 的语义非常明确：

~~~text
SharedMemory
memory used to store payload

Connection
mechanism used to exchange pointers
to the payload
~~~

它直接给出 iceoryx2 的数据面拆分：

~~~text
Payload
  放 shared memory

PointerOffset
  走 ZeroCopyConnection
~~~

这一边界把 payload storage 与 descriptor delivery 解耦，是 iceoryx2 数据面的核心架构约束。

## Event 与 Reactor 为什么不是 Connection

数据可达和事件通知不是完全同一个问题。

Event 负责 endpoint 间事件信号。

Reactor 负责：

~~~text
wait on multiple events
~~~

把它们从 payload delivery 中独立出来，能让 pub/sub 数据、event messaging、request-response 与 application event loop 复用不同机制。

## Monitoring 是 IPC 不可缺失的一部分

同机 shared-memory 系统最危险的不是 packet loss，而是：

~~~text
进程突然死亡
但共享对象仍然存在
~~~

所以 Service 明确要求 Monitoring。

这让 dead-node detection 不是上层应用额外 patch，而是 runtime contract 的一部分。

## Thread-safety 也是 Policy

Service 还定义 ArcThreadSafetyPolicy。

源码注释明确区分：

~~~text
MutexProtected
ports thread-safe
payload 可跨线程移动/共享

SingleThreaded
ports/payload 不提供相同 Send/Sync 能力
~~~

因此：

> IPC 与线程安全是正交的两个维度。

## 设计上的收益

上层 PublishSubscribe 算法只依赖抽象：

~~~text
allocate shared chunk
send offset
receive offset
signal event
monitor process
wait on reactor
~~~

而不依赖具体 syscall。

它把整个 runtime 的系统能力编译进 Service 类型。
