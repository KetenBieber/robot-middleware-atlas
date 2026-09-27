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

## Processor 怎样选择并运行一只协程

通知路径完成后，Scheduler 只保证目标任务重新进入可选范围。现在把视角转到执行侧：run queue 如何保存 routine，Processor 如何等待与取任务，以及 `Resume/Yield` 怎样在同一线程上轮流运行不同栈。

### Classic run queue 实际是持久 vector

“run queue”容易让人联想到一只只包含 READY task 的队列：任务 ready 时 push，运行后 pop。Classic 并不是这样。

每个 group 有 20 个优先级槽，每个槽是一只持久的 `std::vector<std::shared_ptr<CRoutine>>`。`vector` 是可增长、连续存储的数组；在这里任务主要于注册和删除时改变成员，热路径则按顺序扫描，因而不用每次等待/运行都把同一 task 出队再入队。代价是选取可能要线性检查许多 routine，删除也需移动后续元素。task 加入后无论 READY、WAIT 还是 SLEEP 都留在 vector 中，状态决定当前能否被选择。

`ClassicContext::NextRoutine()` 从 19 降到 0 扫描。下面是固定提交中保留核心逻辑的删节摘录：

```cpp
for (int i = MAX_PRIO - 1; i >= 0; --i) {
  ReadLockGuard<AtomicRWLock> lock(lq_->at(i));

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
```

`Acquire()` 是另一只原子 flag，用来防止两个 Processor 同时执行同一 CRoutine。拿不到就跳过；拿到以后先调用前面解释的 `UpdateState()`，再判断是否 READY。

找到目标之前，worker 可能扫描许多等待 task。选择成本不是 priority heap 的 `O(log n)`，而与高优先级和当前优先级中被检查的 routine 数量相关。

### 同优先级任务的公平性边界

vector 扫描总从头开始，没有从源码看到显式 round-robin 游标，也没有让刚运行的 task 自动移到尾部。

若同优先级第一只 routine 持续有 backlog，它处理一条后 `Yield(READY)`，下一次扫描仍可能首先遇到它。靠后的 routine 只有在前者暂时不 READY、Acquire 失败或其他 Processor 并行取得前者时更容易被选中。

这不等于后续 task 必然永远饥饿，因为多个 Processor、callback 执行时间和状态变化都会影响结果；但从结构上不能宣称同优先级天然公平。工业配置应限制持续 READY 的高频 task，必要时分组或使用更明确的 processor 绑定。

一个需要公平性的自研版本可以维护每优先级的 rotating cursor，或只把 READY task 放进真正的 deque，运行一轮后排到队尾。代价是通知、去重和并发入队状态会复杂得多。

### `Processor::Run()` 才进入真正 OS 线程

`Processor` 拥有 `std::thread`，主循环不断向 context 请求下一只 routine。下面是固定提交中 `Processor::Run()` 的主循环摘录：

```cpp
void Processor::Run() {
  tid_.store(static_cast<int>(syscall(SYS_gettid)));
  while (running_.load()) {
    auto croutine = context_->NextRoutine();
    if (croutine) {
      croutine->Resume();
      croutine->Release();
    } else {
      context_->Wait();
    }
  }
}
```

没有 ready routine 时，`Wait()` 在 condition variable 上睡眠；有目标时，`Resume()` 从 Processor 主栈切换到 CRoutine 保存的栈。

RoutineFactory 恢复后调用 `DataVisitor::TryFetch()`。这一步才重新获取 CacheBuffer mutex，并把 ring 槽中的 shared pointer 复制到局部 `msg`。随后执行 Component 初始化时绑定的 lambda，依次进入 `Process(msg)` 和用户 `Proc(msg)`。

因此线程边界可以明确画在：

```text
transport thread:
  Fill -> DataNotifier -> NotifyProcessor -> condition_variable.notify_one

Processor thread:
  wake -> NextRoutine -> UpdateState -> Resume -> TryFetch -> Proc
```

消息对象通过 ring 连接两个线程，不通过调用栈直接跨越。

#### 一只 Processor 内核线程怎样被创建和绑核

`Processor::BindContext()` 把调度上下文保存为 `shared_ptr`，然后只启动一次工作线程。固定实现的主体是：

```cpp
void Processor::BindContext(
    const std::shared_ptr<ProcessorContext>& context) {
  context_ = context;
  std::call_once(thread_flag_,
                 [this]() { thread_ = std::thread(&Processor::Run, this); });
}
```

`std::call_once` 是 C++ 的一次性同步原语：多个调用者即使同时到达，也只有一个会执行启动 lambda，其他调用者等待它完成；`once_flag` 保存“已经执行过”的状态。线程进入 `Run()` 后通过 `syscall(SYS_gettid)` 取得 Linux TID。Processor 配置中的逻辑编号用于运行时标识，C++ `std::thread::id` 是标准库线程句柄的标识，POSIX `pthread_t` 是 pthread API 使用的不透明句柄，Linux TID 则是内核线程编号；不同系统调用要求不同形式，源码把 TID 传给设置 nice 值的路径，不能互换着用。

Classic 策略不是创建一个抽象的“执行器”便结束。它在循环中为每个 Processor 创建各自的 `ClassicContext` 实例；同组实例再通过 `ClassicContext` 的静态组表共享优先级队列、锁、条件变量和通知计数。随后 Scheduler 把每只 Processor 的真实线程交给资源控制函数。下面是固定提交的删节摘录：

```cpp
auto ctx = std::make_shared<ClassicContext>(group_name);
auto processor = std::make_shared<Processor>();
processor->BindContext(ctx);
SetSchedAffinity(processor->Thread(), cpuset, affinity, i);
SetSchedPolicy(processor->Thread(), processor_policy,
               processor_prio, processor->Tid());
```

`Scheduler` 的 `processors_` 与 `pctxs_` 两个 `shared_ptr` 容器分别保留 Processor 和策略上下文；`Processor::context_` 还会共享持有绑定给自己的上下文，因此创建时的局部变量离开作用域后，worker 和策略对象仍然存活。关闭时 Scheduler 先通知所有 context、删除 routine，再逐个 Stop Processor 并清空容器。Processor 把 `std::thread` 作为成员，并在 `Stop()` 中先让 context 退出等待，再 `join()` 等待 `Run()` 返回；析构函数也调用 `Stop()`。因此线程使用的 `this` 和 context 至少在正常关闭路径中会活到 worker 结束，而不是靠 detached 线程碰运气。

资源控制函数直接作用于 Processor 的 OS 线程。下面是固定提交摘录：

```cpp
void SetSchedAffinity(std::thread* thread, const std::vector<int>& cpus,
                      const std::string& affinity, int cpu_id) {
  cpu_set_t set;
  CPU_ZERO(&set);
  if (cpus.size()) {
    if (!affinity.compare("range")) {
      for (const auto cpu : cpus) {
        CPU_SET(cpu, &set);
      }
      pthread_setaffinity_np(thread->native_handle(), sizeof(set), &set);
    } else if (!affinity.compare("1to1")) {
      if (cpu_id == -1 || (uint32_t)cpu_id >= cpus.size()) {
        return;
      }
      CPU_SET(cpus[cpu_id], &set);
      pthread_setaffinity_np(thread->native_handle(), sizeof(set), &set);
    }
  }
}

void SetSchedPolicy(std::thread* thread, std::string spolicy,
                    int sched_priority, pid_t tid) {
  struct sched_param sp;
  int policy;
  memset(reinterpret_cast<void*>(&sp), 0, sizeof(sp));
  sp.sched_priority = sched_priority;
  if (!spolicy.compare("SCHED_FIFO")) {
    policy = SCHED_FIFO;
    pthread_setschedparam(thread->native_handle(), policy, &sp);
  } else if (!spolicy.compare("SCHED_RR")) {
    policy = SCHED_RR;
    pthread_setschedparam(thread->native_handle(), policy, &sp);
  } else if (!spolicy.compare("SCHED_OTHER")) {
    setpriority(PRIO_PROCESS, tid, sched_priority);
  }
}
```

`pthread_setaffinity_np()` 把可运行 CPU 集合交给 pthread API：`range` 允许集合内多个 CPU，`1to1` 只放进一个核。它限制“可以在哪些 CPU 上运行”，不保证“何时运行”。`SCHED_OTHER` 是普通分时调度；nice 值是用户可调的调度权重，数值越高通常表示给该线程更低的运行权重。`SCHED_FIFO` 与 `SCHED_RR` 属于 Linux 实时调度策略：FIFO 同优先级任务不会被新到达的同级任务抢占，会持续运行到阻塞、主动让出或被更高优先级任务抢占；RR 则为同优先级任务设置时间片并轮转。设置通常需要相应权限。注意这些调用的返回值在固定实现中没有被检查，因此配置文件写了 FIFO，不能证明内核已经接受；工程验收必须检查返回值或实际线程策略，错误的实时配置还可能让低优先级 housekeeping 线程饥饿。

#### `Resume/Yield`：换用户栈，不换内核线程

普通函数只能从入口一路运行到返回；如果要在 `Proc()` 中间暂停而稍后从原位置继续，就必须保存“下一次从哪里继续”以及那次调用仍需要的栈帧。编译器和 CPU 必须遵守一组跨函数调用的二进制约定，称为 ABI（Application Binary Interface，应用二进制接口）；其中 callee-saved 寄存器承诺在函数返回后仍保留原值，若上下文切换代码不保存它们，恢复的业务函数就可能读到错误的局部变量。下面的 `UserContext` 是说明保存边界的教学模型：

```cpp
struct UserContext {
  void* stack_pointer;       // 当前用户栈顶
  void* instruction_point;   // 下次恢复的位置
  std::uint64_t callee_saved[8];  // ABI 要求保留的寄存器
};

void Resume(UserContext* next);  // 保存当前 context，恢复 next
void Yield(UserContext* main);   // 保存 routine，恢复 Processor 主栈
```

这不是完整可运行的汇编实现，但先固定了对象边界：context 保存寄存器和栈，不保存消息，也不决定哪个 task 优先。OS 线程切换时，内核要保存/恢复线程执行状态并更新调度状态；若切换到另一进程，还要切换进程地址空间，而同一进程的两个线程通常共享地址空间。用户态协程切换只在同一线程中交换用户栈与 ABI 要求保存的寄存器，因此通常更轻，却也无法绕过阻塞系统调用。

固定源码里的 `Resume()` 和 `Yield()` 将这两个方向连起来。下面保留了停止检查、设置当前协程身份和互换栈这条主干：

```cpp
RoutineState CRoutine::Resume() {
  if (force_stop_) {
    state_ = RoutineState::FINISHED;
    return state_;
  }
  if (state_ != RoutineState::READY) {
    return state_;
  }

  current_routine_ = this;
  SwapContext(GetMainStack(), GetStack());
  current_routine_ = nullptr;
  return state_;
}

inline void CRoutine::Yield(const RoutineState& state) {
  auto routine = GetCurrentRoutine();
  routine->set_state(state);
  SwapContext(routine->GetStack(), GetMainStack());
}
```

第一次调用 `Resume()` 时，目标栈由 `MakeContext()` 预先布置，所以执行会进入 `CRoutineEntry`；之后每次回来都从上一次 `Yield()` 后继续。`Yield(state)` 先记录调度状态，再把 routine 栈切回 Processor 主栈。切换汇编返回 `Resume()` 后，Processor 才能继续释放 `Acquire()` 并挑下一个任务。协程没有自己的 Linux TID，也没有发生线程切换；改变的是同一条 OS 线程此刻使用哪一组用户栈和寄存器。

固定版本的 `CRoutine` 构造函数并非每次都直接分配新上下文：它从 `CCObjectPool<RoutineContext>` 取一个预留槽位，`shared_ptr` 的自定义删除器会把槽位归还池中；池耗尽时则警告并退回普通 `new`。以下是对象池的固定提交源码摘录：

```cpp
auto self = this->shared_from_this();
return std::shared_ptr<T>(reinterpret_cast<T*>(free_head.node),
                          [self](T* object) {
                            self->ReleaseObject(object);
                          });
```

`shared_from_this()` 来自 `std::enable_shared_from_this`：它复制对象已有的共享所有权，而不是拿裸 `this` 新建第二个 `shared_ptr` 控制块；后者会让两个独立计数都尝试销毁同一个池。返回的句柄最后一个副本销毁时，不是 `delete object`，而是把槽位放回空闲链；删除器捕获的 `self` 还保证池本身活到归还动作完成。对象池把高频创建/销毁改为复用固定容量存储，降低反复分配的开销，却需要为配置容量预留内存，也不是硬上限，因为溢出路径仍会分配。这里的 `RoutineContext` 只有栈数组和栈指针；教学模型中的寄存器数组不是其字面布局，实际寄存器保存位置由上下文切换汇编按 ABI 约定安排。

固定版本的 `RoutineContext` 内嵌 2 MiB 栈数组和保存的栈指针。下面是它的结构与 C++ 包装器摘录：

```cpp
constexpr size_t STACK_SIZE = 2 * 1024 * 1024;

struct RoutineContext {
  char stack[STACK_SIZE];
  char* sp = nullptr;
};

inline void SwapContext(char** src_sp, char** dest_sp) {
  ctx_swap(reinterpret_cast<void**>(src_sp),
           reinterpret_cast<void**>(dest_sp));
}
```

这里实际对象只有栈和栈指针；寄存器保存区放在栈顶附近，具体位置由启动时构造的初始栈布局与汇编约定。对象至少为 2 MiB 数组保留相应的虚拟地址空间，物理页则可能在使用时才逐步进入驻留集（当前实际驻留在物理内存中的进程页集合）。第一次触碰尚未映射到物理页的栈页时，CPU 会触发 page fault（缺页异常）；这通常不是程序错误，而是内核补齐页映射后重试当前指令。栈增长过程因此可能带来额外延迟，超出 2 MiB 则会越过该 context 的栈边界，不能依赖它像普通线程栈那样自动扩展。

在第一次 `Resume()` 前，`MakeContext()` 手工把栈指针、寄存器保存槽、协程入口和 `CRoutine*` 参数排成汇编预期的形状。这样第一次从这个栈恢复时，看起来就像从一次普通函数调用返回进入 `CRoutineEntry`：

```cpp
constexpr size_t REGISTERS_SIZE = 56;  // x86-64 保存槽大小
ctx->sp = ctx->stack + STACK_SIZE - 2 * sizeof(void*) - REGISTERS_SIZE;
std::memset(ctx->sp, 0, REGISTERS_SIZE);

char* sp = ctx->stack + STACK_SIZE - 2 * sizeof(void*);
*reinterpret_cast<void**>(sp) = reinterpret_cast<void*>(f1);
sp -= sizeof(void*);
*reinterpret_cast<void**>(sp) = const_cast<void*>(arg);
```

第一次出现 `sp` 时容易把它当成普通数据指针；这里它实际模拟的是上下文切换代码即将恢复的机器栈。入口地址和 `arg` 被放在特定槽位，汇编执行完寄存器恢复与 `ret` 后，CPU 就从 `f1(arg)` 的入口开始。固定实现为 x86-64 与 AArch64 分别准备了栈布局，以上是 x86-64 分支的教学相关摘录。

`Resume()` 设置当前线程的 `thread_local current_routine_`，然后把 Processor 主栈与 routine 栈交给 `SwapContext`；`Yield()` 反向交换。x86-64 的固定汇编摘录如下：

```asm
ctx_swap:
      pushq %rdi
      pushq %r12
      pushq %r13
      pushq %r14
      pushq %r15
      pushq %rbx
      pushq %rbp
      movq %rsp, (%rdi)    # 保存当前栈指针

      movq (%rsi), %rsp    # 切到目标上下文的栈
      popq %rbp
      popq %rbx
      popq %r15
      popq %r14
      popq %r13
      popq %r12
      popq %rdi
      ret
```

在 System V x86-64 调用约定中，`%rdi`、`%rsi` 是前两个指针参数寄存器，分别指向“保存当前栈指针的位置”和“目标栈指针的位置”；`%rsp` 是当前栈指针。`pushq` 将寄存器值压入当前栈，`movq %rsp,(%rdi)` 保存暂停点，切换 `%rsp` 后的一串 `popq` 从目标协程自己的栈恢复寄存器，最后 `ret` 从目标栈取出返回地址继续执行。该汇编保存 ABI 要求跨调用保持的寄存器，并额外保留启动布局需要的 `%rdi`。这里没有系统调用，也没有让内核重新选择线程，但仍有保存寄存器、改变栈工作集和 cache 行为的成本。

`thread_local` 的语义是每个 OS thread 各有一份同名实例，而不是每个 coroutine 一份；因此 `current_routine_` 保存的是当前 Processor 线程正在运行的 routine。同一线程先后 `Resume` 不同 coroutine 时，它们会依次覆盖同一份 TLS。若一只 CRoutine 在 `Proc()` 中执行阻塞 `read`、`recv` 或长时间 `sleep`，内核阻塞的是整只 Processor，挂在同一线程上的其他 CRoutine 也无法运行。协程减少线程数量，并没有消除业务代码“短、可返回、避免无界阻塞”的约束。

## 协作式调度的工程边界

运行回放已经把“消息通知”“任务可运行”“worker 被唤醒”和“routine 恢复”区分开。最后再把调度优先级、执行时间和关闭放在一起检查：协程降低线程数量，却不会让长时间运行的业务自动可抢占。

### 协作式执行的非抢占边界

进入 `CRoutine::Resume()` 后，当前 Processor 一直执行这只 routine，直到它 `Yield()` 回主栈。对 Component task 而言，Yield 位于用户 `Proc()` 返回之后。

所以一个执行 20 ms 的 `Proc()` 会占住 Processor 约 20 ms。即使期间更高 Cyber priority 的 routine 变成 READY，同一 worker 也不能从任意 C++ 指令位置安全夺回控制权。

这就是合作式调度：task 在约定边界主动让出，而不是由 Cyber scheduler 进行指令级抢占。Linux 仍可抢占整个 Processor OS 线程去运行其他 OS 线程，但那不是 Cyber routine 之间的抢占。

优点是 routine 切换点清楚，不需要每个 callback 一只内核线程；缺点是 callback 最坏执行时间和阻塞行为直接决定同组任务的长尾。

### 四种“优先级”不要混在一起

机器人系统里至少有四个层次：

1. 应用语义优先级：控制指令是否比日志或可视化重要；
2. Cyber task priority：Classic 扫描哪个 vector 在前；
3. Processor OS thread priority 与 affinity：Linux 何时运行承载 worker；
4. transport 线程 priority：SHM（shared memory，共享内存）dispatcher 或 RTPS（Real-Time Publish-Subscribe）网络监听回调何时产生数据。

提高第 2 层只改变 `NextRoutine()` 的选择顺序，不会让正在运行的低优先级 `Proc()`被打断，也不会让上游 RTPS listener 更早完成反序列化。

把 Processor 设为 `SCHED_FIFO` 也不自动解决互斥锁优先级反转：例如高优先级线程等一把由低优先级线程持有的 mutex（互斥锁），低优先级线程又被中优先级工作持续抢占，高优先级线程便会被间接拖住。是否启用优先级继承是另一层锁协议问题。

Cyber 的 Choreography 是另一种任务放置策略，能把指定 task 放到更明确的 Processor context，减少与无关组件共享扫描和执行时间；它仍要求 OS policy 实际设置成功，并要求该 Processor 上的 callback（由框架在合适时机调用的业务函数）合作式返回。

### 100 Hz 与 1 kHz 控制链的调度后果

100 Hz 控制周期是 10 ms。若同一 Processor 上另一只 callback 尚余 3 ms 才返回，新任务即使已经完成 transport、Fill 和 notify，也先承担这 3 ms 非抢占等待，再加条件变量唤醒、run queue 扫描和自身计算。

1 kHz 周期只有 1 ms。同样的固定开销占比扩大十倍，偶发内存分配器（allocator）慢路径、page fault、mutex 竞争或一次 1 ms 以上 callback 都可能跨周期。

端到端 callback 启动可拆成：

```text
Lstart = Ltransport
       + Ldispatch
       + Lnotify
       + Los_wakeup
       + Lrunqueue_scan
       + Lremaining_current_callback
       + Lcontext_switch
       + Lfetch
```

Cyber 让这些阶段可观察、可分组、可配置，却没有提供 deadline miss handler、budget enforcement 或 WCET（worst-case execution time，最坏执行时间）证明。把它称为软实时运行时更准确。

对关键控制链，常见结构选择是独立 group/CPU、很小的 input queue、短而不阻塞的 `Proc()`、预热内存、确认 OS policy/affinity 系统调用成功，并把日志、录制和可视化放到其他 group。

### Shutdown 对运行中 CRoutine 的等待

Scheduler 删除一只 routine 时，不能在它正在 Processor 栈上执行 `Proc()` 的同时释放对象。`Acquire()` flag 除了防止双 worker 重入，也让删除路径知道 task 是否仍在运行。

Classic 删除会先从 task id map 擦除并 Stop，再在 run queue 写锁下等待 `Acquire()` 成功，随后才移除对应 shared pointer。正在执行的 Processor 在 `Resume()` 返回后调用 `Release()`，删除线程才可能继续。

取得 `Acquire()` 后才从 run queue 移除，确保的是“正在 Resume 的 CRoutine 不会在执行期间从调度结构中消失”；组件通过局部 `shared_ptr` 延长自身对象寿命，也因此不会在这次 `Proc()` 尚未返回时立刻析构。但这不等于组件内部资源已经安全：固定提交的 `ComponentBase::Shutdown()` 先调用派生类 `Clear()`，之后才关闭 Reader 并 `RemoveTask()` 等待 routine 释放执行标志。如果 `Clear()` 释放了正在运行的 `Proc()` 仍在使用的成员，任务删除路径开始等待时，资源访问竞态可能已经发生。对象还活着与对象内部资源尚未释放，是两个不同的生命周期条件。关于这一具体顺序、反例和分阶段关闭方案，见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

永不返回的 `Proc()` 仍会让删除路径无限等待。C++ 没有安全通用的方法从外部杀死任意线程栈，业务代码必须设计取消点和有界 I/O；同时，释放业务资源前要有明确的“在途回调已退出”同步点，不能仅依赖原子 shutdown flag。

## 从最小执行面回到真实消息链

当任务状态、事件记忆、worker 等待和协程切换都能分别解释后，最小实现才有清楚边界：它先演示调度不变量，不冒充 Cyber 原始实现。最后再把这套不变量放回从 DataVisitor 到 `Component::Proc()` 的主链。

### 手搓最小执行面

在已有 ring、Dispatcher 和 Notifier 后，最小 task 可以先不用真正 stackful coroutine，而用显式状态机：

| 状态 | 含义 | 典型迁移 |
|---|---|---|
| `DataWait` | 没有已知工作，等待数据通知 | producer 写入后变为 `Ready` |
| `Ready` | 可以由某个 worker 领取 | worker 领取时变为 `Running` |
| `Running` | 正由一个 worker 执行 `step()` | 有积压/新通知回到 `Ready`，否则回到 `DataWait` |

这里先选择最容易检查的实现：`state` 和 `pending` 都由同一把 mutex 保护，因此它们不是原子变量。producer 写入 ring 后，在锁内把 `pending` 置为 true；若 task 当前不在执行，就把状态改成 `Ready`，随后通知 condition variable。worker 在同一把锁下检查 `Ready`、将它改成 `Running` 并清掉已消费的通知闩锁；三步不可被另一个 worker 插入。CAS（compare-and-swap：仅当当前值仍等于预期值时，才原子地改成新值）是另一种实现路线，常用于更细粒度的并发控制，但它不会自动解决多个字段之间的一致性，所以这个最小实现暂不引入它。执行一次 `step()` 后，若期间又收到通知或 ring 仍有数据，回到 `Ready`；否则进入 `DataWait`。

实现时应写出四个不变量：

```text
1. 同一 task 不得被两个 worker 同时执行
2. buffer 非空后，task 最终必须再检查一次
3. 多次 notify 可以合并，消息数量由 buffer 保存
4. stop 后不再开始新 callback，正在运行的 callback 有清晰退出/等待规则
```

下面的**教学最小例子**不用独立协程栈，只实现持久 task 与一只 worker。它刻意让 `pending` 成为布尔闩锁：十次 Notify 可以合并，但数据条数仍由 task 自己的 ring 保存。`tasks_` 用 `std::unordered_map<TaskId, shared_ptr<MiniTask>>` 建立 task id 到对象的映射；它是哈希表，通常能按 id 快速查找，但不保证遍历顺序。由于哈希表不是并发容器，示例用同一把 `mutex_` 包住 Add、查找和扫描；若一边扩容插入、一边无锁遍历，连容器内部结构都可能被破坏。

```cpp
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <utility>

using TaskId = std::uint64_t;

enum class TaskState { DataWait, Ready, Running };

struct MiniTask {
  explicit MiniTask(std::function<bool()> fn) : step(std::move(fn)) {}

  std::function<bool()> step;
  TaskState state = TaskState::DataWait;
  bool pending = false;
};

class MiniExecutor {
 public:
  bool Add(TaskId id, std::function<bool()> step) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_ || tasks_.count(id) != 0) return false;
    tasks_.emplace(id, std::make_shared<MiniTask>(std::move(step)));
    return true;
  }

  void Notify(TaskId id) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      auto it = tasks_.find(id);
      if (it == tasks_.end()) return;
      it->second->pending = true;
      if (it->second->state != TaskState::Running) {
        it->second->state = TaskState::Ready;
      }
    }
    cv_.notify_one();
  }

  void Run() {
    for (;;) {
      std::shared_ptr<MiniTask> task;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [&] { return stopping_ || HasReadyTask(); });
        if (stopping_) return;
        task = SelectReadyTask();
        task->pending = false;
        task->state = TaskState::Running;
      }

      const bool more = task->step();  // 锁外执行业务

      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (task->pending || more) {
          task->state = TaskState::Ready;
        } else {
          task->state = TaskState::DataWait;
        }
      }
      if (more) cv_.notify_one();
    }
  }

  void Stop() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    cv_.notify_all();
  }

 private:
  bool HasReadyTask() const {
    for (const auto& entry : tasks_) {
      const auto& task = entry.second;
      if (task->state == TaskState::Ready) return true;
    }
    return false;
  }
  std::shared_ptr<MiniTask> SelectReadyTask() {
    for (const auto& entry : tasks_) {
      const auto& task = entry.second;
      if (task->state == TaskState::Ready) return task;
    }
    return {};
  }
  std::mutex mutex_;
  std::condition_variable cv_;
  std::unordered_map<TaskId, std::shared_ptr<MiniTask>> tasks_;
  bool stopping_ = false;
};
```

这里的执行权不是靠一个脱离状态机的 `running` 布尔量表示：worker 在锁内把 `Ready` 改成 `Running`，所以第二个 worker 只能看到 `Running`，不会再次领取同一个 task。业务执行期间锁已释放，producer 可以继续把消息写入 ring 并设置 `pending`；回调结束时，worker 在锁内读取这个闩锁并决定回到 `Ready` 还是 `DataWait`。调用方可以用一只 `std::thread worker([&] { executor.Run(); });` 承载循环，关闭时先 `Stop()`，再 `worker.join()`。这段代码可用 C++14 编译；生产版本还需实现并发安全的 Remove、回调异常隔离，以及“正在运行的 task 何时允许删除”的等待协议。

等状态机正确后再替换为 stackful coroutine，增加 `Resume/Yield` 和独立栈。不要让协程库的上下文切换细节掩盖 runnable state 的正确性。

公平策略也应单独设计。若用 ready deque，就明确入队去重和轮转；若用持久 task vector，就明确扫描复杂度与 starvation 边界。priority、OS affinity 和实时 policy 最后再加，因为它们不能修复错误的唤醒协议。

### 回到前文的完整消息链

现在执行面各个局部机制已经有了位置：DAG 创建对象，DataVisitor 持有消费者自己的读取位置，Dispatcher 把消息写入对应缓存，Notifier 传播可合并事件，event latch 防止等待边界丢失更新，Classic 选择 routine，Processor 恢复协程。

这不是另一条新的链，而是对[完整消息回放](message-to-proc.md)中执行半边的放大：`Receiver -> Dispatcher -> Buffer -> Notifier -> Scheduler -> Processor -> Proc`。读者可以带着已解释的状态和对象关系回到该文，继续对照 INTRA、SHM、RTPS 三条路径各自的接收线程与复制点；若从零复刻，则按本文先验证 worker 等待与任务状态，再接入消息缓存和 transport。
