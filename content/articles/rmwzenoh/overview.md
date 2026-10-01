# rmw_zenoh Runtime 总览：ROS 2 的 RMW 语义怎样落到 Zenoh

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。目标不是介绍如何设置 `RMW_IMPLEMENTATION`，而是回答一个更底层的问题：**当 ROS 2 上层仍然要求 Node、Publisher、Subscription、Service、Client、Graph、QoS 和 WaitSet 这些语义时，一个并不以 DDS Entity 为核心的 Zenoh Runtime 要怎样把这些语义重新实现出来？**

## 1. 它不是“把 DDS 换成 Zenoh socket”

ROS 2 上层看到的是 RMW contract：

~~~text
rmw_create_node
rmw_create_publisher
rmw_publish
rmw_create_subscription
rmw_take
rmw_create_service
rmw_send_request
rmw_wait
...
~~~

Cyclone DDS / Fast DDS 后端通常把这些接口映射到 Participant、DataWriter、DataReader、DDS History 与 RTPS。

rmw_zenoh 走的是另一条路：

~~~text
rclcpp / rcl
    |
    v
RMW C API
    |
    v
rmw_zenoh_cpp
    |
    +-- Context / NodeData
    +-- PublisherData / SubscriptionData
    +-- ClientData / ServiceData
    +-- GraphCache
    +-- WaitSet glue
    |
    v
Zenoh Session
    |
    +-- Publisher / Subscriber
    +-- Query / Queryable
    +-- Liveliness
    +-- Router / peer connections
~~~

这意味着它的核心工作不是 transport wrapping，而是 **semantic adaptation**。

## 2. Context 为什么映射成一个共享 Session

源码 `rmw_context_impl_s::Data` 在 Context 初始化阶段创建一个共享 `zenoh::Session`。

同一个 ROS Context 内的多个 Node、Publisher、Subscription、Service、Client 都复用它。

这样做减少了 session/transport 数量，也建立了明确的 ownership tree：

~~~text
rmw_context_t
    |
rmw_context_impl_s::Data
    |
    +-- shared zenoh::Session
    +-- GraphCache
    +-- graph GuardCondition
    +-- NodeData map
    +-- SHM / Buffer backend context
~~~

所以 Session 不是某个 Publisher 私有的“连接”，而是整个 Context 的共享 Runtime 根。

## 3. ROS Node 在 Zenoh 中没有天然同构物

Zenoh 原生抽象里没有 ROS Node 这种 graph entity。

因此 `rmw_create_node` 不会创建类似 DDS DomainParticipant 的一整套 endpoint namespace，而是建立 `NodeData` 和 graph/liveliness identity。

真正承载数据的对象仍然是 Publisher、Subscriber、Queryable 与 Query。

这说明：

> ROS Node 在 rmw_zenoh 中主要是 **lifecycle + graph ownership**，不是 transport endpoint。

## 4. Topic 数据面：key expression + payload + attachment

Publisher 创建时会根据 domain、topic、type name、type hash 生成 topic key expression。

设计文档给出的逻辑结构是：

~~~text
<domain_id>/<fully_qualified_name>/<type_name>/<type_hash>
~~~

发布时：

~~~text
ROS message
   |
CDR serialization
   |
Zenoh payload
   +
attachment(sequence, source timestamp, GID)
   |
Publisher::put
~~~

Zenoh 自身只要求“key + bytes”，而 ROS 2 还要求 message info、source timestamp、publisher identity 等语义，所以这些被放进 attachment。

## 5. Subscription 为什么不能直接调用 ROS callback

Zenoh Subscriber callback 在 Zenoh Runtime 的接收上下文中执行。

如果它直接调用用户 ROS callback：

~~~text
network receive task
   |
user callback 40 ms
   |
Zenoh receive path stalls
~~~

这会破坏 ROS Executor 模型。

rmw_zenoh 因此把收到的数据变成拥有型 Message，放入 `SubscriptionData` 的本地队列，再通知 WaitSet：

~~~text
Zenoh callback
   |
SubscriptionData::add_new_message
   |
message_queue_
   |
notify rmw_wait
   |
Executor wakes
   |
rmw_take
   |
user callback
~~~

这一步把 **transport execution** 与 **ROS execution** 分开。

## 6. Graph 为什么需要额外重建

Zenoh 的发现目标是建立通信关系，并不天然要求每个进程都维护完整 ROS Graph。

ROS 2 却需要：

- node list；
- topic endpoint info；
- service/client introspection；
- matched endpoint count；
- QoS compatibility events。

rmw_zenoh 因此通过 Zenoh liveliness token 编码 ROS graph entity，并在每个 Context 中维护 `GraphCache`。

~~~text
Zenoh discovery/liveliness
        |
        v
encoded ROS entity metadata
        |
        v
GraphCache
        |
        v
ROS graph API
~~~

这是 rmw_zenoh 最重要的阻抗匹配之一。

## 7. Service 为什么映射成 Query/Queryable

Zenoh 原生 Query 模型非常适合 request/reply：

~~~text
ROS Client
  -> Zenoh Query
  -> Queryable
  -> ROS Service
  -> Query::reply
  -> Client
~~~

但 ROS 2 还要求 request_id、sequence number、WaitSet readiness 和 delayed response correlation。

因此 ClientData / ServiceData 在 Zenoh Query 基础上增加本地状态表和队列。

## 8. Router 的角色不是中心 broker

默认设计要求本机运行 Zenoh Router，用于 discovery 与跨主机连接协调。

但数据面并不意味着所有本机消息都必须经过 Router：

~~~text
Session A ---- peer/data ---- Session B
      \                    /
       \---- discovery ----/
              Router
~~~

所以它与 ROS1 Master 也不同：Router 既参与 Zenoh 网络拓扑，又不等价于 ROS graph server。

## 9. 这套 Runtime 真正值得学什么

rmw_zenoh 的价值不在于“ROS 2 能跑在 Zenoh 上”。

更值得迁移的是四个设计问题：

1. **语义适配**：上层 contract 与底层 primitive 不同怎么办；
2. **执行隔离**：transport callback 如何转换成 Executor readiness；
3. **graph 重建**：底层只提供 liveliness 时怎样恢复完整 graph；
4. **生命周期**：共享 Session、entity token、queue、callback、waiter 怎样安全关闭。

后续文章会沿这四条线进入源码。
