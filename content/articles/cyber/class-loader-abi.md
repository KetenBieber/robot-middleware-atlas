# 动态组件装载：注册宏、工厂对象与共享库生命周期

Cyber RT 的部署文件只保存组件库路径和类名。这里的 `.so` 是 Linux 的共享对象库，通常采用 ELF（Executable and Linkable Format，可执行与可链接格式）文件格式；动态加载器会在运行时把它映射进进程。配置中的 ABI 是 Application Binary Interface（应用二进制接口）：它规定已经编译的两部分代码如何通过函数调用、对象布局、虚函数表和运行库约定彼此协作。运行时因此能只凭字符串创建一个带具体消息模板参数的 C++ 对象：

```text
module_library: "libplanning_component.so"
class_name: "apollo::planning::PlanningComponent"
```

这条链跨越配置字符串、共享库映射、静态初始化、模板工厂、虚函数和共享所有权。所谓静态初始化，是库中具有全局/静态生命周期的登记对象在动态加载器完成映射后执行构造函数；Cyber 正是借它把工厂登记到进程内 registry。理解这条顺序后，DAG 装载不再是黑箱，也能看清插件系统最危险的错误：对象仍活着，提供其析构函数和虚表的 `.so` 却已经卸载。

本文固定 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，沿 `CYBER_REGISTER_COMPONENT`、class factory、`ClassLoader`、`ClassLoaderManager` 和 `ModuleController` 追踪完整生命周期。

## 动态装载解决部署期组合

若把所有算法组件直接链接进 mainboard，并在 `main()` 中手写构造：

```cpp
auto planning = std::make_shared<PlanningComponent>();
auto control = std::make_shared<ControlComponent>();
```

每次更改进程布局都需要修改代码、重新编译并重新链接 mainboard。组件类型集合也会泄漏到基础运行时。

Cyber RT 将两个决策推迟到部署期：

- 哪个共享库进入当前进程；
- 从该库创建哪个 `ComponentBase` 派生类。

mainboard 因此只依赖稳定基类和 class loader，不依赖 Planning、Control 或 Localization 的头文件。DAG 可以把同一个组件库放进不同进程，也能让多个组件共享一个 mainboard 进程。

这种灵活性不是免费的。类名拼写、注册遗漏、重复注册、编译选项不一致与 ABI 破坏都会从编译期错误变成装载期错误。

## 三层类型边界

在读这张图之前，先区分 `shared_ptr`：它是一种可复制的共享所有权句柄，复制它会让对象至少多存活到该句柄释放。整个设计同时使用三种类型表示：

```text
DAG:             "apollo::planning::PlanningComponent"  字符串
factory registry: AbstractClassFactory<Base>*             擦除派生类型
created object:   shared_ptr<ComponentBase>                基类所有权
```

具体派生类型只在注册宏展开和工厂模板实例化时出现。mainboard 创建对象后只保留 `shared_ptr<ComponentBase>`，通过虚函数完成 `Initialize()` 与 `Shutdown()`。

这是一条刻意设计的“类型漏斗”：

```text
Derived template type
    -> factory<Base>
    -> Base pointer
    -> virtual lifecycle calls
```

编译期强类型并没有消失，而是被限制在插件内部；跨库管理接口则收敛成非模板基类。

## ComponentBase 是插件 ABI

`ComponentBase` 提供所有组件共享的生命周期接口。`shared_ptr` 是让多个调用方共同保持对象存活的智能指针；`enable_shared_from_this` 让已被 shared_ptr 管理的对象在成员函数中取得共享同一控制块的指针。下面是帮助观察非模板边界的**简化接口示意，并非完整源码摘录**：

```cpp
class ComponentBase : public std::enable_shared_from_this<ComponentBase> {
 public:
  virtual ~ComponentBase() = default;
  virtual bool Initialize(const ComponentConfig& config) = 0;
  virtual bool Init() = 0;
  virtual void Clear() {}
  void Shutdown();
};
```

基类必须有虚析构函数。通过 `ComponentBase*` 删除 `PlanningComponent` 时，虚调用才能先进入派生析构，再进入基类析构。若基类析构非虚，删除行为未定义，派生成员也可能泄漏。

ABI 不只是函数声明，而是两边已编译机器码能够互相调用所依赖的约定。以下内容都必须在 mainboard 与组件库之间保持兼容：

- 虚函数表顺序与对象布局；
- 编译器及其 C++ ABI；
- `_GLIBCXX_USE_CXX11_ABI` 等标准库 ABI 开关；
- protobuf 类型与运行库版本；
- RTTI、异常和可见性配置；
- 基类头文件的准确版本。

给 `ComponentBase` 中间插入虚函数、改变虚继承关系或让两边使用不兼容编译选项，即使符号能成功解析，也可能在第一次虚调用时跳到错误地址。

插件接口因此应当小而稳定。消息处理的模板复杂度留在 `Component<M...>` 中，跨库边界只暴露少量生命周期函数，是降低 ABI 风险的重要设计选择。

## 注册宏把派生类绑定到共同基类

组件源文件末尾通常写下面这行**固定提交中的真实用法**；这里只将业务类名替换成 Planning 示例类名：

```cpp
CYBER_REGISTER_COMPONENT(PlanningComponent)
```

注册宏 `CYBER_REGISTER_COMPONENT` 继续展开：

接下来对照固定版本的实际代码：

```cpp
#define CYBER_REGISTER_COMPONENT(name) \
  CLASS_LOADER_REGISTER_CLASS(name, apollo::cyber::ComponentBase)
```

第二层宏生成一个针对 `Derived` 与 `Base` 的工厂，并通过静态对象初始化把工厂登记到全局 registry。这里 registry 就是按“基类类型 + 类名”查找工厂的登记表。下面是帮助看清关系的**概念伪代码，不是 Apollo 原始实现**：

```cpp
template<class Derived, class Base>
class ClassFactory final : public AbstractClassFactory<Base> {
 public:
  Base* CreateObj() override { return new Derived(); }
};

static Registrar<PlanningComponent, ComponentBase> registrar(
    "apollo::planning::PlanningComponent");
```

这里的 `static` 不是说“进程启动时一定执行”。它位于插件 `.so` 中；只有动态加载器把该库映射进进程并执行其初始化段后，registrar 构造函数才会运行。`dlopen` 是 POSIX/Linux 常见的动态库装载接口；Apollo 封装了底层调用，本文只用它说明加载时机。

因此正确顺序必须是：

```text
LoadLibrary(.so)
  -> dynamic loader maps library
  -> static registrar constructors run
  -> factory enters registry
  -> CreateClassObj(class_name)
```

如果在 `LoadLibrary()` 之前查询类名，registry 中没有对应工厂。若链接器裁剪了只含注册对象的目标文件，宏明明写在源码里，运行时仍可能找不到类。

## 工厂层完成派生类型擦除

registry 不能直接保存不同类型的 `ClassFactory<Derived, Base>` 对象，因此需要共同的抽象工厂。下面是**教学接口示例**，展示共同父接口，不是 Apollo 原始代码：

```cpp
template<class Base>
class AbstractClassFactory {
 public:
  virtual ~AbstractClassFactory() = default;
  virtual Base* CreateObj() = 0;
};
```

每个具体工厂知道 `new Derived`，调用者只知道返回值可视为 `Base*`。Factory Method（工厂方法）在这里指由具体工厂实现创建步骤、调用者依赖抽象创建接口的安排；类型擦除则隐藏 Derived。二者共同提供：

- 模板在注册点记住具体派生类型；
- 虚函数在查询点抹去派生类型；
- 字符串 key 在部署配置与工厂之间建立映射。

`CreateObj()` 返回裸指针并不表示最终所有权也应裸露。它只是工厂构造操作的最低层结果，`ClassLoader::CreateClassObj()` 必须立即把它放进 RAII 容器。RAII 是 Resource Acquisition Is Initialization（资源获取即初始化）：把资源释放放到拥有它的对象析构中，使提前返回也能自动清理。

## ClassLoader 将裸对象升级为受控 shared_ptr

`ClassLoader::CreateClassObj<Base>()` 先调用工厂拿到基类指针，再给最终删除动作登记 loader 计数。下面是**固定提交源码摘录**：

```cpp
Base* class_object = utility::CreateClassObj<Base>(class_name, this);
if (class_object == nullptr) {
  AWARN << "CreateClassObj failed, ensure class has been registered. "
        << "classname: " << class_name << ",lib: " << GetLibraryPath();
  return std::shared_ptr<Base>();
}

std::lock_guard<std::mutex> lck(classobj_ref_count_mutex_);
classobj_ref_count_ = classobj_ref_count_ + 1;
std::shared_ptr<Base> classObjSharePtr(
    class_object, std::bind(&ClassLoader::OnClassObjDeleter<Base>, this,
                            std::placeholders::_1));
return classObjSharePtr;
```

`std::lock_guard` 构造时获取 mutex，并在函数离开作用域时解锁；所以 `int` 的加一与卸载检查、删除回调中的减一由同一把锁串行化。`std::bind` 把成员函数、裸 `this` 和删除对象指针预先绑定成 deleter。这里不是 lambda 捕获，也不是 loader lease：deleter 仍借用 `ClassLoader` 的地址。

下面是同一固定提交中 deleter 的**真实源码摘录**：

```cpp
template <typename Base>
void ClassLoader::OnClassObjDeleter(Base* obj) {
  if (nullptr == obj) {
    return;
  }

  delete obj;
  std::lock_guard<std::mutex> lck(classobj_ref_count_mutex_);
  --classobj_ref_count_;
}
```

这个顺序是“先执行插件对象析构，再减少仍需该 loader 的对象数”。因此 `ClassLoader` 必须活到 deleter 结束；若先销毁 loader，再释放最后一只组件 `shared_ptr`，`std::bind` 保存的裸地址就会悬空。这里把裸指针立即交给 `shared_ptr` 控制块，是当前固定提交的实际代码；不能把“接管”误写成无异常窗口的事务保证。

自定义 deleter 被保存在 `shared_ptr` 控制块中；控制块是智能指针背后保存共享引用计数和最终释放函数的管理对象。变量的静态类型仍是 `shared_ptr<ComponentBase>`，却能在最后一个强引用消失时执行 loader 专用逻辑。相比之下，`unique_ptr` 表示唯一所有者，并把删除器类型写进它自身的指针类型；共享对象关系下 `shared_ptr` 的删除器不改变 `shared_ptr<Base>` 这个静态类型。

## 活对象计数保护库代码寿命

固定提交中的 `classobj_ref_count_` 是普通 `int`，不是原子变量；`classobj_ref_count_mutex_` 是互斥锁，保证同一时刻只有一个线程执行受保护的计数更新或检查。它保护创建时的递增、deleter 中的递减以及卸载时的检查。这个数记录由当前 loader 创建且尚未完成删除的对象数量，不是普通业务引用计数：`shared_ptr` 控制块已经统计对象有多少强引用；loader 计数统计还有多少最终删除动作需要回到该 loader。

真实 deleter 通过 `std::bind`（把成员函数与参数预先绑定成可调用对象）绑定 `ClassLoader::OnClassObjDeleter` 和裸 `this`，因此 `ClassLoader` 本身必须活到所有关联 deleter 执行完。mainboard 正常关闭先清组件强引用，再调用 `UnloadAllLibrary()`；若仍有对象，`UnloadLibrary()` 会拒绝卸载。它没有让 deleter 自身持有 loader 租约。另一个容易漏掉的边界是，`ClassLoaderManager` 用 `map<string, ClassLoader*>` 保存 loader，析构函数为空；manager 离开作用域时不会自动删除仍在表中的 loader。若活对象使卸载被拒绝，这会遗留 loader/库资源；而直接销毁仍有活对象的独立 `ClassLoader` 会违反 deleter 对裸 `this` 的生命周期前提。推荐实现可以像本文后面的最小版本那样捕获共享的 `LibraryState`，但那是改进方案，不是 Apollo 当前实现。

两个计数处在不同层：

```text
shared_ptr use_count
  -> 一个对象被多少强引用共同拥有

ClassLoader classobj_ref_count
  -> 这个库创建的多少对象仍未完成最终删除
```

复制一只组件 `shared_ptr` 只增加前者。最后一个副本释放时，自定义 deleter 执行 `delete`，再减少后者。

卸载条件不能只检查 mainboard 的 `component_list_` 是否为空。callback、全局 registry 或异步任务中都可能隐藏组件强引用。只要最后的控制块还没触发 deleter，库就仍然必须保留。

## 删除对象必须早于卸载代码

组件对象在内存中通常包含一只指向派生类虚表的隐藏指针：

```text
PlanningComponent object
  [vptr] ---> vtable in libplanning_component.so
              destructor address
              Initialize address
              other virtual function addresses
```

若先执行 `dlclose()`（减少当前进程对共享库的装载引用，引用数归零时系统可解除映射），动态加载器可能让库代码不可用。之后再调用：

**错误顺序示意，不是 Apollo 固定源码：**

```cpp
component.reset();
```

最后一个 shared pointer 会尝试通过已失效的虚表调用派生析构函数。结果可能是段错误，也可能在地址尚未复用时暂时“看起来正常”，形成难复现故障。

正确的不变量是：

```text
任何对象、deleter、callback 或函数指针仍可能进入插件代码
    => 共享库必须保持 loaded
```

这比“组件 vector 已清空”更严格，因为执行中的 `Proc()` 栈帧本身也位于插件代码中。

## ModuleController 是装载批次的所有者

`ModuleController` 同时持有：

```text
ClassLoaderManager
  `-- map<library_path, ClassLoader*>

component_list_
  `-- vector<shared_ptr<ComponentBase>>
```

这两个成员组成一对寿命约束：manager 让库保持加载，component list 让派生对象保持存活。声明顺序和显式 `Clear()` 都必须服从“对象先于代码销毁”。

装载一份 DAG 的主路径是：

```text
ModuleController::LoadModule(dag)
  -> resolve module_library path
  -> ClassLoaderManager::LoadLibrary(path)
  -> for each component config
       -> CreateClassObj<ComponentBase>(class_name)
       -> component->Initialize(config)
       -> component_list_.push_back(component)
```

对象在 `Initialize()` 前已经进入 `shared_ptr`，因此 `ComponentBase::shared_from_this()` 具备有效控制块。若在裸对象阶段调用 Initialize，组件内部执行 `shared_from_this()` 会抛出 `std::bad_weak_ptr`。

## 初始化失败的 RAII 回滚

考虑组件对象已创建，但 `Initialize()` 返回 false：

**教学最小例子：**

```cpp
auto component = manager.CreateClassObj<ComponentBase>(name);
if (!component || !component->Initialize(config)) {
  return false;
}
component_list_.push_back(component);
```

局部 `component` 离开作用域后，shared pointer 自动调用自定义 deleter，派生对象被删除，loader 活对象计数恢复。失败路径不需要手写与成功路径平行的 `delete`。

但“对象内存被回收”不等于“Initialize 的所有副作用都已撤销”。如果初始化到一半已经向 Scheduler、Dispatcher 或 topology 注册资源，组件实现必须让成员析构或显式 Shutdown 能清理部分状态。

这说明 RAII 需要逐层成立：顶层 shared pointer 只能保证组件对象释放，不能替没有 RAII 的子系统注册完成回滚。

## 先拆组件对象，再卸载提供机器码的共享库

动态装载多出了一层普通对象没有的寿命约束：只要对象的虚函数、析构函数、自定义 deleter 或在途 callback 仍可能跳入插件，提供这些指令的 `.so` 就必须保持映射。关闭不能简单写成 `UnloadAllLibrary()`；先拆对象、再卸载代码是必要的外层顺序。固定提交的 `ModuleController::Clear()` 把这个顺序写得很直接：

下面是 `ModuleController::Clear()` 的**固定提交源码摘录**：

```cpp
void ModuleController::Clear() {
  for (auto& component : component_list_) {
    component->Shutdown();
  }
  component_list_.clear();
  class_loader_manager_.UnloadAllLibrary();
}
```

这段代码把“停止组件”“销毁组件对象”“卸载代码”排成三步；但是第一步的名字不能代替对它内部行为的检查。Apollo 当前 `ComponentBase::Shutdown()` 是先调用派生类 `Clear()`，之后才关闭 Readers、移除并等待组件 task。因此，这个外层顺序保证的是“在正常释放组件对象之前先请求关闭，并且对象释放发生在尝试卸库之前”；它**不保证** `Clear()` 执行时 `Proc()` 已经退出。若 `Clear()` 释放正在被 `Proc()` 使用的成员，组件内部仍需单独的停止/等待协议，不能把 DSO 的卸载顺序当成回调静止屏障。具体交错见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

外层依赖顺序可以画成：

```text
1. component->Shutdown()
   - 设置 shutdown flag，并执行组件定义的 Clear()
   - 按 ComponentBase 当前实现，Reader 与组件 task 的关闭在 Clear() 之后

2. component_list_.clear()
   - 释放 ModuleController 对组件的强引用
   - 若它是最后一份，派生析构仍能进入 .so
   - custom deleter 随最终销毁减少 loader 活对象计数

3. ClassLoaderManager::UnloadAllLibrary()
   - 尝试释放库句柄
   - 若仍有活对象，loader 计数会阻止实际卸载
```

顺序的关键不是“看起来整齐”，而是关闭一张有向依赖图：

```text
Processor stack -> callback -> Component object -> plugin machine code
Reader task     -> callback -> Component object -> plugin machine code
shared_ptr      -> deleter  -> virtual destructor -> plugin machine code
```

只有所有指向右侧的路径都断开，`.so` 才能卸载。`component_list_.clear()` 也不等于对象必然已经析构：若其他地方还持有 `shared_ptr`，自定义 deleter 尚未运行，`classobj_ref_count_` 仍为正数，ClassLoader 会拒绝卸载。这个计数是防止“活对象配上已卸载代码”的最后一道保护，不会自动取消 scheduler 中的 callback，也不会替 `Clear()` 等待正在执行的 `Proc()`。

## 回调捕获采用 weak_ptr

Component 初始化时把业务闭包交给 routine。下面是**错误示例，不是 Apollo 源码**：若闭包直接按值捕获 `shared_ptr<Component>`：

```cpp
auto self = shared_from_this();
auto callback = [self](const auto& msg) { self->Process(msg); };
```

就会形成潜在所有权环：

```text
Component -> task/reader -> callback -> Component
```

Cyber RT 使用 `weak_ptr` 捕获，在调用前临时 `lock()`：

接下来对照固定版本的实际代码：

```cpp
std::weak_ptr<Component<M0>> self =
    std::dynamic_pointer_cast<Component<M0>>(shared_from_this());

auto callback = [self](const std::shared_ptr<M0>& msg) {
  if (auto component = self.lock()) {
    component->Process(msg);
  }
};
```

weak pointer 既不延长组件寿命，也避免访问已析构对象。临时 lock 成功后，局部 shared pointer 保证本次 `Process()` 调用期间对象不被删除。

它仍不能单独保证库卸载安全。卸载前必须从 Scheduler 移除 task，并等待正在执行的 callback 返回；否则即使对象寿命正确，Processor 仍可能正在执行 `.so` 中的 `Proc()` 指令。要把这句话与派生资源释放区分开：固定提交的 ComponentBase::Shutdown() 在执行 `Clear()` 时还没有等待 Component task 退出，所以后续等待能够保护 routine/对象在 task 排空前不被调度结构销毁，却不能回头保护 `Clear()` 已释放、而在途 `Proc()` 仍访问的成员。动态库卸载屏障并不自动等于组件内部资源屏障，详见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

## 并发卸载需要状态机

简单的 `loaded` 布尔值不足以描述真实装载器。至少存在这些状态：

```text
UNLOADED -> LOADING -> LOADED -> UNLOADING -> UNLOADED
                         |
                         `-> unload rejected: live objects
```

并发调用需要保护：

- 同一路径不能重复执行两次底层 load；
- 创建对象时库不能进入 unloading；
- 增减活对象计数与卸载条件检查不能竞态；
- registry 中属于该库的 factory 必须在卸载前解除关联；
- load 失败不能留下半注册工厂。

若装载器允许多次 `LoadLibrary(path)`，还需要区分“库句柄引用次数”和“库中活对象数”。前者描述多少调用者请求保持库加载，后者描述多少对象仍依赖库代码，两者都归零才具备卸载条件。

## 字符串工厂的错误模型

动态工厂把一部分错误推迟到运行时。常见失败包括：

| 失败点 | 表现 | 根因 |
|---|---|---|
| 加载库 | `LoadLibrary` 失败 | 路径、依赖库或符号缺失 |
| 查询类 | factory 不存在 | 类名不匹配或注册代码未执行 |
| 创建对象 | 返回空或异常 | 构造失败、抽象类或工厂损坏 |
| 基类匹配 | 无对应 Base registry | 注册时基类不一致 |
| 初始化 | `Initialize` 返回 false | 配置或资源创建失败 |
| 卸载 | 仍有活对象 | 隐藏 shared pointer 或在途 callback |

因此日志必须同时记录 library path、class name、base type、装载器状态和活对象数。只输出“create component failed”无法区分配置拼写与 ABI 问题。

部署系统还可以在启动前做静态预检：解析全部 DAG、确认库文件存在、检查类名清单、检测同进程组件名冲突。预检不能发现所有 ABI 错误，却能把一部分故障提前到车辆出发之前。

## 性能影响集中在生命周期路径

class loader 不在每条消息的热路径上。字符串查表、factory 虚调用和 `new Derived` 只发生在启动期，通常无需为微秒级开销优化。

运行期保留的主要成本是：

- Component 入口的一次虚调用；
- shared pointer 的生命周期管理；
- 为安全卸载维护少量互斥锁和受锁保护的整数计数（Apollo 固定实现如此；其他实现也可能选择原子计数）；
- 组件库带来的代码页和重定位开销。

对机器人中间件而言，确定的失败诊断和正确卸载远比减少一次启动期哈希查找重要。热路径优化应集中在消息复制、锁竞争与调度延迟，而不是把插件工厂改成脆弱的手写函数指针表。

## 可复刻的最小插件骨架

一个教学实现可以先省略真正的 `dlopen`，在单一二进制内验证工厂边界。`std::function` 用统一调用签名包装工厂函数；`std::string_view` 是借用的字符串视图，本身不拥有字符存储。下面是**教学最小例子**：

**教学最小例子：**

```cpp
class Plugin {
 public:
  virtual ~Plugin() = default;
  virtual bool Start() = 0;
  virtual void Stop() = 0;
};

using Factory = std::function<std::unique_ptr<Plugin>()>;

class Registry {
 public:
  bool Add(std::string name, Factory factory);
  std::unique_ptr<Plugin> Create(std::string_view name) const;
};
```

第二阶段再引入共享库，并把返回类型改为带 loader lease 的对象。这里用 `std::atomic<std::size_t>` 统计活对象数：原子操作保证单次计数读改写不会与另一线程的同一操作交错成数据竞争，但它不能单独协调“卸载与创建同时发生”的整个状态转换。下面是**教学推荐实现，不是 Apollo 当前实现**：

**教学推荐实现，不是 Apollo 当前实现：**

```cpp
struct LibraryState {
  void* handle;
  std::atomic<std::size_t> live_objects{0};
};

std::shared_ptr<Plugin> MakePlugin(
    Plugin* raw, std::shared_ptr<LibraryState> library) {
  ++library->live_objects;
  return std::shared_ptr<Plugin>(raw, [library](Plugin* p) {
    delete p;
    --library->live_objects;
  });
}
```

这里 deleter 捕获 `shared_ptr<LibraryState>`，控制块本身就持有一份库租约。即使外层 manager 意外释放状态对象，插件的最后一个 shared pointer 仍能让库状态存活到删除结束。这比 deleter 捕获裸 `this` 更容易证明生命周期安全。

第三阶段加入明确状态机、并发保护、注册撤销和错误报告。最后再接入 Scheduler，并验证 Stop 会等待所有在途 callback 返回。

## 插件系统的设计结论

Cyber RT 动态装载链可以归纳为五条边界规则：

1. 配置层只保存库路径与类名，不依赖业务头文件；
2. 注册点使用模板记住 Derived，registry 使用基类工厂擦除 Derived；
3. 对象创建后立即进入 RAII，初始化失败自动释放；
4. callback 用 weak ownership 打破环，但执行中的代码仍需显式等待；
5. 所有派生对象、deleter 和在途栈帧消失后，才能卸载共享库。

掌握这五条规则后，可以替换宏、容器或动态加载 API，而不会破坏真正重要的部分：插件对象的生命不能超过其数据依赖，却必须短于提供其可执行代码的共享库。
