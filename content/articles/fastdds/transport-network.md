# Transport 与 MessageReceiver：Fast DDS 怎样从 Locator 走到 UDP、TCP 与 SHM

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## RTPS Endpoint 不直接操作 Socket

Fast DDS 把网络层分成：

~~~text
RTPS Writer / Reader
  ↓
Locator
  ↓
NetworkFactory
  ↓
TransportInterface
  ├─ UDPv4 / UDPv6
  ├─ TCPv4 / TCPv6
  ├─ SharedMemTransport
  └─ custom transport
~~~

这让 RTPS reliability 与具体 I/O 实现解耦。

## Participant 创建 ReceiverResources

RTPSParticipantImpl 初始化时会：

~~~cpp
createReceiverResources(
    m_att.builtin
      .metatrafficUnicastLocatorList,
    true,
    false,
    true);

createReceiverResources(
    m_att.builtin
      .metatrafficMulticastLocatorList,
    false,
    false,
    true);
~~~

随后对默认 user traffic locator 也创建接收资源。

所以 discovery traffic 与 user traffic 都有明确 receive resources。

## 为什么 MessageReceiver 和 Transport Receiver 分开

Transport 只负责：

~~~text
从某 locator 收到 bytes
~~~

MessageReceiver 负责：

~~~text
验证 RTPS header
解析 submessage
根据 readerId / writerId 路由
调用 StatefulReader / StatefulWriter
~~~

这样 UDP/TCP/SHM 都可以复用同一套 RTPS parser。

## Endpoint 如何挂到 ReceiverResource

启用 Participant 后：

~~~cpp
for (auto& receiver :
     m_receiverResourcelist)
{
    receiver.Receiver
      ->RegisterReceiver(
          receiver.mp_receiver);
}
~~~

Endpoint 创建/删除时还会 associate/remove 到 MessageReceiver。

因此 receive thread 并不是“遍历全局所有 Reader”。它已经预先拥有针对 locator 的 receiver 与 endpoint association。

## UDPv4 的 Socket 参数来自 Descriptor

UDPv4Transport 构造时保存：

~~~cpp
mSendBufferSize =
    descriptor.sendBufferSize;

mReceiveBufferSize =
    descriptor.receiveBufferSize;
~~~

公共 helper 会保证 socket buffer 至少能容纳 maxMessageSize。

所以：

~~~text
DDS fragment size
Fast DDS maxMessageSize
OS SO_SNDBUF / SO_RCVBUF
NIC MTU
~~~

是四个不同层级的参数。

## Transport Send 并不等于 DDS Write 结束条件

同步 publish 下，写调用可能一直走到 transport send；异步 publish 下，Writer 只把 CacheChange 排进 FlowController，真正 transport send 在异步线程。

因此要先判断 publish mode，再讨论 DataWriter::write 的尾延迟。

## SHM Transport 是“网络层替换”

SharedMemTransport 仍位于 TransportInterface 层。

概念上：

~~~text
RTPS message
→ SHM transport port
→ shared-memory segment
→ remote receiver
→ MessageReceiver
→ RTPS parse
~~~

它省掉 kernel UDP/TCP path，但仍保留 RTPS message framing 与 transport 语义。

这和 Data Sharing 是两个不同层级：SHM Transport 仍搬运 RTPS message，而 Data Sharing 直接改变同机 endpoint 间 payload/history 的交付方式。

## SendBuffersManager 为什么在 Participant 级别

RTPSParticipantImpl 根据 user/event buffer、receive thread 数量和 growing policy 创建 SendBuffersManager。

这意味着多个 endpoint 可以共享 participant 级发送 buffer 资源，而不需要每个 Writer 永久保有独立大 buffer。

## Shutdown 为什么先停 NetworkFactory

Participant 关闭路径会先：

~~~cpp
m_network_Factory.Shutdown();
~~~

然后注销 receiver、disable threads，再销毁 MessageReceiver。

这体现一个很重要的生命周期规则：

> 先阻止 Transport 产生新 callback，再释放 callback 会访问的 Reader/Writer 对象。

和所有高并发中间件一样，关闭顺序本身就是并发正确性。

## Locator 是协议层与 I/O 层之间的地址合同

RTPS endpoint 处理的是 Locator，而不是裸 socket fd。Locator 描述 transport kind、
address、port 等寻址信息；NetworkFactory 再决定哪个 TransportInterface 能处理它。

~~~text
ReaderProxy / WriterProxy locator
        ↓
NetworkFactory
        ↓
matching TransportInterface
        ↓
send / create receiver resource
~~~

这样可靠性状态机不需要为 UDP、TCP、SHM 各写一套 ReaderProxy。

## ReceiverResource 为什么是长期对象

接收端不能每来一个 packet 就临时创建 socket、parser 和 endpoint lookup。Participant
初始化时建立 ReceiverResource，并把 MessageReceiver 注册进去，运行期由 transport
持续把字节交给同一个协议解析入口。

~~~text
transport receive loop
  ↓
ReceiverResource
  ↓
MessageReceiver::processCDRMsg
  ↓
submessage dispatch
~~~

这也是 receiver thread 与 application thread 分离的根源。

## 一个 UDP datagram 不是一个 DDS sample

RTPS datagram 里可以包含多个 submessage，一个大 sample 又可以被拆成多个 DATAFRAG。
因此下面两个等式都不成立：

~~~text
1 UDP packet == 1 DDS sample
1 write()     == 1 send()
~~~

网络抓包时必须按 RTPS sequence/fragment 还原语义，不能只数 UDP 包。

## Fragment size、MTU 与 socket buffer 是三个层级

大消息路径可能依次受到：

~~~text
DDS serialized payload
  ↓ RTPS fragmentation
RTPS message size
  ↓ transport
UDP/TCP/SHM frame
  ↓ OS/NIC
MTU + socket buffers
~~~

调大 SO_SNDBUF 不能消除 RTPS fragment；调小 fragment 也不会自动改变 History 中
CacheChange 的生命周期。每个参数要针对对应层级调。

## 同步和异步 send 的线程归属

同步 publish 下，应用 write 路径可能继续进入 transport send；异步 publish 下，
CacheChange 先进入 FlowController，由异步 sender 线程择机发送。

这带来两种不同的故障表象：

~~~text
sync overload  → write latency 上升
async overload → write 看似正常，但 queue/data age 上升
~~~

所以异步模式的监控不能只看 API 返回时间。

## Transport shutdown 的真正目标是 quiescence

先调用 NetworkFactory::Shutdown，再 unregister/disable receiver，目的不是“按顺序好看”，
而是建立一个时间点：从此以后不会再有 transport thread 产生新的 MessageReceiver
callback。只有这个条件成立，后续释放 Reader/Writer target 才安全。

这个模式可以迁移到任何机器人网络 runtime：

~~~text
stop ingress
→ join/disable I/O execution
→ detach dispatch targets
→ free state
~~~
