# Linger 与终止协议：Data Drain、TERM Barrier 与安全销毁

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

并发 Runtime 最容易被低估的代码通常不是发送，而是关闭。

调用：

~~~cpp
zmq_close(socket);
~~~

从 API 视角只是：

~~~text
这个 socket 不再给应用使用
~~~

但 Runtime 内部可能仍存在：

- socket mailbox 中尚未处理的 command；
- I/O thread 上的 Session；
- Stream Engine；
- Connecter；
- timers；
- poller registration；
- 两端 Pipe；
- Pipe 中已经发布但尚未消费的业务消息；
- 已经发送出去、未来还会返回的 `TERM_ACK`；
- 已经取得目标对象执行资格但尚未处理完的跨线程 command；
- Context 与 Reaper 对 socket 的注册关系。

所以：

> **API close、停止业务数据、对象进入 terminating、异步子对象退出、内存真正释放，是不同时间点。**

libzmq 把这些问题拆成多层协议。

最重要的三个维度是：

~~~text
1. Data Drain Policy
   ZMQ_LINGER

2. Object-tree Quiescence
   own_t
   TERM / TERM_ACK
   sent_seqnum / processed_seqnum

3. Pipe Endpoint Handshake
   delimiter
   pipe_term
   pipe_term_ack
   six-state machine
~~~

而 `socket_base_t` 还额外增加：

~~~text
4. Reaper Barrier
   poller detach
   Context unregister
   send_reaped
   final delete
~~~

因此这篇真正要回答的是：

> **怎样证明“未来已经没有合法执行路径能够再次进入这个对象”，而不是仅仅把一个 stop flag 设成 true？**

前面的 [Pipe 与 HWM](pipe-hwm-backpressure.md) 解释数据面怎样流动，[Command Seqnum 与对象销毁屏障](command-seqnum-quiescence.md) 解释跨线程 command 的 lifetime reservation；这里把它们汇合到完整 shutdown 协议。

而 socket 自身在这些内部 barrier 全部满足后仍不能立刻 `delete`：Reaper 还要摘掉 poller registration、释放 Context slot、回收全局 socket registry，并最终向 Context 发布 DONE。最外层的地址复用与 Runtime join 见 [Context 与 Reaper：Slot 地址空间、Ownership Handoff 与最终销毁屏障](context-reaper-lifecycle.md)。

---

# 一、先把四个时间点彻底分开

## 1. Application Handle Dead

应用调用：

~~~text
zmq_close
~~~

之后：

~~~text
application should no longer use socket
~~~

---

# 二、Runtime Object 仍可能存活

应用不能用了，

并不代表：

~~~text
socket_base_t memory freed
~~~

它还可能：

- 处理 mailbox command；
- 等 Pipe termination；
- 等 Session child；
- 被 Reaper poller 驱动。

---

# 三、Logical Termination 与 Physical Destruction

可以画成：

~~~text
OPEN
 |
 | zmq_close
 v
API-DEAD
 |
 | Reaper takes ownership
 v
TERMINATING
 |
 | child/Pipe/command barriers
 v
LOGICALLY DESTROYED
 |
 | remove poller/context reachability
 v
PHYSICALLY DELETED
~~~

---

# 四、为什么 `close()` 不能直接 `delete this`

假设：

~~~text
thread A:
send command X to socket

application:
close(socket)
delete socket

Reaper/I/O thread later:
process command X
~~~

结果：

~~~text
use-after-free
~~~

---

# 五、`socket_base_t::close()` 实际只做三件核心事

源码：

~~~cpp
int socket_base_t::close()
{
    scoped_optional_lock_t lock(...);

    if (_thread_safe)
        mailbox_safe->clear_signalers();

    _tag = 0xdeadbeef;

    send_reap(this);

    return 0;
}
~~~

---

# 六、第一件：线程安全 Socket 先移除旧 Signaler

`mailbox_safe_t` 可能有：

~~~text
application-side waiters / signalers
~~~

close 后应用侧不应继续等待这个 socket。

所以先：

~~~text
clear_signalers
~~~

切掉旧等待路径。

---

# 七、第二件：把 Socket API 标成 Dead

~~~cpp
_tag = 0xdeadbeef;
~~~

后续 API validity 检查可以拒绝继续使用。

---

# 八、这个 Tag 只解决 API 可用性

它不是：

~~~text
memory reclamation proof
~~~

也不是：

~~~text
mailbox quiescence
~~~

只是：

~~~text
public handle state
~~~

---

# 九、第三件：把 Ownership 转交给 Reaper

~~~cpp
send_reap(this);
~~~

源码注释明确：

~~~text
application thread
→ Reaper thread
~~~

接管剩余 shutdown。

---

# 十、所以 `zmq_close()` 的核心不是“释放”

而是：

> **应用线程放弃 socket，异步销毁责任转移给 Reaper。**

---

# 十一、这是一种 Deferred Reclamation

应用线程不负责：

- drain 所有 child；
- 处理 late command；
- poll TERM_ACK；
- destroy Context registry entry。

它只完成：

~~~text
retire public handle
+
handoff reclamation responsibility
~~~

---

# 十二、Reaper 为什么先把 Socket Mailbox 放进 Poller

`start_reaping()`：

~~~cpp
_poller = poller;

fd = mailbox->get_fd();

_handle =
  _poller->add_fd(fd, this);

_poller->set_pollin(_handle);

terminate();
check_destroy();
~~~

---

# 十三、这个顺序非常关键

不是：

~~~text
terminate
then maybe listen mailbox
~~~

而是：

~~~text
first establish shutdown execution context
then initiate termination
~~~

---

# 十四、为什么

因为 terminate 之后：

~~~text
TERM_ACK
pipe events
child command
~~~

还会继续到 socket mailbox。

如果没有 poller：

~~~text
这些 command 无人消费
→ termination 永远无法闭合
~~~

---

# 十五、Thread-safe Socket 更特殊

它原来使用：

~~~text
mailbox_safe_t
~~~

Reaper 创建新的：

~~~text
_reaper_signaler
~~~

加入 safe mailbox。

---

# 十六、加入后立刻主动 Signal

~~~cpp
_reaper_signaler->send();
~~~

目的：

> **即使 mailbox 中已经存在 command，也确保 Reaper 至少被唤醒一次去检查。**

---

# 十七、这是“接管已有积压”的 Wakeup

如果只注册新 signaler，

但旧 command 已经入队且不会再产生新 transition，

可能出现：

~~~text
queue nonempty
but new Reaper waiter never gets a fresh wake
~~~

主动 signal 关闭这个接管窗口。

---

# 十八、Shutdown Executor 必须先可运行，再发 Shutdown Work

这是一条通用原则：

> **先建立能够消费完成事件的执行环境，再启动异步终止。**

---

# 十九、接下来 `terminate()` 从 Ownership Tree Root 开始

`socket_base_t` 是 `own_t`。

如果没有 owner：

~~~cpp
process_term(
  options.linger.load());
~~~

---

# 二十、默认 Linger 是多少

`options.cpp`：

~~~cpp
linger(-1)
~~~

所以默认：

~~~text
ZMQ_LINGER = -1
~~~

---

# 二十一、官方语义

~~~text
-1
→ infinite linger

0
→ discard pending messages immediately

>0
→ wait at most N milliseconds
~~~

---

# 二十二、Default -1 的后果

如果应用关闭 socket 后继续：

~~~text
zmq_ctx_term()
~~~

Context termination 可能等待：

~~~text
pending messages sent to peer
~~~

而没有有限 deadline。

---

# 二十三、所以“close 返回了”不代表进程退出不会再等

这一点非常容易误判。

~~~text
socket close API returns
~~~

与：

~~~text
Context full termination completes
~~~

是不同 barrier。

---

# 二十四、Linger 回答的唯一核心问题

> **已经进入发送路径但还没有送到 peer 的业务消息，关闭时愿意等多久？**

---

# 二十五、Linger 不回答什么

它不直接回答：

- mailbox 是否还有 command；
- child 是否退出；
- timer 是否取消；
- poller 是否解绑；
- raw pointer 是否仍被 registry 引用；
- socket 内存能否 delete。

---

# 二十六、因此 Linger 不是 General Lifetime Timeout

它是：

~~~text
pending outbound data drain policy
~~~

---

# 二十七、Socket 自己 `process_term()` 首先做什么

~~~cpp
unregister_endpoints(this);
~~~

---

# 二十八、为什么先 Unregister Inproc Endpoint

因为 termination 已开始以后：

~~~text
不应该再允许新的 inproc peer
通过 Context registry
找到这个 socket
并创建新 Pipe
~~~

---

# 二十九、这是 Future Discovery Cutoff

关闭第一原则：

> **先阻止新工作被接纳。**

否则：

~~~text
drain old work
~~~

永远追不上：

~~~text
new work
~~~

---

# 三十、这与 Routing Registry / Subscription Registry 同构

前面看到：

~~~text
ROUTER:
erase routing-id → pipe

XPUB:
remove prefix → pipe
~~~

termination：

~~~text
unregister endpoint
~~~

共同作用：

~~~text
prevent future discovery
~~~

---

# 三十一、然后 Socket 终止 Application-side Pipes

对每条 `_pipes[i]`：

~~~cpp
send_disconnect_msg();
terminate(false);
~~~

---

# 三十二、为什么先发送 Disconnect Message

某些 inproc socket 配置了：

~~~text
disconnect notification payload
~~~

termination 前先把它放进 Pipe。

---

# 三十三、为什么 `terminate(false)`

Socket 这一端不承担：

~~~text
等待业务发送队列 drain
~~~

它要求自己的 attached endpoint 开始立即终止。

---

# 三十四、Linger 并没有因此失效

因为真正控制 network-side pending message drain 的关键对象是：

~~~text
Session
~~~

Linger 会通过：

~~~text
own_t child termination
~~~

继续向 Session 传播。

---

# 三十五、不要把同一连接的两个 Pipe Endpoint 混成一个对象

一条逻辑 connection：

~~~text
socket-side endpoint
<---- ypipe pair ---->
session-side endpoint
~~~

双方生命周期职责不同。

---

# 三十六、Socket-side 先退出业务 API 关系

Session-side 负责：

~~~text
网络 Engine
pending outbound transport work
linger deadline
~~~

---

# 三十七、Socket 为每条 Attached Pipe 登记 TERM_ACK

~~~cpp
register_term_acks(
  _pipes.size());
~~~

---

# 三十八、为什么 Pipe Termination 也计入 own_t Barrier

从 socket 的视角，

这些 Pipe 是：

~~~text
尚未完成的异步生命周期 obligation
~~~

在它们回调：

~~~text
pipe_terminated
~~~

之前，

socket 不应物理销毁。

---

# 三十九、随后进入 `own_t::process_term(linger)`

这会处理：

~~~text
Session
Connecter
Listener
other owned children
~~~

---

# 四十、`own_t` 是一棵显式 Ownership Tree

状态：

~~~cpp
bool _terminating;
atomic_counter_t _sent_seqnum;
uint64_t _processed_seqnum;
own_t *_owner;
int _term_acks;
owned_t _owned;
~~~

---

# 四十一、Ownership Tree 解决什么

不是 C++ 内存所有权语法本身，

而是：

> **哪个对象有权决定 child 什么时候进入 termination。**

---

# 四十二、Child 不应直接自杀

`terminate()`：

~~~text
if root:
    process_term()

else:
    send_term_req(owner, this)
~~~

---

# 四十三、为什么 Child 先请求 Owner

因为 owner 的 `_owned` 集合仍保存：

~~~text
child pointer
~~~

如果 child 自己 delete：

~~~text
owner registry
→ dangling pointer
~~~

---

# 四十四、Lifecycle Authority 应属于 Registry Owner

这是非常通用的设计：

~~~text
registry owns discoverability
→ registry participates in retirement
~~~

---

# 四十五、Owner 收到 `TERM_REQ`

~~~cpp
if (_terminating)
    return;

if (_owned.erase(child) == 0)
    return;

register_term_acks(1);
send_term(child,
          options.linger.load());
~~~

---

# 四十六、先从 Active Ownership Set 删除

意味着：

~~~text
future lifecycle traversal
~~~

不再把它当正常 child。

---

# 四十七、再建立 Outstanding Ack Debt

~~~text
_term_acks += 1
~~~

表示：

~~~text
one asynchronous child termination
must still complete
~~~

---

# 四十八、然后发送 TERM

因此单 child 请求路径是：

~~~text
remove
→ reserve completion debt
→ publish termination command
~~~

---

# 四十九、这与异步 Refcount Debt 很像

先记录：

~~~text
未来必须收到一次 completion
~~~

再启动异步操作。

---

# 五十、整个 Owner 自己进入 Termination 时稍有不同

`process_term()`：

~~~cpp
for each child:
    send_term(child, linger);

register_term_acks(
    _owned.size());

_owned.clear();

_terminating = true;

check_term_acks();
~~~

---

# 五十一、为什么这里是“先 Send，再 Register Ack Count”

表面看起来：

~~~text
ack might race back first
~~~

但要结合 Execution Ownership。

---

# 五十二、Owner Thread 当前正在执行 `process_term()`

即使 child 在另一个线程立刻：

~~~text
send TERM_ACK
~~~

Ack 也只是进入：

~~~text
owner mailbox
~~~

---

# 五十三、Owner 不会在当前调用栈中重入处理 Mailbox

必须等：

~~~text
process_term returns
→ event loop next dispatch
~~~

才处理 ack。

因此在 owner execution model 下：

~~~text
register_term_acks
~~~

仍会先于：

~~~text
process_term_ack
~~~

发生。

---

# 五十四、这说明 Source Ordering 不能脱离 Scheduler 语义看

单看两个线程：

~~~text
send
then increment debt
~~~

似乎危险。

但如果 completion：

~~~text
只能排队
不能同步重入当前 owner
~~~

协议仍可成立。

---

# 五十五、如果未来把 Command Delivery 改成同步 Callback

这个顺序就需要重新验证其并发不变量。

这也是为什么：

> **并发正确性依赖执行模型，不只依赖代码行顺序。**

---

# 五十六、`process_own()` 还有一个关键 Late-child 规则

如果 owner 已经：

~~~text
_terminating == true
~~~

却收到一个晚到：

~~~text
OWN child
~~~

源码不会重新插入 `_owned`。

---

# 五十七、它直接：

~~~cpp
register_term_acks(1);
send_term(child, 0);
~~~

---

# 五十八、为什么 Linger 强制为 0

shutdown 已经开始。

晚到 child 不应：

~~~text
重新延长 root 的数据 drain policy
~~~

它必须立即进入退出。

---

# 五十九、这是 Close Admission Gate

可以总结：

~~~text
RUNNING:
new child → admit

TERMINATING:
new child → reject normal admission
             force immediate retirement
~~~

---

# 六十、比简单 `if closing return` 更完整

因为 child 对象已经存在，

不能只是：

~~~text
ignore it
~~~

否则泄漏。

必须：

~~~text
accept lifecycle responsibility
but never admit into normal runtime
~~~

---

# 六十一、TERM_ACK 是异步 Join

Parent：

~~~text
TERM → child A
TERM → child B
TERM → child C
~~~

然后：

~~~text
_term_acks = 3
~~~

---

# 六十二、Child 完成时

~~~text
TERM_ACK
→ _term_acks--
~~~

直到：

~~~text
0
~~~

---

# 六十三、但 `_term_acks == 0` 仍不能 Delete

真正条件：

~~~cpp
_terminating
&&
_processed_seqnum
  == _sent_seqnum.get()
&&
_term_acks == 0
~~~

---

# 六十四、为什么还需要 Seqnum

TERM_ACK 只证明：

~~~text
已知 child obligations 已完成
~~~

但 mailbox 中可能还有：

~~~text
之前已经合法取得执行资格
但尚未处理的 command
~~~

---

# 六十五、Seqnum 是 Cross-thread Lifetime Reservation Count

`inc_seqnum()`：

~~~cpp
_sent_seqnum.add(1);
~~~

允许不同线程调用。

---

# 六十六、Owner 处理对应 Command 以后

~~~cpp
_processed_seqnum++;
~~~

---

# 六十七、Equality 表示

~~~text
all currently reserved lifecycle-sensitive commands
have crossed the processing boundary
~~~

---

# 六十八、它不是 Business Message Sequence

不要和：

- TCP sequence；
- message id；
- request id；

混淆。

---

# 六十九、它统计的是“未来执行资格”

更完整的 reservation 协议见：

[Command Seqnum 与对象销毁屏障](command-seqnum-quiescence.md)。

---

# 七十、为什么 Seqnum 与 TERM_ACK 必须同时存在

它们覆盖不同来源。

~~~text
TERM_ACK
→ child lifecycle obligations

seqnum equality
→ cross-thread command obligations
~~~

---

# 七十一、一个不能替代另一个

即使：

~~~text
all child TERM_ACK arrived
~~~

仍可能：

~~~text
bind command pending
~~~

---

# 七十二、反过来

即使：

~~~text
sent_seqnum == processed_seqnum
~~~

child 仍可能：

~~~text
draining data
waiting Pipe ACK
~~~

---

# 七十三、所以真正 Barrier 是 Conjunction

\[
Q =
T
\land
(A=0)
\land
(S=P)
\]

其中：

- \(T\)：terminating；
- \(A\)：term acknowledgements；
- \(S\)：sent seqnum；
- \(P\)：processed seqnum。

---

# 七十四、到这里普通 `own_t` 才调用 `process_destroy()`

默认：

~~~cpp
delete this;
~~~

---

# 七十五、但 Socket 是例外

`socket_base_t` 覆盖：

~~~cpp
void process_destroy()
{
    _destroyed = true;
}
~~~

---

# 七十六、为什么只 Mark，不 Delete

因为 socket 还有：

~~~text
Reaper poller registration
Context socket registry
Reaper socket count
~~~

这些是 `own_t` 看不到的外部可达性。

---

# 七十七、所以 Socket 有第二层 Reclamation Barrier

~~~text
own_t internal quiescence
        ↓
_destroyed = true
        ↓
Reaper check_destroy()
        ↓
remove poller fd
        ↓
destroy_socket(context registry)
        ↓
send_reaped()
        ↓
own_t::process_destroy()
        ↓
delete
~~~

---

# 七十八、这是“内部静默”与“外部注册静默”分层

`own_t` 证明：

~~~text
children / commands quiet
~~~

Reaper 再证明：

~~~text
poller / Context no longer references socket
~~~

---

# 七十九、这个模式可以迁移成两个 Quiescence Domain

Domain A：

~~~text
object-internal async work
~~~

Domain B：

~~~text
external scheduler/registry reachability
~~~

---

# 八十、内存 Reclaim 必须等两个 Domain 都静默

只关闭一个 domain：

~~~text
仍然可能 UAF
~~~

---

# 八十一、接下来进入 Data Drain：Session

Socket 的 `own_t::process_term(linger)`：

~~~text
TERM(linger)
~~~

发送给 owned Session。

---

# 八十二、Session 收到 TERM 首先检查 Pipe 状态

如果：

~~~text
_pipe == NULL
_zap_pipe == NULL
_terminating_pipes.empty()
~~~

说明：

~~~text
没有数据面 obligation
~~~

直接：

~~~cpp
own_t::process_term(0);
~~~

---

# 八十三、为什么改成 0

因为没有 Pipe 要 drain。

继续保留 linger deadline 没意义。

---

# 八十四、如果还有 Pipe

Session：

~~~cpp
_pending = true;
~~~

---

# 八十五、`_pending` 的语义

不是：

~~~text
有业务消息
~~~

而是：

> **Session 的 ownership-tree termination 已被推迟，正在等待 Pipe 数据面 termination 闭合。**

---

# 八十六、这是一种 Phase Gate

~~~text
Phase 1:
finish Pipe/data shutdown

Phase 2:
enter own_t child termination
~~~

---

# 八十七、如果 `linger > 0`

Session 安装：

~~~text
linger timer
~~~

deadline：

~~~text
N milliseconds
~~~

---

# 八十八、如果 `linger < 0`

不设置 timer。

所以：

~~~text
delay=true
deadline=none
~~~

---

# 八十九、如果 `linger == 0`

也不设置 timer，

并且：

~~~text
delay=false
~~~

---

# 九十、核心一行

~~~cpp
_pipe->terminate(
    linger != 0);
~~~

把三个语义压缩成一个 bool：

~~~text
linger == 0
→ don't delay for pending data

linger != 0
→ delay until pending data drain
~~~

---

# 九十一、正 Linger 还多一个 Deadline

所以：

~~~text
linger < 0
→ delayed, unbounded

linger > 0
→ delayed, bounded

linger = 0
→ immediate
~~~

---

# 九十二、Linger 不是 Sleep

Session 不会：

~~~text
sleep(N ms)
~~~

它继续由事件驱动运行。

---

# 九十三、Finite Linger 是 Race

两个事件竞争：

~~~text
Pipe drains and terminates
vs
linger timer expires
~~~

---

# 九十四、如果 Pipe 先终止

`pipe_terminated()`：

~~~text
_pipe = NULL
cancel linger timer
~~~

---

# 九十五、然后检查所有 Data-plane Pipe

条件：

~~~text
_pending
&& !_pipe
&& !_zap_pipe
&& _terminating_pipes.empty()
~~~

---

# 九十六、满足后

~~~cpp
_pending = false;
own_t::process_term(0);
~~~

进入下一生命周期阶段。

---

# 九十七、所以 Linger Timer 自动取消

数据已经 drain 完：

~~~text
deadline no longer needed
~~~

---

# 九十八、如果 Timer 先到

~~~cpp
_pipe->terminate(false);
~~~

---

# 九十九、语义切换

~~~text
before deadline:
prefer drain

after deadline:
prefer shutdown
~~~

---

# 一百、这是 Bounded Grace Period

可以抽象：

~~~text
graceful until deadline
then forceful
~~~

---

# 一百零一、非常常见

- HTTP server shutdown；
- thread-pool stop；
- log drain；
- distributed worker eviction；
- actuator communication stop。

---

# 一百零二、为什么 ZAP Pipe 不受 Linger

Session：

~~~cpp
if (_zap_pipe)
    _zap_pipe->terminate(false);
~~~

ZAP 属于：

~~~text
authentication/control path
~~~

不是业务 outbound payload backlog。

---

# 一百零三、这再次说明不要给所有 Queue 一个统一 Drain Policy

不同通道语义不同。

---

# 一百零四、为什么没有 Engine 时要显式 `check_read()`

Session 开始 Pipe terminate 后：

~~~text
delimiter
~~~

可能已经是 inbound queue 中唯一剩余 item。

---

# 一百零五、正常情况下 Engine 会驱动 Read

但：

~~~text
_engine == NULL
~~~

时没有事件源继续消费这个 delimiter。

---

# 一百零六、因此源码主动：

~~~cpp
_pipe->check_read();
~~~

让 delimiter termination 继续推进。

---

# 一百零七、这是“取消执行器后仍要保证完成条件可达”

非常重要。

如果 teardown 顺序：

~~~text
先删除唯一 progress engine
后等待一个只有 engine 能触发的状态
~~~

就会死锁。

---

# 一百零八、Shutdown 必须检查 Progress Dependency

问：

> **我正在等待的 completion，谁会驱动它发生？**

---

# 一百零九、如果驱动者已退出

要么：

- 手动 pump；
- 转移 ownership；
- 改成同步完成；
- 取消等待。

---

# 一百一十、Pipe Termination 为什么需要两条通道

一端 terminate 时同时：

~~~text
control plane:
send_pipe_term(peer)

data plane:
append delimiter to out ypipe
~~~

---

# 一百一十一、为什么 TERM Command 不够

TERM command 可能：

~~~text
比已经排队的业务 payload 更早到 peer owner
~~~

因为它走：

~~~text
mailbox command path
~~~

---

# 一百一十二、如果收到 TERM 就立即销毁

peer inbound queue 中：

~~~text
旧业务消息
~~~

可能被跳过。

---

# 一百一十三、为什么 Delimiter 不够

Delimiter 保持：

~~~text
data queue ordering
~~~

但不能独自完成：

- peer lifecycle handshake；
- simultaneous close；
- endpoint memory ownership transfer。

---

# 一百一十四、所以两条通道各自证明不同事实

TERM：

~~~text
peer has logically requested shutdown
~~~

Delimiter：

~~~text
all data before this marker
has reached queue-order boundary
~~~

---

# 一百一十五、这和 TCP FIN 的思路很像，但不是同一协议

核心思想都是：

~~~text
ordered data boundary
+
lifecycle handshake
~~~

但 libzmq Pipe 是：

~~~text
in-process/cross-thread message channel protocol
~~~

---

# 一百一十六、Pipe 有六个状态

~~~text
active
delimiter_received
waiting_for_delimiter
term_ack_sent
term_req_sent1
term_req_sent2
~~~

---

# 一百一十七、为什么状态这么多

因为存在至少三种独立顺序：

1. TERM 先到；
2. delimiter 先到；
3. 两端同时主动 terminate。

---

# 一百一十八、`active`

普通数据收发状态。

---

# 一百一十九、Peer TERM 先到且要求 Drain

~~~text
active
  |
  | process_pipe_term
  | delay=true
  v
waiting_for_delimiter
~~~

---

# 一百二十、这个状态意味着

~~~text
peer wants to close
but there are still ordered inbound
messages before its delimiter
~~~

---

# 一百二十一、此时为什么不能 ACK

ACK 会让 peer认为：

~~~text
this endpoint no longer references pipe
~~~

但本端还必须继续读 pending data。

---

# 一百二十二、Peer TERM 先到且不要求 Drain

~~~text
active
  |
  | delay=false
  v
term_ack_sent
~~~

同时：

~~~text
_out_pipe = NULL
send_pipe_term_ack(peer)
~~~

---

# 一百二十三、这是 Immediate Drop Path

pending business data 不再构成 barrier。

---

# 一百二十四、Delimiter 先到

~~~text
active
  |
  | process_delimiter
  v
delimiter_received
~~~

---

# 一百二十五、这表示

~~~text
ordered data boundary 已到
but control TERM command 尚未处理
~~~

---

# 一百二十六、为什么不能仅凭 Delimiter Delete

因为 peer lifecycle request/ack protocol 还没闭合。

---

# 一百二十七、之后 TERM 到达

~~~text
delimiter_received
  |
  | process_pipe_term
  v
term_ack_sent
~~~

现在两种条件都满足。

---

# 一百二十八、等待 Delimiter 时 Delimiter 到达

~~~text
waiting_for_delimiter
  |
  | process_delimiter
  v
term_ack_sent
~~~

---

# 一百二十九、此时还做什么

~~~text
rollback own unfinished outbound multipart
_out_pipe = NULL
send ack
~~~

---

# 一百三十、为什么 Rollback

已经处于关闭边界，

未完整 publish 的 multipart 不应该留下半条 message。

---

# 一百三十一、本端主动 Terminate

~~~text
active
  |
  | terminate()
  v
term_req_sent1
~~~

先：

~~~text
send_pipe_term(peer)
~~~

---

# 一百三十二、同时停止普通 Outbound Flow

~~~cpp
_out_active = false;
~~~

---

# 一百三十三、这就是 Admission Closed

termination 一旦开始：

~~~text
no new business writes
~~~

---

# 一百三十四、然后 Rollback 未完成 Multipart

~~~text
partial transaction
→ abort
~~~

---

# 一百三十五、再写 Delimiter

~~~text
delimiter
~~~

进入：

~~~text
out ypipe
~~~

---

# 一百三十六、最关键：Delimiter 不检查 HWM

源码明确：

~~~text
watermarks are not checked
~~~

---

# 一百三十七、为什么必须 Bypass HWM

假设：

~~~text
queue full
→ cannot enqueue delimiter
~~~

而 writer 等：

~~~text
peer consume delimiter
~~~

peer又可能等：

~~~text
termination protocol
~~~

就可能形成 shutdown deadlock。

---

# 一百三十八、所以 Control-progress Marker 必须有特殊 Admission

这和 Proxy：

~~~text
data blocked
control still wakeable
~~~

完全同构。

---

# 一百三十九、系统规则

> **保证系统退出/恢复的控制消息，不能被普通业务背压永久阻塞。**

---

# 一百四十、两端同时 Terminate

本端已经：

~~~text
term_req_sent1
~~~

又收到 peer：

~~~text
pipe_term
~~~

转：

~~~text
term_req_sent2
~~~

---

# 一百四十一、然后立即 ACK 对方

但仍等待：

~~~text
自己之前发出的 TERM
对应的 ACK
~~~

---

# 一百四十二、为什么不能合并成一个 bool

因为两端有：

~~~text
two independent request/ack obligations
~~~

同时关闭时必须知道：

~~~text
我已经回复你了
但你还没回复我
~~~

---

# 一百四十三、这就是 `term_req_sent2`

它编码：

~~~text
both requested
peer request already acknowledged
own request still outstanding
~~~

---

# 一百四十四、`process_pipe_term_ack()` 首先做什么

~~~cpp
_sink->pipe_terminated(this);
~~~

---

# 一百四十五、为什么先通知 Sink

在 delete Pipe 之前，

Socket/Session scheduler/registry 必须：

~~~text
drop all references
~~~

---

# 一百四十六、这就是 Unregister-before-Reclaim

先：

~~~text
remove discoverability/reachability
~~~

后：

~~~text
free memory
~~~

---

# 一百四十七、如果状态是 `term_req_sent1`

说明：

~~~text
我收到对方 ACK
但对方还没收到我的 ACK
~~~

所以 delete 前：

~~~text
send_pipe_term_ack(peer)
~~~

闭合镜像 obligation。

---

# 一百四十八、然后谁释放 Queue

每个 endpoint：

~~~text
deallocates its inbound ypipe
~~~

peer：

~~~text
deallocates opposite direction
~~~

---

# 一百四十九、为什么不让一个 Endpoint 删除两条 Queue

因为每条 queue：

~~~text
read-side ownership
~~~

已经自然对应一个 endpoint。

把 reclaim 分给各自 owner：

~~~text
避免跨线程删除仍可能被对方访问的 queue
~~~

---

# 一百五十、删除 Queue 前手工 Drain `msg_t`

源码：

~~~text
msg_t has no automatic destructor
~~~

所以对 non-conflate queue：

~~~text
while read(msg):
    msg.close()
~~~

---

# 一百五十一、为什么不能直接 Free Queue Memory

未读 `msg_t` 可能持有：

- heap payload；
- refcounted content；
- zero-copy callback；
- metadata refs。

直接 free queue storage 会泄漏这些逻辑资源。

---

# 一百五十二、Container Destruction 与 Element Destruction 是不同责任

尤其自定义 ring/ypipe：

~~~text
free backing pages
~~~

不自动意味着：

~~~text
invoke semantic close for each item
~~~

---

# 一百五十三、最后才

~~~cpp
delete this;
~~~

注意：

~~~text
Pipe
~~~

和：

~~~text
Socket
~~~

删除策略不同。

---

# 一百五十四、Pipe 可以在自己的 ACK 闭合后自 Delete

Socket 还不能，

因为它处于：

~~~text
Reaper external registry
~~~

中。

---

# 一百五十五、这说明同一 Runtime 不需要统一 Reclamation Strategy

每类对象的可达域不同：

~~~text
Pipe
→ peer + sink

Session
→ owner + engine + pipes

Socket
→ Context + Reaper + mailbox + children
~~~

---

# 一百五十六、Lifetime Protocol 应匹配对象 Reachability Graph

不要机械：

~~~text
所有对象 shared_ptr
~~~

也不要机械：

~~~text
所有对象 owner delete child
~~~

---

# 一百五十七、`set_nodelay()` 做什么

~~~cpp
_delay = false;
~~~

用于某些 socket pattern：

~~~text
不希望等待 peer drain
~~~

---

# 一百五十八、但 `terminate(delay)` 还会覆盖 `_delay`

~~~cpp
_delay = delay;
~~~

所以最终 termination 行为取决于：

~~~text
当前调用传入的 drain policy
~~~

而不是一个永远固定的构造配置。

---

# 一百五十九、Pipe 的 Delay 是 Endpoint-local State

同一 Pipe pair 两端：

~~~text
各有自己的 _delay
~~~

因此：

~~~text
谁正在等谁 drain
~~~

必须结合具体 endpoint 看。

---

# 一百六十、Session `_terminating_pipes` 为什么存在

重连等路径中，

旧 Pipe 可能：

~~~text
已经从 current _pipe 脱离
but termination 尚未完成
~~~

---

# 一百六十一、不能因为 `_pipe = NULL` 就认为没有 Lifecycle Debt

所以另外保存：

~~~text
_terminating_pipes
~~~

---

# 一百六十二、这是一种 Retired-but-not-Reclaimed Set

对象已经：

~~~text
不再 active
~~~

但：

~~~text
还不能忘掉
~~~

---

# 一百六十三、它与 RCU Retire List 概念相似

共同模式：

~~~text
logical removal
→ retired list
→ wait grace/completion
→ reclaim
~~~

实现机制不同。

---

# 一百六十四、Session 只有等三个集合全空

~~~text
_pipe == NULL
_zap_pipe == NULL
_terminating_pipes.empty()
~~~

才结束 `_pending`。

---

# 一百六十五、所以 “current pointer is null” 不是 Quiescence Proof

这是非常通用的错误：

~~~text
active pointer removed
→ assume old resources gone
~~~

真实系统常还有：

~~~text
retired set
in-flight list
pending callback
~~~

---

# 一百六十六、Destructor 在这里扮演什么角色

Session destructor：

- assert `_pipe == NULL`；
- assert `_zap_pipe == NULL`；
- assert `_terminating_pipes.empty()`；
- cancel lingering timer if any；
- terminate engine if still present。

---

# 一百六十七、Destructor 不是主要 Shutdown Protocol

它更多是：

~~~text
final invariant enforcement
+
last resource cleanup
~~~

---

# 一百六十八、成熟异步对象不应把所有关闭逻辑塞进 Destructor

因为 destructor 通常无法：

- async wait；
- process future ACK；
- drive event loop；
- negotiate peer protocol。

---

# 一百六十九、正确做法

~~~text
explicit retirement protocol
→ reach safe terminal state
→ destructor verifies
~~~

---

# 一百七十、Linger = -1 为什么有风险

默认无限等待意味着：

~~~text
peer permanently unavailable
+
pending data
+
context termination
~~~

可能让 shutdown 长时间不结束。

---

# 一百七十一、但它也有价值

对于某些应用：

~~~text
don't silently discard buffered data
~~~

比快速退出更重要。

所以默认是 policy choice，

不是 bug。

---

# 一百七十二、机器人控制为什么往往不适合无限 Linger

旧控制指令具有：

~~~text
age
~~~

例如：

~~~text
velocity command t0
~~~

在 2 秒后送达可能比丢弃更危险。

---

# 一百七十三、控制消息应该先定义 Validity Window

例如：

\[
t_{\text{now}} - t_{\text{cmd}}
< T_{\max}
\]

超过：

~~~text
drop
~~~

即使 transport 还愿意 drain。

---

# 一百七十四、Transport Linger 不懂业务时效

它只知道：

~~~text
pending bytes/messages
~~~

不知道：

~~~text
这个 command 现在是否仍安全
~~~

---

# 一百七十五、所以 Safety 不应依赖 Linger

机器人命令更需要：

- timestamp；
- sequence；
- command expiry；
- watchdog；
- state reconciliation。

---

# 一百七十六、Telemetry 又不同

最新状态：

~~~text
old frames low value
~~~

通常：

~~~text
linger = 0 or short
~~~

更合理。

---

# 一百七十七、Audit/Log 又不同

可能：

~~~text
longer bounded drain
~~~

甚至：

~~~text
durable storage
~~~

---

# 一百七十八、同一进程不同 Socket 可以有不同 Linger

这是合理的。

不要把：

~~~text
shutdown policy
~~~

做成全局一个值。

---

# 一百七十九、Monitor Socket 为什么显式 `linger=0`

源码 monitor setup：

~~~text
Never block context termination
on pending event messages
~~~

所以内部 monitor socket：

~~~text
ZMQ_LINGER = 0
~~~

---

# 一百八十、这说明 Observability 数据优先级低于 Shutdown Liveness

监控事件如果 pending：

~~~text
允许丢
~~~

而不是：

~~~text
阻止 Context 退出
~~~

---

# 一百八十一、与 Proxy Capture 原则正好呼应

如果 observability 位于：

~~~text
critical shutdown path
~~~

必须明确：

~~~text
它是否允许拖住业务生命周期
~~~

---

# 一百八十二、Close Protocol 的通用五阶段

可以抽象：

~~~text
1. RETIRE
   stop future admission/discovery

2. DRAIN
   honor data policy until done/deadline

3. QUIESCE
   wait children + commands + callbacks

4. DETACH
   remove poller/registry/external reachability

5. RECLAIM
   free memory
~~~

---

# 一百八十三、libzmq 对应关系

RETIRE：

~~~text
_tag dead
unregister endpoints
_out_active=false
~~~

DRAIN：

~~~text
linger
Pipe delimiter
~~~

QUIESCE：

~~~text
TERM_ACK
seqnum equality
~~~

DETACH：

~~~text
Reaper poller
Context registry
~~~

RECLAIM：

~~~text
delete
~~~

---

# 一百八十四、为什么 Drain 在 Quiesce 之前

因为 Session 的 own_t child termination：

~~~text
被 _pending gate 推迟
~~~

直到 Pipe 数据面完成。

---

# 一百八十五、这建立清晰的 Phase Ordering

~~~text
data-plane policy resolved
        ↓
object subtree termination
        ↓
external scheduler detach
        ↓
free
~~~

---

# 一百八十六、不是所有系统都必须完全同序

但一定要回答：

> **哪些 completion 依赖哪些 executor 仍然存活？**

如果先杀 executor，

后面的 drain/quiesce 可能永远不发生。

---

# 一百八十七、为什么 Shutdown 经常死锁

典型错误：

~~~text
stop I/O thread
then wait network queue drain
~~~

但 queue drain 需要：

~~~text
I/O thread
~~~

于是永远等。

---

# 一百八十八、libzmq 的 no-engine `check_read()` 是一个很小但很好的警示

它说明作者意识到：

~~~text
progress engine 已不存在
~~~

时必须主动推进 delimiter。

---

# 一百八十九、TERM Command 与 Delimiter 是 Control/Data 双轨协议

可以画成：

~~~text
Endpoint A                          Endpoint B

business msg 1  ------------------>
business msg 2  ------------------>

TERM command     ===== mailbox ===>
                                  state:
                                  waiting_for_delimiter

delimiter        ---- ypipe ----->
                                  all prior data crossed
                                  ordered boundary
                                  |
                                  v
                               TERM_ACK
                    <==== command ====
~~~

---

# 一百九十、如果顺序反过来

~~~text
delimiter arrives first
→ delimiter_received

TERM arrives later
→ ACK
~~~

仍可闭合。

---

# 一百九十一、如果双端同时关闭

~~~text
A TERM =====>
       <===== TERM B

A ACK  =====>
       <===== ACK B
~~~

`term_req_sent2` 表示这种交叉状态。

---

# 一百九十二、状态机存在的根本原因是 Message Reordering Across Channels

注意不是：

~~~text
同一个 ypipe 内乱序
~~~

而是：

~~~text
control mailbox
vs
data ypipe
~~~

是两个独立传输通道。

---

# 一百九十三、每条通道内部可以有序

跨通道仍然没有统一顺序。

因此必须：

~~~text
explicit state machine
~~~

处理所有合法 arrival order。

---

# 一百九十四、这对多通道机器人 Runtime 非常重要

例如：

~~~text
shared-memory data plane
+
Unix socket control plane
~~~

即使各自 FIFO，

跨通道：

~~~text
STOP
~~~

与：

~~~text
last data descriptor
~~~

谁先到仍不确定。

---

# 一百九十五、不能依赖“通常哪个更快”

必须编码：

- control-first；
- data-marker-first；
- simultaneous-close。

---

# 一百九十六、Watermark Bypass 的通用场景

适合绕过普通业务 HWM 的不是所有 control。

只应是：

~~~text
guaranteed-progress control
~~~

例如：

- close marker；
- credit return；
- cancellation；
- fatal shutdown；
- lease revocation。

---

# 一百九十七、否则 Control Flood 会绕过 Backpressure

如果所有管理消息都：

~~~text
unbounded bypass
~~~

也会造成另一种资源攻击。

---

# 一百九十八、所以需要区分

~~~text
business admission
critical progress admission
~~~

而不是：

~~~text
control always unlimited
~~~

---

# 一百九十九、`send_disconnect_msg()` 也会 Rollback 未完成 Multipart

disconnect notification 前：

~~~text
rollback incomplete business message
~~~

然后写完整 disconnect msg。

---

# 二百、为什么

不能让：

~~~text
partial business multipart
+
disconnect notification
~~~

拼成一个错误 message。

---

# 二百零一、Transaction Boundary 在 Shutdown 中依然必须维护

关闭不是：

~~~text
可以随便破坏 framing
~~~

反而更需要明确：

~~~text
unfinished transaction abort
then terminal/control message
~~~

---

# 二百零二、`rollback()` 的角色再次统一

在：

- LB send failure；
- ROUTER target loss；
- Pipe terminate；
- disconnect message；

都用于：

~~~text
remove unpublished incomplete multipart tail
~~~

---

# 二百零三、它不是撤销已经被 Peer 观察到的完整消息

边界：

~~~text
only unfinished/unpublished tail
~~~

---

# 二百零四、Shutdown Correctness 的一个总公式

一个异步对象能被安全 reclaim，

至少要满足：

\[
R =
\neg D_f
\land
\neg E_f
\land
Q_i
\land
P_d
\]

其中：

- \(D_f\)：future discoverability；
- \(E_f\)：external event-source reachability；
- \(Q_i\)：internal quiescence；
- \(P_d\)：data policy resolved。

---

# 二百零五、对 libzmq Socket

Future discovery：

~~~text
inproc endpoints unregistered
API tag dead
~~~

External event reachability：

~~~text
Reaper poller removed
Context socket registry removed
~~~

Internal quiescence：

~~~text
TERM_ACK=0
sent_seqnum==processed_seqnum
~~~

Data policy：

~~~text
Session linger resolved
Pipe terminal
~~~

---

# 二百零六、为什么 Shared_ptr 无法自动给出这个公式

`shared_ptr` 只大致提供：

~~~text
memory owner count
~~~

不自动跟踪：

- poller registration；
- mailbox queued command；
- remote TERM_ACK；
- timer；
- pending network data；
- registry discoverability。

---

# 二百零七、Refcount 解决的是“有人持有吗”

Quiescence 解决：

> **过去已经获得访问资格的执行者是否都离开了？**

---

# 二百零八、Linger 解决：

> **业务 backlog 是否还值得等待？**

三者是不同问题。

---

# 二百零九、设计自研 Runtime 时应该显式写 Shutdown Invariant

例如：

~~~text
destroy iff:

accepting_new_work == false
&& active_children == 0
&& in_flight_callbacks == 0
&& queued_commands == 0
&& timers_registered == 0
&& poller_registered == false
&& drain_state in {DONE, EXPIRED, DROP}
~~~

---

# 二百一十、不要只写

~~~cpp
stopping = true;
~~~

然后在 destructor 里希望一切自然结束。

---

# 二百一十一、机器人 Runtime 可以把 Stop 分成三档

## FAST STOP

~~~text
drop stale data
cancel work
preserve control-plane exit
~~~

适合：

- emergency；
- stale control；
- process abort path。

---

# 二百一十二、GRACEFUL STOP

~~~text
stop admission
drain bounded backlog
wait acknowledgements
~~~

适合：

- normal service shutdown；
- bounded telemetry flush。

---

# 二百一十三、DURABLE STOP

~~~text
persist work
then acknowledge shutdown
~~~

适合：

- audit；
- task result；
- non-lossy records。

ZeroMQ linger 只覆盖：

~~~text
GRACEFUL transport drain
~~~

的一部分。

---

# 二百一十四、不要让 E-stop 等待普通 Linger

Safety path 应优先：

~~~text
invalidate old commands
~~~

而不是：

~~~text
ensure every old command eventually sent
~~~

---

# 二百一十五、这与 Latest-value Control 更一致

控制 loop 通常关心：

~~~text
current desired state
~~~

而不是历史 command FIFO 必须全部重放。

---

# 二百一十六、完整 Socket Close 时间线

~~~text
application
    |
    | zmq_close
    v
socket.close
    |
    +-- clear app signalers
    +-- mark tag dead
    +-- send_reap
    |
    v
Reaper
    |
    | start_reaping
    v
register socket mailbox in poller
    |
    v
socket.terminate()
    |
    v
socket.process_term(linger)
    |
    +-- unregister inproc endpoints
    +-- disconnect msgs
    +-- terminate app-side pipes
    +-- register pipe ACK debt
    |
    v
own_t.process_term(linger)
    |
    +-- TERM owned Sessions/children
    +-- register child ACK debt
    +-- stop normal child admission
    |
    v
Session.process_term(linger)
    |
    +-- _pending=true
    |
    +-- linger=0
    |      force pipe termination
    |
    +-- linger>0
    |      delayed termination + timer
    |
    +-- linger<0
           delayed termination, no deadline
    |
    v
Pipe termination state machine
    |
    +-- TERM command
    +-- ordered delimiter
    +-- TERM_ACK
    |
    v
Session pipe_terminated
    |
    +-- cancel timer if drained
    +-- wait all retired pipes
    |
    v
own_t barriers
    |
    +-- term_acks == 0
    +-- sent_seqnum == processed_seqnum
    |
    v
socket.process_destroy()
    |
    +-- _destroyed=true
    |
    v
Reaper check_destroy
    |
    +-- remove poller handle
    +-- remove Context registry entry
    +-- send_reaped
    |
    v
delete socket
~~~

---

# 二百一十七、这里最值得迁移的十二条规则

第一：

> **API close 只应首先 retire public handle；不要把 public retirement 与物理 free 混成一个动作。**

第二：

> **shutdown 开始后先关闭 future admission/discovery，否则 drain 永远可能被新工作追上。**

第三：

> **data drain policy、child lifecycle barrier、cross-thread command barrier、external poller/registry barrier 是不同 quiescence domain。**

第四：

> **Linger 只决定 pending outbound data 等多久，不是通用对象生命周期 timeout。**

第五：

> **finite linger 应建模为“graceful drain 与 deadline 的 race”，而不是 sleep。**

第六：

> **已经进入 terminating 后晚到的新 child 不能重新加入正常 runtime；必须被接管并立即退休。**

第七：

> **TERM_ACK 与 seqnum equality 覆盖不同 obligation，必须同时满足。**

第八：

> **如果 shutdown 正在等待某个事件，必须确认负责驱动该事件的 executor 仍然活着。**

第九：

> **跨 data/control 两条独立 FIFO 的关闭协议必须显式处理两种 arrival order 与双端同时关闭。**

第十：

> **保证退出进展的 marker 不能被普通业务 HWM 永久阻塞。**

第十一：

> **logical unregister / current-pointer=NULL / refcount=0 中任何一个单独条件，都不能自动证明 quiescence。**

第十二：

> **destructor 更适合验证终态，不适合承担需要异步协商的完整 shutdown protocol。**

---

# 二百一十八、最终心智模型

~~~text
                 APPLICATION
                     |
                 zmq_close
                     |
                     v
             +----------------+
             | public retire  |
             +-------+--------+
                     |
                 send_reap
                     |
                     v
                 REAPER
                     |
          mailbox/poller takeover
                     |
                     v
              SOCKET TERM
                     |
       +-------------+-------------+
       |                           |
 future admission              own_t tree
   closed                     TERM / ACK
       |                           |
       |                     seqnum barrier
       |                           |
       +-------------+-------------+
                     |
                  SESSION
                     |
                  linger
             /       |       \
            0       >0       <0
          drop    deadline   infinite
             \       |       /
                     v
                   PIPE
                     |
          TERM + delimiter + ACK
                     |
                     v
              internal quiet
                     |
                     v
             _destroyed=true
                     |
                     v
            external detach
                     |
                     v
                  DELETE
~~~

如果只记一个结论：

> **libzmq 的关闭协议不是“等队列空后 delete”，而是先切断未来可达性，再按 linger 解决业务数据 drain，用 Pipe 的 delimiter+TERM 双轨状态机关闭 endpoint，用 `TERM_ACK + seqnum` 证明对象树和跨线程 command 静默，最后由 Reaper 移除外部 poller/Context 可达性后才真正释放 Socket。**

这里已经把 Socket 交到 Reaper 前后的关闭边界讲清楚；Reaper 如何占用 Context slot、怎样接管 socket mailbox、为什么 `_destroyed=true` 仍不是 delete、以及 Context 为什么必须等最后一个 `REAPED/DONE` 才能释放全局资源，继续看 [Context 与 Reaper：Socket 为什么 Close 以后还不能立刻析构](context-reaper-lifecycle.md)。
