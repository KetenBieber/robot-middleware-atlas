# 真实案例：官方 ddsperf 怎样把 Cyclone DDS 用成吞吐、延迟与线程实验台

Cyclone DDS 本体固定到 e54e991f75a3e67f8e628da3171122e36ea5b872。本案例直接读取同一提交中的 src/tools/ddsperf/ddsperf.c，因此没有第三方版本漂移。

## 为什么选 ddsperf 而不是 HelloWorld

HelloWorld 只能证明 API 能跑。ddsperf 同时包含：

- Participant、Publisher、Subscriber；
- 多种 Topic 数据尺寸；
- Reliable / Best Effort；
- History 与 Resource Limits；
- Writer batching；
- ping/pong latency；
- throughput data stream；
- WaitSet；
- Listener；
- 多线程接收；
- CPU/network statistics。

它非常适合验证前面源码里讨论的机制是否真的会进入工程代码。

## Topic 命名把 Reliability 直接编码进实验

源码根据 reliable 开关生成：

~~~c
snprintf(
  tpname_data,
  sizeof(tpname_data),
  "DDSPerf%cData%s",
  reliable ? 'R' : 'U',
  tp_suf);
~~~

随后 Topic QoS：

~~~c
dds_qset_reliability(
  qos,
  reliable
    ? DDS_RELIABILITY_RELIABLE
    : DDS_RELIABILITY_BEST_EFFORT,
  DDS_SECS(10));
~~~

这不是单纯换一个 enum。Reliable 会进一步改变 Writer WHC、heartbeat、ACKNACK 与 retransmit 行为。

因此比较 R/U 两组结果时，要想到背后的 protocol state 已经不同。

## 为什么 Ping 和 Data 使用不同 History

Ping/Pong 的任务是测 RTT，每次主要关心当前请求；Data stream 的任务则是测试持续吞吐。

源码为 ping reader/writer 设置 KEEP_LAST 1，而 data reader/writer 会根据 histdepth 选择 KEEP_ALL 或 KEEP_LAST N，并同时设置较大的 resource limits。

这说明 QoS 应按数据语义设计：

~~~text
latency probe
-> 不需要大量旧 ping

throughput stream
-> 需要可配置 history
-> 才能观察 consumer 落后与资源上界
~~~

## Writer Batching 是怎么进入真实程序的

创建 data writer 前：

~~~c
dds_qset_writer_batching(
  qos,
  true);

if ((wr_data =
       dds_create_writer(
         pub,
         tp_data,
         qos,
         listener)) < 0)
{
  ...
}

dds_qset_writer_batching(
  qos,
  false);
~~~

注意 batching 配置在 Writer 创建时被编译进 Writer state；随后把 qos object 改回 false，不会倒过来修改已经创建的 data writer。

这正对应前面 Writer 创建篇说的“创建是配置编译”。

## 为什么高吞吐测试值得打开 Batching

不 batching 时，小 sample 可能导致更多 packet flush 和 syscall；batching 允许 xpack 聚合更多 submessages。

收益是吞吐与 syscall amortization，代价则可能是额外等待时间。

所以机器人系统里：

~~~text
高频遥测
适合评估 batching

闭环控制命令
通常更关心 freshness / latency
不能只追 throughput
~~~

## WaitSet 的正确消费方式

ddsperf 的 subscriber waitset thread 有一段很重要的注释和代码：

~~~c
if (!process_data(
      rd_data,
      arg))
{
  /* when we use DATA_AVAILABLE,
     we must read until nothing remains */

  int32_t nxs;

  if ((nxs =
       dds_waitset_wait(
         ws,
         NULL,
         0,
         DDS_INFINITY)) < 0)
  {
    ...
  }
}
~~~

核心不是函数名，而是“先 drain，再 sleep”。

为什么？

如果 DATA_AVAILABLE 已经触发，但应用每次只拿少量 sample，然后直接再次等 condition，有可能 condition 的边沿语义与剩余历史状态组合出饥饿或错误等待。

正确模式是：

~~~text
wake
-> read/take until no more ready data
-> wait again
~~~

这和 Linux epoll 的 edge-triggered 思维很接近。

## Listener 与 WaitSet 都被 ddsperf 使用

ddsperf 用 Listener 监视 publication/subscription matched，同时用 WaitSet 等数据。

这证明二者不是二选一的全局模式，而是可以按事件性质组合：

~~~text
connection/match status
-> listener

high-rate sample consumption
-> dedicated waitset thread
~~~

## 线程模型如何服务实验

ddsperf 可以创建独立 subscriber thread、ping/pong thread，并使用 atomic termflag 控制退出。

这让 benchmark 可以把：

- publish workload；
- receive processing；
- latency probing；
- stats printing

分到不同执行上下文。

对于读源码的人，真正值得学习的是“测量代码自己也会改变系统调度”，所以 benchmark 的 worker placement 必须记录。

## 从 ddsperf 迁移到机器人 Benchmark

可以直接借它的实验变量设计一个 ROS 2/DDS 对照矩阵：

| 变量 | 组 A | 组 B |
| --- | --- | --- |
| Reliability | Best Effort | Reliable |
| History | Keep Last 1 | Keep Last 10/100 |
| Batching | off | on |
| Payload | 64 B | 1 KB / 64 KB |
| Publish mode | synchronous | asynchronous |
| Network | localhost | 独立 NIC |
| CPU | shared cores | affinity isolated |

测量至少包括：

~~~text
publish call p50/p99/p999
end-to-end latency
data age
packet loss
retransmit count
WHC/RHC memory
CPU time
context switches
socket drops
~~~

这样才能把 Cyclone DDS 的源码机制和真实运行结果对应起来。

## 这个案例验证了什么

ddsperf 直接证明：

- QoS 是 runtime behavior，不是文档标签；
- Writer batching 在创建阶段进入 Writer；
- WaitSet 需要正确 drain；
- Listener 与 WaitSet 可以组合；
- throughput、latency、reliability 是不同实验轴；
- 一个成熟 benchmark 本身也必须显式设计线程与退出。

它是阅读 Cyclone DDS 后最适合先跑的官方程序。
