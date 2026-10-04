# Reactor：Shard-per-Core 怎样把线程、任务、I/O 与 Timer 收敛到一个 Ownership Domain

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Seastar 的 Reactor 不是“epoll 回调分发器”。

从源码字段就能看到它更像一个单核 userspace runtime：

~~~text
task queues
pollers
timers
I/O queues
SMP messages
cross-core frees
signals
sleep/wakeup
~~~

这些东西都围绕一个关键前提：

> 当前 shard 上的大部分 mutable runtime state 由当前 Reactor thread 独占。

## Task Queue 是 Reactor 内部的可运行队列

每个 scheduling group 有一个 `task_queue`：

~~~cpp
int64_t _vruntime;
float _shares;
bool _active;
circular_buffer<task*> _q;
~~~

`add_task()` 不需要把任务交给一个跨线程 MPMC global queue，而是直接根据 task 的 scheduling group 放进当前 Reactor 的本地 queue。

~~~cpp
auto sg = t->group();
auto* q = _task_queues[sg._id].get();
q->_q.push_back(t);
~~~

这正是 single-owner 的价值：普通 `circular_buffer` 就够了。

## run_tasks() 为什么不持 Mutex

Reactor 自己就是这些 local task queues 的 owner。

所以：

~~~text
pop front
run_and_dispose
pop next
~~~

不需要每个 task 都和其他 OS thread 抢 scheduler mutex。

这和 Asio 多线程 `io_context::run()` 的模型非常不同。

## Cooperative Scheduling 的核心约束

Task 执行：

~~~cpp
tsk->run_and_dispose();
~~~

Runtime 并不会任意时刻像 OS preemption 一样把 C++ 函数切走。

Task 必须在 async boundary / yield point 把控制权还给 Reactor。

因此 Seastar 的低锁代价换来一个强约束：

> 用户任务不能长期占住 Reactor thread。

## need_preempt 不是 OS 抢占

Reactor 用 task quota / preemption monitor 告诉代码：

~~~text
当前 cooperative timeslice 应该结束了
~~~

`run_tasks()` 每执行一个 task 后检查 `scheduler_need_preempt()`。

长循环中的 Seastar primitive/coroutine 也会检查 `need_preempt()` 并主动 yield。

这仍然是 cooperative preemption。

不是内核直接保存寄存器、切到另一个线程。

## Reactor Main Loop 的结构

主循环可以压成：

~~~text
run_some_tasks()
↓
poll all registered pollers
↓
work exists?
├─ yes → continue
└─ no
   ↓
   idle handler
   ↓
   poll for a while
   ↓
   enter interrupt/sleep mode
~~~

这体现 latency 与 CPU utilization 的经典权衡：

- busy poll 更快，但耗 CPU；
- sleep 更省 CPU，但有 wakeup latency。

Seastar 会先 poll，空闲超过 `max_poll_time` 后才真正关闭 quota timer 并进入 backend wait。

## Poller 为什么是一组独立组件

源码中存在：

- SMP poller；
- lowres timer poller；
- kernel completion poller；
- I/O submission poller；
- signal poller；
- cross-CPU freelist poller；
- execution-stage poller。

Reactor 不把所有子系统写死成一个巨型 switch，而是维护 `std::vector<pollfn*> _pollers`。

其中 cross-CPU freelist poller 很能体现 Reactor 的“完整 CPU runtime”角色：foreign CPU 只把 dead storage 发布到 owner 的 atomic ingress，不主动 wake；owner Reactor 在正常 poll 中批量接管，内存压力时 allocator 又会主动 drain。这个 allocator-side progress protocol 见 [Per-shard Allocator 与 Cross-CPU Free：地址编码、MPSC Ingress 与 Owner-side Reclaim](cross-shard-memory-reclaim.md)。

每个 subsystem 通过 poll interface 提供：

~~~text
poll()
pure_poll()
enter/exit interrupt mode
~~~

这是 Runtime 插件化的一种低层实现。

## pure_poll 为什么存在

进入 sleep 前不能随意执行会改变业务状态的工作。

所以 Reactor 区分：

~~~text
poll_once()
= 可以真正消费/执行工作

pure_poll_once()
= 只判断是否有工作，不产生有副作用执行
~~~

这是一种值得迁移的接口设计：

> “check ready” 与 “consume ready” 不一定应该是同一个操作。

## Shutdown 为什么还要 Drain Final Tasks

Reactor `_stopped` 后仍会：

~~~text
run remaining task queues
run at_destroy queue
arrive_at_event_loop_end
join shards
~~~

因为 shutdown 本身仍可能需要跨 shard completion。

直接退出线程会破坏最后一批 ownership handoff。

## 可迁移原则

1. 如果 mutable runtime state 由一个 thread 独占，本地容器可以非常简单。
2. Cooperative scheduler 的代价是业务代码必须尊重 yield/preemption boundary。
3. Reactor 可以通过 poller interface 组合多个 readiness source。
4. 检查“有没有工作”和真正“执行工作”可以拆成 pure/impure 两种接口。
5. Shutdown 也是一段需要调度与 completion 的 Runtime phase。