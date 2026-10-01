# Holoscan Operator 如何物化成 GXF Entity：从 C++ 图对象到可调度 Codelet

固定源码版本：

- Holoscan SDK v4.6.0：`66a9609ac37515405561b9b8dbdee8e57f41ab11`
- NVIDIA GXF v3.2-1：`daf1810358301f642374dfb3d725be349bba5ec0`

在 Holoscan 应用代码里，我们通常先写出这样的图：

~~~cpp
auto source = make_operator<CameraOp>("camera");
auto infer = make_operator<InferenceOp>("infer");
add_flow(source, infer);
~~~

从 C++ 使用者视角看，`source` 和 `infer` 已经“存在”了。但此时它们还不是 GXF Scheduler 能执行的对象：没有 runtime entity id，没有真正的 Receiver/Transmitter，没有 Codelet，也没有调度状态。

这篇文章只追一件事：

> 一个上层 `Operator` 对象，怎样一步步变成 GXF 中能够被调度、接收消息、执行 `compute()`、再发出消息的运行时实体？

这条链同时解释 Holoscan 为什么需要 Executor、GraphEntity、Codelet、EntityGroup 和 EntityExecutor，以及这些对象各自解决什么问题。

## 1. “图里有一个 Operator”与“运行时有一个执行实体”是两回事

构图阶段的 `Operator` 更接近声明对象。它保存：

- `OperatorSpec`；
- input/output `IOSpec`；
- Condition 和 Resource；
- 参数；
- 与其他 Operator 的图关系；
- 用户实现的 `setup()`、`initialize()`、`compute()`。

但 Scheduler 真正需要的东西完全不同。它至少要知道：

~~~text
这个执行单元的 runtime identity 是什么？
它有哪些可调度条件？
它的输入队列在哪里？
它的输出队列在哪里？
真正调用哪个 Codelet？
它属于哪个 EntityGroup / ThreadPool？
当前是 READY、TICKING 还是 STOP_PENDING？
~~~

因此 Holoscan 必须经过一次“物化”：

~~~text
声明层
Operator + IOSpec + Condition + Resource
                  |
                  | GXFExecutor
                  v
运行时层
GraphEntity(eid)
├── GXFWrapper Codelet(cid)
├── Receiver / Transmitter
├── SchedulingTerm
├── Resource
└── runtime lifecycle state
~~~

这里最值得记住的不是类名，而是职责变化：

> 构图阶段描述“系统应该是什么”；物化阶段建立“runtime 实际要操作的对象”。

## 2. 为什么不能让 Scheduler 直接调 C++ Operator 指针

最朴素的实现可以想成：

~~~cpp
for (Operator* op : graph) {
  if (ready(op)) {
    op->compute(...);
  }
}
~~~

一开始似乎很简单，但很快会碰到问题。

首先，`ready(op)` 到底看什么？它可能同时依赖消息数量、下游容量、周期条件、异步事件和内存可用量。

其次，输入输出也不是普通函数参数。消息可能先进入 staging queue，只有 tick 前才对当前执行可见；输出也可能等 tick 结束后再被路由。

再者，多个 worker 不能同时重入同一个 Operator，否则业务成员变量、队列和内部缓存都会失去串行语义。

最后，一个 ThreadPool 或 GPUDevice resource 需要绑定到稳定的 runtime identity，而不是一个随上层对象生命周期变化的裸 C++ 地址。

所以 GXF 选择了更清晰的分层：

~~~text
Scheduler
  决定“什么时候、由哪个 worker 尝试执行”

EntityExecutor
  决定“这个 Entity 当前是否真的可以进入一次 tick”

Codelet
  提供 runtime 可调用的执行入口

GXFWrapper
  把 Codelet::tick() 适配到 Holoscan Operator::compute()
~~~

这就是 materialization 的根本动机。

## 3. initialize_base()：用户初始化与框架不变量之间的边界

`Operator::initialize()` 默认只调用 `initialize_base()`。固定版本的真实实现是：

~~~cpp
void Operator::initialize_base() {
  if (framework_initialized_) {
    return;  // idempotency guard
  }
  framework_initialized_ = true;

  auto fragment_ptr = fragment();
  if (fragment_ptr) {
    auto& executor = fragment_ptr->executor();
    if (executor.initialize_operator(this)) {
      this->set_op_backend();
    }
    if (!is_metadata_enabled_.has_value()) {
      is_metadata_enabled_ = fragment_ptr->is_metadata_enabled();
    }

    if (spec_) {
      for (auto& [port_name, input_spec] : spec_->inputs()) {
        input_spec->set_unique_id(fmt::format("{}.{}", qualified_name(), port_name));
      }
      for (auto& [port_name, output_spec] : spec_->outputs()) {
        output_spec->set_unique_id(fmt::format("{}.{}", qualified_name(), port_name));
      }
    }

    ensure_contexts();
  } else {
    HOLOSCAN_LOG_WARN("Operator::initialize_base() - Fragment is not set");
  }
}

void Operator::initialize() {
  initialize_base();
}
~~~

这里的第一层设计是幂等：

~~~text
framework_initialized_ == true
→ 再次调用直接返回
~~~

为什么需要它？因为 `initialize()` 同时是用户可扩展 hook 和框架生命周期的一部分。只靠“用户一定记得调用 base class”并不稳健。

可以构造一个很常见的错误：

~~~cpp
class MyOp : public Operator {
 public:
  void initialize() override {
    load_my_model();
    // 忘了 Operator::initialize()
  }
};
~~~

如果框架的关键初始化完全依赖这句 base call，那么一个业务层小失误就会导致：

~~~text
Operator 对象存在
但 GraphEntity / Codelet / port 没有建立
~~~

这类错误会在离用户代码很远的调度阶段爆炸，定位非常困难。

## 4. GXFExecutor 为什么在用户 initialize() 后还会调用 initialize_base()

固定版本的 GXFExecutor 明确做了第二层保护：

~~~cpp
try {
  op->initialize();

  // Ensure framework-level initialization ran even if the operator's initialize()
  // override did not call Operator::initialize(). initialize_base() is idempotent.
  if (op->operator_type() != Operator::OperatorType::kVirtual) {
    op->initialize_base();
  }
} catch (const std::exception& e) {
  HOLOSCAN_LOG_ERROR(
      "Exception occurred during initialization of operator: '{}' - {}",
      op->name(), e.what());
  throw;
}
~~~

于是调用关系变成：

~~~text
GXFExecutor
   |
   +--> op->initialize()          用户可扩展
   |
   +--> op->initialize_base()     框架强制兜底
              |
              +--> framework_initialized_ 幂等门
~~~

这是一种很值得借鉴的框架设计：

> 用户 hook 可以扩展生命周期，但 runtime 自己必须守住不变量。

类似原则在插件系统、驱动框架和控制器生命周期里都很常见。不要把“框架能否正常构造”交给业务代码自觉。

## 5. initialize_base() 真正把 Operator 交给 Executor

`initialize_base()` 最关键的一句是：

~~~cpp
auto& executor = fragment_ptr->executor();
if (executor.initialize_operator(this)) {
  this->set_op_backend();
}
~~~

这一步是声明层和 backend runtime 的边界。

对 GXF backend 来说，`initialize_operator()` 不只是给对象打一个 initialized 标记，而是真正开始创建底层执行结构。固定源码中的主干可以压缩为：

~~~cpp
bool need_to_create_graph_entity = (op_eid_ == 0);

// Create the GraphEntity for the operator
op->initialize_graph_entity(context_, entity_prefix_);

// Create Codelet component
gxf_uid_t codelet_cid =
    (op_cid_ == 0) ? op->add_codelet_to_graph_entity() : op_cid_;

// Set GXF Codelet ID as the ID of the operator
op->id(codelet_cid);

if (need_to_create_graph_entity) {
  op->initialize_async_condition();
}

op->find_ports_used_by_condition_args();

for (const auto& [name, io_spec] : spec.inputs()) {
  gxf::GXFExecutor::create_input_port(fragment(), io_spec.get(), op);
}

for (const auto& [name, io_spec] : spec.outputs()) {
  gxf::GXFExecutor::create_output_port(fragment(), io_spec.get(), op);
}
~~~

于是上层的几个概念开始获得真正的 runtime 对应物：

~~~text
Operator       -> GraphEntity + GXFWrapper Codelet
Input IOSpec   -> Receiver component
Output IOSpec  -> Transmitter component
Condition      -> SchedulingTerm
Resource       -> GXF resource component
~~~

“物化”不是把一个类转换成另一个类，而是把一份图声明展开为一组相互关联的 runtime component。

## 6. GraphEntity 的 eid 与 Codelet 的 cid 为什么不能混为一谈

`initialize_graph_entity()` 先创建一个 `GraphEntity`：

~~~cpp
gxf_uid_t Operator::initialize_graph_entity(
    void* context, const std::string& entity_prefix) {
  const std::string op_entity_name =
      fmt::format("{}{}", entity_prefix, name_);

  graph_entity_ = std::make_shared<nvidia::gxf::GraphEntity>();

  auto maybe = graph_entity_->setup(
      context, op_entity_name.c_str());

  if (!maybe) {
    throw std::runtime_error(
        fmt::format("Failed to create operator entity: '{}'",
                    op_entity_name));
  }
  return graph_entity_->eid();
}
~~~

这里得到的是 **eid（entity id）**。

随后才在这个 Entity 内加入 Codelet，并得到 **cid（component id）**：

~~~text
Entity eid = 100
|
+-- GXFWrapper Codelet cid = 101
+-- Receiver cid = 102
+-- Transmitter cid = 103
+-- SchedulingTerm cid = 104
+-- Resource cid = 105
~~~

所以两种 identity 的语义不同：

- eid 标识“这一组 runtime component 所属的执行实体”；
- cid 标识“Entity 内某一个具体 component”。

ThreadPool/EntityGroup、EntityExecutor 和 Router 更多围绕 eid 工作；调用某个 Codelet、Receiver 或 Resource 时则需要对应 cid/component handle。

如果把两者都理解成“一个对象 id”，后面看 GXF 代码会非常混乱。

## 7. add_codelet_to_graph_entity()：真正建立 Holoscan→GXF 的执行桥

固定源码没有把用户 `Operator` 本身直接交给 GXF，而是添加一个 `GXFWrapper` Codelet：

~~~cpp
gxf_uid_t Operator::add_codelet_to_graph_entity() {
  if (!graph_entity_) {
    throw std::runtime_error(
        fmt::format("graph entity is not initialized for operator '{}'",
                    name_));
  }

  auto codelet_handle =
      graph_entity_->addCodelet<holoscan::gxf::GXFWrapper>(
          name().c_str());

  if (!codelet_handle) {
    throw std::runtime_error(
        fmt::format(
            "Failed to create GXFWrapper codelet corresponding to operator '{}'",
            name_));
  }

  codelet_handle->set_operator(this);
  return codelet_handle->cid();
}
~~~

这一段非常关键。

GXF Scheduler 不需要认识任意用户派生的 C++ Operator 类型。它只需要认识一个稳定的 GXF Codelet：

~~~text
GXF runtime
    |
    v
GXFWrapper::tick()
    |
    v
Holoscan Operator::compute(...)
~~~

这就是 Adapter 模式在 runtime 边界上的真实用途。

如果没有这层 wrapper，GXF 就必须直接依赖 Holoscan 上层对象模型；两层会被硬耦合。

## 8. GXFWrapper::tick() 如何真正调用用户 compute()

固定版本的 `GXFWrapper::tick()` 在调用业务代码前先清理上一轮状态：

~~~cpp
// clear any existing values from a previous compute call
op_->metadata()->clear();

// clear any received streams from previous compute call
exec_context_->clear_received_streams();

// reset acquisition timestamps for all ports
op_input_->reset_acquisition_timestamps();
~~~

随后才进入用户 Operator：

~~~cpp
try {
  if (op_->fragment()->data_flow_tracker() &&
      holoscan::profiler::trace_enabled()) {
    {
      holoscan::profiler::scoped_range p{...};

      op_->compute(
          *op_input_,
          *op_output_,
          *exec_context_);

      create_post_compute_nvtx_range();
    }
  } else {
    op_->compute(
        *op_input_,
        *op_output_,
        *exec_context_);
  }
} catch (const std::exception& e) {
  store_exception();
  return GXF_FAILURE;
}
~~~

所以从 Scheduler 到你写的业务函数，中间真实链路是：

~~~text
Scheduler worker
   |
EntityExecutor::executeEntity(eid)
   |
EntityItem::tick(...)
   |
tickCodelet(GXFWrapper)
   |
GXFWrapper::tick()
   |
Operator::compute(InputContext, OutputContext, ExecutionContext)
~~~

到这里，“Holoscan Operator 最终怎么被执行”就不再是抽象描述，而是一条可以在源码里逐层闭合的调用链。

## 9. IOSpec 为什么必须继续物化成 Receiver/Transmitter

构图时：

~~~cpp
spec.input<Tensor>("in");
spec.output<Tensor>("out");
~~~

这些只是端口描述。

真正执行时，`InputContext::receive()` 必须从某个 runtime queue 取数据，`OutputContext::emit()` 也必须向某个 transmitter 发布 Entity。

因此 `initialize_operator()` 会遍历 inputs/outputs 并创建真实 connector：

~~~text
IOSpec("in")
   |
   v
DoubleBufferReceiver / UCX Receiver / PubSub Receiver
   |
   v
GXF component cid

IOSpec("out")
   |
   v
DoubleBufferTransmitter / UCX Transmitter / PubSub Transmitter
   |
   v
GXF component cid
~~~

这也解释了为什么“图边”最终不能停留在 `add_flow(A, B)`：

~~~text
A.out 的 transmitter
       |
Connection / MessageRouter
       |
B.in 的 receiver
~~~

运行时需要的是 component-to-component 数据路径。

## 10. 为什么环图会在物化阶段反过来修改 queue 和 condition

普通 DAG 可以依赖“上游产生 → 下游消费”的单向推进。

环图却可能出现：

~~~text
A 等 B 腾出输出空间
^               |
|               v
+------ B 等 A 的输入
~~~

于是构图阶段发现 cycle 后，GXFExecutor 会修改执行端口配置。固定源码中：

~~~cpp
if (cycle_detected) {
  input_exec_spec->queue_size(IOSpec::kSizeOne);
  op->metadata_policy(MetadataPolicy::kUpdate);

  if (self_cycle) {
    auto& output_exec_spec = op->output_exec_spec();
    output_exec_spec->condition(ConditionType::kNone);
  }
}
~~~

这说明拓扑并不只是“连线信息”。

它会影响：

- queue capacity；
- metadata 合并策略；
- scheduling condition；
- 是否可能形成死锁。

因此 runtime materialization 其实也是一次 **graph lowering**：上层图结构被翻译成底层可执行约束。

## 11. Scheduler 看到 READY，不代表 Codelet 可以无条件立刻执行

GXF 的 `EntityExecutor::EntityItem::execute()` 首先检查生命周期：

~~~cpp
if (status_ == GXF_ENTITY_STATUS_START_PENDING) {
  return Unexpected{GXF_INVALID_EXECUTION_SEQUENCE};
}

if (status_ == GXF_ENTITY_STATUS_TICK_PENDING ||
    status_ == GXF_ENTITY_STATUS_TICKING) {
  return Unexpected{GXF_INVALID_EXECUTION_SEQUENCE};
}

if (status_ == GXF_ENTITY_STATUS_STOP_PENDING) {
  return Unexpected{GXF_INVALID_EXECUTION_SEQUENCE};
}

std::unique_lock<std::mutex> lock(execution_mutex_);
~~~

这里有两个层次。

Scheduler 决定某个 Entity 值得被 worker 尝试；EntityExecutor 则维护 Entity 自己的生命周期与串行执行边界。

如果没有这层保护，很容易出现：

~~~text
worker 0: Entity E -> compute()
worker 1: Entity E -> compute()   // 上一次还没结束
~~~

此时哪怕输入队列本身线程安全，业务 Operator 的成员变量也未必线程安全。

所以：

> “调度并发”不等于“同一个执行对象允许重入”。

## 12. 一次 tick 的数据面不是“调用 compute() 然后结束”

真正的 `EntityItem::tick()` 顺序非常清楚：

~~~cpp
code = router->syncInbox(entity);

for (size_t i = 0; i < codelets.size(); i++) {
  codelets.at(i).value()->beforeTick(timestamp);
}

setEntityStatus(GXF_ENTITY_STATUS_TICKING);

for (size_t i = 0; i < codelets.size(); i++) {
  code = tickCodelet(codelets.at(i).value());
}

for (size_t i = 0; i < terms.size(); i++) {
  code = terms.at(i).value()->onExecute(timestamp);
}

code = router->syncOutbox(entity);

setEntityStatus(GXF_ENTITY_STATUS_IDLE);
last_execution_timestamp_ = timestamp;
~~~

把它翻译成运行时语义：

~~~text
1. syncInbox
   把本轮应消费的输入推进到可见 stage

2. beforeTick
   更新 Codelet tick 前状态

3. TICKING
   生命周期正式进入业务执行

4. GXFWrapper::tick
   调用户 compute()

5. SchedulingTerm::onExecute
   更新周期/计数等调度状态

6. syncOutbox
   把本轮输出推进并分发到下游

7. IDLE
   本轮结束
~~~

因此一次 Holoscan `compute()` 不是孤立函数调用，而是被包在一个明确的消息快照与生命周期协议中。

## 13. syncInbox / syncOutbox 为什么必须放在 Codelet 两侧

MessageRouter 的 `syncInbox()` 会逐个同步当前 Entity 的 Receiver：

~~~cpp
Expected<void> MessageRouter::syncInbox(const Entity& entity) {
  if (receivers_.find(entity.eid()) != receivers_.end()) {
    const auto& cached_receivers = receivers_[entity.eid()];

    for (auto& rx : cached_receivers) {
      const auto result = rx->sync();
      if (!result) {
        return ForwardError(result);
      }
    }
  }
  return Success;
}
~~~

而 `syncOutbox()` 先同步 Transmitter，再把新消息分发给连接的 Receiver：

~~~cpp
Expected<void> MessageRouter::syncOutbox(const Entity& entity) {
  if (transmitters_.find(entity.eid()) != transmitters_.end()) {
    const auto& cached_transmitters = transmitters_[entity.eid()];

    for (auto& tx : cached_transmitters) {
      RETURN_IF_ERROR(tx->sync());

      const bool has_new_message = tx->size() > 0;
      if (has_new_message) {
        const auto receivers =
            UNWRAP_OR_RETURN(getConnectedReceivers(tx));

        while (tx->size() > 0) {
          Entity message = UNWRAP_OR_RETURN(tx->pop());
          // 后续分发到 connected receivers
        }
      }
    }
  }
  return Success;
}
~~~

它们的意义不是多做一次 queue copy，而是建立 tick 边界：

~~~text
上一阶段到达的数据
      |
      v
syncInbox
      |
   当前 tick
      |
      v
syncOutbox
      |
      v
下游下一阶段可见
~~~

这让 staging queue、调度条件与一次 tick 的输入快照能够配合工作。

## 14. ThreadPool 为什么最终绑定的是 eid，而不是 Operator*

ThreadPool 的约束最终要进入 GXF runtime，所以它不能只保存一个上层 C++ 指针列表。

固定源码在配置 thread pool 时会把 Operator 加入对应 EntityGroup：

~~~cpp
for (const auto& pool : fragment_->thread_pools_) {
  auto pool_entity_group = pool->entity_group();

  for (auto& op : pool->operators()) {
    pool_entity_group->add(op, entity_prefix_);

    auto current_dev_id =
        holoscan::gxf::gxf_device_id(
            context_,
            op->graph_entity()->eid());
    // ...
  }
}
~~~

真正更新 GXF group 时使用的也是 eid：

~~~cpp
gxf_uid_t op_eid = graph_entity->eid();

HOLOSCAN_GXF_CALL_FATAL(
    GxfUpdateEntityGroup(
        context,
        entity_group_gid,
        op_eid));
~~~

因此逻辑关系是：

~~~text
C++ Operator
    |
materialize
    v
GraphEntity eid
    |
EntityGroup
    |
ThreadPool / scheduler resource
~~~

这也是为什么 eid 是 runtime scheduling ownership 的锚点。

## 15. shutdown 为什么必须按反方向拆掉 materialized 对象

创建时的依赖大致是：

~~~text
GXF context
  -> GraphEntity
      -> Codelet / Receiver / Resource
          -> async execution
~~~

销毁时就不能先把最底层 context 抽掉。

固定版本的 Executor 析构会先释放自己持有的 entity resource：

~~~cpp
GXFExecutor::~GXFExecutor() {
  implicit_broadcast_entities_.clear();
  util_entity_.reset();
  gpu_device_entity_.reset();
  scheduler_entity_.reset();
  network_context_entity_.reset();
  connections_entity_.reset();
  fragment_services_entity_.reset();

  destroy_context();
}
~~~

正常 graph 运行结束时也会先等待、关闭相关异步设施并 deactivate：

~~~cpp
auto wait_result = GxfGraphWait(context);

if (wait_result == GXF_SUCCESS) {
  fragment->shutdown_data_loggers();

  GxfGraphDeactivate(context);
}

fragment->reset_backend_objects();
~~~

这条原则可以迁移到几乎所有异步 runtime：

> 先停止执行和新工作进入，再释放执行对象，最后销毁它们依赖的全局 context。

如果顺序反过来，就会出现典型 use-after-context：worker、callback 或资源析构还在访问已经销毁的 backend。

## 16. 把一条 Operator 的完整生命重新跑一遍

现在可以把 materialization 与 execution 串起来：

~~~text
Application::compose()
    |
    | make_operator / add_flow
    v
Operator + IOSpec + graph edge
    |
    | GXFExecutor initialize graph
    v
op->initialize()
    |
    +--> initialize_base()
            |
            +--> executor.initialize_operator(op)
                    |
                    +--> GraphEntity::setup()       -> eid
                    +--> add GXFWrapper Codelet    -> cid
                    +--> create Receiver/Tx
                    +--> create Condition/Resource
    |
    | graph activation
    v
Scheduler sees Entity(eid)
    |
    | readiness satisfied
    v
EntityExecutor::executeEntity()
    |
    +--> lifecycle check
    +--> execution_mutex
    +--> syncInbox
    +--> GXFWrapper::tick()
            |
            +--> Operator::compute()
    +--> SchedulingTerm::onExecute
    +--> syncOutbox
    v
downstream Entity becomes eligible
~~~

到这里，上层 API 和底层 runtime 不再是两个割裂的世界。

## 17. 对机器人实时系统真正有用的结论

物化阶段看起来发生在“启动时”，但它决定了运行期很多关键性质。

如果 port 被物化成什么 queue、queue 有多深，会决定数据年龄和 backlog。

如果 cycle 处理不正确，系统可能不是“慢一点”，而是直接形成调度死锁。

如果 Entity 被分到错误 ThreadPool，感知、推理、控制之间可能产生 CPU contention 或 priority inversion。

如果同一 Entity 可以重入，算法内部状态和 buffer ownership 会失去基本前提。

因此从框架设计角度，Holoscan 的核心分层可以概括成：

~~~text
Operator
  让应用作者描述算法节点

GXFExecutor
  把声明 lower 成 runtime graph

GraphEntity
  提供稳定执行 identity 与 component 容器

GXFWrapper
  适配 GXF Codelet 与 Holoscan compute()

EntityExecutor
  维护一次 tick 的生命周期与串行语义

MessageRouter
  维护 tick 前后的消息 stage 与分发
~~~

理解这条链之后，再看 Scheduler、Condition、Allocator、CUDA stream 或 UCX，就会知道它们最终都挂在哪里：它们不是散落的“功能模块”，而是围绕 materialized Entity 共同构成一次可执行、可调度、可回收的 runtime transaction。
