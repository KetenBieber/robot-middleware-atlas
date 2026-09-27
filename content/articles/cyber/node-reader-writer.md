# 从一个相机通道设计 Node、Reader 与 Writer

一辆车的前视相机正在以 30 Hz 产生图像。驱动模块要发布帧，感知模块要持续处理，诊断工具偶尔还要观察最新一帧。表面上，这似乎只需要一个 callback（回调：把函数交给框架，由框架在事件发生时调用）：相机得到图像后，直接调用感知函数。

真正把这段程序变成中间件以后，问题会立刻增多。谁保证 Writer 发出的确实是图像而不是定位消息？谁拥有 callback 指向的对象？一个模块怎样保存不同消息类型的订阅者？Reader 建立之前已经存在的 Writer 如何被发现？关闭时，怎样避免拓扑线程或调度线程再次进入已经析构的对象？

Cyber RT 的 `Node`、`Reader` 和 `Writer` 正是在回答这些问题。Node 是命名和通信对象创建边界；Reader 是接收端点（endpoint，即一条命名消息流上的接收对象），Writer 是发送端点，通常由业务代码自行持有。三者最值得学习的部分，不是函数名，而是模板与虚函数怎样配合、强弱所有权怎样划分，以及创建和关闭为什么必须按特定方向进行。

本文固定 Apollo 源码提交为 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，主要讨论 reality mode（走 Transport 的正常通信模式）；simulation/intra 指使用进程内传递的非 reality 分支。文中的错误实现与最小复刻均为教学代码，不是 Apollo 上游源码；后续代码块会按固定源码摘录、错误示例或教学草图标明身份，`text` 块是本文作者绘制的对象图/时序图。下文所说的 OS 线程，是由 Linux 内核调度、拥有独立执行栈的执行流；Cyber 的 CRoutine 是在线程之上切换的用户态任务，二者不是同一种调度对象。

## 直接 callback 把所有问题藏在一次函数调用里

先从最容易写出的版本开始。下面是故意保留缺陷的**错误示例**：

先说明代码里的 C++ 句柄：`std::shared_ptr<T>`（共享指针）会共同拥有一个对象，复制句柄会增加引用计数但不复制 `T`；`std::function<void(...)>` 可以保存签名匹配的函数、lambda（用方括号捕获局部状态并定义可调用对象）等；`std::move(x)` 只把 `x` 转成允许移动资源的右值，是否真的转移资源由接收端操作决定。这里的 lambda/callback 可能保存接收对象的 `this`，所以不能只看函数调用，还要检查被捕获对象的寿命。

```cpp
class CameraDriver {
 public:
  void SetCallback(std::function<void(std::shared_ptr<Image>)> callback) {
    callback_ = std::move(callback);
  }

  void OnFrame(std::shared_ptr<Image> image) {
    callback_(std::move(image));  // 驱动线程直接进入感知业务
  }

 private:
  std::function<void(std::shared_ptr<Image>)> callback_;
};
```

如果一次视觉推理需要 30 ms，`OnFrame()` 所在的驱动或接收线程就有 30 ms 不能处理下一帧。若 callback 保存的是感知对象的裸 `this`，感知模块先析构而相机仍在发帧，下一次调用就是悬空访问。再把定位消息接入同一套接口时，还必须为每种消息复制注册代码，或者退回 `void*` 和运行时强转。

加一把 mutex（互斥锁）只能防止同时修改 `callback_`：它让竞争同一把锁的线程一次只有一个能进入保护区，但不能回答 callback 在锁内还是锁外执行、析构怎样等待正在运行的函数、跨进程时对象如何序列化。给每个订阅者开一条 OS 线程也只是把问题改写为：每条线程要占多少栈空间，队列满了丢谁，几十个传感器和算法是否要创建几十条内核线程。

因此，第一步不是急着加入调度器，而是先把接口的类型和对象寿命说清楚。这里 transport（传输层）负责按进程内、共享内存或网络路径送达消息；缓存负责暂存消息，协程负责在调度器分配的线程上执行可暂停任务。只有 endpoint 本身成立，后面的这些层才有可靠的挂载点。

## 类型化 endpoint 把错误挡在编译期

下面是教学用最小接口草图，不是 Apollo 源码：

```cpp
template <class MessageT>
class Writer {
 public:
  bool Write(const std::shared_ptr<MessageT>& message);
};

template <class MessageT>
class Reader {
 public:
  using Callback =
      std::function<void(const std::shared_ptr<MessageT>&)>;
};
```

`Writer<Image>` 的 `Write` 参数不能传入 `shared_ptr<Pose>`，错误会在编译时出现。模板在这里承担的是类型契约：编译器为实际的 `MessageT` 生成代码，`Reader<Image>` 内部的 Receiver、callback 和缓存也都保持同一种消息类型。

Receiver（接收器）是 transport 层的接收对象：它收到底层传输送来的消息后，把消息交给框架的数据分发路径；它不是业务 callback，也不负责调用 `Proc()`。

`const std::shared_ptr<MessageT>&` 又包含两层含义。`const` 修饰的是智能指针句柄，当前函数不能让这个参数改指向另一个对象；它并没有把 `MessageT` 本体变成只读。引用避免仅仅为了传参再增加、减少一次强引用计数，但 endpoint 若要让消息活过当前调用，仍会把 shared pointer 复制进异步结构。

到这里类型安全有了，新的问题却出现了：一个 Node 同时拥有 `Reader<Image>`、`Reader<Pose>` 和 `Reader<Chassis>`，它们是互不相关的模板实例，无法直接放进同一个 `vector<Reader<?>>`。Cyber RT 没有为此放弃模板，而是增加一层非模板运行时接口。

## 模板负责消息类型，虚函数负责异构生命周期

固定提交里的 `proto::RoleAttributes` 是 protobuf（Protocol Buffers）生成的运行时属性消息。`ReaderBase` 不处理任何具体 payload，只定义所有 Reader 都有的生命周期和查询能力。下面是保持原控制流的**固定提交源码摘录**，省略了注释与若干查询函数。若两个线程无同步地同时读写同一普通变量，形成 data race（数据竞争：C++ 内存模型下未排序的冲突访问），程序行为未定义；`std::atomic<bool>` 让初始化标记的单次读写避免这一问题，但它只保护这个标志本身，不会自动保护 Reader 的其他成员或整个关闭过程；`= 0` 的纯虚函数则表示基类只规定接口，具体 Reader 必须提供实现：

```cpp
class ReaderBase {
 public:
  explicit ReaderBase(const proto::RoleAttributes& role_attr)
      : role_attr_(role_attr), init_(false) {}
  virtual ~ReaderBase() {}

  virtual bool Init() = 0;
  virtual void Shutdown() = 0;
  virtual void ClearData() = 0;
  virtual void Observe() = 0;
  virtual bool Empty() const = 0;
  virtual uint32_t PendingQueueSize() const = 0;

  uint64_t ChannelId() const { return role_attr_.channel_id(); }
  bool IsInit() const { return init_.load(); }

 protected:
  proto::RoleAttributes role_attr_;
  std::atomic<bool> init_;
};
```

具体的 `Reader<MessageT>` 再继承这个运行时接口，保存 `CallbackFunc<MessageT>`、`Receiver<MessageT>` 和 `Blocker<MessageT>`。Writer 也采用 `WriterBase <- Writer<MessageT>` 两层结构。上面的类声明就是固定提交中的接口摘录，下面继续看这些成员怎样被具体 Reader 使用。

这里的 `Blocker<MessageT>` 是 Reader 的有界观察历史：业务显式调用 `Observe()` 后可读取它；它不同于 DataVisitor 的 pending ring，后者保存调度任务尚未取走的输入。

这不是“模板和虚函数二选一”。模板解决编译期消息类型，虚函数解决运行时只知道基类时该调用哪个实现。Node 可以保存 `shared_ptr<ReaderBase>`，而业务仍得到 `shared_ptr<Reader<Image>>`。通过基类指针调用 `Shutdown()` 时，C++ 按对象的动态类型选择实际 Reader 的重写实现；编译器通常用虚表和对象中的隐藏指针完成这件事，但 `vptr` 不是 C++ 标准规定的对象布局。

Cyber 的 simulation 分支还在第二层使用继承：`IntraReader<MessageT>` 继承 `Reader<MessageT>`，覆盖 `Init/Shutdown/Observe` 等接口。运行模式在启动后选择实现类，但 `MessageT` 始终没有被擦成 `void*`。

先把核心关系画出来，后文再逐个填充对象：

:::{mermaid}
classDiagram
  class Node {
    -readers_ map~string, shared_ptr~ReaderBase~~
    -node_channel_impl_ unique_ptr~NodeChannelImpl~
    +CreateReader~T~() shared_ptr~Reader_T~
    +CreateWriter~T~() shared_ptr~Writer_T~
  }
  class NodeChannelImpl {
    -node_attr_ RoleAttributes
    +CreateReader~T~()
    +CreateWriter~T~()
  }
  class ReaderBase {
    <<runtime interface>>
    +Init() bool
    +Shutdown()
    +Observe()
  }
  class Reader_T {
    <<template>>
    -reader_func_ CallbackFunc~T~
    -receiver_ shared_ptr~Receiver_T~
    -blocker_ shared_ptr~Blocker_T~
  }
  class IntraReader_T {
    <<template>>
  }
  class WriterBase {
    <<runtime interface>>
    #init_ bool
  }
  class Writer_T {
    <<template>>
    -transmitter_ shared_ptr~Transmitter_T~
  }
  class IntraWriter_T {
    <<template>>
  }

  Node *-- NodeChannelImpl
  Node o-- ReaderBase : strong owners
  ReaderBase <|-- Reader_T
  Reader_T <|-- IntraReader_T
  WriterBase <|-- Writer_T
  Writer_T <|-- IntraWriter_T
  Node ..> Writer_T : creates but does not retain
:::

图中最容易忽略的是最后一条虚线：Node 创建 Writer，却不拥有 Writer。这个不对称关系会直接决定业务代码怎么写，也决定关闭时由谁负责停止发送。

## Node 是名字与资源边界，不是消息总线

公开 API `cyber::CreateNode()` 返回 `unique_ptr<Node>`。`unique_ptr` 表示唯一所有者：句柄销毁时 Node 随之销毁，句柄不能直接复制给第二个 owner。固定提交先在 reality mode 检查框架已初始化，再调用 Node 的 private 构造函数。下面是固定提交源码摘录：

```cpp
std::unique_ptr<Node> CreateNode(const std::string& node_name,
                                 const std::string& name_space) {
  bool is_reality_mode = GlobalData::Instance()->IsRealityMode();
  if (is_reality_mode && !OK()) {
    AERROR << "please initialize cyber firstly.";
    return nullptr;
  }
  return std::unique_ptr<Node>(new Node(node_name, name_space));
}
```

private 构造函数让框架保留创建门：独立应用通常独占 Node；Component 内部则把 `new Node` 放入 `shared_ptr<Node>`。Node 本身没有规定全系统只能用哪种智能指针，关键是 owner 必须覆盖所有 endpoint 的装配期。

Node 的核心成员把并发访问的 Reader 容器与通信能力实现分开持有。下面是固定提交源码摘录：

```cpp
std::mutex readers_mutex_;
std::map<std::string, std::shared_ptr<ReaderBase>> readers_;

std::unique_ptr<NodeChannelImpl> node_channel_impl_ = nullptr;
std::unique_ptr<NodeServiceImpl> node_service_impl_ = nullptr;
```

`NodeChannelImpl` 与 `NodeServiceImpl` 是组合对象，分别承接 channel endpoint 与 service/client 的创建逻辑。它们把 Node 从“所有通信模式都知道一点”的大类拆成两个职责。不过 `NodeChannelImpl` 不是传统的 ABI pimpl：ABI 是 Application Binary Interface（应用二进制接口），pimpl 通常把成员布局藏在实现文件中；这里完整定义和模板实现都在头文件，作用是职责分离，而不是隐藏类型布局。

Reader 创建与 Writer 创建在 Node 中有意不对称。代码里的 `std::mutex` 是互斥锁：同一时刻最多一个线程持有它；`std::lock_guard` 用 RAII（Resource Acquisition Is Initialization，资源获取即初始化）把“进入作用域时加锁、离开时解锁”绑定到对象生命周期，因此提前 return 也会释放锁。下面是**固定提交源码摘录**：

```cpp
template <typename MessageT>
auto Node::CreateWriter(const proto::RoleAttributes& role_attr)
    -> std::shared_ptr<Writer<MessageT>> {
  return node_channel_impl_->template CreateWriter<MessageT>(role_attr);
}

template <typename MessageT>
auto Node::CreateReader(const ReaderConfig& config,
                        const CallbackFunc<MessageT>& reader_func)
    -> std::shared_ptr<cyber::Reader<MessageT>> {
  std::lock_guard<std::mutex> lg(readers_mutex_);
  if (readers_.find(config.channel_name) != readers_.end()) {
    AWARN << "Failed to create reader: reader with the same channel already "
             "exists.";
    return nullptr;
  }
  auto reader =
      node_channel_impl_->template CreateReader<MessageT>(config, reader_func);
  if (reader != nullptr) {
    readers_.emplace(std::make_pair(config.channel_name, reader));
  }
  return reader;
}
```

Writer 路径只是转发并返回。应用必须保存返回的 `shared_ptr<Writer<T>>`；若它只是一个未赋值的临时量，完整表达式结束后 Writer 就会析构。Reader 则以 channel name 为 key 存进 Node 的 map，Node 因而可以统一 `Observe`、`ClearData` 与查询。

map 的 key 只有 channel name。这意味着同一个 Node 不能为同一 channel 创建第二只 Reader，即使模板类型或 callback 不同。factory 成功以后才插入 map，所以创建失败不会留下一个可查询但未初始化的 Reader。

`CreateReader()` 的固定源码摘录持有 `readers_mutex_`，所以重复检查与成功后插入 map 是一个互斥区；但它不保护后文明确指出的 `Observe()`/`ClearData()` 遍历。

`readers_mutex_` 也不是“所有 Node 操作都会自动安全”的总锁：固定提交里的 `CreateReader()`、`GetReader()` 和 `DeleteReader()` 会取得它，但 `Node::Observe()` 与 `Node::ClearData()` 直接遍历 `readers_`，没有取得同一把锁。若一个线程正在 `Observe()`，另一个线程同时 `CreateReader()` 插入 map 或 `DeleteReader()` 擦除节点，标准库 `std::map` 的读写就没有同步，不能称为线程安全。

下面是固定提交中 `Node::Observe()`、`ClearData()` 的真实实现摘录：

```cpp
void Node::Observe() {
  for (auto& reader : readers_) {
    reader.second->Observe();
  }
}

void Node::ClearData() {
  for (auto& reader : readers_) {
    reader.second->ClearData();
  }
}
```

两段都直接遍历 map；相比之下，前面的 `CreateReader()` 在检查重复项并插入成功结果的整个区间持有 `readers_mutex_`。因此应用若跨线程混用这些接口，必须自行串行化，或先在 Node 内加锁复制 `shared_ptr` 快照，再锁外调用 Reader。

因此，若应用要并发调用这些 API，必须由调用方串行化 endpoint 增删与 `Observe/ClearData`；或在 Node 内先拿 `readers_mutex_` 复制一份 `shared_ptr<ReaderBase>` 快照，再释放 map 锁后逐一调用。快照让遍历期间 Reader 对象仍存活，也避免在调用 Blocker 时一直占着 Node 的 map mutex。还要注意 `DeleteReader()` 本身只擦除 Node 的一份强引用：若快照、调用方或 Component 仍持有 Reader，它不会因此立即析构，也不会立即进入析构函数中的 `Shutdown()`。

`GetReader<MessageT>()` 从基类指针执行 `dynamic_pointer_cast`。channel 存在但调用者写错 MessageT 时，cast 返回空指针，不会把另一种模板实例解释成当前类型。这里的 shared pointer cast 不复制 Reader 本体，只在成功时增加同一控制块的强引用计数。

## RoleAttributes 把应用配置补成运行时身份

业务只想说“订阅 `/camera/front`，队列深度为 2”。protobuf 是 Apollo 常用的结构化消息定义与序列化格式；service discovery（服务发现）是在运行时公布并查找哪些 Node/Writer/Reader 存在。transport 和发现模块还需要 host、进程、Node、channel id、消息类型描述和 QoS。QoS 是 Quality of Service（服务质量配置），包含深度等通信策略；在本文这条 Reader 路径中，`qos_profile.depth()` 还决定 Reader 的观察历史容量。`NodeChannelImpl::FillInAttr<MessageT>` 正是配置到运行时对象之间的接缝。

下面是**固定提交源码摘录**，展示 endpoint 参数如何交给对应的具体 Reader 创建逻辑：

```cpp
template <typename MessageT>
void NodeChannelImpl::FillInAttr(proto::RoleAttributes* attr) {
  attr->set_host_name(node_attr_.host_name());
  attr->set_host_ip(node_attr_.host_ip());
  attr->set_process_id(node_attr_.process_id());
  attr->set_node_name(node_attr_.node_name());
  attr->set_node_id(node_attr_.node_id());
  auto channel_id = GlobalData::RegisterChannel(attr->channel_name());
  attr->set_channel_id(channel_id);
  if (!attr->has_message_type()) {
    attr->set_message_type(message::MessageType<MessageT>());
  }
  if (!attr->has_proto_desc()) {
    std::string proto_desc("");
    message::GetDescriptorString<MessageT>(attr->message_type(), &proto_desc);
    attr->set_proto_desc(proto_desc);
  }
  if (!attr->has_qos_profile()) {
    attr->mutable_qos_profile()->CopyFrom(
        transport::QosProfileConf::QOS_PROFILE_DEFAULT);
  }
}
```

`MessageT` 仍是编译期 C++ 类型；`message_type` 与 `proto_desc` 是给发现、匹配和反射使用的运行时元数据。不能因为 RoleAttributes 里有字符串类型名，就误以为 Reader 的模板类型由字符串在运行时生成。

`ReaderConfig` 还包含两个容易混淆的深度。`qos_profile.depth()` 用来构造 Reader 的 Blocker 历史缓存；`pending_queue_size` 用来构造 DataVisitor 中尚未处理消息的 ring。它们服务不同阶段，调大 QoS depth 不等于扩大 Component 的处理 backlog。

补齐属性后，工厂才决定具体 endpoint。下面是 `NodeChannelImpl::CreateReader()` 的**固定提交源码摘录**：

```cpp
if (!role_attr.has_channel_name() || role_attr.channel_name().empty()) {
  AERROR << "Can't create a reader with empty channel name!";
  return nullptr;
}

proto::RoleAttributes new_attr(role_attr);
FillInAttr<MessageT>(&new_attr);

std::shared_ptr<Reader<MessageT>> reader_ptr = nullptr;
if (!is_reality_mode_) {
  reader_ptr =
      std::make_shared<blocker::IntraReader<MessageT>>(new_attr, reader_func);
} else {
  reader_ptr = std::make_shared<Reader<MessageT>>(
      new_attr, reader_func, pending_queue_size);
}

RETURN_VAL_IF_NULL(reader_ptr, nullptr);
RETURN_VAL_IF(!reader_ptr->Init(), nullptr);
return reader_ptr;
```

模式差异集中在创建分支里，调用方无需改 API；两个具体类型随后都经过 `Init()`，失败时工厂返回空指针而不把半初始化 Reader 交给 Node。这是 Factory Method（工厂方法：把具体类选择集中在创建入口）的工程作用，而不是为了套用模式名称。

## 一只 Reader 的创建远不止保存 callback

相机 Reader 创建时，第一帧还没有到达，Cyber 已经搭好数据入口、调度任务和发现关系。下面的时序图以 reality mode 为准：

:::{mermaid}
sequenceDiagram
  participant App as Component / application
  participant N as Node
  participant F as NodeChannelImpl
  participant R as Reader~Image~
  participant S as Scheduler
  participant RM as ReceiverManager~Image~
  participant T as ChannelManager

  App->>N: CreateReader~Image~(ReaderConfig, callback)
  activate N
  N->>N: lock readers_mutex<br/>reject duplicate channel
  N->>F: CreateReader~Image~
  F->>F: FillInAttr~Image~
  F->>R: make_shared + Init()
  activate R
  R->>R: build callback wrapper<br/>Enqueue then user callback
  R->>S: CreateTask(DataVisitor + RoutineFactory)
  S-->>R: task registered
  R->>RM: GetReceiver(role_attr)
  RM-->>R: shared receiver for type + channel
  R->>T: AddChangeListener
  R->>T: GetWritersOfChannel
  R->>T: Join(ROLE_READER)
  deactivate R
  F-->>N: shared_ptr~Reader~
  N->>N: readers_[channel] = reader
  N-->>App: shared_ptr~Reader~
  deactivate N
:::

下面的**固定提交源码摘录**保留 `Reader::Init()` 的关键控制流：

```cpp
template <typename MessageT>
bool Reader<MessageT>::Init() {
  if (init_.exchange(true)) {
    return true;
  }

  std::function<void(const std::shared_ptr<MessageT>&)> func;
  if (reader_func_ != nullptr) {
    func = [this](const std::shared_ptr<MessageT>& msg) {
      this->Enqueue(msg);
      this->reader_func_(msg);
      // 省略统计采样
    };
  } else {
    func = [this](const std::shared_ptr<MessageT>& msg) {
      this->Enqueue(msg);
    };
  }

  croutine_name_ = role_attr_.node_name() + "_" + role_attr_.channel_name();
  auto dv = std::make_shared<data::DataVisitor<MessageT>>(
      role_attr_.channel_id(), pending_queue_size_);
  croutine::RoutineFactory factory =
      croutine::CreateRoutineFactory<MessageT>(std::move(func), dv);
  if (!scheduler::Instance()->CreateTask(factory, croutine_name_)) {
    init_.store(false);
    return false;
  }

  receiver_ = ReceiverManager<MessageT>::Instance()->GetReceiver(role_attr_);
  role_attr_.set_id(receiver_->id().HashValue());
  channel_manager_ =
      service_discovery::TopologyManager::Instance()->channel_manager();
  JoinTheTopology();
  return true;
}
```

这段代码第一次揭示了线程边界：`reader_func_` 没有直接交给 transport Receiver，而是包进 `RoutineFactory`。DataVisitor 收到消息事件后，Scheduler 的 Processor 线程恢复 routine，才执行 `Enqueue` 和用户 callback。lambda 捕获裸 `this`，所以 Reader 关闭时必须先保证对应 task 不会再执行；shared_ptr 本身不会自动修复裸捕获。

`init_.exchange(true)` 是幂等门：已经初始化便直接成功。创建 task 失败时源码把 flag 恢复为 false。局部 Reader 没有插入 Node map，shared pointer 离开作用域后析构。不过 DataVisitor 构造时已向全局 registry 注册弱 buffer 与 notifier，固定实现没有完整的 unregister token；频繁失败或热装载会留下失效登记的遍历成本。

## 多只 Reader 共享底层 Receiver，再拥有各自消费状态

如果同一进程里有诊断、记录和感知三个订阅者，为每只 Reader 都建立一份传输接收端会重复接收同一 channel。SHM 是 shared memory（共享内存），RTPS 是 Real-Time Publish-Subscribe（实时发布/订阅协议）；二者是可能使用的跨进程传输路径，具体序列化、拷贝与接收线程由 transport 实现决定。Cyber 在 `ReceiverManager<MessageT>::GetReceiver` 中按 channel name 缓存一只 receiver。

下面是**固定提交源码摘录**：

```cpp
const std::string& channel_name = role_attr.channel_name();
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

`ReceiverManager<MessageT>` 本身是模板 singleton，所以 map 的完整 key 可以理解为 `MessageT + channel_name`。底层 Receiver 的 listener 不捕获某只 Reader，只把消息按 channel id 交给 `DataDispatcher<MessageT>`。每只 Reader 的 DataVisitor 都有自己的 ring 和读取位置；共享的是 ingress 和 payload 对象，不共享会被某一消费者移动的游标。

于是同一帧图像可以这样流动：

```text
shared Receiver<Image>
  -> DataDispatcher<Image>
       -> Reader A 的 DataVisitor ring -> Reader A callback
       -> Reader B 的 DataVisitor ring -> Reader B callback
       -> Component 的 DataVisitor ring -> Component::Proc
```

每个 ring 槽位复制 `shared_ptr<Image>`，不是复制整幅图像。不同消费者可以独立落后和覆盖旧槽，但最慢者仍可能延长图像对象的寿命。ReceiverManager 的 map 也强持有 receiver，因此 `Reader::Shutdown()` 把自己的 `receiver_` 置空，不代表底层共享 receiver 立刻析构。

Component 场景还有一个反直觉点：Node map 与 `ComponentBase::readers_` vector 可以同时强持有同一只 Reader；与此同时 Component 又有自己的 DataVisitor task。前者是“一只对象的两个 owner”，后者才是“两条独立消费支路”。

## topology 让已有与未来的对端都能建立关系

创建 Receiver 只准备了本地入口，Writer 与 Reader 还要互相发现。这里的 topology（通信拓扑）是 ChannelManager 维护的当前 endpoint 关系。`Reader::JoinTheTopology()` 先注册 change listener（变化监听函数），再查询当前已经存在的 writers 并逐个 `receiver_->Enable(writer)`，最后把自身以 `ROLE_READER` 加入 ChannelManager。

如果顺序反过来，Reader 可能在“查询已有 writer”和“注册未来变化”之间漏掉一次加入事件。固定实现先监听再做快照查询，使已有对端和未来对端都有路径进入。收到 Writer join/leave 变化后，`OnChannelChange` 先核对 role type 和 channel，再调用 receiver Enable/Disable。

这里的 callback 用 `std::bind` 把成员函数与对象指针绑定成一个可调用对象；`std::placeholders::_1` 表示由 Signal 发来的第一个参数稍后再填入。下面是固定提交中的绑定表达式摘录，位置见 `Reader::JoinTheTopology()`：

```cpp
std::bind(&Reader<MessageT>::OnChannelChange,
          this, std::placeholders::_1)
```

捕获裸 `this`。因此 `LeaveTheTopology()` 必须先调用 `RemoveChangeListener(change_conn_)`，让后续 topology 通知不再选择这只 listener。但这一步只是解绑，不自动等于“已开始执行的 callback 全部结束”。

原因在固定提交的 `base::Signal`：发射信号时，它先在 signal mutex 下把 `shared_ptr<Slot>` 复制到本地列表，随后释放 mutex，再逐个调用 slot。下面是上游 `Signal::operator()` 的源码摘录：

```cpp
void operator()(Args... args) {
  SlotList local;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    for (auto& slot : slots_) {
      local.emplace_back(slot);
    }
  }

  if (!local.empty()) {
    for (auto& slot : local) {
      (*slot)(args...);
    }
  }

  ClearDisconnectedSlots();
}
```

`Disconnect()` 只在 mutex 下把 slot 标成断开；它不会等待已经复制到本地列表的 slot 返回。更细一层的 `Slot::operator()` 与 `Disconnect()` 对 `connected_` 的读取与写入也不是原子操作。

所以可能出现如下交错：拓扑线程已经把 slot 复制出来并开始执行 `OnChannelChange()`；Reader 线程调用 `RemoveChangeListener()`，它返回后继续清理 `receiver_`；旧 callback 随后仍可能访问 Reader 或同一个 `receiver_` 成员。`shared_ptr<Slot>` 保住的是 slot 对象，不是 callback 捕获的 Reader。由 `Signal` 源码本身只能证明“从登记表解绑”，不能证明在途回调已经 quiescent（静止）。若两条路径能够并发，完整的析构安全协议还需要 Manager 保证串行通知，或提供显式的 in-flight callback 计数/等待屏障；否则调用侧必须先停止 topology 事件源并等待其线程结束，再销毁 Reader。仅有 `RemoveChangeListener()` 不足以作为通用的并发销毁证明。

## Writer 由调用者拥有，并把发送委托给 Transmitter

Writer 的创建与 Reader 类似：NodeChannelImpl 校验 channel、填充 RoleAttributes、按模式创建具体 Writer 并调用 Init。区别在于 Node 不保留返回值。

reality mode 的 `Writer::Init()` 先在 `WriterBase::lock_` 下检查 `init_`，再让 Transport 工厂创建 `Transmitter<MessageT>`。下面是初始化主干的**固定提交源码摘录**：

```cpp
bool Writer<MessageT>::Init() {
  {
    std::lock_guard<std::mutex> g(lock_);
    if (init_) {
      return true;
    }
    transmitter_ =
        transport::Transport::Instance()->CreateTransmitter<MessageT>(
            role_attr_);
    if (transmitter_ == nullptr) {
      return false;
    }
    init_ = true;
  }
  this->role_attr_.set_id(transmitter_->id().HashValue());
  channel_manager_ =
      service_discovery::TopologyManager::Instance()->channel_manager();
  JoinTheTopology();
  return true;
}
```

mutex 只围住 transmitter 创建和 `init_` 状态变更；endpoint id、ChannelManager 获取和拓扑登记发生在锁外。Writer 的 topology 路径与 Reader 对称：监听 Reader 变化、启用已有 Readers、以 ROLE_WRITER 加入。

Node 不持有 Writer 的设计让输出 endpoint 与业务对象自然同寿命：Component 通常把 Writer 存成成员，业务的 Init 创建，Component 销毁时释放。代价是 API 调用者必须主动保存它；Node 无法在自己的 map 中统一停止所有 Writer。

### 两种 Write 的拷贝语义不同

`Writer::Write()` 的这段**固定提交源码摘录**非常短，却值得逐行读：

```cpp
template <typename MessageT>
bool Writer<MessageT>::Write(const MessageT& msg) {
  RETURN_VAL_IF(!WriterBase::IsInit(), false);
  auto msg_ptr = std::make_shared<MessageT>(msg);
  return Write(msg_ptr);
}

template <typename MessageT>
bool Writer<MessageT>::Write(
    const std::shared_ptr<MessageT>& msg_ptr) {
  RETURN_VAL_IF(!WriterBase::IsInit(), false);
  return transmitter_->Transmit(msg_ptr);
}
```

`Write(const MessageT&)` 会在堆上创建新 MessageT，并调用拷贝构造；大 protobuf 或图像 wrapper 的成本不能忽略。`Write(shared_ptr<MessageT>)` 不在 Writer wrapper 这一层复制 payload，只把共享所有权交给 Transmitter。它仍不能被称为端到端零拷贝：SHM/RTPS 的序列化、共享块和远端反序列化要另行分析。

`AcquireMessage()` 先请求 transmitter 提供一只消息对象，失败时回退到 `make_shared<MessageT>()`。下面是固定提交源码摘录：

```cpp
std::shared_ptr<MessageT> Writer<MessageT>::AcquireMessage() {
  if (!WriterBase::IsInit()) {
    AERROR << "Please Acquire message after init writer!";
    auto m = std::make_shared<MessageT>();
    return m;
  }

  std::shared_ptr<MessageT> m(nullptr);
  if (transmitter_->AcquireMessage(m)) {
    return m;
  } else {
    m = std::make_shared<MessageT>();
    return m;
  }
}
```

这个接口为 transport 预分配或 loaned message 留出入口，但是否真正共享底层 payload，仍取决于具体 Transmitter。

## 把一帧图像的所有权和线程完整回放一次

现在可以把创建和稳定运行放在同一张账本里。假设业务通过 `Writer<Image>::Write(shared_ptr<Image>)` 发布第 42 帧：

1. 业务线程持有一份 `shared_ptr<Image>`，Writer 检查自身已初始化，把同一 shared pointer 传给 Transmitter。Writer 不修改图像内容，也不在这一层创建副本。
2. transport 根据进程关系走 INTRA、SHM 或 RTPS。INTRA 可能继续共享同一对象；跨进程路径必须经过各自的序列化或共享内存协议，不能把第一步的 shared pointer 地址传给另一个进程。
3. 本进程共享 Receiver 把得到的 `shared_ptr<Image>` 交给 DataDispatcher。当前仍是 transport ingress 相关线程；还没有进入 Reader callback。
4. Dispatcher 把 shared pointer 写进各 Reader/Component 私有 ring，再发送 channel 更新事件。每个槽位延长 Image 寿命，payload 不因 fan-out 重复构造。
5. Scheduler 唤醒某个 Processor OS 线程。条件变量通知只使线程有机会变成 runnable，真正何时获得 CPU 仍由 Linux 调度器决定。
6. Processor 恢复 Reader CRoutine，DataVisitor 从 ring 取出 shared pointer，执行捕获 Reader 裸 `this` 的 wrapper。`Reader::Enqueue` 将消息写入 Blocker，随后才调用用户 reader_func。
7. callback 返回后局部 shared pointer 释放一份强计数；ring 槽、Blocker、其他 Reader 或 transport 仍可能拥有对象。只有最后一份强引用释放，Image 才析构。

这条链解释了为什么 endpoint 对象关系与消息对象关系必须分开画。Reader 由 Node、Component 和调用方的 shared pointer 管理；Image 则由 Writer、transport、多个 ring 与 callback 的 shared pointer 管理。两个控制块完全不同。

## 关闭过程是反向拆除回调边，不是简单 delete

相机停止时，最危险的不是释放内存本身，而是某条线程仍能通过旧 callback 回到 endpoint。Reader 与 Writer 的 Shutdown 都是幂等的，但它们等待的范围不同。

:::{mermaid}
sequenceDiagram
  participant Owner as lifecycle owner
  participant Pub as publisher thread
  participant W as Writer
  participant Topo as ChannelManager
  participant R as Reader
  participant S as Scheduler

  Owner->>Pub: request stop
  Pub-->>Owner: join / no more Write
  Owner->>W: Shutdown()
  W->>W: lock, init_ = false, unlock
  W->>Topo: RemoveChangeListener + Leave
  W->>W: release transmitter

  Owner->>R: Shutdown()
  R->>R: init_.exchange(false)
  R->>Topo: RemoveChangeListener + Leave
  R->>R: release local receiver handle
  R->>S: RemoveTask(croutine_name)
  S->>S: wait until routine is not executing
  S-->>R: task removed
  Note over Topo,R: listener disconnect 不等待已复制执行的在途回调
  R-->>Owner: Scheduler task 已移除；拓扑回调仍需单独确认静止
:::

Reader 的固定实现如下：

```cpp
template <typename MessageT>
void Reader<MessageT>::Shutdown() {
  if (!init_.exchange(false)) {
    return;
  }
  LeaveTheTopology();
  receiver_ = nullptr;
  channel_manager_ = nullptr;

  if (!croutine_name_.empty()) {
    scheduler::Instance()->RemoveTask(croutine_name_);
  }
}
```

析构函数再次调用 Shutdown，`init_.exchange(false)` 是单个原子读—改—写：它把标志设为 false，并返回操作前的值，因此只有第一次关闭继续执行。`RemoveTask()` 会等待 Reader 的 CRoutine 不再运行，但它只覆盖 scheduler 执行路径，不覆盖前面讨论的 ChannelManager signal callback。并且 `Shutdown()` 在 `LeaveTheTopology()` 解绑后，马上把 `receiver_` 置空，再到函数末尾才等待 routine；如果 topology callback 正在另一线程执行，固定源码中没有一个显式的 in-flight signal barrier 保证它一定先于 `receiver_` 清理结束。是否由更高层调用顺序避免了这种交错，需要检查实际部署的事件线程和生命周期协议，不能仅从 `RemoveChangeListener()` 推导出来。若用户 callback 永不返回，`RemoveTask()` 也可能永久等待；C++ 没有安全、通用的外部强杀线程栈方案。

因此，图中的 `task removed` 只表示调度器不再让这只 Reader routine 执行，并不是 Reader 整体生命周期的通用“销毁安全”证明。拓扑通知是否还在访问 Reader、以及调用方是否已经停止所有 `Write()`，分别属于另外两条并发边界；必须由上层事件线程的停止/等待协议和生产者 join 来闭合，不能由 `RemoveTask()` 一并代劳。

Writer 的 `Shutdown()` 先在 mutex 下把 `init_` 设为 false，随后锁外退出 topology 并释放 transmitter。下面是固定提交中的实现：

```cpp
void Writer<MessageT>::Shutdown() {
  {
    std::lock_guard<std::mutex> g(lock_);
    if (!init_) {
      return;
    }
    init_ = false;
  }
  LeaveTheTopology();
  transmitter_ = nullptr;
  channel_manager_ = nullptr;
}
```

这里存在一个必须诚实说明的并发边界：`Write()` 调用 `IsInit()` 时只在检查期间持锁，返回后在锁外解引用 `transmitter_`；并发 Shutdown 可能在两步之间把 transmitter 清空。

所以固定实现不能支持“任意线程随时 Write，同时另一线程销毁 Writer”这一强契约。生命周期 owner 应先停止并 join 发布线程或定时器，确认不会再调用 Write，然后才 Shutdown/析构 Writer。RAII 能保证作用域结束时调用清理函数，不能自动建立跨线程停止屏障。

`Node::DeleteReader(channel)` 也不是强制删除对象。它只从 Node map erase 一份 `shared_ptr<ReaderBase>`；调用方或 Component vector 若仍持有 Reader，强计数不为零，对象继续存在。shared ownership 允许句柄独立于 registry 存活，但 API 名称容易让读者误以为立即析构。

Standalone Node 析构函数体为空，依靠成员析构释放自身 map 与两个 impl。外部保存的 Reader shared pointer 仍可能活过 Node，因此 Node 析构也不是“所有通信已经停止”的全局屏障。Component 路径额外通过 `ComponentBase::Shutdown()` 逐只关闭 readers，再移除 Component task；用户创建的 Writer 通常还需要派生 Component 的 `Clear()` 或外部生命周期逻辑先停止生产者。

## 这些选择解决了什么，又留下什么代价

Node/Reader/Writer 这组设计的精华在边界而不在模式标签：

| 选择 | 直接收益 | 代价与适用边界 |
|---|---|---|
| 模板 endpoint + 非模板 base | payload 强类型，同时允许异构容器和统一生命周期 | 模板实现进入头文件，增加编译时间；类型恢复需要 cast |
| Node 强持有 Reader、调用方持有 Writer | Reader 支持 Node 级 Observe/查询；Writer 与业务输出自然同寿命 | 所有权不对称必须被 API 使用者理解；Node 无法统一停止所有 Writer |
| NodeChannelImpl 集中 factory | 运行模式和身份填充不污染 Node 门面 | 不是 ABI pimpl；模板仍让编译依赖较宽 |
| 每类型/通道共享 Receiver | 避免重复底层订阅和解析，统一扇出 | singleton map 延长 Receiver 寿命；热删除不等于立即回收 |
| 每消费者 DataVisitor ring | 慢消费者不移动别人的游标 | fan-out 复制 shared pointer、每消费者占槽位和锁 |
| topology listener + 初始快照 | 覆盖已有和未来对端 | callback 捕获裸 this；注销只阻止后续选择，在途回调仍需 quiescence 协议 |
| shared_ptr payload | 异步阶段易于延长消息寿命 | 原子引用计数有竞争成本；可变 payload 需要额外约束 |

从模式角度看，NodeChannelImpl 的 CreateReader/CreateWriter 是 Factory Method，ReaderBase/WriterBase 是运行时多态接口，Node 对两个 Impl 使用组合，ChannelManager 的变化通知具有 Observer 特征。但这些名称只能作为分析结果：真正重要的是“运行模式会变化”“Node 需要异构 owner”“对端会动态加入”三个工程变化点。

对于机器人控制链，还要关注这些边界对时序的影响。`Write(const T&)` 的堆分配、shared pointer 引用计数、Reader task 的排队、Processor 唤醒都会贡献抖动；pending queue 太深会让控制器处理陈旧样本；共享 Receiver 减少重复解析，却让同 channel 的扇出成本集中到一条 ingress 路径。它们提升了工程可组合性，不等于提供硬实时保证。

## 自己复刻 endpoint：先守住三个边界

读到这里，可以尝试编写一个小型进程内 Node：用模板维护消息类型，用非模板 `ReaderBase` 让 Node 管理异构 Reader，用 `condition_variable` 让回调在工作线程上执行。正确性先于“功能像不像 Cyber”：同一路 channel 的多个订阅者必须各自收到消息；慢 callback 不能持有全局 registry 锁；关闭必须停止生产者、等待 in-flight callback，再释放它们访问的对象。

[连续实现实验](cpp-implementation-lab.md)文末的“可运行的 Node、Reader 与 Writer 最小版”收录了 `mini_node.cc` 的完整 C++17 教学代码、编译命令和运行入口。可以先沿本篇源码理解真实的 endpoint，再用这个缩小版验证模板与基类分工、消息扇出和线程退出协议。

## 从 endpoint 继续走向完整运行时

现在 Node、Reader 与 Writer 已经不再只是 API 名称。模板守住消息类型，非模板基类提供异构生命周期，Node 只拥有需要统一观察的 Reader，业务对象拥有 Writer；NodeChannelImpl 把配置补成运行时身份，ReceiverManager 把相同类型与 channel 的 ingress 合并，DataVisitor 再把消息接收与 callback 执行分开。

这套结构的正确性最终落在关闭顺序上：先停止生产者，再注销 topology callback，随后移除并等待 Reader task，最后释放 endpoint owners。理解这条反向链，才能解释 shared pointer 在哪里有用、哪里无能为力，也才能安全地从最小 typed bus 继续实现 Dispatcher、CRoutine 和 Scheduler。
