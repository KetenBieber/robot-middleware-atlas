# 项目案例：Drake 如何把 LCM 接入系统仿真图

Drake 是模型化机器人设计与仿真框架，长期使用 LCM 连接仿真、硬件、可视化和控制程序。它没有让业务系统直接到处调用 `lcm.publish()`，而是把 LCM 封装成 Drake System，使通信成为可组合 Diagram 的一部分。

可以先想象一个机械手仿真：外部控制程序通过 LCM 发送 16 个关节目标，Drake 中的控制器读取目标、驱动机械手模型，再把关节位置和接触状态发回监控程序。最直接的写法是在控制器里调用 `lcm.handle()` 和 `lcm.publish()`，但这样会产生三个问题：网络消息可能在仿真计算中途改变输入；发布频率会被积分步长意外决定；测试只能依赖真实网络和墙上时钟。

Drake 的解决办法是把接收器、转换器、控制器、物理模型和发布器都变成系统图中的节点：

```text
LCM 网络 → Subscriber System → 命令转换 System → 控制器 → 机械手模型
                                                       ↓
LCM 网络 ← Publisher System ← 状态编码 System ←────────┘
```

网络仍然异步，但新消息只有经过 Drake 的事件调度并写入 `Context` 后，才成为仿真可见状态。理解这一点，才能看懂为什么源码不让 callback 直接修改控制器变量。

真实入口包括 `systems/lcm` 下的 `LcmInterfaceSystem`、`LcmPublisherSystem`、`LcmSubscriberSystem`，以及 Allegro Hand 仿真。本文源码固定到 Drake commit `b07bab525ccb6fe477fb6ca1bad51185792802f6`（`v1.56.0`）；以下代码块会单独标出哪些是上游摘录、哪些是教学骨架。

核心源码入口可以按“抽象接口 → 调度桥 → 收发端”阅读：

| 层次 | 固定版本源码 | 阅读重点 |
|---|---|---|
| transport 抽象 | `lcm/drake_lcm_interface.h` | 发布、订阅与同步调用 `HandleSubscriptions` 的契约 |
| 仿真调度桥 | `systems/lcm/lcm_interface_system.cc` | update-event 如何非阻塞地 pump 订阅 |
| 订阅状态机 | `systems/lcm/lcm_subscriber_system.cc` | 收到消息、计数、互斥保护、超时等待与 Context 提交 |
| 发布调度 | `systems/lcm/lcm_publisher_system.cc` | trigger、period、offset 与 serializer |
| 真实组合 | `allegro_single_object_simulation.cc` | Builder 所有权、端口连线与运行入口 |

先读接口能知道谁允许调用谁；再读调度桥能知道网络事件何时进入仿真；最后读 Allegro 组合，才不会把一串 `AddSystem` 误解成普通对象初始化代码。

## 功能目标

- 在仿真时间而不是任意线程时刻发布状态；
- 把外部 LCM command 转成 Diagram 输入；
- 让真实硬件与仿真使用相同 channel/type；
- 在系统图中明确采样周期和状态更新事件；
- 支持替换真实 LCM 与测试/mock 接口。

几个 Drake 名词可以先翻译成普通程序设计概念：

| Drake 名词 | 可以先怎样理解 |
|---|---|
| `System` | 一个有输入端口、输出端口、状态和事件的计算模块 |
| `LeafSystem` | 不再包含子系统的基本节点 |
| `Diagram` | 多个 System 连接而成的有向计算图 |
| `DiagramBuilder` | 在构建阶段拥有节点并检查端口连接的装配器 |
| `Context` | 某个 System 在特定仿真时刻的状态、参数与输入视图 |
| update event | 在明确时刻提交状态变化的调度事件 |

LCM 只负责把字节从一个进程送到另一个进程；Drake 则负责决定这些字节何时进入模型状态，以及何时从模型读取状态发出去。两者职责不同，所以需要 `Lcm*System` 作为边界。

## 从真实 Allegro Hand 入口开始

`allegro_single_object_simulation.cc` 没有在控制器内部构造 LCM，而是先把通信加入 Diagram。下面这段为**教学重排片段**：变量名和相邻行经过改写，用来突出对象关系；不是上游的连续源码摘录。

```cpp
systems::DiagramBuilder<double> builder;
auto lcm = builder.AddSystem<systems::lcm::LcmInterfaceSystem>();

auto& command_sub = *builder.AddSystem(
    systems::lcm::LcmSubscriberSystem::Make<lcmt_allegro_command>(
        "ALLEGRO_COMMAND", lcm));

auto& command_receiver =
    *builder.AddSystem<AllegroCommandReceiver>(kAllegroNumJoints,
                                               kLcmPeriod);

auto& status_pub = *builder.AddSystem(
    systems::lcm::LcmPublisherSystem::Make<lcmt_allegro_status>(
        "ALLEGRO_STATUS", lcm, kLcmPeriod));
```

`Make<Message>` 把 channel 与生成的 LCM 类型在构建期绑定。`builder.AddSystem` 把对象所有权交给 DiagramBuilder，返回的引用只用于继续配置和连线；不能在 Diagram 销毁后保存该引用。

逐句看这段 C++ 会更容易理解所有权：

1. `DiagramBuilder<double>` 中的 `double` 是标量类型，表示该系统图用双精度数值求值；Drake 也有支持自动微分等其他标量的 System；
2. `AddSystem<LcmInterfaceSystem>()` 是函数模板调用，Builder 在内部创建具体系统并取得所有权；
3. 第一行使用 `auto lcm`，其实际类型是指向 Builder 所有对象的非拥有指针；代码只借用它给其他系统配置依赖；
4. Subscriber 行先返回指针，再用 `*` 解引用并绑定到 `auto&`。引用不能为 null，但它仍不拥有对象，真正 owner 仍是 Builder；
5. `Make<lcmt_allegro_command>` 把生成消息类型写进模板参数，因此 Subscriber 的输出端口在构建期就知道消息类型；
6. `kLcmPeriod` 是发布事件的仿真时间周期，不是让当前 C++ 线程 `sleep` 的墙上时间。

指针、引用与 owner 在这里必须分清：Builder 决定对象何时销毁；局部指针和引用只帮助连线。若再把同一裸指针塞进 `unique_ptr`，就会出现两个对象都认为自己应负责释放资源的重复销毁错误。

通信系统与业务适配器分开：Subscriber 输出 `lcmt_allegro_command`，`AllegroCommandReceiver` 再把它转换成控制图需要的关节向量。反向由 `AllegroStatusSender` 生成 LCM 消息，Publisher 只负责按周期编码发送。

## 组合结构

```text
LCM command channel
  -> LcmSubscriberSystem<lcmt_allegro_command>
  -> command receiver / desired positions
  -> PID controller
  -> MultibodyPlant
  -> status encoder
  -> LcmPublisherSystem<lcmt_allegro_status>
  -> LCM status channel
```

通信适配器变成 Diagram node，端口连接由 `DiagramBuilder` 在构建期完成。算法模块只处理 vector/value ports，不直接依赖 LCM handle。

真实连接代码的形状是：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
builder.Connect(command_sub.get_output_port(),
                command_receiver.get_input_port(0));
builder.Connect(status_sender.get_output_port(0),
                status_pub.get_input_port());
```

端口连接在 `Build()` 前检查数据类型和拓扑。这样错误 channel 仍可能在运行期出现，但“LCM message 接到了错误的向量端口”会更早暴露，而不是在 callback 中靠强制转换维持。

## 朴素 callback 写法为何破坏仿真

在进入 `LcmInterfaceSystem` 前，先看一种很容易写出的实现。下面是**错误示例**，它不来自 Drake：

```cpp
class HandController {
 public:
  void OnLcmCommand(const lcmt_allegro_command& msg) {
    // 网络线程随时执行这里。
    desired_position_ = msg.joint_position;
  }

  void CalcTorque() {
    // 仿真线程同时读取同一个 vector。
    torque_ = Kp_ * (desired_position_ - measured_position_);
  }

 private:
  Eigen::VectorXd desired_position_;
  Eigen::VectorXd measured_position_;
  Eigen::VectorXd torque_;
  double Kp_{};
};
```

问题不只是“两个函数可能同时运行”。`Eigen::VectorXd` 内部持有指向堆内存的指针和长度；赋值时可能重新分配、复制元素并更新元数据。如果网络线程正在改写，而仿真线程同时读取，C++ 将其定义为 data race，程序行为未定义。读者看到的结果可能是旧值、新值、部分更新，甚至访问已经释放的数组。

### 操作系统实际调度的是内核线程

在这个朴素示例里，我们假设网络接收线程直接调用 `OnLcmCommand`。线程是进程内独立调度的执行流：每个线程有自己的寄存器状态和栈，却共享进程的堆与全局对象。CPU 可以在任意可抢占点暂停仿真线程，保存寄存器和程序计数器，再恢复接收线程；这就是一次上下文切换。应用代码不能假定 `desired_position_ = ...` 很短，所以不会被切走。

给两个函数加同一把 `std::mutex` 能消除未定义行为，却仍没有解决仿真语义：同一次微分求解可能第一次读取旧命令，稍后重新求值输出时又读到新命令。相同仿真时刻得到不同输入，数值积分器和回放结果就不再确定。

真正需要的是“接收”和“生效”两个阶段。下面是**教学最小例子**：

```cpp
struct Snapshot {
  Eigen::VectorXd value;
  std::uint64_t sequence{};
};

class CommandMailbox {
 public:
  // 网络线程调用：只更新尚未提交的候选值。
  void Receive(Eigen::VectorXd value, std::uint64_t sequence) {
    std::lock_guard<std::mutex> lock(mutex_);
    pending_ = Snapshot{std::move(value), sequence};
  }

  // 仿真线程只在调度器认可的 update event 中调用。
  std::optional<Snapshot> TakePending() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!pending_) return std::nullopt;
    auto result = std::move(pending_);
    pending_.reset();
    return result;
  }

 private:
  std::mutex mutex_;
  std::optional<Snapshot> pending_;
};
```

`std::lock_guard` 是栈对象：构造时调用 `mutex_.lock()`，离开花括号时析构并自动 `unlock()`。没有竞争时，常见 mutex 实现可以主要在用户态用原子操作取得锁；发生竞争后，线程可能通过类似 Linux futex 的机制进入睡眠，解锁者再把等待线程唤醒。被唤醒只表示线程重新变成 runnable，何时真正获得 CPU 仍由操作系统调度器决定。

`std::optional<Snapshot>` 明确表示“可能没有新命令”，不需要用空向量或特殊序号充当哨兵。`std::move(value)` 把 `value` 转成右值表达式，使 `Eigen::VectorXd` 可以转移内部缓冲所有权；`std::move` 本身不搬数据，真正是否转移由 `Snapshot` 构造和 `VectorXd` 的移动操作决定。

锁只保护 `pending_` 这一小段交接。仿真线程取走快照后立即释放锁，再把值提交到自己的 `Context`；耗时控制计算不在锁内。这个教学例子假设应用用独立线程收包，因此要用 mutex 交接；Drake 的标准 `LcmInterfaceSystem` 路径则在仿真事件里同步调用 `HandleSubscriptions()`，其 Subscriber callback 也沿着该调用栈执行。两种路径都需要把字节先放入接收槽、再在确定的 update 边界提交给 Context，但线程边界不同，不能把这段示例当作 Drake 的逐字实现。

## `LcmInterfaceSystem` 的作用

它把 LCM receive handling 纳入 Drake 执行环境，并为 Publisher/Subscriber systems 提供共同接口。通过抽象 `DrakeLcmInterface`，测试可注入内存实现，避免依赖组播网络。

这是 Dependency Inversion：控制器依赖抽象数据端口，LCM 位于系统边缘；仿真调度器决定何时更新，而不是网络 callback 直接重入动力学求解。

这里还有一层执行上下文隔离。LCM 的 UDPM 后台线程负责从 socket 收包并把完整消息放入 provider 队列；它不直接调用 Drake 订阅 callback。`HandleSubscriptions()` 是同步调用：谁调用它，pending callback 就在谁的调用栈中执行。下文这个版本的 `LcmInterfaceSystem` 在仿真 update-event 路径中以零超时 pump，所以标准 Diagram 中 `LcmSubscriberSystem::HandleMessage` 也由执行该事件的 Simulator 线程调用。应用若另起线程并发调用同一 interface 的 `HandleSubscriptions()`，则必须把这条额外执行路径纳入同步设计。Drake 的 Context 具有明确的所有者和求值阶段；异步 transport 收包不应任意修改正在积分或求导的 Context。

官方类说明给出了更精确的事实：`LcmInterfaceSystem` 本身没有输入、输出、状态或参数，只声明一个 update event，在 LCM 有等待消息时 pump subscriptions。[LcmInterfaceSystem API](https://drake.mit.edu/doxygen_cxx/classdrake_1_1systems_1_1lcm_1_1_lcm_interface_system.html)

```text
Simulator 计算下一事件
  -> LcmInterfaceSystem update event
  -> DrakeLcmInterface::HandleSubscriptions(timeout=0)
  -> LCM handler 把新 bytes 交给各 SubscriberSystem 的内部接收槽
  -> SubscriberSystem 安排自身 unrestricted update
  -> Context state 提交新消息
```

这比“InterfaceSystem 有个接收线程”更准确。`LcmInterfaceSystem` 不创建网络接收线程；UDPM provider 的收包线程与 callback 执行线程是两件事。InterfaceSystem 把同步 pump 放进仿真事件序列，使注册在同一 interface 上的 Subscriber 在该次调用中被服务。若绕过 Simulator 手工求值系统，就必须自己复现 pump 与 update 顺序，否则输出会一直停留在旧 Context 状态。

`LcmInterfaceSystem` 不允许复制或移动，符合 Diagram node 的身份语义。一个 node 在构建后有稳定地址，并被多个 Publisher/Subscriber 以非拥有指针引用。构造函数若接收外部 `DrakeLcmInterface*`，调用者必须保证该对象比 InterfaceSystem 活得更久；这是 C++ 裸指针表达的借用契约，而不是所有权转移。

## Subscriber 跨越异步网络与仿真时间

LCM 消息异步到达，但 Drake Context 在仿真步骤中需要一致状态。`LcmSubscriberSystem::HandleMessage()` 把最新的序列化 bytes 复制进内部接收槽并递增内部计数；随后 `ProcessMessageAndStoreToAbstractState()` 才调用 serializer 解码，再把对象写入 Context state。标准 InterfaceSystem 会在仿真事件中同步 pump handler；若应用另起线程调用 HandleSubscriptions，内部 mutex 才承担跨线程保护。事件安排方式决定外部命令何时成为仿真可见状态。

优点是仿真状态转换可调度；代价是频繁 unrestricted update 可能显著降低仿真速度。性能问题应从“事件种类和频率”分析，而不只是 LCM callback 本身。

若控制命令只需固定采样，离散状态更新比每条消息触发 unrestricted update 更容易分析：

```text
LCM callback -> latest-message slot
每 kLcmPeriod：复制 latest -> Context discrete state
控制器在仿真时刻读取一致快照
```

这会增加至多一个采样周期的输入延迟，并跳过周期内的中间命令。对设定值通常合理，对必须逐条处理的事件则应使用有界队列、序列号和事件消费系统。

### `LcmSubscriberSystem` 的两份状态

官方实现区分内部最新消息和 Context 中已提交消息。`HandleMessage()` 在 InterfaceSystem pump 期间收到待处理消息时先推进内部 message count；`DoCalcNextUpdateTime()` 发现计数变化后安排当前仿真时刻的事件，unrestricted update 再把解码值和计数写入 Context。`HandleMessage()`、`DoCalcNextUpdateTime()` 与 `ProcessMessageAndStoreToAbstractState()`

```text
Subscriber internal slot                 simulation Context
latest bytes + internal_count=N
            | CalcNextUpdateTime
            v
      unrestricted update event
            | deserialize / copy
            v
abstract state message + context_count=N
```

输出端口只依赖 Context state，而不直接读取 Subscriber 的内部接收槽。这样同一个仿真时刻内反复求值输出会得到一致对象；新网络数据先进入 provider 队列，之后经 pump、handler 和 scheduled update 才能改变该 Context。这是 Drake 把外部非确定输入转换成可调度状态的关键。

`GetInternalMessageCount()` 与 `GetMessageCount(context)` 表达两种事实：前者表示网络侧累计收到多少，后者表示当前 Context 已经采纳多少。二者之差说明存在尚未提交的新消息，但不能直接解释为完整 FIFO 深度，因为 SubscriberSystem 关注的是最近处理消息。

初始化参数 `wait_for_message_on_initialization_timeout` 也有明确取舍：小于等于零时只复制已经到达的消息，不在初始化期间等待；大于零时会 pump LCM 直到收到至少一条或墙上时钟超时。把它设为无限等待会让缺少硬件发布者的仿真永远无法初始化，因此生产程序应给有限 deadline 和安全默认值。

### 初始值与陈旧命令

默认构造的 LCM message 可能全零，但“全零”未必是合法安全命令。Receiver System 应同时输出或内部保存有效位、message count 和源时间戳：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
struct CommandSnapshot {
  Eigen::VectorXd value;
  std::int64_t sequence{};
  double source_time{};
  bool valid{};
};
```

控制器在 `valid == false` 或数据年龄超过 timeout 时进入安全状态，而不是把 value-initialized message 当成真实零目标。仿真时间与消息中的设备时间属于不同时钟域；需要先定义偏移/同步策略，不能直接相减。

## Publisher 的采样策略

`LcmPublisherSystem` 可按固定 publish period 读取输入并编码。Allegro 示例给发布器传入 `kLcmPeriod`，使状态输出频率成为模型参数。若每个内部积分步都发布，网络和编码会把仿真速度绑死。

发布周期与 MultibodyPlant 的离散更新周期不是同一个概念。Plant 可以以更细步长求解动力学，LCM 只按较粗周期输出可观测状态。把二者强制设为相同会让外部监控频率反向决定物理求解精度。

`LcmPublisherSystem` 支持初始化一次发布、per-step、periodic 和 forced publish。周期构造参数还包含 offset，用于把多个 channel 的发送相位错开。[LcmPublisherSystem API](https://drake.mit.edu/doxygen_cxx/classdrake_1_1systems_1_1lcm_1_1_lcm_publisher_system.html)

| Trigger | 适合用途 | 风险 |
|---|---|---|
| initialization | 发布静态模型/配置 | 重启消费者可能错过一次性消息 |
| periodic | 状态与传感器流 | 最多一个周期采样等待 |
| per-step | 调试每一步内部状态 | 自适应积分时流量不可预测 |
| forced | 测试或显式快照 | 调用方负责时机与线程 |

Publisher 在事件触发时求值输入端口、用 serializer 编码，再调用共享 `DrakeLcmInterface::Publish`。输入值若计算昂贵，发布事件会把这部分求值成本计入仿真推进；网络发送若阻塞，也会直接影响墙上运行速度。因此“LCM 发布周期”同时是通信参数和仿真调度参数。

## 类型生成与边界

Drake 的 `lcmt_*` 类型由 LCM schema 生成。系统内部使用强类型 C++ 对象，在 LCM 边缘编码；channel 名和 message type 在 Publisher/Subscriber System 构造时绑定。

schema fingerprint 能发现不兼容，但滚动升级仍需要新类型或桥接。记录日志时要同时保存 Drake 版本和 LCM schema commit。

消息适配器还负责单位、关节顺序和时间戳。`lcmt_allegro_command` 到内部向量的转换不能仅复制数组：应验证关节数量、拒绝 NaN/Inf、决定缺失关节的默认值，并把消息时间与仿真时间的关系写清楚。网络时间戳不能自动成为仿真事件时间。

### Serializer 是变化隔离层

非模板构造函数接收 `shared_ptr<const SerializerInterface>`；模板 `Make<LcmMessage>` 只是为具体生成类型创建 serializer 的便利工厂。这让 `LcmSubscriberSystem` 的状态机不需要为每种 `lcmt_*` 类型重新实现。

```text
bytes <-> SerializerInterface <-> AbstractValue(LcmMessage)
                                    |
                                Drake port/state
```

`shared_ptr<const ...>` 表示多个 System 可以共享不可变 serializer，引用计数管理寿命，`const` 限制并发调用时修改配置。它不自动证明 serializer 内部线程安全；真正实现仍应是无状态或自行同步。

如果自定义消息适配器在 decode 中分配超大数组，恶意或损坏长度仍可能耗尽内存。生成代码的 fingerprint 检查之后，还要做业务最大长度、数值范围和单位验证。

## 多总线配置与依赖注入

大型机器人可能同时有控制 LCM、传感器 LCM 和测试内存总线。Drake 的 `LcmBuses` 用名字映射到 interface，`ApplyLcmBusConfig` 可按配置为每条总线向 DiagramBuilder 加入 InterfaceSystem；返回对象只保存指向 builder 所有系统的别名。[Drake LCM namespace](https://drake.mit.edu/doxygen_cxx/namespacedrake_1_1systems_1_1lcm.html)

```text
"control" -> LcmInterfaceSystem(udpm://...)
"sensors" -> LcmInterfaceSystem(udpm://...)
"test"    -> memory/null interface
```

业务 driver 按 bus name 查找接口，而不是自行构造 URL。这样部署配置决定 transport，系统拓扑仍能在构建期集中检查。测试可强制注入内存实现；禁用 LCM 时也能使用 null/memq-null 配置，不需要在每个组件散落 `if (use_lcm)`。

## 日志回放也进入仿真时钟

Drake 还提供 `DrakeLcmLog` 与 `LcmLogPlaybackSystem`。后者根据 Context 中的仿真时间推进日志游标，而不是让一个独立 wall-clock player 随机向仿真灌数据。官方 message-passing 命名空间把它与 Publisher/Subscriber 放在同一组抽象中。[Drake message passing](https://drake.mit.edu/doxygen_cxx/group__message__passing.html)

```text
log timestamp
  -> playback system 计算下一个日志事件的 simulation time
  -> Simulator 截断积分步到该时间
  -> log interface 交付消息
  -> Subscriber unrestricted update
```

这样同一日志在尽快仿真、慢速调试或批量回归中仍保持相同的仿真时间顺序。日志只保存曾被记录的 wire payload；Drake 二进制、schema、Diagram 配置和随机种子仍要另行归档。

## 数据结构与性能边界

最新消息槽的空间为 `O(message_size)`，读取成本主要来自 `O(message_size)` 的解码或复制；队列方案的空间则是 `O(capacity × message_size)`。Publisher 的编码成本随字段和数组长度近似线性增长。

多个 LCM System 共享 `LcmInterfaceSystem`，避免每个 Diagram node 建立独立底层实例。代价是接收 pump 和 channel dispatch 共享资源；高流量 channel 是否拖慢其他订阅，需要用消息计数、事件执行时间和墙上运行速度实际测量。

端到端延迟可以拆成：

```text
L = network arrival
  + wait until interface pump
  + wait until Subscriber update event
  + adapter validation/conversion
  + controller/plant computation
  + wait until Publisher trigger
  + encode + provider send
```

仿真时间延迟和墙上时间延迟必须分开报告。Simulator 可以比实时更快或更慢；消息在 2 ms 仿真时间内传播，不代表墙上只经过 2 ms。只有与真实硬件闭环时，pacing 和 wall clock 才成为控制契约的一部分。

## 优秀设计与工程取舍

Drake 没有让网络 callback 直接修改动力学状态，而是把外部消息提交为 Simulator 能够排序的事件。这增加了 Interface System、Subscriber state 和适配器，却换来同一仿真时刻内的一致输出、可重复推进和明确的采样边界。

最新消息缓存适合关节目标和状态设定值，因为控制器通常更关心新鲜值；代价是周期内的中间消息会被跳过。若每条事件都重要，就需要有界队列、序列号和消费确认，而不能继续把 latest-value slot 当作通用输入模型。

模板化 Publisher/Subscriber 提供生成类型和强类型端口，但 schema 演进需要重新生成并重新构建。对稳定机器人接口这是合理取舍；对动态消息浏览器，则需要额外的反射或原始字节工具。

## 缺点与不适用条件

把通信建模为 Drake System 并不会消除底层 LCM 的限制。默认 UDPM 仍不能保证可靠交付、安全认证或拥塞控制；Interface pump 也不会自动给业务消息建立端到端 deadline。

这种集成还引入了特有代价：频繁 unrestricted update 会降低仿真速度；阻塞的 Publish 会拖慢 Simulator 的墙上推进；多个 channel 共享 interface 时可能互相影响；错误的初始化等待策略会让缺失发布者的程序长期停住。

如果应用只需要一个独立进程把数据转发到文件，完整 Diagram 适配可能过重。如果需求是每条命令必达、严格确认或跨公网安全通信，单靠 Drake 的 System 封装也不够，仍需改变 transport 或在其上增加协议。

## 可迁移的架构方法

在自己的机器人框架中，可以把中间件限制在 ingress/egress adapter：接收器把异步消息转成时间戳快照或有界事件队列，核心计算图只消费类型化端口，发布器按系统时钟采样。

这条方法的重点不是“所有东西都做成 Drake System”，而是建立一个明确提交点：网络侧可以异步变化，控制状态只在调度器认可的时刻更新。这样测试可以分别验证 transport 接收、状态提交和输出发布，中间件故障也不会立刻与动力学模型错误混在一起。

## 最小复刻：增加自己的 LCM 输入输出

开发一个新设备适配通常分四层：

1. 定义 `.lcm` schema 并生成 C++ 类型；
2. 编写 Receiver/Encoder System，在 LCM 类型与内部端口类型之间转换；
3. 在 DiagramBuilder 中创建 Subscriber/Publisher 并连接端口；
4. 为 channel、发布周期和超时策略提供部署配置。

Receiver 不应顺手执行控制算法。保持转换层纯粹以后，可以直接向 Receiver 输入构造的 LCM message 做单元测试，也可以绕过 LCM 给控制器注入向量。

### 从消息端口写出最小 Receiver

假设生成消息 `lcmt_device_command` 含有关节数组和源时间戳，Receiver 的职责只是验证并转换成框架内部向量。下面是结构等价的教学骨架，不是 Drake 仓库的逐字源码：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
class DeviceCommandReceiver final : public drake::systems::LeafSystem<double> {
 public:
  explicit DeviceCommandReceiver(int joint_count)
      : joint_count_(joint_count) {
    // 端口携带的不是裸 bytes，而是一个类型化 abstract value。
    input_ = &this->DeclareAbstractInputPort(
        "command", drake::Value<lcmt_device_command>{});

    // 输出是控制图熟悉的定长向量，不再泄漏 LCM 类型。
    output_ = &this->DeclareVectorOutputPort(
        "desired_position", joint_count_,
        &DeviceCommandReceiver::CalcDesiredPosition);
  }

 private:
  void CalcDesiredPosition(
      const drake::systems::Context<double>& context,
      drake::systems::BasicVector<double>* output) const {
    const auto& message = input_->Eval<lcmt_device_command>(context);
    if (message.joint_position.size() !=
        static_cast<std::size_t>(joint_count_)) {
      throw std::runtime_error("joint count does not match model");
    }
    Eigen::VectorXd value(joint_count_);
    for (int i = 0; i < joint_count_; ++i) {
      value[i] = message.joint_position[i];
      if (!std::isfinite(value[i])) {
        throw std::runtime_error("command contains NaN or Inf");
      }
    }
    output->SetFromVector(value);
  }

  int joint_count_{};  // 构造后不变的系统配置
  const drake::systems::InputPort<double>* input_{};   // 非拥有别名
  const drake::systems::OutputPort<double>* output_{}; // 非拥有别名
};
```

`DeclareAbstractInputPort` 用 `Value<lcmt_device_command>` 建立运行时类型标签；连接时 Drake 可以检查端口类别，求值时 `Eval<T>` 再检查具体类型。它比 `void*` 安全，但消息字段是否合理仍由适配器验证。`DeclareVectorOutputPort` 注册的是一个延迟求值函数：只有下游需要该端口时，框架才调用 `CalcDesiredPosition`。因此计算函数必须是逻辑上的纯函数，不能在一次求值中悄悄递增计数或消费消息，否则缓存重算会改变系统行为。

两个端口指针只是 `LeafSystem` 内部端口对象的非拥有别名。端口由基类管理，Receiver 不应 `delete`，也不应把指针暴露给寿命超过 System 的对象。`joint_count_` 则是模型拓扑的一部分；若运行时命令可以改变自由度，就不能继续使用固定尺寸向量端口，而要重新设计类型或重建 Diagram。

### 把 Receiver 中的 C++ 语法逐项拆开

这段代码短，却集中出现了继承、初始化列表、成员函数指针、`const` 引用和裸指针。逐项看清后，再读 Drake 其他 `LeafSystem` 会容易很多。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
class DeviceCommandReceiver final
    : public drake::systems::LeafSystem<double> {
```

冒号后的 `public` 继承表示 `DeviceCommandReceiver` 是一种 `LeafSystem<double>`，可以通过基类接口放进 Diagram。`final` 禁止继续派生；这里系统的端口和计算规则已经在构造函数中固定，避免子类只改一部分行为而破坏不变量。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
explicit DeviceCommandReceiver(int joint_count)
    : joint_count_(joint_count) { ... }
```

`explicit` 阻止编译器把整数偷偷当成 Receiver，例如 `DeviceCommandReceiver r = 16;` 会被拒绝。冒号后的初始化列表直接构造 `joint_count_`；成员真正的初始化顺序由它们在类中的声明顺序决定，不由列表书写顺序决定。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
&DeviceCommandReceiver::CalcDesiredPosition
```

这不是立即调用函数，而是取得成员函数指针。Drake 保存这个“以后怎样计算输出”的规则；真正求值时，框架同时提供具体对象、`Context` 和输出缓存。它与普通函数指针的区别是，调用成员函数还需要一个 `this` 对象。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
const auto& message = input_->Eval<lcmt_device_command>(context);
```

`auto` 让编译器推导消息类型，`&` 表示不复制对象，`const` 表示当前函数不能借此修改消息。这个引用只在被求值对象仍有效时可用，不能保存到 Receiver 成员供以后使用。尖括号中的类型让 `Eval` 检查 abstract port 里实际保存的是不是 `lcmt_device_command`。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
void CalcDesiredPosition(...) const
```

末尾的 `const` 约束 `this`：计算输出时不能修改普通成员。它帮助维持“同一个 Context 得到同一个输出”的纯计算语义，但不自动保证线程安全；若成员指向外部可变对象，仍可能被其他线程修改。

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
static_cast<std::size_t>(joint_count_)
```

数组长度通常是无符号 `size_t`，模型自由度使用有符号 `int`。显式转换让比较意图可见，也提醒构造函数必须先拒绝负数；否则负数转换成无符号数会变成一个很大的值。

教学骨架中的 `Eigen::VectorXd value(joint_count_)` 每次求值都可能从堆申请缓冲，适合说明转换流程，却不适合直接声称“实时安全”。更稳妥的版本让 Drake 直接提供已经分配好的 `output`，逐项写入其中，或者使用编译期固定维度：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
for (int i = 0; i < joint_count_; ++i) {
  const double x = message.joint_position[i];
  if (!std::isfinite(x)) throw std::runtime_error("invalid command");
  output->SetAtIndex(i, x);  // 不再构造临时 VectorXd
}
```

即便去掉这次临时分配，抛异常、消息中的动态 `std::vector`、LCM 解码和错误日志仍不是硬实时操作。正确边界仍是：在非实时接收/适配路径完成验证，再把固定容量、已提交的数值状态交给高优先级控制计算。

### 将有效性和超时建模为状态，而非临时判断

上面的组合输出适合无状态演示，但工业控制还需要“最后一个有效命令”和“何时失效”。这时 Receiver 应声明离散状态，并用周期事件提交快照：

```text
abstract input: lcmt_device_command
periodic unrestricted/discrete update:
  validate -> write desired_position, source_time, sequence, valid
vector output:
  read only committed state
timeout output/status:
  compare simulation time with accepted_at_sim_time
```

把验证放在 update 中意味着错误命令不会在任意一次端口求值时突然抛出；状态更新点成为唯一提交边界。需要区分至少三个时间：消息携带的设备源时间、LCM 被本进程接收的墙上时间、Context 接纳消息的仿真时间。控制超时若目的是“仿真多久没有采纳新命令”，应使用最后一项；硬件健康监控则通常还要检查前两项。

状态中保存 `sequence` 而不仅是向量，可以检测重复和倒序。若协议没有 sequence，可使用 Subscriber 的 message count 判断本进程是否看到了新消息，但它不能代替跨进程序号：发布者重启、日志回放和转发桥都会改变二者含义。

### Encoder 与 Publisher 保持两段式

输出方向也应分成纯转换和副作用：

```text
plant/controller typed ports
  -> DeviceStatusEncoder::CalcOutput
       组装 lcmt_device_status，设置单位、顺序、时间戳
  -> LcmPublisherSystem
       在 trigger 到来时 serialize + Publish(channel)
```

Encoder 是普通 `LeafSystem`，输出 `Value<lcmt_device_status>`；Publisher 是唯一知道 channel 与 transport 的对象。这样一个状态消息可以同时连接真实 Publisher、日志记录器和断言系统，而无需让 Encoder 发送三次。反过来，若 Encoder 内部直接 `lcm.Publish`，一次输出端口重算可能产生重复网络副作用，仿真回滚或自动微分求值也会变得不可预测。

### 在 DiagramBuilder 中完成所有权与连线

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
auto* lcm = builder.AddSystem<drake::systems::lcm::LcmInterfaceSystem>();
auto* subscriber = builder.AddSystem(
    drake::systems::lcm::LcmSubscriberSystem::Make<lcmt_device_command>(
        "DEVICE_COMMAND", lcm));
auto* receiver = builder.AddSystem<DeviceCommandReceiver>(joint_count);

builder.Connect(subscriber->get_output_port(), receiver->get_input_port());
builder.Connect(receiver->get_output_port(), controller->get_input_port());
```

这里存在三种不同所有权：Builder 拥有所有 System；Publisher/Subscriber 借用 `LcmInterfaceSystem`；局部变量只是指向 Builder 所有对象的别名。`Build()` 后，Builder 把系统树移交给 Diagram，端口拓扑不可随意改变。不要用 `unique_ptr` 同时保存已经交给 Builder 的 System，也不要让 interface 比引用它的收发系统更早销毁。

如果 LCM interface 由外部进程级容器拥有，构造 `LcmInterfaceSystem` 时可以传入借用指针，但应用必须用明确的成员声明顺序保证逆序析构正确：外部 LCM 对象先构造、最后析构；Diagram 后构造、先析构。C++ 成员按声明逆序销毁，不按构造函数初始化列表的视觉顺序销毁。

### 从空目录到可运行设备桥的实现顺序

1. 固定 `.lcm` schema、单位、数组上限、sequence 与时间戳定义，生成类型后先把 fingerprint 当作协议版本边界。
2. 写无状态 Receiver/Encoder，只处理类型转换与数值验证，在 Diagram 中用常量源和捕获器连通端口。
3. 接入 `LcmSubscriberSystem` 和内存 interface，确认 pump、Context 提交与端口求值是三件不同的事。
4. 加入有效位、最后接纳时间和安全默认状态，再决定 latest-value 还是 bounded-event-queue。
5. 接入 `LcmPublisherSystem`，独立选择 plant step、command sample 和 status publish 三个周期。
6. 最后替换成真实 LCM URL，并把 channel、bus name、period、offset、timeout 放入部署配置。

这条顺序先稳定类型和时间语义，再引入网络。真正可复刻的最小闭环不是“收到了字符串”，而是同一套 Receiver/Controller/Encoder 在内存总线、真实 LCM 和日志回放下保持相同的端口与调度语义。

## 测试边界

测试可注入 `DrakeLcmInterface` 的内存实现，发布一条命令后推进仿真至采样点，检查 Receiver 的离散状态；随后推进至发布事件，解码捕获的状态消息。还应覆盖错误长度、旧时间戳、从未收到命令、连续多消息只取最新以及 Diagram 关闭时仍有接收活动。

不要用“睡眠若干毫秒后检查”验证仿真逻辑。测试应显式推进 Simulator 的仿真时间，这正是把通信建模为 System 后获得的确定性。

### 一条可重复测试

```text
1. 用内存 DrakeLcmInterface 创建 Diagram
2. 发布 sequence=10 的 command
3. 只 pump interface，不推进 Simulator：Context 输出仍为旧值
4. AdvanceTo(采样边界)：Context count 与 command 更新
5. AdvanceTo(status publish event)：捕获并解码 status
6. 再发布 11、12，验证 latest 或队列策略
7. 超过命令 timeout，验证安全输出
```

这个测试明确区分 transport 接收、Context 提交和周期发布三个事件。若三者用 wall-clock sleep 混在一起，失败时无法判断是网络未到、Simulator 未推进还是发布事件尚未发生。
