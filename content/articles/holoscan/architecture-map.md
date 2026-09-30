# Holoscan 架构地图：从 Application 到 OS Worker，先把对象寿命和执行责任分开

固定源码版本：`66a9609ac37515405561b9b8dbdee8e57f41ab11`（Holoscan SDK v4.6.0）。

读 Holoscan 最容易出现的错误，是把 Application、Fragment、Operator、GXF Entity、Thread 当成同一层的“节点”。

它们不是。

本篇先不追单个算法，而是按照**寿命、所有权、执行职责**拆 runtime。

## 从一个单体 Pipeline 开始

如果自己写简单程序，很容易先得到：

~~~cpp
class Pipeline {
public:
    Camera camera;
    Preprocessor preprocessor;
    Inference inference;
    Visualizer visualizer;

    void run();
};
~~~

单线程时这个结构足够。

一旦需要图连接、多线程、GPU resource、分布式部署和可配置生命周期，一个 `Pipeline` 对象会承担太多责任。

于是必须拆出：

~~~text
deployment owner
graph topology
algorithm object
shared resource
scheduling condition
scheduler
execution backend
~~~

Holoscan 的对象体系就是围绕这些责任出现的。

## Application / Fragment 是长期图 owner

一个 Application 可以拥有多个 Fragment。

~~~text
Application
├── Fragment A
│   ├── Operator A1
│   └── Operator A2
└── Fragment B
    ├── Operator B1
    └── Operator B2
~~~

Fragment 保存自己的 Graph、Executor、Scheduler、NetworkContext、ThreadPool 和 resource/service registry。

固定 `Fragment::graph_shared()`：

~~~cpp
std::shared_ptr<OperatorFlowGraph> Fragment::graph_shared() {
  if (!graph_) {
    graph_ = make_graph<OperatorFlowGraphImpl>();
  }
  return graph_;
}
~~~

这说明 graph 是 lazy-created long-lived object，不是每个 tick 临时构造一次。

## Graph 只描述 topology，不执行 compute

FlowGraph 的节点类型直接定义成：

~~~cpp
using OperatorNodeType = std::shared_ptr<Operator>;
~~~

Edge data 则是 output port 到 input port 集合的映射。

因此：

~~~text
A ──> B
~~~

只表示逻辑连接。

它没有回答：

~~~text
B 在哪条线程？
A/B 能否并行？
connector capacity 是多少？
payload 在 CPU 还是 GPU？
B 当前 READY 吗？
~~~

所以 Graph 是**结构状态**，不是**执行状态**。

## Operator 是业务状态机，不是 pthread

更合理的关系是：

~~~text
Operator object
  │
  ├── algorithm state
  ├── IOSpec
  └── Conditions
          ↓
       Scheduler
          ↓
      Worker Thread
          ↓
       compute()
~~~

这件事直接影响 C++ 设计。

假设 Operator 里有：

~~~cpp
std::vector<float> history_;
State state_;
~~~

不能因为“一个 Operator 是一个组件”就默认这些成员永远只被固定线程访问。

必须继续确认：

- scheduler 是否可能换 worker；
- 同一个 Operator 是否可能 re-enter；
- 是否被显式 pin 到 ThreadPool；
- callback/resource 是否从其他线程修改其状态。

## Resource 为什么从 Operator 中独立出来

Holoscan 的 Resource 包括 Allocator、CudaStreamPool、ThreadPool、Receiver/Transmitter、Clock 等。

把资源独立建模，有两个好处。

第一，共享关系显式。

例如多个 Operator 可以引用同一个：

~~~cpp
std::shared_ptr<CudaStreamPool>
~~~

第二，算法对象不需要拥有底层硬件资源的全部生命周期。

于是可以得到：

~~~text
Operator:
  “我需要一个 CUDA stream pool”

Runtime:
  “这个 pool 由谁创建、共享、初始化、销毁”
~~~

算法职责和 resource ownership 被拆开。

## ThreadPool 为什么甚至拥有独立 GXF Entity

`Fragment::make_thread_pool()` 非常值得看：

~~~cpp
auto pool_entity =
    std::make_shared<nvidia::gxf::GraphEntity>();

auto maybe_pool =
    pool_entity->setup(
        executor().context(),
        pool_entity_name.c_str());

auto pool_resource =
    make_resource<ThreadPool>(
        name,
        holoscan::Arg("initial_size", initial_size));
~~~

源码注释明确说 ThreadPool unlike a typical Condition/Resource, does not belong to the same entity as an operator。

随后：

~~~cpp
thread_pools_.push_back(pool_resource);
~~~

Fragment 保存所有 pool，等 GXFExecutor 后续完成 entity-group 绑定。

这揭示一个成熟 runtime 的重要拆分：

> **Operator topology != execution-resource topology**

同一张算法图可以通过不同 ThreadPool 划分获得完全不同的 CPU 拓扑。

## Condition 是 Operator 与 Scheduler 的协议

没有 Condition 时，Scheduler 最多知道“这个 Operator 存在”。

真正需要的是：

~~~text
这个 Operator 此刻执行有没有意义？
~~~

例如：

~~~text
input >= 1
AND
downstream free >= 1
AND
memory block >= 1
AND
CUDA event satisfied
~~~

因此 Condition 把不同 subsystem 的状态转成统一 scheduling state：

| Condition | 对应真实资源 |
| --- | --- |
| MessageAvailable | Receiver queue |
| DownstreamMessageAffordable | downstream capacity |
| Periodic | clock |
| MemoryAvailable | allocator |
| CUDA Event/Stream | GPU completion |
| Asynchronous | external actor |

Scheduler 不需要理解每种资源的实现细节，只需要消费统一的 readiness 结果。

## Scheduler 与 Executor 为什么要分开

固定 `Fragment::executor_shared()`：

~~~cpp
if (!executor_ && !is_gpu_resident_) {
  executor_ = make_executor<gxf::GXFExecutor>();
}
~~~

Scheduler 则单独设置。

可以把职责先压成：

~~~text
Scheduler
  what can run now?
  which worker?
  when to re-check?

Executor
  materialize GXF graph
  create runtime entities
  wire resources/connectors
  drive lifecycle
~~~

这样 execution backend 与 scheduling policy 不会绑死。

## 单 Fragment 默认 Greedy，为什么 Multi-Fragment 又换 EventBased

`Fragment::scheduler()` 在普通情况下可以 lazy-create GreedyScheduler。

但分布式 Application 在 `application.cpp` 中有额外策略：

~~~cpp
// we'll use EventBasedScheduler as it works better
// with UCX connections for distributed apps.
return SchedulerType::kEventBased;
~~~

更进一步，固定源码直接写道：

~~~cpp
// GreedyScheduler is not supported for
// multi-fragment apps as it causes deadlock.
~~~

如果 multi-fragment 配成 Greedy，会覆盖成 EventBasedScheduler。

这说明一个非常重要的系统原则：

> 局部对象的默认策略，不一定在全局 topology 下仍然正确。

很多 runtime 问题恰恰发生在“每个模块单测都没问题，组合后却失效”。

## GXF Entity 与 Holoscan Operator 不要混成一个概念

Holoscan SDK 是上层编程模型，GXF 是底层图运行时。

初始化以后通常可以理解为：

~~~text
Holoscan Operator
↓ materialize
GXF Entity
├── runtime/codelet component
├── Receiver
├── Transmitter
├── SchedulingTerm
└── Resource handles
~~~

这和：

~~~text
ROS Publisher
↓
RMW Publisher
↓
DDS DataWriter
~~~

属于同一种 façade → backend runtime 映射。

读源码时一定要知道当前位于哪一层。

## FlowGraph 为什么使用 shared_ptr

NodeType 是：

~~~cpp
std::shared_ptr<Operator>
~~~

不是说“所有权越共享越好”。

图构造阶段确实存在多个结构需要同时引用同一个 Operator：

~~~text
Fragment registry
FlowGraph
OperatorSpec
Executor materialization
Resources / Conditions
~~~

shared_ptr 很方便表达这些共享引用。

但 runtime 中仍然会出现 raw borrowed pointer。

例如 tracking wrapper 内部有：

~~~cpp
holoscan::Operator* op_ = nullptr;
~~~

它不会延长 Operator 生命周期。

所以 shutdown order 仍然必须正确。

> 智能指针解决局部 ownership 表达，不会自动解决整个 runtime 的销毁协议。

## Resource 本身也会形成依赖图

以 UCX 为例。

UcxTransmitter 需要：

~~~text
UcxSerializationBuffer
~~~

SerializationBuffer 又需要：

~~~text
Allocator
~~~

如果没有显式提供，wrapper 会自动创建。

所以隐式资源图：

~~~text
UcxTransmitter
└── UcxSerializationBuffer
    └── UnboundedAllocator
~~~

这解释了为什么很多 initialize() 代码都先：

~~~cpp
add_arg(...)
~~~

最后才：

~~~cpp
GXFResource::initialize();
~~~

父级 component materialize 之前，依赖必须先存在。

## GPU Tensor 又增加一条异步生命周期

CPU 对象的典型寿命：

~~~text
create
→ use
→ destructor
~~~

GPU Tensor 还要考虑：

~~~text
CPU creates/wraps buffer
↓
CUDA kernel starts
↓
CPU call returns
↓
kernel still using buffer
↓
CUDA event/stream completion
↓
buffer may be recycled
~~~

固定 `OutputContext::emit()` 在添加 Tensor 到 GXF Entity 时会取得输出 CUDA stream。

源码注释直接说明：

> stream-aware deallocation allows allocators like BlockMemoryPool to defer memory reuse until GPU operations complete.

这就是为什么 allocator/resource ownership 与 CUDA stream 不能分开理解。

## Shutdown 是对象关系的最终验证

分布式 runtime 的关闭尤其能暴露 ownership 是否正确。

固定 `application.cpp` 没有粗暴 interrupt，而是：

~~~cpp
root_fragment->stop_execution();
std::this_thread::sleep_for(...);
~~~

源码给出的原因：

~~~text
allow queued UCX messages to be sent
prevent unsent UCX messages from being lost
~~~

所以 shutdown 不是：

~~~text
stop = true
→ destruct everything
~~~

而更像：

~~~text
stop new production
↓
let queued / in-flight work settle
↓
stop operators
↓
remove graph roots
↓
release connector/network resources
↓
destroy long-lived owners
~~~

这和 UCX request、iceoryx2 sample、线程池 task 的关闭原则完全一致。

## 最有用的架构图不是 UML，而是 Runtime Ownership Map

~~~text
Application
│
├── Fragment
│   ├── OperatorFlowGraph
│   │   ├── shared_ptr<Operator>
│   │   └── port-map edges
│   │
│   ├── Scheduler
│   │   └── Clock
│   │
│   ├── ThreadPool(s)
│   │   └── OS workers
│   │
│   ├── Executor
│   │   └── GXF context/entities
│   │
│   ├── Resources
│   │   ├── Allocator
│   │   ├── CudaStreamPool
│   │   ├── Receiver
│   │   └── Transmitter
│   │
│   └── Conditions
│       └── scheduling terms
│
└── other Fragments
~~~

跨 Fragment 再增加：

~~~text
UcxTransmitter
↓
UCX
↓
UcxReceiver
~~~

## 可以迁移到自研 Runtime 的四个原则

### 按寿命拆对象

~~~text
Runtime/Application
  long-lived

ExecutionDomain/Fragment
  deployment lifetime

Operator
  algorithm lifetime

Message/Tensor
  per-item lifetime
~~~

### Graph 和 Scheduler 分离

~~~text
Graph:
what depends on what?

Scheduler:
what can run now?
~~~

### CPU/GPU Resource 单独建模

ThreadPool、Allocator、CudaStreamPool 不是算法实现细节。

它们是并行度、jitter、backpressure 和 memory capacity 的直接控制器。

### 用 shutdown 反向检查 ownership

只要还有一个问题答不出来：

~~~text
谁先停止生产？
谁 drain queue？
谁等 CUDA/UCX completion？
谁最后释放 allocator/network context？
~~~

就说明对象关系还没有真正理解。
