# Cyber RT 全景：机器人系统中的组件运行时

本文以 Apollo Cyber RT 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 为准。涉及上游实现时直接展示固定版本源码；不以源码链接或仓库路径代替代码。

在进入 `DataDispatcher`、`CRoutine` 或共享内存之前，先回答一个更基础的问题：为什么 Apollo 需要 Cyber RT？如果不先理解它在整台机器人中的位置，后面看到的 Reader、Component 和 Scheduler 就只会变成一串没有因果关系的类名。

可以先想象一辆自动驾驶车辆上最普通的一条数据链：激光雷达驱动产生点云，定位模块给出车辆姿态，感知模块识别障碍物，预测和规划模块决定未来轨迹，控制模块把轨迹变成方向盘和制动指令。这些模块的周期、算力需求和故障边界都不相同，却必须持续交换数据。

```text
LiDAR driver ── point cloud ──> Perception
                                     |
Localization ─── vehicle pose ───────+
                                     v
                               Prediction
                                     |
                                     v
                                  Planning
                                     |
                                     v
                                   Control ──> actuator
```

如果每个模块都直接调用下一个模块，感知代码就要知道规划对象的地址和线程；一个慢函数会沿调用栈阻塞上游；模块拆到另一个进程或另一台机器时，所有接口都要重写。如果每个模块自己操作 socket，开发者又要重复解决序列化、发现、断线、线程、队列、生命周期和监控。

Cyber RT 位于算法组件与 Linux/网络之间。它把这些重复问题收拢为一套组件运行时：业务代码声明“我读取哪些 channel、写出哪些 channel”，运行时负责创建通信端点、缓存消息、唤醒任务、安排线程并管理组件的装载与关闭。

## Cyber RT 的四项核心职责

把 Cyber RT 只称为“消息中间件”会漏掉一半。它至少同时承担四种职责。

第一种是数据传输。Writer 把消息写到 channel，Reader 从 channel 接收；同进程可以传共享对象，同主机跨进程可以走共享内存，跨主机可以走 RTPS。业务组件不用因为部署位置变化而改写 `Proc()`。

第二种是执行调度。消息到达并不等于立刻在网络线程中运行算法。数据先进入有限缓存；通知路径记录“需要重新检查”的事件并提示 ProcessorContext。Processor 获得 CPU 后重新扫描任务，等待态的 CRoutine 才可能在状态更新后成为 READY，再由 Processor 恢复执行。

第三种是组件装配。一个 `.dag` 文件描述要加载哪些动态库、创建哪些 Component、每个 Component 订阅哪些 channel。`mainboard` 读取配置并通过 class loader 创建对象，所以同一份运行时可以装载不同的感知或规划图。

第四种是拓扑与生命周期。Reader/Writer 加入系统后要发布自己的角色信息，匹配端点要启用对应 transport。关闭时，固定提交的 ComponentBase::Shutdown() 先设置关闭标志、调用派生 `Clear()`、关闭 Reader，最后才移除并等待 Component task；ModuleController 随后销毁组件对象，再卸载动态库。这里有一个重要边界：关闭标志只挡住尚未进入 `Proc()` 的后续调用，task 的等待发生在派生 `Clear()` 之后；若 `Clear()` 释放在途 `Proc()` 正在访问的资源，还需要额外同步或重新设计关闭协议。详见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

这四件事形成一条因果链：DAG 先决定“有哪些对象”，拓扑决定“谁和谁通信”，transport 决定“字节怎样到达”，scheduler 决定“业务代码何时执行”；生命周期协议则保证这些关系能在失败和退出时安全拆除。后续章节就按这条顺序展开，而不是按源码目录逐个点名。

## 典型 Cyber RT 进程结构

Apollo 常用 `mainboard` 作为承载组件的进程。一个进程可以加载多个组件动态库，组件共享进程级的 Scheduler、DataDispatcher、DataNotifier 和 transport 基础设施。

```text
mainboard process
|
|-- ModuleController
|     |-- ClassLoaderManager
|     `-- Component instances
|
|-- Component: perception
|     |-- Node
|     |-- Readers: pointcloud, localization
|     `-- Writers: obstacles
|
|-- Component: planning
|     |-- Node
|     |-- Readers: obstacles, prediction
|     `-- Writers: trajectory
|
|-- process-wide Transport
|-- process-wide Scheduler
|     `-- Processor OS threads
`-- process-wide topology / discovery
```

“进程”和“组件”不能混为一谈。进程是操作系统隔离和资源分配单位，Component 是 Cyber 管理的业务单元。多个 Component 可以在同一进程中共享地址空间，也可以被放进不同 mainboard 进程，通过共享内存或网络通信。

这种部署弹性很适合机器人软件。开发阶段可以把模块拆开，便于观察和重启；追求低延迟时又可以把高频链路放到同一进程，减少序列化。代价是性能不再只由业务代码决定：同一进程中的组件会共享 worker、全局 registry 和内存带宽，错误的分组可能让互不相关的任务相互干扰。

## Apollo 中的 Planning 与 Control 组件

抽象名词最好尽早落到实物。固定提交中的 Planning 部署声明装载规划动态库，并为组件配置三路主要输入：

```protobuf
module_config {
  module_library: "modules/planning/planning_component/libplanning_component.so"
  components {
    class_name: "PlanningComponent"
    config {
      name: "planning"
      readers: [
        { channel: "/apollo/prediction" },
        { channel: "/apollo/canbus/chassis" },
        { channel: "/apollo/localization/pose" }
      ]
    }
  }
}
```

对应的 `PlanningComponent` 在 C++ 类型中再次表达这三路输入：

```cpp
class PlanningComponent final
    : public cyber::Component<prediction::PredictionObstacles,
                              canbus::Chassis,
                              localization::LocalizationEstimate> {
 public:
  bool Init() override;
  bool Proc(const std::shared_ptr<prediction::PredictionObstacles>&,
            const std::shared_ptr<canbus::Chassis>&,
            const std::shared_ptr<localization::LocalizationEstimate>&) override;
};
```

这段代码先给出一个可观察事实：Planning 不是随便订阅任意消息后执行同一个无类型 callback，框架在编译期就知道 `Proc()` 的参数类型与顺序。后面研究 `AllLatest` 时还会看到，三路参数并不表示严格时间同步；第一路是触发输入，其他路提供当时最新值。

`ControlComponent` 则展示另一种执行方式。它的部署声明使用 `timer_components`，周期为 10 ms：

```protobuf
timer_components {
  class_name: "ControlComponent"
  config {
    name: "control"
    interval: 10
  }
}
```

Control 不是“某条输入消息一到就调用 `Proc(msg)`”，而是每 10 ms 运行一次无参数 `Proc()`，从成员 Reader 中取得 localization、chassis、planning 等最新快照，再计算油门、制动与转向。

Planning 与 Control 的对照说明：Component 是由 Cyber 管理生命周期和执行入口的算法单元，不是 Reader callback 的另一个名字。消息触发与定时触发都能进入同一组件运行时，后续分析必须先确认当前组件属于哪一种。

## Message、Channel 与 Component

第一次写 Cyber 组件，最先接触的通常不是 transport 或 scheduler，而是 Message、Channel 和 Component。

Message 是传输的数据类型，常见实现是 protobuf 生成的 C++ 类。它描述“数据长什么样”，例如点云、定位结果或控制命令。

Channel 是有名字的数据流。它不是一只 C++ 队列对象，也不是固定的 socket；`/apollo/localization/pose` 这样的名字标识一类逻辑数据，实际 transport 会随 writer/reader 的部署关系变化。

Component 是由 Cyber 装载和调度的业务对象。组件实现 `Init()` 完成自身初始化，实现 `Proc(...)` 处理输入消息。先用最小形态表达这层接口：

下面是**教学最小例子，不是 Apollo 原始源码**。它只展示派生组件要提供的接口形状；真实组件的初始化、Reader 创建和调度任务注册由框架的 `Initialize()` 完成。

```cpp
class MyComponent : public Component<InputMessage> {
 public:
  bool Init() override;
  bool Proc(const std::shared_ptr<InputMessage>& msg) override;
};

CYBER_REGISTER_COMPONENT(MyComponent)
```

这几行已经包含四个需要理解的 C++ 机制。

`Component<InputMessage>` 是模板实例。模板参数把“这只组件接收什么消息”带进编译期，框架因而能生成类型正确的 Reader、DataVisitor 和 callback，不需要在每次消息到达时做运行时类型判断。

`override` 表示派生类正在覆盖基类的虚函数。框架持有的可以只是 `ComponentBase` 指针，但调用 `Init()` 或内部 `Process()` 时，C++ 虚函数分派仍会进入实际派生类。这样 class loader 不需要在编译时知道每一种业务组件。

`std::shared_ptr<InputMessage>` 是共享所有权句柄。多个 Reader 支路和缓存槽位可以引用同一个消息对象，最后一个引用消失时对象才析构。它避免进程内 fan-out 时复制整个 protobuf，但引用计数本身仍有原子操作成本。

注册宏把派生类和 `ComponentBase` 的工厂关系登记给 class loader。宏不是在“启动一个组件”，而是在动态库被加载时提供“怎样按类名创建对象”的信息；真正的实例仍由 `ModuleController` 按 DAG 创建。

到这里，业务接口已经清楚了，但仍有一个关键空白：系统怎样知道要创建 `MyComponent`，它的输入 channel 又从哪里来？这就是 DAG 存在的原因。

## DAG 的运行时装配语义

Cyber 的 DAG 配置把动态库、类名、组件名字和 Reader 配置连在一起。protobuf（Protocol Buffers，协议缓冲区）消息定义把这些字段变成有类型、可序列化的配置对象。下面把两个配置定义中的关键 message 摘录到一处；这是**固定提交源码节选**，省略 import、注释和无关字段，并非单一连续原文：

```protobuf
message ComponentInfo {
  optional string class_name = 1;
  optional ComponentConfig config = 2;
}

message ModuleConfig {
  optional string module_library = 1;
  repeated ComponentInfo components = 2;
  repeated TimerComponentInfo timer_components = 3;
}

message DagConfig {
  repeated ModuleConfig module_config = 1;
}

message ReaderOption {
  optional string channel = 1;
  optional QosProfile qos_profile = 2;
  optional uint32 pending_queue_size = 3 [default = 1];
}

message ComponentConfig {
  optional string name = 1;
  optional string config_file_path = 2;
  optional string flag_file_path = 3;
  repeated ReaderOption readers = 4;
}
```

`module_library` 告诉 class loader 去哪里找 `.so`，`class_name` 对应注册宏登记的派生类，`name` 成为组件及调度 task 的身份，`ReaderOption` 则决定输入 channel 和缓存深度。

这里体现了“配置描述、对象实现”分离。组件代码只表达怎样处理消息，部署者可以在不重新编译算法的情况下调整组件装箱方式、输入 channel 和 queue size。它让同一组件能进入不同车辆或仿真拓扑，也带来配置复杂度：类名、动态库、channel 和 scheduler group 中任何一处不一致，都可能让错误发生在运行时而非编译时。

这里的 “DAG” 不要误解为 mainboard 会读取显式边并对整张算法图做一次全局拓扑排序。配置里没有 `A -> B` 这样的依赖边；组件之间通过同名 channel 隐式连接，真正执行仍由消息到达或 Timer 触发。

`mainboard` 的启动链是：

```text
main(argc, argv)
  -> ModuleArgument::ParseArgument       解析 DAG 和进程组参数
  -> cyber::Init                         初始化全局运行时
  -> ModuleController::Init
  -> ModuleController::LoadAll
  -> ModuleController::LoadModule
       -> 读取 module_library
       -> ClassLoaderManager::LoadLibrary
       -> CreateClassObj<ComponentBase>(class_name)
       -> Component::Initialize(config)
       -> component_list_.emplace_back(instance)
```

一个进程的入口是 `main()`；配置装配由 `ModuleController::Init()`、`LoadAll()` 和 `LoadModule()` 接续完成。最关键的边界不是函数名，而是组件对象何时创建、何时被框架长期持有。

这里把决定对象寿命的关键几行直接贴出，避免只凭函数名想象“加载模块”发生了什么。以下是固定提交中 `ModuleController::LoadModule()` 的**源码摘录**，省略 timer component 的同构分支与日志：

```cpp
class_loader_manager_.LoadLibrary(load_path);

for (auto& component : module_config.components()) {
  const std::string& class_name = component.class_name();
  std::shared_ptr<ComponentBase> base =
      class_loader_manager_.CreateClassObj<ComponentBase>(class_name);
  if (base == nullptr || !base->Initialize(component.config())) {
    return false;
  }
  component_list_.emplace_back(std::move(base));
}
```

顺序有实际意义：先装入共享库，库里的工厂注册才可用；工厂按配置类名返回基类句柄，调用者因此不必在编译期知道派生组件类型；`Initialize()` 成功后才把 `shared_ptr` 移入 `component_list_`，由 ModuleController 持续拥有实例。初始化失败会直接返回，不会把这个实例放进正常组件列表。`std::move(base)` 移动的是共享指针句柄，不会复制整个组件对象。完整装载和失败路径见[从 DAG 到 Component 的对象生命周期](dag-to-component.md)。

这一章之后会单独追踪“DAG 到 Component”的对象创建链，因为它回答了三个后续问题：Node 是谁创建的，Reader 为什么在第一条消息前已经存在，关闭时又为什么必须先销毁组件再卸载动态库。

## Node、Reader 与 Writer 的协作关系

Component 初始化时会拥有一个 Node。Node 是创建通信实体的门面：它用名字建立拓扑身份，对外提供 `CreateReader<T>()`、`CreateWriter<T>()` 等类型安全 API，对内把具体工作交给 `NodeChannelImpl`。

```text
Component
  `-- Node
       |-- Reader<InputA>
       |-- Reader<InputB>
       |-- Writer<Output>
       `-- NodeChannelImpl
```

Reader 表示“这个进程中的某个消费者想读取某个 channel”。它不仅保存用户回调，还拥有观察缓存、DataVisitor task、transport receiver 引用和 topology 角色。Writer 则把类型化消息交给底层 transmitter，并向 topology 声明自己能在某个 channel 上发送。

Node 的存在是为了隔离变化。如果 Component 直接构造 SHM receiver 或 RTPS publisher，业务代码就会知道部署细节；如果它直接操作 Scheduler，通信 API 又会和执行策略耦合。Node 把业务看到的“创建一个 Reader”翻译成角色属性、channel id、QoS、receiver、DataVisitor 和 task。

第一次阅读时，可以把 Node 理解为工厂和命名边界，但不要把“Factory Pattern”当成解释终点。真正价值是 Component 不需要知道 Reader 背后最终使用 INTRA、SHM 还是 RTPS，也不需要知道 callback 会由哪只 Processor 执行。

把 `Reader` 的初始化代码贴出来后，这种分层就不只是几个名词。下面是固定提交中 `Reader<MessageT>::Init()` 的**源码摘录**，保留 task、receiver 和 topology 的建立顺序：

```cpp
template <typename MessageT>
bool Reader<MessageT>::Init() {
  if (init_.exchange(true)) {
    return true;
  }

  std::function<void(const std::shared_ptr<MessageT>&)> func;
  if (reader_func_ != nullptr) {
    func = [this](const std::shared_ptr<MessageT>& msg) {
      this->Enqueue(msg);
      this->reader_func_(msg);
      // 省略原函数的统计采样语句
    };
  } else {
    func = [this](const std::shared_ptr<MessageT>& msg) {
      this->Enqueue(msg);
    };
  }

  croutine_name_ = role_attr_.node_name() + "_" + role_attr_.channel_name();
  auto dv = std::make_shared<data::DataVisitor<MessageT>>(
      role_attr_.channel_id(), pending_queue_size_);
  croutine::RoutineFactory factory =
      croutine::CreateRoutineFactory<MessageT>(std::move(func), dv);
  if (!scheduler::Instance()->CreateTask(factory, croutine_name_)) {
    init_.store(false);
    return false;
  }

  receiver_ = ReceiverManager<MessageT>::Instance()->GetReceiver(role_attr_);
  role_attr_.set_id(receiver_->id().HashValue());
  channel_manager_ =
      service_discovery::TopologyManager::Instance()->channel_manager();
  JoinTheTopology();
  return true;
}
```

这段初始化分成两条相连但职责不同的线。前半段把用户 callback 包成 `func`，再交给 `RoutineFactory` 和 Scheduler；消息到达后它由 CRoutine 执行，不是直接在 transport listener 中运行。后半段才取得可复用的 Receiver，并把 Reader 注册到 channel topology。lambda 用 `[this]` 捕获 Reader 裸指针，意味着 task 不拥有 Reader；关闭 Reader 时必须先移除并等待 task，不能只销毁 `shared_ptr<Reader>`。更细的 callback 线程、Receiver 共享与关闭次序见[Node、Reader 与 Writer 的对象生命周期](node-reader-writer.md)。

## 数据面与执行面的分离

现在已经知道 Reader 从何而来，下一步才适合问消息怎样触发 `Proc()`。

Cyber 没有让 transport 接收线程直接调用业务函数。它把运行时分成数据面和执行面：

```text
数据面
Writer -> Transport -> Receiver -> DataDispatcher -> CacheBuffer

执行面
DataNotifier -> Scheduler -> Processor -> CRoutine -> Component::Proc
```

数据面的任务是让消息安全地到达一个有界位置。执行面的任务是决定哪个任务何时获得 CPU。`DataNotifier` 只传递“某个 channel 有更新”这一事件，不把 payload 再复制进 scheduler queue。

这种分层解决了最危险的耦合：如果 LiDAR 的 SHM 接收线程直接运行感知 `Proc()`，一次耗时推理就会阻止该线程继续接收其他共享内存消息；如果 RTPS listener 直接运行控制器，网络库的线程优先级和业务实时需求也会绑在一起。

分层并不等于免费。消息要经历缓存锁、通知扇出、worker 唤醒、run queue 扫描和协程恢复。Cyber 的选择不是“消除开销”，而是把不可控的业务执行从 transport 线程移走，再用统一 scheduler 管理它。

数据面与执行面的边界可以沿[完整消息链](message-to-proc.md)逐跳核对：从 Receiver 和 Dispatcher 写缓存开始，经 Notifier/Scheduler 传递事件，再由 Processor 恢复协程进入 `Component::Proc()`。这篇旧总览是专题入口的兼容页；新读者应按[架构地图](architecture-map.md)进入课程，再根据问题跳到这条消息链，而不必依赖本页之后的隐含章节顺序。

## INTRA、SHM 与 RTPS 的部署边界

默认 HybridReceiver 会依据 writer 与 reader 的位置选择通信方式：

```text
同一进程       -> INTRA -> 共享同一 Message 对象
同一主机跨进程 -> SHM   -> 共享内存 block / 本地反序列化
不同主机       -> RTPS  -> 网络字节流与反序列化
```

INTRA 的优势是正常同类型路径可以直接传 `shared_ptr<MessageT>`，避免 payload 序列化。它仍会执行 Dispatcher 扇出、缓存加锁和 scheduler 通知，所以“零 payload copy”不能理解为“零延迟”。

普通 SHM 路径把序列化字节放进共享内存，接收进程仍需构造本地 protobuf 对象并解析。它减少内核网络栈的数据搬运，不自动等于端到端零拷贝。Arena 路径尝试让对象直接位于预留内存区中，但随之而来的是 block 复用、读锁和对象生命周期问题。

RTPS 允许跨主机通信，也带来字符串复制、序列化、网络拥塞和 listener 线程调度。提高 Cyber Component task 的优先级不会自动提高 RTPS 接收线程，也不会消除网络抖动。

从架构角度看，Hybrid transport 隔离的是“部署距离变化”。同一个 Component 可以从同进程挪到另一进程甚至另一主机，而业务接口不变；但每种路径的复制点和线程完全不同，性能分析不能只看统一 API。

## CRoutine 与 Scheduler 的执行模型

如果每个 Reader 都创建一只永久 OS 线程，大量组件会带来大量线程、栈空间和上下文切换；线程之间的优先级、CPU affinity 和共享资源竞争也难以统一配置。

Cyber 把消息处理任务包装为 CRoutine。CRoutine 拥有可保存和恢复的执行上下文，却由少量 Processor OS 线程承载。Routine 没有数据时处于 `DATA_WAIT`；消息通知先留下更新标记并通知 context，Processor 返回调度循环后调用 `NextRoutine()`，由 `UpdateState()` 检查标记并将合适的等待态改为 `READY`，随后 `Resume()` 才恢复协程。消息通知、状态转变、OS 线程可运行和协程继续执行不是同一事件。

```text
many CRoutines
  |  perception callback
  |  planning callback
  |  reader enqueue task
  `  ...
        |
        v
ProcessorContext / scheduling policy
        |
        v
small set of Processor OS threads
```

协程与线程的区别在这里很重要。OS 线程由内核抢占调度；Cyber routine 在一次 `Proc()` 内是合作式的，只有 callback 返回或代码显式 yield 后才让出当前 Processor。高优先级 routine 已经 READY，也不能打断同一 Processor 上正在执行的低优先级 `Proc()`。

因此 Cyber 的 priority 主要影响“下一次选择谁”，不是硬实时抢占保证。要让 1 kHz 控制任务更可预测，还需要隔离 Processor、固定 CPU、设置并验证 OS policy、限制 callback 最坏执行时间、避免动态分配与阻塞锁。Cyber 提供配置这些边界的能力，但不替应用完成最坏时延证明。

## Cyber RT 的工程优势

它最明显的优势是为 Apollo 的组件图提供了完整而统一的运行时。通信、调度、组件装载、配置、拓扑和监控使用相同的角色与命名体系，算法模块不必各自拼装基础设施。

同一 API 下的多 transport 让部署能在同进程低复制、同主机共享内存和跨主机网络之间移动。对大型机器人，这比把每条连接写死为 socket 或直接函数调用更容易重组。

有界缓存和 overwrite-oldest 行为适合许多状态型传感器流。消费者落后时内存不会无限增长，并可跳过已经失去控制价值的旧数据。Scheduler 的 group、priority、CPU affinity 和 Choreography 绑定又提供了比“每个 callback 随便起线程”更可控的执行拓扑。

动态库加 DAG 的组合也提升了部署复用：组件实现与装箱方式分开，mainboard 可以按配置创建对象并统一关闭。这对于包含大量感知、定位和规划插件的系统很实用。

## 复杂度代价与能力边界

统一运行时也意味着学习曲线陡峭。一个看似简单的 Reader callback 背后可能存在 transport receiver、Dispatcher、两只 DataVisitor、两条 CRoutine 和共享 Scheduler；如果只读 API，很容易误判线程、队列和所有权。

进程级 singleton 与 registry 简化了共享设施，却使动态装载边界复杂。过期 weak buffer 和 notifier 项如果没有显式注销，会增加长期热路径扫描。启动阶段稳定、运行阶段少变更的车辆软件更容易满足这种假设；高频创建销毁端点的通用动态系统需要额外谨慎。

合作式协程降低了线程数量，却把公平性和 callback WCET 变成应用责任。Classic 策略扫描持久 routine vector，同优先级公平性并非天然 round-robin；一个长 `Proc()` 也会占住 Processor。

多输入 Component 的默认语义是主输入触发、其他输入取最新值，而不是按时间戳同步。对于传感器融合，应用必须自行检查数据年龄和跨输入时间差。

最重要的边界是：低延迟不等于确定性，支持 priority 不等于端到端实时优先级，使用共享内存也不等于整个链路零拷贝。Cyber 很适合构建高吞吐、软实时、可配置的车载数据流，但不能代替硬实时控制器、WCET 分析或安全认证运行时。

## 适用场景与不适用场景

Cyber RT 很适合一台高算力机器人或车辆上的大型感知—规划图：消息类型明确，模块数量多，既有同进程高频链路，也有跨进程隔离需求；系统希望用配置组织组件，并统一管理 CPU 与生命周期。

它也适合需要把算法插件装进公共宿主进程的场景。Component 模板和 class loader 提供固定扩展边界，DAG 决定实际组合，运行时能把 Reader/Writer、调度 task 和拓扑角色一起建立起来。

对于资源很小的 MCU、只有几条静态链路的设备，整套组件运行时可能过重。对于必须证明微秒级最坏时延的电机电流环，Linux、动态分配、共享 registry 和合作式 callback 也通常不是合适的最后执行层。对于端点频繁变化、需要强背压或逐消息可靠交付的通用分布式系统，还要仔细核对 Cyber 的 queue 与 transport 语义是否匹配。

## 架构骨架：每个运行时模块在整条因果链中的位置

把关注点从文件位置转回模块职责，真实运行时由下面这些对象组成：

:::{mermaid}
flowchart TB
  MB[mainboard / ModuleController] --> CL[ClassLoader / Component factory]
  MB --> COMP[Component 生命周期]
  COMP --> NODE[Node / Reader / Writer]
  NODE --> DISC[service discovery]
  NODE --> TRANS[INTRA / SHM / RTPS]
  TRANS --> DATA[DataDispatcher / CacheBuffer]
  DATA --> VIS[DataVisitor / DataNotifier]
  VIS --> SCHED[Scheduler / ClassicContext]
  SCHED --> CR[CRoutine / RoutineContext]
  SCHED --> PROC[Processor OS threads]
  NODE --> BLOCK[Blocker 观察缓存]
  CONFIG[DAG / Component / Scheduler 配置] --> MB
  CONFIG --> COMP
  CONFIG --> SCHED
:::

这不是阅读顺序。阅读顺序应当由问题决定：创建问题从 `mainboard` 进入，消息问题从 Reader/Receiver 进入，调度问题从 Notifier 进入，关闭问题再沿对象所有权反向返回。

## 从宏观到源码的阅读顺序

第一步是“从 DAG 到 Component”。此时关注第一条消息到达之前发生的事情：mainboard 怎样解析配置，class loader 怎样按字符串创建 C++ 派生对象，Component 怎样创建 Node 和 Reader，对象由谁持有。没有这一步，后面所有 runtime 对象都会像凭空出现。

第二步是“一条消息如何到达 `Component::Proc()`”。它沿 Receiver、Dispatcher、CacheBuffer、Notifier、CRoutine 和 Processor 走完整链，并把线程、锁、复制、唤醒和过载连起来。读完应能解释为什么接收线程不直接调用算法。

第三步单独研究 `pending_queue_size`。用具体槽位追踪五条消息和一个慢消费者，才能真正理解 `size + 1` ring、visitor 私有游标、overwrite-oldest 和“落后后跳最新”的控制含义。

第四步研究多输入 `AllLatest`。它回答 `Component<M0, M1>` 究竟在做同步、配对还是 sample-and-hold，以及辅助输入断流时为什么可能一直复用旧值。

第五步把 INTRA、SHM 和 RTPS 分开。每篇只追一种 transport 的发送、接收、内存所有权和错误路径，不让“共享内存”三个字掩盖反序列化或 block 生命周期。

第六步深入 Scheduler。Classic 的持久 vector、Choreography 的 processor 绑定、CRoutine 防丢唤醒和 shutdown 等待要分别建立状态机，最后再讨论 100 Hz/1 kHz 控制链怎样配置。

最后沿 `Shutdown()` 反向拆解整个对象图。只有能说清在途 callback、task、Reader、receiver、Component 和动态库按什么顺序退出，才算真正理解这套运行时，而不只是会调用 API。

## 最小 Cyber-like 运行时的实现起点

最小实现不需要一开始复制整个 Cyber。先做一个有名字的 channel registry、一只固定容量 ring、一个拥有私有游标的 visitor，再做“写入数据后只发事件”的 notifier。此时可以在单线程中验证消息与过载语义。

然后加入一只 worker 线程和 ready task 列表，把 callback 从 producer 调用栈移走。再加入可暂停 routine 或状态机任务，验证“通知可以合并，但非空 buffer 最终必被重新检查”。

数据面和执行面稳定后，再接进程内传递、共享内存和网络 adapter。最后加入 DAG、工厂与动态库，让配置决定对象组合。这个顺序保持了同一个设计原则：先明确数据结构不变量，再增加并发；先闭合生命周期，再增加部署弹性。

这份导读的目的不是把 Cyber RT 一次讲完，而是给后面的每个源码细节确定位置。Reader 不再是孤立 API，DataVisitor 不再是突然出现的模板，CRoutine 也不再只是“更轻的线程”。它们分别位于装配、数据和执行三条链上，共同把一张机器人算法图变成可以运行和关闭的程序。
