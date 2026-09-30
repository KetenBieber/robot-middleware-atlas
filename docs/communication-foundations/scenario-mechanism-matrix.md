# 场景 × 机制矩阵：从工程问题快速跳到候选设计

同一种“通信”需求可能对应完全不同的数据结构与 OS primitive。先用数据语义、时间约束、边界和 failure semantics 缩小候选范围，再进入具体实现。

| 场景 | 数据语义 | 优先候选 | 常见反模式 | 真实案例 |
| --- | --- | --- | --- | --- |
| Estimator 200 Hz → Controller 1 kHz | latest state | versioned mailbox / double buffer / seqlock | unbounded FIFO 保存所有历史 | Apollo latest auxiliary state、控制程序 |
| IMU driver → estimator | ordered high-rate stream | SPSC bounded ring | generic MPMC queue | 驱动/控制 runtime |
| 多 callback → supervisor | events | bounded MPSC | 多线程直接共享 vector/deque 无协议 | Cyber/普通机器人 supervisor |
| Emergency Stop | critical event | dedicated/priority path + bounded state | 与日志/普通 task 共用 backlog queue | 工业控制安全链 |
| Camera 60 Hz → Perception 25 Hz | freshness stream | small bounded queue + drop-old/latest | reliable unbounded FIFO | Holoscan video pipeline |
| Logger 多来源写盘 | loss-tolerant events | MPSC + batching | 每个 producer 同步写磁盘 | logging runtime |
| 多 worker task runtime | runnable tasks | per-worker queue + optional stealing | 单全局 hot queue | Holoscan EventBasedScheduler |
| Executor 等多类事件 | readiness | eventfd/epoll/WaitSet | 高频 sleep polling | DDS WaitSet、UCX arm |
| 大图像跨进程 | large shared payload | SHM pool + descriptor + notification | serialize/copy 整帧 | iceoryx2 / DDS SHM |
| GPU Tensor 同机跨模块 | device-resident payload | tensor handle + stream/event + pool | D2H→CPU message→H2D | Holoscan |
| GPU/NPU Buffer 同机跨进程 | accelerator-resident payload | stable slot + generation + capability broker + fence | 直接传 pointer/fd 整数 | rosidl::Buffer CUDA VMM / QC dma-buf |
| 大 Tensor 跨主机 | heterogeneous large payload | UCX/RDMA + registration + bounded pool | protobuf/TCP copy all | UCX/Holoscan distributed |
| EtherCAT PDO 周期 | cyclic process image | staged contiguous IOmap + cyclic thread | 每个设备单独 socket/send | SOEM/IgH |
| Planner/状态多输入 | snapshot | versioned snapshot / staged LocalView | “加 mutex 就等于原子快照” | Apollo Planning |
| 配置/路由 control plane | low-frequency topology | map/hash/list/cache | lock-free everything | Holoscan FlowGraph |

---

## 候选方案的三项约束检查

### 1. 业务语义是否一致

Camera 和 Motor Command 都是“消息”，但 Camera 可以 drop-old，Motor Command 往往不能。

### 2. 时间边界是否一致

同一个 FIFO，在离线 batch 与 1 kHz control loop 里的意义完全不同。

### 3. failure semantics 是否一致

日志丢几条和 emergency stop 丢一条不是同一级问题。

---

## 快速决策树：State 还是 Event

~~~text
每一条更新都必须被处理？
├─ 是 → Event / FIFO / bounded queue
└─ 否
   ↓
   旧值在新值到达后还有意义？
   ├─ 否 → latest mailbox / versioned snapshot
   └─ 是 → history/ring/timeseries
~~~

---

## 快速决策树：Blocking 还是 Drop

~~~text
Producer 可以被阻塞吗？
├─ 可以
│  └─ lossless required? → bounded blocking
└─ 不可以
   ↓
   历史必须完整吗？
   ├─ 是 → admission/rate control / independent storage
   └─ 否 → drop-old / latest / sample
~~~

---

## 快速决策树：共享内存还是网络序列化

~~~text
同一主机？
├─ 否 → network protocol / UCX / DDS / Zenoh
└─ 是
   ↓
   payload 很大、频率很高？
   ├─ 否 → socket/pipe/普通 IPC 可能够用
   └─ 是 → SHM pool + descriptor + wakeup
~~~

---

## 快速决策树：单全局 Queue 还是 Sharding

~~~text
worker 数少、contention 可忽略？
├─ 是 → global queue 简单优先
└─ 否
   ↓
   workload 分布稳定？
   ├─ 是 → static/per-worker queues
   └─ 否 → per-worker + stealing / sharded queues
~~~

---

## 快速决策树：CPU copy 还是 Device-resident

~~~text
下游计算也在 GPU/NPU？
├─ 否 → copy 到最终需要的 domain
└─ 是
   ↓
   能否共享/export device buffer？
   ├─ 是 → handle + event/fence + pool
   └─ 否 → pinned staging / UCX pipeline
~~~


