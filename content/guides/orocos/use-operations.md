# Orocos RTT 使用教程：部署、实时检查与故障恢复

抓取控制器的 2 ms 控制周期之外，部署管理器还需要发出复位、读诊断和重新标定命令。若这些函数直接在管理线程运行，它们可能与周期 hook 同时访问控制器状态；若全部同步排队等待，又可能让高优先级线程等待低优先级组件。这里从部署失败回滚讲到 Operation 执行策略与关闭，把调用者、队列和目标执行线程连成一条真实路径。

本文固定到 Orocos RTT 提交 [`600102e8be9c81905b20930e32d43b28244ab173`](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。连接策略以 [`ConnPolicy.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ConnPolicy.hpp) 为准，线程停止以 [`Activity.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Activity.cpp) 为准，hook 与异常收敛则追入 [`ExecutionEngine.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp)。

## 成熟部署配置

将组件类型、实例名、属性文件、Activity 参数、Port connections 和启动顺序纳入版本控制。Deployer 启动时打印最终解析值，而不是只保留模板。

端口可由 Deployer 精确连接并设置 policy；`connectPorts` 按同名同类型自动连接，适合快速实验，不适合需要逐连接审计的生产配置。官方手册建议成熟应用使用部署描述。[Orocos Components Manual](https://www.orocos.org/stable/documentation/rtt/v2.x/doc-xml/orocos-components-manual.html)

## 把部署看成一笔事务

一个控制系统通常含设备、估计器、控制器和记录器。部署不是“依次调用几个 start”，而是一笔有回滚路径的事务：

```text
解析配置
  -> 加载全部组件类型
  -> 创建实例并设置属性
  -> 设置 Activity
  -> 建立并检查端口连接
  -> 按依赖顺序 configure
  -> 按下游到上游的顺序 start
  -> 提交 Running
```

任一步失败都不能继续半启动。若第三个组件 configure 失败，应 cleanup 前两个已配置组件；若 start 途中失败，应先停止已经运行的上游，再停止下游，随后 cleanup。部署器应记录每个动作是否已提交，而不是靠最终状态猜测要回滚什么。

```cpp
// 教学最小例子：演示配置阶段提交记录的结构，不是 RTT 源码
struct DeploymentRecord {
    RTT::TaskContext* task{};
    bool configured{};
    bool started{};
};

bool start_system(std::vector<DeploymentRecord>& order) {
    for (auto& item : order) {
        if (!item.task->configure()) return rollback(order);
        item.configured = true;
    }
    for (auto& item : order) {
        if (!item.task->start()) return rollback(order);
        item.started = true;
    }
    return true;
}
```

示例省略了日志和异常边界，但保留了核心思想：回滚依据显式记录，只撤销已经成功的步骤。`rollback` 按逆序 stop 与 cleanup，而且自身必须幂等。

### 完成回滚实现，而不是把它留在注释里

```cpp
// 教学最小例子：只撤销已经成功提交的 stop/configure 步骤
bool rollback(std::vector<DeploymentRecord>& order) noexcept {
    bool all_ok = true;
    for (auto it = order.rbegin(); it != order.rend(); ++it) {
        if (it->started) {
            if (!it->task->stop()) {
                all_ok = false;
                continue;  // 线程仍可能进入对象，不能 cleanup
            }
            it->started = false;
        }
        if (it->configured) {
            if (it->task->cleanup()) {
                it->configured = false;
            } else {
                all_ok = false;
            }
        }
    }
    return all_ok;
}
```

逆向迭代对应依赖图的反向释放：最后启动的上游生产者先停止，避免它继续向正在清理的下游写入。`started/configured` 是提交日志，不是组件状态的重复缓存；每次动作成功后立刻更新，使第二次 rollback 不会重复执行副作用。`noexcept` 表达失败清理不能再把异常抛过原始错误，但函数仍通过返回值保留“回滚不完整”这一事实。

示例使用非拥有型 `TaskContext*`，因为组件生命周期由 Deployer 管理；vector 只记录事务状态。若复刻独立部署器，可用 `std::unique_ptr<TaskContext>` 表达实例所有权，但不能同时让插件管理器和容器都认为自己负责 delete。

## Activity 参数不是装饰性配置

为每个组件建立执行预算表。以下数字只是一个**教学配置草案**，用于分配目标上限；RTT 不会从此表自动限流或保证截止期：

| 组件 | 触发方式 | 周期/事件上限 | 优先级 | 允许阻塞 | 预算 |
|---|---|---:|---:|---|---:|
| device | 周期 | 1 ms | 85 | 否 | 120 µs |
| estimator | 周期 | 2 ms | 82 | 否 | 300 µs |
| controller | 周期 | 2 ms | 80 | 否 | 250 µs |
| logger | BUFFER 事件 | 500 Hz | 20 | 文件线程可阻塞 | 1 ms/批 |

优先级不是越高越好。若低优先级 logger 持有 controller 需要的普通互斥锁，高优先级反而会暴露优先级反转。跨优先级共享对象应消除锁依赖、使用具备优先级继承的同步原语，或把操作转为单向有界消息。

CPU affinity 也不是默认收益。把多个高频组件固定在同一核会互相挤占；分到不同核又会增加缓存一致性和跨核唤醒成本。配置应来自测量，并和内核、IRQ、CPU 隔离方案一起记录。

### 把周期预算写成可验算的不等式

对周期为 `T` 的组件，一次 Engine step 不只包含 `updateHook()`：

```text
C_step = C_engine
       + C_port_events
       + C_operations
       + C_update_hook
       + C_runtime_noise

必须满足：C_step <= T - J_release - safety_margin
```

`J_release` 是线程实际被调度的释放抖动。只有在你的执行器确实规定每周期最多处理 `B` 个 OwnThread Operation 时，才能把 `B × C_operation_max` 当作上界。固定 RTT 提交的 `processMessages()` 把消息队列排空，没有可配置的每周期数量上限；消息容量为 100 只限制空间，并不限制控制线程处理这些消息所花的时间。测量 RTT 时应把排空消息/端口回调的时间都算进完整 Engine step，并把 callback 保持有界。平均耗时不能证明截止期；至少记录最大值、p99.9、超期次数和连续超期长度。[Engine 队列及消息/端口 drain 源码](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L54-L75)

框架本身也会执行状态检查、消息处理和事件分发，因此“业务 hook 只用了 700 µs”并不意味着 1 ms 周期还有 300 µs。测量点应包围完整 Activity step，同时在 hook 内保留分项计时。

## ConnPolicy 是资源与丢弃语义的合同

`ConnPolicy` 不只有 DATA/BUFFER 两种标签。固定源码还定义了锁策略、push/pull、初始化样本、mandatory 和 transport 等维度：

```cpp
// 教学最小例子：为特定连接显式设置容量、同步和 mandatory 策略
auto policy = RTT::ConnPolicy::buffer(
    128,                         // 有界容量
    RTT::ConnPolicy::LOCK_FREE,  // 同步实现
    false,                       // 建连时不注入初值
    false);                      // PUSH，而非 PULL
policy.type = RTT::ConnPolicy::CIRCULAR_BUFFER;
policy.mandatory = true;
```

| 决策 | 满载或运行时效果 | 适用语义 |
|---|---|---|
| DATA | 只保留最后值 | 位姿、速度等当前状态 |
| BUFFER | 满时拒绝较新的样本 | 不希望覆盖已排队事件 |
| CIRCULAR_BUFFER | 满时覆盖最旧样本 | 优先保证最新状态 |
| LOCKED | mutex 同步 | 先保证通用正确性 |
| LOCK_FREE | lock-free 实现 | 需验证类型与平台支持 |
| UNSYNC | 无同步，不具线程安全 | 仅可证明无并发访问时 |
| mandatory | 某连接写失败会使 write 失败 | 必须观察的关键输出 |

LOCK_FREE 不等于整个 `write()` 没有任何不确定成本：消息复制、类型构造、transport 和 cache miss 仍需计入。UNSYNC 也不是性能开关；只要读写可能并发，它就是数据竞争。生产部署应把每条连接的 policy 完整打印出来，否则同名 Port 看起来连接成功，实际丢弃方向却可能相反。

容量 `N` 的 BUFFER 至少占 `O(N × sizeof(T))`，动态字段还会带来额外堆存储。到达率 `λ` 长期大于消费率 `μ` 时，BUFFER 必然满；增大容量只能把丢包改成长队列年龄。控制系统通常同时约束容量和最大样本 age，超龄后主动进入安全态。

## 管理 Operation 的调用路径

假设控制器暴露 `resetFault()`。如果它修改 updateHook 同时访问的状态，应注册为 OwnThread，让修改在组件 Engine 串行执行：

```cpp
// 教学最小例子：管理线程异步发送，再在非实时上下文收集结果
addOperation("resetFault", &Controller::resetFault, this,
             RTT::OwnThread)
    .doc("Reset a recoverable fault while stopped");
```

调用方仍需处理“已排队但尚未执行”“执行失败”和“关闭时被取消”三种情况。同步等待 OwnThread Operation 的线程不应是更高优先级实时线程；更稳妥的监管方式是异步发送、轮询完成状态，并设置绝对超时。

只读查询也不能自动标成 ClientThread。如果查询遍历会被 updateHook 修改的容器，ClientThread 会引入数据竞争；应提供不可变快照、原子统计量，或仍放到 OwnThread。线程选择来自数据所有权，而不是函数名字看起来像 getter。

### 用 SendHandle 把等待变成显式状态

```cpp
// 教学最小例子：通过 SendHandle 检查发送/完成状态
RTT::OperationCaller<bool(void)> reset_fault =
    peer->getOperation("resetFault");

auto handle = reset_fault.send();
if (!handle.ready()) {
    report_binding_error();
} else {
    bool result = false;
    const RTT::SendStatus status = handle.collectIfDone(result);
    if (status == RTT::SendSuccess && result) {
        report_recovered();
    }
}
```

`send()` 返回并不代表业务函数已经运行；`SendHandle` 保存对异步 invocation state 的观察能力。固定源码中，空或不兼容的 handle 令 `ready()` 为 false，`collect()`/`collectIfDone()` 返回 `SendStatus`，调用方必须区分尚未完成、发送失败与成功结果。销毁 handle 通常只是放弃收集结果，不应被理解为安全取消正在操作硬件的函数。

轮询必须挂在低优先级监督循环，并由单调时钟控制绝对 deadline。不要在高优先级控制线程中做无界 `collect()`；它可能等待低优先级 Activity，从而制造优先级反转。超时后也不能假装目标没有执行，因为请求可能已经进入 Running；带副作用的 Operation 需要 command id、可查询状态或协作式取消协议。

OwnThread 参数必须跨越排队时间。值参数会被 invocation state 拥有，成本可分析；引用或裸指针可能在真正执行前悬空。大对象可使用 `shared_ptr<const T>`，但要接受原子引用计数与对象寿命延长；实时命令更适合固定容量值类型。

## 监督器状态机

组件的 Error 状态需要系统级决策。监督器可以维护如下状态：

```text
BOOT -> CONFIGURING -> READY -> RUNNING
                      ^          |
                      |          +-- recoverable --> DEGRADED
                      |                              |
                      +--------- recovered ----------+
RUNNING/DEGRADED -- fatal --> SAFE_STOP -> CLEANUP -> FAILED
```

进入 `DEGRADED` 时先发布安全输出、冻结新的管理命令，再决定恢复动作。重试次数与总时限必须由系统配置规定，例如 10 秒内最多三次；超过上限转入 SAFE_STOP。不要让 `errorHook()` 自己无限重连，因为它在组件的 Engine 执行上下文运行，会延长当前工作步骤，也缺少整个系统的依赖视角。

一个可操作的恢复序列是：

1. 停止上游命令源，阻止新工作进入故障组件；
2. 请求组件 stop，并确认 Activity 已退出；
3. 在非实时线程关闭并重开设备资源；
4. cleanup 后重新加载配置并 configure；
5. 检查端口连接、初始样本和设备健康；
6. 从下游到上游重新 start；
7. 在观察窗口内限制输出，并确认无再次故障。

如果设备驱动无法可靠取消阻塞 I/O，进程内 recover 并不安全。应把驱动隔离到可重启进程，以进程退出作为最终取消机制。这是可行性边界，不是再加一层异常捕获能够修复的问题。

## 运行期禁止项

- updateHook 内无界分配；
- 阻塞文件、网络和日志 I/O；
- 无上限处理 Operation/Port callback；
- 未知最坏时间的锁；
- 动态加载插件和首次解析大配置。

在 configure 阶段预触页、预分配容器并建立连接。运行期用 allocator hook、page fault 与 queue watermarks 验证假设。

### C++ 中看似无分配、实际可能分配的操作

- `std::vector::operator=` 在目标 capacity 不足时扩容；
- `std::string` 超过 small-string buffer 时进入堆；
- `std::function` 捕获对象较大时可能分配；
- 日志格式化和异常构造通常会分配；
- 第一次调用某些库函数可能触发 lazy initialization；
- `shared_ptr` 创建控制块以及最后析构复杂对象都可能落在实时线程。

预分配不仅是 `reserve()` 一次：还要约束输入最大长度，确认赋值不会突破 capacity，并把最终释放放到非实时阶段。可以在进入 Running 前预热代码路径，在 updateHook 周围统计 major/minor page fault 与分配次数；发现运行期分配时记录调用栈，而不是只在代码审查中寻找 `new`。

## 错误与安全态

RunTimeError/Exception 不能只改变状态枚举。组件应明确安全输出，并由监督组件决定 recover、stop/cleanup/reconfigure 或重启进程。

`errorHook` 必须有界，不能无限重连。设备重连放到低优先级服务或状态机，并给出最大尝试时间。

固定源码中的 `ExecutionEngine::processHooks()` 给出两条不同路径：组件主动进入 `RunTimeError` 后，Engine 在后续 step 调用 `errorHook()`；`updateHook()` 抛出未处理异常时，Engine 捕获异常并调用 `taskc->exception()`，进入更严重的 Exception 路径。源码注释表明该路径会调用 stop/cleanup hook。

因此不能把 `error()` 与抛异常当成同一种恢复机制。可恢复的传感器超时应显式发布安全输出并进入 RunTimeError，由有界 `errorHook()`维持安全行为；破坏组件不变量的异常应停止继续更新，由系统监督器决定重新创建实例或重启进程。无论哪条路径，安全输出都不能只依赖“后续还有一次 hook 被调用”，硬件接口还应有独立 watchdog。

## 关闭剧本

```text
stop upstream producers
  -> controller.stop() / stopHook safe output
  -> join Activity
  -> disconnect ports and cancel Operations
  -> cleanup() releases configured resources
  -> unload component libraries
```

不能在线程仍进入 ExecutionEngine 时卸载组件共享库。

`Activity::stop()` 也不是简单写一个布尔值。非周期 Activity 若正在自定义 loop 中，RTT 会先请求 `breakLoop()`；返回 false，或 loop 在 stop timeout 内没有返回，`stop()` 都会失败。周期 Activity 同样要等当前 step 离开同步点。固定源码的析构函数在 `stop()` 后还调用 `terminate()`，因为注释明确指出 stop 不保证底层线程已经结束。

这带来三个工程要求：

1. 自定义阻塞 loop 必须实现能从另一线程安全调用的 `breakUpdateHook()`/取消路径；
2. 设备 I/O 必须有有限超时或可中断句柄，否则 Activity 无法证明停止；
3. 只有 Activity 已停止并完成线程收口后，才能析构 TaskContext 或卸载包含其虚函数代码的共享库。

若 stop 失败，不应继续 cleanup/unload 并期待析构“顺便解决”。监督器应保持对象与库存活，切断硬件输出，然后升级为进程终止；这就是无法取消的第三方阻塞调用所形成的可行性边界。

## 实时报告

每次部署记录 OS、scheduler、priority、affinity、period、WCET p99.9/max、deadline miss、queue depth、drops、page faults 和运行时分配。只写“实时优先级 80”不足以证明确定性。

## 故障注入

- input 停更，验证 OldData watchdog 与安全输出；
- Operation 洪泛，确认固定 RTT 的消息队列有容量上限但 drain 时间没有 per-cycle batch 上限，并测量 updateHook 延迟；
- buffer 满载，验证 drop counter；
- updateHook 抛异常，验证状态与 stop；
- stop 与 Port 断连并发，验证无死锁；
- 重复 cleanup/unload，验证资源恰好释放一次。
