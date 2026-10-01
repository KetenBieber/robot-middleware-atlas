# ROS1 Master vs ROS2 DDS Discovery：从中心目录到分布式收敛，故障模型发生了什么变化

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。比较重点不是“中心化好还是去中心化好”，而是 graph state 在哪里保存、怎样传播，以及故障会以什么形式出现。

Supporting source: `rmw_dds_common@e26ba1079886c5598614172db0b27526aa6af07d`，用于核对 `GraphCache` 与 `ParticipantEntitiesInfo` 的 ROS graph 同步路径。


## 1. ROS1：Master 是 graph control plane

ROS1 publisher/subscriber 向 Master 注册：

~~~text
registerPublisher
registerSubscriber
lookupNode / publisherUpdate
~~~

Master 维护 topic、node API、publisher/subscriber registration，但 payload 并不经过 Master。匹配完成后，subscriber 通过 publisher XML-RPC 进行 `requestTopic`，再建立 TCPROS 连接。

~~~text
Master = discovery / registry
TCPROS = payload path
~~~

这是“中心化发现 + 点对点数据面”。

## 2. Master 故障为什么不会立即切断已有 TCPROS

若 TCPROS 已经建立：

~~~text
publisher <------ TCPROS ------> subscriber
~~~

后续 payload 不经过 Master。

所以 Master 挂掉后，已有连接可以继续通信；但新节点加入、重连、graph 查询都会受影响。

~~~text
existing data plane can survive
new graph operations fail
~~~

## 3. ROS2：没有一个全局 ROS Master

ROS2 底层 DDS 使用 participant/endpoint discovery。

~~~text
Participant discovery
    |
Endpoint discovery
    |
Writer <-> Reader matching
~~~

ROS graph 仍然存在，只是不再集中在单个 Master。

`rmw_dds_common::GraphCache` 保存 ROS node 与 endpoint 关系，并通过 `ParticipantEntitiesInfo` 同步 graph information。

所以去中心化不等于没有 registry：

~~~text
one central registry
      ↓
many local graph caches + discovery/synchronization
~~~

## 4. ROS2 的故障从单点不可达变成收敛问题

ROS1 常见故障：

- ROS_MASTER_URI 错；
- Master 不可达；
- publisher XML-RPC URI 不可达；
- stale registration；
- TCPROS connect 失败。

ROS2 常见故障更像：

- DDS Domain 不一致；
- multicast/discovery traffic 被网络阻断；
- endpoint QoS 不兼容；
- graph cache 尚未收敛；
- participant lease/liveliness 状态变化；
- backend-specific discovery 配置错误。

“节点都启动了但看不到对方”在 ROS2 中不再只对应一个中心目录问题。

## 5. discovery latency 也改变了形态

ROS1 常见发现链：

~~~text
node -> Master register
Master -> subscriber publisherUpdate
subscriber -> publisher requestTopic
~~~

ROS2 发现依赖分布式协议：

~~~text
participant appears
  -> participant discovery
  -> endpoint discovery
  -> QoS matching
  -> ROS graph update
~~~

启动时延可能来自网络 discovery、endpoint announcement、QoS matching 或 graph propagation。

## 6. ROS Graph 与 DDS endpoint view 不是同一个东西

DDS endpoint discovery 与 ROS `GraphCache` 是相邻层，不应合并理解。

DDS 负责知道 Writer/Reader；ROS graph 还需要表达 node、namespace、topic/service 等 ROS 语义。

~~~text
DDS endpoints 是否发现?
        |
RMW graph 是否更新?
        |
rclcpp graph API 是否看到?
~~~

## 7. 对机器人系统部署的影响

单机比赛机器人通常希望启动快、网络拓扑固定、故障定位直接；ROS1 Master 的中心目录很容易理解。

多机、动态节点、跨主机场景则更需要 distributed discovery、endpoint policy、domain isolation 与去中心单点依赖。

ROS2/DDS 提供更强能力，但部署复杂度也更高。

## 8. 没有 Master 不等于没有控制面成本

ROS2 仍然需要 discovery traffic，还要维护 graph cache、endpoint state 与 QoS compatibility。

~~~text
ROS1:
central coordination cost

ROS2:
distributed synchronization cost
~~~

成本只是换了位置。

## 9. 故障排查顺序

ROS1：

~~~text
Master reachable?
registration correct?
publisher XMLRPC reachable?
requestTopic success?
TCPROS connected?
~~~

ROS2：

~~~text
same DDS domain?
participants discovered?
endpoints discovered?
QoS compatible?
RMW GraphCache converged?
Executor actually taking data?
~~~

理解这条差异，比只记住“ROS2 没有 roscore”更有工程价值。
