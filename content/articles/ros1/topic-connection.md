# ROS1 Topic 建链：从 publisherUpdate 到一条真正的 TCPROS 连接

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。

Master 告诉 Subscriber “这个 topic 有一个 Publisher”以后，消息仍然不会自动流动。此时 Subscriber 拿到的是对方的 XML-RPC URI，而不是一条已经可用的 socket。接下来要完成的是**从 graph identity 切换到 transport endpoint**。

## 1. 为什么 Master 不直接保存 TCP host/port

最直觉的实现是：

~~~text
/topic -> TCP host:port
~~~

但 publisher 可能支持多种 transport，Subscriber 也可能有不同偏好。把所有协议细节放进 Master，会让 discovery service 对 TCPROS、UDPROS 乃至未来 transport 都产生强依赖。

ROS1 因此拆成：

~~~text
Master:
  topic -> publisher XML-RPC URI

Publisher XML-RPC:
  requestTopic(protocol offers)
      -> selected transport parameters
~~~

Master 只解决“找谁”；真正的 Publisher 解决“怎么连我”。

## 2. publisherUpdate 为什么传完整集合

Master 的 `publisherUpdate` 给 Subscriber 一份完整 publisher URI set。Subscriber 对比本地 `publisher_links_`：

~~~text
current links   = {A, B}
new target set  = {B, C}

subtractions    = {A}
additions       = {C}
~~~

A 被 drop，B 保留，C 才进入新连接 negotiation。

如果更新只发送“新增 C”这类边沿事件，一旦某次 RPC 丢失，Subscriber 更难恢复到真实集合。完整目标集合更适合做 reconciliation。

## 3. 新 URI 进入 negotiateConnection

`Subscription::negotiateConnection` 为 TCPROS 构造协议 offer：

~~~cpp
XmlRpcValue tcpros_array, protos_array, params;

tcpros_array[0] = std::string("TCPROS");
protos_array[0] = tcpros_array;

params[0] = this_node::getName();
params[1] = name_;
params[2] = protos_array;
~~~

逻辑上是：

~~~text
caller_id
topic
[
  ["TCPROS"]
]
~~~

随后解析 Publisher XML-RPC URI，创建 `XmlRpcClient` 并发起非阻塞 `requestTopic`：

~~~cpp
if (!c->executeNonBlock("requestTopic", params))
{
  // control-plane negotiation failed
}
~~~

为什么不用同步 RPC？因为某个 Publisher 的 XML-RPC 响应慢，不应该长时间占住本地订阅管理路径。

## 4. Publisher 的 requestTopic 只负责选择数据协议

固定源码中：

~~~cpp
bool TopicManager::requestTopic(
    const string& topic,
    XmlRpcValue& protos,
    XmlRpcValue& ret)
{
  ...
  if (proto_name == string("TCPROS"))
  {
    XmlRpcValue tcpros_params;
    tcpros_params[0] = string("TCPROS");
    tcpros_params[1] = network::getHost();
    tcpros_params[2] =
        int(connection_manager_->getTCPPort());

    ret[0] = int(1);
    ret[1] = string();
    ret[2] = tcpros_params;
    return true;
  }
  ...
}
~~~

返回值：

~~~text
["TCPROS", publisher_host, publisher_tcp_port]
~~~

注意：这仍然没有业务 payload，只是 transport capability negotiation 的结果。

## 5. Subscriber 是 TCPROS 的主动连接方

收到 host/port 后，Subscriber 创建 `TransportTCP` 并主动连接 Publisher 的 server socket。随后创建：

~~~text
Subscription
    |
    +-- PublisherLink
            |
            +-- Connection
                    |
                    +-- TransportTCP
                            |
                            +-- OS socket
~~~

这些对象解决不同层的问题：

- `Subscription`：逻辑 topic 订阅；
- `PublisherLink`：这个 topic 的某一个远端 Publisher；
- `Connection`：一次 framed async byte stream；
- `TransportTCP`：socket/readiness/syscall。

如果把这四层压成一个类，那么 graph 变化、TCP 重连、消息 framing、订阅 fan-in 全部会耦合在同一个状态机里。

## 6. TCP connect 成功为什么仍然不能收消息

TCP 不知道当前连接究竟服务哪个 ROS topic。Subscriber 先写 connection header：

~~~cpp
M_string header;
header["topic"] = parent->getName();
header["md5sum"] = parent->md5sum();
header["callerid"] = this_node::getName();
header["type"] = parent->datatype();
header["tcp_nodelay"] =
    transport_hints_.getTCPNoDelay() ? "1" : "0";

connection_->writeHeader(
    header,
    boost::bind(
      &TransportPublisherLink::onHeaderWritten,
      this,
      boost::placeholders::_1));
~~~

Publisher 校验 topic、type、md5sum，再回自己的 header。只有双方接受后，这条 TCP fd 才获得“ROS topic connection”的身份。

## 7. 为什么 md5sum 比只比较 type 名更可靠

只比较：

~~~text
sensor_msgs/Image
~~~

并不能证明两边的字段定义一致。某个包可能在不同构建环境里拥有同名但不同 schema。

ROS1 用 message definition 派生的 MD5 作为连接期 compatibility identity。Publisher 在 `Publication::validateHeader` 中检查 header，类型不匹配时在业务 payload 发送前拒绝连接。

因此 ROS1 的类型安全主要发生在**连接建立期**，不是每帧动态解释 schema。

## 8. self-subscription 为什么不能走 TCP

如果 Publisher 和 Subscriber 在同一个 roscpp 进程里，通过本机 TCP 把对象序列化、交给内核，再反序列化回来没有意义。

`Subscription` 会识别本进程 XML-RPC URI，跳过远端 negotiation，建立 intra-process links。

同一个 Publication 因而可能同时拥有：

~~~text
TransportSubscriberLink A -> remote process
TransportSubscriberLink B -> remote process
IntraProcessSubscriberLink -> same process
~~~

上层 Publisher 看到的仍是统一的 SubscriberLink abstraction。

## 9. graph 更新与消息 hot path 为什么必须分开

graph 变化是低频事件：

~~~text
publisher appears
publisher disappears
process restarts
~~~

数据发送可能是 100 Hz、1 kHz，甚至大图像的高带宽流。

因此合理的结构是：

~~~text
control path:
publisherUpdate
  -> set diff
  -> requestTopic
  -> create/drop link

hot path:
message
  -> reuse existing links
  -> enqueue/write
~~~

如果每帧 publish 都重新问 Master“现在有哪些 Subscriber”，控制面延迟和锁就会进入最热路径。

ROS1 把连接建立成本摊销到 graph 变化时。

## 10. 失败应该回到它所属的状态机

ROS1 建链过程中有几类不同失败：

~~~text
Master unavailable
  -> graph/discovery failure

requestTopic timeout
  -> negotiation failure

TCP connect/disconnect
  -> transport/link failure

md5sum mismatch
  -> schema failure

callback too slow
  -> execution/queue overload
~~~

它们不应该由一个统一“大连接管理器”用同一种重试策略处理。

例如 schema mismatch 重试一百次没有意义；TCP 短暂断连可以重试；callback overload 则要改 queue/WCET，而不是重连网络。

## 11. 从发现到数据通道的完整时序

~~~text
Subscriber          Master           Publisher XMLRPC       TCP server
    |                  |                    |                   |
    | registerSub      |                    |                   |
    |----------------->|                    |                   |
    | pub URI list     |                    |                   |
    |<-----------------|                    |                   |
    |                                       |                   |
    | requestTopic(["TCPROS"])              |                   |
    |-------------------------------------->|                   |
    | ["TCPROS", host, port]                |                   |
    |<--------------------------------------|                   |
    |                                                           |
    | TCP connect                                               |
    |---------------------------------------------------------->|
    |                                                           |
    | ROS connection header                                     |
    |---------------------------------------------------------->|
    | publisher header                                          |
    |<----------------------------------------------------------|
    |                                                           |
    |        repeated length + payload stream                    |
~~~

这个时序清楚地说明：Master 只负责第一段 graph discovery；真正的连接身份和数据 protocol 都由节点之间自行完成。

## 12. 这套两阶段设计的可迁移原则

ROS1 的具体 XML-RPC/TCPROS 今天未必是最佳选择，但两阶段设计非常通用：

~~~text
name/service discovery
      |
      v
capability / endpoint negotiation
      |
      v
long-lived data path
~~~

现代 DDS、Zenoh、共享内存 runtime 依旧会把“建立关系”和“搬运 payload”区分开，只是 discovery、QoS matching 和 transport selection 更复杂。
