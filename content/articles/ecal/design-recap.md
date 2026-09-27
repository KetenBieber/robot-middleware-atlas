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

**固定提交源码摘录（`eclipse-ecal/ecal@1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`，`CPublisher::Send`）：**

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

**固定提交源码摘录（同一提交，`CSubGate::ApplySample`）：**

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
