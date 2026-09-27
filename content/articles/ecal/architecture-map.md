# eCAL 功能与组件地图：发现驱动的多传输发布订阅

eCAL 的核心功能是让动态启动的进程先发现彼此，再为每对 Publisher/Subscriber 选择合适传输，并把多条物理路径汇入统一回调。源码可以按五个功能域阅读。

本篇概览与后续分析使用同一固定源码 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

## 先从一次相机帧推导 eCAL 的骨架

假设相机进程每 33 ms 发布一帧图像：同机感知进程希望走共享内存，局域网记录器希望走 UDP，远端诊断工具则需要 TCP。朴素方案是在每次 Send 中遍历订阅者、检查能力并重新选择传输；订阅者增多后，topic 查找和连接协商就进入每帧路径。若把多线程分发也放在同一张索引锁下，注册更新还会被长临界区挡住。更糟的方案是把同进程示例里的“直接调用回调”照搬到跨进程传输：远端 callback 并不在 Publisher 的地址空间里，socket send 成功也不能证明对方业务已经运行。

因此需要把问题拆成两个时间尺度。控制面周期维护“谁存在、支持什么、连接是否仍有效”的软状态；数据面在 Send 时读取已建立连接数及各 layer 的计数摘要，再调用已准备好的 writer。固定提交中的 CGlobals、Registration Provider/Receiver、PubGate/SubGate 和 PublisherImpl/SubscriberImpl 对应这些职责：registration 接收路径更新连接 map 和计数，业务线程据计数进入 SHM、UDP 或 TCP writer，接收线程再把数据交给 SubGate 管理的对象。eCAL 这里没有一个不可变连接计划快照；发送线程与注册线程看到的是由锁和原子字段维护的分阶段状态。

PubGate/SubGate 不是网络传输层，而是按 topic 找到并持有本地实现对象的索引；PublisherImpl/SubscriberImpl 不是业务回调本身，而是把连接状态、reader/writer 和生命周期协议组合起来的运行时对象；Registration 只提供发现与能力信息，不等于数据已经可以读取。若把这些职责合成一个类，Finalize 就必须同时停止发现线程、关闭 I/O、清空 topic 索引并等待用户回调，任意顺序错误都可能在已析构对象上继续执行。

若没有预先维护的连接状态而在每次 Send 时重新读取注册表，发送延迟会直接暴露给发现线程的锁竞争。真实 eCAL 的 SubGate 会为 payload 分发复制 `shared_ptr` 快照并在锁外调用 SubscriberImpl，但 Publisher registration 分发路径会持 PubGate 读锁调用 PublisherImpl；两条路径不能合并成一个“Gate 总是锁外回调”的规则。eCAL 付出的代价是多一层 Impl、Gate 和 registration 状态，以及连接变化时启动/停止 writer 的协调；换来的收益是高频数据面不必逐一重新协商所有订阅端。

:::{mermaid}
flowchart LR
  APP[业务 CPublisher] -. weak pointer .-> PI[PublisherImpl]
  PG[PubGate] -- shared pointer / 索引 --> PI
  REG[Registration] --> PG
  REG --> SG[SubGate]
  SG -- shared pointer / 索引 --> SI[SubscriberImpl]
  SI -. weak pointer .-> SA[业务 CSubscriber]
  PI --> SHM[SHM writer]
  PI --> UDP[UDP writer]
  PI --> TCP[TCP writer]
  SHM --> SG
  UDP --> SG
  TCP --> SG
  SI --> SUB[Subscriber callback 或 Read slot]
:::

## 使用场景与非目标

eCAL 适合主机内或受控局域网中的高带宽机器人数据流，例如相机、点云、车辆传感器和多进程仿真。它的优势是同一 Publisher API 能按端点位置与能力选择 SHM、UDP 或 TCP，并提供监控、录制和回放生态。

它不是分布式数据库，也不替业务定义跨 topic 事务、严格全局顺序或无限离线积压。发现成功只表示实体可见，不能证明消费者及时处理；选择可靠传输也不能替应用解决过期数据和过载。

## 功能域与组件

| 功能域 | 核心组件 | 对外能力 | 实现难点 |
|---|---|---|---|
| 进程运行时 | CGlobals | Initialize/Finalize | 线程、Gate、Provider 的依赖顺序 |
| 发现控制面 | Registration Provider/Receiver、SampleApplier | 自动发现、租约过期 | 软状态、重复注册、陈旧实体 |
| 发布连接 | PublisherImpl、PubGate | Topic 发布与匹配 | 能力协商、每连接选层 |
| 传输层 | SHM/UDP/TCP Writer/Reader | 本机与跨机通信 | 复制、队列、ACK、故障语义 |
| 接收交付 | SubGate、SubscriberImpl | Callback/同步 Read | 去重、借用 payload、慢回调 |

## 从机器人需求反推组件

| 需求 | 首先进入的组件 | 还必须检查的边界 |
|---|---|---|
| 同机发送相机和点云 | SHM writer/reader、memfile pool | buffer 数、慢读者、resize 与崩溃恢复 |
| 局域网广播低延迟状态 | UDP layer、registration | 分片、丢包、sample 去重 |
| 可靠传输远端字节流 | TCP layer、connection state | 队头阻塞、慢消费者、重连 |
| 进程动态启停后自动相遇 | Registration Provider/Receiver | 租约、identity、重复/陈旧样本 |
| 一个 topic 多个本地实体 | PubGate/SubGate | topic 索引、所有权、注销竞态 |
| 监控和录制现有数据流 | registration/monitoring、recorder | 可见性不等于业务及时处理 |
| 全局初始化多个库模块 | CGlobals、Initialize/Finalize | 计数、部分失败与逆序关闭 |

定位问题时应先判断属于哪一平面。发现表能看到实体但没有数据，通常要继续检查 capability 和 transport；Send 返回成功但算法没有新数据，要检查 reader layer、SubGate 去重、callback/队列与数据年龄；Finalize 卡住，则从 registration/transport worker 和在途 callback 反向追踪。

## 固定版本中的调用主线

本系列固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。阅读顺序不按源码文件排序，而按机器人数据实际经过的对象排列：先看 `CGlobals::Initialize/Finalize` 如何建立与关闭进程运行时；再从 `CPublisher::Send` 进入 `CPublisherImpl::Write`，沿选中的 SHM、UDP 或 TCP writer 前进；随后从 Registration 的发布样本追到 `CPublisherImpl::ApplySubscriberRegistration`，看连接状态如何建立；最后从各 reader 回到 `CSubGate::ApplySample` 和 `CSubscriberImpl::ApplySample`，还原去重与业务交付。

这条顺序让读者始终沿着“谁建立对象、谁持有对象、样本现在在哪里、下一跳由哪个线程执行”前进。下文各篇会直接展示这些符号的连续源码摘录；提交固定后，代码片段本身就是核对依据。

## 自顶向下数据链

```text
Initialize -> Registration 周期广播
Publisher/Subscriber registration 相遇
  -> capability intersection
  -> connection 选择 SHM / UDP / TCP
Send -> selected writers
  -> reader layers
  -> SubGate 去重与路由
  -> Subscriber callback 或 latest-value Read
```

这条链可拆成两个时间尺度：Registration 周期性维护“谁存在、支持什么”；Send 高频使用已经建立的 writer 集合。控制面允许最终一致，数据面追求短路径。若每次 Send 都重新发现和协商，发布延迟会被网络控制面支配。

### 发现时间线

```text
local Publisher Create
  -> PubGate 持有 Impl
  -> Registration Provider 周期发布 descriptor
  -> remote Registration Receiver 收到
  -> SampleApplier 过滤/归一化
  -> remote Sub/Pub Gate 应用匹配状态
  -> 双方根据位置与 capability 建立/启用 layer
```

descriptor 到达不是同步握手完成。远端可能先看到 registration，稍后才打开 reader/writer；本地也可能在首次 Send 时仍没有目标。业务若要求启动屏障，需要显式等待匹配条件或在应用协议中增加 ready，不能把“休眠一秒”当正确性机制。

### 单次发送时间线

```text
CPublisher::Send
  -> weak facade lock PublisherImpl
  -> 读已建立 subscriber count；为 0 时只刷新统计并返回 false
  -> CPublisherImpl::Write 读取各 layer atomic counters
  -> 视 SHM/UDP/TCP 组合决定 payload staging
  -> SHM / UDP / TCP writer 写入
  -> 任一启用 writer 成功则 bool 为 true
```

固定提交中的 `CPublisher::Send()` 先检查公共已建立连接数，再进入 `CPublisherImpl::Write()`；后者读取 atomic layer counters，但随后读写普通成员（如 `m_payload_buffer`、`m_clock` 和 SHM writer index）。因此“计数读取是原子的”不能推出“同一个 Publisher 的整个 Send 并发安全”。应用若由多个线程同时 Send，应串行调用或分配独立 Publisher，并自行定义消息顺序。

这条发送入口可以直接从固定源码验证。调用者此时仍在自己的线程中，`buf_` 与 `len_` 是本次发送的借用视图；先构造的 `CBufferPayloadWriter` 只包装地址和长度，不会替调用者延长原缓冲区寿命：

**固定提交源码摘录（`eclipse-ecal/ecal@1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`，`CPublisher::Send` 两个连续重载）：**

```cpp
bool CPublisher::Send(const void* const buf_, const size_t len_, const long long time_ /* = DEFAULT_TIME_ARGUMENT */)
{
  CBufferPayloadWriter payload{ buf_, len_ };
  return Send(payload, time_);
}

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

第一层只把裸地址和长度交给 payload writer；第二层先通过 `weak_ptr::lock()` 临时取得强引用，因此 Gate 并发注销时，当前调用仍能在 `publisher_impl` 局部变量释放前完成对象访问。这个强引用只解决对象内存寿命，不会串行化同一个 Impl 上的多个 `Send()`，也不会让异步 transport 继续借用调用者的 `buf_`。无已建立订阅端时，代码只刷新统计并返回 `false`；有订阅者时才生成时间戳并进入实现对象。真正选择 writer、复制或复用 payload 的位置是下一层 `CPublisherImpl::Write()`，不能把这里的成功返回解释成对端业务 callback 已运行。

## 核心对象与所有权

```text
CGlobals
  |-- Registration provider / receiver threads
  |-- PubGate --shared--> PublisherImpl <--weak-- CPublisher facade
  |-- SubGate --shared--> SubscriberImpl <--weak-- CSubscriber facade
  `-- transport layer factories / observers

PublisherImpl
  |-- connection state per subscriber/capability
  `-- SHM / UDP / TCP writers

SubscriberImpl
  |-- reader-layer inputs
  `-- callback / receive buffer / duplicate state
```

Gate 既是按 topic 查找实体的索引，也是实现对象的所有权根。公开 facade 持弱引用，使全局 Finalize 清空 Gate 后，遗留句柄安全失效而不是指向已释放内存。`CSubGate::ApplySample()` 在分发前取得局部强引用快照，释放索引锁后再进入 SubscriberImpl；CPubGate registration 路径的锁边界不同，具体见对应章节。

### 所有权与借用表

| 对象 | 所有权根 | 临时使用者 | 关闭约束 |
|---|---|---|---|
| PublisherImpl | PubGate；Send 的 facade lock 可暂时共享持有 | 发送调用线程、registration 分发线程 | facade 的 weak pointer 不串行化多个 Send |
| SubscriberImpl | SubGate；分发快照可暂时共享持有 | reader layer、callback/read 调用 | SHM observer join 提供其线程的关闭屏障；Gate 清空不能证明所有在途调用退出 |
| registration provider/receiver | CGlobals | 周期线程、SampleApplier | 先停新控制事件，再拆 Gate |
| SHM/TCP/UDP writer | PublisherImpl/transport runtime | Send 调用线程 | 同一 Publisher 并发 Send 对普通成员与 writer 的访问需要应用串行化 |
| payload view | transport/sample owner | Subscriber callback | callback 返回后失效，异步必须复制/转移 |
| monitoring descriptor | registration/monitoring state | tools/observer | 不应反向保活业务实体 |

`shared_ptr` 只能延长内存寿命，不能定义操作是否仍被允许。Impl 即使被一次 Send 的局部强引用保活，也可能处于 Stopping；方法必须同时检查状态。相反，单独的 atomic running 也不能保护已释放对象，facade 仍需安全取得寿命租约。

发送后的接收汇聚也不是“收到 payload 就直接调用业务函数”。不同 transport reader 都把 topic 元数据和一段仍由 reader/observer 管理的借用 payload 交给 SubGate。SubGate 先在 topic 索引中找出本进程的 SubscriberImpl，再把对象寿命与索引锁分开：

**固定提交源码摘录（`eclipse-ecal/ecal@1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`，`CSubGate::ApplySample`）：**

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

`shared_lock` 允许多个接收方同时读 topic 索引；每复制一个 `shared_ptr`，当前分发就暂时拥有一个强引用。离开花括号时索引锁释放，但 vector 仍让这些 Impl 活到循环结束，因此慢 callback 不占住全局 topic 锁。这里的 vector 只复制对象句柄，没有复制 `buf_` 的 payload 字节；有效期仍由上游 reader 的调用协议决定，异步保存必须另行复制或取得显式 lease。源码还有一个值得保留的细节：循环把 `applied_size` 覆写为每个 reader 的返回值，最后的布尔结果反映最后一个匹配 reader，而不是对所有 reader 做逻辑或；样本仍会逐个尝试交付。调用者不能把这个 `bool` 当作成功 reader 数或全体交付报告。

## 控制面：软状态如何收敛

每个实体周期发布 registration；接收端按实体 identity 更新缓存和最后可见时间；超时未刷新则移除。重复 registration 是正常情况，更新操作必须幂等。网络短暂丢包只延迟收敛，不应立即删除健康实体。

一条 registration 需要携带 topic、类型、进程/主机 identity、传输能力和打开数据面所需参数。identity 若只使用 topic，会把多个 Publisher 合并；若进程重启复用旧 identity，又可能让陈旧连接与新实体混淆。

能力协商是集合交集：本机且双方支持 SHM 时可选择共享内存；远端可选 UDP/TCP；配置还能禁用某层。若多层同时启用，需要 sample identity 去重，否则同一发布可能从两条路径交付两次。

软状态表至少应区分实体身份、进程实例和最后可见时间。进程重启若只复用 topic/host，会让旧 transport 状态污染新实体；identity 中需要 boot/session 维度，或在新 registration 到达时明确替换旧 epoch。

过期阈值过短会把丢包抖动放大成连接 churn，过长则让故障实体停留更久。registration 周期 `T` 与失效阈值 `E` 应结合网络丢包率和恢复目标选择，并观察连续未见次数，而不是只用一次定时器事件删除。

## 数据面：三类传输的语义差异

| Layer | 合适负载 | 主要成本 | 失败/过载表现 |
|---|---|---|---|
| SHM | 同机大图像、点云 | 映射、跨进程同步、可选复制 | buffer 被慢读者占用、对象重建 |
| UDP | 局域网低延迟状态流 | 分片、内核缓冲、丢包 | 不保证到达；大消息丢一片即失整条 |
| TCP | 需要可靠字节流的远端连接 | 帧复制、内核 socket 缓冲、重传与队头阻塞 | 每连接一个异步写和一个可覆盖待发帧，慢消费者可观察到跳帧 |

“自动选最快层”不是完整策略。相机流可能接受丢旧，命令流可能要求可靠与有界确认；layer 选择必须结合 payload、频率、可靠性与消费者速度。

### 线程与数据移动矩阵

| 阶段 | 常见线程 | 数据/状态 | 主要风险 |
|---|---|---|---|
| Initialize/Finalize | 应用管理线程 | 全局模块图 | 并发初始化、部分失败、重复关闭 |
| registration send/receive | 周期/接收 worker | descriptor 与实体缓存 | 重复、陈旧、锁竞争 |
| Publisher Send | 应用线程 | 借用 payload、各 layer 的 atomic counter 与 writer 状态 | 编码/复制、I/O、部分成功 |
| SHM observer | transport worker | memfile header/payload view | resize、崩溃 owner、慢借用 |
| UDP/TCP reader | transport worker | frame/sample | 分片、队列、重连 |
| SubGate dispatch | reader/分发线程 | Impl 强引用快照 | 去重、锁外 callback |
| Subscriber callback | 分发或配置线程模型 | 借用样本 | 慢用户代码、自注销 |

应用需要知道 callback 在哪个线程发生，才能决定是否允许阻塞、是否与同步 Read 并发以及如何关闭。把 callback 一律再投递线程池会增加队列和数据年龄；直接执行又会让用户 WCET 反压接收路径，必须结合消息类别选择。

### SHM 不是一个抽象名词

共享内存路径至少包含：命名/打开 memfile、校验内部 header、跨进程同步、选择可写 buffer、发布 sample identity、reader 借用或复制、槽位回收。任何一步失败都要处理进程异常退出和版本/尺寸不匹配。

若 publisher 拥有 `B` 个最大容量 `Smax` 的槽，单实体 payload 内存主项约为 `B×Smax`。zero-copy reader 持有槽位期间，publisher 可用槽减少；慢 callback 会从消费端跨进程反馈为发送端等待或覆盖压力。减少 memcpy 不等于降低最坏延迟。

## 一次发送要检查的四个边界

1. 调用者 buffer 是否在 Send 返回前保持有效，writer 是否立即复制；
2. registration 线程是否正在更新 connection map、连接计数或 writer；
3. 多 layer 写入的成功含义是任一成功、全部成功还是逐端点统计；
4. Subscriber 汇聚后如何根据 publisher id/clock 去重和判断过期。

这四点分别对应 C++ 借用、并发状态更新、错误聚合和协议 identity。只分析 `memcpy` 次数无法说明发送语义。

## 章节对应关系

《阅读基础》建立控制面、数据面和 Gate 概念；《Publisher 发送链》解释连接选择和多层写入；《Registration 控制面》解释发现；《SHM 数据路径》深入本机大数据；《Subscriber 接收链》解释汇聚与去重；《全局生命周期》收束线程和资源。

可以把这些章节变成七个连续源码任务：

1. 从 CPublisher 构造进入 PubGate，确认 facade 与 Impl 的强弱引用；
2. 从 `Send` 进入 `CPublisherImpl::Write`，记录每个 layer 的选择与复制；
3. 从 registration provider 的周期 tick 进入 SampleApplier，画出 identity/timeout；
4. 从 Subscriber descriptor 回到 PublisherImpl，确认 capability 交集怎样更新连接；
5. 从一个 SHM sample 进入 observer、SubGate 和 SubscriberImpl callback；
6. 验证多 layer 同时到达时 sample identity 如何去重；
7. 从 Finalize 逆序追踪控制线程、transport、Gate、Impl 与 callback。

读完后应能解释“发现了却没数据”“同一消息为何重复”“Finalize 为什么等待”分别落在哪一段，而不是只给配置建议。

## 关键取舍

软状态发现避免依赖中心注册服务，但必须周期发送并处理超时；每连接选层能适应本机/远端差异，却增加状态机和重复数据去重；SHM 降低 payload 复制，但 zero-copy 会把 subscriber 持锁时间传回 publisher。

另一个重要取舍是全局运行时。集中 CGlobals 能统一线程和 provider 生命周期、减少每实体资源，却引入初始化计数、部分失败回滚和 Finalize 顺序。库调用方不能把全局关闭视为销毁一个普通 Publisher。

### 模式与工程代价

- Facade/PImpl：公开对象保持小而稳定，Impl 可变；代价是间接访问和失效句柄语义；
- Registry/Gate：统一 topic 索引与所有权；代价是共享锁域和注销竞态；
- Strategy：SHM/UDP/TCP 替换传输；代价是 capability、错误聚合和去重；
- Soft State：周期 registration 无中心依赖；代价是收敛延迟与超时取舍；
- Layer counters：Send 用 O(1) atomic 计数判断是否尝试某层；代价是计数不是跨字段一致快照，也不单独保护 writer 指针的发布与销毁；
- Observer：transport 与 monitoring 分发事件；代价是 callback 生命周期和关闭屏障。

## C++ 设计能力

重点不是 facade 类名，而是：PImpl 隔离 ABI、Gate 用索引管理动态实体、锁内复制快照后锁外调用 callback、condition variable 用谓词处理虚假唤醒、RAII 按逆依赖关闭全局对象。

阅读每个类时可用同一组问题：

- `shared_ptr` 的所有权根在哪里，是否可能形成环；
- `span`、裸指针和 callback payload 的有效期到哪里；
- mutex 保护哪个跨字段不变量，是否在锁内做 I/O 或回调；
- atomic 只是状态提示，还是被错误当成多字段事务；
- 构造中途失败由哪个 owner 逆序撤销 registration、线程和 OS handle；
- Finalize 返回后是否仍可能有 observer 或 callback 在途。

## 性能与可行性判断

关键自变量包括 Publisher/Subscriber 数、每 topic 匹配数、payload 大小、发送频率、SHM buffer 数、UDP 分片数、TCP 队列容量与 callback WCET。内存预算至少包含实体表、每连接状态、SHM `publisher × buffers × capacity` 和 Subscriber 本地复制缓冲。

可行性不能只用平均吞吐判断。应观察 Send p50/p99、SHM mutex 等待、drop/overwrite、registration 收敛时间、数据年龄、callback 队列高水位和关闭耗时。zero-copy 只有在持锁 callback 的最坏时间可控时才可能改善尾延迟。

定量预算可分四部分：控制面成本近似实体数与 registration 频率之积；数据面 CPU 近似 `publish_rate × (serialization + Σlayer_cost)`；SHM 主内存近似各 publisher 的槽数乘最大容量；接收延迟由 transport、SubGate、callback 排队和用户 WCET 共同组成。

多层发送的返回值不足以表达每端点结果时，必须用逐层统计补充。否则调用者看到 Send 成功，却无法发现 TCP 长期断开而 UDP 偶尔成功。对控制命令还应增加序号、deadline 与业务确认；可靠字节流不等于恰好执行一次。

## 实现路线

先做固定配置的单 transport pub/sub；再加入 Gate 与多 Subscriber；随后增加 registration 租约和 capability；最后加入 SHM buffer rotation、多路径去重与进程级生命周期。

阶段完成条件应明确：固定 transport 版能够有界排队和关闭；Gate 版允许 callback 自注销且不死锁；发现版能容忍重复与短暂丢包；SHM 版验证 header 边界、进程崩溃和 resize；多层版证明同一 sample 最多交付一次；全局版证明部分初始化失败和重复 Finalize 不泄漏资源。
