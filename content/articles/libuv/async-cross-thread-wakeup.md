# 跨线程唤醒：atomic pending + eventfd 为什么比“共享一个 Queue”更完整

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

跨线程事件程序最容易漏掉的一件事是：**共享状态已经改变，不代表 sleeping event loop 会自动醒来。**

## 只用共享 Queue 为什么不够

假设 worker thread：

~~~text
push(result_queue)
~~~

而 loop thread 正在：

~~~text
epoll_wait(..., -1)
~~~

内存中的 queue 已经有数据，但内核没有任何 fd readiness 变化，loop 仍然会睡着。

所以跨线程 handoff 必须同时包含：

~~~text
1. shared state publication
2. kernel-visible wakeup
~~~

## Linux 下 libuv 使用 eventfd

初始化：

~~~c
err = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
~~~

eventfd 被注册成 loop 的 async_io_watcher。

其他线程通过 write(eventfd) 唤醒 epoll。

## 为什么 async handle 还有 pending flag

如果多个线程短时间连续调用 uv_async_send()：

~~~text
send
send
send
send
~~~

并不意味着必须向 eventfd 写四次。

libuv 的 async pending 字段把状态压成：

~~~text
bit 0: pending flag
bits 1+: busy counter
~~~

核心发送逻辑：

~~~c
while (!uv__pending_cas(&handle->pending,
                        &current,
                        current + 3))
  if (current & 1)
    return 0;

uv__async_notify(handle);
uv__pending_fetch_add(&handle->pending, -2);
~~~

只要 pending bit 已经是 1，后续 send 可以直接返回。

这就是 event coalescing。

## 为什么 Coalescing 很重要

假设 producer 每 10 微秒发一次 notification，而 event loop 100 微秒才能处理一次。

如果每次都做 syscall：

~~~text
10 writes
→ 10 kernel wakeups/events
~~~

大量 notification 本身会成为负载。

如果业务只需要：

> “至少提醒一次：状态有变化，请重新检查共享状态”

那么 coalescing 是正确的。

这和 condition_variable 的语义非常接近：notification 不是数据本身。

## 为什么 pending CAS 使用 seq_cst

固定源码注释明确说明：

~~~text
all accesses before uv_async_send
must be visible to async callback
~~~

发送线程可能先写：

~~~c
shared_state = new_value;
uv_async_send(&async);
~~~

loop callback 醒来后必须看到 new_value。

因此 pending flag 不只是“防重复通知”的 bit，它还参与 memory ordering。

这条链可以抽象成：

~~~text
Producer writes shared state
↓
seq_cst publish on pending
↓
eventfd wakeup
↓
loop clears pending with seq_cst
↓
Consumer reads shared state
~~~

## 为什么 eventfd 本身不能代替 Memory Ordering

OS wakeup primitive 负责：

~~~text
让 sleeping thread 重新 runnable
~~~

C/C++ atomic ordering 负责：

~~~text
让共享内存读写具有语言层 happens-before
~~~

两层不能混为一谈。

## Loop 侧为什么先 Move async_handles

uv__async_io() 会把 loop->async_handles 移到局部 queue，再逐个检查 pending。

这样用户 callback 即使间接修改 async handle 集合，也不会破坏当前 traversal。

这是和 Timer ready_queue 相同的 two-phase 思路：

~~~text
stabilize container view
→ invoke arbitrary callback
~~~

## Linux 为什么给 async eventfd 加 EPOLLET

linux.c 对 async_io_watcher 特殊加入 EPOLLET。

目标是减少每次 wakeup 都读 eventfd 的 syscall 成本。

因为 async pending bit 才是真正状态，eventfd 只是负责把 epoll 从 sleep 中叫醒。

这再次体现：

> wakeup fd 是提示，不是 work queue。

## 非 Linux 平台为什么可以换成 Pipe 或 EVFILT_USER

真正抽象需求只有：

~~~text
other thread can trigger
event loop poller can observe
non-blocking
can coalesce/recheck state
~~~

Linux eventfd、Unix pipe、kqueue EVFILT_USER 只是不同 OS 对同一控制通道的实现。

## uv__async_spin 为什么还需要 Busy Counter

close async handle 时，可能有别的线程正在 uv_async_send() 的临界窗口内。

因此 close 不能只把 pending bit 设成 closing。

它还要等待：

~~~text
bits 1+ busy counter == 0
~~~

代码先短暂 cpu_relax，自旋太久后再 yield。

这是一个小型 hybrid wait：

~~~text
short spin
→ likely fast completion

long contention
→ yield CPU
~~~

## 这套模型怎样迁移到普通 Runtime

跨线程 task submission 可以设计成：

~~~text
MPSC queue
+
atomic pending flag
+
eventfd
~~~

Producer：

~~~text
enqueue task
↓
if transition idle→pending
  write eventfd
~~~

Consumer：

~~~text
epoll wakes
↓
clear pending
↓
drain bounded batch
↓
if queue still non-empty
  keep runnable / re-arm
~~~

这比“每 push 一次就 write eventfd 一次”更可扩展。

## 什么时候不能 Coalesce

如果 notification 本身携带计数语义：

~~~text
每一个 pulse 都必须处理
每个 interrupt 都代表独立 credit
~~~

简单 pending bit 会丢事件数量。

这时必须让共享状态保存完整计数/队列，wakeup 只负责提醒重新检查。

所以正确问题始终是：

> 哪个对象是真正的数据，哪个对象只是 wakeup？
