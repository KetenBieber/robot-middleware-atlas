按解决方案选型：Runtime Mechanism Atlas
========================================

同一个工程问题通常有多种实现机制。选型时需要同时比较：

* 语义：state、event、history、task；
* topology：SPSC、MPSC、SPMC、MPMC；
* progress：blocking、lock-free、wait-free；
* 时间：deadline、Data Age、WCET、tail latency；
* ownership：copy、move、loan、shared、pool；
* 边界：thread、process、host、device；
* failure：overflow、crash、disconnect、shutdown。

.. toctree::
   :maxdepth: 1

   mechanism-selection-atlas

底层机制按边界继续展开：

.. toctree::
   :maxdepth: 2

   local-runtime
   ipc-distributed
   heterogeneous-data-plane
   mapping-and-cases
