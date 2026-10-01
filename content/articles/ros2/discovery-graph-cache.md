# 没有 ROS Master 以后：DDS Discovery 与 ROS Graph Cache 如何协作

本篇主固定版本为 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，Graph 机制核对 `rmw_dds_common@e26ba1079886c5598614172db0b27526aa6af07d` 与 `rmw_cyclonedds@e370e09ca76fc811e42ff07bd3a5e3b92f18c51e`。底层 SPDP/SEDP 的协议细节可与 Atlas 的 Cyclone DDS / Fast DDS discovery 专题互相对照。

## “ROS2 去中心化”不等于没有 graph 管理

ROS1 的 discovery 很容易画：

~~~text
Publisher ----register----> ROS Master
Subscriber ---lookup------> ROS Master
~~~

删除 Master 之后，ROS2 依赖 DDS 已有的 Participant 和 Endpoint discovery。

但 DDS 只认识：

~~~text
DomainParticipant
DataWriter
DataReader
GUID
QoS
~~~

ROS 用户却还会查询：

~~~text
Node name
Node namespace
topic/service 属于哪个 node
ros2 node list
ros2 topic list
~~~

这就需要额外的 ROS graph 语义。

## DDS discovery 与 ROS graph 是两个相邻层

~~~text
              DDS discovery
     Participant / Writer / Reader
                   |
                   v
                  RMW
                   |
          ROS identity mapping
                   |
                   v
             GraphCache
     Node / namespace / endpoints
~~~

DDS discovery 解决“网络上有哪些 DDS entities”。

ROS graph 解决“这些 entities 在 ROS 世界属于谁”。

## 为什么 GraphCache 需要双向关系

`GraphCache` 的本地更新接口接收 participant GID、node name、namespace 和 endpoint GID。

只保存：

~~~text
writer_gid -> topic
~~~

是不够的。

当一个 node 被销毁时，还需要快速回答：

~~~text
node /camera_driver
    |
    +-- writer A
    +-- writer B
    +-- reader C
~~~

这和 ROS1 `RegistrationManager` 的双索引思想非常接近。分布式 discovery 改变了状态来源，却没有消灭 graph 数据结构本身。

## add_node 为什么返回一条 ParticipantEntitiesInfo

固定接口不是简单的 void：

~~~cpp
rmw_dds_common::msg::ParticipantEntitiesInfo
GraphCache::add_node(
  const rmw_gid_t & participant_gid,
  const std::string & node_name,
  const std::string & node_namespace)
~~~

publisher 创建时也有：

~~~cpp
GraphCache::associate_writer(
  const rmw_gid_t & writer_gid,
  const rmw_gid_t & participant_gid,
  const std::string & node_name,
  const std::string & node_namespace)
~~~

它们返回 participant-entities 描述，是因为本地 ROS graph 的变化还要传播给远端 participant。

所以 graph update 本身也是一种分布式状态同步。

## 远端 participant 怎样合并这份状态

`GraphCache::update_participant_entities` 的固定实现先锁住 graph：

~~~cpp
void
GraphCache::update_participant_entities(
  const rmw_dds_common::msg::ParticipantEntitiesInfo & msg)
{
  std::lock_guard<std::mutex> guard(mutex_);
  // reconcile remote participant/node/entity state
}
~~~

它不是业务 topic callback，而是在把另一个 participant 对 ROS graph 的描述合并到本地缓存。

因此系统里至少存在两类 DDS 数据：

1. 业务 payload；
2. 用于 ROS graph 语义同步的数据。

## 为什么不能只靠 ParticipantEntitiesInfo，不要 DDS discovery

因为 graph message 本身要送给谁、对应 DataWriter/DataReader 如何发现、远端 participant 是否存在，这些仍依赖 DDS discovery。

更准确的顺序是：

~~~text
SPDP
 |
 | discover DomainParticipant
 v
SEDP
 |
 | discover DataWriter/DataReader
 v
DDS endpoints can communicate
 |
 v
ROS graph information propagates
 |
 v
remote GraphCache converges
~~~

ROS graph 是构建在 DDS discovery 上的一层，而不是替代它。

## endpoint 创建为什么要先成功，再写 graph

publisher 创建时通常先建立真实 DDS writer，拿到稳定 GID 后再执行：

~~~text
create DataWriter
      |
      v
obtain publisher_gid
      |
      v
graph_cache.associate_writer(...)
      |
      v
publish ParticipantEntitiesInfo
~~~

这样避免 graph 中出现“逻辑上存在、物理 endpoint 却创建失败”的幽灵条目。

## 这是最终一致的控制面

ROS1 Master 是中心状态。注册调用返回时，Master 自己已经更新。

ROS2 更像：

~~~text
local endpoint created
      |
local graph updated
      |
DDS discovery / graph update propagation
      |
remote graph converges
~~~

因此程序刚启动时立即运行 `ros2 topic list`，看到的拓扑可能还在传播。

这不是业务 payload 丢包，而是 discovery/control plane 尚未收敛。

## 去中心化得到什么，又付出什么

没有单点 Master 后：

~~~text
node A <---- DDS ----> node B
   \                    /
    \------ DDS -------/
           node C
~~~

节点可以依赖 middleware 自己建立 endpoint。

但每个 participant 都需要承担 discovery、GraphCache、endpoint matching、participant lifetime 和 QoS compatibility。

“去中心化”从来不等于“管理成本为零”，而是把中心化成本转移到协议和参与者本身。

## 与机器人启动时序的关系

机器人系统常见：

~~~text
t0 sensor start
t1 localization start
t2 planner start
t3 controller start
~~~

ROS2 中“进程已经启动”不意味着“通信拓扑已经稳定”。还要考虑：

- Domain ID；
- discovery 网络是否可达；
- participant/endpoint discovery；
- QoS compatibility；
- ROS graph convergence。

因此安全控制链更适合显式做 endpoint/lifecycle readiness，而不是固定 sleep 几秒后假设系统完成发现。

## ROS1 与 ROS2 的 graph 对照

~~~text
ROS1
             ROS Master
          /      |       \
  publisher   subscriber  node
  registry     registry   registry

ROS2
       DDS Participant discovery
                |
       DDS Endpoint discovery
                |
          per-process RMW
                |
       local GraphCache state
                |
     ParticipantEntitiesInfo sync
                |
      remote GraphCache state
~~~

ROS2 删除的是中心 Master，不是 graph 这个抽象。
