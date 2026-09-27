Tutorials
=========

.. contents:: Contents
   :depth: 2
   :local:

一条主线，两个内容分区
----------------------------------------

每个中间件首页包含源码解读与使用教程两个分区，但它们由同一条学习主线连接：

* **源码解读** 先建立功能和组件地图，再沿真实调用链下钻到对象所有权、队列、锁、协议字段、调度与资源回收，最后汇总取舍与最小复刻。
* **使用教程** 把这些内部机制落到环境、最小程序、部署、诊断、录制回放、过载和真实项目开发。

第一次接触某个项目时，先读功能介绍与组件地图，知道它解决什么问题以及各模块为何存在；随后走完源码主链和语言机制，再用基础教程验证 API 现象，最后阅读真实项目案例。若本机环境暂时不可用，也可以先完成源码与最小复刻设计，不必把安装问题当成理解架构的前置条件。

六类实现坐标
------------

* Apollo Cyber RT：自动驾驶组件运行时、协程调度与有界数据缓存。
* Orocos RTT：硬实时组件、Activity、ExecutionEngine 与 Port。
* YARP：研究机器人 Port、Protocol 与可插拔 Carrier。
* Eclipse eCAL：汽车级多传输 pub/sub 与共享内存。
* Eclipse Zenoh：边缘数据空间、声明、路由与背压。
* LCM：轻量 UDP 多播、分片、接收队列、类型与日志。

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

从最小单元到完整系统
--------------------

#. 先认识它解决的问题、典型部署形态与完整仓库地图。
#. 找到不可再拆的协议字段、消息对象、队列槽位或执行单元。
#. 用这些最小单元组成第一个单进程闭环，明确每个对象由谁创建和销毁。
#. 加入序列化、传输或调度，沿一条真实调用链观察数据表示如何变化。
#. 再加入发现、路由、背压、线程切换和关闭，使局部实现成为完整运行时。
#. 最后分析数据结构不变量、时间与空间复杂度、设计模式、失败路径和替代方案。

怎样判断一句话的边界
--------------------

源码能够直接说明的对象关系、分支和字段，正文会链接到固定提交的具体 symbol。
涉及性能和控制效果时，文章会把成立条件写在句子里：例如锁竞争“可能”形成尾延迟，
并不等于已经在所有硬件上观察到瓶颈；一个 priority 字段参与队列排序，也不等于优先级已经
传递到操作系统调度器。这样的限定属于技术解释本身，不再用内部标签打断阅读。
