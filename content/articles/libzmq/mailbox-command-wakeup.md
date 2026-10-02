# Mailbox：跨线程 Command、Lost Wakeup 与 Owner-Thread 执行模型

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

libzmq 的 I/O thread 同时面对两类完全不同的事件：

~~~text
network readiness
+
cross-thread control command
~~~

例如：

- TCP socket 变成 readable；
- connector 完成连接；
- timer 到期；
- 另一线程要求 `activate_read`；
- 另一线程要求 `attach`；
- 另一线程要求 `pipe_term`；
- administrative thread 要求 `stop`。

真正困难的地方不是：

> 怎么把一个 `command_t` 放进线程安全队列？

而是：

> **I/O thread 已经睡在 poller 里时，怎样让跨线程 command 与 socket readiness 进入同一个等待域，而且不产生 lost wakeup、重复 wakeup 和跨线程直接修改对象状态？**

libzmq 的答案由四层组成：

~~~text
ctx_t::_slots
    ↓
mailbox_t
    ↓
ypipe_t<command_t>
    +
signaler_t
    ↓
poller
    ↓
owner I/O thread
    ↓
destination->process_command()
~~~

其中最关键的不是 `eventfd`，而是 `ypipe` 中 `_c` 指针所实现的 **publication + sleep-state handshake**。

---

## 1. 先看最终目标：对象状态只能由 Owner Thread 执行

假设一个 `session_base_t` 属于 I/O thread 2。

别的线程不能直接这样做：

~~~text
Thread A
    |
    +--> session->process_attach(...)
~~~

因为此时 session 的：

- engine；
- pipe；
- poller registration；
- timer；
- termination state；

可能正在 owner I/O thread 中被访问。

libzmq 选择：

~~~text
Thread A
    |
    | construct command_t
    v
target mailbox
    |
    v
I/O thread 2 wakes
    |
    v
destination->process_command(cmd)
~~~

所以跨线程共享的是：

~~~text
command
~~~

而不是：

~~~text
arbitrary mutable object state
~~~

这就是 owner-thread / command-passing 模型。

---

## 2. object_t 只保存 Thread Identity，不直接保存 Thread Object

`object_t` 有：

~~~text
_ctx
_tid
~~~

发送 command：

~~~cpp
void object_t::send_command(
    const command_t &cmd)
{
    _ctx->send_command(
        cmd.destination->get_tid(),
        cmd);
}
~~~

所以 routing key 是：

~~~text
destination->get_tid()
~~~

而不是：

~~~text
destination mutex
~~~

对象首先声明：

> 我的状态属于哪个 execution domain。

---

## 3. ctx_t::_slots 是 Thread-ID → Mailbox Routing Table

Context 初始化时建立：

~~~text
_slots[term_tid]
→ termination mailbox

_slots[reaper_tid]
→ reaper mailbox

_slots[io_thread_tid]
→ io_thread mailbox

_slots[socket_tid]
→ socket mailbox
~~~

真正 send：

~~~cpp
void ctx_t::send_command(
    uint32_t tid,
    const command_t &command)
{
    _slots[tid]->send(command);
}
~~~

所以命令路由是：

~~~text
destination object
    ↓
destination tid
    ↓
ctx slot
    ↓
target mailbox
~~~

这比：

~~~text
global command queue
~~~

更接近 execution ownership。

---

## 4. 为什么不是一个 Global Queue

如果所有对象都共享：

~~~text
one global MPMC queue
~~~

consumer 还必须再次判断：

~~~text
这个 command 应由哪个 I/O thread 执行？
~~~

并进行二次调度。

libzmq 直接让：

~~~text
thread identity
→ mailbox identity
~~~

于是：

~~~text
routing
~~~

和：

~~~text
execution ownership
~~~

天然对齐。

---

## 5. io_thread_t 拥有自己的 Mailbox 与 Poller

构造：

~~~cpp
_poller = new poller_t(...);

_mailbox_handle =
    _poller->add_fd(
        _mailbox.get_fd(),
        this);

_poller->set_pollin(
    _mailbox_handle);
~~~

所以这个 poller 等待集合中同时有：

~~~text
network fd
listener fd
connector fd
mailbox signaler fd
timer
...
~~~

跨线程 command 被转换成：

~~~text
poller-visible readiness
~~~

从而与 I/O event 共享一个 event loop。

---

# 一、为什么 Queue 和 Wakeup 必须分开

## 6. Command Queue 保存事实，Signaler 只保存“需要醒来”

`mailbox_t`：

~~~cpp
typedef ypipe_t<
    command_t,
    command_pipe_granularity>
    cpipe_t;

cpipe_t _cpipe;
signaler_t _signaler;
mutex_t _sync;
bool _active;
~~~

四个字段的职责完全不同。

| 字段 | 语义 |
|---|---|
| `_cpipe` | command 的权威存储 |
| `_signaler` | 让 poller 返回 |
| `_sync` | 把多个 sender 串行成一个逻辑 writer |
| `_active` | receiver-local 快路径状态 |

最重要的边界是：

~~~text
command data
!=
wakeup notification
~~~

---

## 7. 为什么 Signaler 不能直接承载 Command

你当然可以设计：

~~~text
socketpair
→ serialize entire command
→ write bytes
~~~

但 libzmq 没这么做。

因为 command queue 与 wakeup primitive 对性能要求不同：

~~~text
command
→ userspace memory queue
→ cheap batching

wakeup
→ kernel-visible fd
→ syscall / scheduler involvement
~~~

如果每个 command 都做 kernel write：

~~~text
high-frequency control plane
→ syscall amplification
~~~

---

## 8. Doorbell Pattern

libzmq mailbox 可以抽象成：

~~~text
shared command queue
+
doorbell fd
~~~

producer：

~~~text
publish command
        ↓
if receiver sleeping
        ↓
ring doorbell
~~~

consumer：

~~~text
poller wakes
        ↓
consume doorbell
        ↓
drain command queue
~~~

真正的数据永远在：

~~~text
_cpipe
~~~

signaler 只是：

~~~text
doorbell
~~~

---

# 二、为什么 mailbox 外层是 MPSC，内部却用 SPSC ypipe

## 9. Mailbox 的真实线程拓扑

对于一个 I/O thread：

~~~text
sender A ----\
sender B -----+--> same mailbox --> one receiver
sender C ----/
~~~

所以 API 边界实际上是：

~~~text
MPSC
~~~

但 `ypipe_t` 明确要求：

~~~text
single writer
single reader
~~~

---

## 10. libzmq 没有把 ypipe 做成 MPMC

它选择：

~~~text
many physical writers
        ↓
_sync mutex
        ↓
one logical writer
        ↓
SPSC ypipe
~~~

也就是：

> **通过改变访问拓扑，保留更简单的底层数据结构。**

---

## 11. _sync 只保护 Writer Side

`send()`：

~~~cpp
_sync.lock();

_cpipe.write(cmd, false);
const bool ok =
    _cpipe.flush();

_sync.unlock();

if (!ok)
    _signaler.send();
~~~

锁内只有：

~~~text
write
flush
~~~

receiver 不拿：

~~~text
_sync
~~~

所以 `_sync` 不是：

~~~text
mailbox global mutex
~~~

而是：

~~~text
multi-producer serialization lock
~~~

---

## 12. 为什么这是很有价值的设计

你可以做：

~~~text
generic MPMC queue
~~~

但那意味着 reader/write side 都要遵守更复杂协议。

libzmq 选择：

~~~text
complexity at boundary:
MPSC → mutex serialization

simple hot structure:
SPSC ypipe
~~~

这通常更容易：

- 证明；
- 调优；
- 控制 cache contention；
- 复用已有 SPSC primitive。

---

# 三、ypipe 不是“普通无锁队列”

## 13. ypipe 的四个 Cursor

核心成员：

~~~cpp
yqueue_t<T, N> _queue;

T *_w;
T *_r;
T *_f;

atomic_ptr_t<T> _c;
~~~

其中：

~~~text
_w
writer-only

_r
reader-only

_f
writer-only

_c
writer/reader shared atomic point
~~~

真正跨线程竞争只有：

~~~text
_c
~~~

---

## 14. _queue 本身也按 Ownership 拆分

`yqueue_t`：

~~~text
front/pop
→ reader thread only

back/push
→ writer thread only
~~~

chunk recycle 中只有少数共享点需要 atomic。

这和 Asio Strand 的思路很像：

> 不要先问“这个容器线程安全吗”，先问“哪些字段由谁拥有”。

---

## 15. write() 只写 Writer-private State

~~~cpp
_queue.back() = value;
_queue.push();

if (!incomplete)
    _f = &_queue.back();
~~~

这里还没有让 reader 看见新 command。

它只是：

~~~text
writer creates unpublished items
~~~

---

## 16. _f 表示 Future Flush Boundary

可以理解为：

~~~text
_f
=
如果现在 flush，
reader 最多可以看到哪里
~~~

所以：

~~~text
write
~~~

与：

~~~text
publish
~~~

是分开的。

---

## 17. 为什么要支持 Write-but-not-Flush

在 message pipe 场景中，一个 multipart message 可以：

~~~text
frame 1
frame 2
frame 3
~~~

只有完整 message 才应该对 reader 可见。

因此：

~~~text
queue insertion
~~~

和：

~~~text
publication boundary
~~~

必须分离。

Mailbox command 都是：

~~~cpp
write(cmd, false)
~~~

所以每条 command 都是 complete item。

---

# 四、_c 才是整个 Lost-Wakeup Protocol 的核心

## 18. _c 的定义非常特殊

源码注释：

~~~text
Points past the last flushed item.

If it is NULL,
reader is asleep.
~~~

所以 `_c` 同时表达两种东西：

~~~text
publication boundary
+
reader sleep state
~~~

这是整个算法最重要的一点。

---

## 19. _c 不是“queue head”

正常 active 状态：

~~~text
_c
→ first item beyond published region
~~~

passive 状态：

~~~text
_c == nullptr
~~~

因此：

~~~text
nullptr
~~~

在这里不是：

~~~text
queue empty
~~~

而是：

> **reader 已完成 empty 检查，并把自己登记成 sleeping/passive。**

---

## 20. 为什么 Sleep State 必须进入共享 Atomic State

最危险的经典 race：

~~~text
Consumer:
  sees queue empty

Producer:
  publishes command
  sees no sleeping flag
  decides no wake needed

Consumer:
  goes to sleep
~~~

结果：

~~~text
queue nonempty
+
consumer asleep
+
no future wake
~~~

这就是 lost wakeup。

---

## 21. 错误设计：Queue 与 sleeping bool 分离

例如：

~~~cpp
if (queue.empty())
{
    sleeping = true;
    wait();
}
~~~

producer：

~~~cpp
queue.push(cmd);

if (sleeping)
    signal();
~~~

如果：

~~~text
queue.empty()
~~~

和：

~~~text
sleeping=true
~~~

之间没有共同线性化协议，

就存在：

~~~text
check-before-sleep window
~~~

---

## 22. ypipe 把“Empty Check + Sleep Registration”合成一个 CAS

reader：

~~~cpp
_r =
    _c.cas(
        &_queue.front(),
        NULL);
~~~

这个 CAS 的语义是：

> 如果 shared publication pointer 仍然等于我当前已经消费到的 front，那么没有新数据；把 `_c` 原子地变成 `NULL`，登记我进入 passive 状态。

---

## 23. CAS 成功时意味着什么

假设：

~~~text
_c == front
~~~

说明：

~~~text
reader 已消费所有 published items
~~~

reader CAS：

~~~text
_c:
front → NULL
~~~

成功。

于是：

~~~text
reader officially passive
~~~

之后任何 writer 都必须看见：

~~~text
_c == NULL
~~~

---

## 24. CAS 失败时意味着什么

如果 reader 正准备：

~~~text
front → NULL
~~~

但 writer 已经先 flush：

~~~text
_c:
front → new_f
~~~

reader CAS 失败。

atomic CAS 返回实际值：

~~~text
_r = new_f
~~~

于是 reader知道：

~~~text
新数据已经 published
~~~

因此：

~~~text
不能睡
~~~

---

# 五、writer flush() 与 reader sleep registration 共用同一个线性化点

## 25. flush() 第一件事：没有新数据就什么都不做

~~~cpp
if (_w == _f)
    return true;
~~~

这里：

~~~text
_w
= 已经 flush 到哪里

_f
= 现在可以 flush 到哪里
~~~

相等说明：

~~~text
no unpublished completed item
~~~

---

## 26. 正常 Active Reader 路径

writer：

~~~cpp
if (_c.cas(_w, _f) == _w)
{
    _w = _f;
    return true;
}
~~~

CAS 成功表示：

~~~text
_c 仍是 writer 预期的 old publication boundary
~~~

也就是说 reader 没把它改成：

~~~text
NULL
~~~

因此：

~~~text
reader active
~~~

writer 只需要发布新 boundary。

不需要 kernel wakeup。

---

## 27. Passive Reader 路径

如果：

~~~cpp
_c.cas(_w, _f) != _w
~~~

根据这个 SPSC 协议，这里意味着：

~~~text
_c == NULL
~~~

也就是：

~~~text
reader 已 passive
~~~

writer：

~~~cpp
_c.set(_f);
_w = _f;
return false;
~~~

返回：

~~~text
false
~~~

告诉 mailbox：

> 新 command 已经 published，但 reader 正在睡，必须发外部 wakeup。

---

## 28. 为什么此时可以 non-atomic _c.set()

源码明确：

~~~text
reader is asleep
therefore we don't care about thread-safeness
~~~

因为 reader 已经通过：

~~~text
_c = NULL
~~~

把 execution ownership 交给 writer。

在收到外部 signal、重新执行以前：

~~~text
reader 不会同时操作 _c
~~~

所以 writer 可在这一协议阶段使用：

~~~text
non-atomic set
~~~

这不是“随便优化”。

它依赖明确的 ownership phase。

---

# 六、Lost Wakeup Proof

## 29. Race A：Writer 先 Publish

初始：

~~~text
_c = front
~~~

writer：

~~~text
CAS(front → new_f)
success
~~~

此时：

~~~text
_c = new_f
~~~

reader随后：

~~~text
CAS(front → NULL)
~~~

失败，并读到：

~~~text
new_f
~~~

于是：

~~~text
reader does not sleep
~~~

command 不会丢。

---

## 30. Race B：Reader 先登记 Passive

reader：

~~~text
CAS(front → NULL)
success
~~~

此时：

~~~text
_c = NULL
~~~

writer：

~~~text
CAS(old_w → new_f)
~~~

失败。

writer随后：

~~~text
_c = new_f
flush() returns false
~~~

mailbox：

~~~text
_signaler.send()
~~~

于是 reader 会被 poller 唤醒。

---

## 31. 两种 Interleaving 都安全

所以：

~~~text
writer publish
vs
reader sleep registration
~~~

之间没有：

~~~text
“双方都以为对方会处理”
~~~

的空窗。

真正原因不是：

~~~text
eventfd 很可靠
~~~

而是：

> **publication 与 sleep registration 在同一个 `_c` CAS 上竞争。**

---

## 32. 这比“先 push 再 signal”更完整

很多教程写：

~~~text
push queue
signal eventfd
~~~

这当然可以工作。

但 libzmq 又进一步优化：

~~~text
只有 reader 真 passive
才 signal
~~~

要做到这一点，就必须有：

~~~text
可靠 sleeping-state handshake
~~~

`_c == NULL` 正是这个 handshake。

---

# 七、mailbox_t::_active 为什么不是 Shared Atomic

## 33. _active 只由 Receiver Thread 使用

`recv()`：

~~~cpp
if (_active)
{
    if (_cpipe.read(cmd))
        return 0;

    _active = false;
}
~~~

sender 从不访问：

~~~text
_active
~~~

所以它不需要：

- atomic；
- mutex；
- memory_order。

---

## 34. Sender 通过 _c 感知 Reader 状态

这很关键。

sender不是：

~~~text
if (!mailbox._active)
    signal()
~~~

而是：

~~~text
_cpipe.flush()
→ returns whether reader passive
~~~

因此：

~~~text
owner-local optimization state
~~~

和：

~~~text
cross-thread synchronization state
~~~

被分开了。

---

## 35. Owner-local State 尽量保持普通字段

这是并发设计中非常值得迁移的原则：

~~~text
cross-thread state
→ atomic / lock / protocol

owner-thread state
→ ordinary variable
~~~

不要因为程序有多线程，就把所有 bool 都改成 atomic。

---

# 八、recv() 是 Active / Passive 状态机

## 36. Active 状态先走纯内存快路径

~~~cpp
if (_active)
{
    if (_cpipe.read(cmd))
        return 0;

    _active = false;
}
~~~

只要 ypipe 中还有 prefetched / published command：

~~~text
不碰 kernel
~~~

---

## 37. Queue Drain 完以后进入 Passive

当：

~~~text
_cpipe.read()
→ false
~~~

意味着 `check_read()` 已经完成：

~~~text
_c → NULL
~~~

sleep registration。

然后：

~~~cpp
_active = false;
~~~

只是 receiver-local 镜像。

---

## 38. Passive 状态才进入 Signaler Wait

~~~cpp
int rc =
    _signaler.wait(timeout);
~~~

此时才发生：

~~~text
poll/select/kernel wait
~~~

因此 mailbox 的普通 steady-state fast path是：

~~~text
memory queue only
~~~

---

## 39. Wake 后先消费 Signaler

~~~cpp
rc =
    _signaler.recv_failable();
~~~

这一步清理：

~~~text
doorbell readiness
~~~

然后：

~~~cpp
_active = true;
~~~

再从：

~~~text
_cpipe
~~~

读取真正 command。

---

## 40. Doorbell 和 Data 必须成对维护

如果只读 command、不清 signaler：

~~~text
poller fd remains readable
→ repeated useless wake
~~~

如果只清 signaler、不读 command：

~~~text
notification consumed
但 queue backlog 未处理
~~~

所以：

~~~text
doorbell state
~~~

和：

~~~text
queue state
~~~

必须有明确消费顺序。

---

# 九、为什么构造函数故意先把 Reader 变 Passive

## 41. mailbox_t 初始化

~~~cpp
const bool ok =
    _cpipe.check_read();

zmq_assert(!ok);

_active = false;
~~~

刚创建时 queue 当然为空。

但这里故意执行一次：

~~~text
check_read
~~~

不是多余动作。

---

## 42. 它把 _c 从 Terminator 改成 NULL

于是 mailbox 初始就是：

~~~text
reader passive
~~~

这样 I/O thread 把 signaler fd 注册进 poller 后，

第一条 command：

~~~text
flush()
→ sees _c == NULL
→ returns false
→ signaler.send()
~~~

能够可靠唤醒 poller。

---

## 43. 如果不先进入 Passive 会怎样

假设初始：

~~~text
_c != NULL
~~~

第一条 command flush：

~~~text
returns true
~~~

sender 认为：

~~~text
reader active
no signal needed
~~~

但 I/O thread 可能已经：

~~~text
blocking in poller
~~~

于是第一条 command 可能长期滞留。

所以初始化状态必须和 wait protocol 对齐。

---

# 十、Signaler 是 OS-facing Wake Primitive

## 44. signaler_t::get_fd()

mailbox 暴露：

~~~cpp
fd_t get_fd() const
{
    return _signaler.get_fd();
}
~~~

从 poller 看：

~~~text
mailbox signal
~~~

只是一个普通 readable fd。

---

## 45. Linux eventfd 路径

`send()`：

~~~cpp
const uint64_t inc = 1;
write(_w, &inc, sizeof(inc));
~~~

于是：

~~~text
eventfd counter > 0
→ fd readable
→ poller returns
~~~

---

## 46. 非 eventfd 平台可以使用 socketpair

所以 mailbox 并不依赖：

~~~text
Linux-only API
~~~

它需要的抽象只有：

~~~text
sender can signal
receiver can poll
receiver can consume signal
~~~

---

## 47. Signaler 是 Wait-Set Adapter

它真正做的是：

~~~text
cross-thread memory event
        ↓
convert to fd readiness
        ↓
merge into I/O poller
~~~

可以把它理解为：

> **把线程间控制面事件适配成 I/O wait-set 能理解的形式。**

---

# 十一、为什么不是每条 Command 都发送一次 Signal

## 48. 第一条 Passive→Active Transition 才需要 Doorbell

假设 reader passive：

~~~text
_c = NULL
~~~

command A：

~~~text
flush
→ false
→ signal
~~~

同时 `_c` 已经被 writer恢复为：

~~~text
published boundary
~~~

---

## 49. Command B 随后到来

在 reader 尚未真正运行以前：

~~~text
_c != NULL
~~~

所以第二个 writer：

~~~text
flush
→ CAS success
→ true
→ no signal
~~~

因此：

~~~text
A B C D ...
~~~

可以共享：

~~~text
one wakeup
~~~

---

## 50. 这叫 Wakeup Coalescing

多个 command：

~~~text
many queue publications
~~~

合并成：

~~~text
one transition:
PASSIVE → NEEDS WAKE
~~~

只有状态边界变化需要 kernel notification。

---

## 51. 比“one command one eventfd increment”更省

高频 command 下：

~~~text
N command
~~~

理想可能变成：

~~~text
N memory writes
+
1 syscall wakeup
~~~

而不是：

~~~text
N memory writes
+
N syscall wakeups
~~~

---

# 十二、eventfd 自己仍然支持计数

## 52. signaler::recv() 处理 dummy > 1

eventfd read 会一次拿到累计 counter。

libzmq：

~~~cpp
if (dummy > 1)
{
    const uint64_t inc =
        dummy - 1;

    write(_w, &inc, sizeof(inc));
}
~~~

也就是：

~~~text
consume exactly one logical signal
put remaining signals back
~~~

---

## 53. 为什么还要支持多个 Signal

虽然 mailbox 的 passive handshake 通常会 coalesce，

`signaler_t` 是通用 primitive：

~~~text
其他调用路径
平台差异
并发 signal
~~~

仍可能让 counter 大于 1。

所以底层 primitive 不能假设：

~~~text
永远只有一个 wake
~~~

---

# 十三、I/O Thread 为什么一次 Wake 要 Drain Mailbox

## 54. in_event()

~~~cpp
command_t cmd;

int rc =
    _mailbox.recv(&cmd, 0);

while (
    rc == 0
    || errno == EINTR)
{
    if (rc == 0)
        cmd.destination
           ->process_command(cmd);

    rc =
      _mailbox.recv(&cmd, 0);
}
~~~

它不会：

~~~text
one poll wake
→ one command
→ back to poll
~~~

---

## 55. 它持续 Drain 到 EAGAIN

所以：

~~~text
one wakeup
→ many commands
~~~

进一步摊薄 kernel wakeup 成本。

这是 mailbox batching 的第二层。

---

## 56. 第一层 Coalescing 与第二层 Batching

第一层：

~~~text
multiple sends
→ one signaler wake
~~~

第二层：

~~~text
one in_event
→ drain multiple commands
~~~

两者配合：

~~~text
N commands
≈
1 kernel wakeup
+
N memory dequeues
~~~

---

## 57. 这会引出 Fairness 问题

源码本身也显式提出了这个公平性问题：

~~~text
Do we want to limit number of commands
I/O thread can process in a single go?
~~~

原因是：

~~~text
command stream continuously nonempty
~~~

可能导致 I/O thread 长时间：

~~~text
只处理 control plane
~~~

而不回 poller 服务 network fd。

---

## 58. Throughput 与 Tail Latency 的 Tradeoff

无限 drain：

~~~text
fewer syscalls
better batching
worse fairness
~~~

限制 burst：

~~~text
more poller returns
more overhead
better fairness
~~~

这和：

- Nginx posted-event batch；
- Asio ready batch；
- libzmq proxy burst；

是同一类设计问题。

---

# 十四、process_command() 才是 Owner-thread State Mutation 点

## 59. command_t 本身只是 Envelope

核心字段：

~~~text
destination
type
args
~~~

发送线程只构造：

~~~text
what should happen
~~~

不会直接执行：

~~~text
how target mutates
~~~

---

## 60. Owner Thread Dispatch

mailbox drain：

~~~cpp
cmd.destination
   ->process_command(cmd);
~~~

再进入：

~~~cpp
switch (cmd.type)
{
case activate_read:
    process_activate_read();
    break;

case attach:
    process_attach(...);
    process_seqnum();
    break;

...
}
~~~

状态 mutation 真正发生在：

~~~text
target owner thread
~~~

---

## 61. 这把 Lock-based Sharing 改成 Execution Ownership

传统：

~~~text
Thread A
lock object
modify

Thread B
lock object
modify
~~~

libzmq：

~~~text
Thread A
send intent

Thread B
send intent

Owner thread
execute A
execute B
~~~

所以同步对象从：

~~~text
object field
~~~

提升到了：

~~~text
execution order
~~~

---

# 十五、为什么这比“给 Session 加一把 Mutex”更容易扩展

## 62. Session 内部可能有很多状态

例如：

~~~text
engine pointer
pipe pointer
poller handles
retry timer
mechanism state
termination state
~~~

如果每个跨线程调用者都可以直接访问：

~~~text
需要非常复杂的 lock discipline
~~~

---

## 63. Owner-thread 模型只需要一个规则

~~~text
mutable runtime state
only mutated on owner thread
~~~

跨线程：

~~~text
publish command
~~~

于是对象内部大量字段可以继续：

~~~text
ordinary non-atomic fields
~~~

---

## 64. 复杂性没有消失，只是移动到边界

owner-thread architecture 仍然需要解决：

- command queue；
- wakeup；
- target lifetime；
- shutdown；
- command ordering；
- queue growth。

但这些问题被集中到：

~~~text
mailbox + lifetime protocol
~~~

而不是散落在每一个业务对象字段上。

---

# 十六、Mailbox 解决 Execution Transfer，不解决 Target Lifetime

## 65. command_t 持有 Raw Destination Pointer

~~~text
cmd.destination
~~~

是目标对象 pointer。

因此 mailbox 只保证：

~~~text
command eventually arrives
~~~

不自动保证：

~~~text
destination still alive
~~~

---

## 66. 这就是 seqnum/quiescence 存在的原因

对于需要生命周期保护的 command：

~~~text
sender
    ↓
destination->inc_seqnum()
    ↓
publish command
    ↓
owner thread process_command()
    ↓
process handler
    ↓
process_seqnum()
~~~

于是：

~~~text
future command eligibility
~~~

提前建立：

~~~text
lifetime reservation
~~~

完整机制见 [Command Seqnum 与 Quiescence](command-seqnum-quiescence.md)。

---

## 67. Mailbox 与 Seqnum 是两种完全不同的正确性

Mailbox：

~~~text
transport correctness
+
wakeup correctness
~~~

Seqnum：

~~~text
destination lifetime correctness
~~~

不能因为 command 安全进了 queue，就认为 raw pointer 已经安全。

---

# 十七、Destructor 中的 _sync.lock/unlock 到底保证什么

## 68. mailbox_t::~mailbox_t()

~~~cpp
_sync.lock();
_sync.unlock();
~~~

表面看什么都没做。

实际语义：

~~~text
等待当前已经进入 send() critical section
的 writer 离开
~~~

---

## 69. 它不是完整 Quiescence Protocol

它只保证：

~~~text
destructor 能拿到 _sync 时
没有 writer 正在该 mutex 临界区
~~~

它不保证：

~~~text
之后不会有新的线程再调用 send()
~~~

因此上层仍然必须先保证：

~~~text
no new sender can start
~~~

再 destroy mailbox。

---

## 70. “拿一次 Mutex”不是 Unregister

这和很多 teardown bug 类似：

~~~text
wait current critical section
!=
prevent future entry
~~~

真正 teardown 需要：

~~~text
retire new entry
→ drain active entry
→ reclaim
~~~

Mailbox destructor这里只提供：

~~~text
drain current send critical section
~~~

这一小部分。

---

# 十八、mailbox_safe_t 为什么是另一种模型

## 71. libzmq 还有 mailbox_safe_t

它同样有：

~~~text
cpipe
~~~

但同步边界不同：

- 使用外部 `_sync`；
- 支持 condition variable；
- 支持多个 signaler；
- sender 在 lock 内 broadcast/signal；
- receiver 也围绕同一 external lock 工作。

这不是普通 `mailbox_t` 的简单别名。

---

## 72. mailbox_t 的优化前提更强

普通 mailbox：

~~~text
exactly one receiver
~~~

因此：

~~~text
_active
~~~

可以 receiver-local，

而 ypipe：

~~~text
single reader
~~~

契约成立。

---

## 73. mailbox_safe_t 面对更宽泛的等待/访问模式

因此它不能完全使用：

~~~text
one receiver local state
~~~

同样的优化边界。

这再次证明：

> 数据结构不是脱离线程拓扑单独选出来的。

---

# 十九、与 Asio Wakeup Protocol 的对照

## 74. 两者都把 Wakeup 当 Hint，而不是 Work

Asio：

~~~text
wakeup_event / interrupter
→ wake runtime
→ recheck queue/state
~~~

libzmq：

~~~text
signaler
→ wake poller
→ mailbox.recv()
→ read cpipe
~~~

真正权威状态都不在 wake primitive 中。

---

## 75. 两者都遵循 Publish-before-Notify

Asio：

~~~text
op_queue.push
→ wake worker/reactor
~~~

libzmq：

~~~text
cpipe.write + flush
→ if passive
→ signaler.send
~~~

这是避免：

~~~text
wake sees no work
then sleeps before publication
~~~

的基础。

---

## 76. 差异在于 Sleep-state Detection

Asio Scheduler：

~~~text
event state + waiter count
under Scheduler mutex
~~~

libzmq ypipe：

~~~text
_c == NULL
encoded in atomic publication pointer
~~~

两者都解决：

~~~text
check-before-sleep race
~~~

只是实现范式不同。

---

## 77. Asio 更像 Shared Scheduler State Machine

~~~text
queue
mutex
condition-style event
reactor interrupter
~~~

libzmq mailbox 更像：

~~~text
SPSC publication protocol
+
producer serialization mutex
+
fd doorbell
~~~

这非常适合作为两种 Runtime 风格的对照。

---

# 二十、为什么 _c 使用 acq_rel CAS

## 78. C++11 atomic_ptr 实现

~~~cpp
_ptr.compare_exchange_strong(
    cmp,
    val,
    std::memory_order_acq_rel);
~~~

所以 publication boundary 的 CAS同时承担：

~~~text
release:
writer publishes prior queue writes

acquire:
reader/writer observes peer state
~~~

---

## 79. Queue Element 本身不需要 Atomic

writer先：

~~~text
write command bytes
~~~

再用 `_c` 的 release side：

~~~text
publish visibility
~~~

reader acquire 后：

~~~text
可以读取 published command
~~~

这就是典型：

~~~text
ordinary payload
+
atomic publication pointer
~~~

结构。

---

## 80. 不要把每个 Command Field 都改成 atomic

正确设计是：

~~~text
payload written privately
        ↓
one publication edge
        ↓
reader acquires
~~~

而不是：

~~~text
destination atomic
type atomic
every arg atomic
~~~

这能大幅降低复杂度。

---

# 二十一、yqueue 的 Chunking 为什么与 Mailbox 有关

## 81. command queue 不是每 Push 都 malloc

`yqueue_t<T,N>`：

~~~text
allocate chunk of N elements
~~~

producer/consumer 以 chunk 为单位管理存储。

所以高频 command 不会：

~~~text
one command
→ one heap allocation
~~~

---

## 82. 最近释放 Chunk 还会作为 Spare 复用

reader pop 完一个 chunk：

~~~text
old chunk
→ _spare_chunk
~~~

writer分配新 chunk时：

~~~text
先尝试拿 spare
~~~

这降低 malloc/free 频率。

---

## 83. 一个 Atomic Spare Pointer 就够

因为：

~~~text
writer consumes spare
reader publishes spare
~~~

是另一条非常窄的跨线程 handoff。

Again：

> 只让真正跨 ownership boundary 的字段 atomic。

---

# 二十二、为什么 Poller 不需要知道 command_t

## 84. poller 只认识 i_poll_events

Mailbox fd readable以后：

~~~text
poller
→ io_thread_t::in_event()
~~~

Poller 不知道：

~~~text
command type
destination
args
~~~

所以：

~~~text
event multiplexing
~~~

和：

~~~text
command dispatch
~~~

完全解耦。

---

## 85. 这让 Poller Backend 可以替换

libzmq 支持：

- epoll；
- kqueue；
- poll；
- select；
- devpoll；
- pollset。

只要：

~~~text
signaler exposes pollable fd
~~~

Mailbox 上层协议不变。

---

# 二十三、Stop Command 也是普通 Command

## 86. io_thread_t::stop()

~~~cpp
send_stop();
~~~

也不是：

~~~text
直接跨线程 _poller->stop()
~~~

而是：

~~~text
administrative thread
→ mailbox
→ owner I/O thread
→ process_stop()
~~~

---

## 87. process_stop() 在 Owner Thread 内修改 Poller

~~~cpp
_poller->rm_fd(
    _mailbox_handle);

_poller->stop();
~~~

Poller 文档也明确：

~~~text
start 后，
大多数 add/rm/set/reset 操作
应在 poller callback / worker thread 内完成
~~~

所以 command routing 维护了 poller 的 thread-affinity contract。

---

# 二十四、Command Passing 还能保护第三方 Backend 的线程约束

很多 OS/runtime API 并不天然支持：

~~~text
arbitrary-thread mutation
~~~

owner thread 模式可以让：

~~~text
all backend mutation
~~~

集中发生在正确线程。

例如：

- epoll registration；
- GUI event loop；
- GPU context；
- serial driver；
- CAN device object；
- database connection。

---

# 二十五、一个完整 Send 时序

## 88. Active Reader 情况

~~~text
Producer A
    |
    | _sync.lock
    v
_cpipe.write(cmd)
    |
    v
_cpipe.flush()
    |
    | CAS _c succeeds
    | reader active
    v
return true
    |
    v
_sync.unlock
    |
    | no signal
    v
return
~~~

receiver继续：

~~~text
memory drain
→ sees command
~~~

没有 syscall。

---

## 89. Passive Reader 情况

~~~text
Receiver
    |
    | queue empty
    v
check_read()
    |
    | CAS front -> NULL
    v
reader passive
    |
    v
poller blocks
~~~

producer：

~~~text
write command
    |
    v
flush()
    |
    | CAS sees NULL
    v
publish _c = new_f
    |
    | return false
    v
signaler.send()
    |
    v
fd readable
~~~

receiver：

~~~text
poller returns
    |
    v
in_event()
    |
    v
mailbox.recv()
    |
    v
consume signal
    |
    v
read command
~~~

---

# 二十六、最危险的错误实现是什么

## 90. 错误 1：Queue Empty Check 后直接 Poll

~~~text
if queue empty
    poll()
~~~

没有：

~~~text
sleep registration handshake
~~~

就可能 lost wakeup。

---

## 91. 错误 2：每次 Send 都 Signal

correctness 可能没问题，

但高负载时：

~~~text
command rate
≈ kernel wake syscall rate
~~~

性能很差。

---

## 92. 错误 3：Signal 后再 Publish

~~~text
ring doorbell
→ receiver wakes
→ queue empty
→ receiver sleeps
→ producer publishes
~~~

可能再次产生 lost wakeup。

正确顺序必须：

~~~text
publish
→ notify
~~~

---

## 93. 错误 4：让 Sender 直接调用 Destination

~~~text
sender thread
→ process_command
~~~

这会破坏：

~~~text
owner-thread invariant
~~~

把整个 object graph 重新变成共享 mutable state。

---

## 94. 错误 5：Mailbox Destroy 只靠 Mutex

如果新 sender 在 destructor 后还能开始：

~~~text
lock/unlock
~~~

无法阻止 UAF。

必须先有：

~~~text
lifecycle retirement
~~~

---

# 二十七、对机器人 Runtime 的直接迁移

## 95. CAN Owner Thread

~~~text
Planner
Teleop
Safety
Diagnostics
     \   |   /
      \  |  /
       command mailbox
             |
             v
        CAN owner thread
             |
      +------+------+
      |             |
   CAN fd         timers
      |             |
      +------+------+
             |
             v
       Motor state machine
~~~

所有：

- mode switch；
- enable/disable；
- setpoint update；
- bus reset；

都变成 command。

---

## 96. 为什么比“所有线程直接调 CAN Driver”更清晰

直接共享 driver：

~~~text
planner locks
safety locks
diagnostic locks
rx callback locks
~~~

很快会出现：

- lock ordering；
- teardown race；
- callback reentrancy；
- priority inversion。

owner thread：

~~~text
other threads send intents
driver state single-owned
~~~

问题集中很多。

---

## 97. Serial / UART 同样适用

~~~text
multiple business modules
→ command mailbox
→ serial owner event loop
~~~

owner loop同时等待：

~~~text
UART fd
timer
command signaler fd
~~~

和 libzmq 的结构几乎一样。

---

## 98. Camera / Sensor Runtime 也可以使用 Control Mailbox

例如采集线程独占：

~~~text
camera device
stream state
buffer queue
~~~

其他线程只发送：

~~~text
start
stop
change mode
change exposure
reset
~~~

避免控制 API 与采集 callback 跨线程直接交错。

---

# 二十八、如何从零设计类似 Mailbox

## 99. 先确定 Thread Topology

第一问：

~~~text
多少 producer？
多少 consumer？
~~~

如果是：

~~~text
MPSC
~~~

不一定需要 MPMC。

可以考虑：

~~~text
producer serialization
→ SPSC core
~~~

---

## 100. 再确定 Authoritative Data 与 Wake Signal

必须分开：

~~~text
queue
= truth

eventfd
= notification
~~~

绝不能把：

~~~text
event count
~~~

误当成：

~~~text
queue item count
~~~

---

## 101. 第三步：定义 Sleep Registration Linearization Point

你必须能回答：

> consumer 在哪个原子动作之后，producer 可以确定“它已经睡了，因此必须 wake”？

libzmq 答案：

~~~text
_c CAS to NULL
~~~

如果这个问题答不上来，lost wakeup proof 就没有完成。

---

## 102. 第四步：定义 Wake Coalescing

应该明确：

~~~text
何时第一次 signal
何时后续 producer 不必 signal
何时 receiver 清 signal
~~~

否则系统可能：

~~~text
wake storm
~~~

---

## 103. 第五步：定义 Lifecycle Reservation

如果 command 携带：

~~~text
raw target pointer
~~~

还必须回答：

~~~text
target 在 command 排队和处理期间
为什么不会被 delete？
~~~

libzmq 用：

~~~text
seqnum / TERM_ACK / Reaper
~~~

完成更高层 quiescence。

---

# 二十九、Mailbox 的五层 Ownership

## 104. Producer Ownership

producer只拥有：

~~~text
construct command
+
send critical section
~~~

不拥有 target mutable state。

---

## 105. Queue Writer Ownership

`_sync` 保证某一时刻只有：

~~~text
one logical ypipe writer
~~~

---

## 106. Queue Reader Ownership

只有 mailbox receiver：

~~~text
read/front/pop
~~~

---

## 107. Target State Ownership

只有 destination owner thread：

~~~text
process_command
~~~

---

## 108. Lifetime Ownership

由：

~~~text
own_t seqnum
term ack
reaper
context
~~~

等更高层协议管理。

所以 Mailbox 不是全部 ownership 的终点。

---

# 三十、和 Asio Strand 的关系

## 109. 两者都在解决“不要直接共享 Mutable State”

Strand：

~~~text
multiple submitters
→ one logical serial executor
~~~

libzmq：

~~~text
multiple senders
→ one owner thread mailbox
~~~

---

## 110. 主要区别：Thread Affinity

Asio Strand：

~~~text
logical owner
can migrate across OS threads
~~~

libzmq I/O object：

~~~text
tid identifies specific owner execution domain
~~~

所以 libzmq更接近：

~~~text
Actor / event-loop affinity
~~~

---

## 111. 两者都强调 State Mutation 与 Submission 分离

external thread：

~~~text
submit intent
~~~

owner：

~~~text
perform mutation
~~~

这是非常通用的 Runtime 架构原则。

---

# 三十一、和 Asio Scheduler Wakeup 的关系

## 112. Asio Scheduler Event

~~~text
queue state
+
waiter state
+
condition/event
~~~

---

## 113. libzmq Mailbox

~~~text
ypipe publication pointer
+
passive marker
+
signaler fd
~~~

---

## 114. 两者都必须证明同一件事

~~~text
work published
+
consumer may sleep
~~~

这个交界处不能丢通知。

因此最重要的不是：

~~~text
pthread_cond vs eventfd
~~~

而是：

> **sleep registration 和 work publication 是否共享一个可证明的线性化协议。**

---

# 三十二、可以迁移的十条规则

第一：

> **Queue 保存工作；Wake Primitive 只提示重新检查 Queue。**

第二：

> **MPSC API 不代表底层必须使用 MPMC；可以先串行 producer，再使用 SPSC。**

第三：

> **Reader 是否 asleep 必须成为 cross-thread protocol 的一部分，而不是一个未经同步的旁路 bool。**

第四：

> **Publish-before-notify 是基本顺序。**

第五：

> **只有 Passive→Runnable 这样的状态边界才真正需要 kernel wakeup。**

第六：

> **Owner-local state 尽量保持普通字段，跨 ownership boundary 的少数字段才 atomic。**

第七：

> **Poller 只负责 Event Multiplexing，不应该理解 Command 业务语义。**

第八：

> **Cross-thread command transport 与 target lifetime reservation 是两个独立问题。**

第九：

> **Destructor 等待当前 sender 离开，不等于禁止未来 sender；retire 与 drain 必须分开。**

第十：

> **Owner-thread execution 可以用 execution ownership 替代大量 object-level locking。**

---

# 三十三、最终模型

~~~text
             many producer threads
          A          B          C
           \         |         /
            \        |        /
             v       v       v
                 _sync
                   |
                   | serialize writers
                   v
             ypipe writer side
              _w / _f
                   |
                   | write command
                   v
          atomic publication _c
             /             \
            /               \
 reader active           reader passive
  _c != NULL              _c == NULL
      |                       |
      | flush CAS             | flush CAS fails
      | succeeds              |
      v                       v
 no wakeup needed        restore _c = _f
                              |
                              v
                        signaler.send()
                              |
                              v
                     poller-visible fd
                              |
                              v
                        owner I/O thread
                              |
                              v
                       mailbox.recv()
                              |
                              v
                        ypipe reader
                              |
                              v
              destination->process_command()
                              |
                              v
                    mutable state transition
~~~

如果只记住一个结论，应当是：

> **libzmq Mailbox 的精髓不是“用 eventfd 唤醒线程”，而是让 command publication、reader sleep registration 和 wakeup decision 在同一个可证明的 SPSC handoff 协议中闭合；eventfd 只是最后把这份已经正确建立的内存状态变成 poller 能看到的门铃。**
