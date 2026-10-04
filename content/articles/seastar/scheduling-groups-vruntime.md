# Scheduling Group：一个 Reactor 只有一条线程，怎样隔离 CPU 份额、延迟与 Backlog

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

[Reactor 主循环](reactor-shard-per-core.md)已经回答了一个基础问题：一个 shard 上的 runnable continuation 最终都会变成 task，由同一个 Reactor thread 执行。

但单线程并不会自动带来公平性。假设同一颗 CPU 上同时有：

~~~text
latency-sensitive RPC
perception post-process
metrics
background compaction
~~~

如果所有 continuation 只进入一个全局 FIFO，那么某个组件只要制造更多 ready continuation，就会隐式得到更多 CPU。这意味着“内部并发度”会偷偷变成“业务 CPU 权重”。

Seastar 的 scheduling group 就是在解决这个问题。它不是 OS 线程优先级，也不是额外线程池，而是 Reactor 内部的一组：

~~~text
独立 runnable queue
+
CPU shares
+
vruntime 记账
+
cooperative preemption
+
runtime / wait / starvation observability
~~~

完整路径是：

~~~text
business component
      |
      v
scheduling_group id
      |
      v
per-shard task_queue
      |
      +-- shares
      +-- vruntime
      +-- runnable tasks
      |
      v
active task queues
      |
      v
select low vruntime group
      |
      v
run cooperative batch
      |
      v
measure actual elapsed CPU time
      |
      v
vruntime += runtime / shares
      |
      +------> reinsert if backlog remains
~~~

---

## 1. 一个全局 FIFO 为什么会把并发度误当成 CPU 权重

同一固定版本的教程用一个非常直接的反例解释这个问题：一个异步循环可以启动不同数量的并发 continuation。

~~~cpp
seastar::future<long>
loop(int parallelism, bool& stop) {
    return seastar::do_with(
        0L,
        [parallelism, &stop] (long& counter) {
            return seastar::parallel_for_each(
                std::views::iota(0u, parallelism),
                [&stop, &counter] (unsigned) {
                    return seastar::do_until(
                        [&stop] { return stop; },
                        [&counter] {
                            ++counter;
                            return seastar::make_ready_future<>();
                        });
                }).then([&counter] {
                    return counter;
                });
        });
}
~~~

如果组件 A 使用 `parallelism=1`，它大致持续保有 1 条 runnable continuation；组件 B 使用 `parallelism=10`，它大致持续保有 10 条。

单 FIFO 看到的是：

~~~text
A0
B0 B1 B2 B3 B4 B5 B6 B7 B8 B9
A1
B10 B11 B12 ...
~~~

scheduler 并不知道这些 B task 属于同一个“后台业务”。它只知道有 11 个 runnable execution nodes。

因此：

~~~text
更多 runnable nodes
        |
        v
更多 FIFO slots
        |
        v
更多 CPU execution opportunities
~~~

这暴露出一个错误的资源模型：

> runnable task 数量不应该成为业务 CPU 权重。

一个模块把并发度从 1 改成 100，不应该顺便把别的模块 CPU 预算压缩约 100 倍。

Scheduling group 的第一层作用，就是把公平单位从单个 continuation 提升到 component/group。

---

## 2. Scheduling Group 本身只是一个全局 ID

接口层的 `scheduling_group` 并不保存 queue：

~~~cpp
class scheduling_group {
    unsigned _id;

private:
    explicit scheduling_group(unsigned id) noexcept
        : _id(id) {}

public:
    constexpr scheduling_group() noexcept
        : _id(0) {}

    bool active() const noexcept;
    const sstring& name() const noexcept;
    const sstring& short_name() const noexcept;

    void set_shares(float shares) noexcept;
    float get_shares() const noexcept;

    future<> update_io_bandwidth(
        uint64_t bandwidth) const;

    // ...
};
~~~

因此：

~~~text
scheduling_group handle
       =
small identity
       !=
OS thread
       !=
shared global runqueue
~~~

真正的 task queue 存在每个 Reactor 内部：

~~~cpp
std::array<
    std::unique_ptr<task_queue>,
    max_scheduling_groups()
> _task_queues;
~~~

同一个 group ID 在多个 shard 上的关系是：

~~~text
              scheduling_group id = 5
                        |
          +-------------+-------------+
          |             |             |
          v             v             v
       shard 0        shard 1       shard 2
       Reactor        Reactor       Reactor
          |             |             |
          v             v             v
    task_queue[5]  task_queue[5] task_queue[5]
       local          local         local
~~~

它采用的是：

**global identity + per-shard execution state**。

---

## 3. 为什么创建 Group 必须是异步全局协议

固定实现先分配统一 ID，再让所有 Reactor 建立本地 group：

~~~cpp
future<scheduling_group>
create_scheduling_group(
        sstring name,
        sstring shortname,
        float shares) noexcept {

    auto aid = allocate_scheduling_group_id();

    if (aid < 0) {
        return make_exception_future<scheduling_group>(
            std::runtime_error(
                fmt::format(
                    "Scheduling group limit exceeded while creating {}",
                    name)));
    }

    auto id = static_cast<unsigned>(aid);
    SEASTAR_ASSERT(id < max_scheduling_groups());

    auto sg = scheduling_group(id);

    return smp::invoke_on_all(
        [sg, name, shortname, shares] {
            return engine().init_scheduling_group(
                sg, name, shortname, shares);
        }).then([sg] {
            return make_ready_future<scheduling_group>(sg);
        });
}
~~~

为什么不能只在调用者 shard 建 queue？

因为 task 可以跨 shard：

~~~text
shard 0
  task belongs to SG 5
       |
       v
smp::submit_to(3, ...)
       |
       v
shard 3
  continuation must still belong to SG 5
~~~

如果 shard 3 没有 `task_queue[5]`，group identity 就无法跨 shard保持一致。

因此 create future ready 的语义是：

~~~text
allocate global SG id
      |
      v
invoke_on_all()
      |
      v
each Reactor creates local task_queue/state
      |
      v
group identity becomes globally usable
~~~

---

## 4. 每个 Shard 创建的不只是 Queue

单个 Reactor 的初始化：

~~~cpp
future<>
reactor::init_scheduling_group(
        scheduling_group sg,
        sstring name,
        sstring shortname,
        float shares) {

    return with_shared(
        _scheduling_group_keys_mutex,
        [this, sg,
         name = std::move(name),
         shortname = std::move(shortname),
         shares] {

            get_sg_data(sg).queue_is_initialized = true;

            _task_queues[sg._id] =
                std::make_unique<task_queue>(
                    sg._id,
                    name,
                    shortname,
                    shares);

            return with_scheduling_group(
                sg,
                [this, sg] {
                    auto& sg_data =
                        _scheduling_group_specific_data;

                    auto& this_sg =
                        get_sg_data(sg);

                    for (const auto& [key_id, cfg]
                         : sg_data.scheduling_group_key_configs) {

                        this_sg.specific_vals.resize(
                            std::max<size_t>(
                                this_sg.specific_vals.size(),
                                key_id + 1));

                        this_sg.specific_vals[key_id] =
                            allocate_scheduling_group_specific_data(
                                sg, key_id, cfg);
                    }
                });
        });
}
~~~

因此 scheduling group 还是一个 execution context：

~~~text
scheduling_group
  |
  +-- local runnable queue
  +-- CPU shares
  +-- scheduler metrics
  +-- I/O priority mapping
  +-- group-specific values
  +-- current execution context
~~~

CPU fairness 是主线，但它不是一个只有 `shares` 数字的轻量标签。

---

## 5. Current Scheduling Group 是 Thread-local Context

固定版本保存当前 group 的方式：

~~~cpp
inline
scheduling_group*
current_scheduling_group_ptr() noexcept {
    static thread_local scheduling_group sg;
    return &sg;
}

inline
scheduling_group
current_scheduling_group() noexcept {
    return *internal::current_scheduling_group_ptr();
}

inline
bool
scheduling_group::active() const noexcept {
    return *this == current_scheduling_group();
}
~~~

因此 `current_scheduling_group()` 回答的是：

> 当前 Reactor 正以哪个调度上下文执行这段代码？

Reactor 开始运行某个 queue 时：

~~~cpp
void reactor::run_tasks(task_queue& tq) {
    *internal::current_scheduling_group_ptr()
        = scheduling_group(tq._id);

    // execute tasks...
}
~~~

`run_some_tasks()` 完成后会把 current group 恢复为 default，防止 runtime 后续代码意外继承最后一个业务 group。

---

## 6. `with_scheduling_group()` 不是简单改 TLS

固定实现先判断目标 group 是否已经 active：

~~~cpp
template <typename Func, typename... Args>
auto
with_scheduling_group(
        scheduling_group sg,
        Func func,
        Args&&... args) noexcept {

    using return_type =
        decltype(
            func(
                std::forward<Args>(args)...));

    using futurator =
        futurize<return_type>;

    if (sg.active()) {
        return futurator::invoke(
            func,
            std::forward<Args>(args)...);
    } else {
        return internal::schedule_in_group(
            sg,
            [func = std::move(func),
             args = std::make_tuple(
                 std::forward<Args>(args)...)] () mutable {

                return futurator::apply(
                    func,
                    std::move(args));
            });
    }
}
~~~

如果：

~~~text
current SG == target SG
~~~

直接 inline invoke。

如果：

~~~text
current SG != target SG
~~~

则真正创建一个目标 group task：

~~~cpp
template <typename Func>
auto
schedule_in_group(
        scheduling_group sg,
        Func func) noexcept {

    auto tsk =
        make_task(
            sg,
            std::move(func));

    schedule_checked(tsk);

    return tsk->get_future();
}
~~~

所以跨 group 的真实路径是：

~~~text
with_scheduling_group(target)
      |
      v
make_task(target, callable)
      |
      v
schedule_checked()
      |
      v
target task_queue
      |
      v
future completes when target task runs
~~~

这一步才真正建立了 CPU accounting boundary。

---

## 7. Group Context 怎样沿 Continuation 传播

带 group 创建 task：

~~~cpp
template <typename Func>
lambda_task<Func>*
make_task(
        scheduling_group sg,
        Func&& func) noexcept {

    return new lambda_task<Func>(
        sg,
        std::forward<Func>(func));
}
~~~

Reactor 未来运行这个 queue 时：

~~~text
select target queue
      |
      v
current_scheduling_group = target
      |
      v
task::run_and_dispose()
      |
      v
user function
~~~

普通 task 的默认构造又会读取 `current_scheduling_group()`，所以在这个 execution context 内生成的新异步 task 会继承 group。

固定测试还验证了 yield 后 group 保持不变：

~~~cpp
with_scheduling_group(sg, [&] {
    return yield().then([&] {
        BOOST_REQUIRE_EQUAL(
            internal::scheduling_group_index(
                current_scheduling_group()),
            internal::scheduling_group_index(sg));
    });
}).get();
~~~

因此 group 不是“一次函数调用的临时参数”，而是被异步 runtime 持续携带的执行上下文。

---

## 8. Ready Future 的 Inline Fast Path 是一个重要边界

[Future / Continuation](future-continuation-task.md) 已经拆过这一点：release build 中，如果 dependency 已 ready，`then_impl()` 可能直接 inline callback。

固定代码：

~~~cpp
template <
    typename Func,
    typename Result =
        futurize_t<
            internal::future_result_t<Func, T>>>
Result
then_impl(Func&& func) noexcept {
#ifndef SEASTAR_DEBUG
    using futurator =
        futurize<
            internal::future_result_t<Func, T>>;

    if (failed()) {
        return futurator::make_exception_future(
            static_cast<future_state_base&&>(
                get_available_state_ref()));

    } else if (available()) {
        return futurator::invoke(
            std::forward<Func>(func),
            get_available_state_ref()
                .take_value());
    }
#endif

    return then_impl_nrvo<Func, Result>(
        std::forward<Func>(func));
}
~~~

因此并不是每个 `.then()` 都会重新进入 scheduling queue。

如果当前已在 SG A：

~~~text
SG A execution slice
      |
      v
ready future
      |
      v
inline .then()
      |
      v
still inside current SG A slice
~~~

这样能减少 task allocation 和 queue round-trip，但也意味着很长的 ready-future chain 仍依赖 cooperative preemption，不能假设“每个 then 都天然切一次 scheduler”。

---

## 9. 真正的隔离来自 Per-group Runnable Queue

`task_queue` 的核心字段：

~~~cpp
struct task_queue {
    int64_t _vruntime = 0;

    float _shares;

    int64_t
        _reciprocal_shares_times_2_power_32;

    bool _active = false;
    const uint8_t _id;

    sched_clock::time_point _ts;

    sched_clock::duration _runtime = {};
    sched_clock::duration _waittime = {};
    sched_clock::duration _starvetime = {};

    uint64_t _tasks_processed = 0;

    circular_buffer<task*> _q;

    // ...
};
~~~

两个业务组件现在是：

~~~text
SG A:
  A0 A1 A2 ...

SG B:
  B0 B1 B2 B3 B4 B5 B6 B7 B8 ...
~~~

Reactor 不再直接问：

~~~text
哪一个 continuation 在一个全局 FIFO 最前面？
~~~

而是先问：

~~~text
哪一个 scheduling group 现在应该获得 CPU？
~~~

因此 B 把内部并发度从 10 提到 1000，主要增加的是：

~~~text
B.queue_length
~~~

而不是直接生成 1000 个跨越 A 的全局排队位置。

---

## 10. Shares 是 CPU 时间权重，不是 Task 数量权重

创建 queue 时：

~~~cpp
reactor::task_queue::task_queue(
        unsigned id,
        sstring name,
        sstring shortname,
        float shares)
    : _shares(
        std::max(shares, 1.0f))
    , _reciprocal_shares_times_2_power_32(
        (uint64_t(1) << 32)
        / _shares)
    , _id(id)
    , _ts(now()) {

    rename(name, shortname);
}
~~~

运行时预先计算：

~~~text
2^32 / shares
~~~

热点路径就可以用整数乘法和移位近似除法。

转换 virtual runtime：

~~~cpp
int64_t
reactor::task_queue::to_vruntime(
        sched_clock::duration runtime) const {

    auto scaled =
        (runtime.count()
         * _reciprocal_shares_times_2_power_32)
        >> 32;

    return std::max<int64_t>(
        scaled,
        0);
}
~~~

第一性近似：

\[
\Delta v
\approx
\frac{\Delta t}{w}
\]

其中：

- \(\Delta t\)：实际 CPU elapsed runtime；
- \(w\)：shares；
- \(\Delta v\)：virtual runtime 增量。

shares 越大，同样真实 CPU 时间产生的 vruntime 增量越小。

---

## 11. 一个数值例子：为什么 100 Shares 对 25 Shares 接近 4:1

假设两个 group 都始终有 backlog：

~~~text
SG A shares = 100
SG B shares = 25
~~~

如果二者都真实运行 1 ms：

\[
\Delta v_A
\propto
\frac{1}{100}
\]

\[
\Delta v_B
\propto
\frac{1}{25}
\]

于是：

\[
\Delta v_B
\approx
4\Delta v_A
\]

B 每消耗相同真实 CPU 时间，virtual runtime “变贵”速度约为 A 的 4 倍。

为了让两者长期处于相近的 scheduler frontier，A 需要获得更多真实 runtime。

理想持续饱和情况下：

\[
\frac{T_A}{T_B}
\approx
\frac{100}{25}
=
4
\]

所以 shares 表达的是：

> 长期 CPU bandwidth ratio。

不是“每 100 个 callback 跑 25 个 callback”这种 task-count ratio。

---

## 12. 为什么必须按 Actual Runtime 收费

假设：

~~~text
SG A:
  one task = 50 us

SG B:
  one task = 1 ms
~~~

按 task count 轮转：

~~~text
A executes 1 task
B executes 1 task
~~~

看起来 1:1，CPU 时间却是：

~~~text
A = 50 us
B = 1000 us
~~~

相差 20 倍。

固定 `run_some_tasks()` 直接测每次 group batch 的 elapsed time：

~~~cpp
auto t_run_started =
    t_run_completed;

task_queue* tq =
    pop_active_task_queue(
        t_run_started);

run_tasks(*tq);

t_run_completed = now();

auto delta =
    t_run_completed
    - t_run_started;

account_runtime(
    *tq,
    delta);
~~~

收费函数：

~~~cpp
void
reactor::account_runtime(
        task_queue& tq,
        sched_clock::duration runtime) {

    if (
        runtime
        >
        (2 * _cfg.task_quota)) {

        _stalls_histogram.add(
            runtime);

        tq._time_spent_on_task_quota_violations
            +=
            runtime
            - _cfg.task_quota;
    }

    tq._vruntime +=
        tq.to_vruntime(runtime);

    tq._runtime +=
        runtime;
}
~~~

所以 scheduler 记的是：

~~~text
实际消耗了多少 CPU 时间
~~~

而不是：

~~~text
执行了多少个 callback
~~~

---

## 13. Active Queue 按 VRuntime 排序

比较器只有一个核心：

~~~cpp
struct reactor::task_queue::indirect_compare {
    bool operator()(
            const task_queue* tq1,
            const task_queue* tq2) const {

        return
            tq1->_vruntime
            <
            tq2->_vruntime;
    }
};
~~~

插入 active queue 时维持这个次序：

~~~cpp
void
reactor::insert_active_task_queue(
        task_queue* tq) {

    tq->_active = true;

    auto& atq =
        _active_task_queues;

    auto less =
        task_queue::indirect_compare();

    if (
        atq.empty()
        ||
        less(
            atq.back(),
            tq)) {

        atq.push_back(tq);

    } else {
        atq.push_front(tq);

        size_t i = 0;

        while (
            i + 1 != atq.size()
            &&
            !less(
                atq[i],
                atq[i + 1])) {

            std::swap(
                atq[i],
                atq[i + 1]);

            ++i;
        }
    }
}
~~~

下一组直接从 front 取：

~~~cpp
reactor::task_queue*
reactor::pop_active_task_queue(
        sched_clock::time_point now) {

    task_queue* tq =
        _active_task_queues.front();

    _active_task_queues.pop_front();

    tq->_starvetime +=
        now
        - tq->_ts;

    return tq;
}
~~~

所以 scheduler 的核心策略可以压缩成：

~~~text
pick lowest vruntime runnable group
~~~

---

## 14. 为什么不用严格 High / Low Priority

严格优先级：

~~~text
high queue not empty
      |
      v
always high
      |
      v
low may never run
~~~

对于后台 compaction、cache maintenance、metrics 这类任务，永久 starvation 往往不可接受。

Shares / vruntime 追求的是：

~~~text
A should get more CPU than B
but B must still make progress
~~~

因此它是 proportional-share fairness，而不是 strict priority。

---

## 15. Sleeping Group 醒来时不能拿着远古低 VRuntime

假设：

~~~text
time 0:
  A vruntime = 100
  B vruntime = 100

B waits on I/O for 10 s

time 10 s:
  A vruntime = 5000
  B vruntime = 100
~~~

如果 B 醒来后直接拿 100 重新进入 active queue，它会获得巨大“历史低价”，可能连续运行很久才能追上 5000。

固定 `activate()` 会校正：

~~~cpp
void
reactor::activate(
        task_queue& tq) {

    if (tq._active) {
        return;
    }

    tq._vruntime =
        std::max(
            _last_vruntime,
            tq._vruntime);

    auto now =
        reactor::now();

    tq._waittime +=
        now
        - tq._ts;

    tq._ts = now;

    _activating_task_queues
        .push_back(&tq);
}
~~~

关键就是：

~~~cpp
tq._vruntime =
    std::max(
        _last_vruntime,
        tq._vruntime);
~~~

含义是：

> I/O-bound group 可以低延迟恢复，但不能积攒无限 sleeper credit。

---

## 16. `_last_vruntime` 是 Scheduler Frontier

每次选中 queue：

~~~cpp
_last_vruntime =
    std::max(
        tq->_vruntime,
        _last_vruntime);
~~~

它形成单调调度前沿：

~~~text
old queue vruntime
       |
       +------\
               max
       +------/
last scheduler frontier
~~~

inactive queue 重新激活时至少对齐到这个 frontier，从而避免 active/inactive 状态切换破坏长期 fairness。

---

## 17. Runtime、Waittime、Starvetime 是三种不同时间

固定 queue 维护：

~~~cpp
sched_clock::duration _runtime = {};
sched_clock::duration _waittime = {};
sched_clock::duration _starvetime = {};
~~~

它们不是三个重复统计量。

### Runtime

`account_runtime()`：

~~~cpp
tq._runtime += runtime;
~~~

代表真正使用 CPU 的时间。

### Waittime

`activate()`：

~~~cpp
tq._waittime +=
    now
    - tq._ts;
~~~

源码 metrics 描述把它解释成 queue 等待某些外部东西，例如 I/O。

这段时间里 queue 通常没有 runnable task。

### Starvetime

`pop_active_task_queue()`：

~~~cpp
tq->_starvetime +=
    now
    - tq->_ts;
~~~

它描述：

> queue 已经 runnable，却还没被 scheduler 选中的等待时间。

因此：

~~~text
waittime
  = waiting for work/event

starvetime
  = work ready, waiting for CPU
~~~

---

## 18. 对机器人系统，这两个 Wait 必须分开

假设控制相关任务晚了 10 ms。

情况 A：

~~~text
queue empty
sensor result not ready
waittime increases
~~~

可能是：

- camera latency；
- driver；
- DMA；
- network；
- upstream estimator。

情况 B：

~~~text
control task already runnable
but another group owns CPU
starvetime increases
~~~

问题是：

- CPU contention；
- shares 太低；
- 某个 group batch 太长；
- task 没及时 yield。

因此：

> data not ready 与 CPU did not schedule ready work 是两个不同故障域。

只看 end-to-end callback latency 无法区分它们。

---

## 19. 固定版本已经把 Scheduler 状态暴露为 Metrics

`task_queue::register_stats()` 注册：

~~~text
runtime_ms
waittime_ms
starvetime_ms
tasks_processed
queue_length
shares
time_spent_on_task_quota_violations_ms
~~~

这些量组合起来能诊断不同 overload。

### Queue Length 上升 + Runtime 很高

~~~text
arrival rate > service capacity
~~~

### Queue Length 上升 + Starvetime 高

~~~text
work is ready
but CPU share / competitors prevent progress
~~~

### Waittime 高 + Queue 常空

~~~text
component mostly waits on upstream event
~~~

### Quota Violation 持续增加

~~~text
cooperative execution slices are too long
~~~

这比一个总 CPU utilization 指标更接近 runtime 根因。

---

## 20. VRuntime 决定“下一次选谁”，Task Quota 决定“什么时候能再选”

即使 vruntime policy 完美，如果某个 group 一进入执行就永远不返回 Reactor，其他 group 还是运行不了。

`run_tasks()` 在 task 之间检查：

~~~cpp
if (
    internal::
    scheduler_need_preempt()) {

    if (
        tasks.size()
        <=
        _cfg.max_task_backlog) {

        break;
    }

    // overload branch...
}
~~~

因此：

~~~text
vruntime
  -> next group policy

cooperative preemption
  -> next scheduling decision opportunity
~~~

最核心的关系是：

> VRuntime 决定“下一次应该选谁”，yield/preemption boundary 决定“什么时候存在下一次”。

---

## 21. 1000 Shares 也抢占不了一个 20 ms 的普通 C++ 函数

假设：

~~~text
control SG shares = 1000
background SG shares = 10
~~~

background 正在执行：

~~~cpp
void bad_task() {
    busy_compute_for_20ms();
}
~~~

中间没有 Future boundary、yield 或 preemption check。

这 20 ms 内 Reactor 拿不回控制权。

因此高 shares 不等于：

~~~text
bounded dispatch latency
~~~

它只能影响下一个 scheduler decision。

这也是 scheduling group 属于 soft real-time isolation，而不是 hard real-time scheduling 的根本原因。

---

## 22. Quota Violation 在测 Cooperative Scheduling 是否失效

`account_runtime()`：

~~~cpp
if (
    runtime
    >
    (2 * _cfg.task_quota)) {

    _stalls_histogram.add(
        runtime);

    tq._time_spent_on_task_quota_violations
        +=
        runtime
        - _cfg.task_quota;
}
~~~

它不是 deadline-miss counter。

它更接近：

> 当前 group 一次连续执行片段明显超过 task quota 后，超出的时间累积了多少。

常见原因：

- callback 同步计算太重；
- loop 没有 yield；
- ready-future chain 太长；
- execution stage batch 太大；
- cooperative boundary 太稀疏。

对于 event-loop runtime，这往往比平均 task latency 更能暴露“为什么整个 shard 都抖了”。

---

## 23. Backlog 太大时，固定版本会改变调度目标

`run_tasks()` 的 overload 分支：

~~~cpp
if (
    internal::
    scheduler_need_preempt()) {

    if (
        tasks.size()
        <=
        _cfg.max_task_backlog) {

        break;

    } else {
        reset_preemption_monitor();
        lowres_clock::update();

        // rate-limited backlog warning
    }
}
~~~

正常情况：

~~~text
quota expired
  -> break
  -> poll / reschedule
~~~

严重 backlog：

~~~text
queue > max_task_backlog
  -> reset preemption
  -> continue draining
~~~

这是一个明确的策略切换：

~~~text
normal load:
  protect latency and fairness

severe overload:
  prioritize draining runaway ready queue
  accept worse short-term latency
~~~

所以 scheduling group 不是 admission control。

如果 producer 长期快于 consumer，shares 不会让 queue 自动变 bounded。

---

## 24. Scheduling Group 隔离 CPU Share，不隔离 Queue Capacity

假设：

~~~text
SG A shares 100
arrival  = 20k tasks/s
service  = 10k tasks/s
~~~

即使系统里没有 SG B：

~~~text
arrival > service
~~~

queue 仍会持续增长。

完整系统还需要：

~~~text
scheduling group
+
bounded queue
+
backpressure
+
admission control
+
freshness / deadline policy
~~~

这与消息中间件里的 HWM 本质类似，只是稀缺资源从网络/缓冲区变成 CPU execution time。

---

## 25. `set_shares()` 是 Shard-local

创建 group 时：

~~~text
create_scheduling_group()
  -> invoke_on_all()
  -> every shard initializes group
~~~

但是之后：

~~~cpp
sg.set_shares(x);
~~~

固定接口明确说明 adjustment is local to the calling shard。

实现也只修改当前 Reactor：

~~~cpp
void
scheduling_group::set_shares(
        float shares) noexcept {

    engine()
        ._task_queues[_id]
        ->set_shares(shares);

    engine()
        .update_shares_for_queues(
            internal::priority_class(*this),
            shares);
}
~~~

因此：

~~~text
group identity
  = global

CPU scheduler state
  = per shard
~~~

在 shard 0 调到 500，并不自动改变 shard 1。

如果业务要全局动态调权，应显式在所有 shard 执行对应更新。

---

## 26. Dynamic Shares 改的是未来收费速率

`task_queue::set_shares()`：

~~~cpp
void
reactor::task_queue::set_shares(
        float shares) noexcept {

    _shares =
        std::max(
            shares,
            1.0f);

    _reciprocal_shares_times_2_power_32 =
        (uint64_t(1) << 32)
        /
        _shares;
}
~~~

它没有重写历史 `_vruntime`。

因此从 100 改为 1000：

~~~text
historical vruntime
  stays unchanged

future runtime
  charged at slower virtual rate
~~~

这符合资源账本语义：过去的 CPU 已经消费，policy 变化只影响未来。

---

## 27. CPU Shares 与 I/O Bandwidth 不是同一个 Resource Contract

`set_shares()` 还会调用：

~~~cpp
update_shares_for_queues(
    internal::priority_class(*this),
    shares);
~~~

说明 CPU scheduling group 与 I/O priority class 有映射关系。

但接口另有：

~~~cpp
future<>
scheduling_group::update_io_bandwidth(
        uint64_t bandwidth) const {

    return engine()
        .update_bandwidth_for_queues(
            internal::priority_class(*this),
            bandwidth);
}
~~~

固定 API 注释还专门区分：

- `set_shares()`：shard-local CPU share adjustment；
- `update_io_bandwidth()`：不是 shard-local，而是所有 shard 总体的 bandwidth 语义。

因此：

~~~text
CPU share
!=
I/O bandwidth budget
~~~

资源隔离必须按资源种类分别建模。

---

## 28. 为什么销毁 Group 也是全局协议

顶层销毁：

~~~cpp
future<>
destroy_scheduling_group(
        scheduling_group sg) noexcept {

    if (
        sg
        ==
        default_scheduling_group()) {

        return make_exception_future<>(
            /* cannot destroy default */);
    }

    if (
        sg
        ==
        current_scheduling_group()) {

        return make_exception_future<>(
            /* cannot destroy current */);
    }

    return smp::invoke_on_all(
        [sg] {
            return engine()
                .destroy_scheduling_group(sg);
        }).then([sg] {
            deallocate_scheduling_group_id(
                sg._id);
        });
}
~~~

单 Reactor：

~~~cpp
future<>
reactor::destroy_scheduling_group(
        scheduling_group sg) noexcept {

    return with_scheduling_group(
        sg,
        [this, sg] {
            get_sg_data(sg)
                .specific_vals
                .clear();
        }).then(
        [this, sg] {
            get_sg_data(sg)
                .queue_is_initialized
                = false;

            _task_queues[sg._id]
                .reset();
        });
}
~~~

这要求调用方保证 group 已不再使用。

因为 task 中只携带 group identity；如果 queue/state 已销毁但旧 task 仍引用该 ID，执行上下文就不存在了。

---

## 29. 为什么不能销毁 Current Group

当前正在 SG 5 执行时：

~~~text
current task
  |
  v
current_scheduling_group = 5
~~~

如果此时同步销毁 SG 5：

~~~text
destroy task_queue[5]
destroy group-local data
~~~

当前调用栈后续创建 task、timer、continuation 时仍可能默认继承 SG 5。

这相当于执行上下文自己拆掉承载自己的 namespace。

所以固定接口显式拒绝：

~~~cpp
if (
    sg
    ==
    current_scheduling_group()) {
    // fail
}
~~~

这是一条很通用的 runtime 生命周期原则：

> 不要允许执行上下文在自己仍活跃时同步销毁自身承载结构。

---

## 30. High-priority Task 也不是硬实时抢占

Reactor 有：

~~~cpp
void
reactor::add_high_priority_task(
        task* t) noexcept {

    add_urgent_task(t);

    // break .then() chains
    request_preemption();
}
~~~

它做两件事：

1. 把 task 放到本 group queue 前端；
2. 请求当前执行链尽快回到 Reactor。

但是路径仍然是：

~~~text
urgent task arrives
      |
      v
request_preemption()
      |
      v
current code reaches cooperative boundary
      |
      v
Reactor regains control
      |
      v
urgent task can execute
~~~

它无法在任意 C++ 指令处强制保存当前调用栈并切走。

---

## 31. Shares 不等于 Deadline、WCET 或 SCHED_FIFO

必须明确排除四种误解。

### Shares 不是 Deadline

~~~text
shares = 1000
~~~

没有表达：

~~~text
must finish before 2 ms
~~~

### Shares 不是 WCET

scheduler 不知道 task 的 worst-case execution time。

### Shares 不是 Fixed Priority

低 shares group 仍应长期获得 progress。

### Shares 不是 Kernel Preemption

高 shares group 不能随时打断当前普通 C++ 函数。

因此最准确的定义是：

> Scheduling Group 是 userspace cooperative proportional-share scheduler 的 policy primitive。

---

## 32. 一个更合适的机器人 Runtime 映射

在一个 soft real-time shard 上可以划分：

~~~text
state / control-adjacent   shares 800
perception post-process    shares 600
RPC                        shares 200
diagnostics                shares 80
background cleanup         shares 40
~~~

它可以解决：

- cleanup 生成大量 continuation 时不自动吞掉 CPU；
- diagnostics backlog 不直接淹没 perception；
- RPC spike 被限制在相对 CPU 份额；
- 每个 group 可以独立观察 queue length / starvation / runtime。

但真正要求严格 dispatch bound 的 1 kHz servo loop，仍更适合独立 RT execution context，而不是简单把 shares 设置得很大。

---

## 33. 控制系统真正应该监控哪些 Scheduler Signal

| 指标 | 含义 | 高值常见原因 |
|---|---|---|
| `runtime_ms` | 实际 CPU 消耗 | 业务计算重 |
| `queue_length` | runnable backlog | arrival > service |
| `waittime_ms` | 等待外部事件 | sensor / I/O / upstream latency |
| `starvetime_ms` | ready 但拿不到 CPU | contention / shares 不足 |
| `tasks_processed` | execution node 数量 | continuation 粒度 |
| `runtime / tasks` | 平均 task 粒度 | 单 task 是否过重 |
| `time_spent_on_task_quota_violations_ms` | cooperative slice 超长 | 不 yield / inline chain / heavy callback |

这些指标合起来可以把“系统慢了”拆成：

~~~text
算得太慢？
ready 了但调度不到？
上游数据根本没来？
还是 runnable arrival 已超过容量？
~~~

---

## 34. 从 First Principles 重建一次 Scheduling Group

不看类名，只从问题构造机制。

### 第一步：单 FIFO 不够

业务并发度不同，不应该自动改变 CPU 权重。

所以每个业务隔离域需要独立 runnable queue。

### 第二步：独立 Queue 之后要决定谁先跑

严格 priority 可能永久饿死低优先级工作。

所以需要比例公平。

### 第三步：Task 数量不是资源

task 长短不同，真正的稀缺资源是 CPU time。

### 第四步：用 Shares 表达业务权重

\[
\Delta v
\approx
\frac{\Delta t}{\text{shares}}
\]

### 第五步：选择较低 VRuntime Group

低 vruntime 代表相对 shares 而言“消费得更少”。

### 第六步：按真实 elapsed runtime 收费

不是按 callback 数量收费。

### 第七步：Sleeping Group 不能积攒无限信用

activation 时对齐 `_last_vruntime` frontier。

### 第八步：公平策略必须有重新决策机会

所以需要 cooperative task quota / yield。

### 第九步：公平性不能替代容量控制

还需要 bounded queue、backpressure 和 overload policy。

最终得到：

~~~text
per-component queues
        +
actual-time accounting
        +
weighted vruntime
        +
cooperative preemption
        +
activation correction
        +
overload observability
~~~

---

## 35. 把一条异步链完整跑过 Scheduling Group

假设当前在 default group：

~~~cpp
return with_scheduling_group(
    control_sg,
    [] {
        return read_sensor()
            .then(process_state)
            .then(publish_state);
    });
~~~

如果 `control_sg` 不是 current group：

~~~text
with_scheduling_group(control_sg)
      |
      v
make_task(control_sg, lambda)
      |
      v
schedule_checked()
      |
      v
control_sg task_queue
      |
      v
Reactor selects by vruntime
      |
      v
run_tasks(control queue)
      |
      v
current_scheduling_group = control_sg
      |
      v
lambda starts
~~~

如果 `read_sensor()` 尚未 ready：

~~~text
future dependency pending
      |
      v
continuation stores scheduling context
      |
      v
event completes
      |
      v
continuation becomes runnable
      |
      v
control_sg queue
~~~

如果 dependency 已 ready，release fast path 可能 inline：

~~~text
current control_sg execution slice
      |
      v
ready .then()
      |
      v
inline callback
      |
      v
still charged inside current SG batch
~~~

当 cooperative boundary 被观察到：

~~~text
Reactor regains control
      |
      v
measure elapsed runtime
      |
      v
vruntime += runtime / shares
      |
      v
reinsert group if backlog remains
      |
      v
compare all active groups again
~~~

这就是 group identity、continuation inheritance、CPU accounting 和 cooperative scheduling 的完整闭环。

---

## 36. 与其他 Seastar 章节怎样拼成一条执行链

这一篇回答：

~~~text
多个 runnable groups
怎样共享同一颗 CPU？
~~~

[Reactor 主循环](reactor-shard-per-core.md)回答：

~~~text
task execution
怎样和 I/O / SMP / timer / sleep
共享一个 event loop？
~~~

[SMP Message Queue](smp-message-queue.md)回答：

~~~text
跨 shard 工作
怎样进入目标 Reactor？
~~~

[Future / Continuation](future-continuation-task.md)回答：

~~~text
异步依赖完成后
怎样重新变成 runnable task？
~~~

连起来就是：

~~~text
remote/local event
      |
      v
future becomes ready
      |
      v
task with scheduling_group
      |
      v
per-group local queue
      |
      v
vruntime scheduler
      |
      v
task::run_and_dispose
      |
      v
next async boundary
~~~

---

## 37. 最终需要记住的三个不变量

### 不变量一：Runnable 数量不能偷偷决定业务 CPU 权重

CPU policy 属于 scheduling group，不属于 ready continuation 数量。

### 不变量二：公平性按实际 CPU 时间收费

\[
\text{vruntime increment}
\propto
\frac{\text{actual runtime}}
{\text{shares}}
\]

### 不变量三：Fairness 依赖 Cooperative Progress

scheduler 再合理，也只能在当前执行流归还控制权后重新选择。

因此一句话概括：

> **Scheduling Group 把一个 shard 上的 runnable continuation 按业务隔离域分队列，并用实际 CPU 时间除以 shares 累积 vruntime，在 cooperative task boundary 上实施比例公平。它能隔离吞吐与 soft-latency 干扰，但不提供硬实时 deadline、WCET 或强制抢占。**
