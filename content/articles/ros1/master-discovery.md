# ROS1 Master：中心化发现怎样维护一个动态机器人图

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。

ROS1 Master 最容易被误解成“所有 topic 的中心服务器”。如果真这样设计，一台机器人上相机、雷达和点云全部绕 Master 转发，它会迅速成为带宽瓶颈和单点数据故障。实际实现完全不同：Master 是一个**控制面注册表**，负责把名字、节点和 XML-RPC API 组织成可查询、可更新的 graph。

## 1. 从一张最朴素的表开始

只支持一个 publisher 时，可以写：

~~~python
publishers = {
    "/camera/image": "http://10.0.0.3:41123/"
}
~~~

Subscriber 查一次 map，就知道去哪里联系 publisher。

但机器人系统很快提出更多要求：

- 一个 topic 可以有多个 publisher；
- 一个 node 可以发布多个 topic；
- node 重启后 XML-RPC URI 可能变化；
- 同名 node 重新注册时旧实例不能继续占着 graph；
- publisher 集合变化后已有 subscriber 必须获知变化；
- node 消失时要一次清理它的 publications、subscriptions、services。

于是“一个 dict”开始分裂成正向索引、反向索引和通知路径。

## 2. Registrations 是从资源名到节点集合的主索引

`Registrations` 的核心数据结构很直接：

~~~python
class Registrations(object):
    def __init__(self, type_):
        self.type = type_
        ## { key: [(caller_id, caller_api)] }
        self.map = {}
        self.service_api_map = None
~~~

对 topic publication 来说，一项可能是：

~~~text
key = /camera/image

value = [
  (/camera_front, http://10.0.0.3:41123/),
  (/bag_player,   http://10.0.0.8:39011/)
]
~~~

为什么 value 必须是 list，而不是一个 URI？因为 ROS1 允许多个 publisher 同时提供同一 topic。

为什么这里用 Python dict + list 就足够？因为 Master 的负载是**低频 graph 查询和更新**，不是每条消息的热路径。它没有必要为每帧图像做 fan-out。

## 3. 只保存 topic -> node 为什么还不够

节点关闭时，Master 面临反向问题：

> /camera_front 注册过哪些资源？

如果只有 topic map，清理一个节点就得扫描所有 topic/service/param。源码因此维护 `NodeRef`：

~~~python
class NodeRef(object):
    def __init__(self, id, api):
        self.id = id
        self.api = api
        self.param_subscriptions = []
        self.topic_subscriptions = []
        self.topic_publications  = []
        self.services  = []
~~~

于是 Master 实际拥有两种索引：

~~~text
资源视角:
topic/service/param
      |
      v
Registrations
      |
      +--> node A
      +--> node B

节点视角:
node name
      |
      v
NodeRef
      |
      +--> publications
      +--> subscriptions
      +--> services
      +--> param subscriptions
~~~

这是一种典型的**双索引**设计。正向索引优化“谁提供这个名字”，反向索引优化“这个节点消失时要删什么”。

如果改成一个 `vector<Node>`，按 topic 查 publisher 就要遍历所有节点；如果只保留 topic map，node teardown 又变成全表扫描。多维护一份索引，换来清晰的查询复杂度和生命周期路径。

## 4. RegistrationManager 为什么统一持有四类表

~~~python
class RegistrationManager(object):
    def __init__(self, thread_pool):
        self.nodes = {}

        self.publishers = Registrations(
            Registrations.TOPIC_PUBLICATIONS)
        self.subscribers = Registrations(
            Registrations.TOPIC_SUBSCRIPTIONS)
        self.services = Registrations(
            Registrations.SERVICE)
        self.param_subscribers = Registrations(
            Registrations.PARAM_SUBSCRIPTIONS)
~~~

`nodes` 的 key 是 caller_id；各种 `Registrations` 的 key 是 topic/service/param 名。两个 key 空间回答不同问题。

服务与 topic 又有一个语义差异：topic 天生是一对多集合；service 在 Master 注册模型里要求一个 active provider，因此 `service_api_map` 需要额外维护 provider URI。

这说明“都叫名字服务”并不意味着底层容器语义相同。

## 5. registerSubscriber 同时完成注册和初始发现

固定源码：

~~~python
def registerSubscriber(
        self, caller_id, topic, topic_type, caller_api):
    try:
        self.ps_lock.acquire()

        self.reg_manager.register_subscriber(
            topic, caller_id, caller_api)

        if (not topic in self.topics_types and
                topic_type != rosgraph.names.ANYTYPE):
            self.topics_types[topic] = topic_type

        pub_uris = self.publishers.get_apis(topic)
    finally:
        self.ps_lock.release()

    return 1, "Subscribed to [%s]" % topic, pub_uris
~~~

这里有两个重要事实。

第一，Subscriber 注册成功时就拿到**当前全部 publisher XML-RPC URI**，不需要再发一个固定的“lookup publisher”步骤。

第二，注册和读取当前 publisher snapshot 位于同一个 graph 临界区，避免在这两步之间漏掉显然的状态更新。

## 6. 新 Publisher 出现时为什么不能等 Subscriber 自己轮询

如果 Subscriber 只在启动时查一次：

~~~text
t0 subscriber starts
   publisher set = {}

t1 camera starts

t2 subscriber still knows {}
~~~

让每个 subscriber 每 100 ms 轮询 Master 可以收敛，但节点越多，无意义 RPC 越多。ROS1 选择事件推送。

`registerPublisher`：

~~~python
self.reg_manager.register_publisher(
    topic, caller_id, caller_api)

if (topic_type != rosgraph.names.ANYTYPE or
        not topic in self.topics_types):
    self.topics_types[topic] = topic_type

pub_uris = self.publishers.get_apis(topic)
sub_uris = self.subscribers.get_apis(topic)

self._notify_topic_subscribers(
    topic, pub_uris, sub_uris)
~~~

真正发给 subscriber 的是：

~~~python
def publisher_update_task(api, topic, pub_uris):
    ret = xmlrpcapi(api).publisherUpdate(
        '/master', topic, pub_uris)
~~~

因此 graph 变化走的是：

~~~text
new publisher
    |
registerPublisher
    |
    v
Master updates registration tables
    |
    v
publisherUpdate(topic, full publisher URI set)
    |
    v
subscriber reconciles local connections
~~~

Master 发的是完整 publisher set，而不是“只加一个 A”的增量命令。Subscriber 可以根据目标集合与当前集合求差，使状态更容易收敛。

## 7. 为什么远程通知必须离开 graph 临界区

XML-RPC 到远端 node 的请求可能因为网络、进程暂停或故障持续很久。如果把它直接写成：

~~~python
with graph_lock:
    update_table()
    subscriber_a.publisherUpdate(...)
    subscriber_b.publisherUpdate(...)
    subscriber_c.publisherUpdate(...)
~~~

一个失联 subscriber 就可能让整个 Master graph 更新停在锁里。

固定实现创建：

~~~python
self.thread_pool =
    rosmaster.threadpool.MarkedThreadPool(num_workers)

self.ps_lock =
    threading.Condition(threading.Lock())
~~~

`NUM_WORKERS = 3`，通知被排进线程池。注册表更新属于短控制面事务，远程 RPC 属于不可控 I/O，两者需要隔离。

这个原则在任何服务发现系统里都成立：**不要把外部调用放进核心状态锁的同步闭环里**。

## 8. ps_lock 究竟保护什么

`ps_lock` 保护的是 graph 一致性：

- publisher/subscriber registration；
- topic type table；
- node reverse index；
- 与这些结构一致性相关的查询和更新。

它不保护 TCPROS payload，也不会在 Publisher 每发一帧时竞争。

因此“ROS1 discovery 是中心化的”不能推导出“数据热路径有一把中央全局锁”。

## 9. 同名 Node 重启为什么不能简单覆盖 dict

ROS node name 是逻辑身份。旧进程可能仍然存活，同时新进程用同一个 caller_id 重新注册：

~~~text
/node_a -> old XML-RPC URI

/node_a -> new XML-RPC URI
~~~

如果仅做：

~~~python
nodes["/node_a"] = new_uri
~~~

旧实例注册过的 topic/service 仍可能留在其他索引里。

`RegistrationManager._register_node_api` 会检查 caller_id 已绑定的 API，识别 URI 变化，并安排旧节点 shutdown/registration cleanup。于是“同名覆盖”实际上是一次生命周期迁移，而不是普通 map assignment。

## 10. Master 崩溃以后什么继续，什么停止

Master 不拥有 TCPROS connection。因此它退出后：

~~~text
已经建立的 TCPROS:
  可以继续传输

新 publisher:
  无法正常注册到 graph

新 subscriber:
  无法正常发现 graph

已有 subscriber 想发现后续新 publisher:
  无法通过 Master 收到新的 publisherUpdate
~~~

这比“roscore 一挂所有 topic 立刻断掉”更准确。

## 11. Master 为什么适合教学，但不适合表达复杂 QoS

Master 知道：

~~~text
/topic X
  publishers: ...
  subscribers: ...
~~~

但不知道一条数据通道需要：

- Best Effort 还是 Reliable；
- history depth；
- durability；
- deadline；
- liveliness；
- resource limits。

这些策略分散在 TCP、queue 和应用实现中。

ROS2/DDS 后来把 endpoint discovery 和 QoS matching 结合起来，根本变化不是把 Python Master 改成 C++，而是**通信 graph 本身开始携带更多传输语义**。

## 12. 从源码作者视角总结 Master

如果从空目录重写一个 ROS1 风格的 discovery service，最自然的演化是：

~~~text
topic -> one URI
  |
  | multiple publishers
  v
topic -> list[(node, URI)]
  |
  | need node teardown
  v
+ node -> registered resources reverse index
  |
  | need late publisher discovery
  v
publisherUpdate push
  |
  | remote RPC may block
  v
notification thread pool
  |
  | same-name node restart
  v
identity replacement + cleanup
~~~

这条因果链比记住 `registerPublisher/registerSubscriber` API 更重要。Master 的真正价值，是用很少的数据结构把一个动态机器人 graph 的控制面组织清楚，同时刻意不进入 payload 热路径。
