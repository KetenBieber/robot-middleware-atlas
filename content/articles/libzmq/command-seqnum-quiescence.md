# libzmq Command Seqnum：在途命令、预留引用与对象销毁屏障

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

[Socket Owner 与 Command Routing](socket-command-owner.md) 已经建立了 libzmq 的控制面模型：

~~~text
foreign thread
    |
    | command_t
    v
target mailbox
    |
    v
owner thread
    |
    v
process_command()
~~~

只看执行权，这个模型已经很漂亮：foreign thread 不直接修改目标对象的大量 mutable state，而是把状态迁移送回 owner thread。

这条命令真正怎样安全进入 owner thread——包括 MPSC sender 如何被串成 SPSC writer、`_c` 怎样同时编码 publication boundary 与 reader passive state、以及 signaler 为什么只在被动读者需要唤醒时才写 fd——见 [Mailbox：跨线程 Command、Lost Wakeup 与 Owner-Thread 执行模型](mailbox-command-wakeup.md)。

但它马上带来一个更困难的问题：

> command 已经发出、还没有被 owner thread 处理时，目标对象能不能先析构？

如果答案是“能”，那么 mailbox 里会留下：

~~~text
command.destination = dangling pointer
~~~

于是 owner thread 迟到一步取出 command 时，就会进入已经释放的对象。

libzmq 没有简单地把所有对象都改成 `shared_ptr`，而是在 `own_t` 里建立了一套非常有意思的生命周期协议：

~~~text
sent_seqnum
processed_seqnum
term_acks
ownership tree
Reaper
~~~

其中最容易误解的就是 `seqnum`。

它不是网络 sequence number，也不是 command 的排序编号。

更准确地说：

> **它是一笔“目标对象尚欠多少个已获准、但还没有完成处理的生命周期敏感 command”的债务。**

理解这套机制，对任何 actor runtime、event loop、设备驱动、机器人通信线程和异步资源管理器都很有价值。

---

## 1. 先从一个最小 UAF 开始

假设只有两个线程：

~~~text
Thread A                      Owner Thread B

object->mailbox.send(X)
                              begin shutdown
                              delete object

                              dequeue X
                              X.destination->process()
~~~

如果 `X.destination` 就是刚才那个 object：

~~~text
mailbox still owns command
object memory already free
~~~

这是最典型的异步 command lifetime bug。

---

## 2. “mailbox 已经 thread-safe”解决不了这个问题

mailbox 能证明：

~~~text
command bytes safely move
from producer to consumer
~~~

但不能证明：

~~~text
command.destination remains alive
until consumer uses it
~~~

这两个问题完全不同：

~~~text
publication safety
!=
destination lifetime
~~~

因此 queue / mailbox 的并发正确性并不等于对象生命周期正确。

---

## 3. libzmq 把 lifetime accounting 放进 own_t

`own_t` 的核心字段：

~~~cpp
bool _terminating;

atomic_counter_t _sent_seqnum;
uint64_t _processed_seqnum;

own_t *_owner;
owned_t _owned;

int _term_acks;
~~~

这几个字段分别回答：

~~~text
_terminating
  是否进入 shutdown

_sent_seqnum
  有多少 seqnum-tracked command
  已经获得发送资格

_processed_seqnum
  owner thread 已处理多少
  seqnum-tracked command

_owner / _owned
  ownership tree

_term_acks
  还有多少 child / external
  termination obligation 未完成
~~~

---

## 4. 最终销毁条件不是一个 bool

固定源码：

~~~cpp
void own_t::check_term_acks ()
{
    if (_terminating
        && _processed_seqnum
             == _sent_seqnum.get ()
        && _term_acks == 0) {

        zmq_assert (_owned.empty ());

        if (_owner)
            send_term_ack (_owner);

        process_destroy ();
    }
}
~~~

因此真正的销毁条件是：

~~~text
terminating
AND
no outstanding seqnum-tracked command
AND
no outstanding termination ack
AND
owned set empty
~~~

只有全部成立才进入物理销毁。

---

## 5. sent_seqnum / processed_seqnum 不是“下一个命令编号”

如果发送三个受保护 command：

~~~text
sent = 0

send A
sent = 1

send B
sent = 2

send C
sent = 3
~~~

owner 处理：

~~~text
process A
processed = 1

process B
processed = 2
~~~

此时：

~~~text
sent = 3
processed = 2
~~~

表达的是：

~~~text
还有 1 笔 lifecycle-sensitive command
没有完成
~~~

它不关心 A/B/C 的业务编号是什么。

---

## 6. 更准确地把它理解成“reservation count”

可以写成：

\[
N_{\text{inflight}}
=
N_{\text{sent}}
-
N_{\text{processed}}
\]

这里的 `sent` 更接近：

~~~text
reservation granted
~~~

而 `processed` 更接近：

~~~text
reservation released
~~~

因此：

~~~text
sent == processed
~~~

说明当前已经建立的所有 reservation 都已经归还。

---

## 7. inc_seqnum 必须发生在 command publication 之前

发送路径：

~~~cpp
if (inc_seqnum_)
    destination_->inc_seqnum ();

command_t cmd;
cmd.destination = destination_;
cmd.type = command_t::bind;

send_command (cmd);
~~~

顺序是：

~~~text
reserve lifetime
    |
    v
publish command
~~~

不能反过来。

---

## 8. 为什么“先发 command，再 inc”会出错

错误版本：

~~~text
publish command X
        |
        v
target begins terminate
sees sent == processed
        |
        v
delete target
        |
        v
sender inc_seqnum()
~~~

最后一步已经在访问 freed object。

即使 sender 没来得及执行最后一步，command 本身也已经指向 dead destination。

所以正确顺序必须是：

~~~text
increment debt
before command becomes visible
~~~

---

## 9. reservation-before-publication 是一种通用模式

它和很多并发协议同构：

~~~text
increment refcount
before publishing pointer

register waiter
before checking condition

mark in-flight
before starting async I/O

reserve request id
before handing work to executor
~~~

共同目标都是：

> 先建立“别人不能把我需要的对象收走”的证明，再暴露异步工作。

---

## 10. inc_seqnum 明确允许跨线程调用

固定源码：

~~~cpp
void own_t::inc_seqnum ()
{
    // This function may be called
    // from a different thread!
    _sent_seqnum.add (1);
}
~~~

因此 `_sent_seqnum` 使用：

~~~text
atomic_counter_t
~~~

而不是普通整数。

---

## 11. processed_seqnum 为什么可以是普通 uint64_t

`_processed_seqnum`：

~~~cpp
uint64_t _processed_seqnum;
~~~

它由：

~~~text
target owner thread
~~~

在 `process_seqnum()` 中推进：

~~~cpp
void own_t::process_seqnum ()
{
    _processed_seqnum++;

    check_term_acks ();
}
~~~

设计假设是：

~~~text
processed-side mutation
belongs to owner execution domain
~~~

而外部线程只通过：

~~~text
atomic sent-side reservation
+
mailbox command
~~~

参与。

---

## 12. 这体现了“只把真正跨线程的字段做 atomic”

如果把整个 `own_t` 都做成：

~~~text
atomic fields everywhere
~~~

代码很难理解。

libzmq 的思路是：

~~~text
foreign side:
  only atomic reservation + mailbox publication

owner side:
  normal state transition
~~~

同步边界很窄。

---

## 13. atomic_counter_t 的 C++11 路径

固定实现中：

~~~cpp
old_value =
    _value.fetch_add(
        increment_,
        std::memory_order_acq_rel);
~~~

因此跨线程 `inc_seqnum()` 本身是原子的。

但这里必须强调：

> `seqnum` 不是 command payload 的 publication primitive。

---

## 14. command 内容的可见性来自 mailbox / queue 协议

command 中还有：

~~~text
destination
type
args
~~~

这些字段如何从 sender 对 owner 可见，是：

~~~text
mailbox
ypipe
signaler
producer/consumer synchronization
~~~

负责的。

`seqnum` 只回答：

~~~text
target can be reclaimed yet?
~~~

不能把这两个 synchronization domain 混在一起。

---

## 15. 哪些 command 会自动 process_seqnum

`object_t::process_command()`：

~~~cpp
case command_t::plug:
    process_plug ();
    process_seqnum ();
    break;

case command_t::own:
    process_own (...);
    process_seqnum ();
    break;

case command_t::attach:
    process_attach (...);
    process_seqnum ();
    break;

case command_t::bind:
    process_bind (...);
    process_seqnum ();
    break;

case command_t::inproc_connected:
    process_seqnum ();
    break;
~~~

这几类 command 被纳入 seqnum lifetime barrier。

---

## 16. process_seqnum 必须放在业务 handler 后面

例如：

~~~cpp
process_bind (pipe);
process_seqnum ();
~~~

不能写成：

~~~cpp
process_seqnum ();
process_bind (pipe);
~~~

因为 `process_seqnum()` 内部会调用：

~~~text
check_term_acks()
~~~

它可能发现：

~~~text
terminating
processed == sent
term_acks == 0
~~~

进而：

~~~text
process_destroy()
~~~

---

## 17. processed 过早推进可能让当前对象在 handler 前被销毁

错误时序：

~~~text
command arrives
    |
    v
processed++
    |
    v
check_term_acks
    |
    v
delete this
    |
    v
process_bind(...)
~~~

显然不成立。

所以这类 command 的 pattern 是：

~~~text
execute state transition
    |
    v
release lifecycle reservation
~~~

---

## 18. process_seqnum 是 completion，不是 dequeue acknowledgement

command 从 mailbox 被读出来并不代表债务可以马上释放。

真正 completion point 是：

~~~text
command-specific state mutation finished
~~~

这和异步 request 的：

~~~text
dequeued
!=
completed
~~~

完全一样。

---

## 19. 为什么有些 command 不走 seqnum

例如：

~~~text
activate_read
activate_write
pipe_term
term
term_ack
reap
reaped
~~~

并不是全部经过：

~~~text
inc_seqnum / process_seqnum
~~~

这不是遗漏。

它们属于不同 lifecycle domain。

---

## 20. seqnum 只保护 own_t 的一类跨 owner command

`inc_seqnum()` 是：

~~~text
own_t-level lifecycle primitive
~~~

不是整个 libzmq 所有 command 的统一引用计数。

例如 `pipe_t` 有自己的：

~~~text
pipe termination protocol
~~~

父子对象又有：

~~~text
TERM / TERM_ACK
~~~

socket 最外层还有：

~~~text
Reaper
~~~

不同对象使用不同 barrier。

---

## 21. TERM_ACK 为什么不简单等价于 seqnum

`seqnum` 回答：

~~~text
有没有已经获得目标对象执行资格
但尚未处理完的 command？
~~~

`TERM_ACK` 回答：

~~~text
我已经要求哪些 child / resource
进入终止，
它们是否真的完成？
~~~

一个是：

~~~text
in-flight command barrier
~~~

另一个是：

~~~text
shutdown dependency barrier
~~~

---

## 22. 两类 barrier 关注不同的因果关系

seqnum：

~~~text
sender
  |
  | reserve
  v
target command
  |
  | finish
  v
release reservation
~~~

TERM_ACK：

~~~text
parent
  |
  | request child termination
  v
child
  |
  | terminal
  v
TERM_ACK
~~~

所以最终销毁需要两者同时归零。

---

## 23. launch_child 同时产生两种关系

`launch_child()`：

~~~cpp
object_->set_owner (this);

send_plug (object_);

send_own (this, object_);
~~~

第一条：

~~~text
send_plug(child)
~~~

目标是 child。

默认会：

~~~text
child.sent_seqnum++
~~~

第二条：

~~~text
send_own(parent, child)
~~~

目标是 parent。

会：

~~~text
parent.sent_seqnum++
~~~

---

## 24. 为什么 parent 的 own command 也需要 seqnum

`send_own(this, child)` 最终会：

~~~text
parent->_owned.insert(child)
~~~

假设 parent 此时正准备销毁。

如果没有 reservation：

~~~text
child created
send OWN to parent mailbox

parent:
  thinks no work pending
  deletes itself

OWN arrives
  destination = dead parent
~~~

所以新 child 的 ownership registration 本身也是 lifecycle-sensitive command。

---

## 25. terminating 时收到 late OWN 怎么办

`process_own()`：

~~~cpp
if (_terminating) {
    register_term_acks (1);
    send_term (object_, 0);
    return;
}

_owned.insert (object_);
~~~

也就是说：

~~~text
OWN command arrived
~~~

并不保证：

~~~text
child becomes active member
~~~

如果 parent 已经 shutdown：

~~~text
late child
→ immediately terminate
~~~

这避免 termination 过程中又把对象重新加回 active tree。

---

## 26. 这说明 seqnum 只保证“命令能安全到达”

seqnum 并不保证：

~~~text
command will be accepted semantically
~~~

它只保证：

~~~text
target remains alive
long enough to decide
what to do with command
~~~

这个区别非常重要。

---

## 27. seqnum 是 lifetime permission，不是 business success

可以把它理解成：

~~~text
I promise not to reclaim target
before this command has been adjudicated
~~~

而不是：

~~~text
I promise command will succeed
~~~

---

## 28. 最有意思的地方：find_endpoint 的 pre-reservation

inproc lookup：

~~~cpp
endpoint_t endpoint =
    it->second;

endpoint.socket->inc_seqnum ();

return endpoint;
~~~

注意：

~~~text
inc_seqnum
发生在 endpoint registry lock 内
~~~

然后才把：

~~~text
socket*
~~~

返回给调用者。

---

## 29. 这不是普通 lookup，而是“lookup + lifetime reservation”

语义不是：

~~~text
find pointer
unlock
later maybe use
~~~

而是：

~~~text
lock registry
find target
reserve target lifetime
unlock
return target pointer
~~~

所以返回的 raw pointer 并不是裸奔。

它伴随一笔：

~~~text
future command debt
~~~

---

## 30. unregister_endpoint 使用同一把 registry lock

endpoint 删除同样：

~~~cpp
scoped_lock_t locker(
    _endpoints_sync);
~~~

因此 lookup 与 unregister 有一个明确的互斥顺序。

---

## 31. 两种合法时序

情况 A：lookup 先拿锁。

~~~text
lookup lock
find socket
inc_seqnum
unlock

unregister lock
erase endpoint
unlock
~~~

即使 endpoint 随后从 registry 消失：

~~~text
旧 lookup 已经提前建立 reservation
~~~

所以目标不能因为 shutdown 立即销毁。

---

## 32. 情况 B：unregister 先拿锁

~~~text
unregister lock
erase endpoint
unlock

lookup lock
not found
~~~

后来的 caller 根本拿不到 target pointer。

---

## 33. 这正是“关闭入口 + drain 旧引用”

可以抽象成：

~~~text
registry mutex
    |
    +-- acquire path:
    |     lookup
    |     reserve
    |
    +-- retire path:
          erase visibility
~~~

然后：

~~~text
final destroy
wait reservations drain
~~~

这与我们前面在 callback quiescence 中建立的模型完全一致。

---

## 34. raw pointer 也可以安全，但必须配套 reservation protocol

很多人看到：

~~~cpp
socket_base_t *socket;
~~~

会直接认为：

~~~text
raw pointer = unsafe
~~~

并不准确。

真正要问的是：

~~~text
pointer 在离开 owner registry 后
谁保证 pointee lifetime？
~~~

libzmq 的答案之一就是：

~~~text
seqnum reservation
~~~

---

## 35. 为什么后续 send_bind 要传 false

`find_endpoint()` 已经：

~~~text
peer.socket->inc_seqnum()
~~~

所以真正发：

~~~cpp
send_bind(
    peer.socket,
    new_pipes[1],
    false);
~~~

这里的 `false` 表示：

> 这条 bind command 的 lifecycle debt 已经在更早的 lookup 阶段建立。

---

## 36. inc_seqnum=false 不是性能选项

它是一个 correctness contract。

含义是：

~~~text
caller proves:
this command already owns
one seqnum reservation
~~~

如果证明不成立，就可能 UAF。

---

## 37. 少加一次会怎样

如果：

~~~text
没有 reservation
~~~

却写：

~~~cpp
send_bind(target, pipe, false);
~~~

可能发生：

~~~text
target begins shutdown
processed == sent
term_acks == 0
delete target

bind command arrives later
~~~

这是经典 dangling destination。

---

## 38. 多加一次也会出问题

反过来，如果前面已经：

~~~text
inc_seqnum()
~~~

后面又：

~~~text
send_bind(..., true)
~~~

就得到：

~~~text
sent += 2
~~~

但 bind 被处理只执行一次：

~~~text
processed += 1
~~~

于是：

~~~text
processed < sent forever
~~~

对象可能永远达不到销毁条件。

---

## 39. 所以 seqnum 错误有两种对称失败

少记 debt：

~~~text
premature reclaim
→ UAF
~~~

多记 debt：

~~~text
debt never clears
→ shutdown hang / leak-like retention
~~~

这就是为什么 `inc_seqnum=false` 是一个非常重的 proof obligation。

---

## 40. pending inproc connection 又展示了另一种 reservation

没有 binder 时：

~~~cpp
endpoint_.socket->inc_seqnum ();

_pending_connections.insert(
    addr_,
    pending_connection);
~~~

这里 reservation 属于：

~~~text
connecting socket
~~~

因为 pending connection registry 将来还需要回到这个 socket 完成连接协议。

---

## 41. pending registry 本身不能代替对象 lifetime

`_pending_connections` 保存连接信息。

如果 connector socket 可以在等待期间被直接 delete：

~~~text
pending registry
→ dangling socket pointer
~~~

所以在插入 pending state 前先：

~~~text
inc_seqnum
~~~

将对象 lifetime 延长到 pending resolution。

---

## 42. inproc_connected 是一个纯 completion command

后续 binder 出现时：

~~~cpp
send_inproc_connected(
    pending_connection.endpoint.socket);
~~~

这个 command 没有显式 payload state transition。

在 `process_command()` 中：

~~~cpp
case command_t::inproc_connected:
    process_seqnum ();
    break;
~~~

它的主要作用就是：

~~~text
release earlier pending reservation
~~~

---

## 43. 这是一种很漂亮的“lifetime completion token”

一开始：

~~~text
pending connection inserted
→ sent_seqnum++
~~~

最终：

~~~text
inproc_connected arrives
→ processed_seqnum++
~~~

于是一个可能跨越很长时间的 pending registry state 被纳入同一 lifecycle barrier。

---

## 44. command 本身可以只用于结算债务

这说明 command queue 不一定只承载：

~~~text
business state mutation
~~~

也可以承载：

~~~text
lifetime completion
synchronization acknowledgement
ownership handoff
~~~

Runtime command protocol 往往同时是 lifecycle protocol。

---

## 45. connect_inproc_sockets 又预留 binder lifetime

函数开头：

~~~cpp
bind_socket_->inc_seqnum ();
~~~

然后才继续建立 pipe、调整 HWM、处理 routing id。

为什么这么早？

因为后面可能：

~~~text
direct process bind
or
send async bind
~~~

无论哪条路径，都需要保证 binder 活到 bind 被完成。

---

## 46. bind_side：本线程直接 process_command

如果已经运行在 binder 所属路径：

~~~cpp
command_t cmd;
cmd.type = command_t::bind;
cmd.args.bind.pipe =
    pending_connection.bind_pipe;

bind_socket_->process_command(cmd);
~~~

因为 `process_command(bind)` 内部会：

~~~text
process_bind
process_seqnum
~~~

刚才的 reservation 在同一次调用里结算。

---

## 47. connect_side：异步 send_bind(..., false)

如果需要跨 owner：

~~~cpp
send_bind(
    bind_socket_,
    bind_pipe,
    false);
~~~

仍然不二次 increment。

因为函数开头：

~~~text
bind_socket_->inc_seqnum()
~~~

已经预付了这笔 debt。

---

## 48. 为什么要允许“预付 debt”，而不是总在 send_bind 内 increment

因为有些地方 lifetime reservation 必须发生在：

~~~text
更早的 critical section
~~~

而不是等到真正 command send。

典型就是：

~~~text
lookup under registry lock
~~~

如果等离开 lock 以后才 increment：

~~~text
lookup pointer
unlock
target unregister + destroy
inc_seqnum on dead pointer
~~~

窗口已经出现。

---

## 49. reservation 必须贴着 pointer acquisition

可以总结成：

> **什么时候 raw pointer 第一次脱离保护域，什么时候就必须建立跨域 lifetime reservation。**

这是本文最重要的可迁移原则之一。

---

## 50. listener 创建 Session 也采用预增

固定源码：

~~~cpp
session_base_t *session =
    session_base_t::create(...);

session->inc_seqnum ();

launch_child (session);

send_attach(
    session,
    engine,
    false);
~~~

这里手动 increment 对应未来：

~~~text
attach command
~~~

---

## 51. launch_child 自己还会产生 plug reservation

`launch_child(session)`：

~~~text
send_plug(session)
~~~

默认会：

~~~text
session.sent_seqnum++
~~~

因此创建阶段至少存在两笔不同 debt：

~~~text
PLUG debt
ATTACH debt
~~~

不能因为目标对象是同一个，就把它们混成一笔。

---

## 52. 每个异步 obligation 都必须有自己的 completion

创建 Session 的逻辑类似：

~~~text
create session

reserve attach
reserve plug

publish plug
publish ownership
publish attach
~~~

owner thread 最终：

~~~text
process plug
release plug debt

process attach
release attach debt
~~~

只有所有已建立 obligation 都完成，seqnum 才追平。

---

## 53. 这是一种“异步构造完成屏障”

对象已经：

~~~text
new
~~~

并不代表它已经：

~~~text
fully integrated into runtime
~~~

创建阶段还可能有：

- plug into poller/thread；
- register owner；
- attach Engine；
- bind Pipe。

所以 constructor return 与 runtime-ready 是不同状态。

---

## 54. seqnum 让“构造中的对象”也不能被过早 teardown

假设对象刚创建就遇到 Context shutdown。

如果：

~~~text
plug/attach commands still in flight
~~~

termination 也不能直接 free。

因为：

~~~text
sent != processed
~~~

它会继续等待。

---

## 55. 这和异步初始化 future 很像

可以抽象成：

~~~text
object allocated
+
N initialization obligations
+
termination may start concurrently
~~~

只有：

~~~text
all init obligations completed
AND
all shutdown obligations completed
~~~

才允许释放。

libzmq 用两个计数域分别表达。

---

## 56. TERM_ACK 为什么必须先 register 再 send TERM

parent：

~~~cpp
register_term_acks (1);
send_term (child, linger);
~~~

顺序不能反过来。

否则 child 可能极快：

~~~text
receive TERM
send TERM_ACK
~~~

而 parent 还没有登记：

~~~text
I expect one ack
~~~

---

## 57. 这和 seqnum 的 reserve-before-publish 是同一原则

seqnum：

~~~text
inc_seqnum
→ publish command
~~~

termination：

~~~text
register_term_acks
→ publish TERM
~~~

都是：

> **先登记未来必须等待的 obligation，再让 completion 变得可能。**

---

## 58. 这是异步协议最重要的顺序不变量之一

错误顺序：

~~~text
start async operation
then increment pending count
~~~

总可能遇到：

~~~text
operation completes immediately
~~~

导致 completion 早于 bookkeeping。

因此通用公式：

\[
\text{Register obligation}
\rightarrow
\text{Publish async work}
\rightarrow
\text{Observe completion}
\]

---

## 59. check_term_acks 这个函数名其实低估了它的职责

它不仅检查：

~~~text
_term_acks
~~~

还检查：

~~~text
_processed_seqnum
==
_sent_seqnum
~~~

所以概念上它更像：

~~~text
CheckQuiescenceAndDestroy()
~~~

而不只是：

~~~text
CheckChildAcks()
~~~

---

## 60. 一个 own_t 至少有两种未完成工作

可以写成：

\[
Q
=
C_{\text{cmd}}
+
C_{\text{term}}
\]

其中：

~~~text
C_cmd
  = sent_seqnum - processed_seqnum

C_term
  = term_acks
~~~

物理销毁要求：

\[
C_{\text{cmd}}=0
\quad\land\quad
C_{\text{term}}=0
\]

同时：

~~~text
terminating == true
owned.empty()
~~~

---

## 61. 为什么不直接做一个 pending counter

理论上可以把所有 obligation 都塞进一个：

~~~text
pending++
pending--
~~~

但这样会丢失语义边界。

seqnum pair 能表达：

~~~text
commands admitted vs commands processed
~~~

TERM_ACK 能表达：

~~~text
shutdown dependencies outstanding
~~~

调试时更容易知道卡在哪一类 obligation。

---

## 62. seqnum pair 还有一个好处：sender 不需要 decrement

foreign sender：

~~~text
only increments sent
~~~

owner thread：

~~~text
only increments processed
~~~

这比多个线程共同：

~~~text
pending--
~~~

更符合 owner-thread 模型。

---

## 63. 这是“分离写者”的状态设计

跨线程：

~~~text
sent_seqnum
  multi-thread atomic increments
~~~

owner-local：

~~~text
processed_seqnum
  single-owner normal increment
~~~

读者通过：

~~~text
equality
~~~

判断 drain。

这与 producer/consumer cursor 设计很相似。

---

## 64. 但它不是严格队列长度

不能说：

~~~text
sent - processed
==
mailbox 中 command 数量
~~~

因为：

- 只有特定 command 被计入；
- command 可能已经 dequeue，正在 handler 中执行；
- 有些 reservation 在真正 send 之前就提前建立；
- `inproc_connected` 可能只用于结算长期 reservation。

所以它是：

~~~text
lifecycle obligations
~~~

不是 mailbox occupancy。

---

## 65. pre-reservation 会让 debt 暂时存在但 command 还没入队

`find_endpoint()`：

~~~text
inc_seqnum
return pointer
~~~

caller 之后才：

~~~text
send_bind
~~~

中间：

~~~text
sent - processed = 1
mailbox may contain 0 related command
~~~

这完全正常。

这笔差额代表：

~~~text
future command right
~~~

已经被授予。

---

## 66. 这和 hazard pointer 有一点相似

hazard pointer 的思想：

~~~text
reader announces:
I may dereference this object

reclaimer waits:
no hazard references remain
~~~

libzmq pre-reservation：

~~~text
caller announces:
I own one future lifecycle-sensitive action

reclaimer waits:
sent == processed
~~~

实现不同，但思考方式相近。

---

## 67. 也和 intrusive refcount 有一点相似

refcount：

~~~text
acquire reference
use object
release reference
~~~

seqnum：

~~~text
inc sent
publish/execute command
inc processed
~~~

区别是：

~~~text
release is not performed
by sender
~~~

而是在 owner thread 完成 command 后结算。

---

## 68. seqnum 很适合 command-passing runtime

因为 command 的终点天然就是：

~~~text
owner thread
~~~

让 owner 在执行完成后推进：

~~~text
processed
~~~

比让 sender 等待并释放引用更自然。

---

## 69. 它不适合任意长期借用

如果 foreign thread 拿到 pointer 后：

~~~text
保存 10 分钟
直接调用多个方法
~~~

单笔 seqnum reservation 并不能自动保护这种任意访问。

协议要求 reservation 与：

~~~text
一个明确 future command obligation
~~~

绑定。

---

## 70. 所以 raw pointer escape 必须有严格 contract

合法：

~~~text
lookup under registry lock
inc_seqnum
return pointer
later send exactly one tracked command
~~~

危险：

~~~text
lookup pointer
store globally
use whenever
~~~

seqnum 不是 general-purpose GC。

---

## 71. close ingress 是 seqnum barrier 成立的前提

假设销毁线程刚看到：

~~~text
sent == processed
~~~

另一线程随后还能无条件：

~~~text
target->inc_seqnum()
~~~

那 barrier 没有意义。

因此 final reclamation 之前必须先阻止新的合法 reservation。

---

## 72. libzmq 的 endpoint registry 展示了这个闭环

socket termination 开头会：

~~~cpp
unregister_endpoints(this);
~~~

而：

~~~text
find_endpoint
unregister_endpoints
~~~

共享：

~~~text
_endpoints_sync
~~~

所以 endpoint lookup 的合法入口被关闭。

---

## 73. shutdown 的结构是 retire-before-drain

可以抽象为：

~~~text
remove from discovery / registry
        |
        v
no new lookup can acquire reservation
        |
        v
wait old seqnum debt drain
        |
        v
destroy
~~~

这正是 quiescence 的标准结构。

---

## 74. seqnum 不能代替 registry retirement

如果只写：

~~~text
wait sent == processed
delete
~~~

却不先：

~~~text
remove visibility
~~~

新的 producer 随时能增加 `sent`。

严重时：

~~~text
destroy can starve forever
~~~

或出现 final check 后的新 reservation。

---

## 75. registry retirement 也不能代替 seqnum drain

反过来：

~~~text
erase from registry
~~~

只阻止未来 lookup。

之前已经：

~~~text
lookup + inc_seqnum
~~~

的 producer 仍然合法地持有 future command right。

所以还必须：

~~~text
wait processed catch sent
~~~

---

## 76. 两者组合才是完整 quiescence

~~~text
RETIRE
  close new acquisition path

DRAIN
  wait existing reservations complete

RECLAIM
  free object
~~~

这和 LCM/eCAL 的 callback 模型是同一个三阶段框架。

---

## 77. 但 libzmq 把 reservation 做到了 command admission 前

这比“callback 入口 active++”更强。

它覆盖：

~~~text
pointer acquired
but command not yet queued
~~~

以及：

~~~text
command queued
but not yet dequeued
~~~

以及：

~~~text
command handler currently running
~~~

直到：

~~~text
process_seqnum()
~~~

全部结束。

---

## 78. 这正好解决 pre-entry window

在 LCM binding 中，如果只在 trampoline：

~~~text
active++
~~~

无法覆盖：

~~~text
C core 已决定要 callback
但 trampoline 尚未进入
~~~

libzmq 的 seqnum 思路更接近：

~~~text
在“获得未来执行资格”时
就建立 reservation
~~~

这是更强的生命周期模型。

---

## 79. process_seqnum 放在 handler 后，所以 active window 也被覆盖

reservation lifetime：

~~~text
pointer acquisition / send intent
        |
        v
command queued
        |
        v
command dequeued
        |
        v
handler executing
        |
        v
handler complete
        |
        v
process_seqnum
~~~

整个链都在 debt 内。

---

## 80. 这就是“execution eligibility lifetime”

很多并发 bug 的原因是只统计：

~~~text
currently executing
~~~

但真正需要保护的是：

~~~text
already eligible to execute
~~~

这两者之间可能有很长 queueing delay。

---

## 81. Reaper 又在 socket 外面增加一层 barrier

`socket_base_t::close()` 并不 delete：

~~~cpp
_tag = 0xdeadbeef;
send_reap(this);
return 0;
~~~

它把 shutdown ownership 交给：

~~~text
Reaper thread
~~~

---

## 82. Reaper 让 socket 在自己的 mailbox/poller 中继续活

`start_reaping()`：

~~~text
socket registers mailbox fd
into Reaper poller
~~~

随后：

~~~text
terminate()
check_destroy()
~~~

socket 仍能继续处理晚到的控制 command。

---

## 83. own_t 的 quiescence 达成以后，socket 也不直接 delete

`socket_base_t::process_destroy()`：

~~~cpp
_destroyed = true;
~~~

它覆盖了 base：

~~~text
own_t::process_destroy()
~~~

所以 own_t 条件满足时只把 socket 标成：

~~~text
logically destroyable
~~~

---

## 84. check_destroy 才做最终 Reaper-side teardown

~~~cpp
if (_destroyed) {
    _poller->rm_fd(_handle);

    destroy_socket(this);

    send_reaped();

    own_t::process_destroy();
}
~~~

顺序是：

~~~text
detach poller
remove Context slot/registry
notify Reaper
physical delete
~~~

---

## 85. 这是“对象内部 quiescence”与“外部 execution source detach”的分层

`own_t` 证明：

~~~text
no tracked object-level obligations remain
~~~

Reaper 再证明：

~~~text
socket no longer registered
with external poller/context machinery
~~~

只有两层都完成才物理删除。

---

## 86. Context 又在 Reaper 外面再套一层 DONE barrier

Context terminate：

~~~text
stop sockets
wait Reaper
~~~

Reaper：

~~~text
terminating == true
AND
_sockets == 0
~~~

才：

~~~text
send_done()
~~~

Context 的 term mailbox 收到 DONE 后才：

~~~text
delete this
~~~

---

## 87. 因此 libzmq shutdown 是一组嵌套 quiescence barrier

可以画成：

~~~text
per-object:
  seqnum debt == 0
  term_acks == 0
        |
        v
socket:
  _destroyed
  poller detached
  Context slot removed
        |
        v
Reaper:
  _sockets == 0
        |
        v
Context:
  DONE received
        |
        v
delete Context
~~~

这比一个：

~~~text
closing = true
~~~

丰富得多。

---

## 88. 为什么 shutdown 往往比 fast path 更复杂

fast path 只要回答：

~~~text
how to send one message?
~~~

shutdown 必须回答：

~~~text
which registries still know me?
which queues still reference me?
which commands already earned execution rights?
which children still owe acknowledgements?
which pollers can still callback me?
which threads can still wake me?
~~~

这才是 Runtime 最难的部分。

---

## 89. 一个设备驱动 Runtime 可以直接复用这套模式

假设：

~~~text
CAN Bus Owner
  |
  +-- MotorSession A
  +-- MotorSession B
~~~

其他线程通过 command queue 发：

~~~text
AddMotor
AttachFilter
ResetDevice
BindTelemetry
~~~

可以给每个 Session 建：

~~~text
sent_ops
processed_ops
term_acks
~~~

---

## 90. 设备注册表 lookup 也应 reserve lifetime

健康监控线程：

~~~text
lock device registry
find MotorSession*
reserve future command
unlock
send ResetDevice
~~~

故障线程关闭设备：

~~~text
lock registry
erase MotorSession
unlock
wait reservations drain
destroy
~~~

这样不用让所有线程直接共享整个驱动状态。

---

## 91. 这比“拿到 shared_ptr 就完了”更能表达协议

`shared_ptr` 能解决：

~~~text
memory alive
~~~

但不自动表达：

~~~text
device logically retired
command should still execute?
shutdown must wait which operation?
new work still allowed?
~~~

seqnum + state machine 把这些 runtime semantics 显式化。

---

## 92. 当然 shared_ptr 与 seqnum 也可以组合

例如：

~~~text
registry owns shared_ptr<Session>
command owns lightweight generation token
shutdown waits logical in-flight counter
~~~

重点不是必须复制 libzmq 的实现。

重点是分清：

~~~text
memory ownership
execution eligibility
shutdown dependency
~~~

---

## 93. generation 还能防止旧 completion 污染新对象

如果对象 ID 复用：

~~~text
Device #7 generation 10 retired
Device #7 generation 11 created
~~~

旧 completion 不应错误地减少新对象 debt。

libzmq 依赖对象地址与生命周期结构。

更现代的 handle runtime 可以加入：

~~~text
generation
~~~

增强 stale command 防护。

---

## 94. seqnum 模式的一个风险：人工配对复杂

每个：

~~~text
inc_seqnum
~~~

必须最终对应一个：

~~~text
process_seqnum
~~~

而且只能对应一次。

这种协议依赖开发者维持配对。

---

## 95. bool inc_seqnum_ 暴露了这种人工 proof burden

API：

~~~cpp
send_bind(..., bool inc_seqnum_ = true)
~~~

调用点传 `false` 时必须能指出：

~~~text
reservation was established at X
completion will happen at Y
~~~

否则很难 code review。

---

## 96. 更现代的设计可以把 reservation 做成类型

概念：

~~~cpp
class CommandReservation
{
 public:
  CommandReservation(Target& t)
      : target_(&t)
  {
    target_->Reserve();
  }

  Command BuildBind(Pipe* p)
  {
    return Command{
      target_,
      p,
      std::move(*this)
    };
  }

 private:
  Target* target_;
};
~~~

让：

~~~text
reservation ownership
~~~

随 command 对象一起移动。

---

## 97. RAII token 能减少 double/missing increment

目标是让非法状态更难表达：

~~~text
tracked command
must carry one reservation token
~~~

而不是：

~~~text
caller remembers
true or false
~~~

这是一种类型系统层面的升级。

---

## 98. 但跨 C ABI / 固定布局 command 时手工计数仍很常见

libzmq 的 `command_t` 追求：

- 固定 command kind；
- union args；
- 少分配；
- 低 overhead。

因此显式 bool 与 counter 是一种性能/复杂度权衡。

---

## 99. 一份源码 review 最该检查哪些配对

看到：

~~~text
inc_seqnum()
~~~

立即追：

~~~text
哪一条 future command
最终 process_seqnum()？
~~~

看到：

~~~text
send_* (..., false)
~~~

立即追：

~~~text
更早的 reservation 在哪里？
~~~

看到：

~~~text
process_seqnum()
~~~

立即追：

~~~text
是否可能没有对应 sent debt？
~~~

---

## 100. 还要检查 reservation 建立时 pointer 是否仍受保护

错误：

~~~text
unlock registry
then inc_seqnum(pointer)
~~~

正确：

~~~text
lock registry
lookup
inc_seqnum
unlock
~~~

这是防止 lookup/reclaim race 的关键。

---

## 101. 还要检查 final destroy 前是否关闭所有 admission path

包括：

- registry lookup；
- ownership tree；
- pending connection table；
- external poller；
- public API；
- timer/event source。

否则旧 debt 清零以后还能产生新 debt。

---

## 102. 这和“unregister != quiescence”是一回事

`unregister`：

~~~text
close future discovery
~~~

`seqnum drain`：

~~~text
finish already admitted work
~~~

`Reaper detach`：

~~~text
close external execution source
~~~

每层解决不同问题。

---

## 103. 把 eCAL、LCM、libzmq 放在一起看

eCAL 固定实现：

~~~text
user callback under internal mutex
→ reentrancy deadlock
~~~

LCM C++ binding：

~~~text
C core deferred delete
but wrapper userdata owner delete too early
→ lifetime gap
~~~

libzmq：

~~~text
reserve target lifetime
before async command eligibility
+
owner-side completion
+
TERM_ACK
+
Reaper
→ layered quiescence
~~~

三者正好展示三个不同方向。

---

## 104. 一个成熟 Runtime 的 shutdown 不变量

可以归纳成：

~~~text
1. stop new discovery/admission

2. preserve object lifetime
   for already admitted work

3. execute / cancel old work

4. wait child/resource acknowledgements

5. detach pollers/timers/registries

6. only then reclaim memory
~~~

libzmq 的源码几乎可以逐项找到对应实现。

---

## 105. 最重要的时序：reservation must precede exposure

无论是：

~~~text
find endpoint
send bind
launch session
send attach
register TERM_ACK
~~~

共同结构都是：

~~~text
bookkeeping first
publication second
completion last
~~~

如果只记一个异步系统设计原则，就是：

> **任何可能比 bookkeeping 更快完成的异步动作，都必须在动作可见之前先登记 obligation。**

---

## 106. 最重要的生命周期原则：eligibility 比 execution 更早

不要只统计：

~~~text
currently inside callback/handler
~~~

还要统计：

~~~text
already selected
already queued
already handed a pointer
already reserved a future command
~~~

真正安全的 quiescence 必须覆盖这一整段。

---

## 107. 最重要的所有权原则：pointer escape 必须带 lifetime proof

当一个 raw pointer 从：

~~~text
registry lock
~~~

保护域中逃出去时，代码必须能够回答：

~~~text
what keeps pointee alive now?
~~~

libzmq 的一个答案是：

~~~text
inc_seqnum reservation
~~~

其他 Runtime 可以使用：

- `shared_ptr`；
- hazard pointer；
- epoch token；
- intrusive ref；
- generation lease；
- owner-thread command token。

但不能没有答案。

---

## 108. 最后画出完整 libzmq 对象销毁路径

~~~text
foreign thread / registry lookup
        |
        | inc_seqnum
        v
future command right
        |
        | publish command
        v
owner mailbox
        |
        | process command body
        v
process_seqnum
        |
        v
sent == processed ?
        |
        +-- no --> keep object alive
        |
        +-- yes
             |
             v
       terminating ?
       term_acks == 0 ?
       owned empty ?
             |
             v
       process_destroy
             |
             v
       socket marks _destroyed
             |
             v
       Reaper removes poller
       Context removes socket slot
             |
             v
       send_reaped
             |
             v
       physical delete
             |
             v
       Reaper sockets == 0
             |
             v
       DONE
             |
             v
       Context may delete
~~~

这不是“close 调用链”。

它是一整套：

~~~text
admission
reservation
execution
drain
detach
reclaim
~~~

协议。

---

## 109. 结论

`_sent_seqnum` / `_processed_seqnum` 最有价值的地方，不在于“用了一个原子计数器”。

真正值得学习的是它背后的协议：

~~~text
raw pointer acquisition
    |
    v
reserve lifetime before escape
    |
    v
publish async command
    |
    v
owner executes state transition
    |
    v
release reservation
    |
    v
shutdown waits old reservations
~~~

它还必须和：

~~~text
registry retirement
TERM / TERM_ACK
poller detach
Reaper DONE
~~~

一起工作。

因此可以把 libzmq 的对象销毁原则压成一句：

> **先关闭新的生命周期入口，再等待所有已经获得执行资格的 command 和 child obligation 完成，最后才让 Reaper 回收对象。**

这套设计可以直接迁移到机器人设备总线、异步驱动、网络 Runtime、线程池 actor、传感器 hot-plug 和动态任务图中。
