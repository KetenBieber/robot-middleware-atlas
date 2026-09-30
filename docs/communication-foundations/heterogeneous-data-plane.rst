跨越 Memory Domain：GPU / NPU / DMA / RDMA 数据面
=================================================

现代具身系统里，一帧数据可能长期不回 CPU。

Camera DMA、CUDA Tensor、NPU buffer、NIC RDMA registration 使“数据在哪里、谁能直接访问”成为接口语义的一部分。

.. toctree::
   :maxdepth: 1

   heterogeneous-memory

三个实现层次可以连续对应：

* :doc:`OpenUCX <../generated/ucx/index>`：memory 通过哪条 transport/lane 移动；
* :doc:`rosidl::Buffer / CUDA Buffer Backend <../generated/rosidlbuffer/index>`：消息 payload 怎样保持 GPU residency，并跨进程导出、映射和回收；
* :doc:`Holoscan <../generated/holoscan/index>`：allocator、queue、CUDA stream/event 和 transport 怎样进入 resource-aware graph runtime。

.. code-block:: text

   Memory Domain
      ↓
   transport capability
      ↓
   accelerator-backed message storage
      ↓
   graph/runtime scheduling

异构数据面仍然遵守 ownership、queue、backpressure、completion 与 failure 的基本约束，只是 payload 不再默认属于 CPU-addressable heap。
