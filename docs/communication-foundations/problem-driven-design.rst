按问题场景设计：从需求反推数据结构与 Runtime
==============================================

面对一条新的数据流，先回答：

* 数据是 state、event、history 还是 task；
* Producer / Consumer 分别是谁；
* 允许多旧、能否丢、能否阻塞；
* payload 位于哪个 memory domain；
* 失败、超时和 shutdown 后怎样恢复。

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
