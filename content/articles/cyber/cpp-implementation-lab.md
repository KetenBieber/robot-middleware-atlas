# Cyber RT C++ 连续实现：从消息入口到 Component::Proc

本章的 Apollo 真实源码统一固定到 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`；真实摘录直接贴出，教学代码会另行标记。

如果从零实现一个机器人进程内消息运行时，第一版往往只有 `callback(message)`；下一步却马上碰到数据积压、慢回调阻塞接收线程、消息类型边界和关闭时的悬空访问。要逐步解决这些问题，可以把运行时拆成有界缓存、Dispatcher、Notifier、工作线程和业务组件，再决定哪些接口值得发展成 CRoutine 与 Scheduler。本章把这些零件组成一个自行实现的最小实验，保留最重要的因果链：消息到达让数据就绪，事件唤醒处理任务，工作线程最后进入组件业务逻辑。

先说明代码来源：本章**明确标注“固定提交源码摘录”的代码才是 Apollo 源码**；前六层的 C++ 片段是逐层推导的教学接口，不能直接拼成单个翻译单元。文末附录则给出另一份独立、完整的 `mini_node.cc`，可用于验证 Node、Reader 与 Writer 的基本生命周期；它也不是上游源码。除特别注明外，教学接口按 C++17 编写（如 `std::shared_mutex` 和类模板实参推导）；工作循环使用 C++17 可用的原子停止标志，不依赖 C++20 才提供的线程停止令牌 API。每层用源码摘录核验模型与 Cyber 的差异。

## 先确定最小功能与非目标

最小系统需要支持：按 channel 注册 Reader；把消息投递到有界缓存；在新消息到达时唤醒对应处理器；由工作线程执行 `Proc()`；关闭后不再进入业务代码。

它暂不包含跨进程传输、服务发现、插件动态加载和多输入融合。先把进程内数据路径做对，再替换 transport 或增加 DAG 装载；否则网络、反射和调度问题会混在一起，难以定位。

## 第一层：类型化消息与类型擦除边界

业务侧希望保留静态类型：

```cpp
template<class Message>
class Component {
 public:
  virtual ~Component() = default;
  virtual bool Proc(std::shared_ptr<const Message> message) = 0;
};
```

`shared_ptr<const Message>` 是共享所有权的句柄：复制句柄会让消息对象在所有使用者释放前继续存活；`const Message` 则表示经由这个句柄不能修改消息。它不阻止发布者还持有的另一个可写句柄修改同一对象，所以运行时约定应是发布后不再改写 payload。若业务必须修改，应创建新消息，避免多个消费者观察到先后不一致的写入。

在固定提交中，业务组件确实以 `Component<M0, ...>` 保留编译期消息类型；插件宿主却通过非模板 `ComponentBase` 持有异构组件。可对照 `Component` 的类型声明与 `Process()` 和 `Component<M0>::Initialize()`：本例用 `type_index + void` 表达类型擦除边界，Apollo 则主要通过模板 Reader/Dispatcher 与 `RoutineFactory` 把类型信息带到调度入口，并不是直接照搬本例的 `ErasedListener` 结构。

Dispatcher 又要在一个容器里管理不同消息类型，因此在注册边界使用类型擦除：把不同具体类型的监听器包装成同一种“接收消息并可调用”的外部接口，同时保留一个运行时类型标签，投递前再校验并恢复类型。`std::type_index` 是可比较的运行时类型标签；`std::function<void(...)>` 则能把 lambda 等可调用对象装进统一的函数签名。

```cpp
using ChannelId = std::uint64_t;

struct ErasedListener {
  std::type_index type;
  std::function<void(std::shared_ptr<const void>)> deliver;
};
```

`void` 指针本身不知道真实类型，所以必须同时保存 `type_index` 并在投递前校验。类型擦除应集中在边界；进入组件后立即恢复模板类型，不能让 `void*` 贯穿系统。

## 第二层：Reader 将 transport 与消费缓存分开

缓存首先需要并发保护。互斥锁（mutex）一次只允许一个线程进入受保护区域，保护的不只是队列字段本身，也包括“检查是否满—淘汰—插入”这组必须作为整体成立的操作；`std::lock_guard` 是作用域锁，构造时加锁、离开作用域时自动解锁。若不加锁，两个 `Push()` 可能同时判断队列未满，再同时插入，使实际容量越界。

```cpp
template<class T>
class ReaderBuffer {
 public:
  explicit ReaderBuffer(std::size_t capacity) : capacity_(capacity) {
    if (capacity_ == 0) {
      // 真实程序需包含 <stdexcept>。
      throw std::invalid_argument("ReaderBuffer capacity must be positive");
    }
  }

  void Push(std::shared_ptr<const T> value) {
    std::lock_guard lock(mu_);
    if (queue_.size() >= capacity_) queue_.pop_front();
    queue_.push_back(std::move(value));
  }

  std::shared_ptr<const T> TakeLatest() {
    std::lock_guard lock(mu_);
    if (queue_.empty()) return {};
    auto result = std::move(queue_.back());
    queue_.clear();
    return result;
  }

 private:
  const std::size_t capacity_;
  std::mutex mu_;
  std::deque<std::shared_ptr<const T>> queue_;
};
```

这里选择“满时丢最旧、消费时取最新”，适合只关心最新状态的感知结果，不适合每条命令都必须执行的控制队列。容量与淘汰策略必须由 channel 配置决定，不能被容器实现偷偷决定。构造函数拒绝容量 0 是必要边界：若直接接受 0，空队列满足 `size() == capacity`，第一次 `Push()` 就会对空 deque 执行 `pop_front()`。配置错误因此会从启动期检查变成未定义行为。

`deque` 的两端操作为摊销 `O(1)`，但每个节点可能带来分配与较差的缓存局部性。固定容量 ring buffer 能避免稳态分配，更接近实时数据路径；教学版本先保留清晰的所有权。

这只 `deque` 是为了让淘汰语义容易读懂而选的教学容器，并非 Cyber 的物理布局。固定提交由 `CacheBuffer` 管理带逻辑序号的固定槽位，`ChannelBuffer` 再把它装成消息指针缓存；`DataVisitor::TryFetch()` 按消费者自己的游标取消息。这里的 `TakeLatest()` 会把其余 pending 消息一起清掉，而 Cyber 的真实 visitor/ring 还有 backlog 与追赶边界，不能把这段教学策略当成 Apollo 的逐行等价物。

## 第三层：Notifier 只传递“可能有工作”

消息本体已经在 ReaderBuffer 中，唤醒通道不应再复制 payload：

这里第一次需要条件变量（condition variable）：它让没有工作时的线程睡眠，而不是持续轮询占满一个 CPU 核。线程等待时会释放互斥锁，满足谓词后再持锁返回；等待必须用谓词重检，因为通知可能发生在真正睡眠前，也可能出现虚假唤醒。单独改一个普通布尔值后调用 `notify_one()` 不能构成可靠协议，状态检查与睡眠之间会有丢失通知窗口。

```cpp
#include <condition_variable>
#include <mutex>

// 教学最小例子：一个 worker 消费一个可合并的“有工作”状态。
class CoalescingSignal {
 public:
  void Notify() {
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (stopping_) return;
      ready_ = true;  // 谓词状态必须与 Wait 使用同一把 mutex
    }
    cv_.notify_one();
  }

  bool Wait() {
    std::unique_lock<std::mutex> lock(mu_);
    cv_.wait(lock, [this] { return stopping_ || ready_; });
    if (stopping_) return false;  // 本例的关闭策略：丢弃尚未取出的工作
    ready_ = false;               // 只消费“至少有一次更新”这个闩锁
    return true;
  }

  void Stop() {
    {
      std::lock_guard<std::mutex> lock(mu_);
      stopping_ = true;
    }
    cv_.notify_all();
  }

 private:
  std::mutex mu_;
  std::condition_variable cv_;
  bool ready_ = false;
  bool stopping_ = false;
};
```

这里故意不用 atomic：`ready_` 和 `stopping_` 的每次读写都在 `mu_` 保护下，普通布尔值已经足够。`Wait()` 检查谓词时持锁；若条件为假，`condition_variable::wait` 会在进入睡眠的原子等待操作中释放这把锁。producer 必须等到锁释放后才能置位并通知，因此只有两种结果：要么 worker 之后检查时看见 `ready_ == true`，要么它已经登记等待并会收到通知。反过来，若只用 atomic 写 `ready_`、却不与等待 mutex 建立这个协议，就可能发生“worker 检查 false → producer 置 true 并通知 → worker 才睡下”的丢唤醒时序。

通知是边沿提示，不承诺一条通知对应一条消息。多次到达可以合并成一次唤醒，worker 醒来后从缓存读取实际状态。这个 `CoalescingSignal` 只展示单个消费循环的就绪闩锁，不保证多个 worker 不会同时执行同一个业务 task；要扩展为多 worker，必须再用任务状态/执行权约束重入。若业务要求逐条处理，缓存应保存消息序列并循环取空，而不是把 `ready_` 改成消息计数器。

真实链路把同一个问题拆成 task 与 worker 两层。先看 `DataNotifier::Notify()` 的**固定提交源码摘录**：

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

它只按 channel 找到并调用已注册回调，不负责线程切换，也不执行 `Proc()`。Scheduler 收到 task 级通知后才通知对应 ProcessorContext；`ClassicContext::Wait/Notify()` 用组级计数和条件变量等待/唤醒 OS worker，而 CRoutine 的 `updated_` 另行保存 task 级更新。因而这里的一个布尔闩锁只是把两层机制压成教学模型，不能说明 Apollo 只有一个 `ready_`。

下面是 `ClassicContext::Wait()` 与 `Notify()` 的**固定提交源码摘录**：

```cpp
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

`wait_for` 带 1 秒超时并用谓词重检；谓词为假时条件变量会暂时释放互斥锁，线程被通知、超时或虚假唤醒后重新持锁检查。注意源码的 `Wait()` 与按 group 的 `Notify()` 呈现了不同名字的锁/条件变量容器，阅读时应沿 `mtx_wrapper_`、`mtx_wq_` 和 `cv_wq_` 的初始化映射确认组关系，不要把教学单锁例子当成它们完全同构的实现。

## 第四层：Dispatcher 在锁外执行 Listener

```cpp
template<class T>
void Dispatcher::Dispatch(ChannelId id, std::shared_ptr<const T> msg) {
  std::vector<ErasedListener> targets;
  {
    std::shared_lock lock(mu_);
    auto it = listeners_.find(id);
    if (it != listeners_.end()) targets = it->second;
  }

  for (auto& target : targets) {
    if (target.type == typeid(T)) target.deliver(msg);
  }
}
```

共享锁只保护 listener 表的结构，不包住回调。复制快照会复制 `std::function`，高频路径可把 listener 存为 `shared_ptr<const Listener>`，快照只复制指针。对应代价是原子引用计数。

ChannelId 可由字符串哈希得到，但哈希碰撞必须处理。只用 64 位 ID 而不保留原始 channel/type 信息，会把极低概率碰撞变成错误投递；安全实现应在注册时检测冲突。

本例用 `shared_mutex + vector<ErasedListener>` 快照，是为了显式展示“复制目标后在锁外调用 callback”。固定提交的 `DataDispatcher<T>::AddBuffer/Dispatch()` 使用的是按消息类型实例化的 singleton 和 atomic hash map，并对相应的 weak buffer 分发；真实实现的同步粒度、注册形式与这里不同，读者应以该文件的具体代码为准。

## 第五层：把类型恢复点连接到组件

```cpp
template<class T>
struct RuntimeBinding {
  std::shared_ptr<ReaderBuffer<T>> buffer;
  std::shared_ptr<Processor> processor;
  std::shared_ptr<Component<T>> component;

  ErasedListener Listener() {
    std::weak_ptr<ReaderBuffer<T>> weak_buffer = buffer;
    std::weak_ptr<Processor> weak_processor = processor;
    return {
      typeid(T),
      [weak_buffer, weak_processor](std::shared_ptr<const void> raw) {
        auto buffer = weak_buffer.lock();
        auto processor = weak_processor.lock();
        if (!buffer || !processor) return;
        buffer->Push(std::static_pointer_cast<const T>(raw));
        processor->Notify();
      }
    };
  }
};
```

lambda 捕获弱引用，避免 Dispatcher 的 listener 表反向拥有整个组件运行时而形成所有权环。类型检查已经在 Dispatcher 完成，所以这里使用 `static_pointer_cast`；若调用链不能保证检查，应改用更强的封装，而不是盲目 cast。

固定提交中的真实装配点是 `Component<M0>::Initialize()`：它用 `shared_from_this()` 派生 `weak_ptr` 捕获业务组件，创建 Reader 与 DataVisitor，再通过 RoutineFactory 把输入读取接到 scheduler task。Reader 的 `Enqueue()` 由接收/分发侧写入 blocker；`Reader<MessageT>::Enqueue()` 展示了该边界。本例 `RuntimeBinding` 把 buffer、worker 和 Component 聚合在一个结构体里，真实 Cyber 并没有这个同名聚合对象。

这个边界的真实实现只有几行，却很重要。以下是 `Reader<MessageT>::Enqueue()` 的**固定提交源码摘录**：

```cpp
template <typename MessageT>
void Reader<MessageT>::Enqueue(const std::shared_ptr<MessageT>& msg) {
  second_to_lastest_recv_time_sec_ = latest_recv_time_sec_;
  latest_recv_time_sec_ = Time::Now().ToSecond();
  blocker_->Publish(msg);
}
```

`Enqueue()` 更新时间戳并把消息发布到 Reader 的 blocker；它本身没有直接调用 `Proc()`，也没有在这里唤醒 OS 线程。后续 blocker/visitor/notifier 与 ProcessorContext 才分别承担缓存变化、task 状态通知和 worker 等待解除，这些边界可与上文的 DataNotifier 和 ClassicContext 源码并读。

## 第六层：工作循环承担 CRoutine 的最小职责

停止标志使用 `std::atomic<bool>`：原子变量让不同线程并发读写同一标志不构成数据竞争；普通 `bool` 即使看起来只是一个字节，也不能在无同步下被一边写一边读。这里没有显式指定 memory order，因此采用默认的顺序一致语义，足以传递“请求停止”这个简单状态。原子标志只改变可见状态，不会自动把阻塞在条件变量上的 worker 叫醒，关闭流程还必须显式通知。

```cpp
template<class T>
// 本片段需要 <atomic>；停止方必须设置 stop_requested 并唤醒 Wait()。
void RunBinding(RuntimeBinding<T>& binding,
                const std::atomic<bool>& stop_requested) {
  while (!stop_requested.load()) {
    // Wait() 必须由 owner 的停止路径配合 Notify() 唤醒，不能只改原子标志。
    if (!binding.processor->Wait()) break;
    if (stop_requested.load()) break;
    auto message = binding.buffer->TakeLatest();
    if (!message) continue;
    try {
      binding.component->Proc(std::move(message));
    } catch (...) {
      // 记录故障并交给运行时策略决定重启、隔离或停止。
    }
  }
}
```

完整 Cyber RT 使用协程和调度策略把大量逻辑任务映射到有限工作线程；这个版本用一个可停止工作循环保留相同语义。以后可把 `Wait()` 改为向 ready queue 放入 routine ID，再由线程池挑选任务，而 Reader、Dispatcher 和 Component 接口无需重写。

异常不能穿过线程入口，否则会调用 `std::terminate`。捕获后也不能简单忽略：系统应记录组件名、channel、消息序号和故障策略。对安全相关控制模块，继续运行可能比停止更危险。

固定源码把这里的普通 `while` 循环分成 `RoutineFactory` 与 `Processor::Run()`：前者在 routine 内循环 `TryFetch()`、调用业务函数并 `Yield()`，后者作为 OS 线程从 context 取 CRoutine、`Resume()`，无任务时等待。本例每个 binding 自己有一个 worker，Cyber 则把多只逻辑 routine 复用到有限 Processor 线程上；两者的资源规模并不相同。

## 关闭协议

这里的 `std::atomic<bool>` 让停止线程与 worker 并发读写同一布尔值时不构成普通数据竞争；没有指定内存序时采用默认的顺序一致语义，强于本例只传递停止位所需。这个标志只表示“请求停止”，不会自动从条件变量等待中唤醒线程。

关闭次序为：先阻止 transport 产生新消息；从 Dispatcher 注销 listener；用 `stop_requested.store(true)` 发布停止请求，并显式通知正在 `Wait()` 的 worker；等待线程退出；最后释放 Component 和 Buffer。原子标志只让并发读写这个标志本身有定义，不会自动唤醒条件变量，因此 `Notify()` 是协议的一部分。若先析构 Component，队列中已经就绪的任务仍可能调用悬空对象。

注销返回是否意味着“不会再被调用”取决于快照语义。若 Dispatcher 已复制 listener，注销只能阻止后续快照。严格屏障需要统计在途分发并等待归零。

这个教学关闭协议比“析构时顺便停线程”更明确，但仍要由外层 owner 实际执行 `Stop()` 并 `join()`，而且 transport 与已复制 listener 都必须先静止。固定 Apollo 的 `ComponentBase::Shutdown()` 先调用派生 `Clear()`，之后才关 Readers 并 `RemoveTask()`；`SchedulerClassic::RemoveCRoutine()` 能等待 routine 不再执行，却不能保护已经在前面的 `Clear()` 释放的业务资源。本文的“先停输入、排空回调、再释放对象”是推荐最小协议，不是 Apollo 当前关闭顺序的原样描述。

## 从最小版扩展到 Cyber RT

```text
单输入 buffer
  -> DataVisitor 管理一个或多个 ChannelBuffer
  -> DataDispatcher 按 channel/type 连接 listener
  -> Notifier 唤醒对应 DataVisitor
  -> Processor 把 visitor 封装为可调度任务
  -> CRoutine 保存协程上下文
  -> Scheduler 依据组、优先级和亲和性选择工作线程
  -> Component::Proc 执行业务逻辑
```

扩展多输入时，不能只分别取“最新值”。必须定义触发输入、缓存深度、匹配规则和时间戳语义。扩展调度器时，不能只增加线程；还要决定任务是否可抢占、同一组件能否并发执行、队列满时如何退化。

## 实现完成标准

最小系统应验证：错误消息类型不会进入组件；慢组件不会持有 Dispatcher 注册表锁；缓存容量有上界；连续通知不会永久丢失就绪状态；关闭后不再进入 `Proc()`；组件异常不会杀死整个工作进程；替换线程循环为 ready queue 调度器时数据层接口保持稳定。这条链路成立后，才具备继续复刻 Cyber RT 调度与多输入语义的基础。

## 附录：可运行的 Node、Reader 与 Writer 最小版

这是与前面运行时分层实验相互独立的 **C++17 教学实现**，不是 Apollo 源码。它专门验证 [Node、Reader 和 Writer 的对象边界](node-reader-writer.md)：类型安全、异构所有权、工作线程与关闭次序。与上面的拆分式教学片段不同，下列 `mini_node.cc` 按单文件组织，可用文中给出的命令编译。

下面的**教学最小例子**不是 Apollo 源码。它只复刻本文最关键的边界：模板保持消息类型，非模板 base 允许 Node 异构持有 Reader，Node 不拥有 Writer，Reader callback 在 worker 线程执行，关闭先停止 worker 再释放对象。为了让一份文件可以直接运行，它没有实现 service discovery、共享 Receiver、ring 覆盖和多 transport。代码里的 `condition_variable` 让 worker 在队列为空时睡眠、由发布线程通知后重查谓词；它必须与保护队列的 mutex 配合。`join()` 让关闭线程等待 worker 的函数真正退出后再继续销毁依赖对象。`weak_ptr<void>` 是仅用于检测 owner 是否仍存活的弱句柄，既不延长 Reader 寿命，也不携带 Reader 的静态类型；`shared_from_this()` 只能在对象已由 `shared_ptr` 管理后调用。

保存为 `mini_node.cc`，在 Linux 上执行：

```bash
c++ -std=c++17 -O2 -pthread mini_node.cc && ./a.out
```

```cpp
#include <condition_variable>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

class ReaderBase {
 public:
  virtual ~ReaderBase() = default;
  virtual void Stop() = 0;
};

template <class T>
class Channel {
 public:
  using Callback = std::function<void(std::shared_ptr<const T>)>;

  void Subscribe(std::weak_ptr<void> lifetime, Callback callback) {
    [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
    subscribers_.push_back({std::move(lifetime), std::move(callback)});
  }

  void Publish(std::shared_ptr<const T> message) {
    std::vector<Callback> callbacks;
    {
      [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
      for (auto it = subscribers_.begin(); it != subscribers_.end();) {
        if (it->lifetime.expired()) {
          it = subscribers_.erase(it);
        } else {
          callbacks.push_back(it->callback);
          ++it;
        }
      }
    }
    for (auto& callback : callbacks) callback(message); // registry 锁外调用
  }

 private:
  struct Subscription {
    std::weak_ptr<void> lifetime;
    Callback callback;
  };
  std::mutex mu_;
  std::vector<Subscription> subscribers_;
};

template <class T>
class Reader final : public ReaderBase,
                     public std::enable_shared_from_this<Reader<T>> {
 public:
  using Callback = std::function<void(std::shared_ptr<const T>)>;

  static std::shared_ptr<Reader> Create(std::shared_ptr<Channel<T>> channel,
                                        Callback callback) {
    auto reader = std::shared_ptr<Reader>(
        new Reader(std::move(channel), std::move(callback)));
    reader->Start();
    return reader;
  }

  ~Reader() override { Stop(); }

  void Stop() override {
    {
      [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
      if (stopped_) return;
      stopped_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
  }

 private:
  Reader(std::shared_ptr<Channel<T>> channel, Callback callback)
      : channel_(std::move(channel)), callback_(std::move(callback)) {}

  void Start() {
    std::weak_ptr<Reader> weak = this->shared_from_this();
    channel_->Subscribe(weak, [weak](std::shared_ptr<const T> message) {
      if (auto self = weak.lock()) self->Enqueue(std::move(message));
    });
    worker_ = std::thread([this] { Run(); });
  }

  void Enqueue(std::shared_ptr<const T> message) {
    {
      [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
      if (stopped_) return;
      queue_.push(std::move(message));
    }
    cv_.notify_one();
  }

  void Run() {
    for (;;) {
      std::shared_ptr<const T> message;
      {
        std::unique_lock<std::mutex> lock(mu_);
        cv_.wait(lock, [this] { return stopped_ || !queue_.empty(); });
        if (stopped_ && queue_.empty()) return; // drain 后退出
        message = std::move(queue_.front());
        queue_.pop();
      }
      callback_(std::move(message)); // 队列锁外执行业务
    }
  }

  std::shared_ptr<Channel<T>> channel_;
  Callback callback_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::queue<std::shared_ptr<const T>> queue_;
  bool stopped_ = false;
  std::thread worker_;
};

template <class T>
class Writer {
 public:
  explicit Writer(std::shared_ptr<Channel<T>> channel)
      : channel_(std::move(channel)) {}
  void Write(std::shared_ptr<const T> message) {
    channel_->Publish(std::move(message));
  }

 private:
  std::shared_ptr<Channel<T>> channel_;
};

class Node {
 public:
  template <class T, class Callback>
  std::shared_ptr<Reader<T>> CreateReader(
      const std::string& name, std::shared_ptr<Channel<T>> channel,
      Callback&& callback) {
    auto reader = Reader<T>::Create(
        std::move(channel), std::forward<Callback>(callback));
    [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
    if (!readers_.emplace(name, reader).second) return nullptr;
    return reader;
  }

  template <class T>
  std::shared_ptr<Writer<T>> CreateWriter(
      std::shared_ptr<Channel<T>> channel) {
    return std::make_shared<Writer<T>>(std::move(channel));
  }

  ~Node() {
    std::vector<std::shared_ptr<ReaderBase>> snapshot;
    {
      [[maybe_unused]] std::lock_guard<std::mutex> lock(mu_);
      for (auto& entry : readers_) snapshot.push_back(entry.second);
      readers_.clear();
    }
    for (auto& reader : snapshot) reader->Stop();
  }

 private:
  std::mutex mu_;
  std::unordered_map<std::string, std::shared_ptr<ReaderBase>> readers_;
};

struct Image { int sequence = 0; };

int main() {
  auto channel = std::make_shared<Channel<Image>>();
  Node node;
  auto reader = node.CreateReader<Image>(
      "/camera/front", channel,
      [](const std::shared_ptr<const Image>& image) {
        std::cout << "process frame " << image->sequence << '\n';
      });
  auto writer = node.CreateWriter<Image>(channel); // Node 不保存 Writer
  writer->Write(std::make_shared<Image>(Image{42}));
  reader->Stop(); // drain 第 42 帧，并 join worker
}
```

这个版本为了教学使用无界 `std::queue`，生产者长期快于消费者时会无限增长；下一步应替换为固定容量 ring，并明确满时覆盖旧样本还是阻塞生产者。`Channel` 已经用 weak lifetime 清理过期订阅，避免 registry 反向拥有 Reader；`Reader::Create` 则保证 `shared_from_this()` 只在 shared owner 建立以后调用。

它还刻意让 Node 析构时先在锁内复制 Reader owners、清空 map，再在锁外 Stop。用户 callback 绝不能在 Node registry mutex 内执行，否则 callback 若创建/删除 Reader 会发生自死锁。继续向 Cyber 演进时，可以把每只 Reader 的 `std::thread` 替换成共享 Processor 与 routine，再加入 RoleAttributes、共享 Receiver 和 transport factory；对象边界不需要推倒重来。
