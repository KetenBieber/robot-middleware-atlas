按问题场景设计：从需求反推数据结构与 Runtime
==============================================

面对一条新的数据流，先回答：

* 数据是 state、event、history 还是 task；
* Producer / Consumer 分别是谁；
* 允许多旧、能否丢、能否阻塞；
* payload 位于哪个 memory domain；
* 失败、超时和 shutdown 后怎样恢复。

本组文章不是术语速查表。首次阅读建议按“最新状态 → 流式背压 → 控制安全 → 多线程 Runtime → 大对象 IPC → 网络 Runtime”的顺序推进；GPU/异构数据面放到这些基础之后。最后的“场景 × 机制矩阵”只用于复习和选型，不应该作为第一篇阅读。

代码示例遵守统一约定：

* 标成 ``cpp`` 的教学示例必须是完整的最小程序，包含必要类型、对象实例、线程创建和 ``main()``，读者不需要猜“这个变量到底属于谁”；
* 为了说明控制流而保留的伪代码统一标成 ``text``，不冒充可编译 C++；
* 从 libuv、Asio、Folly 等工程中截取的真实源码会明确标注“源码片段”，它依赖项目上下文，不宣称可以单独编译；
* 并发示例必须说明哪些线程共享同一个对象、谁拥有对象生命周期、停止和 ``join`` 怎样发生。

这样可以把三种东西分开：**可运行教学程序、概念伪代码、真实工程源码片段**。

首次阅读路线：

.. code-block:: text

   latest state / event / history
              ↓
   streaming / backpressure / data age
              ↓
   control safety / deadline / priority
              ↓
   task / worker / thread runtime
              ↓
   shared-memory IPC / ownership
              ↓
   network runtime / event loop
              ↓
   GPU / heterogeneous data plane（可选高级）
              ↓
   scenario mechanism matrix（复习索引）

设计过程可以抽象成：

.. code-block:: text

   场景
      ↓
   业务语义
      ↓
   时间 / 容量 / 可靠性约束
      ↓
   最简单实现
      ↓
   失败模式
      ↓
   候选机制空间
      ↓
   数据结构 + OS primitive + runtime topology
      ↓
   工业实现
      ↓
   方案切换条件

.. toctree::
   :maxdepth: 1

   scenario-latest-state-vs-event
   scenario-streaming-pipeline
   scenario-control-safety
   scenario-thread-runtime
   scenario-large-payload-ipc
   scenario-network-runtime-design
   scenario-distributed-gpu
   scenario-mechanism-matrix

:doc:`场景 × 机制矩阵 <scenario-mechanism-matrix>` 用于快速缩小候选范围；具体同步、容器和传输机制在 :doc:`解决方案空间 <solution-space>` 中展开。
