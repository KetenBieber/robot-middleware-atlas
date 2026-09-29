# Progress Engine：为什么“网络已经完成”不等于“你的 Request 已完成”

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

高性能通信库常见的一个误区是：调用 non-blocking send 后，后台线程会自动把一切做完。UCX 的默认使用模型不能这样理解。UCP Worker 是 progress domain，而进度通常需要显式被驱动。

固定源码中的 ucp_worker_progress 几乎把真相全部暴露出来：

~~~c
unsigned ucp_worker_progress(ucp_worker_h worker)
{
    unsigned count;

    UCP_WORKER_THREAD_CS_ENTER_CONDITIONAL(worker);

    ucs_assert(worker->inprogress++ == 0);
    count = uct_worker_progress(worker->uct);
    ucs_async_check_miss(&worker->async);
    ucs_assert(--worker->inprogress == 0);

    UCP_WORKER_THREAD_CS_EXIT_CONDITIONAL(worker);

    return count;
}
~~~

UCP 自己没有在这里偷偷创建业务线程；它进入 Worker 的条件临界区，然后让 UCT worker 推进底层接口、completion 与 pending callback。

## 官方例子为什么 while 里一直 progress

ucp_hello_world 的等待逻辑是：

~~~c
while (!request->completed) {
    ucp_worker_progress(ucp_worker);
}

request->completed = 0;
status = ucp_request_check_status(request);
ucp_request_free(request);
~~~

这段代码非常值得重视。Request 的完成依赖 progress 调用频率。如果一个推理线程连续占用 CPU 80 ms 且同一线程负责 progress，即使 NIC 很早收到了 completion，应用侧状态机也可能更晚才被推进。

在机器人系统里，这等价于把 communication progress 的 WCET/调度延迟加入数据年龄预算。

## Busy progress 和 event-driven wait 是两种调度策略

UCX 也提供 eventfd/wakeup 机制。官方例子会取得 Worker fd，arm Worker，然后进入 epoll_wait：

~~~c
status = ucp_worker_get_efd(ucp_worker, &epoll_fd);

status = ucp_worker_arm(ucp_worker);
if (status == UCS_ERR_BUSY) {
    /* event 已经到达，不能睡 */
}

err = epoll_wait(epoll_fd_local, &ev, 1, -1);
~~~

这解决的是 CPU 利用率与响应延迟的权衡。一直 busy progress 延迟低但占 CPU；arm + epoll 可以睡眠，但系统调用、调度唤醒与 race-handling 会进入延迟路径。正确顺序必须是“先 arm，若已经有事件则不睡”，否则可能丢掉从检查到 sleep 之间的唤醒。

## Thread mode 不是性能标签

UCS 定义 SINGLE、SERIALIZED、MULTI 三种线程共享模式。SINGLE 表示只有创建/主线程访问；SERIALIZED 允许多个线程但要求外部串行；MULTI 允许并发访问。

官方 hello world 明确选择：

~~~c
worker_params.field_mask  = UCP_WORKER_PARAM_FIELD_THREAD_MODE;
worker_params.thread_mode = UCS_THREAD_MODE_SINGLE;
status = ucp_worker_create(ucp_context, &worker_params, &ucp_worker);
~~~

所以设计一个具身 runtime 时，应先问“哪个线程拥有 Worker、谁调用 progress、推理线程会不会饿死 progress、是否需要独立通信线程”，而不是先把模式切成 MULTI。并发能力越强，内部同步成本通常也越高。

Progress Engine 把网络问题重新变成调度问题：**通信延迟 = transport 时间 + protocol 时间 + progress 获得 CPU 的时间。** 对实时机器人，这第三项往往不能忽略。

## 从第一性原理看：为什么非阻塞 API 仍然需要“有人做事”

一个 `send_nbx()` 返回以后，真正的传输可能还需要经历：

~~~text
request created
↓
protocol stage selected
↓
UCT operation issued
↓
NIC / shared-memory / GPU operation progresses
↓
completion arrives
↓
request state advances
↓
callback / completion flag becomes visible
~~~

调用栈已经返回，只能说明“应用线程不再停留在 send 函数内部”，并不能说明后面的状态机凭空自行推进。

这也是理解异步系统最重要的一条边界：

> **non-blocking 描述调用者是否等待，progress model 描述谁负责让未完成工作继续前进。**

有的系统内部总有后台线程，有的系统把 progress 交给 executor，有的系统要求应用显式 poll。UCX 的 Worker 模型让这个责任非常显式。

固定版本中的 [`ucp_worker_progress()`](https://github.com/openucx/ucx/blob/8a6b06fb880accbb933a79cda893883872c68d9d/src/ucp/core/ucp_worker.c#L3185) 直接进入 `uct_worker_progress(worker->uct)`，所以分析一条 UCX 数据路径时必须把 **谁调用 progress、多久调用一次** 画在时序图里，而不能只画 Endpoint 与 NIC。

## Progress budget：通信线程同样需要 CPU 预算

假设：

~~~text
GPU inference = 35 ms
control period = 10 ms
network transfer itself = 0.3 ms
~~~

如果 inference 与 UCX progress 共用一条线程，并且这 35 ms 内没有任何 progress point，那么 0.3 ms transport 并不能推出 0.3 ms application-visible completion。

request 可能要等 35 ms 以后才继续推进。因此端到端模型至少要写成：

~~~text
T_complete
≈
T_transport
+ T_protocol
+ T_wait_for_progress
+ T_completion_dispatch
~~~

其中 `T_wait_for_progress` 是程序组织产生的，不是网卡产生的。

## Busy Polling：用一个 CPU Core 换更短的唤醒路径

最直接的 progress loop：

~~~cpp
while (!stop) {
    while (ucp_worker_progress(worker) != 0) {
        // keep draining ready work
    }
}
~~~

它的优点是没有 sleep、没有 kernel wakeup，事件到来后很快再次 poll。代价则是长期占用 CPU、与业务线程竞争 core/cache，并且空闲时仍有功耗。

对于固定部署的高吞吐服务器，独占 core 可能完全合理；对于机器人 SoC，CPU core 还要承担控制、感知前处理和驱动线程，这个成本必须显式纳入资源预算。

## Event-driven Progress：真正困难的是“准备睡眠”的竞态

eventfd/epoll 的难点不是 API，而是下面这个窗口：

~~~text
Consumer: 检查当前没有 completion
                      │
                      │ Producer / NIC event arrives
                      ▼
Consumer: 进入 epoll_wait
~~~

如果事件只是一瞬间的边沿，而没有正确的 arm / pending-state 协议，就可能发生 lost wakeup。

UCX 的顺序不是“看起来没事 → 直接 sleep”，而是：

~~~text
drain progress
↓
arm worker
↓
如果返回 UCS_ERR_BUSY
    说明 arm 过程中已经发现事件
    不允许 sleep
↓
只有 arm 成功
才进入 poll / epoll_wait
~~~

固定源码的 API 文档与 [`ucp_worker_arm()`](https://github.com/openucx/ucx/blob/8a6b06fb880accbb933a79cda893883872c68d9d/src/ucp/core/ucp_worker.c#L3283) 都围绕这个协议展开。

这和 condition variable 的 predicate 规则本质相同：

> **睡眠不是状态，睡眠只是“确认当前没有工作以后节省 CPU”的优化。真正的事实来源始终是共享状态。**

## 三种 Runtime 组织方式

### 方案 A：业务线程顺手 progress

~~~text
Perception thread
  compute
  ↓
  ucp_worker_progress()
  ↓
  compute
  ↓
  ucp_worker_progress()
~~~

优点是线程少、ownership 简单。问题是 progress interval 被业务 WCET 决定。只要一次 kernel/inference 太长，通信进度就被饿死。

### 方案 B：独立 Communication Worker

~~~text
Sensor / inference threads
        │
        │ MPSC command queue
        ▼
Communication thread
        │ owns UCP Worker
        ├─ submit requests
        ├─ ucp_worker_progress
        └─ completion queue
              │
              ▼
        application consumers
~~~

这里一个很重要的设计收益是：

> UCP Worker 可以保持 `UCS_THREAD_MODE_SINGLE`，而不是为了“很多业务线程都能发”直接切到 MULTI。

多 Producer 的并发先被应用侧 MPSC queue 收敛，真正操作 UCX Worker 的仍然只有一个 owner。这正是 Communication Foundations 中“先改变 topology，再优化 atomic”的具体应用。

### 方案 C：每条高吞吐 Pipeline 独占 Worker

~~~text
Camera pipeline ── Worker A
LiDAR pipeline  ── Worker B
VLA tensor path ── Worker C
~~~

它减少单 Worker 内部共享，但会增加 Worker 数量、Endpoint/iface state、event fd、progress CPU 和资源重复，所以 per-thread Worker 也不是免费性能。

## 为什么“直接用 MULTI”通常不是第一答案

`UCS_THREAD_MODE_MULTI` 解决的是“多个线程可以合法并发访问同一个 Worker”。它没有解决：

~~~text
业务 queue 是否有界
多个 producer 是否争一个热点 endpoint
completion 应该回到哪条业务执行流
实时线程能不能承受内部锁
~~~

如果控制线程、相机线程、日志线程全部直接进入同一个 MULTI Worker，程序虽然线程安全，但 ownership 已经变得更难推理。

更可控的思路通常是先问：

~~~text
能否 single-owner？
能否 MPSC submit？
能否按 pipeline shard？
~~~

只有确实需要共享调用时再付出 MULTI 的同步成本。

## 一个可迁移的小项目：Communication Progress Thread

可以脱离 UCX 写一个最小模型：

~~~text
N producers
    ↓
bounded MPSC<RequestDescriptor>
    ↓
Comm thread
    ↓
progress()
    ↓
bounded completion channel
    ↓
business thread
~~~

`RequestDescriptor` 不需要携带大 payload，只保存：

~~~cpp
struct RequestDescriptor {
    BufferHandle buffer;
    PeerId peer;
    Operation op;
    CompletionToken token;
};
~~~

然后测四组数据：

~~~text
submit queue depth
submit -> issue latency
issue -> completion latency
completion -> business consume latency
~~~

再分别切换 busy progress、event-driven progress、业务线程 inline progress 与 dedicated thread。它比单测 messages/s 更能解释一个机器人 runtime 为什么出现偶发 20 ms 尾延迟。

## 把 UCX Progress 和线程通信放回同一张图

最终，UCX 的 progress engine 不是一个“网络专题例外”，而是线程间数据流的一种特殊形式：

~~~text
network / device completion
        ↓
progress state machine
        ↓
request becomes complete
        ↓
notification / callback
        ↓
business execution flow
~~~

它仍然要回答：谁拥有状态、谁负责推进、谁负责唤醒、队列满了怎么办、关闭时怎样让所有 request 收束。

这就是为什么学 UCX 最终仍然会回到并发程序组织，而不是停在 RDMA API。
