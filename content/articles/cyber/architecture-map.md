# 从零构造 Cyber RT：六层代码骨架与一帧消息的运行时

本文以 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 为源码基线。带有“固定源码”字样的片段取自该提交；标为“教学实现”的代码用于解释设计选择，不是 Apollo 原码。本文只建立架构骨架，把逐函数的传输、队列、协程与动态库细节留给后续专题。

设想需要给车辆接入一只 100 Hz 相机：驱动每 10 ms 发布图像；感知算法偶尔需要 30 ms；规划还要消费定位数据，控制则希望每 10 ms 读取最新轨迹。中间件既不能让相机接收线程停下来等待感知，也不能让慢日志模块夺走规划模块的消息。于是先给自己一个约束：**一帧图像的存储位置、唤醒信号和业务执行位置必须分离。**

第一版很容易写成同步观察者。下面是**教学反例**：

```cpp
template <class T>
class Channel {
 public:
  void Subscribe(std::function<void(const T&)> cb) {
    callbacks_.push_back(std::move(cb));
  }
  void Publish(const T& msg) {
    for (auto& cb : callbacks_) cb(msg);  // 发布线程同步运行算法
  }
 private:
  std::vector<std::function<void(const T&)>> callbacks_;
};
```

这段实现没有跨线程同步，假定启动后订阅者固定。在上述车辆中，感知的 30 ms 回调让发布者在一次调用里错过后续两帧；如果又允许在遍历时销毁订阅者，保存了裸 `this` 的回调还可能调用已经析构的对象。简单地“加线程池”只转移了阻塞位置：如果没有有界缓存和任务身份，30 ms 消费者面对 10 ms 生产者仍会无限排队。架构应先明确数据语义，再配置线程数。

## 第一层：让业务只看见 Node、Reader 和 Writer

不应该让规划 `Proc()` 判断图像来自共享内存还是网络。对业务代码，`Channel` 是一个**逻辑名字**；`Message` 是一个**编译期类型**；`Node` 是创建通信端点并给它们赋予运行时身份的入口。Reader/Writer 是端点，不是两只互相指向的业务对象。

```text
Component / 普通业务代码
       |
       v
Node ── CreateReader<T> / CreateWriter<T>
       |              |
       v              v
    Reader<T>      Writer<T>
       |              |
       +---- Transport
```

在这层，模板参数 `T` 决定序列化类型，运行时 channel 名决定逻辑路由。两者必须同时匹配；单有字符串名字不能让任意两个不同 protobuf 类型安全通信。`Node` 不负责调用 `Proc()`，否则通信端点会再次与算法执行绑在一起。

从这里继续追源码：[Node、Reader 与 Writer](node-reader-writer.md) 分解端点构造、发现、Receiver 共享、`Write` 的拷贝差异与关闭时的回调关系。现在只需要记住：**Reader 建立接收入口，Component 另外建立业务执行入口**，两者不总是同一只回调。

## 第二层：装配是运行时的工作，不是编译时写死的依赖

第一版把 `Perception`、`Planning` 和 `Control` 直接放进 `main()`，很快会碰到“换一套部署就重新链接主程序”的问题。需要一个非模板基类统一管理异构 Component，再按 DAG 指定的动态库和类名创建实际对象。

固定源码中的 `ModuleController::LoadModule(const DagConfig&)` 给出了装配的核心顺序：

```cpp
for (auto module_config : dag_config.module_config()) {
  std::string load_path;
  if (!common::GetFilePathWithEnv(module_config.module_library(),
                                  "APOLLO_LIB_PATH", &load_path)) {
    AERROR << "no module library [" << module_config.module_library()
           << "] found!";
    return false;
  }
  AINFO << "mainboard: use module library " << load_path;

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
  // 以下还有 timer_components 的同构创建分支，此处省略。
}
```

这是从配置到对象的真正类型转换点：`module_library` 决定到哪里找机器码，`class_name` 决定用哪个注册工厂，`ComponentBase` 允许一只 `vector` 容纳各不相同的业务派生类。`Initialize` 是框架入口，内部随后调用业务实现的 `Init()`；两者不能混称。`component_list_` 持有成功初始化的对象，动态库必须活得比对象及其代码回调更久。插件登记和卸载顺序详见[动态组件装载](class-loader-abi.md)，DAG、初始化失败和 Component 关闭则见[从 DAG 到 Component](dag-to-component.md)。

这里还要看到真实代码中的边界：如果中途一个 `Initialize` 返回 `false`，函数直接退出，不能由这一段就推断“当前批次已自动完整回滚”。要判断局部装配失败后的资源状态，必须继续看调用方怎样调用 `Clear()`、此前创建的 reader/task 如何销毁，以及插件工厂是否仍保有共享库。这是实现需要验证的失败路径，而不是插件模式自动赠送的保证。

## 第三层：一个消费者一个缓存，先解决“数据放哪儿”

现在假定相机 Reader 已经存在，网络接收回调刚拿到一个 `shared_ptr<Image>`。`shared_ptr` 是带引用计数的共享所有权句柄；复制它通常只增加引用计数，并不复制整张图像。多名消费者要拥有**独立的读取进度**，否则日志取走一帧，规划就再也读不到它。

对应的最小对象关系是：

```text
一帧 Image #42
      |
      v
DataDispatcher<Image>  [channel_id -> 缓存弱引用列表]
      |                  |
      v                  v
Component 的 ring     Reader 的 ring
      |                  |
DataVisitor cursor    Reader 的消费游标
```

真正的缓存是 `CacheBuffer`，一只由 `ChannelBuffer` 强持有。`DataDispatcher` 只保存缓存的 `weak_ptr`：登记关系不能阻止已删除消费者释放自己的 ring。固定源码 `DataVisitor<M0>` 构造时把同一 channel 的数据入口和通知入口**分别登记**：

```cpp
explicit DataVisitor(const VisitorConfig& configs)
    : buffer_(configs.channel_id, new BufferType<M0>(configs.queue_size)) {
  DataDispatcher<M0>::Instance()->AddBuffer(buffer_);
  data_notifier_->AddNotifier(buffer_.channel_id(), notifier_);
}

bool TryFetch(std::shared_ptr<M0>& m0) {  // NOLINT
  if (buffer_.Fetch(&next_msg_index_, m0)) {
    next_msg_index_++;
    return true;
  }
  return false;
}
```

`TryFetch` 并不让 producer 同步运行算法。它在消费者真正拿到执行机会之后，才携带自己的 `next_msg_index_` 去读 ring。`DataVisitor` 是消费视图，`CacheBuffer` 是存储，`DataDispatcher` 是扇出登记表：把三者混称“一只消息队列”，就看不到为什么多个消费者能够独立掉帧。

固定源码的 `DataDispatcher<T>::Dispatch` 将这三种职责切开：

```cpp
template <typename T>
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

现在第 42 帧在当前接收线程里同步写入每只仍存活的缓存，**随后**才发送一次 channel 更新事件。每个 `Fill` 持有的是该缓存自己的互斥锁；遍历总成本随历史登记数量增长，即便消息体只存在一份，`shared_ptr` 复制、锁竞争和多次槽位赋值也不是零开销。`CacheBuffer` 满时覆盖旧槽，`ChannelBuffer::Fetch` 会在读取位置落后时调整游标，因此队列上限保护内存，却不保证每帧都会进入业务。可以在[容量为 3 的 ring](pending-queue-ring.md)中手算精确边界。

这里的并发契约有一个实际缺口：`AddBuffer()` 在 `buffers_map_mutex_` 下追加内层 `vector`，但上面的 `Dispatch()` 不拿同一把锁就迭代它。`AtomicHashMap` 负责外层映射，不会让 map 值里的 `vector` 变成并发安全；**运行中创建 Reader 与接收线程重叠时，可能在扩容期间形成 C++ 数据竞争**。这不是所有运行都会复现的故障，但不能把“习惯在启动期注册”写成源码提供的通用热插拔保证。锁的具体范围、过期登记项和替代实现详见[Dispatcher 与 Notifier](dispatcher-notifier.md)。

## 第四层：通知只说“可能有活”，不运送消息

如果通知也携带图像，调度器就要理解图像类型，或者再建一套消息队列。更窄的接口是：`DataNotifier` 记录 channel 对应的无参 callback；当 Dispatcher 写完缓存，只通知相应任务**重新检查自己的缓存**。这使数次写入可以合并成一次唤醒，而消息数仍由 ring 的索引表示。

固定源码中的通知过程为：

```cpp
inline bool DataNotifier::Notify(const uint64_t channel_id) {
  NotifyVector* notifies = nullptr;
  if (notifies_map_.Get(channel_id, &notifies)) {
    for (auto& notifier : *notifies) {
      if (notifier && notifier->callback) {
        notifier->callback();
      }
    }
    return true;
  }
  return false;
}
```

关键不是函数只有几行，而是它**同步**执行 callback：调用它的仍是刚才完成缓存写入的接收线程。回调不能在这里运行 30 ms 的感知；它通常只把任务 ID 交给 Scheduler。`DataNotifier` 的 map 保存 `shared_ptr<Notifier>`，与 Dispatcher 保存的弱缓存引用不同，固定版本没有相应的注销接口，长期动态创建/删除 Reader 会累积旧 notifier。再加上 callback 绑定发生在 task 登记之后，启动期还存在“新消息到达而 callback 尚未绑定”的窄窗口。这两个实现边界不能被“有一个 Notifier 类”掩盖。

## 第五层：调度器只管任务，Processor 才拥有 OS 线程

此时已有两份完全不同的状态：缓存里存在第 42 帧；Notifier 把“这个 task 需要检查”送给 Scheduler。下一层需要定义一个长期存在的任务：`CRoutine` 包装无参任务函数与可恢复的用户栈；`Scheduler` 登记任务并交给调度策略；`ProcessorContext` 提供选任务/等待接口；`Processor` 拥有操作系统线程并执行主循环。这些名字可先翻成“工作、工作名册、分配策略、真正做事的工人”。

固定源码的 `Scheduler::CreateTask` 展示了类型化数据如何穿过执行边界：

```cpp
auto task_id = GlobalData::RegisterTaskName(name);

auto cr = std::make_shared<CRoutine>(func);
cr->set_id(task_id);
cr->set_name(name);

if (!DispatchTask(cr)) {
  return false;
}

if (visitor != nullptr) {
  visitor->RegisterNotifyCallback([this, task_id]() {
    if (cyber_unlikely(stop_.load())) {
      return;
    }
    this->NotifyProcessor(task_id);
  });
}
return true;
```

这里 `func` 已被 `RoutineFactory` 包装成无参、可循环取数的函数；`task_id` 则是从通知层进入调度层的稳定身份。lambda 捕获原始 `this` 和整数 ID，不拥有任务，也不携带消息。注册顺序是**先让 task 进入策略，再绑定通知 callback**；不能把这段源码理解成原子安装“任务 + callback”。

固定源码 `Processor::Run()` 才是 OS worker 上的主循环：

```cpp
while (cyber_likely(running_.load())) {
  if (cyber_likely(context_ != nullptr)) {
    auto croutine = context_->NextRoutine();
    if (croutine) {
      snap_shot_->execute_start_time.store(cyber::Time::Now().ToNanosecond());
      snap_shot_->routine_name = croutine->name();
      croutine->Resume();
      croutine->Release();
    } else {
      snap_shot_->execute_start_time.store(0);
      context_->Wait();
    }
  } else {
    std::unique_lock<std::mutex> lk(mtx_ctx_);
    cv_ctx_.wait_for(lk, std::chrono::milliseconds(10));
  }
}
```

`NextRoutine()` 不返回，或者返回空时没有执行 `Proc()`；`Wait()` 使没有工作可做的 OS worker 可以睡眠。只有选中了 READY routine，`Resume()` 才在**这只 Processor 线程**上切换用户态栈，进入 RoutineFactory 的取数循环。routine 执行 `Proc()` 后主动 `Yield()`，或者确认没有消息后让出；Linux 不会把每只 CRoutine 当成可以独立抢占的线程。

这给出另一条清楚的性能边界：高优先级 routine 无法抢占同一只 Processor 上正在执行的长 `Proc()`。Classic 的优先级影响**下一次选择**，不等同于 Linux 实时线程优先级。条件变量的唤醒也不等于代码已经运行；还要经过 OS 排队、线程恢复、任务扫描和 routine `Resume()`。`state_` 的并发窗口在[通知与 CRoutine 状态](croutine-wakeup.md)中讲解；从 READY 到实际 CPU 执行的下半段见[Processor 与切栈](processor-context-switch.md)。

## 第六层：把对象、编译依赖和执行线程画成三张不同的图

第一次读这套架构，最容易误把“谁调用谁”“谁拥有谁”和“哪个线程执行”画成同一张图。它们是不同问题。

### 模块与构建边界

```text
DAG/proto + mainboard
           |
     ClassLoader -----> ComponentBase / Component<M...>
                                  |
                               Node API
                                  |
                     +------------+-----------+
                     v                        v
                 Transport                  Scheduler
                     |                        |
                DataDispatcher <---+     RoutineFactory / CRoutine
                     |             |          |
                CacheBuffer   DataVisitor     ProcessorContext
                     |             |          |
                     +-----> DataNotifier     Processor
                                   |          ^
                                   +----------+
```

图中从 DataNotifier 到 Scheduler 的边是**运行时回调**，不能直接据此推断 data 源码静态依赖 scheduler：data 模块只看见 `std::function<void()>`。同理，算法只看 Node/Component 的公开接口，不应该包含某一个 SHM/RTPS 接收实现。变换部署和变换消息类型是两种独立的变化轴，分别靠运行时工厂与模板封装。

### 稳定态对象与所有权

```text
ModuleController
    └── strong ComponentBase
           ├── strong Node
           └── strong Reader(s)
    Scheduler
    └── strong CRoutine
           └── task 函数闭包 ─ strong DataVisitor
                                  └── strong ChannelBuffer / CacheBuffer
    DataDispatcher ── weak CacheBuffer
    DataNotifier   ── strong Notifier ─ callback(task_id, raw Scheduler*)
```

这张图专门暴露一个非对称性：弱缓存登记在最后一个强引用释放后自动失效，但 Notifier 强登记不跟着 DataVisitor 析构。运行时删除一个 task 与释放注册表条目不是同一件事。对长期运行并频繁重载组件的系统，这会影响内存和每帧的通知遍历成本；它还要求组件和 Scheduler 的关闭顺序保证旧回调不会访问已析构对象。

### 谁在什么线程上执行

```text
Writer 调用线程或 transport 接收线程
    → Receiver listener → DataDispatcher::Dispatch
    → Fill（分别锁住各个 CacheBuffer）
    → DataNotifier::Notify
    → Scheduler::NotifyProcessor → ClassicContext::Notify
                                          |
                                    notify_one()
                                          |
Processor OS thread（被 Linux 调度后）
    → ClassicContext::NextRoutine
    → CRoutine::Resume / SwapContext
    → DataVisitor::TryFetch → Component::Process → Proc
```

INTRA 在调用线程上同步分发，SHM 有自己的通知/分发线程，RTPS 则进入第三方传输库 listener 所在的执行上下文；它们在写入 DataDispatcher 后才汇合。**一张“消息路径箭头图”如果不标线程切换点，就不足以分析 jitter。** 传输支路及反序列化次数在[完整消息链](message-to-proc.md)单独核对。

## 用第 42 帧回放一次真实运行

以下时间只是**教学时序**，不是对 Apollo 测得的延迟或线程执行顺序作保证。假设上一轮感知已在等待数据，CPU 恰好空闲：

| 时刻 | 当前执行者 | 发生的动作 | 第 42 帧的位置 |
| --- | --- | --- | --- |
| t0 | transport 回调线程 | Receiver 把 `shared_ptr<Image>` 交给 Dispatcher | 回调局部变量 |
| t1 | 同一线程 | `Dispatch` 遍历存活缓存并各自 `Fill` | 多只 ring 引用同一对象 |
| t2 | 同一线程 | `Notify(channel_id)` 同步调用注册的无参 callback | 仍留在 ring |
| t3 | 同一线程 | Scheduler 记录更新，并通知对应 group 的 worker | 仍留在 ring |
| t4 | Processor OS 线程 | 等待返回；`NextRoutine` 扫描并更新 routine 状态 | 仍留在 ring |
| t5 | 同一 Processor 线程 | `Resume` 恢复 routine 栈，`TryFetch` 按私有游标读取 | ring 与局部句柄共享 |
| t6 | 同一 Processor 线程 | `Component::Process` 调用业务 `Proc` | 由业务继续持有句柄 |

这张表有四个必须核对的例外。第一，消息可能因为满 ring 被覆盖，所以收到 notify 不保证取到第 42 帧。第二，多个通知可能合并，不能把“一条消息 = 一次线程唤醒”当成契约。第三，固定源码里 `CRoutine::state_` 是普通枚举，通知线程读取和 Processor 线程写入没有共同锁；`atomic_flag` 只保证自身的原子读改写，不能替整个状态机证明无竞态。第四，如果业务 `Proc` 长时间不 `Yield`，其他 routine 即使已经 READY 仍可能等待下一次选择。

## 关闭必须按“先禁止入口、再等待在途任务、最后卸载代码”思考

固定源码 `ComponentBase::Shutdown()` 给出的真实次序是：

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

`is_shutdown_` 拒绝随后进入 `Process` 的调用，但**不自动等待已经进入 `Proc` 的任务**；后面的 `RemoveTask` 才会与任务执行状态协调。由于派生 `Clear()` 实际排在 `RemoveTask` **之前**，如果它释放了在途 `Proc` 正在使用的成员，会留下业务级生命周期风险。`ModuleController::Clear()` 在调用组件 `Shutdown` 后清空组件表，最后才 `UnloadAllLibrary()`，这一层维护的是对象先于动态库的销毁顺序。读者应该分别检查业务资源、CRoutine、接收端和动态库，而不是把“有 Shutdown 函数”当作统一的安全保证。

## 从这张骨架进入源码专题

本页只完整讲清模块边界和一条单输入消息的主要因果关系，细节留在真正发生状态转换的位置：先读[从 DAG 到 Component](dag-to-component.md)与[端点创建](node-reader-writer.md)确定第一帧到来前的对象；再读[ring 的槽位与游标](pending-queue-ring.md)、[Dispatcher 的注册与并发](dispatcher-notifier.md)和[多输入 AllLatest](multi-input-fusion.md)确定数据语义；最后顺次进入[任务状态与唤醒](croutine-wakeup.md)、[Processor 的执行循环](processor-context-switch.md)与[端到端消息链](message-to-proc.md)。

如果打算手写一个缩小版，不要先移植 Apollo 的所有目录。先用 **Node API → 每消费者有界 ring → 无参通知 → 单线程 worker → 可删除 task** 五个最小对象把语义和关闭写对，再添加多输入快照、固定线程池、协程切栈、动态装配和多种 transport。每增加一层，都写一个能观察失败的实验：故意让消费速度低于生产速度、在通知窗口注册新消费者、运行中移除 task、在 `Proc` 内阻塞、在部分初始化失败时退出。这样设计出来的“模块图”才能最终落回可以独立验证的代码。
