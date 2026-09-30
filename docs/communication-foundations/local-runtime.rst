同一地址空间：从普通对象到并发 Runtime
==========================================

同一进程内，线程共享虚拟地址，但不会自动共享正确的执行顺序。

技术依赖关系是：

.. code-block:: text

   object ownership
      ↓
   happens-before / memory order
      ↓
   queue semantics / overload policy
      ↓
   SPSC / MPSC / MPMC implementation
      ↓
   wakeup / executor / shutdown

.. toctree::
   :maxdepth: 1

   ownership-address-space
   threads-memory-order
   queues-backpressure
   concurrent-queues-progress
   thread-dataflow-lab

一个典型本地数据流可以同时使用多种通信语义：

.. code-block:: text

   driver thread
      ↓ SPSC
   estimator
      ↓ latest-value mailbox
   controller
      ↓ MPSC event/command queue
   supervisor
