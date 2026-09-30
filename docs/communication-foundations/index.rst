Communication & Runtime Design：从需求到机制
===============================================

机器人 Runtime 的通信问题可以从两个方向分析。

**按问题场景设计**

先定义数据语义、实时性、容量和故障边界，再反推 queue、ownership、scheduler、IPC 与 data plane。

**按解决方案选型**

横向比较 mutex、ring、MPSC、eventfd、WaitSet、shared memory、UCX、CUDA stream 等机制的保证、代价与适用边界。

两种分析都沿三条线展开：

* **数据线**：payload 在哪里、发生几次 copy、跨过哪些 memory domain；
* **控制线**：谁通知谁、谁排队、谁真正被 OS/GPU scheduler 调度；
* **所有权线**：谁能写、谁能读、什么时候可复用、异常退出后谁回收。

.. toctree::
   :maxdepth: 3

   problem-driven-design
   solution-space
