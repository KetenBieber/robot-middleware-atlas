# Writer / Reader 创建：从 DDS façade 编译出协议 Endpoint

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 创建不是 malloc，而是配置编译

Writer 创建接收 Participant/Publisher、Topic、QoS 和 Listener。调用者即使直接传 Participant，内部仍可以构造 implicit Publisher，保持 ownership tree。

创建阶段会把 Topic type、序列化 representation、History、QoS、network endpoint、PSMX 与 listener 组合成长期对象；高频发送路径才不需要每帧重新协商这些信息。

## Writer 先确定运行策略

固定 dds_writer.c 中：

~~~c
wr->whc_batch =
  wqos->writer_batching.batch_updates ||
  gv->config.whc_batch;

wr->protocol_version =
  gv->config.protocol_version;
~~~

随后装配 PSMX endpoint，并根据 data representation 派生 sertype。

## 真正创建 DDSI Writer

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

WHC 被直接交给 DDSI writer。这说明 Writer History Cache 不是外围工具，而是协议 endpoint 本身的数据结构。

## Writer 进入 ownership tree

~~~c
dds_entity_register_child(
  &pub->m_entity,
  &wr->m_entity);

dds_entity_init_complete(
  &wr->m_entity);
~~~

从此它既受 Publisher/Participant 生命周期约束，也参与 SEDP discovery 和 matching。

## Async send thread 在何时出现

创建第一个 async writer 时：

~~~c
if (async_mode &&
    !gv->sendq_running)
{
  ddsi_xpack_sendq_init(gv);
  ddsi_xpack_sendq_start(gv);
}
~~~

不是每个 Writer 各自一个 send thread，而是 Domain 级共享 sendq。线程数更少，但多个 async writers 会共享 queue capacity 和发送执行资源。

## Reader 为什么先拥有 RHC

Reader 创建时，DDSI endpoint 直接拿到 DDSc RHC interface：

~~~c
rc = ddsi_new_reader(
  &rd->m_rd,
  &rd->m_entity.m_guid,
  NULL,
  pp,
  tp->m_name,
  tp->m_stype,
  rqos,
  &rd->m_rhc->common.rhc,
  dds_reader_status_cb,
  rd,
  vl_set);
~~~

网络层完成 RTPS 解析、defrag 与 reorder 后，就可以把已可交付的样本送到 Reader History Cache。

## PSMX callback 也在创建期绑定

Reader 创建完成后会遍历 PSMX endpoints，如果插件提供 on_data_available，则把 Reader handle 交给它。共享内存/plugin path 不是每次读写时临时全局扫描，endpoint binding 已经在配置阶段完成。

## 实时循环前应完成哪些工作

高频控制线程最好只承担：

~~~text
read latest input
-> compute
-> write output
~~~

Topic/type 创建、QoS merge、Writer/Reader、discovery、matching、memory pool、PSMX endpoint 与 waitset attachment 都应尽量在进入周期循环前完成。否则偶发配置工作会直接进入周期尾延迟。
