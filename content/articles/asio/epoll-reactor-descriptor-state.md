# epoll Reactor：为什么每个 fd 都需要 descriptor_state 与分类型 Operation Queue

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

Asio 的 Linux Reactor 不是 `epoll_wait → callback`，而是：

~~~text
fd
↓
descriptor_state
↓
read / write / except operation queues
↓
readiness
↓
perform()
↓
scheduler completion
~~~

## descriptor_state 是用户态 Control Block

核心字段包括：

~~~cpp
mutex mutex_;
int descriptor_;
uint32_t registered_events_;
op_queue<reactor_op> op_queue_[3];
bool try_speculative_[3];
bool shutdown_;
~~~

它同时保存 fd identity、epoll interest、pending operation、per-fd synchronization 与 lifecycle state。

## 为什么 Read / Write / Except 分三条 Queue

同一个 socket 可以同时存在 pending read、write、connect/error。

不同 readiness bit 驱动不同 operation class：

~~~text
EPOLLIN  → read queue
EPOLLOUT → write/connect queue
EPOLLPRI → except queue
~~~

如果全部塞进一条 queue，每次 readiness 都要遍历筛选。

## Speculative I/O 是 Fast Path

`start_op()` 在 op queue 为空且允许 speculative 时，会先尝试 non-blocking syscall。

~~~text
success
→ immediate completion

EAGAIN / would block
→ enqueue operation
→ wait epoll
~~~

只有真的会阻塞，才进入完整 reactor 路径。

## EPOLLET 为什么要求正确 Drain

Asio 使用 edge-triggered epoll。收到 edge 后，用户态必须持续 non-blocking I/O 直到 EAGAIN，否则不会自动得到同一 readiness 的重复通知。

因此 ET 不是简单的“性能更高”，而是把更多状态机责任交给 runtime。

## Interrupter 与 timerfd 也进入 epoll

Reactor 还会把跨线程 interrupter 与 timerfd 注册进 epoll：

~~~text
socket readiness
timer readiness
cross-thread wakeup
↓
one epoll wait set
~~~

这展示了 Linux waitable-fd 模型如何影响 runtime 架构。

其中 interrupter 不是普通“每次写 eventfd 唤醒”的实现：Asio 在 epoll 路径里让 interrupter fd 保持 readable，并通过 `EPOLL_CTL_MOD` 重新触发 ET 通知，以打断阻塞的 `epoll_wait()`。Scheduler 的 condition-variable wait 与这条 Reactor wait 属于两个不同阻塞域，完整 wake routing 见 [Scheduler 与 Reactor 唤醒协议](scheduler-reactor-wakeup.md)。

## Per-descriptor Mutex 为什么优于一把 Global Mutex

如果所有 socket start/cancel 都抢同一 reactor lock，高连接数下会形成共享热点。

把 operation queue 锁分片到 descriptor：

~~~text
fd A operations → mutex A
fd B operations → mutex B
~~~

能把大部分竞争限制在单连接内部。

## Shutdown 不是 Close epoll_fd 就结束

完整 shutdown 还要：

~~~text
mark reactor shutdown
↓
walk descriptor_state
↓
move pending reactor_op out
↓
cancel timers
↓
abandon/destroy operations
~~~

Kernel registration 生命周期与 userspace operation 生命周期必须分别回收。

尤其要注意：`deregister_descriptor()` 可以先让 fd 脱离 epoll、把 descriptor state 标成 shutdown，再把原先 pending 的 operation 作为 `operation_aborted` deferred completion 交给 Scheduler。因此“descriptor 已关闭”和“handler 永远不会再执行”不是同一个边界；这条生命周期链在 [Work、Lifetime 与 Cancellation](work-lifetime-cancellation.md) 中单独展开。

## 可迁移原则

1. OS resource 通常需要用户态 control block。
2. readiness class 可对应独立 operation queue。
3. 快路径先 speculative syscall，慢路径再进入 reactor。
4. 高并发对象优先局部锁，而不是 global lock。
5. shutdown 必须同时清理 kernel state 与 userspace pending state。