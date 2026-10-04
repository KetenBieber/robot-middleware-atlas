# Context 与 Reaper：Slot 地址空间、Ownership Handoff 与最终销毁屏障

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

前面的 [Linger 与终止协议](linger-termination-protocol.md) 已经回答了一个对象内部的问题：

> Pipe、Session、Socket 在开始关闭以后，怎样等待数据 drain、TERM/TERM_ACK、seqnum 与 child lifecycle obligation 达到安全边界。

但还有最后一层没有回答：

> **当 application thread 已经调用 `zmq_close()`，谁继续处理这个 socket 的晚到 command？谁负责把它从 poller、Context slot 和 socket registry 中摘掉？整个 Context 又如何证明“所有 socket 都真的回收完了”以后才允许自己析构？**

libzmq 的答案不是：

~~~text
close()
→ delete socket
~~~

而是：

~~~text
application ownership
        ↓
send_reap
        ↓
Reaper ownership
        ↓
drain late commands
        ↓
own_t quiescence
        ↓
socket _destroyed
        ↓
poller detach
        ↓
Context unregister
        ↓
REAPED
        ↓
delete socket
        ↓
last socket gone
        ↓
Reaper DONE
        ↓
Context delete
~~~

所以 `ctx_t + reaper_t + socket_base_t` 共同构成的是：

> **Runtime 最外层的 reclamation barrier。**

它解决的不只是“怎么关 socket”，而是更一般的：

- control-plane 地址何时失效；
- logical tid 何时可以复用；
- background executor 何时可以停止；
- registry removal 与 physical delete 怎样排序；
- asynchronous close 怎样变成 Context 级 join；
- 如何避免旧 command 在 slot reuse 后命中新对象。

---

# 一、先建立完整对象图

Context 内部至少有四类执行实体：

~~~text
                 ctx_t
                   |
      +------------+-------------+
      |            |             |
 term mailbox    Reaper       I/O threads
      |            |             |
      |         mailbox        mailbox
      |            |             |
      +------------+-------------+
                   |
                _slots[]
                   |
          application sockets
             mailbox each
~~~

真正关键的全局结构：

~~~text
_slots
_empty_slots
_sockets
_reaper
_io_threads
_term_mailbox
_slot_sync
_terminating
_starting
~~~

---

# 二、`_slots` 不是 OS Thread Table

源码：

~~~cpp
std::vector<i_mailbox *> _slots;
~~~

它保存的是：

~~~text
logical tid
→ mailbox
~~~

不是：

~~~text
logical tid
→ std::thread
~~~

---

## 1. 为什么这一区别重要

libzmq command routing：

~~~cpp
_ctx->send_command(
    destination->get_tid(),
    cmd);
~~~

Context：

~~~cpp
_slots[tid]->send(cmd);
~~~

所以一个 object 的 `_tid` 本质上是：

> **control-plane mailbox address。**

---

# 三、Context 的 logical TID 是地址，不是线程身份

可能占 slot 的对象包括：

- Context termination mailbox；
- Reaper；
- I/O thread；
- application socket。

这些对象的执行模型不同，

但 command sender 不需要知道：

~~~text
目标对象属于什么 C++ thread class
~~~

只需要：

~~~text
目标 tid
~~~

---

# 四、这是一层 Runtime Address Space

可以类比：

~~~text
virtual address
→ memory page

actor id
→ mailbox

logical tid
→ libzmq mailbox
~~~

Context 负责：

~~~text
address resolution
~~~

而 mailbox 负责：

~~~text
command transport
~~~

---

# 五、`start()` 怎样布局这个地址空间

源码首先计算：

~~~cpp
slot_count =
    max_sockets
  + io_thread_count
  + 2;
~~~

额外两项：

~~~text
term_tid
reaper_tid
~~~

---

# 六、Slot 0/1 类似 Runtime Reserved Address

先注册：

~~~text
term_tid
→ _term_mailbox

reaper_tid
→ reaper mailbox
~~~

随后：

~~~text
I/O thread slots
~~~

最后：

~~~text
application socket free slots
~~~

---

# 七、为什么先建立 Term/Reaper，再启动 I/O Thread

因为一旦后台 Runtime 开始工作，

就必须已经具备：

~~~text
shutdown destination
+
reclamation destination
~~~

否则 startup failure 或早期 close 可能没有控制路径可以收尾。

---

# 八、这是一条通用启动原则

> **先建立控制面和回收面，再开放业务执行面。**

很多系统反过来：

~~~text
先启动 worker
以后再初始化 stop/reaper
~~~

会制造启动失败时最难处理的半初始化对象。

---

# 九、Context 为什么 Lazy Start

Context 构造后：

~~~text
_starting = true
~~~

第一次 `create_socket()`：

~~~cpp
if (_starting)
    start();
~~~

才创建：

- Reaper thread；
- I/O threads；
- slot table。

---

# 十、Lazy Runtime Initialization 的收益

如果程序只：

~~~text
create context
set options
destroy context
~~~

没有任何 socket，

就不必：

- 建 poller；
- 启后台线程；
- 建完整 slot pool。

---

# 十一、`create_socket()` 第一道 Gate：Termination

~~~cpp
if (_terminating) {
    errno = ETERM;
    return NULL;
}
~~~

含义：

> **Context 进入 shutdown 后，不允许新的 socket obligation 被加入。**

---

# 十二、为什么这是 Shutdown 第一原则

如果一边：

~~~text
等待 sockets → 0
~~~

另一边仍允许：

~~~text
create_socket()
~~~

那么 termination condition 可能永远达不到。

所以必须：

~~~text
close admission
before
wait drain
~~~

---

# 十三、第二道 Gate：Slot Capacity

~~~cpp
if (_empty_slots.empty()) {
    errno = EMFILE;
    return NULL;
}
~~~

Context 的 socket 上限最终受：

~~~text
可分配 logical control address
~~~

限制。

---

# 十四、Socket 创建时发生什么

~~~cpp
slot = _empty_slots.back();
_empty_slots.pop_back();

sid = max_socket_id + 1;

s = socket_base_t::create(...);

_sockets.push_back(s);
_slots[slot] = s->get_mailbox();
~~~

---

# 十五、这里同时建立两类 Registry

第一类：

~~~text
_slots[tid]
→ mailbox
~~~

用于：

~~~text
command routing
~~~

第二类：

~~~text
_sockets
→ live socket objects
~~~

用于：

~~~text
Context lifecycle bookkeeping
~~~

---

# 十六、为什么不能只保留 `_slots`

因为：

~~~text
slot occupancy
~~~

和：

~~~text
Context-owned socket lifecycle
~~~

不是一个问题。

Context terminate 要：

~~~text
iterate all sockets
send stop
wait sockets empty
~~~

这更适合 `_sockets`。

---

# 十七、为什么不能只保留 `_sockets`

command sender 已经知道：

~~~text
destination tid
~~~

如果每次都扫描：

~~~text
_sockets
→ find tid
→ get mailbox
~~~

成本和复杂度都没必要。

所以 `_slots` 是：

~~~text
O(1) control routing index
~~~

---

# 十八、双 Registry 带来的核心不变量

对一个 live socket：

~~~text
socket ∈ _sockets
AND
_slots[socket.tid] == socket.mailbox
~~~

销毁时必须同时撤掉两者。

---

# 十九、`_slot_sync` 保护的不只是 Vector

源码注释明确说它同步：

~~~text
sockets
empty_slots
terminating
zombie socket access
~~~

所以它是：

> **Context 级 slot/lifecycle registry lock。**

---

# 二十、为什么它还是 Memory Barrier

同一个 socket 是否：

- 仍注册；
- 已退休；
- slot 是否 free；
- Context 是否 terminating；

这些状态必须被不同 CPU 核一致观察。

所以 lock 同时承担：

~~~text
mutual exclusion
+
publication ordering
~~~

---

# 二十一、Socket Close 为什么不是 Context Destroy

应用：

~~~cpp
zmq_close(socket);
~~~

只结束：

~~~text
application-side use right
~~~

不是：

~~~text
Context-side lifetime
~~~

---

# 二十二、`close()` 真正做的事

核心：

~~~cpp
_tag = 0xdeadbeef;
send_reap(this);
~~~

thread-safe socket 还先：

~~~text
clear old application signalers
~~~

---

# 二十三、`_tag` 只证明 Public Handle Dead

它回答：

~~~text
应用还能合法调用 socket API 吗？
~~~

答案：

~~~text
no
~~~

但它不证明：

~~~text
socket memory can be freed
~~~

---

# 二十四、Close 是 Ownership Transfer

源码注释直接写：

~~~text
Transfer ownership of the socket
from application thread
to Reaper thread
~~~

所以：

> **close 的关键动作不是 deallocation，而是 handoff。**

---

# 二十五、为什么 Application Thread 不自己 Drain

如果 close 同步等待：

- Session TERM_ACK；
- Pipe delimiter；
- I/O command；
- linger；
- inproc obligations；

可能需要后台 owner threads 继续运行。

如果应用线程同时持锁或阻塞 runtime dependency，

就容易形成 shutdown deadlock。

---

# 二十六、Reaper 是什么

它不是 GC。

它不会：

~~~text
scan unreachable objects
~~~

它也不会：

~~~text
自动发现用户忘记 close 的 socket
~~~

它只处理：

~~~text
explicitly retired socket
~~~

---

# 二十七、更准确的名字

可以理解为：

~~~text
Asynchronous Destruction Executor
~~~

职责：

~~~text
take retired socket
drive late control work
detach external registrations
reclaim memory
~~~

---

# 二十八、Reaper 自己仍然使用 Mailbox + Poller

构造：

~~~text
mailbox
+
poller
~~~

注册：

~~~text
reaper mailbox fd
→ poller
~~~

启动：

~~~cpp
_poller->start("Reaper");
~~~

---

# 二十九、Reaper 没发明另一套并发模型

整个 libzmq 一直复用：

~~~text
mailbox
→ signaler fd
→ poller
→ owner-thread process_command
~~~

因此 teardown 也仍然在：

~~~text
事件驱动 owner model
~~~

内进行。

---

# 三十、为什么这是好事

如果 close 阶段突然改成：

~~~text
跨线程直接调用对象方法
+
额外 mutex
~~~

那么正常运行和 shutdown 会出现两套 concurrency semantics。

最容易出 bug。

---

# 三十一、`send_reap()` 到达 Reaper 后

Reaper：

~~~cpp
process_reap(socket)
{
    socket->start_reaping(_poller);
    ++_sockets;
}
~~~

这里 `_sockets` 是：

~~~text
Reaper 当前正在负责回收的 socket 数
~~~

---

# 三十二、这个 Count 不是 Context `_sockets`

有两个不同计数/集合：

~~~text
ctx_t::_sockets
→ Context registry 中仍存在的 socket

reaper_t::_sockets
→ Reaper 尚未完成回收的 socket 数
~~~

---

# 三十三、为什么要两层 Barrier

Context registry 回答：

~~~text
这个 socket 仍属于 Context 吗？
~~~

Reaper count 回答：

~~~text
还有多少 deferred destruction job 未完成？
~~~

---

# 三十四、`start_reaping()` 第一步：转移 Event Ownership

~~~cpp
_poller = reaper_poller;
~~~

然后把 socket mailbox 的 fd：

~~~text
add_fd
set_pollin
~~~

---

# 三十五、为什么必须先注册到 Reaper Poller

因为随后 `terminate()` 会触发：

- TERM；
- TERM_ACK；
- seqnum completion；
- Pipe events；
- child completion。

这些 command 仍会进入 socket mailbox。

---

# 三十六、如果先 Terminate、后注册

可能发生：

~~~text
TERM sent
↓
ACK already queued
↓
old owner no longer pumps
↓
new owner not yet observing mailbox
~~~

shutdown progress 会卡住。

---

# 三十七、Handoff 的正确顺序

~~~text
establish new executor
        ↓
establish wakeup path
        ↓
start retirement protocol
        ↓
remove old owner responsibility
~~~

---

# 三十八、Thread-safe Socket 为什么更特殊

普通 socket mailbox：

~~~text
mailbox_t
→ already has one signaler fd
~~~

thread-safe socket：

~~~text
mailbox_safe_t
~~~

可有多个 signaler/waiter。

close 时应用侧 signaler 已清除。

---

# 三十九、Reaper 必须重新建立 Signaler

~~~text
_reaper_signaler =
    new signaler_t();

mailbox_safe
→ add_signaler(_reaper_signaler);
~~~

然后：

~~~cpp
_reaper_signaler->send();
~~~

---

# 四十、为什么刚注册就主动 Send

因为 mailbox 可能已经：

~~~text
nonempty
~~~

但这些 command 是在新 signaler 注册前入队的。

如果等：

~~~text
下一条新 command
~~~

才触发 edge，

已有 backlog 可能永久无人处理。

---

# 四十一、这是典型 Handoff Lost-Wakeup Window

错误：

~~~text
queue already nonempty
↓
install new waiter
↓
no new enqueue
↓
new waiter sleeps forever
~~~

修复：

~~~text
install waiter
↓
force one wake
↓
drain current backlog
~~~

---

# 四十二、这和 ypipe Lost Wakeup 是同一类问题

不同层级，

相同逻辑：

> **状态已经为 true 时，新 waiter 加入不能只依赖 future transition。**

---

# 四十三、`start_reaping()` 最后才调用

~~~cpp
terminate();
check_destroy();
~~~

说明：

~~~text
reclamation executor established
before
termination begins
~~~

---

# 四十四、为什么紧接着 `check_destroy()`

有些 socket 可能：

- 没有 child；
- 没有 pending command；
- 没有 pipe obligation。

`terminate()` 可以立刻让：

~~~text
own_t quiescence condition satisfied
~~~

于是 `_destroyed=true`。

不需要等下一次 poll event。

---

# 四十五、Socket 为什么 Override `process_destroy()`

普通 `own_t`：

~~~cpp
process_destroy()
{
    delete this;
}
~~~

但 socket：

~~~cpp
process_destroy()
{
    _destroyed = true;
}
~~~

---

# 四十六、为什么 Socket 不能在 Own Barrier 完成时直接 Delete

因为还有：

~~~text
Reaper poller
→ socket event sink
~~~

以及：

~~~text
Context _slots
Context _sockets
~~~

这些外部结构仍可能持有对 socket 的 reachability。

---

# 四十七、所以 Own Quiescence 不是最终 Quiescence

own_t 证明：

~~~text
child lifecycle debt = 0
command reservation debt = 0
~~~

但 socket 还要证明：

~~~text
external registry reachability = 0
poller reachability = 0
~~~

---

# 四十八、这是多域 Quiescence

可以拆：

~~~text
Domain A
own_t child barrier

Domain B
seqnum command barrier

Domain C
Reaper poller registry

Domain D
Context socket/slot registry
~~~

---

# 四十九、`_destroyed=true` 的含义

更准确地是：

> **内部对象树已满足销毁条件，可以进入外部 detach 阶段。**

不是：

~~~text
memory already gone
~~~

---

# 五十、Reaper 如何驱动 Socket

socket mailbox fd 可读：

~~~cpp
socket_base_t::in_event()
~~~

内部：

~~~text
process_commands(0, false)
↓
check_destroy()
~~~

---

# 五十一、所以 Reaper 不 Busy Wait

它不会：

~~~text
while (!destroyed)
    poll bool
~~~

而是：

~~~text
command event
→ process state transition
→ check terminal condition
~~~

---

# 五十二、这是 Event-driven Reclamation

只有：

~~~text
可能改变销毁条件的事件
~~~

到来时才重新检查。

---

# 五十三、`check_destroy()` 是真正 Physical Reclaim Gate

条件：

~~~cpp
if (_destroyed)
~~~

然后顺序：

~~~text
1. rm_fd
2. destroy_socket(this)
3. send_reaped()
4. own_t::process_destroy()
~~~

---

# 五十四、第一步为什么是 Poller Detach

~~~cpp
_poller->rm_fd(_handle);
~~~

只要 socket 还注册在 poller：

~~~text
future event dispatch
→ may call socket pointer
~~~

所以 delete 前必须先切断 event reachability。

---

# 五十五、这是经典 Unregister-before-Reclaim

先：

~~~text
future callback discovery impossible
~~~

后：

~~~text
free object
~~~

---

# 五十六、第二步：Context Unregister

~~~cpp
destroy_socket(this);
~~~

Context：

~~~text
free tid slot
_slots[tid] = NULL
erase from _sockets
~~~

---

# 五十七、为什么 Slot Release 必须在这里

不能在 `close()` 时就做：

~~~text
_slots[tid] = NULL
~~~

因为 close 以后仍有 command：

~~~text
TERM_ACK
seqnum completion
child lifecycle event
~~~

需要通过同一个 tid/mailbox 到达 socket。

---

# 五十八、为什么更不能立刻 Reuse Slot

假设旧 socket：

~~~text
tid = 42
~~~

还有一个晚到 command：

~~~text
destination = old socket
routing target tid = 42
~~~

如果 close 后立即：

~~~text
slot 42 → new socket
~~~

就存在 ABA 风险。

---

# 五十九、Slot Reuse ABA

~~~text
old socket A
tid 42
    |
close A
    |
slot 42 freed too early
    |
new socket B
tid 42
    |
late command for A
    |
_ctx->_slots[42]
    |
    v
B mailbox
~~~

这是非常危险的：

~~~text
lifecycle alias
~~~

---

# 六十、为什么正常协议避免这个问题

slot 只有到：

~~~text
own_t command obligations complete
+
socket destroyed
+
Reaper poller detached
+
Context destroy_socket
~~~

才回到：

~~~text
_empty_slots
~~~

---

# 六十一、Seqnum 与 Slot Reuse 是同一条 Lifetime Chain

`_sent_seqnum == _processed_seqnum`

证明：

~~~text
那些需要 lifetime reservation 的 tracked command
已经全部处理
~~~

之后才能走到：

~~~text
_destroyed
→ destroy_socket
→ slot reusable
~~~

---

# 六十二、所以 Seqnum 不是纯 Command Statistic

它最终保护的是：

> **对象对应 control-plane address 的可复用时刻。**

---

# 六十三、Slot = Address，因此 Reuse = Address Rebinding

从系统视角：

~~~text
tid 42
~~~

类似：

~~~text
一个可复用 handle/index
~~~

只要旧异步操作还能指向旧 generation，

就不能把相同 numeric address 绑定给新对象。

---

# 六十四、这与 File Descriptor Reuse 很像

典型 bug：

~~~text
close fd=7
new socket gets fd=7
late async completion for old fd=7
accidentally acts on new socket
~~~

libzmq slot 也有同类风险。

---

# 六十五、Generation Counter 能不能解决

理论上可以设计：

~~~text
(tid, generation)
~~~

但当前 libzmq 不是这么做。

它采用：

~~~text
delay slot reuse
until old async obligations quiescent
~~~

---

# 六十六、这是两种常见 Strategy

方案 A：

~~~text
generation-tagged handle
~~~

方案 B：

~~~text
quiescence-before-reuse
~~~

libzmq 主要是 B。

---

# 六十七、`destroy_socket()` 的完整操作

~~~cpp
tid = socket->get_tid();

_empty_slots.push_back(tid);
_slots[tid] = NULL;

_sockets.erase(socket);
~~~

最后：

~~~cpp
if (_terminating
    && _sockets.empty())
    _reaper->stop();
~~~

---

# 六十八、为什么 Reaper Stop 由 Last Socket Removal 触发

Context terminate 的最终目标：

~~~text
all socket reclamation complete
~~~

当 `_sockets.empty()`：

~~~text
Context registry 已无 socket
~~~

才能告诉 Reaper：

~~~text
no more reaping jobs should arrive
~~~

---

# 六十九、这里又有一层 Admission Closure

Context `_terminating=true` 已经阻止：

~~~text
new create_socket
~~~

所以当 `_sockets` 降到 0，

它不会再反弹到非零。

---

# 七十、这是 Monotonic Shutdown State

shutdown 阶段应尽量满足：

~~~text
work obligations
monotonically decrease
~~~

而不是：

~~~text
close one
create two
~~~

---

# 七十一、第三步：`send_reaped()`

socket 从 Context registry 移除后：

~~~text
send REAPED command
→ Reaper
~~~

Reaper：

~~~cpp
--_sockets;
~~~

---

# 七十二、为什么不是 Context `_sockets.empty()` 就直接 Done

因为 Context registry removal 与：

~~~text
Reaper 当前 destruction job accounting
~~~

是两个不同状态。

Reaper 自己必须确认：

~~~text
它接管的每个 reap job
都有对应 reaped completion
~~~

---

# 七十三、Reaper `_sockets` 是 Completion Debt Counter

`process_reap`：

~~~text
debt++
~~~

`process_reaped`：

~~~text
debt--
~~~

---

# 七十四、这和 TERM_ACK Counter 结构相似

共同模式：

~~~text
launch async obligation
→ increment debt

completion
→ decrement debt

shutdown
→ wait debt == 0
~~~

---

# 七十五、不同之处

TERM_ACK：

~~~text
object ownership subtree
~~~

Reaper `_sockets`：

~~~text
socket physical reclamation jobs
~~~

---

# 七十六、第四步才是真 Delete

~~~cpp
own_t::process_destroy();
~~~

最终：

~~~text
delete this
~~~

到这一刻前：

- poller 已摘除；
- Context slot 已清空；
- Context socket registry 已移除；
- Reaper completion 已发送。

---

# 七十七、为什么 Send Reaped 在 Delete 之前

`send_reaped()` 需要：

~~~text
this execution context
~~~

完成 Reaper accounting。

如果先 delete：

~~~text
后续 completion publication
~~~

无法安全执行。

---

# 七十八、Completion-before-Reclaim

这是一个非常重要的通用规则：

> **如果某个管理者还需要收到“我已经退出”的 completion，那么 completion 必须在对象存储释放之前发布。**

---

# 七十九、Reaper 什么时候真正停止

`process_stop()`：

~~~cpp
_terminating = true;

if (!_sockets) {
    send_done();
    rm mailbox fd;
    stop poller;
}
~~~

---

# 八十、如果 Stop 时还有 Socket

只设置：

~~~text
_terminating = true
~~~

继续处理：

~~~text
REAP / REAPED / socket mailbox events
~~~

---

# 八十一、最后一个 Socket Reaped

~~~cpp
if (!_sockets
    && _terminating)
~~~

才：

~~~text
send DONE
remove Reaper mailbox
stop poller
~~~

---

# 八十二、为什么 Stop Request 不是 Stop Completion

这是异步 runtime 最基本的区分：

~~~text
stop requested
≠
executor has no outstanding work
~~~

---

# 八十三、Reaper 的状态机

~~~text
RUNNING
   |
   | STOP
   v
STOP_REQUESTED
   |
   | sockets > 0
   | keep processing
   |
   | sockets == 0
   v
DONE_PUBLISHED
   |
   v
POLLER_STOPPED
~~~

---

# 八十四、Context `shutdown()` 是什么

`ctx_t::shutdown()`：

~~~text
set _terminating=true
send stop to sockets
if none, stop Reaper
return
~~~

它不：

~~~text
wait DONE
delete Context
~~~

---

# 八十五、所以 Shutdown 更像

~~~text
request_stop()
~~~

目标是：

- 让 blocking socket calls 被中断；
- 阻止新 socket 创建；
- 启动退出。

---

# 八十六、Context `terminate()` 更像 Join

它不仅发 stop，

还：

~~~text
wait Reaper DONE
assert _sockets.empty()
delete Context
~~~

---

# 八十七、为什么要区分 Request-stop 与 Join

如果所有 stop API 都同步 join：

- signal handler；
- management thread；
- async control path；

会很难组合。

两阶段 API 更通用：

~~~text
shutdown
→ announce cancellation

terminate
→ await completion + reclaim
~~~

---

# 八十八、`terminate()` 为什么会处理 Restarted Termination

源码：

~~~text
termination may have been interrupted
then restarted
~~~

所以先记：

~~~text
restarted = _terminating
~~~

只有第一次：

~~~text
send stop to sockets
~~~

避免重复广播生命周期请求。

---

# 八十九、这说明 Termination 本身也可能被 EINTR 打断

Context 等：

~~~cpp
_term_mailbox.recv(&cmd, -1)
~~~

如果：

~~~text
EINTR
~~~

可以返回上层，

之后用户再次调用 terminate。

---

# 九十、Shutdown Protocol 必须支持 Re-entry

成熟 teardown 不能假设：

~~~text
第一次调用一定执行到结尾
~~~

---

# 九十一、Context 等待什么

不是轮询：

~~~text
_sockets.empty()
~~~

而是阻塞在：

~~~text
_term_mailbox
~~~

等待：

~~~text
command_t::done
~~~

---

# 九十二、谁发送 DONE

Reaper。

所以：

~~~text
Context thread
~~~

不会自己猜：

~~~text
是不是所有 background work 都完成了
~~~

而是等最终 executor 明确 publish completion。

---

# 九十三、这是 Explicit Completion Signal

比：

~~~text
sleep + check count
~~~

更可靠。

---

# 九十四、DONE 的完整因果链

~~~text
Context sets terminating
        ↓
all sockets stop/close
        ↓
socket ownership → Reaper
        ↓
own_t barriers finish
        ↓
socket poller detached
        ↓
Context registry erase
        ↓
REAPED
        ↓
Reaper socket debt = 0
        ↓
DONE
        ↓
Context term mailbox
~~~

---

# 九十五、收到 DONE 后 Context 还会 Assert

~~~cpp
zmq_assert(_sockets.empty());
~~~

这不是多余。

它验证：

~~~text
Reaper completion signal
~~~

与：

~~~text
Context registry truth
~~~

一致。

---

# 九十六、Completion Event 与 State Truth 双验证

好的异步 protocol 经常：

~~~text
event says condition changed
state proves condition holds
~~~

而不是：

~~~text
仅相信 event
~~~

---

# 九十七、这和 Holoscan Event Wakeup 结构相似

事件：

~~~text
只是提示重新检查
~~~

最终 state：

~~~text
才是权威 truth
~~~

同样原则在不同 Runtime 反复出现。

---

# 九十八、Context 为什么最后才 Delete 自己

只有：

~~~text
all sockets gone
Reaper done
~~~

以后，

Context 才能释放：

- I/O threads；
- Reaper；
- slot table；
- global runtime resources。

---

# 九十九、Destructor 第一条 Assert

~~~cpp
zmq_assert(_sockets.empty());
~~~

Destructor 不负责：

~~~text
替你完成 socket close
~~~

它假设 protocol 已经完成。

---

# 一百、Destructor 是 Final Invariant Checker

这和 Session 一样：

> **主要 shutdown 工作应在显式异步协议中完成；析构函数负责验证 terminal invariants 并做最后资源清理。**

---

# 一百零一、Context Destructor 为什么先 Stop I/O Threads

即使 socket 都没了，

I/O thread object 仍存在。

先：

~~~text
io_thread->stop()
~~~

再：

~~~text
delete io_thread
~~~

使其内部 thread join/cleanup 有明确 stop 请求。

---

# 一百零二、为什么 Reaper 最后才 Delete

Socket reclamation 已经全部结束，

Context terminate 已收到 DONE，

此时才：

~~~text
delete _reaper
~~~

---

# 一百零三、Executor 必须活得比它负责的 Job 久

通用原则：

> **不能先销毁 completion executor，再等待它负责完成的对象。**

---

# 一百零四、Pending Inproc 为什么在 Context Terminate 开头特殊处理

源码会复制：

~~~text
_pending_connections
~~~

对每个 pending address：

~~~text
create temporary PAIR
bind pending endpoint
close PAIR
~~~

---

# 一百零五、看起来为什么很奇怪

Context 都要退出了，

为什么还：

~~~text
临时创建 socket + bind
~~~

---

# 一百零六、因为 Pending Connect 已经形成 Lifecycle Debt

此前 connect path 可能已经：

- 创建 pipe；
- 记录 pending connection；
- 增加 seqnum；
- 等 bind completion。

如果直接丢掉 pending registry：

~~~text
某些 lifetime reservation 永远无法完成
~~~

---

# 一百零七、所以 Shutdown 不是“忽略未完成建链”

而是：

> **把已经承诺出去的异步关系推进到可关闭状态。**

---

# 一百零八、Temporary PAIR 是 Completion Adapter

它的目的不是：

~~~text
建立业务连接
~~~

而是：

~~~text
让 pending connect protocol 获得它期待的 bind/completion path
~~~

然后立即 close。

---

# 一百零九、这是“完成再取消”而不是“直接忘记”

很多 async runtime 都有同类问题：

~~~text
submitted but not attached work
~~~

shutdown 时不能：

~~~text
erase vector
~~~

就认为义务消失。

---

# 一百一十、Pending Work 也属于 Reachability Graph

即使没有 fully live socket/session link，

pending object 中仍可能有：

~~~text
raw pointer
seqnum reservation
future callback destination
~~~

---

# 一百一十一、Context `_starting` 为什么在这里重要

如果从未启动：

~~~text
没有 Reaper
没有 I/O threads
没有 socket
~~~

Context terminate 可以直接进入最终资源释放。

---

# 一百一十二、这就是 Lazy Init 的 Shutdown 对称性

~~~text
never started
→ nothing asynchronous to join

started
→ must pass Reaper DONE barrier
~~~

---

# 一百一十三、Runtime Startup 与 Shutdown 应对称

建立什么：

~~~text
Reaper
I/O thread
slot registry
~~~

就要明确谁负责拆什么。

---

# 一百一十四、Socket Slot 的 Lifetime

可以画成：

~~~text
FREE
 |
 | create_socket
 v
ASSIGNED
 |
 | mailbox published in _slots
 v
ROUTABLE
 |
 | close
 v
RETIRED-BUT-ROUTABLE
 |
 | Reaper drains late commands
 v
QUIESCENT
 |
 | poller rm_fd
 | _slots[tid]=NULL
 v
UNBOUND
 |
 | tid → _empty_slots
 v
FREE
~~~

---

# 一百一十五、最容易犯的错误

把：

~~~text
close()
~~~

直接等同：

~~~text
FREE
~~~

会跳过：

~~~text
RETIRED-BUT-ROUTABLE
~~~

这个非常关键的阶段。

---

# 一百一十六、为什么 Retired Socket 还必须 Routable

因为：

~~~text
已经在飞行中的 completion command
~~~

仍要找到它。

例如：

- TERM_ACK；
- PLUG/OWN/ATTACH completion；
- inproc completion；
- Pipe termination event。

---

# 一百一十七、逻辑退役后保留地址不是矛盾

它只是意味着：

~~~text
no new business admission
but old control obligations still need destination
~~~

---

# 一百一十八、Business Reachability 与 Control Reachability 不同

Close 可以先切：

~~~text
application API reachability
~~~

再保留：

~~~text
control-plane reachability
~~~

直到 quiescence。

---

# 一百一十九、这也是安全 callback unregister 的同类结构

理想 callback：

~~~text
user discovery disabled
but invocation state retained
until in-flight callbacks drain
~~~

libzmq socket：

~~~text
application handle dead
but mailbox destination retained
until commands drain
~~~

---

# 一百二十、Reaper Poller Registration 也是一种 Temporary Reachability

进入 reaping 后：

~~~text
socket
~~~

会暂时被：

~~~text
Reaper poller
~~~

引用。

这不是 leak，

而是有意延长 lifetime 以完成 teardown。

---

# 一百二十一、Temporary Reachability 必须有明确 Exit

对应：

~~~text
_poller->rm_fd(_handle)
~~~

没有这一步：

~~~text
delete socket
~~~

就是 callback UAF 风险。

---

# 一百二十二、为什么 `destroy_socket()` 在 `_slot_sync` 下执行

它同时修改：

~~~text
_slots
_empty_slots
_sockets
~~~

还可能检查：

~~~text
_terminating && _sockets.empty()
~~~

这些必须是一个一致的 lifecycle transition。

---

# 一百二十三、Last Socket Detection 必须和 Registry Mutation 原子

错误：

~~~text
erase socket
unlock
check size == 0
~~~

另一个线程如果还能 create，

会产生竞态。

当前实现：

~~~text
same _slot_sync
+
terminating blocks create
~~~

保证 shutdown 单调性。

---

# 一百二十四、为什么 `_sockets` 用 Intrusive Array

Context `_sockets` 是：

~~~text
array_t<socket_base_t>
~~~

适合：

- 迭代所有 sockets 发送 stop；
- O(1) remove；
- 不要求稳定顺序。

---

# 一百二十五、这和 FQ/LB 的设计哲学一致

不同业务，

相同结构思想：

> **如果高频操作是 membership removal，而顺序不重要，用 intrusive index + swap erase。**

---

# 一百二十六、但 `_slots` 用 Vector

因为它的访问模式是：

~~~text
tid → exact mailbox
~~~

需要 O(1) direct index。

---

# 一百二十七、`_empty_slots` 也是 Vector Stack

分配：

~~~text
back + pop_back
~~~

回收：

~~~text
push_back
~~~

不需要排序。

---

# 一百二十八、三种 Container 对应三种访问模式

~~~text
_slots
→ indexed lookup

_sockets
→ membership + iteration + O(1) removal

_empty_slots
→ free-list stack
~~~

---

# 一百二十九、这是很好的 Data-structure-from-Access-pattern 例子

不要问：

~~~text
哪个 STL 最高级？
~~~

而问：

~~~text
最频繁的操作是什么？
是否需要稳定顺序？
是否已知整数索引？
是否是 free-list？
~~~

---

# 一百三十、Context 的 Shutdown Barrier 可以分五层

~~~text
1. Admission Barrier
   _terminating
   no new socket

2. Socket Logical Barrier
   own_t term/seqnum

3. Socket External Reachability Barrier
   Reaper poller + Context registry

4. Reaper Job Barrier
   reaper._sockets == 0

5. Context Join Barrier
   DONE on _term_mailbox
~~~

---

# 一百三十一、为什么这么多 Barrier

因为每一层证明的事实不同。

---

# 一百三十二、Admission Barrier 证明

~~~text
future work set cannot grow
~~~

---

# 一百三十三、Own Barrier 证明

~~~text
socket internal async ownership debt is zero
~~~

---

# 一百三十四、External Reachability Barrier 证明

~~~text
future poller/registry lookup cannot reach socket
~~~

---

# 一百三十五、Reaper Barrier 证明

~~~text
all deferred socket reclamation jobs finished
~~~

---

# 一百三十六、Context DONE 证明

~~~text
global shutdown executor has no remaining socket work
~~~

---

# 一百三十七、最终 Delete Context 证明

~~~text
runtime-wide ownership graph has collapsed to zero live sockets
~~~

---

# 一百三十八、这就是 Hierarchical Quiescence

不是一个：

~~~text
atomic<bool> stopped
~~~

能够表达的。

---

# 一百三十九、为什么 Stop Flag 不够

一个 flag 最多表达：

~~~text
intent
~~~

不能表达：

- outstanding command 数；
- child 数；
- poller registration；
- registry membership；
- deferred job 数。

---

# 一百四十、Intent、Progress、Completion 要分开

~~~text
_terminating
→ intent

TERM_ACK / seqnum / REAPED
→ progress

DONE
→ global completion
~~~

---

# 一百四十一、Context Termination 的完整状态图

~~~text
RUNNING
   |
   | shutdown/terminate
   v
NO_NEW_SOCKETS
   |
   | stop all live sockets
   v
SOCKETS_RETIRING
   |
   | close → REAP
   v
REAPER_OWNS_SOCKETS
   |
   | each socket quiesces
   v
SOCKETS_REAPED
   |
   | Context _sockets == 0
   v
REAPER_STOP_REQUESTED
   |
   | reaper._sockets == 0
   v
DONE
   |
   v
CONTEXT_RECLAIM
~~~

---

# 一百四十二、为什么 Context `_sockets==0` 与 Reaper `_sockets==0` 都有意义

它们理论上最终会接近同时归零，

但属于：

~~~text
不同 owner 的不同 accounting domain
~~~

不要依赖“应该差不多”。

---

# 一百四十三、分层 Counter 的价值

每个层只维护：

~~~text
自己启动的 obligation
~~~

而不是一个全局超级 refcount。

---

# 一百四十四、为什么不用一个 Global Refcount

因为对象退出条件不仅是：

~~~text
引用数量
~~~

还包括：

- event deregistration；
- ordered protocol；
- mailbox completion；
- child termination；
- policy deadline。

---

# 一百四十五、Refcount 只解决 Ownership Quantity

它不回答：

~~~text
什么时候允许新 lookup？
poller callback 是否还注册？
协议 ACK 是否完成？
~~~

---

# 一百四十六、Reaper 与 Reference Counting 的本质差异

Reaper 维护的是：

~~~text
reclamation task completion
~~~

不是：

~~~text
所有 raw/shared reference 数量
~~~

---

# 一百四十七、为什么 `send_done()` 走 Mailbox

Context terminate thread 可能阻塞等待。

Reaper 不应：

~~~text
直接跨线程调用 Context teardown method
~~~

而是通过：

~~~text
term mailbox
~~~

发布完成。

---

# 一百四十八、Completion 也遵循 Owner-thread Boundary

即使是 shutdown 最后一条消息，

仍然走统一 command transport。

---

# 一百四十九、这是 Protocol Uniformity 的价值

正常运行和 shutdown：

~~~text
same mailbox semantics
same wakeup mechanism
same ownership boundary
~~~

减少特殊-case concurrency bug。

---

# 一百五十、Context Term Mailbox 为什么单独存在

它不属于 ordinary socket/I/O thread。

用途非常专一：

~~~text
Context termination waiter
← DONE
~~~

---

# 一百五十一、Dedicated Completion Channel 的好处

避免 Context join 与业务 command 混在一起。

它可以明确断言：

~~~cpp
cmd.type == command_t::done
~~~

---

# 一百五十二、这是 Control-plane Isolation

复杂 Runtime 中：

~~~text
business events
shutdown completion
~~~

最好不要完全共享一个无类型 wakeup。

---

# 一百五十三、`ctx_shutdown()` 为什么给 Blocking Calls 发 STOP

用户线程可能阻塞在：

- recv；
- send；
- poll。

如果 Context 只是：

~~~text
_terminating=true
~~~

这些线程未必会醒。

---

# 一百五十四、Stop Command 是 Cancellation Wakeup

作用：

~~~text
wake blocked API
→ observe ETERM / termination
~~~

---

# 一百五十五、Cancellation 必须同时改变 State 与 Wake Waiter

只改状态：

~~~text
waiter sleeps forever
~~~

只发 wake：

~~~text
waiter醒来但不知道为什么
~~~

正确：

~~~text
publish termination state
+
deliver wakeup
~~~

---

# 一百五十六、这和 Condition Variable 基本原则相同

~~~text
set predicate
notify waiter
~~~

而不是只做其中一个。

---

# 一百五十七、Context `_terminating` 是 Predicate

STOP/mailbox signal 是：

~~~text
wakeup mechanism
~~~

---

# 一百五十八、为什么 Context 终止时不立即 Stop I/O Threads

因为 socket/session teardown 仍可能依赖：

~~~text
I/O thread processing
~~~

如果 Context 一开始就杀 I/O threads：

~~~text
TERM_ACK/Session completion
~~~

可能永远无法发生。

---

# 一百五十九、Executor Teardown 必须反向依赖顺序

如果：

~~~text
Socket depends on Session
Session depends on I/O thread
~~~

则关闭通常：

~~~text
retire Socket/Session work
wait completion
then stop I/O thread
~~~

---

# 一百六十、这就是 Dependency-aware Shutdown

不是：

~~~text
for each thread: stop()
for each object: delete()
~~~

---

# 一百六十一、为什么 Reaper 自己先启动、后销毁

它是：

~~~text
socket reclamation dependency
~~~

所以生命周期必须覆盖所有 socket。

---

# 一百六十二、Runtime 生命周期可以画成嵌套区间

~~~text
Context
|-------------------------------------------|

Reaper
   |-------------------------------------|

I/O Threads
   |----------------------------------|

Sockets
      |--------------------------|

Sessions/Pipes
         |-------------------|
~~~

外层对象不能先于其完成依赖退出。

---

# 一百六十三、但 Reaper 并不是 Socket Owner 的整个生命周期

Application 创建和使用阶段：

~~~text
application-side ownership
~~~

close 后：

~~~text
reclamation ownership
~~~

所以所有权会迁移。

---

# 一百六十四、Ownership Transfer 比 Shared Ownership 更清楚

不是：

~~~text
application + reaper both shared_ptr
~~~

而是明确 phase：

~~~text
before close
→ application domain

after close
→ reaper domain
~~~

---

# 一百六十五、Phase Ownership 可以减少同步

如果同一时刻只有一个 domain 被允许执行某类 mutation，

就不需要所有字段都加锁。

---

# 一百六十六、为什么 Socket Mailbox 还能在 Handoff 后继续用

mailbox 存储属于 socket，

对象没 delete 前一直有效。

只是：

~~~text
polling它的人
~~~

从 application interaction context 转成 Reaper poller。

---

# 一百六十七、这就是 Stable Object + Changing Executor

很多 Runtime：

~~~text
object lifetime
~~~

和：

~~~text
which thread currently drives it
~~~

不是一回事。

---

# 一百六十八、迁移 Executor 时最危险的三个问题

1. wakeup source 是否完整迁移；
2. old executor 是否真的停止；
3. in-flight work 是否仍能找到 object。

libzmq Reaper handoff 三个都显式处理。

---

# 一百六十九、Slot Registry 与 Actor System 很像

Actor：

~~~text
actor id
→ mailbox
~~~

libzmq：

~~~text
tid
→ mailbox
~~~

对象跨线程操作通过消息，

而不是直接共享 mutable state。

---

# 一百七十、区别在于 Slot 会 Reuse

因此：

~~~text
actor id generation
~~~

类问题会自然出现。

libzmq 用：

~~~text
quiescence-before-reuse
~~~

避免旧 command 命中新对象。

---

# 一百七十一、对自己设计 Runtime 的直接启发

如果用：

~~~text
vector<Mailbox*>
free_ids
~~~

一定要问：

> **一个 ID 什么时候可以重新分配？**

不是：

~~~text
对象调用 close 以后
~~~

而是：

~~~text
所有旧 generation 的 async sender 都不可能再投递以后
~~~

---

# 一百七十二、如何证明“再也不会投递”

常见手段：

- seqnum reservation；
- generation id；
- hazard/epoch；
- join；
- unregister + drain；
- refcount；
- owner-thread barrier。

---

# 一百七十三、libzmq 组合了多种手段

~~~text
seqnum
TERM_ACK
registry erase
Reaper completion
owner-thread mailbox
~~~

没有试图让一个机制解决全部问题。

---

# 一百七十四、这是很重要的系统设计观

> **不同异步关系应该有不同 completion proof。**

---

# 一百七十五、Command Quiescence

证明：

~~~text
tracked command all processed
~~~

工具：

~~~text
sent_seqnum / processed_seqnum
~~~

---

# 一百七十六、Child Quiescence

证明：

~~~text
owned async children all terminated
~~~

工具：

~~~text
TERM_ACK debt
~~~

---

# 一百七十七、Poller Quiescence

证明：

~~~text
future event callback cannot discover socket
~~~

工具：

~~~text
rm_fd
~~~

---

# 一百七十八、Registry Quiescence

证明：

~~~text
future context lookup/routing cannot discover socket
~~~

工具：

~~~text
_slots=NULL
_sockets.erase
~~~

---

# 一百七十九、Reclamation Quiescence

证明：

~~~text
all Reaper jobs completed
~~~

工具：

~~~text
reaper._sockets == 0
~~~

---

# 一百八十、Runtime-wide Quiescence

证明：

~~~text
whole Context can die
~~~

工具：

~~~text
DONE
+
_sockets.empty assert
~~~

---

# 一百八十一、这就是 Quiescence Vector

可以把系统终止条件看成：

\[
Q =
(Q_{cmd},
 Q_{child},
 Q_{poller},
 Q_{registry},
 Q_{reaper})
\]

只有：

\[
Q=(0,0,0,0,0)
\]

才允许最终 Context reclaim。

---

# 一百八十二、为什么单一 Refcount 很难表达

这些维度并不都是：

~~~text
pointer count
~~~

例如：

~~~text
poller registration
~~~

是可达性状态，

不是共享引用数量。

---

# 一百八十三、Context 与 Reaper 最值得学习的不是类名

而是下面这条 pipeline：

~~~text
RETIRE PUBLIC HANDLE
        ↓
PRESERVE CONTROL ADDRESS
        ↓
TRANSFER EXECUTOR
        ↓
DRAIN ASYNC OBLIGATIONS
        ↓
REMOVE EVENT REACHABILITY
        ↓
REMOVE REGISTRY REACHABILITY
        ↓
PUBLISH COMPLETION
        ↓
RECLAIM
        ↓
REUSE ADDRESS
~~~

---

# 一百八十四、注意 Reuse 在 Reclaim 之后

这点非常关键。

很多 handle-table 系统会犯：

~~~text
unregister
→ immediately recycle id
~~~

但旧 completion 仍可能回来。

---

# 一百八十五、如果必须早 Reuse 怎么办

就需要：

~~~text
generation tag
~~~

例如：

~~~text
handle = {index, generation}
~~~

旧 completion：

~~~text
generation mismatch
→ discard
~~~

---

# 一百八十六、机器人设备表同样需要这个设计

例如：

~~~text
device slot 7
~~~

旧电机 controller 断线，

新 controller 恰好复用 slot 7。

晚到 CAN/TCP async completion 如果只带：

~~~text
slot=7
~~~

就可能污染新设备。

---

# 一百八十七、机器人 Runtime 推荐

要么：

~~~text
quiescence-before-slot-reuse
~~~

要么：

~~~text
slot + epoch
~~~

最好两者之一明确存在。

---

# 一百八十八、Executor Ownership 也适用于设备热插拔

设备：

~~~text
ACTIVE owner thread
~~~

disconnect 后：

~~~text
REAPER/cleanup executor
~~~

继续：

- drain callback；
- detach epoll；
- close fd；
- retire registry；
- free buffers。

---

# 一百八十九、不要在业务线程直接复杂析构

尤其 destructor 会：

- free DMA；
- deregister poller；
- wait worker；
- close device；
- call callbacks；

容易制造 latency spike 和 lock inversion。

---

# 一百九十、Reaper Pattern 的优点

把复杂 reclamation：

~~~text
搬到专用执行域
~~~

业务 API close 可以快速完成：

~~~text
logical retirement
~~~

---

# 一百九十一、但异步 Close 的代价

用户必须理解：

~~~text
close returned
≠
all resources physically gone
~~~

所以 Context 还需要：

~~~text
terminate/join
~~~

---

# 一百九十二、API 设计必须暴露最终 Barrier

如果只有：

~~~text
async close
~~~

没有：

~~~text
join/drain context
~~~

应用退出时很难证明资源完全回收。

---

# 一百九十三、libzmq 用 Context Terminate 提供 Barrier

这就是：

~~~text
system-level join
~~~

---

# 一百九十四、为什么 Context Terminate 不是简单 Thread Join

因为要 join 的不是一个 thread，

而是：

~~~text
whole dependency graph
~~~

---

# 一百九十五、Thread Join 只是最后一层实现动作

真正 barrier 是：

~~~text
sockets closed
→ reaped
→ DONE
~~~

之后 destructor 才能安全停止/删除后台线程对象。

---

# 一百九十六、Context 是 Runtime Root

所有：

- sockets；
- I/O threads；
- reaper；
- inproc registry；
- slot address space；

最终都依赖它。

所以它必须最后死。

---

# 一百九十七、Root Object 的 Destroy Rule

> **Root 必须等待所有可能回调/命令回到 Root 管理域的对象都退出以后再 reclaim。**

---

# 一百九十八、这和 GUI Event Loop 类似

窗口对象可以先 close，

但 event loop/root application：

~~~text
必须等 pending event/callback drain
~~~

再退出。

---

# 一百九十九、这和 GPU Context 类似

Stream/task 可以结束，

但 CUDA context：

~~~text
不能在仍有 pending work 时销毁
~~~

同样是 root lifetime barrier。

---

# 二百、这和 ROS Executor Shutdown 也类似

节点 handle dead：

~~~text
不等于
callback executor already quiescent
~~~

如果 callback 正在执行，

必须先有：

~~~text
cancel admission
+
drain/join executor
~~~

---

# 二百零一、Context 的最终五层模型

~~~text
Layer 1
Logical Address Space
_slots / tid

Layer 2
Live Object Registry
_sockets

Layer 3
Async Reclamation Executor
Reaper

Layer 4
Completion Channel
_term_mailbox / DONE

Layer 5
Root Resource Owner
ctx_t destructor
~~~

---

# 二百零二、每层的核心不变量

Address Space：

~~~text
live tid maps to correct mailbox
~~~

Registry：

~~~text
all Context-owned live sockets discoverable
~~~

Reaper：

~~~text
every REAP has one REAPED
~~~

Completion：

~~~text
DONE only after reaper debt = 0
~~~

Root：

~~~text
no socket remains before delete
~~~

---

# 二百零三、源码作者最应该守住的十条规则

第一：

> **可复用 ID/slot 本质是地址；旧 generation 的异步操作未 quiescent 前不能重绑定给新对象。**

第二：

> **API close 与 memory free 必须分离，尤其对象仍是 mailbox/poller/registry target 时。**

第三：

> **ownership handoff 必须先建立新 executor 和 wakeup path，再启动 termination。**

第四：

> **内部 quiescence 与外部 reachability 是不同维度；own_t 能 delete 普通对象，不代表 socket 可以直接 delete。**

第五：

> **poller unregister、registry erase、completion publish、memory free 有严格先后关系。**

第六：

> **stop request 与 stop completion 必须分离；Reaper `_terminating` 只是 intent，`DONE` 才是 completion。**

第七：

> **Context shutdown 先禁止新 work，再等待旧 work 单调减少。**

第八：

> **pending/half-created relationship 也是 lifetime debt，shutdown 不能简单丢 registry。**

第九：

> **executor 必须活得比它负责推进完成的对象更久。**

第十：

> **Destructor 应验证终态，不应承担需要未来事件才能完成的异步 shutdown protocol。**

---

# 二百零四、最终完整时序

~~~text
Application
   |
   | zmq_close(socket)
   v
socket public tag dead
   |
   | REAP
   v
Reaper mailbox
   |
   | process_reap
   v
socket start_reaping
   |
   +--> install socket mailbox into Reaper poller
   |
   +--> install/force signaler if thread-safe
   |
   +--> terminate()
   |
   v
own_t / Pipe / Session barriers
   |
   | all TERM_ACK + seqnum complete
   v
socket._destroyed = true
   |
   v
socket::check_destroy
   |
   +--> poller.rm_fd
   |
   +--> ctx.destroy_socket
   |       |
   |       +--> _slots[tid] = NULL
   |       +--> tid → _empty_slots
   |       +--> _sockets.erase
   |       +--> maybe reaper.stop
   |
   +--> send REAPED
   |
   +--> delete socket
   |
   v
Reaper process_reaped
   |
   | --_sockets
   |
   | if terminating && count==0
   v
send DONE
   |
   v
Context _term_mailbox
   |
   v
assert ctx._sockets.empty()
   |
   v
delete Context
   |
   +--> stop/delete I/O threads
   +--> delete Reaper
   +--> release global resources
~~~

---

# 二百零五、最终心智模型

不要把 Context 看成：

~~~text
一些 socket 的容器
~~~

更准确地说：

> **Context 是 libzmq 的控制面地址空间与 Runtime root；Reaper 是 retired socket 的临时 execution owner；slot、seqnum、TERM_ACK、poller registration、REAPED 和 DONE 一起定义了“什么时候一个 logical address 可以解绑、什么时候对象可以释放、什么时候整个 Runtime 可以退出”。**

如果只记住一句：

> **安全销毁不是“没人再想用这个对象”，而是“所有未来能够找到、唤醒、回调或投递到这个对象的路径都已被逐层关闭，并且所有已经承诺的异步义务都已经完成”。**
