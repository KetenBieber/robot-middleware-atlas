# 全局生命周期：CGlobals 的对象图与逆序关闭

eCAL 的 Publisher、Subscriber、registration、reader layer、SHM observer 和监控都共享进程级资源。`CGlobals` 把这些对象组织成一张依赖图，公共 `Initialize()` 与 `Finalize()` 决定它们何时可用、以什么顺序停止。

本章固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`；TCP 子模块涉及的源码固定为 `eclipse-ecal/tcp_pubsub@352e711b9ef10fec42ba7536bda244f43bf092cc`。

生命周期顺序不是清理风格问题。Subscriber 析构仍要访问 reader layer 和 registration provider；observer 线程退出前仍可能访问 memfile map；服务 callback 可能保存其他 gate 的裸指针。销毁依赖早于使用者会产生 use-after-free。

## 进程顶层拥有 Runtime

公共 `eCAL::Initialize()` 安装配置与 unit name，按 component mask 决定启用 publisher、subscriber、service 等子系统，再创建全局 `CGlobals`。

推荐由进程入口唯一拥有这对调用。下面是教学调用例，不是 eCAL 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
int main() {
  if (!eCAL::Initialize("camera_node")) return 1;
  RunApplication();
  eCAL::Finalize();
}
```

本专题固定提交的 Core API 使用 `Initialize(unit_name)`，而不是旧主版本常见的 `Initialize(argc, argv, ...)`。业务库不应各自无条件 Initialize/Finalize。该生命周期不是每个调用者独立引用计数；某个库提前 Finalize 可能拆除整个进程共享运行时。

## Component mask 控制对象图裁剪

只需要 Publisher 的进程无需创建完整 service/client 子系统。Component mask 允许按功能建立部分对象图。

裁剪要求 accessor 能区分“尚未初始化”和“此组件未启用”。返回空或失败比构造半功能对象更安全。

依赖检查也应在初始化期完成，例如创建 SubGate 需要相应 reader layers 与 registration provider，不能等第一次订阅才发现缺失。

## 初始化分为创建与启动

一个稳健运行时通常先构造全部对象及依赖，再启动线程。下面是推荐的两阶段模型，不是 eCAL 固定提交的逐字执行顺序：

```text
Phase 1: allocate/configure objects
Phase 2: wire callbacks and references
Phase 3: start threads/transports
Phase 4: publish initialized=true
```

若构造一个对象就立刻启动线程，该线程可能通过 accessor 看见尚未建立的后续依赖。

固定提交先创建 gates、memfile infrastructure、registration 和 reader layers，再启动 registration provider/receiver、memfile pool、各 gate 与 monitoring。但 `CGlobals::Initialize()` 并没有等所有相关对象都创建完再发布状态：它在启动这些组件后执行 `initialized.store(true)`，随后才创建/启动可选 TimeGate。源码注释解释了这个次序：TimeGate 可能创建 subscriber，所以此时必须让 eCAL 被判断为已初始化；同一注释也称该设计“very fragile”。这是一项特定版本的可观察顺序，不应把前面的理想模型误写成源码事实。

## CGlobals 的主要所有权图

```text
CGlobals
  +-- DescGate
  +-- MemFileMap
  +-- MemFileThreadPool
  +-- PubGate
  +-- SubGate
  +-- ServiceGate / ClientGate
  +-- RegistrationProvider
  +-- RegistrationReceiver
  +-- Monitoring
  +-- SHM ReaderLayer
  +-- UDP ReaderLayer
  +-- TCP ReaderLayer
  `-- TimeGate
```

Gate 继续强持有 PublisherImpl/SubscriberImpl；Reader layer 强持有其 transport resources；observer pool 持有 memfile observers 和 threads。

画出所有权图后，关闭顺序就可以由边方向推导，而不是依赖析构成员的偶然声明顺序。

## Initialize 的依赖顺序

`CGlobals::Initialize()` 的主要过程可整理为：

```text
1. create description gate
2. create memory-file map and observer pool
3. create enabled pub/sub/service/client gates
4. create registration provider/receiver and inject gates
5. create monitoring
6. create SHM/UDP/TCP reader layers
7. start registration provider and receiver
8. attach description callbacks
9. start memfile pool and gates
10. mark initialized
11. create/start time synchronization gate
```

具体成员可能随版本变化，但不变量是：线程启动前依赖对象存在；对外 accessor 生效前，公共功能已经完成装配。

下面直接看固定提交中的 `CGlobals::Initialize()`。这是函数主体的连续源码摘录，保留可执行语句、略去原注释；它展示 gate、registration、reader layer 的创建，线程启动、`initialized` 发布和 TimeGate 创建顺序。平台编译开关决定部分分支是否参与当前构建：


```cpp
bool CGlobals::Initialize(unsigned int components_)
{
  bool new_initialization(false);

  if (!descgate_instance)
  {
    descgate_instance = std::make_shared<CDescGate>();
    new_initialization = true;
  }

#if defined(ECAL_CORE_REGISTRATION_SHM) || defined(ECAL_CORE_TRANSPORT_SHM)
  if (!memfile_map_instance)
  {
    memfile_map_instance = std::make_shared<CMemFileMap>();
    new_initialization = true;
  }
  if (!memfile_pool_instance)
  {
    memfile_pool_instance = std::make_shared<CMemFileThreadPool>(memfile_map_instance);
    new_initialization = true;
  }
#endif

#if ECAL_CORE_SUBSCRIBER
  if ((components_ & Init::Subscriber) != 0u)
  {
    if (!subgate_instance)
    {
      subgate_instance = std::make_shared<CSubGate>();
      new_initialization = true;
    }
  }
#endif

#if ECAL_CORE_PUBLISHER
  if ((components_ & Init::Publisher) != 0u)
  {
    if (!pubgate_instance)
    {
      pubgate_instance = std::make_shared<CPubGate>();
      new_initialization = true;
    }
  }
#endif

#if ECAL_CORE_SERVICE
  if ((components_ & Init::Service) != 0u)
  {
    eCAL::service::ServiceManager::instance()->reset();
    if (!servicegate_instance)
    {
      servicegate_instance = std::make_shared<CServiceGate>();
      new_initialization = true;
    }
    if (!clientgate_instance)
    {
      clientgate_instance = std::make_shared<CClientGate>();
      new_initialization = true;
    }
  }
#endif

#if ECAL_CORE_REGISTRATION
  const Registration::SAttributes registration_attr =
    BuildRegistrationAttributes(eCAL::GetConfiguration(), eCAL::Process::GetProcessID());
  if (!registration_provider_instance)
  {
    SRegistrationProviderContext registration_provider_context;
    registration_provider_context.attributes  = registration_attr;
    registration_provider_context.memfile_map = memfile_map_instance;
    registration_provider_context.subgate     = subgate_instance;
    registration_provider_context.pubgate     = pubgate_instance;
    registration_provider_context.servicegate = servicegate_instance;
    registration_provider_context.clientgate  = clientgate_instance;
    registration_provider_instance = std::make_shared<CRegistrationProvider>(registration_provider_context);
    new_initialization = true;
  }
  if (!registration_receiver_instance)
  {
    SRegistrationReceiverContext registration_receiver_context;
    registration_receiver_context.attributes  = registration_attr;
    registration_receiver_context.memfile_map = memfile_map_instance;
    registration_receiver_instance = std::make_shared<CRegistrationReceiver>(registration_receiver_context);
    new_initialization = true;
  }
#endif

#if ECAL_CORE_MONITORING
  if ((components_ & Init::Monitoring) != 0u)
  {
    if (!monitoring_instance)
    {
      monitoring_instance = std::make_shared<CMonitoring>(registration_receiver_instance);
      new_initialization = true;
    }
  }
#endif

  m_shm_reader_layer_instance = std::make_shared<eCAL::CSHMReaderLayer>(subgate_instance, memfile_pool_instance);
  m_udp_reader_layer_instance = std::make_shared<eCAL::CUDPReaderLayer>(subgate_instance);
  m_tcp_reader_layer_instance = std::make_shared<eCAL::CTCPReaderLayer>(subgate_instance);

#if ECAL_CORE_REGISTRATION
  if (registration_provider_instance) registration_provider_instance->Start();
  if (registration_receiver_instance) registration_receiver_instance->Start();
#endif
  if (descgate_instance)
  {
#if ECAL_CORE_REGISTRATION
    if (registration_receiver_instance)
      registration_receiver_instance->SetCustomApplySampleCallback("descgate", [this](const auto& sample_) {
        if (descgate_instance) descgate_instance->ApplySample(sample_, tl_none);
      });
#endif
  }
#if defined(ECAL_CORE_REGISTRATION_SHM) || defined(ECAL_CORE_TRANSPORT_SHM)
  if (memfile_pool_instance) memfile_pool_instance->Start();
#endif
#if ECAL_CORE_SUBSCRIBER
  if (subgate_instance && ((components_ & Init::Subscriber) != 0u)) subgate_instance->Start();
#endif
#if ECAL_CORE_PUBLISHER
  if (pubgate_instance && ((components_ & Init::Publisher) != 0u)) pubgate_instance->Start();
#endif
#if ECAL_CORE_SERVICE
  if (servicegate_instance && ((components_ & Init::Service) != 0u)) servicegate_instance->Start();
  if (clientgate_instance && ((components_ & Init::Service) != 0u)) clientgate_instance->Start();
#endif
#if ECAL_CORE_MONITORING
  if (monitoring_instance && ((components_ & Init::Monitoring) != 0u)) monitoring_instance->Start();
#endif

  components |= components_;
  initialized.store(true);

#if ECAL_CORE_TIMEPLUGIN
  if ((components_ & Init::TimeSync) != 0u)
  {
    if (timegate_instance == nullptr)
    {
      timegate_instance = CTimeGate::CreateTimegate();
      new_initialization = true;
    }
  }
#endif

  return new_initialization;
}
```

上面的上下文赋值把 gates 和 memfile map 的 `shared_ptr` 放入 registration 构造上下文；构造函数再把上下文保存在成员中，因此这些引用共享控制块，`CGlobals` reset 自己的成员不会销毁仍有其他强引用的对象。三个 reader layer 接着拿到 SubGate（以及 SHM 所需的 observer pool），之后才逐个 `Start()`。DescGate 回调捕获 `[this]`，所以它把 CGlobals 生命周期变成回调源的依赖边。最关键的状态边界在代码末尾：先启动组件，再合并 component mask、存储 `initialized=true`，最后才创建可能注册 Subscriber 的 TimeGate。

## 半初始化对象不通过普通 accessor 暴露

全局 accessor 只有在全局指针存在且 `IsInitialized()` 为 true 时才返回 CGlobals。Initialize 内部不能依赖普通 accessor 获取正在构造的自己，而应通过参数或成员直接注入依赖。

这能避免一部分半初始化访问，但并不构成“所有组件就绪”的原子发布：TimeGate 还在后面创建，且 `initialized` 是 `std::atomic<bool>` 的默认顺序存储，不能替代多个对象之间的生命周期协调。需要判断接口可用性的具体条件时，应继续追踪 accessor 对各个 component 的检查，而不能只看这个总标志。

## 启动失败需要逆序回滚

假设 UDP reader 创建成功，TCP executor 启动失败。Initialize 不能只返回 false 并留下前者线程。

下面是推荐的启动回滚伪代码，不是 eCAL 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
if (!StartRegistration()) return Rollback();
if (!StartReaders())      return Rollback();
if (!StartGates())        return Rollback();
```

Rollback 与正常 Finalize 应复用相同 Stop primitive，否则错误路径往往比正常路径更容易泄漏线程和命名对象。

## Finalize 的真实次序不是简单“先封入口”

固定提交的 `CGlobals::Finalize()` 先 `timegate_instance.reset()`，注释明确要求 TimeGate 先停、但此时 eCAL 仍被视为运行；之后才用 `initialized.exchange(false)` 尝试取得关闭权。只有 exchange 返回 true 才继续拆除。也就是说，调用刚进入 Finalize 时公共 initialized 标志尚未关闭，不能把它描述成“先封入口再停止所有生产者”。下方的 `CGlobals::Finalize()` 连续摘录会逐步展示这个先后关系。

这也解释了为什么关闭不能只看一个布尔值：TimeGate 的析构、公共 accessor、已经取得的 `shared_ptr`、registration 回调和 reader worker 是不同入口，必须分别分析。`initialized=false` 阻止部分新的全局访问，却不会撤销其他线程已取得的强引用，也不会自动等完已有 callback。

## Gate 必须早于 Reader Layer 销毁

SubGate `Stop()` 清空 topic multimap，释放 SubscriberImpl strong pointer。SubscriberImpl 析构还会：

- 从 UDP/TCP/SHM reader layer 移除订阅；
- 在 receive mutex 下清空 callback，但不会为阻塞中的同步 `Read()` 设置 shutdown 谓词或发送关闭通知；
- 把 created 设为 false；
- 提交 unregister sample。

因此 Gate/Impl 清理时，reader layers 与 registration provider 必须仍存在。若先 reset reader layer，Subscriber 析构会访问已释放依赖。

这就是“先销毁使用者，再销毁被使用资源”。

关闭顺序来自同一函数，而不是只靠上面的列表推测。以下摘录保留 `Finalize()` 的全部可执行语句，省去原注释：

接着看 `CGlobals::Finalize` 的真实实现：

```cpp
bool CGlobals::Finalize()
{
#if ECAL_CORE_TIMEPLUGIN
  timegate_instance.reset();
#endif

  if (!initialized.exchange(false)) return false;

#if ECAL_CORE_MONITORING
  if (monitoring_instance) monitoring_instance->Stop();
#endif
#if ECAL_CORE_SERVICE
  eCAL::service::ServiceManager::instance()->stop();
  if (clientgate_instance)  clientgate_instance->Stop();
  if (servicegate_instance) servicegate_instance->Stop();
#endif
#if ECAL_CORE_PUBLISHER
  if (pubgate_instance) pubgate_instance->Stop();
#endif
#if ECAL_CORE_SUBSCRIBER
  if (subgate_instance) subgate_instance->Stop();
#endif
  if (descgate_instance)
  {
#if ECAL_CORE_REGISTRATION
    if (registration_receiver_instance)
      registration_receiver_instance->RemCustomApplySampleCallback("descgate");
#endif
  }
#if ECAL_CORE_REGISTRATION
  if (registration_receiver_instance) registration_receiver_instance->Stop();
  if (registration_provider_instance) registration_provider_instance->Stop();
#endif
#if defined(ECAL_CORE_REGISTRATION_SHM) || defined(ECAL_CORE_TRANSPORT_SHM)
  if (memfile_pool_instance) memfile_pool_instance->Stop();
  if (memfile_map_instance) memfile_map_instance->Stop();
#endif

#if ECAL_CORE_MONITORING
  monitoring_instance.reset();
#endif
#if ECAL_CORE_SERVICE
  servicegate_instance.reset();
  clientgate_instance.reset();
#endif
#if ECAL_CORE_PUBLISHER
  pubgate_instance.reset();
#endif
#if ECAL_CORE_SUBSCRIBER
  subgate_instance.reset();
#endif
#if ECAL_CORE_REGISTRATION
  registration_receiver_instance.reset();
  registration_provider_instance.reset();
#endif
  descgate_instance.reset();
#if defined(ECAL_CORE_REGISTRATION_SHM) || defined(ECAL_CORE_TRANSPORT_SHM)
  memfile_pool_instance.reset();
  memfile_map_instance.reset();
#endif
  m_udp_reader_layer_instance.reset();
  m_tcp_reader_layer_instance.reset();
  m_shm_reader_layer_instance.reset();

  return true;
}
```

先 reset TimeGate 的动作发生在 `exchange(false)` 之前；这个原子交换既关闭全局 initialized 状态，也让并发 `Finalize()` 中只有一个调用继续拆解。之后的 `Stop()` 与 `reset()` 是两个不同阶段：前者要求各对象停止工作，后者撤销 CGlobals 持有的强引用。服务管理器必须先停，因为 service callback 保存 gate 函数的裸指针；Subscriber gate 在 reader layer 之前停止/reset，因为 Subscriber 析构还要注销 transport。即使函数顺序满足依赖，也要继续问每个 Stop 是否 join 其线程、是否等待已经复制出的 callback 快照；顺序本身不是全局静止屏障。

## Registration Receiver 与 Provider 的停止位置

固定提交先处理 Gate，再停止 registration receiver/provider。控制样本入口因此不是在 Gate 清理前统一关闭；阅读某次清理是否仍会与接收回调并发，必须继续看 receiver 的 Stop 是否等待线程退出，以及 gate 的状态检查和锁。Service 的顺序是源码特别注释的例外：先停 ServiceManager，再停 ClientGate/ServiceGate，因为 service implementation 的 callback 持有 gate 函数的裸指针。Registration provider 则在 Gate Stop 之后才停止，使实体清理仍能提交 unregister。源码顺序为：

```text
stop monitoring
stop service manager, then client/service gates
stop publisher/subscriber gates
remove description callback; stop registration receiver
stop registration provider
stop/join memfile observer pool; stop memfile map
reset gates, registration objects and reader layers
```

这不是教科书式的统一反向拓扑排序，而是源代码里按组件逐个执行的协议。SHM observer pool 的 Stop 会通知并 join 每个 observer；这给 SHM callback 提供一个实际等待点。相反，Gate 清空时复制出去的 `shared_ptr` 快照仍可让某次 `ApplySample()` 继续执行，所以 `Stop()` 返回本身不等于所有 Subscriber callback 都已结束。UDP/TCP 的 teardown 语义也不能从 SHM 的 join 推广，TCP executor 的实现还位于该源码树之外。

## Callback 是隐藏依赖边

对象图不仅由成员指针构成。注册到另一个模块的 callback 也可能捕获 raw pointer 或 `this`：

```text
ServiceManager callback -> ServiceGate
DescGate callback       -> other gate/context
Observer callback       -> Subscriber layer
```

被捕获对象销毁前必须先 unregister callback 或停止 callback source。否则容器表面上已清空，后台事件仍能调用悬空地址。

核对关闭路径时，应继续搜索 `SetCallback`、`AddCallback`、lambda 捕获和 userdata，而不只看类成员。

## 停止线程分为 Signal、Join、Destroy

每个拥有线程的模块应遵循：

```text
Signal:  设置 stopping 并唤醒阻塞等待
Join:    等线程确认退出
Destroy: 释放线程可能访问的 queue/mutex/memory
```

只设置 flag 不唤醒可能卡在 event wait；先释放 queue 再 join 会发生 use-after-free；detach 线程则无法证明资源安全回收。

SHM observer、registration receiver、provider 周期线程和 TCP executor 都应按这个模型检查。TCP 是重要例外：关闭一个 Publisher 不等于关闭共享 executor。

## TCP Publisher 关闭了，进程线程池仍可能活着

固定版本的 `CDataWriterTCP` 把 executor 放在静态 `shared_ptr` 中，并用静态 mutex 保护首次创建。第一个 TCP writer 的 `thread_pool_size` 决定这份 executor 的线程数；随后创建的 writer 复用同一个对象。相关连续源码摘录如下：


```cpp
std::mutex                            CDataWriterTCP::g_tcp_writer_executor_mtx;
std::shared_ptr<tcp_pubsub::Executor> CDataWriterTCP::g_tcp_writer_executor;

CDataWriterTCP::CDataWriterTCP(const eCAL::eCALWriter::TCP::SAttributes& attr_) :
  m_attributes(attr_)
{
  {
    const std::lock_guard<std::mutex> lock(g_tcp_writer_executor_mtx);
    if (!g_tcp_writer_executor)
    {
      g_tcp_writer_executor = std::make_shared<tcp_pubsub::Executor>(m_attributes.thread_pool_size, TcpPubsubLogger);
    }
  }

  m_publisher = std::make_shared<tcp_pubsub::Publisher>(g_tcp_writer_executor, GetPreferredAnyAddress(), ANY_PORT);
  m_port      = m_publisher->getPort();
}
```

这段代码做了两个不同生命周期的动作：静态 executor 只在第一次构造 writer 时建立；每个 writer 则各自创建 `tcp_pubsub::Publisher`，共享 executor 只负责运行它们的异步 I/O。因而，即使所有实体都销毁，静态 executor 仍有强引用，不能从“Publisher 数量变成零”推导出线程池已停止。

`tcp_pubsub` 固定子模块提交 `352e711b9ef10fec42ba7536bda244f43bf092cc` 中，公开 `Publisher` 析构会调用 `Publisher_Impl::cancel()`。后者关闭 acceptor、取消等待中的 accept，并在会话列表锁内复制 `shared_ptr<PublisherSession>` 快照，解锁后逐个取消 session：


```cpp
void Publisher_Impl::cancel()
{

#if (TCP_PUBSUB_LOG_DEBUG_ENABLED)
  log_(logger::LogLevel::Debug, "Publisher " + localEndpointToString() + ": Shutting down");
#endif

  {
    asio::error_code ec;
    acceptor_.close(ec);
    acceptor_.cancel(ec);
  }

  is_running_ = false;

  std::vector<std::shared_ptr<PublisherSession>> publisher_sessions;
  {
    // Copy the list, so we can safely iterate over it without locking the mutex
    const std::lock_guard<std::mutex> publisher_sessions_lock(publisher_sessions_mutex_);
    publisher_sessions = publisher_sessions_;
  }
  for (const auto& session : publisher_sessions)
  {
    session->cancel();
  }
}
```

这个快照只保护取消循环期间的 Session 对象生命周期；锁外的 `session->cancel()` 让关闭逻辑不持有列表 mutex 调用另一对象。它关闭的是某个 Publisher 的 acceptor 与连接，不是 executor 的 `io_context`。eCAL 的 `CGlobals::Finalize()` 拆掉 Gate 时会释放 Gate 持有的 PublisherImpl 强引用；若 `CPublisher::Send()` 等调用栈仍临时持有强引用，writer 的析构可以延后到该调用退出。最终 writer 释放时会沿成员析构进入 Publisher 取消路径，但静态 `g_tcp_writer_executor` 仍存在。因此多次 `Initialize/Finalize` 之间复用 executor 是可能的，最后一个 TCP Topic 消失也不代表进程线程数立即下降。

这里还存在一条必须纳入关闭审查的竞态窗口。`is_running_` 是 atomic，但它只让发送方观察到“停止发送”；Session vector 则由另一把 mutex 保护。`acceptClient()` 的异步 accept handler 没有串到 Session 的 `data_strand_`，也没有在成功分支先重查 `is_running_`。因此若 accept 已成功、handler 尚未执行，外部关闭线程可以先关闭 acceptor、设停止位并完成 Session 快照；之后 accept handler 仍可启动新 Session 并把它追加到快照之外：

```text
close thread:   close acceptor -> running=false -> snapshot sessions -> cancel snapshot
executor:       successful accept completion waits in io_context queue
executor:       handler runs -> session.start() -> append to sessions -> accept again
```

这条时间线可由固定 `eclipse-ecal/tcp_pubsub@352e711b9ef10fec42ba7536bda244f43bf092cc` 中的 `Publisher_Impl::cancel()` 与 `acceptClient()` 对照核实：只有 `PublisherSession::data_strand_` 的 handlers 才绑定该 strand，而 accept handler 直接交给共享 `io_context`。Session 的关闭回调捕获 `shared_from_this()` 的 Publisher_Impl，Publisher_Impl 又在 vector 中强持有 Session；正常 `session->cancel()` 会经 `sessionClosedHandler()` 把它从 vector 删除并打破这组强引用。若新 Session 恰好晚于取消快照加入，且远端保持 TCP 连接，它没有被本次 cancel 覆盖，可能让本地仍看到残留连接，并延长两侧对象生命周期；之后远端断开才会进入读错误/关闭路径。发送端的 atomic stop 位会让 `send()` 拒绝新样本，但不会替代 Session 清理。完整引用链还涉及 `PublisherSession::start()` 与 `sessionClosedHandler()`。这是从该固定版本的锁域和 handler 控制流推出的关闭竞态，不应写成作者已声明的缺陷或意图。

复刻时可以把 accept/cancel 放到同一个 strand，或用关闭状态与 in-flight accept 屏障串行化：停止方先禁止新 accept、取消正在等待的 accept，再等其完成 handler 收敛；成功 handler 若在关闭态取得连接，必须立即取消新 Session，而不是登记为活跃连接。代价是关闭协议更复杂，若在 executor worker 自己等待该屏障会自我死锁，所以等待应由外部 owner 执行或拆成异步 drain 状态。

executor 自己的关闭发生在它的析构函数里，而不是每个 eCAL `Finalize()` 里。其固定源码明确调用 `stop()`：


```cpp
Executor::~Executor()
{
  executor_impl_->stop();
}
```

子模块中的 `Executor_Impl::stop()` 完整函数还会删除 work guard 并停止 `io_context`：

接着看 `Executor_Impl::stop()` 的真实实现：

```cpp
void Executor_Impl::stop()
{
#if (TCP_PUBSUB_LOG_DEBUG_ENABLED)
  log_(logger::LogLevel::Debug, "Executor::stop()");
#endif

  // Delete the dummy work
  dummy_work_.reset();

  // Stop the IO Service
  io_context_->stop();
}
```

worker 启动片段揭示了另一个所有权关系：每个线程捕获 `shared_from_this()`，因此 `Executor_Impl` 本体至少活到这些线程退出 `run()` 并释放捕获副本。下面是 `Executor_Impl::start()` 中创建线程的连续片段：

接着看 `Executor_Impl::start()` 的真实实现：

```cpp
for (size_t i = 0; i < thread_count; i++)
{
  thread_pool_.emplace_back([me = shared_from_this()]()
                            {
#if (TCP_PUBSUB_LOG_DEBUG_ENABLED)
                              std::stringstream ss;
                              ss << std::this_thread::get_id();
                              const std::string thread_id = ss.str();

                              me->log_(logger::LogLevel::Debug, "Executor: IoService::Run() in thread " + thread_id);
#endif

                              me->io_context_->run();

#if (TCP_PUBSUB_LOG_DEBUG_ENABLED)
                              me->log_(logger::LogLevel::Debug, "Executor: IoService: Shutdown of thread " + thread_id);
#endif
                            });
}
```

`Executor_Impl::start()` 为每个配置线程构造 `std::thread`；线程 lambda 捕获 `shared_from_this()`，然后阻塞在 `io_context_->run()` 等待异步操作。`work_guard` 让没有 socket 事件时 `run()` 也不会因“当前无工作”而退出。eCAL `Finalize()` 只取消 writer/session，不 reset 这份静态 executor，所以线程可以继续处于内核可等待状态；这不等于它们正在消耗一个 CPU 核。

更需要注意的是，子模块的 `Executor_Impl` 析构函数会 `detach()` 线程句柄，而不是对每个 `std::thread` 做 `join()`；线程持有自己的 shared pointer，源码注释说它们退出时管理自身生命周期。这里的 `stop()` 只是请求 `io_context::run()` 返回，不能单独证明调用者已经等到所有线程完成。文章能从固定源码确认“没有显式 join 屏障”，但不能仅靠这几个函数断言 eCAL 的所有构建和宿主环境都不支持卸载。

若应用把 eCAL core 作为动态库装载，`dlclose`/宿主卸载会涉及静态对象析构和代码映射生命周期。已经在该库代码中运行或即将退出的 worker 必须先结束，库代码才可安全卸载；但上述 executor 路径没有向调用者暴露“所有 worker 已 join”的完成点。对需要热卸载的插件宿主，必须额外核实完整链接与关闭实现，不能把 `eCAL::Finalize()` 当作卸载屏障。可复刻系统若要支持热卸载，应由明确的 owner 执行 stop、等待 worker 全部退出，再销毁 callback/logger 和映射到的库代码；如果目标只覆盖进程退出，则应把支持边界写成进程级生命周期。

这些结论由固定 eCAL commit 与子模块 commit 中 `CDataWriterTCP`、`Publisher_Impl::cancel/acceptClient`、`PublisherSession::start/sessionClosedHandler` 和 `Executor_Impl::start/stop` 的连续摘录共同支撑。`std::thread` worker 在 Linux 上由内核调度；socket readiness 使 Asio 有机会派发 handler，但通知/就绪不等于内核马上分配 CPU，业务回调开始运行也还要经过该 executor 的队列与线程调度。

## 同步 Read 的关闭唤醒

固定版本的 `CSubscriberImpl::Read()` 对负 timeout 使用谓词 `m_read_buf_received` 永久等待；析构函数没有相应的 shutdown predicate/`notify_all`。同步 `Read()` 是 eCAL v5 兼容 facade 暴露的 `ReceiveBuffer()` 路径：facade 以 `shared_ptr` 成员持有 Impl，但调用 `Read()` 时没有先复制出一份局部强引用。因此，“Impl 一定因 Read 局部强引用而延迟析构”不是源码事实。能确定的是：只要 facade 仍持有 Impl，阻塞中的 `Read(-1)` 不会因 Gate 注销或 Runtime Finalize 被通知退出；若应用先 join 这个读取线程，且没有新样本，join 会一直等。若另一个线程同时在同一 facade 上调用 `Destroy()`，它会注销并 reset 该共享成员，而 facade 没有为 `ReceiveBuffer()` 与 `Destroy()` 建立互斥；调用方必须自行串行化生命周期，否则既可能悬挂，也不能证明在途 `Read()` 的 `this` 仍有效。下方同时展示 v5 facade 和 Impl 的真实等待/析构代码。


```cpp
CSubscriberImpl::~CSubscriberImpl()
{
  if (!m_created) return;

  StopTransportLayer();

  {
    const std::lock_guard<std::mutex> lock(m_receive_callback_mutex);
    m_receive_callback = nullptr;
  }

  m_created = false;
  Unregister();
}

bool CSubscriberImpl::Read(std::string& buf_, long long* time_, int rcv_timeout_ms_)
{
  if (!m_created) return(false);

  std::unique_lock<std::mutex> read_buffer_lock(m_read_buf_mutex);
  if (!m_read_buf_received)
  {
    if (rcv_timeout_ms_ < 0)
    {
      m_read_buf_cv.wait(read_buffer_lock, [this]() { return this->m_read_buf_received; });
    }
    else if (rcv_timeout_ms_ > 0)
    {
      m_read_buf_cv.wait_for(read_buffer_lock, std::chrono::milliseconds(rcv_timeout_ms_),
                             [this]() { return this->m_read_buf_received; });
    }
  }

  if (m_read_buf_received)
  {
    buf_.clear();
    buf_.swap(m_read_buf);
    m_read_buf_received = false;
    if (time_ != nullptr) *time_ = m_read_time;
    return(true);
  }
  return(false);
}
```

`Read()` 以 `unique_lock` 保护接收 buffer 和谓词，并让条件变量在等待期间暂时释放 mutex；收到通知后重新拿锁，再由谓词处理虚假唤醒。成功路径通过 `string::swap` 把 buffer 所有权内容交给调用者，并清掉“有新样本”状态。析构只停 transport、在 callback mutex 下置空 callback、再注销；它没有改 `m_read_buf_received`，也没有 `notify_all()`。所以负 timeout 等待者不会因 Runtime Finalize 或 Gate 注销自动退出。`m_created` 是普通成员，不能充当 Read 与并发 Destroy 之间的同步；v5 facade 的成员 shared_ptr 也没有并发保护，调用方须串行化这些操作。

v5 facade 的拥有关系和直接调用可由下面两段固定提交源码看出：


```cpp
bool CSubscriber::Destroy()
{
  if (m_subscriber_impl == nullptr) return(false);

  RemReceiveCallback();

  auto subgate = g_subgate();
  if (subgate) subgate->Unregister(m_subscriber_impl->GetTopicName(), m_subscriber_impl);

  m_subscriber_impl.reset();
  return(true);
}

bool CSubscriber::ReceiveBuffer(std::string& buf_, long long* time_, int rcv_timeout_) const
{
  if (m_subscriber_impl == nullptr) return(false);
  return(m_subscriber_impl->Read(buf_, time_, rcv_timeout_));
}

std::shared_ptr<CSubscriberImpl> m_subscriber_impl;
```

`ReceiveBuffer()` 通过成员 `shared_ptr` 取得裸 `this` 指针并进入 `Read()`，没有一个新的每次调用的强引用副本。单线程按序调用时，成员控制块负责保持 Impl；跨线程一边执行 `ReceiveBuffer()`、一边 reset 同一个 `shared_ptr` 成员，不只是缺少 shutdown 通知：对同一个 `shared_ptr` 成员对象的读写也没有同步，属于 data-race 候选，正在执行的成员函数还可能失去对象寿命保障。稳妥调用顺序是先用应用自己的停止条件让读取线程退出，再 join 线程，最后 `Destroy()`。不过单纯在该 `Read()` 上等新样本不是可取消等待：教学复刻应把 `stopping` 纳入同一 mutex 保护的等待谓词，并在停止时 `notify_all()`，让线程以 cancelled 状态返回后再 join。代价是 API 必须明确区分“收到空消息”和“等待被取消”，而不是把两种状态压成一个 bool。

可复刻实现应让等待谓词包含 shutdown。下面是推荐示例，不是 eCAL 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
stopping = true;
read_cv.notify_all();
```

Waiter 醒来后返回 cancelled/false，而不是把空 buffer 当成新消息。

## 在途 Callback 的等待

清除 Gate 索引不能自动终止已经取得 shared pointer 快照的 ApplySample。实现析构与关闭需要和 receive mutex/callback 生命周期协调，确保对象不会在 callback 栈上执行时释放。

Shared pointer 快照保护对象内存，但 callback 可能访问其他全局依赖；这些依赖也必须活到全部在途调用结束。

一种明确设计是每模块维护 in-flight counter：进入 callback 增加，退出减少；Stop 拒绝新进入并等待 counter 为零。

## Finalize 的概念逆序

可以整理成：

```text
1. reset TimeGate while the initialized flag is still true
2. exchange initialized to false; return if another close won
3. stop monitoring; stop service manager, then client/service/pub/sub gates
4. remove the description callback
5. stop registration receiver, then registration provider
6. stop/join memfile observer pool; stop memfile map
7. reset gates, registration, description, memfile and reader-layer owners
```

真实代码对 service raw callbacks 有特定顺序要求。逐项检查每个 Stop 是否同步等待、Gate 快照是否仍在途、以及 reset 前是否还有外部强引用；这些性质不会由列表顺序自动保证。

## 重复 Initialize/Finalize 的语义

如果 Initialize 不是引用计数，调用两次再 Finalize 一次通常不会保留“一份所有权”。多个库各自配对调用会互相影响。

更清晰的复刻封装可以提供以下租约模型；它是设计示例，不是 eCAL 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class RuntimeLease {
 public:
  RuntimeLease();   // process-wide refcount++
  ~RuntimeLease();  // refcount--, last one stops runtime
};
```

或者明确规定仅 main 拥有生命周期，库只接受已初始化 Runtime 引用。后者依赖更显式，也更容易测试。

## 生命周期失败模式

常见故障包括：

- Gate 已清空但 registration thread 又插入连接；
- Observer 仍运行，memfile map 已释放；
- Callback 捕获 raw pointer 的对象先析构；
- 阻塞 Read 未唤醒导致 Finalize 卡死；
- Publisher facade 仍存在但实现 weak pointer 已过期，调用静默失败；
- 半初始化失败留下 thread 或 named shared-memory object。

这些问题通常只在启动失败、快速重启或并发关闭时出现，正常数据流测试无法覆盖。

## 可复刻的 Runtime 所有权

**推荐接口骨架（不是 eCAL 源码）：**

```cpp
class Runtime {
 public:
  static Expected<std::unique_ptr<Runtime>, Error>
  Start(Config config);

  void Stop();
  ~Runtime() { Stop(); }

 private:
  std::atomic<State> state_{State::Constructing};
  std::unique_ptr<Registration> registration_;
  std::unique_ptr<ReaderLayers> readers_;
  std::unique_ptr<Gates> gates_;
  std::unique_ptr<ObserverPool> observers_;
};
```

让 `Start()` 完成后才返回对象。成员析构顺序仍应由显式 Stop 控制，避免仅靠声明逆序隐藏关键依赖。

状态机至少包括 Constructing、Running、Stopping、Stopped；Stop 使用 compare-exchange 保证幂等。

## 生命周期设计结论

`CGlobals` 的价值是把进程共享资源集中到一张所有权图。固定版本在大部分组件建立并启动后发布 `initialized`，再创建可选 TimeGate；Finalize 则先 reset TimeGate、随后关闭总标志，再依照 service callback、Gate、registration、SHM observer 和 reader-layer 的实际依赖收尾。源码中 SHM pool 的 join 能闭合它自己的线程访问，但 Gate 快照和永久 Read 等待说明：只有逐线程、逐回调检查等待屏障，才能证明完整关闭。

真正可复用的能力是“从依赖图推导逆序关闭”，并把 callback、线程和等待者也视为依赖边。只清空容器而不处理在途执行，不能算完成生命周期设计。
