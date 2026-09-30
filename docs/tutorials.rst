Tutorials
=========

.. contents:: Contents
   :depth: 2
   :local:

分析一套 Runtime 时先回答五个问题
-----------------------------------

* **对象**：核心对象、最小数据单元和生命周期是什么；
* **数据**：payload 怎样表示、缓存、复制和跨边界移动；
* **并发**：哪些线程/协程读写哪些状态，使用什么 queue、lock、atomic 和 wakeup；
* **执行**：ready work 怎样经过 scheduler/executor 获得 CPU/GPU；
* **失败**：过载、断连、进程退出和 shutdown 时状态怎样收敛。

十二类实现坐标
----------------

* Apollo Cyber RT：自动驾驶组件运行时、协程调度与有界数据缓存。
* Orocos RTT：硬实时组件、Activity、ExecutionEngine 与 Port。
* YARP：研究机器人 Port、Protocol 与可插拔 Carrier。
* Eclipse eCAL：汽车级多传输 pub/sub 与共享内存。
* Eclipse Zenoh：边缘数据空间、声明、路由与背压。
* LCM：轻量 UDP 多播、分片、接收队列、类型与日志。
* IgH EtherCAT Master：工业实时以太网主站、PDO 过程映像、datagram、从站状态机与周期收发。
* SOEM：轻量用户态 EtherCAT MainDevice Library，Context、固定 frame slot、IOmap、RAW Socket 与 OSAL/OSHW。
* Eclipse Cyclone DDS：DDS/RTPS、自动发现、QoS、可靠性、History Cache、WaitSet 与 ROS 2 RMW。
* eProsima Fast DDS：DDS/RTPS、CacheChange/History、Discovery Server、FlowController、Data Sharing/SHM 与 ROS 2 RMW。
* Eclipse iceoryx2：Rust zero-copy IPC、SharedMemory、PointerOffset、loan/reclaim、backpressure 与 crash recovery。
* OpenUCX：异构高性能数据面、UCP/UCT、lane/protocol selection、RDMA/GPU memory 与显式 progress。

项目入口
--------

.. list-table::
   :header-rows: 1
   :widths: 24 38 38

   * - 项目
     - 使用主线
     - 源码主线
   * - :doc:`Apollo Cyber RT <generated/cyber/index>`
     - 环境工具、Node Pub/Sub、Component/DAG/Launch
     - Transport、Dispatcher、DataVisitor、CRoutine、Scheduler
   * - :doc:`Eclipse eCAL <generated/ecal/index>`
     - SDK/CMake、String/Protobuf、Monitor 与录制回放
     - Discovery、PubGate/SubGate、SHM、UDP/TCP、生命周期
   * - :doc:`Eclipse Zenoh <generated/zenoh/index>`
     - C++ 后端、Pub/Sub/Query、Router/ACL
     - Session、Face、Resource tree、Route cache、Final 与关闭
   * - :doc:`LCM <generated/lcm/index>`
     - Provider、类型生成、事件循环、日志与丢包诊断
     - Provider vtable、UDPM、分片重组、订阅分发与类型指纹
   * - :doc:`Orocos RTT <generated/orocos/index>`
     - Deployer、TaskContext、Activity、Port policy 与实时检查
     - ExecutionEngine、Operation 线程模型、ChannelElement 与关闭
   * - :doc:`YARP <generated/yarp/index>`
     - Name Server、BufferedPort、RPC、Carrier 与管理工具
     - PortCore、Unit、Protocol、Carrier、扇出与并发关闭
   * - :doc:`IgH EtherCAT Master <generated/ethercat/index>`
     - Master/Domain/PDO 配置、周期收发、状态监测与 DC
     - process image、datagram、FSM、device/NIC、实时与关闭边界

   * - :doc:`SOEM <generated/soem/index>`
     - Context、IOmap、RAW Socket、frame index、PDO/FMMU、WKC/DC 与应用侧实时线程
     - 真实案例：官方 ec_sample、ETH RSL soem_interface、Elfin ROS2、IPE ros2_control/CiA-402
   * - :doc:`Eclipse Cyclone DDS <generated/cyclonedds/index>`
     - DDS Entity、QoS、WaitSet、ddsperf 与 ROS 2 rmw_cyclonedds
     - SPDP/SEDP、WHC/RHC、RTPS/UDP、defrag/reorder、线程/PSMX/loan
   * - :doc:`eProsima Fast DDS <generated/fastdds/index>`
     - DDS Entity、QoS、Discovery Server、官方 delivery_mechanisms 与 ROS 2 rmw_fastrtps
     - CacheChange/History、StatefulWriter/Reader、FlowController、UDP/TCP/SHM、Data Sharing/loan
   * - :doc:`Eclipse iceoryx2 <generated/iceoryx2/index>`
     - Node/Service/Publisher/Subscriber、共享内存、loan 与 WaitSet
     - PointerOffset、ZeroCopyConnection、fan-out/backpressure、borrow/reclaim 与 dead-node cleanup
   * - :doc:`OpenUCX <generated/ucx/index>`
     - UCP Endpoint、Tag/AM/RMA、request、busy progress 与 eventfd wakeup
     - UCT transport、Memory Domain、lane/protocol selection、Rendezvous 与 GPU/异构内存路径

从最小单元到完整系统
--------------------

#. 先认识它解决的问题、典型部署形态与完整仓库地图。
#. 找到不可再拆的协议字段、消息对象、队列槽位或执行单元。
#. 用这些最小单元组成第一个单进程闭环，明确每个对象由谁创建和销毁。
#. 加入序列化、传输或调度，沿一条真实调用链观察数据表示如何变化。
#. 再加入发现、路由、背压、线程切换和关闭，使局部实现成为完整运行时。
#. 最后分析数据结构不变量、时间与空间复杂度、设计模式、失败路径和替代方案。
