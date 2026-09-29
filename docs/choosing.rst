Choosing a middleware project
=============================

.. contents:: Contents
   :depth: 2
   :local:

先区分中间件类型
----------------

.. list-table::
   :header-rows: 1
   :widths: 20 25 30 25

   * - 类型
     - 代表
     - 核心问题
     - 不负责
   * - 组件运行时
     - Cyber RT、Orocos RTT
     - 生命周期、线程与任务调度
     - 不等于单一网络协议
   * - 通信抽象
     - YARP、eCAL、LCM
     - 发现、序列化、缓存与传输
     - 通常不提供完整控制调度
   * - Zero-copy IPC
     - Eclipse iceoryx2
     - 同机共享内存、offset descriptor、loan/reclaim 与 crash recovery
     - 不直接解决跨主机路由与完整分布式 QoS
   * - 异构高性能数据面
     - OpenUCX
     - Endpoint lane、RDMA/GPU memory、protocol selection 与 progress
     - 不提供完整 Topic/Discovery/QoS 或业务计算图调度
   * - DDS/RTPS 数据总线
     - Eclipse Cyclone DDS、eProsima Fast DDS
     - Participant/Endpoint 发现、QoS、可靠性、History 与 WaitSet
     - 不负责 ROS 2 Executor 的业务调度，也不等于硬实时控制环
   * - 数据空间/边缘路由
     - Zenoh
     - pub/sub、query、storage 与跨网路由
     - 不是硬实时控制执行器
   * - 实时工业总线/主站栈
     - IgH EtherCAT Master、SOEM
     - 周期过程数据、从站状态机、时钟同步与网卡数据面
     - 不是通用进程间消息总线

建议路径
--------

* 自动驾驶运行时：Cyber RT → eCAL → Zenoh。
* 硬实时控制：Orocos RTT → eCAL → LCM。
* 研究机器人平台：YARP → LCM → Zenoh。
* ROS 2 通信底层：先对照 Cyclone DDS 与 Fast DDS 的 RMW、History、可靠性、WaitSet 和共享内存，再与 Executor 调度分层分析。
* 学习完整可审计内核：先读 LCM，再与其他大型框架对照。
* 学习同机共享内存 IPC：Communication Foundations → iceoryx2 → eCAL/DDS 共享内存路径。
* 学习跨 CPU/GPU/网络的异构数据面：Communication Foundations → UCX → dataflow runtime。
* 工业伺服与 I/O 实时链路：Orocos RTT / 专用 RT loop → EtherCAT Master → 驱动与从站对象字典。

十个中间件与两种 EtherCAT 主站实现解决的不是同一个问题
------------------------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 14 17 17 17 17 18

   * - 项目
     - 首要抽象
     - 谁执行业务代码
     - 主要拓扑机制
     - 最突出的能力
     - 需要额外补齐
   * - Cyber RT
     - Component / Node
     - Scheduler 的 CRoutine
     - DAG、Topology、Transport
     - 自动驾驶计算图与协程调度
     - 跨域安全、算法 WCET 约束
   * - Orocos RTT
     - TaskContext
     - 显式 Activity / Engine
     - Deployment 与 Port connection
     - 生命周期和实时执行边界
     - 广域发现、现代网络生态
   * - YARP
     - Port
     - Unit、应用调用或回调线程
     - Name Server 与 Carrier
     - 运行时连线和异构机器人集成
     - 硬实时保证、大规模连接优化
   * - eCAL
     - Publisher / Subscriber
     - transport callback 与应用 worker
     - soft-state registration
     - 同机 SHM 与多传输自动选择
     - 严格业务确认与安全域
   * - Cyclone DDS
     - Participant / Writer / Reader
     - 应用调用线程、DDS receive/delivery/sendq 与上层 Executor
     - SPDP/SEDP、RTPS endpoint matching
     - 标准 DDS QoS、可靠性、WHC/RHC 与 ROS 2 RMW 生态
     - 业务级 ACK、Executor WCET 与硬实时调度边界
   * - Fast DDS
     - DomainParticipant / DataWriter / DataReader
     - 应用写线程、Transport receiver、ResourceEvent/FlowController 与上层 Executor
     - PDP/EDP、Discovery Server、RTPS endpoint matching
     - CacheChange/History、FlowController、SHM/Data Sharing/loan 与 rmw_fastrtps
     - 业务级 ACK、共享内存命中条件、Executor WCET 与硬实时调度边界
   * - iceoryx2
     - Node / Service / Publisher / Subscriber
     - 应用线程直接 loan/send/receive，事件等待由 WaitSet/Reactor 驱动
     - Service static/dynamic state 与 endpoint connection
     - SharedMemory + PointerOffset + ZeroCopyConnection 的 zero-copy IPC
     - 跨主机路由、完整 DDS QoS 与应用计算调度
   * - OpenUCX
     - Context / Worker / Endpoint / Request
     - 应用线程或通信线程显式驱动 Worker progress
     - Wireup address exchange、Endpoint lane 与 transport capability matching
     - SHM/TCP/RDMA/GPU transport、memory type 与协议自动选择
     - Topic/Schema/Discovery/QoS 与完整业务调度
   * - Zenoh
     - key expression / Session
     - 异步 runtime 与 handler
     - 声明、Face、Router、scouting
     - pub/sub + query + storage 数据空间
     - 硬实时执行与业务 schema
   * - LCM
     - channel / provider
     - 调用 ``handle`` 的应用线程
     - 无中心 UDPM 或其他 provider
     - 极小内核、类型生成与日志回放
     - 可靠交付、认证和全局发现
   * - IgH EtherCAT Master
     - Master / Domain / PDO
     - 应用实时线程 + 主站/device 执行路径
     - EtherCAT 线性总线、从站状态与 DC
     - 周期过程数据与工业伺服链路
     - 上层组件调度、通用消息语义
   * - SOEM
     - ``ecx_contextt`` / IOmap
     - 应用自己的周期线程直接调用 SOEM Library
     - EtherCAT 线性总线、用户态 RAW Socket
     - 轻量、可移植、源码短且易嵌入控制进程
     - 实时调度、故障策略与线程组织更多由应用承担

这张表不能替代容量与故障分析。例如 Cyber 和 Orocos 都能执行组件，但前者强调大型数据流和协程调度，后者强调 Activity、Port policy 与可推理的实时组件生命周期。eCAL 和 LCM 都能做发布订阅，但 eCAL 用发现与多 transport 承担更多运行时工作，LCM 则把事件循环、丢包恢复和应用确认更多留给使用者。

按功能需求选择
--------------

需要组件运行时
^^^^^^^^^^^^^^

若系统需要由配置装配业务组件，并让框架决定线程、周期或协程，先比较 Cyber RT 与 Orocos RTT。

* 自动驾驶或高吞吐感知—规划图，已有 Apollo 生态：选择 Cyber RT。
* 控制组件需要明确 Activity、Operation 执行线程和 Port 缓冲策略：选择 Orocos RTT。
* 仅仅需要消息通信时，不必为了 Component/TaskContext 引入完整执行运行时。

需要同机大数据 IPC
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

iceoryx2 是这一问题最适合从第一性原理进入的专题：Publisher 从共享 DataSegment 直接 loan chunk，连接中传递 PointerOffset，Subscriber 将 offset 翻译成本地映射地址，Sample 释放后再把 offset 归还给 Publisher reclaim。它尤其适合研究大图像、点云和同机感知流水线中的 ownership、fan-out、背压与进程崩溃恢复。

eCAL 是更直接的起点：registration 让端点相遇，同机优先使用 SHM，跨主机再走 UDP/TCP。选择前仍要回答：大消息是否真的命中 SHM、慢订阅者如何影响 buffer/ACK、网络 fallback 是否满足既定可靠性语义。

Cyber RT 也有 SHM，但它通常与 Component、DataVisitor 和 Scheduler 一起出现。若只需要通用 IPC，单独引入整套 Apollo 运行时可能成本过高。

Cyclone DDS 与 Fast DDS 都适合需要 DDS/RTPS 互操作或 ROS 2 RMW 的系统。Cyclone DDS 通过 PSMX/loan/local delivery 优化同机路径；Fast DDS 则需要分清 SHM Transport、Data Sharing 和 loan_sample 三层机制。无论哪一个实现，是否真正命中共享内存、类型是否可借用、是否同时服务网络 Reader，都必须从实际 endpoint 与数据路径核对。

需要跨边缘的数据空间
^^^^^^^^^^^^^^^^^^^^

Zenoh 用同一 key expression 空间连接 publication、subscription、query、reply、liveliness 和 storage。它适合机器人本体—边缘—云之间的动态拓扑，但表达式路由、异步 pending state、Router 与 ACL 增加了控制面复杂度。

若需求只是固定局域网内的高速 channel，不需要 query/storage 或跨域路由，LCM/eCAL 可能更容易审计。

需要跨 CPU、GPU 与 RDMA 的大数据面
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

OpenUCX 适合把大 tensor、图像或其他 buffer 的物理位置纳入通信决策。它先为 peer 建立多条 lane，再以 operation、message length、memory type、system device 等信息选择 eager、zcopy、rendezvous 或具体 UCT transport。它尤其适合研究 GPU memory、RDMA registration、multi-lane 与显式 progress，但需要上层另行提供 topic/discovery/schema、业务 QoS 与计算图调度。

需要最小、可读、可回放的总线
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

LCM 的 provider vtable、UDPM framing、接收 ring、通知 pipe、订阅 dispatch 和 event log 可以在较小代码面内完整追踪。它适合受控局域网、实验机器人、仿真和教学，也适合在应用明确承担 ACK、安全与过载策略时投入工程使用。

需要运行时可重连的研究机器人网络
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

YARP 的 Port、Name Server 和 Carrier 允许实验期间动态改变连接，并能通过不同 Carrier 适配 TCP、UDP、local 等路径。它对 iCub 一类多模块研究平台非常自然。连接数、每连接工作单元、后台写所有权和动态管理权限则需要专项评估。

按非功能需求排除
----------------

.. list-table::
   :header-rows: 1
   :widths: 24 38 38

   * - 约束
     - 可以优先考察
     - 不能直接推断
   * - 微秒级硬实时控制
     - Orocos RTT，或独立实时环 + 其他中间件
     - 使用 SHM、Rust 或 lock-free 不等于有 WCET
   * - 大图像/点云同机吞吐
     - iceoryx2、eCAL SHM、Cyber SHM、Zenoh SHM、Cyclone DDS PSMX/loan、Fast DDS Data Sharing/SHM/loan
     - “零复制”不等于全链无序列化和借用风险
   * - 跨 GPU / NIC / 主机的大 Tensor
     - OpenUCX，或在更高层 dataflow runtime 中使用 UCX 类数据面
     - RDMA/GPU Direct 能力不等于自动获得最优调度或业务 QoS
   * - 不允许丢命令
     - 任一底层 + 应用 request id/ACK/幂等
     - TCP、Reliable 或 FIFO 不等于动作已执行
   * - 动态跨网路由与 ACL
     - Zenoh Router
     - key 可见或 transport 加密不等于权限规则完整
   * - 确定性仿真回放
     - LCM + Drake、Cyber Record、eCAL measurement
     - 保存消息不等于保存配置、时钟和二进制版本
   * - 运行时重新连线
     - YARP、Zenoh、eCAL discovery
     - 名字存在不等于链路或数据新鲜

常见组合而不是单选
------------------

工业机器人系统经常把执行运行时和通信总线分层组合：

.. code-block:: text

   实时控制进程
     Orocos TaskContext / 专用 RT loop
             |
        bounded adapter
             v
   机器内部数据总线
     iceoryx2 / eCAL SHM / Cyclone DDS / Fast DDS / LCM / YARP
             |
       大 Tensor 数据面
          OpenUCX
             |
        gateway + schema
             v
   边缘与云
     Zenoh Router / storage / query

这种组合需要在适配边界明确命名、类型、时间戳、顺序、重复、背压、认证和关闭。中间件叠加不会自动叠加优点；如果两个层都缓存、重试和重排，尾延迟与重复状态反而更难推理。

EtherCAT Master 应放在另一条更靠近设备的数据路径上：控制线程通过 process image 读写 PDO，主站负责把这些字节转换成周期 EtherCAT 帧并与从站状态机、邮箱协议和分布式时钟协同。它可以与 Orocos、ROS 2 或自研控制框架组合，但不能把 EtherCAT 的周期通信能力直接等同于上层组件调度或消息语义。

源码学习的推荐起点
------------------

* 第一次学习中间件内核：从 :doc:`LCM <generated/lcm/index>` 开始，完整走通 provider、协议、队列、类型与日志。
* 学习线程、进程、跨主机与异构内存的统一通信模型：先读 :doc:`Communication Foundations <communication-foundations/index>`，再进入具体实现。
* 学习同机 zero-copy ownership：阅读 :doc:`iceoryx2 <generated/iceoryx2/index>`，沿 SharedMemory、PointerOffset、loan/send/receive/reclaim、backpressure 与 dead-node cleanup 走完整链。
* 学习异构高性能数据面：阅读 :doc:`OpenUCX <generated/ucx/index>`，沿 Context/Worker/Endpoint、lane selection、request/protocol selection、progress、Memory Domain 与 rendezvous 走完整链。
* 学习 C++ 组件和调度：对照 :doc:`Cyber RT <generated/cyber/index>` 与 :doc:`Orocos RTT <generated/orocos/index>`。
* 学习发现和多传输：阅读 :doc:`eCAL <generated/ecal/index>`，重点比较控制面与数据面。
* 学习可插拔协议与命名：阅读 :doc:`YARP <generated/yarp/index>` 的 PortCore、Protocol 和 Carrier。
* 学习 Rust 异步路由：阅读 :doc:`Zenoh <generated/zenoh/index>` 的 Session、Resource、Route cache 和 Query Final。
* 学习 ROS 2 DDS/RTPS 底层：阅读 :doc:`Cyclone DDS <generated/cyclonedds/index>`，沿 SPDP/SEDP、QoS matching、WHC/RHC、RTPS、WaitSet 一路追到 rmw_cyclonedds。
* 学习另一套 ROS 2 DDS/RTPS 实现：阅读 :doc:`Fast DDS <generated/fastdds/index>`，重点对照 CacheChange/History、StatefulWriter/Reader、Discovery Server、FlowController、Data Sharing/SHM/loan 与 rmw_fastrtps。
* 学习工业实时主站：从 :doc:`IgH EtherCAT Master <generated/ethercat/index>` 的周期数据路径开始，随后进入 Domain/process image、datagram/FSM、device/NIC 与 DC。
* 学习轻量用户态 EtherCAT 主站：继续读 :doc:`SOEM <generated/soem/index>`，重点比较 Context/固定数组、RAW Socket、IOmap 与 frame-index 数据面如何替代 IgH 的内核对象图。

每个专题首页都按“功能需求 → 组件地图 → 源码主链 → 语言机制 → 设计取舍与性能 → 最小复刻 → 实际案例”给出入口。选择项目之后不必再从文件名猜阅读顺序。
