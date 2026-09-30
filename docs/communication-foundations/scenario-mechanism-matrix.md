# 场景 × 机制矩阵：从工程问题快速跳到候选设计

这页是 **Scenario 系列的复习索引，不是入门文章**。如果表格里的术语有一半以上陌生，先回前面的场景页，不要试图一次把整张表背下来。

## 读矩阵前只需要知道六组词

- latest state / double buffer / seqlock：见 [最新状态、事件流与历史](scenario-latest-state-vs-event.md)；
- SPSC / MPSC / MPMC / work stealing：见 [多线程 Runtime](scenario-thread-runtime.md)；
- bounded queue / drop-old / backpressure / admission control：见 [高频 Sensor → 慢速 Perception](scenario-streaming-pipeline.md)；
- eventfd / epoll / WaitSet / owner thread：见 [机器人网络 Runtime](scenario-network-runtime-design.md)；
- SHM / descriptor / offset / loan / generation：见 [大对象跨进程 IPC](scenario-large-payload-ipc.md)；
- tensor handle / fence / RDMA / registration：属于高级异构数据面，见 [GPU Tensor 跨模块与跨主机](scenario-distributed-gpu.md)。

矩阵的正确读法是从左向右：**先确认业务语义，再看候选机制**。不要反过来从“我想用 lock-free queue”开始找场景。

## 基础 Runtime 场景

| 场景 | 先确认的数据语义 | 第一候选 | 常见反模式 | 对应学习页/案例 |
| --- | --- | --- | --- | --- |
| Estimator 200 Hz → Controller 1 kHz | latest state | mutex + snapshot；有瓶颈再看 versioned/double buffer | unbounded FIFO 保存所有历史 | latest-state Scenario / Apollo |
| IMU driver → estimator | ordered high-rate stream | bounded SPSC ring | 一上来就 generic MPMC | thread/queue foundations |
| 多 callback → supervisor | ordered events | bounded MPSC | 多线程直接共享 vector/deque 无协议 | thread-runtime Scenario |
| Emergency Stop | critical persistent state + event | dedicated safety state/path | 与日志/普通 task 共用 backlog queue | control-safety Scenario |
| Camera 60 Hz → Perception 25 Hz | freshness stream | small bounded queue + drop-old | reliable unbounded FIFO | streaming Scenario / Holoscan |
| Logger 多来源写盘 | loss-tolerant events | MPSC + batch | 每个 producer 同步写磁盘 | runtime cases |
| 多 Worker Task Runtime | runnable tasks | global queue 起步；有竞争再 per-worker | 还没测 contention 就先上复杂 stealing | thread-runtime Scenario |
| Event Loop 多类事件 | readiness | epoll/WaitSet + wakeup fd | 高频 sleep polling | network-runtime Scenario |
| 大图像跨进程 | large shared payload | SHM pool + descriptor + notification | 每帧 serialize/copy 整帧 | large-payload IPC / iceoryx2 |
| EtherCAT PDO 周期 | cyclic process image | staged contiguous IOmap + cyclic thread | 每个设备各自 socket/send | SOEM / IgH |
| Planner 多输入 | snapshot | staged/versioned snapshot | “加 mutex 就等于原子快照” | latest-state Scenario / Apollo |
| 配置/路由 control plane | low-frequency topology | 普通 map/hash/list/cache | 为低频配置路径强行 lock-free | runtime architecture pages |

## 异构/GPU 场景：基础打牢后再读

| 场景 | 数据语义 | 候选机制 | 先别踩的坑 |
| --- | --- | --- | --- |
| GPU Tensor 同进程跨模块 | device-resident payload | tensor handle + stream/event + pool | CPU 返回不等于 GPU 数据 ready |
| GPU/NPU Buffer 同机跨进程 | accelerator-resident payload | stable slot + generation + capability transfer + fence | 直接传 pointer/fd 整数 |
| 大 Tensor 跨主机 | heterogeneous large payload | UCX/RDMA + registration + bounded pool | 默认 D2H→TCP→H2D 全拷贝 |

这里的 **fence** 是“证明某次异步访问已经完成、Buffer 可以进入下一生命周期阶段”的同步对象；**capability transfer/broker** 是“把 fd、GPU handle、DMA-BUF 等不能只靠整数值跨进程使用的 OS/设备能力安全交给另一进程”的机制。它们不是第一遍 Scenario 阅读的必备知识。

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


