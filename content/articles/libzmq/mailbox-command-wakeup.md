# Mailbox：跨线程命令如何进入同一个 I/O Event Loop

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

libzmq 的 I/O thread 同时承担两类输入：网络 fd 上的 I/O 事件，以及其他线程发来的控制命令。控制命令包括 `stop`、`attach`、`activate_read`、`activate_write`、`pipe_term` 等。它们最终都必须在目标对象所属线程中执行，否则对象状态机会在多个线程之间被直接并发修改。

这使问题从“线程间怎样传一个对象”变成了更严格的运行时约束：

~~~text
网络事件
    \
     +--> 同一个 poller --> I/O thread --> target object
    /
跨线程 command
~~~

关键点不是再造一只线程安全队列，而是让“有 command 到达”成为 poller 能观察到的事件。

## 一个 mailbox 实例究竟被谁共享

`io_thread_t` 内部拥有一只 `mailbox_t`：

~~~text
ctx_t
  |
  +-- io_thread_t #0
  |      |
  |      +-- mailbox_t #0
  |      |      +-- _cpipe
  |      |      +-- _signaler
  |      |      +-- _sync
  |      |      +-- _active
  |      |
  |      +-- poller_t #0
  |
  +-- io_thread_t #1
         |
         +-- mailbox_t #1
         +-- poller_t #1
~~~

不是“每个发送线程一只 mailbox”。对于某个目标 I/O thread，所有向它发送 command 的线程最终都把命令送进**同一个 mailbox 实例**。

因此一只 mailbox 的线程拓扑是：

~~~text
application/admin thread A --\
session/owner thread B -------+--> same mailbox_t --> one receiver thread
another sender thread C -----/
~~~

接收侧只有一个线程；发送侧可以有多个线程。这一点直接决定了为什么内部既出现 `ypipe`，又出现 `mutex`。

## 从 condition_variable 模型出发

标准 C++ 中，一只典型阻塞队列至少有三类共享对象：

~~~text
shared queue       保存事实
mutex              保护共享事实
condition_variable 等待“事实可能发生变化”
~~~

完整对象关系应当是：

~~~text
SharedQueue shared
   +-- shared.q
   +-- shared.m
   +-- shared.cv

producer thread ----\
                     >--- 都持有同一个 shared 的引用
consumer thread ----/
~~~

`condition_variable` 本身不保存任务。即使调用 `notify_one()`，消费者醒来后仍然必须重新检查 `q.empty()`。通知只表示“你等待的条件可能变化了”。

但是 I/O thread 还存在另一个阻塞点：

~~~text
epoll_wait / poll / select
~~~

如果线程已经睡在 `epoll_wait()`，另一线程只做：

~~~text
queue.push(command)
cv.notify_one()
~~~

并不能让 `epoll_wait()` 返回。反过来，如果线程睡在 `cv.wait()`，网络 socket 变成 readable 也不能直接让 condition variable 返回。

所以不能同时让同一线程分别阻塞在两套独立等待机制上。

## mailbox_t 的四个核心成员

固定提交中的成员定义：

~~~cpp
typedef ypipe_t<command_t, command_pipe_granularity> cpipe_t;
cpipe_t _cpipe;

signaler_t _signaler;

mutex_t _sync;

bool _active;
~~~

四个成员解决四个不同问题：

| 成员 | 作用 | 谁访问 |
| --- | --- | --- |
| `_cpipe` | 保存真正的 `command_t` | 多个 sender 经串行化后写；唯一 receiver 读 |
| `_signaler` | 产生 poller 可见 wakeup | sender 触发；receiver/poller 消费 |
| `_sync` | 把多个 sender 串行化成一个逻辑 writer | 所有发送线程 |
| `_active` | 记录 reader 是否还处于主动读 pipe 状态 | receiver thread |

这里最重要的分离是：

~~~text
command data != wakeup signal
~~~

`command_t` 必须存在 `_cpipe` 中；`_signaler` 只负责把睡眠线程叫醒。

## 为什么 ypipe 已经 lock-free，mailbox 还要 mutex

`ypipe_t` 的契约是：

~~~text
one writer
one reader
~~~

而 mailbox 外部实际拓扑是：

~~~text
many writers
one reader
~~~

libzmq 没有把底层 queue 做成通用 MPMC，而是先改变拓扑：

~~~text
sender A --\
sender B ----> _sync mutex --> one logical writer --> ypipe
sender C --/
~~~

锁保护的不是 reader，也不是整个 command 处理过程。它只保护发送侧对 `_cpipe.write()/flush()` 的组合操作。

这类设计比“所有地方都使用最通用的并发容器”更容易推理：

~~~text
MPSC at API boundary
      |
      | serialize producers
      v
SPSC in hot data structure
~~~

## send() 的实际发布顺序

固定提交中的 `mailbox_t::send()`：

~~~cpp
void mailbox_t::send (const command_t &cmd_)
{
    _sync.lock ();
    _cpipe.write (cmd_, false);
    const bool ok = _cpipe.flush ();
    _sync.unlock ();

    if (!ok)
        _signaler.send ();
}
~~~

这里有三个阶段。

### sender-side critical section

~~~text
lock
  |
  +-- write command
  +-- flush publication boundary
unlock
~~~

多个 sender 不能同时修改同一只 `ypipe` 的 writer-side cursor。

### command 先成为可读事实

`_cpipe.write()` 写入 command，`flush()` 再把 completed item 发布给 reader。reader 是否睡眠，由 `flush()` 的返回值给出。

所以数据必须先发布：

~~~text
publish command
      ↓
decide whether wakeup is needed
      ↓
optional signal
~~~

如果顺序反过来：

~~~text
signal
  ↓
reader wakes
  ↓
queue still has no visible command
~~~

就需要额外的同步协议来处理空醒和重新入睡窗口。

### 只有 reader passive 时才 signal

`flush()` 返回：

~~~text
true  -> reader 仍 active
false -> reader 已 passive，需要外部 wakeup
~~~

因此并不是每个 command 都对应一次 kernel signaling。

如果 receiver 正在持续 drain command，这些命令可以只通过内存中的 ypipe 连续流动，不必为每条 command 进行一次 syscall。

## 为什么 unlock 在 signaler.send() 之前

发送路径先释放 `_sync` 再调用 `_signaler.send()`。

这避免一个纯 wakeup 操作扩大 sender mutex 的持有时间。更重要的是，`_sync` 的职责只是让多个 writer 对 ypipe 的修改串行化；它不是“receiver 被唤醒以后必须持有的锁”。

这与 condition variable 的常见模式相似：

~~~text
lock
modify predicate state
unlock
notify
~~~

真正决定正确性的仍然是共享状态与发布协议，不是“notify 时必须拿着 mutex”。

## _active 为什么是 reader-local 状态

构造函数先执行：

~~~cpp
const bool ok = _cpipe.check_read ();
zmq_assert (!ok);
_active = false;
~~~

它刻意让 mailbox 从 passive 状态开始。这样如果 I/O thread 一启动就把 signaler fd 放进 poller，后续第一条 command 的 `flush()` 能识别 reader passive，并触发 `_signaler.send()`。

`_active` 不需要 atomic，因为它只由唯一 receiver thread 读取和修改。sender 不直接访问它；sender 从 ypipe 的共享 publication pointer 判断 reader 状态。

这体现了一个重要的并发设计原则：

~~~text
owner-local state -> 普通字段
cross-thread handoff -> atomic / mutex / OS primitive
~~~

## recv()：先走内存快路径，再进入 OS wait

固定提交中的逻辑：

~~~cpp
int mailbox_t::recv (command_t *cmd_, int timeout_)
{
    if (_active) {
        if (_cpipe.read (cmd_))
            return 0;

        _active = false;
    }

    int rc = _signaler.wait (timeout_);
    if (rc == -1)
        return -1;

    rc = _signaler.recv_failable ();
    if (rc == -1)
        return -1;

    _active = true;

    const bool ok = _cpipe.read (cmd_);
    zmq_assert (ok);
    return 0;
}
~~~

状态机：

~~~text
        cpipe still has data
     +-------------------------+
     |                         |
     v                         |
 ACTIVE -- empty --> PASSIVE --+
                     |
                     | signaler ready
                     v
                   ACTIVE
~~~

所以 mailbox 不是“每次 recv 都 poll 一下 fd”。只要 reader 仍 active，就优先使用纯内存路径。

## signaler 为什么必须暴露 fd

`signaler_t::get_fd()` 返回接收侧 fd。I/O thread 构造时把它和网络 fd 放入同一 poller：

~~~cpp
_mailbox_handle = _poller->add_fd (_mailbox.get_fd (), this);
_poller->set_pollin (_mailbox_handle);
~~~

于是 poller 的等待集合可以统一成：

~~~text
TCP fd
listener fd
IPC fd
mailbox signaler fd
timer backend
...
~~~

poller 不关心某个 ready fd 背后代表“网络 packet”还是“另一个线程让我执行 command”。它只负责把事件交回相应对象。

## signaler_t 在 Linux 上实际做什么

`signaler_t` 是跨平台抽象。在支持 eventfd 的平台上，`send()` 写入一个 `uint64_t 1`：

~~~cpp
const uint64_t inc = 1;
ssize_t sz = write (_w, &inc, sizeof (inc));
~~~

其他平台可以使用 socket pair。对上层 mailbox 来说，唯一需要的性质是：

~~~text
sender can trigger
+
receiver can poll a fd
+
receiver can consume readiness
~~~

这就是为什么控制面 wakeup 可以与 socket readiness 统一。

## notify 不等于立即执行 command

发送线程调用：

~~~text
_signaler.send()
~~~

只意味着 poller 所等待的对象变成 ready。

后续仍有完整调度链：

~~~text
sender thread
  |
  | write signaler
  v
kernel marks fd readable
  |
  v
I/O thread becomes runnable
  |
  | OS scheduler chooses when it runs
  v
poller returns
  |
  v
io_thread_t::in_event()
  |
  v
mailbox.recv()
  |
  v
cmd.destination->process_command(cmd)
~~~

没有任何一步保证“send 后目标 handler 立刻执行”。如果 I/O thread 没有 CPU、正在处理之前的 event、被更高优先级线程抢占，command 都会延后。

## io_thread_t::in_event() 为什么一次 drain 多条 command

固定源码：

~~~cpp
command_t cmd;
int rc = _mailbox.recv (&cmd, 0);

while (rc == 0 || errno == EINTR) {
    if (rc == 0)
        cmd.destination->process_command (cmd);

    rc = _mailbox.recv (&cmd, 0);
}
~~~

一次 poller wakeup 后，不只处理一条 command，而是持续 `recv(..., 0)` 到当前 mailbox 为空。

这样可以把：

~~~text
N commands
~~~

从 N 次 kernel wakeup，压成更接近：

~~~text
1 次 wakeup
+
N 次 memory queue dequeue
~~~

代价也很明确：如果 command 持续不断，I/O thread 可能花较长时间 drain 控制面。源码甚至留下了是否限制单次 command 数量的 TODO，本质上是在 throughput、latency 与 fairness 之间取舍。

## command 最终在哪里执行

每个 `command_t` 都带：

~~~text
destination
type
args
~~~

例如 `activate_read` 最终进入：

~~~cpp
case command_t::activate_read:
    process_activate_read ();
    break;
~~~

sender 不是直接跨线程调用目标对象：

~~~text
thread A -> peer->process_activate_read()
~~~

而是：

~~~text
thread A
  |
  | enqueue command
  v
owner thread mailbox
  |
  v
destination->process_command()
  |
  v
process_activate_read()
~~~

对象状态仍由 owner thread 修改，跨线程只传递 command。这就是 command passing 相比“给所有对象成员都加 mutex”的根本价值：**减少共享 mutable state，而不是仅仅换一种通知 API。**

## destructor 为什么还要碰 _sync

`mailbox_t::~mailbox_t()`：

~~~cpp
_sync.lock ();
_sync.unlock ();
~~~

它没有修改任何数据，作用是等待已经进入 `send()` 临界区的发送线程离开。

因此 mailbox 销毁不能与“另一个线程仍在 send 临界区”无条件并发。这里的 lock/unlock 只是最后一道同步；上层生命周期仍必须保证销毁阶段不会持续产生新的 sender。

## mailbox 的完整数据/控制分离

~~~text
Data path
---------
sender
  -> _sync
  -> _cpipe.write
  -> ypipe flush publication
  -> receiver reads command_t

Wakeup path
-----------
ypipe says reader passive
  -> _signaler.send
  -> poller readiness
  -> io_thread_t::in_event
  -> drain _cpipe
~~~

因此：

~~~text
ypipe 保存工作
signaler 提醒有工作
poller 统一等待边界
owner thread 执行工作
~~~

四者解决的是四种不同职责。

## 与 condition_variable 的对应关系

| 场景 | 共享事实 | 阻塞原语 | 唤醒来源 |
| --- | --- | --- | --- |
| 普通 producer/consumer | queue + predicate | condition_variable | producer notify |
| Event Loop mailbox | ypipe command queue | poller | fd-compatible signaler |
| 网络连接 | socket receive buffer | poller | NIC/kernel readiness |

condition variable 适合“等待某个共享内存 predicate”；fd signaler 适合“把跨线程控制事件并入已有 I/O wait set”。

## 对机器人 Runtime 的直接映射

假设网络线程独占 socket、连接状态与重连状态机，其他线程只允许提交命令：

~~~text
Planner thread --------\
UI/teleop thread -------+--> command mailbox --> Network owner thread
Health monitor thread --/                         |
                                                   +--> sockets
                                                   +--> timers
                                                   +--> reconnect FSM
~~~

此时不要让多个 sender 直接锁住 network connection object 修改状态。更清晰的边界是：

~~~text
other threads publish command
network thread owns mutable network state
~~~

只要 command queue 有界、关闭协议明确、wakeup 可以并入 poller，这套模式可以直接迁移到设备驱动线程、串口 owner thread、CAN owner thread、日志 I/O thread 和数据库 writer thread。
