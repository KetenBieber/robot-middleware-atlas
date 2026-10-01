# Liveliness 与 GraphCache：Zenoh 如何重建 ROS 2 Graph

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇研究控制面：为什么 Zenoh 本身能够完成 endpoint discovery，rmw_zenoh 仍然需要独立的 ROS GraphCache。

## 1. ROS Graph 需要比“能不能通信”更多的信息

ROS 2 graph API 需要回答：

- 有哪些 node；
- node 属于哪个 namespace；
- 谁 publish/subscribe 某 topic；
- service/client 在哪里；
- endpoint type/QoS 是什么；
- matched endpoint 数量是多少。

Zenoh 的通信发现并不天然维护同样的 ROS 元数据结构。

所以 rmw_zenoh 必须额外建立 ROS-specific control plane。

## 2. Liveliness token 就是 graph advertisement

每个 Node、Publisher、Subscription、Service、Client 都会声明一个 liveliness token。

~~~text
NN = Node
MP = Message Publisher
MS = Message Subscription
SS = Service Server
SC = Service Client
~~~

token key 中编码 domain、session id、node id、entity id、entity kind、namespace、node name、topic/service name、type、type hash 与 QoS。

因此 token 不承载业务 payload，而是一个结构化 graph advertisement。

## 3. 为什么 key 中必须有 type hash

Zenoh 只看 key expression 是否匹配。

但 ROS 2 要求同名 topic 若 type definition 不一致，不能被当作兼容 endpoint。

所以 data key 与 liveliness key 都编码 type name/type hash。

这相当于把 DDS 中“type identity 参与 endpoint matching”的语义显式搬进 Zenoh key space。

## 4. ROS_DOMAIN_ID 为什么也进入 key

若不同 ROS domain 共享同一套 Zenoh Router，不能让两个 domain 的 endpoint 误通信。

因此 domain id 进入 key expression：

~~~text
domain 0 /chatter/type/hash
domain 7 /chatter/type/hash
~~~

底层基础设施可共享，逻辑 domain 仍然隔离。

## 5. GraphCache 的内部状态不是简单 set

一条 PUT 进入：

~~~text
GraphCache::parse_put(keyexpr)
    |
Entity::make(...)
    |
find/create GraphNode
    |
update topic/service maps
    |
matched QoS/event callbacks
~~~

DELETE 走对应删除路径。

GraphCache 是 derived state cache：真实源头是 liveliness token，cache 是对这些 token 的可查询投影。

## 6. 为什么回调必须在 graph_mutex 之外执行

固定版本源码特意说明：callback 不能在 graph mutex 持有期间执行，否则 callback 若重入 GraphCache 会死锁。

所以更新函数先在锁内收集待执行 callback，再释放 graph lock 后调用：

~~~text
lock
 mutate graph
 collect callbacks
unlock
invoke callbacks
~~~

这是 registry/cache 系统中非常典型的重入安全设计。

## 7. GraphCache 为什么还承担 QoS matched event

DDS 后端天然拥有 Writer/Reader matching event。

Zenoh 后端没有完全同构的 DDS entity matcher。

rmw_zenoh 因此在 GraphCache 更新 endpoint 时，根据 ROS QoS compatibility 计算匹配关系，并产生 RMW 层事件。

所以 GraphCache 同时承担 graph introspection 与 endpoint compatibility projection。

## 8. 初始快照与增量更新

Context 初始化先通过 liveliness_get 获取现有 token：

~~~text
existing entities
   |
snapshot
   |
GraphCache
~~~

随后 liveliness subscriber 处理未来 PUT/DELETE：

~~~text
future changes
   |
incremental update
   |
GraphCache
~~~

这是 distributed registry 常见的 snapshot + stream 模式。

## 9. graph guard condition 的作用

GraphCache 更新完成后，Context 触发 ROS graph guard condition：

~~~text
Zenoh liveliness event
   |
GraphCache mutate
   |
rmw_trigger_guard_condition
   |
rcl wait set
   |
ROS graph observer wakes
~~~

底层 discovery event 并不会直接运行 ROS 用户代码。

## 10. 对比 DDS RMW

DDS RMW：

~~~text
SPDP/SEDP
   |
RMW discovery callbacks
   |
ROS GraphCache
~~~

rmw_zenoh：

~~~text
Zenoh liveliness keyspace
   |
rmw_zenoh GraphCache
~~~

两者都在解决同一问题：把底层 endpoint discovery 投影成 ROS graph，只是 discovery primitive 不同。

## 11. 真正应该区分的五层

1. transport peer discovery；
2. endpoint existence；
3. application graph identity；
4. QoS/type compatibility；
5. graph observer wakeup。

rmw_zenoh 的 GraphCache 正是把这些层重新粘合起来的适配层。
