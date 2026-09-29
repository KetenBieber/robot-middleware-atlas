# 接收主链：recvmsg、RTPS Parser、Defrag 与 Reorder 为什么必须分层

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 收到 UDP 包只是开始

recvmsg 得到的是一段 RTPS message bytes。接收路径必须先检查 RTPS header、vendor、GUID prefix，再遍历 submessage sequence。

固定 ddsi_receive.c 最终把消息交给 handle_submsg_sequence，解析 DATA、DATAFRAG、HEARTBEAT、ACKNACK 等不同 submessage。

## handle_Data 先确认是不是给我的

handle_Data() 首先检查 receiver state：

~~~c
if (!rst->forme)
{
  RSTTRACE(" not-for-me)");
  return 1;
}
~~~

随后还要进行 security validation、writer lookup 与 sample-info 构造。UDP 端口收到包，并不等于 Reader 已收到数据。

## 为什么需要 Defrag

大 sample 会分成多个 DATAFRAG：

~~~text
sample #42
  frag 1
  frag 2
  frag 3
  ...
~~~

应用不能拿半个 sample，所以接收端要按 writer + sequence + fragment number 重组。丢一个 fragment 时，其余 fragment 可能必须暂存，receive buffer 生命周期也会被拉长。

## 为什么 Defrag 以后还要 Reorder

即使每个 sample 已完整，sequence 到达仍可能是：

~~~text
100, 102, 101, 103
~~~

Reliable Reader 不能简单按到达顺序交付，否则 sequence 语义会被破坏。reorder 负责判断当前 next expected sequence、哪些 sample 已完整但前面有洞、GAP 是否宣布某些序号不会再来，以及 HEARTBEAT 是否改变可判定区间。

~~~text
defrag
解决一个 sample 的片段完整性

reorder
解决多个 sample 的序号顺序
~~~

## 同步交付与 Delivery Queue

固定接收源码存在：

~~~c
if (pwr->deliver_synchronously)
  deliver_user_data_synchronously(...);
else
  ddsi_dqueue_enqueue(...);
~~~

可交付 sample 可能直接在 receive path 向下走，也可能进入 user delivery queue，由另一个线程继续处理。

因此分析 Listener callback 时必须先问当前 proxy writer 走哪条 delivery 模式。

## 锁为什么和顺序语义绑定

receive.c 顶部注释明确说明：dqueue enqueue 可能在持有 proxy-writer lock 时发生；synchronous delivery 也可能在持锁状态下发生；多 receive threads 下，为保持 in-order 语义会采用更保守的锁策略。

锁不只是保护容器，它还是协议顺序保证的一部分。

## 接收 Buffer 的最坏情况

正常单包、无丢失时 bump allocator 可以快速复用内存；一旦出现 fragmented sample、packet loss、out-of-order 或慢 delivery，一些 receive buffers 会被长期引用，内存占用与 allocator 行为随之改变。

所以 benchmark 必须包含丢包、乱序与大消息，而不能只测试 localhost 小消息 happy path。

## 从协议层到应用还差一个 RHC

sample 完成 defrag/reorder 后，仍要转换为 Reader 可接受的 serdata/type，并进入每个匹配 Reader 的 RHC。下一篇会拆 Reader History 为什么比一个普通 ring buffer 复杂得多。
