# 启动与装配：从 DAG 到 Component 实例

假设你已经写好一个点云感知算法，并希望同一份二进制在不同车辆上复用：A 车订阅前激光雷达，B 车改订阅侧向雷达；某辆车还想把感知和规划放进同一个进程。若所有 channel、队列和业务类都写死在 `main()`，每换一种部署就得重编运行时。Cyber RT 把这些决定移进 DAG（Directed Acyclic Graph，在这里是用于装配组件的部署配置），再让 `mainboard -d example.dag` 根据配置创建对象。问题随之变成：动态库如何变成具体 Component，Component 怎样得到 Node 与 Reader，Reader 又在什么时候与 DataVisitor 和调度任务接通？

先确定角色：`Component` 是业务模块实例；`Node` 是创建通信端点的命名门面；`Reader` 订阅某一路 channel；`DataVisitor` 让一个消费者按自己的游标从缓存取消息；task 是调度器能够恢复执行的工作。Transport 负责送达消息，Scheduler 负责安排 task，而 DAG 决定部署时创建哪些业务对象。本章只处理“第一条消息到达之前”的装配，不把通信线程与业务执行线程混为一谈。

这个问题必须先于消息链。若还不知道 Component、Node、Reader、DataVisitor 和 task 从哪里来，阅读[完整消息链](message-to-proc.md)时就无法回答 `DataDispatcher::Dispatch()` “把消息交给谁”；若不知道谁拥有动态库和组件对象，也无法理解 shutdown 为什么要按特定顺序执行。本页先建立对象的出生、归属和退出关系；接下来的 [Node/Reader/Writer](node-reader-writer.md) 章节展开通信端点，而[动态装载与 ABI](class-loader-abi.md)留到消息链建立之后深入讨论。

本章固定在 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，以 `reality mode` 下的单输入 `Component<M0>` 为主线。这里的 `reality mode` 是 Cyber 的运行模式开关，不是“真实车辆”与“仿真”的同义词：该提交中开关为真时，Reader 只接收并入缓存，组件另建 DataVisitor 和 CRoutine task；为假时，Reader 直接带回调。`Component<M0>` 的尖括号表示编译期指定消息类型 M0。第一条消息尚未出现，我们只研究对象创建、所有权和注册关系。

## 业务类型与运行时类名的双入口

下面是一个**教学接口示例，不是 Apollo 固定提交的原样源码**。业务作者面对的是 C++ 类型：组件通过继承专用基类并覆盖虚函数实现初始化和消息处理。`override` 让编译器确认函数签名确实覆盖了基类虚函数；`std::shared_ptr<T>` 是共享所有权句柄，这里用于在回调间传递消息并延长其生命周期：

```cpp
class MyComponent : public Component<InputMessage> {
 public:
  bool Init() override;
  bool Proc(const std::shared_ptr<InputMessage>& msg) override;
};

CYBER_REGISTER_COMPONENT(MyComponent)
```

运行时面对的却是配置字符串：动态库路径和 `class_name`。它无法在 `mainboard` 源码里直接写 `new MyComponent`，因为 mainboard 编译时甚至不知道以后会有哪些业务组件。

这两种视角由 class factory 连接：

```text
编译组件动态库
  -> 注册宏为 MyComponent 生成工厂登记代码

mainboard 读取 DAG
  -> 加载动态库
  -> 按 class_name 查工厂
  -> 工厂创建 MyComponent
  -> 以 shared_ptr<ComponentBase> 保存
```

这里可以顺便拆开三个 C++ 概念。

基类指针 `ComponentBase*` 可以指向派生对象 `MyComponent`，这是多态的对象关系。只有基类中的虚函数调用才会按对象真实类型分派到派生实现；普通同名函数不会自动得到这种行为。

工厂把“选择哪一种具体类”从调用者中拿走。`ModuleController` 只依赖 `ComponentBase`，新增组件时不需要修改 mainboard 的 `if/else`。所谓 Factory Pattern 的价值正在这里：它隔离的是未来会不断增加的组件类型。

动态库又让这种隔离跨过编译边界。组件代码可以单独构建为 `.so`，DAG 决定运行时加载哪个库。ABI 是应用二进制接口（Application Binary Interface），即两个已编译模块对函数调用、对象布局和运行库的约定；类名错误、ABI 不兼容和缺少符号因此可能到装载阶段才暴露。

## DAG 的三层部署结构

Cyber 用 protobuf message 把“加载哪个动态库、创建哪些组件、实例订阅什么”拆成嵌套配置对象。下面是**固定提交源码摘录**，节选自实际 schema 中相关 message 定义（省略 import、注释和无关字段）：

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

为什么配置要分三层？一个动态库可以提供多个类，一个 DAG 也可以从同一库创建多个组件实例；类描述“代码是什么”，ComponentConfig 描述“这次实例怎样运行”。若把 channel 和 queue size 编译死在 C++ 类中，同一个感知算法就很难复用于不同车辆或仿真环境。

`pending_queue_size` 尤其不是无关紧要的配置。它会进入组件自己的 `DataVisitor`，决定有限 ring 保留多少条历史；消费者落后时，旧消息何时被覆盖也由它决定。部署配置因此会改变控制链的数据年龄，而不仅是改变内存用量。

到这里，DAG 仍只是 protobuf 对象。下一步是找谁读取它，以及读完后谁拥有创建出来的组件。

## `mainboard` 把配置交给 `ModuleController`

进程入口 `main()` 先解析部署参数、初始化全局 Cyber 运行时，再创建并启动 `ModuleController`。下面是**固定提交源码节选**，省略信号处理和 profiling 语句，保留 DAG 标识整理、初始化失败与正常关闭三条主路径：

```cpp
int main(int argc, char** argv) {
  ModuleArgument module_args;
  module_args.ParseArgument(argc, argv);
  auto dag_list = module_args.GetDAGConfList();

  std::string dag_info;
  for (auto&& i = dag_list.begin(); i != dag_list.end(); i++) {
    size_t pos = 0;
    for (size_t j = 0; j < (*i).length(); ++j) {
      pos = ((*i)[j] == '/') ? j : pos;
    }
    if (i != dag_list.begin()) {
      dag_info += "_";
    }
    if (pos == 0) {
      dag_info += *i;
    } else {
      dag_info +=
          (pos == (*i).length() - 1) ? (*i).substr(pos) : (*i).substr(pos + 1);
    }
  }
  if (module_args.GetProcessGroup() !=
      apollo::cyber::mainboard::DEFAULT_process_group_) {
    dag_info = module_args.GetProcessGroup();
  }

  apollo::cyber::Init(argv[0], dag_info);

  ModuleController controller(module_args);
  if (!controller.Init()) {
    controller.Clear();
    return -1;
  }

  apollo::cyber::WaitForShutdown();
  controller.Clear();
  return 0;
}
```

```text
main(argc, argv)
  -> ModuleArgument::ParseArgument
       读取 -d/--dag_conf、进程组和 scheduler 参数
  -> cyber::Init
       初始化全局配置、拓扑、transport、scheduler 等设施
  -> ModuleController::Init(dag list)
  -> ModuleController::LoadAll()
       对每个 DAG 调用 LoadModule()
  -> WaitForShutdown()
  -> ModuleController::Clear()
```

`main()` 栈上的 `ModuleController` 是这一批模块的顶层所有者。它内部持有 `ClassLoaderManager`，还用 `vector<shared_ptr<ComponentBase>> component_list_` 保存已经初始化成功的组件实例。

这解释了一个重要设计选择：组件不是由 Scheduler 拥有，也不是由 Node 拥有。Scheduler 拥有的是执行任务，Node 拥有或索引通信实体；真正决定组件对象何时析构的是 ModuleController 的 `component_list_`。

## `LoadModule()` 的装载与实例化流程

`ModuleController::LoadModule()` 对一个 ModuleConfig 连续完成四层动作：

```text
resolve module_library
  -> ClassLoaderManager::LoadLibrary(path)
  -> for each ComponentInfo
       -> CreateClassObj<ComponentBase>(class_name)
       -> base->Initialize(component_config)
       -> component_list_.emplace_back(base)
```

以下是 `ModuleController::LoadModule()` 的**固定提交源码摘录**，保留普通组件分支，省略 timer component 的同构分支和日志：

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

这个片段显示三个生命周期节点不是一句“加载模块”可以代替的：先装入库，才能调用库里的 factory；得到对象后先执行框架 `Initialize()`；初始化成功后才把强引用移交给 `component_list_`。`std::move(base)` 是把局部句柄移动进 vector，不是复制一份组件，也不会让对象地址改变。

顺序不能随意改变。必须先加载 `.so`，库内静态注册对象才有机会把 class factory 放进 registry；必须在 `Initialize()` 成功后才把实例视为正常运行组件；必须持续保存组件强引用，scheduler callback 捕获的 weak self 才能在运行期间锁定成功。

`CreateClassObj<Base>()` 是函数模板：调用点把期望的基类类型作为模板参数传入，工厂再检查登记类是否能按这个基类创建。返回 `shared_ptr<Base>` 后，调用者不需要知道实际派生类型。

这里的 `shared_ptr` 还带有与 class loader 协作的删除逻辑。对象释放时，deleter 不只执行 `delete`，还减少该 loader 的活动对象计数；只要库中仍有对象存活，卸载动态库就不安全。

为什么不能先 `UnloadLibrary()` 再删除组件？派生类虚表、析构函数和成员函数的机器码都位于 `.so` 中。库被卸载后再通过基类指针析构对象，程序可能跳到已经不存在的代码地址。由此得到正常关闭的硬约束：先停止回调并销毁所有派生对象，最后才能卸载库。

## `Initialize()` 与业务 `Init()` 不是同一个函数

这是第一次阅读 Cyber Component 时最容易混淆的命名。

业务类覆盖的是无参 `Init()`，用来读取自身配置、创建 writer 或初始化算法状态。框架调用的是带 `ComponentConfig` 的 `Initialize()`，它负责搭建通用运行时骨架，并在中间调用用户的 `Init()`。

单输入组件的初始化顺序可以从 `Component<M0>::Initialize()` 还原为：

```text
Component<M0>::Initialize(config)
  -> 保存配置，创建 Node(config.name)
  -> 加载 flag/config 文件
  -> 调用派生类 Init()
  -> 从 ReaderOption 构造 ReaderConfig
  -> Node::CreateReader<M0>(reader_cfg)
  -> ComponentBase::readers_ 保存 Reader
  -> 创建组件专用 DataVisitor<M0>
  -> 创建调用 Process(msg) 的 RoutineFactory
  -> Scheduler::CreateTask(factory, node name)
```

先看初始化前半段的**固定提交源码摘录**（省略错误日志）：

```cpp
node_.reset(new Node(config.name()));
LoadConfigFiles(config);

if (config.readers_size() < 1) {
  return false;
}

if (!Init()) {
  return false;
}

bool is_reality_mode = GlobalData::Instance()->IsRealityMode();
```

所以业务 `Init()` 不是 reader 建好后的回调，而是框架搭骨架时的一道成功门槛：它失败时 `Initialize()` 提前返回，后面的订阅和调度任务都还未创建。固定版本对应 `Component<M0>::Initialize()`。

接着，原函数用以下**固定提交源码摘录**分开两种运行模式（`func` 的统计采样语句省略）：

```cpp
std::shared_ptr<Reader<M0>> reader = nullptr;

if (cyber_likely(is_reality_mode)) {
  reader = node_->CreateReader<M0>(reader_cfg);
} else {
  reader = node_->CreateReader<M0>(reader_cfg, func);
}

if (reader == nullptr) {
  return false;
}
readers_.emplace_back(std::move(reader));

if (cyber_unlikely(!is_reality_mode)) {
  return true;
}

data::VisitorConfig conf = {readers_[0]->ChannelId(),
                            readers_[0]->PendingQueueSize()};
auto dv = std::make_shared<data::DataVisitor<M0>>(conf);
croutine::RoutineFactory factory =
    croutine::CreateRoutineFactory<M0>(func, dv);
auto sched = scheduler::Instance();
return sched->CreateTask(factory, node_->Name());
```

注意这里 `func` 的本地定义将在后文回调小节结合原码展开；本片段只省略创建它的代码，没有改变调用关系。reality mode 下 Reader 不接收业务 `func`，组件再建立自己的 DataVisitor task；另一分支则将 `func` 交给 Reader 并在此返回。

为什么框架不先创建 Reader 再调用用户 `Init()`？业务 `Init()` 可能决定自身是否能工作，也可能需要创建输出 Writer 或算法资源。只有它成功，通用输入链才有继续建立的意义。另一方面，这也要求业务初始化失败时正确释放自己已经创建的资源，最好使用 RAII 对象而不是散落的裸指针。

RAII 是 Resource Acquisition Is Initialization（资源获取即初始化），意思是把资源寿命绑定到对象寿命：文件、线程、内存或句柄由成员对象构造时获得、析构时释放。这样后续任一步返回 false，C++ 栈展开和成员析构仍能回收已经成功创建的资源。

## callback 的弱所有权设计

组件 task 最终需要调用对象的 `Process()`。如果闭包直接捕获 `shared_ptr<Component>`，会形成潜在所有权环：

```text
ModuleController -> Component
Scheduler -> CRoutine -> callback -> Component
```

即使 ModuleController 清空列表，Scheduler task 仍强持有 Component；而 Component 的关闭又要删除 Scheduler task，生命周期容易互相卡住。

Cyber 在 `Component<M0>::Initialize()` 中捕获弱引用。`weak_ptr` 观察共享对象却不延长它的寿命，`lock()` 会在调用时尝试取得临时强引用；`shared_from_this()` 要求对象已经进入某个 `shared_ptr` 的控制块，不能为裸对象凭空创建共享身份。下面是**固定提交源码摘录（省略统计语句）**：

```cpp
std::weak_ptr<Component<M0>> self =
    std::dynamic_pointer_cast<Component<M0>>(shared_from_this());

auto func = [self, role_attr](const std::shared_ptr<M0>& msg) {
  auto ptr = self.lock();
  if (ptr) {
    ptr->Process(msg);
  }
};
```

lambda 是一个可保存的函数对象。方括号 `[self, role_attr]` 表示把两个变量按值复制到闭包中；即使 `Initialize()` 返回，这些副本仍随 callback 存活。

`weak_ptr` 不增加对象的强引用计数。调用前必须 `lock()` 尝试取得临时 `shared_ptr`；若组件已经析构，返回空指针，callback 什么也不做。这样调度系统可以引用“组件仍存在时要执行的动作”，却不拥有组件本身。

`dynamic_pointer_cast` 则把 `shared_from_this()` 得到的基类共享指针安全转换为当前模板类型。它依赖对象一开始就由 `shared_ptr` 管理；若对一个没有共享控制块的裸对象调用 `shared_from_this()`，会抛出错误。

这些语法不是装饰。它们共同表达一条生命周期规则：ModuleController 决定组件是否存在，Scheduler 只能在执行瞬间借用它。

## Component 与 Node 的所有权边界

初始化早期会用组件名创建 Node。Node 不是另一个进程，也不是 Scheduler task；它是当前进程内的通信门面和拓扑身份。

```text
Component instance
  `-- shared_ptr<Node>
       `-- unique_ptr<NodeChannelImpl>
            |-- reader map
            |-- writer map
            `-- topology role operations
```

`unique_ptr<NodeChannelImpl>` 表示独占所有权：一只 Node 只有一个具体 channel 实现，Node 析构时它必然随之析构。Node 对 Reader 常用 `shared_ptr`，因为 Reader 还会被 ComponentBase 的 `readers_` vector 引用，并可能被调用者临时持有。

同一个 Reader 同时出现在 Node 的 map 和 ComponentBase 的 vector，并不是创建了两只 Reader。两处保存的是同一控制块的共享指针：Node 需要支持按 channel 查询和删除，ComponentBase 需要在关闭时统一遍历所有输入。

共享所有权让两个管理视角都能安全引用对象，也使析构时刻不再由某一个容器单独决定。分析这类代码时，必须问“还有谁持有 strong reference”，不能看到某处 `erase()` 就认为对象已经销毁。

## `Node::CreateReader()` 建立的运行时对象

`CreateReader<M0>(reader_cfg)` 不是简单返回一个带 callback 的包装器。把 Node 的工厂调用与 Reader 初始化连起来看，会得到：

```text
Node::CreateReader<M0>
  -> NodeChannelImpl::CreateReader
       -> 填充 RoleAttributes: node/channel id、QoS、进程信息
       -> make_shared<Reader<M0>>
       -> Reader<M0>::Init()
            -> 创建 Blocker
            -> 创建 Reader 自己的 DataVisitor<M0>
            -> 创建 Enqueue callback 的 RoutineFactory
            -> Scheduler::CreateTask(reader task)
            -> ReceiverManager<M0>::GetReceiver(role_attr)
       -> Reader::JoinTheTopology()
            -> 监听 writer 加入/离开
            -> 启用已有 writer 的 receiver
            -> 宣告 ROLE_READER
```

这解释了为什么 Reader 是运行时对象而非纯 API：它同时落在数据、执行和发现三条链上。

`NodeChannelImpl::CreateReader()` 的**固定提交源码摘录**展示了这段装配中最关键的选择：

```cpp
if (!role_attr.has_channel_name() || role_attr.channel_name().empty()) {
  AERROR << "Can't create a reader with empty channel name!";
  return nullptr;
}

proto::RoleAttributes new_attr(role_attr);
FillInAttr<MessageT>(&new_attr);

std::shared_ptr<Reader<MessageT>> reader_ptr = nullptr;
if (!is_reality_mode_) {
  reader_ptr =
      std::make_shared<blocker::IntraReader<MessageT>>(new_attr, reader_func);
} else {
  reader_ptr = std::make_shared<Reader<MessageT>>(
      new_attr, reader_func, pending_queue_size);
}

RETURN_VAL_IF_NULL(reader_ptr, nullptr);
RETURN_VAL_IF(!reader_ptr->Init(), nullptr);
return reader_ptr;
```

先补齐 `RoleAttributes`，再按运行模式选择 Reader 类型，最后执行 `Init()` 并检查失败。这也说明 `CreateReader()` 不只是 `make_shared`：构造、task/receiver 初始化与之后的拓扑加入共同组成有效订阅。

Blocker 服务 `Observe()` 一类通用读取接口；DataVisitor 和 Reader task 把 Dispatcher 中的数据搬到 Blocker；Receiver 接入 transport；topology 负责在远端 Writer 出现时启用通信。

在 reality mode 下，Component 创建 Reader 时没有把业务 `Proc()` 闭包交给它。Reader task 只执行 `Enqueue()`。Component 随后再创建第二只 DataVisitor 和第二条 task，用于调用 `Process()/Proc()`：

```text
one channel
  |
  +-> Reader DataVisitor -> Reader CRoutine -> Blocker
  |
  `-> Component DataVisitor -> Component CRoutine -> Proc
```

为什么不让 Reader task 同时调用 `Proc()`？分开后，通用 Reader 观察语义与 Component 连续计算语义拥有独立游标和 backlog；一方变慢不会“取走”另一方的数据。代价是同一 channel 多一份 ring、多一次 shared pointer 写入、多一个 notifier 和多一条调度任务。

## 第一条消息到来前的对象图

完成初始化后，主要强引用和弱引用可以画成：

```text
ModuleController
  |
  `-- shared_ptr<ComponentBase> -------- concrete Component<M0>
                                           |
                                           +-- shared_ptr<Node>
                                           |     `-- shared_ptr<Reader<M0>>
                                           |
                                           +-- shared_ptr<ReaderBase>  (同一 Reader)
                                           |
                                           `-- component task name/id

Reader<M0>
  +-- shared_ptr<Receiver<M0>>
  +-- unique_ptr<Blocker<M0>>
  `-- Reader CRoutine -> shared_ptr<DataVisitor<M0>> -> shared CacheBuffer

Scheduler
  `-- shared_ptr<CRoutine> -> callback -> shared DataVisitor
                                      `-> weak Component self

DataDispatcher
  `-- weak_ptr<CacheBuffer> entries

ReceiverManager<M0>
  `-- shared_ptr<Receiver<M0>> by channel
```

这张图回答了几个常见疑问。

DataVisitor 为什么不会在 `Initialize()` 返回后析构？因为 RoutineFactory 创建的协程函数按值持有它，Scheduler 又持有 CRoutine。

Component 为什么能关闭而不被 callback 永久续命？因为组件 callback 只捕获 weak pointer。

Reader 清空自己的 `receiver_` 后，底层 receiver 为什么可能还活着？因为模板单例 ReceiverManager 还按 channel 持有强引用。

DataDispatcher 为什么存 weak buffer？它是进程级 singleton，寿命往往长于任一 Reader；若 registry 反向强持有缓存，删除 task 也无法回收 DataVisitor 链。

## 初始化失败与局部回滚

工业代码不能只分析成功路径。`Component::Initialize()` 中用户 `Init()`、Reader 创建和 task 创建都可能失败，`ModuleController::LoadModule()` 也会据此返回 false。

已经放进 `component_list_` 的前序组件会在 `ModuleController::Clear()` 中关闭。当前正在初始化的 `base` 还是 `LoadModule()` 的局部 `shared_ptr`；函数返回时，它会析构，已经作为成员保存的 Node、Reader 和 RAII 资源随对象释放。

需要特别小心派生类自己创建的裸线程、文件或 C 句柄。如果派生 `Init()` 已经建立资源，而后续 Reader 或 task 创建失败，当前对象可能尚未进入 `component_list_`，不能假定常规 `Clear()` 一定覆盖它。让成员析构自动释放资源，比依赖“以后某处会调用 Clear”更稳健。

另一个问题是 task 与注册表的残留。Reader 析构会调用自己的 Shutdown 并删除 Reader task；DataVisitor 析构后 Dispatcher 的 weak buffer 无法再锁定。但 DataNotifier 和某些 registry 未必立即移除旧项，动态反复加载时可能留下额外扫描成本。

## 关闭时要区分“拒绝新回调”和“等在途回调结束”

正常退出从 `ModuleController::Clear()` 开始。它逐个调用组件的 `Shutdown()`，清空 `component_list_`，最后才让 class loader 尝试卸载动态库。这个外层顺序有一个必要约束：派生组件的析构函数和虚函数代码都在组件 `.so` 中，必须先销毁组件对象，再卸载承载这些代码的库。真正容易误读的是组件内部的顺序。

**固定提交源码摘录：**`ComponentBase::Shutdown()` 的实际次序如下：

```cpp
virtual void Shutdown() {
  if (is_shutdown_.exchange(true)) {
    return;
  }

  Clear();
  for (auto& reader : readers_) {
    reader->Shutdown();
  }
  scheduler::Instance()->RemoveTask(node_->Name());
}
```

这里的 `is_shutdown_` 是原子布尔值。它解决的是并发读写这个标志本身的问题：关闭线程用 `exchange(true)` 标记关闭，`Process()` 入口用 `load()` 读取。但“标志是原子变量”不等于“关闭线程已经等到 `Proc()` 结束”。**原子性只保证这次标志读写不会彼此撕裂，不会自动暂停另一个线程，也不会替业务对象的其他成员加锁。**

**固定提交源码摘录：**单输入组件的 `Component::Process()` 只在进入用户函数之前检查标志：

```cpp
bool Component<M0, NullType, NullType, NullType>::Process(
    const std::shared_ptr<M0>& msg) {
  if (is_shutdown_.load()) {
    return true;
  }
  return Proc(msg);
}
```

考虑一个相机组件：`Proc()` 正在使用派生类成员 `detector_` 做推理，而 `Clear()` 会释放 `detector_`。可能出现这样的交错：

```text
Processor 线程                         mainboard 关闭线程
------------------------------------   ---------------------------------
Process() 读取 is_shutdown_ == false
进入 Proc()，开始使用 detector_
                                       Shutdown() 将 is_shutdown_ 置为 true
                                       调用派生 Clear()
                                       释放 detector_
Proc() 继续访问 detector_  -> 悬空访问
```

这不是“原子变量失效”，而是原子标志与派生资源之间没有建立“回调已退出”的等待关系。对已经越过 `Process()` 检查、正在执行 `Proc()` 的调用，后续把标志设成 true 不会让它倒退回函数入口。

确实，调度器随后会等待组件任务退出，但这一步发生在 `Clear()` 之后。SchedulerClassic::RemoveCRoutine() 先对 CRoutine 调用 `Stop()`，再由 ClassicContext::RemoveCRoutine() 反复尝试取得它的执行标志；拿到后才从运行队列删除。换句话说，`RemoveTask()` 可以作为“等待该 routine 不再被占用后移除”的边界，但它**不能保护已经在它之前执行完的 `Clear()`**。此外，Reader task 的 `Shutdown()` 也发生在 `Clear()` 后；不能因为它最终会移除 Reader task，就推断组件业务回调在派生清理开始前已经排空。

这一区分在工程上很重要：**设置关闭标志**表示后续进入 `Process()` 的调用会被挡住；**移除并排空任务**表示调度上下文之后不再执行该 routine；二者之间还有“正在运行的用户代码能否继续访问被释放资源”这一段生命周期窗口。把三件事都简称为“关闭协程”，就会漏掉这里的风险。

若设计一个更稳妥的关闭协议，可以把“请求停止”和“释放业务资源”分成两阶段：先设置关闭标志并停止 Reader 的新输入，再移除组件 task、等待可能已进入 `Proc()` 的调用退出，最后调用负责释放 `detector_` 等资源的 `Clear()`。这只是根据当前源码顺序推导出的改进方案，不是 Apollo 当前代码。真实组件还需检查 Reader、输出 Writer、共享资源及其回调之间的依赖，并避免在 `Proc()` 内同步调用会等待自身结束的关闭路径，否则会形成自等待。

按当前实现分析时，派生类应明确承担一个额外约束：`Clear()` 不能假定 `Proc()` 一定已经结束；若它会释放 `Proc()` 正在使用的状态，就必须由业务侧生命周期设计提供同步，或调整关闭协议。反过来，若 `Proc()` 卡在不返回的阻塞调用中，稍后移除任务时也可能一直等不到执行标志释放。合作式调度无法安全地从外部切断任意 C++ 调用栈，因此回调应使用有界等待、可取消 I/O，并响应停止状态。

## 装配配置对机器人运行的影响

虽然本章没有一条传感器消息，控制后果已经出现。

DAG 中的 `pending_queue_size` 决定后续缓存窗口；组件和进程的分组决定哪些任务共享 Processor；动态库装箱决定哪些模块共享地址空间、全局 registry 和内存带宽；Reader 在 topology 中宣告的属性又影响 HybridReceiver 最终选择何种 transport。

启动路径本身也影响可用性。动态库装载、配置解析、Reader 加入拓扑和 transport enable 都发生在第一条有效处理之前。若上层系统在组件尚未完成匹配时就认为控制链 ready，首批消息可能缺失或使用旧状态。一个完整部署应定义“进程启动”和“数据链已就绪”之间的差别。

动态配置提升复用，却降低编译期保障。工业部署通常需要在发车前检查 DAG 中的库路径、class name、channel 类型、QoS 和 scheduler group；错误不应等到车辆运行中才通过超时表现出来。

## 自行实现装配层时的核心不变量

先做一个只支持静态链接的工厂：字符串映射到 `unique_ptr<ComponentBase>` 创建函数。验证新增派生组件时宿主无需修改。

再引入 DAG，把类名、实例名和输入 channel 从代码移到配置。此时要区分“类描述”和“实例配置”，允许同一类创建多个实例。

然后为顶层 Controller 定义唯一清晰的所有权：它强持有 Component，Component 强持有 Node 和 Reader，Scheduler 只拥有 task，callback 只弱引用 Component。任何 registry 若比业务对象活得久，都不应无条件强持有业务对象。

最后才接动态库，并把“活动对象数为零”设为卸载前置条件。失败路径必须让局部对象靠 RAII 回滚，正常关闭则严格执行“封入口—停任务—销毁对象—卸载代码”。

这些不变量比复刻类名更重要。即使最终不用 Apollo 的 class loader，只要所有权和卸载顺序相同，就已经掌握了这一层设计的核心。

## 从对象装配过渡到消息运行

现在，第一条消息到来前需要的对象已经全部出现：Component 与 Reader 存活，Receiver 已注册，两个 DataVisitor 各有缓存，两个 CRoutine 已进入 Scheduler，topology 也知道这只 Reader。

对象图建立后，就可以转向[完整消息链](message-to-proc.md)，从 `ReceiverManager` 的统一 listener 追踪不同 transport 的线程怎样汇流、Dispatcher 怎样把一枚 `shared_ptr` 写入多只缓存、Notifier 怎样只传事件，以及 Processor 怎样最终进入 `Component::Proc()`。专题导航在端点创建章节之后还安排了 class loader 和 Node/Reader/Writer 的细读；这些页分别补足动态类型与通信端点，再由消息链把它们连接起来。

换句话说，本章建立“谁存在、谁拥有谁”，消息链建立“运行时谁调用谁”。二者合在一起，Cyber RT 才会从一堆模板类变成一台能在脑中运转的机器。
