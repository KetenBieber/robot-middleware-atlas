# Cyber RT 的 C++ 类型系统：静态消息类型与动态组件运行时

本文中的真实 C++ 实现统一按 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 核验，并直接粘贴相关代码片段。

设想 Planning 在编译时必须明确接收 Prediction、Chassis 和 Localization，但车辆部署时又要由 DAG（此处指 Apollo 的组件部署配置）决定加载哪个业务组件。若所有决策都放在编译期，部署换模块就要重编 mainboard；若输入也只用字符串，消息类型错误又会拖到运行时才暴露。Cyber RT 把变化拆到不同边界：消息类型在编译期确定，组件实现通过共同生命周期接口在运行时选择。本章沿着这个接缝推进；模板、虚函数、函数包装、所有权和资源清理只在当前问题需要它们时逐一引入。

## 静态层与动态层的接缝

业务类处于静态类型世界。下面是根据固定提交 Planning 组件接口压缩的**签名示例**（省略命名空间、成员和配置方法；并非可替换的完整类定义）。`Component<...>` 是类模板，编译器会把尖括号里的消息类型代入基类定义，形成不同 C++ 类型。`std::shared_ptr<T>` 是共享所有权句柄，多个接收者可共同延长消息对象寿命；`virtual` 标注的函数允许通过共同基类接口调用时按对象真实派生类型选择实现。这里的 `override` 要求编译器核对签名确实覆盖了虚函数，`final` 则禁止这个具体类再被派生：

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

三个模板参数决定 `Proc()` 的三个参数类型。把 `Chassis` 写成 `Image` 会直接造成编译期接口不匹配，而不是等第一条消息到达再做字符串类型检查。模板是编译期生成类型关系的工具，不是运行时按名称寻找类型的机制。

部署层处于动态世界。下面是**教学配置示意，非固定提交原文**：

```protobuf
module_library: ".../libplanning_component.so"
class_name: "PlanningComponent"
```

mainboard 只知道库路径、类名和共同基类 `ComponentBase`。它先用 class loader 按字符串创建基类指针，再通过虚函数进入派生组件的 `Initialize()`。

接缝可以画成：

```text
runtime string "PlanningComponent"
  -> factory creates concrete C++ object
  -> shared_ptr<ComponentBase>
  -> virtual Initialize(config)
  -> concrete Component<M0,M1,M2>
  -> typed Reader/DataVisitor/Proc pipeline
```

外层动态选择“本次运行哪个类”，内层静态保证“该类处理什么消息”。这是大型 C++ 插件系统常见的组合：动态性停在稳定基类边界，性能敏感的数据路径保持模板类型。

## `ComponentBase` 提供非模板插件边界

class loader 需要一个所有组件都能共享的基类，否则 mainboard 无法用同一容器保存不同模板实例：

```text
Component<PointCloud>
Component<Prediction, Chassis, Localization>
TimerComponent
```

这些类的完整 C++ 类型彼此不同，但都能向上转换为 `ComponentBase`。ModuleController 因而可以使用下面的**固定提交成员类型摘录**：

```cpp
std::vector<std::shared_ptr<ComponentBase>> component_list_;
```

这种容器只能调用 `ComponentBase` 暴露的虚接口，例如初始化和关闭，不能直接调用带不同参数的 `Proc()`。这正是边界设计：进程装载层只管理生命周期，不参与类型化数据处理。

若把 ModuleController 本身做成模板，它就必须在编译时枚举所有消息组合，动态库扩展能力会消失。非模板基类把稳定的生命周期协议从不断增长的消息类型集合中抽离出来。

## `Component<M...>` 将消息类型带入整条链

`Component<M...>` 使用消息模板参数定义业务接口。未使用的输入位置由 `NullType` 表示，并通过不同模板特化实现一到四输入组件。

从设计角度看，它相当于把“输入个数”也编码进类型：

```text
Component<M0>
  Proc(shared_ptr<M0>)

Component<M0, M1>
  Proc(shared_ptr<M0>, shared_ptr<M1>)

Component<M0, M1, M2>
  Proc(shared_ptr<M0>, shared_ptr<M1>, shared_ptr<M2>)
```

编译器会为每个实际组合生成相应 `Initialize()`、DataVisitor 和 RoutineFactory 调用。业务代码不需要 `std::variant`、`dynamic_cast<MessageBase*>` 或手写参数数组。

这种设计的优势是类型错误早暴露、调用点可内联、消息访问不需要运行时分派。代价是模板实例增多会扩大编译时间和二进制体积，错误信息也容易深入多层模板。固定最多四输入还能保持实现可控，但扩展到五输入需要修改框架特化，而不是只改配置。

现代 C++ 也可以用 parameter pack（模板参数包：编译期收集零个或多个类型参数）表达任意输入数量。下面是**替代设计示例，不是 Apollo 实现**：

```cpp
template <typename... Messages>
class Component;
```

但任意数量并不自动带来更好设计。初始化 reader 列表、定义主触发输入、构造融合 tuple 和形成清晰 `Proc()` 接口仍需约束。Cyber 的固定上限用少量重复代码换取更直接的 API 和编译行为。

## 私有虚函数仍然可以被派生类覆盖

初学者常把访问控制和虚函数分派混为一件事。基类可以把 `Proc()` 声明为 private virtual，派生类仍能写 `override`；private 只限制“谁能用名字直接调用基类成员”，不禁止虚函数槽被覆盖。

Cyber 让框架内部 `Process()` 调用 `Proc()`。下面是**固定提交源码摘录**：`Component<M0>::Process()`。

```cpp
bool Component<M0, NullType, NullType, NullType>::Process(
    const std::shared_ptr<M0>& msg) {
  if (is_shutdown_.load()) {
    return true;
  }
  return Proc(msg);
}
```

这里形成两层入口：

```text
framework-owned Process()
  -> common shutdown guard / statistics boundary
  -> virtual Proc()
       -> user algorithm
```

业务作者实现 `Proc()`，却不应绕过 `Process()` 自行调用它。这个结构属于 Template Method：基类固定调用骨架和公共检查，派生类只填业务步骤。

把 guard 放在统一 wrapper 而不是要求每个派生类自己写，能防止一部分组件忘记处理 shutdown。代价是函数名相近，若文档不解释 `Process` 与 `Proc` 的边界，读者容易认为它们只是冗余转发。

## `override`、`final` 与接口演化

`override` 要求编译器验证派生函数确实覆盖某个虚函数。若参数中少了 `const`、引用类型不同或消息顺序写错，编译直接失败；没有 `override` 时，这种错误可能悄悄声明出一个新的重载，运行时仍调用基类版本。

`final` 用在 `PlanningComponent final` 上，表示不希望继续从这个业务组件派生。它缩小继承层次，也给编译器更多去虚化机会。框架扩展点位于 `Component<M...>`，具体算法组件通常不需要再成为二级框架。

虚接口一旦跨动态库边界，就涉及 ABI（Application Binary Interface，应用二进制接口）：两份已编译代码如何约定函数调用、对象布局和运行库类型。修改 `ComponentBase` 的虚函数顺序、对象布局或编译器配置，可能让旧组件库与新 mainboard 不兼容。运行时工厂带来部署灵活性，也要求基类接口保持稳定并协调版本。

## `Node::CreateReader<T>` 将类型继续传给通信层

Component 的消息模板类型不会在创建 Reader 时丢失。`Node::CreateReader<MessageT>()`、`Reader<MessageT>`、`ReceiverManager<MessageT>`、`DataDispatcher<MessageT>` 和 `DataVisitor<MessageT>` 继续沿用同一个 `MessageT`。

```text
Component<M0>
  -> Node::CreateReader<M0>
     -> Reader<M0>
        -> ReceiverManager<M0>
        -> DataVisitor<M0>
           -> CacheBuffer<shared_ptr<M0>>
        -> DataDispatcher<M0>
```

这是模板参数沿调用链传播。每一层都能直接调用 `M0` 的序列化 API或保存 `shared_ptr<M0>`，不需要公共 `Message` 基类。

固定提交的缓存不是前文教学 `deque` 的“只留最新值”，而是固定槽位 ring。以下是 `CacheBuffer<T>::Full/Fill()` 的**固定提交源码摘录**：

```cpp
bool Full() const { return capacity_ - 1 == tail_ - head_; }

void Fill(const T& value) {
  if (fusion_callback_) {
    fusion_callback_(value);
  } else {
    if (Full()) {
      buffer_[GetIndex(head_)] = value;
      ++head_;
      ++tail_;
    } else {
      buffer_[GetIndex(tail_ + 1)] = value;
      ++tail_;
    }
  }
}
```

`head_` 与 `tail_` 是逻辑序号而不是物理下标；`GetIndex(pos)` 再用 `pos % capacity_` 映射到 vector。满时先覆盖最旧逻辑位置并同时推进两端，逻辑容量因此是底层槽位数减一。读者可结合 `ChannelBuffer<T>::Fetch()` 看到消费者游标落后时如何检测越界并追到仍在缓存中的最早位置。这里锁不是藏在 `Fill()` 内：`ChannelBuffer` 和 `DataDispatcher` 先锁 `CacheBuffer::Mutex()`，再读写 ring 状态。

相应代价是模板实现通常必须放在头文件中，改动会触发大量重新编译；同一实现也可能在多个 translation unit 实例化，需要依靠链接器合并或显式实例化控制体积。

## 模板 singleton 的真实作用域

`DataDispatcher<T>::Instance()` 看起来像一个 singleton，但每个 `T` 都会形成不同静态实例：

```text
DataDispatcher<PointCloud>::Instance()
DataDispatcher<Chassis>::Instance()
DataDispatcher<Localization>::Instance()
```

这让 registry 天然按消息类型分区。即使两个不同消息错误地使用同一 channel name/id，它们仍不会进入同一 C++ vector。

“singleton”只表达每个模板实例在当前进程中的唯一访问点，不表示跨进程共享，也不表示动态库边界一定只有一份副本。构建系统、符号可见性和链接方式必须保证所有模块引用预期的同一个实例。

全局访问降低了参数传递和装配复杂度，却隐藏依赖、增加隔离难度。自行实现时可以把 Dispatcher registry 显式放进 RuntimeContext，由 Node/Reader 持有 context 引用；这样单元隔离和多 runtime 实例更容易，代价是接口更长。

固定提交的 `DataDispatcher<T>::Dispatch()` 直接展示了注册表与数据面如何接起来。下面是**固定提交源码摘录**，省略命名空间限定：

```cpp
bool DataDispatcher<T>::Dispatch(const uint64_t channel_id,
                                 const std::shared_ptr<T>& msg) {
  BufferVector* buffers = nullptr;
  if (apollo::cyber::IsShutdown()) {
    return false;
  }
  if (buffers_map_.Get(channel_id, &buffers)) {
    for (auto& buffer_wptr : *buffers) {
      if (auto buffer = buffer_wptr.lock()) {
        std::lock_guard<std::mutex> lock(buffer->Mutex());
        buffer->Fill(msg);
      }
    }
  } else {
    return false;
  }
  return notifier_->Notify(channel_id);
}
```

这段代码没有复制消息 payload：它锁住能成功提升的 weak buffer，写入 ring，完成后才按 channel 通知。registry 用 weak pointer，表示 Dispatcher 可发现缓存但不拥有缓存；缓存本身由 Reader/ChannelBuffer 一侧的强引用控制寿命。

## `RoutineFactory` 在类型化数据与无参调度之间做类型擦除

Scheduler 不可能为每种 `Component<M...>` 写一套 worker。它希望所有任务最终看起来都像“恢复一个无参执行体”。

`std::function<返回类型(参数...)>` 是一个可保存任意兼容函数对象的包装器：调用者只看到统一签名，lambda 本身复杂的匿名类型被藏在里面。类型化一侧先保留消息参数，下面是**概念签名**，并非 Apollo 源码摘录：

```cpp
std::function<void(const std::shared_ptr<M0>&)> f;
std::shared_ptr<DataVisitor<M0>> dv;
```

lambda（匿名函数表达式）可以把局部变量捕获到自身闭包中。`std::function<签名>` 是可保存不同函数对象、但以统一调用签名对外呈现的包装器；本例用它隐藏匿名闭包的具体类型。RoutineFactory 用 lambda 生成统一的无参循环；下面是**教学骨架，非固定源码摘录**：

```cpp
[f, dv]() {
  std::shared_ptr<M0> msg;
  for (;;) {
    // TryFetch(msg), f(msg), Yield(...)
  }
}
```

lambda 的闭包类型由编译器匿名生成，里面保存 `f` 与 `dv` 的副本。再把它转换为 `std::function<void()>` 或等价统一 callable 后，Scheduler 只处理无参任务，不需要知道 `M0`。

真实 `CreateRoutineFactory()` 不是抽象地“包一个 callback”，而是把协程状态、取数与回调连接起来。以下为该函数的**固定提交源码摘录**：

```cpp
factory.create_routine = [=]() {
  return [=]() {
    std::shared_ptr<M0> msg;
    for (;;) {
      CRoutine::GetCurrentRoutine()->set_state(RoutineState::DATA_WAIT);
      if (dv->TryFetch(msg)) {
        f(msg);
        CRoutine::Yield(RoutineState::READY);
      } else {
        CRoutine::Yield();
      }
    }
  };
};
```

这里的外层闭包负责创建 routine 执行体，内层闭包才是反复恢复的协程函数。每轮先公布 `DATA_WAIT`，然后尝试从 DataVisitor 取数；有数据才调用 `f(msg)`，执行完以 `READY` 让出；无数据则普通 `Yield()`，由阻塞/唤醒路径之后再决定何时恢复。消息到缓存、状态变为可调度、OS worker 被通知、协程恢复执行是不同步骤，不能压成“通知就执行”。

这就是类型擦除：外层只保留“可以像 `void()` 一样调用”的能力，具体闭包类型和消息类型藏在对象内部。与虚函数类似，它建立统一接口；不同之处是 `std::function` 可以包装 lambda、函数对象、绑定表达式等任意 callable，不要求它们继承共同基类。

类型擦除可能带来间接调用和小对象优化之外的堆分配。性能关键路径要确认闭包大小、`std::function` 实现和创建频率。Cyber 在 task 创建阶段构造这些对象，而不是每条消息重新创建，有助于把成本移出稳定热路径。

## lambda 捕获同时定义行为与所有权

下面是**固定提交 `Component<M0>::Initialize()` 的回调摘录（压缩空白）**，见源码。`dynamic_pointer_cast` 会在运行时检查派生关系，成功时返回共享同一控制块的派生类型指针，失败时返回空；`weak_ptr` 不延长对象寿命，`lock()` 则尝试取得一个临时强引用。这个 callback 不只是“简洁语法”：

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

`[self, role_attr]` 按值捕获，闭包拥有这两个副本。若写成 `[&self]`，Initialize 返回后引用会指向已销毁的局部变量，未来执行 callback 将产生悬空引用。

按值捕获 `shared_ptr<Component>` 又会让 callback 强持有组件，可能形成生命周期环。选择 `weak_ptr` 表达“执行时借用、不要续命”。

因此审查 lambda 时不能只问“调用了哪个函数”，还要逐个检查 capture list：按值还是按引用，是否复制大对象，是否延长所有权，闭包会存活多久，在哪个线程执行。

`role_attr` 按值捕获通常用于稳定保存统计和身份信息；如果它较大，每个 callback 都有一份副本。可以通过只捕获必要字段减小闭包，但会增加代码复杂度。这里是可读性与对象尺寸的局部取舍。

## `enable_shared_from_this` 的前置条件

`shared_from_this()` 并不是凭空为 `this` 创建新控制块。对象必须已经由某个 `shared_ptr` 管理，`enable_shared_from_this` 才能返回共享同一控制块的新指针。

ClassLoader 创建组件后立即放入 `shared_ptr<ComponentBase>`，随后才调用 `Initialize()`，因此 Component 内部能够安全取得自身共享身份。

下面是**错误示例，不是 Apollo 源码**：若错误地这样创建：

```cpp
MyComponent object;
object.Initialize(config); // 内部 shared_from_this()
```

对象没有共享控制块，调用会失败。这个 API 约束说明 Component 不只是普通可栈构造类，它的有效构造协议由 runtime factory 定义。

自行设计时，可以避免让对象在初始化函数中依赖 `shared_from_this()`：先由 builder 创建 `shared_ptr`，再把 weak self 显式注入 callback；或使用两阶段 `Create()` 静态函数隐藏不合法构造方式。

## `shared_ptr` 扇出不等于端到端零拷贝

Dispatcher 向多只 buffer 写入同一 `shared_ptr<T>`，这一步不复制 `T`。但消息在到达 Dispatcher 之前可能已经发生：

```text
RTPS sample -> std::string copy -> ParseFromString -> new T
SHM bytes   -> ParseFromArray  -> new T
INTRA       -> original shared_ptr<T>
```

所以智能指针只能说明当前对象引用怎样传播，不能证明更早的 transport 没有序列化或复制。

`shared_ptr` 控制块的引用计数通常是原子操作。一个消息被许多 CPU core 上的消费者同时复制和释放时，控制块 cache line 可能来回迁移。payload 很大时避免深拷贝收益明显；消息极小、频率极高时，引用计数成本可能占据更大比例。

若生命周期完全静态并且单 producer/consumer，也可以使用对象池句柄、intrusive refcount 或借用视图减少控制块成本；代价是内存回收协议更难证明。

## `unique_ptr`、`shared_ptr` 与 `weak_ptr` 的职责分配

`unique_ptr<T>` 是独占所有权句柄：不能复制，只能移动给新的唯一 owner；离开作用域时自动删除对象。`shared_ptr<T>` 用于多个 owner 共同延长寿命，`weak_ptr<T>` 则观察共享对象但不延长它的寿命。Cyber 对三类指针的选择可以按关系解释：

```text
unique_ptr<NodeChannelImpl>
  Node 独占实现对象，寿命严格随 Node

shared_ptr<ReaderBase>
  Node map 与 Component reader list 共享同一 Reader

weak_ptr<Component>
  callback 可访问 Component，但不阻止其销毁

weak_ptr<CacheBuffer>
  Dispatcher 可访问活缓存，但 registry 不拥有缓存
```

使用 `shared_ptr` 不应是“暂时不知道谁拥有”的默认逃避。每个强引用都应能说出为什么需要共享寿命；每个 weak pointer 都应能说出哪个对象才是实际 owner。

对象图审查可以从删除场景反推：移除 Component task 后，哪些强引用消失；清空 Component reader vector 后，Node map 是否仍持有 Reader；ReceiverManager 是否仍持有 receiver。只有强引用图闭合，析构顺序才可预测。

## 自定义 deleter 将内存释放与库寿命连接起来

class loader 返回的 `shared_ptr<ComponentBase>` 不只是调用普通 `delete`。自定义 deleter 可以在删除派生对象后更新 loader 的活动对象计数，使卸载逻辑知道库中是否仍有活对象。

```text
shared_ptr last release
  -> custom deleter
       -> delete concrete component
       -> decrement class object count
       -> library becomes unloadable only at zero
```

deleter 是 `shared_ptr` 类型擦除的一部分：`shared_ptr<Base>` 的静态类型不需要携带 deleter 模板参数，控制块内部保存实际删除策略。

这种能力很强，也意味着复制一个 shared pointer 会间接延长动态库不可卸载时间。隐藏在全局 callback 或 registry 中的强引用可能让 `UnloadLibrary()`一直失败，因此插件系统更需要清晰的强引用审计。

## RAII 将错误路径变成正常析构路径

RAII 是 Resource Acquisition Is Initialization（资源获取即初始化）：把资源获取和释放绑定到对象构造/析构，使提前 return 或异常离开作用域时也执行清理。`std::lock_guard` 在构造时锁住 mutex、析构时解锁；`unique_ptr` 表示独占对象所有权；`shared_ptr` 在最后一个强引用释放时销毁对象。`std::thread` 另有边界：仍处于 joinable 状态时析构会调用 `std::terminate`，所以 owner 必须请求停止并 `join()` 等待线程函数结束，或明确设计另一种有生命周期保障的退出方式。

RAII 的重点不是“用了智能类”，而是任何 return、异常或后续初始化失败都走相同释放规则。

Component 初始化可能在用户 `Init()`、Reader 创建或 task 创建处失败。若业务资源是成员 RAII 对象，局部 component shared pointer 析构就能回滚；若是裸线程和裸句柄，只依赖正常 `Clear()`，失败路径可能泄漏或留下后台工作。

一套工业组件接口应明确：构造函数只建立不会失败或易回滚的本地状态，`Init()` 完成可失败资源获取，析构函数始终能安全处理部分初始化状态，`Clear()` 用于有序停止而不是唯一释放手段。

## mutex 与 atomic 处理的是不同一致性问题

CacheBuffer 的 mutex（互斥锁）让同一时刻只有一个线程进入受保护区，适合同时更新 head、tail 和槽位内容这组相关状态。用单个 atomic（原子变量）只能保证单个变量的访问不可撕裂，不能自动保持多个字段一致；如果需要维护这种整体不变量，mutex 往往更容易证明正确。

CRoutine 的 `lock_` 和 `updated_` 只表达单比特并发协议：防止双 worker 同时执行，以及记录是否发生更新。它们适合 atomic flag，因为状态小、转换明确。

选择原则不是“atomic 比 mutex 高级”：

```text
compound invariant + nontrivial object lifetime -> mutex often clearer
single flag/counter with defined transitions    -> atomic may fit
```

无锁算法还需要精确 memory order 和对象回收协议。`memory_order`（内存序）是传给原子操作的约束，用来规定该操作与其他内存读写之间哪些先后关系必须对其他线程可见；它不会把多个原子变量的组合更新自动变成一个不可分割的事务。必须区分固定提交中的实际字段：`CRoutine::lock_` 和 `updated_` 是 `std::atomic_flag`，而 `state_` 是普通的 `RoutineState` 枚举，并**非 atomic**。通知线程读取 `state_` 与运行线程修改 `state_` 时，在可见源码里没有统一同步协议，存在 C++ data race 风险；不能因为旁边有两个原子标志就宣称整个状态机线程安全。详见[事件与任务状态](croutine-wakeup.md)及[Processor 执行路径](processor-context-switch.md)。缓存按各自 mutex 保护复合不变量、原子标志仅维护单独事件或执行互斥，这才是从这份源码能够直接观察到的设计。

## 编译期安全、运行时弹性与可调试性的三角取舍

模板消息链提供强类型与较低运行时分派成本；class loader 提供运行时组合；统一基类与类型擦除让 scheduler 能管理异构任务。

三者组合也增加调试层次。一个 callback 不执行，原因可能位于：

```text
factory string / dynamic library
virtual Initialize failure
template-specific Reader creation
topology matching
typed Dispatcher registry
type-erased routine callback
weak_ptr lifetime
scheduler state
```

好的诊断系统应在每个边界保留稳定身份：class name、component name、channel id、task id 和 thread/group。没有这些关联，仅靠 C++ 类型安全无法解释运行时缺消息。

## 面向新中间件的 C++ 结构建议

可以保留稳定非模板基类作为插件生命周期边界，让实际数据路径继续使用模板；不要让动态类型擦除过早侵入 serialization、queue 和 callback 热路径。

用 builder 或 factory 强制合法构造顺序，避免对象在尚未由 shared pointer 管理时调用 `shared_from_this()`。callback capture list 应视为所有权声明，代码审查逐项检查。

registry 返回 RAII registration token，token 析构自动注销；比只存 weak pointer 更完整。长寿命 RuntimeContext 显式拥有 Dispatcher、Notifier 和 Scheduler，可减少无法隔离的全局 singleton。

为 callable type erasure 评估分配成本。task 创建阶段使用 `std::function` 通常可接受，逐消息临时创建大闭包则应避免。对硬实时路径可以使用固定容量 function wrapper 或静态 task table。

使用 mutex 时写出保护的不变量和锁顺序，使用 atomic 时写出状态机与 memory-order 理由。不要让“线程安全容器”成为对内部 value 并发安全的错误承诺。

## 最小复刻的分层代码骨架

下面是**教学最小骨架，不是 Apollo 源码**；它把插件生命周期、消息类型和无参任务三个接口摆在一起：

```cpp
class ComponentBase {
 public:
  virtual ~ComponentBase() = default;
  virtual bool Initialize(const Config&) = 0;
  virtual void Shutdown() = 0;
};

template <typename Message>
class Component : public ComponentBase,
                  public std::enable_shared_from_this<Component<Message>> {
 protected:
  virtual bool Proc(const std::shared_ptr<Message>&) = 0;
};

using Factory = std::function<std::shared_ptr<ComponentBase>()>;
using TaskBody = std::function<void()>;
```

Factory registry 只负责从字符串得到 `shared_ptr<ComponentBase>`；`Component<Message>` 在 Initialize 中创建类型化 Reader 与 DataVisitor；RoutineFactory 把类型化 `Proc(msg)` 包装为统一 `TaskBody`；Scheduler 只看 TaskBody 和 task id。

第一版甚至不需要动态库。先在同一二进制内注册两个组件，验证类型边界、弱引用和 shutdown；再加入 `.so` 与自定义 deleter。这样每次只增加一个生命周期维度。

最终应保持四条规则：

```text
plugin layer knows ComponentBase, not message details
data layer knows Message, not concrete component subclass
scheduler knows TaskBody, not Message
registries observe business objects, but do not secretly own them
```

Cyber RT 的 C++ 设计价值就在这些边界中。模板不是为了炫技，多态不是为了画继承树，智能指针也不是自动内存管理的同义词；它们共同把“可扩展的动态部署”和“高频的静态类型数据路径”放进同一个运行时，同时把各自的成本留在清晰可分析的位置。
