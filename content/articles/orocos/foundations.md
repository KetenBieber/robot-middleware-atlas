# Orocos RTT 阅读基础：一个控制组件如何获得可解释的执行边界

设想一台七轴机械臂：编码器每毫秒给出一次关节状态，控制器也要每毫秒写出力矩；操作员偶尔下达“清除故障”命令，日志组件想保存每个采样点。若把这些工作塞进一个循环，磁盘一次卡顿就会让力矩更新晚到。若再为每个功能随手起线程，控制器和清故障命令就可能同时改同一份状态。

Orocos RTT 的设计问题可以从这里读起：组件的状态转换由谁约束；周期或事件何时让代码运行；跨线程数据保留最新值还是保留历史；停止时怎样确保线程不再访问组件。RTT 把这些决定放在不同对象里，但它不会自动给出硬实时保证。截止时间仍取决于操作系统调度、驱动、用户代码的最坏执行时间、类型复制成本和部署时选定的连接策略。

## 先看两种朴素写法怎样出错

初学者常把采样、控制和日志放在一个循环中：

~~~cpp
// 错误示例：三个不同截止时间共用一个串行循环
while (running) {
    JointState state = device.read();
    TorqueCommand command = controller.compute(state);
    device.write(command);
    log_to_disk(state);            // 某次写盘停 8 ms
    std::this_thread::sleep_for(1ms);
}
~~~

如果文件系统写入用了 8 ms，控制器至少错过数个 1 ms 周期；循环结束后再 sleep 还会把本轮执行时间累加到下一轮，释放时刻逐渐漂移。机器人上的现象是电流更新间隔突然变长，日志中却可能仍有完整记录。问题不是“线程太慢”，而是磁盘工作没有和闭环截止时间隔离。

第二种朴素写法是给控制器和管理命令各开线程，却共享成员变量：

~~~cpp
// 错误示例：普通 bool 和复合目标值没有同步协议
bool running = true;
TorqueCommand target;

void control_thread() {
    while (running) device.write(compute(target));
}
void command_thread(TorqueCommand next) {
    target = next;
}
~~~

C++ 内存模型下，并发读写一个普通对象且没有同步时，程序行为未定义；“结构体通常一次写完”不是保证。可观察结果可能是目标只更新了部分关节，或控制线程读到互不匹配的字段。即使把 running 改成原子变量，target 的复合更新也不会因此变安全。

RTT 把三个缺口分开处理：TaskCore 约束生命周期状态，Activity 提供执行机会，ExecutionEngine 在其执行上下文处理队列和 hook；端口连接中的 ChannelElement 保存或转发样本。先记住这些名称回答不同问题：组件现在允许做什么，哪条执行上下文正在工作，某条样本目前在哪个存储节点。

## 一个组件是接口、状态和业务 hook 的组合

下面的组件是教学最小例子。C++ 的 override 要求派生函数签名确实覆盖基类虚函数；若把 updateHook 拼错，编译器会报错，而不是让框架悄悄调用基类空实现。

~~~cpp
// 教学最小例子：固定长度样本的周期控制组件
class ArmController : public RTT::TaskContext {
public:
    explicit ArmController(const std::string& name)
        : RTT::TaskContext(name),
          state_in_("state"), torque_out_("torque") {
        addPort(state_in_);
        addPort(torque_out_);
    }

    bool configureHook() override { return true; }

    bool startHook() override {
        return state_in_.connected();
    }

    void updateHook() override {
        JointState state;
        const RTT::FlowStatus flow = state_in_.read(state);
        if (flow == RTT::NewData)
            torque_out_.write(computeTorque(state));
    }

private:
    RTT::InputPort<JointState> state_in_;
    RTT::OutputPort<TorqueCommand> torque_out_;
};
~~~

TaskContext 提供生命周期 hook。基类负责何时允许调用它们，派生类填入设备和控制逻辑。派生组件并不拥有自己的调度线程：执行线程归 Activity 管，TaskContext 的 ExecutionEngine 通过 Runnable 接口接入 Activity。后文会追到 TaskCore::start() 与 ExecutionEngine::processHooks()，看状态如何阻止一次不合法的 updateHook。

RTT 的端口是 C++ 模板类型。InputPort<JointState> 在编译期固定样本类型，连接工厂据此拒绝不兼容的端口；这不意味着样本地址从发布端直接共享到订阅端。数据对象、FIFO 或 transport 在连接建立后才出现。

## 把对象关系压到最小

~~~text
TaskContext
  ├─ TaskCore 状态与 hooks
  ├─ ExecutionEngine 队列与 work 顺序
  ├─ Service/Operation 命令接口
  ├─ InputPort / OutputPort 类型化端点
  └─ ActivityInterface 绑定
          └─ Activity / PeriodicActivity / SequentialActivity 等执行实现
Port 连接建立后:
OutputPort -> ChannelElement 链 -> DATA 对象 / BUFFER -> InputPort
~~~

这是角色图，不表示“每个对象都互相拥有”。固定提交中 TaskCore 用成员原始指针 ee 保存 new ExecutionEngine(this)，并在基类析构中按 ee->getParent() == this 删除它，见 [TaskCore.cpp 构造与析构](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L53-L76) 与 [TaskCore.hpp 成员声明](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.hpp#L440-L458)。裸指针表示类承担明确释放责任；它不像 shared_ptr 那样用控制块记录共同拥有者。

TaskContext 用 ActivityInterface::shared_ptr our_act 持有当前 Activity；ActivityInterface 内部的 RunnableInterface* runner 是非拥有指针。因而“Activity 绑定了 TaskContext 的引擎”不表示 Activity 单独保证 TaskContext 活到线程结束：宿主仍须先停止执行者，再销毁被调用对象。见 [TaskContext.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.hpp#L680-L698)、[ActivityInterface.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ActivityInterface.hpp#L45-L75)。

## 状态机为什么必须先于线程

把“已配置”“正在运行”“出错”写成三个独立布尔量，会产生八种组合，其中很多没有意义。例如 running=true、configured=false 是否允许？configure 一次失败后，硬件句柄是否已经打开？用单一状态和经检查的转换，调用者才能知道下一步可做什么。

固定提交 [TaskCore.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.hpp#L95-L107) 定义 PreOperational、Stopped、Running、RunTimeError、Exception 与 FatalError 等状态。正常配置与启停主线如下：

~~~text
PreOperational --configure 成功--> Stopped --start 成功--> Running
      ^                                  |                    |
      |                                  +---- cleanup <--- stop
      +-------------------------------------------------------+
~~~

这只是正常路径；异常、运行期错误和致命错误有各自恢复条件，不应折叠成 bool ok。固定提交 TaskCore::start() 先设置 mTargetState = Running，调用虚函数 startHook()，成功后写 mTaskState = Running，并按 mTriggerOnStart 决定是否触发一次执行；返回 false 时把目标态恢复为 Stopped。关键源码摘录：

~~~cpp
// 固定提交源码摘录：TaskCore::start()，rtt/base/TaskCore.cpp
bool TaskCore::start() {
    if ( mTaskState == Stopped ) {
        TRY (
            mTargetState = Running;
            bool successful;
            { tracepoint_context(orocos_rtt, TaskContext_startHook, mName.c_str());
                successful = startHook(); }
            if (successful) {
                if (mTaskState != Running && (mTargetState == mTaskState)) {
                    exception();
                    return false;
                }
                else {
                    mTaskState = Running;
                    if ( mTriggerOnStart )
                        trigger();
                    return true;
                }
            }
            mTargetState = Stopped;
        ) CATCH_ALL (
            exception();
        )
    }
    return false;
}
~~~

摘录省略日志、std::exception 分支和宏定义，但保留状态写入、虚调用与触发顺序；完整实现见 [TaskCore::start](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L198-L230)。函数没有用 mutex 包住整个 hook，因此不能从“状态转换有检查”推断多个线程同时调用生命周期函数会自动串行。应用应由一个管理者串行发起 configure/start/stop/cleanup。

析构也是状态协议的一部分。固定提交的 TaskCore::~TaskCore() 明确不调用 cleanup()：析构期间派生类成员已经销毁，虚派发不能再安全进入派生类 cleanupHook()。若组件要通过 hook 关闭设备，部署层必须在销毁前显式 stop()、cleanup()；析构只释放框架对象。

## Activity、Runnable 与 ExecutionEngine 各管一层

Activity 是执行实现，可以继承 os::Thread，接收调度器、优先级、周期和 CPU affinity。ActivityInterface 是更窄的协议，管理 Runnable 绑定及 start/stop/trigger。RunnableInterface 定义 initialize()、step()、work(reason)、loop()、breakLoop()、finalize() 等入口。ExecutionEngine 实现 Runnable 的工作，不把某个具体控制组件类型写进线程循环。

RunnableInterface::setActivity() 把 owner_act 记为 ActivityInterface 原始指针；反向绑定由 [ActivityInterface::run()](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ActivityInterface.cpp#L45-L73) 更新。Activity 的 runner 也是借用关系。这个选择省掉循环引用的共享计数，却把正确性责任留给绑定者：正在执行 runner->work() 时，不能销毁 runner。

当 Activity 给 ExecutionEngine 一个执行机会，Engine 再检查 TaskCore 状态并调用业务 hook。Activity 管“何时执行”；Engine 管“本次机会按什么顺序处理框架工作”；TaskContext/派生组件管“业务动作做什么”。如果把三者合成一个 while 循环，线程策略和业务逻辑就会绑死，部署时无法将慢速诊断与控制周期隔开。

## Operation 是命令，Port 是连续样本

“清故障”是偶发命令，可以暴露为 Operation；持续变化的关节状态适合 Port。若把 1 kHz 状态当成每次远程函数调用，调用方与接收方必须匹配每个调用和返回；若把清故障做成 Port，双方还要自行编码请求、确认、超时和重复命令标识。

RTT 的 Operation 可选 ClientThread 或 OwnThread。ClientThread 表示方法在发起调用的线程执行：多个调用方可能与 updateHook 并发进入组件，方法内部仍需同步。OwnThread 把 invocation 投递给拥有者 ExecutionEngine：状态修改可在该引擎线程中串行执行，但调用方可能等待，Engine 每次更新会排空消息队列；RTT 没有按命令类型承诺固定服务时限。不能把“排到拥有者线程”说成“整个组件无竞争”，因为其他 ClientThread 接口仍可并发访问相同成员。

## Port 后面的存储决定旧数据怎样表现

OutputPort<T>::write() 进入连接链；本地默认 PerConnection PUSH 路径在 [ConnFactory::createConnection()](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L448-L525) 创建 ChannelElement，并依据 ConnPolicy 创建数据对象或 FIFO。OutputPort 是端点，不是 FIFO。

DATA 连接只保留一个当前样本，写入会替换旧值；若闭环只关心最近目标，固定空间可避免旧命令在 FIFO 中逐个执行，但也会跳过中间样本，所以状态应带采样序号或时间戳。BUFFER 保存有限 FIFO 项，超过消费者速度后，新写入会失败；circular policy 则按策略丢弃旧项。满载行为、ConnPolicy 的 push/pull 与锁类型是不同维度。LOCK_FREE 只描述存储同步实现，不保证用户类型赋值不分配、不阻塞。

读取返回的 FlowStatus 有 NoData、OldData、NewData，定义见 [FlowStatus.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/FlowStatus.hpp#L49-L59)。NewData 表示这次读到了新样本；OldData 表示连接已有值，但本次没有新写入；NoData 表示当前没有可读值。若控制器仅检查“读取函数是否成功”，传感器停更时可能把旧角度持续当成当前状态。更合适的控制代码要把 OldData 与样本时间戳一起用于安全门限。

## 从问题走到源码

专题按实际依赖顺序展开：先在 TaskContext 生命周期中确认合法状态与析构边界；再沿 Activity/ExecutionEngine 的真实 work 顺序走一周期；然后查看 Operation 参数怎样排队与完成；接着跟踪 OutputPort、ChannelElement 与 InputPort 的数据复制、缓存和 FlowStatus；最后把调度参数、队列积压、异常和 stop/join 放回完整控制链。

每个源码结论都固定到 Orocos RTT 提交 600102e8be9c81905b20930e32d43b28244ab173，仓库为 [orocos-toolchain/rtt](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。缩小版实现和设计推导会明确标注，不冒充该提交已有功能。

