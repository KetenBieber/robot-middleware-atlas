# Context 与 Reaper：Socket 为什么 Close 以后还不能立刻析构

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

前面的 [Linger 与终止协议](linger-termination-protocol.md) 已经解释 own_t 怎样用 TERM/TERM_ACK、seqnum 和 linger 判断一个异步对象什么时候逻辑上可以结束。

但 socket_base_t 还有最后一层问题：

> application thread 调用 close() 以后，谁继续处理晚到 command，谁从 poller 和 Context registry 中摘掉这个 socket，最终又是谁执行 delete？

libzmq 的答案是 Reaper thread。

它不是垃圾回收器，而是一个关闭阶段的 ownership transfer executor。

## 1. close() 为什么不能直接 delete

Socket 关闭时仍可能有：

~~~text
mailbox command already queued
Pipe TERM / TERM_ACK
inproc bind command
Session / Engine child
monitor shutdown
Context slot entry
poller callback
~~~

如果 close() 直接释放对象：

~~~text
application thread
  |
  v
close()
  |
  v
delete socket
~~~

晚到 command 或 poller callback 就可能进入已释放内存。

如果 application thread 自己阻塞等待所有异步关系退场，又可能和需要该线程继续处理的控制路径形成死锁。

因此 libzmq 把阶段拆开：

~~~text
application thread
  |
  | close()
  v
ownership handoff
  |
  v
Reaper thread
  |
  | drain termination/control work
  v
physical destruction
~~~

## 2. Context 的 slot 不是 OS Thread 数组

ctx_t 保存：

~~~cpp
std::vector<i_mailbox *> _slots;
std::vector<uint32_t> _empty_slots;
~~~

Context 启动时预留：

~~~text
slot 0
  -> ctx termination mailbox

slot 1
  -> Reaper mailbox

slot 2...
  -> I/O thread mailboxes

remaining slots
  -> application sockets
~~~

slot 的本质是：

~~~text
logical tid
  -> mailbox
~~~

object_t 发 command 时，只要知道目标 tid，Context 就能在 _slots 中找到 mailbox。

因此 slot 更接近 control-plane address，而不是 OS thread handle。

## 3. 为什么每个 Socket 也占一个 Slot

create_socket()：

~~~cpp
const uint32_t slot =
  _empty_slots.back ();

_empty_slots.pop_back ();

socket_base_t *s =
  socket_base_t::create (
    type_,
    this,
    slot,
    sid);

_sockets.push_back (s);
_slots[slot] =
  s->get_mailbox ();
~~~

这样 I/O thread 或其他 socket 可以：

~~~text
destination socket tid
  |
  v
Context slot
  |
  v
socket mailbox
~~~

把跨线程 command 路由给 application-side socket。

## 4. Context 为什么延迟启动后台线程

ctx_t 初始保持：

~~~text
_starting = true
~~~

第一次 create_socket() 才：

~~~cpp
if (_starting) {
    if (!start ())
        return NULL;
}
~~~

start() 创建：

~~~text
term mailbox
Reaper
configured I/O threads
socket slot pool
~~~

如果程序只创建 Context、设置 option、随后销毁，而从未创建 socket，就不需要提前启动后台线程。

这是 lazy runtime initialization。

## 5. close() 真正做了什么

socket_base_t::close：

~~~cpp
if (_thread_safe)
    mailbox_safe
      ->clear_signalers ();

_tag = 0xdeadbeef;

send_reap (this);

return 0;
~~~

它没有同步执行：

~~~text
delete
join I/O thread
wait all Pipes
wait linger
~~~

最关键的一句是：

~~~text
send_reap(this)
~~~

源码注释直接说明：

~~~text
Transfer ownership of the socket
from the application thread
to the Reaper thread.
~~~

所以 close() 的核心语义是：

> application ownership 结束；后续 teardown 责任交给 Reaper。

## 6. Logical Death 为什么早于 Physical Free

close() 先：

~~~cpp
_tag = 0xdeadbeef;
~~~

这会让后续 public API 不再把该对象视为合法 socket。

但对象内存仍然存在，直到 Reaper 最终回收。

因此：

~~~text
API lifetime
  ends at close()

memory lifetime
  ends later
~~~

异步 Runtime 中经常需要这种两阶段死亡。

## 7. Reaper 自己也是 Mailbox + Poller

reaper_t 构造时：

~~~cpp
_poller =
  new poller_t (*ctx_);

_mailbox_handle =
  _poller->add_fd (
    _mailbox.get_fd (),
    this);

_poller->set_pollin (
  _mailbox_handle);
~~~

启动：

~~~cpp
_poller->start ("Reaper");
~~~

所以 Reaper 没有新的并发模型，仍然复用：

~~~text
mailbox
signaler fd
poller
command_t
process_command
~~~

Reaper mailbox 可读时：

~~~cpp
command_t cmd;

_mailbox.recv (
  &cmd,
  0);

cmd.destination
  ->process_command (cmd);
~~~

它只是换了一个 owner execution context。

## 8. process_reap() 怎样真正接管 Socket

Reaper 收到 reap command：

~~~cpp
void reaper_t::process_reap (
  socket_base_t *socket_)
{
    socket_->start_reaping (
      _poller);

    ++_sockets;
}
~~~

真正 handoff 在 start_reaping() 中完成。

普通 socket 会取自己的 mailbox fd：

~~~cpp
fd =
  static_cast<mailbox_t *> (
    _mailbox)
    ->get_fd ();
~~~

然后把 socket 本身注册到 Reaper poller：

~~~cpp
_handle =
  _poller->add_fd (
    fd,
    this);

_poller->set_pollin (
  _handle);
~~~

从此以后，这只 socket 的 late command 由 Reaper poller 推进。

## 9. Thread-safe Socket 为什么需要额外 Signaler

thread-safe socket 使用 mailbox_safe_t。

进入 reaping 阶段：

~~~cpp
_reaper_signaler =
  new signaler_t ();

fd =
  _reaper_signaler
    ->get_fd ();

mailbox_safe
  ->add_signaler (
    _reaper_signaler);

_reaper_signaler->send ();
~~~

Shutdown handoff 不只是搬一个对象指针，还必须确保新 owner 有可靠的 wakeup source。

否则 mailbox 里已经存在 command，但 Reaper 没有事件可读，就可能永远无法继续 teardown。

## 10. 为什么先接入 Reaper，再 terminate()

start_reaping() 最后：

~~~cpp
terminate ();
check_destroy ();
~~~

顺序是：

~~~text
1. establish new event owner
2. register wakeup fd
3. start termination
~~~

如果 terminate 在前，TERM_ACK 或其他 command 立刻返回时，还没有 execution context 接手处理。

这是一条通用原则：

> ownership transfer 要先建立新的推进路径，再撤销旧路径。

## 11. Socket 的 process_destroy() 为什么只置 Flag

socket_base_t 覆盖：

~~~cpp
void socket_base_t::process_destroy ()
{
    _destroyed = true;
}
~~~

它没有直接 delete this。

因为此刻 socket 仍可能注册在 Reaper poller 中：

~~~text
poller
  -> handle
  -> socket event sink
~~~

如果 own_t 条件刚满足就释放内存，poller registry 会悬空。

所以逻辑终止只设置：

~~~text
_destroyed = true
~~~

真正 delete 必须在 Reaper execution context 中先完成 poller detach。

## 12. Reaper 怎样推进最终销毁

socket 进入 Reaper 后，mailbox readable 会调用：

~~~cpp
socket_base_t::in_event ()
~~~

其中：

~~~cpp
process_commands (
  0,
  false);

check_destroy ();
~~~

因此 teardown 是事件驱动的：

~~~text
late command arrives
  |
  v
Reaper wakes
  |
  v
process TERM / ACK / seqnum
  |
  v
own_t may mark destroyed
  |
  v
check_destroy
~~~

不是一个 busy-wait loop。

## 13. check_destroy() 才真正完成回收

源码：

~~~cpp
if (_destroyed) {
    _poller->rm_fd (
      _handle);

    destroy_socket (this);

    send_reaped ();

    own_t::process_destroy ();
}
~~~

四步顺序非常重要。

第一：

~~~text
remove from Reaper poller
~~~

避免未来 callback。

第二：

~~~text
destroy_socket(this)
~~~

让 Context 移除 socket registry 与 slot。

第三：

~~~text
send_reaped()
~~~

让 Reaper 的 active reaping count 减一。

最后：

~~~text
own_t::process_destroy()
  -> delete this
~~~

到 delete 前，socket 已经从主要外部可达结构全部摘除。

## 14. destroy_socket() 为什么把 Slot 放回 Free List

Context：

~~~cpp
const uint32_t tid =
  socket_->get_tid ();

_empty_slots.push_back (tid);
_slots[tid] = NULL;

_sockets.erase (socket_);
~~~

slot 可以被未来 socket 复用。

但 slot reuse 必须发生在旧 socket 的 control work 已经完成以后。

否则可能发生：

~~~text
old command targeting tid 42
  |
slot 42 reused
  |
new socket now owns slot 42
~~~

这也是前面 seqnum / termination barrier 必不可少的原因。

## 15. Reaper 为什么记录 _sockets

process_reap：

~~~cpp
++_sockets;
~~~

process_reaped：

~~~cpp
--_sockets;
~~~

如果 Reaper 已经收到 stop：

~~~cpp
if (!_sockets
    && _terminating) {

    send_done ();

    _poller->rm_fd (
      _mailbox_handle);

    _poller->stop ();
}
~~~

停止条件不是简单：

~~~text
stop requested
~~~

而是：

~~~text
stop requested
AND
no socket remains in reaping
~~~

这又是一层 shutdown barrier。

## 16. Context shutdown() 与 terminate() 的差别

shutdown() 主要做：

~~~text
set _terminating
send stop to sockets
return
~~~

它让阻塞中的 socket call 被打断，并让后续 API 返回 ETERM。

terminate() 则要完成：

~~~text
all sockets closed
Reaper finished
Context resources released
~~~

可以类比：

~~~text
request_stop()
join()
~~~

两个动作不应混为一谈。

## 17. terminate() 怎样等待 Reaper Done

terminate() 对现存 socket：

~~~cpp
_sockets[i]->stop ();
~~~

如果已经没有 socket：

~~~cpp
_reaper->stop ();
~~~

随后不是 busy-wait，而是等待专用 termination mailbox：

~~~cpp
command_t cmd;

const int rc =
  _term_mailbox.recv (
    &cmd,
    -1);

zmq_assert (
  cmd.type
  == command_t::done);
~~~

Reaper 在：

~~~text
_terminating == true
AND
_sockets == 0
~~~

时 send_done()。

所以完整 join：

~~~text
Context terminate thread
      |
      | STOP
      v
sockets
      |
      | close / reap
      v
last socket reaped
      |
      v
Reaper sends DONE
      |
      v
term mailbox wakes
      |
      v
Context confirms _sockets empty
      |
      v
delete Context
~~~

## 18. Pending inproc 为什么会影响 Context 退出

terminate() 开头会先处理 _pending_connections。

它为尚未 bind 的 inproc address 临时创建 PAIR socket、bind、再 close。

原因在 [inproc Endpoint Registry](inproc-endpoint-registry.md) 中已经展开。

从 Context 角度：

~~~text
pending inproc
  -> outstanding Pipe / seqnum obligation
  -> socket cannot fully terminate
  -> Reaper count cannot reach zero
  -> Context cannot receive DONE
~~~

因此 Context 关闭必须清算“未完成建链的关系”，而不只是处理已经 fully connected 的 socket。

## 19. Reaper 和 Garbage Collector 的区别

Reaper 不会：

~~~text
扫描引用图
发现 unreachable object
替用户自动 close
~~~

用户仍然负责 close socket。

Reaper 负责的是：

~~~text
after explicit close:
  transfer ownership
  drain async control work
  detach event source
  release Context slot
  delete safely
~~~

所以它更准确的定位是 asynchronous destruction executor。

## 20. Runtime 的四层生命周期

整组 libzmq 可以压成：

~~~text
Message lifetime
  -> msg_t refcount / free callback

Pipe lifetime
  -> HWM / termination handshake

Object lifetime
  -> TERM_ACK + seqnum

Socket / Context lifetime
  -> Reaper + slot + DONE join
~~~

每一层解决不同问题。

payload refcount 为 0 不等于 socket 可以 free；TERM_ACK 收齐也不等于 poller 已经解除注册；最后一只 socket 被 delete 之前，Context 也不能提前结束 Reaper。

这就是生产级消息 Runtime 为什么需要多层生命周期协议。

## 21. 对机器人软件的迁移价值

相机、网络连接、设备 session、异步 worker、timer 等对象都可能遇到：

~~~text
callback after free
timer after free
fd event after free
worker keeps stale pointer
~~~

可以借鉴：

~~~text
public close()
  -> mark API object dead
  -> hand off to lifecycle executor

lifecycle executor
  -> unregister fd/callback
  -> stop children
  -> drain control messages
  -> wait acknowledgement
  -> return registry slot
  -> delete object
~~~

这种模式适合非实时资源层。

硬实时控制核心则更适合预分配和静态生命周期，避免运行时频繁进入复杂 teardown。

至此，libzmq 从消息存储、Pipe、command、I/O、协议、重连、inproc、monitor、proxy 到 Context shutdown 已经形成完整主线。
