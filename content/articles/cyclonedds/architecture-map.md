# Cyclone DDS 架构地图：一份样本为什么同时跨越 DDSc、DDSI 与 DDSRT

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 三层运行时不是三套重复封装

~~~text
Application
   |
   v
DDSc
DDS API / Entity / QoS / WaitSet / RHC
   |
   v
DDSI
RTPS participant / writer / proxy / WHC / discovery
   |
   v
DDSRT + transport
thread / mutex / cond / UDP / TCP / time
~~~

DDSc 回答“应用看到什么”；DDSI 回答“RTPS 协议需要记住什么”；DDSRT 回答“OS 实际执行什么”。

## 为什么本地 Writer 同时有两个对象

src/core/ddsc/src/dds_writer.c 创建 Writer 时最终调用：

~~~c
rc = ddsi_new_writer(
  &wr->m_wr,
  &wr->m_entity.m_guid,
  NULL,
  pp,
  tp->m_name,
  sertype,
  wqos,
  wr->m_whc,
  dds_writer_status_cb,
  wr,
  vl_set);
~~~

于是对象关系实际是：

~~~text
dds_writer
  ├─ DDS entity / listener / topic / QoS
  ├─ application-facing state
  └─ m_wr
       |
       v
ddsi_writer
  ├─ RTPS GUID
  ├─ sequence number
  ├─ matched proxy readers
  ├─ WHC
  ├─ destination address set
  └─ heartbeat/reliability state
~~~

把 API 生命周期与协议生命周期拆开，才能处理“API handle 已关闭，但协议回调或延迟释放还没完全消失”的并发关闭问题。

## 远端 Writer 为什么叫 proxy

SEDP 收到远端 endpoint 后，固定源码在 ddsi_discovery_endpoint.c 中创建：

~~~c
ddsi_new_proxy_writer(
  &proxy_writer,
  gv,
  &ppguid,
  &datap->endpoint_guid,
  as,
  datap,
  gv->user_dqueue,
  gv->xevents,
  timestamp,
  seq);
~~~

远端对象不会变成本地 dds_writer；本地只保存它的协议镜像：

~~~text
local participant
├─ local writer
│  └─ matched proxy readers
└─ local reader
   └─ matched proxy writers

remote participant
└─ proxy participant
   ├─ proxy writer
   └─ proxy reader
~~~

proxy 保存 GUID、QoS、locator、reliability/reorder 状态和匹配关系，但不拥有远端业务对象。

## Domain global state 是运行时根

每个 Domain 内部有一套 ddsi_domaingv 风格的全局状态，里面聚合 entity index、网络接口、transport connections、receive threads、user/builtin delivery queues、timed event queue、GC request queue、type/key maps、configuration 和 PSMX instances。

这不等于“所有东西都拿大锁”。它更像 ownership root：谁负责启动和停止整个协议运行时，谁拥有跨 endpoint 共享的索引和线程。

## 数据面与控制面为什么共用实体索引

RTPS DATA 到来时，接收线程必须根据 GUID 找到哪个 proxy writer、它和哪些 local readers 匹配、sequence number 应进入哪套 reorder，以及 HEARTBEAT/ACKNACK 对应谁。Discovery 恰好负责创建和删除这些 endpoint。

entity index 因此是控制面与数据面的交界：控制面维护“谁存在”，数据面消费这份对象图。

## Thread ownership 地图

固定提交初始化时会创建 GC request queue、builtin delivery queue、user delivery queue、xevent thread、receive thread(s)，还可能创建 TCP listener 与 asynchronous sendq。

与此同时，默认同步 Writer 的发送数据路径仍可能运行在应用调用线程。所以“Cyclone DDS 有后台线程”不能推导出“应用调用不会执行网络工作”。

## 在 ROS 2 中怎么映射

~~~text
rclcpp/rclpy
  -> RMW publisher/subscription
  -> DDS Writer/Reader
  -> DDSI Writer/ProxyWriter
  -> RTPS
~~~

ROS Node 也不等于 DDS Participant。具体 participant、publisher、subscriber 的复用策略属于 RMW 实现。后面的 rmw_cyclonedds 案例会直接沿代码追到这些映射。

## 读每个 struct 时固定问五件事

1. 谁创建它？
2. 谁拥有它？
3. 哪些线程能访问它？
4. 删除前谁负责停止产生新引用？
5. 此时 payload 是 typed sample、serdata、RTPS submessage 还是 socket bytes？

只要这五个问题能回答，Cyclone DDS 的复杂度就会从“名词太多”变成普通的 runtime architecture。
