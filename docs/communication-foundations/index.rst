Communication Foundations：从线程到 GPU 的通信底层
==============================================================

这部分不从某一个中间件出发，而是先建立一套可以反复复用的通信分析坐标系。

任何通信系统最终都要回答七个问题：payload 在哪里、谁拥有它、地址空间是否相同、谁通知谁、队列如何限制容量、过载时如何背压，以及参与者死亡后资源如何回收。

.. toctree::
   :maxdepth: 2

   ownership-address-space
   threads-memory-order
   processes-shared-memory
   queues-backpressure
   network-distributed
   heterogeneous-memory
   atlas-mapping
