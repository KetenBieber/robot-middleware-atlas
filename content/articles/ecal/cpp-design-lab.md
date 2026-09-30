# eCAL C++ 设计实验室：从公开句柄到并发回调

一台巡检机器人启动了图像 Publisher。随后，诊断模块持有它的发送句柄；而系统管理线程发现传输配置出错，准备撤销这只 Publisher 并关闭网络连接。如果业务句柄就是一个裸 `PublisherImpl*`，管理线程先 `delete`，诊断线程再调用 `Send()`，就会访问已释放的对象。反过来，如果业务句柄总是用 `shared_ptr` 强持有实现对象，管理线程清空注册表以后，残留句柄又可能使网络实体始终不能析构。这个问题不取决于底层使用 UDP 还是共享内存：**我们需要区分“业务代码可以引用实体”与“谁决定实体应该继续存在”。**

先从一只只有一个 topic 的进程内 Publisher 开始，让它经历注册、发送、注销以及注销后的旧句柄调用；再一步步加入真实 eCAL 的 Gate、Impl、后台回调与全局关闭。源码基线为 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`，下面的短程序是独立的 C++17 教学实现；介绍 `CPublisher`、`CPubGate` 等真实符号时会明确回到该固定版本。

## 用一个可运行的弱句柄实验建立所有权模型

如果只讲“`weak_ptr` 不增加引用计数”，读者很难知道它为什么出现在中间件公开 API 里。先让运行时的 `Gate` 真正拥有实现对象，而业务侧的 `Publisher` 只保存一只弱句柄。以下程序可用 `g++ -std=c++17 -Wall -Wextra -Werror -pedantic` 编译，它与 eCAL 的真实实现具有相同的关键所有权关系，但省略了传输和发现。

~~~cpp
#include <cassert>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>

struct Impl {
    explicit Impl(std::string channel) : topic(std::move(channel)) {}
    bool Write(const std::string& value) {
        last = value;
        return true;
    }
    std::string topic;
    std::string last;
};

class Gate {
public:
    std::shared_ptr<Impl> Register(std::string name) {
        auto impl = std::make_shared<Impl>(name);
        registry_[std::move(name)] = impl;
        return impl;
    }

    void Clear() { registry_.clear(); }

private:
    std::unordered_map<std::string, std::shared_ptr<Impl>> registry_;
};

class Publisher {
public:
    Publisher(Gate& gate, std::string topic)
        : impl_(gate.Register(std::move(topic))) {}

    bool Send(const std::string& bytes) {
        auto alive = impl_.lock();
        return alive && alive->Write(bytes);
    }

private:
    std::weak_ptr<Impl> impl_;
};

int main() {
    Gate gate;
    Publisher camera(gate, "robot/camera");
    assert(camera.Send("frame-1"));

    gate.Clear();                 // Gate 放弃唯一的长期强引用
    assert(!camera.Send("frame-2")); // 旧句柄安全地报告实体失效
}
~~~

先画出强引用图。`Gate::registry_` 的 map value 是 `shared_ptr<Impl>`，每个实体都由它持有；`Publisher::impl_` 只是观察者，不让对象继续活着。`Gate::Register` 在函数内部还有一只临时强引用；返回给 `Publisher` 构造函数以后，它立刻被转换成 `weak_ptr`，表达式结束时临时强引用被销毁，留下 Gate 这个长期 owner。`Gate::Clear` 释放最后一个 map value 后，`Impl` 可以析构。随后 `weak_ptr::lock()` 失败，`Send` 返回 false，而不是对一个已经释放的地址调用 `Write`。

`std::shared_ptr` 的控制块至少需要跟踪强引用、弱引用以及实际删除动作；`weak_ptr::lock` 必须在控制块上原子地尝试取得强引用，不能先检查 `expired()` 再从裸地址创建新 `shared_ptr`，否则检查与获取之间可能发生析构。成功取得 `alive` 的调用即使与 `Gate::Clear` 并发，当前 `Impl` 的**内存**也会保留到 `alive` 离开作用域。但这并不能保证网络连接仍然活跃：真实中间件还必须在 Impl 内部用明确的关闭状态和同步协议拒绝新发送，并等待在途回调。对象内存安全、业务上的“仍可发送”、线程已经静默，是三个不同的条件。

这时再归纳模式才有意义：`Publisher` 作为 **Facade** 隐藏 Gate 和传输细节，`Gate` 是建立实体及其长期 owner 的 **Registry**，`weak_ptr` 则承载“可访问但不决定寿命”的句柄契约。它们各自解决不同的问题，不应因为代码里同时出现三种类，就给这张对象图套一个含混的“工厂模式”标签。

## 功能边界与对象关系

一个可用的进程内模型至少包含四种对象。下图是教学对象关系；实际 eCAL 的 SubGate payload 分发会先取 `shared_ptr` 快照，CPubGate registration 分发则在 shared lock 下直接遍历实现对象，后文会把这个差异与教学模型分开。

```text
Publisher facade --weak--> PublisherImpl <--shared-- PubGate
                                             ^
registration thread -------------------------|

Subscriber facade --weak--> SubscriberImpl <--shared-- SubGate
                                                 |
transport thread -> snapshot shared_ptrs -> callback
```

Facade 面向业务代码，Gate 是按 topic 查找实体的注册表，Impl 保存传输状态。这里故意让 Facade 不拥有 Impl：全局运行时关闭并清空 Gate 后，遗留在业务对象中的句柄会变成“无效但可析构”的对象，而不是悬空指针。

## 从公开 API 定位到真实源码

等价模型中的四类对象并非凭空抽象，它们分别对应固定提交中的源码边界：

| 阅读层 | 核心对象与操作 | 要回答的问题 |
|---|---|---|
| 公开句柄 | `CPublisher` 构造、`Send`、`Destroy` | 门面保存什么，失效后如何返回 |
| 实体实现 | `CPublisherImpl::Create`、`Write`、`Destroy` | writer 何时建立，发送如何选层 |
| 进程内索引 | `CPubGate` 注册与 `ApplySubscriberRegistration` | 谁持有实现对象，订阅状态怎样到达发布者 |
| 全局编排 | `CGlobals::Initialize`、`Finalize` | Gate、registration、transport 的启动和关闭顺序 |

一次发送的固定源码调用链如下：

```text
CPublisher::Send
  -> weak_ptr lock，取得 CPublisherImpl
  -> CPublisherImpl::Write
       -> 检查已建立 subscriber count
       -> 读取各 transport 的 atomic connection counter
       -> 必要时复制 payload 到成员 staging vector
       -> 调用 UDP/TCP/SHM writer
       -> 更新发送统计与错误结果
```

在解释 `weak_ptr` 之前先看公开句柄实际保存的成员。下面是固定提交中 `CPublisher` 的声明摘录：


```cpp
private:
  std::weak_ptr<CPublisherImpl> m_publisher_impl;
```

`CPublisher` 的构造函数在本次调用栈中先创建一个 `shared_ptr<CPublisherImpl>`，把它赋给这个弱成员，再把同一个强指针交给 PubGate 注册：

接着看 `CPublisher::CPublisher` 的真实实现：

```cpp
CPublisher::CPublisher(const std::string& topic_name_, const SDataTypeInformation& data_type_info_, const Publisher::Configuration& config_)
{
  auto config = eCAL::GetConfiguration();
  config.publisher = config_;

  SPublisherGlobalContext global_context;
  global_context.registration_provider = g_registration_provider();

  auto publisher_impl = std::make_shared<CPublisherImpl>(data_type_info_, BuildWriterAttributes(topic_name_, config), std::move(global_context));
  if (!publisher_impl) return;

  m_publisher_impl = publisher_impl;

  if (auto pubgate = g_pubgate(); pubgate) pubgate->Register(topic_name_, publisher_impl);
}
```

这段代码里的 `publisher_impl` 是局部强 owner；赋给 `m_publisher_impl` 只增加弱观察关系，调用 `Register` 时才把强 owner 交给全局 Gate。函数返回后，局部强指针析构，通常由 Gate 持有实体；若注册不可用，实体则会随该局部变量析构。构造完成并不等于注册成功：这里没有检查 `Register()` 的返回值，所以调用者不能把 facade 已拿到一个 weak pointer 当成 Gate 已接管的证明。这个结论来自函数控制流本身，不是对 API 设计者意图的推测。

发现链则是另一条控制路径：registration 接收远端 Subscriber 的能力信息，CPubGate 按 topic 定位 PublisherImpl，PublisherImpl 在连接 map 锁下更新状态并调整发送计数。发送方检查的是阶段状态而不是不可变连接计划；首次样本可以准备 layer counter，后续 refresh 才增加已建立 subscriber count。不要把“发现订阅者”和“发送 payload”混成一条同步调用链。

在进入具体类之前，先区分接口里最常见的三种“借用”。`const Registration::Sample&` 是对调用者对象的只读别名：函数不复制整份样本，也不能在原对象析构后保存该引用；`const char* payload` 是可能为空的地址，必须与独立的长度一起解释，单看指针无法知道有多少有效字节；C++20 的 `std::span<const std::byte>` 能把地址和长度封装为只读连续视图，但仍不拥有内存。固定 eCAL core 是 C++17，所以教学代码用下面这个很薄的 `ByteView` 表示同一借用；它与 `std::span` 一样不持有缓冲区。若 `Send()` 只在本次调用里同步读取，借用可以很短；一旦某层异步排队，必须在返回前复制数据或转移一个明确 owner。eCAL 的 `CPublisher::Send(const void*, size_t)` 把原始地址包装成 `CBufferPayloadWriter`，它不会自动延长调用者 buffer 的寿命。

**教学最小代码（C++17 的非拥有字节视图，不是 eCAL 源码）：**

```cpp
struct ByteView {
  const std::byte* data;
  std::size_t size;
};
```

## 第一层：用 RAII 表达一个实体的存在

先定义拥有资源的实现对象。下面是教学最小模型，不是固定提交中的 `CPublisherImpl` 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class PublisherImpl final {
 public:
  explicit PublisherImpl(std::string topic)
      : topic_(std::move(topic)) {}

  PublisherImpl(const PublisherImpl&) = delete;
  PublisherImpl& operator=(const PublisherImpl&) = delete;

  bool Write(ByteView bytes) {
    if (!running_.load(std::memory_order_acquire)) return false;
    return WriteSelectedLayers(bytes);
  }

  void Stop() noexcept {
    running_.store(false, std::memory_order_release);
  }

 private:
  std::string topic_;
  std::atomic<bool> running_{true};
};
```

`explicit` 阻止 `std::string` 被意外隐式转换为 `PublisherImpl`。`std::move(topic)` 让成员通过移动构造接收参数资源；它不保证一定转交原来的字符地址，短字符串实现可能直接复制小缓冲。移动之后参数仍可析构，但其值只保证处于有效而未指定状态。删除复制操作不是语法洁癖：实体背后可能绑定 socket、共享内存文件和注册身份，复制一个 C++ 对象并不能复制这些外部事实。

`final` 表明这里不是面向继承扩展的接口。运行时扩展应发生在 transport strategy，而不是通过派生 PublisherImpl 改写生命周期。

### acquire/release 在这里保证了什么

`Stop()` 的 release store 与 `Write()` 的 acquire load 可以建立同步关系：若停止线程在 store 之前写入了其他状态，发送线程观察到 false 后也能观察到这些先前写入。但这段代码在读到 true 后，并不能阻止 Stop 与 `WriteSelectedLayers()` 并发。atomic 只是入口闸门，不是“等待所有发送退出”的屏障。

若销毁语义要求 `Stop()` 返回后不再访问 writer，需要活动操作计数：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class OperationGuard {
 public:
  explicit OperationGuard(PublisherImpl& owner)
      : owner_(&owner), entered_(owner.TryEnter()) {}
  ~OperationGuard() { if (entered_) owner_->Leave(); }
  explicit operator bool() const noexcept { return entered_; }
 private:
  PublisherImpl* owner_;
  bool entered_;
};
```

`TryEnter()` 必须把“状态仍为 Running”和“active_count++”作为同一临界区内的事务。`Stop()` 先切换到 Stopping，阻止新的 guard，再等待计数归零。单独用两个 atomic 会出现发送线程读到 Running、停止线程读到计数为零并销毁 writer、发送线程随后才加计数的竞态。

## 第二层：用 unique、shared 和 weak 表达不同所有权

`std::unique_ptr<T>` 表示只有一个 owner；对象随这个 owner 析构而释放。它适合 Runtime 唯一持有的 provider、transport module 或 gate。`std::shared_ptr<T>` 表示多个 owner 可以共同延长对象生命；最后一个强引用析构时才调用对象析构器。eCAL Gate 对 Impl 的 map value 是 shared pointer，当前 Send 的 `weak_ptr::lock()` 临时得到一个 shared pointer，SubGate 分发 vector 也为当前调用暂时持有 strong refs。`std::weak_ptr<T>` 能观察和尝试取得对象，但不延长对象生命；公共 facade 使用它，使全局 Gate 摘除后旧 facade 的 lock 返回空。

这些智能指针共享一个控制块。概念上控制块保存 deleter、强引用计数与弱引用计数；实现布局由标准库决定。强计数归零时，T 已析构，但只要还有 weak pointer，控制块仍须留着，以便后续 `lock()` 安全地读出“强计数为零”；最后一个 weak pointer 消失后控制块才释放。不要把“对象已析构”和“控制块内存已释放”当作同一个时刻。引用计数更新通常是原子操作，却不保护 T 的普通成员：同一 `shared_ptr` 被复制给两个线程只证明 T 活着，不证明两线程可同时写 `m_payload_buffer`。

可观察的反例是：camera node 的全局 Runtime 正在 Finalize，控制线程从 PubGate 摘掉 Impl；若每个 facade 都是 `shared_ptr`，遗留业务句柄仍让 Impl、writer 和共享对象继续存活，Runtime 不能按计划回收。换为 `weak_ptr` 后，已经 lock 的 Send 会安全完成对象级访问，但 Gate 清理也不会等待这次 Send 或 callback 结束。若析构 writer 前必须等在途操作退出，仍需 activity guard/condition variable 形成屏障，智能指针本身不提供。

模板与虚函数解决的是另外两种变化。`CExpirationMap<Key, T, ClockType, MapType>` 在编译期把 key、value、时钟和底层 map 类型参数化；`CTimeoutProvider` 使用其默认参数实例化出 `CExpirationMap<SampleIdentifier, Sample, steady_clock, std::map>`，编译器为这组参数生成具体类型，类型检查和大部分调用绑定在编译期完成。`CPayloadWriter` 则用 virtual `WriteFull()`/`GetSize()` 让 `CBufferPayloadWriter` 与自定义序列化器共享同一个运行时调用接口；通过 base reference 调用时，虚表把调用分派到实际派生类。模板适合编译期知道的类型差异，virtual interface 适合运行期替换具体 payload writer；二者并不互斥。

## 第三层：公开句柄只取得临时所有权

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class Publisher {
 public:
  explicit Publisher(std::weak_ptr<PublisherImpl> impl)
      : impl_(std::move(impl)) {}

  bool Send(ByteView bytes) const {
    auto impl = impl_.lock();
    if (!impl) return false;
    return impl->Write(bytes);
  }

 private:
  std::weak_ptr<PublisherImpl> impl_;
};
```

`weak_ptr::lock()` 是一个原子化的“若对象仍存活，则取得强引用”操作。不能先调用 `expired()` 再使用裸指针，因为检查与使用之间另一个线程可能释放最后一个 `shared_ptr`。局部变量 `impl` 把对象寿命延长到本次 `Send()` 返回。

固定提交的 `CPublisher::Send()` 正是这样建立本次调用的寿命租约，并把空订阅与真正写入分开：


```cpp
bool CPublisher::Send(CPayloadWriter& payload_, long long time_)
{
  auto publisher_impl = m_publisher_impl.lock();
  if (!publisher_impl) return false;
  if (GetSubscriberCount() == 0)
  {
    publisher_impl->RefreshSendCounter();
    return false;
  }

  const long long write_time = (time_ == DEFAULT_TIME_ARGUMENT) ? eCAL::Time::GetMicroSeconds() : time_;
  return publisher_impl->Write(payload_, write_time, 0);
}
```

从这段真实调用可见，局部强指针活到 `Send()` 返回；它让并发注销不能在方法中途析构 `PublisherImpl`，但 `GetSubscriberCount()` 和后续 `Write()` 不是一个共同锁住的事务，也没有阻止两个应用线程同时进入 `Write()`。这解释了为什么对象生命周期安全与成员并发安全必须分别证明。`GetSubscriberCount()` 返回零时不进入 transport writer，所以返回 `false` 的语义是这次没有发送给已建立订阅者，而不是“对象失效”这一种情况。

这只保证内存寿命，不保证业务状态。对象可能仍存活但已经 `Stop()`；因此 `Write()` 仍需检查运行状态。由此得到一条重要规则：

> 智能指针回答“内存还在不在”，状态机回答“操作还允不允许”。两者不能互相替代。

### `weak_ptr` 的控制块仍有成本

实现对象释放后，只要 facade 仍持有 weak pointer，shared/weak control block 就不能释放。`lock()` 通常需要原子方式尝试增加强引用计数；在极高频 Send 路径上，这会造成共享 cache line 竞争。可选方案是 facade 持有稳定的 `shared_ptr<Impl>`，由 Impl 内部状态拒绝操作；这样对象会活到最后一个 facade 析构，Finalize 无法立即回收 Impl。eCAL 风格的弱句柄优先选择“全局关闭能够统一失效”，代价是每次调用的原子操作。

这里不能换成原始指针加 `running_`：读取 running 本身已经需要解引用对象；如果对象先被释放，连状态检查都是未定义行为。hazard pointer、epoch reclamation 可以减少引用计数，但会显著提高实现复杂度，通常不适合控制面实体句柄。

## 第四层：Gate 同时是索引与所有权根

**教学最小代码（SubGate 风格的锁内快照；不是固定提交源码）：**

```cpp
class PubGate {
 public:
  Publisher Create(std::string topic) {
    auto impl = std::make_shared<PublisherImpl>(topic);
    {
      std::lock_guard lock(mu_);
      by_topic_.emplace(std::move(topic), impl);
    }
    return Publisher{impl};
  }

  std::vector<std::shared_ptr<PublisherImpl>> Find(
      std::string_view topic) const {
    std::vector<std::shared_ptr<PublisherImpl>> result;
    std::lock_guard lock(mu_);
    auto [first, last] = by_topic_.equal_range(std::string(topic));
    for (; first != last; ++first) result.push_back(first->second);
    return result;
  }

 private:
  mutable std::mutex mu_;
  std::unordered_multimap<std::string,
                          std::shared_ptr<PublisherImpl>> by_topic_;
};
```

这是用于推导的 Gate 模型，并非 eCAL `CPubGate` 的逐字实现。模型选择 `unordered_multimap`，平均查找成本约为 `O(1 + k)`，`k` 为同 topic 实体数，最坏情况会退化到 `O(n)`；固定 eCAL 的 `CPubGate` 实际用 `std::multimap`，而 `CSubGate` 才是 `std::unordered_multimap`。示例 `Find()` 像 `CSubGate::ApplySample()` 一样在锁内复制 shared pointer、锁外分发；真实 `CPubGate::ApplySubscriberRegistration()` 会持 shared lock 直接调用 PublisherImpl。每次把 `string_view` 转成 `string` 会分配内存，真正热路径可用透明哈希避免分配，但注册与发现通常属于控制面。

`Find()` 在锁内只复制 `shared_ptr`，随后把快照返回。调用方应在锁外执行连接更新。否则一个较慢的 socket 建连会占住 Gate 的互斥量，使所有 topic 的注册和注销串行等待。

### 创建不是一次 `emplace`

真实创建还会生成实体 ID、向 registration provider 注册、为 transport 添加端点。若第三步失败，前两步必须回滚。可以用局部事务对象记录已经完成的动作：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class CreateRollback {
 public:
  explicit CreateRollback(PubGate& gate) : gate_(gate) {}
  ~CreateRollback() {
    if (!committed_) {
      if (transport_added_) RemoveTransport(entity_);
      if (registered_) Unregister(entity_);
      if (inserted_) gate_.Erase(entity_);
    }
  }

  void Commit() noexcept { committed_ = true; }
  EntityId entity_{};
  bool inserted_{};
  bool registered_{};
  bool transport_added_{};

 private:
  PubGate& gate_;
  bool committed_{};
};
```

这就是 RAII 在“内存之外”的用途：析构函数不只 delete，也可以撤销注册、关闭句柄和恢复一致状态。回滚对象必须在每个成功步骤之后立即更新标志，且析构不能抛异常。`Commit()` 是事务线性化点：从这时起，清理由正式 owner 负责。

`unordered_multimap<string, shared_ptr<...>>` 便于按 topic 查找，却不擅长按 entity ID 精确删除。工业实现通常需要两个索引，或让主表以 ID 为键、topic 表只保存 ID。两张表更新必须在同一锁域中完成，否则发现线程可能看见只插入一半的实体。

固定 eCAL 的 PubGate 采用可重复 topic 的 `std::multimap`，map value 本身是 `shared_ptr`；这既允许同一 topic 上存在多个 PublisherImpl，也使 Gate 成为这些实现对象的强所有权根：


```cpp
using TopicNamePublisherMapT = std::multimap<std::string, std::shared_ptr<CPublisherImpl>>;
std::shared_timed_mutex  m_topic_name_publisher_mutex;
TopicNamePublisherMapT   m_topic_name_publisher_map;

bool CPubGate::Register(const std::string& topic_name_, const std::shared_ptr<CPublisherImpl>& publisher_)
{
  if(!m_created) return(false);

  const std::unique_lock<std::shared_timed_mutex> lock(m_topic_name_publisher_mutex);
  m_topic_name_publisher_map.emplace(std::pair<std::string, std::shared_ptr<CPublisherImpl>>(topic_name_, publisher_));

  return(true);
}
```

`multimap` 的 key 不是唯一的，所以相同 topic 的两个 Publisher 不会互相覆盖；登记时复制进 map 的强指针使 Impl 活到精确注销或 `Stop()` 清表。`unique_lock` 对应一次排他更新，而读端可用共享锁并发查找。代价是对同一 topic 的实体删除仍需在 `equal_range()` 范围内比对身份，不能只按 topic 擦除；锁保护的是容器结构和 owner 集合，不保护 Impl 内部 writer 的普通状态。

## 第五层：回调分发必须脱离注册表锁

**教学最小代码（展示统一回调签名与锁外分发；不是固定提交源码）：**

```cpp
using Callback = std::function<void(ByteView)>;

void SubGate::Deliver(std::string_view topic,
                      ByteView payload) {
  std::vector<std::shared_ptr<SubscriberImpl>> targets;
  {
    std::lock_guard lock(mu_);
    auto [first, last] = by_topic_.equal_range(std::string(topic));
    for (; first != last; ++first) targets.push_back(first->second);
  }
  for (const auto& target : targets) target->Deliver(payload);
}
```

`std::function` 是拥有型、可复制的 type-erased 调用包装器：普通函数、函数对象和 lambda 只要能以相同签名调用，就可存进同一种 `Callback`。调用点只写 `callback(payload)`，不需要知道目标闭包的具体类型；代价是可能有一次小对象优化之外的动态分配和间接调用。捕获方式仍决定闭包保存什么：`[topic_info]` 复制 topic 描述；`[&config]` 保存引用，config 必须活到调用结束；`[this]` 保存裸 `this` 地址，不会给对象增加 shared count；`[weak = weak_from_this()]` 则让回调执行时重新 lock owner。快照解决 Gate 锁持有时间与对象内存寿命问题，但不自动解决捕获对象寿命。

如果快照中的回调在注销之后已经开始分发，它仍可调用刚被注销的实体。若语义要求注销返回后绝不再进入 callback，就要活动 callback 计数与条件变量，注销也会成为阻塞屏障。固定 eCAL 的 SubGate 分发采用前一种“已取得快照的调用可以收尾”语义；SHM observer 线程的 join 另提供该 observer 的关闭屏障。

`ByteView` 不拥有数据，和 `std::span` 一样只包含地址与长度。回调若把视图保存到函数返回以后，就会形成悬空视图。API 必须在文档中明确：借用只在回调期间有效；需要跨线程保存时复制到拥有型 `std::vector<std::byte>` 或共享缓冲区。

### 注销屏障的最小实现

需要强语义时，SubscriberImpl 可以维护关闭状态和活动回调数：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class SubscriberImpl {
 public:
  void Deliver(ByteView payload) {
    Callback callback;
    {
      std::lock_guard lock(mu_);
      if (stopping_) return;
      ++active_callbacks_;
      callback = callback_;
    }

    auto leave = ScopeExit([this] {
      std::lock_guard lock(mu_);
      if (--active_callbacks_ == 0) idle_.notify_all();
    });
    callback(payload);  // 用户代码永远在锁外
  }

  void Stop() {
    std::unique_lock lock(mu_);
    stopping_ = true;
    idle_.wait(lock, [this] { return active_callbacks_ == 0; });
    callback_ = {};
  }

 private:
  std::mutex mu_;
  std::condition_variable idle_;
  bool stopping_{};
  std::size_t active_callbacks_{};
  Callback callback_;
};
```

`ScopeExit` 保证 callback 正常返回或抛异常时都递减计数。必须先在锁内复制 `std::function`，否则另一个线程清空 callback 后会与调用并发。`Stop()` 使用带谓词的 wait，以抵抗虚假唤醒。

这个版本有一个刻意暴露的陷阱：如果 callback 在自己的执行线程内同步调用 `Stop()`，它会等待包含自身在内的计数归零，形成自等待。可选语义包括：禁止回调内同步销毁；检测当前 dispatch token 并延迟清理；把 Stop 拆成非阻塞 `RequestStop()` 与只能从外部线程调用的 `Join()`。API 必须选择一种，而不是把问题留给偶然调度。

## 原子变量与互斥量负责不同层级

`running_` 是一个独立布尔状态，适合 `atomic<bool>`。而“连接集合 + 每层订阅计数 + 当前 writer”是多字段不变量，必须在同一把互斥量下更新：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct LayerState {
  std::mutex mu;
  std::unordered_map<EndpointId, Connection> connections;
  std::size_t shm_readers = 0;
  std::size_t tcp_readers = 0;
};
```

把每个计数器都改成 atomic 并不能保证它们与 `connections` 在同一个逻辑时刻一致。原子适合单变量并发，不自动提供跨变量事务。

固定 eCAL 的 `SSendLayerConnectionCounters` 明确对每层调用 `fetch_add/fetch_sub/load(..., memory_order_relaxed)`；Publisher 已建立连接计数在连接 map 锁内变化，也用 relaxed 更新。relaxed 仍保证这个计数器本身的读改写不可撕裂、遵守原子修改顺序，但不会把 connection map 或 writer 成员的普通写入发布给 Send 线程，也不会让三个 layer counter 成为同一时刻的快照。这里它只把计数当作“是否有候选层”的热路径提示；map 和计数增减的一致性由 `m_connection_map_mutex` 的连接状态转换维护。源码见 `SSendLayerConnectionCounters` 与 `ApplySubscriberRegistration()`。

这不是教学模型，而是实际源码里的三个独立原子摘要与 relaxed 更新。下面摘录展示一个层从连接变化到发送端读取摘要的完整接口：


```cpp
struct SSendLayerConnectionCounters
{
  void Increment(TransportLayer::eType layer_);
  void Decrement(TransportLayer::eType layer_);
  void Reset();

  bool UdpEnabled() const;
  bool ShmEnabled() const;
  bool TcpEnabled() const;

  std::atomic<size_t> udp{ 0 };
  std::atomic<size_t> shm{ 0 };
  std::atomic<size_t> tcp{ 0 };
};

void CPublisherImpl::SSendLayerConnectionCounters::Increment(TransportLayer::eType layer_)
{
  switch (layer_)
  {
  case TransportLayer::eType::udp_mc:
    udp.fetch_add(1, std::memory_order_relaxed);
    break;
  case TransportLayer::eType::shm:
    shm.fetch_add(1, std::memory_order_relaxed);
    break;
  case TransportLayer::eType::tcp:
    tcp.fetch_add(1, std::memory_order_relaxed);
    break;
  default:
    break;
  }
}

bool CPublisherImpl::SSendLayerConnectionCounters::UdpEnabled() const
{
  return (udp.load(std::memory_order_relaxed) > 0);
}
```

`relaxed` 仍让 `udp` 这一整数的增减不撕裂，并为该原子对象建立修改顺序；它没有 release/acquire 发布其他字段。若 registration 线程先改 `m_connection_map`、随后增加计数，Send 线程读到正数只获得“UDP 可能有目标”的提示，真正建立与撤销连接的复合状态仍由 `m_connection_map_mutex` 维护。三个计数分开读取也不是同一时刻的一致快照。两个相机线程同时调用同一 `CPublisherImpl::Write()` 时，计数器的原子性更不会保护 `m_payload_buffer.resize()`、`m_clock` 或 SHM writer index；若 resize 扩容，旧 `data()` 地址还会失效。真实代码据此支持“原子摘要不等于 Publisher 可并发 Send”的结论，应用应在同一实例外串行化调用。

还有一个比“两条 Send 同时写 staging vector”更早发生的边界：动态发现首次为 Publisher 创建某层 writer 时，registration 线程写入普通 `unique_ptr`，而业务线程读取同一成员。下面是固定提交中对应的两个源码落点；只保留相关语句，省略平台宏和日志，不改变它们的控制流：


```cpp
bool CPublisherImpl::StartUdpLayer()
{
  if (m_layers.udp.write_enabled) return false;
  m_layers.udp.write_enabled = true;

  m_writer_udp = std::make_unique<CDataWriterUdpMC>(eCAL::eCALWriter::BuildUDPAttributes(m_publisher_id, m_attributes));
  Register();
  return true;
}

const bool udp_send_enabled = m_writer_udp && m_send_layer_connection_counters.UdpEnabled();
```

这两处不使用同一把锁：`StartUdpLayer()` 在 `m_writer_udp` 上赋值；`Write()` 先读这个 `unique_ptr`，再通过 relaxed 原子 load 读层计数。计数为零时的短路判断不能保护对指针的第一次读取；即使它为正，relaxed load 也不为此前的普通写建立 release/acquire happens-before。因而在首次发现 UDP 订阅者、另一线程同时 Send 的时间线上，按 C++ 内存模型存在对 `m_writer_udp` 的并发读写 data-race 候选。可见结果可能是未定义行为，包括偶发未发送、无效指针访问或崩溃；没有基于该提交的运行时复现时，不应把其中某一种写成必现。`m_connection_map_mutex` 只在 registration 更新 map/count 的区域互斥，而 `Write()` 不获取它，所以那把锁不能保护 writer 指针的发布。工程上应由实现提供同步发布与关闭协议，例如互斥保护的 writer lease 或不可变 `shared_ptr` 计划快照，并保证移除/销毁只发生在在途 lease 退出后；单靠“先建 writer、后加计数”不足以构成标准层同步。

同一 writer 指针还会被周期快照线程读取。`CPubGate::GetRegistrations()` 持 Gate 的 shared lock 枚举 PublisherImpl 并调用 `GetRegistration()`；但 `StartUdpLayer()` 和 `GetRegistrations()` 也都是 shared-lock 读者，前者在锁下创建 `unique_ptr` 并不会因此与后者互斥。`GetRegistrationSample()` 随后读取 writer 指针、普通 active/enabled 字段并调用 writer 获取连接参数；业务 `Write()` 同时写 active 标志。下面把读写双方放在一个源码视野里：


```cpp
void CPubGate::GetRegistrations(Registration::SampleList& reg_sample_list_)
{
  if (!m_created) return;
  const std::shared_lock<std::shared_timed_mutex> lock(m_topic_name_publisher_mutex);
  for (const auto& iter : m_topic_name_publisher_map)
    iter.second->GetRegistration(reg_sample_list_.push_back());
}

if (m_writer_udp)
{
  eCAL::Registration::TLayer udp_tlayer;
  udp_tlayer.type = tl_ecal_udp;
  udp_tlayer.enabled = m_layers.udp.write_enabled;
  udp_tlayer.active = m_layers.udp.active;
  udp_tlayer.par_layer.layer_par_udpmc = m_writer_udp->GetConnectionParameter();
  ecal_reg_sample_topic.transport_layer.push_back(udp_tlayer);
}

udp_sent = m_writer_udp->Write(m_payload_buffer.data(), wattr);
m_layers.udp.active = true;
```

代码中的 Gate shared lock 保证 `m_topic_name_publisher_map` 在遍历期间不被删除，但它是读锁，不能序列化另一位 shared-lock 持有者对 PublisherImpl 的 `StartUdpLayer()`。更重要的是，writer 指针和 active/enabled 标志没有被上述 Gate 锁一起保护；provider 线程采样 registration 与第一次发现的 writer 创建、业务 Send 设置 active 可以并发。观察到的风险是 registration 可能报告不一致状态或读取尚未同步发布的 writer，仍属于源码层的 data-race 候选而非每次必现的失败。`GetRegistrationSample()` 末尾单独取得的 `m_connection_map_mutex` 只保护连接数统计，不会回头保护前面已读取的 layer 字段。复刻版可用一把明确的 layer-state mutex 保护 writer lease 与状态快照，或把状态和强持有 writer 的不可变对象一次发布；代价分别是采样与 Send 多一次锁竞争，或拓扑变化时保留旧快照/延迟析构。

读者可以用同一个量产反例检查两类同步：registration 正把第二个 SHM subscriber 从 pending 改为 established，Send 同时读到 `shm_count > 0`，这只说明它可以尝试调用 SHM writer；不表示 map 状态、公共 subscriber count 和所有 writer 参数已经形成跨变量原子快照。更危险的是两个相机线程同时对同一个 Publisher 调用 `Send()`：`std::vector<char>::resize()` 若超过 capacity 会分配新存储、搬移元素并使旧 `data()` 指针失效；另一个线程可能正把旧 data 指针交给 TCP/UDP writer。eCAL 的计数器是原子的，但 `m_payload_buffer`、`m_clock` 和 SHM `m_write_idx` 是普通成员，`CPublisherImpl::Write()` 没有覆盖整次调用的发送互斥锁，所以并发 Send 会形成数据竞争候选。调用方应串行化同一 Publisher，或以不同 Publisher 分离生产线程，并在应用层明确序号顺序。

## 多传输层复刻可以生成不可变发送计划

Publisher 可能同时知道多个 writer，但一次消息究竟走哪一层，取决于本机/远端位置、双方能力、配置优先级和连接健康状态。固定 eCAL 提交没有下面这种 `SendPlan`：它用连接 map、writer 成员和原子 layer counter 分阶段维护状态，`Write()` 直接读取这些字段。本节给的是可复刻/改进方案，用来说明怎样把前文指出的 writer 发布竞态改成一个明确快照；不要把它倒读成 eCAL 实际拥有的模块。

一种替代设计是在锁内生成不可变计划，锁外执行 I/O：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct SendTarget {
  EndpointId endpoint;
  std::shared_ptr<IWriter> writer;
};

struct SendPlan {
  std::uint64_t generation;
  std::vector<SendTarget> targets;
};

std::shared_ptr<const SendPlan> PublisherImpl::LoadPlan() const {
  std::lock_guard lock(layer_state_.mu);
  return current_plan_;
}
```

发现线程在连接变化时构造新 plan，完整后一次替换 `current_plan_`；发送线程只复制一个 `shared_ptr<const SendPlan>` 并在锁外遍历。`const` 保证计划发布后不会原地修改，因而发送线程看到的是旧版本或新版本，不会看到“targets 已清空但 generation 尚未更新”的半成品。

若订阅者数为 `N`，每次重建计划成本约为 `O(N)`，发送命中成本为 `O(1)` 取得快照加 `O(F)` 扇出，`F` 为实际目标 writer 数。它把低频拓扑变更的计算换成高频发送路径的稳定性。缺点是旧计划会存活到最后一个发送者释放快照，短时间同时占用两份连接表；writer 的关闭也必须容忍旧计划持有引用。

### 同一消息是否只编码一次

可以把 payload 处理拆成三层：

```text
application object
  -> canonical serialized bytes
     -> per-layer frame/header/compression
        -> per-endpoint socket or shared-memory write
```

第一层序列化若与连接无关，可以得到 `shared_ptr<const Buffer>` 并被多个 target 复用。第二层若不同 Carrier 需要不同头、压缩或分片，就仍要逐层生成 frame。所谓“零拷贝”必须说明是哪一层零拷贝：避免应用对象序列化、避免用户态 frame 复制、还是共享内存消费者直接借用同一 payload，三者不是同一保证。

错误聚合也要预先定义。对三个 target 发送时，“任一成功即 Send 成功”“全部成功才成功”和“只报告入队成功”会产生不同的业务语义。返回一个 bool 很难表达部分成功；至少应把逐层错误写入可观测计数，避免调用者只看到 true 却不知道 TCP 分支持续失败。

## 关闭顺序是依赖图的逆序

安全关闭应按以下顺序收口：

```text
拒绝创建新实体
  -> 停止 registration/transport 产生新任务
  -> 标记 Impl 停止并等待在途回调
  -> 清空 Gate 的强引用
  -> 关闭 socket、共享内存和线程
```

若先销毁 Gate，后台线程可能还拿 topic 查询实体；若先关闭底层 transport，正在执行的 `Write()` 可能访问已析构 writer。生命周期设计不能只靠析构函数碰巧按成员逆序运行，而应把依赖关系写成显式阶段。

### 全局引用计数不等于完整状态机

进程中多个库模块可能分别调用 Initialize/Finalize，因此运行时常带初始化计数。计数从 0 变 1 时真正启动，从 1 变 0 时真正关闭；但计数无法表达 Starting、Stopping 和 Failed。至少需要：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
enum class RuntimeState {
  Cold,
  Starting,
  Running,
  Stopping,
  Failed
};
```

所有状态和引用计数必须由同一 mutex 保护。两个线程同时首次 Initialize 时，只能有一个执行启动，另一个等待 Starting 结束；Finalize 遇到 Starting 不能直接减计数并拆除半构造对象。启动中途失败要逆序撤销已启动模块，并发布 Failed 或回到 Cold，不能留下“计数为 1、Gate 为空”的伪运行状态。

## 最小可复刻运行时

下面把 Gate、弱句柄和显式关闭组合成一条完整路径。为保持重点，省略 registration 报文和真实 socket，但所有权与状态边界可以直接扩展：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
#include <condition_variable>
#include <cstddef>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

class Runtime final {
 public:
  static std::shared_ptr<Runtime> Start() {
    auto runtime = std::shared_ptr<Runtime>(new Runtime);
    try {
      runtime->StartModules();
    } catch (...) {
      runtime->Stop();
      throw;
    }
    return runtime;
  }

  Publisher CreatePublisher(std::string topic) {
    auto operation = TryBeginOperation();
    if (!operation) {
      return Publisher{std::weak_ptr<PublisherImpl>{}};
    }

    std::shared_ptr<PublisherImpl> impl;
    {
      std::lock_guard lock(mu_);
      impl = std::make_shared<PublisherImpl>(std::move(topic));
      publishers_.emplace(impl->Id(), impl);
    }

    bool registered = false;
    try {
      registered = registration_->Add(impl->Descriptor());
    } catch (...) {
      std::lock_guard lock(mu_);
      publishers_.erase(impl->Id());
      throw;
    }
    if (!registered) {
      std::lock_guard lock(mu_);
      publishers_.erase(impl->Id());
      return Publisher{std::weak_ptr<PublisherImpl>{}};
    }
    return Publisher{impl};
  }

  void Stop() noexcept {
    std::call_once(stop_once_, [this] {
      std::vector<std::shared_ptr<PublisherImpl>> entities;
      {
        std::unique_lock lock(mu_);
        state_ = State::Stopping;
        operations_cv_.wait(lock, [this] { return active_operations_ == 0; });
        for (auto& [id, impl] : publishers_) entities.push_back(impl);
      }

      if (registration_) registration_->StopIncomingAndJoin();  // 停止远端更新，保留本地注销能力
      for (auto& entity : entities) entity->StopAndWait();

      {
        std::lock_guard lock(mu_);
        publishers_.clear();       // 从 topic index 移除；entities 快照仍暂时持有 Impl
      }

      entities.clear();            // 依赖仍活着时释放 Impl 与它拥有的 writer
      if (registration_) registration_->StopAndJoin();
      registration_.reset();
      if (transport_) transport_->StopAndJoin();
      transport_.reset();

      std::lock_guard lock(mu_);
      state_ = State::Stopped;
    });
  }

  ~Runtime() { Stop(); }

 private:
  Runtime() = default;
  enum class State { Starting, Running, Stopping, Stopped };

  class Operation final {
   public:
    Operation(const Operation&) = delete;
    Operation& operator=(const Operation&) = delete;
    Operation(Operation&& other) noexcept : owner_(std::exchange(other.owner_, nullptr)) {}
    ~Operation() { if (owner_) owner_->FinishOperation(); }

   private:
    friend class Runtime;
    explicit Operation(Runtime* owner) : owner_(owner) {}
    Runtime* owner_;
  };

  std::optional<Operation> TryBeginOperation() {
    std::lock_guard lock(mu_);
    if (state_ != State::Running) return std::nullopt;
    ++active_operations_;
    return Operation{this};
  }

  void FinishOperation() noexcept {
    {
      std::lock_guard lock(mu_);
      --active_operations_;
    }
    operations_cv_.notify_all();
  }

  void StartModules() {
    auto transport = TransportRuntime::Start();
    auto registration = RegistrationRuntime::Start(*transport);
    transport_ = std::move(transport);
    registration_ = std::move(registration);
    std::lock_guard lock(mu_);
    state_ = State::Running;
  }

  std::mutex mu_;
  std::condition_variable operations_cv_;
  std::size_t active_operations_{0};
  State state_{State::Starting};
  std::unordered_map<EntityId,
                     std::shared_ptr<PublisherImpl>> publishers_;
  std::unique_ptr<TransportRuntime> transport_;
  std::unique_ptr<RegistrationRuntime> registration_;
  std::once_flag stop_once_;
};
```

### 逐段解释所有权

构造函数私有，调用者只能使用 `Start()`；这避免拿到尚未启动完成的 Runtime。这里先用 `new Runtime`，再放入 shared pointer，是因为私有构造函数不能直接被普通 `make_shared` 内部代码访问。若不需要 Runtime 共享所有权，更推荐返回 `unique_ptr<Runtime>`，让唯一 owner 更清晰。

`StartModules()` 用局部 unique pointer 先完成 transport，再完成依赖 transport 的 registration。第二步抛异常时，第一个局部对象自动析构；只有全部成功才移动到成员。这是“先在局部构造，最后提交”的启动事务。

CreatePublisher 在锁内确认 Running、构造实体并插表，在锁外调用 registration，避免注册 I/O 占用全局 mutex。如果注册失败，再锁定并按 ID 精确删除。真正实现还必须防止注册期间 Stop 开始：可以把创建计入 active operation，或使 Stop 在切换 Stopping 后等待所有创建事务完成。

Stop 先截取实体强引用快照，所以解锁后对象仍然存在。教学模型把 registration 分成两个阶段：`StopIncomingAndJoin()` 结束接收远端控制更新，但保留本地发 unregister 的能力；`StopAndJoin()` 则在实体清理完成后彻底停掉周期发送线程。`StopAndWait()` 阻止新 Send 并等待在途 Send/回调；清 Gate 后，局部快照仍让 weak facade 暂时可以 `lock()`，所以必须在 Runtime 依赖仍存活时显式 `entities.clear()`，让停止后的 Impl 与 writer 先析构。之后才销毁 registration，再 stop/join transport。Facade 在关闭过程中即使暂时 lock 成功，也应由 Impl 的停止状态拒绝工作；所有其他强引用释放后 weak pointer 才真正过期。`call_once` 令多次 Stop 汇聚到唯一关闭过程。这里的两个 method 名是教学接口，不是 eCAL 符号；真实 `CGlobals::Finalize()` 的 receiver/provider 分段顺序见本章上文。

这版骨架把“正在创建 Publisher”计入 `active_operations_`。`TryBeginOperation()` 先在 `mu_` 下检查 Running 再加一；停止方在同一把锁下切换到 Stopping，随后用 `operations_cv_.wait(lock, predicate)` 等这个计数归零。条件变量不是保存通知的计数器：等待方必须持有 mutex 检查谓词，`wait` 原子地解锁并进入等待，醒来后重新加锁并重查谓词，因为既可能虚假唤醒，也可能另一个创建操作先消费状态。`FinishOperation()` 在同一把锁下减计数，再通知等待方。通知只让等待线程有机会变成 runnable；内核何时给它 CPU，取决于调度器。由于 Stop 在访问或 reset `registration_` 前等完所有 operation，创建方在锁外调用 `Add()` 时该 unique pointer 仍有效。

这个示例约定 `Stop()` 由 Runtime owner 的关闭路径调用，不能从持有 Operation guard 的同步回调里重入；否则 Stop 会等待包含自身在内的活动操作归零，形成自等待。若系统允许回调请求关闭，应把请求投递给独立 owner/supervisor 线程，再由它执行 Stop。代码也不能防止调用者在另一个线程无外部强引用地销毁 Runtime 的同时继续调用成员函数；C++ 对象本身必须先有清晰的调用期 owner。

### 这段骨架仍需补齐的工业能力

- `StartModules()` 要包含配置验证、监控、时间与日志模块的依赖回滚；
- `StopAndWait()` 必须与 Send、回调、重入关闭约束一起设计，并为超时提供诊断和强制中断策略，不能无限等待坏 writer；
- registration Add 成功但响应丢失时，Remove 必须幂等；
- Runtime 自身若被后台回调捕获，必须避免 shared_ptr 环；
- 进程退出阶段的日志设施可能先于 Runtime 消失，析构错误路径不能依赖已销毁单例；
- fork、动态库卸载和静态对象析构顺序需要单独限定支持范围。

## C++ 语法到系统设计的对应表

| C++ 机制 | 局部含义 | 系统级作用 | 常见误用 |
|---|---|---|---|
| `explicit` | 禁止单参数隐式转换 | 避免 topic 字符串意外生成实体 | 误以为它和线程安全有关 |
| `= delete` | 禁止复制 | 防止两份对象重复关闭同一实体 | 删除 copy 后盲目默认 move |
| `weak_ptr::lock` | 尝试取得临时强引用 | Finalize 后句柄安全失效 | `expired()` 后再取裸指针 |
| `shared_ptr<const Plan>` | 共享不可变快照 | 锁外发送仍看到一致路由 | 误以为 const 能保护 writer 内部 |
| `span<const byte>` | 非拥有连续视图 | 避免强制 payload 复制 | 保存到异步任务造成悬空 |
| `lock_guard` | 词法作用域持锁 | 维护短小的状态事务 | 锁内网络 I/O 或用户回调 |
| `unique_lock` | 可解锁并支持 wait | 实现停止屏障 | 无谓替代简单 lock_guard |
| `call_once` | 选出唯一执行者 | 幂等关闭 | 把可能永久阻塞的逻辑藏在其中 |
| `memory_order` | 规定原子可见性 | 发布单字段状态 | 试图替代多字段互斥事务 |

学习这些语法时，不应只记“怎么写”。每出现一个智能指针，都要问谁是所有权根；每出现一个锁，都要写出它保护的不变量；每出现一个 view，都要标注有效期；每出现一个后台线程，都要找到停止请求、唤醒、join 和最终资源释放四个动作。

## 性能与取舍

| 机制 | 收益 | 成本与边界 |
|---|---|---|
| facade 持有 `weak_ptr` | 全局关闭后句柄安全失效 | 每次调用需要一次原子引用计数操作 |
| Gate 持有 `shared_ptr` | 所有权根清晰，快照可跨越解锁 | 高频创建销毁会产生控制块开销 |
| 锁内复制、锁外回调 | 避免重入死锁和长临界区 | 注销与在途回调之间需要定义语义 |
| 多 transport strategy | 同一 API 适应本机大数据和远端通信 | 协商、监控和关闭路径更复杂 |
| 借用 payload view | 避免无条件复制 | 生命周期契约必须非常明确 |

## 复杂度、内存和可行性预算

设本进程 Publisher 数为 `P`，某 topic 的本地实体数为 `K`，一个 Publisher 的已知订阅端点数为 `N`，实际发送目标数为 `F`，payload 大小为 `S`：

| 路径 | 时间复杂度 | 持有空间 | 需要测量的常数 |
|---|---:|---:|---|
| Gate 按 topic 查找 | 平均 `O(1+K)`，最坏 `O(P)` | 哈希表 `O(P)` | 哈希、字符串分配、shared_ptr 增减 |
| 发现变更重建计划 | `O(N)` | 新旧计划短时 `O(N)` | writer 创建、策略过滤、锁等待 |
| 单次 Send 扇出 | `O(F)` 加 I/O | frame 与各层队列 | 序列化、复制、系统调用、反压 |
| Subscriber 快照 | `O(K)` | 临时 shared_ptr 数组 `O(K)` | 引用计数争用 |
| Finalize | `O(P + Q + T)` 加等待 | 实体快照 | 最慢回调、阻塞 I/O、join |

其中 `Q` 是 Subscriber/Service 等其他实体数量，`T` 是 transport worker 数。平均吞吐不能证明可用于机器人数据链路；至少要观察 Send p50/p99、发现到可发送的收敛时间、payload 数据年龄、各层 drop/overwrite、Gate 锁等待、在途回调数和 Finalize 最坏耗时。

内存预算不能只算 payload。还包括每实体控制块、topic/类型字符串、每端点能力与 writer、SHM 多槽缓冲、TCP/UDP 内核缓冲、不可变旧计划以及慢消费者队列。若 SHM 为每个 Publisher 保留 `B` 个容量 `Smax` 的槽位，其主项已经是 `P × B × Smax`；再加一次 Subscriber 持有借用导致的槽位延迟归还，可能比 Gate 元数据大几个数量级。

可行性判断应按消息类别拆开：图像和点云可接受 latest/overwrite，但要求控制内存与数据年龄；命令与状态转换通常不能静默丢弃，需要确认或应用层序号；日志允许批量与延迟；急停不能依赖一个可能排队的普通 pub/sub 回调。eCAL 提供传输机制，业务仍要为每类数据选择可靠性、期限和失效策略。

## 最小复刻的完成标准

一个教学实现至少应证明：同 topic 可注册多个实体；关闭 Gate 后旧 facade 返回失败而不崩溃；回调可安全注销自身；慢回调不阻塞其他 topic 的注册；传输层替换不改变 Publisher API；关闭后没有后台线程继续访问实体表。做到这些，才算复刻了 eCAL 对象模型的核心，而不只是写了一个 `map<string, callback>`。

实现顺序可以固定为八步：单 transport 的同步 Send；按 ID/topic 双索引 Gate；弱 facade 与安全失效；锁外实体快照；registration 软状态输入；不可变多层 SendPlan；回调停止屏障；进程级启动和逆序关闭。每一步先把失败与关闭做完整，再增加下一层并发，避免最后同时调试发现、I/O、所有权和析构竞态。
