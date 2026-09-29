# Discovery Server：为什么 Fast DDS 要把“所有人互相发现”改成客户端—服务器拓扑

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## Simple Discovery 的规模问题

标准 Simple Discovery 适合节点不多的局域网：

~~~text
Participant A
↔ B
↔ C
↔ D
~~~

但 N 个 Participant 全互相公告时，连接关系与 discovery traffic 会快速增长。

机器人群、多进程 perception stack 或大规模仿真里，真正的问题往往不是业务数据，而是 discovery storm。

## Fast DDS 把 PDP 抽象成可替换策略

固定源码里：

~~~cpp
class PDPClient : public PDP
{
    ...
};

class PDPServer :
    public fastdds::rtps::PDP
{
    ...
};
~~~

也就是说 Discovery Server 不是在标准 discovery 外面再套一个代理程序，而是直接进入 Participant Discovery Protocol 的实现层。

## Server 解决什么

拓扑变成：

~~~text
Client A ─┐
Client B ─┼─> Discovery Server
Client C ─┤
Client D ─┘
~~~

Server 集中维护 Participant / Endpoint discovery database，并把相关 discovery 信息转发给需要的客户端。

这样客户端不必和所有 participant 建立直接 discovery 会话。

## 为什么还有 PDPClient 和 PDPServerListener

客户端需要：

- 知道 server locator；
- 向 server 宣布自己；
- 检测 server 状态；
- 接收 server 返回的 Participant 信息。

服务器端则要：

- 消费 client ParticipantProxyData；
- 写入 discovery database；
- 判断哪些 client 需要知道某条更新；
- 处理 dispose / lease / reconnect。

所以它们仍然复用 Reader/Writer/History/TimedEvent，而不是一个裸 TCP directory service。

## Endpoint Discovery 也必须跟着改变

仅把 PDP 中心化还不够。

Participant 被 server 发现以后，Publication/Subscription discovery 也需要由 server 路由，否则业务 endpoint 仍然会全互联广播。

因此 Fast DDS 的 Discovery Server 体系同时有 server-side EDP 路径。

## Server 并不进入业务数据面

这是最容易混淆的一点。

Discovery Server 主要处理：

~~~text
who exists?
what endpoints exist?
how should they match?
~~~

业务 DATA 在 endpoint 匹配后仍可：

~~~text
Publisher ----------------> Subscriber
          direct transport
~~~

所以 Discovery Server 不天然变成业务消息 broker。

它改变的是控制面拓扑，而不是强制改变 user-data data plane。

## 这和 ROS 2 Discovery Server 的关系

ROS 2 使用 rmw_fastrtps 时，可以通过 Fast DDS discovery server 配置改变底层 DDS discovery。

对大规模机器人系统而言，这通常比单纯调 ROS_DOMAIN_ID 更有结构意义：

~~~text
ROS_DOMAIN_ID
主要划分 discovery domain

Discovery Server
改变同一 domain 内 discovery topology
~~~

## 失效模式要主动设计

中心化控制面带来新的问题：

- server 崩溃；
- client 与 server 网络分区；
- server restart；
- 多 server 冗余；
- stale discovery state。

因此真正部署时要把：

~~~text
Discovery Server availability
≠ user-data path availability
~~~

分开监控。

已有 endpoint 之间的数据通路是否继续工作，取决于具体 lease、locator 与实现状态，不应该用“server 掉了所以所有数据一定立刻断”这种模糊结论代替测试。
