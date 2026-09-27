# Orocos RTT C++ 设计实验：从一个 TaskContext 拆解语言机制

这一章不先讲 RTT 类名，而是用一个控制组件解释每项 C++ 机制为什么存在。目标是能独立写出一个生命周期正确、线程边界明确、数据路径有界的最小组件。

## 起点代码

```cpp
// 教学最小例子（机制演示不是固定提交源码）
class Controller final : public RTT::TaskContext {
public:
  explicit Controller(std::string name)
      : RTT::TaskContext(std::move(name)),
        state_in_("state"),
        command_out_("command") {
    addPort(state_in_);
    addPort(command_out_);
    addProperty("gain", gain_);
  }

  bool configureHook() override;
  bool startHook() override;
  void updateHook() override;
  void stopHook() override;

private:
  double gain_{1.0};
  RTT::InputPort<State> state_in_;
  RTT::OutputPort<Command> command_out_;
  Command command_;
};
```
先把这段代码放回源码地图。本文固定 RTT 提交为 [`600102e8`](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)：

| 代码中的概念 | 源码入口 | 继续追踪的方向 |
|---|---|---|
| `TaskContext` 与 hook | [`TaskContext.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.hpp)、[`TaskCore.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskCore.cpp) | 状态转换怎样决定 hook 是否可调用 |
| `InputPort<T>` / `OutputPort<T>` | [`InputPort.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/InputPort.hpp)、[`OutputPort.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/OutputPort.hpp) | 模板端点如何落到运行时连接 |
| `Activity` | [`Activity.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.hpp)、[`Activity.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.cpp) | 谁创建线程，谁触发 step，谁 stop/join |
| `ExecutionEngine` | [`ExecutionEngine.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L207-L248) | Operation 与 Port 事件怎样入队和被排空 |
| `Operation` | [`Operation.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Operation.hpp)、[`OperationCaller.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/OperationCaller.hpp) | ClientThread 与 OwnThread 如何改变调用链 |

读者应先沿一条端到端周期链阅读，而不是逐个类背定义：

```text
Activity thread
  -> ExecutionEngine::step
       -> 处理消息与 Port 事件队列
       -> TaskContext::updateHook
            -> InputPort<State>::read
            -> 控制算法
            -> OutputPort<Command>::write
                 -> ChannelElement chain -> storage/transport
```

这条链同时回答三个问题：代码在哪个线程执行，数据在哪些边界复制，以及队列或回调怎样侵占控制周期。

## `public TaskContext` 表示可替换的组件接口

公有继承表达 “Controller is-a TaskContext”。Deployer 只保存 `TaskContext*` 或基类 handle，却能调用派生类 hook。若使用私有继承，外部不能把 Controller 当作 TaskContext；若使用成员组合，框架也无法通过基类虚函数进入业务 hook。

基类必须有虚析构函数：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
TaskContext* task = new Controller("arm");
delete task;  // 只有基类析构 virtual 才会先执行 ~Controller()
```
否则派生类成员不会正确析构，Port、设备和 buffer 可能泄漏。这是插件基类最基本的 ABI 契约。

虚析构只保证 C++ 析构链完整，不保证 Activity 已经停止。若线程仍可能调用 `updateHook()`，即使 delete 能正确进入 `~Controller()`，它仍会与成员析构并发。正确顺序必须是“停止触发 → 唤醒阻塞线程 → join → 再 delete”。

插件边界还要求创建与销毁使用兼容的运行库。更稳妥的导出形式让插件同时提供工厂和销毁函数，或由 RTT 统一 plugin loader 约定；主程序不能假定可以跨不同 C++ runtime 直接 delete 插件分配的对象。

## `final` 固定当前类的继承边界

`final` 阻止再派生。控制组件通常通过组合算法对象扩展，而不是继续多层继承。这样 hook 的最终实现位置明确，也允许编译器在已知动态类型时去虚化调用。

不应把所有类都标 final。框架扩展点和测试替身需要派生；叶子业务组件适合 final。

若希望替换控制算法，应把算法做成成员接口，而不是派生 Controller：

```cpp
// 教学最小例子：独占拥有算法并允许算法实现多态替换
class IControlLaw {
public:
  virtual ~IControlLaw() = default;
  virtual bool Step(const State&, Command&) noexcept = 0;
};

class Controller final : public RTT::TaskContext {
  std::unique_ptr<IControlLaw> law_;
};
```

这样生命周期 hook 仍只有一个最终实现，算法可在 configure 阶段选择。`unique_ptr` 表达组件独占算法；若热切换算法，需要先在非实时线程完整构造新对象，再通过不可变快照或安全交换发布，不能在 updateHook 中加载插件。

## `explicit` 防止字符串隐式变成组件

若构造函数只有一个 `std::string` 参数且没有 explicit，编译器可以在某些调用中把字符串隐式转换为 Controller 临时对象。组件拥有 Port 和运行时身份，这种隐式构造既昂贵又含义错误。

```cpp
// 教学最小例子：声明单参数构造函数为 explicit
explicit Controller(std::string name);
```

规则是：单参数构造若不是刻意定义数值/值类型转换，默认写 explicit。

## `std::move(name)` 转移局部值而不是悬挂引用

构造函数按值接收 name。左值调用时先复制到参数，右值调用时移动；随后 `std::move` 交给基类。它统一了两套重载。

移动后的 `name` 仍可析构和重新赋值，但其内容未指定，不能再读取业务值。`std::move` 本身不移动，只把表达式转换成可被移动构造函数接受的右值类别。

按值接收适合最终需要拥有字符串的构造函数。如果调用方总是传左值，它会发生一次复制再一次移动；若极端关注配置阶段复制，可提供 `std::string_view` 并在内部构造一次 string，但绝不能把 view 保存为成员，除非能证明原字符存储活得更久。

## 成员初始化顺序由声明顺序决定

即使 initializer list 把 `command_out_` 写在前面，C++ 仍按类内声明顺序初始化。依赖前一个成员的后一个成员必须在声明顺序上也正确，否则代码看起来有序，运行却使用未构造对象。

析构顺序正好相反。这也是把 Activity owner 放在依赖对象之后或之前需要认真设计的原因。

例如：

```cpp
// 教学最小例子：成员声明顺序展示逆序析构；真实线程仍须显式停干净
class ComponentHost {
  Controller controller_;                  // 先构造，后析构
  std::unique_ptr<RTT::Activity> activity_; // 后构造，先析构
};
```

若 Activity 析构保证 stop/join，这个声明顺序让线程先停止，再析构 Controller。若顺序反过来，Controller 会先消失，Activity 仍可能持有 Runnable 指针。成员声明因此是生命周期图的一部分，不只是代码风格。

## `override` 让签名错误在编译期失败

```cpp
// 教学最小例子：override 让 hook 签名在编译期检查
void updateHook() override;
```

如果误写成 `void updateHook() const`，没有 override 时它只是一个新函数，框架仍调用基类空实现；有 override 时编译器直接报错。对所有框架 hook 和接口实现都应使用 override。

## 虚调用的实际过程

概念上对象包含一个指向虚函数表的隐藏指针：

```text
Controller object
  vptr -> Controller vtable
            configureHook -> Controller::configureHook
            updateHook    -> Controller::updateHook
  TaskContext fields
  gain_, ports, command_
```

一次虚调用多一次间接寻址，通常不是控制周期瓶颈。真正成本是 hook 内算法、复制、锁和 cache miss。不要为了省一个虚调用破坏组件边界。

虚调用还可能阻止内联，但一个周期通常只进入一次 updateHook。相反，在每个关节、每个样本的内层循环中使用虚接口，成本才可能被放大。设计时应把运行时多态放在粗粒度策略边界，把数值内核保留为可内联的普通函数或模板。

## 模板 Port 把类型错误提前到编译期

`InputPort<State>` 和 `OutputPort<Command>` 是不同具体类型。模板让 `read(State&)`、`write(const Command&)` 在编译期检查。部署器的动态连接仍需要 typekit 把 C++ 类型注册为运行时名字。

```text
编译期：InputPort<State> 不能连接 OutputPort<Image>
运行期：Deployer 通过 type name 查 typekit，验证两端相容
```

这是一种双层类型系统，而不是模板取代反射。

### 模板代码在编译期展开了什么

可把 Port 的最小形状理解为：

```cpp
// 教学最小例子：类型擦除示意；不是 RTT 的 InputPort 源码
template<class T>
class InputPort {
public:
  FlowStatus read(T& destination) {
    return endpoint_->Read(type_erased_view(destination));
  }
private:
  std::shared_ptr<IInputEndpoint> endpoint_;
};
```

模板外壳知道 `T`，因此能生成正确的引用和类型描述；内部 endpoint 通过非模板虚接口或 type erasure 进入统一连接图。若整条 ChannelElement 链都保持模板类型，编译性能和二进制体积会随类型数增长，动态插件也难以只靠运行时类型名连接。

typekit 的责任是把运行时字符串、C++ 类型信息、构造/复制/序列化操作关联起来。它不能神奇地让两个布局不同但名字相同的类型兼容；跨进程或跨插件 ABI 仍需稳定 wire schema 和版本规则。

### 引用、指针和智能指针分别承诺什么

考虑 `updateHook()` 正在读取测量值，而一个 `ClientThread` Operation 同时修改同一个对象。先分清 C++ 的访问形式：引用是已绑定对象的别名，不能表达“没有对象”，也不能在之后改绑；指针保存地址，可以为空或指向另一个对象；`const T&` 让当前函数不能通过这个别名修改 `T`，但不冻结对象，也不阻止另一线程通过其他别名写它。它们本身都不说明对象由谁释放。

**教学最小例子**如下：

```cpp
// 教学最小例子：引用用于同步调用中的借用；指针可表达可选对象
void CheckFresh(const State& state) {
  Validate(state);
}

State* optional_state = nullptr;
if (optional_state != nullptr) {
  CheckFresh(*optional_state);
}
```

`const State&` 避免在这个同步调用入口复制一个可能很大的样本，但被借用的对象必须至少活到函数返回。若把该地址存到 Operation 队列，原先栈上的 `State` 返回后就会悬空；const 不能延长寿命。固定 RTT 的 `OutputPort<T>::write()` 也以参数引用接收样本，但真正进入 ChannelElement 后仍可能复制，见 [OutputPort::write](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/OutputPort.hpp#L239-L263)。

智能指针描述释放责任。`std::unique_ptr<T>` 是单一拥有者；移动它会把地址责任交给目标，源指针变空，销毁目标时调用删除器。`std::shared_ptr<T>` 允许多个拥有者共享寿命；每个 shared pointer 保存对象指针并关联一个控制块，控制块记录共享计数和最终删除操作。计数保证“最后一个共享拥有者消失时才析构对象”，并不让对象的字段自动线程安全。两个线程分别持有 shared_ptr 并同时写 `T::value`，仍然是数据竞争。`std::weak_ptr<T>` 不增加强计数，适合打破所有权环或表示可失效观察者；调用 `lock()` 若成功会临时得到一个 shared_ptr，持有到局部工作结束。

RTT 这版许多地方使用 `boost::shared_ptr`，不应把它称作 `std::shared_ptr`。TaskCore 对 ExecutionEngine 用原始指针并由析构负责 delete；TaskContext 用 shared pointer 管理默认 Activity，而 Activity 的 Runnable 关系是非拥有指针。对象所有权与线程退出是两项分别证明的事情，见 [TaskCore 构造/析构](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L53-L76)、[TaskContext Activity 所有权转换](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L338-L373)。

### `std::function` 怎样把不同可调用对象变成一个接口

函数指针、带状态的仿函数和 lambda 的具体 C++ 类型不同，调用代码若需要统一保存与调用形式，可以使用 `std::function<R(Args...)>`：它存放任意能以这些参数调用并能产生 `R` 的目标，再由同一 `operator()` 进入实际目标。编译器在模板实例化时仍会生成目标类型；`std::function` 只把目标藏到运行时包装对象之后。包装大目标时实现可能分配，因此不能从接口类型本身推断“无堆分配”。

```cpp
// 教学最小例子：不同 lambda 共享一个 std::function 调用签名
std::function<double(const State&)> score =
    [gain](const State& s) { return gain * s.position; };
double value = score(current_state);
```

lambda 按值捕获的 `gain` 被存进闭包对象，闭包活多久就保有这份副本多久；若写 `[this]`，保存的只是组件地址，不会拥有组件。固定 RTT 源码使用 `boost::function`：[`OperationCallerBinderImpl`](https://github.com/orocos/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/OperationCallerBinder.hpp#L54-L77) 把成员函数指针与对象指针经 `boost::bind` 包装成统一签名；被保存的 object pointer 仍不拥有 TaskContext。OwnThread 调用在真正执行时进入队列分支，ClientThread 则直接调用 `mmeth()`，见 [`LocalOperationCaller::call_impl`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/LocalOperationCaller.hpp#L351-L368)。所以在 RTT 中，Service/Operation 的存活必须被限制在目标组件仍然有效的生命周期之内。

### `read(T&)` 的三个返回状态

端口读取不能只返回 bool，因为控制器需要区分：

- `NewData`：得到自上次读取后到达的新样本，可以推进估计与控制；
- `OldData`：只有上次值，是否继续使用取决于最大允许数据年龄；
- `NoData`：从未收到有效样本，通常不能产生正常控制命令。

`OldData` 不等于“数据仍安全”。组件还需要样本时间戳或本地到达时间：

```cpp
// 教学最小例子：读状态前检查时间戳；const auto 保存返回状态值
const auto status = state_in_.read(state_);
if (status == RTT::NewData) last_state_time_ = clock_->now();

if (status == RTT::NoData ||
    clock_->now() - last_state_time_ > max_state_age_) {
  command_ = SafeCommand();
  command_out_.write(command_);
  return;
}
```

这段逻辑把中间件状态转换为控制安全策略。若只判断 NoData，断线后缓存中的 OldData 可能被无限重复使用。

## 值成员提供确定所有权

Port 作为值成员表示 Controller 独占其生命周期：构造组件时构造 Port，析构组件时逆序析构。相比裸指针，没有 null 状态和单独 delete；相比 shared_ptr，不需要引用计数。

只有当对象需要多态、可选或共享时才引入智能指针。默认优先值语义。

值成员也消除了额外 heap allocation 和引用计数，但前提是类型可在构造阶段建立。设备句柄若只能在 configureHook 打开，可使用一个明确的 RAII handle 或 `optional<Device>` 表达“尚未配置”，而不是裸指针加布尔标志。optional 的析构会在有值时自动调用 Device 析构，仍要保证 Device 析构不会在实时线程执行无界阻塞。

## 运行期避免临时分配

`Command command_` 作为成员预先构造，updateHook 复用它：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
void Controller::updateHook() {
  State state;
  if (state_in_.read(state) != RTT::NewData) return;
  fill_command_in_place(state, gain_, command_);
  command_out_.write(command_);
}
```
若 `State` 内有 vector，局部 state 每周期仍可能分配。更稳妥是成员缓存并在 configureHook reserve 固定上限。需要检查消息赋值操作是否会扩容。

`reserve(max_joints)` 只保证 vector 容量至少达到上限；后续若输入长度超过容量，赋值仍会分配。实时路径必须先验证长度并拒绝超界。`resize(max)` 在配置阶段还会构造元素，适合需要固定可写区间的算法；`reserve` 只分配原始容量，`size()` 仍为零。两者不能混用。

输出端也可能复制 `Command`。即使 `command_` 自身不分配，Port storage 在第一次见到大 vector 时仍可能扩容。要获得有界行为，需要在 configure 阶段为端口样本初始化最大形状，或使用固定容量类型，例如 `std::array<double, MaxJoints>` 加实际长度。

## Hook 异常边界

实时 hook 最好不抛异常。若算法可能失败，用显式状态返回并触发 error：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
if (!algorithm_.step(state, command_)) {
  this->error();
  return;
}
```
异常会栈展开，成本难以界定；即便 ExecutionEngine 捕获，当前周期也已失去时间保证。配置阶段可以用异常封装不可恢复构造错误，但要在框架边界转成状态。

`noexcept` 可以把“不应抛异常”写入类型契约，但如果内部仍抛出，程序会调用 `std::terminate`，并不会自动转成安全状态。因此不能只给 updateHook 标 noexcept；还要审计它调用的容器、日志、用户类型复制和设备 API。固定容量容器、预分配、错误码以及无分配日志路径才是实际保证。

## Operation 的 lambda 捕获决定寿命

```cpp
// 教学最小例子（机制演示不是固定提交源码）
addOperation("reset", [this] { reset(); }, RTT::OwnThread);
```
捕获 `this` 不延长对象寿命。Operation registry 必须在 Controller 析构前停止调用。如果 callback 可能越过 owner 生命周期，应捕获 weak handle：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
[weak = weak_from_this()] {
  if (auto self = weak.lock()) self->reset();
}
```
TaskContext 是否由 shared_ptr 管理取决于部署框架，不能未经确认调用 `shared_from_this`。错误地在栈对象上使用会抛 `bad_weak_ptr`。

### ClientThread 与 OwnThread 改变的是执行所有权

ClientThread Operation 直接在调用者线程执行：少一次排队和唤醒，返回值自然同步，但外部低优先级线程可能进入组件状态并占有内部锁。若它与高优先级 updateHook 共享 mutex，就可能产生优先级反转。

OwnThread Operation 把请求封装成 command，投递给组件 ExecutionEngine：组件状态可在线程内串行访问，但调用者要面对队列已满、组件停止、执行异常、超时和取消。它是 Active Object 模式，不是“把同一个函数换个线程跑”这么简单。

一个最小有界命令对象可以写成：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
struct ResetCommand {
  std::uint64_t request_id;
  std::promise<ResetResult> completion;
};

class CommandQueue {
public:
  explicit CommandQueue(std::size_t capacity) : capacity_(capacity) {}

  bool TryPush(ResetCommand command) {
    std::lock_guard lock(mu_);
    if (closed_ || queue_.size() == capacity_) return false;
    queue_.push_back(std::move(command));
    ready_.notify_one();
    return true;
  }

private:
  const std::size_t capacity_;
  std::mutex mu_;
  std::condition_variable ready_;
  std::deque<ResetCommand> queue_;
  bool closed_{};
};
```
这段代码只展示入队，真实实现还要保证每个 promise 恰好完成一次。正常执行设置 Result；满载应在入队前立即返回 busy；关闭要把未执行命令完成为 cancelled；执行抛异常要转成 error；调用者超时只表示“不再等待”，不能默认命令已经从队列消失。

`std::promise` 自身可能分配，因而不能直接用于硬实时路径。实时版本可以使用预分配 command slot、固定容量 ring 和整数 completion token，由非实时等待线程查询结果。关键不是选择哪种容器，而是同时给出空间上界和每周期处理上限。

### 队列容量不等于每周期时间预算

即使队列容量固定，如果一次执行机会把命令全部 drain，突发 `C` 条命令仍可能把 `updateHook()` 推迟约 `C × WCET_command`。在自己的复刻系统中，可以限制每轮最多处理 `B` 条或最多消耗 `T_budget` 时间：

```text
period budget = operation budget + port-event budget + updateHook WCET
worst case    = Bop × WCETop + Bport × WCETport + WCETupdate
```

容量回答“最多占多少内存”，每轮数量/时间上限回答“最多占多少执行时间”。固定提交的 RTT 队列容量为 100；其 `processMessages()` 与 `processPortCallbacks()` 排空到队列为空，并没有这里展示的可配置每周期 batch budget。源码在 [`ExecutionEngine.cpp` 的队列定义与 drain 循环](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L54-L75) 和 [`processMessages`/`processPortCallbacks`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L207-L248)。因此以上公式是复刻系统的设计约束，不是对 RTT 的实现描述。

## 锁不能包围用户代码

框架锁保护 registry 和状态，但 hook、Operation body、callback 都属于外部代码。正确模式是锁内复制稳定 handle，锁外调用：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
std::vector<std::shared_ptr<Callback>> callbacks;
{
  std::lock_guard lock(mutex_);
  callbacks = callbacks_;
}
for (auto& cb : callbacks) (*cb)();
```
互斥锁保护的是 `callbacks_` 这个 vector 的结构不变量：复制列表时不会与注册/移除并发修改迭代器和存储；锁外每个 shared_ptr 暂时延长 Callback 对象寿命。锁不保护 Callback 内部的数据，也不保护 callback 所访问的组件状态。若回调闭包还捕获 owner 的裸 `this`，即使 Callback 本身活着，owner 仍可能被销毁；卸载必须先阻止新调用并等待所有在途调用结束，或让闭包使用可检测失效的弱引用。引用计数本身也会产生原子操作和潜在缓存竞争。固定 RTT 的 `RosPublishActivity` 采用另一种具体边界：`publishers_lock` 保护集合迭代和 add/remove，而 `publish()` 在持锁期间运行，因此慢发布器会推迟其他发布器，见 [RosPublishActivity::loop/add/remove](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic_ros_publish_activity.cpp#L42-L65)。

### 原子变量如何发布关联数据

原子操作只保证一个原子对象上的读写不会撕裂；要用它发布旁边的普通数据，还需要一个跨线程的先后关系。线程 A 先写 payload，再以 release 存 ready；线程 B 以 acquire 读到 ready 后，才能依赖 payload 写入已对它可见。`relaxed` 只给 ready 本身原子性，不建立 payload 的 happens-before 关系。若机器人命令的序号原子变量读到“新序号”，但旁边的关节向量仍是旧值或新旧混合，问题就会表现为控制目标与 sequence 对不上。

先看会出错的**错误示例**：

```cpp
// 错误示例：relaxed 标记没有发布普通 payload 的顺序保证
Payload payload;
std::atomic<bool> ready{false};

void producer() {
  payload = make_command();
  ready.store(true, std::memory_order_relaxed);
}

void consumer() {
  if (ready.load(std::memory_order_relaxed)) {
    execute(payload);
  }
}
```

即使写线程按源码顺序先赋 payload 再设 flag，C++ 内存模型也没有要求另一线程在观察到 relaxed flag 后一定看见 payload 的新写入。这个例子同时假设只发布一次、读取一次；循环复用时还必须定义 slot 何时可重写和何时读取结束。

单次交接的**教学最小例子**可以用 release/acquire：

```cpp
// 教学最小例子：一次性发布；ready 不复位，payload 发布后不再修改
Payload payload;
std::atomic<bool> ready{false};

void producer() {
  payload = make_command();
  ready.store(true, std::memory_order_release);
}

void consumer() {
  if (ready.load(std::memory_order_acquire)) {
    execute(payload);
  }
}
```

消费者 acquire 读到该次 release 写入后，payload 写入先行发生于 `execute` 的读取。它不允许生产者之后不加同步地重写 payload；也不解决多个消费者只允许一个读到新事件、槽位复用或回收问题。固定 RTT 的 `DataObjectLockFree::Get/Set` 不是这段代码的简单翻译：源码用 `oro_atomic_*` 与 CAS 锁住环形槽位，读侧增加 `read_counter` 后再核对 `read_ptr`，写侧取得 `write_lock` 并再次核对写指针；状态字段 CAS 让一个 reader 消费 NewData。源码注释中还保留 `smp_mb` 屏障位置。见 [`DataObjectLockFree.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/DataObjectLockFree.hpp#L197-L308)。它没有暴露 `std::memory_order` 参数；内存屏障实际效果需沿该版本 `oro_atomic` 平台后端继续核验，不能仅凭类名声称 lock-free 就断言是 wait-free 或无分配。

## 生命周期 hook 是资源事务

下面给出一个比起点代码更完整的组件骨架。重点不是设备 API，而是每个阶段拥有什么以及失败如何回滚：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
class Controller final : public RTT::TaskContext {
public:
  explicit Controller(std::string name)
      : TaskContext(std::move(name)),
        state_in_("state"), command_out_("command") {
    addPort(state_in_);
    addPort(command_out_);
    addProperty("gain", gain_);
  }

  bool configureHook() override {
    if (max_joints_ == 0 || max_joints_ > kHardJointLimit) return false;

    State next_state;
    Command next_command;
    next_state.joints.reserve(max_joints_);
    next_command.effort.reserve(max_joints_);

    auto next_device = Device::Open(device_name_);
    if (!next_device) return false;
    if (!next_device->Configure(max_joints_)) return false;

    state_ = std::move(next_state);
    command_ = std::move(next_command);
    device_.emplace(std::move(*next_device));
    configured_ = true;                 // 最后提交
    return true;
  }

  bool startHook() override {
    if (!configured_ || !device_) return false;
    last_state_time_ = clock_.now();
    if (!device_->Enable()) return false;
    enabled_ = true;
    return true;
  }

  void updateHook() override {
    const auto status = state_in_.read(state_);
    if (status == RTT::NewData) last_state_time_ = clock_.now();

    if (status == RTT::NoData || state_.joints.size() > max_joints_ ||
        clock_.now() - last_state_time_ > max_state_age_) {
      WriteSafeCommand();
      return;
    }

    if (!law_->Step(state_, gain_, command_)) {
      WriteSafeCommand();
      this->error();
      return;
    }
    command_out_.write(command_);
  }

  void stopHook() override {
    WriteSafeCommand();
    if (enabled_ && device_) device_->Disable();
    enabled_ = false;
  }

  void cleanupHook() override {
    device_.reset();
    configured_ = false;
  }

private:
  void WriteSafeCommand() noexcept;

  static constexpr std::size_t kHardJointLimit = 64;
  double gain_{1.0};
  std::size_t max_joints_{kHardJointLimit};
  Duration max_state_age_{Milliseconds(20)};
  std::string device_name_;
  RTT::InputPort<State> state_in_;
  RTT::OutputPort<Command> command_out_;
  State state_;
  Command command_;
  std::optional<Device> device_;
  std::unique_ptr<IControlLaw> law_;
  Clock clock_;
  TimePoint last_state_time_{};
  bool configured_{};
  bool enabled_{};
};
```
### configureHook 采用候选值再提交

临时 `next_state`、`next_command` 和 `next_device` 都由局部 RAII 管理。任何一步返回 false，局部对象自动释放，成员仍保持未配置状态。只有全部成功才 move 到成员并最后设置 `configured_`。这比逐步修改成员、再依靠多组布尔值回滚更容易证明。

`optional<Device>` 表示设备可能不存在，并拥有其值；`emplace` 在 optional 内构造设备。若 Device 不可安全移动，应让 Open 返回 `unique_ptr<Device>`。不能因为语法方便就强迫 OS handle 类型支持错误的 move。

### startHook 只做运行切换

大块内存、文件解析和设备发现应在 configure 阶段完成。startHook 只验证前置条件、重置时序状态并使能硬件。若 Enable 成功后还有下一步可能失败，就需要 guard 保证失败时 Disable；示例把 enabled 标志放在成功之后，避免 stopHook 对未使能设备重复操作。

### updateHook 的每条分支都有安全输出

NoData、过期数据、形状越界和算法失败都进入 SafeCommand。是否每周期重复写安全命令取决于设备协议：有些驱动需要 watchdog 刷新，有些只需一次状态切换。`WriteSafeCommand()` 标为 noexcept 只是契约，内部仍须使用预构造命令且不能走会分配或阻塞的日志路径。

### stopHook 与 cleanupHook 分工

stopHook 使运行中的装置进入安全但仍可再次 start 的状态，因此不销毁配置资源。cleanupHook 才释放 configure 阶段取得的设备，之后必须重新 configure 才能 start。把资源释放塞进 stopHook 会破坏 Stopped → Running 的重启语义。

## Activity 所有权与安全宿主

业务组件本身不应偷偷创建未知 Activity；部署层决定周期、优先级、调度策略和 CPU affinity。一个最小宿主可以表达明确顺序：

```cpp
// 教学最小例子（机制演示不是固定提交源码）
class ComponentHost final {
public:
  ComponentHost(std::unique_ptr<Controller> component,
                std::unique_ptr<RTT::Activity> activity)
      : component_(std::move(component)),
        activity_(std::move(activity)) {
    if (!component_ || !activity_) throw std::invalid_argument("null");
    component_->setActivity(activity_.get());
  }

  ~ComponentHost() { Shutdown(); }

  void Shutdown() noexcept {
    std::call_once(shutdown_once_, [this] {
      component_->stop();       // 生命周期转入 Stopped
      activity_->stop();        // 请求线程停止并等待
      component_->cleanup();
      activity_.reset();
      component_.reset();
    });
  }

private:
  std::unique_ptr<Controller> component_;
  std::unique_ptr<RTT::Activity> activity_;
  std::once_flag shutdown_once_;
};
```
固定 RTT 的 `Activity::stop()` 会请求线程停止并处理阻塞 loop；Activity 析构还调用 `terminate()`，因为 `stop()` 本身并不保证底层线程已经结束。具体同步路径见 [`Activity.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.cpp#L105-L112) 与 [`Activity::stop`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.cpp#L280-L326)。架构不变量是：线程仍可能调用 Runnable 时，不可先析构 Runnable 或其代码所在的动态库。若停止返回失败，显式 shutdown 阶段应保留这些对象并决定升级恢复策略。

## 数据结构、复杂度与实时预算

设端口 buffer 容量为 `C`、样本最大字节数为 `S`。如果一个自研 Engine 明确限制每轮 Operation 与 Port 事件数量，分别用 `Bop` 和 `Bport` 表示；固定 RTT 提交没有这样的 Port/message 每周期 batch 参数，不能把后两者当作其源码属性：

| 路径 | 时间上界的主要项 | 空间上界 | 风险 |
|---|---|---:|---|
| DATA 端口读写 | 一次同步/复制 `O(S)` | `O(S)` | 类型复制内部可能分配 |
| BUFFER 入队出队 | 同步加样本复制 `O(S)` | `O(C×S)` | 满载策略未定义 |
| `readNewest` | 可能丢弃/遍历积压 `O(C)` | 不新增主存储 | 被误认为固定 `O(1)` |
| OwnThread Operation | 入队加等待；执行取决于 body | `O(Cop×command_size)` | 超时不等于取消 |
| 每周期 Engine | `Bop×WCETop + Bport×WCETport + WCETupdate` | 固定工作集 | 批量无界导致 deadline miss |
| stop/join | 最慢 hook/I/O/等待结束时间 | 小量控制状态 | 无法唤醒导致无限关闭 |

所谓 lock-free Port 只描述容器算法的进展性质。若 `T` 的赋值包含 vector 扩容、shared_ptr 引用计数竞争或自定义锁，整体路径仍非 wait-free。实时可行性必须把用户类型、transport、page fault、日志、调度优先级和 CPU 共享一并纳入测量。

周期为 `P` 时，需要验证的不是平均执行时间，而是：

```text
release jitter
+ Engine bounded work
+ updateHook WCET
+ maximum blocking / priority inversion
+ transport or device write bound
< P - safety margin
```

若任一项没有上界，只能说实验中暂未超期，不能声称硬实时。

## 可迁移的设计能力

RTT 最值得迁移的不是 API 名称，而是三轴分离：生命周期状态、执行线程、数据策略彼此正交。相同 Controller 可以处于 Stopped 或 Running，可以由周期或事件 Activity 驱动，也可以把 Port 配成 DATA 或 BUFFER；每个选择都通过显式策略组合，而不是复制一套组件类。

Template Method 固定 configure/start/update/stop/cleanup 的框架顺序，策略对象替换 Activity 与 Channel storage，Active Object 用 OwnThread Operation 串行化组件命令，type erasure 把模板端口接入动态部署系统。迁移到自研框架时，应复制这些边界和不变量，而不是照搬类名。

## 最小复刻路线

1. 先实现 TaskCore 状态枚举和合法转换，让失败保持在可解释状态；
2. 用虚 hook 实现 Template Method，并验证 configure 失败的逆序回滚；
3. 加入单线程周期 Activity，明确 stop、wake、join；
4. 实现 DATA 最新值端口，再实现固定容量 BUFFER 和满载策略；
5. 加入 ExecutionEngine command queue；如果控制 deadline 需要，再为自研执行器增加每周期 batch 上限；
6. 实现 ClientThread/OwnThread 两种 Operation，并给每个请求唯一终态；
7. 最后加入 typekit、插件、跨进程 transport 和部署器。

每一步完成后都应能回答：谁拥有对象、在哪个线程调用、可能复制几次、空间上限是多少、关闭怎样解除阻塞、失败后处于哪个状态。回答不出来时，不应继续叠加下一层动态能力。

## 最小组件的完成标准

- 每个 hook 使用 override，基类析构为 virtual；
- Port 与缓存优先值语义，运行期不扩容；
- Activity 停止并 join 后才析构组件；
- OwnThread Operation 有有界队列和关闭结果；
- callback 不在框架锁内执行；
- OldData/NoData 进入明确安全策略；
- configure 失败能逆序释放已经取得的设备资源。
