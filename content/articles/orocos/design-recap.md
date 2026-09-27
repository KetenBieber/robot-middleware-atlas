# Orocos RTT：把组件、执行与数据边界合成一条控制链

考虑一台移动机器人：里程计每 2 ms 更新一次，控制器也每 2 ms 计算一次速度指令，诊断线程偶尔查询状态，急停状态则必须立刻改变输出。若把这些任务写进一个共享循环，记录日志的慢 I/O 会推迟控制，诊断查询可能与控制同时修改状态，队列在输入过载时还会把几百毫秒前的命令留给执行器。RTT 的价值在于把这几类问题放到不同边界中处理，再由部署者显式配置边界之间的关系。

本文固定版本为 [orocos-toolchain/rtt，commit `600102e8be9c81905b20930e32d43b28244ab173`](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。下图是该版本的角色关系摘要；箭头表示调用或数据方向，所有权需要单独判断。

```text
Deployment
  -> TaskContext
       ├─ TaskCore：状态与生命周期 hook
       ├─ Service：Operation、Property、Attribute
       ├─ ExecutionEngine：消息、端口事件、hook 的执行顺序
       ├─ Activity：周期或事件驱动的执行机会
       └─ DataFlowInterface
            └─ InputPort/OutputPort -> ChannelElement 链 -> storage/transport
```

图里的线不能一概理解为“持有”。固定版本中，TaskCore 用原始 `ExecutionEngine*` 创建并负责删除引擎；TaskContext 的默认 Activity 使用 `boost::shared_ptr` 持有；ActivityInterface 保存非拥有的 Runnable 指针；Runnable 又保存非拥有的 Activity 指针。引用计数只延长被管理对象的寿命，不会替代线程停止与 join。源码分别见 [TaskCore 构造和析构](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L53-L76)、[TaskContext Activity 成员](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.hpp#L680-L698) 与 [ActivityInterface 的 Runnable 绑定](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ActivityInterface.hpp#L45-L75)。

## 一个样本经过哪些边界

假设里程计组件写入 `OutputPort<State>`，控制器通过 `InputPort<State>` 读取。连接建立后，中间有一串 `ChannelElement`：某个节点可以保存 DATA 最新值，另一个可以提供有界 BUFFER，也可以把样本交给 transport。端口是业务 API；存储策略决定多个样本到来时保留什么。若消费者只能使用最新状态，DATA 会覆盖先前状态；若每个离散事件都必须按序处理，则使用有界 BUFFER，并明确满载时拒绝新样本还是覆盖旧样本。固定版本对普通 BUFFER 与 CIRCULAR_BUFFER 的满载语义定义在 [`ConnPolicy.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ConnPolicy.hpp#L50-L105)，读端以 [`FlowStatus`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/FlowStatus.hpp#L48-L67) 区分新值、旧值和无值。

这几个状态对控制有不同后果：OldData 可用于短时保持，但不能当成新测量；若样本 age 超过控制策略允许的上限，继续输出会让旧速度命令或旧姿态估计一直作用在机器人上。教学最小策略可以为每条输入记录最后一次 NewData 的单调时钟时间，过期即写安全输出。该时钟策略属于应用层设计，不是 RTT 的 `FlowStatus` 自动提供的功能。

数据到达也不表示回调已经运行。EventPort 写入样本后可把端口身份加入 Engine 的 port callback 队列并触发 Activity。先后要区分：样本已存入 ChannelElement、通知已排队、Activity 等待条件已满足、操作系统把线程设为 runnable、该线程实际得到 CPU、Engine 最终调用用户回调。固定版本的 [ExecutionEngine::process(PortInterface*) 与 processPortCallbacks()](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L232-L277) 展示了队列及触发入口；[Activity::loop()](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.cpp#L173-L261) 才是在 Activity 线程等待并进入 runnable 工作的循环。

这里有一个重要的过载反例：若一个端口 callback 要写日志并耗时 0.5 ms，短时间堆入 30 个 callback 已可占去约 15 ms，足以让 2 ms 控制器错过多个周期。固定版本的三条 Engine 消息队列容量为 100，但 `processMessages()` 和 `processPortCallbacks()` 会排空队列；队列容量限制存储量，不给单周期执行时间设上限。这个结论由 [`ExecutionEngine.cpp` 的队列定义、drain 与 `work()`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L54-L75) 和 [`processMessages`/`processPortCallbacks`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L207-L248) 支持。**工程推导：**需要硬截止期的系统应把日志/诊断分到较低优先级消费者，约束 callback 的工作，或在自己的 Engine 实现中增加每周期处理限额；这些限额不是该 RTT 提交的现有保证。

## 状态、执行机会与业务动作

`TaskCore` 的状态回答“组件当前允许进入哪个生命周期动作”。`Activity` 回答“哪个线程何时获得执行机会”。`ExecutionEngine` 回答“该次机会按什么顺序处理排队消息、端口回调和 hook”。它们相邻但不可互换：把状态检查放在业务组件的任意线程里，会让非法 start/update 竞态；把调度策略写死在 Engine，则同一业务组件不能更换 Activity；把所有工作都放到周期 hook，会使管理命令和数据回调的延迟不可见。

固定版本 `ExecutionEngine::work(reason)` 对 Trigger 处理消息和端口回调；周期超时或 I/O ready 还会运行 function 队列与 TaskCore hooks。`processHooks()` 只有在当前态与目标态都为 Running 时调用 `updateHook()`；运行期错误会进入 `errorHook()`，异常走 TaskCore 的异常状态路径。源码见 [`ExecutionEngine::work/processHooks`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L330-L392)。这一顺序意味着 OwnThread 请求有机会先于周期 hook 修改组件状态；若其动作耗时过长，控制 hook 仍会被延迟。

线程的调度策略是 Linux 层的另一道约束。Activity 可以请求调度器、优先级、周期和 CPU affinity；这些参数不会缩短业务执行时间，也不保证配置权限或内核调度策略已生效。低优先级线程持有控制线程所需的普通 mutex 时，高优先级控制线程可能等锁而不能运行，这就是优先级反转的可观察形式。避免方法包括把共享状态转为单线程所有、限制锁内工作、采用支持优先级继承的锁，并实测最大阻塞时间。不能仅凭优先级数字证明截止期。

## 为什么 Operation 要声明执行策略

假设部署器需要在机器人运行期间清除一个可恢复故障。若 Operation 直接在调用者线程修改组件成员，它可能与 `updateHook()` 并发；给每个字段零散加锁，又容易锁住用户代码或形成锁顺序环。固定版本的 Service 默认把 Operation 设为 ClientThread；其成员函数在调用线程运行。OwnThread 则把 invocation 交给目标 TaskContext 的 ExecutionEngine。源码在 [`Service::addOperation`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Service.hpp#L387-L408)、[`OperationCallerInterface::isSend`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/OperationCallerInterface.hpp#L117-L124) 和 [`LocalOperationCallerImpl`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/LocalOperationCaller.hpp#L130-L160)。

OwnThread 的参数会跨越排队时间。固定版本把 invocation 放进执行引擎，但引用参数的存储可能只是保存原变量地址：[`BindStorage::AStore<T&>`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/BindStorage.hpp#L64-L88)。调用方若传入局部变量后立即返回，目标线程执行时就可能访问悬空引用。最小复刻应默认复制小型值参数；对大型数据显式定义共享不可变对象的生命周期；对关闭中的命令定义“完成、拒绝或取消”的可观察结果。发送成功只表示成功排入，不表示业务函数已运行；`SendHandle` 也没有一般性取消正在执行函数的接口，见 [`SendHandle.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/SendHandle.hpp#L53-L123)。

## 从最小系统复刻到工程边界

下面是**教学最小例子的实现顺序**，不是 RTT API 摘录：

1. 先实现 Task 状态转换与失败提交规则，失败的 configure/start 不得伪装为成功状态；
2. 再实现单个可 join 的周期线程，使用绝对截止时间等待，并让 stop 能从另一线程唤醒阻塞线程；
3. 加入固定容量 Operation 队列，明确满载返回、参数所有权及 close 对未完成调用的结果；
4. 建立 DATA 与 FIFO 两种不同存储语义，再增加 OldData/NewData/NoData 和最大 age；
5. 把慢 callback、诊断和记录放进独立执行上下文，测量每个上下文的最坏执行时间；
6. 最后再加入动态类型、插件、跨进程 transport 与实时调度部署。

如果从 lock-free 容器开始，内存序、重用时机、业务所有权和关闭语义会同时进入调试面。**工程推导：**先用互斥锁写出可检查的不变量，再测量确认热点，最后为有证据的瓶颈替换数据结构，通常更容易证明正确。RTT 本身也同时包含 LOCKED、LOCK_FREE 与 UNSYNC 连接选项；是否能使用取决于样本类型、平台实现和并发访问，见 [`ConnPolicy.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ConnPolicy.hpp#L50-L105)。

关闭过程将以上边界重新合在一起：先拒绝新工作，再发布安全输出并停止上游写入；等待当前 Activity step 离开同步点；确认线程不会再进入 Engine；收尾 Operation 和连接；之后执行 cleanup 并释放 TaskContext/插件。固定版本 `TaskCore::stop()` 通过 `ExecutionEngine::stopTask()` 与 Activity 同步后运行 `stopHook()`，而析构与业务 cleanup 是不同路径，见 [`TaskCore::stop`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L232-L255)、[`ExecutionEngine::stopTask`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L401-L409)、[`TaskContext::~TaskContext`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L119-L147)。若 Activity 停止失败而部署者仍销毁组件，OS 线程可能沿 Runnable 指针进入已释放的 Engine 或派生对象；因此 stop 结果、线程退出和对象析构必须作为三个可验证的时刻记录。
