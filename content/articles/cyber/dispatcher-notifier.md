# 进程内数据分发：Dispatcher、弱引用与 Notifier

本文中的真实实现统一固定到 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`；关键代码直接粘贴在相应机制的讲解旁。

上一章把 `CacheBuffer` 和 consumer cursor 拆到槽位级别。现在 producer 已经有一枚 `shared_ptr<Message>`，系统也已经有多只 DataVisitor buffer。新的问题是：怎样把这枚消息交给所有相关缓存，又怎样通知对应任务？

这里 producer 是当前正在分发消息的一侧；consumer cursor 是每个消费者自己的逻辑读取序号。`shared_ptr<Message>` 是带共享引用计数的对象句柄，复制句柄通常不深复制 Message。registry 是“channel 标识到订阅对象”的登记表，fan-out 则表示一条输入向多只消费者缓存扇出。后文的 singleton 只是进程内统一访问这张表的实例，并不意味着跨进程共享。

最直接的实现似乎是 `channel -> vector<Reader*>`，循环调用每个 Reader 的 callback。但这会把数据分发、业务对象寿命和执行线程绑在一起：Reader 销毁时 registry 容易留下悬空指针；callback 可能在 transport 线程中做任意耗时工作；同一 channel 的 Component、Reader Observe 和其他订阅者也难以拥有独立队列。这里 `Observe()` 是由调用方主动刷新并读取 Reader 历史视图的接口，与消息到达后自动运行 callback 是两种消费节奏。

把这个朴素方案写成代码，就能看到故障如何发生。下面是**错误教学伪代码，不是 Apollo 源码**：

```cpp
class NaiveDispatcher {
 public:
  void Dispatch(ChannelId channel, const Message& message) {
    auto& readers = readers_[channel];
    for (Reader* reader : readers) {
      reader->Callback(message);  // 当前 transport 线程同步执行业务
    }
  }

 private:
  std::unordered_map<ChannelId, std::vector<Reader*>> readers_;
};
```

设 Reader A 的 callback 用 30 ms 做一次图像推理：接收线程必须等它返回，才能继续遍历同一 channel 的 B、C；即使 B 只是 1 ms 的控制前置处理，也会被 A 排在后面。另一个线程若在遍历期间销毁 A，`Reader*` 不会延长对象寿命，分发线程下一次解引用就是悬空访问。给 vector 加一把全局锁也不能直接解决两者：如果锁包住 callback，慢推理会让注册、注销和其他 channel 也一起堵住；若锁外调用 callback，又必须另行固定 Reader 的生命周期和安全快照。

Cyber 用两层解耦解决这个问题：`DataDispatcher<T>` 只把消息写入缓存，`DataNotifier` 只传播“有更新”的事件。Reader 与 Component 都不会被这两个进程级 singleton 直接拥有。

先把本文后面反复出现的词说清楚：callback（回调）是调用方交给框架、由框架在事件到来时调用的函数；mutex（互斥锁）让同一时刻只有一个线程进入它保护的临界区；`shared_ptr` 是带共享所有权计数的对象句柄，复制句柄不复制其指向的消息本体。文中的 fenced `text` 块是作者绘制的关系图或运行时序，不是源码；C++ 块会标为固定提交摘录或教学草图。

```text
message object
    |
    v
DataDispatcher<T>
    |-- weak buffer A -> Reader DataVisitor ring
    |-- weak buffer B -> Component DataVisitor ring
    `-- weak buffer C -> another subscriber ring
    |
    v
DataNotifier
    |-- callback -> task A
    |-- callback -> task B
    `-- callback -> task C
```

这一章只追 `Dispatch()` 返回之前发生的事情。任务唤醒与执行面放在[CRoutine、Scheduler 与 Processor 专题](croutine-wakeup.md)中展开；两者之间的[多输入组合](multi-input-fusion.md)补充 DataVisitor 怎样形成触发消息与辅助消息的读取视图。

## `DataVisitor` 的双重注册

单输入 `DataVisitor<M0>` 的构造函数很短，却完成了数据面和事件面的双重注册：

下面是固定提交源码摘录，展示构造期间的两次登记：

```cpp
DataVisitor(uint64_t channel_id, uint32_t queue_size)
    : buffer_(channel_id, new BufferType<M0>(queue_size)) {
  DataDispatcher<M0>::Instance()->AddBuffer(buffer_);
  data_notifier_->AddNotifier(buffer_.channel_id(), notifier_);
}
```

`buffer_` 是 `ChannelBuffer<M0>`，内部强持有刚创建的 `CacheBuffer<shared_ptr<M0>>`。第一行注册把缓存放进 `DataDispatcher<M0>` 的 channel 表，第二行注册把 visitor 自己的 Notifier 放进 `DataNotifier` 的 channel 表。

注意两张表存放的东西不同：

```text
DataDispatcher registry: channel_id -> buffers
DataNotifier registry:   channel_id -> event callbacks
```

若把 payload 塞进 notifier，Scheduler 就必须理解任意消息类型，事件队列还会复制或拥有消息；若只通知不缓存，唤醒合并时又会丢失数据。分成两张表后，数据数量由 ring 的 head/tail 表示，唤醒只表达“请重新检查”。

## 按消息类型实例化的模板 singleton

`DataDispatcher<T>::Instance()` 中的 `T` 是消息 C++ 类型。`DataDispatcher<PointCloud>` 与 `DataDispatcher<LocalizationEstimate>` 是两个不同的模板实例，各自有自己的 singleton 状态。singleton 在这里指每个类型、每个进程内共享的一份实例，不是跨进程对象。

模板在这里避免了把消息类型擦除成 `void*`。类型擦除是把不同具体类型藏在共同接口之后，只留下统一操作；例如 `std::function<void()>` 能容纳不同来源但都可无参调用的函数对象。每只 buffer 直接保存 `shared_ptr<T>`，`Dispatch()` 不需要在运行时恢复类型或检查 type id；编译器会为每种实际使用的 `T` 生成类型正确的代码。真正的“类型擦除”会出现在 Scheduler 把不同消息签名包装成统一无参任务的边界，不能与这里的模板分区混为一谈。

代价是同一逻辑机制会为多个消息类型实例化，增加二进制体积；跨动态库（动态加载的共享库）使用模板 singleton 时也必须保证链接与符号可见性一致。源码阅读时要记住：“全局唯一”是相对一个模板类型和进程而言，不是所有消息共用一张表。

## registry 的 key/value 与 fan-out 结构

`DataDispatcher<T>` 定义：

下面是固定提交中的成员声明：

```cpp
using BufferVector =
    std::vector<std::weak_ptr<CacheBuffer<std::shared_ptr<T>>>>;

std::mutex buffers_map_mutex_;
AtomicHashMap<uint64_t, BufferVector> buffers_map_;
```

key 是 `channel_id`。它由 channel name 转换为进程内使用的整数标识，避免热路径反复比较长字符串。

value 是 `vector<weak_ptr<CacheBuffer<shared_ptr<T>>>>`。同一 channel 对应多只 buffer，因为 Reader 自己、Component 自己以及其他订阅者都可以拥有独立队列。

`AddBuffer()` 怎样把一只消费端缓存登记进这个表？下面是 `DataDispatcher<T>::AddBuffer()` 的固定提交源码摘录：

```cpp
template <typename T>
void DataDispatcher<T>::AddBuffer(const ChannelBuffer<T>& channel_buffer) {
  std::lock_guard<std::mutex> lock(buffers_map_mutex_);
  auto buffer = channel_buffer.Buffer();
  BufferVector* buffers = nullptr;
  if (buffers_map_.Get(channel_buffer.channel_id(), &buffers)) {
    buffers->emplace_back(buffer);
  } else {
    BufferVector new_buffers = {buffer};
    buffers_map_.Set(channel_buffer.channel_id(), new_buffers);
  }
}
```

锁保护的是注册方对 value vector 的追加，不是 `Dispatch()` 的遍历；因此这段代码既展示了登记的实际数据结构，也说明了为什么它没有自动提供运行时并发注册保证。

`AtomicHashMap` 是 Apollo 提供的固定大小、以原子操作维护的哈希表；固定提交默认有 128 个桶，整数 key 通过掩码映射到桶，再在桶内查找条目。它的并发查找能力不延伸到 value 内的 `vector`。若哈希分布良好，找到 channel 的桶接近常数时间；桶内查找仍可能遍历冲突条目，命中后还需遍历 `B` 只 buffer，因此 Dispatch 的 fan-out 主体复杂度是 `O(B)`。这里的 lock-free（无锁）描述的是 map 的并发进展机制，不代表内层 vector 自动线程安全或每个调用都有固定耗时。

为什么不用 `channel_id -> one queue`？一只共享 destructive queue 会让多个消费者竞争同一批消息：Reader Observe 取走一条后 Component 就看不到它。即使共享 ring 再给每个 consumer 单独 cursor，消费者的 queue depth、销毁和锁竞争也会耦合。Cyber 选择每个 DataVisitor 一只 buffer，让过载和读取进度互不干扰，代价是每条消息要写多只槽。

## `weak_ptr` 表达“不拥有订阅者”

`weak_ptr` 是观察句柄，不增加对象的强引用计数。要使用它，必须先调用 `lock()` 尝试获得临时 `shared_ptr`：对象仍存活时 lock 成功；最后一个强引用已消失时 lock 返回空。

Dispatcher 是进程级 singleton，可能活到进程退出；DataVisitor 则随 Reader、Component task 的创建和删除而变化。如果 registry 存 `shared_ptr<CacheBuffer>`，即使 task 删除、Component 关闭，registry 仍会让缓存永久存活。

使用 weak pointer 后，所有权方向是：

```text
CRoutine callback
  -> strong DataVisitor
       -> strong ChannelBuffer
            -> strong CacheBuffer

DataDispatcher
  -> weak CacheBuffer only
```

task 被移除后，CRoutine 与 callback 释放 DataVisitor，缓存的强引用可以归零；Dispatcher 下一次遍历时只会看到 lock 失败。registry 能访问存活对象，却不能决定它们的寿命。

这不是“弱引用模式”标签就能解释完的。它解决的是寿命层级倒置：长寿命基础设施不应该因为登记关系强迫短寿命业务对象继续存在。

## `Dispatch()` 的每一步

消息到来时，`DataDispatcher<T>::Dispatch()` 先检查 Cyber 是否已关闭，再按 channel 找 vector，逐只写 buffer，最后通知。下面是固定提交源码摘录，保留了 shutdown guard 和主要分支：

```cpp
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

按执行顺序逐行看：

`IsShutdown()` 为 true 时立即返回 false，关闭过程不会再向缓存分发。随后 `Get()` 失败表示这个消息类型和 channel 尚无缓存注册；函数同样返回 false，不会创建临时队列，也不会让未知消息无限积压。

range-for 循环遍历所有历史注册项。`auto buffer = buffer_wptr.lock()` 的变量只在当前迭代内持有强引用，保证取得 mutex、写槽并退出临界区之前 buffer 不会在另一个线程析构。

`std::lock_guard<std::mutex>` 使用 RAII（Resource Acquisition Is Initialization，资源获取即初始化）：构造时取得 mutex，离开花括号作用域时对象析构并解锁；即使 `Fill()` 未来抛出异常，也不会因为手写遗漏而永远不解锁。这里 mutex 只保护当前 CacheBuffer 的槽位和序号，不保护整个 fan-out。

`Fill(msg)` 复制的是 `shared_ptr<T>`。所有 buffer 指向同一消息对象，payload 不会在这一步按订阅者数量复制。

本次循环完成后才调用 `Notify(channel_id)`，因此由这次 Dispatch 自己发出的通知晚于它自己的所有 buffer 写入。但这不是“整条 channel 的 fan-out 是原子事务”：多个 transport 线程可以并发调用 Dispatch，每次只在单只 buffer 上加锁。比如 D1 写完 A buffer 后暂停，D2 写完 A、B 并 Notify，consumer 可能在 D1 写 B 之前被这次通知促使运行。consumer 读到的是各只 ring 当时的状态，不是跨所有消费者、跨并发发布者的一致快照。

## Dispatch 运行在哪个线程

`Dispatch()` 没有创建线程或投递到内部 worker。`ReceiverManager` 注册的 listener 同步调用它，因此线程来自上游 transport：

```text
INTRA: Writer/Transmitter 调用线程
SHM:   ShmDispatcher 专用线程
RTPS:  Fast RTPS listener callback 线程
```

这意味着循环中的 weak lock、每只 buffer mutex、shared pointer 引用计数和 `Notify()` fan-out 都会增加上游 transport callback 的执行时间。

对 INTRA 来说，这些成本直接反映到 publisher 的 `Write()` 路径。对 SHM 来说，一个 channel 有很多本地消费者时，ShmDispatcher 线程会串行写多只 buffer，推迟继续处理共享内存通知。对 RTPS 来说，成本叠加在字符串复制和反序列化之后。

Dispatcher 成功把业务 `Proc()` 移出了 transport 线程，但它本身仍属于 transport 热路径。设计隔离不是“所有工作都异步”，而是只把不受框架控制的业务计算异步化。

## 每只 buffer 独立加锁的粒度选择

如果 `Dispatch()` 用一把全局锁保护所有 channel 和 buffer，任何高频 channel 都会阻塞其他 channel 的分发。Cyber 把 mutex 放在每只 CacheBuffer 上，使不同 DataVisitor 的读写彼此独立。

```text
channel X buffers: X-reader-lock, X-component-lock
channel Y buffers: Y-reader-lock, Y-component-lock
```

单次 Dispatch 仍按 vector 顺序串行获取这些锁。若 X-reader 的 Processor 正在长时间持有它的 buffer lock，X-component 的写入也要等前一个锁完成，因为 producer 还没走到 vector 的下一项。

正常 `Fetch()` 临界区很短，只复制 shared pointer 和更新局部输出；因此这一选择通常简单有效。若未来在锁内加入复杂融合、日志或分配，fan-out 长尾会迅速放大。

## `AtomicHashMap` 并没有让整个 value 自动线程安全

外层容器支持并发查找，不等于里面的 `BufferVector` 可以在无同步情况下同时 append 和遍历。

这里需要用 C++ 的 data race（数据竞争）术语精确描述：若两个线程并发访问同一内存位置，至少一个是写入，而且访问间没有同步，就会形成数据竞争，程序行为未定义。`AddBuffer()` 使用 `buffers_map_mutex_` 保护注册阶段对 vector 的修改；`Dispatch()` 读取 vector 时没有取得同一把 mutex。不能把“应该在启动阶段注册”误写成源码已经保证的生命周期约束：固定提交的 `NodeChannelImpl::CreateReader()` 同步调用 `Reader::Init()`，而 `Reader::Init()` 创建 `DataVisitor` 后就会进入 `AddBuffer()`。这条公开调用路径没有与正在运行的 `Dispatch()` 共用注册锁。

因此具体的危险时序是：SHM/RTPS 接收线程已经取得某 channel 的 `BufferVector*` 并正在 range-for 遍历；与此同时，应用线程动态创建一个 reader，`AddBuffer()` 对同一 vector 执行 `emplace_back()`。若新元素触发扩容，vector 会搬迁底层数组，而遍历方仍在读取它；这构成数据竞争，行为未定义，不只是本轮偶尔漏看新 Reader。问题来自“map 的并发查找”与“map 中 value 的并发修改”是两件事：AtomicHashMap 保护前者，不会替 vector 建立同步。

应用可以把 Reader 全部创建放在 transport 开始派发之前，作为调用侧的串行化约定；但这不是 Dispatcher 强制的不变量，也不能支持热插拔。若要支持运行中注册，就必须修正 registry 的并发契约：例如让 Dispatch 持共享锁、AddBuffer 持独占锁，或由写者构造新的不可变 vector 快照再原子发布给读者。快照方案把复制和分配移到低频注册路径，读侧只遍历一个稳定版本；代价是旧快照要等读者释放后才能回收。RCU（Read-Copy-Update，读-复制-更新）也能提供类似读写分离，但需要额外定义读者临界区与回收时机。

一个真正支持高频热插拔的版本可以有三类选择：

```text
read/write mutex
  注册简单，Dispatch 每次承担读锁成本

copy-on-write snapshot
  注册复制 vector，Dispatch 原子读取稳定快照

RCU-style update
  读侧近似无阻塞，旧快照延迟到读者离开后回收
```

选择依据不是模式名字，而是注册频率、消息频率、可接受读侧抖动和内存回收复杂度。只有当部署确实保证“所有注册都先于派发”时，省略 Dispatch 读锁才成立；通用插件平台或任何允许运行时创建 Reader 的程序，都需要更强的动态并发语义。把这个前提写进 API、启动顺序和关闭协议，才能让优化成为可检查的系统约束，而不是靠调用者记住的一句口头约定。

## 过期 weak entry 的长期扫描成本

weak pointer 不造成缓存泄漏，却不自动从 vector 删除自己。若进程反复创建和销毁 Reader，vector 可能变成：

```text
[expired, expired, live-A, expired, live-B, ...]
```

每次 Dispatch 仍需读取每个 weak pointer 并尝试 lock。长期动态重载会让 `B` 的定义从“活订阅者数”变成“历史注册项数”，热路径开销逐渐增加。

更完整的 registry 通常让 `AddBuffer()` 返回 registration token。DataVisitor 析构时用 token 注销对应 entry；若直接 erase vector 中间元素会改变迭代稳定性，也可以采用 tombstone + 周期压缩，或 copy-on-write 生成新快照。

这说明 weak pointer 只解决“不要悬空访问、不要强行续命”，没有解决“registry 如何维护长期规模”。所有权安全与数据结构维护是两个问题。

## 从 channel 更新到 task 事件

`DataVisitorBase` 创建一只 `Notifier`，其中保存一个无参 callback。固定源码中，`DataNotifier` 的 map 强持有 `shared_ptr<Notifier>`，而非只保存弱引用；这点和 Dispatcher 的 `weak_ptr<CacheBuffer>` 正好相反。先看登记、通知和 callback 写入的真实代码。下面是固定提交中三个函数的源码摘录，省略函数外的类定义与注释：

`Notifier` 自身的结构非常小，消息类型已在这一层被擦除：

```cpp
struct Notifier {
  std::function<void()> callback;
};

class DataNotifier {
 public:
  using NotifyVector = std::vector<std::shared_ptr<Notifier>>;
  // 其他接口与成员省略
};
```

它只保留“无参调用”这个统一接口，不保存 channel payload。`std::function<void()>` 是类型擦除包装：真实 lambda 可以捕获 `task_id`，但 `DataNotifier` 无需知道该整数如何对应 CRoutine。

```cpp
inline void DataNotifier::AddNotifier(
    uint64_t channel_id, const std::shared_ptr<Notifier>& notifier) {
  std::lock_guard<std::mutex> lock(notifies_map_mutex_);
  NotifyVector* notifies = nullptr;
  if (notifies_map_.Get(channel_id, &notifies)) {
    notifies->emplace_back(notifier);
  } else {
    NotifyVector new_notify = {notifier};
    notifies_map_.Set(channel_id, new_notify);
  }
}

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

void RegisterNotifyCallback(std::function<void()>&& callback) {
  notifier_->callback = callback;
}
```

第一段把 notifier 的强引用放进 channel vector；第二段同步遍历并执行其中非空 callback；第三段只是给该 callback 赋值。固定实现没有注销接口，`DataVisitorBase` 也没有析构时从 map 移除 notifier 的代码，所以 task 移除不会清除该项。callback 只捕获 Scheduler 指针和整数 task id，不会反向拥有 DataVisitor，但 registry 会让小型 Notifier 与闭包留到进程级 singleton 销毁；后续该 channel 的每条消息仍会扫描并调用旧 callback。Classic 策略中 task 已不存在时，`NotifyProcessor(task_id)` 返回 false；这避免访问已删除 routine，却不回收 stale entry（失效登记项）。

更微妙的是，task id 由名字散列并保存在 `GlobalData` 的进程级表中；同名 task 之后重新注册会得到同一个 id。下面是 `GlobalData::RegisterTaskName()` 的关键源码分支：

```cpp
auto id = Hash(task_name);
while (task_id_map_.Has(id)) {
  std::string* name = nullptr;
  task_id_map_.Get(id, &name);
  if (task_name == *name) {
    break;
  }
  ++id;
}
task_id_map_.Set(id, task_name);
return id;
```

因此旧 callback 可能转而通知新 CRoutine，造成重复通知。如果程序频繁动态创建/销毁订阅者，`N` 会按历史注册数量增长，而不只是活动 task 数。

`Scheduler::CreateTask()` 的相关部分是：

下面是固定提交源码摘录；它只截取 task 注册与 callback 绑定部分：

```cpp
bool Scheduler::CreateTask(std::function<void()>&& func,
                           const std::string& name,
                           std::shared_ptr<DataVisitorBase> visitor) {
  if (cyber_unlikely(stop_.load())) {
    return false;
  }

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
}
```

这个顺序解决的是一个方向的依赖：先把 CRoutine 放进调度策略，再让 notifier callback 按 task id 查找它；若反过来，事件可能先到而 task 还不存在。但它没有自动封住另一侧的窗口——`DispatchTask(cr)` 已经让 routine 可被 Processor 选择，`RegisterNotifyCallback()` 才给 notifier 写入真正的回调。

对普通 Reader，这段顺序位于 `Reader::Init()` 内，transport receiver 在 task 创建之后才加入拓扑并启用 writer；对 reality mode 的 Component，情况更微妙：`Component::Initialize()` 先创建并初始化 Reader，已有 writer 因而可能已经可达，之后才创建第二只 Component `DataVisitor` 和对应 task。若 Processor 在 `DispatchTask()` 后很快运行 routine，先检查空 ring、设置 `DATA_WAIT` 并 yield；此时恰有一帧写入 ring，而 notifier callback 尚未注册，`DataNotifier::Notify()` 就没有可用回调调用 `NotifyProcessor()`。若此后没有新帧，routine 可能继续等待，直到别的路径再次促使它检查缓存。这是由固定源码顺序推导的窄窗口，不表示每次启动必然丢帧。

这里还有两个并发访问需要分开看：`DataNotifier::AddNotifier()/Notify()` 中，`AddNotifier()` 用 mutex 保护 `NotifyVector::emplace_back()`，但 `Notify()` 遍历 vector 时没有取得同一把锁；`DataVisitorBase::RegisterNotifyCallback()` 又直接给普通 `std::function` 赋值，而 transport 线程可能在 `Notify()` 中读取并调用它。若这些操作真正在不同线程重叠，不能只用“callback 默认是空”来证明安全；vector 与 `std::function` 都缺少共同同步。一个可验证的设计应让 notifier 完全构造并绑定 callback 后才发布到可见 registry，或以锁/不可变快照协调读写，并在 task 首次等待前重查 ring。当前实现的确先发布 task 再绑定 callback，因果方向合理，但启动期间的数据可达性和 callback 发布仍需由外部时序或额外同步覆盖；完整运行回放见[从 Receiver 到 Component::Proc()](message-to-proc.md)。

Notifier callback 不捕获 DataVisitor 或消息，只捕获 scheduler 和整数 task id。它表达的是“请让编号为 X 的 routine 重新参与选择”。

`DataNotifier::Notify()` 又会同步遍历 channel 下的 notifier 并调用 callback，所以通知 fan-out 仍在当前 transport 线程内完成。

## 可合并通知与持久数据

假设 producer 很快写入 A、B、C，Processor 只被唤醒一次。只要 ring 保存了消息，routine 恢复后可以连续 `TryFetch()`；事件数量不需要等于消息数量。

```text
Fill A -> notify
Fill B -> notify  -- these wake intents may collapse
Fill C -> notify

Processor wakes
  -> Fetch according to cursor and ring state
```

这种设计减少 scheduler event queue 的 payload 和条目数量，也避免“每条消息必须对应一次 OS wakeup”。它要求 buffer 与 event 的顺序严格正确，并要求消费者在处理一条后继续检查 backlog，而不是无条件重新睡眠。

[`CRoutine::updated_`](croutine-wakeup.md) 正是为休眠边界上的通知竞态服务：事件可以合并，但不能在“consumer 刚检查为空、正准备睡”时永久丢失。该事件位属于执行面，不替代本章 DataVisitor buffer 中的消息数量和游标。

## Dispatch 的结构性成本

设 registry 中有 `B_live` 只存活 buffer、`B_dead` 只过期 weak entry、`N` 个 notifier。结构成本近似为：

```text
Tdispatch = Thash_lookup
          + (B_live + B_dead) * Tweak_lock
          + B_live * (Tmutex + Tshared_ptr_assign + Tring_update)
          + N * Tnotify_callback
```

这不是实际时间公式，而是定位优化对象的分解。消息 payload 大小通常不直接进入 `Tshared_ptr_assign`，却会影响上游反序列化和最后析构；订阅者数量则直接放大 fan-out。

对 1 kHz channel，哪怕单次额外几十微秒也会吃掉明显周期预算。增加监控 Reader、录制 Reader 或调试观察者时，不能假定它们只消耗自己的 CPU；每个新 DataVisitor 都会让 producer 的 Dispatch 多写一只 buffer。

## 从控制系统看这种分发语义

多个消费者各有独立 ring，因此慢日志模块不会把控制 Component 的 cursor 向后拖。它可能增加 producer 的同步 fan-out 成本，却不会直接改变控制 buffer 的 queue depth。

通知发生在所有 buffer 写完之后，同一条消息对各 consumer 的可见顺序清楚。但各任务由 scheduler 分别执行，实际 callback 时间可能相差很大；“同时收到”不等于“同时处理”。

当 consumer 过载时，producer 通常继续覆盖该 consumer 的旧 ring，不等待 `Proc()`。这保护 transport 线程和其他订阅者，却把丢样本风险留给每个任务。控制链要监控数据时间戳和 drop，不应只监控 transport 是否仍有流量。

优先级也只从 Notifier 之后开始影响任务选择。Dispatcher fan-out 本身不按业务优先级排序；一个低重要性 observer 如果在 vector 前面并产生锁等待，仍可能推迟高重要性 Component buffer 的写入。

## 最小 Dispatcher/Notifier 接口

在已经实现 ring 的基础上，可以先定义三个小接口。下面是教学设计草图，不是 Apollo 源码，也不是完整可编译实现：

```cpp
using ChannelId = std::uint64_t;

template <typename T>
class Dispatcher {
 public:
  Registration Add(ChannelId, std::weak_ptr<Ring<T>>);
  bool Dispatch(ChannelId, const T& value);
};

class NotifierRegistry {
 public:
  Registration Add(ChannelId, std::function<void()> wake);
  void Notify(ChannelId);
};
```

`Registration` 应该能在析构时注销，防止历史项无限增长。Dispatcher 必须定义运行中 Add/Remove 与 Dispatch 的并发模型；如果第一版只支持启动期注册，应在 API 和状态机中明确禁止热插拔，而不是依赖口头约定。

Dispatch 保持“先写全部 buffer、后 Notify”。Notifier callback 不携带 `T`，Scheduler 只接收 task id。每个 callback 在执行前重新检查对应 ring，允许多个事件合并。

这个最小版本不需要 `AtomicHashMap`。先用 `unordered_map<ChannelId, vector<Entry>> + shared_mutex` 把语义写对，再根据读写比例替换 snapshot 或 RCU。数据结构优化应发生在并发契约明确之后。

## 从数据分发过渡到任务唤醒

到 `DataNotifier::Notify()` 返回为止，所有工作仍在 transport callback 所在线程。消息已经安全落进 ring，scheduler 也收到 task id，但 `Proc()` 还没有运行。

在[执行面章节](croutine-wakeup.md)中，从 `Scheduler::NotifyProcessor(task_id)` 开始，可以继续追踪 `SetUpdateFlag()`、condition variable、Classic run queue、`Processor::Run()` 和 RoutineFactory。那里回答最后一块空白：一个无参事件怎样跨过线程边界，最终让 DataVisitor 在另一只 OS 线程上取出本章写入的消息。
