# UCT Transport 模型：怎样用一张 VTable 统一 SHM、TCP、RDMA 与 GPU

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

UCP 要做自动协议选择，前提是底层 transport 必须用同一种语言描述自己的能力。否则 TCP 有一套接口、IB 有一套接口、CUDA IPC 又有一套接口，高层最终只能写满 if/else。

UCT 的解决方案是 Component → Memory Domain → Interface → Endpoint。

~~~text
Component
  |
  +-- Memory Domain (MD)
  |     registration / allocation / rkey
  |
  +-- Interface (iface)
        progress / event / capability
        |
        +-- Endpoint (ep) -> peer
~~~

## iface ops 就是 transport 的“虚函数表”

固定源码中的 uct_iface_ops_t 把 transport 能做的动作列成函数指针：

~~~c
typedef struct uct_iface_ops {
    uct_ep_put_short_func_t       ep_put_short;
    uct_ep_put_bcopy_func_t       ep_put_bcopy;
    uct_ep_put_zcopy_func_t       ep_put_zcopy;

    uct_ep_get_short_func_t       ep_get_short;
    uct_ep_get_bcopy_func_t       ep_get_bcopy;
    uct_ep_get_zcopy_func_t       ep_get_zcopy;

    uct_ep_am_short_func_t        ep_am_short;
    uct_ep_am_bcopy_func_t        ep_am_bcopy;
    uct_ep_am_zcopy_func_t        ep_am_zcopy;

    uct_ep_tag_eager_short_func_t ep_tag_eager_short;
    uct_ep_tag_eager_bcopy_func_t ep_tag_eager_bcopy;
    uct_ep_tag_eager_zcopy_func_t ep_tag_eager_zcopy;

    uct_ep_pending_add_func_t     ep_pending_add;
    uct_ep_flush_func_t           ep_flush;

    uct_iface_progress_func_t     iface_progress;
    uct_iface_event_fd_get_func_t iface_event_fd_get;
    uct_iface_event_arm_func_t    iface_event_arm;
} uct_iface_ops_t;
~~~

C 没有 virtual，但这就是典型 runtime polymorphism。UCP 不需要知道某个 ep 是 mlx5、TCP 还是 cuda_ipc；只要 capability 宣告与函数表一致，就能用统一入口调用。

## short、bcopy、zcopy 是三种数据搬运契约

short 适合很小的 payload：数据通常直接嵌入发送描述符，固定开销最低。bcopy 让 transport 通过 pack callback 把用户数据复制到内部 buffer，调用者可以更早释放原 buffer。zcopy 则让 transport 直接操作用户 buffer，通常需要 registration 和 completion。

因此“zero-copy 一定最快”并不成立。小消息为了避免 registration/completion 开销，short 或 bcopy 反而可能更划算。大消息才更容易摊薄 zcopy 的准备成本。

这也是 UCP 为什么需要 protocol performance model：底层只提供 primitives，高层根据长度、memory type、设备拓扑组合它们。

## MD 与 Interface 为什么要分开

Memory Domain 关注“这块内存能否被这个设备注册/访问”；Interface 关注“通信怎样 progress、有哪些 endpoint operation”。一块 IB 设备内存的 registration 生命周期可能比某条 peer endpoint 长得多，所以不能把注册状态直接塞进连接对象。

如果用 C++ 设计同类系统，最容易犯的错误是创建一个 TransportConnection 类，把 device、registered memory、peer、progress thread、send queue 全部放进去。UCX 的拆法更接近资源本身的生命周期：MD 管内存能力，Iface 管设备通信上下文，EP 管 peer。

UCT 因而不是“很多驱动的共同父类”这么简单。它给 UCP 提供的是一套 **可枚举、可比较、可组合的 transport capability algebra**，自动选路才有可能成立。
