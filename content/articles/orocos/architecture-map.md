# Orocos RTT 功能与组件地图：从机器人需求推回源码边界

一台机械臂里，驱动器需要初始化，控制器每毫秒取关节状态，诊断线程偶尔查询故障，日志器想保存历史测量。若把这些功能写进一个循环，任何磁盘或诊断延迟都会进入控制 deadline；若所有逻辑随手开线程，同一关节状态就会被多个执行者并发访问。RTT 的模块边界不是“类名目录表”，而是试图让组件生命周期、执行上下文、命令与数据流分别可配置。

源码事实固定到 [orocos-toolchain/rtt commit 600102e8be9c81905b20930e32d43b28244ab173](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。这个版本来自 RTT 2.8.99 配置，本文所有源码链接都指向该提交。

## 从一次控制故障推回要增加的对象

最朴素方案是：周期循环读输入、算力矩、写驱动，再处理服务命令与日志。一旦日志输出阻塞 8 ms，1 ms 控制环已错过多个释放点。把各块改成线程又会让 reset 命令与 updateHook 同时改控制器状态，C++ 对普通共享对象的未同步读写行为未定义。

因此最先要引入的不是“更快线程”，而是资源与状态边界：TaskCore 表示配置、运行和错误状态；TaskContext 组合端口与 Service；Activity 提供执行机会；ExecutionEngine 规定这一执行上下文先处理什么；Port 后的 storage 决定最新值与历史缓存的差别。每一层必须回答运行时问题：

| 机器人需求 | 对应组件 | 必须继续核对的结果 |
|---|---|---|
| 控制器按固定周期执行 | Activity、ExecutionEngine | OS 实际调度、hook 最坏时间、Engine 队列处理上界 |
| 配置失败后设备无残留 | TaskCore hooks | 局部 RAII 是否回滚 hook 副作用 |
| 控制器读取最新关节状态 | InputPort、DATA policy | New/Old/NoData 与时间戳年龄 |
| 保留每个安全事件 | 有界 BUFFER | 满载拒绝、队列年龄、事件序列 |
| 接收清故障命令 | Service/Operation | ClientThread 或 OwnThread、等待和超时 |
| 运行时装配组件 | Deployer、plugin、typekit | ABI、动态库与对象析构先后 |
| 跨进程交换 Port 数据 | transport ChannelElement | 序列化、复制点、协议与网络时延 |

这不是某一个类的属性清单。一个 TaskContext 可以换 Activity，也能以不同 ConnPolicy 连接相同类型 Port；部署配置因此属于行为的一部分。

## 固定提交的代码地图与构建产物

本仓库将源码分为多个目录，不代表每个目录产出单独动态库。顶层 CMake 固定 RTT 版本为 2.8.99，读取可选 orocos-rtt.cmake 或默认配置；rtt/CMakeLists.txt 收集根目录以及各子目录的源文件，再构建目标相关的 orocos-rtt-OROCOS_TARGET 共享库，可选再构建静态库。它同时生成 rtt-config.h、目标平台头、pkg-config 文件和 CMake package/export 文件。OS target 与构建选项决定实际编进二进制的 Activity、锁和 allocator 实现。源码入口见 [顶层 CMakeLists.txt](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/CMakeLists.txt#L22-L44)、[目标选项与子目录 source collection](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/CMakeLists.txt#L100-L180)、[共享库与静态库目标](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/CMakeLists.txt#L189-L243)。

| 目录 | 主要代码职责 | 为什么放在独立目录 | 对外阅读入口 |
|---|---|---|---|
| rtt 根目录 | TaskContext、Activity、ExecutionEngine、Port、Operation 和 ConnPolicy | 组件公共 API 与核心调用链便于从固定头文件进入 | rtt/TaskContext.hpp、Activity.hpp、ExecutionEngine.hpp、InputPort.hpp、OutputPort.hpp |
| rtt/base | TaskCore、Runnable、ChannelElement、buffer/data object 接口 | 作为低层抽象与可替换实现的共同契约 | rtt/base/TaskCore.hpp、RunnableInterface.hpp、ChannelElement.hpp |
| rtt/internal | ConnFactory、端点、队列、Operation invocation | 实现模板装配与运行期连接细节，降低公开 API 的实现负担 | rtt/internal/ConnFactory.hpp、LocalOperationCaller.hpp |
| rtt/os + os/<target> | 线程、互斥锁、条件变量、平台 ABI | 将内核/RTOS API 差异收敛到目标适配层 | rtt/os/Thread.cpp、Condition.hpp、os/gnulinux/fosi.h |
| rtt/types、typekit、plugin | TypeInfo、类型注册、插件加载 | 静态 C++ 类型与 Deployer 运行时装配之间需要一个转换层 | rtt/types/TypeInfo.hpp、typekit、plugin |
| transports/*、marsh | 数据外传与序列化 | 本地 ChannelElement 无法代替跨进程协议与类型编解码 | rtt/transports、rtt/marsh |
| scripting、deployment、extras | 状态机脚本、部署、可选 Activity/设备扩展 | 让基础组件内核不必把所有部署功能写进 TaskCore | rtt/deployment、rtt/extras |

CMake 子目录通过 GLOBAL_ADD_SRC/INCLUDE 收集代码，并把共同源码编进目标相关 RTT 库；这些是代码职责边界，不是强制的独立共享库边界。目录并不自动禁止交叉 include，模块依赖必须沿头文件与 CMake 选项核实。比如启用默认 Activity 会构建线程活动，启用 sequential 默认模式则改变 TaskContext 默认执行对象；锁自由实现和 transport 能力也依赖 build-time 目标选项。不能仅凭目录名断言某个二进制具备所有功能。

## 从组件构造到周期 hook 的运行关系

TaskContext 构造时建立 TaskCore/ExecutionEngine、Service、ServiceRequester 和默认 Activity；setup() 将 configure、start、stop、cleanup、trigger 注册为默认 ClientThread Operations，并启动默认 Activity。Activity 通过 RunnableInterface 接入 ExecutionEngine。派生组件只实现 hook，不负责创建调度线程。实据在 [TaskContext 构造和 setup](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L70-L117)。

~~~text
部署器创建 TaskContext
  -> TaskContext::setup 注册生命周期 Service/Operation
  -> Activity 获得线程或宿主执行上下文
  -> Activity 调 Runnable::work(reason)
  -> ExecutionEngine 处理工作并检查 TaskCore 状态
  -> 仅 Running 状态执行 updateHook
  -> InputPort 读取 storage，OutputPort 写连接链
~~~

运行时对象的主要线索为：Activity 通过 ActivityInterface 的 raw Runnable 指针调用 Engine；TaskContext 用 shared_ptr 持有默认 Activity；TaskCore 以 raw pointer 持有 ExecutionEngine 并在父类析构释放；Port 连接通过 ChannelElement shared/intrusive pointer 管理节点。引用计数延长的是相关对象寿命，不代表线程已经退出，也不保证借用的对象或动态库可先行销毁。所有权源码分布在 [TaskCore.cpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L53-L76)、[TaskContext.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.hpp#L680-L698) 和 [ActivityInterface.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ActivityInterface.hpp#L45-L75)。

### 构造链：先建立执行引擎，再选择 Activity

如果 TaskContext 在派生类尚未构造完毕时就让后台线程调用业务 hook，线程可能观察到未初始化的 Port 或设备成员。RTT 把构造步骤分开：`TaskCore` 基类首先创建 `ExecutionEngine`，`TaskContext` 再按编译配置选择 `Activity`，`setup()` 注册服务后才启动默认活动。先看本地固定提交的真实构造：

~~~cpp
    TaskContext::TaskContext(const std::string& name, TaskState initial_state /*= Stopped*/)
        :  TaskCore( initial_state, name )
           ,tcservice(new Service(name,this) ), tcrequests( new ServiceRequester(name,this) )
#if defined(ORO_ACT_DEFAULT_SEQUENTIAL)
           ,our_act( new SequentialActivity( this->engine() ) )
#elif defined(ORO_ACT_DEFAULT_ACTIVITY)
           ,our_act( new Activity( this->engine(), name ) )
#endif
    {
        this->setup();
    }
~~~

`TaskCore(initial_state, name)` 的初始化发生在派生对象成员初始化之前；构造函数体里的 `setup()` 则负责建立运行时操作入口。`our_act` 是 TaskContext 管理的 Activity 对象，内部借用 Engine 的 Runnable 接口；组件运行时状态仍受 TaskCore 控制。`our_act->start()` 让执行宿主可运行，**不是业务已经进入 Running 状态的保证**。业务 `updateHook()` 只有通过 Engine 的状态门控才会被调用。

自己实现时应先构造完整的消息队列与事件消费者，再开放 producer 或启动线程；将 Activity 线程启动与业务设备使能分成不同状态转换。否则构造期与停止期会出现对半初始化组件的并发访问。
### 一周期里执行的工作不是 updateHook 独占线程

ExecutionEngine::work(reason) 规定顺序：Trigger 时处理消息与端口 callbacks；TimeOut/IOReady 时再处理 function 队列和 TaskCore hooks。状态为 Running 才进 updateHook，RunTimeError 时进入 errorHook；事实见 [ExecutionEngine.cpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L330-L392)。

本提交在 Engine 构造时为 message、port、function 各创建容量 100 的 MWSR 队列。但 processMessages 与 processPortCallbacks 都 drain 到空，容量是空间界限，不是每周期批处理时间预算。如果一条 Port callback 耗时 0.5 ms，几十条积压足以错过 1 ms deadline。对严格截止期要隔离慢工作，或在自研复刻中加入每周期明确 batch；后者是改进方案，不应被写成此提交的事实。队列满返回 false，应用应把它变成拒绝计数和控制策略，而不能仅凭 Activity 收到 trigger 就宣称任务会执行。

### 直接对照 `ExecutionEngine::work` 的真正分派顺序

`Activity::trigger()` 提交的是一次执行请求；线程真正获得 CPU 以后，Engine 还会按触发原因决定是否进入业务 hook。下面是固定提交的完整工作分派：

~~~cpp
    void ExecutionEngine::work(RunnableInterface::WorkReason reason) {
        // Interprete work before calling into user code such that we are consistent at all times.
        if (taskc) {
            ++taskc->mCycleCounter;
            switch(reason) {
            case RunnableInterface::Trigger :
                ++taskc->mTriggerCounter;
                break;
            case RunnableInterface::TimeOut :
                ++taskc->mTimeOutCounter;
                break;
            case RunnableInterface::IOReady :
                ++taskc->mIOCounter;
                break;
            default:
                break;
            }
        }
        if (reason == RunnableInterface::Trigger) {
            /* Callback step */
            processMessages();
            processPortCallbacks();
        } else if (reason == RunnableInterface::TimeOut || reason == RunnableInterface::IOReady) {
            /* Update step */
            processMessages();
            processPortCallbacks();
            processFunctions();
            processHooks();
        }
    }
~~~

纯 `Trigger` 只排空命令和端口回调；`TimeOut` 或 `IOReady` 在它们之后还会执行 function 队列和生命周期 hook。这意味着一次到达的端口事件不必然对应一次 `updateHook()`，OwnThread 命令也能先于同轮控制计算修改状态。

假设本轮累积 10 条各耗时 0.6 ms 的命令：即使控制周期配置为 1 ms，`processMessages()` 的完整排空也可能先花掉 6 ms。固定提交的三个 Engine 队列各有容量 100，但这是待办空间上限，不是每周期 CPU 时间预算。`getActivity()->trigger()` 是让 Activity 尝试执行；`msg_cond.broadcast()` 则通知等待 Operation 完成的线程，二者分别服务于不同的等待者，不能被画成同一条业务执行线程。
## 一条样本的空间路径与时间路径

样本的空间路径是 OutputPort 到 ConnFactory 创建的 ChannelElement 链，再到 InputPort 的读取端。本地 PerConnection PUSH 可在连接链上放 ChannelDataElement 或 ChannelBufferElement；DATA 是单值最新状态，BUFFER 是有限 FIFO，CIRCULAR_BUFFER 满时丢旧项。由 [ConnFactory::buildDataStorage](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L150-L205) 与 [ChannelBufferElement::read/write](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ChannelBufferElement.hpp#L95-L134) 可追到存储实现。

~~~text
空间：调用者样本
  -> OutputPort::write(sample)
  -> DATA 槽位 / BUFFER 队列
  -> InputPort::read(sample)
  -> 控制器局部样本副本

时间：写入返回
  -> 通知 InputPort/Engine 队列
  -> Activity 通知和操作系统调度
  -> Engine callback/hook 开始
  -> 控制运算
~~~

写入成功只说明 mandatory connection 的缓冲接受了样本，未必接收端已读取或设备已执行。线程通知晚于样本进入缓存；callback 只有 Engine 线程被 OS 调度后才开始。高负载时，样本可能已在 DATA 中被新值覆盖，Engine 仍尚未处理对应端口事件；空间状态与执行状态因此可能不是一一对应。

三条正交轴可以帮助定位配置：生命周期轴决定能否配置/运行/清理；执行轴决定在哪条线程及何时运行；数据轴决定保留多少样本、覆盖谁、谁主动发送。同一 TaskContext 可以跑 1 kHz 周期，也可以以事件方式触发；相同 InputPort 类型可以连 DATA 或 BUFFER。读一个类名无法判断组件 deadline、线程数或排队行为。

### 读到“最新值”仍然可能花费与积压长度成正比的时间

机器人控制通常希望使用最新姿态而非顺序执行过期样本。RTT 的 `InputPort<T>::readNewest()` 不是直接读取最新槽位，而是持续读取新样本直到队列不再返回 `NewData`。下面是固定源码：

~~~cpp
        FlowStatus readNewest(typename base::ChannelElement<T>::reference_t sample, bool copy_old_data = true)
        {
            FlowStatus result = read(sample, copy_old_data);
            if (result != RTT::NewData)
                return result;

            while (read(sample, false) == RTT::NewData);
            return RTT::NewData;
        }
~~~

若 BUFFER 中积压了 `k` 条样本，函数会进行数量级为 `O(k)` 的读取与样本赋值；若 `T` 含动态分配的字段，这些复制还可能产生 allocator 抖动。`readNewest()` 解决数据新鲜度的一部分问题，却没有常数时间保证。对必须证明周期上界的控制器，应依据场景选用 DATA 最新值存储，或者在应用层自行给 drain 设置工作量上限；后者是推荐改进，不是这个 RTT 提交已经实现的额外参数。
## 从真实实现推回设计取舍

TaskCore 固定生命周期骨架，使所有组件共享状态检查，但 hook 能包含分配、阻塞和设备错误；Activity 可替换，部署时线程策略成为系统配置；OwnThread Operation 把命令放入 Engine 串行处理，代价是排队与同步等待；ChannelElement 链使 storage/transports 可组合，代价是复制与关闭节点分散；TypeKit/plugin 使运行时按名字创建不同类型和组件，代价是 ABI 与动态库寿命进入对象生命周期。它们隔离不同变化点，也引入新故障边界。

硬实时控制建议将周期控制与日志、诊断拆到不同 Activity 或进程，给 Port 指定数据新鲜度策略，为每个队列明确 capacity 与超载动作，并测量样本年龄、队列高水位、callback WCET、release jitter 和 stop 最坏时间。Orocos RTT 提供这些设计所需的入口，但系统是否可行必须在具体 OS、平台构建和负载上验证。

## 可复刻顺序

先实现 TaskCore 状态转换和局部 RAII 回滚；再写单 Activity 的绝对周期与 stop/join；然后加入有界 Engine 消息队列、明确 batch 与错误状态；接下来实现 DATA 和 FIFO 两种 ChannelElement、NewData/OldData/NoData；之后实现 OwnThread invocation、参数复制和关闭结果；最后再加入运行时类型、plugin、部署文件及 remote transport。

每一步都用机器人反例验证：配置一半失败是否关设备；消息比控制器处理快时是旧数据还是丢数据；慢 Operation 是否饿死 updateHook；引用参数先析构会怎样；stop 失败时是否仍保留 Activity 和动态库。完成这些运行回放，架构图才从目录图变成可复刻的系统图。

