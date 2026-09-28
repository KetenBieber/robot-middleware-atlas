# Subscriber 接收链：多层汇聚、去重与回调交付

同一 `CSubscriber` 可能同时面对 UDP 接收线程、TCP executor 和多个 SHM observer。eCAL 需要把不同传输带来的样本统一成同一业务语义，同时避免重复消息、限制 callback 并发，并支持 callback 与同步 `Read()` 两种消费方式。

本章固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

## Subscriber 的对象所有权

公共 `CSubscriber` 持有 `weak_ptr<CSubscriberImpl>`，全局 `CSubGate` 的 topic multimap 持有 strong pointer。

```text
CSubscriber facade --weak--> CSubscriberImpl
                               ^
                               |
CSubGate topic index ------shared
```

构造过程先取得全局 registration provider 和 reader layers，创建实现对象，再注册到 SubGate。固定提交中的 Impl 先初始化并启动 reader layers、置 `m_created=true`，外层随后才把它插入 SubGate；这个短窗口里 reader 已能到达，但 Gate 尚无对应对象，所以该 sample 会被丢弃，不存在构造缓存或重放承诺。Unregister 后，Gate 删除强引用；正在分发的局部 shared pointer 快照仍能保护对象到当前调用结束。这个次序决定了启动阶段没有“先到样本暂存起来等待注册完成”的行为。

## 实现对象连接三类 reader layer

`CSubscriberImpl` 构造时生成 entity id 和 topic identity，并根据配置调用每个全局 reader layer 的 `AddSubscription()`。

三条 layer 的资源粒度不同：

- UDP：相同 topic 共享 sample receiver 与组播资源；
- TCP：为 topic 建 reader，远端地址由 Publisher registration 补入；
- SHM：还不知道 memfile name，等待 Publisher registration 公布后创建 observer。

Subscriber API 统一，并不意味着底层建立连接的时机一致。

## 三条输入路径汇聚到 SubGate

```text
UDP receive/reassembly thread
  -> CUDPReaderLayer::ApplySample(serialized sample)
  -> CSubGate::ApplySample

TCP executor worker
  -> CDataReaderTCP::OnTcpMessage
  -> split eCAL header and payload
  -> CSubGate::ApplySample

SHM observer thread
  -> wait named event
  -> lock memory file
  -> parse header and payload view
  -> CSHMReaderLayer::OnNewShmFileContent
  -> CSubGate::ApplySample
```

SubGate 是多层共同入口。它先得到 topic name，再查本进程所有匹配 SubscriberImpl。

## Gate 使用快照后调用

`CSubGate::ApplySample()` 在 shared lock 下只完成查找与 shared pointer 复制。下面是该函数从查找至返回的固定提交源码摘录：


```cpp
bool CSubGate::ApplySample(const Payload::TopicInfo& topic_info_, const char* buf_, size_t len_, long long id_, long long clock_, long long time_, size_t hash_, eTLayerType layer_)
{
  if (!m_created) return false;

  // apply sample to data reader
  size_t applied_size(0);
  std::vector<std::shared_ptr<CSubscriberImpl>> readers_to_apply;

  // Lock the sync map only while extracting the relevant shared pointers to the Datareaders.
  // Apply the samples to the readers afterwards.
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

`std::shared_lock` 在 map 查找和 vector 填充期间阻止并发写索引；每次 `shared_ptr` 复制都会让对应 `CSubscriberImpl` 多一个强所有者。花括号结束时 Gate 锁释放，但 vector 还活着，所以随后执行的回调期间不会持 Gate map 锁，Impl 也不会因并发 Unregister 而析构。循环结束、vector 析构才逐个减引用。注意 `applied_size` 被每个 target 覆盖，返回值只看最后一个匹配 reader，并不是“任一业务 callback 成功”的聚合确认；调用者不能用它统计交付数量。

用户 callback 不持有 Gate 锁。否则一个耗时图像处理函数会阻塞整个进程注册/注销其他 topic。

局部 strong pointer 也解决并发 unregister：Gate 可以立即移除索引，当前快照中的实现对象等本次调用结束再析构。

## ApplySample 是接收语义中心

`CSubscriberImpl::ApplySample()` 的逻辑顺序可以简化为：

**教学伪代码（不是固定提交源码摘录）：**

```cpp
lock(receive_mutex);

if (!created) return rejected;
if (!layer_enabled(source_layer)) return rejected;
if (already_seen(publisher, clock)) return duplicate;
if (!id_filter_accepts(sample_id)) return rejected;

mark_layer_active(source_layer);
update_drop_counter(publisher, clock);
update_frequency_latency_size(...);

if (receive_callback) {
  receive_callback(topic_info, payload_view);
} else {
  read_slot.assign(payload, payload + size);
  read_slot_ready = true;
  read_cv.notify_one();
}
```

顺序不能随意改变。去重应在 callback 前；统计应明确计算收到、接受还是实际交付；callback 与 Read 的分支必须共享同一条过滤结果。

下面是同一入口的固定提交连续源码摘录，输入是 reader layer 传来的 `TopicInfo`、借用 payload 指针及 publisher clock。保留完整 `CSubscriberImpl::ApplySample()` 控制流，省略范围为零。


```cpp
size_t CSubscriberImpl::ApplySample(const Payload::TopicInfo& topic_info_, const char* payload_, size_t size_, long long id_, long long clock_, long long time_, size_t /*hash_*/, eTLayerType layer_)
{
  // ensure thread safety
  const std::lock_guard<std::mutex> lock(m_receive_callback_mutex);
  if (!m_created) return(0);

  // We don't want to apply samples which are received on layers which are not activated for this subscriber
  if (!ShouldApplySampleBasedOnLayer(layer_))
  {
    return 0;
  }

  auto publication_info = PublicationInfoFromTopicInfo(topic_info_);

  // We do not want to apply duplicate / old samples
  if (!ShouldApplySampleBasedOnClock(publication_info, clock_))
  {
    // not clear why we are returning the size_ if we are not applying the sample, but why not...
    return size_;
  }

  // We might not want to apply samples sent with a given ID (deprecated!)
  if (!ShouldApplySampleBasedOnId(id_))
  {
    return 0;
  }

  // store receive layer
  m_layers.udp.active |= layer_ == tl_ecal_udp;
  m_layers.shm.active |= layer_ == tl_ecal_shm;
  m_layers.tcp.active |= layer_ == tl_ecal_tcp;

#ifndef NDEBUG
  // log it
  eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::ApplySample");
#endif

  // increase read clock
  m_clock++;

  TriggerMessageDropUdate(publication_info, clock_);
  TriggerStatisticsUpdate(time_);

  // reset timeout
  m_receive_time = 0;

  // store size
  m_topic_size = size_;

  // execute callback
  bool processed = false;
  {
    // call user receive callback function
    if(m_receive_callback)
    {
#ifndef NDEBUG
      // log it
      eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::ApplySample::ReceiveCallback");
#endif
      // prepare data struct
      SReceiveCallbackData cb_data;
      cb_data.buffer   = static_cast<const void*>(payload_);
      cb_data.buffer_size  = size_;
      cb_data.send_timestamp  = time_;
      cb_data.send_clock = clock_;

      STopicId topic_id;
      topic_id.topic_name          = topic_info_.topic_name;
      topic_id.topic_id.host_name  = topic_info_.host_name;
      topic_id.topic_id.entity_id  = topic_info_.topic_id;
      topic_id.topic_id.process_id = topic_info_.process_id;

      SPublicationInfo pub_info;
      pub_info.entity_id  = topic_info_.topic_id;
      pub_info.host_name  = topic_info_.host_name;
      pub_info.process_id = topic_info_.process_id;

      // execute it
      const std::lock_guard<std::mutex> exec_lock(m_connection_map_mtx);
      (m_receive_callback)(topic_id, m_connection_map[pub_info].data_type_info, cb_data);
      processed = true;
    }
  }

  // if not consumed by user receive call
  if (!processed)
  {
    // push sample into read buffer
    const std::lock_guard<std::mutex> read_buffer_lock(m_read_buf_mutex);
    m_read_buf.clear();
    m_read_buf.assign(payload_, payload_ + size_);
    m_read_time = time_;
    m_read_buf_received = true;

    // inform receive
    m_read_buf_cv.notify_one();
#ifndef NDEBUG
    // log it
    eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::ApplySample::Receive::Buffered");
#endif
  }

  return(size_);
}
```

payload 指针从 reader layer 传入，没有在函数入口变成 owning buffer。持有对象的两层引用是上段 Gate vector 中的 `shared_ptr` 和当前调用栈；它们保护的是 Impl 生命周期，不会把 payload 变成自有内存。若 source 是 SHM zero-copy，这个函数返回之前 observer 仍持有共享 memfile 读锁；若 source 是 buffered SHM，`payload_` 指向 observer 的本地 `receive_buffer`，仍只在同步调用期间有效。

锁域从代码可逐项画出：`m_receive_callback_mutex` 在函数开头取得，直到函数返回才由 RAII `lock_guard` 释放；callback 分支又在 `m_connection_map_mtx` 下同步调用业务 callback。Registration 更新同样要改这个 Subscriber 的 Publisher connection map，因此一个 40 ms callback 会让同一 Subscriber 的后续 UDP/TCP/SHM 样本等待，并让该 Subscriber 的连接状态更新等待。

同一把 `m_receive_callback_mutex` 还有一个可直接复现的重入死锁：业务 callback 里发现机器人进入急停，就调用 `sub.RemoveReceiveCallback()` 试图立刻停用自己。`ApplySample()` 尚未返回，仍持有这把普通、非递归的 `std::mutex`；`RemoveReceiveCallback()` 又尝试锁它，于是当前线程等待自己释放锁，callback 无法返回，后续该 Subscriber 的 UDP/TCP/SHM 数据和关闭清理都可能停住。源码可对照 `CSubscriber::RemoveReceiveCallback()` 与 `CSubscriberImpl::RemoveReceiveCallback()`：公开句柄先 `weak_ptr::lock()`，实现函数随后正好锁 `m_receive_callback_mutex`。安全做法是 callback 只记录/排入“停用订阅”的控制请求，让另一条控制线程在 callback 返回后调用移除接口。不能换成递归锁来掩盖问题，因为那只允许同线程再次进入，不会解决 callback 与 transport teardown 的职责交叠。

## 同一 Subscriber 的 callback 串行

`receive_callback_mutex` 覆盖 ApplySample 的主要处理过程。即使 UDP、TCP 和 SHM 在不同线程同时到达，同一 SubscriberImpl 也一次只处理一条样本。

这给业务 callback 一个简单保证：默认不会被同一 Subscriber 并发重入。代价是 callback 执行时间直接阻塞该订阅的所有输入路径。

```text
UDP thread enters callback for 30 ms
TCP thread arrives same subscriber
  -> waits receive mutex up to about 30 ms
SHM observer also arrives
  -> waits behind TCP/UDP
```

若 callback 需要昂贵推理，应快速复制/移动必要数据到工作队列，再返回中间件线程。

## 多层去重使用 Publisher identity 与 clock

发送端为每条逻辑样本递增 clock。接收端按 Publisher identity 保存已见 clock。

```text
same publisher, clock 42 via SHM -> accept
same publisher, clock 42 via UDP -> duplicate, drop
same publisher, clock 43 via TCP -> accept
```

Identity 不能只有 topic，因为同一 topic 可以有多个 Publisher。通常需要 entity、process 与 host 共同区分来源。

Clock 跳跃还能估计丢样本数量，但在 Publisher 重启、clock 回绕或注册信息不完整时要重置对应状态，不能把新进程的低 clock 当成永久旧数据。

## Layer enabled 与 active 不同

Enabled 表示配置允许读取某层；active 表示运行期确实从该层收到过有效样本。

Registration 周期会把 active 状态发布给监控工具。固定提交的 `ApplySample()` 在 `m_receive_callback_mutex` 下写 `m_layers.*.active`，但 `GetRegistrationSample()` 读取这些字段时没有取得这把锁；不同数据线程调用会被 receive mutex 串行，registration/provider 线程却不因此与它同步。这些字段是普通 bool，按 C++ 内存模型这是数据竞争候选。可观测影响不一定是 payload 坏掉，而可能是注册样本报告的 active 标志滞后/不一致；应分别核对 `ApplySample()` 与 `GetRegistrationSample()`。这属于由读写同步关系推出的风险，不能把它误写成上游声明的行为保证。

这类状态不在核心 payload 中，却常成为数据竞争来源，因为开发者容易把“只是监控字段”误认为无需同步。

## ID filter 位于业务交付之前

Subscriber 可配置接受的 sample id 集合。Filter 在去重之后、callback 之前执行。

若 filter set 能在运行时修改，setter 与 ApplySample 读取必须共享锁或采用不可变快照：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
std::shared_ptr<const IdSet> filter;
auto snapshot = std::atomic_load(&filter);
std::atomic_store(&filter, std::make_shared<const IdSet>(updated_ids));
```

更新线程创建新 set 后原子替换，接收线程读取一份稳定 shared pointer。这样热路径无需长时间持配置锁。

## callback 获得借用 payload

Callback 参数中的 buffer 通常指向：

- UDP/TCP 解包缓冲；
- SHM 映射区域；
- reader layer 的临时连续内存。

它只在 callback 调用期间有效。要异步处理：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
void OnImage(const ReceiveCallbackData& sample) {
  auto owned = std::make_shared<std::vector<std::byte>>(
      sample.buffer,
      sample.buffer + sample.size);
  worker_queue.Push(std::move(owned));
}
```

若消息类型支持直接 decode 到自有对象，也可在 callback 中完成 decode 后转交对象。对于超大图像，这次复制可能昂贵，需要对象池或 SHM-aware loan 设计。

## callback 与 Read 是互斥消费模式

注册 receive callback 后，ApplySample 不再填同步 read slot。没有 callback 时，它把 payload 复制到一个 `std::string`/byte slot，并唤醒 condition variable。

```text
callback installed -> invoke callback, no read-slot update
no callback         -> overwrite single read slot
```

这不是两个观察者各拿一份数据。切换模式时要理解当前未读 slot 与 callback 安装时序。

## Read 是单槽 latest-value mailbox

同步 `Read(timeout)` 等待 `read_slot_ready`：

- timeout < 0：一直等；
- timeout = 0：立即检查；
- timeout > 0：限时等待。

新样本到达时执行 `read_slot.assign(...)`，会覆盖尚未取走的旧样本。成功 Read 用 swap/移动把内容交给调用者，再清 ready flag。

```text
sample A -> slot=A
sample B -> slot=B  (A overwritten)
Read     -> returns B
```

这种语义适合只关心最新状态的控制与监控，不适合必须处理每一事件的命令流。

## condition variable 的正确谓词

条件变量不是把消息存起来的队列。`m_read_buf_received` 才是受 `m_read_buf_mutex` 保护的谓词；`notify_one()` 只提示等待线程重新检查它。notify 发生时 ApplySample 仍持有 buffer mutex，等待线程即使因此从 blocked 变为 runnable，也必须等发送线程退出临界区并释放 mutex，之后被 OS 调度到 CPU 才能继续。`wait(lock,predicate)` 会在睡眠前释放 mutex，返回前重新取得它，并在虚假唤醒后重复检查谓词。

固定提交的 `CSubscriberImpl::Read()` 实际使用 `m_read_buf_received` 作为唯一等待谓词。下面是源码摘录：


```cpp
bool CSubscriberImpl::Read(std::string& buf_, long long* time_ /* = nullptr */, int rcv_timeout_ms_ /* = 0 */)
{
  if (!m_created) return(false);

  std::unique_lock<std::mutex> read_buffer_lock(m_read_buf_mutex);

  // No need to wait (for whatever time) if something has been received
  if (!m_read_buf_received)
  {
    if (rcv_timeout_ms_ < 0)
    {
      m_read_buf_cv.wait(read_buffer_lock, [this]() { return this->m_read_buf_received; });
    }
    else if (rcv_timeout_ms_ > 0)
    {
      m_read_buf_cv.wait_for(read_buffer_lock, std::chrono::milliseconds(rcv_timeout_ms_), [this]() { return this->m_read_buf_received; });
    }
  }

  // did we receive new samples ?
  if (m_read_buf_received)
  {
#ifndef NDEBUG
    // log it
    eCAL::Logging::Log(Logging::log_level_debug3, m_attributes.topic_name + "::CSubscriberImpl::Read");
#endif
    // copy content to target string
    buf_.clear();
    buf_.swap(m_read_buf);
    m_read_buf_received = false;

    // apply time
    if (time_ != nullptr) *time_ = m_read_time;

    // return success
    return(true);
  }

  return(false);
}
```

这是固定提交中的 `CSubscriberImpl::Read()`。特别注意负 timeout 分支没有 `shutting_down` 谓词。`CSubscriberImpl` 析构也没有广播一个 shutdown 标志；如果线程已经进入 `Read(-1)`，该调用持有 weak-lock 得来的局部 `shared_ptr`，销毁 facade 或从 Gate 注销并不能让 Impl 析构来唤醒它。Finalize 清 Gate 后若没有后续 sample，这个 reader 线程可以永久 blocked，强引用也就一直不释放。可复刻系统应让谓词包含 `ready || closing`，关闭时设置 closing 并 notify_all，醒来后返回取消状态。

下面是推荐的关闭谓词示例，不是 eCAL 源码：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
std::unique_lock lock(read_mutex);
bool ready = read_cv.wait_for(lock, timeout, [&] {
  return read_slot_ready || shutting_down;
});
```

POSIX 的 [`pthread_cond_wait(3)`](https://man7.org/linux/man-pages/man3/pthread_cond_wait.3.html) 规定了等待时释放 mutex、返回前重新取得 mutex 的语义；Linux 的 pthread 运行库通常以 futex 作为竞争时进入内核等待/唤醒的底层机制，但 C++ 标准库调用本身不能保证每次等待都会执行某个特定 futex 系统调用。futex 允许无竞争路径主要在用户态完成，真正需要睡眠时才请求内核把线程置为 blocked；线程被通知后也只是有机会转为 runnable，是否马上得到 CPU 仍由调度器决定。对该项目，实际失败可按“线程进入内核等待—Gate 删除索引—没有 shutdown notify—线程仍 blocked—shared_ptr 不释放—析构不发生”逐步复现。

## Connection map 为 callback 提供类型信息

Publisher registration 带来 datatype descriptor 和 layer parameters。Subscriber 按 publisher identity 保存 connection entry。

收到 payload 时，ApplySample 可以从 connection map 找到对应 data type info，并一起传给 callback。

如果 callback 在持有 connection map mutex 时执行，用户在 callback 内调用会再次获取同一锁的生命周期 API，可能造成自锁或长时间阻塞 registration 更新。

更稳健的模式是锁内复制必要 metadata 与 callback 对象，锁外执行用户函数。

## SHM callback 的额外锁域

Zero-copy SHM 路径可能让 Subscriber callback 直接查看 memory file。为了防止 Publisher 覆盖这块区域，SHM observer 在 callback 完成前保持 named mutex。

```text
observer locks shared buffer
  -> SubGate
  -> SubscriberImpl
  -> user callback
observer unlocks shared buffer
```

此时慢 callback 不只阻塞本 Subscriber，还可能阻塞 Publisher 下一次复用同一 buffer。多 buffer rotation 可以缓解，但不能消除无限慢 callback。

## 返回值不能简单等同于交付

ApplySample 的不同分支可能返回 payload size 或 0：重复样本可能报告已处理 size，layer/filter 拒绝可能返回 0。

因此上层若要统计“业务 callback 次数”，应在真正调用 callback 处计数，而不是用 ApplySample 返回字节数推断。

明确区分：

```text
received by transport
parsed by reader layer
accepted by filter/dedup
delivered to callback/read slot
successfully decoded by application
```

## Subscriber 内部的 STL：每个容器对应一种查询

读 `CSubscriberImpl` 的成员表时，不要只看到“很多状态”。把容器换成问题句就清楚了：

| 成员 | 固定类型 | 它回答的问题 |
|---|---|---|
| `m_connection_map` | `std::map<PublicationInfo, SConnection>` | 这个 Publisher endpoint 的 datatype 与 layer 状态是什么？ |
| `m_id_set` | `std::set<long long>` | 当前 sample id 是否允许？ |
| `m_read_buf` | `std::string` | callback 模式关闭时，最新 payload 是什么？ |
| `m_publisher_message_counter_map` | 内部 `std::map` | 这个 Publisher 的 clock 是否已见过/是否单调？ |
| `m_message_drop_map` | 内部 `std::map` | 每个 Publisher 的 sequence gap 统计是什么？ |
| `m_sample_hash_queue` | `std::deque<size_t>` | 固定头文件里存在，但本提交实现中没有实际读写引用 |

最后一项是很好的源码阅读提醒：看到成员名不等于它参与当前版本算法。固定提交全树搜索 `m_sample_hash_queue` 只有声明，不能因为名字像“去重队列”就替作者补出不存在的行为。

### 为什么 receive callback mutex 和 connection map mutex 分开？

`ApplySample()` 入口先获取 `m_receive_callback_mutex`，保护 callback 函数对象并串行同一 Subscriber 的交付；真正调用用户 callback 前，又取得 `m_connection_map_mtx`，因为需要从 connection map 取得 Publisher datatype。

固定锁顺序是：

~~~text
m_receive_callback_mutex
   -> m_connection_map_mtx
      -> user callback
~~~

这有直接的 reentrancy 含义：`RemoveReceiveCallback()` 同样要获取非递归的 `m_receive_callback_mutex`。如果用户在自己的 receive callback 内对同一 Subscriber 直接调用 Remove，当前线程会再次请求自己已经持有的 mutex，存在自死锁路径。

通用设计里更稳健的形状通常是：

~~~text
lock
  -> copy callback + datatype snapshot
unlock
  -> invoke arbitrary user code
~~~

如果还要承诺“注销返回后绝无 in-flight callback”，再加 in-flight counter、generation 或 quiescence barrier。锁外调用与注销静默期是两件不同的事。

### 为什么 atomic 不能替代 connection map mutex？

`m_connection_count` 用 atomic 是因为它只是独立计数摘要。`SConnection` 却包含 datatype、layer states 与 state，这些字段必须组成一致快照。

把每个字段都改成 atomic 可能产生：

~~~text
datatype = new
layer = old
state = established
~~~

这种逻辑混合状态。mutex 的价值是保护复合不变量，而不仅是保证一个整数不会“写一半”。

### condition_variable 为什么和单槽 string 配对？

Read 模式等待的是“最新值是否发生变化”，不是第 N 个历史样本。因此 `string + bool + condition_variable` 足够表达 latest-only。

condition variable 的 notify 只表示状态可能改变；线程醒来仍要用谓词重新检查 `m_read_buf_received`，因为允许 spurious wakeup，也可能出现通知与真正进入 wait 的竞态。


## 热路径复杂度

若同 topic 有 K 个本地 Subscriber：

```text
SubGate lookup       average O(1) / map-dependent
snapshot             O(K) shared_ptr copies
per subscriber       lock + dedup map lookup + callback/slot copy
```

Callback 模式不复制 payload 本体，但 K 个 callback 串行执行。Read 模式每个订阅者复制 S 字节到自己的 slot，总成本 `O(K*S)`。

SHM zero-copy 只消除部分 payload copy，不能消除 fan-out、锁、去重与业务执行。

## 最小复刻结构

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class Subscription {
 public:
  DeliveryResult Apply(const SampleView& sample, Layer source);

 private:
  std::mutex receive_mutex_;
  std::unordered_map<PublisherId, Clock> last_seen_;
  Callback callback_;
  std::vector<std::byte> read_slot_;
  bool read_ready_ = false;
  std::condition_variable read_cv_;
};

class SubscriptionGate {
  std::shared_mutex index_mutex_;
  std::unordered_multimap<std::string,
      std::shared_ptr<Subscription>> by_topic_;
};
```

先实现单 layer 和 callback，再加入 read slot；随后加入多 layer identity 去重，最后才接 SHM borrowed view。每一步都明确 buffer 有效期与 callback 线程。

## 接收链的设计结论

eCAL Subscriber 用 SubGate 汇聚三种传输，用 shared pointer 快照隔离全局索引锁，用对象级 receive mutex 串行同一订阅，再通过 publisher clock 去重。Callback 模式强调低复制，Read 模式提供单槽 latest-value 语义。

它的优势是多层输入对业务统一；代价是慢 callback、SHM 锁域与运行期 setter 同步必须谨慎。理解这些边界后，才能判断所谓“共享内存零拷贝”在真实业务 WCET 下是否仍具备预期性能。
