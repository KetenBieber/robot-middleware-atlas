# Cyber RT 从哪里开始：先区分通信、装配和执行

固定源码版本：Apollo `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。

一辆车上的激光雷达、定位、感知、规划和控制不会按同一频率运行。雷达持续生产数据，感知有计算长尾，规划依赖多路输入，控制通常按自己的周期读取最新状态。Cyber RT 要解决的不是“把 A 的指针交给 B”这样一个函数调用，而是让**消息传递、组件装配和任务执行能够分别改变**。

本文给出最小阅读坐标和两个真实组件的执行对照。想从零设计模块并沿固定源码回放一帧消息，请从[架构骨架与运行时](architecture-map.md)进入；具体源码专题不需要先读完全部总览。

## 在一个进程里，哪些东西不是一回事

```text
mainboard 进程
  ModuleController ── 依据 DAG 创建 Component 对象
        |
        +── PlanningComponent（业务对象）
        |     ├── Node / Readers / Writer
        |     └── Component task ── CRoutine
        |
        +── ControlComponent（业务对象）
        |     └── Timer ── 周期性请求业务执行
        |
        +── Transport（INTRA / SHM / RTPS）
        +── DataDispatcher ── 每个消费者的有界缓存
        +── DataNotifier ── channel 更新事件
        └── Scheduler / Processor（真正的 OS worker）
```

这里“进程”是 Linux 的地址空间边界；“组件”是 Cyber 管理生命周期的业务对象；“Reader”是 channel 接收端点；“CRoutine”是可暂停恢复的用户态任务；“Processor”才拥有实际的 OS 线程。`Node` 是有名字的通信实体工厂，并不代表每个 Node 都有专属线程。

一个 Writer 可以发给多只 Reader，同一 channel 的不同消费者也可以各有缓存。INTRA 表示同进程传递，SHM 表示同机跨进程共享内存路径，RTPS 表示跨网络协议路径；它们不是三种业务接口，也不能仅因出现 SHM 就宣称端到端零复制。跨进程边界仍可能包含序列化、反序列化与分配。

## Planning：一条主输入唤醒，多条输入一起参与计算

对固定版本的 `Component<M0, M1, ...>`，尖括号中的模板参数是 C++ 编译期消息类型；框架据此生成不同签名的 `Proc(...)` 和对应 Visitor。多输入并不表示到达某个时间戳就自动严格对齐。正常运行模式下，第一个 Reader 对应主触发输入，辅助输入由 `AllLatest` 提供最新可用快照。

单输入情况能最直接看到框架为什么要创建两类消费对象。固定源码 `Component<M0>::Initialize` 在调用业务 `Init()`、准备好 reader 配置后，执行下列连续片段：

```cpp
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
```

这段不是“Reader 收到数据就运行 `Proc`”。正常模式先创建 Reader；然后另外创建供 Component task 使用的 DataVisitor；再把取数循环包装成 RoutineFactory，交给 Scheduler 创建任务。因此 Reader 自身的消费入口和 Component 的业务执行入口有不同缓存与生命周期。仿真模式分支则把 `func` 直接交给 Reader，这正是分析执行线程时必须先确认的配置差异。

`func` 内部捕获的 `weak_ptr<Component>` 负责在执行前尝试提升为 `shared_ptr`，避免闭包本身强行延长 Component 生命周期。但弱引用成功不代表 `Clear()` 可以与正在运行的 `Proc()` 随意并发；业务资源仍需要自己的关闭协议。[装配与生命周期](dag-to-component.md)将逐步展开。

## Control：按周期触发与按消息触发不是同一套时钟

对于固定版本的 `TimerComponent`，业务实现无参数 `Proc()`，框架创建计时器调用它。对应固定源码中的 `TimerComponent::Process()` 与初始化末段是：

```cpp
bool TimerComponent::Process() {
  if (is_shutdown_.load()) {
    return true;
  }
  return Proc();
}
```

```cpp
timer_.reset(new Timer(config.interval(), func, false));
timer_->Start();
return true;
```

这意味着计时器的间隔配置定义**请求执行的节奏**，不代表 `Proc()` 的实际开始时刻有硬实时保证。CPU 是否可用、此前任务是否长时间占用 Processor、实际定时实现和线程调度都会产生抖动。对控制系统，要同时记录传感器时间戳、开始执行时间和输出时间，才能讨论状态年龄与控制周期。

消息触发与定时触发最终可能共用底层调度资源，却绝不能把 `Reader callback`、`Timer callback` 和 `Component::Proc()` 不加区分地叫作“收到消息后执行的回调”。

## 一帧数据进来后，先问哪三个问题

```text
数据在哪：
  Receiver → DataDispatcher → CacheBuffer
                              ↑
                      DataVisitor 之后才按游标读取

谁通知：
  DataNotifier → Scheduler → ProcessorContext 的等待谓词
                               ↓
                           OS worker 醒来

谁运行算法：
  Processor → 选中 CRoutine → Resume → Process → Proc
```

一帧图像落进 ring、一次通知被接收、一只 worker 从条件变量返回和 `Proc()` 真正开始，是四个不同时间点。给任何一段代码做延迟分析，都要找出当前线程、锁域、数据对象与下一跳；看见 `notify_one()` 不能直接推断算法已经开始处理新帧。

还要保留两个读源码时不可省略的限制。第一，有界 ring 的 `pending_queue_size` 决定能保留多少历史，慢消费者会遇到覆盖或游标调整，而不是保留每一帧。第二，固定版本的 Dispatcher 注册表与 Notifier 登记存在运行时并发和注销方面的边界；`CRoutine::state_` 的跨线程访问也不能只靠另一个 `atomic_flag` 证明安全。因此这套实现可以用于学习实际工业系统如何取舍，却不应被抽象成所有平台通用的无锁、无丢唤醒或确定性实时模板。

## 按问题选择源码章节

| 现在想弄清什么 | 阅读入口 |
| --- | --- |
| 从零确定模块与编译依赖，画清线程/所有权 | [架构骨架](architecture-map.md) |
| DAG 如何真正创建 Component，插件何时才能卸载 | [装配](dag-to-component.md)、[插件 ABI](class-loader-abi.md) |
| Reader/Writer 怎样连接 Receiver 与传输 | [通信端点](node-reader-writer.md) |
| 消费者为何有独立游标，`AllLatest` 实际怎样组合输入 | [有界缓存](pending-queue-ring.md)、[多输入组合](multi-input-fusion.md) |
| Dispatcher、Notifier 如何注册、扇出与处理并发 | [数据分发](dispatcher-notifier.md) |
| 一次通知怎样更新等待状态 | [Scheduler 与协程](croutine-wakeup.md) |
| READY routine 怎样获得 CPU 并切回协程栈 | [Processor 与上下文切换](processor-context-switch.md) |
| 把运输、缓存、通知和执行接成一条完整链 | [Receiver 到 Proc](message-to-proc.md) |
| 复刻时怎样划分 C++ 类型与对象所有权 | [C++ 类型运行时](cpp-type-runtime.md)、[最小实现](cpp-implementation-lab.md) |

首次学习时，先用[架构骨架](architecture-map.md)形成对象图，再选一条实际消息链深入；不要在还没见过缓存和 Processor 之前就记一串类名。读完每段真实源码，能在纸上回答“消息现在在哪里、哪只线程运行、哪个对象拥有它、下一个同步边界在哪里”，才说明这一层已经读懂。
