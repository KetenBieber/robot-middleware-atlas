# UCX 总览：当一份 Tensor 可能在 CPU、GPU、共享内存或 RDMA 网卡旁边

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

机器人系统里的“通信”不总是 topic 到 topic。相机图像可能刚由 GPU 预处理完，视觉模型输出仍在 CUDA memory；另一块 GPU 需要它，或另一台机器要通过 RDMA 拉走它。此时真正昂贵的常常不是消息头，而是 **payload 位于哪里、是否注册、需要复制几次、哪个设备能够直接访问它**。

UCX 的切入点正是这个数据面问题。它不先定义一个完整的机器人 pub/sub 世界，而是把一次通信拆成两层：UCP 给应用 endpoint、tag、stream、RMA、Active Message、request；UCT 把高层动作落到具体 transport。共享内存、TCP、InfiniBand、CUDA IPC、ROCm 等因此不是几个完全独立的 API，而是候选数据路径。

## Memory type 是协议输入，而不是注释

固定源码把内存位置直接做成枚举：

~~~c
typedef enum ucs_memory_type {
    UCS_MEMORY_TYPE_HOST,
    UCS_MEMORY_TYPE_CUDA,
    UCS_MEMORY_TYPE_CUDA_MANAGED,
    UCS_MEMORY_TYPE_ROCM,
    UCS_MEMORY_TYPE_ROCM_MANAGED,
    UCS_MEMORY_TYPE_RDMA,
    UCS_MEMORY_TYPE_ZE_HOST,
    UCS_MEMORY_TYPE_ZE_DEVICE,
    UCS_MEMORY_TYPE_ZE_MANAGED,
    UCS_MEMORY_TYPE_GAUDI,
    UCS_MEMORY_TYPE_LAST,
    UCS_MEMORY_TYPE_UNKNOWN = UCS_MEMORY_TYPE_LAST
} ucs_memory_type_t;
~~~

这件事很关键。普通 socket API 通常只看到“地址 + 长度”；UCX 的协议选择还要知道这块地址属于 HOST 还是 CUDA、靠近哪个 system device、底层 Memory Domain 能不能 register/access。于是同一个发送操作，在不同机器拓扑和 buffer 位置上可以得到不同协议。

## 四层不是为了分目录，而是为了隔离变化

可以把 UCX 看成下面这条纵向链：

~~~text
Application / framework
        |
        v
UCP: endpoint / request / tag / AM / RMA / protocol selection
        |
        v
UCT: component / memory domain / iface / ep / transport operations
        |
        +---- shared memory
        +---- TCP
        +---- InfiniBand / RDMA
        +---- CUDA / ROCm / Level Zero
        |
        v
hardware + memory
~~~

UCS 提供容器、内存池、异步上下文、时间与系统工具；UCM 处理内存分配/映射事件相关机制。这个拆法让“协议策略”和“硬件 transport 实现”不会绑死：UCP 可以选择 rendezvous，而真正的 GET ZCOPY 由具体 UCT endpoint 实现。

## 一次发送真正要回答的四个问题

第一，peer endpoint 有哪些可用 lane。第二，buffer 是什么 datatype、memory type、system device。第三，在这个长度区间里 eager、zcopy、rendezvous、pipeline 谁的代价最低。第四，操作若没有立即完成，谁负责推进 request 到 completion。

第四点尤其容易被忽略。UCX 是显式 progress 驱动的系统之一。官方 hello world 的等待循环不是 sleep，而是持续调用 ucp_worker_progress。也就是说，通信完成时间不仅与网卡有关，还与 **应用多久让 progress engine 获得一次执行机会** 有关。

对于具身系统，这会直接变成数据年龄问题：感知线程忙于模型推理而长期不 progress，网络即使很快，完成事件也可能不能及时向上推进。UCX 因此很适合用来研究“异构内存 + transport + 进度模型”这一层，而不是把它简单归类为另一个消息总线。
