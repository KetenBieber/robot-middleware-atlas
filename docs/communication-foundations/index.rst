Communication Foundations：从线程到 GPU 的通信底层
==============================================================

这一章不是某一个中间件的 API 教程，而是一套用来拆任何通信系统的“底层坐标系”。

当机器人系统里写下一句 publish(msg) 时，真正发生的事情可能完全不同：同线程只是一次函数调用，同进程跨线程需要建立 happens-before，跨进程要解决虚拟地址与共享内存生命周期，跨主机要面对序列化、分片、可靠性和发现，而进入 GPU/NPU 后，问题又变成 memory domain、DMA 与异步同步。

因此这里不先背 ROS、DDS、Zenoh、iceoryx2 或 UCX 的名词，而是反复追问三条线：

- 数据线：payload 到底存在哪里，发生了几次 copy，跨过了哪些 memory domain？
- 控制线：谁通知谁，谁排队，谁唤醒线程，什么时候发生调度？
- 所有权线：谁可以写，谁可以读，什么时候可以复用，参与者死亡以后谁负责回收？

把这三条线画清楚，很多“zero-copy”“异步”“可靠”“实时”的宣传词才有可验证的含义。

推荐按下面顺序阅读。前四篇建立 CPU 侧通信机制，第五篇扩展到分布式系统，第六篇进入 GPU/NPU/RDMA，最后一篇再把这些机制映射回 Atlas 中已经拆过的中间件。

.. toctree::
   :maxdepth: 2

   ownership-address-space
   threads-memory-order
   processes-shared-memory
   queues-backpressure
   network-distributed
   heterogeneous-memory
   atlas-mapping
