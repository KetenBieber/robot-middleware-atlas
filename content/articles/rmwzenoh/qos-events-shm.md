# QoS、Events 与 SHM：没有 DDS Entity 后，ROS 2 Contract 怎样继续成立

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇研究语义差异最明显的部分：ROS 2 QoS 与 Event API 比 Zenoh 原生 pub/sub contract 更宽，rmw_zenoh 必须决定哪些直接映射、哪些组合实现、哪些暂不支持。

## 1. QoS 不是一组可以机械复制的字段

ROS 2 QoS 包括 reliability、durability、history、depth、deadline、lifespan、liveliness 与 lease duration。

DDS RMW 可以把很多字段映射到 DDS policy；Zenoh 没有完全同构的 policy table。

所以 rmw_zenoh 必须逐项回答：

~~~text
native?
emulated?
local queue?
graph compatibility only?
unsupported?
~~~

## 2. history/depth 为什么落在本地 queue

SubscriptionData 和 ServiceData 都会根据 RMW history/depth 控制 deque。

所以 KEEP_LAST/Depth 至少有一部分是：

~~~text
RMW local buffering policy
~~~

而不是 Zenoh transport 自己的历史缓存。

这意味着 queue memory 与 drop-oldest 行为必须由 rmw_zenoh 自己负责。

## 3. durability 为什么需要更复杂的机制

TRANSIENT_LOCAL 表达 late-joiner 可以获得历史样本。

单纯的普通 Zenoh publisher/subscriber 并不能自动等价 DDS transient local。

固定实现使用 advanced/history 相关能力与补取路径组合出对应行为。

所以 durability 不是“把一个 enum 放进 key”就结束。

## 4. QoS 为什么还要编码进 liveliness key

GraphCache 需要知道远端 endpoint 的 QoS，才能回答：

- endpoint info；
- compatibility；
- matched event；
- best-available adaptation。

因此 `qos_to_keyexpr` 把 QoS 序列化进 liveliness token。

这属于 control-plane metadata，不是 data-plane packet policy。

## 5. configuration represented 不等于 runtime behavior implemented

这是非常容易混淆的一点。

固定版本可以把某个 QoS 字段编码进 graph metadata，但这并不自动说明相应 runtime behavior 已实现。

源码 `rmw_event.cpp` 明确将部分 deadline/liveliness event 标记为 unsupported。

所以要区分：

~~~text
configuration represented
compatibility computable
runtime behavior implemented
event observable
~~~

四者不是同一件事。

## 6. Event 为什么部分来自 GraphCache

Publisher/Subscription matched event 可以通过 graph endpoint 的 PUT/DELETE 与 compatibility 变化推导。

因此 GraphCache 能生成一部分 matched / incompatible 事件。

而 message lost 更接近数据面 queue 行为。

不同 event 的事实源不同，不能用一个统一“event manager”概念掩盖。

## 7. Deadline/Liveliness 为什么难以顺手补上

Deadline 需要时间监督：

~~~text
expected arrival / write interval
   |
timer / watchdog
   |
miss event
~~~

Liveliness 需要 ownership lease 与失活判定。

如果 Zenoh 原生 primitive 与 ROS 定义不完全同构，就需要额外 timer/state machine。

“Zenoh liveliness”与“ROS QoS Liveliness policy”也不能因为名字相同就视为同一语义。

## 8. SHM transport optimization 属于哪里

Context 初始化会读取 Zenoh shared-memory transport 配置，并建立 SHM 相关状态。

Publisher 对较大 payload 可以选择 SHM buffer path。

但这属于：

~~~text
transport/storage optimization
~~~

而不是：

~~~text
RMW QoS semantic guarantee
~~~

二者应分层理解。

## 9. 为什么 SHM 不自动等于端到端 zero-copy

完整链还包括：

~~~text
ROS object
  |
CDR serialization?
  |
SHM buffer
  |
Zenoh transport
  |
receiver payload representation
  |
deserialize?
  |
ROS object
~~~

如果 typed ROS message 仍先被序列化进 SHM buffer，那么减少的是 transport copy，不是 ROS object 到 ROS object 的全链 zero-copy。

## 10. Buffer-aware endpoint 又是另一层

固定版本源码已经包含 rosidl::Buffer-aware endpoint 与 backend metadata。

这允许 graph token 宣告可接受的 storage backend，并在 endpoint 建立时做能力协商。

这比普通 SHM transport 更接近：

~~~text
application-visible storage contract
~~~

但仍然要和普通 message path、fallback、durability 等机制组合。

## 11. 语义适配应该建立能力矩阵

实现 RMW backend 时，不应该只问“底层有没有 reliability”。

更有效的是逐项列出：

| ROS 语义 | 底层 primitive | 额外状态 | 可观察 event |
|---|---|---|---|
| KEEP_LAST | local deque | depth | message lost |
| type compatibility | key/type hash | GraphCache | matched/incompatible |
| transient local | history/query | retained state | late join behavior |
| deadline | timer needed | last-event timestamp | deadline missed |
| liveliness | lease/state | timer/identity | liveliness changed |

## 12. 最重要的结论

RMW backend 的任务不是把 RMW struct 机械翻译成底层 config。

真正目标是：

> 对每个上层可观察行为，找到底层 primitive + 本地状态 + 生命周期 + event 的完整实现闭环。

rmw_zenoh 正好是研究这种语义适配的典型案例。
