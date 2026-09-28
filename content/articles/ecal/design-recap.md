# eCAL 设计复盘：构建发现驱动的多传输总线

eCAL 的核心不是简单同时支持 SHM、UDP 和 TCP，而是用 registration 控制面把每个订阅连接映射到合适的数据路径，再让 Publisher 与 Subscriber 共享统一的消息身份、去重和生命周期语义。

本章复盘固定 eCAL 源码 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

## 完整对象与数据流

```text
Runtime/CGlobals
  +-- Registration Provider/Receiver
  +-- PubGate -> PublisherImpl -> SHM/UDP/TCP writers
  +-- SubGate -> SubscriberImpl
  +-- SHM/UDP/TCP reader layers
  `-- MemFile observer infrastructure

Control plane:
  entity snapshot -> registration receiver -> gates
  -> connection map -> layer selection/resources

Data plane:
  Send -> PrepareWrite -> selected writers
  -> reader layers -> SubGate -> dedup/filter
  -> callback or latest-value Read slot
```

控制面允许周期收敛，数据面避免逐条遍历全部连接。两者通过 layer counters、TCP port 和 memfile list 连接。

## 最小复刻阶段

### 单进程 Gate

先实现 topic 到 shared implementation 的 multimap。分发时锁内复制 shared pointer，锁外 callback。验证 concurrent unregister 不产生悬空访问。

### 单传输内存队列

加入 PublisherImpl、SubscriberImpl 与 sample identity。实现 callback 模式和单槽 Read，明确它们互斥。

### 周期 Registration

实现本进程实体快照、接收表和 lease timeout。所有显式/超时下线统一产生 Unregister 事件。

### Connection 状态与选层

为每个 reader 保存 Pending/Active、capability 与 selected layer。先用纯函数选择传输，再维护每层原子连接计数。

### UDP/TCP 数据路径

UDP 明确分片丢失和无确认；TCP 明确 per-session queue 与慢连接策略。两层使用相同 publisher id/clock，Subscriber 在统一入口去重。

### SHM Copy 模式

建立 versioned header、named mutex、event、单 buffer。Reader 锁内验证并复制，锁外 callback。

### 多 Buffer 与 ACK

加入 round-robin、容量 reserve、registration 更新 memfile list，再加入有界 ACK 等待与失效订阅者状态。

### Zero-copy Loan

只有测得 copy 成为瓶颈后再设计。优先使用显式 buffer lease，而不是让任意用户 callback 长时间占用 named mutex。

## 关键数据结构

| 结构 | 作用 | 主要复杂度 |
|---|---|---:|
| topic multimap | topic 到本地实现对象 | 查找平均/对数 + `O(K)` fan-out |
| connection map | 每个远端实体的状态与选层 | `O(log P)` 或平均 `O(1)` |
| layer counters | Send 热路径摘要 | `O(1)` |
| expiration map + list | identity 查找与按时间过期 | refresh `O(log N)`，过期按 K |
| read slot | 同步最新值 | 写 `O(S)`，固定一条逻辑容量 |
| SHM buffer vector | 读写并行余量 | 轮换 `O(1)`，内存 `O(B*S)` |
| observer map | memfile 到观察者 | `O(log M)`，线程可能 `O(M)` |

K 为同 topic 本地订阅数，P 为同 topic Publisher 数，N 为发现实体数，B 为 SHM buffer 数，M 为 memfile 数，S 为 payload 大小。

## 为什么这些 STL 不是随便挑一个容器

如果只看类图，很容易把 `std::map`、`unordered_multimap`、`vector`、`set`、`string` 当成实现细节。实际上它们直接表达了查询方式、ownership、热路径和并发边界。

### SubGate：`unordered_multimap<string, shared_ptr<SubscriberImpl>>`

一个 topic 可以在同一进程里有多个 Subscriber，所以普通 `unordered_map<string, Subscriber>` 不够；key 不能唯一。`unordered_multimap` 让 `equal_range(topic)` 直接得到同 topic 的全部实现对象，平均查找接近 O(1)，fan-out 再付 O(K)。

value 用 `shared_ptr` 也不是偶然。Gate 是实现对象的强 ownership root；公开 `CSubscriber` 只持 `weak_ptr`。因此“用户句柄还存在”和“运行时实体仍然注册”是两回事。

分发时固定源码再复制到：

~~~cpp
std::vector<std::shared_ptr<CSubscriberImpl>> readers_to_apply;
~~~

vector 适合一次查找后顺序 fan-out，连续存放 shared_ptr，遍历 cache locality 好；每个 shared_ptr 又临时增加强引用。这样 Gate 解锁以后，即使另一个线程 `Unregister()`，本轮已经选中的 Impl 仍活到调用结束。

如果直接拿 multimap iterator 解锁后继续遍历，另一线程 erase 会让 iterator 失效；若全程持 Gate 锁，则任意慢 callback 会阻塞整个进程注册/注销其他 Subscriber。

### Connection table：为什么用 `std::map`

Publisher 和 Subscriber 都用 `std::map<SampleIdentifier, Connection>` 保存远端 endpoint 状态。这里不是每帧按 topic 广播，而是 registration 到来时查一个具体 endpoint，并保存 datatype、layer state、selected layer 和连接状态。

`std::map` 的节点稳定性和确定 O(log N) 很适合“控制面小集合 + 插入删除 + 复合状态”。它的 cache locality 不如平坦数组，但 registration 不是大 payload 热路径。

eCAL 再把 Send 真正需要的信息压成：

~~~cpp
std::atomic<size_t> udp;
std::atomic<size_t> shm;
std::atomic<size_t> tcp;
~~~

复杂连接状态留在 map，每帧 Send 只读 O(1) 摘要。这就是“控制面富状态，数据面短路径”。

### `std::set<long long>` 表达 membership，不是队列

Subscriber 的 filter id 用 `std::set<long long>`。它要回答的是“这个 id 是否允许”，而不是“按到达顺序保存消息”。容器选择来自查询语义。

### `std::string + mutex + condition_variable` 是 latest-value mailbox

同步 `Read()` 不是 `deque<string>`。固定实现只有一个 `m_read_buf`、received flag、time、mutex 和 condition variable。新样本覆盖旧样本，所以空间是 O(S)，而不是 O(N*S)。

这适合姿态、温度、最新检测结果；不适合“每个命令必须执行一次”。容器本身就是消息语义的一部分。

### SHM Writer：`vector<shared_ptr<CSyncMemoryFile>>` 就是运行时槽位数组

固定 Writer 保存：

~~~cpp
std::vector<std::shared_ptr<CSyncMemoryFile>> m_memory_file_vec;
size_t m_write_idx;
~~~

Write 后 index 自增取模，形成 round-robin。vector 在这里合适，因为 buffer 数在配置/重建时整体创建，运行时需要 O(1) 下标访问与顺序遍历；list 会让按 index 轮换退化，deque 的首尾稳定插入也没有收益，固定 array 又失去运行时可配置数量。

### `map<process_id, set<entity_id>>` 是跨进程资源引用关系

同一订阅进程里可以有多个 Subscriber endpoint 共享一组 SHM memory files。Writer 不能因为其中一个 endpoint 注销就立刻 Disconnect 整个进程。

所以：

~~~text
process_id
   -> set<entity_id>
~~~

只有某进程对应 set 变空，才真正 Disconnect 该进程的 SHM 通知/ACK 资源。这里 map 是进程索引，set 是 endpoint membership，本质是一份层级 ownership 关系。

### `CExpirationMap = map + list`：双索引换复杂度

Soft-state 同时需要按 endpoint key 查找，又要按最后访问时间从最旧开始过期。

只用 map，找最旧项要扫全表；只用 list，按 key refresh 是 O(N)。固定实现把 value 放在 map，并保存指向 timestamp list 节点的 iterator：

~~~text
map: key -> {value, iterator}
                   |
                   v
list: oldest ... newest
~~~

refresh 先通过 map 定位，再 O(1) 移动 list 节点到尾部；expire 从 list 头连续删除。这和 LRU 的“双索引”思想相同。

### `CExpandingVector`：用常驻内存换 allocator 抖动

Registration 周期性构造 SampleList。`CExpandingVector` 保留底层 `std::vector<T> data` 的完整尺寸，另用 `internal_size` 表示逻辑元素数；clear 不把底层 slot 全释放，下一周期优先复用。

收益是减少后台周期线程重复分配；代价是峰值容量可能长期驻留，而且 full_size 与逻辑 size 不同。它优化的是 allocator 行为，不是渐进复杂度。

## 线程、进程与 OS 边界不能混成一个并发问题

| 边界 | 共享对象 | 典型工具 | 不能靠什么解决 |
|---|---|---|---|
| 同进程多线程 | C++ 对象、普通地址空间 | mutex、atomic、condition_variable、shared_ptr | mmap 不能替代 C++ data-race 同步 |
| 同机跨进程 | 命名共享内存与 kernel object | Linux `shm_open/ftruncate/mmap(MAP_SHARED)`；Windows `CreateFileMapping/MapViewOfFile` | 普通 std::mutex 不能跨独立进程保护共享页 |
| 跨主机 | socket / 网络协议 | UDP/TCP、sequence、buffer、backpressure | shared_ptr 和虚拟地址没有跨主机意义 |

Linux `mmap(MAP_SHARED)` 只让页内容对映射同一对象的进程可见，并不自动提供“writer 写完 header 后 reader 才读”的事务语义。仍然需要 eCAL 的 header、named synchronization 和提交顺序。

Windows 用另一套 kernel object API，但两个进程得到的虚拟地址仍可完全不同，因此共享区里不能保存只对当前进程有效的裸指针。

### ownership 最终要落实为关闭顺序

~~~text
Runtime / CGlobals
   owns Gate + transport infrastructure
Gate
   owns Impl via shared_ptr
public facade
   borrows Impl via weak_ptr
Impl
   owns reader/writer resources
callback
   borrows incoming bytes only during invocation
application worker
   must own copied/loaned business data explicitly
~~~

安全关闭应按依赖图逆序：停止业务生产，阻止新 callback，等待或取消 in-flight 工作，drain/cancel worker，销毁 Publisher/Subscriber facade 触发 Gate unregister，释放 transport/SHM 资源，最后 Finalize Runtime。

智能指针只解决对象何时析构，不会自动解决线程什么时候停止调用、跨进程 lease 什么时候归还、OS handle 什么时候能关闭。


## 端到端延迟组成

```text
L = discovery readiness
  + publisher scheduling
  + optional stable-buffer copy
  + writer-specific queue/lock
  + transport or SHM notification
  + reader scheduling
  + SubGate snapshot/fan-out
  + Subscriber receive mutex wait
  + previous callback time
  + decode/business work
```

Discovery readiness 通常只影响连接建立期；SHM named mutex、TCP pending queue 和 Subscriber callback WCET 会持续影响运行期尾延迟。

## 优势

- 同机/远端连接可以使用不同传输；
- Soft-state discovery 能从异常退出自动收敛；
- Gate 快照避免用户 payload callback 占全局 topic 锁；
- Layer counter 让发送热路径不随连接数线性增长；
- SHM 可在 copy、buffer 数、zero-copy 与 ACK 间权衡；
- Subscriber 将多传输汇聚到统一过滤、去重和统计语义。

## 限制

- 发现最终一致，connected 状态有 refresh 延迟；
- 多传输与动态资源使生命周期和诊断明显复杂；
- Callback 可在对象锁或 SHM named mutex 范围内执行；
- 同步 Read 是 latest-only，不保证逐条消费；
- 原生 SHM header 对 ABI 一致性有要求；
- 每 memfile observer 模型在大量实体时带来线程成本；
- ACK 与 zero-copy 都可能把慢 Subscriber 传播成 Publisher 阻塞。

## 适用边界

eCAL 特别适合单机多进程的大图像/点云流水线，同时需要跨主机监控或计算节点的系统。SHM 提供本机吞吐，网络 layer 提供部署伸缩，registration 让连接自动形成。

硬实时控制链不应让不受控 callback 直接运行在 reader/observer 临界路径。可采用短 callback + 有界实时队列，把业务执行放到固定优先级线程，并为过载选择 latest、drop 或 backpressure。

需要强安全域、跨 WAN 路由、严格 schema 协商或端到端可靠确认时，还要额外协议与基础设施。

## 可迁移的设计能力

eCAL 提供五项值得迁移的能力：

1. 用软状态发现把崩溃恢复转成租约过期；
2. 用连接级策略选择 transport，而非系统全局固定一种路径；
3. 用控制面 map 与数据面摘要分离丰富状态和热路径成本；
4. 用共同 message identity 在多层输入处去重；
5. 用依赖图和逆序关闭管理 Gate、线程、callback 与共享资源。

自行实现时，应先保证这些不变量，再优化 zero-copy。复制成本容易测量，跨进程锁和生命周期错误则往往只在高负载与关闭竞态中出现。

## 一条样本如何贯穿所有模块

把一个同机图像 topic 从发布到订阅展开，前面的对象会形成连续调用链：

```text
CPublisher::Send(image)
  -> PublisherImpl 生成 publisher_id + clock + payload identity
  -> 查询该 topic 的 layer counters
  -> SHM writer 选择可写 memfile buffer
  -> 写入本机 ABI 布局的 SMemFileHeader 与 payload
  -> observer/event 唤醒 SHM reader
  -> reader 校验 header、长度、版本并取得样本
  -> SubGate 按 topic 找到 SubscriberImpl 快照
  -> SubscriberImpl 做 publisher/clock 去重
  -> callback 或 latest-value slot 接收数据
```

这条链的起点不是一个全局总线函数，而是公开句柄先验证实体是否仍可取得、再把写入交给当前 PublisherImpl。固定提交中的真实重载如下：


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

这里的 `lock()` 只把弱句柄提升为本次调用期间的强 owner；无已建立订阅时，`Write()` 根本不会被调用。Send 返回 `true` 也只说明实现对象报告至少有一条 writer 成功，不等于远端 callback 已开始。真正进入哪一个 writer、是否先把 payload 复制到 staging buffer，在 `CPublisherImpl::Write()` 中根据各层连接计数决定；这里运行的仍是调用 `Send()` 的应用线程。

从 SHM、UDP、TCP 抵达的样本最后都要汇到本地 SubGate。以下固定源码说明它如何把“找 topic”与“执行用户逻辑”分成两个锁域：


```cpp
bool CSubGate::ApplySample(const Payload::TopicInfo& topic_info_, const char* buf_, size_t len_, long long id_, long long clock_, long long time_, size_t hash_, eTLayerType layer_)
{
  if (!m_created) return false;

  size_t applied_size(0);
  std::vector<std::shared_ptr<CSubscriberImpl>> readers_to_apply;
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

共享锁只保护 `topic_name -> SubscriberImpl` 的索引查找；把 `shared_ptr` 拷入 vector 会为这次分发增加强引用，因而释放索引锁后仍能安全调用实现对象。代码没有复制 `buf_` 指向的图像字节，payload 的有效期仍由上游 reader/observer 保证。循环会对所有同 topic 的 SubscriberImpl 调用 `ApplySample()`，但 `applied_size` 被每次赋值覆盖，最终 bool 只看最后一个匹配项；它不是成功交付数量，也不是聚合成功状态。

Registration 不逐条搬运图像。它周期发布 Publisher 的 topic、类型、进程、可用 layer 和 SHM 资源描述；Subscriber 收到后建立 connection 状态并选择层。只有当连接摘要表明 SHM/TCP/UDP 某层存在读者时，发送热路径才进入对应 writer。

这解释了两个常见现象：刚启动时实体可见和数据可达之间存在收敛窗口；某一 transport 出错也不等于 topic 从发现表消失。控制面事实、连接事实和样本新鲜度必须分别观测。

## 所有权与线程表

| 对象 | 所有者 | 主要线程 | 不能违反的不变量 |
|---|---|---|---|
| Globals/Runtime | 进程级初始化层 | 初始化与后台服务线程 | 最后一个实体销毁后才能 Finalize |
| PubGate/SubGate | Runtime | API 调用者与接收线程 | 锁内取得 shared implementation，锁外进入用户逻辑 |
| PublisherImpl | PubGate；Send 内 facade lock 临时共享持有 | 发送调用线程、registration 分发线程 | 应用应串行同一 Publisher 的 Send；atomic counter 不保护普通 writer 状态 |
| SubscriberImpl | SubGate；分发快照临时共享持有 | transport reader/callback 线程 | SHM observer join 可等本线程；Gate 清空不能证明所有其它在途调用都结束 |
| connection map | Gate/impl | registration 与发送路径 | 丰富状态更新后再原子更新热路径摘要 |
| SHM buffer | publisher 资源管理器 | writer 与多个 reader | header 提交顺序使 reader 不见半写 payload |
| TCP session pending slot | 单连接 session | publisher 与 Asio completion handler | 最多一写进行中加一个最新待发帧；新帧可覆盖 pending，handler 错误时关闭 session |

门面析构不应直接假设后台线程已经停止。安全关闭通常需要：标记 closing、从 Gate 摘除、阻止新 callback、唤醒等待者、等待在途调用退出、销毁 transport 资源，最后才释放实现对象。

## 最小发送决策器

复刻时可以先把 transport 选择写成纯函数，避免连接管理、锁与 I/O 混在一起：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct Capabilities {
    bool same_host{};
    bool shm{};
    bool tcp{};
    bool udp{};
    std::size_t payload_size{};
};

enum class Layer { Shm, Tcp, Udp, None };

Layer select_layer(const Capabilities& c) {
    if (c.same_host && c.shm) return Layer::Shm;
    if (c.tcp && c.payload_size > kUdpThreshold) return Layer::Tcp;
    if (c.udp) return Layer::Udp;
    if (c.tcp) return Layer::Tcp;
    return Layer::None;
}
```

真实策略还会包含配置、可靠性和对端能力，但纯函数有三个好处：所有组合可穷举测试；策略变化不会直接操作 socket；connection map 可以保存选择原因，诊断时不只看到最终枚举。

发送端不应每次遍历所有 Subscriber 再决定 layer。registration 更新连接表时维护 `shm_reader_count`、`tcp_reader_count` 等摘要；`Send()` 只做几个原子读取。控制面更新是 `O(P)`，但每条消息的固定判断保持 `O(1)`，这正是控制面复杂、数据面简短的设计价值。

## 手搓实现的阶段验收

1. 单进程 Gate：注销与分发并发时无悬空指针，callback 不在全局锁内；
2. 单层队列：callback 与同步 latest read 的语义互斥且可测试；
3. 软状态发现：崩溃实体在租约后过期，显式下线与超时下线进入同一路径；
4. 多层选择：每个 capability 组合都有确定结果与原因；
5. SHM copy：reader 永远看不到半写 header/payload，恶意长度被拒绝；
6. 多 buffer：慢 reader 不无限占用 buffer，ACK 等待有截止时间；
7. 关闭竞态：registration、callback、Send 与 Finalize 并发时不重入已析构对象。

这七阶段都通过后，zero-copy 才是性能优化问题；在此之前，它只会扩大借用生命周期与崩溃恢复的状态空间。
