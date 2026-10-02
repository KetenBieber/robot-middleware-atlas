# Cyber RT 动态注册的并发边界：Publication、Callback Lifetime 与 Quiescence

本文固定到 Apollo Cyber RT 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`。

:doc:`Dispatcher 与 Notifier <dispatcher-notifier>` 已经解释一帧消息怎样写入多只 DataVisitor ring，再用无参 notification 唤醒 CRoutine；:doc:`多输入融合 <multi-input-fusion>` 则解释 `AllLatest` 如何把主输入和辅助输入固化成参数组。

当系统允许消息流动期间创建或销毁 Reader、DataVisitor、callback 和融合策略以后，真正的问题变成：

> 注册函数返回时，其他线程看到的是一个完整对象还是构造中的半成品？注销返回时，过去已经取得引用或 callback 的线程是否全部退出？

至少需要区分四件事：

~~~text
publication
  新对象何时对并发 reader 可见

mutation
  已发布对象内部还会不会修改

ownership
  registry 是否延长对象寿命

quiescence
  注销返回时旧 reader / callback
  是否已经全部退场
~~~

这四件事分别需要不同机制。mutex、atomic、shared_ptr、weak_ptr、RCU 和 join 并不是可以互换的“线程安全工具”。

## 1. Registry 不是一个容器问题

设相机 channel 已经持续分发：

~~~text
receiver thread
      |
      v
DataDispatcher<Image>::Dispatch()
      |
      v
channel registry
      |
      +--> consumer A
      +--> consumer B
      +--> consumer C
~~~

另一个线程此时注册 consumer D。

一句：

~~~cpp
registry[channel].push_back(d);
~~~

同时隐藏了：

1. append 与遍历是否同步；
2. D 的 ring/callback/fusion state 是否完整；
3. D 对应 task 是否已经进入 Scheduler；
4. 删除 D 后 registry 是否还保存旧 entry；
5. 删除前已经取得 callback 的线程是否仍可继续执行。

所以真正的注册协议应该是：

~~~text
private construction
        |
        v
establish all invariants
        |
        v
publish stable entry
        |
        v
concurrent readers may observe it
~~~

注销则是：

~~~text
prevent new acquisition
        |
        v
wait old users to leave
        |
        v
destroy callback state
        |
        v
destroy data state
~~~

最后那个“等待旧使用者离开”就是 quiescence。

## 2. DataDispatcher 的外层 map 和内层 vector 是两种对象

固定源码：

~~~cpp
using BufferVector =
    std::vector<
        std::weak_ptr<
            CacheBuffer<
                std::shared_ptr<T>>>>;

std::mutex buffers_map_mutex_;

AtomicHashMap<
    uint64_t,
    BufferVector>
    buffers_map_;
~~~

容易出现的误解是：

> 外层是 AtomicHashMap，所以整个 registry 都可以并发读写。

实际上，`AtomicHashMap` 只管理它自己的 entry/value pointer。

value 内部的普通 `std::vector` 仍然需要独立同步协议。

## 3. AtomicHashMap 真正原子的是什么

固定实现的 Entry：

~~~cpp
struct Entry {
  K key = 0;

  std::atomic<V*> value_ptr = {
      nullptr};

  std::atomic<Entry*> next = {
      nullptr};
};
~~~

更新已有 value 时会 CAS `value_ptr`：

~~~cpp
auto old_val_ptr =
    target->value_ptr.load(
        std::memory_order_acquire);

if (target->value_ptr.
        compare_exchange_strong(
            old_val_ptr,
            new_value,
            std::memory_order_acq_rel,
            std::memory_order_relaxed)) {
  delete old_val_ptr;
  return;
}
~~~

因此 map 的同步边界是：

~~~text
Entry pointer
Value pointer
~~~

但 Cyber 在已有 channel 上并不替换整个 BufferVector，而是取得 `BufferVector*` 后直接 `emplace_back`。

这绕开了 value pointer 的原子替换语义。

## 4. AddBuffer 只实现 writer-vs-writer 互斥

固定 `AddBuffer()`：

~~~cpp
void DataDispatcher<T>::AddBuffer(
    const ChannelBuffer<T>&
        channel_buffer) {

  std::lock_guard<std::mutex> lock(
      buffers_map_mutex_);

  auto buffer =
      channel_buffer.Buffer();

  BufferVector* buffers = nullptr;

  if (buffers_map_.Get(
          channel_buffer.channel_id(),
          &buffers)) {

    buffers->emplace_back(buffer);

  } else {
    BufferVector new_buffers = {
        buffer};

    buffers_map_.Set(
        channel_buffer.channel_id(),
        new_buffers);
  }
}
~~~

`buffers_map_mutex_` 能串行化两个 AddBuffer。

但它没有被 Dispatch 获取。

因此它提供的是：

~~~text
writer <-> writer
~~~

而不是：

~~~text
writer <-> reader
~~~

## 5. Dispatch 会无锁遍历同一 vector

固定 `Dispatch()`：

~~~cpp
BufferVector* buffers = nullptr;

if (buffers_map_.Get(
        channel_id,
        &buffers)) {

  for (auto& buffer_wptr :
       *buffers) {

    if (auto buffer =
            buffer_wptr.lock()) {

      std::lock_guard<std::mutex>
          lock(buffer->Mutex());

      buffer->Fill(msg);
    }
  }
}
~~~

如果另一个线程同时执行：

~~~cpp
buffers->emplace_back(buffer);
~~~

就变成：

~~~text
thread A
  range-for reads vector storage

thread B
  vector::emplace_back mutates size
  and may reallocate storage
~~~

外层 AtomicHashMap 的 Get 即使完全正确，也不能让内层 vector 的这种并发读写合法。

## 6. vector reallocation 为什么不只是“漏看新订阅者”

假设：

~~~text
size = 4
capacity = 4
data -> old_array
~~~

注册第 5 个 entry 可能：

~~~text
allocate new_array
move four weak_ptr
free old_array
update data pointer
~~~

Dispatch 的 iterator 却可能仍指向 old_array。

所以风险不仅是某帧暂时看不到新 Reader，而可能是 iterator/reference 落入正在迁移或释放的存储。

## 7. 第一次 publication 与后续 append 的语义不同

某 channel 第一次出现：

~~~cpp
BufferVector new_buffers = {
    buffer};

buffers_map_.Set(
    channel_id,
    new_buffers);
~~~

vector 先完整构造，再由 map 发布。

概念上接近：

~~~text
construct privately
→ publish pointer
~~~

已有 channel 时则是：

~~~text
Get published vector pointer
→ mutate published vector in place
~~~

因此真正的问题不是 AtomicHashMap 完全无用，而是**已发布 value 被再次原地修改，而 reader 仍把它当稳定对象遍历**。

## 8. DataNotifier 重复同一种结构

固定成员：

~~~cpp
using NotifyVector =
    std::vector<
        std::shared_ptr<Notifier>>;

std::mutex notifies_map_mutex_;

AtomicHashMap<
    uint64_t,
    NotifyVector>
    notifies_map_;
~~~

AddNotifier 有 writer mutex：

~~~cpp
std::lock_guard<std::mutex> lock(
    notifies_map_mutex_);

notifies->emplace_back(
    notifier);
~~~

Notify 却直接遍历：

~~~cpp
for (auto& notifier :
     *notifies) {

  if (notifier &&
      notifier->callback) {
    notifier->callback();
  }
}
~~~

所以 DataDispatcher 与 DataNotifier 的 registry 都是：

~~~text
AtomicHashMap
+
mutable vector value
+
writer-only mutex
+
reader traversal without same mutex
~~~

## 9. shared_ptr 只解决 lifetime，不会保护 vector

`shared_ptr<Notifier>` 可以让已经取得的 Notifier 活到当前引用释放。

但它不会保护：

~~~text
vector size/capacity
vector iterator
std::function assignment
callback logical state
~~~

必须分开：

~~~text
ownership
  object 是否还活着

synchronization
  同一 object 是否允许并发读写
~~~

智能指针主要回答前者。

## 10. weak_ptr 是 non-owning registry，但不是 unregister

Dispatcher 存：

~~~cpp
weak_ptr<CacheBuffer<...>>
~~~

这避免进程级 singleton 永久续命每只 ring。

Dispatch 时：

~~~cpp
if (auto buffer =
        buffer_wptr.lock()) {
  ...
}
~~~

对象已销毁时，未来的 lock 会失败。

但这只意味着：

~~~text
future acquisition may fail safely
~~~

它没有提供：

~~~text
remove stale registry entry
stable vector mutation
wait in-flight users
~~~

## 11. weak_ptr 失效与 quiescence 是两件事

假设：

~~~text
T0 Dispatch
  weak_ptr.lock()
  obtains shared_ptr<CacheBuffer>

T1 DataVisitor destructor starts

T0 continues
~~~

T0 已经取得 shared_ptr，所以 owner 丢掉自己的引用不意味着 T0 立即停止。

这本身能保护 CacheBuffer。

但如果 CacheBuffer 内部还保存 callback，而 callback 捕获的对象已经先销毁，shared_ptr 只会让“带着悬空 callback 的 CacheBuffer”继续活得更久。

这就是 transitive lifetime。

## 12. DataVisitor 的发布不是一个原子动作

单输入构造：

~~~cpp
DataDispatcher<M0>::Instance()->
    AddBuffer(buffer_);

data_notifier_->AddNotifier(
    buffer_.channel_id(),
    notifier_);
~~~

两输入构造：

~~~cpp
DataDispatcher<M0>::Instance()->
    AddBuffer(buffer_m0_);

DataDispatcher<M1>::Instance()->
    AddBuffer(buffer_m1_);

data_notifier_->AddNotifier(
    buffer_m0_.channel_id(),
    notifier_);

data_fusion_ =
    new fusion::AllLatest<M0, M1>(
        buffer_m0_,
        buffer_m1_);
~~~

一个真正 ready 的两输入 visitor 至少需要：

~~~text
M0 buffer
M1 buffer
Notifier
AllLatest
fusion callback
Scheduler task
notify callback
~~~

源码却让这些状态逐步对进程级 registry 可见。

## 13. 多输入 buffer 会先于 fusion policy 被发布

一旦：

~~~cpp
DataDispatcher<M0>::Instance()->
    AddBuffer(buffer_m0_);
~~~

完成，M0 接收线程就可能在 registry 中找到这只 CacheBuffer。

但 `AllLatest` 还可能没有构造。

因此可能看到：

~~~text
buffer visible
fusion behavior not installed
~~~

这就是 partially initialized publication。

## 14. AllLatest 的 callback 是后装的

两输入构造：

~~~cpp
AllLatest(
    const ChannelBuffer<M0>& buffer_0,
    const ChannelBuffer<M1>& buffer_1)
    : buffer_m0_(buffer_0),
      buffer_m1_(buffer_1),
      buffer_fusion_(...) {

  buffer_m0_.Buffer()->
      SetFusionCallback(
          [this](
              const std::shared_ptr<M0>&
                  m0) {
            ...
          });
}
~~~

于是存在：

~~~text
publish M0 CacheBuffer
        |
        | initialization window
        v
install fusion callback
~~~

## 15. SetFusionCallback 没有取得 Fill 使用的 mutex

实现：

~~~cpp
void SetFusionCallback(
    const FusionCallback& callback) {
  fusion_callback_ = callback;
}
~~~

而 Dispatch：

~~~cpp
std::lock_guard<std::mutex>
    lock(buffer->Mutex());

buffer->Fill(msg);
~~~

Fill 会读取：

~~~cpp
if (fusion_callback_) {
  fusion_callback_(value);
}
~~~

setter 并没有拿 `buffer->Mutex()`。

所以可能发生：

~~~text
thread A
  reads std::function in Fill

thread B
  writes same std::function
~~~

只有调用一方加锁不能形成 happens-before。

双方必须遵守同一同步协议。

## 16. 早到 M0 还会产生业务语义差异

callback 尚未安装时，Fill 会走普通 ring 分支。

但 AllLatest 后续的 `Fusion()` 读取的是单独的 fusion ring。

因此早到 M0 不会自动在 callback 安装后被重新组合成：

~~~text
(M0, latest M1)
~~~

它可能永远没有对应 fusion tuple。

所以 publication 顺序影响的不只是 C++ 数据竞争，还会影响可观察的数据语义。

## 17. Notifier 也经历“先发布对象，再绑定 callback”

`DataVisitorBase` 先创建：

~~~cpp
DataVisitorBase()
    : notifier_(
          new Notifier()) {}
~~~

此时：

~~~cpp
std::function<void()> callback;
~~~

还是空的。

DataVisitor 构造时已经：

~~~cpp
data_notifier_->AddNotifier(
    channel_id,
    notifier_);
~~~

真正 callback 到 Scheduler::CreateTask 最后才绑定：

~~~cpp
visitor->RegisterNotifyCallback(
    [this, task_id]() {
      if (cyber_unlikely(
              stop_.load())) {
        return;
      }

      this->NotifyProcessor(
          task_id);
    });
~~~

所以 Notifier registry 也会先看到一个尚未完成运行时绑定的对象。

## 18. std::function 的后绑定没有同步

`RegisterNotifyCallback()`：

~~~cpp
void RegisterNotifyCallback(
    std::function<void()>&& callback) {
  notifier_->callback = callback;
}
~~~

Notify 侧：

~~~cpp
if (notifier &&
    notifier->callback) {
  notifier->callback();
}
~~~

如果两条线程重叠，就是普通 `std::function` 的并发读写。

“默认 callback 为空”是逻辑值，不是同步原语。

## 19. Scheduler::CreateTask 的顺序只解决了一半

固定顺序：

~~~cpp
if (!DispatchTask(cr)) {
  return false;
}

if (visitor != nullptr) {
  visitor->RegisterNotifyCallback(...);
}
~~~

先把 CRoutine 放进 Scheduler，再绑定 callback 有合理性：

~~~text
callback must not notify
a task that does not exist yet
~~~

但反向窗口是：

~~~text
task already runnable
callback not yet installed
~~~

routine 可能先运行、发现无数据、进入 DATA_WAIT，而恰好到来的 notification 还没有真正 callback 可以调用。

## 20. 普通 Reader 与 reality-mode Component 的窗口不同

普通 Reader 的关键顺序：

~~~text
create DataVisitor
→ CreateTask
   → DispatchTask
   → bind callback
→ GetReceiver
→ JoinTheTopology
~~~

receiver 在 CreateTask 返回以后才建立，因此 transport 数据进入的窗口相对受限。

reality-mode Component 则先创建 Reader：

~~~text
CreateReader
→ receiver/topology active
→ readers_ stores Reader
→ create Component DataVisitor
→ create Component task
→ bind callback
~~~

已有 writer 时，数据可能已经到达 Reader，而 Component 的 DataVisitor/task 还在后面构造。

因此不能把普通 Reader 的初始化时序直接作为 Component 的热注册保证。

## 21. 动态注册至少有三个 commit point

可以抽象为：

~~~text
Commit A
  buffer visible to DataDispatcher

Commit B
  Notifier visible to DataNotifier

Commit C
  task and notify callback usable
~~~

多输入还多一个：

~~~text
Commit F
  fusion callback installed
~~~

一个完整 hot-plug protocol 不应该让并发 reader 无约束地分别观察这些中间 commit。

更稳健的模型是：

~~~text
REGISTERING
→ ACTIVE
→ DRAINING
→ DEAD
~~~

并让 registry 只向数据面发布 ACTIVE entry。

## 22. 注销更难，因为旧 reader 可能已经获得引用

注册主要解决“别人是否看到半成品”。

注销还要解决“别人已经拿到旧对象”。

例如 Dispatch 已经完成 weak_ptr 提升，随后 DataVisitor 开始销毁。

此时 registry 中 weak_ptr 即使马上失效，也无法撤回当前 Dispatch 手里的 shared_ptr。

所以：

~~~text
erase / expire
does not cancel in-flight use
~~~

这就是 quiescence 必须独立存在的原因。

## 23. DataDispatcher 没有 RemoveBuffer

固定接口只有 AddBuffer 与 Dispatch，没有对应 RemoveBuffer。

DataVisitor 销毁后，weak entry 会留在 vector。

未来 Dispatch 的 `lock()` 会失败，这能避免直接访问已经释放的 CacheBuffer。

但：

~~~text
create
destroy
create
destroy
...
~~~

会让 registry 保留越来越多 expired weak entry。

高频 Dispatch 的扫描成本因此会逐渐包含历史垃圾。

## 24. DataNotifier 会强持有历史 Notifier

Notifier registry 保存：

~~~cpp
vector<shared_ptr<Notifier>>
~~~

且没有 RemoveNotifier。

DataVisitor 释放自己的 `notifier_` 后，DataNotifier 里的强引用仍然保留对象和 callback。

这不意味着 Notifier 会悬空。

更准确的结论是：

> 动态反复创建/销毁 visitor 后，旧 callback entry 不会自然从 registry 消失；后续 Notify 仍会扫描它们。

## 25. Scheduler 原始 this 在固定实现里的真实边界

Notifier callback 捕获 Scheduler 的原始 `this`。

但固定 scheduler factory 的 `CleanUp()` 只调用：

~~~cpp
obj->Shutdown();
~~~

没有 delete scheduler instance。

所以在正常 process-lifetime 模型里，旧 callback 不应被简单描述成“CleanUp 后必然 UAF”。

更准确的是：

~~~text
Notifier callback lifetime
and task lifetime
are not one-to-one
~~~

旧 callback 可以长期存在，只靠 `stop_` 或 NotifyProcessor 找不到 task 变成逻辑 no-op。

## 26. RemoveTask 等待的是 CRoutine，不是 transport callback

`ClassicContext::RemoveCRoutine()`：

~~~cpp
while (!cr->Acquire()) {
  std::this_thread::sleep_for(
      std::chrono::microseconds(1));
}

croutines.erase(it);
cr->Release();
~~~

这可以等待 Processor 上正在执行的 CRoutine 释放 acquire flag。

因此它建立的是：

~~~text
task execution quiescence
~~~

而不是：

~~~text
DataDispatcher quiescence
DataNotifier quiescence
topology callback quiescence
~~~

不同执行域需要不同 barrier。

## 27. 多输入析构还有 transitive lifetime 风险

两输入 DataVisitor 析构先：

~~~cpp
delete data_fusion_;
data_fusion_ = nullptr;
~~~

AllLatest 曾给 M0 CacheBuffer 安装：

~~~cpp
[this](...) {
  ...
}
~~~

但 AllLatest 析构没有显式清空 CacheBuffer 中的 fusion callback。

于是存在：

~~~text
AllLatest dead
CacheBuffer alive
callback still captures old this
~~~

如果某个 in-flight Dispatch 之前已经通过 weak_ptr.lock() 获得 CacheBuffer shared_ptr，它还可能继续让 CacheBuffer 存活并调用 Fill。

这说明：

> 保住容器对象的 lifetime，不等于保住容器内部 callback 所依赖的 transitive owner。

## 28. 只给 SetFusionCallback 加锁为什么仍然不够

给 setter 也取得 buffer mutex，可以解决：

~~~text
std::function read/write race
~~~

但仍没有解决：

1. buffer 是否在 callback 安装前已经公开；
2. 早到 M0 是否走错普通 ring；
3. 注销时是否有 in-flight Fill；
4. callback 捕获的 AllLatest 是否活到最后一次调用返回。

因此 mutex around assignment 只是 mutation safety，不是完整 publication/quiescence protocol。

## 29. 方案 A：shared_mutex + stable snapshot

注册很少、Dispatch 很频繁时，可以先把语义写对：

~~~cpp
struct Entry {
  std::weak_ptr<Buffer> buffer;
};

std::shared_mutex mu;

std::unordered_map<
    ChannelId,
    std::vector<Entry>>
    registry;
~~~

Dispatch 只在锁内复制 entry：

~~~cpp
std::vector<Entry> snapshot;

{
  std::shared_lock lock(mu);
  snapshot = registry[channel];
}

for (const auto& entry :
     snapshot) {
  if (auto b =
          entry.buffer.lock()) {
    DispatchOne(*b);
  }
}
~~~

注册和注销用 unique lock。

关键原则是：

> 锁只保护 registry metadata 和 snapshot，不跨过真正 callback/业务执行。

## 30. 为什么不能持 registry lock 执行 callback

callback 可能：

- 创建新 Reader；
- 取消自身订阅；
- 做 I/O；
- 等另一个线程；
- 再次进入 registry。

如果 callback 执行期间还持 registry shared lock，就可能出现：

~~~text
callback
→ unsubscribe self
→ waits unique lock
→ same thread still owns shared lock
→ self-deadlock
~~~

所以结构锁必须在调用任意用户 callback 之前释放。

## 31. 方案 B：Copy-on-Write immutable snapshot

更适合 read-mostly 的形式：

~~~cpp
using Snapshot =
    std::vector<Entry>;

std::atomic<
    std::shared_ptr<
        const Snapshot>>
    current;
~~~

reader：

~~~cpp
auto snapshot =
    current.load(
        std::memory_order_acquire);

for (const auto& e :
     *snapshot) {
  ...
}
~~~

writer：

~~~cpp
auto old =
    current.load(...);

auto next =
    std::make_shared<Snapshot>(
        *old);

next->push_back(new_entry);

current.store(
    next,
    std::memory_order_release);
~~~

reader 从头到尾只遍历不可变 vector，不会遇到 reallocation。

旧 snapshot 由最后一个 reader shared_ptr 自动回收。

## 32. 方案 C：RCU / epoch 把 quiescence 做成系统机制

更极端的 hot read path 可以：

~~~text
reader
  enter read-side section
  load immutable registry
  iterate
  exit

writer
  publish new registry
  retire old registry
  wait grace period
  reclaim old registry
~~~

grace period 表示：

> 所有可能仍在读旧版本的 reader 都离开以后，旧版本才回收。

这正是 quiescence 的系统化实现。

但如果还没有性能证据，shared_mutex/COW 通常更容易证明和维护。

## 33. Callback lifetime 应该与短寿命 facade 解耦

AllLatest 当前捕获 raw `this`。

更容易证明的是把运行状态独立成 shared state：

~~~cpp
struct FusionState {
  ChannelBuffer<M1> m1;
  ChannelBuffer<FusionData> out;

  std::atomic<bool> active{
      true};
};
~~~

callback 捕获：

~~~cpp
std::weak_ptr<FusionState>
    weak;
~~~

执行时：

~~~cpp
if (auto s = weak.lock()) {
  if (!s->active.load()) {
    return;
  }

  ...
}
~~~

这样 callback 的有效期不再直接依赖 AllLatest facade 的析构瞬间。

但 weak_ptr 仍不能替代 registry unregister；否则历史 entry 仍会永久扫描。

## 34. RAII RegistrationHandle 可以把注销职责实体化

概念 API：

~~~cpp
class RegistrationHandle {
 public:
  ~RegistrationHandle() {
    Close();
  }

  void Close();

 private:
  Registry* registry_;
  ChannelId channel_;
  EntryId id_;
};
~~~

Close 应表达：

~~~text
mark DRAINING
→ remove from published snapshot
→ prevent new callback acquisition
→ wait in-flight callbacks
→ return
~~~

因此“注销完成”的定义不再只是 map erase。

## 35. quiescence 可以从 in-flight counter 起步

概念状态：

~~~cpp
struct CallbackState {
  std::atomic<bool> enabled{
      true};

  std::atomic<uint32_t>
      in_flight{0};

  std::mutex mu;
  std::condition_variable cv;
};
~~~

调用：

~~~text
acquire call permission
→ increment in_flight
→ execute
→ decrement
→ notify close waiter
~~~

Close：

~~~text
disable new calls
→ unpublish registry entry
→ wait in_flight == 0
→ destroy state
~~~

真正实现时要正确处理 enabled 检查与 increment 之间的竞态，可以使用 CAS、mutex 或成熟 epoch primitive。

这里关键是协议定义，而不是某个具体类名。

## 36. Task quiescence 与 callback quiescence 要分开

完整 teardown 至少要考虑：

~~~text
1. stop new input admission

2. unpublish buffer/notifier entries

3. wait transport callback quiescence

4. remove scheduler task

5. wait task quiescence

6. destroy fusion/business state
~~~

第 3 步和第 5 步是两个不同 barrier。

RemoveTask 不能替代 transport drain。

## 37. 构造顺序应该从不变量反推

先写不变量：

~~~text
Invariant 1
任何 Dispatcher 可见的 fusion M0 buffer，
fusion callback 已经完整安装。

Invariant 2
任何 ACTIVE Notifier，
拥有完整 callback。

Invariant 3
任何 callback 可引用的 task_id，
Scheduler 中已经存在对应 task。

Invariant 4
注销返回以后，
旧 callback 不再进入业务对象。
~~~

再决定 new、register、publish、activate 的顺序。

这比记忆现有 constructor 先后更有迁移价值。

## 38. 一个更稳健的构造事务

概念流程：

~~~text
Phase 1 private build
  create buffers
  create fusion state
  install fusion callback
  create final notifier callback
  prepare task

Phase 2 scheduler admission
  publish task

Phase 3 registry publication
  publish immutable buffer snapshot
  publish notifier snapshot

Phase 4 transport admission
  attach Receiver
  join topology

ACTIVE
~~~

失败时 rollback 私有状态，不让半完成对象永久残留在 singleton registry。

## 39. “通常启动期注册”不能替代 runtime contract

如果 runtime 强制：

~~~text
construct complete graph
→ activation barrier
→ start data plane
~~~

很多竞态可以结构性消失。

但如果 API 允许运行中 CreateReader/CreateComponent，而 transport 与 registry mutation 没有统一同步，就不能靠“工程上通常不会这样用”证明 hot-plug 安全。

必须区分：

~~~text
supported invariant
vs
common usage pattern
~~~

## 40. 为什么具身系统很在意这个问题

长期运行机器人会遇到：

- camera/lidar 重连；
- 感知模式切换；
- 插件重载；
- 故障节点重启；
- 临时诊断订阅；
- 推理服务重新接入；
- VLA pipeline 动态重构。

如果 runtime 只保证静态图安全，上层就应该用进程重启或 epoch 切换来承载动态变化。

如果需要 hot plug，就必须给 publication 与 quiescence 一等语义。

## 41. 与 Holoscan event wakeup 的对照

Holoscan 那条链强调：

~~~text
event is a hint
condition is truth
~~~

Cyber registry 可以得到：

~~~text
registry entry is reachability
object state is truth
~~~

对象出现在 registry，不应该隐含：

~~~text
fully initialized
callback enabled
lifetime infinite
~~~

registry 只应该提供一个稳定访问句柄；ACTIVE、DRAINING、DEAD 应由明确 lifecycle state 表达。

## 42. 最终把四类问题固定下来

### Publication

~~~text
何时构造完成？
何时对并发 reader 可见？
~~~

常用机制：mutex、release/acquire、immutable snapshot、activation barrier。

### Mutation

~~~text
发布后还会不会修改？
谁和谁并发？
~~~

常用机制：mutex/shared_mutex、atomic、COW、immutable state。

### Ownership

~~~text
entry 是否延长对象寿命？
~~~

常用机制：shared_ptr、weak_ptr、owner thread。

### Quiescence

~~~text
注销返回时，
所有旧访问是否结束？
~~~

常用机制：in-flight counter、join、barrier、epoch/RCU grace period。

这四类问题如果混在一起，就会出现：

~~~text
用了 shared_ptr
为什么仍有 dangling callback？

加了 mutex
为什么仍然看到半初始化对象？

erase 了 registry
为什么旧 callback 还在运行？
~~~

## 43. 固定源码的边界可以压缩成一张图

~~~text
DataDispatcher
  AtomicHashMap<
      channel,
      vector<weak buffer>>

  AddBuffer:
    writer serialized

  Dispatch:
    iterates same vector
    without registration mutex

  no RemoveBuffer


DataNotifier
  AtomicHashMap<
      channel,
      vector<shared Notifier>>

  AddNotifier:
    writer serialized

  Notify:
    iterates same vector
    without registration mutex

  no RemoveNotifier


DataVisitor
  publishes buffers/notifier
  during construction

  task callback bound later


AllLatest
  installs fusion callback
  after M0 buffer publication

  callback captures raw this


Scheduler
  publishes task before
  notifier callback binding

  RemoveTask waits CRoutine

  but does not establish
  transport callback quiescence
~~~

这不能简单压成“线程安全”或“线程不安全”。

真正重要的是知道每一层已经建立了什么契约，还缺什么契约。

## 44. 源码作者最值得带走的规则

~~~text
1. construct privately, publish once

2. never mutate a container
   while lock-free readers iterate it

3. ownership and synchronization
   are separate problems

4. callbacks should not depend on
   shorter-lived raw owners

5. unregister is not quiescence

6. stop task and stop callback
   are different barriers

7. registry locks protect metadata,
   not arbitrary user code

8. read-mostly registries naturally favor
   immutable snapshot / COW / RCU

9. lifecycle invariants first,
   constructor order second
~~~

具身系统运行时真正需要追求的不是“所有东西都 lock-free”。

而是：

> **每一次 publication 都只有一个清晰的可见性边界；每一次 teardown 都有一个可证明的 quiescence 边界。**

只有这样，动态 Reader、插件、感知链重建和长期在线恢复才不会把“偶尔能跑”误当成 runtime contract。
