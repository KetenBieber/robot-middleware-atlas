# 从 READY 到 Proc：ClassicContext、Processor 与协程切栈

固定源码版本：Apollo `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。本篇紧接[数据到达怎样唤醒 CRoutine](croutine-wakeup.md)：前一篇已经区分存放消息的 `CacheBuffer`、传递事件的 `DataNotifier`、任务级 `updated_` 和 worker 等待计数 `notify_grp_`。现在从 **Processor 真正获得 CPU 的一刻**，追到业务 `Component::Proc()`。

例如相机任务刚被通知，但同一 Processor 正在运行一次 30 ms 推理。`READY` 意味着它具备被选资格，不等于 Linux 会立即抢占当前任务。接下来每一处调度判断都应回到这条执行时间线。

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
