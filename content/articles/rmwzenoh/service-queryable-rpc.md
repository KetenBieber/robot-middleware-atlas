# Service 映射为 Queryable：Zenoh Query 怎样实现 ROS 2 Request/Response

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇研究 ClientData / ServiceData，重点解释 Zenoh Query/Queryable 与 ROS 2 Service request_id 之间怎样做语义适配。

## 1. 为什么 Query/Queryable 天然适合 Service

ROS Service：

~~~text
Client request
   |
Server handles
   |
Client response
~~~

Zenoh Query：

~~~text
Querier / get
   |
Queryable
   |
reply
~~~

结构非常接近。

所以 ServiceData 创建时会声明 queryable；ClientData 则通过 Session query/get 发起请求。

## 2. 相似不等于完全同构

ROS 2 Service 还要求：

- `rmw_request_id_t`；
- sequence number；
- writer GID；
- request/response timestamps；
- WaitSet readiness；
- request 可以先 take，response 稍后才 send。

Zenoh Query 本身不会自动提供完全相同的 RMW ABI，因此还需要本地适配状态。

## 3. ServiceData 为什么先把 Query 放入 queue

Queryable callback 收到 Query 后，不直接运行用户 service callback。

它只做：

~~~text
Query callback
   |
ZenohQuery owned wrapper
   |
ServiceData::add_new_query
   |
query_queue_
   |
WaitSet notification
~~~

然后 Executor 调用 `rmw_take_request`，才把 request 交给 rcl/rclcpp Service。

这和 Subscription 的设计完全一致：transport callback 只做有限工作。

## 4. request_id 从哪里来

Client 发请求时会在 attachment 中携带 sequence number、GID 与 timestamp。

ServiceData `take_request` 解析 attachment，填充：

~~~text
rmw_service_info_t
  request_id.sequence_number
  request_id.writer_guid
  source_timestamp
  received_timestamp
~~~

所以 ROS request identity 被显式编码进 Zenoh query metadata。

## 5. 为什么 ServiceData 必须保存原始 Query

`rmw_take_request` 可以发生在时刻 t0：

~~~text
take request
   |
user callback starts
~~~

而 `rmw_send_response` 可能在 t1 才发生。

要让 Zenoh 在 t1 对原来的 Query 执行 `reply`，ServiceData 必须保留那次 Query object。

所以源码维护：

~~~text
writer_gid hash
   ->
sequence number
   ->
owned ZenohQuery
~~~

即 `sequence_to_query_map_`。

## 6. 为什么键是 GID + sequence

单独 sequence number 不能保证跨多个 Client 唯一。

两个 client 都可能出现 sequence=42。

因此 request identity 必须结合 writer identity：

~~~text
(client GID, sequence)
~~~

这正是 `rmw_request_id_t` 的语义。

## 7. send_response 为什么会删除 map entry

一旦 response 已经成功关联到原 Query：

~~~text
lookup GID
  |
lookup sequence
  |
move Query out
  |
erase entry
  |
Query::reply(...)
~~~

原 request 的 correlation state 就完成生命周期。

如果不 erase，就会泄漏 Query handle、map state，以及可能关联的 transport/resource lifetime。

## 8. Service queue 的 QoS depth 说明了什么

`ServiceData::add_new_query` 同样会根据 history/depth 控制 `query_queue_`。

这提醒我们：即使上层语义叫 RPC，请求在进入 Executor 之前仍然可能经历 middleware queue。

如果 service callback 长时间处理，积压问题仍然存在。Service 并不会因为是 request/response 就自动拥有独立线程。

## 9. Client 侧为什么也要本地状态

ROS Client 发请求后，需要等待对应 response ready。

因此 ClientData 也必须维护：

- in-flight request identity；
- reply queue；
- WaitSet readiness；
- response take；
- timeout/shutdown cleanup。

底层 Zenoh reply 到达，不等于上层 future 已完成；它仍要经过 RMW -> Executor -> Client response handling。

## 10. Query timeout 与 ROS timeout 不是同一层

Zenoh Query 可以有 transport/query timeout。

ROS application 也可能对 future 设置自己的 timeout。

两者对应不同状态：

~~~text
Zenoh timeout
  -> transport/query completion

ROS timeout
  -> application no longer wishes to wait
~~~

如果应用 timeout 后本地 correlation state 不清理，就会形成 stale pending request。

## 11. DDS Service 与 Zenoh Service 的真正对照

DDS RMW 常见实现将 Service 表示为 request topic + response topic。

rmw_zenoh：

~~~text
Query -> Queryable -> Reply
~~~

二者底层 primitive 不同，但 RMW 最终都必须恢复：

~~~text
request identity
ready/take
server callback
response correlation
client completion
~~~

真正稳定的是 RMW contract，不是某一种底层消息模型。

## 12. 可迁移的设计

当底层 primitive 与上层 RPC 不完全同构时，可以采用：

~~~text
transport request handle
    +
application request id
    +
local correlation map
    +
scheduler readiness
~~~

将一次网络交互扩展成上层可延迟完成的 RPC 生命周期。
