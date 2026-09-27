# eCAL 阅读基础：发现、传输层与共享内存

eCAL 是面向高带宽数据流的发布订阅中间件。它与 LCM 最大的直观差异是：发送者不会永远固定使用一种传输。Publisher 先通过发现机制了解 Subscriber 在哪里、支持哪些 layer，再为本机或远端连接选择 SHM、UDP 或 TCP。

本文固定源码版本为 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。在进入具体类之前，先建立阅读后续源码需要的概念。

## 一条消息之前先发生发现

下面是一个教学调用例，展示应用入口；它不是本文所固定提交中的完整源码摘录：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
eCAL::CPublisher publisher("camera/image");
publisher.Send(image_data, image_size);
```

但 `Send()` 能走哪条路径，取决于此前已经交换的注册信息：

```text
Subscriber 周期广播自身 registration
  -> topic = camera/image
  -> host/process/entity identity
  -> SHM/UDP/TCP read capabilities

Publisher 收到 registration
  -> 找到同 topic 的本地 PublisherImpl
  -> 判断 subscriber 是同机还是异机
  -> 按优先级选择双方共同支持的 layer
  -> 必要时创建对应 writer
  -> 在后续 registration 中公布 writer 参数
```

数据面发送以前，控制面已经完成了“谁在订阅”和“用什么方式连接”的协商。

## 控制面与数据面

控制面传递少量元数据：进程、topic、类型、传输能力、TCP 端口、共享内存文件名和存活状态。它可以周期发送，允许一定延迟。

数据面传递真正业务 payload：图像、点云、状态或 protobuf bytes。它追求低延迟和高吞吐。

```text
Control plane
  registration -> match -> select transport -> update connection state

Data plane
  Send(payload) -> SHM / UDP / TCP -> Subscriber callback
```

把两者分开后，热路径不用每次重新查找全部订阅者能力。控制面维护丰富 connection map，数据面只检查每个 layer 的原子连接计数。

## Publisher facade 与实现对象

公共 `CPublisher` 是应用拿到的轻量句柄，真正状态位于 `CPublisherImpl`：

```text
CPublisher
  `-- weak_ptr<CPublisherImpl>

CPubGate
  `-- shared_ptr<CPublisherImpl>
```

下面是教学最小调用，不是上游源码摘录：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
if (auto impl = impl_weak.lock()) {
  impl->Write(...);
}
```

`weak_ptr` 不拥有对象。上面的 `lock()` 尝试取得临时 `shared_ptr`；如果对象已无强引用，结果为空。`lock()` 与取得强引用是一个不可分割的操作，不能先检查 `expired()` 再保存裸指针。

全局 Gate 通常是强引用根。`weak_ptr::lock()` 会在对象仍有强引用时临时增加强引用，使这一次 `Send()` 内对象不会被销毁。Gate 清空后，若没有在途调用或其他强引用，facade 的下一次 `lock()` 才会失败；若某个分发线程已取得局部强引用，Impl 仍可能活到那次调用结束。因而弱句柄解决的是“调用时对象内存是否仍活着”，并不单独保证 `Finalize()` 已等完所有回调或其依赖仍可用。

Subscriber 使用相同模式：公共 `CSubscriber` 持 weak pointer，`CSubGate` 持实现对象 strong pointer。

## Gate 是按 topic 建立的对象索引

PubGate 和 SubGate 可以理解为进程内注册表：

固定提交 `eclipse-ecal/ecal@1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 在两个 Gate 中分别声明如下；这些是两个头文件里的并列源码摘录。


```cpp
using TopicNamePublisherMapT = std::multimap<std::string, std::shared_ptr<CPublisherImpl>>;
std::shared_timed_mutex  m_topic_name_publisher_mutex;
TopicNamePublisherMapT   m_topic_name_publisher_map;
```

接着看 `CSubGate` 的真实实现：

```cpp
using TopicNameSubscriberMapT = std::unordered_multimap<std::string, std::shared_ptr<CSubscriberImpl>>;
std::shared_timed_mutex  m_topic_name_subscriber_mutex;
TopicNameSubscriberMapT  m_topic_name_subscriber_map;
```

这两个成员声明已经把关键差异直接写在代码里：PubGate 用有序 `multimap`，SubGate 用 `unordered_multimap`，二者的 topic 查询复杂度和迭代语义不同。固定提交中的对象分别是 `CPubGate` 与 `CSubGate`。

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
using PublisherIndex =
    std::multimap<std::string, std::shared_ptr<CPublisherImpl>>;
using SubscriberIndex =
    std::unordered_multimap<std::string, std::shared_ptr<CSubscriberImpl>>;
```

Key 是 topic name，value 是实现对象。同一 topic 可能有多个 Publisher 或 Subscriber，所以要保留同名键。真实源码没有让两种 Gate 共用一种容器：`CPubGate` 使用 `std::multimap`，`CSubGate` 使用 `std::unordered_multimap`。因此不能把一种容器的查找复杂度泛化到另一种。

两边的锁边界也不同。`CSubGate::ApplySample()` 在 shared lock 内查找并把匹配的 `shared_ptr` 复制进局部 vector，释放 Gate 锁后才调用 SubscriberImpl；vector 的强引用把对象保活到分发结束。`CPubGate::ApplySubscriberRegistration()` 则在 shared lock 仍持有时遍历 multimap 并直接调用 PublisherImpl，没有构造同样的快照。慢注册处理会延长该读锁持有时间，使需要 unique lock 的 Publisher 注册/注销等待。读者不能把 SubGate 数据分发的做法误套到所有 Gate 操作上。

下面直接并排看这两条真实调用路径。摘录来自固定提交 `eclipse-ecal/ecal@1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`，分别是 `CPubGate::ApplySubscriberRegistration` 和 `CSubGate::ApplySample`；每段均为对应函数的连续摘录。


```cpp
void CPubGate::ApplySubscriberRegistration(const Registration::Sample& ecal_sample_)
{
  if(!m_created) return;

  const auto&        ecal_topic = ecal_sample_.topic;
  const std::string& topic_name = ecal_topic.topic_name;

  // check topic name
  if (topic_name.empty()) return;

  // TODO: Substitute ProducerInfo type
  const auto& subscription_info = ecal_sample_.identifier;
  const SDataTypeInformation& topic_information = ecal_topic.datatype_information;

  CPublisherImpl::SLayerStates layer_states;
  for (const auto& layer : ecal_topic.transport_layer)
  {
    // transport layer versions 0 and 1 did not support dynamic layer enable feature
    // so we set assume layer is enabled if we receive a registration in this case
    if (layer.enabled || (layer.version < 2))
    {
      switch (layer.type)
      {
      case tl_ecal_udp:
        layer_states.udp.read_enabled = true;
        break;
      case tl_ecal_shm:
        layer_states.shm.read_enabled = true;
        break;
      case tl_ecal_tcp:
        layer_states.tcp.read_enabled = true;
        break;
      default:
        break;
      }
    }
  }

  std::string reader_par;
#if 0
  for (const auto& layer : ecal_sample.transport_layer())
  {
    // layer parameter as protobuf message
    // this parameter is not used at all currently
    // for subscriber registrations
    reader_par = layer.par_layer().SerializeAsString();
  }
#endif

  // register subscriber
  const std::shared_lock<std::shared_timed_mutex> lock(m_topic_name_publisher_mutex);
  auto res = m_topic_name_publisher_map.equal_range(topic_name);
  for(TopicNamePublisherMapT::const_iterator iter = res.first; iter != res.second; ++iter)
  {
    iter->second->ApplySubscriberRegistration(subscription_info, topic_information, layer_states, reader_par);
  }
}
```

registration sample 先被解析为订阅者身份、类型信息和 layer 能力；真正进入 Gate 锁后，函数按 topic 取出所有本地 Publisher，并在 shared lock 仍然持有时逐个调用 `ApplySubscriberRegistration`。这让“读取 topic 索引”和“更新某个 Publisher 的连接状态”处在同一个 Gate 读临界区中：并发读者可以共存，但要修改 topic map 的独占操作必须等它退出。慢连接更新因此可能推迟其他 topic map 写操作，即使它只影响一个 topic。这里没有 User callback；它调用的是中间件内部的 Publisher 实现，不过锁内调用仍然扩大了 Gate 锁的持有时间。

接着看 `CSubGate::ApplySample` 的真实实现：

```cpp
bool CSubGate::ApplySample(const Payload::TopicInfo& topic_info_, const char* buf_, size_t len_, long long id_, long long clock_, long long time_, size_t hash_, eTLayerType layer_)
{
  if (!m_created) return false;

  // apply sample to data reader
  size_t applied_size(0);
  std::vector<std::shared_ptr<CSubscriberImpl>> readers_to_apply;

  // Lock the sync map only while extracting the relevant shared pointers to the Datareaders.
  // Apply the samples to the readers afterward.
  {
    const std::shared_lock<std::shared_timed_mutex> lock(m_topic_name_subscriber_mutex);
    auto res = m_topic_name_subscriber_map.equal_range(topic_info_.topic_name);
    std::transform(
      res.first, res.second, std::back_inserter(readers_to_apply), [](const auto& match) { return match.second; }
    );
  }

  for (const auto& reader : readers_to_apply)
  {
    applied_size = reader->ApplySample(topic_info_, buf_, len_, id_, clock_, time_, hash_, layer_);
  }

  return (applied_size > 0);
}
```

这里每复制一个 `shared_ptr`，局部 vector 就取得该 SubscriberImpl 的一份强所有权。离开花括号时 Gate 的 shared lock 已释放；之后即使另一个线程把该 Subscriber 从索引注销，当前 vector 仍会让对象活到本轮 `ApplySample` 返回。与此不同，`buf_` 是函数参数中的裸 `const char*` 借用视图，vector 不拥有 payload；Gate 在返回前同步调用每个 reader，输入方必须保证这段内存一直有效。锁外调用缩短了全局索引临界区，但不代表所有后续锁都已释放：每个 `SubscriberImpl::ApplySample` 还有自己的 callback 和 connection-map 同步，慢处理仍会拖住该订阅实体，具体在《Subscriber 接收链》中沿调用继续追踪。

还要读出返回值的精确边界：循环不断覆盖同一个 `applied_size`，所以这个 bool 反映最后一个匹配 reader 的结果；它不是对全部 reader 结果做逻辑或。它不会改变 Gate 对匹配对象逐个分发的动作，却限制了调用方能从该返回值推断出的信息，不能把它讲成“至少一个订阅者交付成功”。

## transport layer 是数据路径策略

eCAL 在该版本中主要使用：

- SHM：同机共享内存，适合大 payload；
- UDP：无连接网络数据报，适合低延迟广播；
- TCP：面向连接的可靠字节流，适合需要可靠性的远端订阅。

选择 layer 不只是选择 API。它会改变复制次数、阻塞位置和失败语义：

| Layer | 数据位置 | 可靠性 | 慢消费者影响 |
|---|---|---|---|
| SHM | 命名共享内存文件 | 同机内存可见性与事件同步 | 可能占用/等待共享 buffer |
| UDP | 数据报及应用分片 | 不保证到达和顺序 | 通常不反压发送者，可能丢包 |
| TCP | 每连接字节流 | 有序可靠传输 | 队列覆盖、socket backpressure |

一个 Publisher 可以同时启用多层：本机 Subscriber 走 SHM，另一台主机走 UDP 或 TCP。

## 发现采用软状态

eCAL 不把一次 registration 当成永久事实。每个实体周期重复声明自己存在，接收端记录最后看见时间。长时间没有刷新时，timeout 逻辑把实体视为离线。

```text
registration arrives -> entity alive, refresh deadline
periodic refresh      -> extend deadline
explicit unregister   -> remove now
process crash         -> no unregister, remove after timeout
```

这叫 soft state。它能从进程崩溃和网络短暂丢包中恢复，不需要中央服务器维护绝对真相。

代价是上线、下线都不是瞬时全局一致；超时太短会误判抖动，太长会保留失效连接。

## registration sample 与业务 sample

源码中两类 sample 不应混淆：

- registration sample：描述实体、topic 和 layer 参数；
- payload sample：真正发送给业务 callback 的数据。

Registration receiver 把控制样本交给 PubGate/SubGate，改变 connection map 和 reader/writer 状态。Reader layer 把 payload sample 交给 SubGate，再进入 `SubscriberImpl::ApplySample()`。

二者可能都使用 SHM 或 UDP 传送，但目的不同。

## layer capability 与 layer selection

Publisher 和 Subscriber 都声明某层是否 enabled。选择算法按本机或远端优先级，从高到低寻找第一个交集：

```text
local priority:  [SHM, TCP, UDP]
publisher:       [SHM=yes, TCP=yes, UDP=yes]
subscriber A:    [SHM=yes, TCP=yes, UDP=no]
selected A:      SHM

remote priority: [TCP, UDP]
subscriber B:    [SHM=no, TCP=no, UDP=yes]
selected B:      UDP
```

同一 Publisher 的不同连接可以选择不同 layer。选择结果保存在 connection entry 中，而不是 Publisher 全局只有一个 transport。

## connection map 与原子计数器

控制面需要知道每个 subscriber 的 host、process、entity、状态和 selected layer，所以使用 map。

如果每次 `Send()` 都遍历 map，发送热路径会随着订阅者数量增长。eCAL 额外维护每层连接数：

```text
shm_connection_count
udp_connection_count
tcp_connection_count
```

发现线程修改 connection map 时同步更新计数；发送线程只检查 `count > 0`，快速决定要调用哪些 writer。

这是“丰富控制状态 + 紧凑数据面摘要”的通用模式。

## 共享内存不是一块无锁数组

SHM writer 创建命名内存文件，Subscriber 根据 registration 中的名称打开同一对象。双方还需要：

- header：记录 payload 长度、clock 和元数据；
- named mutex：跨进程保护读写；
- event：通知新数据到达；
- buffer rotation：避免只有一块内存时读写完全串行；
- 可选 ACK：发布者等待订阅进程确认读取。

```text
Publisher process                 Subscriber process
-----------------                 ------------------
lock named mutex
write header + payload
unlock
signal named event ----------->   observer wakes
                                   lock named mutex
                                   read/view payload
                                   callback
                                   unlock
                                   optional ACK event
```

这里至少有两个独立边界：发布端是否把输入直接写入映射目标，以及接收端是否把映射内容复制到本地缓冲。它们不能合并成一个“零拷贝开关”。接收端若直接把映射指针交给 callback，指针只在相应同步读取/回调期间有效，而且通常仍持有跨进程读锁；这不表示没有锁，也不表示 callback 可以永久保存指针。固定版本的具体 copy 路径见[SHM 数据路径](shm-memory-protocol.md)。

## buffer rotation 降低读写冲突

若只有一块共享内存，Subscriber callback 正在读时，Publisher 必须等待同一 named mutex。多 buffer 允许写端轮换：

```text
buffer 0: reader A still using
buffer 1: free, publisher writes next sample
buffer 2: previous complete sample
```

Buffer 数量增加会提高并发余量，也增加共享内存占用。它不能无限保护慢消费者；所有 buffer 都被占用时，发布者仍要等待、覆盖或失败，具体取决于实现策略。

## callback 模式与同步 Read 模式

Subscriber 可以注册 callback，也可以主动 `Read()`。该版本的核心语义不是两者同时得到每条消息：

- 有 callback 时，`ApplySample()` 直接调用 callback；
- 没 callback 时，payload 被复制到一个 read slot；
- 后续新消息覆盖 slot 中旧消息；
- `Read()` 取走 slot 并清除 ready 标志。

这更像 latest-value mailbox，不是 FIFO queue。慢 `Read()` 用户会丢中间状态，但能较快追上最新值。

## 锁外快照与锁内 callback 的区别

Gate 锁只用于查找并复制实现对象，用户 callback 不持有 gate 锁。这避免一只慢 callback 阻塞整个进程的 topic 注册表。

SubscriberImpl 自己还有 receive callback mutex，用于串行化同一订阅的多条传输输入。固定版本在取得 connection metadata 后还会持有 connection-map mutex 同步执行用户 callback；因此慢 callback 会阻塞同一 Subscriber 的下一条样本，也会拖住该 Subscriber 的连接注册/注销更新。若 callback 重入会取得同一 map mutex 的操作，还可能自锁。Gate 快照只缩短 Gate 的锁域，并没有把所有回调锁都移出。

阅读锁时必须说清是哪一层：全局 gate 锁、单 subscriber 锁、connection map 锁，还是跨进程 SHM mutex。笼统写“有锁保护”无法推导阻塞范围。

## 去重连接多条传输路径

同一消息可能因过渡状态或多层启用从 SHM、UDP、TCP 到达。Publisher 为发送生成 entity id、clock 和 hash，Subscriber 根据 publisher identity 与 clock 判断是否已经处理。

去重必须发生在业务 callback 前，否则切换 layer 时应用可能收到重复样本。

它也意味着“某层 reader 收到 bytes”不等于“callback 获得消息”。样本还可能因重复、layer disabled 或 id filter 被丢弃。

## 线程地图

```text
Application Thread
  CPublisher::Send
    -> PublisherImpl::Write
       -> SHM/UDP/TCP writer

Registration Thread(s)
  receive periodic entity samples
    -> PubGate/SubGate
    -> update connections and layer parameters

UDP receive handler/thread
  -> UDP reader layer -> SubGate -> SubscriberImpl

TCP executor callback (实现位于该源码树之外，不能由本仓库断言其 drain 语义)
  -> TCP reader -> SubGate -> SubscriberImpl

SHM Observer Thread
  -> wait event -> lock memfile -> SubGate -> SubscriberImpl
```

同一 SubscriberImpl 可能被多个传输线程并发调用，因此它需要自己的串行化与去重状态。

## 后续阅读的四个固定问题

每进入一个 eCAL 函数，都先回答：

1. 这是控制面还是数据面；
2. 当前运行在哪个线程；
3. Payload 是复制、共享映射还是借用指针；
4. 慢 callback 会阻塞当前对象、当前 layer，还是整个进程。

后续章节将按这四个问题依次拆解 Publisher 发现与发送、Subscriber 汇聚、SHM 内存协议和全局生命周期。
