# Reactor：Shard-per-Core 怎样把线程、任务、I/O 与睡眠协议收敛到一个 Ownership Domain

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Seastar 的 Reactor 容易被一句“每核一个事件循环”说得过于简单。真正重要的并不是它也会轮询 I/O，而是它先改变了运行时的**所有权边界**：

> 一个 shard 对应一个长期运行的 Reactor 执行线程；这个 shard 上的大部分 task queue、timer、I/O submission state、poller registry 和调度状态，都尽量由这个 Reactor thread 单独推进。

这条约束一旦成立，很多传统多线程 runtime 必须反复解决的问题会换一种形态：

- 本地 ready queue 不需要设计成全局 MPMC 队列；
- scheduling group 可以直接操作本 shard 的普通容器；
- Future continuation 可以在当前 shard 上直接变成 task；
- I/O completion、SMP message、timer、signal 不必各自拥有一套独立 worker 调度器；
- 真正的跨核共享只集中在 SMP queue、alien queue、cross-CPU free 等明确边界上。

因此，Reactor 不是某个网络模块下面的“小循环”，而是一个 shard 的执行内核。前面几篇已经分别拆过 [SMP message queue](smp-message-queue.md)、[Future continuation](future-continuation-task.md)、[sharded / foreign_ptr](sharded-foreign-ptr.md) 和 [cross-shard reclaim](cross-shard-memory-reclaim.md)。这一篇把它们重新接回同一个问题：

**这些跨核入口最终怎样回到某一个 Reactor thread，并在不依赖全局 scheduler mutex 的前提下继续执行？**

---

## 1. 先把 shard-per-core 从口号还原成真实线程

“每核一个 Reactor”首先是一个真实的线程与对象绑定关系，而不是逻辑标签。

固定版本启动非 0 shard 时，会为每个 shard 建立线程；如果启用了 thread affinity，还会把线程固定到分配的 CPU，然后在该线程上创建 Reactor：

~~~cpp
for (i = 1; i < smp::count; i++) {
    auto allocation = allocations[i];
    create_thread([this, /* ... */, i, allocation, backend_selector, reactor_cfg] {
        auto thread_name = fmt::format("reactor-{}", i);
        pthread_setname_np(pthread_self(), thread_name.c_str());

        if (thread_affinity) {
            smp::pin(allocation.cpu_id);
        }

        init_default_smp_service_group(i);
        lowres_clock::update();
        allocate_reactor(i, backend_selector, reactor_cfg);

        reactors[i] = &engine();
        // ...
        engine().configure(reactor_opts);
        engine().do_run();
    });
}
~~~

`allocate_reactor()` 又把 shard id 和当前 Reactor 写进 thread-local 状态：

~~~cpp
void smp::allocate_reactor(unsigned id,
                           reactor_backend_selector rbs,
                           reactor_config cfg) {
    SEASTAR_ASSERT(!reactor_holder);

    void* buf;
    int r = posix_memalign(&buf, cache_line_size, sizeof(reactor));
    SEASTAR_ASSERT(r == 0);

    *internal::this_shard_id_ptr() = id;
    local_engine = new (buf) reactor(
        this->shared_from_this(), _alien, id, std::move(rbs), cfg);

    reactor_holder.reset(local_engine);
}
~~~

这里的 `local_engine` 是 thread-local Reactor 指针。于是代码里常见的：

~~~cpp
engine().add_task(t);
this_shard_id();
current_scheduling_group();
~~~

并不是去某个全局 Reactor 数组里查“我属于谁”，而是在当前执行线程里直接找到本 shard 的运行时状态。

这就是 ownership domain 的第一层含义：

~~~text
OS thread / CPU affinity
        |
        v
thread-local shard id
        |
        v
thread-local Reactor
        |
        +-- local task queues
        +-- local timers
        +-- local pollers
        +-- local I/O queues
        +-- local scheduling state
~~~

它并不意味着“Seastar 没有共享状态”。SMP queues、alien queues、systemwide barrier、跨核回收入口都显然涉及其他 CPU。区别是：**共享边界被压缩到少数明确协议里，本地 steady-state 数据结构尽量保持单 owner。**

---

## 2. 为什么 Reactor 的 ready queue 可以只是普通 `circular_buffer`

Reactor 为 scheduling group 保存独立的 `task_queue`。固定版本中的核心字段是：

~~~cpp
struct task_queue {
    explicit task_queue(
        unsigned id, sstring name, sstring shortname, float shares);

    int64_t _vruntime = 0;
    float _shares;
    int64_t _reciprocal_shares_times_2_power_32;
    bool _active = false;
    const uint8_t _id;

    sched_clock::time_point _ts;
    sched_clock::duration _runtime = {};
    sched_clock::duration _waittime = {};
    sched_clock::duration _starvetime = {};
    uint64_t _tasks_processed = 0;

    circular_buffer<task*> _q;
    sstring _name;
    // ...
};
~~~

最值得注意的反而不是 `_vruntime`，而是：

~~~cpp
circular_buffer<task*> _q;
~~~

如果多个 OS worker 都可以并发 push/pop 同一个 ready queue，这里通常需要 mutex、MPMC queue 或 work-stealing deque 的并发协议。Seastar 的常规本地调度不这样做。

本 shard 上调度一个 task 时：

~~~cpp
void reactor::add_task(task* t) noexcept {
    auto sg = t->group();
    auto* q = _task_queues[sg._id].get();

    bool was_empty = q->_q.empty();
    q->_q.push_back(std::move(t));
    shuffle(q->_q.back(), q->_q);

    if (was_empty) {
        activate(*q);
    }
}
~~~

`schedule(task*)` 只是进一步调用当前 Reactor：

~~~cpp
void schedule(task* t) noexcept {
    engine().add_task(t);
}
~~~

因此这里的关键前置条件不是“`circular_buffer` 天生线程安全”，而是：

**普通 `schedule()` 发生在 task 所属 shard 的 Reactor execution domain 内。**

跨 shard 工作不能把另一个 Reactor 的 `_q` 当普通容器直接改。跨核入口要先经过 SMP message queue，再由目标 shard 的 SMP poller 把消息转化为目标 Reactor 可以执行的 task。这个边界在 [SMP Message Queue](smp-message-queue.md) 中已经展开。

### 如果去掉 single-owner 会发生什么

假设 CPU 1 可以直接向 CPU 0 的 `_task_queues[x]._q` 做 `push_back()`，同时 CPU 0 在 `pop_front()`：

~~~text
CPU 0 / Reactor                       CPU 1
----------------                       -----
read deque metadata
                                      push_back()
                                      possibly reallocate / mutate indices
pop_front() using stale metadata
~~~

此时不只是“可能顺序不公平”，而是容器本身的数据竞争和结构损坏。

所以 Seastar 不是“省略了一把锁”，而是把问题改写成：

~~~text
跨核生产
   |
   v
SMP queue / explicit handoff
   |
   v
目标 Reactor 收到
   |
   v
目标 Reactor 自己 add_task()
   |
   v
local circular_buffer
~~~

这是 shard-per-core 架构最核心的推理链。

---

## 3. `task*` 为什么看起来不像普通 C++ RAII 对象

`task_queue` 保存的是裸 `task*`。如果只看这个字段，很容易问：谁 delete？

固定版本的 `task` 接口把销毁协议直接合进执行接口：

~~~cpp
class task {
protected:
    scheduling_group _sg;

    ~task() = default;

public:
    explicit task(
        scheduling_group sg = current_scheduling_group()) noexcept
        : _sg(sg) {}

    virtual void run_and_dispose() noexcept = 0;
    virtual task* waiting_task() noexcept = 0;

    scheduling_group group() const {
        return _sg;
    }
};
~~~

基类析构甚至不是 public virtual destructor。注释明确说明：具体 task 通过自己的 `run_and_dispose()` 完成执行和销毁。

Reactor 真正消费 task 时：

~~~cpp
auto tsk = tasks.front();
tasks.pop_front();

_current_task = tsk;
tsk->run_and_dispose();
_current_task = nullptr;

++tq._tasks_processed;
++_global_tasks_processed;
~~~

所以这里的生命周期模型不是：

~~~text
queue owns unique_ptr<task>
  -> pop
  -> destructor
~~~

而更接近：

~~~text
queue stores scheduler-owned execution node pointer
  -> Reactor removes pointer
  -> dynamic task type executes
  -> run_and_dispose() finishes its own disposal protocol
~~~

这和 Future continuation 的实现直接相连：很多 continuation 本身就是 task 节点，完成后不再需要额外一次通用虚析构分派。代价是实现 task 派生类时必须严格遵守这一销毁契约。

---

## 4. `run_tasks()`：真正执行用户 continuation 的地方

某个 scheduling group 被选中后，Reactor 会进入 `run_tasks(task_queue&)`：

~~~cpp
void reactor::run_tasks(task_queue& tq) {
    *internal::current_scheduling_group_ptr()
        = scheduling_group(tq._id);

    auto& tasks = tq._q;

    while (!tasks.empty()) {
        auto tsk = tasks.front();
        tasks.pop_front();

        _current_task = tsk;
        tsk->run_and_dispose();
        _current_task = nullptr;

        ++tq._tasks_processed;
        ++_global_tasks_processed;

        if (internal::scheduler_need_preempt()) {
            if (tasks.size() <= _cfg.max_task_backlog) {
                break;
            } else {
                reset_preemption_monitor();
                lowres_clock::update();
                // rate-limited backlog warning...
            }
        }
    }
}
~~~

先看第一行：

~~~cpp
*internal::current_scheduling_group_ptr()
    = scheduling_group(tq._id);
~~~

这保证在当前 task 内新创建的后续 task，默认会继承当前 scheduling group。也就是说 scheduling group 不只是 queue 上的标签，它会沿当前执行上下文向新 continuation 传播。

然后才是：

~~~cpp
tsk->run_and_dispose();
~~~

这个调用可能执行：

- Future continuation；
- coroutine resume；
- loop helper 创建的内部 task；
- poller registration task；
- shutdown 相关 continuation；
- 业务层异步状态机的一小步。

所以“Reactor 执行 task”不是等价于“执行一个完整请求”。一个业务请求通常会被 async boundary 切成很多 continuation task，反复回到 Reactor。

---

## 5. Cooperative scheduling：为什么不能理解成 OS 抢占

Seastar 会检查 `need_preempt()`，但它并不会在任意 C++ 指令之间保存当前 task 的寄存器现场，再强制跳到另一个 task。

固定版本的 preemption monitor 很小：

~~~cpp
struct preemption_monitor {
    std::atomic<uint32_t> head;
    std::atomic<uint32_t> tail;
};
~~~

检查逻辑是：

~~~cpp
inline bool monitor_need_preempt() noexcept {
    std::atomic_signal_fence(std::memory_order_seq_cst);

    auto np = internal::get_need_preempt_var();
    auto head = np->head.load(std::memory_order_relaxed);
    auto tail = np->tail.load(std::memory_order_relaxed);

    return __builtin_expect(head != tail, false);
}

inline bool need_preempt() noexcept {
#ifndef SEASTAR_DEBUG
    return internal::monitor_need_preempt();
#else
    return true;
#endif
}
~~~

这里发生的事情只是：

**运行时代码能够很便宜地询问“当前 quota 是否已经到，该不该把控制权还给 Reactor”。**

真正的切换仍然必须发生在代码愿意检查这个条件的地方。

例如 Seastar 的循环工具会在循环中检查 `need_preempt()`，coroutine awaiter 也会在 ready future 场景检查它；`run_tasks()` 则在每个 task 完成之后检查 scheduler 版本的 preemption 条件。

因此要区分三件事：

~~~text
OS preemption
  内核可在任意允许抢占的位置切换 OS thread

Seastar task quota
  runtime 标记“应该让出执行权”

cooperative yield boundary
  Seastar primitive / continuation / coroutine 真正观察标记并返回 Reactor
~~~

### 一个 CPU 密集死循环为什么仍然能卡住 shard

如果业务代码写成：

~~~cpp
for (;;) {
    compute_one_iteration();
}
~~~

并且中间没有 Future boundary、没有 coroutine suspension、没有 `need_preempt()` / `yield()` 检查，那么 Reactor 没有通用机制把这个普通 C++ 调用栈强行切走。

结果不是“只有这个请求变慢”，而是这个 shard 上：

- timer 不能及时 expire；
- SMP message 不能及时消费；
- I/O completion 不能及时转成 continuation；
- 其他 scheduling group 也无法运行；
- tail latency 一起上升。

这正是 shard-per-core 用低锁换来的重要编程约束。

---

## 6. `scheduler_need_preempt()` 为什么和普通 `need_preempt()` 分开

在 release build 中二者语义基本一致，但 debug build 中 scheduler 有特殊处理。源码解释了原因：如果 debug 模式的 `need_preempt()` 永远返回 true，scheduler 每跑一个 task 就回去检查 I/O，会极大拖慢测试。

因此 `scheduler_need_preempt()` 在 debug 下改成有界的周期检查，同时仍观察真实 preemption monitor。

这不是微不足道的测试代码细节，它说明 Reactor 里存在两种不同的“让出”语义：

- **业务 primitive**：宁可更积极 yield，用来暴露依赖 cooperative progress 的问题；
- **scheduler 内部**：必须保证自己仍能有效批处理 task，不能因为 debug 语义退化成一 task 一次完整 poll。

这也是为什么 `run_some_tasks()` 末尾专门写着：

~~~cpp
} while (have_more_tasks() && !need_preempt());
~~~

而不是继续使用 `scheduler_need_preempt()`。

---

## 7. backlog 太大时，为什么 quota 到了还可能继续跑

`run_tasks()` 还有一个很容易忽略的分支：

~~~cpp
if (internal::scheduler_need_preempt()) {
    if (tasks.size() <= _cfg.max_task_backlog) {
        break;
    } else {
        reset_preemption_monitor();
        lowres_clock::update();
        // warn about too long queue
    }
}
~~~

直觉上 quota 到了应该立刻切走，但当单个 task queue 已经积压得太深时，固定版本会选择重置 preemption monitor 并继续处理，同时打印限频告警。

原因可以从结果倒推：

如果一个 queue 的生产速度已经让 backlog 持续超过阈值，而 scheduler 每个 quota 都只处理极少量任务，它可能永远追不上 backlog，内存与排队延迟继续膨胀。

所以这里实际上在两个坏结果之间做取舍：

~~~text
严格 quota
  -> 更快回去 poll I/O
  -> 但超大 backlog 可能继续膨胀

继续 drain backlog
  -> 当前 scheduling latency 变差
  -> 但避免 ready task 队列无限堆积
~~~

这不是硬实时保证，而是一种 overload handling 策略。对机器人系统尤其要注意：如果控制相关 continuation 与高吞吐后台任务落在同一 shard、同一 scheduling group，超大 backlog 会直接改变闭环任务的可调度时间。

---

## 8. `run_some_tasks()`：task queue 之上的第二层 scheduler

`run_tasks()` 只负责某一个 scheduling group。真正决定“下一组跑谁”的是 `run_some_tasks()`：

~~~cpp
void reactor::run_some_tasks() {
    if (!have_more_tasks()) {
        return;
    }

    reset_preemption_monitor();
    lowres_clock::update();

    sched_clock::time_point t_run_completed = now();
    _cpu_stall_detector->start_task_run(t_run_completed);

    do {
        auto t_run_started = t_run_completed;

        insert_activating_task_queues();
        task_queue* tq = pop_active_task_queue(t_run_started);

        _last_vruntime = std::max(
            tq->_vruntime, _last_vruntime);

        run_tasks(*tq);

        t_run_completed = now();
        auto delta = t_run_completed - t_run_started;

        account_runtime(*tq, delta);
        tq->_ts = t_run_completed;

        if (!tq->_q.empty()) {
            insert_active_task_queue(tq);
        } else {
            tq->_active = false;
        }
    } while (have_more_tasks() && !need_preempt());

    _cpu_stall_detector->end_task_run(t_run_completed);

    *internal::current_scheduling_group_ptr()
        = default_scheduling_group();
}
~~~

完整状态流是：

~~~text
new task
   |
   v
add_task()
   |
   +-- queue was empty? -- yes --> activate(queue)
   |                              |
   |                              v
   |                     _activating_task_queues
   |
Reactor iteration
   |
   v
insert_activating_task_queues()
   |
   v
_active_task_queues ordered by vruntime
   |
   v
pop_active_task_queue()
   |
   v
run_tasks(queue)
   |
   v
measure wall runtime delta
   |
   v
account_runtime()
   |
   +-- still has tasks --> insert back
   |
   +-- empty -----------> inactive
~~~

这一层才真正把 task queue 变成 CPU scheduler。

---

## 9. vruntime 先只理解成“付费后的 CPU 时间”

完整 scheduling group 机制留给下一篇 [Scheduling Group 与 vruntime](scheduling-groups-vruntime.md)。这里只建立 Reactor 主循环需要的最小直觉。

固定版本把实际运行时间按 shares 缩放：

~~~cpp
int64_t
reactor::task_queue::to_vruntime(
        sched_clock::duration runtime) const {
    auto scaled =
        (runtime.count()
         * _reciprocal_shares_times_2_power_32) >> 32;

    return std::max<int64_t>(scaled, 0);
}

void
reactor::task_queue::set_shares(float shares) noexcept {
    _shares = std::max(shares, 1.0f);
    _reciprocal_shares_times_2_power_32 =
        (uint64_t(1) << 32) / _shares;
}

void
reactor::account_runtime(
        task_queue& tq,
        sched_clock::duration runtime) {
    if (runtime > (2 * _cfg.task_quota)) {
        _stalls_histogram.add(runtime);
        tq._time_spent_on_task_quota_violations
            += runtime - _cfg.task_quota;
    }

    tq._vruntime += tq.to_vruntime(runtime);
    tq._runtime += runtime;
}
~~~

shares 越大，同样的真实 CPU 时间转换出的 vruntime 增量越小，于是该 queue 更慢地“变贵”。

active queues 按 vruntime 排序：

~~~cpp
struct reactor::task_queue::indirect_compare {
    bool operator()(
            const task_queue* tq1,
            const task_queue* tq2) const {
        return tq1->_vruntime < tq2->_vruntime;
    }
};
~~~

因此可以先把它理解成：

> Reactor 不只是轮询 ready queue；它还维护一个“每个 scheduling group 已经消耗了多少加权 CPU 时间”的账本。

---

## 10. 为什么 inactive queue 重新激活时不能带着很老的 vruntime

I/O-bound queue 可能长时间没有 task。如果它保持很久以前的低 vruntime，突然重新出现时会比所有活跃 CPU-bound queue 都“便宜”很多，从而连续霸占调度。

固定版本的 `activate()` 会做校正：

~~~cpp
void reactor::activate(task_queue& tq) {
    if (tq._active) {
        return;
    }

    tq._vruntime = std::max(
        _last_vruntime,
        tq._vruntime);

    auto now = reactor::now();
    tq._waittime += now - tq._ts;
    tq._ts = now;

    _activating_task_queues.push_back(&tq);
}
~~~

这条：

~~~cpp
tq._vruntime = std::max(_last_vruntime, tq._vruntime);
~~~

非常关键。

它允许刚唤醒的 I/O-bound group 获得低延迟，但不允许它拿着一个“几秒前的历史低价”回到 scheduler 后长期碾压其他 group。

所以 Reactor 里的 fairness 不是简单 round-robin，而是同时考虑：

- queue 是否 active；
- 上次累计 vruntime；
- shares；
- 实际执行时间；
- task quota；
- backlog overload。

---

## 11. Task scheduler 跑完以后，Reactor 才开始检查外部世界

Reactor 主循环不是：

~~~text
epoll_wait
 -> callback
 -> epoll_wait
~~~

固定版本真正的结构更接近：

~~~text
while (true):
    run_some_tasks()

    if stopped:
        drain final tasks
        finish shard
        break

    poll_once()

    if work:
        continue

    idle handler with pure_poll

    if still idle long enough:
        try_sleep()
~~~

对应源码：

~~~cpp
std::function<bool()> check_for_work = [this] {
    return poll_once() || have_more_tasks();
};

std::function<bool()> pure_check_for_work = [this] {
    return pure_poll_once() || have_more_tasks();
};

while (true) {
    run_some_tasks();

    if (_stopped) {
        // shutdown path ...
        break;
    }

    _polls++;
    lowres_clock::update();

    if (check_for_work()) {
        // leave idle accounting
    } else {
        // idle handler
        // maybe spin
        // maybe sleep
    }
}
~~~

这说明 Seastar 的 Reactor 至少有两类工作：

1. **已经变成 task 的工作**：Future continuation、用户异步逻辑等；
2. **还停留在 readiness/completion source 的工作**：SMP queue、kernel completion、I/O submission、timer、signal 等。

Poller 的作用就是把第二类工作不断转化、推进，最终让新的 continuation 回到第一类 task scheduler。

---

## 12. poller 是“进度源”，不是传统意义上的 callback list

poller 的接口只有四个核心动作：

~~~cpp
struct pollfn {
    virtual ~pollfn() {}

    virtual bool poll() = 0;
    virtual bool pure_poll() = 0;

    virtual bool try_enter_interrupt_mode() = 0;
    virtual void exit_interrupt_mode() = 0;
};
~~~

可以把它拆成两个维度：

~~~text
normal progress
  poll()

idle detection
  pure_poll()

sleep prepare
  try_enter_interrupt_mode()

wake rollback / resume
  exit_interrupt_mode()
~~~

这比单纯的“`poll()` 返回 ready”多了一整套睡眠协议。

Reactor 正常轮询时：

~~~cpp
bool reactor::poll_once() {
    bool work = false;
    for (auto c : _pollers) {
        work |= c->poll();
    }
    return work;
}
~~~

因此一次 loop 会让多个 subsystem 都获得 progress 机会，而不是遇到第一个有工作者就 short-circuit。

---

## 13. poller 顺序为什么是运行时数据流的一部分

固定版本在 `do_run()` 里直接说明 poller 的顺序会影响性能，因为前一个 poller 产生的工作可能立刻喂给后一个 poller。

核心创建顺序是：

~~~cpp
poller smp_poller(
    std::make_unique<smp_pollfn>(*this));

poller reap_kernel_completions_poller(
    std::make_unique<reap_kernel_completions_pollfn>(*this));

poller io_queue_submission_poller(
    std::make_unique<io_queue_submission_pollfn>(*this));

poller kernel_submit_work_poller(
    std::make_unique<kernel_submit_work_pollfn>(*this));

poller final_real_kernel_completions_poller(
    std::make_unique<reap_kernel_completions_pollfn>(*this));

poller batch_flush_poller(
    std::make_unique<batch_flush_pollfn>(*this));

poller execution_stage_poller(
    std::make_unique<execution_stage_pollfn>());

// ...

poller syscall_poller(
    std::make_unique<syscall_pollfn>(*this));

poller drain_cross_cpu_freelist(
    std::make_unique<drain_cross_cpu_freelist_pollfn>());

poller expire_lowres_timers(
    std::make_unique<lowres_timer_pollfn>(*this));

poller sig_poller(
    std::make_unique<signal_pollfn>(*this));
~~~

其中最典型的流水线是：

~~~text
SMP poller
  收到 remote I/O request
        |
        v
I/O queue submission poller
  发现可以提交
        |
        v
kernel submit poller
  提交到 backend
        |
        v
second completion reap
  处理可能立即完成的事件
~~~

如果把 I/O submission 放在 SMP poller 前面，那么本轮刚收到的跨 shard 请求要等下一轮才能提交，凭空增加一个 event-loop turn 的延迟。

所以 poller 顺序不是“把 vector 怎么排都行”，而是隐含了一条 runtime pipeline。

---

## 14. `pure_poll()` 并不等于数学意义上的“无副作用”

旧式解释常把：

~~~text
poll()      = 真正执行工作
pure_poll() = 只看，不做任何事
~~~

说得太绝对。

接口注释确实说 `pure_poll()` 用来检查是否需要工作，而不真正做业务工作，但固定实现里有更细的边界。

例如 kernel completion poller：

~~~cpp
virtual bool pure_poll() override final {
    return poll();
    // actually performs work,
    // but triggers no user continuations, so okay
}
~~~

I/O submission poller 也直接：

~~~cpp
virtual bool pure_poll() override final {
    return poll();
}
~~~

所以更准确的理解是：

> `pure_poll()` 必须对“idle handler 正在判断能否休眠”这一场景安全；它不能偷偷推进会让 idle handler 自身状态假设失效的用户 continuation，但某些底层维护工作可以发生。

为什么需要这个区别？

主循环空闲时会调用用户可配置的 idle CPU handler：

~~~cpp
auto handler_result =
    _idle_cpu_handler(pure_check_for_work);
~~~

源码明确说明不能把普通 `check_for_work()` 交进去，因为普通 poll 可能直接运行会改变 idle handler 状态的 task。

因此 `pure` 描述的是**相对于 Reactor idle protocol 的安全性**，不是 C++ 函数式编程意义上的 referential transparency。

---

## 15. poller 为什么注册自己时也要先变成一个 task

`poller` 对象构造后，并没有直接：

~~~cpp
engine()._pollers.push_back(this);
~~~

固定实现反而创建 registration task：

~~~cpp
void poller::do_register() noexcept {
    // We may be running inside a poller ourselves and therefore
    // in the middle of iterating reactor::_pollers.
    auto task = new registration_task(this);

    engine().add_task(task);
    _registration_task = task;
}
~~~

原因非常具体：

Reactor 此刻可能正在执行：

~~~cpp
for (auto c : _pollers) {
    work |= c->poll();
}
~~~

如果某个 `poll()` 的执行过程里创建了新的 poller，并直接修改 `_pollers`，就可能使当前 vector iteration 的 iterator/reference 失效。

Seastar 的解决思路不是再加一把“poller registry mutex”，而是：

~~~text
poller construction
   |
   v
schedule registration task
   |
   v
current poll_once() finishes
   |
   v
Reactor returns to task scheduler
   |
   v
registration task mutates _pollers
~~~

这再次利用了单 owner event loop 的串行化能力。

---

## 16. poller 析构为什么比注册更难

删除更危险，因为当前 `poll_once()` 可能已经拿到了这个 `pollfn*`。

固定版本的析构逻辑：

~~~cpp
poller::~poller() {
    if (_pollfn) {
        if (_registration_task) {
            _registration_task->cancel();
        } else if (!engine()._finished_running_tasks) {
            auto dummy = make_pollfn([] {
                return false;
            });

            auto dummy_p = dummy.get();

            auto task =
                new deregistration_task(std::move(dummy));

            engine().add_task(task);
            engine().replace_poller(
                _pollfn.get(), dummy_p);
        }
    }
}
~~~

这里有两层保护。

### 情况一：注册 task 还没跑

那 poller 还没有进入 `_pollers`，直接取消 registration 即可。

### 情况二：已经注册

不能先 delete `_pollfn` 再等未来某个 task 从 vector 删除，因为当前 iteration 可能还会访问这个地址。

所以先：

~~~text
live pollfn pointer
      |
      v
replace with dummy pollfn
      |
      v
future deregistration task
      |
      v
remove dummy safely
~~~

这是一个非常典型的 event-loop lifetime 技巧：

**先让共享 registry 中的可见对象变成安全占位符，再延迟真正结构修改。**

它解决的不是锁竞争，而是 iteration lifetime。

---

## 17. 从 polling 进入 sleeping，真正难的是 lost wakeup

如果 Reactor 发现：

~~~text
task queue empty
pollers report no work
~~~

最朴素的实现可能是：

~~~text
if no_work:
    backend.wait()
~~~

但这里存在经典竞态：

~~~text
Reactor / CPU 0                      CPU 1
---------------                      -----
check queue: empty
                                     enqueue work
                                     sees CPU 0 not sleeping?
                                     decide not to wake
enter sleep
...
forever waiting
~~~

问题在于“确认没有工作”和“宣布我已经可以被唤醒”不是一个原子步骤。

Seastar 的 `try_sleep()` 因此先让**每一个 poller**进入 interrupt mode：

~~~cpp
void reactor::try_sleep() {
    for (auto i = _pollers.begin();
         i != _pollers.end();
         ++i) {

        auto ok = (*i)->try_enter_interrupt_mode();

        if (!ok) {
            while (i != _pollers.begin()) {
                (*--i)->exit_interrupt_mode();
            }
            return;
        }
    }

    _backend->wait_and_process_events(
        &_active_sigmask);

    for (auto i = _pollers.rbegin();
         i != _pollers.rend();
         ++i) {
        (*i)->exit_interrupt_mode();
    }
}
~~~

如果任何一个 subsystem 说“现在不能安全睡”，Reactor 会把前面已经进入 interrupt mode 的 poller 按反向顺序退出，然后放弃休眠。

它很像一个 prepare / rollback 协议：

~~~text
prepare poller A
prepare poller B
prepare poller C
      |
      +-- C says no
             |
             v
rollback B
rollback A
return to polling
~~~

只有所有 progress source 都能保证“未来事件会把我唤醒”时，backend 才真正 wait。

---

## 18. SMP poller 如何堵住“刚检查完就来消息”的竞态

跨核 message 是最典型的 lost-wakeup 风险。

目标 Reactor 的 SMP poller 进入 interrupt mode 时：

~~~cpp
virtual bool try_enter_interrupt_mode() override {
    _r._sleeping.store(
        true,
        std::memory_order_relaxed);

    bool barrier_done =
        try_systemwide_memory_barrier();

    if (!barrier_done) {
        _r._sleeping.store(
            false,
            std::memory_order_relaxed);
        return false;
    }

    if (poll()) {
        // raced
        _r._sleeping.store(
            false,
            std::memory_order_relaxed);
        return false;
    }

    return true;
}

virtual void exit_interrupt_mode() override final {
    _r._sleeping.store(
        false,
        std::memory_order_relaxed);
}
~~~

顺序非常重要：

~~~text
1. 先发布 sleeping=true
2. 做 systemwide barrier 协调
3. 再 poll 一次跨核队列
4. 确认没有 race 后才允许真正 sleep
~~~

发送方把 item push 进跨核 queue 后：

~~~cpp
void smp_message_queue::lf_queue::maybe_wakeup() {
    std::atomic_signal_fence(
        std::memory_order_seq_cst);

    remote->wakeup();
}
~~~

目标 Reactor 的 `wakeup()`：

~~~cpp
void reactor::wakeup() {
    if (!_sleeping.load(
            std::memory_order_relaxed)) {
        return;
    }

    _sleeping.store(
        false,
        std::memory_order_relaxed);

    uint64_t one = 1;
    auto res = ::write(
        _notify_eventfd.get(),
        &one,
        sizeof(one));

    SEASTAR_ASSERT(
        res == sizeof(one));
}
~~~

于是竞态被改造成两个可接受结果。

### 情况 A：消息先被二次 poll 看见

~~~text
sleeping=true
barrier
remote push
target poll() sees work
try_enter_interrupt_mode() returns false
Reactor does not sleep
~~~

### 情况 B：Reactor 已经进入可唤醒状态

~~~text
sleeping=true
barrier
target sees no work
remote push
remote wakeup() sees sleeping=true
eventfd write
backend wait is interrupted/woken
~~~

真正不能发生的是：

~~~text
remote 认为 target 不需要 wake
+
target 又在消息到达后进入不可唤醒睡眠
~~~

这就是 sleep protocol 里 systemwide barrier、`_sleeping` 和再次 poll 同时存在的原因。

---

## 19. 为什么 `wakeup()` 不是每次跨核 push 都写 eventfd

`wakeup()` 开头先检查：

~~~cpp
if (!_sleeping.load(std::memory_order_relaxed)) {
    return;
}
~~~

如果目标 Reactor 正在正常 polling，就没有必要每个跨核 batch 都触发一次系统调用。目标 CPU 很快会在 SMP poller 中看到 queue。

只有目标进入 sleeping handshake 后，发送方才需要 eventfd 把它从 backend wait 拉回来。

因此跨核通知路径有两种成本：

~~~text
target awake
  push lock-free queue
  target normal poll sees it
  no eventfd syscall

target sleeping
  push queue
  wakeup()
  eventfd write
  backend wakes
~~~

这正是 busy-poll runtime 常见的设计目标：**活跃态尽量不为 wakeup 付系统调用成本，休眠态才启用显式通知。**

---

## 20. idle handler 与 `max_poll_time`：低延迟和 CPU 占用怎样切换

主循环没有在第一次发现 idle 时立刻睡。

固定版本先记录 idle，并运行 idle handler。如果 handler 认为没有更多低优先级工作，Reactor 仍会继续短暂 polling。只有 idle 时间超过 `max_poll_time` 才进入真正 sleep：

~~~cpp
if (go_to_sleep) {
    internal::cpu_relax();

    if (idle_end - idle_start
            > _cfg.max_poll_time) {

        struct itimerspec zero_itimerspec = {};
        _task_quota_timer.timerfd_settime(
            0, zero_itimerspec);

        _cpu_stall_detector->start_sleep();
        try_sleep();
        _cpu_stall_detector->end_sleep();

        idle_end = now();

        _task_quota_timer.timerfd_settime(
            0, task_quote_itimerspec);
    }
}
~~~

这里的性能取舍是：

~~~text
短暂空闲
  -> continue polling
  -> 更高 CPU 占用
  -> 更低事件发现延迟

持续空闲
  -> try_sleep()
  -> 降低 CPU 占用
  -> 需要 interrupt-mode/wakeup 协议
~~~

因此 `max_poll_time` 不只是“省电参数”，它直接参与 tail latency 与 CPU utilization 的交换。

---

## 21. Timer、I/O、Signal 为什么都需要自己的 interrupt-mode 逻辑

不同 progress source 无法用同一种“睡前检查”。

### Low-resolution timer

如果最近一个低精度 timer 未来会到期，sleep 前要用可唤醒的 high-resolution timer 把它代理出来：

~~~cpp
virtual bool try_enter_interrupt_mode() override {
    auto next = _r._lowres_next_timeout;

    if (next == lowres_clock::time_point::max()) {
        return true;
    }

    auto now = lowres_clock::now();

    if (next <= now) {
        return false;
    }

    _nearest_wakeup.arm(next - now);
    _armed = true;
    return true;
}
~~~

### I/O queue submission

如果延迟调度的 I/O 已经到了提交时间，就不能睡；否则先 arm 一个唤醒 timer：

~~~cpp
auto next = _r.next_pending_aio();
auto now = steady_clock_type::now();

if (next <= now) {
    return false;
}

_nearest_wakeup.arm(next);
_armed = true;
return true;
~~~

### Signal

Signal poller 会先调整 signal mask，再重新检查是否已经 race 到 signal；如果发生 race，则撤销 interrupt mode 并返回 normal loop。

这说明 poller interface 的价值不只是“插件化一组 poll()”。

它真正统一的是：

> 每一种事件源都必须回答：**现在有没有工作？如果我要睡，你怎样保证下一次事件一定能唤醒我？**

---

## 22. 为什么 cross-CPU free poller 可以“不主动 wake”

cross-shard memory reclaim 的策略恰好相反。源码明确说 foreign CPU 把待释放对象排给 owner 后，不会因为这个动作专门唤醒 owner：

~~~cpp
class reactor::drain_cross_cpu_freelist_pollfn final
        : public simple_pollfn<true> {
public:
    virtual bool poll() final override {
        return memory::drain_cross_cpu_freelist();
    }
};
~~~

原因是 free 本身不是需要低延迟完成的业务事件。对象已经逻辑死亡，延迟 reclaim 只影响内存回收时机。

所以：

~~~text
SMP request
  需要目标尽快执行
  -> sleeping target must be woken

cross-CPU free
  延迟 reclaim 通常没有业务副作用
  -> 可以等目标因别的事件醒来再 drain
~~~

这说明“是否需要 wakeup”不是 lock-free queue 的固有属性，而是由**事件的 progress requirement**决定。

---

## 23. Reactor 并不把所有阻塞系统调用都塞进主线程

`syscall_pollfn` 用来接收 syscall work queue 的完成：

~~~cpp
class reactor::syscall_pollfn final
        : public reactor::pollfn {
    reactor& _r;

public:
    virtual bool poll() final override {
        return _r._thread_pool->complete();
    }

    virtual bool pure_poll() override final {
        return poll();
    }

    virtual bool try_enter_interrupt_mode() override {
        _r._thread_pool->enter_interrupt_mode();

        if (poll()) {
            _r._thread_pool->exit_interrupt_mode();
            return false;
        }

        return true;
    }

    virtual void exit_interrupt_mode() override final {
        _r._thread_pool->exit_interrupt_mode();
    }
};
~~~

因此 shard-per-core 不能被误解成“进程里只有这些 Reactor threads”。

更准确地说：

- Reactor thread 是异步 runtime 的主执行 owner；
- 真正会阻塞的工作可以转移到辅助线程；
- 辅助线程完成后通过 completion queue 回到 Reactor；
- 用户 continuation 仍然在目标 shard 的 Reactor 上继续。

这样可以避免一个 blocking syscall 把整个 shard 的 cooperative scheduler 卡死。

---

## 24. Reactor 的 main loop 为什么先跑 task，再 poll

固定循环每轮先：

~~~cpp
run_some_tasks();
~~~

然后才：

~~~cpp
poll_once();
~~~

这意味着已经 ready 的 continuation 会优先获得一段 task quota；quota 到期后再回到 I/O/SMP/timer progress source。

如果反过来一直 poll 到没有事件才运行 task，高 I/O 负载下 ready continuation 可能长期饥饿；如果只跑 task 不定期 poll，I/O completion 又无法进入 task system。

所以 task quota 实际是在两类 progress 之间建立边界：

~~~text
CPU-side continuation progress
         |
         | task quota / need_preempt
         v
I/O / SMP / timer progress
         |
         v
new continuations
         |
         +--------> task queues
~~~

这也是 cooperative preemption 在 Reactor 架构里的真正位置：它不负责“两个函数谁抢占谁”，而是负责**什么时候从 task execution phase 返回到 event progress phase**。

---

## 25. Shutdown 不能直接跳出 event loop

`_stopped` 变成 true 后，Reactor 并没有马上 `break`。

固定版本：

~~~cpp
if (_stopped) {
    load_timer.cancel();

    // Final tasks may include sending the last response to cpu 0,
    // so run them
    while (have_more_tasks()) {
        run_some_tasks();
    }

    while (!_at_destroy_tasks->_q.empty()) {
        run_tasks(*_at_destroy_tasks);
    }

    _finished_running_tasks = true;

    _smp->arrive_at_event_loop_end();

    if (_id == 0) {
        _smp->join_all();
    }

    break;
}
~~~

原因就在注释里：最后的 task 仍可能承担跨 shard completion，例如把“我已经停止”的响应送回 CPU 0。

如果直接：

~~~text
_stopped = true
break
destroy reactor
~~~

就可能留下：

- 已进入 local task queue 的 completion；
- 等待 CPU 0 的 shutdown response；
- at-destroy work；
- 仍假设 event loop 会继续推进的生命周期操作。

所以 shutdown 自己也是 runtime protocol，而不是一个 bool。

---

## 26. `_finished_running_tasks` 还是 poller 生命周期的一道边界

前面看到 poller destructor 会通过 task 延迟注销。但 shutdown drain 结束后：

~~~cpp
_finished_running_tasks = true;
~~~

此后再析构 poller，就不能继续“schedule 一个 deregistration task”，因为 event loop 已经不会再执行 task。

固定 poller 析构因此有：

~~~cpp
else if (!engine()._finished_running_tasks) {
    // schedule deregistration...
}
~~~

这说明 shutdown 的关键不是“线程是否马上销毁”，而是 runtime 逐步关闭自己的能力：

~~~text
normal
  can schedule tasks
  can register/deregister pollers
  can process cross-shard completion

stopping
  drain remaining work

finished_running_tasks
  no future task execution allowed
  destructors must not depend on scheduling

event loop end
  shard synchronization / join
~~~

这类分阶段能力收缩，比一个 `running=false` 更容易推理。

---

## 27. Reactor 中真正需要原子与 barrier 的地方恰好揭示了架构边界

如果只看 local task queue，会产生“Seastar 几乎不用同步”的错觉。

实际上固定版本仍大量使用：

- `std::atomic`；
- SPSC / lock-free queues；
- eventfd；
- systemwide memory barrier；
- semaphore；
- thread-local state；
- backend wait/wakeup；
- cross-shard completion protocol。

只是这些同步原语主要出现在：

~~~text
shard A <----> shard B
Reactor <----> blocking worker
Reactor <----> kernel/backend
running <----> sleeping
normal <----> shutdown
~~~

而不是散布在每一次本地 task pop/push 上。

这就是 shard-per-core 的真正收益：

> 不是消灭并发，而是把并发集中在边界，把 shard 内部恢复成更容易推理的顺序程序。

---

## 28. 对机器人 runtime 最值得迁移的不是“每核一个线程”本身

直接照搬 shard-per-core 并不一定适合所有机器人软件。摄像头、驱动、控制器、GPU runtime、ROS/DDS callback 的约束各不相同。

真正值得迁移的是下面几条设计判断。

### 28.1 先定义 owner，再选择容器

如果一个队列明确只允许控制线程消费，也只允许控制线程内部直接修改，那么本地容器可以保持简单；其他线程通过 command queue 交接。

不要先选 MPMC queue，再让整个系统围着“任何线程都可以碰任何对象”生长。

### 28.2 把 cross-domain handoff 做成显式协议

Seastar 把跨 shard 调用变成 SMP message，把外部线程注入变成 alien queue，把跨核 free 变成 owner-side reclaim。

机器人 runtime 也可以显式区分：

~~~text
sensor thread -> estimator owner
planner shard -> controller owner
GPU completion -> CPU owner
telemetry thread -> logger owner
~~~

而不是共享一堆 mutable singleton。

### 28.3 Cooperative scheduling 必须配套 stall 约束

如果采用单线程事件循环，任何长时间同步计算都会放大同 shard 所有任务的 jitter。

因此需要同时设计：

- task quota；
- yield boundary；
- stall detector；
- scheduling groups / priorities；
- blocking work offload；
- overload/backlog policy。

只学“单线程无锁”而不学这些约束，系统会在高负载时比传统线程池更脆弱。

### 28.4 睡眠协议必须回答 lost-wakeup

“队列为空就 sleep”不是完整设计。

必须能回答：

1. 谁先宣布 sleeping？
2. producer 怎样知道 consumer 需要 wake？
3. queue publish 与 sleeping flag 之间的内存序是什么？
4. consumer 在真正阻塞前是否重新检查 work？
5. wake signal 是否具有记忆性？
6. 进入 interrupt mode 失败时怎样 rollback？

Seastar 的 SMP poller 把这些问题全部显式化了。

### 28.5 Shutdown 必须保留 progress

如果关闭过程还需要：

- completion 回传；
- worker join；
- buffer 归还；
- callback 静默；
- remote owner reclaim；

那就不能在第一个 stop flag 出现时直接停止 scheduler。

---

## 29. 把一条消息完整跑过 Reactor

最后把前几章连成一条时间线。

假设 shard 0 要在 shard 1 上执行一个 continuation：

~~~text
shard 0
  |
  | smp::submit_to(1, work)
  v
SMP queue 0 -> 1
  |
  | maybe_wakeup()
  |  ├─ shard 1 awake: no syscall
  |  └─ shard 1 sleeping: eventfd wake
  v
shard 1 Reactor
  |
  | smp_pollfn::poll()
  v
process_queue()
  |
  | create/schedule target task
  v
reactor::add_task()
  |
  v
task_queue::_q
  |
  | run_some_tasks()
  | select active scheduling group
  v
run_tasks()
  |
  | task::run_and_dispose()
  v
user continuation
  |
  | produces ready future / next continuation
  +------------------------> local task queue
~~~

如果 continuation 发起异步 I/O：

~~~text
continuation
  |
  v
I/O queue
  |
  v
io_queue_submission_pollfn
  |
  v
kernel_submit_work_pollfn
  |
  v
kernel/backend
  |
  v
reap_kernel_completions_pollfn
  |
  v
future becomes ready
  |
  v
new task
  |
  +------> run_some_tasks()
~~~

到这里，Reactor 的角色才完整：

**它不是把 callback 放进 epoll 之后调用一下，而是在一个 shard 内把 task scheduling、I/O progress、cross-core ingress、timer、signal、sleep/wakeup 和 shutdown 统一成同一个执行所有权模型。**

---

## 30. 阅读下一章前应该保留的对象图

~~~text
                          cross-shard producers
                                  |
                         SMP / alien / reclaim
                                  |
                                  v
+-------------------------------------------------------------+
|                  one shard / one Reactor                     |
|                                                             |
|  task_queue[sg] -> active queues -> run_some_tasks()         |
|         |                              |                     |
|         |                              v                     |
|         |                        run_tasks()                  |
|         |                              |                     |
|         |                      task::run_and_dispose()        |
|         |                              |                     |
|         +<------ future continuation <-+                     |
|                                                             |
|  pollers                                                    |
|    |- SMP ingress                                            |
|    |- kernel completion                                      |
|    |- I/O submission                                         |
|    |- timers                                                 |
|    |- signals                                                |
|    |- syscall completion                                     |
|    `- cross-CPU reclaim                                      |
|                                                             |
|  idle                                                        |
|    pure_poll -> interrupt-mode prepare -> backend wait        |
|                    ^                         |                |
|                    |---- eventfd wakeup ------|                |
+-------------------------------------------------------------+
~~~

下一篇 [Scheduling Group 与 vruntime](scheduling-groups-vruntime.md) 会继续回答一个现在已经无法回避的问题：

**既然所有 ready continuation 最终都回到同一个 Reactor thread，那么多个业务类别怎样分配这一颗 CPU 的时间，`_shares`、`_vruntime`、task quota 和 starvation accounting 又怎样共同工作？**
