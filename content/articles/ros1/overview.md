# ROS1 Communication Runtime：从名字发现到回调执行

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）。序列化机制另对照 roscpp_core `a1a194271bfb35b97a09553f2782585cf7fec9db`，Nodelet 另对照 nodelet_core `5ed9cabe9388d48a8e228f682d005f313b0a7e89`。

ROS1 的通信不能只理解成“topic + TCP”。真正决定运行时行为的是三条彼此分离的链：**节点怎样相遇、payload 怎样移动、收到数据以后谁获得 CPU 执行 callback**。把这三件事拆开以后，ROS1 的设计反而非常适合拿来学习机器人中间件。

假设机器人上有两个进程：

~~~text
camera_node                         planner_node
发布 /camera/image                 订阅 /camera/image
~~~

最直接的实现是把相机 IP 和端口写进规划器配置。只要进程重启换端口、节点迁移到另一台机器、同一 topic 出现多个 publisher，这个方案就开始失效。ROS1 因此把“谁和谁通信”与“真正搬运消息”分开。

## 1. 三个平面对应三个不同的工程问题

~~~text
                  +----------------------+
                  |      ROS Master      |
                  |  registration graph  |
                  +----------+-----------+
                             |
                    XML-RPC control plane
                             |
           +-----------------+-----------------+
           |                                   |
     Publisher Node                      Subscriber Node
           |                                   |
           +----------- TCPROS ----------------+
                     payload data plane
                              serialized bytes
                                    |
                                    v
                           SubscriptionQueue
                                    |
                                    v
                            CallbackQueue
                                    |
                                    v
                              Spinner thread
                               execution plane
~~~

**发现平面**解决“对端在哪里”；**数据平面**解决“payload 怎样过去”；**执行平面**解决“什么时候运行用户代码”。

这三个平面拥有完全不同的时间尺度。Master 的注册更新可以是低频控制事件；TCPROS 在相机流上可能持续搬运几十 MB/s；CallbackQueue 则直接影响控制程序是否因为某个慢 callback 而积压。

如果把它们混成一层，就很容易出现错误判断，例如：

- “Master 很慢，所以 topic 数据也会慢”——数据建立连接后并不经过 Master；
- “TCP 已经收到包，所以 callback 已经执行”——中间还有队列与 OS 调度；
- “queue_size 越大越安全”——控制系统可能只是更晚地处理旧数据。

## 2. Master 是注册表，不是消息 broker

固定源码中的 `ROSMasterHandler` 直接持有 graph registration：

~~~python
self.thread_pool = rosmaster.threadpool.MarkedThreadPool(num_workers)
self.ps_lock = threading.Condition(threading.Lock())

self.reg_manager = RegistrationManager(self.thread_pool)

self.publishers  = self.reg_manager.publishers
self.subscribers = self.reg_manager.subscribers
self.services = self.reg_manager.services
self.param_subscribers = self.reg_manager.param_subscribers

self.topics_types = {}
~~~

这里没有“topic payload queue”。Master 的核心状态是**名字、节点与 XML-RPC URI 的关系**。

因此 ROS1 的结构是：

~~~text
centralized discovery
+
peer-to-peer payload transport
~~~

而不是：

~~~text
Publisher -> Master -> Subscriber
~~~

一旦 TCPROS 已经建立，图像帧不会绕 Master 一圈。

## 3. Subscriber 第一次拿到的不是 TCP 端口

`registerSubscriber` 返回当前 publisher 的 XML-RPC API 列表：

~~~python
self.reg_manager.register_subscriber(topic, caller_id, caller_api)

if not topic in self.topics_types and topic_type != rosgraph.names.ANYTYPE:
    self.topics_types[topic] = topic_type

pub_uris = self.publishers.get_apis(topic)

return 1, "Subscribed to [%s]" % topic, pub_uris
~~~

后续新 publisher 出现，Master 再通过 `publisherUpdate` 主动通知已有 subscriber。

因此 ROS1 建链实际上有两次寻址：

~~~text
Master:
  topic -> publisher XML-RPC URI

Publisher XML-RPC API:
  requestTopic() -> TCPROS host + port
~~~

这比“Subscriber 去 Master lookup 一下 TCP 地址”更准确。控制面先找到 publisher 的管理入口，真正的数据端口由 publisher 自己在 transport negotiation 阶段给出。

## 4. 为什么还要 requestTopic

如果 Master 直接保存 TCP 地址，它就必须知道每种 transport 的参数和协商规则。ROS1 选择让 Publisher 自己决定数据通道。

Publisher 侧的 `TopicManager::requestTopic` 会检查 subscriber 提供的协议列表。遇到 TCPROS 时：

~~~cpp
if (proto_name == string("TCPROS"))
{
  XmlRpcValue tcpros_params;
  tcpros_params[0] = string("TCPROS");
  tcpros_params[1] = network::getHost();
  tcpros_params[2] = int(connection_manager_->getTCPPort());

  ret[0] = int(1);
  ret[1] = string();
  ret[2] = tcpros_params;
  return true;
}
~~~

这里返回的是：

~~~text
["TCPROS", host, port]
~~~

从这一刻开始，控制面才真正切换到数据面。

## 5. TCP 连接成功仍然不是 ROS topic 已经可用

TCP 只提供有序 byte stream，不知道 topic、消息类型，也不知道一条消息在哪里结束。TCPROS 在连接建立后还要做两层协议：

~~~text
TCP established
      |
      v
connection header
  topic
  type
  md5sum
  callerid
      |
      v
message stream
  uint32 payload length
  payload bytes
  uint32 payload length
  payload bytes
  ...
~~~

所以真实链路不是：

~~~text
C++ object -> send()
~~~

而是：

~~~text
C++ message
  -> serialization
  -> length-prefixed bytes
  -> TCP
  -> framing
  -> deserialization
~~~

## 6. 网络线程收到消息以后为什么不直接跑 callback

如果 PollManager 的网络线程收到一帧激光点云后直接执行规划 callback，一个 30 ms 的规划函数会让同一网络执行上下文停止处理其他 socket。

ROS1 把这条链拆开：

~~~text
PollManager thread
    |
    v
TransportTCP
    |
    v
Connection
    |
    v
TransportPublisherLink
    |
    v
Subscription::handleMessage
    |
    v
SubscriptionQueue::push
    |
    v
CallbackQueue::addCallback

---------------- thread handoff ----------------

Spinner thread
    |
    v
SubscriptionQueue::call
    |
    v
deserialize
    |
    v
user callback
~~~

因此“网络 I/O progress”和“业务代码执行”属于两个执行平面。这个分离后来在 ROS2 里仍然存在，只是执行层被组织成 WaitSet / Executor / Callback Group。

## 7. SubscriptionQueue 为什么是有界 FIFO

固定源码在队列满时丢掉最旧消息：

~~~cpp
if (fullNoLock())
{
  queue_.pop_front();
  --queue_size_;
  full_ = true;
}

queue_.push_back(i);
++queue_size_;
~~~

这是一个很值得机器人控制工程师注意的决定。

假设 odometry 是 100 Hz，而 callback 只能处理 50 Hz：

~~~text
无限 FIFO:
  backlog 持续增长
  -> callback 处理越来越旧的状态

有界 FIFO + drop-old:
  历史样本被丢弃
  -> 数据年龄受到容量约束
~~~

但 queue_size=100 仍可能允许接近 1 s 的历史积压。于是 `queue_size` 不是普通“缓存调大一点”，而是在**突发吞吐容忍度和最大数据年龄**之间做选择。

## 8. 同进程路径为什么不一定需要序列化

roscpp 发布时并不是立刻把消息变成 bytes。它先询问 Publication 当前有哪些 subscriber：

~~~cpp
bool nocopy = false;
bool serialize = false;

if (m.type_info && m.message)
{
  p->getPublishTypes(serialize, nocopy, *m.type_info);
}
else
{
  serialize = true;
}

if (serialize || p->isLatching())
{
  SerializedMessage m2 = serfunc();
  m.buf = m2.buf;
  m.num_bytes = m2.num_bytes;
  m.message_start = m2.message_start;
}

p->publish(m);
~~~

如果 compatible subscriber 都在同一个 C++ 进程里，就有机会直接共享 message object，而不是先序列化再反序列化。

Nodelet 的作用并不是发明另一个消息协议，而是把多个逻辑组件放进同一地址空间，让 roscpp 已有的 intra-process path 真正可用于图像、点云等大 payload。

## 9. 三张地图把 ROS1 放回系统工程中

### 模块职责

~~~text
rosmaster
  graph registration / XML-RPC discovery

roscpp TopicManager
  publication/subscription lifecycle
  requestTopic negotiation

Connection + TransportTCP
  framed async byte stream

SubscriptionQueue + CallbackQueue
  overload policy + executable work

Spinner
  OS threads that consume work

Nodelet
  same-process composition + intra-process path
~~~

### 依赖方向

~~~text
business callback
      ^
      |
Spinner -> CallbackQueue -> SubscriptionQueue
                              ^
                              |
                         Subscription
                              ^
                              |
                       PublisherLink
                              ^
                              |
                         Connection
                              ^
                              |
                        TransportTCP

Master/XML-RPC only participates in discovery/setup
~~~

### 运行时对象与线程

~~~text
Master process:
  XML-RPC worker threads
  registration tables

Publisher process:
  XML-RPC manager
  PollManager thread
  Publication / SubscriberLinks

Subscriber process:
  XML-RPC manager
  PollManager thread
  Subscription / PublisherLinks
  CallbackQueue
  Spinner thread(s)
~~~

同一个“ROS node”内部已经包含多个执行上下文。后续做实时性分析时，必须明确自己讨论的是网络 poll thread、Spinner thread，还是用户另外创建的控制线程。

## 10. ROS1 最值得保留的设计原则

ROS1 的局限很多，但几条原则直到今天仍然值得学习：

- **控制面和数据面分离**：发现服务不进入 payload 热路径；
- **transport negotiation**：先用稳定控制协议交换能力，再选择数据通道；
- **连接建立和消息 hot path 分离**：graph 变化才重新协商；
- **I/O progress 和业务执行分离**：慢 callback 不应该阻塞所有 socket；
- **有界队列必须定义过载语义**：drop、block、overwrite 或 backpressure 不能含糊；
- **同进程与跨进程 ownership 不同**：shared_ptr 只解决一个地址空间里的寿命，跨进程必须重新定义 wire/shared-memory protocol。

把这套较小的 Runtime 看明白，再进入 ROS2 的 rclcpp → rcl → rmw → DDS/Zenoh → Executor，会容易分辨每一层究竟在解决 ROS1 的哪个问题。
