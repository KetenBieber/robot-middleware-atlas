实现专题：按机制而不是“工业 / 实验”分类
============================================

旧版导航曾把项目分成“工业级中间件”和“轻量与边缘中间件”。这类标签对源码学习帮助有限：同一个项目可能既服务工业部署，也可能只是某一层 building block；更重要的是，它们解决的根本问题并不相同。

这里改为按**运行时职责**理解各个实现。选择阅读顺序时，先问自己现在研究的是执行调度、消息数据面、共享内存、异构传输，还是设备总线。

组件与执行运行时
----------------

:doc:`Apollo Cyber RT <generated/cyber/index>` 与 :doc:`Orocos RTT <generated/orocos/index>` 的共同重点不是网络协议本身，而是“业务代码怎样获得 CPU”。

Cyber 适合研究消息驱动任务、CRoutine、Scheduler、Processor 和有界缓存如何组合成大型感知—规划运行时；Orocos RTT 更适合研究 Activity、ExecutionEngine、Operation 和 Port policy 怎样形成显式的实时组件边界。

消息总线、发现与分布式数据空间
--------------------------------

:doc:`YARP <generated/yarp/index>`、:doc:`eCAL <generated/ecal/index>`、:doc:`LCM <generated/lcm/index>`、:doc:`Cyclone DDS <generated/cyclonedds/index>`、:doc:`Fast DDS <generated/fastdds/index>` 与 :doc:`Zenoh <generated/zenoh/index>` 都在解决“数据怎样从一个软件实体到另一个实体”，但承担的语义差异很大。

LCM 适合观察最小网络数据面；eCAL/YARP 更强调运行时发现、连接与多 transport；DDS 把 discovery、QoS、History 和可靠性纳入标准模型；Zenoh 进一步把 pub/sub、query、storage 和跨网路由放在统一 key space 中。

同机 Zero-copy 与异构数据面
---------------------------

:doc:`iceoryx2 <generated/iceoryx2/index>` 与 :doc:`OpenUCX <generated/ucx/index>` 不应该用“轻量 / 重型”来区分。

iceoryx2 的核心问题是**共享 sample 的所有权协议**：shared-memory pool、PointerOffset、loan/borrow/reclaim、fan-out、backpressure 与进程死亡回收。

UCX 的核心问题是**一块 memory 应该通过哪条硬件路径移动**：Context/Worker/Endpoint、lane、memory domain、protocol selection、RDMA/GPU memory、request 与 progress。

两者都与具身系统的大图像、点云和 tensor 数据路径直接相关，但处在不同抽象层。

工业现场总线与设备数据面
------------------------

:doc:`IgH EtherCAT Master <generated/ethercat/index>` 与 :doc:`SOEM <generated/soem/index>` 面向的是设备侧周期通信，而不是通用 pub/sub。

这里需要追踪 PDO/process image、datagram、从站状态机、Distributed Clocks、WKC、NIC 和应用实时线程。它们可以与上面的运行时或消息中间件组合，但职责边界必须保持清楚。

阅读时始终回到同一套坐标
------------------------

无论进入哪一个项目，都可以继续使用 :doc:`Communication Foundations <communication-foundations/index>` 的三条主线：

* **数据线**：payload 在哪里，复制了几次，跨过哪些 memory domain；
* **控制线**：谁通知谁，谁排队，谁真正唤醒和调度执行流；
* **所有权线**：谁能写、谁能读、什么时候可以复用，异常退出后谁负责回收。

下面的隐藏目录只负责把所有实现页纳入 Sphinx 文档树，项目之间不再用“工业 / 实验”这种容易制造误解的标签划分。

.. toctree::
   :maxdepth: 2
   :hidden:

   generated/cyber/index
   generated/orocos/index
   generated/yarp/index
   generated/ecal/index
   generated/lcm/index
   generated/cyclonedds/index
   generated/fastdds/index
   generated/zenoh/index
   generated/iceoryx2/index
   generated/ucx/index
   generated/ethercat/index
   generated/soem/index
