# Discovery：SPDP 找 Participant，SEDP 找 Endpoint

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 两阶段发现是对象依赖决定的

DDS discovery 常被说成“自动发现节点”。从 RTPS 实现看，更准确的是：

~~~text
SPDP
  -> Participant discovery

SEDP
  -> Writer / Reader discovery

QoS matching
  -> endpoint match
~~~

不能直接从 Writer 开始，因为 endpoint 的 GUID prefix、default locator、lease、builtin endpoint capability 与安全上下文都依赖 participant。

## SPDP 创建的是 proxy participant

ddsi_discovery_spdp.c 先从接收的 participant data 生成 default/meta address set，再调用：

~~~c
struct ddsi_proxy_participant
  *proxy_participant;

if (!ddsi_new_proxy_participant(
      &proxy_participant,
      gv,
      &datap->participant_guid,
      builtin_endpoint_set,
      as_default,
      as_meta,
      datap,
      lease_duration,
      rst->vendor,
      timestamp,
      seq))
{
  return HSR_NOT_INTERESTING;
}
~~~

远端 Participant 没有跨网络变成本地 DDSc Participant。这里只建立其协议镜像与 soft-state。

## Lease 是 discovery 的生命线

proxy participant 要记住“远端还活着多久”。Lease 到期后，远端 participant 及其 proxy endpoints 都需要被回收。Discovery 因而天然是带过期语义的分布式状态维护，不是一次性配置文件。

## 为什么广播 SPDP 会触发响应

固定源码区分 directed packet 与 broadcasted packet。广播发现会主动 respond_to_spdp，加速双方互相认识，而不是完全等待下一次周期公告。

## SEDP 宣布 endpoint

本地 Writer 创建后，ddsi_sedp_write_writer() 会寻找 Participant 的 builtin SEDP writer，并发布 endpoint GUID、Topic/Type、QoS、locator 等描述。

因此 SEDP 本身也是建立在 builtin DDSI endpoints 上的一套发现流。

## 收到 SEDP 后创建 proxy endpoint

远端 Writer 的核心分支：

~~~c
if (pwr)
  ddsi_update_proxy_writer(
    pwr,
    seq,
    as,
    xqos,
    timestamp);
else
{
  struct ddsi_proxy_writer *proxy_writer;

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
}
~~~

远端 Reader 对应 proxy_reader。

## 为什么 proxy writer 直接拿到 user delivery queue

网络 DATA 最先由 receive thread 解析；但业务交付不必总在 receive thread 发生。proxy writer 创建时保存 gv->user_dqueue，后续接收路径可以在完成 defrag/reorder 后把 ready sample 交给 delivery queue。

Discovery 因此不仅记录“谁在哪里”，还给数据面装配执行环境。

## 发现不等于匹配

发现到 endpoint 只说明它存在。真正建立通信关系还要比较 Topic、Partition、Type/Data Representation、Reliability、Durability、Deadline、Ownership、Liveliness 等。

~~~text
graph visible
!= QoS compatible
!= endpoint matched
!= sample delivered
~~~

这也是 ROS 2 中 topic list 能看到但收不到消息时必须继续检查 QoS 与数据面的原因。
