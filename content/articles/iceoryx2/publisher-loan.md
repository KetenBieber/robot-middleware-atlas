# Publisher Loan：为什么真正的 Zero-copy 必须让应用直接写共享 Chunk

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 普通 Send-copy 仍然需要先得到共享内存

固定源码中的 send_copy 其实先做：

~~~rust
let sample = self.loan_uninit()?;
sample.write_payload(value).send()
~~~

所以 send_copy 只是方便 API。

底层仍然围绕 loaned SampleMut 构造发送路径。

## loan_uninit 做了什么

固定源码：

~~~rust
pub fn loan_uninit(
    &self,
) -> Result<
    SampleMutUninit<
        Service,
        MaybeUninit<Payload>,
        UserHeader>,
    LoanError>
{
    SampleMutUninit::new(
        &self.publisher_shared_state,
        self.loan_chunk(1)?,
    )
}
~~~

这说明应用拿到的 SampleMutUninit 已经关联 PublisherSharedState 和一个来自 data segment 的 chunk。

应用不是先在私有 heap 写好对象再复制。

## 为什么提供 Uninit API

如果 payload 很大：

~~~text
loan default-initialized buffer
→ default constructor / fill zeros
→ 再覆盖成真实数据
~~~

可能产生无意义写入。

loan_uninit 允许：

~~~text
allocate shared chunk
→ 直接在最终位置构造 payload
~~~

对于图像、点云、tensor-like slice 更合理。

## Loan 的数量必须有限

Publisher Builder 提供：

~~~rust
.max_loaned_samples(6)
~~~

这不是任意 API 限制。

每个还没 send/drop 的 loan 都占着共享 chunk。

如果应用无限 loan 不归还：

~~~text
Pool
→ exhausted
→ next loan fails
~~~

所以 max_loaned_samples 本质上是 ownership budget。

## Publisher 的 Sender 保存哪些状态

固定源码中的 Sender 有：

~~~text
segment_states
data_segment
connections
receiver_max_buffer_size
receiver_max_borrowed_chunks
sender_max_borrowed_chunks
number_of_chunks
max_number_of_segments
loan_counter
backpressure_strategy
message_type_details
~~~

这说明 Publisher 不只是“拿一块 shm 写”。

它必须同时管理：

- 本地 allocation；
- 远端 connection；
- loan 数量；
- receiver borrow 上限；
- backpressure；
- dynamic segment 状态。

## SampleMut::send 才发生 Ownership 转移

固定源码：

~~~rust
pub fn send(self) -> Result<usize, SendError> {
    self.shared_state.call(
        |publisher_shared_state|
            publisher_shared_state
                .send_sample(&self.chunk)
    )
}
~~~

注意参数是 self，而不是 &self。

从 API 语义上，send 消耗 SampleMut。

这表达：

~~~text
发送前：
application 暂时拥有可写 sample

send 以后：
application 不应继续修改
runtime 开始管理 delivery/reclaim
~~~

Rust ownership 在这里直接成为 IPC ownership protocol 的第一道约束。

## 为什么 Send 后不能继续写

一个 Subscriber 可能已经在另一进程读同一 shared chunk。

如果 Publisher 仍可修改：

~~~text
Subscriber read
↔
Publisher write
~~~

就形成跨进程 data race。

所以 zero-copy 的前提是：

> payload 不复制以后，对写权限的约束必须更严格。

## 动态 Slice 为什么还要 AllocationStrategy

对于 [T] slice，Publisher Builder 可以配置 initial_max_slice_len 与 allocation_strategy。

如果用户突然 loan 更大的 slice，runtime 需要决定：

- static capacity 不够就失败；
- dynamic segment 扩展；
- 如何让 receiver 映射新 segment。

这也是 flexibility 与 predictability 的直接权衡。

## Loan 对机器人数据意味着什么

理想点云 pipeline：

~~~text
shared chunk loan
↓
sensor/decoder 直接填
↓
publish offset
↓
consumer 直接读
~~~

而不是：

~~~text
sensor private buffer
↓
copy to middleware
↓
copy to subscriber
~~~

这使 iceoryx2 的优化目标非常明确：让大 payload 从产生开始就位于最终共享存储，而不是在多个私有缓冲区之间搬运。
