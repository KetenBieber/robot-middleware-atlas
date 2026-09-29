# ZeroCopyConnection：为什么 Queue 里传的是 PointerOffset 而不是 Payload

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## Connection 的接口已经暴露了设计核心

固定源码：

~~~rust
pub trait ZeroCopySender {
    fn try_send(
        &self,
        ptr: PointerOffset,
        sample_size: usize,
        channel_id: ChannelId,
    ) -> Result<
        Option<PointerOffset>,
        ZeroCopySendError>;

    fn blocking_send<F>(
        &self,
        ptr: PointerOffset,
        sample_size: usize,
        channel_id: ChannelId,
        ...
    ) -> Result<
        Option<PointerOffset>,
        ZeroCopySendError>;

    fn reclaim(
        &self,
        channel_id: ChannelId,
    ) -> Result<
        Option<PointerOffset>,
        ZeroCopyReclaimError>;
}
~~~

三件事正好组成：

~~~text
deliver
wait/backpressure
reclaim
~~~

## 为什么返回 Option<PointerOffset>

发送新的 offset 时，connection 还可能顺便返回已经可以回收的旧 offset。

这让：

~~~text
send new reference
+
reclaim old reference
~~~

可以在同一控制路径里推进。

真正的大 payload 不需要来回搬。

## Receiver 侧接口

~~~rust
pub trait ZeroCopyReceiver {
    fn has_data(
        &self,
        channel_id: ChannelId
    ) -> bool;

    fn receive(
        &self,
        channel_id: ChannelId
    ) -> Result<
        Option<PointerOffset>,
        ZeroCopyReceiveError>;

    fn release(
        &self,
        ptr: PointerOffset,
        channel_id: ChannelId
    ) -> Result<(), ZeroCopyReleaseError>;

    fn borrow_count(
        &self,
        channel_id: ChannelId
    ) -> usize;
}
~~~

这已经非常接近一个“offset ownership channel”。

## Queue 的元素为什么很小

假设 payload 是 20 MB：

~~~text
queue element:
PointerOffset + metadata
~~~

而不是：

~~~text
queue element:
20 MB payload
~~~

这样 queue 的 cache footprint、copy cost 与消息尺寸不再线性绑定。

大数据只存在 shared memory pool。

## Connection 为什么要知道 Buffer Size

ZeroCopyConnectionBuilder 直接配置：

~~~text
buffer_size
safe_overflow
receiver_max_borrowed_chunks
max shared-memory segments
number of chunks
number of channels
channel state
~~~

所以 connection 本身也是容量 contract。

zero-copy 并没有消灭 queue，它只是把 queue 中的数据从 payload 变成 descriptor。

## Used Chunk Tracking

Sender 不能只把 offset 扔出去后忘掉。

一个 chunk 可能：

~~~text
sent
→ queued
→ received
→ borrowed
→ released
→ returned
→ reclaimed
~~~

任何一步被中断，runtime 都需要知道 chunk 当前是否仍可能被对端访问。

因此 used-chunk tracking 是内存安全的一部分。

## Channel State 为什么需要原子状态

ZeroCopyConnection 还有 ChannelState。

它表达：

~~~text
OPEN
DISCONNECT_HINT
CLOSED
~~~

关闭不能简单 delete queue。

双方可能还有：

- 在途 offset；
- borrowed sample；
- retrieve buffer 中等待归还的 offset。

所以 disconnect 本身也是状态机。

## 这和 Socket Queue 有何不同

Socket：

~~~text
send bytes
↓
kernel copies/queues bytes
↓
remote receives bytes
~~~

ZeroCopyConnection：

~~~text
payload already shared
↓
send descriptor
↓
remote maps same allocation
~~~

因此：

> connection 的正确性主要围绕 descriptor 生命周期，而不是 payload serialization。

## 对具身大消息的意义

消息越大，descriptor-based IPC 的优势越明显，因为 control-plane queue 的成本不会跟 payload size 同步增长。

但真正瓶颈会转移到：

- shared memory bandwidth；
- cache misses；
- producer/consumer computation；
- borrow duration；
- queue capacity；
- fan-out ownership。
