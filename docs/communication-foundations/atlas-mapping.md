# Atlas 映射表：现有中间件分别在通信栈哪一层做了什么

有了统一坐标系，就可以避免“所有项目都是 pub/sub，所以它们差不多”的错觉。

## 一张总表

| 项目 | 主要边界 | Payload 机制 | 通知/调度 | 过载机制 |
| --- | --- | --- | --- | --- |
| LCM | 跨主机/进程 | serialization + UDP datagram | receive thread + notify pipe + handle thread | bounded receive queue / network loss |
| eCAL | 同机 + 跨机 | SHM / UDP / TCP | transport callbacks | SHM rotation、ACK/queue、network buffers |
| Cyber RT | 线程/进程 + runtime | intra / SHM / RTPS | Dispatcher → Notifier → CRoutine Scheduler | pending queue / cache |
| YARP | 进程/跨主机 | Carrier transport | per-connection Unit / callback | carrier / background write policy |
| Zenoh | 分布式 | transport + routed key space | async runtime | bounded channels / congestion control |
| Cyclone DDS | 同机 + 跨机 | PSMX/local/RTPS | receive/delivery/sendq + WaitSet | WHC/RHC/QoS |
| Fast DDS | 同机 + 跨机 | Data Sharing / SHM / RTPS | receiver / FlowController / WaitSet | History/ResourceLimits/backpressure |
| iceoryx2 | 同机进程间 | shared memory + PointerOffset | zero-copy connection / event / reactor | buffer、discard/retry、safe overflow |
| IgH / SOEM | 主机↔设备 | process image + EtherCAT frame | application RT loop + NIC | fixed cycle / WKC / device state |

## 为什么要把线程、进程和分布式分开

同样写 publish(msg)，背后的成本可能完全不同。

### Intra-process

~~~text
pointer/reference
→ queue
→ worker
~~~

### Shared-memory IPC

~~~text
loan shared chunk
→ descriptor/offset
→ other process maps same pages
~~~

### Network

~~~text
serialize
→ packetize
→ kernel/NIC/network
→ parse/reassemble
~~~

### Distributed routing

~~~text
serialize
→ transport
→ router state
→ possibly multiple hops
→ remote endpoint
~~~

所以 API 表面的一致性不能替代数据路径分析。

## 推荐阅读顺序

如果目标是理解通信底层，而不是某个产品：

~~~text
Communication Foundations
↓
LCM
  学最小网络数据面
↓
iceoryx2
  学共享内存 ownership
↓
eCAL / DDS
  学多传输与生产级控制面
↓
Zenoh
  学分布式路由
↓
UCX
  学 CPU/GPU/RDMA unified transport
~~~

## 对具身智能最重要的连接

未来整套 Atlas 会逐渐形成：

~~~text
Communication Foundations
       │
       ├─ Threads / Queues
       ├─ Shared Memory IPC
       ├─ Network / Distributed
       └─ Heterogeneous Memory
              │
              ▼
      iceoryx2 / UCX
              │
              ▼
       GXF / Holoscan
              │
              ▼
      GPU perception / VLA
~~~

这时中间件不再是孤立工具，而是具身运行时基础设施的一部分。
