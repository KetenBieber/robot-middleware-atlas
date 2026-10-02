# eCAL Callback 重入与 Quiescence：为什么用户代码不能运行在内部锁里

本文固定到 Eclipse eCAL 提交 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

[Subscriber 接收链](subscriber-delivery.md) 已经说明一条样本怎样从 `CSubGate` 进入 `CSubscriberImpl::ApplySample()`，以及 receive callback、latest-value `Read()`、SHM/UDP/TCP 三条 reader path 如何汇合。真正进入 Runtime 设计层以后，还需要继续追一个比“callback 会不会慢”更危险的问题：

> **中间件能不能在持有自己的内部 mutex 时直接执行任意用户 callback？**

从 C++ 语言层面看，callback 只是一个 `std::function`。从 Runtime 角度看，它却是一段几乎完全不受控制的外部程序：

```text
callback may:
  run for 10 us
  run for 100 ms
  allocate memory
  block on I/O
  call middleware APIs
  remove itself
  destroy its owner
  acquire application locks
  trigger another callback
```

因此“调用 callback”不是普通函数调用，而是一个 **control boundary**。

一旦内部锁跨过这条边界，就要同时面对：

- reentrancy；
- lock-order inversion；
- self-deadlock；
- unregister 与 in-flight callback 的竞态；
- callback 对象生命周期；
- metadata snapshot；
- teardown quiescence；
- borrowed payload lifetime。

eCAL 固定源码恰好同时展示了这些问题。

---

# 1. 先建立对象所有权图

现代 `eCAL::CSubscriber` facade 自己并不强持有 `CSubscriberImpl`。

固定源码：

```cpp
class CSubscriber
{
  // ...
private:
  std::weak_ptr<CSubscriberImpl>
      m_subscriber_impl;
};
```

构造时先创建：

```cpp
auto subscriber_impl =
    std::make_shared<CSubscriberImpl>(
        data_type_info_,
        BuildReaderAttributes(
            topic_name_, config),
        global_context);

m_subscriber_impl =
    subscriber_impl;
```

随后注册进 `CSubGate`：

```cpp
auto subgate = g_subgate();

if (subgate) {
  subgate->Register(
      topic_name_,
      subscriber_impl);
}
```

而 `CSubGate` 内部保存：

```cpp
using TopicNameSubscriberMapT =
    std::unordered_multimap<
        std::string,
        std::shared_ptr<
            CSubscriberImpl>>;
```

所以真正的 ownership 更接近：

```text
CSubscriber facade
   |
   | weak_ptr
   v
CSubscriberImpl
   ^
   |
   | shared_ptr
CSubGate registry
```

这已经暗示一个重要事实：

> facade 是否还存在，与 Impl 是否还活着，不是同一个问题。

---

# 2. CSubGate 已经使用了正确的 registry snapshot 思路

收到样本以后，`CSubGate::ApplySample()` 没有拿着全局 topic registry 锁一直执行 Subscriber。

它先复制匹配对象的强引用：

```cpp
std::vector<
    std::shared_ptr<CSubscriberImpl>>
        readers_to_apply;

{
  const std::shared_lock<
      std::shared_timed_mutex>
      lock(
        m_topic_name_subscriber_mutex);

  auto res =
      m_topic_name_subscriber_map
        .equal_range(
            topic_info_.topic_name);

  std::transform(
      res.first,
      res.second,
      std::back_inserter(
          readers_to_apply),
      [](const auto& match) {
        return match.second;
      });
}
```

然后离开 registry lock，再调用：

```cpp
for (const auto& reader
     : readers_to_apply)
{
  applied_size =
      reader->ApplySample(...);
}
```

这是一种非常值得保留的设计：

```text
lock registry
    |
    v
snapshot stable owners
    |
    v
unlock registry
    |
    v
execute object-local work
```

它同时解决两件事：

1. 遍历过程中 registry 可以继续演化；
2. 已经被 snapshot 的对象通过 `shared_ptr` 保持存活。

但是：

> **snapshot lifetime safety 不是 callback quiescence。**

后面会反复看到这一区别。

---

# 3. Receive callback 的真实锁域

`CSubscriberImpl::ApplySample()` 一进入函数就取得：

```cpp
const std::lock_guard<std::mutex>
    lock(
      m_receive_callback_mutex);
```

这个 `lock_guard` 的作用域一直到整个 `ApplySample()` 返回。

在 callback 分支中，又取得第二把锁：

```cpp
const std::lock_guard<std::mutex>
    exec_lock(
      m_connection_map_mtx);

(m_receive_callback)(
    topic_id,
    m_connection_map[pub_info]
      .data_type_info,
    cb_data);
```

因此真正的锁链是：

```text
m_receive_callback_mutex
        |
        v
m_connection_map_mtx
        |
        v
arbitrary user receive callback
```

这不是“callback mutex 保护一下 `std::function`”这么简单。

用户代码执行的整个时间，都位于两个 Runtime 内部锁的临界区中。

---

# 4. 为什么这会造成 self-deadlock

`RemoveReceiveCallback()` 同样需要：

```cpp
const std::lock_guard<std::mutex>
    lock(
      m_receive_callback_mutex);

m_receive_callback = nullptr;
```

现在假设业务 callback 想一次性处理某个命令，然后立刻取消自己：

```cpp
subscriber.SetReceiveCallback(
    [&](...) {
      HandleOnce();
      subscriber
        .RemoveReceiveCallback();
    });
```

真实调用链：

```text
ApplySample()
  lock m_receive_callback_mutex
    |
    v
  invoke user callback
    |
    v
  RemoveReceiveCallback()
    |
    v
  lock m_receive_callback_mutex
```

当前线程正在等待自己释放一把普通、非递归 `std::mutex`。

但只有 callback 返回以后：

```text
callback returns
→ ApplySample returns
→ lock_guard destructs
→ mutex unlock
```

所以形成闭环：

```text
callback waits for mutex
mutex waits for callback return
```

这就是经典 **self-deadlock**。

---

# 5. SetReceiveCallback 同样不能在当前 callback 内安全调用

现代 API 提供：

```cpp
void CSubscriber::SetReceiveCallback(
    ReceiveCallbackT callback_);
```

内部最终也会调用：

```cpp
CSubscriberImpl::
    SetReceiveCallback(...)
```

而 setter 取得同一把：

```text
m_receive_callback_mutex
```

所以以下逻辑也存在同样问题：

```text
callback A
  |
  v
SetReceiveCallback(B)
  |
  v
tries to re-lock
m_receive_callback_mutex
```

也就是说这里真正不安全的不是某一个 Remove API，而是：

> **用户 callback 不能重入任何需要同一内部锁的 callback-control API。**

---

# 6. legacy v5 Destroy 甚至会把“自销毁”也带进同一死锁

v5 facade 的 `Destroy()` 先做：

```cpp
RemReceiveCallback();
```

之后才：

```cpp
subgate->Unregister(...);
m_subscriber_impl.reset();
```

因此如果 receive callback 内直接调用：

```cpp
subscriber.Destroy();
```

第一步就进入：

```text
RemReceiveCallback
→ RemoveReceiveCallback
→ lock receive_callback_mutex
```

同样自死锁。

从应用层看只是“收到停止命令以后销毁 Subscriber”，从 Runtime 锁图看却是：

```text
data-plane callback
    |
    v
control-plane teardown
    |
    v
same callback mutex
```

这正是 reentrancy 设计必须提前考虑的场景。

---

# 7. 为什么不能简单换成 recursive_mutex

一个看似直接的补丁是：

```cpp
std::recursive_mutex
    receive_callback_mutex;
```

这样同一线程可以再次取得 mutex。

但它只掩盖了最表面的 self-deadlock。

## 7.1 用户代码仍然运行在 Runtime 内部锁域

callback 还是会长时间占用内部锁。

只要 callback：

```text
does inference
blocks on network
waits another application mutex
```

transport / registration 路径仍被拖住。

## 7.2 lock-order inversion 仍然存在

假设业务层已有：

```text
AppMutex
```

线程 A：

```text
lock RuntimeMutex
→ callback
→ wait AppMutex
```

线程 B：

```text
lock AppMutex
→ middleware API
→ wait RuntimeMutex
```

形成：

```text
RuntimeMutex -> AppMutex
AppMutex     -> RuntimeMutex
```

这与 mutex 是否 recursive 无关。

## 7.3 recursive mutex 让 ownership 边界更难读

源码读者必须额外推理：

```text
当前 recursion depth 是多少？
哪个 API 真正释放最后一层锁？
```

对于基础 Runtime，通常应该减少这种隐式嵌套，而不是增加。

---

# 8. 但当前长锁域其实偷偷提供了一个强保证

如果另一个线程调用：

```cpp
RemoveReceiveCallback();
```

而 receive callback 正在运行，Remove 会阻塞在：

```text
m_receive_callback_mutex
```

直到 callback 返回。

随后 Remove 获得锁、清空 `std::function`、再返回。

于是，在没有并发重新安装新 callback 的前提下，当前实现隐含提供了一个很强的语义：

> **RemoveReceiveCallback 返回时，之前已经进入的 receive callback 已经执行完。**

这不是普通“从 registry 删除”。

这是一个 **quiescence barrier**。

---

# 9. 所以“锁内 copy callback，锁外执行”不是全部答案

最自然的重构是：

```cpp
ReceiveCallbackT callback;

{
  std::lock_guard lock(
      callback_mutex);

  callback =
      m_receive_callback;
}

if (callback) {
  callback(...);
}
```

这会立即解决：

```text
self remove
self replace
long user code holds callback mutex
```

但是会改变 Remove 的语义。

一种交错：

```text
Treceive:
  lock
  copy callback
  unlock

Tcontrol:
  lock
  callback = nullptr
  unlock
  Remove returns

Treceive:
  invoke copied callback
```

于是：

```text
Remove has returned
but old callback starts afterwards
```

如果调用方原本依赖：

```text
Remove returns
=> no callback can touch my state anymore
```

就会出现新的生命周期 bug。

因此：

> **lock-free callback invocation 与 quiescent unregister 是两个不同问题。**

---

# 10. 先把三个概念彻底分开

## 10.1 Lifetime safety

对象是否还活着？

例如 `CSubGate::ApplySample()` 通过：

```text
shared_ptr snapshot
```

保证当前 invocation 持有的 `CSubscriberImpl` 不会析构。

## 10.2 Logical unregister

对象是否还能被未来新请求发现？

例如：

```text
CSubGate::Unregister
```

从 topic registry 删除 entry。

## 10.3 Quiescence

已经开始或已经取得执行资格的旧请求是否全部退出？

例如：

```text
all in-flight callbacks == 0
```

只有第三个条件成立，才能说真正进入静默期。

三者必须分别证明。

---

# 11. CSubGate::Unregister 只解决未来 discovery

固定代码：

```cpp
const std::unique_lock<
    std::shared_timed_mutex>
    lock(
      m_topic_name_subscriber_mutex);

auto res =
    m_topic_name_subscriber_map
      .equal_range(topic_name_);

for (auto iter = res.first;
     iter != res.second;
     ++iter)
{
  if (iter->second == datareader_)
  {
    m_topic_name_subscriber_map
      .erase(iter);

    ret_state = true;
    break;
  }
}
```

如果另一个 receive thread 已经更早执行完：

```text
registry lookup
→ shared_ptr copied into readers_to_apply
→ registry lock released
```

那么之后的 `Unregister()` 无法把那个局部 `shared_ptr` 从另一线程栈里拿走。

所以：

```text
Unregister returns
```

只意味着：

```text
future registry lookup
will not discover this entry
```

不意味着：

```text
all previous ApplySample calls
have returned
```

---

# 12. 现代 CSubscriber 析构不是 receive callback 的天然 quiescence barrier

现代 facade 析构：

```cpp
CSubscriber::~CSubscriber()
{
  auto subscriber_impl =
      m_subscriber_impl.lock();

  auto subgate = g_subgate();

  if (subgate &&
      subscriber_impl)
  {
    subgate->Unregister(
        subscriber_impl
          ->GetTopicName(),
        subscriber_impl);
  }
}
```

它没有先执行：

```text
RemoveReceiveCallback
```

也没有等待：

```text
in_flight == 0
```

所以可能出现：

```text
Treceive:
  CSubGate snapshots shared_ptr
  unlocks registry

Tdestroy:
  ~CSubscriber()
  Unregister()
  returns

Treceive:
  shared_ptr still alive
  ApplySample()
  user callback()
```

Impl 生命周期仍然安全，因为局部 `shared_ptr` 保住了对象。

但 facade 析构与 callback 静默不是同一件事。

如果 callback 捕获了外部 owner 的裸 `this`，Runtime 的 Impl 生命周期安全也不会自动保护那个 application object。

---

# 13. v5 Destroy 与现代析构形成一个很有意思的对照

v5 `Destroy()`：

```text
RemReceiveCallback
→ Unregister
→ reset shared_ptr
```

外部控制线程调用时，第一步的 callback mutex 会等待已有 receive callback 退出。

因此它更接近：

```text
drain callback
→ remove registry entry
→ release owner
```

但如果 **callback 自己** 调用 Destroy，就会在第一步 self-deadlock。

现代 destructor：

```text
Unregister
```

不会出现同一个 receive callback mutex 的自死锁，却也没有显式 callback drain。

这说明 Runtime API 设计里不能只问：

> “Destroy 能不能返回？”

还要明确：

> “Destroy 返回时，允许旧 callback 继续跑吗？”

---

# 14. connection map 锁为什么也不应该跨用户 callback

receive callback 调用前：

```cpp
const std::lock_guard<std::mutex>
    exec_lock(
      m_connection_map_mtx);

(m_receive_callback)(
    topic_id,
    m_connection_map[pub_info]
      .data_type_info,
    cb_data);
```

这把锁保护的是：

```text
publisher identity
→ datatype
→ layer state
→ active connection state
```

Registration 更新同一张 map。

所以 callback 越慢：

```text
publisher registration update
publisher unregistration update
```

等待越久。

控制面 latency 被业务 callback 的 WCET 直接污染。

---

# 15. 为什么不能只把 connection_map_mtx 提前 unlock

当前 callback 第二个参数直接来自：

```cpp
m_connection_map[pub_info]
    .data_type_info
```

如果只是这样改：

```text
unlock map
→ pass reference into callback
```

另一线程可以：

```text
erase connection
replace SConnection
```

原引用就可能失效。

因此正确做法不是“把 unlock 往前挪”。

而是：

```text
lock
  find connection
  copy stable metadata
unlock

invoke callback(metadata_snapshot)
```

例如：

```cpp
SDataTypeInformation
    data_type_snapshot;

{
  std::lock_guard lock(
      m_connection_map_mtx);

  auto it =
      m_connection_map.find(
          pub_info);

  if (it !=
      m_connection_map.end())
  {
    data_type_snapshot =
        it->second
          .data_type_info;
  }
}

callback(
    topic_id,
    data_type_snapshot,
    cb_data);
```

这是典型：

> **snapshot mutable metadata before crossing into arbitrary user code。**

---

# 16. callback 本体同样应该 snapshot

receive callback 对象本身也应该先复制稳定版本：

```cpp
ReceiveCallbackT
    callback_snapshot;

{
  std::lock_guard lock(
      m_receive_callback_mutex);

  callback_snapshot =
      m_receive_callback;
}
```

之后内部 callback mutex 就可以释放。

现在 Runtime 锁域变成：

```text
callback mutex
  -> snapshot callback
unlock

connection mutex
  -> snapshot metadata
unlock

invoke arbitrary user code
```

这比：

```text
callback mutex
  -> connection mutex
     -> user callback
```

更容易证明。

---

# 17. 但 snapshot callback 后必须重新设计 unregister semantics

如果只 snapshot，没有 in-flight bookkeeping：

```text
T1 acquires callback snapshot
T2 removes callback and returns
T1 invokes old snapshot
```

因此必须先决定 API 需要哪一种语义。

## 17.1 Weak removal

定义：

```text
remove returns
=> no future acquisition of callback
```

但已经取得 snapshot 的 callback 允许继续。

这种实现简单。

## 17.2 Strong removal / drain

定义：

```text
remove returns
=> no callback from this registration
   is still executing or can start
```

这就需要：

```text
active flag
+
in-flight counter
+
wait/drain
```

不能只清空 `std::function`。

---

# 18. 一个 callback slot 至少需要哪些状态

教学结构：

```cpp
struct CallbackSlot
{
  std::mutex mutex;
  std::condition_variable cv;

  Callback callback;

  bool active = true;

  std::size_t in_flight = 0;

  std::uint64_t generation = 0;
};
```

每个字段回答不同问题：

```text
callback
  现在应该调用谁

active
  还能不能产生新的 invocation

in_flight
  旧 invocation 是否全部退出

generation
  当前 registration 属于哪一代
```

---

# 19. Acquire callback 的协议

接收线程：

```cpp
Invocation TryAcquire()
{
  std::lock_guard lock(mutex);

  if (!active ||
      !callback)
  {
    return {};
  }

  ++in_flight;

  return Invocation{
    callback,
    generation
  };
}
```

锁内只做：

```text
check
copy
increment counter
```

不执行用户代码。

---

# 20. callback 返回后 Release invocation

无论 callback 正常返回还是抛异常路径需要被封装，最终都要：

```cpp
void ReleaseInvocation()
{
  std::lock_guard lock(mutex);

  --in_flight;

  if (!active &&
      in_flight == 0)
  {
    cv.notify_all();
  }
}
```

这样 quiescence 变成一个显式谓词：

```text
active == false
&&
in_flight == 0
```

而不是“恰好有一把大 mutex”。

---

# 21. strong Remove 怎样等待静默期

外部控制线程：

```cpp
void DisableAndDrain()
{
  std::unique_lock lock(mutex);

  active = false;
  callback = nullptr;
  ++generation;

  cv.wait(
      lock,
      [&] {
        return in_flight == 0;
      });
}
```

这时：

```text
Remove returns
```

才真正意味着：

```text
old callback quiescent
```

---

# 22. 但 self-remove 绝不能同步等待自己

假设 callback invocation 已经计入：

```text
in_flight = 1
```

它自己调用：

```text
DisableAndDrain()
```

如果里面等待：

```text
in_flight == 0
```

永远无法成立。

因为唯一能把：

```text
1 -> 0
```

的代码就在 callback 返回以后。

于是又构造出新的 self-deadlock。

所以 self-unregister-safe API 通常必须把：

```text
disable
```

和：

```text
drain
```

拆开。

---

# 23. 两阶段 API 比一个 Remove 更诚实

例如：

```cpp
handle.disable();
```

语义：

```text
no new callback acquisition
```

它可以从 callback 自己内部调用。

另一个外部控制线程再：

```cpp
handle.wait_quiescent();
```

语义：

```text
wait until all old callbacks exit
```

或者：

```cpp
handle.close();
```

在非 callback context 中组合两步。

这种 API 比模糊的：

```text
RemoveCallback()
```

更容易表达真实生命周期。

---

# 24. 另一种实现：RegistrationHandle + shared state

可以让 registry 只持：

```cpp
std::shared_ptr<CallbackState>
```

callback invocation 获取：

```text
shared_ptr state
```

这只能保证 state 对象活着。

它仍不能替代：

```text
in_flight drain
```

因为：

```text
object alive
!=
callback disabled
!=
callback quiescent
```

`shared_ptr` 解决 ownership，不解决 synchronization protocol。

---

# 25. immutable callback snapshot 适合 read-mostly 场景

如果 callback 更新很少、调用很多，可以把当前 callback entry 做成：

```cpp
std::shared_ptr<
    const CallbackEntry>
```

调用侧：

```cpp
auto snapshot =
    std::atomic_load(
        &callback_entry);
```

更新侧：

```cpp
std::atomic_store(
    &callback_entry,
    new_entry);
```

好处：

- reader 热路径不拿 callback mutex；
- 每次 invocation 获得稳定 callback 对象；
- 旧版本生命周期由 `shared_ptr` 自动延长。

但是：

> 旧 entry 活着，仍然不等于旧 callback 已经停止执行。

需要 strong remove 时仍要：

```text
in-flight
epoch
RCU grace period
or explicit drain
```

---

# 26. event callback 里还有第二类问题：check-before-lock

`CSubscriberImpl::FireEvent()`：

```cpp
if (m_event_id_callback)
{
  // build callback data...

  const std::lock_guard<std::mutex>
      lock(
        m_event_id_callback_mutex);

  m_event_id_callback(
      topic_id,
      data);
}
```

注意顺序：

```text
read std::function
    |
    v
later lock callback mutex
    |
    v
invoke std::function
```

而 setter/remover：

```text
lock callback mutex
→ mutate std::function
```

这比“锁内调用用户代码”又多了一层问题。

---

# 27. std::function 的并发读写不是靠内部 mutex 自动修好的

线程 A：

```text
if (m_event_id_callback)
```

线程 B：

```text
lock event mutex
m_event_id_callback = nullptr
```

线程 A 的 bool conversion 发生在锁外。

也就是说：

```text
reader
does not participate
in writer's mutex protocol
```

这不是合法的同步关系。

`std::function` 不是一个可以一边赋值、一边无锁检查的 atomic callback slot。

---

# 28. 即使忽略 data race，也存在 TOCTOU

用理想化顺序看：

```text
Tfire:
  check callback != null

Tremove:
  lock
  callback = null
  unlock

Tfire:
  lock
  callback(...)
```

检查的是旧状态，调用的是新状态。

所以：

```text
check
then
lock
then
use
```

本身就不是稳定快照。

这类模式应当改成：

```text
lock
  copy callback
unlock

if snapshot:
  invoke snapshot
```

而不是：

```text
if callback
  lock
  invoke callback
```

---

# 29. v5 event adapter 又增加一层 callback-under-lock

legacy adapter 保存：

```cpp
std::map<
  eSubscriberEvent,
  v5::SubEventCallbackT>
    m_event_callback_map;

std::mutex
    m_event_callback_map_mutex;
```

core 只注册一只 internal callback。

internal callback：

```cpp
const std::lock_guard<std::mutex>
    guard(
      m_event_callback_map_mutex);

const auto& v5_callback =
    m_event_callback_map.find(
        callback_data_.event_type);

if (v5_callback !=
        m_event_callback_map.end() &&
    v5_callback->second != nullptr)
{
  auto data =
      v6tov5CallbackData(...);

  v5_callback->second(
      topic_id_.topic_name
        .c_str(),
      &data);
}
```

也就是说用户 v5 event callback 在：

```text
m_event_callback_map_mutex
```

内部执行。

---

# 30. v5 event callback 自移除同样会 self-deadlock

`RemoveEventCallback(type)`：

```cpp
const std::lock_guard<std::mutex>
    guard(
      m_event_callback_map_mutex);

m_event_callback_map[type_] =
    nullptr;
```

所以：

```text
internal_callback
  locks event_callback_map_mutex
    |
    v
  user callback
    |
    v
  RemEventCallback(type)
    |
    v
  tries same mutex
```

又形成：

```text
callback waits for itself
```

---

# 31. 实际锁链甚至是两层 callback mutex

core `FireEvent()` 先取得：

```text
m_event_id_callback_mutex
```

调用 legacy internal callback。

internal callback 再取得：

```text
m_event_callback_map_mutex
```

然后进入用户代码。

因此 v5 subscriber event callback 的锁链是：

```text
core event mutex
    |
    v
adapter map mutex
    |
    v
user event callback
```

如果 callback 重入控制 API，必须分析两把锁，而不是只看 adapter。

---

# 32. Publisher legacy event callback 使用了同一个结构

固定版本的 `CPublisherImpl::FireEvent()` 同样是：

```text
if callback
→ lock event callback mutex
→ invoke
```

v5 `CPublisherEventCallbackAdapater` 也同样：

```text
lock callback map
→ invoke user callback
```

因此这不是一个孤立 Subscriber 特例。

它暴露的是一个可迁移的 Runtime anti-pattern：

> **callback storage mutex 被同时拿来做 callback execution mutex。**

存储同步与执行生命周期不应该自动等价。

---

# 33. receive callback mutex 现在承担了三个职责

当前 `m_receive_callback_mutex` 同时负责：

```text
1. protect std::function mutation

2. serialize same-subscriber
   delivery

3. make Remove wait for
   current callback completion
```

这就是为什么“直接缩短临界区”会影响语义。

更好的设计是把三个职责拆成三个机制：

```text
callback slot lock
delivery ordering policy
in-flight quiescence
```

然后分别推理。

---

# 34. 同一 Subscriber 是否必须串行交付，是独立策略

当前整个 `ApplySample()` 被：

```text
m_receive_callback_mutex
```

串行化。

这意味着 UDP、TCP、SHM 同时到达同一 Subscriber 时，只能一个接一个进入完整处理链。

如果 Runtime 需要保证：

```text
same Subscriber callback
never concurrent
```

可以单独设置：

```text
delivery strand
serial executor
per-subscriber queue
```

而不是借用“保护 callback std::function 的 mutex”顺便实现。

职责分离以后，代码意图会清楚很多。

---

# 35. Strand / serial executor 是另一种设计

可以让不同 transport thread 只做：

```text
parse
dedup
enqueue delivery record
```

真正 callback 统一由：

```text
SubscriberSerialExecutor
```

串行执行。

这样得到：

```text
no concurrent callback reentry
```

但不会让 UDP/SHM transport thread 长时间持 Subscriber 内部 mutex。

代价是：

- 多一次 queue；
- 额外 scheduler hop；
- payload ownership 必须延长；
- latency model 改变。

所以是否采用，取决于系统需求。

---

# 36. borrowed payload 决定了“异步执行”不能随便加

当前 `ApplySample()` 收到：

```cpp
const char* payload_;
```

它不是 owning buffer。

callback data：

```cpp
cb_data.buffer =
    static_cast<const void*>(
        payload_);
```

所以若只是：

```text
在 ApplySample 内
锁外同步 invoke callback
```

通常仍处于原始调用栈的 payload lifetime 中。

但如果改成：

```text
enqueue callback to another thread
return ApplySample immediately
```

原 `payload_` 可能已经失效。

此时必须：

- copy payload；
- decode 成自有对象；
- 或取得真正的 loan / buffer lease。

因此：

> **unlock-before-callback 与 async-callback 是两种完全不同的重构。**

前者不必自动引入 payload copy；后者通常需要重新设计 ownership。

---

# 37. SHM zero-copy 会让 callback WCET 影响更外层资源

SHM 路径可能在 observer 层持有共享内存槽位对应的同步对象，再一路进入：

```text
SubGate
→ SubscriberImpl
→ user callback
```

即使先移除 Subscriber 内部 mutex，慢 callback 仍可能延长 SHM buffer 的借用时间。

所以：

```text
don't hold internal callback mutex
```

并不等于：

```text
callback no longer affects transport latency
```

还要继续审计 outer resource lifetime。

---

# 38. metadata snapshot 和 payload lease 应分别设计

callback 需要两类输入：

```text
metadata
  topic id
  datatype info
  timestamps
  counters

payload
  actual bytes / SHM view
```

metadata 通常较小，可以：

```text
copy under lock
```

payload 可能很大，应通过：

```text
borrowed view
shared buffer
loan handle
reference-counted block
```

单独管理。

不要因为大 payload 不想复制，就把所有 metadata mutex 也一起跨 callback 持有。

---

# 39. 一个更清晰的 receive delivery pipeline

可以把流程拆成：

```text
ApplySample
   |
   v
validate layer
dedup/filter
update statistics
   |
   v
snapshot callback
snapshot datatype
acquire payload view
   |
   v
release internal metadata locks
   |
   v
mark invocation in-flight
   |
   v
invoke user code
   |
   v
release invocation
```

每一步都有明确 ownership。

---

# 40. callback invocation token

一种教学结构：

```cpp
class InvocationToken
{
 public:
  InvocationToken(
      std::shared_ptr<State> state,
      Callback cb)
      : state_(std::move(state)),
        callback_(
          std::move(cb))
  {
  }

  ~InvocationToken()
  {
    state_->ReleaseOne();
  }

  Callback&
  callback()
  {
    return callback_;
  }

 private:
  std::shared_ptr<State>
      state_;

  Callback callback_;
};
```

获取 token 时：

```text
in_flight++
```

析构时自动：

```text
in_flight--
```

这样异常路径也更容易维持计数不变量。

---

# 41. generation 为什么有价值

只用：

```text
active bool
```

在快速 remove/re-add 中不容易区分：

```text
old callback A
new callback B
```

generation：

```text
A = generation 10
remove
B = generation 11
```

可以让日志、drain 和控制协议知道：

```text
which registration
this invocation belongs to
```

尤其适合热重载和动态 graph。

---

# 42. self-remove-safe 的精确定义

一个实用协议可以定义：

## disable()

```text
after disable linearization point:
  no new invocation of this generation
  may be acquired
```

允许旧 invocation 完成。

## wait_quiescent()

```text
wait until all invocations
of retired generation exit
```

不能由同一 generation 的当前 callback 同步等待自己。

## close()

```text
disable
+
wait_quiescent
```

只允许在非 callback execution context 使用，或者内部识别 self-context 并退化为 deferred drain。

这比一个语义模糊的 `RemoveCallback()` 更容易验证。

---

# 43. self-removal 为什么天然偏向 deferred reclamation

callback 自己宣布：

```text
I am retired
```

是合理的。

但它不能同时证明：

```text
I have already returned
```

所以 self-removal 本质上更接近：

```text
logical retirement now
physical/quiescent reclamation later
```

这和：

- RCU；
- hazard pointer retirement；
- epoch reclamation；
- event-loop deferred delete；

背后是同一类思想。

---

# 44. CSubGate snapshot 已经是这种思路的一半

`CSubGate` 做了：

```text
lookup under lock
→ copy strong owners
→ unlock
→ invoke
```

这本身就是：

```text
stable read snapshot
```

但缺少：

```text
retirement generation
in-flight callback drain
```

所以它解决的是：

```text
object lifetime
```

而不是：

```text
callback retirement
```

这两个概念正好可以从同一个源码里对照学习。

---

# 45. registry lock 与 callback lock 应有明确边界

一个 Runtime 最危险的结构通常是：

```text
global registry lock
→ object lock
→ user callback
```

因为任意 callback 都可能：

```text
register another object
remove current object
query registry
destroy parent
```

如果每层都把用户代码放在锁里，很容易形成不可见的 lock graph。

更健康的结构：

```text
global registry
  snapshot owner
unlock

object metadata
  snapshot state
unlock

callback slot
  acquire invocation
unlock

user code
```

这样 lock graph 在进入用户代码前被截断。

---

# 46. event callback 的正确 snapshot 顺序

固定源码是：

```text
check callback
→ lock
→ call live callback object
```

更稳健的是：

```cpp
SubEventCallbackT
    callback_snapshot;

{
  std::lock_guard lock(
      m_event_id_callback_mutex);

  callback_snapshot =
      m_event_id_callback;
}

if (callback_snapshot)
{
  callback_snapshot(
      topic_id,
      data);
}
```

至少先解决：

- lock-free `std::function` read；
- check/use TOCTOU；
- user callback under core event mutex。

若还需要 strong Remove，再叠加 in-flight protocol。

---

# 47. legacy adapter map 同样要 snapshot

不要：

```text
lock map
→ find
→ invoke callback
```

而是：

```cpp
v5::SubEventCallbackT
    callback_snapshot;

{
  std::lock_guard lock(
      m_event_callback_map_mutex);

  auto it =
      m_event_callback_map.find(type);

  if (it !=
      m_event_callback_map.end())
  {
    callback_snapshot =
        it->second;
  }
}

if (callback_snapshot)
{
  callback_snapshot(...);
}
```

这样用户 callback 可以安全调用：

```text
RemoveEventCallback
AddEventCallback
```

至少不会因为同一 map mutex 自锁。

---

# 48. 如果需要强 event-callback quiescence，也必须单独 drain

snapshot 以后，同样会出现：

```text
T1 copies event callback

T2 removes event callback
Remove returns

T1 invokes old snapshot
```

所以 receive callback 和 event callback 最终都归结到同一个模型：

```text
snapshot for reentrancy safety

+
in-flight accounting
for strong quiescence
```

---

# 49. FireDroppedEvent 说明用户 callback 可能出现在更深的锁链里

`GetMessageDropsAndFireDroppedEvents()` 固定源码先取得：

```cpp
m_message_drop_map_mutex
```

再取得：

```cpp
m_connection_map_mtx
```

随后在循环里：

```cpp
FireDroppedEvent(
    publisher_info,
    m_connection_map[
      publisher_info]
      .data_type_info);
```

`FireDroppedEvent()` 最终进入：

```text
event callback
```

所以 dropped-event 用户代码外层至少还可能存在：

```text
message-drop mutex
→ connection-map mutex
→ event-callback mutex
→ user code
```

固定源码在这里直接留下了说明：dropped event 不应继续在当前调用线程的深锁链中触发，而应考虑排队后再处理。这个注释本身已经表明，event firing 与持锁统计路径的职责并不适合绑在一起。

---

# 50. 但“换线程”不是唯一方案

把 event callback 全部扔到另一个线程确实能切断当前锁链。

但会引入：

- queue ordering；
- callback payload ownership；
- shutdown drain；
- thread lifecycle；
- event loss/backpressure；
- scheduler latency。

很多情况下，更小的第一步只是：

```text
copy event data + callback
under lock
→ unlock
→ synchronous callback
```

只有业务确实要求隔离 transport/control-plane latency 时，再引入异步 executor。

---

# 51. Runtime 设计要显式写出 callback contract

至少应该回答：

```text
Callback may call RemoveCallback?

Callback may destroy its Subscriber?

Callbacks for one Subscriber
can run concurrently?

RemoveCallback returns before
old callback exits?

Destructor waits for callbacks?

Callback payload valid for how long?

Event ordering guaranteed?
```

这些不是文档细节。

它们决定内部：

- mutex；
- queue；
- refcount；
- generation；
- drain；
- payload lease；

应该怎样设计。

---

# 52. “线程安全”这个词太粗

某个 callback API 可以做到：

```text
no data race
```

但仍然：

```text
self-deadlock
```

也可以做到：

```text
no self-deadlock
```

但仍然：

```text
Remove returns while old callback runs
```

还可以做到：

```text
object lifetime safe
```

但 application capture 已经销毁。

所以应该分别检查：

```text
data-race freedom
reentrancy safety
lifetime safety
logical removal
quiescence
ordering
payload validity
```

---

# 53. 为什么 shared_ptr 不能替代 quiescence

eCAL 的 `readers_to_apply` 已经很好地证明：

```text
shared_ptr snapshot
```

可以让 Impl 在 callback 执行期间存活。

但如果应用写：

```cpp
class CameraNode
{
  eCAL::CSubscriber sub_;

  void Start()
  {
    sub_.SetReceiveCallback(
      [this](...) {
        UseMembers();
      });
  }
};
```

Runtime 能保证：

```text
CSubscriberImpl alive
```

不代表：

```text
CameraNode this alive
```

如果 owner 正在析构，而旧 callback 仍能启动，仍然存在 application lifetime risk。

真正解决它的是：

```text
callback quiescence
or
weak application capture
or
shared application state
```

不是中间件内部的一个 shared_ptr。

---

# 54. Destructor 是否应该 drain，要由 API contract 决定

两种设计都可能合理。

## 54.1 Non-blocking destructor

```text
unregister now
old in-flight work may finish later
```

优点：

- teardown 不容易长时间阻塞；
- event-loop 风格自然。

要求：

- callback state 独立拥有；
- application capture 不能依赖 facade lifetime；
- 文档明确语义。

## 54.2 Draining destructor

```text
disable callback
unregister
wait in-flight == 0
return
```

优点：

```text
destructor return
=> callback silence
```

但必须处理：

- callback self-destruction；
- indefinite slow callback；
- shutdown deadlock；
- timeout/cancel policy。

不能只凭直觉选择。

---

# 55. self-destruction 是最难的一类 teardown

如果 callback 内：

```text
delete owner
```

而 owner destructor 想：

```text
wait current callback exit
```

就是：

```text
destructor waits callback
callback is destructor caller
```

天然闭环。

常见解决方向：

- owner 用 `shared_from_this` 延长到 callback 返回；
- callback capture `weak_ptr`；
- destructor 只 logical-disable，不同步 drain；
- 真正资源回收 deferred 到 executor；
- 明确禁止 callback 内销毁 owner。

无论选择哪一种，都必须成为显式 contract。

---

# 56. 对机器人软件，quiescence 比 Web callback 更重要

机器人 Runtime 中 callback 可能操作：

- actuator command buffer；
- trajectory state；
- perception tensor；
- shared image pool；
- EtherCAT process image；
- GPU/SHM loan；
- safety state machine。

如果 teardown 返回以后旧 callback 仍继续访问：

```text
motor controller state
```

后果不只是一次陈旧 UI 更新。

所以：

> **动态重配置、模式切换和停机路径必须把 callback quiescence 当成一等问题。**

---

# 57. 一个可迁移的 callback-state 模型

可以把 callback Runtime 拆成四层：

```text
Registration State
  ACTIVE
  RETIRED

Invocation State
  in_flight count
  generation

Callable Snapshot
  stable std::function
  or immutable callback entry

Payload / Metadata Lease
  stable callback arguments
```

任何一次 callback 都必须拿到：

```text
valid registration generation
+
stable callable
+
valid argument lifetime
+
invocation ownership
```

---

# 58. 一条完整 invocation 的推荐流程

```text
transport thread
    |
    v
locate SubscriberImpl
(shared_ptr snapshot)
    |
    v
validate / dedup
    |
    v
snapshot metadata
    |
    v
acquire callback invocation
(in_flight++)
    |
    v
release Runtime internal locks
    |
    v
invoke user callback
    |
    v
release invocation
(in_flight--)
```

这样任意用户代码不再位于：

```text
registry mutex
callback mutex
connection map mutex
```

之内。

---

# 59. 一条完整 unregister 的推荐流程

弱语义：

```text
mark RETIRED
remove from future lookup
return
```

强语义：

```text
mark RETIRED
remove from future lookup
wait in_flight == 0
return
```

self-remove：

```text
mark RETIRED
return from callback
last invocation release
signals quiescence
```

调用者若还要等待完全静默，应在 callback 外部等待。

---

# 60. 为什么顺序通常是 retire before drain

如果先等：

```text
in_flight == 0
```

但仍允许新 invocation 进入，可能永远等不到 0。

所以必须先关闭入口：

```text
ACTIVE -> RETIRED
```

然后才：

```text
wait in_flight drain
```

这和服务 shutdown 的：

```text
stop accepting new work
→ drain existing work
→ free resources
```

完全同构。

---

# 61. registry unregister 也应先于最终 reclamation

一般生命周期：

```text
disable callback acquisition
        |
        v
remove registry visibility
        |
        v
drain in-flight
        |
        v
destroy callback state
        |
        v
destroy object
```

具体前两步谁先，取决于是否允许当前对象继续 receive-but-buffer，不应机械照搬。

核心是：

> 新 work 的入口必须在等待 quiescence 之前被关掉。

---

# 62. 同步 remove 与异步 retire 可以同时存在

可以提供：

```cpp
handle.disable();
```

快速、self-safe。

再提供：

```cpp
handle.wait();
```

给控制线程。

或者：

```cpp
auto future =
    handle.close_async();
```

由 event loop 在最后一只 invocation 退出时完成 future。

这比让 callback 自己同步等待更加自然。

---

# 63. 可以怎样验证一个 callback Runtime

判断 callback Runtime 是否可靠，最有效的方法之一是把关键并发时序逐条展开。

## 63.1 Self-remove

```text
callback entered
→ RemoveCallback
```

是否自锁？

## 63.2 Concurrent remove

```text
T1 acquired callback
T2 remove
T1 invoke
```

Remove 返回语义是什么？

## 63.3 Remove before invoke

```text
T1 snapshot
T2 remove returns
T1 invokes old callback
```

允许吗？

## 63.4 Destroy during callback

```text
callback running
owner destructor
```

谁保持谁的 lifetime？

## 63.5 Re-add generation

```text
remove A
add B
late A completion
```

是否会污染 B 的状态？

这些交错比一句“mutex protected”更能说明正确性。

---

# 64. 对固定 eCAL 源码的结论边界

固定版本已经做得好的地方：

```text
CSubGate registry:
  shared_mutex

ApplySample:
  copies shared_ptr owners
  before invoking object work

connection map:
  protected as compound state

receive callback:
  same Subscriber serialized
```

这些机制都很有价值。

真正需要额外注意的是：

```text
receive callback:
  invoked while holding
  receive callback mutex
  and connection map mutex

event callback:
  checked before callback mutex

event callback:
  invoked while holding
  callback mutex

v5 event adapter:
  user callback invoked under
  callback-map mutex

modern facade destructor:
  unregisters registry entry
  but does not explicitly drain
  prior ApplySample snapshots
```

这几件事属于同一个问题族：

> **callback execution、callback storage synchronization 和 callback retirement 被耦合在了少数几把 mutex 上。**

---

# 65. 一个最小教学版 CallbackSlot

下面不是 eCAL 源码，而是把前面的原则压缩成一个最小结构：

```cpp
class CallbackSlot
{
 public:
  using Callback =
      std::function<void(
          const Message&)>;

  struct Invocation
  {
    std::shared_ptr<
        CallbackSlot> owner;

    Callback callback;

    ~Invocation()
    {
      if (owner) {
        owner->Release();
      }
    }
  };

  std::optional<Invocation>
  Acquire()
  {
    std::lock_guard lock(mutex_);

    if (!active_ ||
        !callback_)
    {
      return std::nullopt;
    }

    ++in_flight_;

    return Invocation{
      shared_from_this(),
      callback_
    };
  }

  void Disable()
  {
    std::lock_guard lock(mutex_);

    active_ = false;
    callback_ = nullptr;
    ++generation_;
  }

  void WaitQuiescent()
  {
    std::unique_lock lock(mutex_);

    cv_.wait(
      lock,
      [&] {
        return in_flight_ == 0;
      });
  }

 private:
  void Release()
  {
    std::lock_guard lock(mutex_);

    --in_flight_;

    if (!active_ &&
        in_flight_ == 0)
    {
      cv_.notify_all();
    }
  }

  std::mutex mutex_;
  std::condition_variable cv_;

  Callback callback_;

  bool active_ = true;
  std::size_t in_flight_ = 0;
  std::uint64_t generation_ = 0;
};
```

真正工程还要处理：

- exception safety；
- self-context detection；
- generation 过滤；
- stop token；
- callback priority；
- payload ownership。

但核心不变量已经清楚。

---

# 66. 最终把这类问题压成八条规则

```text
1. Never execute arbitrary user code
   while holding a registry lock.

2. Callback-storage mutex
   should protect storage,
   not automatically execution.

3. Snapshot callback and metadata
   before crossing the user-code boundary.

4. shared_ptr lifetime
   is not unregister quiescence.

5. Unregister from lookup
   is not drain of in-flight work.

6. Self-remove cannot synchronously
   wait for itself to finish.

7. Strong removal requires
   explicit in-flight accounting
   or an equivalent grace-period protocol.

8. Async callback execution changes
   payload ownership requirements.
```

如果只记住一句：

> **“锁内复制、锁外调用”解决的是 reentrancy；“retire + in-flight drain”解决的是 quiescence。它们不能互相替代。**

这也是从 eCAL 这段 Subscriber 源码中最值得迁移到机器人 Runtime、线程池、网络库、设备驱动和事件系统里的设计原则。
