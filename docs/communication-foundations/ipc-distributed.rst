跨越地址空间：进程 IPC 与分布式协议
======================================

跨进程以后，地址、对象寿命和故障边界都发生变化。

同机进程可以映射同一批物理页，却拥有不同虚拟地址和 allocator 生命周期，因此 shared memory 仍然需要 offset/handle、pool、descriptor、notification 与 crash recovery。

跨主机以后，共享物理页也消失，通信继续增加 serialization、framing、reliability、discovery、routing、partial failure 与时钟边界。

.. code-block:: text

   same host:
   payload pool + descriptor + notification

   cross host:
   serialized representation + protocol state + transport notification

.. toctree::
   :maxdepth: 1

   processes-shared-memory
   network-distributed
