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
   * - 数据空间/边缘路由
     - Zenoh
     - pub/sub、query、storage 与跨网路由
     - 不是硬实时控制执行器

建议路径
--------

* 自动驾驶运行时：Cyber RT → eCAL → Zenoh。
* 硬实时控制：Orocos RTT → eCAL → LCM。
* 研究机器人平台：YARP → LCM → Zenoh。
* 学习完整可审计内核：先读 LCM，再与其他大型框架对照。

六个项目解决的不是同一个问题
----------------------------

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

eCAL 是更直接的起点：registration 让端点相遇，同机优先使用 SHM，跨主机再走 UDP/TCP。选择前仍要回答：大消息是否真的命中 SHM、慢订阅者如何影响 buffer/ACK、网络 fallback 是否满足既定可靠性语义。

Cyber RT 也有 SHM，但它通常与 Component、DataVisitor 和 Scheduler 一起出现。若只需要通用 IPC，单独引入整套 Apollo 运行时可能成本过高。

需要跨边缘的数据空间
^^^^^^^^^^^^^^^^^^^^

Zenoh 用同一 key expression 空间连接 publication、subscription、query、reply、liveliness 和 storage。它适合机器人本体—边缘—云之间的动态拓扑，但表达式路由、异步 pending state、Router 与 ACL 增加了控制面复杂度。

若需求只是固定局域网内的高速 channel，不需要 query/storage 或跨域路由，LCM/eCAL 可能更容易审计。

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
     - eCAL SHM、Cyber SHM、Zenoh SHM
     - “零复制”不等于全链无序列化和借用风险
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
     eCAL SHM / LCM / YARP
             |
        gateway + schema
             v
   边缘与云
     Zenoh Router / storage / query

这种组合需要在适配边界明确命名、类型、时间戳、顺序、重复、背压、认证和关闭。中间件叠加不会自动叠加优点；如果两个层都缓存、重试和重排，尾延迟与重复状态反而更难推理。

源码学习的推荐起点
------------------

* 第一次学习中间件内核：从 :doc:`LCM <generated/lcm/index>` 开始，完整走通 provider、协议、队列、类型与日志。
* 学习 C++ 组件和调度：对照 :doc:`Cyber RT <generated/cyber/index>` 与 :doc:`Orocos RTT <generated/orocos/index>`。
* 学习发现和多传输：阅读 :doc:`eCAL <generated/ecal/index>`，重点比较控制面与数据面。
* 学习可插拔协议与命名：阅读 :doc:`YARP <generated/yarp/index>` 的 PortCore、Protocol 和 Carrier。
* 学习 Rust 异步路由：阅读 :doc:`Zenoh <generated/zenoh/index>` 的 Session、Resource、Route cache 和 Query Final。

每个专题首页都按“功能需求 → 组件地图 → 源码主链 → 语言机制 → 设计取舍与性能 → 最小复刻 → 实际案例”给出入口。选择项目之后不必再从文件名猜阅读顺序。
