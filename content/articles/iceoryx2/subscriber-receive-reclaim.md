# Subscriber Receive：从 PointerOffset 到本地 Sample，再把 Chunk 归还给 Publisher

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## Public API 很薄

固定源码中的 typed Subscriber：

~~~rust
pub fn receive(
    &self,
) -> Result<
    Option<Sample<Service, Payload, UserHeader>>,
    ReceiveError>
{
    Ok(self.receive_impl()?.map(
        |(details, chunk)| Sample {
            subscriber_shared_state:
                self.subscriber_shared_state.clone(),
            details,
            chunk,
            ...
        }
    ))
}
~~~

真正关键的是 receive_impl / Receiver。

## 第一步：从 Connection 收 PointerOffset

Receiver 最终调用：

~~~rust
connection
    .receiver
    .receive(channel_id)
~~~

得到：

~~~text
Option<PointerOffset>
~~~

注意仍然不是 payload copy。

## 第二步：把 Offset 翻译成本地地址

随后：

~~~rust
connection
    .data_segment
    .register_and_translate_offset(offset)
~~~

Subscriber 通过对应 Publisher 的 DataSegmentView，找到自己进程里的本地映射地址。

这一步实现：

~~~text
cross-process descriptor
↓
process-local pointer
~~~

## 第三步：构造 Chunk / Sample View

成功翻译以后：

~~~text
PointerOffset
+ MessageTypeDetails
+ local mapped address
↓
Chunk
↓
Sample
~~~

Sample 对应用表现得像普通借用对象。

但它不是 Subscriber heap 上的新副本。

## Borrow Limit 为什么必要

Receiver 暴露 borrow_count，Service static config 也有 subscriber max borrowed samples。

如果应用：

~~~text
receive sample A
不 drop

receive sample B
不 drop

receive sample C
不 drop
...
~~~

Publisher 就无法回收对应 chunk。

因此 Subscriber 的 borrow limit 是整个共享 pool 的内存保护机制。

## release_offset 做什么

固定源码：

~~~rust
pub(crate) fn release_offset(
    &self,
    chunk: &ChunkDetails,
    channel_id: ChannelId)
{
    ...
    unsafe {
        connection
            .data_segment
            .unregister_offset(chunk.offset)
    };

    connection
        .receiver
        .release(
            chunk.offset,
            channel_id)
}
~~~

它做两件不同的事：

1. 解除 receiver 对 dynamic mapping 的本地注册；
2. 通过 ZeroCopyConnection 把 PointerOffset 归还。

## 为什么 Release 也可能失败

固定错误之一：

~~~text
RetrieveBufferFull
~~~

这暴露了一个非常重要的事实：

> zero-copy 回收本身也需要有界通信资源。

如果“归还 offset”的控制通道失效，chunk 生命周期就不能正确闭环。

## 从 Sample 生命周期看全过程

~~~text
Publisher
loan Chunk X
↓
write
↓
send offset(X)
↓
Subscriber
receive offset(X)
↓
translate
↓
Sample borrows X
↓
application reads
↓
Sample drop
↓
release offset(X)
↓
Publisher reclaim X
↓
Pool reuses X
~~~

这就是零拷贝系统真正需要理解的生命周期。

## 慢 Subscriber 的真实成本

慢 consumer 不一定让 Publisher 多 copy。

它更可能造成：

~~~text
borrowed chunks ↑
available pool chunks ↓
connection buffer occupancy ↑
eventual backpressure / discard
~~~

所以 zero-copy 把性能问题从 memcpy 转换成资源占用与生命周期问题。

## 对应用的一个重要规则

不要无意中长期保存 Sample。

如果业务必须长期保留数据，有三种选择：

- 明确增大资源预算；
- 把需要的部分复制到自己的长期存储；
- 改变 pipeline，让长期 consumer 不持有共享 pool 的实时 chunk。

“零拷贝”不是要求任何地方都绝不复制，而是把 copy 放在真正需要 ownership 分离的位置。
