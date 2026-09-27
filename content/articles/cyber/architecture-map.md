# 从空目录设计 Cyber RT：模块骨架、源码地图与运行时架构

本文中的 Apollo 实际实现统一按固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 核验。后文“固定提交源码摘录”均直接粘贴自该版本；正文不使用源码链接或仓库文件路径代替代码。

这次先不从 `DataDispatcher`、`CRoutine` 或设计模式名字开始。把自己放到 Cyber RT 源码作者的位置：手里只有一批感知、定位、规划和控制算法，目标是从空目录写出一套能装配、通信、调度、关闭和排障的运行时。每增加一个模块，都必须由上一版的具体失败推出；每个抽象都要落到头文件、实现文件、数据结构和线程边界。

本文先搭出可以开始编码的骨架，再把骨架逐项映射到固定 Apollo 提交。后续专题负责深入单条链路，但读者在进入任何细节前，应先能回答四个问题：仓库为什么这样分目录，模块之间允许怎样依赖，运行时会创建哪些对象和线程，以及第一行代码应该从哪里写起。

## 使用场景与系统边界

Cyber RT 适合自动驾驶和类似的多传感器计算流水线：组件通过 DAG 装配，Node/Reader/Writer 传递类型化消息，DataVisitor 组合多输入，Scheduler 用协作式 routine 执行业务，Recorder/Player 支撑复现。它强调的是“通信、装配与执行一体化运行时”。

先把这一串源码名翻译成普通角色。DAG 是描述动态库、组件实例和输入 channel 的部署配置，不是把算法对象直接连起来的 C++ 图；Node 是带名字的通信实体工厂，Reader/Writer 分别表示某条 channel 的读端和写端；DataVisitor 是某个消费者自己的缓存视图；Scheduler 管理可运行任务，而 CRoutine 是能暂停、以后再从原位置继续的用户态任务。Recorder/Player 则把 channel 数据写入文件并重新送回运行时，用于复现输入。后文再展开类和字段时，这些名字始终落在“装配、收发、存数据、安排执行、回放”五类职责中。

它不自动保证传感器严格时间同步、业务命令恰好一次、算法 WCET 或硬实时调度。WCET 是 worst-case execution time，即一次业务计算可能持续的最长时间；没有这个上界，就无法仅凭平均耗时证明截止期。INTRA 是同进程内的直接传递路径；SHM（shared memory，共享内存）让同机进程访问共享映射区；RTPS（Real-Time Publish-Subscribe）是面向网络发布订阅的协议路径。它们都只是 transport 的一段，因此 SHM 不等于整条链零复制。协程减少线程数但不抢占长时间 Proc；录制输入也不自动消除墙钟、随机数和外部设备带来的非确定性。

下面先把用户代码、中间件和 Linux 放进一张可维护的分层图。箭头表示一次消息处理会穿过的主要边界，不表示所有模块都只能单向调用：

:::{mermaid}
flowchart TB
  APP[业务 Component<br/>Init 与 Proc]
  API[Node / Reader / Writer<br/>类型化 public API]
  CTRL[mainboard / DAG / ClassLoader<br/>装配与生命周期]
  DATA[DataDispatcher / CacheBuffer / DataVisitor<br/>数据面]
  EXEC[DataNotifier / Scheduler / CRoutine / Processor<br/>执行面]
  TRANS[INTRA / SHM / RTPS<br/>传输适配]
  OS[Linux<br/>进程与线程调度；futex 内核等待/唤醒；mmap 内存映射；socket 通信端点；dlopen 动态库加载]

  CTRL --> APP
  APP --> API
  API --> TRANS
  TRANS --> DATA
  DATA --> EXEC
  EXEC --> APP
  CTRL --> EXEC
  TRANS --> OS
  EXEC --> OS
:::

这张图里最关键的是环而不是层数：Component 通过 API 声明通信，transport 把外部数据交给 data，data 只保存消息并发事件，execution 最后再次进入 Component。mainboard 则在第一条消息前创建整个环，并在关闭时按反方向拆除它。图里的 Linux 名词分别标出这些跨层边界：`socket` 是内核管理的网络通信端点，`mmap` 把文件或共享内存映射进进程地址空间，`dlopen` 在运行期装入动态库；`futex` 是 Linux 用于用户态同步对象等待/唤醒的内核机制，Cyber 的线程等待通常经由 C++ 条件变量等封装使用它，而不是业务代码直接调用 futex。后文会在实际源码出现这些机制时再展开其代价。

## 从机器人约束推导最小运行时

前面的场景给出了一组不能靠单个 callback 满足的约束。下面先只推导必要的模块和数据结构，再把它们映射到 Apollo 的目录与类；这样读者先知道每个对象是为哪一个缺口出现，看到真实类名时就不会把它们当成孤立清单。

### 先写约束，不急着写类

源码作者首先面对的不是类图，而是五条互相冲突的约束：

1. 算法只声明输入输出，不能绑定某种 socket 或某只接收线程；
2. 同一 channel 可以有多个消费者，慢消费者不能移动别人的读取位置；
3. 接收线程不能执行不可控的感知或规划计算；
4. 内存必须有上界，过载时要明确阻塞、丢旧还是丢新；
5. 组件、任务、通信端点和动态库必须按可证明的顺序关闭。

这五条约束分别逼出通信门面、每消费者缓存、执行器、有界数据结构和生命周期管理。后文所有模块都应能回指其中至少一条；不能回指需求的抽象，暂时不该进入第一版。

### 直接 callback 能运行，但不能成为中间件

最小版本只需保存 callback，发布时同步遍历。下面是故意保留缺陷的**错误示例**：

```cpp
template <class T>
class DirectChannel {
 public:
  using Callback = std::function<void(std::shared_ptr<const T>)>;

  void Subscribe(Callback cb) { callbacks_.push_back(std::move(cb)); }

  void Publish(std::shared_ptr<const T> msg) {
    for (auto& cb : callbacks_) {
      cb(msg);  // publisher 线程直接执行业务
    }
  }

 private:
  std::vector<Callback> callbacks_;
};
```

它在单线程演示中成立，一进入车辆程序就暴露架构问题：30 ms 的感知 callback 会让发布者停 30 ms；遍历期间增删订阅者会破坏 vector；callback 保存裸 `this` 时可能在组件析构后调用悬空对象；跨进程后 `shared_ptr` 地址没有意义；生产速度超过消费速度时也没有地方表达容量和丢弃策略。

因此第一次重构不是“加入更多设计模式”，而是把一条同步调用拆成三种责任：数据放在哪里、事件怎样通知、业务由谁执行。

### 从失败点切出运行时模块与 schema

从失败点反推，第一份可维护目录不需要复刻 Apollo 全仓库，但应先固定依赖方向：

```text
mini_cyber/
|-- api/          Node、Reader、Writer；业务只依赖这一层
|-- transport/    intra/shm/rtps adapter；只负责把 bytes 变成消息
|-- data/         channel registry、per-consumer ring、visitor
|-- execution/    notifier、task、worker、调度策略
|-- component/    业务生命周期与输入输出绑定
|-- runtime/      配置解析、装配、启动和逆序关闭
`-- proto/        配置与跨进程消息 schema
```

依赖只能向内收敛：`api` 可以调用 data/transport，transport 只能把消息交给 data，data 可以发布无类型事件给 execution；execution 不应反向包含具体 protobuf 类型，业务 Component 也不应 include SHM 或 RTPS 实现头。这样替换 transport 不会改业务，替换 scheduler 不会改缓存。

对照固定源码，公开门面由 Node、Reader、Writer 承担；INTRA、SHM、RTPS 适配由 transport 对象承担；缓存和分发由 CacheBuffer、ChannelBuffer、DataDispatcher 与 DataVisitor 承担；执行面由 CRoutine、Scheduler、Processor 协作；启动装配则由 mainboard、Component 和 ClassLoader 完成。源码模块的边界不是分类标签，而是编译依赖防火墙。

:::{mermaid}
flowchart LR
  MB[mainboard] --> CL[class_loader]
  MB --> COMP[component]
  CL --> COMP
  COMP --> NODE[node]
  COMP --> DATA[data]
  COMP --> SCHED[scheduler]
  NODE --> TRANS[transport]
  NODE --> DATA
  SCHED --> CROUTINE[croutine]
  SCHED --> DATA
  TRANS --> DATA
  TRANS --> TOPO[service_discovery]
  NODE --> TOPO
  PROTO[proto 配置] --> MB
  PROTO --> COMP
  PROTO --> TRANS
  PROTO --> SCHED
:::

这张图的实线只表达源码层的主要使用/组装方向，不等同于稳定态消息流。`Scheduler` 通过 `RoutineFactory/DataVisitorBase` 使用 data 抽象；data 模块并不 include Scheduler。运行时则由 transport 调用 DataDispatcher，DataNotifier 再调用 Scheduler 注入的无类型 callback，这条事件回边将在后面的时序图中单独画出。把编译依赖和 callback 流分开，才能解释 transport 为什么无需反向 include 具体 Component。

### 先写接口骨架，再决定实现

下面是**教学骨架**，不是 Apollo 原样源码。它先把数据面与执行面隔开：

```cpp
using ChannelId = std::uint64_t;
using TaskId = std::uint64_t;

template <class T>
class Buffer {
 public:
  virtual void Push(std::shared_ptr<const T>) = 0;
  virtual bool TryPop(std::shared_ptr<const T>&) = 0;
  virtual ~Buffer() = default;
};

class Executor {
 public:
  virtual TaskId Add(std::function<void()> step) = 0;
  virtual void Notify(TaskId) = 0;
  virtual void Remove(TaskId) = 0;
  virtual ~Executor() = default;
};
```

`Buffer<T>` 保留编译期消息类型，避免 `void*` 和运行时强转；`Executor` 只看无参任务和整数身份，不依赖任意消息类型。连接两者的 closure 捕获 visitor：任务运行时再从 buffer 取数据。Cyber 的 `DataVisitor<M...>`、`RoutineFactory` 和 `CRoutine` 正是在更完整的实现中完成这次类型擦除。

### 数据结构由不变量决定

一个 channel 多个消费者时，不能只有一只 destructive queue（取出即从队列删除的队列）。每位消费者需要独立游标或独立 ring；全局 registry 又不能强行延长消费者寿命。下面是**教学最小示例**，用来表达这些不变量，不是 Apollo 原样源码：

```cpp
template <class T>
struct Subscription {
  std::mutex mutex;
  std::vector<std::shared_ptr<const T>> slots;
  std::uint64_t head = 0;
  std::uint64_t tail = 0;
  std::uint64_t cursor = 0;
};

template <class T>
using Registry =
    std::unordered_map<ChannelId,
                       std::vector<std::weak_ptr<Subscription<T>>>>;
```

`vector` 适合固定容量 ring，因为构造后槽位连续且不再扩容；`weak_ptr` 让长寿命 registry 能找到订阅者，却不决定其析构时刻；每只订阅缓存一把 mutex，把 head、tail、槽位和读取判断放进同一临界区。若运行中允许注册，外层 vector 的扩容必须另有同步或快照；若要求无锁，则槽位发布、覆盖和对象回收都要重新设计内存序，不能只删掉 mutex。

固定源码对应 `CacheBuffer<shared_ptr<T>>`、`ChannelBuffer` 与 `DataDispatcher<T>::BufferVector`。专题页会逐行证明覆盖和游标语义；架构层先记住选择依据：状态流要有界且偏向最新值，长寿命基础设施不能拥有短寿命业务对象。

### 直到 callback 必须隔离时才引入线程

第一版先用一只操作系统（operating system，OS）worker 线程，不急着实现协程。producer 在同一把 mutex 下写 ready queue 和 `stopping` 状态，再调用 `notify_one()`；worker 用谓词等待。下面是**教学伪代码**：省略队列字段和 `HasReadyTask()`/`SelectReadyTask()` 的实现，重点展示等待、停止和执行之间的锁边界。这个骨架明确选择“停止时排空已就绪任务，然后退出”的策略：

```cpp
void WorkerLoop() {
  for (;;) {
    std::function<void()> task;
    {
      std::unique_lock<std::mutex> lock(mutex);
      cv.wait(lock, [&] { return stopping || HasReadyTask(); });
      if (stopping && !HasReadyTask()) {
        return;  // 已停止接收新任务，也已排空 ready queue
      }
      task = SelectReadyTask();
    }  // 离开作用域释放 mutex

    if (task) {
      task();  // 业务代码绝不在队列锁内执行
    }
  }
}
```

这里第一次需要操作系统知识。mutex 同时保护 ready queue 与 `stopping` 谓词，避免 worker 检查完队列后、真正睡眠前 producer 改变条件却没有留下可观察状态。线程调用 `wait` 时会原子地释放 mutex 并进入阻塞；通知只让它变成 runnable，何时获得 CPU（central processing unit，中央处理器）仍由内核调度器决定。Linux 的常见实现会让无竞争 mutex 走用户态原子快路径，竞争等待再借助 futex 类系统调用睡眠。业务必须在锁外运行，否则 producer 会再次被长 callback 阻塞。若系统要求“停止即丢弃排队任务”，就要把 drain 条件改成显式 cancel/clear 协议，不能省略停止语义后仍无条件调用可能为空的 task。

当 task 数量增长后，才有理由把“一任务一线程”改成“少量 Processor 内核线程承载大量 CRoutine”。这是资源模型优化，不是正确性的起点：先用普通线程把队列谓词、关闭和所有权写对，再引入用户态上下文切换。

## 把推导结果映射到真实源码模块

前面得到的是一套最小设计，不是对 Apollo 历史开发过程的断言。现在换一个方向核验它：Cyber RT 的真实目录、核心类型和依赖关系分别承担了哪些职责；下表中的设计动机是从工程约束推导，源码落点则以固定提交为准。

### 从自动驾驶需求反推组件

| 需求 | 首先进入的组件 | 必须联读的边界 |
|---|---|---|
| 按车型/场景装配算法模块 | DAG、mainboard、ModuleController、ClassLoader | 插件 ABI、实例与 DSO 寿命 |
| 相同 API 覆盖进程内和跨进程 | Reader/Writer、Transport、ReceiverManager | INTRA/SHM/RTPS 的复制与发现差异 |
| 一个 channel 多个消费者互不移动游标 | DataDispatcher、ChannelBuffer | `O(K)` 扇出、容量与覆盖 |
| 多传感器输入触发一次算法 | DataVisitor、AllLatest | 主触发源、辅助数据年龄、非严格同步 |
| 回调不阻塞网络接收 | DataNotifier、CRoutine、Scheduler、Processor | 唤醒不丢失、Proc WCET、协作式饥饿 |
| 可记录和回放线上输入 | Record/Player、Channel、Clock | 回放时间、拓扑和外部副作用 |
| 运行期查看数据链健康 | Topology、Monitor、statistics | 可见不等于新鲜或处理完成 |

定位故障时先选择平面：组件没创建看装配；Reader 存在但收不到看拓扑/Transport；Dispatcher 有数据但 Proc 不运行看 Notifier/Scheduler；Proc 运行但输入陈旧看 ChannelBuffer/DataVisitor；回放结果不同看 Clock、外部状态与算法确定性。

这张需求表已经回答“为什么要有这些能力”；后面的源码地图只再回答一个问题：能力具体落在哪些目录、接口和配置文件。保留这一层分工，就不需要再用另一张功能清单重复列一遍同样的六类职责。

### 仓库入口与源码问题

下面这张表不是文件导航，而是把从零设计时必须同时检查的代码层次、实际对象和设计文件类型列在一起；后文直接粘贴关键实现，不要求读者按路径跳转：

先区分四类文件。源码作者不是只写 `.cc`；配置 schema、部署实例和构建边界共同定义架构：

| 层次 | 需要一起理解的源码对象或配置概念 | 它决定什么 |
|---|---|---|
| public C++ interface | Node、Reader、Writer、Component | 业务可依赖的类型、数据入口与生命周期 |
| runtime implementation | DataDispatcher、CacheBuffer、DataVisitor、Scheduler、Processor、transport receiver | 队列、锁、线程、复制和错误路径 |
| design/config schema | DAG 配置、Component 配置、Scheduler 配置 | 哪些架构选择可以在部署时改变 |
| concrete deployment | 组件装配声明与运行参数 | 实际组件、channel、queue depth、group 与 CPU 配置 |
| build boundary | 模块构建目标、公开头文件与链接依赖 | 哪个模块公开接口、组合哪些实现和第三方库 |

读一个模块时应把这几类信息并排理解。例如只看 `SchedulerClassic::NotifyProcessor()`，无法知道 task priority、group 和 Processor policy 从哪里配置；只看一份组件装配声明，也无法知道 `pending_queue_size` 最终怎样改变 ring。配置字段与运行时对象必须沿同一个字段语义追踪。

| 层 | 关键类、函数或机制 | 先回答的问题 |
|---|---|---|
| 启动装配 | mainboard、ModuleController、ClassLoader | DAG 如何变成装载批次与 Component 实例 |
| 组件模板 | `Component<M0...M3>::Initialize()` | 不同输入数如何生成 Reader、Visitor 与 routine |
| 节点实体 | Node、Reader、Writer | 业务 API 怎样进入 Transport 和 Dispatcher |
| Transport | Receiver、Dispatcher、INTRA/SHM/RTPS listener | Receiver 共享、传输回调与线程边界 |
| 数据缓存 | CacheBuffer、ChannelBuffer、DataDispatcher | 每消费者 ring、游标和扇出 |
| 多输入 | DataVisitor、AllLatest | 主输入触发与辅助输入快照 |
| 通知 | DataNotifier | channel 事件怎样合并并通知 routine |
| 调度 | SchedulerClassic、ClassicContext、Processor | routine 状态、运行表和 OS worker |
| 插件 | ClassFactory、ClassLoader、ModuleController | 工厂注册、DSO pinning 和卸载 |

每读一段代码，都同步记录线程、owner、`shared_ptr` 复制、锁域和关闭动作。Cyber 的核心问题跨越多个模块，只搜索 `Proc()` 会跳过决定数据何时可见的全部运行时层。

### 模块依赖顺序

```text
DAG/Launch
  -> ClassLoader 创建 Component
       -> Component 创建 Node / Reader / Writer
            -> Transport 建立 Receiver
                 -> DataDispatcher 写 CacheBuffer
                 -> DataNotifier 发布可合并事件
       -> Scheduler 记录更新并通知 ProcessorContext
            -> ClassicContext 重新选择 READY CRoutine
                                  -> DataVisitor 读取消息快照
                                       -> Component::Proc()
```

这条顺序解释章节为什么必须从装配进入通信，再进入缓存、融合和调度。只单独解释 `CRoutine` 或 SHM，读者无法知道它们在完整系统中解决哪一段问题。

:::{mermaid}
sequenceDiagram
  participant MB as mainboard
  participant MC as ModuleController
  participant C as ComponentM0
  participant R as ReaderM0
  participant W as WriterT
  participant HT as HybridTransmitterT
  participant RX as Receiver
  participant DD as DataDispatcherM0
  participant CB as CacheBufferM0
  participant N as DataNotifier
  participant DV as DataVisitorM0
  participant S as Scheduler
  participant CC as ClassicContext
  participant CR as CRoutine
  participant P as Processor

  MB->>MC: LoadAll(DagConfig)
  MC->>C: CreateClassObj + Initialize
  C->>C: 调用业务 Init()
  opt 业务 Init 创建输出端
    C->>W: Node::CreateWriter(output_cfg)
    W->>HT: Init -> CreateTransmitter
  end
  C->>R: Node::CreateReader(reader_cfg)
  R->>DV: create reader visitor + reader task
  R->>RX: ReceiverManager.GetReceiver
  R->>RX: JoinTheTopology / Enable peer
  C->>DV: create component visitor
  C->>S: CreateTask(component RoutineFactory)
  Note over W,HT: topology change enables mode for each matched Reader
  C->>W: Write(shared_ptr msg)
  W->>HT: Transmit(msg)
  HT->>HT: iterate constructed mode transmitters
  HT-->>RX: enabled INTRA / SHM / RTPS transmitter(s)
  RX->>DD: Dispatch(channel_id, msg)
  DD->>CB: Fill(shared_ptr<Message>) for each live consumer buffer
  DD->>N: Notify(channel_id)
  N->>S: notifier callback -> NotifyProcessor(task_id)
  S->>CR: 若等待态，SetUpdateFlag()
  S->>CC: Notify(group)
  CC-->>P: condition_variable.notify_one()
  P->>CC: NextRoutine()
  CC->>CR: Acquire() + UpdateState()
  CR-->>CC: READY
  CC-->>P: 返回选中的 routine
  P->>CR: Resume() / SwapContext()
  CR->>DV: TryFetch() -> ChannelBuffer::Fetch()
  DV-->>C: Process(msg) -> Proc(msg)
:::

时序图把启动期与稳定期接在一起，但 Writer 是可选的业务输出端：固定版本的 `Component<M0>::Initialize()` 先调用派生类 `Init()`，业务代码可以在其中通过 Node 创建 Writer；框架随后创建 Reader、Reader visitor 和 Component visitor，再建立 Component task。Reader 初始化可能在组件 visitor 注册前就启用已存在的 Writer，所以对端若已在发送，接收与第二只 buffer 注册之间存在需要同步约束的窗口。固定版本的 `Writer::Write(shared_ptr)` 进入 `transmitter_->Transmit`；`HybridTransmitter` 根据 topology 中对端与本端的主机、进程关系 enable 对应 mode，但每次发送仍遍历已构造的 mode transmitter。哪些实现真正送达由各 transmitter 的 enable/peer 状态决定，并不是每条消息经过一次显式 switch 只选一条分支。

图中先把传输汇合为 Receiver 以突出共同数据路径；具体执行上下文并不相同：INTRA transmitter 在调用线程中同步调用 dispatcher；SHM 由 `ShmDispatcher::ThreadFunc()` 所在线程监听共享内存通知并分发；RTPS 则由 Fast RTPS subscriber listener 回调进入 Cyber 的 `RtpsDispatcher`，该回调运行于传输库的接收执行上下文，不应误画成 Cyber 为每个 Reader 新建的线程。可对照固定源码：INTRA 同步 dispatch、SHM dispatcher 线程、RTPS listener 到 dispatcher。

消息进入 data 面后，Dispatcher 写入的是每个消费者注册的 `CacheBuffer`，不是直接调用 `DataVisitor`；routine 恢复后才通过 visitor 的 `ChannelBuffer::Fetch()` 读取 payload。调度细节也可沿固定源码逐跳核对：Dispatcher 填缓存并发通知、Scheduler 更新等待 routine 并通知 ClassicContext、ClassicContext 等待与唤醒/选择、Processor 选择并恢复 routine、CRoutine::Resume、DataVisitor 创建与读取 CacheBuffer。

### 从启动配置走到稳定态消息链

序列图把系统分成两个时间尺度。启动时，`ModuleController` 按 DAG 选择动态库与组件；Component 建立 Node，业务 `Init()` 可创建 Writer，框架再创建 Reader、visitor 和调度任务。稳定运行后，一帧图像经过 transport，进入 Dispatcher 为每个消费者维护的缓存；Notifier 只报告更新，Scheduler 置更新标记并通知 Context，Processor 再选择 READY routine，协程恢复后 DataVisitor 才读取 payload 并进入 `Proc()`。关闭时 `ComponentBase::Shutdown()` 的源码次序是设置关闭标志、调用派生 `Clear()`、关闭 Reader，最后才移除并等待 Component task。这个等待能为后续对象析构和动态库卸载建立边界，但发生在 `Clear()` 之后；若派生 `Clear()` 释放了在途 `Proc()` 正在使用的成员，仍需业务侧同步或重新设计两阶段关闭。详见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

多输入组件仍沿用这条主线，只在读取边界上增加一步：主输入决定何时执行，`AllLatest` 把其他输入当时的最新值固定成一组参数；这降低等待，却不等同于按时间戳同步。C++ 设计也服务于这些边界：模板保留消息类型，运行时基类承接异构组件，智能指针表达共享或非拥有关系，而 RAII 与关闭顺序共同限制资源寿命。详细语法和源码分别在端点、类型系统与调度专题中展开。

### 把启动图还原为固定提交中的代码

前面的时序图不是只靠类名拼出的概念图。固定提交里的 `ModuleController::LoadModule()` 确实按配置装入动态库、按类名创建 `ComponentBase`，并在初始化成功后把组件放进自己的强引用容器。下面直接看这段**固定提交源码摘录**（省略错误分支日志，代码本身未改写）：

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

这里的 `component_list_` 不只是“记录日志”的清单：它让组件对象在 `ModuleController` 管理期保持存活。`std::move(base)` 把局部强引用句柄移入 vector，局部变量不再代表另一个所有者；若初始化失败，循环立刻返回，不会把半初始化对象加入列表。只有理解这段装配代码，后面的 `Node`、Reader 和调度 task 才不会像在消息到达时凭空出现。

组件本身再把 DAG 的 `ComponentConfig` 变成通信端点和执行任务。下面是 `Component<M0>::Initialize()` 的**固定提交源码连续摘录**（代码未改写）：

```cpp
template <typename M0>
bool Component<M0, NullType, NullType, NullType>::Initialize(
    const ComponentConfig& config) {
  node_.reset(new Node(config.name()));
  LoadConfigFiles(config);

  if (config.readers_size() < 1) {
    AERROR << "Invalid config file: too few readers.";
    return false;
  }

  if (!Init()) {
    AERROR << "Component Init() failed.";
    return false;
  }

  bool is_reality_mode = GlobalData::Instance()->IsRealityMode();

  ReaderConfig reader_cfg;
  reader_cfg.channel_name = config.readers(0).channel();
  reader_cfg.qos_profile.CopyFrom(config.readers(0).qos_profile());
  reader_cfg.pending_queue_size = config.readers(0).pending_queue_size();

  auto role_attr = std::make_shared<proto::RoleAttributes>();
  role_attr->set_node_name(config.name());
  role_attr->set_channel_name(config.readers(0).channel());

  std::weak_ptr<Component<M0>> self =
      std::dynamic_pointer_cast<Component<M0>>(shared_from_this());
  auto func = [self, role_attr](const std::shared_ptr<M0>& msg) {
    auto start_time = Time::Now().ToMicrosecond();
    auto ptr = self.lock();
    if (ptr) {
      ptr->Process(msg);
    } else {
      AERROR << "Component object has been destroyed.";
    }
    auto end_time = Time::Now().ToMicrosecond();
    // sampling proc latency and cyber latency in microsecond
    uint64_t process_start_time;
    statistics::Statistics::Instance()->SamplingProcLatency<
                        uint64_t>(*role_attr, end_time-start_time);
    if (statistics::Statistics::Instance()->GetProcStatus(
          *role_attr, &process_start_time) && (
                        start_time-process_start_time) > 0) {
      statistics::Statistics::Instance()->SamplingCyberLatency(
                        *role_attr, start_time-process_start_time);
    }
  };

  std::shared_ptr<Reader<M0>> reader = nullptr;

  if (cyber_likely(is_reality_mode)) {
    reader = node_->CreateReader<M0>(reader_cfg);
  } else {
    reader = node_->CreateReader<M0>(reader_cfg, func);
  }

  if (reader == nullptr) {
    AERROR << "Component create reader failed.";
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
}
```

这段代码把几个设计选择放在了同一条生命线上。`Init()` 先运行，所以业务组件可以通过 `node_` 创建自己的 Writer；框架随后按 DAG 创建 Reader。供 routine 执行的 `func` 捕获 `weak_ptr` 而不是强持有 Component：若改成强引用，组件持有 task、task 闭包又持有组件，就会互相延长寿命，单靠清空组件列表无法析构。每次任务执行再 `lock()` 临时取得强引用，若组件已经结束生命周期便不再调用 `Process()`。真实模式下，Reader 和 Component task 各自建立 visitor/routine；测试模式则把 callback 交给 Reader，二者并非同一条调度路径。

### 消息先写缓存，协程稍后才读取

前面图中的“数据面”和“执行面”在源码里也由不同对象完成。`DataDispatcher<T>::Dispatch()` 只遍历当前仍然存活的缓存引用、写入消息，然后发出 channel 通知；它不调用 `DataVisitor::TryFetch()`，也不在这里执行业务 `Proc()`。以下为该函数的**固定提交源码连续摘录**（代码未改写）：

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

`buffer_wptr.lock()` 是一次有条件的所有权提升：消费者 visitor 还活着才会拿到临时 `shared_ptr` 并写缓存，已销毁消费者对应的过期弱引用不会延长缓存生命。每一只缓存由自己的 mutex 保护；Dispatcher 把同一消息对象扇出到多个 CacheBuffer，不会把 payload 复制进 scheduler 的任务队列。这里通知的是“这个 channel 有新数据”，不是“业务回调已经运行”。

消费者游标在 `ChannelBuffer::Fetch()` 中修正。以下为该函数的**固定提交源码连续摘录**（代码未改写）：

```cpp
template <typename T>
bool ChannelBuffer<T>::Fetch(uint64_t* index,
                             std::shared_ptr<T>& m) {  // NOLINT
  std::lock_guard<std::mutex> lock(buffer_->Mutex());
  if (buffer_->Empty()) {
    return false;
  }

  if (*index == 0) {
    *index = buffer_->Tail();
  } else if (*index == buffer_->Tail() + 1) {
    return false;
  } else if (*index < buffer_->Head()) {
    auto interval = buffer_->Tail() - *index;
    AWARN << "channel[" << GlobalData::GetChannelById(channel_id_) << "] "
          << "read buffer overflow, drop_message[" << interval << "] pre_index["
          << *index << "] current_index[" << buffer_->Tail() << "] ";
    *index = buffer_->Tail();
  }
  m = buffer_->at(*index);
  return true;
}
```

首次读取时游标 `0` 被设为当前 tail；若游标已经追上 tail 后一格，就表示暂时没有新消息；若 buffer 的 head 已越过这个消费者的游标，消费者已落后到被覆盖的数据之外，代码把它推进到最新可读 tail，再复制一份 `shared_ptr` 句柄给调用者。DataVisitor 的 `TryFetch()` 正是在成功后递增自己的 `next_msg_index_`。因此图中的时序必须是 `Dispatch -> CacheBuffer::Fill -> Notify -> Scheduler/Processor -> DataVisitor::TryFetch`，不能画成 Dispatcher 直接把消息交给 visitor。

### 通知、状态变更、线程运行与协程恢复是四步

`SchedulerClassic::NotifyProcessor()` 的固定源码显示，事件到达首先只找到 routine；routine 若处于等待态就设置 update flag，然后通知它所属 group 的 ClassicContext。它本身不把 routine 直接改成 READY，也不调用 `Resume()`。以下为该函数的**固定提交源码连续摘录**（代码未改写）：

```cpp
bool SchedulerClassic::NotifyProcessor(uint64_t crid) {
  if (cyber_unlikely(stop_)) {
    return true;
  }

  {
    ReadLockGuard<AtomicRWLock> lk(id_cr_lock_);
    if (id_cr_.find(crid) != id_cr_.end()) {
      auto cr = id_cr_[crid];
      if (cr->state() == RoutineState::DATA_WAIT ||
          cr->state() == RoutineState::IO_WAIT) {
        cr->SetUpdateFlag();
      }

      ClassicContext::Notify(cr->group_name());
      return true;
    }
  }
  return false;
}
```

通知只让等待中的操作系统线程有机会继续检查，不代表 routine 已经开始运行。`ClassicContext::Wait()` 用 group 的通知计数作谓词，并以带超时的条件变量等待；`NextRoutine()` 之后扫描该策略队列、取得 routine 执行权，再调用 `UpdateState()` 检查更新标记并决定是否已 READY。以下连续源码摘录覆盖选择、等待和通知（代码未改写）：

```cpp
std::shared_ptr<CRoutine> ClassicContext::NextRoutine() {
  if (cyber_unlikely(stop_.load())) {
    return nullptr;
  }

  for (int i = MAX_PRIO - 1; i >= 0; --i) {
    ReadLockGuard<AtomicRWLock> lk(lq_->at(i));
    for (auto& cr : multi_pri_rq_->at(i)) {
      if (!cr->Acquire()) {
        continue;
      }

      if (cr->UpdateState() == RoutineState::READY) {
        return cr;
      }

      cr->Release();
    }
  }

  return nullptr;
}

void ClassicContext::Wait() {
  std::unique_lock<std::mutex> lk(mtx_wrapper_->Mutex());
  cw_->Cv().wait_for(lk, std::chrono::milliseconds(1000),
                     [&]() { return notify_grp_[current_grp] > 0; });
  if (notify_grp_[current_grp] > 0) {
    notify_grp_[current_grp]--;
  }
}

void ClassicContext::Shutdown() {
  stop_.store(true);
  mtx_wrapper_->Mutex().lock();
  notify_grp_[current_grp] = std::numeric_limits<unsigned char>::max();
  mtx_wrapper_->Mutex().unlock();
  cw_->Cv().notify_all();
}

void ClassicContext::Notify(const std::string& group_name) {
  (&mtx_wq_[group_name])->Mutex().lock();
  notify_grp_[group_name]++;
  (&mtx_wq_[group_name])->Mutex().unlock();
  cv_wq_[group_name].Cv().notify_one();
}
```

这里的通知计数很关键：条件变量自身不存储通知；如果线程尚未睡下，单独 `notify_one()` 不会留下一个未来可消费的事件。ClassicContext 先在 mutex 下递增 `notify_grp_`，等待谓词观察这个持久状态，因此线程稍后进入 `Wait()` 仍能看到工作。条件变量可能虚假唤醒，故 `wait_for` 检查谓词；而 1000 ms 超时也表示线程可能周期性重新检查，不能把每次状态变化都归因于一次通知。

之后由 Processor 的 Linux 线程执行循环：选中 routine 才调用 `Resume()`；扫描不到则 `Wait()`。`CRoutine::Resume()` 在验证 READY 后切换到 routine 自己保存的上下文。下面分别给出 Processor 调用循环与 CRoutine 恢复执行的**固定提交源码摘录**：

```cpp
void Processor::Run() {
  tid_.store(static_cast<int>(syscall(SYS_gettid)));
  AINFO << "processor_tid: " << tid_;
  snap_shot_->processor_id.store(tid_);

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
}
```

```cpp
RoutineState CRoutine::Resume() {
  if (cyber_unlikely(force_stop_)) {
    state_ = RoutineState::FINISHED;
    return state_;
  }

  if (cyber_unlikely(state_ != RoutineState::READY)) {
    AERROR << "Invalid Routine State!";
    return state_;
  }

  current_routine_ = this;
  SwapContext(GetMainStack(), GetStack());
  current_routine_ = nullptr;
  return state_;
}
```

因此一次“唤醒”必须拆成不同观察点：Scheduler 留下 update flag；ClassicContext 递增 group 通知并发 condition-variable 通知；Linux 再决定 Processor 线程何时获得 CPU；Processor 选择到 READY routine 后才由 `Resume()` 切换执行上下文。若同一 Processor 正在执行长时间 `Proc()`，通知可到达且线程可运行，协程仍要等当前协程主动 yield；这些层次不能用一个“协程被唤醒了”概括。

读者现在知道启动、稳态和关闭怎样共用一条对象链。下一步要把关系具体到运行中：哪些对象强拥有彼此，哪些只保留弱引用，哪些代码由 transport 线程执行，哪些代码只能等 Processor 获得 CPU。

## 把稳定态运行时还原成对象和线程

模块图告诉我们代码如何依赖，运行时图则回答对象由谁持有、线程在哪里切换、消息在哪一层被复制或共享。先沿所有权和线程把这些点连成一张运行中的系统图，再看单条消息的逐层动作。

### 对象所有权地图

理解组件名还不够，必须知道谁让谁活着：

:::{mermaid}
classDiagram
  class ComponentBase
  class ComponentM0
  class Node
  class ReaderM0
  class ComponentVisitorM0
  class ReaderVisitorM0
  class ComponentChannelBufferM0
  class ReaderChannelBufferM0
  class CacheBufferMsgPtr
  class DataDispatcherM0
  class CRoutine
  class RoutineClosure
  class Scheduler
  class ProcessorContext
  class ClassicContext
  class Processor

  ComponentBase <|-- ComponentM0
  ComponentBase o-- Node : node_ shared_ptr
  ComponentBase o-- ReaderM0 : readers_ vector shared_ptr
  Node o-- ReaderM0 : readers_ map shared_ptr
  ComponentM0 ..> ComponentVisitorM0 : creates Proc visitor
  ReaderM0 ..> ReaderVisitorM0 : Reader::Init visitor
  ComponentVisitorM0 *-- ComponentChannelBufferM0 : buffer_ member
  ReaderVisitorM0 *-- ReaderChannelBufferM0 : buffer_ member
  ComponentChannelBufferM0 --> CacheBufferMsgPtr : shared_ptr buffer_
  ReaderChannelBufferM0 --> CacheBufferMsgPtr : shared_ptr buffer_
  DataDispatcherM0 ..> CacheBufferMsgPtr : vector of weak_ptr
  Scheduler o-- CRoutine : shared_ptr
  CRoutine *-- RoutineClosure
  RoutineClosure --> ComponentVisitorM0 : shared_ptr capture
  RoutineClosure --> ReaderVisitorM0 : shared_ptr capture
  Scheduler o-- ProcessorContext : pctxs_ shared_ptr vector
  Scheduler o-- Processor : processors_ shared_ptr vector
  Processor o-- ProcessorContext : context_ shared_ptr
  ClassicContext --|> ProcessorContext
  ClassicContext o-- CRoutine : static cr_group_ group/priority vector shared_ptr
:::

图中的字段和 `shared_ptr` 标签比菱形本身更重要：它表示可共享的强引用，不表示只有一个对象能拥有目标。固定提交中，`ComponentBase` 的 `node_` 与 `readers_` 分别强持有 Node 和 Reader；`Node::readers_` 又以 channel 名为键保存同一 Reader 的另一份 `shared_ptr`，所以 Reader 有两个独立的所有权入口。ComponentBase 的关闭路径 ComponentBase 的成员字段 Node 的 Reader 注册表

调度队列是这张图中需要落到具体策略的一条边：`ProcessorContext` 只声明 `NextRoutine()`、`Wait()` 等接口，真正保存 Classic 策略 routine 队列的是派生类 `ClassicContext` 的静态 `cr_group_`（按 group 和 priority 分组）；不能把队列所有权写成接口本身。可对照 `ProcessorContext` 接口与 `ClassicContext` 的队列定义。

两个 visitor 也不是同一个对象被两边借用：`Component<M0>::Initialize()` 为 `Proc()` 建一只 `DataVisitor`，`Reader::Init()` 还会为 Reader 自己的回调/入队 routine 建另一只。每只 visitor 内含一个 `ChannelBuffer` 包装对象，再由包装对象的 `buffer_` 强持有自己的 `CacheBuffer`；Dispatcher 的注册表只保存 `weak_ptr`，不会替消费者延长 buffer 的寿命。Component visitor 的创建与 task 注册 Reader visitor 的创建 visitor 持有 ChannelBuffer ChannelBuffer 持有 CacheBuffer Dispatcher 的弱引用注册表

`RoutineFactory` 把 visitor 按值捕获进生成的任务函数；Scheduler 将函数交给 `CRoutine` 保存。于是初始化时的局部 `shared_ptr` 消失后，routine 仍能访问自己的 visitor。Scheduler 的 `id_cr_` 映射和策略 context 的运行队列都会持有 `CRoutine`；Scheduler 的 `pctxs_` 保存 context，Processor 的 `context_` 也共享该 context。RoutineFactory 的闭包捕获 Scheduler 创建 task Scheduler 所有权容器 Processor 的 context 引用 ClassicContext 队列元素类型

| 对象 | 强持有关系 | 其他共享或观察关系 | 关闭约束 |
|---|---|---|---|
| Component 实例 | ModuleController/装载记录 | Scheduler routine 的闭包弱引用，执行时临时 lock | 实际源码先 Clear、关 Reader、再 RemoveTask 等待；析构后才能卸载 DSO，但 Clear 不自动等待 Proc |
| 动态库句柄 | ClassLoader 装载批次 | 该库创建的全部对象与工厂函数 | 最后一个实例销毁后才能卸载 |
| Node | ComponentBase::node_ (shared_ptr) | 创建 Reader/Writer 时由 Component 使用 | 关闭 Reader 和 task 后，Node 随最后一个强引用释放 |
| Reader | `ComponentBase::readers_` 与 `Node::readers_` 都保存 `shared_ptr` | Reader routine 的默认入队闭包会捕获 Reader 的 `this` | `Reader::Shutdown()` 会移除自己的 routine；Node::Observe 的在途 Signal callback 是否静止还需单独保证，Node 不是唯一 owner |
| Receiver | ReceiverManager | 同类型、同 channel Reader 共享 | 移除全部 listener 后才能关闭 transport |
| ChannelBuffer | DataVisitor 以值成员保存 ChannelBuffer；其 buffer_ 再强持有 CacheBuffer | DataDispatcher 通过 weak_ptr 观察 CacheBuffer | 销毁 visitor 前停止对应 task；在途 Dispatch 的临时强引用结束后才释放缓存 |
| CacheBuffer | 每只 DataVisitor 的 ChannelBuffer::buffer_ 强持有 | DataDispatcher 注册 weak_ptr，Dispatch 时临时 lock | visitor 与在途 Dispatch 都不再持有后才能析构 |
| DataVisitor | Component 初始化或 Reader::Init() 创建；task 建立后由对应 CRoutine 的函数闭包强持有 | RoutineFactory 与 Scheduler::CreateTask() 期间短暂共享 | 对应 task 停止后闭包释放 visitor；Dispatcher 弱注册不延长其寿命 |
| CRoutine | Scheduler 的 id_cr_ 映射 | ClassicContext 的静态 `cr_group_` 分组/优先级队列和当前执行局部引用也共享 | 从映射与队列脱离、执行停止后，才能回收 routine、上下文栈和闭包 |
| ProcessorContext | Scheduler 的 pctxs_ 与 Processor 的 context_ 共同强持有 | Processor 线程通过 context 取任务 | Processor::Stop() 停止并 join 工作线程后，再允许释放共享引用 |

`shared_ptr` 解决“最后一个使用者之前不释放”，却不能自动打破环。若 Component 拥有 task，而 task 闭包又强引用 Component，就会形成循环；关闭函数需要显式取消任务或让回指使用 `weak_ptr`。动态库则比普通对象多一层 ABI 约束：虚函数、析构函数和模板实例代码所在的 DSO 必须在对象整个生命期保持装载。

### 线程与执行上下文地图

:::{mermaid}
flowchart LR
  subgraph PROC[一个 Cyber 进程]
    subgraph MAIN[mainboard 主线程]
      LOAD[装载 DAG 与 Component]
    end
    subgraph INTRA[INTRA 同步调用栈 不新建接收线程]
      WR[Writer::Write]
      ID[IntraTransmitter -> IntraDispatcher]
      WR --> ID
    end
    subgraph SHM[SHM 接收路径]
      LISTEN[ShmDispatcher ThreadFunc<br/>线程等待通知并读取 block]
      SHMDIS[SHM listener -> Dispatch]
      LISTEN --> SHMDIS
    end
    subgraph RTPS[RTPS 接收路径]
      RTPSCB[Fast RTPS subscriber listener callback<br/>传输库接收执行上下文]
      RTPSDIS[RtpsDispatcher -> listener]
      RTPSCB --> RTPSDIS
    end
    DD[DataDispatcher -> CacheBuffer -> DataNotifier]
    subgraph PTH[Processor Linux 线程]
      WAIT[Context Wait / NextRoutine]
      SW[SwapContext]
      WAIT --> SW
      subgraph USTACK[CRoutine 独立用户栈]
        FETCH[DataVisitor TryFetch]
        PROC_CB[Component Proc]
        FETCH --> PROC_CB
      end
      SW -->|Resume| FETCH
      PROC_CB -->|Yield| SW
    end
    subgraph AUX[拓扑 / monitor / record 线程]
      OBS[发现 统计 持久化]
    end
    RING[(每消费者 CacheBuffer)]
    ID --> DD
    SHMDIS --> DD
    RTPSDIS --> DD
    DD -->|shared_ptr 与锁| RING
    DD -.->|event -> Scheduler -> cv notify| WAIT
    RING -->|恢复后读取| FETCH
  end
  subgraph KERNEL[Linux 内核]
    KS[线程调度 futex socket mmap]
  end
  SHM --> KS
  RTPS --> KS
  PTH --> KS
  AUX --> KS
:::

图中的三种 transport 线程边界不是抽象推断，而是能从回调入口直接读出来。INTRA 的固定源码只有一个同步转发动作。下面是该转发的**固定提交源码摘录**（代码未改写）：

```cpp
template <typename M>
bool IntraTransmitter<M>::Transmit(const MessagePtr& msg,
                                   const MessageInfo& msg_info) {
  if (!this->enabled_) {
    ADEBUG << "not enable.";
    return false;
  }

  dispatcher_->OnMessage(channel_id_, msg, msg_info);
  return true;
}
```

这里没有创建接收线程或排入独立接收队列：调用 `Writer::Write()` 的线程继续执行 `OnMessage()`，所以如果在这条同步调用栈里做业务计算，发布者也会被一起阻塞。Cyber 随后的 Dispatcher 只写缓存并通知 Scheduler，才把业务 `Proc()` 从这段调用栈上移开。

SHM 则由初始化时显式创建的线程读取共享内存通知。以下给出 `ShmDispatcher::ThreadFunc()` 中监听、过滤主机、确定 channel 与读取 block 的**固定提交源码摘录**（保留完整控制流，省略诊断日志）：

```cpp
void ShmDispatcher::ThreadFunc() {
  ReadableInfo readable_info;
  while (!is_shutdown_.load()) {
    if (!notifier_->Listen(100, &readable_info)) {
      ADEBUG << "listen failed.";
      continue;
    }

    if (readable_info.host_id() != host_id_) {
      ADEBUG << "shm readable info from other host.";
      continue;
    }

    uint64_t channel_id = readable_info.channel_id();
    int32_t block_index = readable_info.block_index();
    int32_t arena_block_index = readable_info.arena_block_index();

    {
      ReadLockGuard<AtomicRWLock> lock(segments_lock_);
      if (segments_.count(channel_id) == 0) {
        continue;
      }

      if (block_index != -1) {
        // check block index
        if (previous_indexes_.count(channel_id) == 0) {
          previous_indexes_[channel_id] = UINT32_MAX;
        }
        uint32_t& previous_index = previous_indexes_[channel_id];
        if (block_index != 0 && previous_index != UINT32_MAX) {
          if (block_index == previous_index) {
            ADEBUG << "Receive SAME index " << block_index << " of channel "
                   << channel_id;
          } else if (block_index < previous_index) {
            ADEBUG << "Receive PREVIOUS message. last: " << previous_index
                   << ", now: " << block_index;
          } else if (block_index - previous_index > 1) {
            ADEBUG << "Receive JUMP message. last: " << previous_index
                   << ", now: " << block_index;
          }
        }
        previous_index = block_index;
        ReadMessage(channel_id, block_index);
      }

      if (arena_block_index != -1) {
        if (arena_previous_indexes_.count(channel_id) == 0) {
          arena_previous_indexes_[channel_id] = UINT32_MAX;
        }
        uint32_t& arena_previous_index = arena_previous_indexes_[channel_id];
        if (arena_block_index != 0 && arena_previous_index != UINT32_MAX) {
          if (arena_block_index == arena_previous_index) {
            ADEBUG << "Receive SAME index " << arena_block_index
                   << " of channel " << channel_id;
          } else if (arena_block_index < arena_previous_index) {
            ADEBUG << "Receive PREVIOUS message. last: " << arena_previous_index
                   << ", now: " << arena_block_index;
          } else if (arena_block_index - arena_previous_index > 1) {
            ADEBUG << "Receive JUMP message. last: " << arena_previous_index
                   << ", now: " << arena_block_index;
          }
        }
        arena_previous_index = arena_block_index;
        ReadArenaMessage(channel_id, arena_block_index);
      }
    }
  }
}
```

真实循环在每次通知后读取消息索引、确认本机 channel segment 存在，再调用 `ReadMessage()`；arena block 则走 `ReadArenaMessage()`。这不是 `Processor` 的 routine 轮询。源码中对 block index 的日志分支也说明：共享内存通知和业务回调不是一个事件，接收侧还要检查 block 是否跳号、重复或回退。

`Init()` 里真正建立 OS worker 的语句很短，摘录自同一文件（行 220–226）：

```cpp
bool ShmDispatcher::Init() {
  host_id_ = common::Hash(GlobalData::Instance()->HostIp());
  notifier_ = NotifierFactory::CreateNotifier();
  thread_ = std::thread(&ShmDispatcher::ThreadFunc, this);
  scheduler::Instance()->SetInnerThreadAttr("shm_disp", &thread_);
  // statistics::Statistics::Instance()->CreateSpan("protobuf_parse_time");
  return true;
}
```

RTPS 的入口不同：Fast RTPS 建立 Subscriber 时保存 Cyber 的 listener；库收到数据后调用 `SubListener::onNewDataMessage()`，该 callback 取出样本再同步调用传入的函数。先看 Subscriber 创建时保存 listener 的**固定提交源码摘录**：

```cpp
  auto listener_adapter = [this, self_attr](uint64_t channel_id,
                                const std::shared_ptr<std::string>& msg_str,
                                const MessageInfo& msg_info) {
    statistics::Statistics::Instance()->AddRecvCount(
      self_attr, msg_info.msg_seq_num());
    statistics::Statistics::Instance()->SetTotalMsgsStatus(
                              self_attr, msg_info.msg_seq_num());
    this->OnMessage(channel_id, msg_str, msg_info);
  };

  new_sub.sub_listener = std::make_shared<SubListener>(listener_adapter);

  new_sub.sub = eprosima::fastrtps::Domain::createSubscriber(
      participant_->fastrtps_participant(), sub_attr,
      new_sub.sub_listener.get());
```

再看 Cyber listener 的类型声明，以下为**固定提交源码摘录**：

```cpp
class SubListener : public eprosima::fastrtps::SubscriberListener {
 public:
  using NewMsgCallback = std::function<void(
      uint64_t channel_id, const std::shared_ptr<std::string>& msg_str,
      const MessageInfo& msg_info)>;

  explicit SubListener(const NewMsgCallback& callback);
  virtual ~SubListener();

  void onNewDataMessage(eprosima::fastrtps::Subscriber* sub);
  void onSubscriptionMatched(eprosima::fastrtps::Subscriber* sub,
                             eprosima::fastrtps::MatchingInfo& info);  // NOLINT

 private:
  NewMsgCallback callback_;
  MessageInfo msg_info_;
  std::mutex mutex_;
};
```

再看 listener 自己实际做的事。以下是**固定提交源码连续摘录**（省略消息元数据逐字段赋值）：

```cpp
void SubListener::onNewDataMessage(eprosima::fastrtps::Subscriber* sub) {
  RETURN_IF_NULL(sub);
  RETURN_IF_NULL(callback_);
  std::lock_guard<std::mutex> lock(mutex_);

  // fetch channel name
  auto channel_id = common::Hash(sub->getAttributes().topic.getTopicName());
  eprosima::fastrtps::SampleInfo_t m_info;
  UnderlayMessage m;

  RETURN_IF(!sub->takeNextData(reinterpret_cast<void*>(&m), &m_info));
  RETURN_IF(m_info.sampleKind != eprosima::fastrtps::ALIVE);

  // fetch MessageInfo
  char* ptr =
      reinterpret_cast<char*>(&m_info.related_sample_identity.writer_guid());
  Identity sender_id(false);
  sender_id.set_data(ptr);
  msg_info_.set_sender_id(sender_id);

  Identity spare_id(false);
  spare_id.set_data(ptr + ID_SIZE);
  msg_info_.set_spare_id(spare_id);

  uint64_t seq_num =
      ((int64_t)m_info.related_sample_identity.sequence_number().high) << 32 |
      m_info.related_sample_identity.sequence_number().low;
  msg_info_.set_seq_num(seq_num);

  // fetch message string
  std::shared_ptr<std::string> msg_str =
      std::make_shared<std::string>(m.data());

  uint64_t recv_time = Time::Now().ToNanosecond();
  uint64_t base_time = recv_time & 0xfffffff0000000;
  int32_t send_time_low = m.timestamp();
  uint64_t send_time = base_time | send_time_low;
  int32_t msg_seq_num = m.seq();

  msg_info_.set_msg_seq_num(msg_seq_num);
  msg_info_.set_send_time(send_time);

  // callback
  callback_(channel_id, msg_str, msg_info_);
}
```

这证明 RTPS 的 listener callback 与 `ShmDispatcher` 的 `std::thread` 不是同一执行实体；当前片段只证明 Cyber 注册并执行了回调，具体由 Fast RTPS 哪个内部线程调用 listener 属于第三方库实现边界，不能从 Cyber 此处代码推断成“每 Reader 一只 Cyber 线程”。共同的收敛点是在 `RtpsDispatcher::OnMessage()` 后续 listener handler 中，而后才进入 DataDispatcher。

这张图解释了“线程安全”必须具体到边界。SHM dispatcher 线程和 RTPS listener 执行上下文分别会与关闭线程并发操作 listener；INTRA 则在 `Writer::Write()` 的调用栈中同步进入 dispatcher。Processor 线程还会和配置/监控线程并发读取状态；同一 Component 的 `Proc()` 是否串行，则取决于它创建的 routine 和 Scheduler 配置，不能只看 Component 类本身。

业务回调被移出接收执行路径是一项有价值的隔离，但不是免费隔离。消息仍要先进入有界缓存；Processor 若持续跟不上，结果从“接收路径被业务阻塞”转化为“旧数据被 ring 覆盖、数据年龄增长或任务长期就绪”。因此缓存容量、Proc WCET 与调度份额必须合起来分析。

### 一条消息的逐层数据动作

| 阶段 | 数据表示 | 主要动作 | 复制与分配 |
|---|---|---|---|
| SHM/RTPS 接收 | block/serialized bytes | 校验并反序列化 | SHM 普通路径仍可能创建 `MessageT` |
| Receiver listener | `shared_ptr<MessageT>` | 交给 DataDispatcher | 增加引用计数，不复制消息体 |
| Dispatcher fan-out | 同一个 shared pointer | 写入 K 个 ChannelBuffer | `O(K)` 元数据写入 |
| ChannelBuffer | ring 中的 shared pointer | 追加索引；满载时逻辑淘汰最旧项 | 固定容量，无消息体深复制；物理槽引用可能延后一条写入才释放 |
| DataVisitor | 若干 shared pointer | 取主输入并组合最新辅助输入 | 复制智能指针，固定融合快照 |
| Component::Proc | `shared_ptr<const M...>` | 算法读取并产生输出 | 由业务算法决定 |

这能纠正“共享内存等于端到端零复制”的误解：共享内存只描述某一传输段，反序列化、业务消息对象、protobuf 字段访问和输出序列化仍可能产生复制。性能分析要逐段记录 bytes，而不是给整条链贴一个 zero-copy 标签。

## 沿同一条消息检查设计边界

现在对象、线程和数据表示已经有了共同坐标。接下来的平面划分、容量预算和设计取舍都沿用这条消息链：哪些阶段可能排队，哪些阶段改变消息年龄，哪些阶段只是通知任务而没有运行算法。

### 控制面、数据面与执行面的边界

| 平面 | 负责内容 | 典型变化频率 | 失败表现 |
|---|---|---:|---|
| 部署控制面 | DAG、类加载、组件实例 | 启停时 | 类名错误、ABI 不匹配、半装载 |
| 拓扑控制面 | Channel、端点发现、连接 | 秒级或拓扑变化 | Reader 可见但 transport 未就绪 |
| 数据面 | 收包、分发、缓存 | 每条消息 | drop、覆盖、反序列化失败 |
| 执行面 | 通知、routine 就绪、Processor | 每次调度 | 饥饿、长 Proc、唤醒丢失 |
| 运维面 | 监控、record/replay | 周期/事件 | 指标缺失、磁盘跟不上 |

“组件正在运行”只覆盖生命周期状态，不证明这五个平面都健康。现场指标应至少区分 transport 收包数、Dispatcher 投递数、buffer 覆盖数、routine 唤醒数、Proc 完成数和输出发送数，才能定位消息消失在哪一段。

### 复杂度和容量的第一张预算表

设同一 channel 有 `K` 个逻辑消费者，每个消费者 buffer 容量为 `Cᵢ`，平均消息体大小为 `S`：

- Dispatcher 每条消息的扇出时间为 `O(K)`；
- ring 元数据空间约为 `O(ΣCᵢ)`；
- 若所有 buffer 共享同一个 `shared_ptr` 消息体，消息体并非简单变成 `K×S`，但慢消费者会延长旧消息的共享寿命；
- 多输入 AllLatest 的辅助输入查找随输入数增长，实际成本还包含各 buffer 的索引修正；
- Scheduler 扫描成本取决于策略、优先级桶和就绪 routine 数；Classic 策略不能用“协程切换很快”代替最坏扫描分析。

容量增加可以吸收短突发，却会提高最坏数据年龄和在途对象数量。对于规划/控制链，旧数据通常比丢弃更危险，因此容量不是越大越安全。

## 用源码路线把模块重新组装

读者现在可以把目录、依赖、对象与运行时串起来。最后沿一条连续阅读路线收束这张地图：先确认固定版本中的入口与 symbol，再按消息调用链进入专题，复刻时按依赖从简单结构逐层增加功能。

### 源码阅读坐标

关键入口均固定到同一提交，正文以直接摘录的函数名作为阅读坐标：

- `ModuleController::LoadModule()`：配置怎样进入装载器；
- `Component<M0>::Initialize()`：模板输入怎样创建 Reader、Visitor 与 routine；
- `Reader::Init()`：Reader 怎样把 transport 与数据分发接起来；
- `DataDispatcher<T>::Dispatch()`：消息怎样扇出；
- `DataVisitor::TryFetch()`：消费者游标与通知怎样结合；
- `Scheduler::CreateTask()`：任务怎样进入 Scheduler；
- `Processor::Run()`：最终由哪个 OS 线程执行 routine。

阅读这些实现时，每到一次 `shared_ptr` 复制、锁获取、回调注册或任务创建，都把它补回上面的所有权表和线程图。这样源码细节不会变成互不相干的函数列表。

### 沿着消息主链阅读源码

1. 从 `mainboard` 解析 DAG 进入 ModuleController，确认每个动态库和 Component 由谁持有；
2. 从 Component 模板初始化进入 Node/Reader 创建，记录 callback 如何变成 routine；
3. 从 transport Receiver listener 进入 DataDispatcher，验证接收线程不执行 `Proc()`；
4. 从 Dispatcher 遍历同 channel buffer，记录 `shared_ptr<Message>` 的 `O(K)` 元数据扇出；
5. 从 ChannelBuffer 的 index/ring 进入 DataVisitor，解释覆盖、游标修正和 AllLatest；
6. 从 DataNotifier 事件进入 Scheduler/Processor，定位 routine 由 DATA_WAIT 变为 READY、被 Processor 选中执行、再主动 Yield 到下一状态的过程；执行期不是一个名为 RUNNING 的 RoutineState；
7. 从 shutdown 反向追踪 routine、Visitor、Reader/Receiver、Component、factory 与 DSO。

这七步结束后，应能把一条消息从 bytes 一直讲到 `Proc()`，并回答任何时刻哪个线程可访问、哪个 owner 保活、队列满时丢什么、关闭如何解除等待。

## 设计代价决定从哪里开始复刻

源码路线带我们从入口一直走到 `Proc()`，但看懂调用方向还不等于知道方案适合什么负载。这里把数据结构、所有权和调度的代价放回机器人场景，再据此安排一个可逐步验证的复刻顺序。

### 设计模式与工程代价

模式不是起点。这里的“先遇到”是从工程需求反推的设计顺序，不是在断言 Apollo 作者的历史开发过程。同一个 Reader 可能部署在进程内、共享内存或网络上；同一个 mainboard 又要按 DAG 创建不同 Component。为说明这两种变化轴如何导出接口，下面是**教学接口示意**，不是固定提交的原样源码；固定源码中的实际抽象分别见 `Receiver` 接口和 `ClassLoaderManager` / ModuleController 创建路径：

```cpp
template <class T>
class Receiver {
 public:
  virtual void Start(std::function<void(std::shared_ptr<T>)>) = 0;
  virtual void Stop() = 0;
  virtual ~Receiver() = default;
};

class ComponentFactory {
 public:
  virtual std::shared_ptr<ComponentBase> Create() = 0;
  virtual ~ComponentFactory() = default;
};
```

Node 只依赖 `Receiver<T>` 的能力，不依赖 RTPS/SHM 类；ModuleController 只依赖非模板 `ComponentBase` 和工厂，不在主程序里枚举所有业务派生类。虚函数带来运行期分派与 ABI 约束，模板 Receiver 又带来实例化和 DSO 符号问题，所以固定源码并不是“到处套模式”，而是在编译期类型安全与运行期部署弹性之间切边界。

- Plugin Factory：DAG 用字符串选择 Component 类型；部署灵活，但 ABI、注册宏和 DSO pinning 进入运行时；
- Facade：Node/Reader/Writer 隐藏多 transport；API 统一，但底层延迟和复制语义仍不同；
- Registry/Flyweight：ReceiverManager 共享相同 channel/type Receiver；减少资源，但 listener 生命周期更复杂；
- Per-consumer Buffer：Dispatcher 为每个消费者保留独立游标；隔离慢消费者，代价是 `O(K)` 扇出与元数据；
- Observer/Event Latch：Notifier 合并数据事件并唤醒 routine；减少忙轮询，但必须避免丢唤醒；
- Active Object/Coroutine：业务在 Processor 执行；隔离接收线程，但非抢占长 Proc 会造成饥饿；
- Snapshot Join：AllLatest 组合主触发和辅助最新值；低等待，但不提供时间同步保证。

模式是否成立要在关闭和过载下验证。正常数据流中能运行的 observer，如果注销与通知并发时访问旧 callback，就仍是不完整设计。

### 系统级可行性预算

设 channel 消费者数 `K`、每消费者 ring 容量 `Cᵢ`、消息最大对象大小 `Smax`、发布频率 `f`、Proc 最坏时间 `W`、同 Processor 就绪 routine 数 `R`：

| 路径 | 主要时间成本 | 空间主项 |
|---|---|---|
| transport receive | 收包、校验、反序列化 | SHM/RTPS buffer + Message 对象 |
| Dispatcher | 每消息 `O(K)` | buffer 元数据 `O(ΣCᵢ)` |
| 消息寿命 | 由最慢消费者持有 shared_ptr | 旧消息对象可跨多个 ring 存活 |
| 多输入融合 | 主输入消费 + 辅助索引查找 | 各输入 ring 与快照指针 |
| Scheduler | 策略队列/扫描 + context switch | routine 栈与上下文 `O(R)` |
| Processor 响应 | 前序 routine 执行 + 当前 `W` | OS thread stack |

协作式调度下，某 routine 的响应上界不仅是自身 W，还包含同 Processor 上在它之前运行且不让出的工作。若 Proc 内执行大模型推理、阻塞 I/O 或长循环，应拆到独立 Processor/线程池并用有界结果 channel 回传。

内存也不能只按 `ΣCᵢ×sizeof(shared_ptr)` 计算：ring 共享 message body，但不同时间索引可能同时保留多个大对象。保守估算要结合发布速率、消费者最大滞后和实际消息对象/反序列化容量。

控制链应观测 data age 而不仅是 queue length。容量增大可以降低短突发 drop，却会让规划消费更旧障碍物；对自动驾驶而言，及时拒绝/降级常比排队处理所有旧帧安全。

### 从零实现路线

先实现单进程 Node 与类型化 Reader/Writer，再加入每消费者有界 buffer；随后把 callback 与执行线程分离，加入 DataVisitor；最后才做 DAG、插件、SHM/RTPS 和多策略 Scheduler。这个顺序先固定消息与生命周期语义，再增加部署弹性和性能优化。

每个阶段都有可验证产物：单进程阶段验证类型与 channel；buffer 阶段验证慢消费者互不移动游标；执行分离阶段验证 transport 回调从不运行业务；DataVisitor 阶段验证触发输入与辅助快照；插件阶段验证失败回滚与 DSO pinning；多传输阶段验证同一消息身份与重复抑制；Scheduler 阶段验证优先级、饥饿与关闭屏障。

实现顺序中不要提前加入动态插件和多 transport。先用单线程、单进程把消息身份、buffer 覆盖、通知谓词和关闭状态机写正确，再逐层增加并发；否则一次消息丢失可能同时来自 transport、Dispatcher、ring、Notifier 或 Scheduler，难以定位。
