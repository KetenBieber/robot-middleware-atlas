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
