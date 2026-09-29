# Thread Safety、Event 与 WaitSet：IPC 数据面之外，线程怎样等待和被唤醒

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## IPC 不等于 Thread-safe

iceoryx2 提供不同 Service variant。

普通 ipc::Service 的核心系统机制是：

~~~rust
type Connection =
    zero_copy_connection::recommended::Ipc;
type Event =
    event::recommended::Ipc;
type Monitoring =
    monitoring::recommended::Ipc;
type Reactor =
    reactor::recommended::Ipc;

type ArcThreadSafetyPolicy<T> =
    SingleThreaded<T>;
~~~

ipc_threadsafe::Service 使用同样的 IPC Connection/Event/Monitoring/Reactor，但把 ArcThreadSafetyPolicy 换成 MutexProtected。

所以：

~~~text
能跨进程通信
≠
同一个 Port 对象可以任意跨线程并发使用
~~~

这种设计把“是否跨进程”和“是否允许同一对象跨线程并发访问”拆成两个独立能力维度。

## 为什么不用“所有对象都默认加锁”

线程安全不是免费的。

如果某个组件明确单线程：

~~~text
sensor worker
owns Publisher
~~~

额外 mutex 只会增加 lock/unlock 指令、cache coherence、复杂性与潜在 priority inversion。

因此把 thread-safety 作为类型级策略，可以让使用者显式选择成本。

## Event 与 Pub/Sub 是两类数据

Pub/Sub 传 payload。

Event 传的是“发生了某件事”的通知。

典型：

~~~text
new frame ready
deadline missed
state changed
shutdown requested
~~~

这些场景不一定需要另一个大 payload queue。

所以 Service trait 把 Event 单独抽象。

## Reactor 解决“等多个事件”

如果程序同时要等：

~~~text
sensor event
control deadline
periodic timer
shutdown
~~~

最差的写法是开四个 polling loop。

Reactor 允许底层把多个 waitable source 交给一个事件复用机制。

Linux recommended IPC Reactor 当前映射到 epoll。

因此 WaitSet 的角色类似：

~~~text
应用级 event multiplexer
      ↓
Service::Reactor
      ↓
OS readiness primitive
~~~

## WaitSet 能附着什么

固定源码文档展示 notification、interval 与 deadline。

WaitSetAttachmentId 区分：

~~~rust
Tick(...)
Deadline(...)
Notification(...)
~~~

因此一个 event loop 能同时处理周期任务和 IPC 事件。

## RAII Guard 为什么重要

attach_notification / attach_interval 返回 WaitSetGuard。

Guard drop 时自动 detach。

这解决一个典型并发 bug：

~~~text
source 已销毁
但 waitset 还保存旧 attachment
~~~

RAII 把 attachment 生命周期绑到 guard。

## wait_and_process 不等于 Runtime Scheduler

WaitSet 只负责：

~~~text
阻塞
→ 事件 ready
→ 调 callback
~~~

它并不自动提供 task priority、WCET 分析、多 worker work stealing 或 deadline scheduler。

所以它更接近事件循环 primitive，而不是 Cyber Scheduler 或 GXF Scheduler。

## 线程唤醒仍然要经过 OS

即使 Event 已经触发：

~~~text
notifier writes/signals
↓
reactor becomes ready
↓
waiting thread becomes runnable
↓
OS scheduler gives CPU
↓
wait_and_process callback runs
~~~

因此“通知已经发出”和“业务代码开始执行”是两个时间点。

## 为什么这对具身 Runtime 有价值

一个高性能 pipeline 可以：

~~~text
payload:
shared-memory zero-copy

notification:
Event / Reactor

compute:
application worker / scheduler
~~~

把数据存储、事件唤醒和任务调度分开。

同样的分层也适用于 GXF/Holoscan 一类 dataflow runtime：数据存储、事件唤醒和任务调度应分别分析。
