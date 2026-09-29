# Event：Notifier 与 Listener 为什么不是另一套 Publish/Subscribe

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

Publish/Subscribe 解决的是：

~~~text
一份 payload
从 Publisher 到 Subscriber
~~~

Event 解决的是：

~~~text
某件事发生了
把一个小事件标识通知 Listener
~~~

两者都叫通信，但数据结构和成本模型完全不同。

## Event 的数据不是 Payload Chunk

Notifier API 的核心是：

~~~rust
pub fn notify_with_custom_event_id(
    &self,
    value: EventId,
) -> Result<usize, NotifierNotifyError> {
    self.__internal_notify(value, false)
}
~~~

事件携带的是 EventId，不需要先 loan 一块大共享 chunk。

这非常适合：

~~~text
frame ready
state changed
deadline reached
shutdown requested
blackboard entry updated
~~~

## 一个 Notifier 为什么维护多个 Listener Connection

固定源码中的连接结构：

~~~rust
struct Connection<Service: service::Service> {
    notifier:
        <Service::Event as Event<
            RelocatableCountingBitSet
        >>::Notifier,
    listener_id: UniqueListenerId,
    node_id: UniqueNodeId,
}
~~~

每个逻辑 Listener 都对应底层 Event connection。

Notifier 自己持有：

~~~text
ListenerConnections
└─ Vec<Option<Connection>>
~~~

所以 Event fan-out 不是把一个 payload 放入多条数据队列，而是向多个 listener 的事件 primitive 发通知。

## Connection 会动态更新

Notifier 创建时：

~~~rust
listener_connections
    .lock()
    .populate_listener_channels();
~~~

之后 notify / single-listener notify 前还会更新 connections。

因此：

~~~text
Service dynamic config
记录活跃 Listener

Notifier 本地 connection table
缓存实际可通知 endpoint
~~~

控制面和事件快路径仍然是分层的。

## 为什么 Event 用 CountingBitSet

Service 的 Event associated type建立在：

~~~text
RelocatableCountingBitSet
~~~

概念上可以把它理解为：

~~~text
EventId
→ bit/index

同一个 EventId 连续触发
→ count 增加
~~~

这解释了 v0.10.0 的 Listener 回调参数为什么是：

~~~text
EventActivation {
    id,
    count
}
~~~

而不是只返回一个布尔值。

如果 producer 连续触发同一事件多次，consumer 可以知道累计次数。

## Listener 的三种等待语义

固定源码：

~~~rust
pub fn try_wait(...)
pub fn timed_wait(...)
pub fn blocking_wait(...)
~~~

分别对应：

~~~text
try_wait
现在没事件就立即返回

timed_wait
最多阻塞 duration

blocking_wait
一直阻塞直到事件到来
~~~

这比让应用自己轮询共享 atomic 更完整，因为 OS wait primitive 可以让线程真正睡眠。

## notify 不等于 callback 立刻执行

事件时序仍然是：

~~~text
Notifier
↓
底层 event primitive ready
↓
Listener 所在线程变成 runnable
↓
OS scheduler
↓
blocking/timed wait 返回
↓
应用 callback
~~~

因此要区分 notification latency 和 application reaction latency。

后者还包含 scheduler latency。

## Linux 上 WaitSet 最终落到 epoll

固定源码 iceoryx2-cal/src/reactor/recommended.rs：

~~~rust
#[cfg(target_os = "linux")]
pub type Ipc =
    crate::reactor::epoll::Epoll;
~~~

非 Linux POSIX fallback：

~~~rust
pub type Ipc =
    crate::reactor::posix_select::Reactor;
~~~

所以 iceoryx2 的 Reactor 不是抽象概念而已。

在 Linux IPC variant 下，最终确实映射到 epoll。

## WaitSet 为什么还要把 Listener 里的事件读完

官方 event_multiplexing/wait.rs 特别提醒：

~~~rust
listener
    .try_wait(|event| {
        ...
    })
    .unwrap();
~~~

原因是：

> WaitSet 只告诉你“这个 fd 仍然可读”。

如果 callback 不消费底层 pending events：

~~~text
fd 仍然 ready
↓
epoll 立即再次返回
↓
callback 再次运行
↓
busy loop
~~~

这和网络 socket readiness 完全一样。

## 单独通知某个 Listener

v0.10.0 还提供：

~~~rust
notify_single_listener(...)
notify_single_listener_with_custom_event_id(...)
~~~

它通过 ListenerKey 定位 connection。

这说明 Event 模式不只有广播。

可以实现：

~~~text
1 → N broadcast

或者

1 → selected listener
~~~

## Deadline 为什么属于 Event Service Contract

Notifier 有 deadline，并且 NotifyError 明确包含：

~~~text
MissedDeadline
~~~

这不是说 OS 自动给你实时保证。

它表达的是应用级 timing contract：

~~~text
两次 notification 之间
不应超过某个时间
~~~

runtime 可以检测违反，但不能代替 SCHED_FIFO / CPU affinity / WCET 设计。

## Event 与 Pub/Sub 的选择

如果你传的是：

~~~text
20 MB point cloud
~~~

用 pub/sub shared chunk。

如果你只需要说：

~~~text
point cloud slot #7 已更新
~~~

Event 更自然。

很多高性能 pipeline 最终会组合：

~~~text
共享内存保存状态/数据
+
Event 只负责唤醒
~~~

这种“共享状态与轻量通知分离”的结构，同样常见于 GXF/Holoscan 一类 dataflow runtime。
