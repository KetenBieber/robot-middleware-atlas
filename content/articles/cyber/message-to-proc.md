# 完整消息链：从 Receiver 到 `Component::Proc()`

设想 Apollo 的前视相机刚产生第 42 帧图像。传输层已经把字节收进进程，但规划或感知组件的 `Proc()` 还没有开始运行。中间件接下来必须解决四件不同的事：把消息保存到不会无限增长的地方，找到所有订阅者，唤醒负责业务计算的执行体，并让操作系统线程真正运行它。

这条路径从 transport callback 一直延伸到 `Component::Proc()`：ring 负责暂存消息，Dispatcher 找到订阅侧缓存，Notifier 把“有新数据”转成调度信号，Scheduler 再把对应协程交给 Processor 线程执行。

源码基线统一为 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。下文标为“固定提交源码摘录”的片段直接取自该版本；为聚焦当前机制，省略的语句会明确标出，不用源码跳转链接或文件地址代替代码。

这里的 transport 是把进程内对象、共享内存字节或网络报文送进当前进程的传输层；callback（回调）是传输库在自己的调用上下文中反向调用的一段函数，而不是一只新线程。`Component::Proc()` 则是框架最终进入业务算法的虚函数入口：基类声明 `virtual` 方法后，运行时会依据对象的实际类型调用派生组件覆写的版本。两者之间加入的 ring 是固定容量、反复复用槽位的环形缓存：它让接收方先把数据安置下来，再由另一条执行线程处理。

如果只看 Cyber RT 暴露给业务层的接口，很容易形成一个过于简单的印象：Reader 收到消息，框架调用 `Component::Proc()`，链路到此结束。真正的运行时链路还包含消息缓存、订阅分发、唤醒、协程状态转换和 OS worker 执行。

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

这些类名可以先压缩成三个职责角色。假设 `/camera/front` 刚收到第 42 帧图像：

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

`Reader<MessageT>::Init()` 把 Reader 的队列入口和 transport receiver 连接起来。代码中的 `Blocker` 是 Reader 供观察 API 使用的有界消息历史，不是组件执行队列；它由 Reader 自己的 routine 在取到消息后写入，而不是由 transport listener 直接执行算法。下列是固定提交源码摘录，行尾注释为本文添加：

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

## 两种进度分开：第 42 帧已经到达，算法还没开始

上游的 INTRA、SHM 或 RTPS 最终都把接收到的 `shared_ptr<M0>` 送入 `DataDispatcher<M0>`。下面不再重复每种传输内部的解码和监听代码，而是只跟踪一个可观察的状态：假设第 42 帧现在刚结束反序列化，`Component::Proc()` 仍未执行。接下来的每一步究竟保存了什么状态、由哪只线程执行？

### 先把消息放进各自的 ring

固定提交的 `DataDispatcher<T>::Dispatch` 几乎完整地回答了“接收线程做多少事才能返回”：

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

这里 `buffers_map_` 的 key 是 channel ID，value 是 `vector<weak_ptr<CacheBuffer<shared_ptr<T>>>>`。Dispatcher 本身不强占消费者 ring；每次 `lock()` 暂时保证当前写入对象仍存活。如果一个 channel 有三只消费者，循环可能尝试三次弱引用提升，分别取得三把 buffer 锁并写入三只槽。消息本体不因这三次句柄复制而深复制；但共享引用计数、每只 ring 的锁和槽位赋值仍属于接收线程的串行工作。

“各自的 ring”解决的是慢消费者不能夺走别人数据的需求：控制任务很慢时，它自己可能因为 ring 满而跳过旧帧，不会直接推动监控 Reader 的游标。但 Dispatcher 对同一 channel 的 fan-out 是串行的，一个消费者的缓存锁等待仍可能延迟其他消费者获得第 42 帧。注册并发也是单独的正确性问题：`AddBuffer()` 追加内层 vector 时有写锁，上面的 `Dispatch()` 读取 vector 却不取得它；运行中创建 Reader 若与遍历重叠，存在数据竞争。这个限制以及 Notifier 的强引用累积在[Dispatcher 的逐函数分析](dispatcher-notifier.md)中展开。

`CacheBuffer` 的物理槽位数为 `pending_queue_size + 1`，正常能容纳用户设置的历史深度。写满后用覆盖策略保护上界，首次 `Fetch` 又有自己的 latest 语义，不能根据 `vector` 大小想当然地认定消费者会读到每一帧。对第 42 帧，真正的结论只是：“在 t1 时刻，尚存活的订阅缓存已尝试按各自锁顺序接纳它”；并不意味着每只消费者最终都处理了它。

### 通知不带第 42 帧，也不等于调用业务

`Dispatch` 最后一行才进入 `DataNotifier::Notify`。固定源码中：

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

这里的 value 不是图像列表，而是 `vector<shared_ptr<Notifier>>`，每个 Notifier 只持有 `std::function<void()>`。因此第 42 帧仍留在 ring，通知调用携带的是“对应 channel 的消费任务可以再检查一次数据”。`Notify` 同步运行所有已登记的无参 callback：SHM 接收线程不会在这一步自动转换成 Processor 线程，INTRA 仍处于原始 Writer 的调用栈。若这里直接塞进感知 `Proc()`，就会重新引入“接收线程被算法长尾阻塞”的旧设计。

注册路径也能看出这个设计为什么需要两层登记。单输入 `DataVisitor<M0>` 构造时先为自己的 `ChannelBuffer` 调用 `DataDispatcher<M0>::AddBuffer`，随后将自己的 `Notifier` 登记到 `DataNotifier`。数据存储与事件回调使用两张表：前者决定消息体放哪里，后者决定哪些 task 需要检查。这两个注册动作本身不是“一次不可分割的安装”；task 的 callback 稍后才由 Scheduler 绑定，启动阶段与运行时热注册都需要特别检查时序。

固定 `Scheduler::CreateTask` 的相关连续代码揭示了 callback 究竟捕获了什么：

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

`task_id` 是稳定的任务名散列身份；闭包并未捕获图像、Reader 或 Component。它也没有保有 Scheduler 的强引用，只是原始 `this`：要安全销毁运行时，必须管理旧 notifier callback 的可达性。固定实现先 `DispatchTask` 再 `RegisterNotifyCallback`，这让 task 先具备被找到的条件，却也留下 callback 尚未绑定而数据已经能到达的窄窗口。[Notifier 注册与注销的实现边界](dispatcher-notifier.md)详细解释为什么不能只靠“登记完成”四个字忽略这个问题。

## 一次 channel 事件怎样变成一次任务重查

下面沿 Classic 策略继续：`SchedulerClassic::NotifyProcessor(crid)` 查找 task。如果它看到该 routine 正处于 `DATA_WAIT` 或 `IO_WAIT`，就清除 `updated_` 原子标志，表示有待消费的更新；随后通知任务所属 group 的 `ClassicContext`。这条同步回调仍运行于刚才的接收执行上下文。

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

这里的 “WAIT” 是 CRoutine 的逻辑状态，不是这只接收线程在等 condition variable。真正可能睡着的是另一边的 Processor OS 线程。固定 `ClassicContext::Notify` 用受 mutex 保护的计数留下事件，再通知一名等待者：

```cpp
void ClassicContext::Notify(const std::string& group_name) {
  (&mtx_wq_[group_name])->Mutex().lock();
  notify_grp_[group_name]++;
  (&mtx_wq_[group_name])->Mutex().unlock();
  cv_wq_[group_name].Cv().notify_one();
}
```

计数解决“线程还没等下去时通知先到了”的基本条件变量问题；`updated_` 则在另一层保存 task 应当重查的意图。两者都**不是数据消息计数器**。多条图像到达可能合并成一个更新意图，最终还有多少帧只能问 ring 和 DataVisitor。更深的竞态边界不能略过：固定提交的 `CRoutine::state_` 是普通枚举，通知线程读取它，Processor 线程可能同时修改它；代码没有用同一把锁保护这组访问，所以不能用另一个字段 `updated_` 的原子性证明整体状态迁移已经具备 C++ 内存模型下的完整同步。具体竞争时间线参见[等待态与事件位](croutine-wakeup.md)。

## Processor 把“有活可做”变成真正的执行

假设某只 Processor 正在 `Wait()`：条件变量返回只说明该 OS 线程得到了再次检查机会，Linux 仍决定它什么时候获得 CPU。下一步 `ClassicContext::NextRoutine()` 扫描所属 group 的优先级槽，尝试 `Acquire()` 防止同一 routine 被其他 Processor 同时执行，再调用 `UpdateState()` 根据事件位或定时到期把等待态转成 `READY`。`READY` 只是可被选取的资格；它不会抢占同一 Processor 上正在运行的长 `Proc()`。

固定源码 `Processor::Run()` 的主循环显示了最后一次真正的执行权切换：

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

`Resume()` 通过 `SwapContext` 在当前 Processor 线程上切到这只 routine 保存的用户态栈。它不是启动另一条 OS 线程。RoutineFactory 的单输入函数会先把自己标成 `DATA_WAIT`，然后用 `dv->TryFetch(msg)` 尝试从私有游标读第 42 帧。如果读到，调用最初包装的 `f(msg)`；Component 的 `f` 再经 `Process(msg)` 检查关闭标志并进入业务 `Proc(msg)`。若读不到，routine `Yield()` 把执行权还给 Processor，等待下一次状态更新。

至此才真正完成第 42 帧的运行时故事：它原本在 transport 线程的局部句柄里，经过 Dispatcher 被复制为各只 ring 的共享句柄；Notifier 和 Scheduler 从未运送它；Processor 上的 DataVisitor 后来才从自己的 ring 取出共享句柄并进入业务代码。缓存覆盖、事件合并、任务选择与协程恢复是四种不同事件，不能简单合并为“消息到达触发 Proc”。

关于 Classic 的完整优先级表、`NextRoutine/Wait`、`Resume/Yield` 寄存器与栈边界、停止时如何等待正在执行的 routine，继续看[Processor 与上下文切换](processor-context-switch.md)。本文只需要一个性能结论：从 `notify_one` 到 `Proc` 之间仍有 Linux 调度延迟、调度器扫描、可能排在前面的非抢占工作和用户态切栈成本。

## 过载、动态注册与关闭：正常回放之外的三条分支

**慢消费者。** 相机以 100 Hz 发帧、某 Component 以 30 ms 处理一帧时，单 worker 的处理速率最多约为 33.3 帧/秒，不可能靠无限期追加通知保持完整输入。有限 ring 允许旧样本被覆盖，消费者的独立游标帮助控制内存上界；但数据年龄和跨输入时间差仍需要业务检查。`pending_queue_size` 不是越大越安全：增大容量减少短突发丢帧，却可能让控制算法读取更旧状态。容量与精确游标恢复规则见[有界缓存](pending-queue-ring.md)。

**运行中注册。** `AtomicHashMap` 的并发查找并不保护作为 value 的 `vector`；DataNotifier 的登记回调也不能靠 `std::function` 默认空值建立跨线程同步。在需要在线添加、删除 Reader 的产品里，应补充注册/注销协议，或者限定注册只发生在接收线程启动前；不能把固定源码的接口存在等同于“任意时刻热插拔安全”。实现级例子见[注册表与通知回调](dispatcher-notifier.md)。

**关闭。** 固定 `ComponentBase::Shutdown()` 先设置 `is_shutdown_`，再调用业务 `Clear()`，随后关闭 Reader，最后移除 task。置位只会阻止之后进入 `Process` 的调用，**不会打断已经运行的 `Proc`**。由于 `Clear()` 在任务移除/等待之前，如果派生类释放了在途 `Proc` 正在使用的资源，业务自身还需要同步。再往外一层，`ModuleController::Clear()` 应让组件释放后才卸载动态库；如果旧 callback 仍持有可能调用旧代码的函数地址，就还要核对它是否已与任务和库的生命周期脱钩。这些问题不是“有一个 Shutdown 函数”就能一次性解决。

## 如何继续沿源码追踪

本篇保留了传输接收的三条实际路径，并以 Dispatcher、Notifier、Scheduler、Processor 和 Component 的固定代码把它们接回到 `Proc()`。需要逐槽复现第 42 帧为什么被覆盖，进入[CacheBuffer 与 ChannelBuffer](pending-queue-ring.md)；需要深挖弱引用登记、无参回调何时安全发布，进入[Dispatcher 与 Notifier](dispatcher-notifier.md)；需要写出缩小版任务状态机，先读[CRoutine 的等待与通知](croutine-wakeup.md)，再读[Processor 的选择与上下文切换](processor-context-switch.md)。

读源码时应分别记下四个时间戳：transport 实际得到消息、ring 写入完成、Processor 开始执行、业务 `Proc` 完成。它们之间的差异比单独报告“中间件一次传输用时”更能定位机器人闭环中的延迟与抖动；没有测量、负载和部署条件时，不应把这些结构上的可能成本写成固定的毫秒数。
