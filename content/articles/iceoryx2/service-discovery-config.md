# Service Discovery 与 Static/Dynamic Config：端点怎样在没有中心 Broker 的情况下相遇

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## Service 不是每次连接都从零协商

应用通常先：

~~~rust
let service = node
    .service_builder(&service_name)
    .publish_subscribe::<Payload>()
    .open_or_create()?;
~~~

这一步不是简单“创建一个 topic”。

它建立或打开一份共享 Service contract。

## StaticConfig 保存什么

PublishSubscribe 的 PortFactory 可以直接访问 static_config。

固定源码示例展示的字段包括：

~~~text
service name
service id
message type details
max publishers
max subscribers
subscriber max buffer size
history size
subscriber max borrowed samples
safe overflow
~~~

这些值决定 endpoint 是否能在同一 Service 中共存。

所以它们不能在每个 Publisher 建立以后随便改变。

## DynamicConfig 保存什么

同一 PortFactory 还暴露 dynamic_config。

其中运行时会变化的典型信息是：

~~~text
number of active publishers
number of active subscribers
endpoint details
ownership/resource state
~~~

因此：

~~~text
StaticConfig
= compatibility contract

DynamicConfig
= runtime membership/state
~~~

## 为什么 Service Name 要先 Hash

Service trait 规定 ServiceNameHasher。

名字可以是用户友好的语义标识，而 runtime 需要稳定的资源命名。

典型过程：

~~~text
ServiceName
↓
MessagingPattern
↓
ServiceHash
↓
static/dynamic storage names
↓
shared system resources
~~~

这让 filesystem/shared-memory 等底层资源不直接依赖任意长度的用户字符串。

## Open、Create 与 Open-or-Create 是不同语义

Create：

~~~text
我要求当前不存在
并由我创建 contract
~~~

Open：

~~~text
我要求已经存在
并且配置兼容
~~~

Open-or-create：

~~~text
存在则验证兼容后加入
不存在则建立
~~~

如果两边 payload type、容量或 messaging pattern 不兼容，不能仅因为名字相同就强行连接。

## 为什么需要 Persistent Dynamic State

普通进程局部容器会随着 crash 一起消失。

但 shared-memory runtime 需要在异常死亡后回答：

~~~text
这个 publisher slot 原来是谁的？
这个 node 是否还活着？
这个 connection 是否已经成为 stale？
~~~

因此部分动态 ownership state 必须足够持久，才能让另一个活进程恢复系统。

## 发现与数据面必须分开

Service discovery 只回答：

~~~text
哪些 endpoint 存在？
它们兼容吗？
如何建立 connection？
~~~

真正的大 payload 仍在 Publisher DataSegment。

所以可以画成：

~~~text
control plane
Static/Dynamic Storage
        │
        └─ endpoint details
             ↓
        build connection

data plane
SharedMemory Chunk
        │
        └─ PointerOffset
             ↓
        ZeroCopyConnection
~~~

## 为什么这比中心 Broker 更适合本机 IPC

本机高性能 IPC 的目标往往是：

~~~text
稳定状态下
不需要每条数据经过中心进程
~~~

Service metadata 可以帮助 endpoint 相遇，匹配成功后数据直接在 Publisher 与 Subscriber 的 connection 之间走。

因此控制面可以相对复杂，而数据面保持很短。
