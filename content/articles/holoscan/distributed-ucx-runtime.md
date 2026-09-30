# Distributed Fragment 与 UCX：一条 Graph Edge 怎样变成网络连接

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

同一 Fragment 里：

~~~text
Operator A → Operator B
~~~

通常还是本进程 connector。

一旦 A/B 属于不同 Fragment，逻辑 edge 必须被 materialize 成真正的 distributed data path。

Holoscan 选择 UCX。

这篇不再重复 UCX 内部 lane/protocol 细节；那些已经在 Atlas 的 OpenUCX 专题拆过。这里重点看 **Holoscan 怎样把图语义翻译成 UCX runtime resource，并且怎样让 queue、serializer、scheduler、shutdown 与网络连接组合起来。**

## 跨 Fragment以后，Edge 需要多出哪些状态

本地 edge 大致只关心：

~~~text
source output port
destination input port
connector capacity/policy
~~~

分布式 edge 至少还要增加：

~~~text
receiver address
receiver port
local address / port
connection retry
serialization buffer
network context
transport lifetime
~~~

所以 graph partition 会把“一条线”扩展成一组 runtime object。

## UcxTransmitter/Receiver 仍然保留 DoubleBuffer 语义

固定 header 明确写道：UcxTransmitter/UcxReceiver 基于与 DoubleBufferTransmitter/Receiver 相同的 double-buffer queue，同时增加 serialization 与 UCX network path。

因此分布式化并没有把 queue/backpressure 问题消掉。

仍然有：

~~~text
capacity
policy: pop / reject / fault
~~~

只是 queue 后面多了 network transport。

这点非常重要：

> 网络不是队列的替代品，网络只是在两个 queue/domain 之间增加新的 transport stage。

## 一条跨 Fragment 数据路径

可以画成：

~~~text
Operator A
↓
local output message/entity
↓
UcxTransmitter double-buffer stage
↓
UcxSerializationBuffer / serializers
↓
UCX active-message transport
↓
network / IPC / accelerator-capable path
↓
UcxReceiver double-buffer stage
↓
MessageAvailableCondition
↓
Scheduler
↓
Operator B
~~~

这条链同时有：

- application backlog；
- transmitter queue；
- serialization buffer；
- UCX in-flight request；
- receiver queue；
- scheduler ready queue。

所以端到端 backpressure 绝不只有一个 depth。

## UcxTransmitter 初始化为什么会自动创建 SerializationBuffer

固定 `UcxTransmitter::initialize()`：

~~~cpp
auto has_buffer = std::find_if(args().begin(), args().end(), ...);

if (has_buffer == args().end()) {
    auto buffer = frag->make_resource<UcxSerializationBuffer>(
        "ucx_tx_serialization_buffer");
    add_arg(Arg("buffer") = buffer);
}
~~~

Receiver 同样如此。

这说明 wrapper 在初始化时补齐依赖资源图：

~~~text
UcxTransmitter
└── UcxSerializationBuffer
~~~

SerializationBuffer 如果没有 allocator，又会创建 UnboundedAllocator。

最终：

~~~text
Transport resource
└── Serialization buffer
    └── Allocator
~~~

这就是 runtime resource dependency graph。

## SerializationBuffer 为什么是独立对象

最简单做法是在每次 send 时：

~~~cpp
std::vector<std::byte> tmp;
serialize(message, tmp);
send(tmp);
~~~

但 streaming runtime 会不停重复：

~~~text
allocate temporary
serialize
free
~~~

固定 SerializationBuffer 让 buffer capacity 与 allocator 都成为可配置资源。

更重要的是，它把：

~~~text
message semantic serialization
与
transport queue
~~~

分开。

## HOLOSCAN_UCX_SERIALIZATION_BUFFER_SIZE 为什么还要联动 UCX TCP segment

固定 `ucx_serialization_buffer.cpp` 在读取：

~~~text
HOLOSCAN_UCX_SERIALIZATION_BUFFER_SIZE
~~~

后，还会根据需要调整：

~~~text
UCX_TCP_RX_SEG_SIZE
UCX_TCP_TX_SEG_SIZE
~~~

源码甚至给出运行时错误背景：

~~~text
RTS is too big ... max ...
~~~

这说明 abstraction layer 并不是完全隔离的。

上层 serialization header 太大，最终仍然会撞到底层 UCX transport/protocol limit。

真实系统里所谓“封装”经常只是把依赖隐藏起来，不是让依赖消失。

## Holoscan Message 为什么需要 CodecRegistry

普通 C struct 可以固定 schema。

Holoscan `Message` 内部可能装不同 C++ 类型。

serializer 会先：

~~~cpp
auto index = std::type_index(message.value().type());
auto& registry = holoscan::CodecRegistry::get_instance();
auto maybe_name = registry.index_to_name(index);
~~~

再把 codec name 写入 endpoint，然后取对应 serializer。

协议逻辑：

~~~text
type_index
↓
codec name
↓ serialize codec name
↓
type-specific serializer
↓
bytes
~~~

Receiver 反过来先读 codec name，再从 registry 找 deserializer。

这是一种 runtime type-erasure serialization。

## 为什么 MetadataDictionary 不能只 memcpy

MetadataDictionary 是：

~~~text
key → Message(value)
~~~

serializer 先发送 item count，然后对每个 item：

~~~text
serialize key string
serialize Holoscan Message value
~~~

所以 distributed graph 中 metadata 本身也有 wire cost。

如果把大量调试/追踪信息塞进 metadata，不是“免费附带”。

## MessageLabel 为什么会撞 Buffer Capacity

Data Flow Tracking 会给 message 带路径/timestamp label。

固定 serializer 对 MessageLabel：

~~~text
num paths
↓
per path:
  num operators
  operator_name
  receive timestamp
  publish timestamp
~~~

序列化完成后还会检查：

~~~cpp
if (total_size > buffer_capacity) {
    ... error ...
}
~~~

源码特别说明这是 non-tensor data 的 UCX serialization-buffer limitation。

这揭示 observability 的真实成本：

> 追踪数据越丰富，控制/metadata plane 自己也可能成为负载。

## 为什么不能把 Tensor 与 Metadata 当成同一种流量

一个 20 MB Tensor 和一个几十字节 timestamp label 对 transport 的最佳策略完全不同。

这也正是我们前面 UCX 专题强调的：

~~~text
small control metadata
vs
large payload
~~~

应该允许不同 protocol/memory movement。

Holoscan 上层 Graph 仍然把它们组织成同一 message/entity，但底层 serializer/UCX 会面对不同数据特征。

## 分布式应用为什么默认更偏向 EventBasedScheduler

固定 `application.cpp` 明确写：

~~~cpp
// we'll use EventBasedScheduler as it works better
// with UCX connections for distributed apps.
~~~

而且更强：

~~~cpp
// GreedyScheduler is not supported for
// multi-fragment apps as it causes deadlock.
~~~

Greedy 会被覆盖成 EventBased。

为什么网络一加入，scheduler correctness 都会变化？

因为 distributed readiness 依赖外部 actor：

~~~text
remote Fragment
network connection
UCX progress/events
receiver arrival
~~~

如果 scheduler 只按单图局部“没有 ready work”判断结束/死锁，很容易把“正在等待远端”误判成系统不会再前进。

这就是 distributed system 里经典的：

> 本地 idle != 全局 deadlock。

## network_connection_timeout 为什么独立于 deadlock timeout

EventBasedScheduler wrapper 有：

~~~text
stop_on_deadlock_timeout
network_connection_timeout
~~~

固定源码说明 network connection establishment 阶段使用更长 timeout，默认 5000 ms，避免 UCX 建连时触发 false deadlock。

这又是状态机分阶段的体现：

~~~text
STARTING / CONNECTING
  timeout A

RUNNING / WAITING
  timeout B
~~~

不要用一个 timeout 参数解决所有生命周期阶段。

## Shutdown 为什么不能直接 interrupt Executor

这是这一页最有价值的源码之一。

固定 `application.cpp`：

~~~cpp
// Use stop_execution() instead of executor().interrupt()
// to allow queued UCX messages to be sent
root_fragment->stop_execution();

std::this_thread::sleep_for(
    std::chrono::milliseconds(fragment_shutdown_grace_period_ms));
~~~

源码继续解释：如果等待不足，未发送 UCX message 会丢失。

所以 distributed shutdown 必须考虑：

~~~text
application stops producing
↓
queued transmitter messages
↓
UCX in-flight sends
↓
remote receive
↓
connection teardown
~~~

直接把 executor thread interrupt 掉，相当于从控制面把数据面电源拔掉。

## 为什么停止 Fragment 要按 Graph Root 层层推进

固定 application shutdown 会找当前 root fragments，停止一层、等待 grace period、从 fragment graph 删除，再寻找下一层 root。

概念上：

~~~text
upstream roots stop
↓
allow outgoing data drain
↓
remove roots
↓
next layer becomes root
↓
stop next layer
~~~

这是 dependency-aware shutdown，而不是所有 Fragment 同时 kill。

对于 pipeline：

~~~text
Source → Preprocess → Inference → Sink
~~~

先停 Source 才能让下游把已有数据收干净。

这和线程池 shutdown 的：

~~~text
close producer
→ drain queue
→ stop consumers
~~~

完全同构。

## Queue Capacity 在网络路径上为什么更危险

本地 queue 深度 10 可能只意味着多几个 object。

跨主机 queue 深度 10 可能意味着：

~~~text
10 × 20 MB tensors
= 200 MB in-flight logical backlog
~~~

而且真正占用可能分布在：

~~~text
sender pool
serialization buffers
UCX requests
NIC queues
receiver buffers
~~~

所以 distributed pipeline 更应该关注 **Data Age**，而不是只扩大 capacity。

## 一个错误的“解决网络抖动”方法

~~~text
network sometimes slow
→ increase queue from 2 to 100
~~~

短期 drop 可能少了。

但 30 FPS 视频：

~~~text
100 frames
≈ 3.3 seconds history
~~~

对机器人感知几乎已经失去实时意义。

更好的问题是：

~~~text
这个数据必须完整吗？
还是 latest frame 更重要？
是否应该 drop-old？
是否应该降 producer rate？
是否应该在 source 侧产生 backpressure？
~~~

## UCX 这里只是 Data Plane，不负责全部系统语义

UCX 能解决：

- endpoint/transport；
- protocol selection；
- shared-memory/network/RDMA/GPU-aware movement；
- request/progress。

但它不替 Holoscan 决定：

~~~text
哪个 Operator 应该运行？
queue overflow 丢谁？
Fragment 什么时候算 deadlock？
应用 shutdown 顺序？
Tensor semantic type 是什么？
~~~

因此分层应保持：

~~~text
Application / Graph semantics
↓
Scheduler / Conditions
↓
Connector / Serialization
↓
UCX transport
↓
NIC / shared memory / GPU path
~~~

## 这和具身大模型分布式推理有什么直接关系

未来一个 VLA pipeline 很可能是：

~~~text
Robot-side camera
↓
GPU vision encoder
↓
visual tokens / embeddings
↓ network
policy / reasoning GPU
↓
action
~~~

不应该简单设计成：

~~~text
serialize everything
→ unbounded queue
→ TCP socket
~~~

更合理的是分层：

~~~text
Graph semantics:
  frame id / deadline / tensor schema

Scheduler:
  only run when downstream/memory available

Buffer:
  GPU/pinned pools

Transport:
  UCX / RDMA / staged fallback

Overload:
  freshness-aware policy
~~~

Holoscan Distributed Fragment 正好提供了一个成熟参考。

## 本篇真正要带走的五条原则

1. 跨主机以后，逻辑 Edge 会膨胀成 queue + serializer + transport + remote queue。
2. 本地 idle 不等于全局 deadlock，distributed scheduler 必须理解外部 progress。
3. Serialization metadata 也有容量和成本，不是免费控制面。
4. Shutdown 是网络协议的一部分；先停 producer，再 drain in-flight。
5. 增大 queue 不是网络抖动的万能解，实时系统必须同时看 Data Age。
