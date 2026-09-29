# PSMX 与 Loan：共享内存不是一个布尔开关

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## PSMX 是什么层

当前 Cyclone DDS 用 PSMX 抽象可插拔的 Pub/Sub Message Exchange。它让某些数据交换不必走常规 RTPS/UDP，同时仍与 DDS Writer/Reader、Topic、QoS 和 loan 生命周期协调。

重要的是：PSMX 是接口层，不能把它简单等同于某一个具体共享内存实现。

## Topic 先拥有一组 PSMX Topics

固定 dds_psmx.c 中，内部 PSMX topic 保存：

~~~c
psmx_topic->psmx_endpoints = NULL;
~~~

创建 Writer/Reader endpoint 时，再根据 Topic 上的 PSMX instances 与 QoS 创建 endpoint。

## Endpoint 为什么保存多个 PSMX Endpoint

dds_endpoint_add_psmx_endpoint() 会遍历 psmx_topics：

~~~c
for (uint32_t i = 0;
     i < psmx_topics->length;
     i++)
{
  struct dds_psmx_endpoint_int
    *psmx_endpoint =
      psmx_topic->ops.create_endpoint(
        psmx_topic,
        qos,
        endpoint_type);

  ...
  ep->psmx_endpoints.endpoints[
    ep->psmx_endpoints.length++
  ] = psmx_endpoint;
}
~~~

因此内部不是单个 shm pointer，而是一组可扩展 exchange endpoints。

## Shared Memory 是 Feature Capability

固定源码检查：

~~~c
dds_psmx_supported_features(
  psmx_endpoint
    ->psmx_topic
    ->psmx_instance)
&
DDS_PSMX_FEATURE_SHARED_MEMORY
~~~

也就是说“这个 endpoint 是否支持共享内存”是 PSMX capability，不是 DDS API 预设所有 PSMX 都等于 SHM。

## Loan 为什么必须验证来源

Writer write path 会检查 loan_origin 是否来自当前 Writer 对应的 PSMX endpoint。

这是 ownership 安全的关键：

~~~text
从 endpoint A 借出的 buffer
不能随意交给 endpoint B 当作自己的 pool object
~~~

否则释放者、metadata layout、memory pool owner 都可能错位。

## PSMX Writer Loan 如何减少 Copy

理想路径是：

~~~text
application request loan
-> PSMX/backend pool 返回 sample memory
-> application 直接填充
-> dds_write
-> backend 接管该 loan
-> reader/backend 消费
~~~

这样可以消掉“应用临时 buffer -> middleware transport buffer”的一次复制。

但前提是数据类型、endpoint、loan origin 和生命周期都匹配。

## 为什么 Serdata 与 PSMX Loan 仍会同时出现

固定 dds_write.c 中同时处理 serdata、PSMX loan 与 serialized key。原因是同一个 Writer 可能还要服务：

- network readers；
- local readers；
- 多个 PSMX endpoints；
- keyed topic。

共享内存优化不能破坏其他路径需要的 RTPS serialization 与 key semantics。

## 为什么某些情况下仍然必须 Copy

源码根据 refcount、type conversion 与 loan ownership 决定是否复制 serdata 或 loan。只要一个 buffer 同时被多个独立生命周期使用，就不能靠“共享一个裸指针”假装没有所有权问题。

所以 zero-copy 真正的第一性原理是：

> 让生产者直接写最终消费者可接受、且生命周期可安全转移的存储。

少任何一个条件，都可能需要 copy。

## 对点云和图像的意义

大图像、点云、深度数据很适合 loan/shared-memory path，因为一次完整 memcpy 的绝对成本高。但是否真正获益要检查：

~~~text
RMW 是否暴露 loan
类型是否支持
Writer 是否命中 PSMX
Reader 是否同一交换后端
是否又为了其他 network reader 做 serialization
业务 callback 是否延长 loan 生命周期
~~~

不要仅凭配置文件里出现 shared-memory 字样就宣称端到端零拷贝。
