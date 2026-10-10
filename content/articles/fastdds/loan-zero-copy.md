# loan_sample：真正的 Zero-copy 需要什么前提

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## 普通 write 为什么至少有一次业务数据搬运

非 loan 路径：

~~~text
application object
→ calculate serialized size
→ payload pool
→ serialize into payload
→ CacheChange
~~~

即使后面使用 SHM，应用对象到 middleware payload 这一段仍然存在。

Loan 的目标是从一开始就让应用写到 middleware 能接管的存储。

## loan_sample 有严格前提

DataWriterImpl::loan_sample() 固定源码先检查：

~~~cpp
if (!type_->is_plain_ctx(
        type_support_context_,
        data_representation_) ||
    SerializedPayload_t::
      representation_header_size >
    type_->get_max_serialized_size_ctx(
        type_support_context_))
{
    return RETCODE_ILLEGAL_OPERATION;
}
~~~

这说明并不是所有 DDS/ROS 类型都能 loan。

类型布局必须足够可预测，middleware 才能把 payload storage 安全映射成用户对象。

## Writer 还必须已经 Enabled

~~~cpp
if (nullptr == writer_)
{
    return RETCODE_NOT_ENABLED;
}
~~~

Loan 依赖 Writer 的 payload pool 与 runtime state，不是独立 allocator API。

## write 时如何识别 Loan

perform_create_new_change()：

~~~cpp
SerializedPayload_t payload;

bool was_loaned =
    check_and_remove_loan(
        data,
        payload);

if (!was_loaned)
{
    ...
    serialize(...)
}
~~~

这就是关键 fast path：

~~~text
loaned sample
→ reclaim payload ownership
→ skip normal allocation /
  serialization path
~~~

普通 sample 才需要重新申请 payload 并 serialize。

## 为什么失败时 Loan 要还给应用

History add 失败时：

~~~cpp
if (was_loaned)
{
    payload =
      std::move(
        ch->serializedPayload);

    add_loan(
      data,
      payload);
}
~~~

也就是说 write 没 commit 成功，middleware 不能偷偷吃掉 application 的 loan。

这是 ownership transaction：

~~~text
application owns loan
↓ write begin
temporarily transfer
↓
commit success
middleware owns

or

commit failure
return ownership
~~~

## 官方真实示例怎么用

delivery_mechanisms：

~~~cpp
void* sample = nullptr;

if (RETCODE_OK ==
    writer_->loan_sample(sample))
{
    auto* msg =
      static_cast<
        DeliveryMechanisms*>(sample);

    msg->index() = ++index;

    ret =
      RETCODE_OK ==
      writer_->write(sample);
}
~~~

应用没有创建临时 message 再 memcpy 给 Writer，而是直接填 Writer loan 出来的对象。

## Loan + Data Sharing 才可能接近端到端零拷贝

理想同机路径：

~~~text
Writer pool loan
→ application fill
→ CacheChange references shared payload
→ Data Sharing shared history
→ Reader loan/view
→ application read
~~~

但只要出现类型不 plain、跨主机 Reader、安全加密、representation 转换、应用主动复制或 RMW 不暴露 loan，就可能重新引入 serialization/copy。

## ROS 2 为什么不能看到 Fast DDS 支持 Loan 就宣布 Zero-copy

还要经过：

~~~text
ROS message type
→ rmw_fastrtps type support
→ rmw loan capability
→ rcl/rclcpp API
→ Fast DDS DataWriter loan
→ Data Sharing topology
~~~

任何一层不支持，用户看到的 publish() 都可能回到普通 copy/serialization 路径。

## Loan 是一个所有权状态机

把 loan_sample 当作“返回一块指针”会忽略最重要的部分：这块内存从借出到 write 成功
之间有明确 owner 变化。

~~~text
Writer pool owns free slot
        ↓ loan_sample
application owns writable loan
        ↓ write
temporary transfer
   ┌────┴─────┐
commit ok   commit fail
   ↓            ↓
History owns   ownership returned
payload        to application loan
~~~

这也是 check_and_remove_loan 与失败后 add_loan 必须成对存在的原因。

## 为什么 plain type 条件这么重要

如果类型含有无法直接映射的动态结构，应用看到的 C++ 对象布局就不等于 wire/shared
payload 布局。middleware 只能重新序列化，而不能安全地把一块协议存储直接解释成
用户对象。

因此 loan 的收益来自“布局可预测”，而不是 C++ API 技巧。

## Writer loan 只解决发送端第一段 copy

即使 Writer 侧跳过普通序列化路径，后续仍可能因为：

- 远端网络 Reader；
- transport framing；
- 安全变换；
- Reader API 复制；
- 上层 RMW 类型适配；

重新产生数据搬运。端到端 zero-copy 必须同时审查 Reader 侧消费方式。

## Loan 与 History 容量绑定

借出的 sample 来自 Writer 可管理的 pool。应用长期持有大量 loan 而不 write/return，
本质上是在占用 middleware 的有限资源；History 又需要 slot 保存已提交 Change。

所以 loan API 不等于无限制 allocator，错误使用会把内存压力从 memcpy 变成 pool
exhaustion。

## 混合本地/远端 Reader 时不要过度承诺

理想本地链路：

~~~text
loan_sample
→ application fill
→ Data Sharing
→ Reader view/loan
~~~

但只要同一个 Writer 还要服务远端 Reader，就必须保留跨主机传输所需的协议路径。
能否避免额外 copy 要看具体 payload pool、representation 和发送策略，而不是只看
Writer API 是否调用了 loan_sample。

## 什么时候值得使用

Loan 对大而固定布局的图像、点云、张量消息最有潜力；对于几十字节控制消息，省掉
一次小 copy 往往不如简化生命周期更重要。优化前应先测：

~~~text
serialization time
copy bytes
pool contention
History occupancy
loan failure rate
end-to-end latency
~~~

否则可能为了“零拷贝”引入更复杂的错误恢复与资源泄漏风险，却没有显著收益。
