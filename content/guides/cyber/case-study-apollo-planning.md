# 项目案例：Apollo Planning 如何把 Cyber RT 变成规划流水线

Apollo Planning 不是为了展示 Cyber API 而写的样例，而是 Cyber RT 的生产级使用者。它把 Prediction（障碍物运动预测）、Chassis（车速、挡位等底盘状态）、Localization（车辆位姿估计）等高频输入组织为一次规划触发，把 Routing（路线/导航命令）、Traffic Light（信号灯检测）、Pad（驾驶员交互）和 Story（场景语义）等异步输入维护为辅助状态，最后发布 ADCTrajectory（发送给 Control 的规划轨迹消息）。

从驾驶任务看，Planning 回答的是一个不断重复的问题：车辆已经知道自己在哪里、周围有哪些交通参与者、底盘当前是什么状态，那么未来几秒应该沿着怎样的轨迹运动。它不直接转动方向盘，也不负责识别障碍物；它把上游感知和定位结果整理成带时间、速度、加速度与路径点的轨迹，再交给 Control 执行。

先把算法侧几个名字放回普通工程语言里：`LocalView` 是 Planning 为单个周期收集的输入视图；`Frame` 是算法在该周期内组织车辆状态、障碍物和候选路径等数据的工作对象；`Scenario` 表示当前驾驶情境，`Stage` 是情境中的推进阶段，`Task` 是阶段内按序执行的一步算法。它们是 Planning 层对象，不是 Cyber 的线程或通信对象。把输入送进或送出组件的 `Reader`/`Writer` 是 Cyber 通信对象：前者订阅 channel，后者向 channel 发布消息。

一次最小规划周期可以先记成下面这条主线：

```text
Prediction 新数据到达
  -> Cyber 收集 Chassis 与 Localization 的最新匹配输入
  -> PlanningComponent 冻结本周期辅助状态
  -> Planning 算法检查输入、建立 Frame、运行 Scenario/Task
  -> 生成 ADCTrajectory
  -> Writer 发布给 Control 和监控模块
```

这条链把部署文件、组件运行时和 Planning 算法放在同一条数据路径上。这里 DAG 是 Apollo 用于描述一组运行组件及配置的部署文件；名称来自有向无环图（Directed Acyclic Graph），但不能仅凭扩展名就假定其中显式表达了所有运行时数据依赖。`LocalView` 则是 Planning 算法读取本周期输入的视图。这样模板参数、互斥锁和规划对象都有明确位置，而不是一组互不相干的 Cyber RT 名词。

项目入口固定到 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。本文直接展示 Planning 部署声明、组件模板、输入处理和输出发布的真实代码摘录；配置字段、C++ 类型与调用链都按这一版本核验。

## 功能需求与通信形状

规划模块需要同时满足：

- 主输入到达后尽快产生新轨迹；
- 低频辅助输入可在两次规划间更新；
- 多输入来自不同频率，不能要求严格同时到达；
- 规划算法与 Cyber 生命周期、Reader/Writer 创建分离；
- 可由 DAG/Launch 装载、用 Record 回放复现。

因此它没有把所有 channel 都放进一个任意多输入 callback，而是区分触发输入和状态型 Reader。这一功能划分先于代码结构。

这里的“触发输入”会推动一次新的 `Proc()` 执行；“状态输入”只是更新组件保存的最新值，等待下一次规划使用。例如新的 Prediction 通常意味着环境已经变化，应尽快重新规划；Routing 则可能几十秒都不变，没有必要每次收到同一路线时单独运行完整规划。

## 版本稳定结构与可变消息集合

Apollo master 上的辅助输入会随规划架构演进。当前 `PlanningComponent::Init()` 可见 `PlanningCommand`、控制交互消息以及导航模式下的 `MapMsg` 等 Reader；较早文档会列出 RoutingResponse、TrafficLightDetection、PadMessage 和 Stories。阅读时应把“辅助状态通过显式 Reader 进入组件”视为稳定模式，把具体 topic 清单视为版本事实。

这也是源码分析必须固定 commit 的原因。DAG、配置 proto 和 C++ 代码若来自不同版本，Reader 名称、配置字段和算法入口可能无法互相对应。

## 组件对象图

`Component` 是被 mainboard 装载的业务组件；`DAG` 描述组件库、类名、实例和输入配置；`CRoutine` 是 Cyber 调度器管理的协作式任务：它在承载它的操作系统 worker 线程中主动让出执行权，之后由调度器重新选择并恢复；它不是操作系统线程本身。对象图中的这些名字分别对应业务对象、部署配置和执行体。

```text
PlanningComponent : Component<PredictionObstacles, Chassis, LocalizationEstimate>
  ├─ Proc(prediction, chassis, localization)   主触发路径
  ├─ readers created in Init()
  │    ├─ RoutingResponse
  │    ├─ TrafficLightDetection
  │    ├─ PadMessage
  │    └─ Stories / MapMsg ...
  ├─ planning algorithm object
  └─ Writer<ADCTrajectory>
```

模板参数把三种主输入带入 DataVisitor/Component 调度链；普通 Reader callback 更新共享的辅助状态。这里体现了 Cyber 的两种使用方式：模板输入负责调度边界，显式 Reader 负责异步状态摄取。

## 从 DAG 到一次规划

```text
planning.launch
  -> planning.dag
  -> mainboard loads PlanningComponent library
  -> Component::Initialize creates Node and Readers
  -> main input updates DataVisitor
  -> Scheduler wakes CRoutine
  -> PlanningComponent::Proc(...)
  -> algorithm RunOnce / Plan
  -> planning_writer_->Write(adc_trajectory)
```

官方文档明确指出最终轨迹通过 `planning_writer_->Write(adc_trajectory_pb)` 送回 Cyber。这个 Writer 不在每次 Proc 中创建，而是组件初始化时建立长期实体。

## 部署文件、组件类和配置必须形成一个闭环

Apollo 的运行实体不是单个 `.cc` 文件，而是一组必须版本一致的产物：

```text
launch
  -> 选择 process/mainboard 与 dag
dag
  -> shared library + class name + component config
class registration
  -> 字符串类名映射到 PlanningComponent factory
component config proto
  -> topic、算法配置、运行模式
C++ Init
  -> 读取 config，创建 Reader/Writer/algorithm
```

任意一层错位都会产生不同故障：库路径错导致无法装载；注册类名错导致 factory 查找失败；DAG Reader 与模板签名/配置不一致会让输入永不触发；topic 配置错会让组件存在但数据链为空；算法配置版本错则可能在 Init 或首次 Proc 才失败。

部署审查应把以下字段放在同一张表中，而不是逐文件人工猜测：

| 契约 | 定义位置 | 运行时消费者 |
|---|---|---|
| component library | DAG | ClassLoader |
| class name | DAG + registration macro | ClassFactory |
| instance name | DAG | topology/scheduler/monitor |
| trigger channels | Component 模板/DAG reader config | DataVisitor |
| auxiliary channels | Planning config | `Init()` 中 CreateReader |
| output channel | Planning config | `CreateWriter` |
| algorithm mode/config | flags + proto | `PlanningBase::Init` |

这张表也是发布包升级检查单。只替换 `.so` 而沿用旧 DAG/proto，可能仍能装载，却在输入数量、topic 或配置字段上产生隐蔽不兼容。

## `Init()`：从配置到运行时对象

下面是根据固定提交压缩出的**源码结构摘录**。它保留对象关系和关键调用，但省略了其他 Reader、错误处理与监控代码，不能当作可直接替换原文件的完整实现：

```cpp
bool PlanningComponent::Init() {
  injector_ = std::make_shared<DependencyInjector>();

  if (FLAGS_use_navigation_mode) {
    planning_base_ = std::make_unique<NaviPlanning>(injector_);
  } else {
    planning_base_ = std::make_unique<OnLanePlanning>(injector_);
  }

  ACHECK(ComponentBase::GetProtoConfig(&config_));
  planning_base_->Init(config_);

  planning_command_reader_ = node_->CreateReader<PlanningCommand>(
      config_.topic_config().planning_command_topic(),
      [this](const std::shared_ptr<PlanningCommand>& message) {
        std::lock_guard<std::mutex> lock(mutex_);
        planning_command_.CopyFrom(*message);
      });

  planning_writer_ = node_->CreateWriter<ADCTrajectory>(
      config_.topic_config().planning_trajectory_topic());
  return true;
}
```

`DependencyInjector` 是共享依赖容器，向规划算法提供 vehicle state、planning context、history 等服务。`unique_ptr<PlanningBase>` 表示 PlanningComponent 独占具体规划模式；`shared_ptr<DependencyInjector>` 则允许多个内部对象共享依赖而不复制状态。

`ACHECK` 表达“缺少组件配置时无法继续启动”的不可恢复条件。Reader callback 复制 protobuf 到组件成员，并用同一互斥量保护。复制增加成本，却把消息寿命从 callback 的 `shared_ptr` 中解耦，方便组件建立稳定快照。

lambda 捕获 `[this]` 要求 Reader 在 PlanningComponent 析构前停止回调。若框架关闭顺序反过来，mutex 和成员都会成为悬空访问；因此 Reader 实体寿命必须被组件/Node 管理并在组件析构前撤销。

逐句理解这段 C++：

1. `std::make_shared<DependencyInjector>()` 在堆上创建对象，并返回共享所有权指针；最后一个 `shared_ptr` 离开作用域时对象才销毁；
2. `std::make_unique<OnLanePlanning>` 表示具体 planner 只有组件拥有，不能被意外复制给另一个 owner；
3. `CreateReader<PlanningCommand>` 的尖括号是模板实参，编译器由此生成只接收 `PlanningCommand` 的类型化 Reader；
4. lambda 参数使用 `const std::shared_ptr<PlanningCommand>&`，表示 callback 借用框架传来的智能指针、不重置这个句柄；但 `PlanningCommand` 不是 `const`，类型本身仍允许修改 `*message`。这段 callback 只是调用 `CopyFrom()` 读取字段；若要编译期禁止修改，需要 `shared_ptr<const PlanningCommand>`，固定接口并未这样声明；
5. `[this]` 让 lambda 能访问 `mutex_` 和 `planning_command_`，但不会延长组件寿命，所以关闭顺序仍由框架保证；
6. `std::lock_guard<std::mutex>` 在构造时加锁、离开花括号时自动解锁，即使 `CopyFrom` 抛出异常也不会忘记释放锁。

`shared_ptr` 不等于“所有对象都应该共享”。Injector 会被多个规划内部对象共同引用，适合共享所有权；具体 `PlanningBase` 只有一个明确 owner，使用 `unique_ptr` 更能表达设计意图。智能指针首先是所有权说明，其次才是自动释放工具。

### Init 是资源事务而不是字段赋值集合

实际初始化至少包含依赖容器、具体 PlanningBase、配置、多个 Reader、Writer 和算法内部资源。更稳妥的自研组件使用局部候选对象，全部成功后再提交到成员：

```cpp
bool PlanningComponent::Init() {
  auto injector = std::make_shared<DependencyInjector>();
  auto planner = MakePlanner(injector, FLAGS_use_navigation_mode);
  PlanningConfig config;
  if (!ComponentBase::GetProtoConfig(&config)) return false;
  if (!planner->Init(config)) return false;

  auto command_reader = node_->CreateReader<PlanningCommand>(
      config.topic_config().planning_command_topic(),
      [this](const auto& msg) { OnPlanningCommand(msg); });
  auto trajectory_writer = node_->CreateWriter<ADCTrajectory>(
      config.topic_config().planning_trajectory_topic());
  if (!command_reader || !trajectory_writer) return false;

  injector_ = std::move(injector);
  planning_base_ = std::move(planner);
  planning_command_reader_ = std::move(command_reader);
  planning_writer_ = std::move(trajectory_writer);
  config_ = std::move(config);
  return true;
}
```

局部 `unique_ptr/shared_ptr` 在失败返回时自动回收，最后一段 move 是提交点。仍需注意 callback 在 Reader 创建后是否可能立刻触发：如果 lambda 捕获的 `this` 依赖尚未提交的成员，就必须先让组件进入明确 Initializing 状态，或把 callback state 独立构造后再注册。

## `Proc()`：逐段采样辅助状态，而不是原子冻结全局快照

一次规划需要把主触发输入与异步辅助状态放进 `LocalView`。但“放进同一个结构体”不代表它们在同一个时刻被采样：固定提交的 `PlanningComponent::Proc()` 对不同辅助字段分开加锁、分段复制。下面保留这段真实顺序；摘录从函数入口截到快照组装完成，后面的 `CheckInput()`、`RunOnce()` 和发布逻辑省略。

**固定提交源码摘录（添加讲解注释，省略快照组装后的 `CheckInput()`、算法与发布主体；闭括号用于封闭摘录）：**`PlanningComponent::Proc()`

```cpp
bool PlanningComponent::Proc(
    const std::shared_ptr<prediction::PredictionObstacles>&
        prediction_obstacles,
    const std::shared_ptr<canbus::Chassis>& chassis,
    const std::shared_ptr<localization::LocalizationEstimate>&
        localization_estimate) {
  ACHECK(prediction_obstacles != nullptr);

  // check and process possible rerouting request
  CheckRerouting();

  // process fused input data
  local_view_.prediction_obstacles = prediction_obstacles;
  local_view_.chassis = chassis;
  local_view_.localization_estimate = localization_estimate;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!local_view_.planning_command ||
        !common::util::IsProtoEqual(local_view_.planning_command->header(),
                                    planning_command_.header())) {
      local_view_.planning_command =
          std::make_shared<PlanningCommand>(planning_command_);
    }
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    local_view_.traffic_light =
        std::make_shared<TrafficLightDetection>(traffic_light_);
    local_view_.relative_map = std::make_shared<MapMsg>(relative_map_);
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!local_view_.pad_msg ||
        !common::util::IsProtoEqual(local_view_.pad_msg->header(),
                                    pad_msg_.header())) {
      // Check if CLEAR_PLANNING PadMessage is received and process.
      if (pad_msg_.action() == PadMessage::CLEAR_PLANNING) {
        local_view_.planning_command = nullptr;
        planning_command_.Clear();
      }
      local_view_.pad_msg = std::make_shared<PadMessage>(pad_msg_);
    }
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    local_view_.stories = std::make_shared<Stories>(stories_);
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!local_view_.control_interactive_msg ||
        !common::util::IsProtoEqual(
            local_view_.control_interactive_msg->header(),
            control_interactive_msg_.header())) {
      local_view_.control_interactive_msg =
          std::make_shared<ControlInteractiveMsg>(control_interactive_msg_);
    }
  }
  // 固定源码从这里继续执行 CheckInput()、RunOnce() 与轨迹发布；此处为讲解而省略。
}
```

关键细节不是“全都加了一把 mutex”，而是**同一把 mutex 被分成多个临界区使用**。它保证每次 `CopyFrom()` 写入的成员与对应读取/复制不会同时访问同一 protobuf 对象；但在两个花括号之间，某个 Reader callback 可以先更新 `stories_` 或 `traffic_light_`。于是一次 `LocalView` 可能包含较早复制的 PlanningCommand 和较晚复制的 Stories。只有 traffic light 与 relative map 在同一个临界区复制，才共享同一个锁保护区间；其他字段没有跨字段的原子快照保证。

用一个具体时序看这个边界：`Proc()` 先复制 header 为 100 的 PlanningCommand 并释放锁；随后 Stories Reader callback 把 header 为 101 的新场景写入 `stories_`；`Proc()` 之后的 Stories 临界区再复制这条新值。`RunOnce()` 这时看到的是“命令 100 + 场景 101”。若两者本来就允许独立更新，这只是 latest 状态组合；若业务要求它们来自同一代 routing/scene，这个组合就必须被版本或 command id 检查拒绝。mutex 排除了同一 protobuf 的并发读写，却没有替业务定义跨 topic 的一致性。

这与“算法运行时输入不会变化”是两件事。辅助消息被复制进 `local_view_` 后，后续 Reader callback 改写的是组件成员，不会改写已复制的 protobuf 值；主输入则由 `shared_ptr` 延长消息对象寿命。`RunOnce()` 因而能消费这组已经拼好的对象，但这些对象不保证源时间戳相同，也不保证都来自同一轮 callback 更新。若业务需要跨字段一致性，应给输入附加版本/sequence 并校验组合，或在同一个受锁快照协议中复制需要共同一致的字段；仅仅使用相同 mutex、却分段解锁，并不能得到整体事务。

锁区只做字段比较和 protobuf 复制，不在锁内运行规划算法。若把 `RunOnce()` 放进锁里，Reader callback 可能被一次长规划阻塞，反而让下一周期看到更陈旧的辅助状态。

这里仍不是严格时间同步：三个模板输入由 DataVisitor 的 `AllLatest` 语义组合，辅助状态又在不同锁区间采样。组件必须检查 header timestamp、sequence 和业务超时，不能把“同时放入 `LocalView`”误解为“同一物理时刻采样”。

还要读懂参数的真实 C++ 类型：`const std::shared_ptr<T>&` 中的 `const` 限定的是 shared_ptr 句柄，`T` 本身仍是可修改类型；它并没有用类型系统禁止 callback 改写消息。本函数实际只读取主输入并把辅助状态 `CopyFrom()` 到组件自有成员，遵守的是调用约定。若要编译期保证只读，接口需要使用 `std::shared_ptr<const T>`，这与固定源码签名不同。辅助状态之所以另行复制，是因为 Reader callback 之后仍会更新组件成员；复制让本轮算法不依赖这些成员下一次何时变化。

### LocalView 需要携带可验证的时间契约

一次快照至少应能回答每个输入的源时间、接收时间和序号。可把检查规则写成表：

| 输入 | 触发/状态 | 最大年龄 | 跨输入约束 | 失效输出 |
|---|---|---:|---|---|
| Prediction | 主触发 | 由规划周期定义 | 与 Localization/Chassis 时间差受限 | not-ready trajectory |
| Chassis | 主输入最新值 | 控制状态阈值 | gear/speed 与定位时刻近似 | 安全停车/不规划 |
| Localization | 主输入最新值 | 最严格之一 | pose timestamp 单调 | 设计意图是 not-ready + reason；固定提交先调用依赖定位的 `SetLocation()`，缺失分支存在先解引用风险 |
| PlanningCommand | 辅助状态 | 可按 command 生命周期 | command ID 与 routing 对应 | 等待新命令 |
| TrafficLight/Story | 辅助状态 | 场景相关 | map/route 版本匹配 | 忽略陈旧感知或降级 |

“最大年龄”应来自配置并进入监控，不应散落成魔法常量。回放时还要决定使用消息 header 时间还是回放 Clock；否则同一 Record 在不同墙钟速度下会得到不同过期判断。

## 线程与共享状态地图

```text
各辅助 Reader 的 CRoutine（由 Processor OS 线程承载）
  -> Reader::Enqueue 保存消息
  -> 执行对应 callback，在 mutex 下 CopyFrom 到组件成员

PlanningComponent 的 Component CRoutine（由 Processor OS 线程承载）
  -> DataVisitor 取 Prediction / Chassis / Localization
  -> 分别进入多个 mutex 临界区，逐组复制辅助状态
  -> 锁外 CheckInput + RunOnce
  -> Writer::Write trajectory/status

mainboard shutdown thread（固定源码顺序）
  -> ComponentBase::Shutdown：先 Clear()，本类未覆盖，落到空实现
  -> 逐只 Reader::Shutdown：移除对应 Reader task
  -> RemoveTask(node_->Name())：移除并等待组件 task
  -> ModuleController 清空组件 owner，最后尝试卸载动态库
```

这里的 Reader callback 不是 transport 接收线程直接调用的业务 lambda。固定 reality-mode 路径中，`Reader::Init()` 把 `reader_func_` 包进 Reader 自己的 `RoutineFactory` task；Component 的主 `Proc()` 则由 `Component::Initialize()` 创建另一只 task。两者都是 scheduler 管理的逻辑任务，分别可能由不同 Processor 线程运行，因此要按可能并发来保护共享成员，不能因“它们都属于一个组件”就假定同线程串行。

同一把 `mutex_` 保护 PlanningComponent 的辅助成员，但 Proc 每复制一组就释放一次锁。PlanningCommand、traffic light/map、pad、stories 与 control-interactive 状态因此各自没有 data race，却不组成一个跨字段原子事务。若算法需要它们来自同一更新代，应额外记录并校验版本；若需要真的一致快照，就要改变源码的加锁范围或发布不可变聚合对象，不能只把锁名写成“共享 mutex”。

上图后半段是这份固定提交的关闭路径，不是抽象的推荐顺序。`PlanningComponent` 未覆写 `Clear()`，但 `ComponentBase::Shutdown()` 一般会在 Reader 和组件 task 排空之前调用派生 `Clear()`；其他组件若在 `Clear()` 释放仍被 `Proc()` 使用的资源，仍有前文所述的窗口。模块列表清空也不等于拓扑系统所有在途 callback 已自动静止，注销与等待边界仍要分别核查。

History、PlanningContext 和 Injector 中的服务也可能跨周期可变。它们通常由规划 CRoutine 单线程推进，比“所有字段都加锁”更容易证明；异步 callback 不应直接修改 Scenario/Frame 内部结构，而应只提交下一周期消费的状态。

## C++ 设计一：模板输入定义调度协议

`Component<A, B, C>` 不只是函数签名便利。它让框架在编译期知道三种 protobuf 类型，构造对应 DataVisitor，并把类型化 `shared_ptr` 传给 Proc。注意 `Proc()` 的形参虽然是 `const std::shared_ptr<T>&`，它只是不能重置形参里的指针，不能阻止函数修改 `*ptr`；消息只读在这条固定接口中是调用约定，不是 `const T` 类型保证。代价是支持的输入数量和组合由模板特化决定，编译时间、错误信息和 ABI 都更复杂。

设计能力是：用模板表达稳定的类型关系，用非模板 `ComponentBase` 提供动态插件边界。静态安全和运行时装配不是二选一，而是在不同边界同时使用。

## C++ 设计二：辅助 Reader 的共享状态

辅助 Reader callback 与 Proc 属于不同的 scheduler task，可能运行在不同调度上下文。固定代码用同一把 `mutex_` 保护对各 protobuf 成员的读写，避免单个对象被 `CopyFrom()` 与快照复制同时访问；但它没有把全部成员包在一个临界区里，因此只保证字段级访问互斥，不保证一整组辅助输入来自同一时刻。只看到 `CreateReader` 或“用了 mutex”都不足以判定快照一致性，还要追踪 callback executor、每个 lock scope 和版本字段。

可改进的通用结构是把每类辅助输入保存为不可变 `shared_ptr<const T>` 快照，callback 原子替换，Proc 在开始时一次性加载局部副本。这样一次规划内看到的辅助状态不会中途变化。

源码选择 `CopyFrom + mutex` 的优点是行为直观，protobuf 对象地址稳定；缺点是 callback 和 Proc 快照都可能发生深复制。不可变 `shared_ptr` 交换可以减少复制，却会把外部消息对象的寿命延长，并需要明确原子 shared pointer 或 mutex 语义。两种方案没有脱离 payload 大小与更新频率的绝对优劣。

## 多输入 latest 语义与非严格同步

DataVisitor 的 AllLatest 更接近“主输入到达时取其他输入最新值”。这适合规划低延迟，但可能组合不同时间的 Chassis 与 Localization。Planning 必须依赖 header timestamp、状态估计和超时检查判断数据是否仍可用。

优秀之处是框架没有强制昂贵的全输入 barrier；取舍是时间一致性责任进入业务模块。

## 从组件进入具体规划算法

默认车道规划的主线为：

```text
PlanningComponent::Proc
  -> CheckInput
  -> OnLanePlanning::RunOnce
  -> 初始化 Frame / ReferenceLine
  -> PublicRoadPlanner::Plan
  -> ScenarioManager::Update
  -> selected Scenario::Process
  -> current Stage::Process
  -> ordered Task::Execute / Process
  -> 组合 ADCTrajectory
```

这是 Strategy + State Machine + Pipeline 的组合。`PlanningBase` 在导航模式与车道模式间提供策略边界；ScenarioManager 按当前世界状态选择场景；Scenario 内按 Stage 推进状态；Stage 再按配置顺序运行 Task。

Task 顺序不是普通容器细节。前一个 Task 产生的 path boundary、reference line 或 speed decision 可能是后一个 Task 的输入；改变配置顺序等于改变算法数据依赖。要扩展新 Task，必须说明它读取和写入 Frame/ReferenceLineInfo 的哪些字段。

## Planning Task 的扩展流程

新 Task 不应从“复制现有类并改算法”开始，而应先定义数据契约：

1. 写出输入字段：Frame 中的障碍物、ReferenceLineInfo、PlanningContext 或配置；
2. 写出输出字段：path boundary、speed profile、decision 或 debug 信息；
3. 标注前置 Task：哪些字段必须已经产生；
4. 标注后继 Task：谁会读取本 Task 输出；
5. 定义失败语义：跳过、尝试 fallback、终止当前 reference line 还是发布 not-ready；
6. 估计复杂度和最大候选规模；
7. 最后把 Task 注册并放入对应 Stage 配置顺序。

最小类结构通常是：

```cpp
class SpeedGuardTask final : public Task {
 public:
  bool Init(const std::string& config_dir,
            const std::string& name,
            const std::shared_ptr<DependencyInjector>& injector) override;

  Status Execute(Frame* frame,
                 ReferenceLineInfo* reference_line_info) override;

 private:
  SpeedGuardConfig config_;
};
```

`Frame*` 和 `ReferenceLineInfo*` 是一次规划周期的借用对象，Task 不应把它们保存到下一周期。`DependencyInjector` 可共享跨周期服务，但不能让 Task 通过它偷偷写入没有生命周期协议的全局状态。`Status` 要带可诊断原因，不能只返回 false 让上层猜测。

测试时先构造最小 Frame/ReferenceLineInfo 验证纯 Task，再把 Task 放进单 Stage，最后才通过完整 PlanningComponent 和 Record 验证。这样失败能定位到算法、流水线配置或 Cyber 接入中的某一层。

## 输入未就绪时的可解释输出

这条路径要连同 `CheckInput()` 的调用顺序一起读，不能只看它后面的空值分支。固定提交的 `PlanningComponent::Proc()` 在快照之后调用 `CheckInput()`；而 `CheckInput()` 一进入便先调用 `SetLocation(&trajectory_pb)`，随后才判断 `local_view_.localization_estimate == nullptr`。`SetLocation()` 自己会读取 `local_view_.localization_estimate->pose()`，所以缺少 Localization 时，执行流可能在空值检查之前解引用空指针，根本到不了 not-ready 发布分支。

**固定提交中的关键语句节选（为突出顺序而压缩，局部变量名已简化，不是逐字源码）：**`PlanningComponent::CheckInput()` 与 `PlanningComponent::SetLocation()` 的关键顺序：

```cpp
// 固定提交摘录，省略 CheckInput 中其他字段检查与日志。
bool PlanningComponent::CheckInput() {
  ADCTrajectory trajectory_pb;
  SetLocation(&trajectory_pb);  // 先读取定位，再检查它是否为空
  auto* not_ready = trajectory_pb.mutable_decision()
                        ->mutable_main_decision()
                        ->mutable_not_ready();
  if (local_view_.localization_estimate == nullptr) {
    not_ready->set_reason("localization not ready");
  }
  // 其余检查、填 header 与发布代码省略。
}

void PlanningComponent::SetLocation(ADCTrajectory* trajectory) {
  auto* pose = trajectory->mutable_location_pose();
  pose->mutable_vehice_location()->set_x(
      local_view_.localization_estimate->pose().position().x());
  // 后续还会读取 y 坐标并尝试生成车道宽度。
}
```

这段摘录刻意把导致问题的因果顺序保留下来，并省略了不影响这个结论的代码。源码确实为其他未就绪条件构造、填充并发布带 `not_ready.reason` 的轨迹；但不能把它概括成“Localization 缺失时也安全发布”。针对这一输入，先检查必需数据、再生成依赖该数据的 Location，才是建议的修复方向；这是基于当前源码推导出的改进，不是此固定提交的行为。

这种设计让 Control 和监控系统区分“Planning 没有运行”与“Planning 运行了但输入未准备好”。安全系统中，负结果也是协议的一部分。只打日志而不发布状态，会让下游只能等超时猜测故障。

同理，正常发布轨迹后组件还发布 command execution status，并把轨迹加入 history。输出、反馈和历史是三个不同职责：Control 消费轨迹，命令发起方观察执行状态，规划内部使用历史形成跨周期上下文。

## Rerouting 展示 Client 与共享状态交互

当 planning context 标记需要重新路由时，组件构造 `LaneFollowCommand`，通过 Cyber Client 异步发出请求，再清除 `need_rerouting`。这里的危险是重复发送和状态竞争：检查、构造请求、发送与清标志应有明确串行上下文或锁。

若发送失败，直接清除标志可能丢失重试；若不清除，又可能每周期重复发请求。工业实现需要 request id、in-flight 状态、退避和结果回调，而不是单个 bool 承担完整协议。

## DAG 与进程边界的工程作用

Launch 中的 `process_name` 决定组件是否共进程，进而影响 INTRA/SHM/RTPS 路径和故障隔离。Planning 与其他模块共进程可减少传输成本，但一个未捕获异常可能扩大故障域。生产配置应把性能收益和独立重启需求一起评估。

DAG 还把组件库、类名、配置文件和 Reader 参数绑定成部署契约。ClassLoader 能加载类型并不表示配置兼容；发布包必须让 `.so`、DAG 和 proto 配置来自一致版本。动态插件边界也要求注册宏、符号可见性和基类 ABI 匹配。

## 性能预算与数据结构

单周期成本可近似拆为：输入 protobuf 深复制、LocalView 构造、参考线更新、Scenario/Stage/Task 计算、轨迹序列化与发布。规划算法通常远大于 Dispatcher 查表成本，但大消息复制与调试字段也可能显著增加尾延迟。

History 和 Frame 缓存必须有界，否则长时间运行会线性增长。轨迹点数量决定序列化成本，障碍物数量与候选路径/参考线数量又会放大算法复杂度。性能报告应同时给出障碍物规模、参考线数量、场景、debug 开关和 percentile，而不能只有平均 Proc 时间。

CRoutine 是协作式任务；一个过长且不 yield 的 Planning Proc 会占住对应 Processor。增加调度线程不能自动解决同一组件的最坏执行时间，也可能带来更多缓存争用。

### 一次周期的预算表

```text
Tcycle = Tvisitor_fetch
       + Tsnapshot_copy
       + Tinput_validation
       + Tframe/reference_line
       + Σ(Tscenario_stage_task)
       + Ttrajectory_build
       + Tserialize_publish
```

还要单独统计主输入到达至 routine 获得 Processor 的调度等待。若规划目标周期为 100 ms，平均算法 40 ms 不代表安全：障碍物峰值、双 reference line、debug 字段和同 Processor 上其他长 routine 可能把 p99 推过 deadline。

LocalView 中 protobuf 深复制的成本近似字段总大小；不可变 shared pointer 快照可以减少复制，却会延长消息寿命。History 若保留 H 个 Frame，每帧障碍物/候选轨迹规模为 S，内存主项近似 `O(H×S)`，必须有固定淘汰策略。

## 优秀设计与工程取舍

Apollo Planning 最值得学习的地方，是把“消息何时到达”与“算法怎样规划”隔开。Cyber Component 负责调度和通信，`LocalView` 把本周期输入冻结成稳定视图，`PlanningBase` 再消费这个视图。算法因此不需要在每个 Task 中直接访问 Reader，也不会在一次计算中途突然换输入。

模板主输入把关键调度契约放进 C++ 类型，错误更早暴露；普通 Reader 则允许低频状态独立更新，避免为了一个交通灯消息重新设计固定模板签名。代价是系统同时存在框架管理的输入和组件自行管理的共享状态，开发者必须自己维护锁、时间戳与关闭顺序。

Scenario → Stage → Task 的层级让大型规划流程可以配置和替换，但也会把执行顺序分散到 proto、注册表和多个类。它适合算法团队并行开发的生产项目；对只有一种简单策略的小机器人，直接复制整套层级会增加不必要的动态分派和配置复杂度。

## 缺点与性能边界

大量 protobuf 深复制会增加延迟与内存带宽；多输入 latest 语义不能证明严格同步；动态 DAG、共享库与 proto 必须版本一致；Planning 算法的最坏执行时间仍会占住 CRoutine 所在 Processor。Cyber 提供隔离和调度边界，但不会自动使复杂算法满足实时上界。

辅助 Reader 使用共享成员和互斥锁，新增状态时容易出现“字段已加入但未纳入同一快照”的错误。`shared_ptr` 能延长消息寿命，却不能保证消息时间一致；锁能排除 data race，也不能证明业务数据来自同一时刻。

这套结构适合拥有多源感知、复杂场景状态机和离线 Record 回放需求的自动驾驶系统。它不适合资源很小、输入固定、只运行单一局部控制律的控制器；最内层制动与转向安全也不能只依赖普通 Planning 周期和 Cyber 调度。

## 可迁移的规划组件方法

1. 将输入按“触发事件”和“持续状态”分类，而不是把所有消息塞进一个 callback；
2. 用类型化主输入固定执行签名，辅助输入保存不可变快照并记录源时间；
3. 在周期入口一次性冻结输入视图，锁外运行耗时算法；
4. 让算法层只依赖 `LocalView` 和输出值，不直接依赖 Reader、Writer 或 Node；
5. 缺失、过期和跨输入时间偏差都形成可发布的状态，而不是简单返回失败；
6. 部署文件、注册类名、配置 proto 与共享库作为同一个版本单元发布；
7. 用 Record 固化输入，并用 sequence、timestamp 和业务容差验证重放。

## 最小复刻：构建可回放的规划组件

不要直接复制 Apollo 全部 Planning。可以先实现如下工程：

```text
mini_planning/
├── proto/mini_planning.proto
├── mini_planning_component.h/.cc
├── mini_planning_base.h/.cc
├── straight_strategy.cc
├── safe_stop_strategy.cc
├── conf/mini_planning.pb.txt
├── dag/mini_planning.dag
└── launch/mini_planning.launch

主输入：Prediction + Chassis + Localization
辅助输入：PlanningCommand
输出：MiniTrajectory + PlanningStatus
```

开发顺序如下：

1. 定义 proto，让输出携带 header、输入 sequence 集合和 status reason；
2. 创建 `Component<A, B, C>`，`Proc()` 只检查输入并发布固定速度直线轨迹；
3. 在 `Init()` 中创建辅助 Reader，锁内复制 command，关闭时先停止 callback ingress；
4. 引入 `LocalView`，在锁内冻结状态，在锁外检查年龄和跨输入 skew；
5. 抽出 `MiniPlanningBase::RunOnce(const LocalView&, Output*)`，确保算法不依赖 Cyber Node；
6. 加入正常直线与安全停车两种 Strategy，所有失败出口都产生可解释输出；
7. 编写 DAG、config 与 launch，让 library、class、topic 和 proto 形成闭环；
8. 最后录制输入 channel，使用确定的回放 Clock 比较相同输入集合下的业务输出。

Record 验证不应要求整条 protobuf 字节完全一致，因为时间戳、debug 顺序和浮点末位可能变化。应先确认每次 `Proc()` 使用相同的 input sequence set，再按轨迹点位置、速度、状态码等字段设置业务容差。输入集合不同属于调度或快照问题，不能归因于算法随机性。

完成标准是：辅助 callback 与算法没有数据竞争；一次 `Proc()` 只使用冻结视图；过期输入发布明确原因；算法可以用手工构造的 `LocalView` 测试；Reader/Writer 创建失败不会留下半初始化组件；关闭后没有 callback 或 CRoutine 继续访问已卸载插件；切换 DAG 进程边界不改变业务接口。
