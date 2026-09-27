# 完整消息链：从 Receiver 到 `Component::Proc()`

设想 Apollo 的前视相机刚产生第 42 帧图像。传输层已经把字节收进进程，但规划或感知组件的 `Proc()` 还没有开始运行。中间件接下来必须解决四件不同的事：把消息保存到不会无限增长的地方，找到所有订阅者，唤醒负责业务计算的执行体，并让操作系统线程真正运行它。

本章跟随这一帧数据从 transport callback 一直走到 `Component::Proc()`。即使没有阅读前面的专题，也可以把本文当作一条完整入口；ring、Dispatcher、Notifier、协程和 Scheduler 都会在它们真正出现的位置解释。

源码基线统一为 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。下文标为“固定提交源码摘录”的片段直接取自该版本；为聚焦当前机制，省略的语句会明确标出，不用源码跳转链接或文件地址代替代码。

这里的 transport 是把进程内对象、共享内存字节或网络报文送进当前进程的传输层；callback（回调）是传输库在自己的调用上下文中反向调用的一段函数，而不是一只新线程。`Component::Proc()` 则是框架最终进入业务算法的虚函数入口：基类声明 `virtual` 方法后，运行时会依据对象的实际类型调用派生组件覆写的版本。两者之间加入的 ring 是固定容量、反复复用槽位的环形缓存：它让接收方先把数据安置下来，再由另一条执行线程处理。

如果只看 Cyber RT 暴露给业务层的接口，很容易形成一个过于简单的印象：Reader 收到消息，框架调用 `Component::Proc()`，链路到此结束。本章不再把中间类当成第一次出现的名词，而是检查它们组装后是否形成一条闭合的数据、线程和生命周期路径。

真正决定运行时行为的问题却藏在这句话中间：消息由哪个线程收进来，为什么不会直接在接收线程里执行 `Proc()`，数据存在哪里，谁把协程变成可运行状态，调度器又在什么时候选中它？这些问题会直接决定一次感知—规划—控制链路的排队延迟、抖动和数据年龄。这里的 data age（数据年龄）是样本被采集到控制或算法实际使用之间经过的时间；它不仅包含函数执行时间，也会增加传输、排队和等待 worker 的延迟。

为避免后文把“进程”和“线程”混为一谈：进程是操作系统提供的地址空间与资源边界；线程是内核可以调度的一条执行流，每条线程有自己的调用栈。通信库调用 callback 时，通常是在它当前的接收/派发执行流里直接调用函数，不会因为函数叫 callback 就自动创建新线程。下文的 INTRA 指同一进程内直接传递消息的路径，SHM 是跨进程共享内存路径，RTPS（Real-Time Publish-Subscribe）是 DDS（Data Distribution Service，数据分发服务）生态常用的线缆协议；Apollo 这个固定版本通过 Fast RTPS 实现该路径。这些名字表示不同的数据入口，不表示它们必然共享同一条 OS 线程。

## 朴素的直接回调为何不够

初学者很容易把接收器写成下面这样。它是用于说明问题的**错误示例**，不是 Cyber RT 源码：

签名里的 `std::shared_ptr<Image>` 是一种共享所有权句柄：复制这个句柄不会复制整幅图像，而是让多个持有者共同延长同一个 `Image` 对象的寿命。`Proc()` 是虚函数，因此基类指针可以在运行时进入具体感知组件覆写的算法。这里的错误不在于这两种 C++ 语法，而在于把耗时算法直接放进接收回调的调用栈。

```cpp
void CameraReceiver::OnMessage(std::shared_ptr<Image> image) {
  perception_component_->Proc(image);  // 收到就直接做完整感知
}
```

单看功能，这段代码完全合理：消息到了，就调用业务函数。问题出现在运行环境。固定版本的 RTPS 路径中，Fast RTPS 的接收 listener 持有互斥锁取样，并在锁的作用域内同步调用 Cyber listener。下面先看这条回调的真实控制流；这是**固定提交源码节选**，省略中间用于填写发送方身份和时间戳的元数据语句：

```cpp
void SubListener::onNewDataMessage(eprosima::fastrtps::Subscriber* sub) {
  RETURN_IF_NULL(sub);
  RETURN_IF_NULL(callback_);
  std::lock_guard<std::mutex> lock(mutex_);

  eprosima::fastrtps::SampleInfo_t m_info;
  UnderlayMessage m;
  RETURN_IF(!sub->takeNextData(reinterpret_cast<void*>(&m), &m_info));
  RETURN_IF(m_info.sampleKind != eprosima::fastrtps::ALIVE);

  std::shared_ptr<std::string> msg_str =
      std::make_shared<std::string>(m.data());
  // 此处填入发送时间、序号等 MessageInfo 字段。
  callback_(channel_id, msg_str, msg_info_);
}
```

Cyber 接到该回调后也没有另起线程：`RtpsDispatcher::OnMessage()` 查找 channel 对应的 handler 并同步调用它。下面是**固定提交源码摘录**：

```cpp
void RtpsDispatcher::OnMessage(uint64_t channel_id,
                               const std::shared_ptr<std::string>& msg_str,
                               const MessageInfo& msg_info) {
  if (is_shutdown_.load()) {
    return;
  }
  ListenerHandlerBasePtr* handler_base = nullptr;
  if (msg_listeners_.Get(channel_id, &handler_base)) {
    auto handler =
        std::dynamic_pointer_cast<ListenerHandler<std::string>>(*handler_base);
    handler->Run(msg_str, msg_info);
  }
}
```

因此，如果把 `Proc()` 塞进这条同步回调链，一次 30 ms 视觉推理就会让这条 listener 在锁内停留 30 ms，后续同一 listener 的回调只能等它释放锁。这里的 mutex（互斥锁）是让同一把锁同时只由一条线程持有的同步工具；`std::lock_guard` 在构造时加锁、离开作用域时自动解锁，这种对象寿命管理称为 RAII（Resource Acquisition Is Initialization，资源获取即初始化）：把资源的取得与对象构造绑定，把释放与对象析构绑定。这样即使函数提前返回或抛出异常，离开作用域时也会解锁；若手工 `lock()` 后有一条返回路径忘了 `unlock()`，后续回调就可能永久等在这把锁上。mutex 保护 listener 内部状态，却不会让慢回调自动转移到别的线程。

网络 socket 的接收缓冲区是内核为 socket（网络端点）维护的有限报文队列，与 DDS reader history（Fast RTPS 在用户态保存的可靠性/历史样本状态）以及 Cyber 后文的 `CacheBuffer` 不是同一层。消息若处理不过来，具体在哪层排队、何时丢弃取决于 Fast RTPS 的线程拓扑和 QoS（Quality of Service，服务质量策略，例如可靠性和历史深度）；仅凭这个 listener 回调不能断言一定是内核 socket buffer 溢出。固定源码能确认的是：这条 listener callback 在 `SubListener::onNewDataMessage()` 内同步执行，且在互斥锁作用域中；更广泛的网络接收后果必须结合底层库配置判断。

再开一个线程也不能自动解决：每条消息创建线程会产生线程栈、内核对象和调度开销；线程池则是让固定数量的 OS worker 反复从任务队列取活，避免线程数随消息数增长，但它必须回答队列是否有界、满了丢谁、哪个任务优先、关闭时怎样等待在途任务。举个可观察的反例：相机以 100 Hz 到帧，而一个 worker 每帧处理 30 ms，稳定服务能力只有约 33 帧/秒，积压会以约 67 帧/秒增长。无界队列最终耗尽内存并让控制器处理越来越旧的图像；有界队列则必须明确是阻塞 producer、丢新帧还是覆盖旧帧。

Cyber RT 选择把链路拆成两部分：接收线程只完成解析、缓存和通知；长期存在的 Processor 线程从调度器选择已经就绪的协程，再运行 `Proc()`。这样慢业务不会直接占住 transport listener，但代价是多出排队、唤醒和调度延迟。后文看到的每个对象，都在完成这次拆分中的一个具体责任。

后面会出现不少类名，可以先把它们压缩成三个角色。假设 `/camera/front` 刚收到第 42 帧图像：

```text
放数据的人：Receiver -> DataDispatcher -> CacheBuffer
叫醒执行者：DataNotifier -> Scheduler
取数据并运行的人：DataVisitor -> CRoutine -> Component::Proc
```

第 42 帧不会作为函数参数一路穿过所有这些对象。数据路径把 `shared_ptr<Frame>`（共享所有权句柄，而非图像副本）放进缓存；通知路径只携带“某个 channel 更新了”的事件；协程被调度后再从自己的 DataVisitor 取出帧。这种分离正是全文的主线。每进入一层，都可以问三个相同的问题：第 42 帧现在存在哪里，当前是哪条线程，下一步由谁唤醒谁。

本文沿上述固定提交的正常部署路径（reality mode）追踪单输入 `Component<M0>`。尖括号中的 `M0` 是 C++ 模板参数：编译器会为具体消息类型生成一份类型明确的组件代码，因此同一个 `Component` 骨架可用于图像、点云等不同消息，而不必把 payload 擦成无类型指针。这里的 reality mode 是相对于 Cyber 仿真运行分支的正常运行路径：Reader 建立数据入口，但业务 `func` 不作为 Reader callback 直接执行，而是另建组件 task 交给 Cyber scheduler。为了回答这个问题，真正需要进入的是组件初始化、Reader、transport、缓存、协程和 scheduler 这六个模块；下文沿对象之间的调用逐步展开。

### 先把整条链放进脑中

稳定运行时存在两条不同的时间线。数据线负责把消息放进有界缓存，执行线负责让消费该缓存的协程重新获得 CPU：

:::{mermaid}
sequenceDiagram
    participant T as transport callback 线程
    participant D as DataDispatcher：把消息扇出到缓存
    participant B as CacheBuffer：每个消费者的有界存储
    participant N as DataNotifier：只报告 channel 更新
    participant S as SchedulerClassic：按 task id 路由通知
    participant C as ClassicContext：选择任务并管理等待
    participant CV as condition_variable：OS worker 等待原语
    participant P as Processor：承载协程的 OS 线程
    participant R as CRoutine：可暂停恢复的用户态任务
    participant V as DataVisitor：保存消费者游标
    participant M as Component::Proc：业务算法入口
    T->>D: Dispatch(channel_id, shared_ptr<M>)
    loop 每个已注册消费者缓存
        D->>B: Fill(shared_ptr<M>)
    end
    D->>N: Notify(channel_id)，只发事件
    loop channel 下每个 notifier callback
        N->>S: NotifyProcessor(task_id)，仍在 T 线程
        S->>R: 若 routine 正在等待，则记下“需要重查”事件
        S->>C: Notify(group)
        C->>C: 增加该 group 的通知计数
        C->>CV: notify_one()
    end
    opt 有 Processor 正在条件变量上等待
        CV-->>P: 等待结束，线程变为 runnable
    end
    Note over P: runnable 只表示可被 Linux 调度；不代表此刻已获得 CPU
    P->>C: NextRoutine() 选择可运行任务
    C->>R: UpdateState() 消费更新标记
    Note over C,R: 只有等待态才变为可运行
    C-->>P: 返回被选中的 CRoutine
    P->>R: Resume() 恢复该 routine 的栈
    R->>V: 协程取数循环调用 TryFetch()
    V->>B: 按私有游标 Fetch()
    B-->>V: 返回共享消息句柄
    V-->>R: shared_ptr<M>
    R->>M: component lambda -> Process() -> Proc()
:::

这张图最重要的是把四件事拆开：消息先写进缓存；通知回调同步记下“需要重查”并通知等待条件；Linux 让某个等待中的 Processor 线程变为可运行后，仍要决定何时给它 CPU；它拿到 CPU 后，`NextRoutine()` 才消费更新标记、把等待态转成 `READY` 并选出 routine。最后 `Resume()` 才切回 CRoutine 的用户栈，DataVisitor 再取消息并进入 `Proc()`。这几步既不是同一函数，也不一定发生在同一条线程。

Dispatcher 负责把同一对象写入各订阅缓存；DataVisitor 代表某个消费者的私有读取位置；CRoutine 保存可暂停和恢复的执行上下文；Processor 才是承载协程的 OS 线程。ProcessorContext 则把运行策略接到 Processor：它回答“下一只可运行 routine 是谁”和“暂时无任务时如何等待”，并不另建线程。前几者保存数据或执行对象，DataNotifier 是两条路径之间的通知桥。

## 初始化阶段已经把两条消费支路装好了

这里先解释为什么同一 channel 会出现两只 `DataVisitor`。Cyber RT 的普通 Reader 还要支持 `Observe()` 和 `GetLatestObserved()`，而 Component 则需要持续调用业务 `Proc()`。如果两者共用一个会出队的读取游标，就会出现“谁先读取，谁把消息从另一方手里拿走”的竞争：业务组件处理了第 42 帧以后，调试或观察接口可能再也看不到它；反过来也一样。

Cyber RT 因此不是让两名消费者争抢同一个游标，而是为 Reader 语义和 Component 语义分别建立缓存视图。二者可以共享同一个 `shared_ptr<M0>` 所指向的消息对象，但各自维护“我已经读到哪里”。先理解这个需求，再看下面的初始化代码，`Reader DataVisitor` 与 `Component DataVisitor` 就不再像无缘无故的重复对象。

消息到来之前，`Component<M0>::Initialize()` 已经创建了 Reader、组件自己的 `DataVisitor` 和一个调度任务。下面直接看这三个对象如何在初始化函数中接起来；这是**固定提交源码摘录**，编号注释用于说明调用关系：

接下来的 `[self, role_attr](...) { ... }` 是 C++ lambda（匿名函数表达式）；编译器会生成一个带成员的闭包对象，方括号里的值决定它保存什么。`weak_ptr` 是与共享所有权控制块关联、但不增加强引用计数的观察句柄；它不会延长组件寿命，调用 `lock()` 才能在对象仍存活时临时取得 `shared_ptr`。`shared_from_this()` 从一个已由 `shared_ptr` 管理的对象取回同一控制块下的拥有型句柄，不能安全地对栈对象调用；`dynamic_pointer_cast` 则在运行期检查对象的实际派生类型，成功时保留共享所有权、失败时返回空句柄。后面的 `make_shared<DataVisitor>()` 会构造堆对象并返回拥有型句柄。参数 `const std::shared_ptr<M0>& msg` 是对智能指针句柄的只读引用，不复制句柄、也不增加引用计数；`const` 限制的是这个句柄不能被重新赋值，并不代表它指向的 `M0` 内容不可修改。这里 lambda 按值捕获弱引用和配置副本，避免把局部变量引用留给稍后才运行的 task。`std::move(reader)` 把局部智能指针句柄移交给 `readers_` 容器，通常使局部句柄变空；它移动的是句柄，不是消息对象或 Reader 本体。

```cpp
std::weak_ptr<Component<M0>> self =
    std::dynamic_pointer_cast<Component<M0>>(shared_from_this());

auto func = [self, role_attr](const std::shared_ptr<M0>& msg) {
  auto ptr = self.lock();                 // ① 不让 callback 延长组件寿命
  if (ptr) {
    ptr->Process(msg);                    // ② 虚函数入口前还有关闭检查
  }
};

if (cyber_likely(is_reality_mode)) {
  reader = node_->CreateReader<M0>(reader_cfg);  // ③ 不把 func 交给 Reader
} else {
  reader = node_->CreateReader<M0>(reader_cfg, func);
}

readers_.emplace_back(std::move(reader));

data::VisitorConfig conf = {
    readers_[0]->ChannelId(),
    readers_[0]->PendingQueueSize()};
auto dv = std::make_shared<data::DataVisitor<M0>>(conf); // ④ 组件专用缓存视图

croutine::RoutineFactory factory =
    croutine::CreateRoutineFactory<M0>(func, dv);        // ⑤ 回调和取数器装进协程

return scheduler::Instance()->CreateTask(factory, node_->Name());
```

`func` 捕获的是 `weak_ptr<Component<M0>>`。如果捕获强引用，调度器持有协程、协程持有闭包、闭包再持有组件，就可能形成让组件无法析构的所有权环。弱引用把关系改成“调度任务可以尝试访问组件，但不拥有组件”。

### C++ 所有权：`shared_ptr`、`weak_ptr` 与 lambda 捕获

`std::function<void()>` 是可调用对象的统一包装器：调用方只需写 `callback()`，不必知道里面保存的是函数指针还是某种 lambda 闭包；这种隐藏具体调用对象类型的方式叫类型擦除，保存目标时也可能发生动态分配。lambda 按值捕获会把值留在闭包对象中，按引用捕获则要求变量比回调活得久，否则异步执行时会悬空。这里先把语法讲清，再用一个**教学最小例子**看强引用如何形成寿命环：

```cpp
struct Component;

struct Task {
  std::function<void()> callback;
};

struct Component : std::enable_shared_from_this<Component> {
  std::shared_ptr<Task> task;

  void StartWrong() {
    task = std::make_shared<Task>();
    task->callback = [self = shared_from_this()] {
      self->Run();
    };
  }

  void Run() {}
};
```

外部创建 `shared_ptr<Component>` 时，C++ 同时建立一块控制块，其中保存强引用计数和弱引用计数。`Component::task` 增加强计数持有 `Task`；lambda 的 `self` 又增加强计数持有 `Component`。即使外部指针已经释放，两边仍互相拥有，两个析构函数都不会执行。

下面是**教学最小例子**，展示改成弱引用后的写法，不是上游源码：

```cpp
void StartSafe() {
  task = std::make_shared<Task>();
  std::weak_ptr<Component> weak = shared_from_this();
  task->callback = [weak] {
    if (auto self = weak.lock()) {
      self->Run();
    }
  };
}
```

`[weak]` 表示 lambda 按值保存一份 `weak_ptr`，并不是保存局部变量的悬空引用。`lock()` 原子地尝试增加强引用计数：对象仍存在就返回非空 `shared_ptr`，对象已经销毁就返回空指针。在 `if` 代码块结束前，局部 `self` 又暂时保证对象不会被另一线程析构。

真实源码还调用了 `shared_from_this()`。这要求当前对象原本已经由 `shared_ptr` 管理；若对栈对象或普通 `new` 后的裸指针直接调用，无法找到有效控制块。`dynamic_pointer_cast<Component<M0>>` 则在运行期检查基类共享指针实际指向的对象类型，转换失败得到空指针，不转移或复制组件对象本身。

到这里，`weak_ptr` 解决的是组件寿命，不是线程安全。data race（数据竞争）是两个线程并发访问同一内存位置、至少一方写入非原子对象、且访问之间没有同步先后关系；在 C++ 中它会使程序行为未定义，不只是“偶尔读到旧值”。例如一个线程执行 `state = 1`，另一个同时读取普通 `state`，若两边没有共同 mutex 或正确的原子协议，就已经是数据竞争。两个线程同时访问 Component 的普通字段仍然需要锁、原子变量或单线程所有权；智能指针只保证对象在使用期间还活着，不保证对象内部状态线程安全。原子对象能使对那个单独值的操作不撕裂，但不会自动保护旁边的字段。

更容易被忽略的是第 ③ 到第 ⑤ 步。在 reality mode 下，组件创建 Reader 时没有把业务回调传进去，随后却另外创建了组件专用的 `DataVisitor` 和 task。Reader 并未因此变成空壳；它自己的 `Init()` 还会建立另一套 `DataVisitor + CRoutine`。

`Reader<MessageT>::Init()` 把 Reader 的队列入口和 transport receiver 连接起来。代码中的 `Blocker` 是 Reader 供观察 API 使用的有界消息历史，不是组件执行队列；接下来会看到它是在 Reader 自己的 routine 取到消息后写入，而不是由 transport listener 直接执行算法。下列是**固定提交源码摘录**，行尾注释为本文添加：

```cpp
if (reader_func_ != nullptr) {
  func = [this](const std::shared_ptr<MessageT>& msg) {
    this->Enqueue(msg);                   // 写入 Reader 的 Blocker
    this->reader_func_(msg);
  };
} else {
  func = [this](const std::shared_ptr<MessageT>& msg) {
    this->Enqueue(msg);                   // reality mode 仍会执行
  };
}

croutine_name_ = role_attr_.node_name() + "_" + role_attr_.channel_name();
auto dv = std::make_shared<data::DataVisitor<MessageT>>(
    role_attr_.channel_id(), pending_queue_size_);
croutine::RoutineFactory factory =
    croutine::CreateRoutineFactory<MessageT>(std::move(func), dv);
sched->CreateTask(factory, croutine_name_);

receiver_ = ReceiverManager<MessageT>::Instance()->GetReceiver(role_attr_);
```

Reader 闭包里的 `Blocker<MessageT>` 不等于组件的 `DataVisitor`。它服务于 Reader 的 `Observe()` / `GetLatestObserved()` API；固定提交把它实现为两条保存共享消息句柄的有界列表——发布历史和最近一次观察快照：

```cpp
// 固定提交源码摘录
using MessagePtr = std::shared_ptr<T>;
using MessageQueue = std::list<MessagePtr>;
MessageQueue observed_msg_queue_;
MessageQueue published_msg_queue_;
mutable std::mutex msg_mutex_;
```

`std::list` 是由独立链表节点串起来的容器，不像 ring 那样把槽位连续放在数组里。`Enqueue()` 把新句柄放在发布列表前端，并从尾部删除超出容量的旧项；`Observe()` 在同一把 `msg_mutex_` 下把句柄列表复制成观察快照。复制的是链表节点和一串 `shared_ptr`，不是图像像素，因此开销仍随历史容量线性增长，且节点可能分配内存。Reader 的公开 `GetLatestObserved()` 实际返回 `shared_ptr` 值，因此调用者可把该句柄留在本地，让消息在解锁之后继续存活。组件 `DataVisitor` 则从自己的 `CacheBuffer` 按私有游标逐条 `TryFetch()`，并参与调度通知。两套存储服务不同 API，不能把 Reader 的观察历史误当作 `Proc()` 的任务队列。真实落点见 `Blocker::Enqueue/Observe()` 与 `Blocker` 成员。

于是，一个单输入组件在正常模式下通常有两条进程内消费支路：

```text
                            +-> Reader DataVisitor -> Reader task -> Blocker::Publish
DataDispatcher(channel) ----|
                            +-> Component DataVisitor -> Component task -> Proc
```

它们共享同一个 `MessageT` 对象，却各自拥有缓存槽位、读取游标、通知项和调度任务。Reader 支路维持通用 `Reader::Observe()/GetLatestObserved()` 语义，Component 支路负责持续驱动业务计算；任何一方变慢，都不会把另一方尚未读取的数据“消费掉”。代价是每增加一条支路，就多一次 shared pointer 写槽、一次缓存加锁和一个通知项。正因为一个 channel 可以有多份独立缓存，Dispatcher 的 value 才不是“唯一 Reader”，而是一组缓存弱引用。

## Transport Receiver 不是最终回调执行者

同一消息类型和 channel 的多个 Reader 不会各自创建一套底层接收端。模板单例 `ReceiverManager<MessageT>` 用 channel name 去重，并把 transport listener 固定为 `DataDispatcher::Dispatch()`。下面的**固定提交源码摘录**展示它如何查找已存在的接收端，或创建并注册一个新接收端：

```cpp
std::lock_guard<std::mutex> lock(receiver_map_mutex_);
if (receiver_map_.count(channel_name) == 0) {
  receiver_map_[channel_name] =
      transport::Transport::Instance()->CreateReceiver<MessageT>(
          role_attr,
          [](const std::shared_ptr<MessageT>& msg,
             const transport::MessageInfo& msg_info,
             const proto::RoleAttributes& reader_attr) {
            data::DataDispatcher<MessageT>::Instance()->Dispatch(
                reader_attr.channel_id(), msg);
          });
}
return receiver_map_[channel_name];
```

这一层做了两件事。第一，transport 只需解析一次消息，进程内的多位消费者再共享 `shared_ptr<MessageT>`。第二，Reader 的数量和传输连接的数量被解耦：增加一个进程内订阅者，通常只是向 Dispatcher 注册一只新缓存，而不是重建网络端点。

代价也随之出现。底层 receiver 的角色属性和 QoS 来自第一次创建；后来的同类型同 channel Reader 复用它。阅读配置问题时，不能只看某一个 Reader 的构造参数，还要确认 receiver 是否早已存在。

`Receiver<M>::OnNewMessage()` 本身只是同步调用已保存的 listener。它不创建新线程，也没有隐藏的工作队列。因此，`Dispatch()` 最初运行在哪个线程，取决于消息由哪种 transport 送来。

### INTRA：发送调用栈上的同步扇出

同进程路径中，`IntraReceiver` 把 `OnNewMessage` 注册给 `IntraDispatcher`；`IntraDispatcher::OnMessage()` 随即执行对应 `ListenerHandler::Run()`。正常的同类型路径直接传递原 `shared_ptr<MessageT>`，既不序列化 payload，也不切换到 scheduler worker。

这意味着缓存锁竞争和通知扇出的成本会反映到发送者调用时延上。所谓“进程内零拷贝”只描述 payload 没有被复制，并不表示发送路径没有原子引用计数、mutex、容器遍历和唤醒成本。

### SHM：共享内存接收线程先解析，再进入 Dispatcher

`ShmDispatcher::Init()` 创建专用接收线程；线程循环从 notifier 取得 `(channel_id, block_index)`，定位共享内存 block，再调用已注册 listener。先看线程从哪里启动，这是**固定提交源码摘录**：

```cpp
bool ShmDispatcher::Init() {
  host_id_ = common::Hash(GlobalData::Instance()->HostIp());
  notifier_ = NotifierFactory::CreateNotifier();
  thread_ = std::thread(&ShmDispatcher::ThreadFunc, this);
  scheduler::Instance()->SetInnerThreadAttr("shm_disp", &thread_);
  return true;
}
```

`std::thread` 在这里创建的是操作系统可调度的线程；线程入口 `ThreadFunc()` 反复等待共享内存可读通知，而不是为每个消息新建线程。下面保留其循环的关键分支，省略日志及用于比较上一 block 序号的诊断代码：

```cpp
void ShmDispatcher::ThreadFunc() {
  ReadableInfo readable_info;
  while (!is_shutdown_.load()) {
    if (!notifier_->Listen(100, &readable_info)) {
      continue;
    }
    if (readable_info.host_id() != host_id_) {
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
        ReadMessage(channel_id, block_index);
      }
      if (arena_block_index != -1) {
        ReadArenaMessage(channel_id, arena_block_index);
      }
    }
  }
}
```

这里持有共享段读锁期间直接调用读取函数；`ReadMessage()`/`ReadArenaMessage()` 最终同步触发相应 listener，因此反序列化、Dispatcher 扇出和 Notifier 通知都发生在这只 SHM 接收线程上。锁对象离开花括号时析构并释放读锁，即使 `continue` 提前离开当前轮次也一样。

普通共享内存路径并不是把业务对象地址直接交给组件，而是新建 `MessageT` 并从 block 反序列化。下面是**固定提交源码摘录**，末行注释指出解析和 listener 调用仍在接收线程上：

```cpp
auto msg = std::make_shared<MessageT>();
RETURN_IF(!message::ParseFromArray(
    rb->buf,
    static_cast<int>(rb->block->msg_size()),
    msg.get()));

listener(msg, msg_info);  // 解析、扇出和通知都还在 shm dispatcher 线程
```

因此普通 SHM 消除了跨进程 socket payload 传输，却没有消除反序列化和对象分配。Arena 分支的目标更进一步：protobuf 对象及其字段可以由 arena（区域分配器，一批对象共用一段分配区，减少逐字段堆分配）承载，跨进程传递的 wrapper 保存消息相对共享段基址的偏移和相关 block 序号。这里最关键的区别不是“有没有 `shared_ptr`”，而是反序列化究竟复制对象，还是返回映射区内原对象的地址。固定提交的 protobuf 适配器直接取回 arena 中的指针，没有执行 `CopyFrom()`：

```cpp
template <typename MessageT>
bool ParseFromArenaMessageWrapper(ArenaMessageWrapper* wrapper,
                                  MessageT* message,
                                  MessageT** message_ptr) {
  *message_ptr = wrapper->GetMessage<MessageT>();
  // message->CopyFrom(*message_ptr);
  return true;
}

void* ProtobufArenaManager::GetMessage(ArenaMessageWrapper* wrapper) {
  auto segment = GetSegment(GetMessageChannelId(wrapper));
  if (!segment) {
    return nullptr;
  }
  auto address = reinterpret_cast<uint64_t>(segment->GetShmAddress()) +
                 GetMessageAddressOffset(wrapper);
  return reinterpret_cast<void*>(address);
}
```

也就是说，wrapper 不是一份 protobuf 消息副本；它保存 channel 身份和相对共享段的偏移，接收端用自己映射到的 segment 基址加偏移，重新取得 arena 中那只对象的地址。不同进程的虚拟地址本来可以不同，因而共享协议传的是偏移而不是发送进程的裸指针。adapter 先用 `make_shared` 暂时构造了一只本地 `MessageT`，随后 `reset(arena_address, custom_deleter)` 把句柄改指向映射区对象；`CopyFrom()` 没有执行。此时 `shared_ptr` 的计数只能说明还有多少代码持有这个句柄，不能说明写端仍被禁止重用对应的 arena block。

接收端随后把这个外部地址包装成 `shared_ptr<MessageT>`，并在调用 listener 前增加相关 arena block 的读计数；但读计数的移除发生在 listener 返回后，而不是最后一个 `shared_ptr` 销毁时。下面是固定提交中的删节摘录，统计代码省略，原本处于注释状态的 last-owner 解锁代码仍标成注释：

```cpp
auto msg = std::make_shared<MessageT>();
auto msg_wrapper = arena_manager->CreateMessageWrapper();
memcpy(msg_wrapper->GetData(), rb->buf, 1024);
MessageT* msg_p;
if (!message::ParseFromArenaMessageWrapper(msg_wrapper.get(), msg.get(),
                                           &msg_p)) {
  AERROR << "ParseFromArenaMessageWrapper failed";
}
auto segment = arena_manager->GetSegment(self_attr.channel_id());
auto msg_addr = reinterpret_cast<uint64_t>(msg_p);
msg.reset(reinterpret_cast<MessageT*>(msg_addr),
          [arena_manager, segment, msg_wrapper](MessageT* p) {
            // 上游把 last-owner 解锁逻辑留在注释中：
            // auto related_blocks =
            //     arena_manager->GetMessageRelatedBlocks(msg_wrapper.get());
            // for (auto block_index : related_blocks) {
            //   segment->RemoveBlockReadLock(block_index);
            // }
          });
for (auto block_index :
     arena_manager->GetMessageRelatedBlocks(msg_wrapper.get())) {
  segment->AddBlockReadLock(block_index);  // 返回值未检查
}

listener(msg, msg_info);
auto related_blocks =
    arena_manager->GetMessageRelatedBlocks(msg_wrapper.get());
for (auto block_index : related_blocks) {
  segment->RemoveBlockReadLock(block_index);
}
```

把这几行放回前面的线程链，就能看见寿命边界：SHM 接收线程持有 block 读计数时同步调用 listener；listener 把同一个消息句柄写进 `CacheBuffer` 并发出 task 通知；随后适配器立即减掉 block 读计数；Processor 线程却可能稍后才从缓存取出该指针并进入 `Proc()`。捕获 `segment` 和 `msg_wrapper` 的自定义删除器能延长 C++ 控制对象的寿命，但固定提交并没有在该删除器里保留 block 读计数。读计数降为零后，arena 写端便可能重新取得该 block；因此不能仅凭缓存中的 `shared_ptr` 推断 arena 内消息内容在延迟执行期间仍不可改写。还有一个更早的边界：代码先从 wrapper 解析出 `msg_p`，之后才尝试增加相关 block 的读计数；若写端正占有该 block，`AddBlockReadLock()` 会返回失败，但调用方忽略返回值，仍把该指针交给 listener。这个适配器片段也没有在获得读保护后再校验 block 代次。

这不是把“引用计数”换个名字。arena block 的读计数是跨进程共享区里的写入保护；`shared_ptr` 的计数只决定本进程何时调用自定义删除器。读端尝试把非负计数加一；写端则必须把空闲值 `0` 改成写占用值 `-1`。下面把两侧用于争用同一计数的核心代码并排贴出，省略的只有失败日志：

```cpp
bool ArenaSegment::AddBlockReadLock(uint64_t block_index) {
  auto& block = blocks_[block_index];
  int32_t lock_num = block.lock_num_.load();
  if (lock_num < ArenaSegmentBlock::kRWLockFree) {
    return false;  // 当前 block 正由写者占用
  }
  int32_t try_times = 0;
  while (!block.lock_num_.compare_exchange_weak(
      lock_num, lock_num + 1, std::memory_order_acq_rel,
      std::memory_order_relaxed)) {
    ++try_times;
    if (try_times == ArenaSegmentBlock::kMaxTryLockTimes) {
      return false;
    }
    lock_num = block.lock_num_.load();
    if (lock_num < ArenaSegmentBlock::kRWLockFree) {
      return false;
    }
  }
  return true;
}

bool ArenaSegment::AddBlockWriteLock(uint64_t block_index) {
  auto& block = blocks_[block_index];
  int32_t rw_lock_free = ArenaSegmentBlock::kRWLockFree;
  if (!block.lock_num_.compare_exchange_weak(
      rw_lock_free, ArenaSegmentBlock::kWriteExclusive,
      std::memory_order_acq_rel, std::memory_order_relaxed)) {
    return false;
  }
  return true;
}
```

写端不是永远使用同一个槽，而是用递增序号对 block 数取模；如果候选 block 仍被读者占用，就继续找下一个：

```cpp
uint64_t ArenaSegment::GetNextWritableBlockIndex() {
  const auto block_num = state_->struct_.block_num_.load();
  while (1) {
    uint64_t next_idx = state_->struct_.message_seq_.fetch_add(1) % block_num;
    if (AddBlockWriteLock(next_idx)) {
      return next_idx;
    }
  }
  return 0;
}
```

这里的“原子”表示这次比较和条件写入对并发参与者不可拆开观察；`compare_exchange_weak` 是原子“比较并交换”：仅当当前值仍是预期的 `0` 时，才将它改为 `-1`。若当前值不是 `0`，写端不能把该槽标成独占；若比较偶然失败，即使值后来可用，外层选槽循环也会再试。成功路径的 `memory_order_acq_rel` 同时带有 acquire（取得先前释放的同步状态）与 release（发布本次独占取得）语义，失败路径的 relaxed 只表示该次失败读取不承担额外同步。接收端提前 `RemoveBlockReadLock()` 让计数回到零后，写端就可以循环选中该槽。可见共享区映射对象仍活着，与其中某个 protobuf 对象仍受保护，是两回事。

`fetch_add(1) % block_num` 让候选位置循环回到先前槽位；`while (1)` 则说明所有候选 block 都被读者锁住时，写端不会睡眠等待，而是持续尝试。因而“把 block 锁一直留到 `shared_ptr` 最后释放”虽然能保护延迟消费者，却也可能在慢消费者长期持有消息、槽位被全部占满时让发送线程忙等。安全的替代设计不能只把解锁语句搬进删除器，还要在暴露指针前取得并校验读保护、把保护寿命延长到最后一个消费者句柄释放、处理部分加锁失败并定义池耗尽后的发送端策略；更简单的方案是在读保护有效时深拷贝到进程私有对象。这是根据固定提交的指针、加锁、解锁和轮转选槽代码得出的工程风险，不等于每次运行必然读到损坏数据。

### RTPS：listener 线程承担字符串复制和反序列化

Fast RTPS 回调 `SubListener::onNewDataMessage()` 在互斥区内取样并构造 `std::string`；Cyber 注册的类型适配回调再把字符串解析成真正的 `MessageT`。下面是**固定提交源码节选**，省略传输统计语句：

```cpp
auto listener_adapter = [listener, self_attr](
                            const std::shared_ptr<std::string>& msg_str,
                            const MessageInfo& msg_info) {
  auto msg = std::make_shared<MessageT>();
  RETURN_IF(!message::ParseFromString(*msg_str, msg.get()));
  // 此处记录传输延迟与接收状态。
  listener(msg, msg_info);
};
```

解码完成后，底层字符串和类型化消息对象不是同一块存储：前者被复制/构造并解析，后者再传给 `Receiver` 的 listener，最终进入 `DataDispatcher`。这一段分配和反序列化成本发生在 RTPS listener 的同步回调链，而非 Cyber 的 Processor 线程。

从控制链看，这一点很实用：即使 `Proc()` 被安排在独立高优先级 worker 上，前面的 RTPS listener 仍可能受到字符串分配、反序列化和其自身 OS 调度的影响。提高 Cyber task priority 不会自动缩短这部分上游时间。

## 接收线程把消息写入每个消费者的缓存

现在 transport 已把 `MessageT` 交给进程内 listener；下一步不是执行算法，而是把同一消息放进各消费者独立的缓存。这样一个消费者读取消息时，不会替另一个消费者移动游标。下面先看负责扇出的 registry，再看每只缓存的容量和落后语义。

### `DataDispatcher`：按 channel 把一个对象扇出到多只缓存

`DataDispatcher<T>` 的核心结构很小。下列为**固定提交源码摘录**：

```cpp
using BufferVector =
    std::vector<std::weak_ptr<CacheBuffer<std::shared_ptr<T>>>>;

std::mutex buffers_map_mutex_;
AtomicHashMap<uint64_t, BufferVector> buffers_map_;
```

key 是 `channel_id`，value 是该 channel 下所有 `DataVisitor` 缓存的弱引用列表。`weak_ptr` 很关键：全局 Dispatcher 可以比 Reader 和 Component 活得更久，但 registry 不应因此延长订阅者寿命。

`weak_ptr` 解决的是缓存对象的寿命，不负责保护装它们的 `std::vector`。这两个问题容易被混为一谈：即使每个元素都能安全 `lock()`，另一个线程扩容 vector 时，遍历者仍可能读到已经搬迁的存储。先看 `Dispatch()` 如何使用这张表，再回到注册与分发并发时会发生什么。

消息到来时，下面的**固定提交源码摘录**展示扇出逻辑；行尾注释是教学说明：

```cpp
bool Dispatch(const uint64_t channel_id,
              const std::shared_ptr<T>& msg) {
  BufferVector* buffers = nullptr;
  if (!buffers_map_.Get(channel_id, &buffers)) {
    return false;
  }

  for (auto& buffer_wptr : *buffers) {
    if (auto buffer = buffer_wptr.lock()) {
      std::lock_guard<std::mutex> lock(buffer->Mutex());
      buffer->Fill(msg);                  // 复制 shared_ptr，不复制 T
    }
  }

  return notifier_->Notify(channel_id);   // 数据全部落槽后再发事件
}
```

设同一 channel 注册了 `B` 只缓存，这段分发至少是 `O(B)`：逐项锁定弱引用、逐只加锁和写槽。payload 不会被复制，但 `shared_ptr` 控制块的引用计数会变化，多个核心同时处理同一对象时还可能争用那条 cache line。

这里的锁粒度有意放在单只 buffer，而不是整个 channel。这样不同缓存的消费者互不共享读取锁；但 producer 仍串行访问它们，一个慢锁会推迟后续所有 buffer 的写入和通知。对拥有许多 observer 的高频 channel，这一扇出点比 hash lookup 本身更值得关注。

`AtomicHashMap` 使用固定 bucket，避免每次查找都获取全局 map mutex；但 map 的线程安全不会自动延伸到它返回的 `BufferVector*`，因为 vector 本身仍可变。`AddBuffer()` 在 `buffers_map_mutex_` 下执行 `emplace_back()`，`Dispatch()` 遍历同一只 vector 时却没有取得这把锁。若两者并发，扩容可能搬迁 vector 的元素，而遍历线程同时读取旧存储；这构成 C++ data race，行为未定义。

这不是 API 已经替调用者保证的“只在启动时注册”。固定提交的 `NodeChannelImpl::CreateReader()` 会同步调用 `Reader::Init()`，后者构造 `DataVisitor` 并调用 `AddBuffer()`；公开调用路径没有把这一步与活跃的 `Dispatch()` 用同一把锁串行化。因此，启动期集中创建 reader、运行期只读是一种能避开冲突的使用约定，不是 Dispatcher 自身强制的不变量。若应用在消息持续到达时动态创建 reader，就必须在调用侧串行化注册，或修复 registry：例如让读写双方持有同一把锁，或在注册表中发布不可变 snapshot。后一方案避免读者与 vector 扩容竞争，但会把复制/分配成本移到注册路径。这个边界也说明 `AtomicHashMap` 只保护 map 的查找与 bucket，不等于它管理的 value 自动线程安全。

另一个长期运行问题是过期弱引用没有在这条路径中被移除。它不会造成对象泄漏，却会让反复创建和销毁 Reader 的进程不断积累空槽，增加每次 `Dispatch()` 的扫描成本。工业实现若允许动态组件重载，应让注册返回可注销 token，或定期压缩列表。

### `CacheBuffer`：有界并覆盖旧项；游标恢复还要看边界

`CacheBuffer(size)` 实际分配 `size + 1` 个槽位，用多出的空槽区分满和空。逻辑容量仍是调用者给出的 `size`。下面直接看构造、判满和覆盖写入的**固定提交源码摘录**；行尾注释是教学说明，不是上游原注释：

```cpp
explicit CacheBuffer(uint64_t size) {
  capacity_ = size + 1;                   // ① 留一格区分 full / empty
  buffer_.resize(capacity_);
}

bool Full() const {
  return capacity_ - 1 == tail_ - head_;  // ② 有效元素上限仍是 size
}

void Fill(const T& value) {
  if (fusion_callback_) {
    fusion_callback_(value);              // ③ 多输入时 M0 可改走融合路径
  } else if (Full()) {
    buffer_[GetIndex(head_)] = value;     // ④ 写入 head_ 边界槽，再淘汰最旧逻辑序号
    ++head_;
    ++tail_;
  } else {
    buffer_[GetIndex(tail_ + 1)] = value;
    ++tail_;
  }
}
```

`head_` 和 `tail_` 是单调递增的逻辑序号，真正访问数组时才取模。这比让两个下标直接在 `[0, capacity)` 内回绕更容易判断某个消费者的私有游标是否已经落后。

这里的“覆盖旧数据”首先是逻辑淘汰，不一定在同一次 `Fill()` 中物理覆盖最老消息所在的数组格。容量 3 时内部有 4 个槽；写满后的下一条数据会写到 `head_` 对应的边界槽，再同时增加 `head_` 和 `tail_`。最老序号立即不再可读，但旧槽里的 `shared_ptr` 可能留到后续一条消息才被赋新值、真正释放引用。因而逻辑可读容量仍是 3，而物理上短暂保留的消息引用最多可能是 4；完整的 A–G 轨迹见[有界消息缓存专题](pending-queue-ring.md)。

消费者并不共享一个“弹出即删除”的队头。每个 `DataVisitor` 保存自己的 `next_msg_index_`，`ChannelBuffer::Fetch()` 用它读取底层 ring。`Fetch()` 的索引修正 决定了慢消费者的真实行为。下列是**固定提交源码摘录**，说明文字已缩去：

```cpp
if (*index == 0) {
  *index = buffer_->Tail();               // 第一次只取当前最新项
} else if (*index == buffer_->Tail() + 1) {
  return false;                           // 已经追到生产者之后
} else if (*index < buffer_->Head()) {     // Head() 返回 head_ + 1，即最早有效序号
  auto interval = buffer_->Tail() - *index;
  // log dropped interval
  *index = buffer_->Tail();               // 历史已被覆盖，直接追到最新项
}
m = buffer_->at(*index);                  // 复制一份 shared_ptr
```

游标是 visitor 私有状态，不是 ring 的共享读指针。第一次读取从当前 `tail_` 开始；正常情况下 `TryFetch()` 每成功一次就把它加一；如果游标小于公开访问器 `Head()`（即私有淘汰边界 `head_ + 1`），说明对应历史已被覆盖，下一次读取直接推进到 `tail_`。游标等于 `Head()` 时则正指向最早仍有效的消息。由此可见，这不是逐条补齐全部尚未处理的历史（backlog），而是牺牲部分连续性、尽快追上新数据；容量为 3 的 A–G 轨迹和字段映射见[环形缓存章节](pending-queue-ring.md)。

这里还有一个容易被“单调递增”四个字掩盖的边界。`head_`、`tail_` 和 visitor 游标都是 `uint64_t`；`tail_ - head_` 的无符号减法在回绕时仍按模 $2^{64}$ 计算，只要两者的真实距离始终远小于计数空间，`Size()` 和 `Full()` 仍能工作。但 `index < Head()` 这种普通大小比较并不是回绕安全比较，`tail_ + 1` 也会在最大值处变成 0。现实消息速率下走满 64 位计数器几乎不可达，但从零复刻时仍应把“计数器在进程寿命内不回绕、queue size 远小于计数空间”写成不变量，或采用显式 epoch / 回绕安全的序号比较。传入的 `size` 还必须小于 `UINT64_MAX`，否则构造函数中的 `size + 1` 本身就会溢出。

因此它更接近机器人状态流常见的 freshness 语义：内存有界，消费者过慢时丢旧样本。对于里程计、姿态或当前障碍物集合，这通常比处理几十毫秒前的完整队列更合理；对于审计日志、计费或必须逐事件处理的状态机则不合适。

## 缓存保存数据，Notifier 只推动下一次检查

消息现在已经在 ring 中。还缺一件事：等待中的 consumer 怎样知道自己应该重新检查缓存。Cyber 把这件事分成“数据状态留在 buffer”和“可合并事件送到 scheduler”，因此后面的通知路径不会搬运图像本身。

### `DataNotifier` 传的是事件，不是消息

`DataVisitor<M0>` 构造时完成两项注册：把自己的 buffer 加入 `DataDispatcher<M0>`，再把 notifier 加入全局 `DataNotifier`。取数时，它只推进自己的游标。下列为**固定提交源码摘录**：

```cpp
DataVisitor(uint64_t channel_id, uint32_t queue_size)
    : buffer_(channel_id, new BufferType<M0>(queue_size)) {
  DataDispatcher<M0>::Instance()->AddBuffer(buffer_);
  data_notifier_->AddNotifier(buffer_.channel_id(), notifier_);
}

bool TryFetch(std::shared_ptr<M0>& m0) {
  if (buffer_.Fetch(&next_msg_index_, m0)) {
    ++next_msg_index_;
    return true;
  }
  return false;
}
```

Notifier 不携带 payload。Dispatcher 已经先把数据写入所有 buffer，然后才按 channel 发通知。多个快速到达的通知可以合并，消费者恢复后依然以 buffer 状态为准。这是一种很有价值的分离：调度器只管理“任务可能有工作”这一事实，不需要把任意消息类型塞进自己的运行队列（run queue，即调度器接下来要检查或执行的任务集合）。若把每条消息都复制进调度队列，调度器就必须了解所有消息类型，还可能在任务执行变慢时积累一条独立于有界 buffer 的无界积压。

关键在于这个事件如何走到 Scheduler。**固定提交源码摘录：**`DataNotifier::Notify()`：

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

`Notify()` 只按 channel 找到回调列表并逐个同步调用 `std::function<void()>`；它不创建新线程、不搬运消息，也不把 callback 放进另一个队列。因此调用它的 `DataDispatcher::Dispatch()` 所在线程会一直执行到这些通知回调返回：INTRA 下可能是发布者线程，SHM 下是共享内存派发线程，RTPS 下则是 listener 回调线程。固定源码的 `AddNotifier()` 在注册时持锁，而 `Notify()` 遍历 vector 没取同一把锁；若允许运行中动态增加 visitor，两侧并发时仍需同步，完整边界见[Dispatcher 与 Notifier 的注册/分发分析](dispatcher-notifier.md)。

创建 task 时，`Scheduler::CreateTask()` 把 visitor 的 notify callback 绑定到 task id。下列为**固定提交源码摘录**，说明性注释已标明：

```cpp
auto cr = std::make_shared<CRoutine>(func);
cr->set_id(task_id);
DispatchTask(cr);

if (visitor != nullptr) {
  visitor->RegisterNotifyCallback([this, task_id]() {
    if (stop_.load()) {
      return;
    }
    this->NotifyProcessor(task_id);
  });
}
```

于是完整唤醒关系是：

```text
DataDispatcher::Dispatch(channel)
  -> DataNotifier::Notify(channel)
    -> channel 下每个 Notifier callback
      -> Scheduler::NotifyProcessor(task_id)
        -> 找到 CRoutine
        -> SetUpdateFlag()
        -> 通知相应 ProcessorContext 的 condition variable（让空闲 worker 睡眠或重查）
```

先认识源码接下来要比较的 `RoutineState`：这是 Cyber 给 CRoutine 使用的调度标签；`DATA_WAIT` 表示上次检查时没有可取数据，`IO_WAIT` 表示 routine 等待异步 I/O。它们不是 Linux 工作线程的状态，也不意味着操作系统线程已经睡下；后文会追踪标签何时变成 `READY`。

下面把“通知”落到固定提交的调度策略函数。**固定提交源码摘录：**`SchedulerClassic::NotifyProcessor()`：

```cpp
bool SchedulerClassic::NotifyProcessor(uint64_t crid) {
  if (cyber_unlikely(stop_)) return true;
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

这里比较的两个状态只是调度器对 routine 的上次观察：如果 routine 显示正在等数据或 I/O，通知路径才清除 `updated_` 标记，提示下一轮重新检查。

这段函数只按 task id 找到 CRoutine；若它看起来在等待数据或 I/O，就清除 `updated_` 标记，再通知其 group 的等待条件。`ClassicContext::Notify()` 会增加该组的通知计数并 `notify_one()`；这里没有调用 `Resume()`，也没有进入 `Proc()`。这把 `id_cr_lock_` 保护映射查找，不是 CRoutine 执行期间的 `lock_`；状态字段跨线程是否安全，稍后还要单独核对。

所以“通知”到这里仍然没有执行 `Proc()`，也没有把 `RoutineState` 直接改成 `READY`。`SetUpdateFlag()` 只留下“下一轮要重查”的原子标记，`ClassicContext::Notify()` 只增加等待计数并通知条件变量；之后某条 Processor OS 线程要先被内核调度，再进入 `NextRoutine()`，由 `CRoutine::UpdateState()` 消费标记并把 `DATA_WAIT/IO_WAIT` 改为 `READY`。只有该 routine 随后被选中、`Resume()` 真正恢复它的栈，业务循环才会再取数据并进入 `Proc()`。

### 防止在“检查为空”和“进入休眠”之间丢通知

先看没有保护时会怎样。下面每一步都可能只相差几个 CPU 指令：

```text
时刻  消费协程                         接收线程
t1    检查 buffer，发现为空
t2                                     写入 frame 42
t3                                     发送一次 notify
t4    把自己标成等待并真正睡眠
t5    —— 没有新的 frame，也就没有第二次 notify ——
```

问题不在于 frame 42 丢了；它仍躺在 ring 里。真正丢失的是“应该再来检查一次”的事件。消费者在通知发出以后才睡下，于是缓存非空，消费者却可能一直沉睡。这类竞态通常叫 lost wakeup。

Cyber 的解法由两部分共同完成：ring 保存消息数量，`updated_` 只记住“等待边界附近发生过更新”。因此，即便 A、B、C 三次到达被合并为一个事件，消息仍由 ring 分别保存；事件位只负责阻止消费者漏掉下一次检查机会。

在看循环之前，先把源码里的 `RoutineState` 和操作系统线程状态分开。Cyber 的 `enum class RoutineState` 是调度器给每个 CRoutine 记录的“下一步是否值得尝试”的标签：`READY` 表示可以被 Processor 选中，`DATA_WAIT` 表示当前没有可取的数据，`IO_WAIT` 表示等待异步 I/O，`SLEEP` 表示等待期限，`FINISHED` 表示函数已结束。它不是 Linux 线程的 `running/blocked/runnable` 状态，也不是 CRoutine 此刻是否正在占用 CPU 的完整状态机；枚举中甚至没有 `RUNNING`。下面的循环会在调用 `TryFetch()` 前先写入 `DATA_WAIT`，即使紧接着取数成功并执行 `f(msg)`，这个字段仍可能暂时是 `DATA_WAIT`。所以它描述的是调度协议的一部分，而不能单独当成“该协程已经睡着”的事实。

这个区别解释了为什么“消息到达”不等于“Proc 已经开始”。如果 CRoutine 正在自己的 Processor 上执行，它可能在下一次 `TryFetch()` 直接取到刚入 ring 的消息；如果它已经 `Yield()` 回到 Processor 主循环，数据写入只会促成通知和一次新的调度检查。只有被选中后 `Resume()` 才会恢复协程栈，业务闭包才可能继续运行。本文后面会把这两种路径分别回放。

协程函数由 `CreateRoutineFactory<M0>` 生成。它循环尝试从 visitor 取数据，成功时调用业务函数，失败时让出执行权。下列为**固定提交源码摘录**，行尾中文注释为本文添加：

```cpp
for (;;) {
  CRoutine::GetCurrentRoutine()->set_state(RoutineState::DATA_WAIT);

  if (dv->TryFetch(msg)) {
    f(msg);                               // 最终进入 Component::Process
    CRoutine::Yield(RoutineState::READY); // 可能还有 backlog，继续可运行
  } else {
    CRoutine::Yield();                    // 没有数据，保持 DATA_WAIT
  }
}
```

顺序不能随意交换：先把自己标为 `DATA_WAIT`，再检查 buffer。否则可能出现消费者看见空队列，producer 随即写入并通知，但消费者之后才进入等待，导致这次通知无人接住。

Cyber 又用 `CRoutine::updated_` 封住这一窗口。它的类型 `std::atomic_flag` 是 C++ 提供的两态原子标记：与普通 `bool` 不同，两个线程并发操作时，`test_and_set()` 仍会作为一个不可拆开的读—改—写动作，返回旧值并把它置为 `true`。普通 bool 若被一边写、一边无同步地读，会构成前面定义的数据竞争；这个 flag 只记“至少有过一次更新”，不统计消息数，也不保存 payload。代码中的 `std::memory_order_release` 是内存序：它约束本线程此前的写入不能越过这次原子更新；若想让另一线程借它观察到这些普通写入，还需要对端匹配的 acquire 操作。producer 的 `SetUpdateFlag()` 清除此标记，scheduler 扫描 routine 时，`UpdateState()` 再用 `test_and_set()` 消费它。下列为**固定提交源码摘录**：

```cpp
if (!updated_.test_and_set(std::memory_order_release)) {
  if (state_ == RoutineState::DATA_WAIT ||
      state_ == RoutineState::IO_WAIT) {
    state_ = RoutineState::READY;
  }
}
```


这里的方向容易看反：`SetUpdateFlag()` 实际调用 `updated_.clear(std::memory_order_release)`，把 flag 清为 `false`，表示等待边界附近有更新尚待处理；`UpdateState()` 调用 `test_and_set()`，原子地取出旧值并重新设为 `true`。旧值是 `false` 时，调度器才尝试把 `DATA_WAIT/IO_WAIT` 改为 `READY`。连续多次 `clear()` 会合并成同一个待处理事件，所以它是闩锁而不是计数器。

`memory_order` 说明这个原子操作与周围普通内存读写之间允许怎样排序，不会唤醒线程，也不会自动发布消息内容。固定源码的 `test_and_set()` 使用 `memory_order_release`，它没有 acquire 语义；不能据此声称 Processor 已通过 `updated_` 看见 producer 写入的 payload。消息对象由 `CacheBuffer::Mutex()` 的解锁/加锁配对保护，`updated_` 只提供“再检查一次”的提示。若把 ring 换成无锁结构，就必须另行设计匹配的发布/读取内存序，不能把这个 release-only 用法照搬过去。

预期的握手路径由三方完成：consumer 先设 `DATA_WAIT`、再检查 buffer 并 yield；producer 写入 buffer 后调用 `SetUpdateFlag()` 并通知 context；随后某个 Processor 的 `NextRoutine()` 调用 `UpdateState()`，发现待处理 flag 后尝试把等待态改为 `READY`。因此必须分别看“缓存已经有数据”“Processor 被通知”“routine 被选中”三个事实。`updated_` 的确记录了等待边界附近发生过更新，但由于同一份普通 `state_` 也被不同线程读写，不能仅凭这个 flag 证明整个跨线程握手在 C++ 内存模型下无数据竞争；下一段会把这一边界具体展开。

不过，这段代码所表达的握手意图不能直接等同于“`state_` 已经线程安全”。固定源码中，RoutineFactory 在 Processor 线程通过 `set_state()` 写普通枚举 `state_`；transport callback 所在线程进入 `SchedulerClassic::NotifyProcessor()` 时，又在 `id_cr_lock_` 保护下读取同一个字段。该锁保护的是 task-id map；Processor 执行 routine 时并不持有它。`ClassicContext::NextRoutine()` 虽会先 `Acquire()` routine 的 `lock_` 再调用 `UpdateState()`，通知路径却没有取得这把 `lock_`。因此这些普通 `state_` 读写之间没有共同同步关系，按 C++ 内存模型存在 data race；`updated_` 是原子量并不能替旁边的普通枚举建立互斥或可见性。更不能把 release-only 的 `test_and_set()` 当成 acquire 屏障。

所以应把两件事分开判断：`updated_` 的事件闩锁说明作者试图记住等待边界附近的通知；但要对整个状态转换作线程安全证明，还需要对 `state_` 本身建立一致的同步协议。可行方向包括让读写双方使用同一把锁，或将状态转换改成经过审慎设计的原子状态机；仅把字段类型机械替换成 `atomic<RoutineState>`，仍需重新验证“检查缓存—进入等待—通知”的整体时序。这里讨论的是固定源码可见的并发边界，不代表每次运行都会出现可观察故障。

它不是一条消息对应一个计数的 semaphore，而是可合并事件：buffer 的 `head/tail` 保存“有几条数据”，事件闩锁只保证“非空 buffer 最终会被再次检查”。成功处理一条消息后，协程用 `Yield(READY)` 保持可运行，因此即使没有下一次通知，也能继续清理 backlog。

自己实现类似机制时，可以用 event flag，也可以用递增 sequence counter。关键不是选哪个原语，而是维持同一个不变量：只要 buffer 从空变为非空，消费者最终一定会再次检查它；即使多次唤醒被合并，也不能让已有数据永久睡在队列里。

## 通知怎样变成 Processor 上的一段 CPU 时间

到这里，事件已经沿 task id 到达调度侧，但“任务可运行”仍不等于“任务正在运行”。接下来把 condition variable 的线程等待、Processor 的任务选择和 CRoutine 的用户态恢复分开看，避免把这些不同层次统称为一次唤醒。

### 从通知到 CPU 执行还隔着操作系统调度

“唤醒 worker”很容易被误解成“worker 立刻执行”。当 worker 没有任务时，若反复检查队列，它会忙等并白白占 CPU；`std::condition_variable`（条件变量）让线程在一个共享条件暂不成立时睡眠，生产者改变条件后再通知它重查。条件变量本身不保存消息也不累计通知，因此真正的事实必须放在由同一把 mutex 保护的谓词里；否则通知可能先发生、worker 后睡下而丢失。`std::unique_lock` 是可显式释放和重新取得 mutex 的 RAII 锁对象，`wait()` 需要用它在睡眠时释放锁、返回前重新加锁；普通 `lock_guard` 只在作用域结束时自动解锁。下面是**教学最小例子**：

```cpp
std::mutex mutex;
std::condition_variable cv;
bool has_work = false;

void Worker() {
  std::unique_lock<std::mutex> lock(mutex);
  cv.wait(lock, [] { return has_work; });
  has_work = false;
  lock.unlock();
  RunTask();
}

void Notify() {
  {
    std::lock_guard<std::mutex> lock(mutex);
    has_work = true;
  }
  cv.notify_one();
}
```

逐行看这个例子：worker 持锁检查 `has_work`；`wait(lock, predicate)` 在条件为假时释放锁并睡眠，返回前重新持锁，再次检查谓词；`Notify()` 也先持同一把锁把谓词设真，再在锁外调用 `notify_one()`。即使通知先于 worker 的 `wait()`，谓词仍为真，worker 不会睡过去；即使条件变量发生虚假唤醒，谓词形式也会让它继续检查，而不是误跑空任务。若 worker 原本正在等待，通知只会使它变成 runnable（可运行）；它还要等 Linux 调度器分配 CPU、重新取得 mutex，`wait()` 才返回，用户任务不会在 `notify_one()` 调用栈里执行。Linux 的常见 C++ 库实现可能在真正需要阻塞时借助 futex（fast userspace mutex）系统调用；futex 是针对用户态同步字的低层内核睡眠/唤醒接口，不是消息队列，也不是每次条件变量通知都必然进入内核。

这不只是类比。固定提交中的 `ClassicContext::Wait()/Notify()` 正是用 condition variable 把空闲 Processor 停下来。下列为**固定提交源码摘录**，展示等待谓词和通知计数：

```cpp
// 固定提交源码摘录
void ClassicContext::Wait() {
  std::unique_lock<std::mutex> lk(mtx_wrapper_->Mutex());
  cw_->Cv().wait_for(lk, std::chrono::milliseconds(1000),
                     [&]() { return notify_grp_[current_grp] > 0; });
  if (notify_grp_[current_grp] > 0) {
    notify_grp_[current_grp]--;
  }
}

void ClassicContext::Notify(const std::string& group_name) {
  (&mtx_wq_[group_name])->Mutex().lock();
  notify_grp_[group_name]++;
  (&mtx_wq_[group_name])->Mutex().unlock();
  cv_wq_[group_name].Cv().notify_one();
}
```

`notify_grp_` 是由同一把 mutex 保护的等待谓词：通知先把计数加一再唤醒，worker 即使晚一点进入 `wait_for()`，也会先看到谓词为真而不睡下。1000 ms 超时则提供一次兜底重查，并不表示正常消息需要等待一秒。Choreography 的专用 context 使用同样的“计数 + condition variable”结构，只是通知目标是绑定的 Processor，而不是 Classic group 中任意一个空闲 worker。

条件变量允许虚假唤醒，所以 `wait(lock, predicate)` 会反复检查 `has_work`。这个布尔状态与 Cyber 的消息 ring / update flag 扮演相似角色：通知可以合并或偶然发生，正确性最终依赖一个可重新检查的状态条件，不能依赖“每次 notify 必须精确对应一次执行”。

通知不会把发送者的优先级“传”给被通知的线程。Linux 的 `SCHED_OTHER` 是普通分时调度：内核在可运行线程之间分配 CPU，Cyber 的 `SetSchedPolicy()` 对这一策略通过 `setpriority()` 设置 nice 值（影响分时权重的数值），而不是传入实时静态优先级。`SCHED_FIFO` 则是实时策略：固定优先级线程不会按普通时间片轮转，通常要等它阻塞、主动让出，或被更高优先级线程抢占才离开 CPU；配置错误可能饿死低优先级系统工作。Linux 的 [调度策略说明](https://man7.org/linux/man-pages/man7/sched.7.html)可对照 Cyber 的 `SetSchedPolicy()`。此处固定实现没有检查 `pthread_setschedparam()` 的返回值，系统权限也可能拒绝实时策略；配置文件写了 FIFO 并不能证明该线程实际运行在 FIFO 下。

还要分清三种容易混叫“优先级”的东西：Cyber routine priority 决定 `ClassicContext` 扫描哪一组任务；OS policy/priority 决定 Linux 在线程层面何时给 Processor CPU；CPU affinity（CPU 亲和性）只是允许线程运行的 CPU 集合，不是优先级，也不预留这些 CPU。Cyber 的 `SetSchedAffinity()` 调用 `pthread_setaffinity_np()` 设置掩码；[Linux 文档](https://man7.org/linux/man-pages/man3/pthread_setaffinity_np.3.html)说明实际集合还会受系统在线 CPU 和 cpuset 限制。固定辅助函数也不检查该系统调用的返回值，因此配置 affinity 仍不等于内核已接受。

最后，priority inversion（优先级反转）不是“高优先级设置失败”：低优先级线程先拿住 mutex，高优先级 Processor 随后等待它，而中优先级线程又不断抢占低优先级线程，于是高优先级工作被间接拖延。`notify_one()` 只通知等待者，不会自动提升锁持有者优先级；是否启用了优先级继承必须另查 mutex 协议，不能从 Cyber task priority 推出。

### `Processor` 才是 `Proc()` 所在的 OS 线程

这时要把“工作放在哪里”和“谁在线程上取工作”分开。`ProcessorContext` 是策略接口，向 `Processor` 提供 `NextRoutine()`、`Wait()` 和关闭动作；它不是线程，也不是一个正在执行的任务。Classic 实现通过 context 访问按 group/priority 组织的 routine 表、锁和等待条件；`Processor` 持有 context 的共享引用，并运行真正的 OS 线程。接口与具体实现的关系在前面的对象关系图中已经给出；下面直接看 `Processor::Run()` 如何通过该接口取任务或等待。

因此 Scheduler 先把 `CRoutine` 路由到某种策略维护的运行表；Processor 每轮向 context 要一个可执行 routine。若取不到，context 才让这条 OS 线程等待通知或超时，而不是让它持续空转。下列为固定源码中的 `Processor::Run()`：

```cpp
while (running_.load()) {
  auto croutine = context_->NextRoutine();
  if (croutine) {
    croutine->Resume();
    croutine->Release();
  } else {
    context_->Wait();
  }
}
```

协程不能只靠一个“暂停”标志保存 C++ 函数走到哪一步：它还需要自己的栈和暂停点。Cyber 的 `RoutineContext` 持有独立栈与保存的栈指针 `sp`；`SwapContext()` 把 ABI 要求跨调用保留的寄存器保存到该栈，再装入另一份栈指针和寄存器现场。ABI（应用二进制接口）规定函数调用时哪些寄存器由调用方保存、哪些必须由被调用方保持。这个交换发生在同一条 Processor OS 线程内，不创建线程，也不请求内核切换到另一条线程。`current_routine_` 是 `thread_local`（线程局部变量），每个 OS 线程各有一份，用来让当前 Processor 上正在运行的代码找到自己的 CRoutine。

有了这个最小模型再读代码：`Resume()` 做的不是“通知协程”，而是从当前 Processor 的主栈切到这个 CRoutine 保存的栈。**固定提交源码摘录：**`CRoutine::Resume()` 设置当前线程的 `current_routine_`，交换栈指针；协程之后调用 `Yield()` 时，执行流回到这一行之后，`Resume()` 才清除线程局部指针并返回：

```cpp
// 固定提交源码摘录
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
  SwapContext(GetMainStack(), GetStack());  // 主栈 -> routine 栈
  current_routine_ = nullptr;               // routine yield/finish 后返回
  return state_;
}
```

协程那一侧的 `Yield()` 做相反方向的交换。**固定提交源码摘录：**`CRoutine::Yield()` 先按需更新状态，再把当前 routine 的保存位置写回 `RoutineContext::sp`，切回 Processor 主栈：

```cpp
inline void CRoutine::Yield(const RoutineState& state) {
  auto routine = GetCurrentRoutine();
  routine->set_state(state);
  SwapContext(routine->GetStack(), GetMainStack());  // routine 栈 -> 主栈
}
```

为什么一组保存位置能继续执行？普通 C++ 函数调用按 ABI 约定使用寄存器和栈：调用者保存可丢弃的临时寄存器，被调用者必须保留约定寄存器。Cyber 的 `RoutineContext` 直接内嵌 `char stack[2 * 1024 * 1024]` 和一个栈指针 `sp`，所以每个 CRoutine 的上下文对象带有约 2 MiB 的栈存储预算；100 个 routine 的这些栈按对象大小合计约 200 MiB，物理驻留量则取决于实际访问过哪些页。`Proc()` 的大型局部数组或深递归会消耗这只 routine 栈，而不是 Processor 主栈。

进程看到的虚拟地址由操作系统按页映射；“缺页”表示 CPU 访问的页当前需要内核处理，可能是首次为匿名内存建立映射，并不必然意味着从磁盘读数据。内核处理后会重试触发访问的指令。因而 routine 第一次触碰新的栈页时，可能额外经历缺页处理并造成调度抖动；“协程切换只换几个寄存器”不等于整个执行路径没有内存延迟。栈页、page fault 与上下文切换成本的展开见[调度与协程专题](croutine-wakeup.md)。`MakeContext()` 第一次构造 context 时，还在新栈顶摆好入口函数 `CRoutineEntry` 与参数，使首次 `Resume()` 能像返回到一个已经准备好的调用现场那样进入协程。

固定提交的 x86-64 `ctx_swap` 通过保存源 `%rsp`、装入目标 `%rsp`、恢复 ABI 要求保留的寄存器并 `ret`，在两个已准备好的用户栈之间切换。首次进入协程时，`MakeContext()` 预置入口和参数；之后则从上次 `Yield()` 留下的返回位置继续。这里保留调用链所需的含义，不重复展开逐条汇编；寄存器清单、栈布局和首次 trampoline 的完整解释见[CRoutine 上下文切换专题](croutine-wakeup.md)。

这和内核线程切换有清楚边界：这里没有创建或切换 Linux 线程，也不需要让内核调度另一个线程；同一个 Processor OS 线程仍在运行，只是换了用户态栈和少量寄存器。它也没有保存完整 CPU 状态、信号屏蔽字或任意线程局部资源，因此不是通用的线程迁移机制。切换很轻，但若 routine 调用阻塞式系统调用，阻塞的是承载它的整个 Processor 线程，同线程上的其他协程也会停住。`context_` 由 CRoutine 持有的 `shared_ptr` 管理；栈帧中的局部对象会一直留在该栈上，直到协程恢复后正常退出/析构，不能把 `Yield()` 当成函数返回。

只有 `Resume()` 真正恢复栈之后，循环中的 `TryFetch()` 才可能取得 `shared_ptr<M0>` 并调用组件闭包，闭包再进入 `Process()`。下列是固定提交源码摘录：

```cpp
bool Component<M0, NullType, NullType, NullType>::Process(
    const std::shared_ptr<M0>& msg) {
  if (is_shutdown_.load()) {
    return true;
  }
  return Proc(msg);
}
```

至此才能准确回答最初的问题：`Proc()` 通常运行在 Cyber scheduler 创建的 `Processor` OS 线程中，不在 SHM dispatcher、RTPS listener 或 INTRA publisher 的调用栈上。前者负责执行，后者负责生产数据和事件。

#### 协程和线程不是同一种调度对象

`Processor` 是操作系统能够看见的内核线程，拥有内核调度实体、线程栈和 OS 优先级。`CRoutine` 是 Cyber 在用户态管理的协程：它保存自己的栈、寄存器现场和运行状态，却必须借用某个 Processor 才能执行。

可以把关系理解为：

```text
一个进程
  ├─ RTPS / SHM 接收线程
  └─ 多个 Processor 内核线程
       └─ 每个线程轮流 Resume 多个 CRoutine
```

从协程 A 切到协程 B，通常只需在用户态保存和恢复寄存器、栈指针，不必让内核完成一次完整线程调度，所以成本可以较低。但这不意味着协程可以安全调用任意阻塞函数。如果 A 在某个 Processor 上执行阻塞式 socket read，内核阻塞的是整个 Processor 线程；挂在同一线程上的 B、C 协程也无法运行。合作式调度要求业务代码尽快返回或在框架认可的位置 yield。

`CRoutine::Resume()` 也不是新建线程。它把当前 Processor 的执行流切换到协程保存的栈；`Yield()` 再保存当前位置并返回调度循环。因此 `Proc()` 中的普通局部变量位于协程栈上，yield 后仍需保留；组件成员则位于进程堆对象中，可能被其他线程访问，需要另外分析同步关系。

这也是 transport 与 scheduler 分层的核心价值。网络接收线程不需要承担不可预测的业务 WCET（Worst-Case Execution Time，最坏执行时间：在明确的硬件、输入和系统负载假设下，一段任务可能消耗的最大执行时间；它不是平均耗时，也不是脱离运行条件的绝对常数）；组件可以使用统一的 task priority、group、CPU affinity 和调度策略；同一套 `DataVisitor` 还能接入 INTRA、SHM、RTPS 三种上游。

代价是一次消息至少多出缓存写入、事件分发、worker 唤醒、run queue 选择和协程切换。它减少了接收线程被业务阻塞的风险，却没有让延迟凭空消失，而是把延迟变成更容易配置和隔离的几个阶段。

### Classic scheduler 的 priority 不是抢占式实时调度

Classic 策略按优先级从高到低扫描持久化的 routine vector。核心选择逻辑实际位于 `ClassicContext::NextRoutine()`，下列为固定提交源码摘录：

```cpp
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
```

这里不是“READY task 被弹出、运行后排到队尾”的典型队列，而是从高优先级 vector 前端反复扫描。选择成本与扫描到目标前经过的 routine 数量相关；同优先级任务也没有从这段代码中得到显式 round-robin 游标。若靠前任务持续保持 READY，靠后任务可能承受额外等待。

尚未开始运行的 routine 受 `NextRoutine()` 的扫描顺序影响；已经进入 `Proc()` 的 routine 则受合作式执行约束。一个 `Proc()` 在返回或主动 yield 之前，同一 Processor 不会因为另一只更高优先级 Cyber task 就强行抢占它。因此需要区分四个层次：

1. 控制业务认为谁更重要；
2. Cyber run queue 中的 task priority；
3. Processor OS 线程采用的 policy、priority 和 CPU affinity；
4. SHM/RTPS 等 transport 线程自己的 OS 调度属性。

只调高第 2 层，不会缩短正在执行的低优先级 `Proc()`，也不会自动提高 transport listener 的优先级。把 Processor 设成 `SCHED_FIFO` 同样不能消除普通 mutex 引起的优先级反转。

Choreography 策略把指定任务绑定到更明确的 Processor，隔离性比共享扫描更直观，但应用仍需约束 callback 的最坏执行时间（WCET）、阻塞行为和内存分配。这里说的“软实时”是指错过截止时间会降低控制或感知质量、但系统不把每次超时都定义为灾难性安全事故；Cyber 提供的是构建这种执行拓扑的工具，不是端到端截止时间证明，也不会替应用证明每个 callback 都能按时完成。

多输入版本会在 M0 buffer 上安装 `AllLatest` 融合 callback：M0 触发，其他输入只提供当时的 latest snapshot。这是“主输入到来时采样并保持其他输入最新值”的 sample-and-hold，不是时间同步器；其锁序、tuple 分配和数据年龄需要单独沿多输入链展开。

## 有界内存下的过载与丢数据语义

设 producer 周期为 `Tp`，某个 visitor 的逻辑队列深度为 `P`。消费者没有落后时，它可以按序读取保留窗口；最老样本仅由队列造成的数据年龄上限接近 `(P - 1) * Tp`。一旦落后超过 ring 窗口，下一次 fetch 会追到当前尾部，跳过已覆盖历史。

过载链可以按源码行为写成：

```text
producer 继续 Dispatch
  -> ring 满时最旧逻辑消息退出可读区（物理 shared_ptr 可能下一次写槽才释放）
  -> 多次通知可以合并
  -> routine 每处理一条后保持 READY，尝试清 backlog
  -> visitor 游标落到 head 之前时跳到 tail
```

所以它的取舍是“有界内存 + producer 通常不等待业务消费者 + 允许丢旧数据”。但 producer 仍可能在 `Dispatch()` 中等待 buffer mutex 和 notifier 扇出；不能把它称为完整的 non-blocking data path。

对于 100 Hz 输入，`P=10` 代表约 90 ms 的历史窗口。若控制器逐条追赶，这些旧状态通常已经失去价值；`P=1` 更接近 keep-last。对于 1 kHz 输入，同样的深度也意味着约 9 个控制周期。队列深度不是单纯的吞吐参数，而是在“保留连续历史”和“保证数据新鲜”之间选择。

端到端 callback 启动时间可以拆成下面这些区段。它们不是固定常数；在不同 transport 和调度策略下，有些区段可能为零、互相重叠或受批处理影响：

```text
Lstart = Ltransport_decode
       + Ldispatcher_fanout_and_buffer_locks
       + Lnotifier_and_scheduler_bookkeeping
       + Lprocessor_wakeup
       + Lrunqueue_scan
       + Lcurrent_nonpreemptive_callback
       + Lcoroutine_resume
       + Lvisitor_fetch
```

闭环完成时间还要再加 `Cproc` 和下游 actuator path。尤其是 `Lcurrent_nonpreemptive_callback`：若同一 Processor 正在执行另一个 2 ms 的 `Proc()`，新到达的高优先级 task 即使已经被唤醒，也可能先承担这段剩余执行时间。RTPS 字符串分配、protobuf 解析、shared pointer 原子计数、mutex 竞争、condition-variable 唤醒、page fault 和 OS 抢占也会形成长尾。协程切换通常比内核线程切换轻，但它无法抵消链路其他部分的不确定性。

## 关闭标志先挡住后续入口，但不等于已经排空在途 Proc

`ComponentBase::Shutdown()` 的拆解顺序与初始化大致成镜像：

```text
设置 Component shutdown flag
  -> Clear()
  -> Reader::Shutdown(): topology / receiver 引用 / Reader task
  -> 移除 Component task，并等待正在持有执行权的 routine
  -> DataVisitor 与 CacheBuffer 析构
  -> Dispatcher 中 weak buffer 仍可能留槽，但 lock() 失败
```

入口处的 `Process()` 检查使在 shutdown flag 设为 true 之后才进入该检查的消息不再调用 `Proc()`。它并不会中断已经越过检查、正在执行的业务函数。注意两个动作的区别：原子布尔值解决的是关闭标志本身的并发读写；“等所有在途 Proc 退出”需要另一个明确的同步边界。

**固定提交源码摘录：**ComponentBase::Shutdown() 的顺序是先调用派生类 `Clear()`，再逐个关闭 Reader，最后移除 Component task。单输入组件的 Component::Process() 则在读取标志后直接进入 `Proc(msg)`，没有在这两步之间加锁，也没有先等待 task 排空。

例如 `Proc()` 正在访问派生类的 `detector_`，此时 mainboard 线程进入 `Shutdown()` 并在 `Clear()` 中释放它，正在运行的 `Proc()` 仍可能继续使用已经释放的对象。随后发生的 `RemoveTask()` 确实会停止并移除 CRoutine；Classic 策略在 ClassicContext::RemoveCRoutine() 中等待 routine 释放执行标志，但等待发生在 `Clear()` 之后，所以不能反过来保护 `Clear()` 已经释放的资源。

因此，应把源码事实和推荐协议区分开：Cyber 先置关闭标志，再执行业务 `Clear()`、关闭 Reader，最后等待并移除 Component task；如果重新设计关闭协议，更安全的常见顺序是先禁止新输入，再停止输入侧 task，等待可能已进入 `Proc()` 的 Component task 退出，最后释放 `Proc()` 使用的资源。后一顺序是依据在途访问风险提出的设计建议，不是当前 Apollo 的执行顺序。派生组件若沿用现有 API，不能假设 `Clear()` 被调用时 `Proc()` 已经结束，应核查资源是否共享，并自行建立必要同步。

`Reader::Shutdown()` 退出 topology、释放自己的 receiver/channel manager 引用，并删除 Reader task。task 消失后，其 `DataVisitor` 和缓存可以析构；Dispatcher 中相应 `weak_ptr::lock()` 随后失败，不再向该缓存写数据。

不过 ReceiverManager 还可能长期持有按 channel 共享的 receiver，所以 `reader->receiver_ = nullptr` 不必然立即销毁底层 transport 对象。Notifier registry 也可能保留旧回调；回调使用 task id 查找 scheduler 状态，目标任务不存在时只能返回失败。这些残留项主要带来扫描开销，也说明完整的动态卸载设计需要显式注销，而不能只依赖弱引用兜底。

移除正在运行的 routine 时，scheduler 需要等其 `Acquire()` 执行权释放。若某个 `Proc()` 永不返回，局部 shutdown 也可能被永久拖住。合作式调度把 callback 的终止责任交给业务代码，因此组件实现必须能响应自身停止条件，不能在 `Proc()` 中做无期限阻塞 I/O。

全局 `Scheduler::Shutdown()` 则让各 context 退出等待、逐项移除 routine，再停止并 join Processor 线程。全局 `cyber::IsShutdown()` 与单个组件的 `is_shutdown_` 是两层条件：一个控制进程级数据入口，一个保护局部业务对象，不能混为同一个生命周期开关。

## 从完整消息链提取运行时设计

如果从零实现一个 Cyber-like 最小运行时，最稳妥的顺序不是先造一个庞大 scheduler，而是沿依赖逐层闭合：

先实现固定容量的 `shared_ptr` ring，写清 overwrite-oldest、消费者私有游标和落后时的跳转规则。再用 `ChannelBuffer` 把 channel identity 与存储组合起来，让多个 visitor 能独立消费。

随后实现 `channel_id -> weak buffer list` 的 Dispatcher registry。若系统允许运行中注册和注销，必须从一开始定义 snapshot 或读写同步，而不是假设 vector 永远不变。Notifier 只传事件，不传 payload，并返回可注销句柄。

有了数据面，再实现“设为 WAIT → 检查 buffer → yield”的 consumer loop和防丢唤醒 event latch。`ProcessorContext` 只负责选择 ready routine，`Processor` 只负责 OS 线程上的 select/resume/wait；这种分层让 Classic、绑定式或 deadline-aware 策略能够替换，而业务对象不知道具体 scheduler。

最后再接 Component wrapper 和不同 transport。每接一种 transport，都标清 payload 第一次分配、复制或解析的位置，以及 listener 所在线程。到这一步，系统才真正拥有可解释的端到端延迟链。

下面是一个可单独编译的**教学最小复刻**。它不实现协程、registry 和优先级，只保留发布线程不直接执行业务、缓存有界、等待条件可重查这三个性质。先选清楚缓存与所有权：`std::deque` 是可从两端插入和删除的序列，适合在容量满时从队首丢弃最旧样本、从队尾取最新样本；`std::shared_ptr<const T>` 让队列和正在处理的局部变量可以共同持有同一条消息，并通过这个只读句柄禁止消费者修改 `T`。`const` 只约束该句柄，并不能撤销生产者手里其他可写别名，所以生产者发布前必须完成写入，之后不再改动 payload。下面的 `std::thread` 构造会创建一个操作系统工作线程；析构里的 `join()` 阻塞当前析构线程，直到 `Run()` 真正返回。条件变量通知只让等待中的 worker 有机会重新检查谓词，不保证它此刻已经退出。

```cpp
// 教学最小例子：g++ -std=c++17 -pthread -c mini_runtime.cc
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

template <class T>
class LatestWorker {
 public:
  using Message = std::shared_ptr<const T>;
  LatestWorker(std::size_t cap, std::function<void(Message)> proc)
      : cap_(cap), proc_(std::move(proc)) {
    if (cap_ == 0) throw std::invalid_argument(std::string{});
    worker_ = std::thread(&LatestWorker::Run, this);
  }
  ~LatestWorker() {
    {
      std::lock_guard<std::mutex> lock(mu_);
      stop_ = true;
    }
    cv_.notify_one();
    worker_.join();
  }
  void Publish(Message msg) {
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (queue_.size() == cap_) queue_.pop_front();
      queue_.push_back(std::move(msg));
    }
    cv_.notify_one();
  }

 private:
  void Run() {
    for (;;) {
      Message msg;
      {
        std::unique_lock<std::mutex> lock(mu_);
        cv_.wait(lock, [&] { return stop_ || !queue_.empty(); });
        if (stop_ && queue_.empty()) return;
        msg = std::move(queue_.back());
        queue_.clear();  // 过载后直接追到最新样本
      }
      proc_(std::move(msg));  // 锁外执行
    }
  }
  const std::size_t cap_;
  std::function<void(Message)> proc_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<Message> queue_;
  bool stop_ = false;
  std::thread worker_;
};
```

析构函数先在 mutex 下置 `stop_`，再通知并 `join()`；worker 只有在 `stop_` 为真且队列为空时才退出，因此已经入队的最新样本会被处理完，`join()` 也会等待当前 `proc_` 返回。这个小例子没有实现“停止时丢弃待处理消息”，也没有让 `Publish()` 与对象析构并发安全：调用者必须先停止并 join 所有 producer，再销毁 `LatestWorker`。否则 producer 仍可能通过已经析构的 `this` 访问 mutex 或队列。真实框架要么明确这种外部生命周期协议，要么由更高层共享所有权和关闭状态阻止新的提交。

这个缩小版只有一只 OS worker，所以不是 Cyber 源码的改写。继续演进时，可以给每位消费者建立独立实例，让 Dispatcher 只保存弱引用，再把 OS thread 拆成共享 Processor 与可挂起 routine。无论加多少层，都要守住同一边界：registry 不能与遍历无同步并发，payload 可见性由队列同步保证，通知只承诺“再检查一次”；在自建系统里，停止新任务与释放业务资源之间还必须有等待在途业务退出的明确边界。

Cyber RT 这套设计最值得借鉴的地方，是把“消息是否存在”与“任务何时运行”拆成两个可独立演化的子系统：有界缓存保存事实，可合并事件推动调度。它带来了 transport 隔离、统一调度和新鲜度优先的过载行为；同时也留下弱引用表膨胀、共享 vector 的动态注册边界、持久 run queue 的公平性以及多输入时间一致性等工程代价。

读完源码后，`Component::Proc()` 就不再是一个神秘 callback。它是一次 transport 接收、一次进程内缓存扇出、一次事件传播、一次 worker 唤醒、一次 routine 选择和一次协程恢复共同作用的终点。机器人闭环的可预测性，也正是由这些看似“不属于算法”的细节共同决定的。
