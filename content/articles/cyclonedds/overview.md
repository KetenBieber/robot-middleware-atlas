# Eclipse Cyclone DDS 总览：ROS 2 消息下面真正运行的 DDS/RTPS 数据路径

本专题固定源码版本为 Cyclone DDS 11.0.1，commit e54e991f75a3e67f8e628da3171122e36ea5b872。

## 不要把 DDS 理解成一个 send/recv 包装器

如果只从 ROS 2 API 看 Cyclone DDS，很容易形成一个过度简化的图：

~~~text
Publisher -> DDS -> Subscriber
~~~

真正决定一条机器人消息延迟、缓存、可靠性和线程边界的，是下面这条数据链：

~~~text
dds_write()
  -> serdata / type support
  -> Writer History Cache
  -> RTPS DATA / DATAFRAG / HEARTBEAT
  -> xmsg / xpack
  -> UDP sendmsg()
  -> network
  -> recvmsg()
  -> RTPS parser
  -> defrag + reorder
  -> Reader History Cache
  -> WaitSet / Listener
  -> application
~~~

而在这条数据链工作之前，还有一条控制链：

~~~text
SPDP -> 发现远端 Participant
SEDP -> 发现远端 Writer / Reader
QoS matching -> 建立 endpoint match
~~~

Cyclone DDS 同时管理实体层级、发现数据库、History Cache、可靠性状态、RTPS 协议状态、网络线程、delivery queue、event queue 和 waitset。后续源码阅读的目的，就是把这些名词重新还原成“谁创建对象、谁保存状态、哪个线程访问它、数据现在是什么表示”。

## 为什么机器人系统值得读这一套源码

机器人通信中有几句话经常被说得过于轻松：Reliable 就不会丢、Keep Last 1 就只是“保留最新值”、DDS 是异步的、ROS 2 Executor 在等 DDS 数据、shared memory 就等于 zero-copy。

固定提交自己的开发文档 docs/dev/write-to-take.md 给出了一条极有价值的事实：默认配置下，从 dds_write 到真正的 sendmsg 可以都发生在调用应用线程。只有进入 asynchronous write 模式，真正的 packet transmit 才会转移到 sendq 线程。

因此控制线程里调用一个“发布 API”，不天然意味着只是 O(1) 入队。序列化、WHC 插入、RTPS 消息构建、scatter/gather 组织和 socket send 都可能进入发布调用的尾延迟。

## 两层对象模型：DDSc 与 DDSI

~~~text
Application
   |
   v
DDSc
dds_entity / dds_writer / dds_reader / waitset / RHC
   |
   v
DDSI
ddsi_writer / proxy_writer / WHC / discovery / RTPS
   |
   v
DDSRT + transport
mutex / cond / thread / UDP / TCP / time
~~~

DDSc 负责应用可见的 DDS 语义；DDSI 负责 RTPS 网络语义；DDSRT 负责 OS 原语。一个本地 Writer 往往同时有 DDSc façade 和 DDSI endpoint，远端 Writer 则只在本进程里形成 proxy_writer。

## 固定源码地图

| 层 | 代表目录 | 本专题关注对象 |
| --- | --- | --- |
| DDS C API | src/core/ddsc | Entity、Writer、Reader、RHC、WaitSet |
| DDSI/RTPS | src/core/ddsi | discovery、proxy endpoint、WHC、RTPS、reorder |
| Platform runtime | src/ddsrt | mutex、cond、socket、thread、time |
| PSMX | src/core/ddsc/src/dds_psmx.c 等 | plugin/shared-memory exchange |
| 工具 | src/tools/ddsperf | 官方高吞吐真实使用方式 |

## 最值得观察的数据结构

固定提交里能看到 AVL tree、hash table、circular list、sequence-number 管理结构、动态数组、mutex + condition variable，以及接收缓冲区中的 bump allocation。

这些选择都可以从访问模式推导：Entity child 要稳定地址和有序查找，RHC instance 要按 key 快速定位，WaitSet attachment 规模小但动态变化，receive buffer 则希望在无分片无乱序的常见路径上几乎不做通用堆分配。

## 完成标准

读完整个专题后，应该能从源码回答：

~~~text
ROS 2 publish 为什么会走到 dds_write_ts？
dds_write 什么时候在调用线程 sendmsg？
Reliable 为什么要求 WHC 保留样本？
远端 Writer 怎样从 SEDP 变成 proxy_writer？
DATAFRAG 怎样经过 defrag / reorder？
Reader 为什么还需要自己的 RHC？
rmw_wait 为什么能被 DDS WaitSet 唤醒？
shutdown 为什么必须先停 I/O 再回收实体？
~~~

后续所有源码结论统一以 e54e991f 为 Cyclone DDS 本体真值。
