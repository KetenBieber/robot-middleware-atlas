# 从一任务一线程到 Cyber Scheduler：任务、协程与 Processor

> 源码基线：Apollo Cyber RT 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。下文标明“固定源码摘录”的代码均直接取自该版本；删节只省略与当前机制无关的外围部分，不用链接或本地文件路径代替代码。

自动驾驶进程里可能同时存在相机感知、点云处理、定位、预测、规划、监控和录制任务。如果每个 Reader（订阅某条 channel 的消费者对象）都在消息到达时临时创建线程，线程栈、创建开销和内核调度实体会随消息频率爆炸；如果每个任务永久占一只线程，几十到几百个低占空比任务又会浪费栈空间并增加上下文切换。

先把本章会反复出现的四个“执行位置”分开。**进程**是拥有独立虚拟地址空间的资源边界；**操作系统线程**是 Linux 可以独立调度的执行实体，拥有内核记录和线程栈；**用户态协程**则是运行时保存的一组寄存器与用户栈，它不能被 Linux 单独调度，只能在承载它的线程上通过 `Resume/Yield` 主动切换；**任务**是中间件的逻辑工作，不等于线程，也不等于协程。线程上下文切换要进入内核调度路径，协程切换通常只在用户态保存/恢复寄存器和栈指针，但协程如果执行阻塞系统调用，整只承载线程仍会睡眠。

还要先划出用户态与内核态的边界：系统调用（system call）是用户代码请求内核提供文件、网络等服务的受控入口。若协程执行的阻塞式 `read()` 或 `recv()` 正在等待数据，进入睡眠的是承载协程的整只操作系统线程；同一线程上的其他协程也不能运行。协程的轻量切换不能把一个阻塞系统调用变成非阻塞操作。

在 Cyber 中，**CRoutine** 是承载一段可暂停/恢复任务的 C++ 对象：它保存任务函数、调度状态和一个 `RoutineContext`；后者保存这只用户态协程的栈与恢复位置。`Processor` 是运行时的 worker（工作线程执行器），拥有一只可被 Linux 调度的 `std::thread`，并循环领取任务、运行到协程让出；`ProcessorContext` 是供 Processor 调用的调度策略接口。它们把逻辑任务与执行资源分开，本章将先推导为什么需要这种分离，再对照固定提交中的 `RoutineFactory -> CRoutine -> ProcessorContext -> Processor`。

这个策略边界不是抽象图上的猜测。固定提交中的 `ProcessorContext` 直接把 Processor 需要的操作收敛成“停止、取下一只任务、没有任务时等待”三项；下面是**固定提交源码摘录**：

```cpp
class ProcessorContext {
 public:
  virtual void Shutdown();
  virtual std::shared_ptr<CRoutine> NextRoutine() = 0;
  virtual void Wait() = 0;

 protected:
  std::atomic<bool> stop_{false};
};
```

`NextRoutine()` 和 `Wait()` 后的 `= 0` 表示纯虚函数：基类规定操作，但不提供可直接使用的策略实现；`ClassicContext` 等具体类必须覆盖它们。`Processor` 通过基类指针调用时，C++ 会根据对象的真实类型在运行时分派到具体策略。若没有这条边界，`Processor::Run()` 就必须自己理解 Classic 的优先级 vector、Choreography 的 task 绑定等内部结构；新增策略会把 worker 主循环也改成一串分支。要注意，`ProcessorContext` 是“怎样选任务”的接口，`RoutineContext` 才是“暂停后怎样恢复栈”的对象，两者只因英文都含 Context 而同名相似，职责完全不同。

接口里的 `std::atomic<bool> stop_` 是可并发读取/写入的停止位：它避免一个线程普通写 `bool`、另一个线程同时读取所产生的数据竞争。但“把停止位改成 true”不会自动唤醒条件变量；正在 `Wait()` 的线程只会在谓词成立、收到后重新检查谓词，或等待超时后继续。因此具体 Context 还必须改变 `Wait()` 正在检查的共享条件并发出通知。下面会看到 Classic 的关闭路径怎样同时做这两件事。

为避免把唤醒机制拆散，先沿着前文的[完整消息链](message-to-proc.md)回到执行面的交界处：transport 线程已经把消息写入 `CacheBuffer`，[`DataNotifier`](dispatcher-notifier.md) 已调用 `NotifyProcessor(task_id)`，但业务 `Proc()` 尚未执行；`DataVisitor` 如何保存私有读取位置和缓存如何覆盖旧消息，分别见[有界缓存](pending-queue-ring.md)与[多输入组合](multi-input-fusion.md)。本章只接着回答通知如何变成可运行任务、Processor 何时恢复协程。

消息路径也有两个必须分开的“位置”。`DataVisitor` 是某个消费者的读取视图：它保存自己的 ring 引用和游标，负责在 Processor 恢复后取出消息；`DataNotifier` 是事件桥：它只把“某个 channel 可能有新数据”转换为 task 通知，不携带 payload（消息正文），也不执行用户业务函数 `Proc()`。因此，消息到达、routine 状态变为 `READY`（可供 Processor 选取，不等于正在执行）、OS worker 被唤醒和协程恢复，是四个不同事件。后文每跨过一个边界都会重新标出当前线程、对象状态和下一跳。

消息路径中的 ring（环形缓冲区，ring buffer）不是“把消息排队后立刻执行”的线程队列，而是固定容量的循环槽：逻辑序号按容量取模，满时按实现策略覆盖旧槽，每个 DataVisitor 持有自己的 cursor（读取游标）。与之不同，Cyber Scheduler 是进程内 C++ 对象，登记并选择 CRoutine、把通知交给 Processor；Linux scheduler 属于内核，决定承载 Processor 的 OS thread 何时获得 CPU，所以 READY 只表示可被选择，并不表示已经运行。

## 从线程到可复用任务

先从最容易写出的实现开始：收到一条消息就启动一条线程。它能很快展示“线程数量不能跟消息数量一起增长”的问题；接着再看为什么线程池仍不够，以及 Cyber 为什么还要把任务状态与 Processor 线程拆开。

### 第一版：每条消息创建线程

初学者可能先写出下面的**错误示例**。代码里的 `std::shared_ptr<const Image>` 是带引用计数的共享所有权句柄：复制或移动这个句柄不会复制图像像素，`const` 只禁止通过该句柄修改图像。`std::move(x)` 本身只是把表达式转换成可移动的右值类别；真正转移句柄的是 `shared_ptr` 的移动操作，转移后源句柄为空：

```cpp
void OnMessage(std::shared_ptr<const Image> image) {
  std::thread([this, image = std::move(image)] {
    component_->Proc(image);
  }).detach();
}
```

这段代码同时暴露了四个 C++/OS 事实。`std::thread` 构造一个可被 Linux 调度的 OS 线程；每次消息都重新申请线程栈和内核线程对象。`detach()` 会放弃 `std::thread` 对象上等待该线程结束的 join 句柄，调用方无法再等待它，所以关闭时没有“在途工作已经清空”的同步点。lambda 是一个编译器生成的函数对象，`[this, image = std::move(image)]` 把裸指针和消息句柄保存进对象；按值移动 `shared_ptr` 只转移句柄，不会延长裸 `this` 指向的 Component 寿命。60 Hz 相机若一次推理超过 16.7 ms，线程会持续累积。这里真正需要的不是“更多线程”，而是把逻辑任务与执行它的 OS 线程分离。

这里的 **RAII**（Resource Acquisition Is Initialization，资源获取即初始化）也值得先说清：把资源绑定到 C++ 对象，在析构函数中释放，是让离开作用域自动触发清理的惯用方式。`std::thread` 对象仍关联着一条线程、析构前必须 `join()` 或 `detach()` 时称为 joinable；调用 `join()` 会等待线程函数真正返回。若一个仍为 joinable 的 `std::thread` 对象直接析构，标准库会调用 `std::terminate()` 终止进程，而不是替程序猜测应该等待还是放弃线程。RAII 能帮助管理这项义务或 shared ownership，却不能替代跨线程的停止协议；一个仍在运行的 detached 线程不会因为拥有它的 C++ 对象离开作用域就自动安全停止。

### 第二版：线程池仍然缺少持久任务状态

`std::function` 是类型擦除的可调用对象：它把 lambda、函数指针和自定义函数对象统一包装成同一个 `void()` 接口，让线程池不必知道具体闭包类型。闭包按值捕获的 consumer 与 msg 会随包装对象共同存活，但间接调用可能带来额外分配和动态分派；它仍然没有回答 task 的状态、游标和关闭协议如何保存。

下面是**教学示例，不是 Apollo 源码**：普通线程池会把每条消息包装成一次性 `std::function<void()>`：

```cpp
void OnMessage(MessagePtr msg) {
  pool.Submit([consumer, msg = std::move(msg)] {
    consumer->Proc(msg);
  });
}
```

它限制了内核线程数，却让 ready queue（待执行任务队列）同时拥有任务和 payload。这里的 callback 是提交给线程池、之后由 worker 调用的函数。高频 channel 会产生大量 lambda 闭包（捕获变量与可调用代码组成的函数对象）及引用计数操作；复制 `shared_ptr` 会更新它指向的共享控制块计数，最后一份句柄销毁时才释放消息对象，这避免大消息复制但仍有成本。同一组件可能被多个 worker 并发重入，即不同线程同时进入同一个 `Proc()`；如果组件成员没有同步，线程池不会自动保证安全。队列满时还必须决定阻塞接收线程、丢任务还是丢消息。更重要的是，消息已经存在 DataVisitor 的有界 ring 中，再把 payload 复制进执行队列会建立第二套容量和丢弃语义。

Cyber 因而采用持久任务：ring 保存“有多少数据”，CRoutine 保存“从哪里继续执行”，scheduler 只管理“哪只任务可能可运行”。一次通知不携带 payload，只让既有 task 再检查自己的 DataVisitor。

这一章回答执行面的核心问题：task id 怎样找到一只 CRoutine，休眠中的 worker 怎样被唤醒，为什么“检查队列为空”和“准备睡眠”之间不会丢事件，Classic scheduler 又怎样选择最终运行的 routine。

先给出本章边界：消息 payload 已经留在 ring 中，后续调度链不再搬运它。调度器只传播 task identity 和 runnable state，直到 Processor 恢复协程，DataVisitor 才重新从 ring 取出 `shared_ptr<Message>`。

从零设计时，可以由四个问题推导四种对象：

| 工程问题 | 推导出的对象 | 固定源码对应 |
|---|---|---|
| 业务函数和读取位置怎样长期绑定 | task/routine | `RoutineFactory`、`CRoutine` |
| 谁保存调度状态（READY 可选、DATA_WAIT/IO_WAIT 暂候、SLEEP 等期限） | 任务控制块（task control block：集中记录某个任务调度信息的对象；这里是设计概念，不是 Linux 内核结构） | `CRoutine::state_`、`updated_` |
| 谁从许多任务中选择下一只 | scheduling context | `ClassicContext`、`ChoreographyContext` |
| 谁真正占用 CPU | worker | `Processor` 内的 `std::thread` |

:::{mermaid}
flowchart LR
  DV[DataVisitor<br/>payload 与 cursor]
  RF[RoutineFactory<br/>类型化函数变成无参循环]
  CR[CRoutine<br/>栈 上下文 状态 task id]
  PC[ClassicContext<br/>ProcessorContext 接口实现]
  RQ[ClassicContext::cr_group_<br/>静态组表中的优先级 vector]
  RC[RoutineContext<br/>CRoutine 持有的协程栈与寄存器上下文]
  P[Processor<br/>OS std::thread]
  PROC[Component Proc]

  DV --> RF
  RF --> CR
  P --> PC
  PC --> RQ
  RQ --> CR
  CR --> RC
  CR --> DV
  CR --> PROC
:::

这组边界刻意把“做什么”和“用哪只线程做”拆开。CRoutine 是逻辑任务，不是线程；Processor 是执行资源，不保存消息。`ProcessorContext` 是策略接口，负责向 Processor 提供“选下一只任务、没有任务时等待、关闭时退出”等操作；它既不是协程栈上下文 `RoutineContext`，也不直接拥有 Classic 的队列。`ClassicContext` 实现该接口，但同组实例实际共享的是 `ClassicContext::cr_group_` 等静态组表，其中优先级 vector 持有 `shared_ptr<CRoutine>`。`RoutineContext` 则由每只 CRoutine 组合持有，保存这只协程暂停后恢复所需的栈和寄存器状态。DataVisitor 保存数据，却不决定自己何时获得 CPU。

```text
DataNotifier callback                         Processor OS thread
      |                                             |
      v                                             |
Scheduler::NotifyProcessor(task_id)                 |
      |                                             |
      +-> CRoutine updated_ 事件标记（一位，可合并通知）|
      +-> ClassicContext::Notify() -----------------+ wake
                                                    v
                                      ProcessorContext::NextRoutine()
                                      [实际进入 ClassicContext::NextRoutine]
                                                    |
                                      CRoutine::UpdateState()
                                                    |
                                      CRoutine::Resume()
                                                    |
                                      DataVisitor::TryFetch()
                                                    |
                                      Component::Process/Proc()
```

图中的 `std::condition_variable` 是 C++ 的线程等待/通知工具：无任务的 Processor OS 线程可等待一个由 mutex（互斥锁：同一时刻只让一条线程持有）保护的谓词（根据共享状态判断是否有工作要做的条件表达式）；`notify_one()` 只让某个等待线程重新检查条件，不携带 payload，也不直接选择 CRoutine。后文会再展开它如何防止忙等、怎样处理虚假唤醒和超时。

:::{mermaid}
sequenceDiagram
  participant T as transport thread
  participant N as DataNotifier
  participant S as SchedulerClassic
  participant C as ClassicContext
  participant CV as condition_variable
  participant P as Processor OS thread
  participant R as CRoutine
  participant D as DataVisitor

  T->>N: Notify(channel_id)
  N->>S: NotifyProcessor(task_id)
  S->>R: SetUpdateFlag
  S->>C: Notify(group)
  C->>C: notify_grp[group]++ under mutex
  C->>CV: notify_one()
  opt a Processor thread is waiting
    CV-->>P: notify makes the blocked thread eligible to run
  end
  Note over P: Linux schedules it later; wait returns after mutex reacquisition
  P->>C: NextRoutine
  C->>R: Acquire + UpdateState
  P->>R: Resume
  R->>D: TryFetch
  D-->>R: shared_ptr message
  R->>R: Component Proc
  R-->>P: Yield
:::

同一条垂直线不等于同一线程。左侧通知链在 transport 线程同步运行；`notify_one` 之后，右侧 Processor 何时继续由 Linux 调度器决定；`Resume` 才从 Processor 主栈切到 CRoutine 栈。

### Task 的函数、数据与调度身份

Component 初始化时已经构造 `RoutineFactory`。Factory 中有两样东西：一段未来要运行的函数，以及与该函数配套的 DataVisitor。

`Scheduler::CreateTask()` 把工厂产物变成调度对象。先读懂签名里的 `std::function<void()>&&`：`&&` 是右值引用，允许接口接收临时函数对象；但参数一旦在函数体内有了名字，表达式就成为左值，是否真的移动还要看它传给哪个构造函数。下面是固定提交源码中的相关**删节摘录**：

```cpp
bool Scheduler::CreateTask(const RoutineFactory& factory,
                           const std::string& name) {
  return CreateTask(factory.create_routine(), name, factory.GetDataVisitor());
}

bool Scheduler::CreateTask(std::function<void()>&& func,
                           const std::string& name,
                           std::shared_ptr<DataVisitorBase> visitor) {
  if (cyber_unlikely(stop_.load())) return false;  // 省略原有日志
  auto task_id = GlobalData::RegisterTaskName(name);
  auto cr = std::make_shared<CRoutine>(func);
  cr->set_id(task_id);
  cr->set_name(name);

  if (!DispatchTask(cr)) return false;
  if (visitor != nullptr) {
    visitor->RegisterNotifyCallback([this, task_id]() {
      if (cyber_unlikely(stop_.load())) return;
      this->NotifyProcessor(task_id);
    });
  }
  return true;
}
```

这里有个值得从 C++ 值类别看清的细节：左值是指向一个有身份对象的表达式，右值常是可被接收方转移资源的临时值；`std::function<void()>&&` 允许调用端把临时函数对象交进来，但一旦参数有了名字，函数体中的 `func` 表达式就是左值。源码又把它传给 `CRoutine(const RoutineFunc&)`，因此这一步会复制函数对象，而不是因为 `&&` 自动移动。即使写 `std::move(func)`，这个 `const&` 构造参数仍会复制；要真正移动还得连同构造接口一起设计。`CRoutine` 随后通过 `make_shared` 创建并持有自己的 `func_`；工厂生成的 lambda 按值捕获 DataVisitor，Scheduler 的 task map 和策略队列再共同持有 `shared_ptr<CRoutine>`。于是 task 存活期间，函数闭包、DataVisitor、读取游标和 CacheBuffer 都沿这条所有权链存活。

task id 是调度身份，不是线程 id。多只 CRoutine 可以由同一 Processor 线程轮流执行；同一只 CRoutine 也不是永远绑定在创建它的线程上，具体约束由 scheduling policy 和 context 决定。

创建顺序同样重要：`DispatchTask(cr)` 先让策略认识这只 routine，随后才注册按 task id 唤醒它的 callback。这样第一个 notifier 事件到来时，scheduler 已经有目标对象可查。

真实 `CRoutine` 不是只有一个 callback。它把一只可调度任务所需的状态集中在同一对象中。下列 `std::atomic_flag` 是标准提供的单比特原子标记，多个线程可并发执行 test-and-set 而不会把它读成普通共享 `bool`；其精确操作语义随后展开。下面是固定提交字段摘录：

```cpp
// 固定提交源码摘录：省略 getter/setter
RoutineFunc func_;
RoutineState state_;
std::shared_ptr<RoutineContext> context_;
std::atomic_flag lock_ = ATOMIC_FLAG_INIT;
std::atomic_flag updated_ = ATOMIC_FLAG_INIT;
bool force_stop_ = false;
int processor_id_ = -1;
uint32_t priority_ = 0;
uint64_t id_ = 0;
std::string group_name_;
```

`func_` 与 `context_` 采用组合：任务拥有要执行的函数和用户态上下文，不需要通过继承为每种消息类型创建新的调度类。消息类型已经在 RoutineFactory 的 lambda 中被擦除，Scheduler 因而只存一种 `shared_ptr<CRoutine>`。

两只 atomic flag 解决不同不变量。`lock_` 是执行权，防止两个 Processor 同时 Resume 同一栈；`updated_` 是可合并事件，防止等待边界丢通知。priority、group 和 processor id 支持 Classic 分组扫描与 Choreography 绑定；把调度属性放在 task control block 中，策略无需认识业务 Component。

这里第一次出现 `std::atomic_flag`，先不要把它当作“更快的 bool”。普通 `bool` 若被一个线程写、另一个线程同时读，且没有 mutex 或原子协议，就会产生数据竞争（data race）；编译器和 CPU 都不必按源码直觉安排这些访问。`atomic_flag` 是标准提供的单比特原子对象：`test_and_set()` 以一个不可分割的读-改-写操作返回旧值并把标志置为 true，`clear()` 把它恢复为 false。于是 `lock_` 可以表达“谁成功取得执行权”，而不是把一个普通布尔值先读后写，给两个 Processor 留出同时通过的窗口。

**内存序**（memory order）描述原子操作与普通内存访问之间允许怎样重排。`memory_order_release` 常用于发布者离开临界区，`memory_order_acquire` 常用于消费者取得同一同步点后读取已发布状态；只有形成匹配的 release/acquire 关系，才可把它当作可见性边界。固定版本的 `Acquire()`/`Release()` 用 acquire/release 保护 `lock_` 的执行所有权，但 `updated_` 的 `test_and_set(memory_order_release)` 本身不是 acquire 读取，不能据此宣称 payload 已发布；消息内容的同步仍由 CacheBuffer 的 mutex 解锁/加锁配对完成。这个区别很重要：原子 flag 解决的是一个状态位的不变式，不会自动让整只对象或消息变成线程安全。

### `CreateRoutineFactory` 生成的不是一次性 callback

单输入 `CreateRoutineFactory<M0>` 返回一个永不主动结束的循环。下面是固定提交源码摘录：

这里的 `RoutineState` 是调度协议中的提示，不是 Linux 线程状态，也不是一张完整描述 routine 当前运行/阻塞的状态表。先看固定提交中的枚举和两个状态入口；这是**固定提交源码摘录**：

```cpp
using Duration = std::chrono::microseconds;
enum class RoutineState { READY, FINISHED, SLEEP, IO_WAIT, DATA_WAIT };

inline void CRoutine::HangUp() { CRoutine::Yield(RoutineState::DATA_WAIT); }

inline void CRoutine::Sleep(const Duration& sleep_duration) {
  wake_time_ = std::chrono::steady_clock::now() + sleep_duration;
  CRoutine::Yield(RoutineState::SLEEP);
}
```

`READY` 表示可以被 Processor 选取；`DATA_WAIT` 表示等数据更新，`IO_WAIT` 表示等 I/O 相关事件，`SLEEP` 带有一个到期时间，`FINISHED` 表示任务结束。`HangUp()` 不是把 Linux 线程挂起，而是在当前 CRoutine 栈上记录 `DATA_WAIT` 并让出 Processor；`Sleep()` 先记录单调时钟上的到期点，再以 `SLEEP` 让出。**单调时钟**只向前推进，不受系统校时把墙上时间拨快或拨慢影响，因此适合计算“经过了多久”。

枚举没有长期表示正在运行的 `RUNNING` 值。`DATA_WAIT` 的名字尤其容易误导：RoutineFactory 在每次 `TryFetch()` 之前都先写入 `DATA_WAIT`，即使这一轮立即取到消息并运行 `f(msg)`，字段仍暂时保留这个值；因此它不能证明 routine 已经暂停。ProcessorContext 只根据这些标签判断是否尝试 `Resume()`；真正把控制权还给 Processor 的动作发生在 `Yield()`。

在进入这个循环前，还要看清 RoutineFactory 的两层类型擦除：CreateRoutineFactory<M> 内部仍按消息类型 M 做编译期检查；交给 Scheduler 时，创建函数被擦成 std::function<std::function<void()>()>，visitor 则擦成 std::shared_ptr<DataVisitorBase>。这样 Scheduler 不必依赖 MessageT，代价是类型信息转移到 factory 和 visitor 的边界。

```cpp
template <typename M0, typename F>
RoutineFactory CreateRoutineFactory(
    F&& f, const std::shared_ptr<data::DataVisitor<M0>>& dv) {
  RoutineFactory factory;
  factory.SetDataVisitor(dv);
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
  return factory;
}
```

这里容易把“协程函数”误解成普通函数。普通函数调用后会一直运行到 return，再销毁栈帧；CRoutine 在 `Yield()` 时保存自己的执行上下文和栈位置，切回 Processor 主上下文。下次 `Resume()` 时，它从上次 Yield 后继续，而不是重新从函数第一行创建所有局部状态。

循环顶部先把自身状态设为 `DATA_WAIT`，然后才调用 `TryFetch()`。有数据时执行业务函数，并以 `READY` 状态 yield，表示可能还有 backlog，可以继续被选择；没有数据时按当前等待状态 yield，让 Processor 去做其他任务或休眠。

为什么处理一条后不是立刻进入 `DATA_WAIT`？若 ring 中已有多条消息，保持 READY 可以继续 drain，而不必等待另一个 notify。下一次循环顶部仍会先设等待态、再检查 buffer；确认真的空了才睡。

## 数据到达怎样取消等待

此时已经有了可以重复执行的 task，但 worker 仍需要在“无任务时休息”和“有数据时尽快返回”之间建立可靠约定。下面沿一条通知说明等待谓词、协程等待状态和 Processor 线程等待如何分层。

### 先把 condition variable 变成一个可运行的模型

`std::condition_variable` 是“线程暂时睡眠，直到共享条件可能改变”的等待原语。它本身不保存消息，也不保证一次通知对应一次任务；真正的状态放在 mutex 保护的普通数据中，等待线程用谓词反复检查。`std::unique_lock` 可在等待期间释放并重新取得 mutex，`std::lock_guard` 则只在离开作用域时释放锁。下面是**教学最小例子，不是 Cyber 源码**：

```cpp
std::mutex mutex;
std::condition_variable cv;
bool ready = false;

// worker
std::unique_lock<std::mutex> lock(mutex);
cv.wait(lock, [&] { return ready; });
// wait 返回时，lock 已重新持有；业务在锁外执行

// producer
{
  std::lock_guard<std::mutex> lock(mutex);
  ready = true;
}
cv.notify_one();
```

调用 `wait` 时，线程会在同一个原子步骤中释放 mutex 并进入等待；被通知后，它只是变成 **runnable**（可运行），还要重新竞争 mutex，最后由 Linux 调度器决定何时真正获得 CPU。谓词必须放进循环，因为线程可能发生**虚假唤醒**（没有对应业务事件也返回），也可能被通知后发现另一个 worker 已经消费了状态。超时等待只是周期性重查的兜底，不是业务事件计数器。

因此，`notify_one` 不能单独修复竞态。若 producer 在 consumer 检查之后、真正等待之前写入数据并通知，而“数据非空”没有被保存成可观察的状态，通知就可能没有接收者。Cyber 的 `ClassicContext` 用受 mutex 保护的组通知计数，CRoutine 又用 `updated_` 记录 task 级事件；两个层次分别解决 OS worker 等待和 routine 状态更新。

这两份“记忆”不能混为一谈。`notify_grp_` 记的是“这个 group 有 worker 需要重新扫描”，不记录是哪只 CRoutine，也不记录消息内容；`updated_` 记的是“这只 CRoutine 可能需要重新检查自己的等待状态”，它同样不保存消息。真正的 payload 仍在 `CacheBuffer`，真正是否有数据由 `DataVisitor::TryFetch()` 再确认。源码的设计意图是让一次通知即使撞上协程让出的边界，也能通过状态重查把 routine 放回候选集合；下面要同时看清它覆盖的时序和源码没有建立的同步保证。

### 最经典的丢唤醒窗口

一个错误实现可能这样写：

```text
consumer: 检查 queue -> 发现空
producer: 写入 queue -> notify
consumer: 进入 wait
```

producer 发通知时 consumer 还没有真正等待，通知没有唤醒任何人；随后 consumer 睡下，而 queue 已经非空。如果未来不再来消息，这条数据会永久卡住。

condition variable 本身不会替你记住过去发生的 notify。正确实现必须让“检查条件”和“准备等待”之间有受保护的状态，或让事件留下可被之后观察的序号/flag。

Cyber 的预期协议使用 `CRoutine::updated_` 作为一位 event latch，并依赖 RoutineFactory 的顺序关闭窗口：先写 `DATA_WAIT`，再读缓存，最后才 `Yield()`。但 `event latch` 不是“无论怎样都不会丢”的魔法；通知方还要根据 routine 当前状态决定是否清除它，因此接下来必须核对状态读写是否有共同的同步边界。

### `updated_` 的命名比布尔值更绕

在 `CRoutine` 中，producer 侧 `SetUpdateFlag()` 会 clear atomic flag；scheduler 侧 `UpdateState()` 再通过 `test_and_set` 检查并恢复置位。

其状态转换与执行权核心是**固定提交源码摘录**：

```cpp
inline RoutineState CRoutine::UpdateState() {
  if (state_ == RoutineState::SLEEP &&
      std::chrono::steady_clock::now() > wake_time_) {
    state_ = RoutineState::READY;
    return state_;
  }
  if (!updated_.test_and_set(std::memory_order_release)) {
    if (state_ == RoutineState::DATA_WAIT ||
        state_ == RoutineState::IO_WAIT) {
      state_ = RoutineState::READY;
    }
  }
  return state_;
}

inline bool CRoutine::Acquire() {
  return !lock_.test_and_set(std::memory_order_acquire);
}
inline void CRoutine::Release() {
  lock_.clear(std::memory_order_release);
}
inline void CRoutine::SetUpdateFlag() {
  updated_.clear(std::memory_order_release);
}
```

`UpdateState()` 的真实顺序值得注意：若 `SLEEP` 的截止时刻已到，它先改成 `READY` 并立即返回；只有没有走这条到期分支时，才用 `updated_` 检查异步通知。因此“时间到期”与“收到数据/I/O 更新”不是同一种唤醒原因，也不是一个布尔值的两种写法。比如周期任务在 deadline 到期时被扫描，它可直接进入 READY；等待数据的 routine 则要靠通知方留下的 flag，在扫描时把 `DATA_WAIT` 或 `IO_WAIT` 改成 READY。

直觉上可以把“clear”理解为“还有一次更新尚未被 scheduler 消费”，把“set”理解为“当前没有未消费更新”。这里不能只看变量名猜 true/false 含义，要同时阅读写侧和读侧。

`atomic_flag` 保证多个线程同时修改这一位状态时不会发生普通数据竞争。固定源码的 `test_and_set(std::memory_order_release)` 没有 acquire 语义，不能用它证明 scheduler 看见了 producer 此前写入的 payload；payload 的发布与读取由 CacheBuffer mutex 的解锁/加锁配对建立同步。这个 flag 只保存事件状态。若把 ring 换成无锁结构，就必须另行设计 acquire/release 配对，不能照搬这里的内存序。

event latch 只有一位，因此 A、B、C 三次更新可能合并成一个“有更新”。消息数量由 ring 保存，flag 只保证 consumer 最终再检查一次。

### 把竞态逐步走一遍

先按设计意图分两种时序。若消息通知发生在本轮写入 `DATA_WAIT` 之前，routine 随后仍会执行 `TryFetch()`，因此应能看到已经写入缓存的消息；若通知发生在 `DATA_WAIT` 已写入之后，通知方应清除 `updated_`，routine 让出后 scheduler 再把它从等待态改为 READY。这个顺序说明为什么等待状态要写在取数据之前：消费者不能先确认“没有数据”，然后才宣布自己睡眠。

固定源码中的访问器揭示了需要额外审慎的一点：

```cpp
inline void CRoutine::set_state(const RoutineState& state) { state_ = state; }
inline RoutineState CRoutine::state() const { return state_; }

inline void CRoutine::SetUpdateFlag() {
  updated_.clear(std::memory_order_release);
}
```

`state_` 是普通枚举，不是 atomic；这两个访问器也没有拿 mutex。routine 在 Processor 线程里写 `state_`，transport 通知线程则在 `NotifyProcessor()` 中读取它，然后才决定是否清除 `updated_`。两边没有共同锁，`updated_` 自己的原子性也不会顺带保护 `state_`。因此，当读写恰好并发时，C++ 内存模型将其视为 data race（数据竞争，程序行为未定义），不能仅凭下面的理想化交错证明固定实现对所有编译器和平台都正确。

把通知放在三个时点比较，能看出设计意图与语言保证之间的边界：

| 消息到达时点 | 按预期的下一步 | 固定实现提供的保护 |
|---|---|---|
| 本轮写入 `DATA_WAIT` 之前 | routine 接下来仍会执行 `TryFetch()`，应直接取到已写入缓存的消息 | 组通知计数要求 worker 重新扫描；此时不一定需要 task 级事件位 |
| `TryFetch()` 确认空之后、`Yield()` 之前 | 通知方看到等待态并清除 `updated_`；下一轮 `UpdateState()` 将等待态改为 READY | 原子事件位记住一次更新，避免只靠瞬时 `notify_one()` |
| 通知线程读取状态恰与 routine 写入 `DATA_WAIT` 重叠 | 需要一个明确的同步协议，才能决定状态观察顺序 | 源码中的 `state_` 是普通枚举，读写没有共同锁；并发发生时属于 C++ data race，不能从标准层面证明前两种结果 |

因此，源码确实同时用了“先声明等待、再检查缓存”和“事件位留待下一轮消费”这两种设计，但不能据此宣称整套状态访问已经线程安全。从零实现时，应让等待状态与事件记录处在一致的锁协议中，或设计经过证明的原子状态迁移；仅增加一个 `atomic_flag` 并不会自动同步对象的其他字段。

### `NotifyProcessor()` 实际还要唤醒 OS 线程

把 CRoutine 标成可能 READY 还不够。如果承载它的 Processor 正在 condition variable 上睡眠，必须让 OS 线程重新运行。

Classic 策略的 `SchedulerClassic::NotifyProcessor()` 在 task map 中找到 CRoutine。只有当它观察到 `DATA_WAIT` 或 `IO_WAIT` 时才调用 `SetUpdateFlag()`；无论当前状态如何，随后都会通知对应 group。`ClassicContext::Notify()` 增加 wake 计数并 `condition_variable::notify_one()`。

下面把通知方的真实代码并排贴出。它们是固定提交摘录，保留了 task 查找、等待态判断、组计数更新和线程通知这条完整边界：

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

void ClassicContext::Notify(const std::string& group_name) {
  (&mtx_wq_[group_name])->Mutex().lock();
  notify_grp_[group_name]++;
  (&mtx_wq_[group_name])->Mutex().unlock();
  cv_wq_[group_name].Cv().notify_one();
}
```

第一段没有直接执行 `cr->Resume()`，也没有把 payload 传给 Processor；它只把 task 标记为需要重新检查，并要求相同 group 的某个 worker 重新扫描。第二段先在互斥区内把 `notify_grp_` 加一，再释放锁并调用 `notify_one()`。事件计数先于通知写入共享状态，因而即使当时没有线程处于等待，后来进入 Wait 的 worker 仍可通过谓词看到这次通知。

同步边界可以据对象关系再核对一遍：`id_cr_lock_` 保护 task-id 到 CRoutine 的注册表，不保护 `state_`；`lock_` 由 `NextRoutine()` 获取，并由 Processor 在 `Resume()` 返回后释放，用来防止 routine 重入和保证删除时机；通知线程既不获取 `lock_`，`updated_` 也只同步自身的原子位。也就是说，状态字段上的 race 不是 `notify_grp_` 或 `lock_` 能替它消除的。上一段说明了这一实现限制；这里关注的下一层是：即使 task 状态需要修正，休眠中的 OS worker 仍要被独立通知。

完整调用是：

```text
DataNotifier::Notify(channel)
  -> visitor Notifier callback
    -> SchedulerClassic::NotifyProcessor(task_id)
      -> id_cr_ lookup
      -> croutine->SetUpdateFlag()
      -> ClassicContext::Notify()
        -> condition_variable.notify_one()
```

这个调用仍在 transport 线程中同步执行，直到 `notify_one()` 返回。真正的 Processor 何时得到 CPU 由 Linux scheduler 决定，因此条件变量通知之后还有 OS 调度延迟。

Processor 没找到 READY routine 才进入 `ClassicContext::Wait()`。真实等待代码如下：

```cpp
void ClassicContext::Wait() {
  std::unique_lock<std::mutex> lk(mtx_wrapper_->Mutex());
  cw_->Cv().wait_for(
      lk, std::chrono::milliseconds(1000),
      [&]() { return notify_grp_[current_grp] > 0; });
  if (notify_grp_[current_grp] > 0) {
    notify_grp_[current_grp]--;
  }
}
```

关闭时不能只设 `stop_`：`Wait()` 的谓词没有读取它，所以一个已经睡下的 worker 不会因为原子位变化就自己醒来。若只存入 `true`，当前 `wait_for` 仍可能等到一秒超时才回到调度循环；若把它改为没有超时的 `wait()`，而又没有通知，之后等待 `join()` 的关闭线程就可能一直卡住。固定提交的 `ClassicContext::Shutdown()` 同时设置停止位、在 Wait 共用的 mutex 下把组计数改成正数，再 `notify_all()`：

```cpp
void ClassicContext::Shutdown() {
  stop_.store(true);
  mtx_wrapper_->Mutex().lock();
  notify_grp_[current_grp] = std::numeric_limits<unsigned char>::max();
  mtx_wrapper_->Mutex().unlock();
  cw_->Cv().notify_all();
}
```

这里三个动作解决三个不同问题：原子位让之后进入 `NextRoutine()` 的 worker 看见停止；mutex 保护 `Wait()` 的谓词状态，避免仅仅发出瞬时通知却没有留下条件；`notify_all()` 让同组正在等待的多个 Processor 都有机会重新检查。恢复的 worker 返回 `NextRoutine()` 后会先读 `stop_` 并得到空指针，外层 Processor 再按自己的关闭标志退出。通知不是“杀掉线程”，而是让线程从阻塞态变为可运行，之后仍要经过 Linux 调度。

`wait_for(lock, timeout, predicate)` 先在持锁状态检查 `notify_grp_[current_grp] > 0`；为假时，它释放 mutex 并阻塞当前 OS 线程。收到通知或超时后，函数会重新取得 mutex、再检查谓词，只有谓词为真才返回真。谓词重查能处理虚假唤醒；超时让线程最多每秒重查一次，并不是用超时来模拟消息。随后代码消费一个组通知计数。因为 `Notify()` 在同一把 mutex 下先递增计数，worker 即使晚于通知才进入等待，也会看见计数而不睡下；通知只让内核线程变成 runnable，何时重新取得 mutex 并继续扫描仍由 Linux 调度器决定。

`notify_one()` 也不是指定“必须唤醒执行这个 task 的某一线程”。Classic group 的多个 Processor 可以等待同一 context，醒来的 worker 再扫描全部 routine 状态。

## 下一站：唤醒并不等于执行

到这里，消息已落入缓存，通知线程已经要求 Scheduler 重新检查任务，ClassicContext 的组通知计数也让正在休眠的 Processor 有机会重新扫描。但 `notify_one()` 本身不运行 `Proc()`：Linux 先调度 OS worker，随后 `NextRoutine()` 才更新并选择一只可运行的 CRoutine。

下一篇[Processor 与上下文切换](processor-context-switch.md)沿固定源码追踪优先级槽、`Processor::Run()`、`Resume/Yield`、协作式执行与关闭协议。本篇讨论的数据事件与任务状态，是下一篇全部执行动作的前提。
