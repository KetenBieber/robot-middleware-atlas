# 背压与线程安全：UCS_ERR_NO_RESOURCE 到底意味着什么

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

高性能 transport 不可能承诺每次 send 都立刻拿到 descriptor、queue entry 或 NIC resource。资源暂时用尽时，最危险的设计不是返回错误，而是假装成功并让内部队列无限长。

UCT 把这件事显式表示为 UCS_ERR_NO_RESOURCE。

## Pending queue 是“稍后重试”，不是永久缓存

UCT public API 对 uct_ep_pending_add 的契约很明确：

~~~c
UCT_INLINE_API ucs_status_t
uct_ep_pending_add(uct_ep_h ep, uct_pending_req_t *req, unsigned flags)
{
    return ep->iface->ops.ep_pending_add(ep, req, flags);
}
~~~

注释规定：加入 pending 后，request 暂由 UCT 持有，直到 callback 被调用并返回 UCS_OK；如果返回 UCS_ERR_BUSY，则说明发送资源已经可用，调用者应立即重试。

这个语义比“push 到 vector 以后总会发”严格得多。它要求 request 自身就是可重新驱动的状态机。

## 为什么 no-resource 是背压的一部分

设发送端产生数据速率为 λ，transport 长期服务速率为 μ。若 λ > μ，任何有限系统最终都必须做三选一：阻塞生产者、丢数据、限制/合并请求。把 request 无限制堆在 heap 里只能把崩溃时间推迟。

UCX 的 pending 机制主要解决“底层资源暂时不可用时怎样重试”，但它不会替业务定义“旧图像能不能丢”“控制命令是否必须可靠”。这些仍需要上层 runtime 决定。

因此在机器人 pipeline 中要区分两层 backpressure：UCT 的 resource backpressure 与业务数据年龄策略。前者关心 descriptor/NIC queue；后者关心一帧 80 ms 前的图像还有没有价值。

## 线程模式定义了访问契约

固定源码：

~~~c
typedef enum {
    UCS_THREAD_MODE_SINGLE,
    UCS_THREAD_MODE_SERIALIZED,
    UCS_THREAD_MODE_MULTI,
    UCS_THREAD_MODE_LAST
} ucs_thread_mode_t;
~~~

SINGLE：只有拥有者线程访问。SERIALIZED：多个线程可以访问，但调用必须被串行化。MULTI：允许并发调用。

这三个模式不是“低/中/高性能档位”。如果业务天然单线程拥有 Worker，SINGLE 反而可以减少内部同步。如果多个 producer 共享 Worker，就必须重新考虑 MULTI 的锁成本，或者采用 per-thread Worker + 上层分片。

## Worker 临界区说明了什么

ucp_tag_send_nbx 与 ucp_worker_progress 都会通过 UCP_WORKER_THREAD_CS_ENTER_CONDITIONAL 进入与 thread mode 相关的保护区。这说明 Endpoint/Request/Progress 并不是任意线程随意并发操作的纯函数集合。

对于具身系统，一个常见架构是让 perception/control 线程只提交轻量 descriptor，由专门 communication worker 负责 UCX progress；另一个方案是每个高吞吐 pipeline 独占 Worker。两种方案没有绝对优劣，关键是明确 **所有权、并发访问模式和 progress CPU 预算**。

从实时角度看，背压和线程安全最终都指向同一件事：不能只优化平均带宽。request 队列长度、锁竞争、progress 调度和资源重试都会进入尾延迟。
