# Shutdown、生命周期与 DDS-RMW 对照：rmw_zenoh 的真正架构边界

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。最后一篇把创建、运行和关闭放进同一张图，并与 Cyclone/Fast DDS RMW 做机制层对照。

## 1. 创建顺序本质上是一个提交事务

创建 Publisher、Subscription、Service、Client 时，往往需要同时创建：

- C ABI handle；
- C++ Data object；
- Zenoh endpoint；
- liveliness token；
- local queue/event state；
- graph identity。

这些步骤不可能全部原子完成。

所以正确模式是：

~~~text
construct internal resources
   |
validate each step
   |
only after success
   v
publish public rmw handle
~~~

失败时按逆序自动释放。

这就是 RAII 在 C ABI 边界上的真正价值。

## 2. public handle 为什么不能过早暴露

如果先返回或注册 rmw handle，再创建 liveliness token 失败：

~~~text
upper layer sees entity
graph does not
transport incomplete
~~~

就会出现半构造对象。

所以 `handle->data` 的提交应是创建事务末端，而不是开头。

## 3. shutdown 为什么必须先阻止新入口

每个 Data object 都维护 shutdown state。

先将状态原子地切为 shutting down，可以让新的 publish、take、query callback 先看到：

~~~text
entity no longer accepts work
~~~

然后才执行外部资源 undeclare。

这避免关闭过程一边释放 endpoint，一边又有新工作进入。

## 4. weak_ptr 解决什么，不解决什么

异步 Zenoh callback 捕获 weak_ptr，可以防止：

~~~text
object destroyed
callback starts later
use-after-free
~~~

但 weak_ptr 并不能自动解决：

- callback 已经 lock 成 strong_ptr 后 shutdown；
- queue 与 waiter 的同步；
- session close；
- graph token undeclare；
- response correlation map 清理。

所以 pointer ownership 只是生命周期协议的一部分。

## 5. undeclare 为什么是重要的关闭动作

Publisher、Subscription、Service 不只拥有本地 C++ object，还在 Zenoh Runtime 中声明了 endpoint/token。

关闭必须撤销：

~~~text
data endpoint
liveliness identity
queryable/subscriber callbacks
~~~

否则远端可能继续看到 stale graph entity，或本地 callback 仍可能进入。

## 6. Context shutdown 为什么最后 close Session

Session 是所有 endpoint 的共享根。

如果先 close Session，再逐个 entity 做 undeclare，entity teardown 可能进入一个已关闭的 Runtime。

更安全的生命周期模型是：

~~~text
stop new upper-layer work
   |
quiesce / destroy endpoints
   |
remove graph identity
   |
finish callbacks
   |
close shared Session
~~~

固定版本 Context shutdown 已经把 Session close 当作全局屏障，并依赖 Zenoh 等待 in-flight callbacks 的语义。

## 7. rmw_zenoh 与 DDS RMW 的对象图对照

Cyclone/Fast DDS：

~~~text
Context
  |
Participant
  |
Writer / Reader
  |
DDS History
  |
RTPS / Transport
~~~

rmw_zenoh：

~~~text
Context
  |
Zenoh Session
  |
Publisher / Subscriber / Queryable / Query
  |
local queues + GraphCache
  |
Zenoh routing / transport
~~~

上层 RMW contract 相同，内部对象完全不同。

## 8. Discovery 对照

DDS：

~~~text
SPDP participant discovery
SEDP endpoint discovery
  |
RMW GraphCache projection
~~~

rmw_zenoh：

~~~text
Router / Zenoh peer discovery
liveliness token stream
  |
rmw_zenoh GraphCache
~~~

所以 rmw_zenoh 不需要伪造 DDS Participant，只需要恢复 ROS graph 可观察语义。

## 9. Topic receive 对照

DDS：

~~~text
Reader History
   |
WaitSet ready
   |
rmw_take
~~~

rmw_zenoh：

~~~text
Zenoh callback
   |
SubscriptionData deque
   |
condition_variable / rmw_wait
   |
rmw_take
~~~

二者的共同点是：transport arrival 与 user callback execution 之间存在明确的 middleware buffering/readiness 边界。

## 10. Service 对照

DDS RMW 常见：

~~~text
request topic
response topic
request_id correlation
~~~

rmw_zenoh：

~~~text
Zenoh Query
Queryable
Query::reply
local correlation map
~~~

底层 primitive 不同，但上层仍然得到 `rmw_request_id_t` 与 WaitSet-driven execution。

## 11. QoS 对照

DDS 的优势是很多 ROS QoS 与 DDS policy 天然同构。

rmw_zenoh 则需要更显式地区分：

~~~text
native Zenoh behavior
local queue emulation
graph metadata
timer/state emulation
unsupported feature
~~~

这让它成为研究 abstraction impedance mismatch 的好案例。

## 12. 为什么 RMW 是 ROS2 架构中很关键的一层

如果 rclcpp 直接依赖 DDS DataWriter/DataReader，那么换 Zenoh 就意味着重写整个 ROS client library。

RMW 把上层稳定 contract 固定在：

~~~text
create entity
publish/take
request/response
wait
graph
QoS/event
~~~

底层实现才可以完全重组。

所以 RMW 的真正价值不是“支持多个厂商 DDS”，而是允许 ROS execution / graph / API 语义与底层分布式通信模型解耦。

## 13. 从源码作者视角得到的最终原则

rmw_zenoh 展示了四个非常通用的 Runtime 原则：

1. **上层 contract 稳定，底层 primitive 可替换**；
2. **transport callback 只完成 ownership + readiness，不执行任意业务**；
3. **control-plane graph 可能需要由底层较弱 discovery primitive 重新构造**；
4. **生命周期必须覆盖 callback、queue、waiter、graph identity 与共享 transport 根对象**。

这四条原则比 Zenoh 或 ROS2 本身更值得迁移到其他机器人 Runtime。
