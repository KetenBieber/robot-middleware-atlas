# DataWriter::write 主链：锁、Loan、序列化、CacheChange 与 History 如何进入一次调用

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

这是 Fast DDS 专题里最重要的一篇，因为它直接回答：

> ROS 2 publisher 调用下面，到底执行了多少工作？

## API 入口非常薄

~~~cpp
ReturnCode_t DataWriterImpl::write(
        const void* const data)
{
    if (writer_ == nullptr)
    {
        return RETCODE_NOT_ENABLED;
    }

    return create_new_change(
        ALIVE,
        data);
}
~~~

真正工作进入 perform_create_new_change()。

## 第一件事：先锁 low-level Writer

~~~cpp
auto max_blocking_time =
    steady_clock::now() +
    microseconds(
      TimeConv::Time_t2MicroSecondsInt64(
        qos_.reliability()
            .max_blocking_time));

std::unique_lock<RecursiveTimedMutex>
    lock(writer_->getMutex());
~~~

strict realtime 构建中则使用 try_lock_until(max_blocking_time)。

因此 application write 首先可能因为 Writer 内部竞争而阻塞。

## 第二件事：判断 data 是否来自 loan

~~~cpp
SerializedPayload_t payload;

bool was_loaned =
    check_and_remove_loan(
        data,
        payload);
~~~

如果 data 原本就是 Writer loan 出去的 sample，payload ownership 可以直接取回。

否则进入普通序列化路径。

## 第三件事：普通 Sample 要申请 Payload

~~~cpp
uint32_t payload_size =
    fixed_payload_size_
    ? fixed_payload_size_
    : type_->calculate_serialized_size_ctx(
        type_support_context_,
        data,
        data_representation_);
~~~

随后：

~~~cpp
if (!get_free_payload_from_pool(
        payload_size,
        payload))
{
    return RETCODE_OUT_OF_RESOURCES;
}

if (!type_->serialize_ctx(
        type_support_context_,
        data,
        payload,
        data_representation_))
{
    payload_pool_->release_payload(
        payload);
    return RETCODE_ERROR;
}
~~~

这条链清楚说明：

~~~text
write()
不保证无 allocation
不保证无 serialization
不保证常数时间
~~~

## 第四件事：创建 CacheChange_t

~~~cpp
CacheChange_t* ch =
    history_->create_change(
        change_kind,
        handle);
~~~

成功后：

~~~cpp
ch->serializedPayload =
    std::move(payload);
~~~

从这一刻开始，payload 的生命周期不再属于栈上的临时对象，而属于 History / CacheChange。

## 第五件事：真正 Commit 到 History

~~~cpp
added =
    history_->add_pub_change(
        ch,
        wparams,
        lock,
        max_blocking_time);
~~~

如果 Reader filtering 打开，则走带 commit hook 的版本。

这里“传入 lock”很有设计意味：History 插入需要和 Writer mutex 的释放/等待协议协同，而不是 helper 内部偷偷再拿一个未知锁。

## 为什么 History 插入可能失败

资源可能因为：

- History depth；
- Resource Limits；
- reliable Reader 未确认；
- payload/change pool 耗尽；
- max_blocking_time 超时；

而暂时无法释放旧 change。

所以 write 的 failure mode 不只是 network error。

## Deadline 与 Lifespan 发生在写调用尾部

History commit 成功后：

~~~cpp
history_->set_next_deadline(...);

deadline_timer_->cancel_timer();
deadline_timer_->restart_timer();
~~~

Lifespan 也更新 timer interval 并 restart。

因此 QoS timer 管理也是 application write 的一部分。

## 整条链

~~~text
DataWriter::write
↓
DataWriterImpl::write
↓
writer mutex
↓
loan?
├─ yes -> reuse payload ownership
└─ no  -> calculate size
         -> payload pool
         -> serialize
↓
History::create_change
↓
CacheChange_t
↓
add_pub_change
↓
WriterHistory
↓
notify low-level RTPS Writer
↓
deadline/lifespan timer
~~~

后面才进入 StatefulWriter / FlowController / Transport。

这就是为什么控制环里调用 publish 之前必须测 p99/p999，而不是只看 API 平均耗时。

## CacheChange 不是消息对象，而是 RTPS 状态载体

很多实现分析容易把 CacheChange_t 理解成：

~~~text
CacheChange = serialized message
~~~

但在 Fast DDS 中它承担的是更高层职责：

~~~text
CacheChange_t
    |
    +-- sequenceNumber
    +-- writerGUID
    +-- kind(ALIVE / NOT_ALIVE)
    +-- serializedPayload
    +-- fragment information
    +-- instance metadata
~~~

原因是 RTPS 可靠传输不是简单发送一次 payload，而需要回答：

- 这个样本属于哪个 Writer？
- 它在 Writer 序列中的编号是多少？
- 哪些 Reader 已经确认？
- 哪些 fragment 需要重新发送？
- 什么时候可以从 History 回收？

因此 History 保存的不是消息缓存，而是协议状态机仍需要的数据。

## WriterHistory 是可靠性的物理基础

可靠模式下：

~~~text
application write
      |
      v
CacheChange
      |
      v
WriterHistory
      |
      v
send
      |
      v
wait ACKNACK
      |
      v
remove change
~~~

如果 WriterHistory 过浅：

~~~text
old change removed
      |
Reader sends NACK
      |
Writer cannot repair
~~~

因此 history depth 不只是内存参数，它直接决定可靠协议的恢复能力。

## 对机器人控制的意义

对于 1 kHz 控制状态：

~~~text
KEEP_LAST + small depth
~~~

通常更关注最新状态。

对于地图、任务状态：

~~~text
larger history
+
reliable
~~~

因为旧数据仍可能具有语义价值。

所以 DataWriter::write 的延迟、History 策略和控制任务的数据新鲜度必须一起设计。
