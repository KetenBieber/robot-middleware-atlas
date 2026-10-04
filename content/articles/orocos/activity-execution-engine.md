# Activity 与 ExecutionEngine：一个 1 ms 周期怎样走到 updateHook

机械臂的力矩控制每 1 ms 执行一次。传感器线程偶尔写入新状态，操作员线程则可能同时请求清故障。需要区分四件事：队列接收了请求、Activity 收到执行通知、操作系统把线程变为 runnable、线程实际拿到 CPU 后进入 Engine。它们发生在不同时间，不能都称为“唤醒”。

如果直接写一个 sleep 循环，工作时间加到 sleep 之后，周期会漂；如果每次收到输入就调用控制函数，突发输入会让控制以高于设计值的频率执行。RTT 把“执行时机”封装为 Activity，把“本次机会处理哪些工作”放在 ExecutionEngine。本文所说固定源码均来自 `orocos-toolchain/rtt` commit `600102e8be9c81905b20930e32d43b28244ab173`。

## Activity 与 Runnable 把线程和业务分开

Activity 继承 `base::ActivityInterface` 和 `os::Thread`：前者描述“何时开始、何时停止、周期是多少”，后者承接操作系统线程；被执行的业务则通过 `RunnableInterface` 提供。这个接口拆分有一个直接后果：Activity 保存一个 `RunnableInterface*`，它只是借用指针，不负责 delete 对象。拥有 Runnable 的 TaskContext 或外部管理者必须保证它活到 Activity 停止并且底层线程结束，否则线程中的虚函数调用会变成悬空访问。

固定提交中的 `ActivityInterface` 构造和析构能看见这个所有权边界：

~~~cpp
// 固定提交源码摘录：ActivityInterface 构造与析构
ActivityInterface::ActivityInterface(RunnableInterface* run) : runner(run) {
    if (runner)
        runner->setActivity(this);
}

ActivityInterface::~ActivityInterface()
{
    if (runner) {
        runner->setActivity(0);
    }
}
~~~

`runner` 是普通裸指针，没有 `delete`、`shared_ptr` 或引用计数；析构只解除反向 Activity 指针。它不会延长 Runnable 生命周期，所以控制器拥有者需要在 Activity 的线程彻底退出前保留对象。

构造周期 Activity 时，RTT 把 scheduler、priority 和 period 交给 Thread，同时又清掉 Thread 自己的周期设置：

~~~cpp
// 固定提交源码摘录：Activity::Activity(scheduler, priority, period, ...)
Activity::Activity(int scheduler, int priority, Seconds period,
                   RunnableInterface* r, const std::string& name)
    : ActivityInterface(r), os::Thread(scheduler, priority, period, 0, name),
      update_period(period), mtimeout(false), mstopRequested(false),
      mwaitpolicy(ORO_WAIT_ABS)
{
    Thread::setPeriod(0, 0);
}
~~~

`ActivityInterface(r)` 只把 `r` 存作 runner 并回设 Activity 指针；构造器没有接管其所有权。`Thread::setPeriod(0, 0)` 也不是取消 Activity 周期：周期仍保存在 `update_period`，以后由 Activity 自己用绝对时刻等待。这样能避免底层线程周期机制与 Activity 的超时工作路径同时驱动同一个 Runnable。调度器和优先级只是线程配置，是否成功以及实际调度策略仍需查看 OS 适配结果。先看一个会重入的朴素写法：

~~~cpp
// 错误示例：任意传感器事件直接并发调用控制函数
void onSensorMessage(const JointState& s) {
    controller.update(s); // 可能与周期线程同时修改控制器状态
}
~~~

两路传感器各以 2 kHz 写入时，回调可能以 4 kHz 进入；如果周期线程也调用 update，就会并发重入。RTT 的端口事件路径把 PortInterface 加入 Engine 队列并触发 Activity，而不是让消息线程直接调用 updateHook。Activity 提供执行上下文，Engine 再按 work reason 选择处理内容。

这个线程边界也解释了 Activity 与线程池的区别：一个普通 Activity 对应一个 OS 线程；线程池通常让多个工作项共享一组线程。进程是拥有地址空间、打开文件等资源的容器；同一进程里的线程共享这些资源，但每条线程有自己的寄存器现场、栈和调度状态。RTT 的多个 Activity 因而可以并行访问同一 TaskContext 与组件成员，线程分开并不会自动保护共享状态。RTT 还提供 SequentialActivity 和 SlaveActivity 等执行方式，它们由宿主或 master 驱动，并不因此自动获得独立 CPU 时间。若多个控制器排在同一串行 master 上，前一项用掉 4 ms 就会把后一项至少推迟 4 ms。Linux 调度器选择 runnable 线程执行；高优先级会影响竞争次序，却不会缩短回调本身耗时，更不等于截止期保证。

## 通知、可运行和开始执行是不同事件

OwnThread Operation 从调用线程走到组件时序大致如下。调用线程先把 invocation 放进 Engine 的命令队列；若组件已 active，`Activity::trigger()` 再广播条件变量并调用 `Thread::start()`：

~~~text
调用线程：构造 invocation -> ExecutionEngine::process() 入队
           -> Activity::trigger() 通知等待者并启动/唤起线程
Activity：从等待返回 -> 线程变为 runnable -> OS 分配 CPU
          -> Activity::loop() -> Activity::work(reason)
          -> ExecutionEngine::work(reason) -> 队列与 hook
~~~

~~~cpp
// 固定提交源码摘录：Activity::trigger()
bool Activity::trigger() {
    tracepoint(orocos_rtt, Activity_trigger, getName());
    if (!Thread::isActive())
        return false;
    msg_cond.broadcast();
    Thread::start();
    return true;
}
~~~

这两个调用覆盖不同状态：`broadcast()` 让正在 `msg_cond` 上等待的线程有机会继续；`Thread::start()` 在非周期线程已经回到内层命令等待时，通过内部 semaphore 请求它继续。它们都不意味着 CPU 已经分配给线程。消息入队失败时也不会由通知补偿；而 Activity 未 active 时，`trigger()` 直接返回 false。

非周期 Thread 在已经 active 时的 `start()` 会向内部 semaphore 发送一次信号：

~~~cpp
// 固定提交源码摘录：Thread::start() 的非周期 active 分支
if (period == 0) {
    if (isActive()) {
#ifndef OROPKG_OS_MACOSX
        if (rtos_sem_value(&sem) > 0)
            return true;
#endif
        rtos_sem_signal(&sem);
        return true;
    }
    active = true;
    if (this->initialize() == false || active == false) {
        active = false;
        return false;
    }
    running = true;
    rtos_sem_signal(&sem);
    return true;
}
~~~

当线程已睡在这个 semaphore 上时，信号令其结束内核等待，变为 runnable；当线程仍在 loop 内部等条件变量时，`msg_cond` 的 broadcast 才是该 wait 的通知。RTT 因而有两种不同的阻塞点；究竟是哪一层在睡，要看 Thread 当前处于配置等待还是 Activity 周期 loop。

非周期 Activity 还有一个需要读者注意的跨线程边界。公开的 `TaskCore::trigger()` 会转调 `Activity::timeout()`：

~~~cpp
// 固定提交源码摘录：TaskCore::trigger()
bool TaskCore::trigger()
{
    return this->engine()->getActivity() &&
           this->engine()->getActivity()->timeout();
}
~~~

`Activity::timeout()` 写入 `mtimeout`，随后广播并启动 Thread：

~~~cpp
// 固定提交源码摘录：Activity::timeout()
bool Activity::timeout()
{
    if (update_period > 0)
        return false;
    mtimeout = true;
    msg_cond.broadcast();
    Thread::start();
    return true;
}
~~~

执行线程在 `Activity::loop()` 顶部读取并清除同一字段：

~~~cpp
// 固定提交源码摘录：Activity::loop() 的 timeout 分支
if (mtimeout) {
    mtimeout = false;
    this->step();
    this->work(base::RunnableInterface::TimeOut);
}
~~~

字段声明本身就是普通 bool：

~~~cpp
// 固定提交源码摘录：Activity 的事件与关闭状态字段
bool mtimeout;
bool mstopRequested;
int mwaitpolicy;
~~~

在这个固定提交里，`mtimeout` 的读写没有共同 mutex，也没有 atomic 操作。非周期 Activity 的 `timeout()` 可以由别的线程写入 `mtimeout=true`，Activity 线程则在 `loop()` 中读取并清零；条件变量通知本身不能代替对这个普通 `bool` 的同步。在 C++ 并发模型中，这属于应当修正的未同步访问。它还是一个 pending 位而非计数器：两次 `timeout()` 在消费前都只留下 `true`，不会记住“两次必须执行两轮”。缩小版若要求无丢失的事件次数，可在队列中保存事件；若只需合并成“至少有一次工作”，则用 mutex+谓词或 atomic exchange 明确这个语义，并让承载数据的队列单独建立发布/消费同步。

runnable 只是内核可选择该线程的状态，不表示线程已经执行。线程在条件变量上等待时，pthread 接口会进入内核阻塞路径；调度器保存当前线程的寄存器和栈指针等执行现场，再让另一条 runnable 线程占用 CPU，这种交接称为上下文切换。通知到来只会让等待线程有资格重新竞争；它可能仍要等更高优先级工作先运行。RTT Linux 适配通过 pthread_cond_wait / pthread_cond_timedwait / pthread_cond_broadcast 实现 os::Condition；pthread 可能借助 futex，但 RTT 没有直接调用 futex，实际系统调用路径由 libc 与内核决定。

条件变量不是消息队列，也不保存通知。正确使用方式是把“我要等的状态”放在 mutex 保护下循环检查；wait 在睡眠前释放 mutex，收到通知后重新取得 mutex，再检查谓词，虚假唤醒也只会多跑一轮。Engine 给第三方调用者的等待路径先看一次谓词，再持有 `msg_lock` 复查，并在仍不满足时进入 wait：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::waitForMessagesInternal()
void ExecutionEngine::waitForMessagesInternal(
    boost::function<bool(void)> const& pred)
{
    if (pred())
        return;
    os::MutexLock lock(msg_lock);
    while (!pred()) {
        msg_cond.wait(msg_lock);
    }
}
~~~

这里 `boost::function<bool(void)> const&` 是对调用对象的 const 引用：Engine 不复制也不拥有它，调用在等待函数返回前同步使用它，因此传入 lambda 的捕获对象必须活到等待结束。`os::MutexLock` 是 RAII 锁，构造时获取 `msg_lock`，离开作用域时自动释放；`Condition::wait()` 临时释放 mutex 让 Engine 线程处理命令，再在返回前重新获得它。第三方等待者之所以不会漏掉“命令执行完成”的信号，还依赖消费端在完成命令后先取得同一把锁、再广播。若等待发生在 Engine 自己的线程上，它走另一条边处理消息并等待：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::waitAndProcessMessages()
void ExecutionEngine::waitAndProcessMessages(
    boost::function<bool(void)> const& pred)
{
    if (pred())
        return;

    while (true) {
        this->processMessages();
        {
            os::MutexLock lock(msg_lock);
            if (!pred()) {
                msg_cond.wait(msg_lock);
            } else {
                return;
            }
        }
    }
}
~~~

Engine 自己等待时，先排空队列，再锁住 `msg_lock` 复查谓词。固定源码中，外部 `process(DisposableInterface*)` 在入队后会广播，但不在修改队列状态时持有这把锁；它还会触发 Activity。若另一个线程恰好在 Engine 已排空、已检查谓词但尚未真正进入 wait 的窗口提交并广播，条件变量本身不会保存该通知，二者存在可疑的丢通知时序。这个跨线程等待路径应结合 `process()` 的广播位置和实际调用约束单独审计，不能把“使用了条件变量”当作没有丢通知的证明。缩小版应让谓词状态与 wait 使用同一把锁，或使用有记忆的计数信号/代际计数来闭合这个窗口。Linux generic backend 把 RTT 的 `Condition` 落到 pthread 条件变量，并以 `CLOCK_MONOTONIC` 建立超时基准：

~~~cpp
// 固定提交源码摘录：Linux 条件变量适配
typedef pthread_cond_t rt_cond_t;

static inline int rtos_cond_init(rt_cond_t *cond)
{
    pthread_condattr_t attr;
    int ret = pthread_condattr_init(&attr);
    if (ret != 0) return ret;
    ret = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    if (ret != 0) {
        pthread_condattr_destroy(&attr);
        return ret;
    }
    ret = pthread_cond_init(cond, &attr);
    pthread_condattr_destroy(&attr);
    return ret;
}

static inline int rtos_cond_destroy(rt_cond_t *cond)
{
    return pthread_cond_destroy(cond);
}

static inline int rtos_cond_wait(rt_cond_t *cond, rt_mutex_t *mutex)
{
    return pthread_cond_wait(cond, mutex);
}

static inline int rtos_cond_timedwait(
    rt_cond_t *cond, rt_mutex_t *mutex, NANO_TIME abs_time)
{
    TIME_SPEC arg_time = ticks2timespec(abs_time);
    return pthread_cond_timedwait(cond, mutex, &arg_time);
}

static inline int rtos_cond_broadcast(rt_cond_t *cond)
{
    return pthread_cond_broadcast(cond);
}
~~~

pthread 条件变量通常在竞争睡眠时由 libc 与内核借助 futex 一类等待机制完成阻塞和通知；RTT 代码只调用 pthread 接口。被通知的线程先从 blocked 变为 runnable，再与其他可运行线程竞争 CPU，之后才会回到 C++ 调用点。条件变量的通知、状态谓词成立和业务代码开始执行是三个独立事件。

## 固定版本的周期循环

Activity 线程在启动后进入 `Activity::loop()`。下面保留这个函数从一次业务工作到下一次等待、再到 overrun 处置的连续控制流：

~~~cpp
// 固定提交源码摘录：Activity::loop()
void Activity::loop() {
    nsecs wakeup = 0;
    int overruns = 0;
    while ( true ) {
        // since update_period may be changed at any time, we need to recheck it each time:
        if ( update_period > 0.0) {
            if ( wakeup == 0 ) {
                wakeup = os::TimeService::Instance()->getNSecs() + Seconds_to_nsecs(update_period);
            }
        } else {
            wakeup = 0;
        }

        if (mtimeout) {
            mtimeout = false;
            this->step();
            this->work(base::RunnableInterface::TimeOut);
        } else {
            if ( update_period > 0 ) {
                this->step();
                this->work(base::RunnableInterface::Trigger);
            } else {
                if (runner) {
                    runner->loop();
                    runner->work(base::RunnableInterface::Trigger);
                } else {
                    this->step();
                    this->work(base::RunnableInterface::Trigger);
                }
            }
        }

        os::MutexLock lock(msg_lock);
        if ( wakeup == 0 ) {
            return;
        } else {
            bool time_elapsed = ! msg_cond.wait_until(msg_lock,wakeup);
            if (time_elapsed) {
                nsecs now = os::TimeService::Instance()->getNSecs();
                nsecs nsperiod = Seconds_to_nsecs(update_period);
                wakeup = wakeup + nsperiod;
                if ( wakeup < now )
                {
                    ++overruns;
                    if (overruns == maxOverRun)
                        break;
                }
                else if (overruns != 0) {
                    --overruns;
                }
                if ( mwaitpolicy == ORO_WAIT_REL ) {
                    wakeup = now + nsperiod;
                }
                mtimeout = true;
            }
        }
        if (mstopRequested) {
            mstopRequested = false;
            return;
        }
    }
    if (overruns == maxOverRun)
    {
        this->emergencyStop();
        log(Critical) << rtos_task_get_name(this->getTask())
                << " got too many periodic overruns in step() ("
                << overruns << " times), stopped Thread !"
                << endlog();
        log(Critical) << " See Thread::setMaxOverrun() for info."
                << endlog();
    }
}
~~~

第一次进入周期分支时，`wakeup` 取当前单调时间加 period；下一次不是“本轮结束后 sleep(period)”，而是 `wakeup += period`，所以计算时间不会逐轮累加到周期相位里。超时后 Activity 将 `mtimeout` 设为 true，下一轮以 `TimeOut` 原因进入 `work()`。`ORO_WAIT_REL` 则把下一释放时刻改为“现在加周期”，这是明确选择用相位重置换取恢复时间。若下一释放时刻已经落后于 `now`，overrun 计数上升；到阈值触发 `emergencyStop()`。绝对时刻只能减少漂移，不能使 8 ms 的工作在 1 ms 截止期内完成。

Linux 上 pthread_cond_timedwait 阻塞后线程成为 blocked，内核可运行其他线程。定时或通知到来后，线程先变 runnable，竞争并重新取得 mutex，再从 wait 返回，最后由 OS 分配 CPU。消息写入、通知发送、线程 runnable、线程 running 和业务 hook 开始是不同的时间点。

## Engine 如何安排一次执行机会

Activity 的 `step()` 会委托给绑定的 Runnable；TaskContext 的 Runnable 是 ExecutionEngine。该提交中 Engine 的 `step()` 留空，原因是一次执行分成 Trigger 与周期 TimeOut 两类，所以分派发生在 `work(reason)`。调用输入是 Activity 给出的原因枚举，源码按原因更新计数并选择需要排空的队列：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::work()
void ExecutionEngine::work(RunnableInterface::WorkReason reason) {
    if (taskc) {
        ++taskc->mCycleCounter;
        switch(reason) {
        case RunnableInterface::Trigger: ++taskc->mTriggerCounter; break;
        case RunnableInterface::TimeOut: ++taskc->mTimeOutCounter; break;
        case RunnableInterface::IOReady: ++taskc->mIOCounter; break;
        default: break;
        }
    }
    if (reason == RunnableInterface::Trigger) {
        processMessages();
        processPortCallbacks();
    } else if (reason == RunnableInterface::TimeOut ||
               reason == RunnableInterface::IOReady) {
        processMessages();
        processPortCallbacks();
        processFunctions();
        processHooks();
    }
}
~~~

Trigger 只排空命令和端口事件，周期超时或 I/O ready 还运行可执行函数与生命周期 hook。这一点会影响实时预算：若一个非周期事件到来，它不一定运行一次 updateHook。`processHooks()` 只有当前状态和目标状态都为 Running 才调用 `updateHook()`；RunTimeError 则走 `errorHook()`。因此 Engine 不是“线程的另一个名字”，而是处于 Activity 所拥有线程中的业务次序与状态门控。消息会先于 hook 执行，OwnThread 命令可能在本轮改好设定值，再由同一轮 hook 使用。

固定版本为 message、port callback 和 function 各建立容量 100 的 MWSR 队列：多线程可以提交，Engine 所在线程负责消费。`MWSR` 的意思是 multi-writer, single-reader：多个调用线程可生产，Engine 线程单独消费。队列对象本身由 Engine 构造时 `new` 出来并由析构函数删除；其中存放的指针则各有不同生命周期。先看队列的建立：

`MWSRQueue<T>` 的底层实现由构建开关选出：常规构建继承原子索引队列，关闭汇编原子支持时继承带 mutex 的队列。

~~~cpp
// 固定提交源码摘录：MWSRQueue<T> 的编译期实现选择
template<class T>
class MWSRQueue
#if defined(OROBLD_OS_NO_ASM)
    : public LockedQueue<T>
#else
    : public AtomicMWSRQueue<T>
#endif
{
public:
    MWSRQueue(int qsize)
#if defined(OROBLD_OS_NO_ASM)
        : LockedQueue<T>(qsize)
#else
        : AtomicMWSRQueue<T>(qsize)
#endif
    {}
};
~~~

这是编译期选择，不是运行中自动切换。原子版本减少了互斥锁进入内核阻塞的机会，却仍要为比较交换和缓存一致性付出成本；锁队列更易于理解，却可能让高优先级 consumer 等待低优先级 producer。不能仅凭“Atomic”就推断整条 callback 路径 wait-free。

~~~cpp
// 固定提交源码摘录：ExecutionEngine 队列构造
#define ORONUM_EE_MQUEUE_SIZE 100

ExecutionEngine::ExecutionEngine(TaskCore* owner)
    : taskc(owner),
      mqueue(new MWSRQueue<DisposableInterface*>(ORONUM_EE_MQUEUE_SIZE)),
      port_queue(new MWSRQueue<PortInterface*>(ORONUM_EE_MQUEUE_SIZE)),
      f_queue(new MWSRQueue<ExecutableInterface*>(ORONUM_EE_MQUEUE_SIZE))
{}

~~~

一个 OwnThread invocation 到达 Engine 时调用 `process(DisposableInterface*)`。队列是否接收成功由 enqueue 的布尔返回值决定：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::process(DisposableInterface*)
bool ExecutionEngine::process(DisposableInterface* c)
{
    if (taskc && taskc->mTaskState == TaskCore::FatalError)
        return false;
    if (c && this->getActivity()) {
        bool result = mqueue->enqueue(c);
        this->getActivity()->trigger();
        msg_cond.broadcast();
        return result;
    }
    return false;
}
~~~

调用者把 invocation 的裸指针交给 `process()`。若 enqueue 成功，Engine 消费线程之后调用 `executeAndDispose()`，由 invocation 完成自身释放；若 enqueue 失败或当前状态不接受工作，这个函数不执行也不 dispose，调用者仍需处理对象。`trigger()` 与 `msg_cond.broadcast()` 又是两种通知：前者让 Activity 的执行线程继续，后者用于叫醒正在等待 invocation 谓词成立的第三方线程。

Engine 消费时不持有 `msg_lock` 执行每条命令；全部排空后短暂取得该锁再广播，是为了让等待谓词线程不会错过完成通知。下面两段分别展示命令消费和端口消费：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::processMessages()
void ExecutionEngine::processMessages()
{
    if (mqueue->isEmpty())
        return;
    DisposableInterface* com(0);
    {
        while (mqueue->dequeue(com)) {
            assert(com);
            com->executeAndDispose();
        }
        MutexLock locker(msg_lock);
    }
    if (com)
        msg_cond.broadcast();
}
~~~

`executeAndDispose()` 把命令执行和命令对象生命周期合在一个调用里。`msg_lock` 没有包住用户命令，因此慢命令不会阻塞 Engine 的完成通知锁；但命令仍在 Engine 线程串行执行，会直接挤占周期时间。

~~~cpp
// 固定提交源码摘录：ExecutionEngine::processPortCallbacks()
void ExecutionEngine::processPortCallbacks()
{
    if (port_queue->isEmpty())
        return;
    TaskContext* tc = dynamic_cast<TaskContext*>(taskc);
    if (tc) {
        PortInterface* port(0);
        while (port_queue->dequeue(port)) {
            assert(port);
            tc->dataOnPortCallback(port);
        }
    }
}
~~~

端口队列只保存端口身份，不拥有并删除端口；TaskContext 的端口注册关系必须持续到这条 callback 被消费。两条队列都一直 dequeue 到空，因此容量 100 只是最多能存多少待办，不限制一次执行最多处理多少；如果有 100 个各耗时 0.5 ms 的回调，单是 drain 就可能用掉约 50 ms，而且并发 producer 在 drain 期间继续入队时还会延长。队列满时提交方得到 `false`，Activity 的通知不能恢复没有入队的命令。

Engine 析构时并不执行剩余 message：它将剩余 function 标成 unloaded，对尚未执行的 Disposable 则调用 dispose，再删掉三个队列容器：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::~ExecutionEngine() 的待办清理
Logger::In in("~ExecutionEngine");

ExecutableInterface* foo;
while (f_queue->dequeue(foo))
    foo->unloaded();

DisposableInterface* dis;
while (mqueue->dequeue(dis))
    dis->dispose();

delete f_queue;
delete port_queue;
delete mqueue;
~~~

析构不会替外部释放端口对象，也不会替组件执行尚未处理的业务命令。这正是关闭时必须先停生产者、清楚处理队列剩余工作的原因。

function 队列略有不同：Engine 在一轮开始读取 `f_queue->size()`，每次成功 dequeue 后减一；尚未完成的 function 再放回队列。只要开轮计数大于零，这个计数就限制本轮执行次数。但源代码把 `nbr` 限制放在成功 dequeue 之后：若开轮时 `nbr == 0`，恰有生产者在 size 快照后、第一次 dequeue 前提交新项，第一次成功 dequeue 后 `nbr` 会变成负数，`nbr == 0` 不再成立。若该 `ExecutableInterface::execute()` 返回 true，它会被重新放回队尾；计数继续减小，甚至当 producer 已停止，这条函数仍可在本轮反复执行，直到某次 `execute()` 返回 false。周期 Activity 可能因此迟迟走不到 `processHooks()`，关节命令保留在旧值。这个边缘时序说明 `size()` 只是瞬时快照，没有和后续 dequeue 构成原子批次边界。复刻时应在 dequeue 前检查预算，例如 `while (remaining > 0 && f_queue->dequeue(foo))`，并给每项执行成本设界。

## Port 写入之后，回调还要经过几层

一次本地 Port 写入可以先把样本交给连接中的 DATA 或 buffer，再把端口身份入 callback 队列并触发 Activity。下面是提交入口；调用者给的是一个非拥有的端口指针，触发后 Engine 线程稍后再分派 callback：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::process(PortInterface*)
bool ExecutionEngine::process(PortInterface* port)
{
    if (taskc && taskc->mTaskState == TaskCore::FatalError)
        return false;
    if (port && this->getActivity()) {
        bool result = port_queue->enqueue(port);
        this->getActivity()->trigger();
        return result;
    }
    return false;
}
~~~

注意这个固定提交即使 `enqueue` 返回 `false`，也仍会调用 `trigger()`；触发只令 Activity 有机会查看现有队列，不会补回丢失的端口事件。样本可能已在 ChannelElement 中，端口通知却没有入队，因而“数据写入成功”和“用户 callback 会执行”必须分开计数。消费端在前一段摘录中，最终由 `TaskContext::dataOnPortCallback(port)` 执行业务通知。

| 阶段 | 状态改变 | 还没有发生的事 |
|---|---|---|
| ChannelElement 写入成功 | 样本进入 DATA/FIFO | callback 尚未开始 |
| Engine enqueue 成功 | callback 等待消费 | 线程未必 runnable |
| Activity trigger | 等待线程收到通知 | 线程未必获 CPU |
| OS 调度线程 runnable/running | 线程可执行/正在执行 | 用户 updateHook 尚未开始 |
| Engine 调用 callback/hook | 用户代码开始 | 设备输出尚未确认 |

enqueue 失败时，触发信号不能恢复丢失的队列项；TaskContext 进入 FatalError 后 Engine 也拒绝新工作。监控时应分开记录写入状态、队列失败、触发时刻、callback 开始和控制输出时间。

## Stop 请求、同步点与线程析构

若非周期 Runnable::loop() 阻塞在用户代码中，广播条件变量不会取消任意 I/O。Activity::stop() 设置 stopRequested 并通知等待者；若自定义 loop 正在运行，还要求 breakLoop() 请求它返回；随后用超时锁等待当前工作离开同步点。失败会返回 false。

~~~cpp
// 固定提交源码摘录：Activity::stop()
bool Activity::stop()
{
    if (!active)
        return false;

    running = false;
    {
        os::MutexLock lock(msg_lock);
        mstopRequested = true;
        msg_cond.broadcast();
    }

    if (update_period == 0) {
        if (inloop) {
            if (!this->breakLoop()) {
                log(Warning) << "Failed to stop thread " << this->getName()
                             << ": breakLoop() returned false." << endlog();
                running = true;
                return false;
            }
            MutexTimedLock lock(breaker, getStopTimeout());
            if (!lock.isSuccessful()) {
                log(Error) << "Failed to stop thread " << this->getName()
                            << ": breakLoop() returned true, but loop() function did not return after "
                            << getStopTimeout() << " second(s)." << endlog();
                running = true;
                return false;
            }
        }
    } else {
        MutexTimedLock lock(breaker, getStopTimeout());
        if (lock.isSuccessful()) {
            rtos_task_make_periodic(&rtos_task, 0);
        } else {
            log(Error) << "Failed to stop thread " << this->getName()
                       << ": step() function did not return after "
                       << getStopTimeout() << " second(s)." << endlog();
            running = true;
            return false;
        }
    }

    this->finalize();
    active = false;
    return true;
}
~~~

`stop()` 对周期 Activity 等当前 step 返回，对非周期 Activity 则先调用 `breakLoop()`，再用有时限的 `breaker` 等待 loop 退出。它成功后运行 `finalize()` 并将 Activity 标成 inactive；失败则恢复 `running` 并返回 false。Activity 析构随后还会调用 Thread 的 `terminate()`，而该实现中的 OS 适配删除操作必须 join 底层线程，因为 `stop()` 本身不保证线程已结束。TaskCore 的 `stop()` 另有一层：Engine 先 stop 再 start Activity，利用 stop 返回时 step 已退出建立同步点，随后才调用 `stopHook()`。所以组件停止和 OS 线程销毁是两种不同操作。关闭管理者必须检查 stop 结果，在失败时保留 Activity、Runnable、TaskContext 与动态库代码，不能让在途线程访问已经析构的对象。

把 TaskContext 状态机和 Engine 的同步点并排看，才能知道 `stopHook()` 为什么在这一步之后执行：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::stopTask()
bool ExecutionEngine::stopTask(TaskCore* task)
{
    if (this->getActivity() && this->getActivity()->stop()) {
        this->getActivity()->start();
        return true;
    }
    return false;
}
~~~

~~~cpp
// 固定提交源码摘录：TaskCore::stop() 的状态分支；外围异常宏与 tracing 略
bool TaskCore::stop()
{
    TaskState orig = mTaskState;
    if (mTaskState >= Running) {
        // TRY 宏内的主体
        mTargetState = Stopped;
        if (engine()->stopTask(this)) {
            stopHook();
            mTaskState = Stopped;
            return true;
        } else {
            mTaskState = orig;
            mTargetState = orig;
        }
    }
    return false;
}
~~~

Engine 的 `stopTask()` 暂停 Activity 等当前执行步骤退出，然后再启动同一个 Activity；它没有销毁线程，而是为 TaskCore 建立“updateHook 不再同时运行”的同步点。TaskCore 才能随后调用 `stopHook()` 并提交 Stopped 状态。真实源码还为异常和 tracing 包住这些语句；摘录保留的是这里影响状态顺序的主体。最后，Activity 析构仍需真正回收 OS 线程：

~~~cpp
// 固定提交源码摘录：Activity::~Activity()
Activity::~Activity()
{
    stop();
    terminate();
}
~~~

`stop()` 可能因等待超时而失败，所以析构路径中的 `terminate()` 才负责等待底层线程结束。Linux backend 使用 `pthread_join()`；只有线程退出之后，才可释放 Activity 成员和它执行的代码。若 `stop()` 失败，调用者不能先卸载组件动态库再假设析构会让业务代码安全退出。

`Thread::terminate()` 先设置退出标志并给内部 semaphore 发信号，再让 RTOS 后端删除线程；在 Linux 上，后端删除操作以 join 等待执行线程真正结束：

~~~cpp
// 固定提交源码摘录：Thread::terminate()
void Thread::terminate()
{
    // avoid callling twice.
    if (prepareForExit) return;

    Logger::In in("Thread");
    log(Debug) << "Terminating " << this->getName() << endlog();

    prepareForExit = true;
    rtos_sem_signal(&sem);

    rtos_task_delete(&rtos_task); // this must join the thread.
    active = false;

    log(Debug) << " done" << endlog();
}
~~~

这段逻辑表明 `terminate()` 不会强制打断正在运行的 C++ 代码；semaphore 只能释放睡在该 semaphore 上的线程。若执行线程卡在不返回的设备调用，join 仍然等不到结束。还要注意 Linux `rtos_task_delete()` 的 join 失败分支会记录错误并返回，而 `terminate()` 随后仍设置 `active = false`；这个标志因此不能单独证明线程已经退出。动态库卸载会撤掉线程下一条指令可能所在的代码页，所以必须以线程确实结束为条件，而非只看 Activity 标志。

缩小版实现可从单线程绝对周期 loop 开始，增加受 mutex 保护的退出谓词与 predicate wait；再实现固定容量队列和明确 batch；最后加异常传播、超时 stop、join 与完成时间戳。每一步记录样本写入、通知发送、线程 runnable、获 CPU、hook 开始、stop 返回和线程退出时刻，才能解释控制延迟。

