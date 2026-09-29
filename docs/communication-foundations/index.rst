Communication Foundations：从线程到 GPU 的通信底层
==============================================================

这一章不是某一个中间件的 API 教程，而是一套用来拆任何通信系统的“底层坐标系”。

当机器人系统里写下一句 publish(msg) 时，真正发生的事情可能完全不同：同线程只是一次函数调用，同进程跨线程需要建立 happens-before，跨进程要解决虚拟地址与共享内存生命周期，跨主机要面对序列化、分片、可靠性和发现，而进入 GPU/NPU 后，问题又变成 memory domain、DMA 与异步同步。

因此这里不先背 ROS、DDS、Zenoh、iceoryx2 或 UCX 的名词，而是反复追问三条线：

- 数据线：payload 到底存在哪里，发生了几次 copy，跨过了哪些 memory domain？
- 控制线：谁通知谁，谁排队，谁唤醒线程，什么时候发生调度？
- 所有权线：谁可以写，谁可以读，什么时候可以复用，参与者死亡以后谁负责回收？

把这三条线画清楚，很多“zero-copy”“异步”“可靠”“实时”的宣传词才有可验证的含义。

推荐按下面顺序阅读。前两篇先建立线程时序与内存模型，随后专门进入并发队列、进展保证与可运行实验；接着再扩展到进程间共享内存、背压、分布式网络和 GPU/NPU/RDMA，最后把这些机制映射回 Atlas 中已经拆过的中间件。

这里有一个贯穿整章的目标：**不是只学“怎样造一个 middleware”，而是学会怎样组织程序中的数据流。** 同样的 SPSC ring、MPSC event queue、latest-value mailbox、reactor 和 bounded pool，可以出现在机器人中间件里，也可以直接出现在控制器、驱动、感知 pipeline、RTOS task 或 MCU 的 ISR → main-loop 数据通路里。

.. toctree::
   :maxdepth: 2

   ownership-address-space
   threads-memory-order
   concurrent-queues-progress
   thread-dataflow-lab
   processes-shared-memory
   queues-backpressure
   network-distributed
   heterogeneous-memory
   atlas-mapping
