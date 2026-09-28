# LCM 设计复盘：先理解边界，再从零实现消息总线

本篇的实现事实固定到本地 LCM 提交 `ad0c54cee0ec048ef12357c34349ec1443158864`。结论对应前面已经展开的 [Provider 工厂](provider-vtable.md)、[UDPM 发送](udpm-publish-protocol.md)、[接收和重组](receive-reassembly.md)与[订阅分发](subscription-dispatch.md)；下面只合并它们的对象、线程、容量和关闭约束，不把教学改进冒充固定提交已提供的能力。

前面的章节已经分别拆开公共句柄、provider、UDP 协议、接收线程、订阅分发、类型生成和日志。这里先把这些机制重新组合成一条完整消息链，再依次讨论它为什么这样设计、性能代价在哪里、哪些思想能够迁移，最后才给出最小复刻顺序。

这个顺序很重要。若一开始就照着类名写代码，很容易复刻出“能够发送”的演示，却遗漏 buffer 寿命、过载、取消订阅和关闭协议。先知道每层解决的矛盾，才能判断哪些机制可以暂时省略，哪些不变量从第一版就不能破坏。

## 一条消息的完整路径

```text
Publisher application thread
  -> generated EncodedSize(message)
  -> allocate/reuse byte buffer
  -> generated Encode(message, buffer)
  -> lcm_publish(channel, buffer)
  -> provider vtable::publish
  -> UDPM transmit mutex
  -> LC02 or LC03 framing
  -> sendmsg

Receiver kernel/network
  -> recvmsg on provider thread
  -> magic and bounds validation
  -> optional LC03 reassembly
  -> subscription quota admission
  -> complete-message queue
  -> notify pipe

Application event-loop thread
  -> poll/select
  -> lcm_handle
  -> dequeue one message
  -> channel cache lookup
  -> callback outside core mutex
  -> generated fingerprint check and Decode
  -> business logic
  -> all matching callbacks return
  -> receive buffer reclaimed
```

这条链只有两个主要接收侧执行上下文：provider 接收线程与应用 handle 线程。发送则发生在调用 `publish` 的线程中，没有隐藏发送 worker。回调很慢时，网络线程仍可能继续接收；代价不会消失，而是表现为排队、内存增长或订阅配额丢弃。

## 七个最小模块形成一条闭环

### ByteCodec

把类型对象转换为确定的 wire bytes，并在开头写 schema fingerprint。它只负责字段和字节，不知道 channel、socket 或 subscription。

### BusCore

保存 provider、订阅权威集合和 channel 匹配缓存。它定义公共 API，却不包含 UDP 私有字段。

### Provider

通过函数指针表统一 create、publish、handle、ready fd 与 destroy。它隔离传输差异，但每个实现仍必须公开自己的阻塞和排队语义。

### Framing

把 channel、sequence 和 payload 组织成协议报文。短消息一次发送，大消息需要显式分片、重组和边界验证。

### ReceiveQueue

把网络线程与 callback 线程分开。通知 fd 让外部事件循环知道完整消息已经可取；容量和订阅配额决定过载时保留哪些数据。

### SubscriptionIndex

把正则订阅编译并缓存到具体 channel。它负责配额和安全取消，但不长期拥有 provider payload。

### EventLog

顺序保存 timestamp、channel 与原始 wire payload。它不需要理解具体类型，并能作为回放 provider 接回 BusCore。

## 优秀设计来自明确的变化边界

LCM 最值得学习的不是某一个技巧，而是每层只吸收一种变化。

- schema 变化由生成代码和 fingerprint 处理，provider 始终只看 bytes；
- UDP、内存队列和日志 provider 共享小型函数表，BusCore 不包含传输私有字段；
- 后台线程只完成接收与入队，业务 callback 仍在调用 `lcm_handle()` 的线程执行；
- ready fd 把库接入 `poll/select`，应用无需接受一套固定线程池；
- 在线接收和离线回放共享同一 wire payload，算法输入边界不需要重写。

这些选择让调用链很短，也让应用继续拥有调度权。例如控制循环可以在明确位置调用 `lcm_handle_timeout(0)`，而不必允许任意网络线程重入控制器。但“应用拥有调度权”同时意味着应用必须自己处理慢回调、调用频率和线程优先级。

## 数据结构与性能账本

| 阶段 | 时间复杂度 | 数据动作 | 主要风险 |
|---|---:|---|---|
| encode | `O(S)` | 对象写入连续 bytes | 动态长度溢出 |
| UDPM short publish | `O(1)` syscall | iovec 避免用户态拼接 | socket/锁等待 |
| UDPM fragmented publish | `O(N)` syscalls | 原 payload 的分片视图 | 丢片、长时间持有发送锁 |
| receive | `O(packet)` | 内核复制到用户区 | socket buffer 溢出 |
| reassembly | `O(S)` | 分片复制到完整 buffer | 内存压力；固定版本靠新项触发 LRU，不按时间过期 |
| admission | `O(H)` | 更新匹配订阅的计数 | 核心锁竞争 |
| dispatch | `O(H)` + callbacks | payload 在回调期间借用 | 慢 callback 串行阻塞 |
| decode | `O(S)` | 构造业务对象 | 分配与失败清理 |

`S` 是消息字节数，`N` 是分片数，`H` 是匹配订阅数。表里的 `O(1)` 不代表常数时间有保证；一次 `sendmsg` 仍可能受内核队列、系统调用和 transmit mutex 影响。

UDPM 的 ring buffer 主要减少未分片报文的频繁分配，并非固定内存上限。空间不足时实现会换成更大的 ring，旧 ring 要等仍引用它的消息释放后才能销毁。因此应同时观察 ring 容量、完整消息队列、订阅配额和分片重组表，不能用一个“队列深度”代替全部内存预算。

## 工程优势与适用范围

LCM 的主要优势是机制少、边界清楚。公共层与传输层解耦，callback 线程由应用控制，wire payload 可以原样记录，多语言通过生成代码而不是运行时反射互通。

这使它适合受控局域网中的实验机器人、车辆研究平台、需要多语言消息和确定回放的数据采集系统。它也适合嵌入已有事件循环，因为 `get_fileno()` 暴露的是“完整消息可处理”的 ready 事件，而不是要求应用直接理解 UDP 分片。

## 缺点与性能边界

默认 UDPM 没有端到端可靠交付、拥塞控制、安全认证或访问控制。短消息 `publish` 返回 0 表示本机 `sendmsg()` 接受了完整 datagram，不能证明远端接收、解码或执行了业务逻辑；固定版本长消息分支甚至会在片段发送失败时仍返回 0，因此不能把它解释成本机完整发送保证。`lcm_udpm_publish()`。

单个 LCM 实例一次只允许一个 handle 分发者，匹配 callback 按顺序执行。一个 20 ms callback 会直接推迟其后的 callback；将处理转交工作线程虽然能缩短 handle 时间，却要求应用复制 payload，并自行定义工作队列容量和关闭顺序。

类型 fingerprint 擅长拒绝不兼容布局，但不是动态 schema 协商。分片重组能够还原完整大消息，却不能恢复丢失分片；消息越大、分片越多，整条消息成功到达的概率越容易下降。

因此，可靠命令、跨公网通信、身份认证、动态服务发现、复杂 QoS 和硬实时调度都不能由 LCM 默认机制直接保证。系统若需要这些能力，应增加明确的协议层或选择更合适的中间件，而不是把希望寄托在“局域网通常没问题”。

## 可迁移的设计能力

从 LCM 可以提取五种通用方法：

- 用稳定小接口隔离可替换传输，让核心逻辑不依赖 socket 私有字段；
- 用通知句柄把后台接收转换为外部事件循环可组合的 ready 事件；
- 把 callback 放在核心锁外，并用延迟回收处理回调中的取消订阅；
- 把在线通信与离线回放统一到相同的字节和分发边界；
- 分别约束消息数、总字节、重组状态和处理时间，而不是只设置一个模糊队列长度。

这些思想同样适用于 ROS 2 executor、设备驱动、共享内存总线和自研日志系统。迁移时要复制的是边界和不变量，而不是 `lcm_t` 的字段名称。

## 最小复刻从单线程语义开始

### 第一阶段：单线程内存总线


```cpp
class MemoryBus {
 public:
  using Bytes = std::span<const std::byte>;
  using Handler = std::function<void(std::string_view, Bytes)>;

  SubscriptionId Subscribe(std::string pattern, Handler handler);
  void Publish(std::string channel, std::vector<std::byte> payload);
  bool HandleOne();
};
```

`Publish` 把拥有自身存储的 Message 放入 `std::deque`，`HandleOne` 取出一条并调用匹配 handler。先验证注册、精确匹配、callback 顺序、取消订阅和 payload 借用期，不要在事件语义尚未稳定时提前优化内存。

### 第二阶段：显式过载策略

同时设置最大消息数和最大总字节数，因为十条图像与十条姿态消息的内存代价完全不同。明确选择 `drop-newest`、`drop-oldest`、阻塞或立即拒绝，并允许不同 channel 使用不同语义。控制状态常关心最新值，命令与审计数据则通常不能静默覆盖。

### 第三阶段：Provider 接口

把 MemoryQueue 移到 `ProviderOps` 后面，让 BusCore 只调用抽象方法。若需要稳定 C ABI，使用 `ops struct + opaque self`；若只在单一 C++ 程序内部使用，可以先采用纯虚接口。两者隔离的是同一个变化点。

### 第四阶段：UDP 短消息

先限制为小于安全 MTU 的单数据报，格式包含 magic、sequence、channel length、payload length、channel 和 payload。为 header 编写纯 encode/decode 函数，并用固定字节 fixture 测试大小端、截断、超长和错误 magic。

### 第五阶段：接收线程与通知句柄

网络线程只 parse 和 enqueue，业务 callback 继续由 `HandleOne()` 调用者执行。队列从空变为非空时发出通知；若处理一条后仍有数据，ready 状态必须保持。此阶段就完成关闭协议：发出停止事件、唤醒、join，最后销毁 socket 与队列。

### 第六阶段：分片重组

重组 key 至少包含来源标识与 sequence。每项保存目标 buffer、fragment bitmap、总片数、channel 和 deadline，并分别限制单消息大小、并行项数、总字节数与过期时间。完成后把 buffer 移入完整消息队列，避免再次复制大消息。

### 第七阶段：生成式类型

先支持少量固定基本类型与定长数组，为 schema 计算稳定 fingerprint，并生成 Size、Encode、Decode 和 Cleanup。跨语言 golden test 要证明 C++ 与 Python 对同一对象产生完全相同的字节；任意截断位置都必须安全失败。

### 第八阶段：日志与回放

日志直接保存 wire payload，不 decode。回放 provider 逐条读取事件并通过同一 BusCore 分发。先支持尽快回放，再加入单调时钟定时、倍率、seek 与暂停；同时归档 schema 版本，避免日志字节与新生成类型失配。

## 把一次 15 ms 卡顿当成架构设计评审

假设 1 kHz 的关节状态和 10 Hz 的点云共用一个 LCM 实例，可视化 callback 偶尔为了截图阻塞 15 ms。这个很普通的故障足以把整套运行时的层次全部暴露出来：

~~~text
t=0 ms   JOINT #1 到达，应用开始 callback
t=1 ms   JOINT #2 到达，receiver 仍可 recv
t=2 ms   JOINT #3 到达，filled queue / subscription 准入继续变化
...
t=15 ms  callback 返回，应用线程重新处理积压消息
~~~

这里的时间只是教学时序，不是固定仓库的测量结果。真正需要检查的是：**发送成功、内核接收、provider 重组完成、subscription 获得准入、业务 callback 完成**是五个不同事件。任何一个“success”都不能替代另外四个。

从这一个故障往回看，前面的所有机制就不再是孤立模块：

| 机制 | 它解决的具体失败 | 它没有替你解决什么 |
|---|---|---|
| generated codec | 本机对象布局不能直接当 wire format | schema 演进策略 |
| provider vtable | transport 变化不应污染公共 API | 不同 provider 语义完全一致 |
| LC02 / LC03 | 单 UDP 报文大小有限 | 可靠交付与重传 |
| receiver thread | 慢 callback 不直接卡 socket recv | 应用侧持续过载 |
| per-subscription quota | 慢订阅者可独立拒绝新消息 | 历史消息补发 |
| ring / descriptor reuse | 减少频繁分配并维持 payload 寿命 | 固定内存上限 |
| deferred unsubscribe | callback 遍历期不释放正在引用的节点 | 跨线程随意析构用户对象 |
| EventLog | 保存原始总线字节以便回放 | 保存完整程序、schema 与外部世界状态 |

### 设计模式应当最后命名

当我们发现“公共 publish/subscribe/handle 稳定，而具体 transport 独立变化”时，才需要 `(provider state, vtable)`；把这个结构命名为 C 风格 Strategy 只是总结。URL scheme 找到对应 vtable 并调用 `create()`，承担的是 Factory。两者解释的是“如何选择和构造 transport”，不能顺手解释 callback 生命周期、队列配额、ring 回收或线程通知；那些是独立约束。

同样地，`get_fileno()` 也不是“为了支持 epoll 的技巧”。它解决的是另一个变化轴：**LCM 不应该强迫应用接受自己的主循环。** GUI、控制器、仿真器可以拥有自己的 event loop，只把 LCM ready fd 当作其中一个事件源。代价是应用必须自己决定什么时候 handle、一次处理多少、停止时如何唤醒阻塞等待。

### 如果从零实现，先写不变量再写类

在创建任何 `Bus`、`Provider`、`Subscription` 之前，先把这些约束写成测试：

1. typed decode 不依赖 C++ `sizeof`，fingerprint 不匹配必须失败；
2. provider 的状态对象与方法表必须严格成对；
3. callback 返回前 payload 有效，返回后不得再借用；
4. 核心锁不能覆盖用户 callback；
5. 一个订阅满了不能让另一个订阅的计数一起失真；还要检查 receiver 与 handle 交错时，计数不能被误当成特定消息的逐条准入记录；
6. 旧 ring 仍被消息引用时不能提前释放；
7. callback 内取消订阅不能让当前遍历使用悬空对象；
8. shutdown 必须先停止接纳新工作，再让 receiver/handle 退出，最后销毁业务对象；
9. UDPM `publish()==0` 不能被解释为远端已经处理；
10. 日志回放的事件时间不能自动等同传感器采样时间。

能用测试和状态图证明这些不变量，才算真正复刻了 LCM 的核心结构；“两个进程能互相打印字符串”只证明 happy path 能跑。

## 最后一关：用一次故障检验整条设计链

前面八层架构若只凭“能发、能收”验收，会遗漏最容易毁掉机器人实验的部分：消息年龄、分片缺失、跨线程对象寿命和关闭竞态。我们给第一版消息总线设置一个简单但尖锐的负载：关节状态每 1 ms 发布一条，偶尔夹杂一份大型点云，而可视化 callback 会随机阻塞 15 ms。

~~~text
t=0ms  状态 #1 发出并到达，GUI callback 开始画图
t=1ms  状态 #2 到达，receiver 可以先放进完整消息队列
t=2ms  状态 #3 到达，GUI 仍占住 handle 线程
...
t=15ms GUI 终于返回，但它接下来拿到的可能已经是旧消息
       某订阅者的配额满时，后来的消息也可能被拒绝
~~~

这里的具体时间只是假定的教学负载，并非上游性能实测。它迫使我们将“成功”拆成五个事件：发送系统调用完成、网络包到达、provider 重组成功、subscription 准入通过、用户 callback 消费完成。它们发生在不同时间与不同对象里，不能共用一个成功标志。

| 构件 | 必须保持的不变量 | 亲手制造的失败 |
|---|---|---|
| 生成类型 | wire bytes 不依赖 C++ 对象内存布局 | 用修改后的 schema 读旧消息 |
| Provider | 方法表与私有实例匹配，唯一拥有者负责销毁 | 切换内存与 UDP 后端 |
| LC02/LC03 | 成功重组的切片按偏移刚好覆盖全部 data | 丢第一片、重复末片、乱序交付 |
| 接收队列 | head/tail/count 同锁维护，callback 期间 payload 不被覆盖 | 让消费者长期阻塞 |
| 订阅分发 | 各订阅配额独立，回调内取消订阅不悬空 | A 回调取消 B，C 已经满额 |
| Ring allocator | 释放必须回到消息所属的 ring，而非总是当前 ring | 旧消息仍在途时让 ring 换代 |
| EventLog | 坏长度不能越界，日志时间不自动等于传感器采样时间 | 截断文件并变速回放 |
| Shutdown | 停止接收与 callback，确认借用结束，再析构依赖对象 | callback 正在执行时请求退出 |

特别区分**实现事实**与**设计目标**：固定版本的 UDPM 大消息发送遇到部分分片 `sendmsg()` 失败，仍可能递增序号并返回 0。因此“每片成功才报告发送成功”是你复刻或改进时应补的契约，而不是 LCM 本身已经提供的保障。对真正需要执行确认的运动命令，应另建消息序号、ACK、去重、超时和失效安全状态。

### 把设计模式放到最后才有意义

首先发现 UDP、内存队列和文件日志是独立变化的实现，而上层只需要稳定的 publish/subscribe/handle 语义，才自然得到 `(provider instance, vtable)`；此时再把它称为 C 风格 Strategy。然后因为 URL 要同时选择匹配的 vtable 和实例创建器，才出现 Provider 工厂。Strategy/Factory 并不自动解决分片、线程、取消订阅或 ring 的所有权。能用最小反例验证这些边界，才说明你真的从使用者走到了框架设计者。

## 完成标准由不变量决定

一个“能跑”的演示还不是完整消息总线。最小复刻至少要证明：

1. provider buffer 在全部匹配 callback 返回前有效，之后不可访问；
2. 核心 mutex 不覆盖用户 callback；
3. 同一 Bus 实例一次只有一个分发者；
4. unsubscribe 不会释放当前迭代仍引用的订阅对象；
5. 队列、重组与日志都有消息数和字节数边界；
6. shutdown 先停止并 join 线程，再释放线程可能访问的状态；
7. publish 成功不会被解释为远端接收成功；
8. fingerprint mismatch 必须显式失败，不能继续错位解析。

读者若能用测试和状态图证明这些不变量，而不只是让两个进程互相打印字符串，就已经复刻了 LCM 最核心的工程结构。
