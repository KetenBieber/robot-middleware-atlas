# Registration 控制面：周期软状态、超时与连接状态机

Publisher 与 Subscriber 需要在没有中央协调器的情况下相遇。eCAL 通过周期 registration 解决：每个进程不断广播当前实体快照，接收者刷新过期时间并把变化交给各个 Gate。

这套机制允许异常退出后自动收敛，也意味着“连接存在”不是一次报文写下的永久事实，而是必须持续续租的软状态。

本章固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

## 发送侧同时发布快照与增量

`CRegistrationProvider` 启动周期线程。每次 tick：

```text
create sample list
  -> add current process registration
  -> ask PubGate for current publishers
  -> ask SubGate for current subscribers
  -> ask service/client gates
  -> append queued explicit register/unregister samples
  -> clear one-shot queue
  -> send complete list through registration transport
```

周期快照提供最终一致性：即使某次新增通知丢失，下一轮完整实体列表仍会再次声明它。

显式样本降低常见变化的发现延迟。`RegisterSample()` 入队后会主动 trigger 发送线程；`UnregisterSample()` 只入队，通常等下一周期发送。每次发送仍重新构造本进程当前完整实体描述，再把队列里的单次 register/unregister 样本附加到同一批次，并非只有增量流。

周期线程把增量队列搬到当前快照时有一个需要源码读者注意的同步边界。`AddSingleSample()` 用 `m_applied_sample_list_mtx` 保护 `push_back`，但固定版本 `RegisterSendThread()` 先在锁外调用 `empty()`，只有确认非空后才加锁复制并清空：


```cpp
// append applied samples list to sample list
if (!m_applied_sample_list.empty())
{
  const std::lock_guard<std::mutex> lock(m_applied_sample_list_mtx);
  std::copy(m_applied_sample_list.begin(), m_applied_sample_list.end(), std::back_inserter(m_send_thread_sample_list));
  m_applied_sample_list.clear();
}
```

若另一个 API 线程恰好在 `empty()` 读取 vector size 时执行 `push_back`，它们之间没有共同锁同步，按 C++ 内存模型属于 data-race 候选；这不是对作者意图的判断，而是从这两个访问点推出的风险。一个缩小版修复是无条件取得同一把 mutex，再在锁内检查、复制并清空；复制出的样本列表随后在锁外发送，避免把网络发送也放进队列锁域。源码只在增量片段前后省略了本轮周期快照收集和发送，关键共享状态访问没有删节。

## Soft state 的租约模型

每个远端实体都可以理解为一份带截止时间的租约：

```text
first registration  -> create lease, deadline = now + timeout
refresh              -> move deadline forward
explicit unregister  -> remove immediately
no refresh           -> expiration thread removes after deadline
```

它不要求进程崩溃时还能发送“我已退出”。只要刷新停止，系统最终删除失效实体。

代价是故障检测时间至少接近 timeout。Timeout 过短会把网络抖动误判成离线；过长会让 Publisher 继续向不存在的连接写数据。

## Registration transport 与 payload transport 分离

Registration 可以经 SHM 或 UDP 接收，但它描述的是控制状态，而不是业务 payload。

```text
registration receiver thread
  -> deserialize list of entity samples
  -> CSampleApplier
  -> multiple control consumers
```

即使业务 topic 选择 TCP，Publisher/Subscriber 相遇仍依赖 registration transport。排查“TCP 没数据”时，可能根因是控制面 registration 被防火墙或 domain 配置隔离，而不是 TCP socket 本身。

## CSampleApplier 是控制面分发器

`CSampleApplier` 先执行接受过滤，再把样本分发给命名 callback 集合：

```text
"gates"    -> Publisher/Subscriber/Service/Client gates
"timeout"  -> refresh expiration state
"descgate" -> datatype descriptions
"monitor"  -> monitoring view
```

一条 registration 因此可以同时改变连接、刷新寿命并更新监控。

`CSampleApplier::ApplySample()` 持 callback-map mutex 执行所有 consumers。下面是从过滤成功到返回的完整固定提交函数：


```cpp
bool CSampleApplier::ApplySample(const Registration::Sample& sample_)
{
  if (!AcceptRegistrationSample(sample_))
  {
    Logging::Log(Logging::log_level_debug1, "CSampleApplier::ApplySample : Incoming sample discarded");
    return false;
  }

  // forward all registration samples to outside "customer" (e.g. monitoring, descgate, pub/subgate/client/service gates)
  {
    const std::lock_guard<std::mutex> lock(m_callback_custom_apply_sample_map_mtx);
    for (const auto& iter : m_callback_custom_apply_sample_map)
    {
      iter.second(sample_);
    }
  }
  return true;
}
```

锁保护的是 callback map 的迭代期，避免同时增删节点让迭代器失效；它也意味着每个 consumer 的工作时间都串在 registration 接收线程上。假设第一个 callback 同步做 200 ms 的磁盘操作，后续 gates 收到 registration 至少推迟这段时间；若 callback 内重入 `SetCustomApplySampleCallback()` 或 `RemCustomApplySampleCallback()`，它会再次请求同一普通 mutex 并自锁。源码事实是“锁内同步调用”；锁外复制 callback 后调用是可选改进，需额外约定 callback 注销并发时旧副本是否仍可运行。

## 接受过滤决定可见域

Registration sample 不是无条件接受。实现先判断 sample 是否属于 SHM 可见域：同 host 或相同 SHM transport domain 都算成员。成员 sample 只有来自其他 process 才无条件接受；本进程自己的 sample 要看 loopback。既非同 host、也非同 domain 的 sample 才由 `network_enabled` 决定接受与否。换言之，这些不是并列过滤开关，而是不同分支条件；见 `CSampleApplier::AcceptRegistrationSample()`。Topic name 相同但处于不同可见域的进程可能永远不会建立连接。

可见域属于部署拓扑，不属于业务 topic API。诊断工具应把“收到但过滤”与“从未收到”区分开。

## ExpirationMap 的双索引结构

Timeout provider 需要两种操作：按 entity 快速刷新，以及按时间顺序快速找出已过期项。

单一 `std::map<Identity, Entry>` 便于查 identity，却无法快速找到最早 deadline。单一按时间排序结构又不便更新指定 entity。

`CTimeoutProvider` 用默认模板参数实例化 `CExpirationMap<SampleIdentifier, Sample, steady_clock>`；默认 `MapType` 是 `std::map`。内部使用：

```text
ordered map: identity -> entry + list iterator
recency list: oldest -----------------> newest
```

Refresh 时在 map 中查找约 `O(log N)`，再用 list iterator `splice` 到尾部 `O(1)` 并更新时间戳。过期扫描从头开始，遇到第一个未过期项即可停止；删除过期项还要从有序 map 中按 key 删除。

下面的固定实现展示两条索引怎样保持一致：过期时从 map 取出值、同时删除 map/list 节点；refresh 时不搬动 map entry，而是把对应 list 节点移到尾部并更新时间戳。


```cpp
std::map<Key, T> erase_expired()
{
  std::map<Key, T> erased_values;
  const auto eviction_limit = get_curr_time() - _timeout;
  auto it = _access_timestamps_list.begin();
  while (it != _access_timestamps_list.end() && it->timestamp < eviction_limit)
  {
    auto erased_value = _internal_map.find(it->corresponding_map_key);
    erased_values[it->corresponding_map_key] = erased_value->second.map_value;
    _internal_map.erase(it->corresponding_map_key);
    it = _access_timestamps_list.erase(it);
  }
  return erased_values;
}

void update_timestamp(const typename InternalMapType::iterator& it_in_map)
{
  auto& it_in_list = it_in_map->second.timestamp_list_iterator;
  _access_timestamps_list.splice(_access_timestamps_list.end(),
                                 _access_timestamps_list, it_in_list);
  it_in_list->timestamp = get_curr_time();
}
```

两个容器必须在同一个 tracker mutex 下一起更新，否则 map 可能指向已经释放的 list iterator，或过期扫描遇到找不到对应 value 的 timestamp 节点。这里的时间是 `steady_clock`，避免系统墙上时钟回拨把租约突然延长或提前过期。实现只在最旧时间戳已越过截止线时继续扫描，不必遍历仍有效的后缀。

这是 LRU/expiry index 常见的“双结构一致性”模式。删除时必须同时移除 map 节点和 list 节点。

## 超时被转换成合成 Unregister

Expiration thread 不直接操作 PubGate/SubGate 内部 map。它生成与真实 unregister 相同形状的 registration sample，再交回 `CSampleApplier`：

```text
lease expires
  -> construct synthetic unregister sample
  -> CSampleApplier::ApplySample
  -> gates receive normal unregister path
```

这避免“显式退出”和“超时退出”维护两套清理逻辑。连接事件、layer counter 和 reader resource 都沿同一状态转换处理。

统一命令形状是 event-sourced 系统可复用的设计：不同原因先归一成同一领域事件，下游不关心事件是网络发来还是本地合成。

真实 timeout provider 先在 `sample_tracker_mutex` 下取出并删除一批超期 sample，然后释放锁，再逐个把 synthetic unregister 交给普通 apply callback。收到正常 refresh 时，provider 则在同一 tracker mutex 下查找 identity，缺失就插入合成注销记录，已存在就刷新它在过期 list 中的位置：


```cpp
void CheckForTimeouts()
{
  std::map<Registration::SampleIdentifier, Registration::Sample> expired_samples;
  {
    std::lock_guard<std::mutex> lock(sample_tracker_mutex);
    expired_samples = sample_tracker.erase_expired();
  }

  for (const auto& registration_sample : expired_samples)
  {
    apply_sample_callback(registration_sample.second);
  }
}

void UpdateOrInsertSample(const Sample& sample_)
{
  std::lock_guard<std::mutex> lock(sample_tracker_mutex);
  auto element = sample_tracker.find(sample_.identifier);
  if (element == sample_tracker.end())
    sample_tracker.insert({ sample_.identifier, CreateUnregisterSample(sample_) });
  else
    sample_tracker.update(element);
}
```

这个锁边界防止控制面 callback 在 tracker 锁内执行：callback 最终会进入 `CSampleApplier` 和 Gate，若在那里等待其他 registration 状态，timeout provider 就不会因一条慢 callback 而卡住所有 refresh。代价是到期删除与注销应用不是一个原子事务。比如线程 A 已从 tracker 取出旧租约并解锁，线程 B 收到新 refresh、刷新并应用 Register，随后线程 A 才应用旧 synthetic Unregister；短时间内 Gate 会移除刚恢复的实体，直到下一轮周期快照再次注册。文章只能把软状态写成最终收敛机制，不能声称这两个线程间不存在过期事件越过新 refresh 的窗口。

## Connection identity 的组成

理想 identity 应包含 host、process、entity 或一个真正全局唯一 ID。该源码快照的部分 `SampleIdentifier` 比较主要依赖 entity id，并注释其全局唯一假设。

如果两个进程随机生成相同 entity id，connection map 和 timeout state 可能把它们合并。概率或许很低，但安全设计应让 key 明确包含命名空间：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct EntityKey {
  HostId host;
  ProcessId process;
  EntityId entity;
};
```

Hash 与 equality 必须使用相同字段。日志和监控也应输出完整 identity，便于诊断碰撞与重启。

## Subscriber 侧的两阶段激活

Publisher registration 到达 SubGate 后，先把 TCP 端口、SHM memfile list 等 layer parameters 交给 reader layer，再更新每个 SubscriberImpl 的 connection map。

状态机为：

```text
ABSENT
  -- first registration --> SEEN_INACTIVE

SEEN_INACTIVE
  -- next registration --> ACTIVE + connected event
  -- unregister/timeout -> ABSENT

ACTIVE
  -- refresh -----------> ACTIVE
  -- unregister/timeout -> ABSENT + disconnected event
```

第一次样本建立记录但不增加公共 publisher count，第二次持续出现才确认 active。这会增加约一个 refresh 周期的 connected 延迟，却过滤只出现一次的瞬态样本。

源码没有在状态机旁完整说明设计意图，因此应把“抑制瞬态”视为合理解释，而把“两次 registration 才 active”视为确定行为。

`CSubscriberImpl` 的实现把第一次样本写为 inactive，后续样本更新同一连接时才把它置 active；count 在锁内从 connection map 重算，connect event 则在锁释放后触发：

接着看 `CSubscriberImpl::ApplyPublisherRegistration` 的真实实现：

```cpp
void CSubscriberImpl::ApplyPublisherRegistration(
  const SPublicationInfo& publication_info_,
  const SDataTypeInformation& data_type_info_,
  const SLayerStates& pub_layer_states_)
{
  bool is_new_connection = false;
  {
    const std::lock_guard<std::mutex> lock(m_connection_map_mtx);
    auto publication_info_iter = m_connection_map.find(publication_info_);
    if (publication_info_iter == m_connection_map.end())
    {
      m_connection_map[publication_info_] = SConnection{ data_type_info_, pub_layer_states_, false };
    }
    else
    {
      auto& connection = publication_info_iter->second;
      if (!connection.state) is_new_connection = true;
      connection = SConnection{ data_type_info_, pub_layer_states_, true };
    }
    m_connection_count = GetConnectionCount();
  }

  if (is_new_connection)
    FireConnectEvent(publication_info_, data_type_info_);
}
```

锁保护的是同一 Subscriber 的连接 map 与由它派生的 count；event callback 被移到该 mutex 外，避免它同步执行期间长期占住 connection state。这个设计仍依赖外层 `CSampleApplier`/Gate 的锁域，不能仅凭这一段说用户 callback 完全不在其他管理锁下运行。

## Publisher 侧的连接激活

PublisherImpl 也为 Subscriber 维护 connection entry。首次 registration 已经选层并可能增加内部发送 layer counter，后续 refresh 才进入 established 和公共 subscriber count。

这形成两个时间概念：

- data path layer 已准备；
- public connection event 已确认。

公共 `Send()` 若要求 established count 大于零，就不会在 pending 窗口发送业务数据。自研实现应避免一半代码检查 layer counter、另一半检查 public count，却没有文档说明二者差别。

## 注册更新带来传输资源变化

Publisher registration 不只更新“在线”状态，还可能改变：

- TCP listening port；
- SHM memory-file list；
- datatype descriptor；
- layer enable/active flags；
- process/host metadata。

Subscriber reader layer 应先应用 layer parameter，再宣布 connection active。否则 connected callback 触发后，业务立刻发送/读取却发现 TCP endpoint 尚未配置。

Publisher 一侧还把“layer 已可发送”与“subscriber 已建立连接”分开：首条 registration 选择并启动 writer、写入 connection map 的 `pending` 项、增加 layer counter；第二条 refresh 才把 `pending` 改成 `established` 并增加 public count。以下摘录保留了这个状态边界：

接着看 `CPublisherImpl::ApplySubscriberRegistration` 的真实实现：

```cpp
void CPublisherImpl::ApplySubscriberRegistration(
  const SSubscriptionInfo& subscription_info_,
  const SDataTypeInformation& data_type_info_,
  const SLayerStates& sub_layer_states_,
  const std::string& reader_par_)
{
  std::vector<eTLayerType> pub_layers;
  std::vector<eTLayerType> sub_layers;
#if ECAL_CORE_TRANSPORT_UDP
  if (m_attributes.udp.enable)            pub_layers.push_back(tl_ecal_udp);
  if (sub_layer_states_.udp.read_enabled) sub_layers.push_back(tl_ecal_udp);
#endif
#if ECAL_CORE_TRANSPORT_SHM
  if (m_attributes.shm.enable)            pub_layers.push_back(tl_ecal_shm);
  if (sub_layer_states_.shm.read_enabled) sub_layers.push_back(tl_ecal_shm);
#endif
#if ECAL_CORE_TRANSPORT_TCP
  if (m_attributes.tcp.enable)            pub_layers.push_back(tl_ecal_tcp);
  if (sub_layer_states_.tcp.read_enabled) sub_layers.push_back(tl_ecal_tcp);
#endif

  const TransportLayer::eType layer = DetermineTransportLayer(
    pub_layers, sub_layers, m_attributes.host_name == subscription_info_.host_name);
  switch (layer)
  {
  case TransportLayer::eType::udp_mc: StartUdpLayer(); break;
  case TransportLayer::eType::shm:    StartShmLayer(); break;
  case TransportLayer::eType::tcp:    StartTcpLayer(); break;
  default: break;
  }

#if ECAL_CORE_TRANSPORT_UDP
  if (m_writer_udp) m_writer_udp->ApplySubscription(
    subscription_info_.host_name, subscription_info_.process_id,
    subscription_info_.entity_id, reader_par_);
#endif
#if ECAL_CORE_TRANSPORT_SHM
  if (m_writer_shm) m_writer_shm->ApplySubscription(
    subscription_info_.host_name, subscription_info_.process_id,
    subscription_info_.entity_id, reader_par_);
#endif
#if ECAL_CORE_TRANSPORT_TCP
  if (m_writer_tcp) m_writer_tcp->ApplySubscription(
    subscription_info_.host_name, subscription_info_.process_id,
    subscription_info_.entity_id, reader_par_);
#endif

  bool is_new_connection = false;
  {
    const std::lock_guard<std::mutex> lock(m_connection_map_mutex);
    auto subscription_info_iter = m_connection_map.find(subscription_info_);
    if (subscription_info_iter == m_connection_map.end())
    {
      m_connection_map[subscription_info_] = SConnection{
        data_type_info_, sub_layer_states_, layer, eConnectionState::pending };
      m_send_layer_connection_counters.Increment(layer);
    }
    else
    {
      auto& connection = subscription_info_iter->second;
      if (connection.state == eConnectionState::pending)
      {
        is_new_connection = true;
        m_connection_count.fetch_add(1, std::memory_order_relaxed);
        connection.state = eConnectionState::established;
      }
      connection.data_type_info = data_type_info_;
      connection.layer_states = sub_layer_states_;
    }
  }

  if (is_new_connection)
    FireConnectEvent(subscription_info_, data_type_info_);
}
```

首条 registration 先做 transport 选择和 writer 的 `ApplySubscription()`，再进入 map 锁写 `pending` 与 relaxed layer counter；它还没有执行 `m_connection_count.fetch_add()`。refresh 命中 pending 时才增加连接数、改状态，并在锁外发 connect event。因此 Send 快路径的 layer counter 与公共连接计数回答不同问题。与此同时，writer 启动/读取发生在 connection-map mutex 之外；这把锁不保护 `m_writer_*` 指针的并发发布，发送线程与 registration 线程的 writer 生命周期还需要单独核对。

## Event callback 的锁域

Connected/disconnected callback 是用户代码。若在 Gate shared lock、connection-map mutex 或 SampleApplier callback-map mutex 内同步执行，慢 callback 会阻塞控制面推进。

更稳健的结构为：

```text
lock state
  -> apply transition
  -> copy Event object + callback handle
unlock state
enqueue event to bounded executor
```

异步事件会改变“注册函数返回前 callback 已完成”的兼容行为，所以需要在 API 文档中明确。至少应避免用户 callback 反向调用同一锁域生命周期 API。

固定实现的 event callback 把几层锁叠在一起。以 Publisher connect 事件为例，registration receiver 的 `CSampleApplier::ApplySample()` 持 callback-map mutex 调 Gate；`CPubGate::ApplySubscriberRegistration()` 持 topic shared lock 调 PublisherImpl；Impl 更新 connection map 后虽已释放自己的 map mutex，但同步进入 `FireEvent()`。`FireEvent()` 先在 event mutex 外读 `std::function`，之后取得 event mutex，并在仍持锁时调用用户 callback：


```cpp
bool CPublisherImpl::SetEventCallback(const PubEventCallbackT& callback_)
{
  if (!m_created) return false;
  const std::lock_guard<std::mutex> lock(m_event_id_callback_mutex);
  m_event_id_callback = callback_;
  return true;
}

bool CPublisherImpl::RemoveEventCallback()
{
  if (!m_created) return false;
  const std::lock_guard<std::mutex> lock(m_event_id_callback_mutex);
  m_event_id_callback = nullptr;
  return true;
}

void CPublisherImpl::FireEvent(const ePublisherEvent type_,
                               const SSubscriptionInfo& subscription_info_,
                               const SDataTypeInformation& data_type_info_)
{
  if (m_event_id_callback)
  {
    SPubEventCallbackData data;
    data.event_type = type_;
    data.event_time = eCAL::Time::GetMicroSeconds();
    data.subscriber_datatype = data_type_info_;

    STopicId topic_id;
    topic_id.topic_id.entity_id = subscription_info_.entity_id;
    topic_id.topic_id.process_id = subscription_info_.process_id;
    topic_id.topic_id.host_name = subscription_info_.host_name;
    topic_id.topic_name = m_attributes.topic_name;
    const std::lock_guard<std::mutex> lock(m_event_id_callback_mutex);
    m_event_id_callback(topic_id, data);
  }
}
```

这里至少有两个具体风险。第一，接收线程处理一次 connect/unregister 样本时，一个线程执行 `if (m_event_id_callback)`，另一个线程在同一 `std::function` 上赋值或清空；前一次读取不受 callback mutex 保护，两个普通对象访问之间没有 happens-before，属于 data-race 候选。即使 if 读到非空，Remove 也可能在 FireEvent 拿锁前清空函数，随后对空函数执行 `operator()`。第二，用户的 connected callback 若同步调用 `RemoveEventCallback()`，FireEvent 持普通非递归 mutex 调用 callback，callback 又请求同一 mutex，会自锁；若 callback 销毁同一个 Publisher，Gate 的注销还会请求正在持有的 topic 索引独占锁。Subscriber 的 `FireEvent/SetEventCallback/RemoveEventCallback` 使用同样的锁序列，且 SubGate 在 shared lock 下同步调用它。修复需要在 callback mutex 内复制一个局部 `std::function` 后解锁再调用，并且 Gate/SampleApplier 也要把 callback 脱离其管理锁域；但局部副本意味着已经开始的 callback 可能晚于 Remove 返回仍继续运行，若 API 要求“Remove 返回后 callback 全部退出”，还须另设 in-flight 计数与等待屏障。

## 时间复杂度与规模边界

设已知实体 N，本次过期 K 个，同 topic 本地对象 T：

```text
refresh expiry entry       O(log N) + O(1) list splice
erase K expired            O(K log N)
gate lookup/fan-out         map-dependent + O(T)
periodic full snapshot      O(number of local entities)
```

Registration 不是每条 payload 的热路径，但实体很多、refresh 很短时会形成持续控制流量与序列化成本。

每进程周期发送完整快照使恢复简单，规模极大时则需要增量序列、版本向量或集中目录。eCAL 的取舍更适合局域网内有限机器人进程集合。

## 可复刻的最小软状态表

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class LeaseTable {
 public:
  std::vector<EntityKey> Refresh(EntitySample sample, TimePoint now);
  std::vector<EntityKey> Expire(TimePoint now);

 private:
  struct Entry {
    EntitySample sample;
    TimePoint deadline;
    RecencyList::iterator order;
    enum { Seen, Active } state;
  };

  std::unordered_map<EntityKey, Entry> entries_;
  RecencyList by_deadline_;
};
```

先实现单线程纯状态机，用虚拟时钟验证 first/second refresh、explicit unregister 和 timeout。再把产生的 `Connected`/`Disconnected` 事件交给 Gate。最后才加入网络 receiver 和周期线程。

## Registration 的设计结论

eCAL 控制面通过周期快照获得崩溃恢复，通过显式增量降低正常变化延迟，通过 ExpirationMap 把失联转成统一 unregister，再由 Gate 将实体状态转换成具体 reader/writer 资源。

它的优点是无中心、可自愈、传输参数可动态传播；代价是最终一致、发现延迟和多个锁域中的用户事件。设计同类系统时，最重要的是把租约、连接状态和数据路径 ready 状态分开命名，而不是统称“已连接”。
