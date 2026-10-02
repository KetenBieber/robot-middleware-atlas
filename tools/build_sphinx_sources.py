from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"
ARTICLES = CONTENT / "articles"
GUIDES = CONTENT / "guides"
DOCS = ROOT / "docs"
GENERATED = DOCS / "generated"
PENDING_SOURCE = "PENDING_LOCAL_SOURCE"


def write_if_changed(path: Path, text: str) -> None:
    """Keep unchanged generated file mtimes stable for incremental builds."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

PROJECTS = {
    "libuv": ("libuv", "2b4b918d3381100854250c89d5159d4206daafb7"),
    "asio": ("Asio", "8806a6803cde7054c3049d3666d3ec36786568c5"),
    "folly": ("Meta Folly", "c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8"),
    "seastar": ("Seastar", "8df8212e53577e1d8477a5c901457cd61d88afc7"),
    "nginx": ("nginx", "b74b5c961e687c76489482b44cedff63acd18c84"),
    "cyber": ("Apollo Cyber RT", "d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa"),
    "orocos": ("Orocos RTT", "600102e8be9c81905b20930e32d43b28244ab173"),
    "yarp": ("YARP", "91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9"),
    "ecal": ("Eclipse eCAL", "1ec0ea2fe5e5e61e3e492be6128c27cc6026d717"),
    "zenoh": ("Eclipse Zenoh", "9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5"),
    "lcm": ("LCM", "ad0c54cee0ec048ef12357c34349ec1443158864"),
    "libzmq": ("ZeroMQ / libzmq", "46493370217ac135246617fa2f6ac819d8b61bfc"),
    "ros1": ("ROS1 Communication Runtime", "30483a9f218f1545eec16d3934bf3cb042e2cb5b"),
    "ros2": ("ROS2 Communication Runtime", "cfdb3b7dcea4a503c0acaa304d033636beeb1dba"),
    "rmwzenoh": ("ROS 2 rmw_zenoh Runtime", "3b5b9bf424443f9800dd148b5f1cc2053bbc37fe"),
    "ethercat": ("IgH EtherCAT Master", "61cc654f5b721ddd54df0f58bdd34106d91c5359"),
    "soem": ("SOEM", "304d1c05eab77dc0d426f1a5cf09c8cc7dc03713"),
    "cyclonedds": ("Eclipse Cyclone DDS", "e54e991f75a3e67f8e628da3171122e36ea5b872"),
    "fastdds": ("eProsima Fast DDS", "39303846fb8534ef69fa65f9fa4bcc9e6a7c995a"),
    "iceoryx2": ("Eclipse iceoryx2", "135d09dd8b29f321f1725920d434864c4e512378"),
    "ucx": ("OpenUCX", "8a6b06fb880accbb933a79cda893883872c68d9d"),
    "rosidlbuffer": ("ROS 2 rosidl::Buffer / CUDA Buffer Backend", "d7cd9642d77a1d64fd85f25ba0bf96e108401900"),
    "holoscan": ("NVIDIA Holoscan SDK", "66a9609ac37515405561b9b8dbdee8e57f41ab11"),
}

PROJECT_OVERVIEWS = {
    "libuv": """libuv 是跨平台异步 I/O Runtime。它用 uv_loop_t 统一驱动 socket readiness、Timer、signal、async wakeup 与关闭回调；Handle 表示长期资源，Request 表示一次异步操作；无法自然映射为非阻塞 readiness 的文件 I/O、DNS 与用户 blocking work 则进入全局 worker thread pool。\n\n固定版本 2b4b918d。核心问题不是 API，而是 event loop phase、fd watcher registry、timer heap、atomic+eventfd 跨线程唤醒、worker completion、stream write backpressure 与 deferred close 怎样组合成一套可迁移的程序 Runtime。""",
    "asio": """Asio 把异步程序拆成 operation、execution context/executor 与 completion handler 三层。Linux 下 epoll_reactor 管 fd readiness 和 per-descriptor operation queue，scheduler 负责 ready completion 与 run() worker，strand 再在 executor 层提供逻辑串行化。\n\n固定版本 Asio 1.38.2（8806a680）。核心问题不是 async_read API，而是 scheduler_operation 的 intrusive/type-erased 设计、descriptor_state 的局部队列与锁、strand 的双队列 ownership、outstanding-work liveness 以及 close/cancel/reclaim 如何共同组成可推理的 C++ async runtime。""",
    "folly": """Folly 提供的是一组工业 C++ Runtime 基础设施：SPSC/MPMC queue、AtomicNotificationQueue、IOBuf、HHWheelTimer、EventBase 与 Executor。它最值得研究的是数据结构和 OS 原语怎样围绕 cache locality、ownership、wakeup、blocking、batch 与 lifetime 组合，而不是单个 API。\n\n固定版本 v2026.09.28.00（c8ad483c）。核心机制包括 producer/consumer cache-line ownership、ticket + per-slot turn、futex 自适应等待、armed notification、IOBuf chain/shared storage、hierarchical timer wheel，以及 queue policy 与 worker lifecycle 的分离。""",
    "seastar": """Seastar 把多核 Runtime 组织成 shard-per-core：每个 shard 拥有自己的 Reactor、task queues、Timer、I/O 与 allocator，跨 shard 协作通过显式 SMP message passing 完成，而不是让所有线程直接共享 mutable state。\n\n本专题固定到 Seastar 25.05.0（8df8212e）。重点是 cooperative reactor、scheduling-group shares/vruntime、SPSC 跨核 request/completion queue、future continuation task、sharded/foreign_ptr execution ownership，以及 per-shard allocator 的 cross-CPU deferred free。""",
    "nginx": """nginx 把高并发网络服务组织成 master/worker process 模型：每个 worker 以单线程 event loop 拥有自己的 connection/event state，epoll/kqueue 负责 readiness，rbtree 管理 Timer，posted queue 延迟执行事件，预分配 connection slot 与 request pool 控制分配成本，共享内存对象再由 slab allocator 管理。\n\n固定版本 b74b5c9。核心问题是 worker ownership、Timer rbtree、posted event、connection free/reusable queue、pointer generation tag、arena/slab 与 graceful shutdown 怎样共同构成一个有界、低共享的服务器 Runtime。""",
    "cyber": """Cyber RT 是 Apollo 面向车载计算图的运行时：DAG 装载组件，Node 创建 Reader/Writer，Transport 接入进程内、共享内存与 RTPS 通道，DataVisitor 把消息缓存转换为可调度事件，CRoutine 与 Scheduler 再决定业务代码何时获得 CPU。

理解它不能停在“有 Component 和协程”这一层。真正的主线是一条消息怎样跨过 transport callback、Dispatcher、有限缓存、Notifier 和 Processor，最后进入 ``Component::Proc()``；每一次复制、锁竞争和非抢占执行都会落到感知—规划—控制链路的延迟预算里。""",
    "ecal": """eCAL 是发现驱动的高性能发布订阅中间件。稳定的 Publisher/Subscriber 门面背后，注册信息负责让端点相遇，PubGate/SubGate 管理匹配关系，SHM、UDP 与 TCP writer/reader 根据本机性、配置和订阅者能力组成实际数据路径。

它的关键不只是“支持多种传输”，而是同一次 Send 如何准备 payload、选择并驱动多条 writer，接收端又怎样把共享内存视图或网络报文交给回调。buffer rotation、确认等待、fragment、TCP 队列和注册过期共同决定慢消费者会阻塞、丢旧还是造成更长的数据年龄。""",
    "zenoh": """Zenoh 把发布订阅、查询和分布式数据空间统一在 key expression 之上。Session 是应用入口，Primitives 隔离会话与路由，Face 表示一侧连接关系，resource tree 与 route cache 把表达式匹配结果变成可复用的数据路径，transport pipeline 再完成 batch、frame 和 fragment。

因此它不能只被理解成另一套 topic pub/sub。要读懂一次 put 或 get，必须同时理解 key expression 的集合语义、声明如何改变路由表、消息如何在多个 Face 间改写 WireExpr，以及拥塞控制和可靠性如何沿异步任务与有限通道传播。""",
    "lcm": """LCM 用很小的 C 运行时完成低延迟消息分发。顶层 ``lcm_t`` 保存 provider vtable、订阅关系和 handle 状态；UDPM provider 用 LC02/LC03 报文、多播 socket、接收线程、重组表、有限 ring 与通知 pipe，把网络接收和用户 callback 分到两个执行上下文。

它的价值在于机制少而边界清楚：publish 可以一直追到 ``sendmsg/iovec``，receive 可以一直追到 fragment reassembly 与锁外 callback。相应代价也直接可见——多播没有端到端可靠性，分片放大丢包概率，队列容量和主线程调用 ``handle`` 的节奏决定数据是否及时。""",
    "libzmq": """libzmq 把 socket pattern 建立在一套显式消息 Runtime 之上：msg_t 管消息存储与共享引用，pipe/ypipe 与 mailbox/command 管线程间数据面和控制面，session/engine/ZMTP 管网络协议；Context 还维护 inproc endpoint registry、socket slot 与 Reaper，monitor 把运行时状态重新编码成消息流，proxy 则在普通 socket 之上继续传播 backpressure。\n\n本专题固定到 46493370。完整主线覆盖小消息内联与大消息 fan-out、SPSC queue、HWM、owner-thread command、协议握手与重连、inproc pending bind、monitor event schema、steerable proxy，以及 linger、TERM/TERM_ACK、seqnum、Reaper DONE 共同组成的多层 shutdown barrier。""",
    "ros1": """ROS1 把机器人通信拆成三个明显的运行时平面：rosmaster 通过 XML-RPC 维护中心化 graph，节点之间再用 requestTopic 协商 TCPROS/UDPROS 数据通道，收到的 payload 最后经过 SubscriptionQueue、CallbackQueue 与 Spinner 执行业务回调。\n\n本专题固定到 ros_comm 30483a9f，并对照 roscpp_core a1a19427 与 nodelet_core 5ed9cabe。重点不是 ROS API，而是 RegistrationManager 双索引、publisherUpdate、requestTopic、TCPROS framing、Serializer<T>、有界 callback queue、Spinner 线程模型与 Nodelet intra-process ownership 怎样共同构成 ROS1 通信 Runtime，并为后续 ROS2/RMW/Executor 对照建立基线。""",
    "ros2": """ROS2 把机器人通信进一步拆成 rclcpp 应用语义、rcl C 核心、RMW 中间件契约、DDS 数据面与 Executor 执行平面。一次 publish 可能先走 IntraProcessManager，也可能进入 rcl_publish → rmw_publish → Cyclone/Fast DDS；接收方向则从 RMW readiness 进入 WaitSet、take 与 CallbackGroup。\n\n本专题固定到 rclcpp Humble cfdb3b7d，并对照 rcl cbaee7c9、rmw_dds_common e26ba107、rmw_cyclonedds e370e09c 与 rmw_fastrtps da0c2d31。重点是 RMW 分层、distributed discovery + GraphCache、QoS compatibility、Executor/WaitSet、take/callback、intra-process ownership 与 LoanedMessage/SHM capability 怎样共同决定数据年龄、复制与调度边界。""",
    "rmwzenoh": """rmw_zenoh 展示了 ROS 2 RMW contract 如何落到一套并非 DDS/RTPS 的分布式通信模型：一个 Context 共享一个 Zenoh Session，Node/Publisher/Subscription/Service/Client 通过 liveliness token 重建 ROS Graph，Topic 使用 key expression + CDR payload + attachment，Service 则映射到 Query/Queryable。\n\n本专题固定到 rmw_zenoh 3b5b9bf4。重点不是配置 RMW_IMPLEMENTATION，而是语义适配：GraphCache 如何补齐 ROS graph、SubscriptionData 如何把 Zenoh callback 隔离到本地 queue 与 WaitSet、QoS 哪些由本地状态实现、Service 如何保存 Query correlation，以及共享 Session、liveliness token、waiter 与异步 callback 怎样形成安全 shutdown。""",
    "orocos": """Orocos RTT 围绕实时组件建立明确的执行边界。TaskContext 暴露生命周期 hooks、Operation 和 Port；Activity 提供线程与周期，ExecutionEngine 统一处理消息、端口事件和组件更新；ConnPolicy 决定数据连接使用最新值还是有界缓冲，以及采用何种同步策略。

它最值得追踪的问题是“谁的线程执行这段代码”。ClientThread 与 OwnThread operation、周期与事件驱动 Activity、DATA 与 BUFFER policy 会给出完全不同的阻塞和数据年龄语义。所谓实时性最终取决于容器进度保证、hook 的最坏执行时间、OS priority/affinity 与关闭顺序能否形成闭环。""",
    "yarp": """YARP 把机器人网络抽象成 Port。PortCore 管理输入输出连接和生命周期，OutputUnit/InputUnit 把每条连接的执行状态隔离开，Protocol 与 Carrier 则把握手、framing、确认和具体传输协议从端口 API 中剥离。Name Server 把逻辑名字解析成可连接的 Contact。

YARP 的主数据路径是一轮 ``Port::write`` 序列化并扇出到多条连接，对端再经 InputUnit 进入 PortReader。同步写、后台写、不同 Carrier、慢连接与断开竞态会改变 buffer 所有权和调用者阻塞时间，也决定它更适合可靠数据流还是只关心最新状态的控制链路。""",
    "ethercat": """IgH EtherCAT Master 把 Linux 主机、网卡、EtherCAT 帧、从站状态机和周期过程数据组织成一条面向工业控制的实时通信链。应用层看到 Master、Domain、PDO 与周期收发 API；源码层真正决定抖动、数据年龄和故障恢复的，是 FMMU/process image、datagram queue、非阻塞 FSM、Device/NIC 与 Distributed Clocks 怎样协同。\n\n本专题固定到 stable-1.6 / 1.6.13（61cc654f）。课程明确分成两部分：前半先从协议与控制系统第一性原理建立 EtherCAT 软件栈理论，后半再沿 ``ecrt_*`` public API 进入 Master/Domain、Datagram、FSM、Device/NIC 和 DC 的真实源码实现。""",
    "soem": """SOEM 是轻量的用户态 EtherCAT MainDevice C Library。它不建立独立内核 Master，而是把 ``ecx_contextt``、固定容量 slave/group/frame 数组、IOmap、raw socket OSHW 与 OSAL 组合成可直接嵌入控制应用的协议运行时。\n\n本专题固定到 v2.0.0（304d1c05）。核心在于把同一套 PDO/FMMU/WKC/DC 语义和 IgH 做架构对照：Context 与对象图、固定 frame slot 与 Datagram queue、application IOmap 与 Domain、raw socket 与 net_device，以及应用自己承担的实时线程和恢复策略。""",
    "cyclonedds": """Cyclone DDS 是 ROS 2 常用的 DDS/RTPS 实现之一。应用层看到 Participant、Writer、Reader、QoS 与 WaitSet；源码层真正决定数据年龄、可靠性、内存与调度边界的，是 DDSc Entity/RHC、DDSI discovery/WHC/RTPS、DDSRT socket/thread 以及 PSMX/loan 怎样协同。\n\n本专题固定到 Cyclone DDS 11.0.1（e54e991f）。一份样本的运行时路径依次经过 Entity 生命周期 → SPDP/SEDP → QoS matching → Writer/Reader 创建 → dds_write → WHC/Reliability → RTPS/UDP → receive/defrag/reorder → RHC → WaitSet/Listener → async/关闭 → PSMX/loan；官方 ddsperf 与 ROS 2 rmw_cyclonedds 4.2.1 案例用于验证这些机制在工程中的组合方式。""",
    "fastdds": """Fast DDS 是 eProsima 的 DDS/RTPS 实现，也是 ROS 2 常用 RMW 后端之一。它以显式 C++ 对象图把 DDS façade、DataWriterImpl/DataReaderImpl、CacheChange、History、StatefulWriter/Reader、Proxy、FlowController 与 UDP/TCP/SHM Transport 串成运行时。\n\n本专题固定到 v3.6.2（39303846）。重点不是重讲一遍 DDS 术语，而是和 Cyclone DDS 做实现层对照：DataWriter::write 怎样在一次调用里完成锁、loan、序列化、CacheChange 与 History；Reliable 怎样落到 ReaderProxy/WriterProxy 与 TimedEvent；异步发送怎样由 FlowController 调度；SHM Transport、Data Sharing 与 loan_sample 为什么是三层不同优化。最后用官方 delivery_mechanisms 与 ROS 2 rmw_fastrtps 固定案例闭环。""",
    "iceoryx2": """iceoryx2 是以 Rust core 实现的 zero-copy IPC runtime。它把大 payload 放进共享内存 DataSegment，通过 PointerOffset 和 ZeroCopyConnection 传递跨进程稳定的 descriptor，再用 borrow/release/reclaim 闭环 sample 生命周期。\n\n本专题固定到 v0.10.0（135d09dd）。核心不在 API，而在共享页、虚拟地址、offset pointer、pool allocator、fan-out ownership、backpressure、WaitSet/Reactor 与 dead-node cleanup 怎样共同组成一套生产级同机 IPC。""",
    "ucx": """OpenUCX 是面向高性能异构数据面的通信框架。UCP 把 endpoint、request、tag/RMA/AM 与协议选择组织成高层语义，UCT 再把这些动作映射到 shared-memory、TCP、InfiniBand/RDMA、CUDA、ROCm、Level Zero 等 transport；UCS 提供数据结构与系统设施，UCM 负责内存事件相关机制。\n\n这一组文章固定到 UCX v1.22.0（8a6b06fb）。核心问题是同一个发送调用怎样依据 endpoint lane、消息尺寸、memory type、system device 与 transport capability 选择实际数据路径，以及 request、progress、registration、rendezvous 和 backpressure 怎样共同决定延迟与数据年龄。""",
    "rosidlbuffer": """rosidl::Buffer / CUDA Buffer Backend 把“消息是什么”和“payload 存在哪里”拆成两层：ROS 消息继续表达 schema 与通信语义，Buffer backend 决定同一 payload 使用 CPU vector、CUDA VMM、平台专用 accelerator memory 还是其他 storage。CUDA backend 进一步把同机 GPU buffer 共享落实到 CUDA VMM、POSIX FD、Unix-domain socket、SCM_RIGHTS、/dev/shm registry、CUDA event 与 atomic IPC refcount。\n\n本专题固定到 ros2/rosidl_buffer_backends 的 d7cd9642。重点不是学习 ROS 2 API，而是研究一条 accelerator-native message path 怎样用 size-class free list、VMM block identity、endpoint locality cache、epoll/eventfd dispatcher、RAII Read/Write Handle 与异步 recycler 同时解决分配、跨进程映射、生命周期、fallback 和 stale-handle 防护。""",
    "holoscan": """NVIDIA Holoscan SDK 是面向实时传感器、视频与 GPU AI pipeline 的图运行时。Application/Fragment/Operator 描述业务图，FlowGraph 保存拓扑，Condition 把“什么时候可执行”显式化，Scheduler/ThreadPool 决定哪个 CPU 执行流获得工作，Allocator/CUDA Stream 管理异构内存，而跨 Fragment 连接再落到 UCX 数据面。\n\n本专题固定到 Holoscan SDK v4.6.0（66a9609a）。重点不是学习 Operator API，而是追踪一个 ready event 怎样进入 EventBasedScheduler、一帧 Tensor 怎样穿过 bounded connector 与 GPU allocator、分布式 Fragment 怎样建立 UCX connection，以及这些机制怎样最终落到 Linux thread priority、CPU affinity、CUDA stream/event 与有限 Buffer Pool。""",
}

PROJECT_EXTRAS: dict[str, list[str]] = {}

ARTICLE_ORDER = {
    "libuv": [
        "overview",
        "event-loop-phases",
        "handle-request-lifetime",
        "timer-heap",
        "async-cross-thread-wakeup",
        "threadpool-workqueue",
        "epoll-watcher-registry",
        "stream-write-backpressure",
    ],
    "asio": [
        "overview",
        "scheduler-operation-queue",
        "epoll-reactor-descriptor-state",
        "strand-serialization",
        "work-lifetime-cancellation",
    ],
    "folly": [
        "overview",
        "spsc-cacheline-cursor",
        "mpmc-ticket-turnsequencer",
        "eventbase-atomic-notification",
        "iobuf-chain-ownership",
        "f14-cache-friendly-hash",
        "concurrent-hashmap-shards-hazptr",
        "rcu-grace-period-reclamation",
        "hhwheel-timer",
        "cpu-thread-pool",
    ],
    "seastar": [
        "overview",
        "reactor-shard-per-core",
        "scheduling-groups-vruntime",
        "smp-message-queue",
        "future-continuation-task",
        "sharded-foreign-ptr",
        "cross-shard-memory-reclaim",
    ],
    "nginx": [
        "overview",
        "worker-epoll-accept",
        "timer-rbtree",
        "posted-event-queue",
        "connection-pool-lifecycle",
        "memory-pool-slab",
    ],
    "cyber": [
        "overview",
        "architecture-map",
        "dag-to-component",
        "node-reader-writer",
        "pending-queue-ring",
        "dispatcher-notifier",
        "multi-input-fusion",
        "registry-publication-quiescence",
        "croutine-wakeup",
        "croutine-state-event-latch",
        "processor-context-switch",
        "message-to-proc",
        "class-loader-abi",
        "cpp-type-runtime",
        "cpp-implementation-lab",
        "design-recap",
    ],
    "lcm": [
        "overview",
        "foundations",
        "architecture-map",
        "provider-vtable",
        "udpm-publish-protocol",
        "receive-reassembly",
        "subscription-dispatch",
        "cpp-binding-lifetime-quiescence",
        "types-and-eventlog",
        "c-abi-cpp-design-lab",
        "design-recap",
    ],
    "libzmq": [
        "overview",
        "msg-storage-refcount",
        "mailbox-command-wakeup",
        "ypipe-yqueue-spsc",
        "pipe-hwm-backpressure",
        "socket-command-owner",
        "command-seqnum-quiescence",
        "inproc-endpoint-registry",
        "io-thread-poller",
        "session-stream-engine",
        "zmtp-handshake-mechanism",
        "tcp-reconnect-state-machine",
        "monitor-event-observability",
        "fq-lb-dist-schedulers",
        "dealer-router-routing",
        "pubsub-trie-distributor",
        "proxy-device-runtime",
        "linger-termination-protocol",
        "context-reaper-lifecycle",
    ],
    "ros1": [
        "overview",
        "master-discovery",
        "topic-connection",
        "tcpros-transport",
        "serialization-message",
        "callback-queue-spinner",
        "service-rpc-runtime",
        "nodelet-intra-process",
        "limitations-and-ros2-transition",
    ],
    "ros2": [
        "overview",
        "publish-rcl-rmw-dds",
        "discovery-graph-cache",
        "qos-contract-mapping",
        "executor-waitset",
        "receive-take-callback",
        "service-client-server-runtime",
        "action-protocol-composition",
        "action-goal-state-executor",
        "intra-process-manager",
        "loaned-message-zero-copy",
        "ros1-vs-ros2-architecture",
        "service-rpc-comparison",
        "latency-budget-comparison",
        "copy-serialization-comparison",
        "scheduling-comparison",
        "backlog-qos-comparison",
        "discovery-failure-comparison",
        "control-chain-case-study",
    ],
    "rmwzenoh": [
        "overview",
        "context-session-router",
        "graph-liveliness-cache",
        "publisher-subscription-dataflow",
        "subscription-waitset",
        "service-queryable-rpc",
        "qos-events-shm",
        "shutdown-dds-comparison",
    ],
    "ecal": [
        "foundations",
        "architecture-map",
        "registration-soft-state",
        "publisher-discovery-send",
        "shm-memory-protocol",
        "subscriber-delivery",
        "callback-reentrancy-quiescence",
        "global-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
    "zenoh": [
        "foundations",
        "architecture-map",
        "session-runtime",
        "publisher-routing",
        "query-lifecycle",
        "resource-route-cache",
        "backpressure-close",
        "rust-cpp-design-lab",
        "design-recap",
    ],
    "orocos": [
        "foundations",
        "architecture-map",
        "taskcontext-lifecycle",
        "activity-execution-engine",
        "operation-threading",
        "ports-channels",
        "realtime-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
    "yarp": [
        "foundations",
        "architecture-map",
        "portcore-architecture",
        "write-fanout",
        "protocol-carrier",
        "read-rpc",
        "close-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
    "ethercat": [
        "overview",
        "theory-stack",
        "theory-frame-datagram",
        "theory-pdo-process-image",
        "theory-state-mailbox",
        "theory-distributed-clocks",
        "theory-realtime",
        "architecture-map",
        "master-lifecycle",
        "domain-process-image",
        "cyclic-send-receive",
        "datagram-frame",
        "slave-fsm-mailbox",
        "device-nic-runtime",
        "distributed-clocks",
        "realtime-concurrency",
        "design-recap",
    ],

    "soem": [
        "overview",
        "architecture-map",
        "context-fixed-arrays",
        "linux-raw-socket",
        "frame-buffer-index",
        "datagram-primitives",
        "slave-discovery",
        "pdo-sm-fmmu",
        "iomap-cyclic",
        "mailbox-coe",
        "distributed-clocks",
        "redundancy",
        "realtime-osal",
        "fault-recovery",
        "soem-vs-igh",
        "design-recap",
    ],
    "cyclonedds": [
        "overview",
        "architecture-map",
        "entity-lifecycle",
        "discovery-spdp-sedp",
        "qos-matching",
        "writer-reader-creation",
        "write-path",
        "whc-reliability",
        "rtps-network",
        "receive-reorder",
        "rhc-read-take",
        "waitset-listener",
        "threads-async-close",
        "psmx-loans",
    ],
    "fastdds": [
        "overview",
        "architecture-map",
        "participant-endpoint-lifecycle",
        "discovery-pdp-edp",
        "discovery-server",
        "qos-matching",
        "writer-reader-creation",
        "write-cachechange",
        "writerhistory-reliability",
        "readerhistory-fragments",
        "transport-network",
        "flowcontroller-async",
        "datasharing-vs-shm",
        "loan-zero-copy",
        "waitset-listener",
        "threads-events-close",
        "fastdds-vs-cyclonedds",
    ],
    "iceoryx2": [
        "overview",
        "architecture-map",
        "service-abstraction",
        "shared-memory-pointer-offset",
        "pool-allocator-layout",
        "service-discovery-config",
        "publisher-loan",
        "zero-copy-connection",
        "subscriber-receive-reclaim",
        "fanout-backpressure-history",
        "event-notifier-listener",
        "thread-safety-event-reactor",
        "request-response-streaming",
        "blackboard-shared-state",
        "dead-node-recovery",
        "iceoryx2-vs-existing-shm",
    ],
    "ucx": [
        "overview",
        "architecture-map",
        "context-worker-endpoint",
        "wireup-lane-selection",
        "tag-send-request",
        "protocol-selection",
        "progress-engine",
        "uct-transport-model",
        "memory-domain-types",
        "rendezvous-gpu-pipeline",
        "backpressure-thread-safety",
        "ucx-vs-message-middleware",
    ],
    "rosidlbuffer": [
        "overview",
        "buffer-backend-contract",
        "cuda-vmm-pool",
        "ipc-fd-shm-registry",
        "stream-handles-lifetime",
        "accelerator-ipc-protocols",
        "endpoint-locality-fallback",
    ],
    "holoscan": [
        "overview",
        "architecture-map",
        "flowgraph-containers",
        "event-based-scheduler",
        "gxf-event-runtime-internals",
        "scheduling-term-event-wakeup",
        "gxf-entity-executor-router",
        "operator-materialization-lifecycle",
        "conditions-connectors-backpressure",
        "allocator-cuda-memory",
        "cuda-stream-event-propagation",
        "distributed-ucx-runtime",
    ],

}
CYBER_ARTICLE_SECTIONS = [
    ('从空目录建立系统全貌', ['architecture-map']),
    ('第一条消息之前：装配与通信端点',
     ['dag-to-component', 'node-reader-writer']),
    ('数据面：有界缓存、分发与输入组合',
     ['pending-queue-ring', 'dispatcher-notifier', 'multi-input-fusion',
      'registry-publication-quiescence']),
    ('执行面：从数据通知到真正获得 CPU',
     ['croutine-wakeup', 'croutine-state-event-latch', 'processor-context-switch']),
    ('把整条消息链重新跑一遍', ['message-to-proc']),
    ('生命周期、ABI 与 C++ 机制',
     ['class-loader-abi', 'cpp-type-runtime']),
    ('从源码原则到最小实现',
     ['cpp-implementation-lab', 'design-recap']),
]

ETHERCAT_ARTICLE_SECTIONS = [
    ("EtherCAT 软件栈理论", [
        "theory-stack",
        "theory-frame-datagram",
        "theory-pdo-process-image",
        "theory-state-mailbox",
        "theory-distributed-clocks",
        "theory-realtime",
    ]),
    ("IgH 主站源码实现解剖", [
        "architecture-map",
        "master-lifecycle",
        "domain-process-image",
        "cyclic-send-receive",
        "datagram-frame",
        "slave-fsm-mailbox",
        "device-nic-runtime",
        "distributed-clocks",
        "realtime-concurrency",
        "design-recap",
    ]),
]


SOEM_ARTICLE_SECTIONS = [
    ("运行时骨架：Context、网卡与 Frame Slot", [
        "architecture-map",
        "context-fixed-arrays",
        "linux-raw-socket",
        "frame-buffer-index",
        "datagram-primitives",
    ]),
    ("配置编译：从 Slave 到 IOmap", [
        "slave-discovery",
        "pdo-sm-fmmu",
        "iomap-cyclic",
    ]),
    ("控制面与工业能力", [
        "mailbox-coe",
        "distributed-clocks",
        "redundancy",
    ]),
    ("实时、故障与架构对照", [
        "realtime-osal",
        "fault-recovery",
        "soem-vs-igh",
        "design-recap",
    ]),
]

GUIDE_ORDER: dict[str, list[str]] = {
    "libuv": [],
    "asio": [],
    "folly": [],
    "seastar": [],
    "nginx": [],
    "libzmq": [],
    "ros1": [],
    "ros2": [],
    "rmwzenoh": [],
    "cyber": ["use-environment", "use-pubsub", "closed-loop-project", "use-component-operations", "case-study-apollo-planning"],
    "ecal": ["use-environment", "use-pubsub", "closed-loop-project", "use-operations", "case-study-mqtt-bridge"],
    "zenoh": ["use-environment", "use-pubsub-query", "use-operations", "case-study-rmw-zenoh"],
    "lcm": ["use-environment", "use-pubsub-types", "closed-loop-project", "use-operations", "case-study-drake"],
    "orocos": ["use-environment", "use-component-ports", "use-operations", "case-study-rtt-ros"],
    "yarp": ["use-environment", "use-ports-rpc", "use-operations", "case-study-icub-navigation"],
    "ethercat": ["use-environment", "closed-loop-project", "real-hardware-deployment"],
    "soem": ["case-study-official-ec-sample", "case-study-leggedrobotics-soem-interface", "case-study-elfin-robot-ros2", "case-study-ipe-ros2-control"],
    "cyclonedds": ["case-study-ddsperf", "case-study-rmw-cyclonedds"],
    "fastdds": ["case-study-delivery-mechanisms", "case-study-rmw-fastrtps"],
    "iceoryx2": ["official-examples-lab"],
    "ucx": ["official-hello-world-lab"],
    "rosidlbuffer": ["case-study-isaac-ros-5-migration"],
    "holoscan": [
        "case-study-endoscopy-tool-tracking",
        "case-study-ultrasound-segmentation",
    ],
}

PROJECT_STORIES = {
    "libuv": """大量 socket、timer、blocking work 和跨线程 completion 同时存在时，关键不是增加更多线程，而是先划分 execution ownership。socket readiness 与 Timer 由 loop thread 驱动，阻塞工作由 worker pool 执行，completion 再通过 loop-local queue 与 async wakeup 回到 owner thread；fd watcher、timer heap、write queue 和 closing list 分别承担 identity、deadline、backpressure 与 deferred reclamation。""",
    "asio": """异步 I/O 不只是在 epoll 上套一层 C++。Asio 把 readiness、operation、execution policy 与 completion lifetime 分成独立对象：descriptor_state 管每个 fd 的 pending operation，scheduler 负责 ready completion，strand 把同一业务对象的 handler 串行化，outstanding work 决定 run loop 何时真正结束。""",
    "folly": """当程序从“一个队列、一个线程池”成长到高并发 Runtime 后，真正需要设计的是 topology、cache-line ownership、等待策略、buffer lifetime、Timer workload 和 shutdown protocol。Folly 把这些问题压到一组可组合的数据结构中：SPSC 先利用单 producer/consumer 约束，MPMC 再用 ticket/turn 管 slot 复用，AtomicNotificationQueue 把 payload 与 wakeup handshake 分开，IOBuf 把 data view 与 storage ownership 分开，Timer/Executor 再分别承担时间与 CPU 调度。""",
    "seastar": """如果 hot mutable state 可以按 Core 拆开，那么最有效的并发优化可能不是更复杂的锁，而是消除共享。Seastar 让每个 shard 独占 Reactor、任务队列、服务实例与 allocator，跨核用 SPSC request/completion queue 传递 work；future continuation 又直接成为 Reactor task，连跨核 free 也先回到 owner 的 freelist，再由 owner 批量回收。""",
    "nginx": """大量连接并不要求大量线程。nginx 先把连接按 worker process 分片，再让每个 worker 的单线程 event loop 独占大部分 mutable connection state；连接对象来自固定 free list，空闲 keepalive 可进入 reusable queue，Timer 用 rbtree，ready event 可以先进入 posted queue，request-scoped 小对象则由 arena-style pool 整体回收。资源容量、事件执行顺序与 shutdown 因而都能在有限状态机中表达。""",
    "cyber": """从 :doc:`总览 <overview>` 中的一帧消息开始，先分清 Node、Reader/Writer、Component 和 Processor 分别属于通信、业务与执行哪一层。随后沿 :doc:`DAG 装配 <dag-to-component>`、:doc:`通信端点 <node-reader-writer>`、:doc:`数据分发 <dispatcher-notifier>` 和 :doc:`多输入融合 <multi-input-fusion>` 建立数据面，再用 :doc:`动态注册、发布与 Quiescence <registry-publication-quiescence>` 把 registry、callback lifetime、hot-plug 与 teardown 的并发边界单独拆开。执行面先读 :doc:`调度唤醒 <croutine-wakeup>`，再进入 :doc:`CRoutine 状态机与 Event Latch <croutine-state-event-latch>`，把 DATA_WAIT/IO_WAIT、lost wakeup、pending event 与 memory order 拆成可证明的等待协议；随后沿 :doc:`Processor 上下文切换 <processor-context-switch>` 追踪 READY task 怎样真正获得 CPU，最后用 :doc:`消息到 Proc <message-to-proc>` 把整条链重新闭合。

读完基础机制后进入 :doc:`端到端闭环工程 <closed-loop-project>`：把 Proto、Bazel、Publisher、DAG Component、输出 Writer 与严格 Observer 放在同一条链里，再回到 :doc:`最小 C++ Runtime <cpp-implementation-lab>` 亲自编译 Node/Reader/Writer 的缩小版。最后用 :doc:`Apollo Planning 案例 <case-study-apollo-planning>` 检查这些机制如何进入真实规划流水线。""",
    "ecal": """把问题收敛到一次相机消息：先在 :doc:`入门 <foundations>` 中分清发布端与订阅端，再读 :doc:`注册和软状态 <registration-soft-state>`，理解它们如何发现彼此。只有端点已经匹配，:doc:`Publisher 发送 <publisher-discovery-send>` 中的 SHM、UDP 和 TCP 选择才有实际意义。

接着追 :doc:`共享内存与消息寿命 <shm-memory-protocol>`：同一帧何时被复制、哪一个槽位能被覆盖，为什么 zero-copy 不等于无条件零拷贝；再沿 :doc:`Subscriber 交付 <subscriber-delivery>` 把接收线程、业务 callback 与 latest-value Read 分开。随后进入 :doc:`Callback 重入与 Quiescence <callback-reentrancy-quiescence>`，把 self-remove、自销毁、callback snapshot、logical unregister、in-flight drain 与 event-callback TOCTOU 拆成统一的生命周期协议。学完 API 后进入 :doc:`三进程闭环工程 <closed-loop-project>`，把 STL 容器选择、线程/进程边界和 Linux/Windows SHM 原语放进同一条 source→relay→observer 链；最后再读 :doc:`C++ 实现实验 <cpp-design-lab>` 和 :doc:`MQTT Bridge 案例 <case-study-mqtt-bridge>`。""",
    "lcm": """从 :doc:`机械臂消息总线的最小问题 <overview>` 开始，只保留“给出 channel 和一串字节”的 API。随后进入 :doc:`Provider 设计 <provider-vtable>`：先亲手写一个会失控的 switch，再理解 C 函数指针如何隔离传输实现。

再沿 :doc:`UDP 发送 <udpm-publish-protocol>`、:doc:`接收与分片重组 <receive-reassembly>` 和 :doc:`订阅分发 <subscription-dispatch>` 追踪同一条消息，找出谁在收包、谁在调用用户代码，以及 C core 为什么用 `callback_scheduled` 做延迟回收。随后进入 :doc:`C/C++ 订阅生命周期 <cpp-binding-lifetime-quiescence>`，继续追 userdata、`channel_buf`、`std::function` 与 Handler 为什么没有自动继承 C core 的 grace period，以及 self-unsubscribe 与 pre-entry race 应如何设计。读完 typed pub/sub 后进入 :doc:`双进程闭环工程 <closed-loop-project>`，把 schema、CMake、sender、receiver 和退出验收逐文件连起来；最后用 :doc:`C ABI 与 C++ 实验 <c-abi-cpp-design-lab>` 将设计压缩到可写、可测试的最小系统，再看 :doc:`Drake 集成 <case-study-drake>`。""",
    "libzmq": """从 :doc:`总览 <overview>` 与 :doc:`msg_t 存储 <msg-storage-refcount>` 建立消息 envelope，再沿 :doc:`Mailbox <mailbox-command-wakeup>`、:doc:`yqueue/ypipe <ypipe-yqueue-spsc>`、:doc:`Pipe/HWM <pipe-hwm-backpressure>` 和 :doc:`Socket owner <socket-command-owner>` 看数据与 command 怎样跨线程。紧接着读 :doc:`Command Seqnum <command-seqnum-quiescence>`，理解为什么 raw pointer 离开 registry lock 前必须先建立 lifecycle reservation，为什么 `sent/processed` 不是队列长度，以及 TERM_ACK、Reaper 与 Context DONE 如何形成分层 quiescence；随后再进入 :doc:`inproc Endpoint Registry <inproc-endpoint-registry>`，把 lookup、pending connect、Pipe ownership 与 seqnum reservation 放回真实连接流程。\n\n网络侧从 :doc:`I/O thread/poller <io-thread-poller>`、:doc:`Session/Engine <session-stream-engine>` 进入 :doc:`ZMTP 握手 <zmtp-handshake-mechanism>` 与 :doc:`TCP 重连 <tcp-reconnect-state-machine>`；:doc:`Monitor Event <monitor-event-observability>` 把这些状态变成结构化观测消息。再用 :doc:`FQ/LB/Distributor <fq-lb-dist-schedulers>`、:doc:`DEALER/ROUTER <dealer-router-routing>`、:doc:`PUB/SUB <pubsub-trie-distributor>` 建立 socket pattern，最后进入 :doc:`Proxy/Device <proxy-device-runtime>` 看上层 forwarding 怎样传播 backpressure。关闭阶段先读 :doc:`Linger/终止协议 <linger-termination-protocol>`，再以 :doc:`Context/Reaper <context-reaper-lifecycle>` 收束 socket slot、poller detach、异步销毁与 Context DONE join。""",
    "ros1": """从 :doc:`Runtime 总览 <overview>` 把 ROS1 分成 graph discovery、payload transport 和 callback execution 三个平面；再沿 :doc:`Master/Discovery <master-discovery>` 追 RegistrationManager、NodeRef、registerSubscriber 与 publisherUpdate，理解中心化发现为何不进入消息热路径。\n\nTopic 主线用 :doc:`Topic 建链 <topic-connection>`、:doc:`TCPROS <tcpros-transport>`、:doc:`序列化 <serialization-message>` 与 :doc:`CallbackQueue/Spinner <callback-queue-spinner>` 把发现、字节传输和用户回调拆开；随后进入 :doc:`Service RPC Runtime <service-rpc-runtime>`，理解 lookupService、ServiceServerLink/ServiceClientLink、call_queue_、condition_variable 与 persistent connection 如何实现同步 RPC。最后用 :doc:`Nodelet <nodelet-intra-process>` 解释同地址空间 no-copy，再通过 :doc:`ROS1→ROS2 <limitations-and-ros2-transition>` 建立后续 RMW/Executor 对照。""",
    "ros2": """从 :doc:`Runtime 总览 <overview>` 建立 rclcpp → rcl → rmw → DDS 与 WaitSet/Executor 两条主链，再用 :doc:`publish 调用链 <publish-rcl-rmw-dds>`、:doc:`DDS Discovery 与 ROS Graph <discovery-graph-cache>`、:doc:`QoS Contract <qos-contract-mapping>`、:doc:`Executor/WaitSet <executor-waitset>` 与 :doc:`Reader→take→callback <receive-take-callback>` 走完 Topic 数据面。\n\nRPC 语义进入 :doc:`Service Runtime <service-client-server-runtime>`，追 request_id、pending_requests_ 与 execute_service/execute_client；长期任务再进入 :doc:`Action 协议组成 <action-protocol-composition>` 和 :doc:`Goal 状态机与 Executor <action-goal-state-executor>`，把 3 个 Service、2 个 Topic、GoalUUID、result retention 与 cancel state machine 拼成完整协议。内存面继续读 :doc:`IntraProcessManager <intra-process-manager>` 与 :doc:`Loaned Message/Zero-copy <loaned-message-zero-copy>`。完成 :doc:`ROS1↔ROS2 架构对照 <ros1-vs-ros2-architecture>` 与 :doc:`Service RPC 对照 <service-rpc-comparison>` 后，再进入延迟、复制、调度、QoS、discovery 与控制链专题。""",
    "rmwzenoh": """先从 :doc:`Runtime 总览 <overview>` 理解“RMW contract 不变、底层 primitive 全换掉”的整体映射；随后进入 :doc:`Context/Session/Router <context-session-router>`，建立共享 Session、NodeData、GraphCache 与 Router 的 ownership。控制面继续读 :doc:`Liveliness/GraphCache <graph-liveliness-cache>`，看 ROS Graph 如何从 Zenoh liveliness keyspace 重建。\n\n数据面沿 :doc:`Publisher/Subscription <publisher-subscription-dataflow>` 跟踪 CDR、attachment、weak_ptr 与 history queue，再用 :doc:`Subscription Queue/WaitSet <subscription-waitset>` 解释 lost-wakeup 防护和 Executor 边界。RPC 进入 :doc:`Service Queryable <service-queryable-rpc>`，理解 Query handle、GID+sequence correlation 与 delayed reply。最后用 :doc:`QoS/Events/SHM <qos-events-shm>` 分清 native、emulated、metadata 与 unsupported 语义，再以 :doc:`Shutdown 与 DDS-RMW 对照 <shutdown-dds-comparison>` 收束生命周期和 RMW 抽象边界。""",
    "orocos": """先从 :doc:`1 ms 控制循环的失败 <foundations>` 出发：把设备、命令与日志塞进同一个线程为什么不够；然后沿 :doc:`TaskContext 生命周期 <taskcontext-lifecycle>` 给配置、启动、异常和释放划边界。

接着进入 :doc:`Activity 与 ExecutionEngine <activity-execution-engine>`，区分“任务可以运行”和“哪个 OS 线程真正执行”；再读 :doc:`Port 与 Channel <ports-channels>` 及 :doc:`Operation 线程模型 <operation-threading>`，理解样本与控制命令的不同时间语义。最后通过 :doc:`C++ 实验 <cpp-design-lab>` 验证对象寿命、虚接口和并发关闭，再对照 :doc:`RTT/ROS 集成 <case-study-rtt-ros>`。""",
    "yarp": """从 :doc:`为何需要带名字的 Port <foundations>` 开始：应用不应知道远端 socket 的每一个细节。进入 :doc:`PortCore 架构 <portcore-architecture>` 以后，先辨别 Port、连接 Unit、Protocol 和 Carrier 的所有权关系，再在 :doc:`Carrier 协议 <protocol-carrier>` 中追握手与 framing。

一份消息怎样送往多条连接，读 :doc:`写入与扇出 <write-fanout>`；回调与 RPC 怎样在对端发生，读 :doc:`读取与 RPC <read-rpc>`；为什么异步发送不能把 Writer 栈地址长期借出，继续读 :doc:`关闭与生命周期 <close-lifecycle>`。把虚接口、RAII、模板与异步对象关系映射回 :doc:`C++ 设计实验 <cpp-design-lab>`，最后进入 :doc:`iCub 案例 <case-study-icub-navigation>`。""",
    "zenoh": """先在 :doc:`Key Expression 入门 <foundations>` 中理解数据集合与单条消息，再沿 :doc:`Session 的建立 <session-runtime>` 追踪 API、内部实体和 Runtime；随后在 :doc:`Resource 与路由缓存 <resource-route-cache>` 中解释为什么通配匹配不应每帧重算。

有了对象与路由模型，再走一次 :doc:`Publisher 到 Transport <publisher-routing>`，随后进入 :doc:`Query、Reply 和 Final <query-lifecycle>`，亲自模拟两路回复、一条超时和最后的回收。完成 :doc:`背压与关闭 <backpressure-close>` 的故障回放以后，再读 :doc:`Rust/C++ 设计实验 <rust-cpp-design-lab>` 以及 :doc:`rmw_zenoh 案例 <case-study-rmw-zenoh>`。""",
    "ethercat": """先读 :doc:`软件栈总图 <theory-stack>`，把 EtherCAT 与普通 Ethernet、CAN/CANopen、ROS 2 pub/sub 的职责边界分开；随后沿 :doc:`帧与 Datagram <theory-frame-datagram>`、:doc:`PDO/FMMU/过程映像 <theory-pdo-process-image>`、:doc:`AL 状态与邮箱 <theory-state-mailbox>`、:doc:`Distributed Clocks <theory-distributed-clocks>` 和 :doc:`实时性 <theory-realtime>` 建立协议与控制系统直觉。\n\n进入源码以后，从 :doc:`对象架构 <architecture-map>` 和 :doc:`Master 生命周期 <master-lifecycle>` 建立 ownership，再追 :doc:`Domain 与 process image <domain-process-image>`、:doc:`周期收发 <cyclic-send-receive>` 和 :doc:`Datagram/Frame <datagram-frame>` 的数据面；最后进入 :doc:`Slave FSM 与 mailbox <slave-fsm-mailbox>`、:doc:`Device/NIC <device-nic-runtime>`、:doc:`DC 实现 <distributed-clocks>` 与 :doc:`实时并发 <realtime-concurrency>`。读完源码后进入 :doc:`FakeEtherCAT 环境 <use-environment>` 与 :doc:`双进程闭环工程 <closed-loop-project>`，用 controller/plant 把 PDO 和 process image 真正跑成闭环；最后按 :doc:`真机部署 <real-hardware-deployment>` 把同一应用骨架迁移到独立 NIC、真实从站、WKC、DC 和实时调度。所有源码结论统一锁定 stable-1.6 / 1.6.13 的 ``61cc654f5b721ddd54df0f58bdd34106d91c5359``。""",
    "soem": """从 :doc:`总览 <overview>` 开始先回答“为什么同样是 EtherCAT Master，SOEM 可以只是一套用户态 C Library”。随后沿 :doc:`Context 与固定数组 <context-fixed-arrays>`、:doc:`Linux RAW Socket <linux-raw-socket>`、:doc:`Frame Buffer/Index <frame-buffer-index>` 和 :doc:`Datagram 原语 <datagram-primitives>` 建立用户态数据面。\n\n配置链继续走 :doc:`Slave Discovery <slave-discovery>` → :doc:`PDO/SM/FMMU <pdo-sm-fmmu>` → :doc:`IOmap 与周期收发 <iomap-cyclic>`；控制面再进入 :doc:`CoE/SDO <mailbox-coe>`、:doc:`Distributed Clocks <distributed-clocks>` 与 :doc:`双网口冗余 <redundancy>`。最后用 :doc:`OSAL 与实时线程 <realtime-osal>`、:doc:`WKC 与故障恢复 <fault-recovery>`、:doc:`SOEM vs IgH <soem-vs-igh>` 和 :doc:`设计复盘 <design-recap>` 闭环。\n\n主站本体读完后，真实案例按“原生 API → 工程封装 → 机器人驱动 → ros2_control/CiA-402”递进：先看 :doc:`官方 ec_sample <case-study-official-ec-sample>`，再看 :doc:`ETH RSL soem_interface <case-study-leggedrobotics-soem-interface>`、:doc:`Elfin ROS2 机械臂 <case-study-elfin-robot-ros2>` 与 :doc:`IPE ros2_control <case-study-ipe-ros2-control>`。案例只用于验证设计机制怎样进入实际项目，SOEM v2.0.0 本体仍以 ``304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`` 为源码真值。""",
    "cyclonedds": """从 :doc:`总览 <overview>` 先建立 DDSc/DDSI/DDSRT 三层地图，再沿 :doc:`Entity 生命周期 <entity-lifecycle>`、:doc:`SPDP/SEDP <discovery-spdp-sedp>` 与 :doc:`QoS Matching <qos-matching>` 理清控制面如何建立 endpoint。随后从 :doc:`Writer/Reader 创建 <writer-reader-creation>` 进入 :doc:`dds_write 主链 <write-path>`、:doc:`WHC/Reliability <whc-reliability>`、:doc:`RTPS/UDP <rtps-network>`、:doc:`Receive/Reorder <receive-reorder>` 和 :doc:`RHC <rhc-read-take>`，最后用 :doc:`WaitSet/Listener <waitset-listener>`、:doc:`线程与关闭 <threads-async-close>` 和 :doc:`PSMX/Loan <psmx-loans>` 收束执行边界。\n\n源码本体读完后不再造教学 Demo，而是直接进入两条真实链：:doc:`官方 ddsperf <case-study-ddsperf>` 用 Reliability、History、Batching 和 WaitSet 验证性能机制；:doc:`ROS 2 rmw_cyclonedds <case-study-rmw-cyclonedds>` 则把 ROS Publisher/Subscription、QoS 与 Executor wait 一路映射到底层 DDS Writer/Reader、dds_write_ts 与 dds_waitset_wait。Cyclone DDS 本体结论统一以 e54e991f 为真值，RMW 案例固定到 19478b0。""",
    "fastdds": """从 :doc:`总览 <overview>` 建立 DDS façade → Impl → RTPS Endpoint → History → Transport 的对象图，再沿 :doc:`Participant/Endpoint 生命周期 <participant-endpoint-lifecycle>`、:doc:`PDP/EDP <discovery-pdp-edp>`、:doc:`Discovery Server <discovery-server>` 和 :doc:`QoS <qos-matching>` 理清控制面。数据面从 :doc:`Writer/Reader 创建 <writer-reader-creation>` 进入 :doc:`DataWriter::write <write-cachechange>`、:doc:`WriterHistory/Reliability <writerhistory-reliability>`、:doc:`ReaderHistory/Fragments <readerhistory-fragments>` 与 :doc:`Transport <transport-network>`，再用 :doc:`FlowController <flowcontroller-async>`、:doc:`Data Sharing vs SHM <datasharing-vs-shm>`、:doc:`Loan <loan-zero-copy>`、:doc:`WaitSet <waitset-listener>` 和 :doc:`线程/关闭 <threads-events-close>` 收束运行时。\n\n本体之后直接进入真实工程：:doc:`官方 delivery_mechanisms <case-study-delivery-mechanisms>` 用同一业务代码切换 SHM Transport、Data Sharing 与 loan；:doc:`ROS 2 rmw_fastrtps <case-study-rmw-fastrtps>` 把 ROS Publisher、QoS 和 Executor wait 映射到底层 DataWriter 与 Fast DDS WaitSet。最后用 :doc:`Fast DDS vs Cyclone DDS <fastdds-vs-cyclonedds>` 分清 DDS/RTPS 必需机制与两套实现自己的数据结构。""",
    "iceoryx2": """先从总览和架构地图建立 Node、Service、Port、DataSegment 与 ZeroCopyConnection 的对象关系，再沿 SharedMemory/PointerOffset、PoolAllocator、Publisher loan、offset delivery、Subscriber receive/reclaim 走完一条 sample 的完整生命周期。随后进入 fan-out/backpressure/history 与 Event/WaitSet，再用 Request/Response 理解 ChannelId/RequestId/PendingResponse/ActiveRequest 的双向状态机，用 Blackboard 理解共享 latest-state 与 UnrestrictedAtomic；最后进入 dead-node cleanup 和与 eCAL/Fast DDS/Cyclone DDS 的共享内存对照。读完源码后直接跑 :doc:`官方示例实验 <official-examples-lab>`，把 pub/sub、event、event multiplexing、request-response 与 blackboard 映射回真实运行现象。整个专题统一锁定 v0.10.0 的 135d09dd8b29f321f1725920d434864c4e512378。""",
    "ucx": """从 :doc:`总览 <overview>` 与 :doc:`架构地图 <architecture-map>` 建立 UCP/UCS/UCT/UCM 的职责边界，再沿 :doc:`Context、Worker 与 Endpoint <context-worker-endpoint>` 和 :doc:`Lane 选择 <wireup-lane-selection>` 看一条 peer connection 如何由多个 transport lane 组成。数据面以 :doc:`Tag Send 与 Request <tag-send-request>` 为入口，进入 :doc:`协议选择 <protocol-selection>` 与 :doc:`Progress Engine <progress-engine>`；随后下沉到 :doc:`UCT Transport 模型 <uct-transport-model>`、:doc:`Memory Domain 与异构内存 <memory-domain-types>`，再用 :doc:`Rendezvous 与 GPU Pipeline <rendezvous-gpu-pipeline>` 理解大 tensor 如何绕开普通 eager copy。最后用 :doc:`背压与线程安全 <backpressure-thread-safety>` 和 :doc:`与消息中间件的边界 <ucx-vs-message-middleware>` 收束；:doc:`官方 Hello World 实验 <official-hello-world-lab>` 把 endpoint、request、progress 与 eventfd 对回可运行代码。源码真值统一锁定 UCX v1.22.0 的 8a6b06fb880accbb933a79cda893883872c68d9d。""",
    "rosidlbuffer": """rosidl::Buffer 把 message schema 与 payload storage 分层：:doc:`Buffer Backend Contract <buffer-backend-contract>` 定义统一语义，:doc:`CUDA VMM Pool <cuda-vmm-pool>` 展开 std::map<size, vector<VmmBlock*>>、generation、grace 与 remote refcount，:doc:`FD/SHM Registry <ipc-fd-shm-registry>` 把 epoll、eventfd、SCM_RIGHTS、shm_open/mmap 连接到 Linux capability transfer，:doc:`Stream Handle 生命周期 <stream-handles-lifetime>` 再用 CUDA event 与 Recycler 处理异步 device lifetime。:doc:`Accelerator IPC 闭环 <accelerator-ipc-protocols>` 把 CUDA VMM 与 Qualcomm dma-buf 两条真实实现放在同一张 ownership/capability/generation 模型中比较；:doc:`Endpoint Locality 与 Fallback <endpoint-locality-fallback>` 负责 optimized path 的能力判定。:doc:`Isaac ROS 5 迁移案例 <case-study-isaac-ros-5-migration>` 展示这些机制怎样进入真实 GPU 机器人软件栈。""",
    "holoscan": """从 :doc:`总览 <overview>` 先把 Holoscan 看成“图 + 调度 + 内存 + 连接器”的 streaming runtime，而不是一套 GPU API。随后用 :doc:`架构地图 <architecture-map>` 分清 Application、Fragment、Operator、Resource、Condition、Scheduler 和 Executor 的寿命边界，再进入 :doc:`FlowGraph 容器设计 <flowgraph-containers>`。执行面先看 :doc:`Event-Based Scheduler <event-based-scheduler>` 与公开 GXF v3.2-1 的 :doc:`EBS 内部实现 <gxf-event-runtime-internals>`，随后用 :doc:`SchedulingTerm 与 Event Wakeup <scheduling-term-event-wakeup>` 把 MESSAGE_SYNC、MEMORY_FREE、TIME_UPDATE、外部异步事件、SchedulingTerm::check 与 READY/WAIT 状态机连成完整控制链，再进入 :doc:`EntityExecutor 与 MessageRouter <gxf-entity-executor-router>` 追一次 tick。随后读 :doc:`Operator→GXF Entity 物化 <operator-materialization-lifecycle>`，把 initialize_base、GraphEntity/eid、GXFWrapper、Codelet、EntityGroup 和生命周期串起来。数据面继续读 :doc:`Condition、Connector 与背压 <conditions-connectors-backpressure>` 和 :doc:`Allocator/CUDA Memory <allocator-cuda-memory>`，再用 :doc:`CUDA Stream 依赖传播 <cuda-stream-event-propagation>` 理解 CudaStreamId、stream handoff 与异步 buffer lifetime。最后用 :doc:`Distributed Fragment/UCX <distributed-ucx-runtime>` 扩展到跨进程/跨主机，并用两个 HoloHub 固定案例观察真实 GPU streaming pipeline。""",
}

INTERNAL_ONLY_SLUGS = {"reconstruction"}

PAGE_RE = re.compile(r"<!--\s*PAGE:\s*([^\s]+)\s*-->")
ADMONITIONS = {
    "NOTE": "note",
    "WARNING": "warning",
    "DANGER": "danger",
    "ANALYSIS": "important",
    "SOURCE": "seealso",
}


def convert_alerts(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = re.match(r"^>\s*\[!(\w+)\]\s*(.*)$", lines[i])
        if not match:
            out.append(lines[i])
            i += 1
            continue
        kind = ADMONITIONS.get(match.group(1).upper(), "note")
        body = [match.group(2)] if match.group(2) else []
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            body.append(re.sub(r"^>\s?", "", lines[i]))
            i += 1
        out.extend([f":::{kind}", *body, ":::"])
    return "\n".join(out)


def normalize_page(block: str) -> tuple[str, str]:
    block = convert_alerts(block.strip())
    title_match = re.search(r"^#\s+(.+)$", block, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled"
    lines = block.splitlines()
    normalized: list[str] = []
    inserted_contents = False
    for line in lines:
        meta = re.match(r"^(Goal|Tutorial level|Time|Source|Commit):\s*(.+)$", line)
        if meta:
            key, value = meta.groups()
            if key == "Source":
                normalized.append(f"**源码入口：** {value}")
            elif key == "Commit":
                normalized.append(f"**固定版本：** {value}")
        else:
            normalized.append(line)
    if not inserted_contents:
        normalized[1:1] = ["", "```{contents} 本页目录", ":depth: 2", ":local:", "```", ""]
    return title, "\n".join(normalized).rstrip() + "\n"


def rewrite_generated_project_links(page: str, project: str) -> str:
    """Keep source-relative links valid after articles/guides share one folder.

    content/articles/<project> and content/guides/<project> both become
    docs/generated/<project>. Rewrite only explicit same-project crosslinks.
    """
    pattern = re.compile(
        r"\]\(\.\./\.\./(?:articles|guides)/([^/]+)/"
        r"([^/)#]+\.md)(#[^)]*)?\)"
    )

    def replace(match: re.Match[str]) -> str:
        if match.group(1) != project:
            return match.group(0)
        return "](" + match.group(2) + (match.group(3) or "") + ")"

    return pattern.sub(replace, page)



def cyber_course_navigation() -> str:
    blocks = ["""组件触发模型：消息触发与周期触发
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. toctree::
   :maxdepth: 2

   overview
"""]
    for title, slugs in CYBER_ARTICLE_SECTIONS:
        entries = '\n'.join(f'   {slug}' for slug in slugs)
        underline = '~' * 80
        blocks.append(
            f'''{title}
{underline}

.. toctree::
   :maxdepth: 2

{entries}
'''
        )
    return '\n'.join(blocks)


def ethercat_course_navigation() -> str:
    blocks = ['''主站软件栈总图
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. toctree::
   :maxdepth: 2

   overview
''']
    for title, slugs in ETHERCAT_ARTICLE_SECTIONS:
        entries = '\n'.join(f'   {slug}' for slug in slugs)
        underline = '~' * 80
        blocks.append(
            f'''{title}
{underline}

.. toctree::
   :maxdepth: 2

{entries}
'''
        )
    return '\n'.join(blocks)


def soem_course_navigation() -> str:
    blocks = ['''SOEM 主站架构总图
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. toctree::
   :maxdepth: 2

   overview
''']
    for title, slugs in SOEM_ARTICLE_SECTIONS:
        entries = '\n'.join(f'   {slug}' for slug in slugs)
        underline = '~' * 80
        blocks.append(
            f'''{title}
{underline}

.. toctree::
   :maxdepth: 2

{entries}
'''
        )
    return '\n'.join(blocks)


def write_project_index(
    project: str,
    pages: list[tuple[str, str]],
    guides: list[tuple[str, str]],
) -> None:
    name, commit = PROJECTS[project]
    if commit == PENDING_SOURCE:
        version_line = (
            "源码版本状态：本地上游源码尚未纳入 source-audit；"
            "固定提交将在首次源码核验时写入。"
        )
    else:
        version_line = f"固定源码版本：``{commit}``"
    if project == 'cyber':
        folder = GENERATED / project
        folder.mkdir(parents=True, exist_ok=True)
        guide_entries = '\n'.join(f'   {slug}' for slug, _ in guides)
        guide_navigation = f'''
实际开发与生产案例
------------------

.. toctree::
   :maxdepth: 2

{guide_entries}
'''
        text = f'''{name}
{'=' * len(name)}

{version_line}

{PROJECT_OVERVIEWS[project]}

源码机制
--------

{cyber_course_navigation()}

{guide_navigation}
'''
        write_if_changed(folder / 'index.rst', text)
        return

    folder = GENERATED / project
    folder.mkdir(parents=True, exist_ok=True)
    underline = "=" * len(name)
    source_slugs = [slug for slug, _ in pages] + PROJECT_EXTRAS.get(project, [])
    source_entries = "\n".join(f"   {slug}" for slug in source_slugs)
    guide_entries = "\n".join(f"   {slug}" for slug, _ in guides)
    source_navigation = ""
    if source_entries:
        source_navigation = f"""
源码解读
--------

.. toctree::
   :maxdepth: 2

{source_entries}
"""
    guide_navigation = ""
    if guide_entries:
        guide_navigation = f"""
使用教程
--------

.. toctree::
   :maxdepth: 2

{guide_entries}
"""
    if project == 'cyber':
        source_navigation = cyber_course_navigation()
    if project == 'ethercat':
        source_navigation = ethercat_course_navigation()
    if project == 'soem':
        source_navigation = soem_course_navigation()
        if guide_entries:
            guide_navigation = f'''
真实工程案例
------------

.. toctree::
   :maxdepth: 2

{guide_entries}
'''
    text = f"""{name}
{underline}

{version_line}

{PROJECT_OVERVIEWS[project]}

{source_navigation}
{guide_navigation}
"""
    write_if_changed(folder / "index.rst", text)


def main() -> None:
    GENERATED.mkdir(parents=True, exist_ok=True)
    # docs/generated is disposable build input. Remove stale Markdown pages
    # before regeneration so deleted/renamed source or guide pages cannot
    # survive as orphan Sphinx documents.
    for project in PROJECTS:
        folder = GENERATED / project
        if folder.is_dir():
            for stale in folder.glob('*.md'):
                stale.unlink()
    grouped: dict[str, list[tuple[str, str]]] = {key: [] for key in PROJECTS}
    guide_grouped: dict[str, list[tuple[str, str]]] = {key: [] for key in PROJECTS}
    for path in sorted(CONTENT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        matches = list(PAGE_RE.finditer(text))
        for index, match in enumerate(matches):
            route = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title, page = normalize_page(text[match.end():end])
            if "/" in route:
                project, slug = route.split("/", 1)
                if project not in PROJECTS:
                    continue
                if slug in INTERNAL_ONLY_SLUGS:
                    continue
                folder = GENERATED / project
                folder.mkdir(parents=True, exist_ok=True)
                write_if_changed(folder / f"{slug}.md", page)
                grouped[project].append((slug, title))

    # New long-form articles use one source file per page.  Keeping them under
    # content/articles makes the authoring source unambiguous while generated/
    # remains disposable build input.
    for project in PROJECTS:
        article_dir = ARTICLES / project
        if not article_dir.is_dir():
            continue
        order = {slug: index for index, slug in enumerate(ARTICLE_ORDER.get(project, []))}
        paths = sorted(
            article_dir.glob("*.md"),
            key=lambda item: (order.get(item.stem, len(order)), item.stem),
        )
        for path in paths:
            slug = path.stem
            title, page = normalize_page(path.read_text(encoding="utf-8"))
            page = rewrite_generated_project_links(page, project)
            folder = GENERATED / project
            folder.mkdir(parents=True, exist_ok=True)
            write_if_changed(folder / f"{slug}.md", page)
            grouped[project].append((slug, title))

    # Practical guides are kept separate from source analysis while sharing
    # the same project landing page and navigation tree.
    for project in PROJECTS:
        guide_dir = GUIDES / project
        if not guide_dir.is_dir():
            continue
        order = {slug: index for index, slug in enumerate(GUIDE_ORDER.get(project, []))}
        paths = sorted(
            guide_dir.glob("*.md"),
            key=lambda item: (order.get(item.stem, len(order)), item.stem),
        )
        for path in paths:
            slug = path.stem
            title, page = normalize_page(path.read_text(encoding="utf-8"))
            page = rewrite_generated_project_links(page, project)
            folder = GENERATED / project
            folder.mkdir(parents=True, exist_ok=True)
            write_if_changed(folder / f"{slug}.md", page)
            guide_grouped[project].append((slug, title))

    for project, pages in grouped.items():
        write_project_index(project, pages, guide_grouped[project])

    source_total = sum(len(pages) for pages in grouped.values())
    guide_total = sum(len(pages) for pages in guide_grouped.values())
    print(f"generated {source_total} source pages and {guide_total} guide pages")


if __name__ == "__main__":
    main()
