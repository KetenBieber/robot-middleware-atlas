真实系统中的机制映射
======================

LCM、DDS、Cyber、iceoryx2、UCX、EtherCAT、Holoscan 等系统使用不同对象和 API，但底层仍可映射到同一组问题：

* 数据放在哪里；
* 谁拥有和回收；
* 使用什么 queue/container；
* 谁负责 wakeup；
* 谁获得 CPU/GPU；
* 资源耗尽以后如何 backpressure；
* shutdown 与 crash 后怎样收敛。

.. toctree::
   :maxdepth: 1

   atlas-mapping
   industry-runtime-cases
