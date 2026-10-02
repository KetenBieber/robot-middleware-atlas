# Linger 与终止协议：为什么 close 不是 delete

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

并发 Runtime 最危险的代码经常不是发送，而是关闭。

当一个 ZeroMQ socket 正在退出时，系统里可能还同时存在：

~~~text
application thread
socket-side pipes
I/O thread
Session
Engine
Connecter
poller registrations
timers
mailbox commands already in flight
messages waiting in Pipe
~~~

如果 close 最终只是立即 delete socket，那么任何一个晚到的 command、timer 或 peer acknowledgement 都可能重新进入已经释放的对象。

libzmq 因此把 termination 做成一套显式协议。这里先分清两个问题：

~~~text
linger
  -> 还没发完的业务数据愿意等多久？

termination barrier
  -> 对象什么时候才真的允许释放？
~~~

它们相关，但不是同一件事。

## 1. queue empty 不等于对象可以销毁

即使 payload queue 已经空了，仍可能有：

~~~text
activate_write command
term request
bind / attach command
poller callback
child termination ack
~~~

已经在路上。

反过来，即使所有控制 command 都处理完，Pipe 中也可能还有调用者希望在关闭前发出去的数据。

所以关闭至少要同时考虑：

~~~text
data-drain policy
+
event-source cancellation
+
child lifecycle
+
in-flight command quiescence
~~~

## 2. own_t 建立显式 ownership tree

own_t 的核心状态：

~~~cpp
_terminating (false),
_sent_seqnum (0),
_processed_seqnum (0),
_owner (NULL),
_term_acks (0)
~~~

owner 创建 child：

~~~cpp
void zmq::own_t::launch_child (
  own_t *object_)
{
    object_->set_owner (this);

    send_plug (object_);
    send_own (this, object_);
}
~~~

运行时因此形成部分 ownership tree：

~~~text
owner
  |
  +-- child A
  +-- child B
  +-- child C
~~~

关闭可以沿这棵树传播，而不是让每个异步对象独立决定何时 delete 自己。

## 3. child 为什么先向 owner 请求终止

terminate()：

~~~cpp
void zmq::own_t::terminate ()
{
    if (_terminating)
        return;

    if (!_owner) {
        process_term (
          options.linger.load ());
        return;
    }

    send_term_req (
      _owner,
      this);
}
~~~

root 没有 owner，只能自己启动终止；普通 child 则先把请求交回 owner。

否则容易出现：

~~~text
owner still stores child pointer
        |
child self-deletes
        |
owner later traverses registry
        |
dangling pointer
~~~

生命周期状态必须由持有 ownership registry 的对象参与协调。

## 4. owner 收到请求后为什么先 erase 再发 TERM

process_term_req：

~~~cpp
void zmq::own_t::process_term_req (
  own_t *object_)
{
    if (_terminating)
        return;

    if (0 == _owned.erase (object_))
        return;

    register_term_acks (1);

    send_term (
      object_,
      options.linger.load ());
}
~~~

时序是：

~~~text
remove child from active owned set
        |
register one expected TERM_ACK
        |
send TERM
~~~

先登记期待的 acknowledgement，再让异步命令出去，可以避免 ack 先到而计数尚未建立的竞态。

## 5. 整棵子树如何进入 terminating

process_term()：

~~~cpp
void zmq::own_t::process_term (
  int linger_)
{
    zmq_assert (!_terminating);

    for (owned_t::iterator it =
           _owned.begin (),
         end = _owned.end ();
         it != end;
         ++it)
        send_term (*it, linger_);

    register_term_acks (
      static_cast<int> (
        _owned.size ()));

    _owned.clear ();

    _terminating = true;
    check_term_acks ();
}
~~~

此时只是：

~~~text
terminating = true
~~~

并没有释放对象。

这个差别很重要。**进入终止状态** 与 **物理销毁** 是两个阶段。

## 6. term_ack 像异步 join

计数逻辑：

~~~cpp
void zmq::own_t::register_term_acks (
  int count_)
{
    _term_acks += count_;
}

void zmq::own_t::unregister_term_ack ()
{
    zmq_assert (_term_acks > 0);
    _term_acks--;

    check_term_acks ();
}

void zmq::own_t::process_term_ack ()
{
    unregister_term_ack ();
}
~~~

可以把它看成：

~~~text
parent sends TERM to N children
       |
       +---- child A ... TERM_ACK
       +---- child B ... TERM_ACK
       +---- child C ... TERM_ACK
       |
       v
term_acks == 0
~~~

但 libzmq 仍然不会立刻 delete。

## 7. 为什么 term_acks == 0 仍不够

真正释放条件：

~~~cpp
void zmq::own_t::check_term_acks ()
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

还必须满足：

~~~text
processed_seqnum == sent_seqnum
~~~

这解决的是 **command 已经发出但尚未被 owner thread 消费** 的问题。

错误时序可以想成：

~~~text
thread A                         owner thread B

send command X
increment sent_seqnum
    |
    |       TERM/ACK completes
    |--------------------------->
                                term_acks -> 0
                                delete object
    |
command X still in mailbox
    |--------------------------->
                                process X on freed object
~~~

只等待 child ack 还不够，因为 mailbox 里可能仍有合法 command 指向该对象。

## 8. sent / processed sequence 是一种 quiescence barrier

inc_seqnum() 明确允许跨线程调用：

~~~cpp
void zmq::own_t::inc_seqnum ()
{
    _sent_seqnum.add (1);
}
~~~

owner thread 处理到相应序号后：

~~~cpp
void zmq::own_t::process_seqnum ()
{
    _processed_seqnum++;
    check_term_acks ();
}
~~~

于是：

~~~text
sent == processed
~~~

表示这一类已经发布的控制工作追平。

这里并不是在追踪业务 payload 的数量，而是在证明：

> 已知可能指向该对象的控制消息已经全部越过处理边界。

这是一种很轻量的 quiescence 条件。

更精确地说，`sent_seqnum` 统计的是已经取得“未来执行资格”的生命周期敏感 command，而不只是已经进入 mailbox 的 command；inproc lookup 甚至会在 registry lock 内先 `inc_seqnum()`，再把目标裸指针交给调用者。这个 pre-reservation 协议、`inc_seqnum=false` 的配对关系以及 Reaper 外层屏障见 [Command Seqnum 与对象销毁屏障](command-seqnum-quiescence.md)。

## 9. 最后一步才是真正 delete

~~~cpp
void zmq::own_t::process_destroy ()
{
    delete this;
}
~~~

真正重要的不是 delete 这一行，而是到达它之前已经证明：

~~~text
terminating == true
owned set is empty
term_acks == 0
processed_seqnum == sent_seqnum
~~~

安全销毁一个异步对象，本质上是在回答：

**未来还有没有任何合法执行路径能再次进入这个对象？**

## 10. linger 解决的是待发送数据，而不是对象引用

Session 的 process_term()：

~~~cpp
if (!_pipe
    && !_zap_pipe
    && _terminating_pipes.empty ()) {
    own_t::process_term (0);
    return;
}

_pending = true;
~~~

如果存在正常 Pipe，则根据 linger 决定是否等待 pending outbound messages。

正 linger 会设置 deadline：

~~~cpp
if (linger_ > 0) {
    zmq_assert (!_has_linger_timer);

    add_timer (
      linger_,
      linger_timer_id);

    _has_linger_timer = true;
}
~~~

然后：

~~~cpp
_pipe->terminate (linger_ != 0);
~~~

这行把三种语义压得很清楚：

~~~text
linger == 0
  -> terminate pipe without waiting
  -> pending data may be abandoned

linger > 0
  -> delay pipe termination
  -> install finite deadline timer

linger < 0
  -> delay pipe termination
  -> no finite timer
~~~

所以 linger 不是“close 时 sleep 一会儿”，而是 Pipe termination 的 drain policy。

## 11. 正 linger 到期以后发生什么

timer callback：

~~~cpp
void zmq::session_base_t::
timer_event (int id_)
{
    zmq_assert (
      id_ == linger_timer_id);

    _has_linger_timer = false;

    zmq_assert (_pipe);
    _pipe->terminate (false);
}
~~~

deadline 到达以后，策略从：

~~~text
try to drain pending messages
~~~

变成：

~~~text
terminate even if pending messages remain
~~~

这正是 bounded shutdown 的含义。

## 12. Socket 自己为什么又会 terminate(false)

socket_base_t::process_term：

~~~cpp
void zmq::socket_base_t::process_term (
  int linger_)
{
    unregister_endpoints (this);

    for (pipes_t::size_type i = 0,
         size = _pipes.size ();
         i != size;
         ++i) {

        _pipes[i]->send_disconnect_msg ();
        _pipes[i]->terminate (false);
    }

    register_term_acks (
      static_cast<int> (
        _pipes.size ()));

    own_t::process_term (linger_);
}
~~~

这里不能据此得出“linger 没有作用”。

Socket-side Pipe endpoint 和 Session-side network lifecycle 属于不同对象边界。Socket 先阻止新 inproc endpoint，终止自己挂着的 Pipe，并为这些 Pipe 登记 acknowledgement；linger 参数继续向 ownership tree 传播，真正掌握 network-side pending messages 的 Session 再执行 drain/deadline 逻辑。

因此关闭并不是所有对象统一等待同一个毫秒数，而是：

~~~text
socket owner
  -> stop accepting new local relationships

Pipe
  -> run endpoint termination protocol

Session
  -> decide whether pending network-bound data should drain

own_t
  -> wait for child/control quiescence
~~~

## 13. 为什么 Connecter 终止时首先取消 timer 和 poller handle

在 TCP Connecter 中，process_term 会先：

~~~text
cancel connect-timeout timer
cancel reconnect timer
remove poller handle
close fd
~~~

然后才进入 own_t 的终止协议。

这是通用 shutdown 顺序：

~~~text
1. prevent new callbacks
2. detach registries / poller / timers
3. stop or terminate children
4. wait for in-flight control work
5. destroy memory
~~~

如果第 5 步先发生，前四层任何晚到事件都可能重新进入 freed object。

## 14. 一条完整的关闭时间线

把主要机制连起来：

~~~text
application calls close
        |
        v
socket begins termination
        |
        +-- unregister endpoints
        +-- terminate attached pipes
        +-- register pipe term acks
        |
        v
own_t::process_term(linger)
        |
        +-- TERM -> owned Sessions / objects
        +-- register child term acks
        |
        v
Session process_term
        |
        +-- linger = 0
        |      immediate pipe termination
        |
        +-- linger > 0
        |      drain + deadline
        |
        +-- linger < 0
               drain without finite deadline
        |
        v
children report TERM_ACK
        |
        v
owner waits
  term_acks == 0
  processed_seqnum == sent_seqnum
        |
        v
TERM_ACK to parent
        |
        v
process_destroy()
        |
        v
delete this
~~~

这比“析构函数里 join 一下线程”更一般，因为中间件里不只有线程，还存在 Pipe peer、poller callback、timer 与跨线程 command。

## 15. shared_ptr 为什么不能自动解决 shutdown

智能指针主要回答：

~~~text
who owns the C++ allocation?
~~~

却不会自动回答：

~~~text
is fd still registered in poller?
is timer still armed?
does a mailbox still contain a command targeting this object?
has peer endpoint acknowledged termination?
should pending payload be drained or dropped?
~~~

所以异步生命周期安全实际上是：

~~~text
memory lifetime
+
event-source lifetime
+
protocol quiescence
~~~

三者组合。

## 16. Linger 在机器人控制里不能机械设置

不同数据语义对“关闭前尽量发完”有不同价值。

旧速度指令：

~~~text
wait 2 seconds and eventually deliver
~~~

可能比直接丢弃更危险，因为控制指令会快速过期。

日志或任务结果则可能值得 bounded drain。

因此更合理的是：

~~~text
safety-critical command
  -> explicit max age
  -> often no stale drain

telemetry
  -> small bounded drain

critical record
  -> longer bounded drain
     or durable storage
~~~

ZeroMQ linger 只提供 transport queue 的排空策略；是否应该等待，必须由业务语义决定。

## 17. 抽象成通用 Shutdown Barrier

设计自研 Runtime 时，可以直接把释放条件写成显式不变量：

~~~text
object can be destroyed iff

A. no new registration can reach it
B. all event sources are detached/cancelled
C. all children reached terminal state
D. all in-flight commands crossed a sequence barrier
E. data-drain policy completed or reached deadline
F. external borrows are no longer valid
~~~

这比在 destructor 里堆多个 stop flag 更容易审查。

到这里，libzmq 的三条主线就闭合了：前面的 [Mailbox 与 Command](mailbox-command-wakeup.md) 解释 command 怎样进入 owner thread，[Pipe 与 HWM](pipe-hwm-backpressure.md) 解释 payload 怎样受容量约束，这篇则解释对象怎样在所有数据与控制路径真正退潮以后才释放。
