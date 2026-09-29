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
