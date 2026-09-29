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

## `UCS_ERR_NO_RESOURCE` 不是普通“失败”，而是状态机分支

如果底层 transport 暂时拿不到 TX descriptor、WQE、credit 或其他发送资源，最简单的错误处理是：

~~~cpp
status = try_send(req);
if (status != UCS_OK) {
    fail(req);
}
~~~

但对高性能异步 transport 来说，很多时候资源只是**暂时不可用**。立即把整个操作判死会丢掉本来可以稍后继续的 request；无限 while retry 又会让某个 CPU core 在资源耗尽时疯狂自旋。

所以需要第三种状态：

~~~text
READY_TO_TRY
     ↓
try transport
     ├── UCS_OK
     │      ↓
     │   IN_FLIGHT / COMPLETE
     │
     └── UCS_ERR_NO_RESOURCE
            ↓
         PENDING
            ↓
      transport resource returns
            ↓
         RETRY
~~~

固定源码在 [`ucp_request.c`](https://github.com/openucx/ucx/blob/8a6b06fb880accbb933a79cda893883872c68d9d/src/ucp/core/ucp_request.c#L336) 会把 UCP request 中嵌入的 `uct_pending_req_t` 提交给 `uct_ep_pending_add()`。

这说明一个重要设计：

> **Request 本身就是可恢复执行的协议状态，而不是“一次函数调用留下的一坨参数”。**

这和线程池里的 task object、协程 frame、网络协议 FSM 是同一种程序组织思想。

## Pending Queue 的 ownership 必须写清楚

当 request 被加入 transport pending queue 后，谁可以继续修改它？

至少要区分：

~~~text
application owns request
        ↓ handoff
UCT pending queue owns retry right
        ↓ callback returns / purge
UCP regains control
        ↓
complete / requeue / fail
~~~

如果应用在 request 还挂在 pending queue 时释放对应对象，就会产生典型的 use-after-free。

因此无论 C、C++ 还是 Rust，异步系统都需要把：

~~~text
memory lifetime
!=
API call lifetime
~~~

写进设计。

这也是为什么“把一个局部 struct 指针交给异步回调”通常危险：函数返回不代表异步系统已经停止使用这块内存。

## Transport Backpressure 和 Business Backpressure 不是一回事

UCT 看到的容量可能是：

~~~text
NIC WQE
TX descriptor
transport credit
endpoint pending capacity
~~~

业务看到的容量却是：

~~~text
camera frame queue
control command age
tensor staging pool
planner request count
~~~

这两层不能互相替代。

例如：

~~~text
Camera 60 Hz
↓
Business queue: 100 frames
↓
UCX worker
↓
Transport currently healthy
~~~

即使 UCX 从未返回 `UCS_ERR_NO_RESOURCE`，应用仍可能已经积压一百帧旧图像。

反过来：

~~~text
Application only has 1 latest tensor
↓
UCX transport temporarily NO_RESOURCE
~~~

业务完全没有积压历史，但 transport 层仍需要 pending/retry。

所以完整数据流必须至少画两种 queue：

~~~text
application backlog
transport pending
~~~

并分别定义容量与指标。

## 一个更可控的线程拓扑：Many Producers → One UCX Owner

假设 Camera、LiDAR、VLA、Logger 四条线程都需要发数据。

最直接是所有线程共享一个 `UCS_THREAD_MODE_MULTI` Worker：

~~~text
P0 ─┐
P1 ─┼──> same UCX Worker
P2 ─┤
P3 ─┘
~~~

这在语义上可行，但会让热点锁、Endpoint 状态和 progress ownership 全部集中。

另一种程序组织方式：

~~~text
P0 ─┐
P1 ─┼──> bounded MPSC descriptor queue
P2 ─┤               ↓
P3 ─┘        Communication thread
                    ↓
             SINGLE UCX Worker
~~~

这里把问题拆成两层：

1. 应用线程间通信由一个明确 MPSC queue 负责；
2. UCX Worker 保持单 owner，内部无需为任意业务线程共享付全部同步成本。

这不是说 SINGLE 一定更快，而是它让 ownership 和调度边界更清楚。

## 为什么 descriptor queue 应该传“句柄”，而不是复制 Tensor

应用侧 MPSC queue 的元素可以设计成：

~~~cpp
struct SendCommand {
    BufferHandle payload;
    std::size_t length;
    MemoryType memory_type;
    PeerId peer;
    CompletionToken token;
};
~~~

真正的大 payload 仍留在 CPU pool、GPU VRAM、pinned host buffer 或 shared memory segment。queue 只传 ownership / reference metadata。

这和 iceoryx2 的 PointerOffset、共享内存 descriptor queue，以及 GPU runtime 中 tensor handle 的思路完全一致：

> **控制面传小 descriptor，数据面保持大 payload 原位。**

## 队列满时不要把策略藏在 `try_send` 循环里

应用 submit queue 满以后必须明确选择：

~~~text
Block producer
Drop new
Drop old
Latest-only overwrite
Return error
~~~

不能写成：

~~~cpp
while (!queue.try_push(cmd)) {
    // retry forever
}
~~~

否则所谓“non-blocking UCX”只是把阻塞从 UCX 内部搬到了自己的 busy loop。

Camera/perception 通常更关心新鲜度，适合 small bounded queue + drop-old；控制命令需要结合 deadline 和序号，过期命令应该直接拒绝发送；日志则更可能接受较大有界队列、batching 和明确的 loss accounting。

所以底层同样是 UCX，业务 queue policy 仍然不同。

## `SERIALIZED` 模式其实是一种“外部锁协议”

`UCS_THREAD_MODE_SERIALIZED` 的意思不是 UCX 自动帮你串行，而是调用者承诺：

~~~text
多个线程可以使用这个 Worker
但是同一时间不会并发进入
~~~

典型实现：

~~~cpp
std::mutex worker_mutex;

void send(...) {
    std::lock_guard lock(worker_mutex);
    ucp_tag_send_nbx(...);
}
~~~

它的优点是内部可以省掉部分 multi-thread 同步；缺点是所有调用者可能争同一把外部锁。

对实时控制线程尤其要问：

~~~text
持锁线程会不会做较长的 progress？
会不会在锁内触发 allocation？
priority inversion 怎么处理？
~~~

因此 SERIALIZED 不是性能技巧，而是一份 synchronization contract。

## Shutdown：Pending Request 是关闭协议的一部分

程序退出不能只做：

~~~text
stop = true
join communication thread
destroy worker
~~~

还要回答：

~~~text
submit queue 里尚未 issue 的 command 怎么办？
已经 issue 但未 complete 的 request 怎么办？
挂在 UCT pending queue 的 request 谁 purge？
completion callback 是否还会访问业务对象？
buffer 什么时候才能回 pool？
~~~

一个更完整的关闭状态机：

~~~text
RUNNING
↓ reject new submissions
DRAINING_APPLICATION_QUEUE
↓
DRAINING_UCX_REQUESTS
↓
PURGE / FAIL REMAINING
↓
release buffers
↓
destroy Endpoint
↓
destroy Worker
↓
destroy Context
~~~

这种“先关入口，再清在途，最后销毁 owner”的顺序和线程池、共享内存 runtime、Cyber Component shutdown 是同一条通用原则。

## 真正应该观测的指标

只记录“UCX bandwidth”远远不够。一个机器人 runtime 至少应暴露：

~~~text
application submit queue depth
application drop count
NO_RESOURCE count
pending request count
retry count
progress iterations
submit -> issue latency
issue -> completion latency
buffer hold time
shutdown drain time
~~~

当尾延迟出现时，才能回答：

~~~text
是业务生产太快？
是 Worker 没拿到 CPU？
是 transport 没资源？
是 GPU staging pool 耗尽？
还是 completion 回业务线程太慢？
~~~

这才是把 UCX 当作“可迁移的程序设计教材”来读，而不是只记三个 thread mode 和一个 `UCS_ERR_NO_RESOURCE`。
