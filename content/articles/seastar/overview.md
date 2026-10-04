# Seastar 总览：Shard-per-Core 如何把并发问题改写成 Ownership、Message Passing 与 Cooperative Scheduling

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`（Seastar 25.05.0）。

多线程 Runtime 常见的出发点是：

~~~text
many worker threads
      |
      v
shared queues / shared maps / shared allocators
      |
      v
mutex / atomics / RCU / lock-free structures
~~~

Seastar 从另一端开始：

> 如果 hot mutable state 可以按 Core 拆开，最有效的并发优化往往不是继续优化锁，而是先消除共享。

它把机器组织成多个 shard。每个 shard 通常对应一个逻辑 CPU，并由一条长期运行的 Reactor thread 推进本地执行状态：

~~~text
CPU 0 / shard 0                  CPU 1 / shard 1
+--------------------+           +--------------------+
| Reactor            |           | Reactor            |
| local task queues  |           | local task queues  |
| timers             |           | timers             |
| I/O state          |           | I/O state          |
| service instances  |           | service instances  |
| local allocator    |           | local allocator    |
+--------------------+           +--------------------+
          |                                ^
          |      explicit SMP message      |
          +--------------------------------+
~~~

因此 Seastar 的核心问题不是“Event Loop 怎么写”，而是：

**怎样让绝大多数 mutable state 只有一个 shard owner，同时让 Future、I/O、跨核调用、对象生命周期和内存回收在多核机器上组成完整系统？**

---

## 1. Shard-per-Core 首先是一条 Ownership 规则

固定版本创建 Reactor 时，把 shard id 和 Reactor 写入当前线程的 thread-local runtime：

~~~cpp
void smp::allocate_reactor(
        unsigned id,
        reactor_backend_selector rbs,
        reactor_config cfg) {

    void* buf;

    int r =
        posix_memalign(
            &buf,
            cache_line_size,
            sizeof(reactor));

    SEASTAR_ASSERT(r == 0);

    *internal::this_shard_id_ptr()
        = id;

    local_engine =
        new (buf) reactor(
            this->shared_from_this(),
            _alien,
            id,
            std::move(rbs),
            cfg);

    reactor_holder.reset(
        local_engine);
}
~~~

所以：

~~~cpp
engine()
this_shard_id()
current_scheduling_group()
~~~

都有一个隐含前提：当前代码已经处于某个明确 shard 的 execution domain。

Seastar 更倾向于：

~~~text
object has one owner shard
        |
        v
operations move to owner
~~~

而不是：

~~~text
object is globally shared
        |
        v
all threads synchronize around it
~~~

---

## 2. 为什么 Local Task Queue 可以是普通容器

Reactor 的 scheduling-group queue 直接使用：

~~~cpp
circular_buffer<task*> _q;
~~~

它不是 MPMC queue。

安全性来自 topology：

~~~text
local Future ready
      |
      v
make task
      |
      v
owner Reactor::add_task()
      |
      v
local circular_buffer
~~~

其他 shard 不应直接 push 这个 queue。远端工作必须先经过 SMP handoff，再由目标 Reactor 自己把工作变成本地 task。

因此设计顺序是：

~~~text
single owner
  -> simple local container

cross-owner communication
  -> explicit concurrent boundary
~~~

不是先选一个万能 concurrent queue，再允许所有线程随意碰状态。

---

## 3. Reactor 是一个 Shard 的执行内核

每个 Reactor 同时推进：

- local task queues；
- scheduling groups；
- Future continuations；
- SMP request/completion；
- kernel I/O completion；
- I/O submission；
- timers；
- signals；
- blocking worker completion；
- cross-CPU memory reclaim；
- idle polling；
- sleep/wakeup；
- shutdown drain。

主循环的系统形状：

~~~text
run_some_tasks()
      |
      v
poll progress sources
      |
      +-- SMP
      +-- I/O
      +-- timer
      +-- signal
      +-- worker completion
      +-- reclaim
      |
      v
new events become tasks
      |
      +------> local scheduler
~~~

所以 Reactor 更接近一个 shard 的 userspace CPU runtime，而不是 socket readiness 的薄封装。

完整 task、poller 与 sleep 状态机见 [Reactor：Shard-per-Core 怎样把线程、任务、I/O 与睡眠协议收敛到一个 Ownership Domain](reactor-shard-per-core.md)。

---

## 4. Future 把“阻塞等待”改造成 Continuation Task

传统阻塞模型：

~~~text
start async work
      |
      v
thread waits
      |
      v
event arrives
      |
      v
thread wakes
~~~

Seastar 的主路径：

~~~text
operation returns Future
      |
      v
register continuation
      |
      v
current task returns to Reactor
      |
      v
event completes
      |
      v
continuation becomes runnable task
      |
      v
Reactor executes it
~~~

于是异步依赖图会变成 runnable task 图：

~~~text
Future dependency graph
        |
        v
Continuation objects
        |
        v
Reactor task scheduling
~~~

一条 Reactor thread 才能承载大量逻辑并发，而不是为每个等待中的 operation 保留一条阻塞 OS thread。

---

## 5. Ready Future 有时会 Inline

固定 `future::then_impl()` 在 release build 中有 fast path：

~~~cpp
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
~~~

因此：

~~~text
.then()
!=
always enqueue a new task
~~~

dependency 已 ready 时，continuation 可以直接在当前 execution slice 中继续。

收益是少一次 task allocation 和 scheduler round-trip；代价是长 ready-chain 可能连续占用 Reactor，因此 cooperative preemption 仍然必须存在。

完整对象状态与 task 化过程见 [Future / Continuation：状态迁移、Task 化 Continuation 与异步控制流](future-continuation-task.md)。

---

## 6. 单线程仍然需要 CPU Fairness

一个 shard 可以同时承载：

~~~text
request processing
perception
background maintenance
metrics
storage work
~~~

如果所有 runnable continuation 进入同一个 FIFO，某个模块只要维持更多 ready nodes，就会隐式获得更多 CPU。

Seastar 为每个 scheduling group 建独立 local queue，并使用：

~~~text
shares
+
actual elapsed runtime
+
vruntime
~~~

记账。

近似关系：

\[
\Delta v
\approx
\frac{\Delta t}{\text{shares}}
\]

shares 越高，相同真实 CPU 时间产生的 vruntime 增量越小，因此该 group 更快再次被选中。

它是 cooperative proportional-share scheduling，不是 hard real-time priority。

完整 CPU accounting、wait/starvation 与 overload 机制见 [Scheduling Group：一个 Reactor 只有一条线程，怎样隔离 CPU 份额、延迟与 Backlog](scheduling-groups-vruntime.md)。

---

## 7. Scheduling Group 也是 Global Identity + Per-Shard State

创建 group 时：

~~~cpp
return smp::invoke_on_all(
    [sg, name, shortname, shares] {
        return engine()
            .init_scheduling_group(
                sg,
                name,
                shortname,
                shares);
    });
~~~

得到的结构不是一个共享全局 queue，而是：

~~~text
SG id = 5
   |
   +--> shard 0 task_queue[5]
   +--> shard 1 task_queue[5]
   +--> shard 2 task_queue[5]
~~~

Identity 可以跨 shard 一致，mutable scheduling state 仍留在每个 owner Reactor。

这个模式贯穿整个 Seastar：

> Identity 可以跨域，hot mutable state 尽量不跨域共享。

---

## 8. 跨 Shard 调用为什么不能直接改目标 Ready Queue

假设 shard 0 要让 shard 1 执行一个函数。

直接：

~~~text
CPU 0 pushes target local queue
~~~

会破坏：

- ready queue single-owner；
- cache locality；
- allocator ownership；
- task lifetime；
- target sleep/wakeup handshake。

Seastar 把跨核边界集中到 `smp::submit_to()` 与 pair-wise queue：

~~~text
origin shard
      |
      v
service-group credit
      |
      v
origin-local pending batch
      |
      v
request queue origin -> target
      |
      v
target SMP poller
      |
      v
target executes work
      |
      v
completion queue target -> origin
      |
      v
origin resolves promise
      |
      v
credit returned
~~~

这是一条完整 round-trip protocol，不只是“把 lambda 塞进并发队列”。

---

## 9. SMP Message Queue 同时承担 Transfer、Backpressure、Completion 与 Wakeup

### Transfer

work item 从 origin 到 target。

### Backpressure

提交前受 SMP service-group semaphore credit 约束。

### Completion

目标完成后，completion 回 origin，再 resolve origin-side promise。

### Wakeup

目标 Reactor sleeping 时，remote producer 必须把它唤醒。

完整 pair-wise SPSC、batch、credit 与 reverse completion 路径见 [SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure](smp-message-queue.md)。

---

## 10. 为什么 Backpressure 必须覆盖完整 Round Trip

只限制 request queue 长度不够。

假设 target dequeue 很快，但业务处理很慢：

~~~text
origin sends
target pops request
origin sees request queue short
origin sends more
target execution backlog grows elsewhere
~~~

真正要限制的是：

~~~text
submitted
but not yet completed
remote operations
~~~

所以 service-group credit 的生命周期是：

~~~text
acquire credit
      |
      v
send
      |
      v
target execute
      |
      v
completion returns
      |
      v
release credit
~~~

这才是 end-to-end backpressure。

---

## 11. 为什么 Pair-wise SPSC 比 Global MPMC 更符合这个架构

一个 global MPMC：

~~~text
CPU 0 \
CPU 1  \
CPU 2 ---> one shared queue
CPU 3  /
~~~

会把所有核的 producer/consumer synchronization 集中到同一组 cache lines。

Pair-wise topology：

~~~text
0 -> 1
0 -> 2
1 -> 0
1 -> 2
2 -> 0
2 -> 1
~~~

每条 queue 的 producer/consumer 角色更明确，hot synchronization 也不会全部集中到一个全局入口。

代价是 queue 数量增加，但 ownership 与 cache topology 更清楚。

---

## 12. SMP Queue 和 Sleep Protocol 必须一起证明

目标 Reactor idle 时，最朴素的实现：

~~~text
check queue empty
then sleep
~~~

会出现 lost wakeup：

~~~text
target CPU                    remote CPU
----------                    ----------
check queue empty
                              push message
                              sees target not sleeping
enter sleep
~~~

固定 Reactor 的 SMP interrupt-mode handshake：

~~~text
target sets _sleeping = true
      |
      v
systemwide memory barrier
      |
      v
poll request queues again
      |
      +-- work exists -> abort sleep
      |
      +-- still empty -> backend may sleep
                           ^
                           |
remote maybe_wakeup() ------+
             |
             v
          eventfd
~~~

因此 queue publish、sleeping flag、barrier、recheck 和 wake notification 是同一套并发协议。

---

## 13. Active Target 为什么不需要每次都写 Eventfd

`wakeup()` 先检查目标是否真的 sleeping。

因此：

~~~text
target active
  -> push queue
  -> normal SMP polling finds work
  -> no eventfd syscall

target sleeping
  -> push queue
  -> wakeup()
  -> eventfd write
  -> backend returns
~~~

这是 busy-poll runtime 常见的性能原则：

> 活跃态依靠 polling，休眠态才支付显式 wakeup syscall。

---

## 14. Ownership 不只决定“在哪个线程运行”

一个真正的 owner model 还要回答：

- 谁能修改对象；
- 谁能销毁对象；
- allocator metadata 属于谁；
- 哪个 shard 能做最终 release；
- shutdown 时谁负责 quiescence；
- handle 跨 shard 后 destructor 在哪里执行。

因此跨核 runtime 不能只研究 function dispatch。

对象层还需要 `sharded<Service>` 与 `foreign_ptr`。

---

## 15. `sharded<Service>`：每个 Shard 有自己的 Mutable Instance

对象图：

~~~text
sharded<MyService>
      |
      +--> shard 0: MyService[0]
      +--> shard 1: MyService[1]
      +--> shard 2: MyService[2]
~~~

本地调用针对当前 shard instance。

远端调用：

~~~text
invoke_on(target, fn)
      |
      v
SMP handoff
      |
      v
target local service instance
~~~

于是大多数 service 方法可以按顺序程序推理。

这比：

~~~text
one global service
+
mutex around every method
~~~

把 concurrency boundary 放得更清楚。

---

## 16. `foreign_ptr`：Handle 可以移动，Destruction 仍有 Owner

有些对象不能复制一份到每个 shard。

此时需要：

~~~text
owner shard A creates object
      |
      v
make_foreign(ptr)
      |
      v
handle moves to shard B
      |
      v
B holds / forwards handle
      |
      v
last release
      |
      v
destruction returns to A
~~~

所以 `foreign_ptr` 不是“任意跨线程访问都安全”的智能指针，而是一个 execution-affine ownership handle。

对象 ownership、`invoke_on` 与析构执行域见 [Sharded 与 foreign_ptr：Owner-Shard、跨核调用与析构执行域](sharded-foreign-ptr.md)。

---

## 17. Lifetime Safety 不等于 Data-race Safety

即使最后 destructor 能正确回 owner，也不表示 foreign shard 可以随意解引用并修改对象内容。

必须区分：

~~~text
lifetime safety
  object is released in valid execution domain

data-race safety
  concurrent memory access itself is valid
~~~

如果 mutable state 只允许 owner 访问，远端仍应：

~~~text
move computation to owner
~~~

而不是把 foreign handle 变成共享裸指针。

---

## 18. Ownership 原则甚至延伸到 Allocator

Seastar allocator 是 per-shard 的。

如果 shard A 分配的对象最后在 shard B 调用 free，B 不能直接修改 A 的 allocator metadata。

固定 cross-CPU free 路径：

~~~text
pointer allocated by A
      |
      v
last holder on B
      |
      v
detect allocation owner = A
      |
      v
push storage to A.xcpu_freelist
      |
      v
A Reactor drains later
      |
      v
A performs real local free
~~~

连 memory reclamation 都遵守 owner-computes。

---

## 19. Cross-CPU Free 为什么不用完整 SMP RPC

每次 free 都走 `smp::submit_to()` 太重。

已经逻辑死亡的 storage 不需要业务 reply，只需要最终回到 owner allocator。

因此固定实现使用：

~~~text
many foreign CPUs
      |
      v
owner xcpu_freelist
      |
      v
MPSC ingress
      |
      v
owner batch drain
~~~

producer 侧发布待回收节点，owner Reactor 批量摘取并完成真正 local free。

这是 deferred owner-side reclamation。

完整地址 owner 识别、MPSC freelist 与 adaptive reclaim 见 [Per-shard Allocator 与 Cross-CPU Free：地址编码、MPSC Ingress 与 Owner-side Reclaim](cross-shard-memory-reclaim.md)。

---

## 20. 为什么 Reclaim 通常不主动 Wake Owner

业务 SMP request：

~~~text
target sleeping
  -> must wake
~~~

因为有用户工作等待执行。

foreign free：

~~~text
storage already logically dead
  -> delayed reclaim usually acceptable
~~~

所以 cross-CPU reclaim poller 可以等 owner 因其他事件醒来再 drain。

这说明“是否 wake consumer”不是 queue 类型自动决定的，而由事件的 progress requirement 决定。

---

## 21. Seastar 不是“没有共享”，而是把共享集中在少数边界

固定 runtime 仍然大量使用：

- atomics；
- SPSC queue；
- MPSC reclaim list；
- semaphore；
- systemwide memory barrier；
- eventfd；
- kernel synchronization；
- cross-shard lifecycle protocol。

变化在于同步不再散落在每个业务对象。

它主要集中于：

~~~text
shard A <-> shard B
Reactor <-> kernel
Reactor <-> blocking worker
awake <-> sleeping
local owner <-> foreign lifetime
normal run <-> shutdown
~~~

所以更准确的描述是：

> Seastar 没有消灭并发，而是把并发集中到 ownership boundary，让 shard 内大部分业务状态恢复成 single-owner sequential state。

---

## 22. Cooperative Scheduling 是 Single-owner 模型的重要代价

单 owner 的收益：

~~~text
local hot state
  -> often no mutex
~~~

代价：

> 当前 task 必须及时把控制权还给 Reactor。

如果普通函数持续计算：

~~~cpp
void bad_task() {
    for (;;) {
        heavy_compute();
    }
}
~~~

没有 yield、Future boundary 或 preemption check，那么这个 shard 的：

- timer；
- SMP ingress；
- I/O completion；
- 其他 scheduling groups；
- shutdown progress；

都会一起被拖住。

因此 shard-per-core 不能只学“单线程无锁”，还必须同时设计：

~~~text
task quota
yield boundary
stall detector
scheduling groups
blocking work offload
backlog policy
~~~

---

## 23. Blocking Work 为什么仍然需要 Worker Thread

Shard-per-core 不意味着整个进程只能存在 Reactor thread。

无法自然异步化的阻塞调用如果直接运行在 Reactor：

~~~text
blocking syscall
      |
      v
Reactor sleeps
      |
      v
whole shard stops
~~~

合理边界是：

~~~text
Reactor submits blocking work
      |
      v
worker performs blocking wait
      |
      v
completion queue
      |
      v
Reactor continuation resumes
~~~

worker 负责阻塞阶段，Reactor 仍然是业务 async state 的 execution owner。

---

## 24. 一条 Sensor Event 怎样贯穿整个 Runtime

假设：

~~~text
shard 0
  camera ingress

shard 1
  perception service

shard 2
  state fusion
~~~

相机数据在 shard 0 ready：

~~~text
I/O completion
      |
      v
Reactor poller
      |
      v
Future resolves
      |
      v
continuation task
      |
      v
camera scheduling group
~~~

需要调用 shard 1：

~~~text
smp::submit_to(1)
      |
      v
acquire round-trip credit
      |
      v
request queue 0 -> 1
      |
      v
wake shard 1 if sleeping
~~~

shard 1：

~~~text
SMP poller
      |
      v
remote work item
      |
      v
local perception service
      |
      v
local Future chain
      |
      v
perception scheduling group
~~~

完成：

~~~text
completion queue 1 -> 0
      |
      v
origin promise resolves
      |
      v
credit returned
~~~

如果某个结果 handle 跨 shard，但 destructor 仍属于创建者：

~~~text
foreign_ptr
      |
      v
handle moves
      |
      v
final destruction returns to owner
~~~

如果 storage 最后在 foreign CPU 遇到 free：

~~~text
xcpu_freelist
      |
      v
owner-side reclaim
~~~

所有机制都在维护同一个问题：

> 现在谁拥有这份 mutable state，跨 owner 时使用什么显式协议？

---

## 25. 完整 Runtime 总图

~~~text
                    external I/O / timer
                            |
                            v
                 +-----------------------+
                 | shard N Reactor       |
                 |                       |
                 | pollers               |
                 |   |                   |
                 |   v                   |
                 | Future completion     |
                 |   |                   |
                 |   v                   |
                 | continuation task     |
                 |   |                   |
                 |   v                   |
                 | scheduling group      |
                 | task_queue            |
                 |   |                   |
                 |   v                   |
                 | vruntime scheduler    |
                 |   |                   |
                 |   v                   |
                 | user code             |
                 +----+-------------+----+
                      |             |
                local |             | cross shard
                      |             v
                      |        SMP request
                      |             |
                      |             v
                      |        target Reactor
                      |             |
                      |        owner state
                      |             |
                      |             v
                      |        completion
                      |             |
                      +<------------+
                      |
                      +--> sharded local service
                      |
                      +--> foreign_ptr lifetime handoff
                      |
                      +--> cross-CPU reclaim to allocator owner
~~~

Idle 状态：

~~~text
no local tasks
+
pollers report no work
      |
      v
prepare interrupt mode
      |
      v
publish sleeping state
      |
      v
barrier + recheck
      |
      v
backend wait
      |
      v
remote/eventfd/timer wakeup
~~~

Shutdown：

~~~text
stop requested
      |
      v
drain final tasks
      |
      v
allow final cross-shard completions
      |
      v
finish owner-local cleanup
      |
      v
shard synchronization / join
~~~

---

## 26. 与 Folly 的真正差别是问题切分方式

可以把两条路线放进一张决策树：

~~~text
Can hot mutable state have a clear owner?
          |
       yes|                     no
          v                      v
shard / ownership          shared concurrency
message passing            primitives
          |                      |
          v                      v
Seastar-like               Folly-like
architecture               data structures
~~~

Folly 更擅长回答：

~~~text
既然必须共享，
如何把共享容器、回收和通知做得足够好？
~~~

Seastar 先问：

~~~text
这份 hot mutable state
真的需要跨核共享吗？
~~~

两者不是互斥的。更常见的优秀架构是：

1. 先按 ownership 拆掉大量不必要共享；
2. 明确少数跨域通道；
3. 对真正必须共享的边界再使用高质量 concurrent primitives。

---

## 27. 为什么这种模型适合 Runtime，但不是万能模板

它特别适合：

- 大量异步 I/O；
- workload 可以按 key/service 分片；
- hot path 对 cache locality 敏感；
- 能接受 cooperative async 编程；
- 愿意显式表达 cross-shard handoff。

直接照搬会困难的场景：

- 大量 blocking third-party library；
- 任意共享 pointer graph；
- 多线程必须同时修改同一对象；
- 强依赖 preemptive thread semantics；
- hard real-time loop 需要内核调度优先级和严格 deadline。

学习重点不是“所有机器人程序都应该改用 Seastar”，而是 ownership-first 的 runtime design。

---

## 28. 机器人 Runtime 最值得迁移的五条原则

### 28.1 先定义 Owner，再决定同步原语

先问：

~~~text
camera buffer owner?
state estimator owner?
controller state owner?
GPU stream owner?
telemetry owner?
~~~

很多锁需求会自然消失。

### 28.2 Cross-domain 操作显式化

不要让任意线程持裸指针修改另一个 execution context 的 mutable state。

可以用：

~~~text
command queue
message passing
owner callback
RPC-like handoff
~~~

### 28.3 Backpressure 要覆盖完整 In-flight 生命周期

不要只看 ingress queue 长度。

真正应限制：

~~~text
submitted but not completed work
~~~

### 28.4 Lifetime 也有 Execution Affinity

handle 可以移动，但 destructor、allocator free、driver release 可能仍必须回 owner。

### 28.5 Sleep/Wakeup 必须证明不会 Lost Wakeup

必须明确：

- sleeping publication；
- memory ordering；
- recheck；
- wake signal；
- rollback；
- notification memory。

---

## 29. 机器人控制还需要额外的 Data Freshness 层

Scheduling group 解决 CPU proportional fairness，但控制系统还关心：

~~~text
sample age
deadline
latest state
stale-work cancellation
~~~

如果 perception backlog 中已有 20 帧：

~~~text
公平地把 20 帧全部算完
~~~

未必比：

~~~text
丢弃 19 帧旧输入
只算最新状态
~~~

更合理。

因此迁移 Seastar 思路时通常需要：

~~~text
ownership
+
bounded queue
+
CPU shares
+
freshness policy
+
deadline / cancellation
~~~

CPU fairness 只是一层。

---

## 30. 对具身智能 Runtime 的映射

一个具身系统可以按执行域理解：

~~~text
Domain A
  camera ingress
  decoder state

Domain B
  visual encoder
  feature cache

Domain C
  policy inference orchestration

Domain D
  robot state fusion
  action publication
~~~

跨域不一定真的使用 Seastar SMP queue，但可以继承同一设计原则：

~~~text
A owns image buffers
B receives immutable / loaned handles
C owns inference request state
D owns control-state mutation
~~~

GPU 资源也可以采用类似 ownership：

~~~text
GPU stream / allocator owner
      |
      v
commands move to owner
      |
      v
completion tokens move back
~~~

比“所有线程随时调用同一个 CUDA context wrapper”更容易推理同步和生命周期。

---

## 31. 七篇源码机制其实维护同一个不变量

### Reactor

一个 shard 需要持续推进本地 task、I/O、timer、SMP、sleep 与 shutdown。

→ [Reactor 主循环](reactor-shard-per-core.md)

### Scheduling Group

单 owner 不代表单业务，多个 runnable component 仍需 CPU 隔离。

→ [Scheduling Group / VRuntime](scheduling-groups-vruntime.md)

### Future / Continuation

一条 Reactor thread 需要把等待拆成 continuation task 才能承载逻辑并发。

→ [Future / Continuation](future-continuation-task.md)

### SMP Message Queue

mutable state 不共享后，跨 shard 必须显式 handoff，并带 backpressure、completion、wakeup。

→ [SMP Message Queue](smp-message-queue.md)

### Sharded / Foreign Pointer

跨核不只有函数调用，还有 service instance、ownership handle 与 destructor execution domain。

→ [Sharded / foreign_ptr](sharded-foreign-ptr.md)

### Cross-shard Reclaim

连 allocator free 都回 owner，才能保持 per-shard allocator 的局部性。

→ [Cross-shard Memory Reclaim](cross-shard-memory-reclaim.md)

这些机制共同维护：

> **mutable state 尽量只有一个 execution owner；跨 owner 时显式传递工作、结果或回收责任。**

---

## 32. 最后保留一张“问题 → 机制”映射

| Runtime 问题 | Seastar 机制 |
|---|---|
| 谁拥有本地 mutable state？ | shard-per-core |
| 谁推进本地执行？ | Reactor |
| 等待 I/O 时怎样不阻塞线程？ | Future / continuation task |
| 多业务怎样共享一颗 CPU？ | scheduling group + vruntime |
| 跨核怎样执行函数？ | `smp::submit_to` + SMP message queue |
| 远端太慢怎么办？ | service-group round-trip credit |
| 目标 Reactor 睡了怎么办？ | sleeping handshake + eventfd wakeup |
| 服务对象怎样按核分片？ | `sharded<Service>` |
| handle 跨核但析构要回 owner？ | `foreign_ptr` |
| foreign CPU 遇到本地 allocator pointer？ | `xcpu_freelist` |
| queue 已 ready 但调度不到？ | starvetime |
| task 不归还 Reactor 怎么办？ | task quota + stall detection |
| shutdown 还有 completion 怎么办？ | final task drain + shard join |

Seastar 最值得学习的不是某个 API，而是一套 Runtime 设计方法：

**先用 ownership 消除大部分共享，再把不可避免的跨域并发集中成少数可证明的 message、backpressure、wakeup、lifetime 与 reclamation 协议。**
