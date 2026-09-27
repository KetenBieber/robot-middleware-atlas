# 多输入组件：DataVisitor 与 AllLatest 的消息组合语义

单输入组件只需要回答一个问题：下一条消息是否已经到达。多输入组件面对的是另一类问题：定位、底盘、预测、规划等输入以不同频率到达时，哪几条消息应当组成一次 `Proc(m0, m1, ...)` 调用。

这里 Component 是由 Cyber 管理生命周期的业务对象，`Proc(...)` 是框架凑齐一组输入后调用的业务虚函数；虚函数允许派生组件提供自己的计算，框架通过基类接口在运行时调用它。protobuf（Protocol Buffers）是 Apollo 使用的结构化消息定义与序列化格式。所谓“组成一组”不是把 protobuf 字段合并成新消息，而是决定本次调用分别使用每条 channel 的哪一个 `shared_ptr`。

Cyber RT 没有在这一层实现严格的时间戳同步器。严格同步会按消息携带的采样时间寻找同一时刻或给定时间窗内的组合；`AllLatest` 则采用“主输入触发，辅助输入取最新值”的 sample-and-hold（采样并保持）策略：主输入进入 Dispatcher 后，融合回调（消息进入缓存时运行的一段函数）逐路读取辅助缓存当前最新的消息指针，并把读到的指针组合写入融合 ring。之后即使辅助输入更新，本次 tuple 也不改变。这个选择代码量不大，却同时决定了融合组生成频率、数据新鲜度、锁顺序、内存分配和算法能够假设的时间关系。

先给几个当前必需的词一个落点：channel 是按名称标识的一路消息流；`shared_ptr<T>` 是共享所有权的 C++ 对象句柄，复制句柄不会复制 `T` 的消息本体；tuple（元组）是按固定位置保存不同类型值的一组数据；callback 是消息到来后由框架调用的业务函数。`M0` 表示配置中的第一路、由它触发组合，`M1` 表示后续辅助输入。文中 `text` 块是本文作者画的对象图、时序或计算过程，不是上游源码；每段 C++ 代码会标清固定源码摘录、教学错误示例或教学草图。

本文继续固定 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，从二输入组件开始，逐层拆开 `Component<M0, M1>`、`DataVisitor<M0, M1>`、`AllLatest<M0, M1>` 和底层 `ChannelBuffer`。

## 多输入组件的运行目标

假设一个融合组件接收：

- `M0 = PointCloud`，10 Hz；
- `M1 = Localization`，100 Hz。

最常见的业务意图不是让每条定位消息都触发一次点云处理，而是每到一帧点云，取当时最新的定位结果，然后执行一次融合：

```text
PointCloud #41 arrives  ----+
                           +--> Proc(cloud#41, pose#408)
latest Localization #408 --+
```

因此两路输入并不对称：

- `M0` 是主输入，决定何时产生一组融合数据；
- `M1` 是辅助输入，只提供主输入到达时的最新快照；
- 任一辅助输入从未产生过数据时，当前主输入不能形成完整参数组；
- 辅助输入单独更新不会立即调度组件。

这是一种采样语义，不是时间同步语义。它适合“慢主传感器 + 快状态估计”的计算图，却不自动保证两条消息具有相同时间戳。

## 从 Component 模板到 DataVisitor 模板

`Component<M0, M1>` 把输入类型写在类模板参数中：

下面是固定提交的类接口摘录：

```cpp
template <typename M0, typename M1>
class Component<M0, M1, NullType, NullType> : public ComponentBase {
 public:
  virtual bool Proc(const std::shared_ptr<M0>& msg0,
                    const std::shared_ptr<M1>& msg1) = 0;
};
```

这里的两个模板参数承担三项职责。

第一，编译器检查派生组件的 `Proc()` 参数。若业务类把第二个参数误写成别的 protobuf 类型，`override` 会直接导致编译错误。

第二，框架可以为两路消息分别实例化 `Reader<M0>`、`Reader<M1>`、`DataDispatcher<M0>` 与 `DataDispatcher<M1>`，不需要把所有消息塞进一个 `MessageBase*` 再做运行时转换。

第三，`CreateRoutineFactory<M0, M1>()` 能生成一个确切的闭包（closure）：闭包是 lambda 生成的可调用对象，并能保存其捕获的值；这里它先取出两只 `shared_ptr`，再以正确顺序调用业务 callback。

初始化期形成的对象图如下：

```text
Component<M0, M1>
  |
  +-- Reader<M0>  ---- transport receiver for channel 0
  +-- Reader<M1>  ---- transport receiver for channel 1
  |
  `-- DataVisitor<M0, M1>
        +-- ChannelBuffer<M0>       主输入缓存
        +-- ChannelBuffer<M1>       辅助输入缓存
        +-- AllLatest<M0, M1>       组合策略
        `-- Notifier                只绑定主输入 channel
```

Reader 解决“消息怎样进入当前进程”，DataVisitor 解决“当前消费者怎样观察这些消息”，AllLatest 解决“多路观察结果怎样组成一次调用”。三者不能合并成一个类，否则 transport 生命周期、消费游标和融合策略会互相污染。

## 固定输入上限与 NullType 特化

Cyber RT 为一至四路输入提供模板特化，以 `NullType`（标记“此输入位置未使用”的占位类型）表示不存在的尾部类型。下面是类型形状示意，展示固定提交支持的签名，并非完整类定义：

```cpp
Component<M0, NullType, NullType, NullType>  // 一输入
Component<M0, M1, NullType, NullType>        // 二输入
Component<M0, M1, M2, NullType>              // 三输入
Component<M0, M1, M2, M3>                    // 四输入
```

这是 C++11 时代常见的定长泛型设计。它的优点是每种 `Proc()` 签名直观，错误信息通常能落在具体特化中，生成的调用也没有运行时循环和类型分派。

代价同样明确：相似代码会在二、三、四输入版本中重复；输入上限成为框架能力的一部分；增加第五路输入需要同时扩展 Component、DataVisitor、DataFusion、AllLatest 和 RoutineFactory。

现代 C++ 教学草图：参数包可以表达任意输入数量；此片段只展示类型形状，不含运行实现：

```cpp
template<class... Messages>
class Component;

template<class... Messages>
using MessageTuple = std::tuple<std::shared_ptr<Messages>...>;
```

但参数包不会自动带来更好的工程结果。框架仍要定义主输入是谁、配置数组怎样与类型序列对齐、错误信息怎样保持可读，以及如何限制不合理的几十路组件。固定上限实际上把复杂度写进了 API 约束。

## DataVisitor 组合每个消费者的私有视图

二输入 `DataVisitor` 为两条 channel 分别创建 `ChannelBuffer`。每只 buffer 都属于这个 visitor，而不是属于全局 channel。下面是固定提交的二输入 `DataVisitor` 构造成员初始化摘录：

```cpp
buffer_m0_(config.channel_id[0],
           new CacheBuffer<std::shared_ptr<M0>>(
               config.queue_size[0])),
buffer_m1_(config.channel_id[1],
           new CacheBuffer<std::shared_ptr<M1>>(
               config.queue_size[1]))
```

这里传给构造器的是原始 `queue_size`，不是 `queue_size + 1`。多出来的空槽由 `CacheBuffer(size)` 构造器内部统一分配：配置为 `N` 时，逻辑上最多保留 `N` 条消息，底层 vector 才有 `N + 1` 个槽位。把两层都加一会误写成逻辑容量 `N + 1`，也不符合固定提交里 `DataVisitor` 与 `CacheBuffer` 的构造约定。

构造完成后，两只缓存分别登记到类型化 Dispatcher。下面是固定提交源码摘录：

```cpp
DataDispatcher<M0>::Instance()->AddBuffer(buffer_m0_);
DataDispatcher<M1>::Instance()->AddBuffer(buffer_m1_);
```

因此任一路 transport 消息都能进入对应缓存。Dispatcher 只保存缓存的 `weak_ptr`，全局注册表不会反向拥有 DataVisitor。组件 task 被删除并释放 visitor 后，弱引用自然失效。

这段结构还有一个容易忽略的结果：同一个 channel 被两个组件订阅时，两个组件各有自己的 ring 和游标。慢组件覆盖自己的历史，不会移动快组件的读取位置。

但“DataVisitor 构造完成后会有 AllLatest”不等于缓存从注册的第一刻起就已经具备融合行为。下面把固定提交中 `DataVisitor<M0,M1>` 构造函数按原顺序摘出；这是上游代码片段，省略了类成员初始化列表和类外模板声明：

```cpp
DataDispatcher<M0>::Instance()->AddBuffer(buffer_m0_);
DataDispatcher<M1>::Instance()->AddBuffer(buffer_m1_);
data_notifier_->AddNotifier(buffer_m0_.channel_id(), notifier_);
data_fusion_ = new fusion::AllLatest<M0, M1>(buffer_m0_, buffer_m1_);
```

也就是说，先把两只缓存交给 Dispatcher，再登记主输入 Notifier，最后才创建 `AllLatest`；而 `AllLatest` 构造函数调用 `SetFusionCallback()` 给 `buffer_m0_` 安装融合函数。此时 Dispatcher 已经可以取得这只缓存。

如果应用在相关 channel 已经持续派发时动态创建多输入组件，就可能出现两类问题。第一类是初始化顺序造成的：Dispatcher 已经能看到 M0 缓存，但 `AllLatest` 尚未给它安装回调；这段时间到达的 M0 只进原始 ring，不会生成融合 tuple。第二类是并发安全：Dispatcher 调用 `Fill()` 时会锁缓存自己的 mutex，但 `SetFusionCallback()` 并不拿这把锁。把两边真正执行的代码并排看，才能判断这把锁到底保护了什么：

```cpp
// 固定提交摘录：Dispatcher 为每个缓存加锁后调用 Fill。
if (auto buffer = buffer_wptr.lock()) {
  std::lock_guard<std::mutex> lock(buffer->Mutex());
  buffer->Fill(msg);
}

// 固定提交摘录：安装函数本身没有取得上面的 mutex。
void SetFusionCallback(const FusionCallback& callback) {
  fusion_callback_ = callback;
}

void Fill(const T& value) {
  if (fusion_callback_) {
    fusion_callback_(value);
  } else {
    if (Full()) {
      buffer_[GetIndex(head_)] = value;
      ++head_;
      ++tail_;
    } else {
      buffer_[GetIndex(tail_ + 1)] = value;
      ++tail_;
    }
  }
}
```

因此，不能因为 `Fill()` 的调用方持有 `buffer->Mutex()`，就说 callback 的读写已经被保护：setter 没有取得同一把锁。若 Dispatcher 正在读取 `fusion_callback_`，同时构造线程写入它，两个线程便会并发访问同一个 `std::function`，至少一次为写入，构成数据竞争。即使两个动作没有重叠，先前到达的 M0 也只写入普通 ring，而 `AllLatest::Fusion()` 读取的是融合 ring；初始化缺口仍然存在。普通场景下组件通常先构造完成再开始接收，所以这一边界不一定会触发；若系统支持运行期热创建，就必须把缓存发布、融合回调安装和首次派发放进同一个同步协议。

因此这类 visitor 需要在消息派发开始前完成构造，才能避开初始化窗口；但固定实现没有把“准备完毕”与“发布给 Dispatcher”做成一次原子操作。通用的修正思路是先在私有对象中建好各缓存和融合策略，完成后再统一注册；若还要支持运行中创建，则注册发布、callback 安装和首条 Dispatch 必须由同一同步协议串起来。仅给 `SetFusionCallback()` 单独加锁还不足以修复过早暴露缓存和首条消息绕过融合的问题。动态注册本身的 vector 并发边界见 [Dispatcher 专题](dispatcher-notifier.md)。

## 主输入决定唤醒边界

多输入 visitor 只把 notifier 注册到主输入 `M0` 的 channel。下面是固定提交源码摘录：

```cpp
DataNotifier::Instance()->AddNotifier(
    buffer_m0_.channel_id(), notifier_);
```

辅助输入 `M1` 到达时，Dispatcher 会更新 `buffer_m1_`，但不会因为这只 visitor 而唤醒组件 task。主输入 `M0` 到达后，执行顺序才是：

```text
M0 Dispatcher
  -> Fill(buffer_m0_)
  -> AllLatest fusion callback
  -> Fill(buffer_fusion_)
  -> DataNotifier::Notify(M0 channel)
  -> Scheduler::NotifyProcessor(task id)
```

把通知绑定在主输入上，使融合组生成与唤醒机会大致随主输入到达，而不是所有输入频率之和。前例中 M0 是 10 Hz，因此不是每条 M1 都重新生成一组参数；这不等于 `Proc()` 必定以 10 Hz 执行。

若主输入选择错误，系统行为会发生根本变化。把 100 Hz 的定位设为 `M0`、10 Hz 的点云设为 `M1`，每次定位到达都可能生成一个新 tuple，其中沿用上一帧点云，增加重复计算并输出大量没有新增点云观测的信息。

这是直接把每路 Reader callback 都接到 `Proc()` 的朴素方案会造成的结果。若 callback 分别运行在接收线程上，它们同时读写 `latest_*` 会形成 data race（数据竞争）：多个线程未同步访问同一内存，且至少一次是写入，程序行为不再有可靠定义。下面是**错误示例（教学代码，不是 Cyber 源码）**：

```cpp
void OnCloud(std::shared_ptr<PointCloud> cloud) {
  latest_cloud_ = cloud;
  if (latest_pose_) {
    Proc(latest_cloud_, latest_pose_);
  }
}

void OnPose(std::shared_ptr<Localization> pose) {
  latest_pose_ = pose;
  if (latest_cloud_) {
    Proc(latest_cloud_, latest_pose_);
  }
}
```

假设两路 callback 都真实执行且已有两路数据，100 Hz 的定位每次到达都会再处理一次最近点云，10 Hz 的点云到达时还会额外处理一次，调用量约为每秒 110 次；大多数调用重复使用同一帧点云。Cyber 的主输入策略选择哪路更新值得生成新组合，并把组合结果先写入 visitor 的有界 ring；它减少的是这种重复触发，不承诺处理器来得及执行每一组。

## AllLatest 在写入主缓存时创建快照

`AllLatest<M0, M1>` 继承 `DataFusion<M0, M1>`，并定义融合结果类型：

下面是固定提交源码中的类型声明：

```cpp
using FusionDataType =
    std::tuple<std::shared_ptr<M0>, std::shared_ptr<M1>>;
```

构造函数把一段 callback 安装到主输入的 `CacheBuffer`。下面是固定提交源码摘录；该闭包在主缓存写入时读取辅助缓存并写融合 ring：

```cpp
buffer_m0_.Buffer()->SetFusionCallback(
    [this](const std::shared_ptr<M0>& m0) {
      std::shared_ptr<M1> m1;
      if (!buffer_m1_.Latest(m1)) {
        return;
      }

      auto data = std::make_shared<FusionDataType>(m0, m1);
      std::lock_guard<std::mutex> lock(buffer_fusion_.Buffer()->Mutex());
      buffer_fusion_.Buffer()->Fill(data);
    });
```

这段代码不是在组件 task 被调度后才临时读取 `M1`，而是在 `M0` 被 Dispatcher 写入并调用融合 callback 时取得 `M1` 的最新值，再把二者固化成 tuple。这里的采样时点是 `Latest()` 持有辅助缓存 mutex 并复制 `Back()` 指针的那一刻，不是传感器曝光时间，也不一定等于 M0 消息最初到达本机的时刻。

两种时机会产生不同语义：

```text
t0: M0#10 到达
t1: M1#51 到达
t2: task 获得 CPU
```

若 M0#10 的派发先进入回调、随后才有 M1#51 写入，Cyber 保存的是 `tuple(M0#10, M1#50)`；若 M1#51 已先完成对辅助缓存的写入，tuple 就会引用 M1#51。对二输入组件而言，`Latest()` 的 mutex 决定这两个并发操作的先后；等待 Processor 获得 CPU 不会再改变已保存的指针。这里的“快照”只表示参数指针一经写入 tuple 就固定，并不表示消息体被深复制，也不意味着按传感器时间戳对齐。

三、四输入版本还有一层边界：`AllLatest` 按 M1、M2、M3 的顺序分别调用 `Latest()`，每次调用各自获取和释放一只缓存 mutex，并没有同时锁住所有 channel。因此若辅助输入在这些读取之间更新，最终 tuple 可能由不同读取时刻的“最新值”组成；不能把它描述为多个 channel 的原子一致快照。严格的跨流一致性需要统一时间戳/水位线策略或一个协调锁，而不是把 `AllLatest` 的指针 tuple 当作同步结果。

## SetFusionCallback 构成缓存层扩展点

`CacheBuffer` 不只保存槽位，还允许设置一只 fusion callback。主消息写入 ring 时，缓存层在同一条写入路径中调用它。

这相当于一个受限的 Observer/Hook 模式：

- `CacheBuffer` 不知道 tuple 的具体类型；
- `AllLatest` 通过闭包捕获其他 ChannelBuffer；
- Dispatcher 仍只调用统一的 `Fill(msg)`；
- 融合策略可以在不修改 transport 的前提下派生数据。

它避免了 Dispatcher 特判“这只 buffer 属于多输入组件”，保持了数据分发层的单一职责。但 callback 执行在 Dispatcher 热路径中，因此不能包含昂贵计算、阻塞 I/O 或不可控等待。

还有一个比“锁里做了什么”更隐蔽的边界：这个闭包捕获的是 `AllLatest` 的裸 `this`，而它被保存到了 `CacheBuffer` 中。必须分别追踪闭包的寿命、`AllLatest` 的寿命和缓存的寿命，不能把它们当成同一个对象。

固定提交里的二输入 `DataVisitor` 用一个裸指针保存融合策略，并在析构函数体内手动删除它。下面是**固定提交源码摘录**，省略成员之外的 `TryFetch()`：

```cpp
~DataVisitor() {
  if (data_fusion_) {
    delete data_fusion_;
    data_fusion_ = nullptr;
  }
}

private:
  fusion::DataFusion<M0, M1>* data_fusion_ = nullptr;
  ChannelBuffer<M0> buffer_m0_;
  ChannelBuffer<M1> buffer_m1_;
```

另一方面，`AllLatest` 构造时把 `[this]` 闭包写入主缓存的 `fusion_callback_`；`DataDispatcher::Dispatch()` 通过 `weak_ptr::lock()` 取得缓存临时强引用后，才持有缓存 mutex 并调用 `Fill()`。因此这条所有权关系是“缓存可以独立于 visitor 暂时存活”，并不是“缓存会拥有闭包捕获的 AllLatest”。

如果关闭路径没有先让在途 Dispatch 全部结束，可能出现这样的交错：

```text
transport thread                         shutdown thread
--------------                           --------------
weak buffer.lock() 成功
等待或持有 CacheBuffer mutex
                                         RemoveTask 返回
                                         ~DataVisitor 删除 data_fusion_
取得 mutex，调用 Fill()
fusion_callback_ 解引用已销毁的 AllLatest
```

这不是说每次关闭都会崩溃；它指出的是固定对象关系本身没有建立所需的寿命保证。`shared_ptr<CacheBuffer>` 只能让缓存活到本次 Dispatch 返回，不能让被 `[this]` 捕获的 `AllLatest` 一起存活。安全关闭需要能证明 Reader/transport 已停止接收新分发、所有已进入的 Dispatch 已退场，然后才清除缓存中的 fusion callback 并销毁 AllLatest。单独给 `SetFusionCallback()` 加锁只能避免 callback 赋值与读取并发，不能独自证明析构期间没有正在执行的闭包。另一个可行方向是让闭包捕获一个独立、可弱锁定的融合状态对象，并定义状态已销毁时的返回行为；该状态不能反向强持有安装它的主 CacheBuffer，否则会形成引用环。

这也解释了为何“task 已停止”与“传输回调已静止”是两种关闭屏障：`RemoveTask()` 等待的是 Processor 上的 CRoutine，不自动等待 transport 线程正在执行的 `DataDispatcher::Dispatch()`。完整关闭时序应把这两类在途工作分别收束。

## Fusion 接口只负责读取已固化的参数组

组件协程真正运行时，`DataVisitor::TryFetch()` 调用 `AllLatest::Fusion()`。下面是固定提交源码摘录：

```cpp
bool Fusion(uint64_t* index,
            std::shared_ptr<M0>& m0,
            std::shared_ptr<M1>& m1) override {
  std::shared_ptr<FusionDataType> fusion_data;
  if (!buffer_fusion_.Fetch(index, fusion_data)) {
    return false;
  }
  m0 = std::get<0>(*fusion_data);
  m1 = std::get<1>(*fusion_data);
  return true;
}
```

`Fusion()` 这个名称容易让人误以为此处执行算法融合。实际上，配对工作早已在主输入写入时完成；这里仅按 visitor 游标从融合 ring 取出 tuple，再把 tuple 元素复制到输出 `shared_ptr`。

因此完整链条分成两段：

```text
transport thread:
  M0 Fill -> read latest M1 -> allocate tuple -> fusion ring Fill -> notify

processor thread:
  fusion ring Fetch -> unpack tuple -> Component::Proc(m0, m1)
```

这种分段让配对时刻稳定，同时把业务计算留在 Processor 线程。但 tuple 分配和辅助缓存加锁仍发生在 transport 接收线程上。

## shared_ptr tuple 的所有权含义

`FusionDataType` 保存的是消息智能指针，而不是消息副本。下面是固定提交中的类型形状简写，定义位置见 `AllLatest` 的 `FusionDataType`：

```cpp
tuple<shared_ptr<M0>, shared_ptr<M1>>
```

创建 tuple 时通常发生：

- 一次 tuple 对象的堆分配；
- 两次 `shared_ptr` 引用计数增加；
- 向 fusion ring 写入 tuple 指针时再次调整引用计数；
- Fetch 和解包阶段继续复制若干 `shared_ptr`。

大体积 PointCloud 或图像本体没有因此被复制。真正的成本集中在小对象分配、原子引用计数和缓存行竞争上。

若主输入频率为 `F0`，每条主消息成功配对一次，则每秒至少产生约 `F0` 个 tuple allocation。对于 10 Hz 感知链通常可接受；对于几十万次每秒的小消息路径，通用堆分配会成为明显成本。

可选优化包括对象池、内联 tuple ring、侵入式引用计数或单生产者场景下的移动所有权。不过这些优化都会增加生命周期证明难度，不应在尚未测得瓶颈时替换清晰的 `shared_ptr` 语义。

## 锁的获取顺序与临界区范围

主输入到达时，调用链会涉及三类缓存：

1. Dispatcher 正在写入 `buffer_m0_`；
2. fusion callback 调用 `buffer_m1_.Latest()`，获取辅助缓存 mutex；
3. callback 再获取 `buffer_fusion_` mutex 并写入 tuple。

概念上的锁序是：

```text
M0 mutex -> M1 mutex -> fusion mutex
```

三、四输入版本会顺序读取更多辅助缓存。因为 fusion callback 只安装在 `M0`，辅助输入写入路径不会反过来获取 `M0` mutex，当前结构避免了明显的 AB-BA 环。但如果扩展代码在辅助缓存 callback 中再读取主缓存，就可能构造反向锁序并引入死锁。

设计新的融合策略时，应把以下规则视为接口契约：

- 所有缓存锁按固定输入序号获取；
- 持锁期间只复制智能指针和写入小型元数据；
- 不调用业务代码；
- 不在锁内等待条件变量或执行 I/O；
- 若要做复杂匹配，先复制候选指针，再在独立结构中计算。

## AllLatest 的时间语义边界

考虑消息序列：

```text
M1 pose#100 @ 10.00 s
M1 pose#101 @ 10.01 s
M0 cloud#20 @ 10.02 s  -> (cloud#20, pose#101)
M0 cloud#21 @ 10.12 s  -> (cloud#21, pose#101), 若期间没有新 pose
```

第二组参数仍然合法，因为 AllLatest 只要求辅助输入存在，不检查它是否过期。框架也不比较 header timestamp、sequence number 或 clock domain。

因此业务层必须自行决定是否接受这组参数。下面是教学检查示例，不是 Cyber 上游代码；实际 accessor 名称要按业务消息定义调整：

```cpp
const auto age = cloud->measurement_time() - pose->measurement_time();
if (std::abs(age) > max_pose_age) {
  return false;
}
```

需要严格同步的传感器算法通常应使用时间窗口：为各输入维护按时间排序的 deque，以主时间戳搜索最近邻或插值点，并设置最大偏差、水位线和超时规则。把 AllLatest 直接称为“同步器”会掩盖这些必要约束。

## 初始缺数与连续覆盖

辅助输入从未到达时，`Latest()` 返回 false，fusion callback 不向融合 ring 写入数据。这里有一个容易被“主输入先入缓存”的直觉掩盖的细节：**该 DataVisitor 的原始 M0 ring 也不会保存这条主消息**。原因不在调度器，而在 `CacheBuffer::Fill()` 的互斥分支。

下面是**固定提交的 `CacheBuffer::Fill()` 源码**，它把普通缓存写入与多输入融合写入明确分开：

```cpp
void Fill(const T& value) {
  if (fusion_callback_) {
    fusion_callback_(value);
  } else {
    if (Full()) {
      buffer_[GetIndex(head_)] = value;
      ++head_;
      ++tail_;
    } else {
      buffer_[GetIndex(tail_ + 1)] = value;
      ++tail_;
    }
  }
}
```

`AllLatest` 构造时给主输入缓存安装融合回调。因此 M0 到达后，写入线程在主缓存的 mutex 内调用这个回调；若辅助输入尚不存在，回调立即返回，既不会推进 M0 的 `tail_`，也不会往融合 ring 存入 tuple。虽然 `DataDispatcher::Dispatch()` 随后仍会通知主 channel，Processor 即使醒来，`TryFetch()` 也没有可取的参数组。稍后 M1 第一次到达，只会更新辅助缓存，不会追溯重建此前的主消息，也不会单独唤醒这个以 M0 为触发源的组件。**要生成第一组参数，必须等到下一次 M0 到来。**

例如相机第 10 帧先到、定位稍后才初始化，`cloud#10` 对这个 DataVisitor 就已经失去生成融合组的机会；定位就绪后 `cloud#11` 到来，才可能得到 `(cloud#11, pose#1)`。原始主 channel 收包数、成功生成的 tuple 数和最终 `Proc()` 次数是三个不同的指标，不应该合成一个“丢帧率”。

这带来两个行为：

- 系统启动初期，组件可能收到多次主输入通知，却暂时没有可处理的 tuple；
- 辅助输入后来第一次到达时不会追溯重建此前丢失的主输入组合。

融合 ring 仍是有界环形缓存。如果 Processor 长时间得不到调度，旧 tuple 会被覆盖；读取游标落后于 head 时，`ChannelBuffer::Fetch()` 会跳到当前 tail。此时丢失的是已经固化的整组参数，而不是单独某一路消息。

于是系统存在两种不同的丢弃：

1. 缺少任一辅助输入时，主消息根本不生成 tuple；
2. tuple 已生成但消费者落后时，fusion ring 覆盖旧 tuple。

日志与指标若只统计原始 channel 丢包，就无法完整反映组件实际漏处理了多少组输入。

## 复杂度与容量模型

设组件有 `K` 路输入，主输入频率为 `F0`，每路原始缓存对外容量为 `Qi`，融合缓存容量为 `Qf`。

每条辅助输入到达时，当前组件的附加成本主要是一次 Dispatcher fan-out 和一次 ring 写入，可近似记为 `O(1)`。

每条主输入到达时，融合 callback 要读取 `K-1` 个 Latest，并创建一只含 K 个智能指针的 tuple，因此为 `O(K)`。由于 Cyber RT 把 K 限制为 4，这个上界很小而且固定。

单个 visitor 的指针槽位容量可以按构造出的物理缓存分账。辅助输入原始 ring 与融合 ring 保存有效消息；M0 缓存在安装融合回调以后通常不保存原始主消息，但固定提交仍为它分配 `Q0+1` 个槽位。若估算**预分配槽位**而非实际仍然持有的对象数，可近似写成：

```text
Mpointer ~= sizeof(shared_ptr) * (Q0 + Q1 + ... + Q(K-1))
         + sizeof(shared_ptr<FusionTuple>) * Qf
```

这还不包括 tuple 对象、控制块和消息本体。主输入对象可以被其他单输入 visitor 的原始 ring 保存，也可以被多个融合 tuple 引用；本 visitor 的原始 M0 ring 在正常融合路径不额外保存它。峰值对象寿命取决于所有实际引用者，而不只取决于这个组件的 queue size。

当一个高频 channel 被 `B` 个 DataVisitor 订阅时，Dispatcher 热路径为 `O(B)`；若其中多个 visitor 又把该 channel 作为主输入，每个 visitor 都会独立执行 Latest 查询和 tuple 分配。共享 transport receiver 减少了接收端数量，却没有消除消费者级缓存与融合成本。

## 设计优势

AllLatest 的价值主要体现在结构可预测：

- 触发源明确，融合组生成与唤醒机会由主输入控制；
- 配对时刻固定在主输入到达点，不受调度排队时间影响；
- 业务 callback 获得类型正确的参数，不需要运行时 downcast；
- 大消息不复制，只延长引用生命周期；
- 每个消费者拥有独立缓存，背压和覆盖互不干扰；
- transport、缓存、融合和执行仍保持分层。

对于状态估计、车辆状态、地图快照等“辅助量更新更快且只关心最新值”的场景，这是一种简单而实用的工程折中。这里的频率结论只描述 M0 到达时生成融合组、并产生唤醒机会；`Proc()` 的实际调用率由辅助数据可用性、ring 覆盖和 Processor 处理能力共同决定，不是 M0 频率保证。

## 设计限制

它的限制也应当在 API 层被明确表达：

- 没有时间戳对齐、插值、最大数据年龄或乱序处理；
- 只有主输入触发，辅助输入更新不会立即重算；
- 固定一至四输入，扩展输入数量需要修改多组模板；
- 主输入接收线程承担 tuple 分配和多把缓存锁；
- `shared_ptr` 解决对象寿命，不解决消息内容的逻辑一致性；
- queue size 控制空间上限，却不能保证实时 deadline；
- 慢消费者会跳过已覆盖 tuple，框架不重放中间组合。

这些不是实现疏漏，而是 AllLatest 选择的语义边界。需要 event-time join、确定性重放或严格多传感器同步时，应替换融合策略，而不是继续堆叠条件到 `Proc()` 入口。

## 可复刻的最小实现

实现一个同类机制时，可以先把语义压缩成三个类型。下面是二输入情形的教学接口草图，不是可直接编译的完整实现；辅助输入仍使用具体 `Side` 类型，不把消息退化成 `void*`：

```cpp
template<class T>
class LatestSlot {
 public:
  void Put(std::shared_ptr<T> value);
  std::shared_ptr<T> Get() const;
};

template<class Main, class Side>
class AllLatestJoin {
 public:
  // tuple 是固定位置、可分别保存不同 C++ 类型值的对象组。
  using Output = std::tuple<std::shared_ptr<Main>, std::shared_ptr<Side>>;

  void OnMain(std::shared_ptr<Main> main);
  void OnSide(std::shared_ptr<Side> side);
  bool TryFetch(std::shared_ptr<Output>& output);
};
```

第一步只实现单线程版本，验证主输入触发、缺数跳过和快照时刻。第二步为每只 LatestSlot 加锁，并固定多锁顺序。第三步加入有界 output ring 和私有消费游标。最后才把 output-ready 事件连接到 scheduler。

测试不应只覆盖“两个输入都有值”的顺利路径，还应覆盖：辅助输入从未到达、辅助数据陈旧、主输入连续覆盖、消费者落后、关闭过程中仍有 dispatch，以及两个 visitor 以相反主输入订阅同一组 channel。

## 从多输入组合继续进入调度链

到这里，`Proc(m0, m1)` 的参数来源已经完整：辅助输入由 Dispatcher 写入 visitor 私有缓存；M0 到达后经已安装的融合回调读取辅助输入最新指针，完整 tuple 才进入融合 ring。随后主 channel 的通知让 task 有机会重新检查数据；Processor 最后从融合 ring 取出这组已固化的参数。如果 M1 尚未就绪，M0 不会保存在该 visitor 的普通主 ring，任务即使被通知也没有可读 tuple。

下一层不再处理消息配对，而是处理执行权：同一时刻多只 tuple 等待处理时，Scheduler 如何选择 CRoutine，优先级为何不等于抢占，以及一个耗时 `Proc()` 如何影响同 processor 上的其他组件。
