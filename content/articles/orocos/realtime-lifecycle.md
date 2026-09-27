# 实时性能与关闭：一次 1 ms 周期为什么超期，停机怎样闭环

一台移动底盘以 1 kHz 更新电机命令。单独测 updateHook() 只花 200 微秒，看上去有 800 微秒余量。但同一 ExecutionEngine 还可能在更新前排空操作消息和端口事件；样本类型的复制也可能申请内存；CPU 上另一个高优先级线程可能占走剩余时间。某次周期若晚到，机器人会看到命令年龄增加，速度控制器仍运行，却基于旧的轮速做决策。

要讨论实时性，先把一轮拆成可观测区间：周期到点、Activity 线程收到通知、内核把线程置为 runnable、线程真正拿到 CPU、Engine 处理消息、updateHook 开始、OutputPort 写入缓存、设备驱动真正接受命令。RTT 提供周期 Activity、线程优先级和端口缓存等构件；这些名称本身不构成端到端硬实时证明。本文固定源码为 `orocos-toolchain/rtt` commit `600102e8be9c81905b20930e32d43b28244ab173`，Linux 线程部分讨论的是该提交的 generic GNU/Linux backend。

## 空载平均值为何掩盖超期

先考虑一个朴素控制循环：每轮开始读传感器、计算、写输出，然后 sleep 1 ms。若日志输出本轮占用 8 ms，sleep 不会取消这 8 ms；线程回到循环时已经错过多个释放点。把 sleep 改为绝对时间只能减少累积漂移，不能让超期工作追上截止期。

RTT 的周期 Activity 把下次释放时刻沿绝对时间轴推进，不是在每轮结束后再睡一个完整 period。其等待与 overrun 分支如下；前面的 step/work 分支已在 Activity 专题中完整展开：

~~~cpp
// 固定提交源码摘录：Activity::loop() 的周期等待与超期分支
os::MutexLock lock(msg_lock);
if (wakeup == 0) {
    return;
} else {
    bool time_elapsed = !msg_cond.wait_until(msg_lock, wakeup);
    if (time_elapsed) {
        nsecs now = os::TimeService::Instance()->getNSecs();
        nsecs nsperiod = Seconds_to_nsecs(update_period);
        wakeup = wakeup + nsperiod;

        if (wakeup < now) {
            ++overruns;
            if (overruns == maxOverRun)
                break;
        } else if (overruns != 0) {
            --overruns;
        }

        if (mwaitpolicy == ORO_WAIT_REL)
            wakeup = now + nsperiod;
        mtimeout = true;
    }
}
if (mstopRequested) {
    mstopRequested = false;
    return;
}
~~~

这是函数中的连续等待分支。正常绝对等待以 `wakeup += period` 前进，因此 200 微秒计算并不会把之后每一轮相位都推迟 200 微秒；若工作已经越过下个释放点，`overruns` 递增，恢复到及时状态后才逐步递减，到达阈值后函数退出循环并调用 `emergencyStop()`。`ORO_WAIT_REL` 改为从当前时刻重新计 period，会放弃原来的相位来避免继续追赶落后的释放点。绝对周期减少累积漂移，却不能减少本次工作耗时，也不保证内核准时调度。

~~~text
理想释放点       t0          t0 + 1 ms         t0 + 2 ms
线程实际执行       [Engine + update]----8 ms---->
可观察结果       第一个输出晚到；后续周期赶不上，数据年龄变大
~~~

有代表性的可行性估算应写成：

~~~text
释放抖动
+ Engine 在 hook 前运行的消息和 callback 成本
+ updateHook 最坏执行时间
+ 输入读取/输出写入与用户类型复制
+ 最大锁阻塞
+ 设备调用上界
+ 高优先级干扰
< 周期 - 安全余量
~~~

这一不等式是部署分析模型，不是 RTT 自动计算的数值。实际测量要按目标 OS、调度策略、CPU 拓扑、设备和峰值流量给出最坏或有置信说明的高分位数据；平均值与空载 demo 不能证明截止期。

## 固定版本 Engine 的队列只有空间上限

RTT 的 ExecutionEngine 构造三条容量为 100 的队列。命令与 Port callback 持续 dequeue 到队列为空；function 队列的实现意图是按开轮时的 `size()` 轮转一批，但并发生产会让这个快照与真实 dequeue 次数不一致：

~~~cpp
// 固定提交源码摘录：ExecutionEngine 队列容量
#define ORONUM_EE_MQUEUE_SIZE 100

ExecutionEngine::ExecutionEngine(TaskCore* owner)
    : taskc(owner),
      mqueue(new MWSRQueue<DisposableInterface*>(ORONUM_EE_MQUEUE_SIZE)),
      port_queue(new MWSRQueue<PortInterface*>(ORONUM_EE_MQUEUE_SIZE)),
      f_queue(new MWSRQueue<ExecutableInterface*>(ORONUM_EE_MQUEUE_SIZE))
{}
~~~

构造只限制三个待办容器的最大槽数，并没有为每个周期规定时间额度。命令和端口事件的消费循环如下：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::processMessages() 的消费循环
while (mqueue->dequeue(com)) {
    assert(com);
    com->executeAndDispose();
}
~~~

~~~cpp
// 固定提交源码摘录：ExecutionEngine::processPortCallbacks() 的消费循环
while (port_queue->dequeue(port)) {
    assert(port);
    tc->dataOnPortCallback(port);
}
~~~

两个循环都以“队列暂时为空”为结束条件，所以只要 producer 在消费期间持续写入，工作量便可能超过最初的 100 项。function 队列稍有不同：它在开轮时取 `size()`，再按成功 dequeue 次数递减：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::processFunctions()
void ExecutionEngine::processFunctions()
{
    ExecutableInterface* foo = 0;
    int nbr = f_queue->size();
    while (f_queue->dequeue(foo)) {
        assert(foo);
        if (foo->execute() == false) {
            foo->unloaded();
            {
                MutexLock locker(msg_lock);
            }
            msg_cond.broadcast();
        } else {
            f_queue->enqueue(foo);
        }
        if (--nbr == 0)
            break;
    }
}
~~~

`processFunctions()` 对重复执行的对象先问 `execute()`：返回 false 就 `unloaded()` 并通知等待它完成的线程；返回 true 则把原指针放回队尾。若开轮 `nbr` 大于零，每次成功 dequeue 才会将计数减一；但开轮恰好为零时，生产者可能在 size 快照后、第一次 dequeue 前提交一个 function。这个新对象成功 dequeue 后计数变成负数，不会触发 `nbr == 0` 的退出条件。若 `execute()` 返回 true，它随即被重新入队；即使 producer 停止，该条 function 仍可在这一轮反复执行，直到某次调用返回 false。周期 Activity 可能因此到不了后面的 `processHooks()`，上一轮写出的电机设定值继续留在设备侧。源码的 size 快照与 dequeue 不是一个原子批次边界。复刻时应在 dequeue 前检查预算，例如 `remaining > 0 && dequeue(...)`；固定次数还需配合每项执行时间上界。容量控制的是积压条数，不是每周期的时间预算。

100 是容量上限，不是每周期只处理 100 条的时间预算；队列中的每个 callback 仍可能调用用户代码，源源不断的新项还会在 drain 期间到达。若 100 个 Port callback 各花 0.4 ms，一个 update 周期就可能晚几十毫秒。反过来，队列满会让 process() 返回失败；应用必须读取返回状态并记录拒绝，而不能以为 trigger 通知会恢复失败的入队。

对需要硬截止期的复刻系统，应把空间与时间分别约束：容量限定最多积压多少，batch 限定每周期最多处理多少。此处 batch 是架构建议，不是这个 RTT commit 的可配置保证。把日志、诊断 RPC 和电机闭环放在同一 ExecutionEngine，工作顺序和排空策略就成为闭环 WCET 的一部分。

## C++ 对象行为会进入最坏执行时间

在 `configureHook()` 中调用 `vector.reserve(n)` 只预留容量；`size()` 仍为零，不能直接按下标写。若周期中 `push_back` 超过 `capacity()`，allocator 申请新内存并移动旧元素；若元素含 `string`、`vector` 或用户自定义复制，成本还包含它们的赋值行为。类似地，RTT 的 `DataObjectLockFree<T>` 并不让样本“零拷贝”：写者从循环缓冲取一个候选槽，读者把所选槽复制到调用者提供的对象。下面是写者 `Set(push)` 的完整核心方法；输入 `push` 是新样本，返回值表示写入并推进缓冲指针是否成功：

默认 `DataObject<T>` 并不在运行时挑选算法；它依赖同一个编译期开关在锁保护缓冲和 lock-free 缓冲之间选择：

~~~cpp
// 固定提交源码摘录：DataObject<T> 的编译期后端选择
template<class T>
class DataObject
#if defined(OROBLD_OS_NO_ASM)
    : public DataObjectLocked<T>
#else
    : public DataObjectLockFree<T>
#endif
{
public:
    DataObject(const T& initial_value = T())
#if defined(OROBLD_OS_NO_ASM)
        : DataObjectLocked<T>(initial_value)
#else
        : DataObjectLockFree<T>(initial_value)
#endif
    {}
};
~~~

因此 `DataObjectLockFree` 的槽位协议只代表非 `OROBLD_OS_NO_ASM` 构建选中的实现；定义了该开关时，默认对象会走 `DataObjectLocked<T>`。必须从实际构建配置确认选择结果，不能从 `DataObject` 或 `DataObjectLockFree` 的名字推断运行期一定没有 mutex。

选中 lock-free 实现时，每个槽保存样本、流状态、读者占用计数、写者占用计数和下一个槽的地址；对象还持有 `read_ptr`、`write_ptr` 与整块缓冲：

~~~cpp
// 固定提交源码摘录：DataObjectLockFree<T> 的槽位字段
const unsigned int MAX_THREADS;
const unsigned int BUF_LEN;

struct DataBuf {
    DataBuf()
        : data(), status(NoData), read_counter(), write_lock(), next()
    {
        oro_atomic_set(&read_counter, 0);
        oro_atomic_set(&write_lock, -1);
    }
    value_t data;
    FlowStatus status;
    mutable oro_atomic_t read_counter, write_lock;
    DataBuf* next;
};

typedef DataBuf* volatile VolPtrType;
VolPtrType read_ptr;
VolPtrType write_ptr;
DataBuf* data;
bool initialized;
~~~

这里的 `VolPtrType` 是“指针本身 volatile”，不是指向 volatile 的 DataBuf；这只要求编译器保留对指针对象的读写，不会给槽位建立 C++ 互斥或内存序。`read_counter` 表示读者正在用某个槽，`write_lock` 用于争用写槽；`status` 标明 NoData、NewData 或 OldData。写入失败可以由 writer 正争用同一写槽造成，也可以是 reader pin 住的槽尚未释放。

两个构造器在创建期分配固定数量的 DataBuf，初始样本版本还调用 `data_sample()` 建好环形链接；析构时由同一个对象释放数组：

~~~cpp
// 固定提交源码摘录：DataObjectLockFree<T> 的构造与析构
DataObjectLockFree(const Options &options = Options())
    : MAX_THREADS(options.max_threads()), BUF_LEN(options.max_threads() + 2),
      read_ptr(0), write_ptr(0), initialized(false)
{
    data = new DataBuf[BUF_LEN];
    read_ptr = &data[0];
    write_ptr = &data[1];
}

DataObjectLockFree(param_t initial_value, const Options &options = Options())
    : MAX_THREADS(options.max_threads()), BUF_LEN(options.max_threads() + 2),
      read_ptr(0), write_ptr(0), initialized(false)
{
    data = new DataBuf[BUF_LEN];
    read_ptr = &data[0];
    write_ptr = &data[1];
    data_sample(initial_value);
}

~DataObjectLockFree() {
    delete[] data;
}
~~~

`max_threads` 直接决定 `BUF_LEN = max_threads + 2`，而不是每次消息到来才动态扩容；调用者应按实际并发访问配置它。初始化样本把每个槽预先赋值，并把 `next` 指针接成环：

~~~cpp
// 固定提交源码摘录：DataObjectLockFree<T>::data_sample()
virtual bool data_sample(param_t sample, bool reset = true) {
    if (!initialized || reset) {
        for (unsigned int i = 0; i < BUF_LEN; ++i) {
            data[i].data = sample;
            data[i].status = NoData;
            data[i].next = &data[i + 1];
        }
        data[BUF_LEN - 1].next = &data[0];
        initialized = true;
        return true;
    } else {
        return initialized;
    }
}
~~~

这段创建期初始化同时说明了为什么应在进入实时循环前设好数据样本：构造环本身会多次执行 `T` 的赋值；若第一次 `Set()` 才发现尚未 initialized，上游还会写日志并调用这段初始化路径。`configureHook()` 里的预设样本不仅是接口声明，也把这类分配和复制移出周期回调。

~~~cpp
// 固定提交源码摘录：DataObjectLockFree<T>::Set()
virtual bool Set( param_t push )
{
    if (!initialized) {
        log(Error) << "You set a lock-free data object of type " << internal::DataSourceTypeInfo<T>::getType() << " without initializing it with a data sample. "
                   << "This might not be real-time safe." << endlog();
        data_sample(value_t(), true);
    }

    PtrType writing = write_ptr;            // copy buffer location
    if (!oro_atomic_inc_and_test(&writing->write_lock)) {
        // abort, another thread already successfully locked this buffer element
        oro_atomic_dec(&writing->write_lock);
        return false;
    }

    // Additional check that resolves the following race condition:
    //  - writer A copies the write_ptr and acquires the lock writing->write_lock
    //  - writer B copies the write_ptr
    //  - writer A continues to update and increments the write_ptr
    //  - writer A releases the lock writing->write_lock
    //  - writer B acquires the lock successfully, but for the same buffer element that already
    //    has been written by writer A and can potentially be accessed by readers now!
    if ( writing != write_ptr ) {
        // abort, another thread already updated the write_ptr, which could imply that read_ptr == writing now
        oro_atomic_dec(&writing->write_lock);
        return false;
    }
    // from here on we are sure that 'writing'
    // is a valid buffer to write to and we
    // have exclusive access

    // copy sample
    writing->data = push;
    writing->status = NewData;

    // if next field is occupied (by read_ptr or counter),
    // go to next and check again...
    PtrType next_write_ptr = writing->next;
    while ( oro_atomic_read( &next_write_ptr->read_counter ) != 0 ||
            next_write_ptr == read_ptr )
        {
            next_write_ptr = next_write_ptr->next;
            if (next_write_ptr == writing) {
                oro_atomic_dec(&writing->write_lock);
                return false; // nothing found, too many readers !
            }
        }

    // we will be able to move, so replace read_ptr
    read_ptr  = writing;
    write_ptr = next_write_ptr; // we checked this in the while loop
    oro_atomic_dec(&writing->write_lock);
    return true;
}
~~~

候选槽用 `write_lock` 做独占占用；获取失败或发现 `write_ptr` 已被另一写者推进时，当前写入返回 false。成功之后 `writing->data = push` 执行的是 `T` 的赋值；如果 `T` 内部是可扩容容器，运行时仍可能分配。之后 writer 搜索可复用的下一槽：被读者 pin 住的槽或当前 `read_ptr` 不能覆盖。找遍固定环仍没有可用槽时返回 false；这避免覆盖正在读的数据，却把过载表现为一次可检查的写入失败。

读者传入已有的目标对象 `pull`，并选择是否复制旧样本或无条件复制。下面保留 `Get(pull, copy_old_data, copy_sample)` 的完整实现：

~~~cpp
// 固定提交源码摘录：DataObjectLockFree<T>::Get()
virtual FlowStatus Get( reference_t pull, bool copy_old_data, bool copy_sample ) const
{
    if (!initialized && !copy_sample) {
        return NoData;
    }

    PtrType reading;
    // loop to combine Read/Modify of counter
    // This avoids a race condition where read_ptr
    // could become write_ptr ( then we would read corrupted data).
    do {
        reading = read_ptr;            // copy buffer location
        oro_atomic_inc(&reading->read_counter); // lock buffer, no more writes
        // XXX smp_mb
        if ( reading != read_ptr )     // if read_ptr changed,
            oro_atomic_dec(&reading->read_counter); // better to start over.
        else
            break;
    } while ( true );
    // from here on we are sure that 'reading'
    // is a valid buffer to read from.

    // compare-and-swap FlowStatus field to make sure that only one reader
    // returns NewData
    FlowStatus result;
    do {
        result = reading->status;
    } while((result != NoData) && !os::CAS(&reading->status, result, OldData));

    if ((result == NewData) ||
        ((result == OldData) && copy_old_data) || copy_sample) {
        pull = reading->data;               // takes some time
    }

    // XXX smp_mb
    oro_atomic_dec(&reading->read_counter);       // release buffer
    return result;
}
~~~

读者先暂时 pin 候选槽，再复查 `read_ptr`；如果发布指针已经变化，就撤销计数并重试。成功 pin 后，writer 不会把该槽作为下一写目标。随后 CAS 把 `NewData` 状态改为 `OldData`，使竞争读取者中至多一个读者报告这份数据是新数据；是否复制由输入标志决定。最后减少 `read_counter` 允许 writer 将来复用该槽。这里“lock-free”描述的是这个缓冲同步协议没有普通 mutex wait；它仍会重试、扫描槽位、执行用户类型复制，满负载时还会返回失败。

`read_ptr` 与 `write_ptr` 的类型是 volatile 指针，RTT 的源码注释说明这样标记是因为多个线程会改它们；但 C++ 的 `volatile` 不是互斥，也不是原子发布协议。计数器经由 `oro_atomic_*`，状态更新经由 `os::CAS`。这些旧接口没有 `std::atomic<T>` 的 `memory_order` 参数；其原子整数抽象只提供 read/set/inc/decrement 一类操作：

~~~cpp
// 固定提交源码摘录：AtomicInt 的原子操作接口
int read() const { return oro_atomic_read(&_val); }
void set(int i) { oro_atomic_set(&_val, i); }
void inc() { oro_atomic_inc(&_val); }
void dec() { oro_atomic_dec(&_val); }
bool inc_and_test() { return oro_atomic_inc_and_test(&_val) != 0; }
~~~

原子读改写解决的是特定计数或状态更新不被并发交错；内存序还规定普通样本数据与状态发布之间的可见顺序。标准 C++ 的 `memory_order_relaxed` 只要求原子变量自身操作不可分割，不发布旁边的 payload；release 写与观察到它的 acquire 读可以建立“先写 payload、后发布状态；读状态后再读 payload”的同步关系；`seq_cst` 还为顺序一致操作提供单一总序。RTT 这里没有把这些选择暴露给调用者，且 `Get` 中的 `// XXX smp_mb` 是注释，不会执行屏障。故不要把这些旧接口一概称为 C++ 原子或推断出某个标准内存序；具体 `oro_atomic_*` 的操作强度取决于构建选择的架构后端，样本字段与 volatile 指针间的发布关系也需按目标编译器、架构和配置审查。复刻时应明确哪个 release 发布哪个槽位、哪个 acquire 读取该发布，再单独证明槽位不会在复制过程中被复用。

缩小版的单槽教学例子可以先展示 release/acquire 的发布关系，但它不等价于 RTT 的多槽、多读者环形算法：

~~~cpp
// 教学最小例子：单生产者和单消费者用一个槽位传递样本
#include <atomic>
std::atomic<bool> ready{false};
int payload = 0;

void produce(int input) {
    // 等消费者释放上一次样本；这是教学协议，不是实时等待策略。
    while (ready.load(std::memory_order_acquire)) {
    }
    payload = input;
    ready.store(true, std::memory_order_release);
}

bool consume(int& output) {
    if (!ready.load(std::memory_order_acquire))
        return false;
    output = payload;
    ready.store(false, std::memory_order_release);
    return true;
}
~~~

这里生产者读到消费者 release 的 false 后才覆盖 payload；消费者 acquire 读到生产者 release 的 true 后再复制 payload。若去掉 release/acquire，仅凭 `ready` 是原子 bool，消费者仍不能据此推断普通 `payload` 已经安全发布。该教学例子用忙等保持单槽，不能直接放进实时控制循环；生产环境要定义有界等待、丢弃策略或多个预分配槽。若要支持多写者、覆盖旧样本和 reader pin，就必须再设计槽位所有权、指针发布、复用条件以及满时策略。实时性优化还要固定最大维数、提前调整目的对象并测量 `T` 的赋值，而不能只看容器名字里有 lock-free。

~~~cpp
// 教学最小例子：在配置期建立运行缓冲，并为失效输入写安全输出
bool configureHook() override {
    command_.positions.resize(max_joints_);
    torque_.resize(max_joints_);
    command_in_.setDataSample(command_);
    torque_out_.setDataSample(torque_);
    return device_.open();
}

void updateHook() override {
    const RTT::FlowStatus flow = command_in_.read(command_, false);
    if (flow != RTT::NewData) {
        fillSafeTorque(torque_);
    } else {
        computeTorque(command_, torque_);
    }
    torque_out_.write(torque_);
}
~~~

这是教学代码，不是上游片段。实时路径复用成员缓冲区，且把 OldData/NoData 变成可观察的安全分支；具体安全输出必须由机器人机械、电气与控制设计确定。若 sample 最大维数不固定，configure 阶段还要验证数据形状，避免长状态把 vector 再扩容。

Linux page fault 表示访问页时地址尚未映射到当前进程可用物理页，内核需处理缺页；首次触及堆页或共享库页可能导致抖动。启动实时循环前预热代码和数据页、锁定内存可减少这类延迟来源，但 mlock 并不能把无界算法或慢设备调用变成有界。操作系统对线程调度和 CPU affinity 也只建立运行约束：固定 CPU 不等于独占 CPU；高优先级线程可以在同 CPU 上抢占低优先级工作，其他中断与内核活动仍影响延迟。

Activity 构造器把 scheduler、priority 和 CPU affinity 交给 `os::Thread`。在线程启动时，RTT 在实际线程内核对请求的周期与 scheduler；如果当前策略与请求不同，它再调用适配层设置，并读取系统实际接受的策略：

~~~cpp
// 固定提交源码摘录：Thread::configure()
void Thread::configure()
{
    rtos_task_set_period(&rtos_task, period);
    if (msched_type != rtos_task_get_scheduler(&rtos_task)) {
        rtos_task_set_scheduler(&rtos_task, msched_type);
        msched_type = rtos_task_get_scheduler(&rtos_task);
    }
}
~~~

GNU/Linux backend 最终通过 pthread 查询当前调度器，并尝试更新优先级与策略：

~~~cpp
// 固定提交源码摘录：Linux rtos_task_set_scheduler()
INTERNAL_QUAL int rtos_task_set_scheduler(RTOS_TASK* task, int sched_type)
{
    int policy = -1;
    struct sched_param param;
    if (task && task->thread != 0 &&
        rtos_task_check_scheduler(&sched_type) == -1)
        return -1;
    if (pthread_getschedparam(task->thread, &policy, &param) == 0) {
        param.sched_priority = task->priority;
        rtos_task_check_priority(&sched_type, &param.sched_priority);
        return pthread_setschedparam(task->thread, sched_type, &param);
    }
    return -1;
}
~~~

在 Linux generic backend 中，`ORO_SCHED_RT` 对应 `SCHED_FIFO`；普通用户若没有实时优先级权限或 `RLIMIT_RTPRIO` 配置，检查可能把请求降为 `SCHED_OTHER`。实时优先级在该 backend 会被约束到 1–99，普通策略的静态优先级则是 0。因而“我在组件里写了 priority=80”只是请求。部署程序要检查 `getScheduler()` 与 `getPriority()` 的实际值和构造日志；如果没有成功切到 `SCHED_FIFO`，控制回调仍按普通调度策略运行。

`Activity::setCpuAffinity()` 最终调用 `Thread::setCpuAffinity()`，后者只是返回 RTOS 适配调用是否成功。在 Linux backend，这个参数按位掩码解释，而不是 CPU 序号：

~~~cpp
// 固定提交源码摘录：Linux rtos_task_set_cpu_affinity()
INTERNAL_QUAL int rtos_task_set_cpu_affinity(RTOS_TASK* task,
                                             unsigned cpu_affinity)
{
    if (cpu_affinity == 0)
        cpu_affinity = ~0;
    if (task && task->thread != 0) {
        cpu_set_t cs;
        CPU_ZERO(&cs);
        for (unsigned i = 0; i < 8 * sizeof(cpu_affinity); i++) {
            if (cpu_affinity & (1 << i))
                CPU_SET(i, &cs);
        }
        return pthread_setaffinity_np(task->thread, sizeof(cs), &cs);
    }
    return -1;
}
~~~

例如只允许 CPU 3，需要给出含第 3 位的 mask；传入 `3` 表示 CPU 0 与 CPU 1 都可运行，不是“绑到 CPU 3”。绑核只缩小可运行 CPU 集合，不会为该线程保留核心，也不消除同核中断、内核工作或同优先级线程的竞争。

线程从 blocked 变成 runnable 后，调度器还要比较当前可运行线程的优先级与 CPU 亲和性，再决定是否切换。一次上下文切换要保存离开线程的寄存器现场、栈指针等状态，并恢复新线程的状态；若发生在不同 CPU，还涉及缓存局部性损失。它不是每次函数调用都会发生，也不是通知 API 的一部分。`SCHED_FIFO` 实时策略不按普通线程的时间片轮转：更高优先级 runnable 线程可以抢占较低优先级线程；同优先级 FIFO 线程若不阻塞、不让出 CPU，可能使排在它后的同优先级工作一直等不到运行。周期 wait、明确 yield 和工作量上界因此会影响同优先级控制任务的时序。

## 优先级反转从一条具体锁时间线开始

假设低优先级诊断线程持有普通 mutex，正把故障表写入 vector；中优先级视觉线程持续运行；高优先级控制线程需要该 mutex 才能写力矩。控制线程虽然优先级最高，却先 blocked 等锁；诊断线程又不能抢过视觉线程完成临界区。控制输出因此晚到。把互斥锁替换成原子变量也不一定合理，因为 vector 的结构变化不是一个原子字段。

优先级反转的关键不是“锁是否互斥”，而是锁等待时谁能继续运行。GNU/Linux backend 的普通 RTT mutex 初始化使用默认 pthread mutex 属性：

~~~cpp
// 固定提交源码摘录：Linux 普通 RTT mutex 初始化
typedef pthread_mutex_t rt_mutex_t;

static inline int rtos_mutex_init(rt_mutex_t* m)
{
    return pthread_mutex_init(m, 0);
}
~~~

这里没有给 mutex 设置 `PTHREAD_PRIO_INHERIT`。因此如果低优先级诊断线程拿着这类锁，高优先级控制线程阻塞等待时，中优先级线程仍可能持续抢占低优先级线程，让锁迟迟不能释放。RTT 不会替组件成员变量自动加锁，也不能从“用了 Mutex”推断有优先级继承。可选修复是让共享状态只由 Engine 线程写、在锁外构造不可变快照并用短临界区交换，或者为确有必要跨优先级共享的锁单独选用并验证 PI mutex。若高优先级线程同步等待低优先级 OwnThread Operation，排队与执行时间也进入高优先级路径。

锁审计要写清谁拿锁、锁保护哪个不变量、读写会不会跨锁、锁内是否调用用户回调或设备 I/O，以及最大持有时间。存在 mutex 不足以证明状态安全；无锁容器也不自动意味着整个路径不会阻塞。

## 组件 stop 建立同步点，Activity 析构才收线程

RTT 的 TaskCore::stop() 不是立刻销毁线程。Engine 先 stop 再 start Activity，让 stop 返回成为当前 step 已经离开的同步点；TaskCore 随后执行 stopHook 并提交 Stopped 状态。摘录保留决定同步和状态顺序的代码：

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
// 固定提交源码摘录：TaskCore::stop() 的状态分支；异常宏与 tracing 略
bool TaskCore::stop()
{
    TaskState orig = mTaskState;
    if (mTaskState >= Running) {
        // TRY 宏中的主体
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

摘录省略了异常捕获与 tracing 语句，但保留了返回值为 true 时的主要状态顺序。组件可以之后再次 start；这次 stop/start 不是 OS 线程 join。

Activity::stop() 设置 `mstopRequested`、广播等待条件并等待 `breaker`；非周期 loop 还需要 Runnable 的 `breakLoop()` 返回 true。下面给出完整停止分支，省略的只有日志语句：

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
                // 原始 Warning 日志语句省略
                running = true;
                return false;
            }
            MutexTimedLock lock(breaker, getStopTimeout());
            if (!lock.isSuccessful()) {
                // 原始 Error 日志语句省略
                running = true;
                return false;
            }
        }
    } else {
        MutexTimedLock lock(breaker, getStopTimeout());
        if (lock.isSuccessful()) {
            rtos_task_make_periodic(&rtos_task, 0);
        } else {
            // 原始 Error 日志语句省略
            running = true;
            return false;
        }
    }

    this->finalize();
    active = false;
    return true;
}
~~~

若用户 loop 阻塞在设备 I/O 且没有取消机制，`breakLoop()` 不能自动中断该系统调用，stop 可能超时并返回 false。Activity 析构随后仍调用 `terminate()`，Linux 适配最终以 `pthread_join()` 等待底层线程：

~~~cpp
// 固定提交源码摘录：Activity::~Activity()
Activity::~Activity()
{
    stop();
    terminate();
}
~~~

~~~cpp
// 固定提交源码摘录：Linux rtos_task_delete()
INTERNAL_QUAL void rtos_task_delete(RTOS_TASK* mytask)
{
    int ret = pthread_join(mytask->thread, 0);
    if (ret != 0) {
        log(Error) << "Failed to join thread " << mytask->name << ": "
                   << strerror(ret) << endlog();
        return;
    }
    pthread_attr_destroy(&(mytask->attr));
    free(mytask->name);
    mytask->name = NULL;
}
~~~

Activity 析构和 Linux 后端分别是两段固定提交摘录。析构先请求停止，再调用 Thread 的 terminate，后者通过 join 等待 OS 线程真正退出。若业务 loop 永不返回，join 也会一直等待；若 join 失败，这个后端函数会记录错误并返回，调用者不能据此断言线程已退出。工程上应先停生产者、设置设备取消/超时，再请求 TaskContext 停止，确认 stop 与线程退出后才断开接口并卸载动态库。

合理停机顺序是先让外部不再生产输入/命令，再等当前 Engine 工作跨过同步点，然后让 stopHook 进入设备安全态；确认没有在途调用后断开连接和清理资源，最后析构 TaskContext 与 Activity。stop 失败时，所有仍被线程借用的 Runnable、TaskContext 和动态库代码都必须保持存活。析构不能替外部管理器证明设备已经安全。

## 异常状态也有真实控制路径

`TaskCore::error()` 本身只改变状态，不发设备命令，也不立即运行 `errorHook()`：

~~~cpp
// 固定提交源码摘录：TaskCore::error()
void TaskCore::error()
{
    if (mTargetState < Running)
        return;
    mTargetState = mTaskState = RunTimeError;
}
~~~

`ExecutionEngine::work()` 只有在周期超时或 I/O ready 时才调用 hook 阶段；Trigger 路径仅处理命令与端口通知：

~~~cpp
// 固定提交源码摘录：ExecutionEngine::work() 的原因分派
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
~~~

在 `processHooks()` 内，只有当前状态和目标状态都为 Running 才调用 `updateHook()`；RunTimeError 且目标仍至少为 Running 时改走 `errorHook()`。这两个状态门控决定业务回调，而不是 OS 线程状态：线程可能仍是 runnable/运行中，组件状态却已进入 RunTimeError。若某系统只收到 Trigger、不再有下一个周期 timeout 或 I/O ready，错误状态可能先于 `errorHook()` 存在一段时间；安全输出不能依赖这个 hook 立即发生。

未处理异常则会进入 TaskCore 的 exception 路径。也就是说状态改变不会直接把安全命令送到驱动，状态迁移、hook 执行和硬件确认仍是不同阶段。

控制器不能只改变状态枚举而不处理输出。一个传感器停更的具体反例是：输入进入 OldData，控制仍基于旧姿态算力矩；实际关节持续运动，力矩目标落后于实际状态。组件应有基于 timestamp 的数据新鲜度判断、明确安全输出和硬件 watchdog。errorHook 不能在高优先级 Activity 中无限重连或写日志；硬件安全动作不能依赖一个可能再也没有机会运行的后续周期。

## 最小复刻与测量

一个小型实时执行器可依次实现：固定维数样本与 configure 预分配；绝对周期 wait；带明确满载返回的队列；固定 per-cycle batch；周期超时统计；从 Running 到 Stop 的 wait/breakLoop/join；最后加安全输出和 Error 状态。每一步都做可观察故障注入：向队列灌入超过处理速度的消息、把 updateHook 延迟到超过周期、让设备调用卡住、并行请求 stop，并观察队列高水位、样本年龄、deadline miss、停止返回时间和线程退出时间。

任何工程结论都要绑定实际 OS、调度策略、CPU 亲和性、RTT 构建选项、连接 policy、样本最大大小和负载。报告最大超期、长尾分布、动态分配数与 stop 超时，不用一个“1 kHz 平均稳定”代替。
