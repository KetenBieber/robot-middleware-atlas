# Pipe 与 HWM：Backpressure、双向通道与异步关闭状态机

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

`ypipe_t` 只解决“一个 writer 怎样把完整 item 发布给一个 reader”。ZeroMQ socket 还需要更高一层对象来表达：双向 endpoint、multipart message、容量、恢复通知、routing metadata、断连以及异步销毁。这个对象就是 `pipe_t`。

因此 `pipe_t` 不是“再包一层 queue”。它已经是一只小型通信状态机。

## pipepair 里到底有几个对象

`pipepair()` 创建：

~~~text
2 x pipe_t endpoint
2 x unidirectional ypipe
~~~

对象关系：

~~~text
               direction 0 -> 1
        +-------------------------------->
        |           upipe1
        |
   pipe[0]                              pipe[1]
        |                                  |
        |           upipe2                 |
        +<---------------------------------+
               direction 1 -> 0
~~~

两个 endpoint 各自保存：

~~~text
_in_pipe
_out_pipe
_peer
~~~

而且它们是交叉连接的：

~~~text
pipe[0]._in_pipe  == upipe1
pipe[1]._out_pipe == upipe1

pipe[1]._in_pipe  == upipe2
pipe[0]._out_pipe == upipe2
~~~

固定源码中的构造关系：

~~~cpp
pipes_[0] =
  new pipe_t (parents_[0],
              upipe1,
              upipe2,
              hwms_[1],
              hwms_[0],
              conflate_[0]);

pipes_[1] =
  new pipe_t (parents_[1],
              upipe2,
              upipe1,
              hwms_[0],
              hwms_[1],
              conflate_[1]);
~~~

双向通信没有要求底层 queue 变成“双向队列”；而是组合两条单向 SPSC channel。

## 每个 endpoint 属于谁

`pipe_t` 继承 `object_t`，因此有所属 thread id。peer endpoint 可以属于另一个线程。

典型关系：

~~~text
socket/application owner thread
    |
    +-- pipe endpoint A
           |
           | command passing
           v
    +-- pipe endpoint B
         session / I/O owner thread
~~~

两个 endpoint 不应该直接跨线程任意修改对方内部字段。

需要让 peer 改状态时，使用 `object_t::send_*()` command：

~~~text
send_activate_read(peer)
send_activate_write(peer, msgs_read)
send_pipe_term(peer)
send_pipe_term_ack(peer)
send_hiccup(peer, ...)
~~~

所以：

~~~text
ypipe
  -> data plane

object command/mailbox
  -> control plane
~~~

这两条路径在 `pipe_t` 中汇合。

## _in_active / _out_active 不是“连接是否存在”

成员：

~~~text
_in_active
_out_active
~~~

表示当前 endpoint 是否值得继续尝试 read/write。

### input 读空

`check_read()`：

~~~cpp
if (!_in_pipe->check_read ()) {
    _in_active = false;
    return false;
}
~~~

底层 ypipe 已进入 passive 后，继续反复 polling 同一 pipe 没有意义。

状态变成：

~~~text
in_active = false
~~~

等 peer 新 publish 数据时，通过：

~~~text
activate_read command
~~~

重新激活。

### output 达到 HWM

`check_write()`：

~~~cpp
if (unlikely (!_out_active || _state != active))
    return false;

const bool full = !check_hwm ();

if (unlikely (full)) {
    _out_active = false;
    return false;
}
~~~

达到容量边界以后：

~~~text
out_active = false
~~~

调用方不应该继续热循环尝试写。等 reader 消费足够多，再通过 `activate_write` 恢复。

因此 active flag 的含义更接近：

~~~text
“当前有进展可能吗？”
~~~

而不是：

~~~text
“物理连接还活着吗？”
~~~

## HWM 不是 queue.size()

`pipe_t` 不通过跨线程读取底层容器 size 来计算容量。

它维护三个单调计数：

~~~text
_msgs_written
_msgs_read
_peers_msgs_read
~~~

writer 估计的 outstanding：

~~~text
_msgs_written - _peers_msgs_read
~~~

HWM 判断：

~~~cpp
const bool full =
  _hwm > 0
  && _msgs_written - _peers_msgs_read >= uint64_t (_hwm);
~~~

这里 `_peers_msgs_read` 只是 peer 最近一次上报的进度，可能落后于 reader 的真实 `_msgs_read`。

因此 writer 使用的是一个保守视图：

~~~text
peer may have consumed more
but writer only trusts last reported progress
~~~

这种设计避免 writer 为了拿到精确 queue size 而不断读取 reader 热状态。

## 为什么计数按完整 message，而不是每个 frame

ZeroMQ 支持 multipart message：

~~~text
frame 1 [more]
frame 2 [more]
frame 3 [last]
~~~

`write()`：

~~~cpp
const bool more = (msg_->flags () & msg_t::more) != 0;
const bool is_routing_id = msg_->is_routing_id ();

_out_pipe->write (*msg_, more);

if (!more && !is_routing_id)
    _msgs_written++;
~~~

只有最后一帧才把 `_msgs_written` 加一。

read side 同样：

~~~cpp
if (!(msg_->flags () & msg_t::more) && !msg_->is_routing_id ())
    _msgs_read++;
~~~

所以 HWM 的业务单位是“完整 message”，而不是底层 frame 数。

如果一条 multipart message 有 20 frame，不能因为写到第 10 frame 就把它当成 10 条独立业务消息参与 backpressure。

## write(false) 为什么必须保留消息 ownership

`pipe.hpp` 的契约写得很明确：

~~~text
write() returns false
-> message object retains ownership of its message buffer
~~~

这让上层可以继续决定：

~~~text
try pipe A
if full:
    try pipe B
~~~

或者：

~~~text
return EAGAIN
drop according to policy
retry later
~~~

失败并不意味着底层偷偷接管了 payload。

容量契约如果不同时定义 ownership，调用者就无法知道失败后还能不能安全重试或销毁消息。

## HWM 为什么会让 writer 停下来

假设：

~~~text
HWM = 1000
_msgs_written = 5000
_peers_msgs_read = 4000
~~~

那么：

~~~text
outstanding = 1000
~~~

达到 HWM，`check_write()` 失败，并设置：

~~~text
_out_active = false
~~~

后续 selector / scheduler 可以把这条 pipe 从 writable candidate 中排除，而不是让它不断：

~~~text
check
fail
check
fail
check
fail
~~~

backpressure 不只存在于计数器里，而是进入上层调度状态。

## LWM 为什么不是 HWM - 1

read side 每消费一定数量才上报：

~~~cpp
if (_lwm > 0 && _msgs_read % _lwm == 0)
    send_activate_write (_peer, _msgs_read);
~~~

`compute_lwm()` 中的源码注释明确解释为什么不能选极端值。

### LWM 太低

如果恢复阈值要求几乎把队列清空：

~~~text
writer fills queue
reader drains almost all
writer restarts very late
~~~

吞吐会形成明显空洞。

### LWM 太高

例如：

~~~text
LWM = HWM - 1
~~~

会出现 lock-step：

~~~text
queue full
reader consumes 1
wake writer
writer writes 1
queue full
sleep writer
reader consumes 1
wake writer
...
~~~

跨线程 command 与调度切换会暴涨。

固定源码采用：

~~~cpp
const int result = (hwm_ + 1) / 2;
~~~

即大致半个 HWM。

## HWM/LWM 实际上形成 hysteresis

容量状态不是：

~~~text
full -> consume one -> writable
~~~

而更像：

~~~text
writer active
   |
   | outstanding reaches HWM
   v
writer inactive
   |
   | reader consumes a batch
   | progress reaches LWM reporting boundary
   v
activate_write
   |
   v
writer active
~~~

暂停和恢复不是同一个瞬时边界，避免系统在“刚满/刚不满”附近高频抖动。

## activate_write 为什么传 msgs_read

peer 不只是发一个“可以写了”布尔通知。

command 携带：

~~~text
msgs_read
~~~

接收端：

~~~cpp
void pipe_t::process_activate_write (uint64_t msgs_read_)
{
    _peers_msgs_read = msgs_read_;

    if (!_out_active && _state == active) {
        _out_active = true;
        _sink->write_activated (this);
    }
}
~~~

先更新 capacity truth，再通知上层 sink：

~~~text
peer progress becomes visible
        ↓
out_active = true
        ↓
sink->write_activated(this)
~~~

notification 不是容量事实本身；`_peers_msgs_read` 才是后续 `check_hwm()` 所使用的事实。

这与 mailbox 的“data != wakeup”原则一致。

## activate_read 怎样从 ypipe passive 状态向上传播

`pipe_t::flush()`：

~~~cpp
if (_out_pipe && !_out_pipe->flush ())
    send_activate_read (_peer);
~~~

底层 ypipe 返回 `false`：

~~~text
reader sleeping/passive
~~~

pipe 将这个低层状态翻译成跨线程 command：

~~~text
ypipe publication
  |
  | reader passive
  v
send_activate_read(peer)
  |
  v
peer owner thread
  |
  v
process_activate_read()
  |
  v
_sink->read_activated(this)
~~~

所以 wakeup 是逐层传播的：

~~~text
atomic SPSC state
-> object command
-> socket/session scheduler event
~~~

## i_pipe_events 把 pipe 与上层策略解耦

`pipe_t` 不直接知道 ROUTER、DEALER、PUB、SUB 应该怎样重新调度。

它只向 `i_pipe_events` 报告：

~~~cpp
read_activated(pipe_t*)
write_activated(pipe_t*)
hiccuped(pipe_t*)
pipe_terminated(pipe_t*)
~~~

具体 socket pattern 再决定：

~~~text
readable pipe 放回哪种 scheduler
writable pipe 放回哪种 load balancer
terminated pipe 从哪些 containers 移除
~~~

底层 pipe 管机制，上层 socket pattern 管策略。

## rollback() 为什么只撤销 unfinished multipart

writer 可能已经写入：

~~~text
frame A [more]
frame B [more]
~~~

但完整消息还没有最后一帧。

`rollback()`：

~~~cpp
while (_out_pipe->unwrite (&msg)) {
    zmq_assert (msg.flags () & msg_t::more);
    const int rc = msg.close ();
    errno_assert (rc == 0);
}
~~~

只删除尚未形成 completed publication boundary 的 multipart 尾部。

已经完成、可被 peer 读取的消息不能靠 rollback 撤回。

因此 message atomicity 与 ypipe 的 `incomplete` publication boundary 是一致的。

## termination 为什么不能直接 delete pipe

pipe 两端可能位于不同线程，且底层 ypipe 中仍有 pending message。

如果某一侧直接：

~~~text
delete peer
free ypipe
~~~

另一侧可能正在：

~~~text
read
write
flush
process command
~~~

这会直接进入 use-after-free。

因此 `pipe_t` 关闭是异步协议，而不是析构函数调用。

## 六个 termination state 分别表示什么

固定源码状态：

~~~text
active
delimiter_received
waiting_for_delimiter
term_ack_sent
term_req_sent1
term_req_sent2
~~~

可以按两个维度理解：

~~~text
A. 谁先发起 terminate
B. pending inbound data 是否还要 drain
~~~

### active

普通数据传输状态。

### term_req_sent1

本端显式调用 `terminate()`，已经向 peer 发送 `pipe_term`，等待 ack。

### waiting_for_delimiter

peer 已要求关闭，但当前策略 `_delay=true`，所以本端还要把已经在途的数据消费完。

### delimiter_received

数据流中的 delimiter 已经先到，但 peer 的 `pipe_term` command 还没到。

### term_req_sent2

双方几乎同时发起 terminate。本端已经发过请求，又收到 peer 的请求；需要回复对方，同时仍等待自己的 ack。

### term_ack_sent

本端已经完成可见数据处理并向 peer 发 ack，进入最终销毁阶段。

## 为什么同时需要 delimiter 和 pipe_term command

它们走两条不同路径：

~~~text
delimiter
  -> data ypipe

pipe_term
  -> command/mailbox control plane
~~~

两条路径跨线程、跨队列传播，先后顺序不一定相同。

所以状态机必须处理：

~~~text
delimiter first
term command first
both sides terminate simultaneously
~~~

如果假设“控制命令一定比数据先到”或反过来，就会留下竞态。

这也是为什么状态里同时存在：

~~~text
delimiter_received
waiting_for_delimiter
~~~

## delay=true 的语义

peer 请求关闭时：

~~~cpp
if (_state == active) {
    if (_delay)
        _state = waiting_for_delimiter;
    else {
        _state = term_ack_sent;
        _out_pipe = NULL;
        send_pipe_term_ack (_peer);
    }
}
~~~

`_delay=true`：

~~~text
先 drain pending inbound message
再完成关闭
~~~

`_delay=false`：

~~~text
允许直接丢弃 pending path
尽快进入 ack
~~~

所以 linger/drain 语义最终会落实到 pipe 生命周期状态，而不是停留在 socket API 参数。

## 为什么 delimiter 可以无视 HWM

`terminate()` 中写 delimiter 时，源码明确说明不检查 watermarks。

原因可以直接用死锁序列看出来：

~~~text
queue full
-> delimiter 被 HWM 拒绝
-> peer 等 delimiter 才完成 drain
-> sender 等 peer ack
-> 双方都无法继续
~~~

终止协议中的控制标记不能被普通业务 backpressure 永久阻塞。

## process_pipe_term_ack() 为什么由每一侧回收自己的 inbound pipe

最终 ack 后：

~~~cpp
upipe_t *in_pipe = _in_pipe;
_in_pipe = NULL;

if (!_conflate && in_pipe) {
    msg_t msg;
    while (in_pipe->read (&msg))
        msg.close ();
}

delete in_pipe;
delete this;
~~~

源码注释明确说明：

~~~text
this endpoint deallocates its inbound pipe
peer deallocates its own inbound pipe
~~~

每条单向 ypipe 的最终销毁责任归属于它的 reader endpoint。

这再次体现 ownership topology：

~~~text
reader owns final reclamation of its inbound channel
~~~

## hiccup 为什么要替换整条 inbound ypipe

`hiccup()` 不只是“发一个通知”。

它会创建新的 inbound pipe：

~~~cpp
_in_pipe =
  _conflate
    ? static_cast<upipe_t *> (
        new ypipe_conflate_t<msg_t> ())
    : new ypipe_t<msg_t, message_pipe_granularity> ();
~~~

再通过 `send_hiccup()` 告诉 peer 替换对应 outbound pipe。

所以 hiccup 的语义是：

~~~text
disconnect old inbound stream
drop in-flight messages on old stream
install a fresh channel
notify peer to redirect its writer
~~~

它比“清空 queue”更彻底，因为底层 channel identity 本身发生变化。

## conflate 为什么是另一种容量语义

`pipepair()` 可以根据 `conflate` 选择：

~~~text
normal ypipe
or
ypipe_conflate
~~~

conflate 不再要求保留所有历史消息，而是只保留最新值。

这和 HWM 的“有界积压”是两种不同语义：

~~~text
HWM queue:
  preserve sequence until capacity boundary

conflate:
  newest value replaces older unread value
~~~

机器人状态流里，位姿、速度估计、最新诊断值经常更接近 conflate/latest-state；命令序列、事件、事务日志则不能这样处理。

## Backpressure 是分层策略，不等于 drop policy

`pipe_t::check_write()` 最终只回答：

~~~text
this pipe can accept another complete message?
~~~

至于返回 false 后怎么办，由上层 socket pattern 决定。

可能是：

~~~text
block
EAGAIN
drop
route to another pipe
temporarily remove pipe from scheduler
~~~

所以：

~~~text
pipe_t
  -> capacity mechanism

socket pattern
  -> overload policy
~~~

不能把 HWM 误解成“ZeroMQ 一到 HWM 就统一丢消息”。

## 三条状态线要分开看

`pipe_t` 内同时存在三类状态：

~~~text
Data availability
  _in_active

Capacity availability
  _out_active
  _msgs_written
  _peers_msgs_read
  _hwm / _lwm

Lifecycle
  _state
  _delay
  delimiter / term / ack
~~~

它们相互影响，但不是同一个状态机。

例如：

~~~text
_out_active = false
~~~

可能只是 HWM 满了，并不意味着 pipe 正在 terminate。

而：

~~~text
_state != active
~~~

即使 HWM 还有空间，也可能禁止新业务消息写入。

把“容量暂停”和“生命周期关闭”混在一个 bool 里，会很难正确处理恢复和销毁。

## 对机器人系统的映射

三个业务通道：

~~~text
control command
camera frame
debug log
~~~

不能只共享：

~~~text
std::queue<Message> global_queue
~~~

因为三者过载语义完全不同。

更合理的约束可能是：

~~~text
control:
  bounded
  stale command must not accumulate
  sequence semantics explicit

camera:
  small bounded queue
  old frame can be dropped
  data age more important than completeness

log:
  larger buffer
  batch-friendly
  lower scheduling priority
~~~

真正要设计的不只是 capacity 数字，而是：

~~~text
HWM:
  什么时候停止生产

LWM:
  什么时候恢复

ownership:
  满时 payload 属于谁

wake policy:
  恢复时通知谁

shutdown:
  pending data 是 drain 还是 drop
~~~

这五项合起来，才是一套完整 backpressure contract。
