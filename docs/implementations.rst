实现专题：运行时职责与数据路径
==================================

不同实现承担的系统职责并不相同。可以先按问题边界区分：执行调度、消息数据面、同机共享内存、异构传输，以及设备总线。

组件与执行运行时
----------------

:doc:`Apollo Cyber RT <generated/cyber/index>`、:doc:`Orocos RTT <generated/orocos/index>` 与 :doc:`NVIDIA Holoscan SDK <generated/holoscan/index>` 关注的是业务代码怎样获得 CPU/GPU，以及运行时怎样组织执行。

Cyber 适合研究消息驱动任务、CRoutine、Scheduler、Processor 和有界缓存如何组成大型感知—规划运行时；Orocos RTT 适合研究 Activity、ExecutionEngine、Operation 和 Port policy 怎样形成显式实时组件边界；Holoscan 则把问题推进到 GPU streaming runtime：FlowGraph、Condition、Event-Based Scheduler、per-worker ready queue、ThreadPool、Allocator、CUDA Stream 与分布式 UCX 怎样共同决定一帧 Tensor 的执行与寿命。

通用异步 Runtime 与网络库
--------------------------

:doc:`libuv <generated/libuv/index>`、:doc:`Asio <generated/asio/index>`、:doc:`Meta Folly <generated/folly/index>`、:doc:`Seastar <generated/seastar/index>` 与 :doc:`nginx <generated/nginx/index>` 关注的是另一类更基础的问题：大量 socket、Timer、blocking work 与连接状态怎样在 OS 执行上下文上组织成事件驱动 Runtime。

消息总线、发现与分布式数据空间
--------------------------------

:doc:`ROS1 Communication Runtime <generated/ros1/index>`、:doc:`ROS2 Communication Runtime <generated/ros2/index>`、:doc:`YARP <generated/yarp/index>`、:doc:`eCAL <generated/ecal/index>`、:doc:`LCM <generated/lcm/index>`、:doc:`ZeroMQ / libzmq <generated/libzmq/index>`、:doc:`Cyclone DDS <generated/cyclonedds/index>`、:doc:`Fast DDS <generated/fastdds/index>` 与 :doc:`Zenoh <generated/zenoh/index>` 都在解决“数据怎样从一个软件实体到另一个实体”，但承担的语义不同。

ROS1 适合把机器人通信的三条基本链一次看清：Master/XML-RPC 负责 graph discovery，TCPROS/UDPROS 负责节点间数据面，CallbackQueue/Spinner 负责把通信 readiness 转成业务执行；Nodelet 又展示同进程部署怎样改变 payload ownership 与复制成本。ROS2 则把这些职责重新分层到 rclcpp、rcl、RMW、DDS 与 Executor：distributed discovery + GraphCache 取代中心 Master，QoS 把数据语义带入 endpoint contract，WaitSet/CallbackGroup 把 middleware readiness 与业务执行解耦，IntraProcessManager 与 LoanedMessage 又提供两类不同的低复制路径。LCM 适合观察极简多播消息数据面；libzmq 适合观察 socket pattern 背后的消息 Runtime；eCAL/YARP 更强调运行时发现、连接与多 transport；DDS 把 discovery、QoS、History 和可靠性纳入标准模型；Zenoh 进一步把 pub/sub、query、storage 和跨网路由放在统一 key space 中。

同机 Zero-copy 与异构数据面
---------------------------

:doc:`iceoryx2 <generated/iceoryx2/index>`、:doc:`OpenUCX <generated/ucx/index>`、:doc:`rosidl::Buffer / CUDA Buffer Backend <generated/rosidlbuffer/index>` 与 Holoscan 的 data plane 位于不同抽象层。

iceoryx2 的核心问题是**共享 sample 的所有权协议**：shared-memory pool、PointerOffset、loan/borrow/reclaim、fan-out、backpressure 与进程死亡回收。

UCX 的核心问题是**一块 memory 应该通过哪条硬件路径移动**：Context/Worker/Endpoint、lane、memory domain、protocol selection、RDMA/GPU memory、request 与 progress。

rosidl::Buffer / CUDA Backend 研究“标准消息语义怎样绑定可替换的 GPU storage”，把 CUDA VMM、POSIX FD、SCM_RIGHTS、共享 endpoint registry、generation/refcount 与 CUDA event 组织成同机 accelerator IPC。

Holoscan 再把 queue capacity、GPU allocator、CUDA stream/event 和 UCX transport 编进 streaming graph。四者可以连成：

.. code-block:: text

   shared-memory ownership
      ↓
   heterogeneous transport
      ↓
   accelerator-backed message storage
      ↓
   application runtime

工业现场总线与设备数据面
------------------------

:doc:`IgH EtherCAT Master <generated/ethercat/index>` 与 :doc:`SOEM <generated/soem/index>` 面向设备侧周期通信，而不是通用 pub/sub。

这一层需要分析 PDO/process image、datagram、从站状态机、Distributed Clocks、WKC、NIC 和应用实时线程。它们可以与上层运行时或消息中间件组合，但职责边界不同。

统一分析坐标
------------

无论进入哪一个项目，都可以使用 :doc:`Communication Foundations <communication-foundations/index>` 的三条主线：

* **数据线**：payload 在哪里，复制了几次，跨过哪些 memory domain；
* **控制线**：谁通知谁，谁排队，谁真正唤醒和调度执行流；
* **所有权线**：谁能写、谁能读、什么时候可以复用，异常退出后谁负责回收。

.. toctree::
   :maxdepth: 2
   :hidden:

   generated/cyber/index
   generated/libuv/index
   generated/asio/index
   generated/folly/index
   generated/seastar/index
   generated/libzmq/index
   generated/ros1/index
   generated/ros2/index
   generated/nginx/index
   generated/orocos/index
   generated/yarp/index
   generated/ecal/index
   generated/lcm/index
   generated/cyclonedds/index
   generated/fastdds/index
   generated/zenoh/index
   generated/iceoryx2/index
   generated/ucx/index
   generated/rosidlbuffer/index
   generated/holoscan/index
   generated/ethercat/index
   generated/soem/index
