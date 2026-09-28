# 设计复盘：Cyber RT 的分层逻辑与可复用能力

本篇复盘的源码事实统一对应 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`；正文直接展示关键机制，不以源码链接或仓库路径代替代码。

从系统边界进入组件装配，再从 ring、Dispatcher、Notifier、CRoutine 和 Processor 组装出消息链后，可以回到设计问题：这些层解决了哪些可观察的故障，哪些机制适合迁移到其他机器人运行时，哪些实现细节反而需要重新设计？调度专题分成[事件与任务状态](croutine-wakeup.md)和[Processor 上真正执行](processor-context-switch.md)，下面的复盘不重复两篇文章的逐行源码。

总结不是再列一次类名。它要把源码事实压缩成可以迁移到其他机器人运行时的设计判断。

## 先用三张图重建全貌

部署图回答“哪些东西在同一进程”：

```text
launch
  -> mainboard process
       |-- dynamic component .so
       |-- Component / Node / Reader / Writer
       |-- Transport singletons
       |-- DataDispatcher / DataNotifier
       `-- Scheduler / Processor threads
```

对象图回答“谁拥有谁”：

```text
ModuleController
  strong -> Component
    strong -> Node / Readers

Scheduler
  strong -> CRoutine
    strong -> DataVisitor
      strong -> CacheBuffer
    callback -> weak Component

DataDispatcher
  weak -> CacheBuffer
```

运行图回答“谁调用谁”：

```text
transport thread
  Receiver -> Dispatcher -> CacheBuffer -> Notifier
                                           |
                                           v
Processor thread
  NextRoutine -> Resume -> DataVisitor -> Process -> Proc
```

三张图不能互相替代。调用关系相邻的两个对象未必互相拥有；同一进程中的对象也未必在同一线程执行。源码分析一旦把部署、所有权和调用混成一张图，就容易产生“Reader 拥有 callback，所以 callback 在 Reader 接收线程运行”一类错误结论。

## 分离 transport 与业务执行，解决的是故障传播

让 receiver thread 直接执行用户 callback 是最短路径，却把 transport 的可用性交给不可控业务代码。一段阻塞 I/O、一次模型推理长尾或一把竞争 mutex 都可能阻止接收线程继续工作。

Cyber 在中间加入有界 buffer 和 scheduler event：

```text
receive/decode
  -> bounded handoff
  -> wake intent
  -> scheduled business execution
```

这条边界可以直接从固定提交的 `DataDispatcher<T>::Dispatch()` 看见。下列是**固定提交源码摘录**：

```cpp
bool Dispatch(const uint64_t channel_id,
              const std::shared_ptr<T>& msg) {
  BufferVector* buffers = nullptr;
  if (!buffers_map_.Get(channel_id, &buffers)) {
    return false;
  }

  for (auto& buffer_wptr : *buffers) {
    if (auto buffer = buffer_wptr.lock()) {
      std::lock_guard<std::mutex> lock(buffer->Mutex());
      buffer->Fill(msg);
    }
  }

  return notifier_->Notify(channel_id);
}
```

接收路径逐个取得缓存的临时强引用，在每只 buffer 自己的互斥锁下写入 `shared_ptr`，全部填完才发 channel 通知。函数里没有调用业务 `Proc()`：消息正文留在缓存，通知把 Processor 引回“重新检查 visitor”的调度路径。若把 `Proc()` 塞回这里，一次 30 ms 的感知计算就会让当前接收回调多占用约 30 ms；其他 channel 的消息要等它返回才能继续走完分发。细看弱引用注册表和这个锁粒度，见[完整消息链中的 Dispatcher 分析](message-to-proc.md)。

这会增加排队和唤醒成本，却把故障范围切开。Transport 只承担解析、扇出和通知；业务执行时间主要消耗 Processor。设计目标不是最少函数调用，而是让慢业务不直接占住通信线程。

如果自己实现机器人中间件，这个边界通常值得保留。只有 callback 极短、调用关系完全静态、线程抖动可以严格控制时，才适合为极低延迟选择 inline callback；即使如此，也应明确禁止 callback 阻塞，而不是默认开发者会自觉。

## 数据与事件分离，解决的是唤醒合并

Scheduler 不保存每条消息，只保存 task state；消息数量由 ring 表示，Notifier 只说“请重新检查”。这样三条消息可以只产生一次有效 wakeup，consumer 恢复后连续取数。

这一设计必须同时满足三个条件：

```text
先写数据，后发事件
事件丢失或合并后，非空 buffer 最终仍会被检查
消费者处理一条后会继续检查 backlog
```

Cyber 的 RoutineFactory 与 `updated_` 事件闩锁旨在避免“数据已经到达、任务却永久睡眠”；但这不等于当前固定提交已经证明了完整的无丢唤醒协议：`CRoutine::state_` 是跨线程访问的普通枚举，通知侧和运行侧缺少可见的统一同步约束。这里的三条是设计一个可靠运行时**必须保证的性质**，不是对当前实现所有竞争交错的无条件正确性背书。自己实现时可以用 sequence counter、eventfd 或受 mutex 保护的条件变量谓词，不必复制 atomic flag 的具体写法；先证明事件记录、状态改变、消费者检查三者在并发时不会留下永久睡眠窗口。

## 每个消费者独立 buffer，换来隔离也增加 producer 成本

独立 buffer 让 Component、Reader Observe、录制器和监控者各有 queue depth 与读取游标。慢日志不会从控制器手中“pop 掉”消息，也不会让控制器必须等待它清空队列。

代价是 producer 侧 fan-out。每新增一个 DataVisitor，Dispatch 就多一次 weak lock、buffer mutex 和 shared pointer assignment。观察者不是免费的旁路；高频 channel 上临时加很多调试订阅者，可能增加发送或接收线程延迟。

另一种设计是单 ring、多 consumer cursor。它减少 payload pointer 的重复槽位，却使最慢消费者、槽位复用和注销回收更难协调。Cyber 选择复制小型 shared pointer 换取生命周期和队列语义简单，符合消费者数有限、消息对象较大的车辆数据流。

## `weak_ptr` 是生命周期工具，不是 registry 清理工具

Dispatcher 存 weak buffer，callback 捕获 weak Component，都在表达“可以访问，但不能决定对方寿命”。这避免长寿命 singleton 或 task 形成强引用环。

过期 weak entry 仍留在 vector，DataNotifier 也可能保留旧 callback。访问安全已经解决，长期扫描规模却没有自动解决。

因此一套更动态的运行时应同时提供：

```text
weak/non-owning reference        防止悬空和反向续命
registration token / unsubscribe 防止 registry 无限积累
stable read snapshot             防止 add/remove 与遍历数据竞争
```

不要因为用了智能指针就认为生命周期设计已经完整。所有权、注销和并发可见性是三项独立责任。

## 有界 overwrite-oldest 体现“状态优先”

`CacheBuffer` 不阻塞 producer，满时覆盖旧槽；visitor 首次读取和严重落后时又直接跳到 tail。系统偏好新鲜状态，而非完整历史。

这种语义适合 localization、chassis、姿态和障碍物集合，控制器处理旧状态通常没有意义。它不适合必须逐项完成的事务、告警或离散命令。

实现自己的 middleware 时，queue policy 不应只有一个全局默认值。至少应明确区分：

```text
latest state       只保留最新值
bounded history    保留有限历史，定义丢旧或丢新
reliable event     允许背压或持久化，不能静默覆盖
time-synchronized 按时间窗匹配多路样本
```

Cyber Component 默认路径更接近前两类。若把同一语义套在全部机器人数据上，错误不一定立刻崩溃，而会表现为偶发状态跳变或丢动作。

## 协程减少线程数量，却没有消除 WCET

CRoutine 让许多任务共享少量 Processor 栈切换资源，Scheduler 可以统一做 group、priority 和 affinity。它比“每个订阅一只线程”更便于控制线程规模。

但 Component callback 内没有自动抢占点。`Proc()` 的最坏执行时间、锁等待、内存分配和 page fault 仍决定同一 Processor 上其他任务的等待上界。

因此正确的推导是：

```text
userspace coroutine
  -> cheaper/coarser controlled switching
  -> fewer OS threads
  -> configurable worker placement

does not imply
  -> preemptive real-time scheduling
  -> deadline guarantee
  -> no priority inversion
```

对严格周期任务，调度结构必须与业务约束一起设计：独立 Processor、CPU affinity、实际生效的 OS policy、受限 WCET、无阻塞 I/O、小 queue 和数据年龄检查缺一不可。

## DAG 与动态库把选择推迟到部署期

`mainboard` 不链接每个具体算法类，而是加载 `.so`、按 class name 查 factory、创建 `ComponentBase` 派生对象。这让同一宿主进程能按 DAG 组合不同算法。

它隔离的变化是“本次车辆/场景要运行哪些组件”。代价是一些错误从编译期推迟到运行期：库路径不存在、注册宏遗漏、class name 拼错、ABI 不兼容、channel 类型不匹配。

工业系统若采用同类架构，需要在部署前对 DAG 与动态库做静态检查，并让启动失败快速、清晰地暴露，而不是进入部分可运行状态。动态性越强，启动与回滚路径越重要。

关闭顺序也成为架构的一部分。**若从零设计一套更稳妥的 plugin runtime**，可以先封业务入口和停止新输入，再等待 Reader/Component task 上的在途 callback 退出，之后释放业务资源、销毁派生对象，最后卸载库。这里是推荐协议，不是 Apollo 当前 Component 的精确执行顺序：固定提交的 ComponentBase::Shutdown() 先调用派生 `Clear()`，然后关闭 Reader，最后才 `RemoveTask()` 等待 Component routine；因此若 `Clear()` 释放了正在运行的 `Proc()` 会访问的成员，等待屏障来得太晚。对象析构安全、动态库卸载安全和派生资源并发安全是三个不同条件，完整调用顺序见[从 DAG 到 Component 的关闭分析](dag-to-component.md)。

下面是 **`ComponentBase::Shutdown()` 的固定提交源码摘录**，保留原有关闭顺序：

```cpp
virtual void Shutdown() {
  if (is_shutdown_.exchange(true)) {
    return;
  }

  Clear();
  for (auto& reader : readers_) {
    reader->Shutdown();
  }
  scheduler::Instance()->RemoveTask(node_->Name());
}
```

`exchange(true)` 是原子读—改—写：第一次进入的线程把关闭标志设为 true 并继续，后续重复调用读到旧值 true 后返回。它只让“关闭请求是否已经发布”成为原子状态；它不会等待 `Proc()` 退出，也不会自动取消或唤醒所有回调。更关键的是，派生 `Clear()` 发生在 Reader 关闭与 Component task 移除之前。因此这段真实代码直接证明外层析构顺序还不是业务成员并发安全屏障：若 `Clear()` 释放的资源仍被在途 `Proc()` 使用，必须由组件另行协调，或调整关闭协议。

## 从变化点推导设计模式

现在可以给部分结构命名，但名称只是理解结果。

Component 与 `Proc()` 使用 Template Method：基类规定初始化、关闭和调用骨架，派生类填入业务步骤。价值是统一生命周期，代价是基类模板和多组输入特化较复杂。

ClassLoader factory 使用 Factory/Registry：mainboard 按字符串选择未来新增的派生类。价值是隔离具体类型，代价是运行期配置错误和全局注册状态。

DataNotifier 类似 Observer：channel 更新同步通知多只 task callback。价值是一对多事件传播，代价是注销和长尾 fan-out。

Classic 与 Choreography 是 Strategy：Scheduler public API 不变，任务放置和选择方式可替换。价值是部署策略变化不进入 Component，代价是不同策略的公平性和配置语义需要分别理解。

这些模式成立是因为它们隔离了具体变化，不是因为类名碰巧符合教科书图形。评估新设计时，应先写“未来会变化什么”，再决定是否需要抽象层。

## 性能分析应沿路径分账

只报告一个“middleware latency”无法指导优化。完整 callback 启动延迟至少要分成：

```text
transport receive / decode
  + dispatcher fan-out and buffer locks
  + notifier callbacks
  + OS worker wakeup
  + run queue scan
  + current non-preemptive callback remainder
  + coroutine switch
  + visitor fetch
```

不同部署改变不同区段。把两个组件移到同进程会减少序列化，却不会消除 Dispatcher、Notifier 和 Scheduler；把 task priority 调高会影响 run queue 选择，却不会减少 RTPS decode；把 queue depth 调大能吸收 burst，却会扩大数据年龄窗口。

优化必须针对主导项。消息很大时先看序列化与复制，订阅者很多时看 fan-out，task 很多时看扫描，长尾明显时看 callback WCET、锁、allocator 与 OS scheduling。

## 面向新实现的改进方向

保留数据/执行分层、有界缓存、弱所有权和策略化 scheduler，同时可以改进四处。

registry 应返回可注销 token，并为运行时 add/remove 定义 snapshot 语义，避免 weak entry 与 notifier 历史项累积。

ready selection 可以使用真正的 per-priority ready deque 或 rotating cursor，给同优先级任务明确公平规则，而不是每次从持久 vector 头部扫描。

多输入融合应把 `latest`、exact timestamp、approximate time window 设计为可替换策略，并把最大数据年龄作为配置，而不是只提供 M0-trigger AllLatest。

实时关键路径应提供可观察指标：每阶段 timestamp、queue drop、data age、wakeup delay、callback WCET 与 deadline miss。没有这些量，配置 priority 和 queue depth 很容易变成猜测。

如果目标是硬实时，还需要更根本的约束：预分配、无阻塞或有界阻塞容器、可证明 WCET、实时内核、priority inheritance、静态拓扑和经过验证的 failure model。Cyber 当前结构不能直接升级为硬实时声明。

## 从零实现的推荐顺序

第一阶段只做数据语义：固定容量 ring、逻辑序号、consumer cursor、overwrite policy。用手算序列证明不会读到复用槽的旧身份。

第二阶段做 registry：channel id 映射到非拥有 buffer 引用，定义 add/remove/dispatch 并发契约。

第三阶段做事件：Notifier 不传 payload，允许合并，但保证非空 buffer 最终被检查。

第四阶段做执行器：一只 OS worker、ready task 状态机、condition variable 和 stop/join。先不用 stackful coroutine。

第五阶段加入多 worker、公平 priority 和 task affinity，再替换为可保存上下文的 coroutine。

第六阶段接 transport adapter，逐条标明数据在哪一步序列化、复制、分配和获得所有权。

第七阶段加入 Component、DAG 与 factory，最后完成初始化失败回滚和逆序 shutdown。

这个顺序有意把“并发正确性”放在“框架外观”之前。先做漂亮的 Node/Component API，却没有稳定的 queue、wakeup 和 lifetime，不会得到可用中间件。

## 固定版本四个仍应保持 OPEN 的并发边界

文档和教学工程已经能够闭环，不代表固定源码里的并发边界因此消失。当前至少有四处不能被“示例跑通”覆盖掉：

| 边界 | 固定源码中的问题形状 | 从零重写时应先定义什么 |
|---|---|---|
| Dispatcher registry | `AddBuffer()` 修改内层 vector，而 `Dispatch()` 遍历没有共享同一锁 | topology freeze、读写锁或 immutable snapshot |
| DataNotifier registry | Add/Notify 与 callback 注册缺少完整注销静默期 | registration token、in-flight counter、quiescence |
| `CRoutine::state_` | 通知侧读取、执行侧写普通枚举，缺少明显共同同步 | 原子状态机或同锁下的状态迁移协议 |
| 多输入 fusion 安装 | buffer 先注册，AllLatest callback 后安装，动态创建时存在可见窗口 | 私有构造后一次发布，或显式初始化 barrier |

这四项对应内部审查的 I-004 至 I-007。它们之所以重要，是因为 Cyber RT 的正常使用通常在启动阶段建立拓扑，很多竞态在稳定运行时不容易触发；一旦把系统扩展成“运行中热插拔 Reader/Component”，原本隐含的静态拓扑假设就会成为 API 契约问题。

因此，[端到端闭环工程](../../guides/cyber/closed-loop-project.md)有意采用“先启动 Component 和 Observer，再启动 source”的顺序。这个工程验证的是正常数据链与生命周期，不把运行时热注册安全性作为已证明性质。若未来真的要实现动态拓扑，应该先解决上述 registry、状态机和初始化可见性问题，再谈 API 外观。

## 完整理解 Cyber RT 的能力边界

看到一个 Component 配置，应该能说出 mainboard 如何找到 `.so` 和类工厂，Node、Reader、DataVisitor 与 task 在什么时候创建。

看到一条消息，应该能沿实际 transport 说出它在哪个线程解析，在哪一步形成 `shared_ptr<Message>`，写入几只 ring，哪些锁会被获取，谁发出 wake，`Proc()` 最终在哪只 Processor 线程运行。

看到 `pending_queue_size`，应该能解释首次读取、正常 backlog 和溢出三种状态下返回哪条消息，并估算队列给 100 Hz 或 1 kHz 链路增加的历史窗口。

看到 priority，应该继续追问它属于 Cyber task、Processor OS thread 还是 transport thread，是否存在不可抢占 callback 和 mutex priority inversion。

看到 shutdown，应该能按 Component flag、Reader、task、DataVisitor、receiver、对象析构和动态库卸载的顺序反向走完。

能完成这些推演，才算理解了 Cyber RT 的实现思路。记住 `Writer::Write()` 或 `Component::Proc()` 的用法，只是会使用入口；知道入口下面为什么需要这些对象、它们怎样共同影响机器人闭环，才具备重新设计一套运行时的能力。
