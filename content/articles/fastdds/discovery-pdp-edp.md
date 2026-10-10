# PDP 与 EDP：Fast DDS 怎样把 Participant 与 Endpoint Discovery 分成两套 Builtin Protocol

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## PDP 是 Participant Discovery Protocol

固定源码中的基类定义得非常明确：

~~~cpp
class PDP :
    public fastdds::statistics::rtps::IProxyQueryable
{
    ...
};
~~~

注释说明它保存 Participant Discovery Data，并提供所有 Participant Discovery 实现共享的接口。

具体实现至少包括：

~~~text
PDPSimple
PDPClient
PDPServer
~~~

这已经比“Fast DDS 支持 discovery”更有信息量：Participant discovery 本身就是可替换策略。

## PDPSimple 对应标准 Simple RTPS Discovery

PDPSimple 继承 PDP：

~~~cpp
class PDPSimple : public PDP
{
    ...
};
~~~

它负责创建 builtin participant discovery Reader/Writer，周期发布本机 ParticipantProxyData，并消费远端 participant data。

发现结果进入本地 discovery database，随后 endpoint discovery 才有 owner participant 上下文。

## 为什么 Endpoint Discovery 要另一个 EDP

Participant 被发现后，系统只知道：

~~~text
GUID prefix
lease duration
default locator
builtin endpoint set
vendor/capability
~~~

还不知道业务 Writer/Reader。

EDP 才负责 Publication / Subscription：

~~~text
WriterProxyData
ReaderProxyData
~~~

Fast DDS 的 EDPSimple 定义：

~~~cpp
class EDPSimple : public EDP
{
    using t_p_StatefulWriter =
        EDPUtils::WriterHistoryPair;

    using t_p_StatefulReader =
        std::pair<StatefulReader*,
                  ReaderHistory*>;
    ...
};
~~~

这里很值得注意：Discovery 自己也复用了 StatefulWriter / StatefulReader 与 History。

## Builtin Discovery 为什么也要 History

SEDP/EDP 消息同样可能：

- 可靠传输；
- 重传；
- 生命周期更新；
- dispose endpoint。

所以 discovery 并不是额外写一套 UDP 广播代码，而是复用 RTPS endpoint + History 基础设施。

这也是中间件架构里很好的“机制复用”：协议自身的控制面也走协议自己的数据通路。

## Endpoint Matching 发生在哪里

收到远端 WriterProxyData / ReaderProxyData 后，Discovery DataBase 会和本地 endpoint 比较：

~~~text
Topic
Type
Partition
Reliability
Durability
Ownership
Data Representation
...
~~~

匹配成功后：

~~~text
local StatefulWriter
  + ReaderProxy(remote reader)

local StatefulReader
  + WriterProxy(remote writer)
~~~

因此 ReaderProxy / WriterProxy 是“发现结果进入可靠数据面”的桥。

## Liveliness 又为什么单独有 WLP

Participant 存活和 Writer liveliness 不是同一层。

BuiltinProtocols 中 WLP（Writer Liveliness Protocol）负责 writer liveliness assertion / lease。

所以控制面可以分成：

~~~text
PDP
Participant 是否存在

EDP
Endpoint 是否存在、属性是什么

WLP
Writer 是否仍满足 liveliness contract
~~~

这和 ROS 2 Graph 中“节点存在”“topic endpoint 存在”“publisher 是否活跃”不是完全同一个状态。

## Discovery 对机器人系统的代价

当 Participant 数量变大，Simple Discovery 近似形成多对多 gossip：

~~~text
P1 <-> P2
P1 <-> P3
P2 <-> P3
...
~~~

每个 participant/endpoint 变化都可能扩散。

几十、上百个机器人进程时，Discovery traffic、proxy object 数量、matching CPU 都会增长。

这正是 Fast DDS 另外提供 Discovery Server 的根本原因。

## Discovery 完成后的真实数据面变化

Discovery 不是业务数据传输本身，它最终产生的是：

~~~text
Participant discovery
        |
        v
Endpoint metadata
        |
        v
QoS matching
        |
        v
ReaderProxy / WriterProxy
        |
        v
StatefulWriter / StatefulReader
~~~

因此 discovery 的最终产物不是“知道对方 IP”，而是创建后续可靠数据面的协议状态。

## ROS 2 启动时的一次匹配过程

典型过程：

~~~text
Node A creates Publisher
        |
        v
DomainParticipant announces itself
        |
        v
PDP discovers Participant B
        |
        v
EDP exchanges Writer/Reader data
        |
        v
QoS compatibility check
        |
        v
matched endpoint pair
        |
        v
DATA can flow
~~~

这解释了为什么 ROS 2 中创建 publisher 后并不会立即收到 subscriber 数据：中间存在 discovery 和 matching 阶段。

## Discovery Server 的本质变化

Discovery Server 并不是简单把广播改成单播，而是改变 discovery graph：

Simple Discovery：

~~~text
N participants
    |
    v
many participant-to-participant exchanges
~~~

Discovery Server：

~~~text
Participant
    |
    v
Server
    |
    v
Other participants
~~~

代价从 participant 数量平方级交互转移到服务器维护的 discovery state。
