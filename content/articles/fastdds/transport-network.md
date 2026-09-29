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
