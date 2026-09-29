# Participant 与 Endpoint 生命周期：为什么 DDS 对象和 RTPS 对象要分两次创建

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## DomainParticipantFactory 是配置入口，不是协议对象

应用调用：

~~~cpp
DomainParticipantFactory::get_instance()
    -> create_participant(...)
~~~

Factory 先处理默认 QoS、XML profile、自动 enable 等 DDS 层配置，真正网络参与者仍由 DomainParticipantImpl 创建。

DomainParticipantImpl::enable() 中会：

~~~cpp
fastdds::rtps::RTPSParticipantAttributes rtps_attr;
utils::set_attributes_from_qos(rtps_attr, qos_);
rtps_attr.participantID = participant_id_;

RTPSParticipant* part =
    RTPSDomain::createParticipant(
        domain_id_,
        false,
        rtps_attr,
        &rtps_listener_);
~~~

这一步把“DDS Participant”编译成“RTPS Participant”。

## 为什么要分两层对象

DDS Participant 关心：

- Publisher / Subscriber ownership；
- Topic 与 TypeSupport；
- DDS QoS；
- Listener / Status；
- API 生命周期。

RTPSParticipantImpl 关心：

- GUID prefix；
- builtin discovery endpoint；
- locator；
- transport；
- receive resource；
- FlowController；
- TimedEvent；
- remote participant proxy。

两类职责变化频率、并发访问方式与生命周期完全不同，分层能避免一个巨型 Participant 类承担所有责任。

## Publisher / Subscriber 是容器对象

PublisherImpl::create_datawriter() 先验证 Topic / TypeSupport / QoS，再创建：

~~~cpp
DataWriterImpl* impl =
    create_datawriter_impl(
        type_support,
        topic,
        qos,
        listener,
        payload_pool);
~~~

最终 DataWriterImpl 还会建立 low-level RTPS writer 与 WriterHistory。

Subscriber 对应：

~~~cpp
DataReaderImpl* impl =
    create_datareader_impl(
        type_support,
        topic,
        qos,
        listener,
        payload_pool);
~~~

因此 API 层对象树和协议层对象图不是一一平铺，而是嵌套 ownership。

## Topic 为什么要 reference

创建 DataWriter/DataReader 后，Fast DDS 会对 TopicImpl 增加 reference。

原因是 endpoint 仍依赖：

- Topic name；
- TypeSupport；
- type object；
- serialization callbacks。

只要 Writer/Reader 活着，Topic 元数据就不能先被销毁。

这和智能指针的思想一致，只是库内部使用自己的 reference/lifecycle 规则。

## DataWriter 析构为什么先删 RTPSWriter

DataWriterImpl 析构路径中，如果 writer_ 仍存在，会：

~~~cpp
RTPSDomain::removeRTPSWriter(writer_);
release_payload_pool();
~~~

顺序很重要。

如果先销毁 payload pool / history，而 RTPS writer 仍可能响应 ACKNACK、heartbeat 或 async flow event，就会形成悬空引用。

正确关闭逻辑必须先停止“还会产生访问的协议对象”，再释放它依赖的数据。

## Endpoint 与 History 共享一把 low-level mutex

WriterHistory 绑定 low-level writer 的 mutex。DataWriterImpl::perform_create_new_change() 也先锁：

~~~cpp
std::unique_lock<RecursiveTimedMutex>
    lock(writer_->getMutex());
~~~

这说明一次写操作、History 插入、可靠性状态变化需要形成一个一致临界区。

RecursiveTimedMutex 的存在也告诉我们：调用栈可能在内部再次进入需要同一 writer lock 的 helper；在 strict realtime 构建下又可以用 max_blocking_time 控制锁等待。

## 为什么 max_blocking_time 是系统边界

在 HAVE_STRICT_REALTIME 路径：

~~~cpp
if (!lock.try_lock_until(max_blocking_time))
{
    return RETCODE_TIMEOUT;
}
~~~

这比“互斥锁一定等到拿到”为实时系统更合理。

但要注意：超时只是把无限等待变成有界失败，并没有保证 deadline 一定满足。锁内部的 serialization、History、Transport 调度仍然要计入 WCET。

## 删除 Participant 为什么一定是树形关闭

Participant 下面可能还有：

~~~text
Publisher
Subscriber
Topic
Writer
Reader
builtin discovery endpoint
transport receiver resources
timed events
~~~

因此 shutdown 必须遵循：

~~~text
停止/删除 child entities
→ 移除 RTPS endpoint
→ 停止 builtin protocols / receive resources
→ 回收 transport/event resources
→ 销毁 participant
~~~

这和 ROS 2 context shutdown 的结构化并发问题本质相同。
