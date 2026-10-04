# Pipe 与 HWM：Backpressure、Progress Feedback 与异步关闭状态机

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

`ypipe_t` 解决的是一个更底层的问题：

> 一个 logical writer 怎样把完整 item 发布给一个 logical reader。

但 ZeroMQ 的 socket runtime 还需要表达：

- 双向 endpoint；
- 多个 pending message；
- multipart 原子性；
- 容量上限；
- producer 暂停；
- consumer progress 回授；
- producer 恢复；
- socket pattern 调度集合；
- 跨线程 control command；
- linger / drain；
- 双端异步终止。

这一层由 `pipe_t` 承担。

所以 `pipe_t` 不应理解成：

~~~text
ypipe wrapper
~~~

而更接近：

> **一条跨线程 message channel 的 resource-local flow-control state machine。**

它同时把：

~~~text
data plane
control plane
capacity accounting
scheduler eligibility
lifecycle
~~~

压进一个稳定的 endpoint 对象里。

---

# 一、先建立完整对象图

## 1. pipepair() 创建的不是“一条双向队列”

源码：

~~~cpp
pipepair(
  object_t *parents_[2],
  pipe_t *pipes_[2],
  const int hwms_[2],
  const bool conflate_[2])
~~~

实际创建：

~~~text
2 × pipe_t endpoint
2 × unidirectional queue
~~~

关系：

~~~text
                 direction 0 → 1
      +----------------------------------+
      |                                  |
      |              upipe1              |
      v                                  |
   pipe[0]                            pipe[1]
      |                                  ^
      |              upipe2              |
      +----------------------------------+
                 direction 1 → 0
~~~

即：

~~~text
pipe[0]._in_pipe  = upipe1
pipe[1]._out_pipe = upipe1

pipe[1]._in_pipe  = upipe2
pipe[0]._out_pipe = upipe2
~~~

双向通信不是靠一个“双向并发容器”实现，而是：

~~~text
two one-way ownership channels
~~~

组合出来的。

---

## 2. 为什么两个 endpoint 要分开

因为两端可以属于不同 execution domain：

~~~text
application/socket owner thread
        |
        +-- pipe endpoint A
                ||
                || two ypipes
                ||
        +-- pipe endpoint B
                |
          session / I/O owner thread
~~~

每一端都只直接修改自己的：

- `_in_active`；
- `_out_active`；
- `_msgs_read`；
- `_msgs_written`；
- `_peers_msgs_read`；
- `_state`；
- `_sink`。

需要改变 peer 状态时，不跨线程直接写 peer 字段。

而是：

~~~text
send_activate_read(peer)
send_activate_write(peer, msgs_read)
send_pipe_term(peer)
send_pipe_term_ack(peer)
send_hiccup(peer, ...)
send_pipe_hwm(peer, ...)
~~~

通过 command/mailbox 返回 peer owner thread。

---

## 3. Pipe 内部同时存在 Data Plane 与 Control Plane

数据面：

~~~text
msg_t
  ↓
ypipe
  ↓
peer pipe
~~~

控制面：

~~~text
command_t
  ↓
mailbox
  ↓
peer owner thread
  ↓
peer pipe::process_*
~~~

这两个面有不同职责。

数据面传：

~~~text
business payload
delimiter
routing frames
~~~

控制面传：

~~~text
activation
termination
HWM update
hiccup
stats
~~~

---

# 二、真正的背压不是“queue.size() >= HWM”

## 4. 最朴素的容量检查为什么很诱人

很多人第一反应：

~~~cpp
if (queue.size() >= hwm)
    return false;
~~~

但跨线程 SPSC queue 中，这个思路有几个问题：

- writer 要读取 reader 正在热更新的 size；
- size 可能需要额外共享原子状态；
- cache line 在两核间频繁 bouncing；
- multipart frame 数不等于业务 message 数；
- queue implementation 可能并没有便宜且一致的 size；
- consumer progress 不需要每条 message 都实时同步给 producer。

libzmq 选择了另一种模型。

---

## 5. 三个单调计数

每个 pipe endpoint维护：

~~~text
_msgs_written
_msgs_read
_peers_msgs_read
~~~

语义：

~~~text
_msgs_written
= 本端成功提交了多少个完整 outbound message

_msgs_read
= 本端已经消费了多少个完整 inbound message

_peers_msgs_read
= 最近一次从 peer 收到的：
  peer 已经消费多少 outbound message
~~~

---

## 6. Writer 估计 Outstanding

writer 不直接看 queue size。

而是：

\[
Q_{\text{estimated}}
=
\texttt{\_msgs\_written}
-
\texttt{\_peers\_msgs\_read}
\]

源码：

~~~cpp
const bool full =
  _hwm > 0
  && _msgs_written
       - _peers_msgs_read
     >= uint64_t(_hwm);
~~~

---

## 7. 这不是精确 Queue Occupancy

因为：

~~~text
_peers_msgs_read
~~~

只是最近一次收到的 consumer progress。

真实 peer 可能已经：

~~~text
读了更多
~~~

但还没有发送下一条 `activate_write`。

所以：

\[
Q_{\text{estimated}}
\ge
Q_{\text{actual}}
\]

通常是一个保守上界或至少不比已知进度更乐观。

---

## 8. 为什么 Conservative View 很合理

容量控制最危险的是：

~~~text
低估 backlog
~~~

因为会继续放量。

高估 backlog 的后果只是：

~~~text
writer 多停一会儿
~~~

所以跨线程背压宁愿：

~~~text
slightly conservative
~~~

也不必追求：

~~~text
every-message exact shared counter
~~~

---

# 三、为什么 HWM 按“完整 Message”计数

## 9. ZeroMQ 的 Data Unit 不是单个 Frame

multipart：

~~~text
frame 0 [more]
frame 1 [more]
frame 2 [last]
~~~

逻辑上是一条 message。

如果 HWM 按 frame：

~~~text
一个 20-frame multipart
≈ 20 个普通 message
~~~

会改变 socket pattern 的业务容量语义。

---

## 10. write() 只在完整 Message 末尾增加 `_msgs_written`

源码：

~~~cpp
const bool more =
  (msg_->flags() & msg_t::more) != 0;

const bool is_routing_id =
  msg_->is_routing_id();

_out_pipe->write(*msg_, more);

if (!more && !is_routing_id)
    _msgs_written++;
~~~

所以中间帧：

~~~text
不计数
~~~

最后一帧：

~~~text
+1 message
~~~

---

## 11. read() 同样只在最后一帧增加 `_msgs_read`

~~~cpp
if (!(msg_->flags() & msg_t::more)
    && !msg_->is_routing_id())
{
    _msgs_read++;
}
~~~

因此两端计数单位一致。

---

## 12. Routing ID 也不进入普通消息计数

routing metadata：

~~~text
不是业务 message
~~~

所以被排除。

否则 ROUTER/DEALER 等内部 routing frame 会污染 backpressure accounting。

---

# 四、HWM 的第一步不是“拒绝 write”，而是改变 Pipe State

## 13. check_write()

~~~cpp
if (!_out_active
    || _state != active)
    return false;

if (!check_hwm())
{
    _out_active = false;
    return false;
}

return true;
~~~

当 HWM 命中：

~~~text
_out_active = false
~~~

这是关键。

---

## 14. 为什么必须把 Full 变成状态

如果每次上层 scheduler 都继续尝试：

~~~text
pipe.write()
→ HWM full
→ false

pipe.write()
→ HWM full
→ false
...
~~~

就变成：

~~~text
busy retry
~~~

浪费 CPU。

---

## 15. Backpressure 要进入 Scheduler Eligibility

`_out_active=false` 以后，

上层 load balancer / distributor 会把这条 pipe 从 active set 移出。

所以真正传播链是：

~~~text
capacity full
    ↓
pipe out_active = false
    ↓
socket scheduler sees write failure
    ↓
pipe leaves active candidate set
~~~

不是只返回一个局部 `false`。

---

# 五、Load Balancer 怎样消费 `_out_active`

## 16. lb_t::has_out()

~~~cpp
while (_active > 0)
{
    if (_pipes[_current]
          ->check_write())
        return true;

    _active--;

    _pipes.swap(
      _current,
      _active);

    ...
}
~~~

如果某 pipe 已 full：

~~~text
check_write() → false
~~~

它被移动到 inactive segment。

---

## 17. Active Set 是 O(1) Partition

`lb_t` 不需要：

~~~text
std::set<writable_pipe>
~~~

它维护：

~~~text
_pipes[0 .. _active-1]
= active

_pipes[_active .. end)
= inactive
~~~

通过 swap 调整 membership。

---

## 18. Backpressure 因此直接改变数据结构分区

这很重要：

> **容量状态最终必须影响调度数据结构，而不是只存在于一个 counter 里。**

---

# 六、Reader Progress 怎样反馈给 Writer

## 19. Consumer 每读一条完整 Message

~~~text
_msgs_read++
~~~

但不是每次都 command peer。

---

## 20. 只有达到 LWM Reporting Boundary 才回授

源码：

~~~cpp
if (_lwm > 0
    && _msgs_read % _lwm == 0)
{
    send_activate_write(
      _peer,
      _msgs_read);
}
~~~

所以：

~~~text
consumer progress
~~~

是批量发送的。

---

## 21. `activate_write` 不只是一个 Bool Wakeup

command 携带：

~~~text
msgs_read
~~~

也就是容量事实。

peer 收到：

~~~cpp
void pipe_t::process_activate_write(
  uint64_t msgs_read)
{
    _peers_msgs_read =
      msgs_read;

    if (!_out_active
        && _state == active)
    {
        _out_active = true;
        _sink->write_activated(this);
    }
}
~~~

顺序很关键：

~~~text
1. update peer progress truth
2. mark output active
3. notify scheduler
~~~

---

## 22. 为什么不能只发 `writable=true`

因为 writer 后续 `check_hwm()` 仍然需要知道：

~~~text
到底消费到了哪里
~~~

如果只发：

~~~text
wake
~~~

却不更新：

~~~text
_peers_msgs_read
~~~

下一次 `check_hwm()` 仍然会认为 full。

---

# 七、LWM 本质上是 Progress Feedback Batch Size

## 23. compute_lwm()

~~~cpp
const int result =
  (hwm + 1) / 2;
~~~

大致：

\[
LWM \approx \frac{HWM}{2}
\]

---

## 24. 为什么不是 1

如果：

~~~text
每读 1 条
→ activate_write
~~~

那 consumer progress 反馈会变成：

~~~text
1 message
≈
1 cross-thread command
~~~

控制面开销过大。

---

## 25. 为什么不是 HWM

如果 reader 必须：

~~~text
整批清空
~~~

才通知 writer：

~~~text
producer idle 时间太长
~~~

吞吐形成明显空洞。

---

## 26. 为什么不是 HWM - 1

源码注释直接描述了 lock-step：

~~~text
full
→ reader consumes one
→ wake writer
→ writer writes one
→ full
→ sleep
→ reader consumes one
→ wake
...
~~~

结果：

~~~text
one message
≈
one thread handoff
~~~

同样很差。

---

# 八、HWM/LWM 形成的是 Hysteresis，而不是单阈值

## 27. 状态机

~~~text
WRITABLE
   |
   | estimated backlog reaches HWM
   v
BLOCKED
   |
   | reader reports batch progress
   v
REACTIVATED
~~~

暂停和恢复不是在同一个瞬时边界反复切换。

---

## 28. 为什么 Hysteresis 对 Runtime 很重要

没有滞回：

~~~text
99
100 full
99 writable
100 full
99 writable
~~~

会导致：

- command storm；
- scheduler membership churn；
- cache line churn；
- thread handoff；
- latency jitter。

---

## 29. 与控制系统里的滞回完全同构

例如温控：

~~~text
> 30°C 开
< 28°C 关
~~~

而不是：

~~~text
29.999 / 30.001
不停抖
~~~

背压也是一种离散控制系统。

---

# 九、为什么 `_peers_msgs_read` 是 Owner-local 普通字段

## 30. Peer 不跨线程直接写它

peer：

~~~text
send_activate_write(...)
~~~

目标 owner thread：

~~~text
process_activate_write(...)
→ _peers_msgs_read = msgs_read
~~~

所以：

~~~text
_peers_msgs_read
~~~

只在本 endpoint owner thread 中更新和读取。

---

## 31. 不需要 Atomic Shared Counter

这是 owner-thread 架构的直接收益：

~~~text
peer progress transfer
→ command message

local state
→ ordinary uint64_t
~~~

而不是：

~~~text
shared atomic<size_t> queue_size
~~~

---

# 十、容量 Truth 与 Wakeup Hint 再次被分开

## 32. `msgs_read` 是 Truth

~~~text
peer consumed N messages
~~~

这是后续 HWM 判断的输入。

---

## 33. `write_activated()` 是 Scheduling Hint

~~~text
这条 pipe 值得重新放回 scheduler active set
~~~

不是容量事实本身。

---

## 34. 顺序必须是 Truth-first

~~~text
update _peers_msgs_read
        ↓
_out_active = true
        ↓
sink->write_activated()
~~~

否则上层一收到 activation，立刻尝试 write，

但容量状态还没更新：

~~~text
又被判 full
~~~

---

# 十一、read-side Active 与 write-side Active 是两种不同状态

## 35. `_in_active`

表示：

~~~text
当前继续尝试 read 是否可能有进展
~~~

---

## 36. `_out_active`

表示：

~~~text
当前继续尝试 write 是否可能有进展
~~~

---

## 37. 它们不是“连接 alive”

即使：

~~~text
_in_active=false
~~~

pipe 仍然存在。

只是：

~~~text
当前 queue empty
等 peer 下一次 publish
~~~

---

# 十二、Read Empty 怎样变成跨线程 Activation

## 38. check_read()

~~~cpp
if (!_in_pipe->check_read())
{
    _in_active = false;
    return false;
}
~~~

`ypipe::check_read()` 在空时：

~~~text
reader enters passive protocol
~~~

---

## 39. Writer flush()

~~~cpp
if (_out_pipe
    && !_out_pipe->flush())
{
    send_activate_read(_peer);
}
~~~

`flush()==false` 表示：

~~~text
reader passive
~~~

于是发送 control command。

---

## 40. Peer Owner Thread

~~~cpp
void pipe_t::process_activate_read()
{
    if (!_in_active
        && valid_state)
    {
        _in_active = true;
        _sink->read_activated(this);
    }
}
~~~

---

## 41. 完整 Read Wake Chain

~~~text
writer publishes message
        ↓
ypipe detects reader passive
        ↓
send_activate_read(peer)
        ↓
peer mailbox
        ↓
peer owner thread
        ↓
process_activate_read()
        ↓
_in_active = true
        ↓
socket/session scheduler
~~~

这是把底层 SPSC wakeup 提升成高层 scheduler activation。

---

# 十三、Pipe 是低层 Queue 与高层 Scheduler 的桥

单条 `pipe_t` 只回答“这条连接当前能不能继续进展”；多条 Pipe 如何被放进 active prefix、如何 O(1) 失活/恢复、multipart 如何冻结 destination / participant set，则由 [FQ / LB / DIST：Active Prefix、Multipart 原子性与消息调度器](fq-lb-dist-schedulers.md) 负责。

当 socket pattern 不是“任选一个可写 peer”，而是应用用 routing-id 指定目标时，HWM 就只是第二层判断：先从 routing registry 找到目标 Pipe，再判断该 Pipe 是否有容量。完整的显式路由、mandatory 错误语义与 handover 生命周期见 [DEALER / ROUTER：显式路由、Routing-ID 生命周期与 Multipart 粘性](dealer-router-routing.md)。

PUB/SUB 又是第三种组合：MTrie 先决定语义上哪些 Pipe 应接收，DIST 再把这些 Pipe 与当前 HWM/eligible 状态相交。慢订阅者只是暂时退出调度资格，并不会因此丢失订阅语义；见 [PUB / SUB：订阅 Trie、反向控制面与 Distributor](pubsub-trie-distributor.md)。

## 42. ypipe 只知道

~~~text
data / no data
reader active / passive
~~~

---

## 43. socket pattern scheduler 只想知道

~~~text
这个 pipe 是否可调度
~~~

---

## 44. pipe_t 负责翻译

~~~text
low-level queue condition
→ high-level runtime event
~~~

这就是 control object 的价值。

---

# 十四、i_pipe_events 把 Mechanism 与 Policy 分开

## 45. Pipe 只发四类事件

~~~cpp
read_activated(pipe_t*)
write_activated(pipe_t*)
hiccuped(pipe_t*)
pipe_terminated(pipe_t*)
~~~

---

## 46. Pipe 不知道 DEALER 怎样调度

`dealer_t`：

~~~cpp
xread_activated(pipe)
{
    _fq.activated(pipe);
}

xwrite_activated(pipe)
{
    _lb.activated(pipe);
}
~~~

所以：

~~~text
pipe
→ mechanism

fq / lb / dist
→ scheduling policy
~~~

---

# 十五、Fair Queue 怎样消费 Read Activation

## 47. fq_t 也维护 Active Prefix

~~~text
_pipes[0 .. _active)
= active readable candidates
~~~

读空：

~~~text
pipe->read() false
~~~

该 pipe 被移出 active prefix。

---

## 48. 新数据到来

~~~text
pipe::process_activate_read
→ sink->read_activated
→ fq_t::activated
~~~

pipe 重新进入 active prefix。

---

## 49. 所以 Backpressure 与 Availability 都是 Membership Transition

写侧：

~~~text
full
→ leave LB active set

peer progress
→ re-enter LB active set
~~~

读侧：

~~~text
empty
→ leave FQ active set

new publication
→ re-enter FQ active set
~~~

非常对称。

---

# 十六、Distributor 的背压语义又不一样

## 50. dist_t 有三段区间

~~~text
[matching)
[active)
[eligible)
[all pipes)
~~~

代表：

- 当前订阅/目标匹配；
- 当前 active；
- 当前 eligible；
- inactive。

---

## 51. Pipe 写失败时 Distributor 会降级 Membership

~~~cpp
if (!pipe->write(msg))
{
    matching--;
    active--;
    eligible--;
    ...
}
~~~

也就是说 HWM 直接影响 fan-out eligibility。

---

## 52. `activated()` 再把 Pipe 提升回来

writer progress recovery：

~~~text
activate_write
→ sink write_activated
→ dist.activated(pipe)
~~~

然后 pipe 重新变 eligible / active。

---

# 十七、Backpressure 是“调度资格”而不仅是容量

## 53. 这条原则非常重要

很多系统把 backpressure 理解成：

~~~text
send() returns false
~~~

但成熟 runtime 会进一步做到：

~~~text
future scheduling excludes blocked resource
~~~

否则上层仍不断撞墙。

---

## 54. 机器人 Runtime 同样适用

例如 CAN TX queue：

~~~text
queue high
→ channel not eligible for normal producer scheduling

driver drains
→ progress event
→ channel re-enters eligible set
~~~

比每个控制模块不断 retry 更稳定。

---

# 十八、为什么 HWM 不能在 Multipart 中间破坏原子性

## 55. lb_t 的 `_more`

一旦第一帧已经选定某条 pipe：

~~~text
multipart remaining frames
~~~

必须继续走同一 pipe。

否则：

~~~text
frame 0 → peer A
frame 1 → peer B
~~~

消息就损坏了。

---

## 56. `_more` 期间 `has_out()` 直接 true

~~~cpp
if (_more)
    return true;
~~~

原因不是说容量无限。

而是：

> 已经开始一条 multipart 后，runtime 必须维护 message atomicity contract。

---

## 57. 如果中途 write 失败

`lb_t::sendpipe()`：

~~~text
rollback already-written unfinished frames
~~~

并返回 `EAGAIN` / special failure path。

---

## 58. rollback() 只撤销尚未 Flush 的 Incomplete Tail

~~~cpp
while (_out_pipe->unwrite(&msg))
{
    assert(msg.more);
    msg.close();
}
~~~

已经完成 publication 的完整 message：

~~~text
不能撤回
~~~

---

# 十九、ypipe 的 Incomplete Boundary 正好支撑 Multipart Atomicity

## 59. ypipe write(value, incomplete)

中间帧：

~~~text
incomplete=true
→ do not advance _f
~~~

最后一帧：

~~~text
incomplete=false
→ advance flush boundary
~~~

---

## 60. 所以 peer 看不到半条 multipart

即使 writer 已经写了：

~~~text
frame A
frame B
~~~

只要还没最后 frame：

~~~text
reader-side publication boundary未推进
~~~

---

## 61. HWM 又按完整 Message 计数

于是两个层次一致：

~~~text
publication atomicity
= complete multipart

capacity accounting unit
= complete multipart
~~~

这是很干净的抽象对齐。

---

# 二十、write(false) 的 Ownership Contract

## 62. Pipe API 明确说明

如果：

~~~text
write() returns false
~~~

则：

~~~text
message object retains buffer ownership
~~~

---

## 63. 为什么这非常重要

上层可能：

~~~text
try pipe A
A full
try pipe B
~~~

或者：

~~~text
return EAGAIN
~~~

或者：

~~~text
drop according to policy
~~~

如果失败后 ownership 不明确：

~~~text
retry / free / forward
~~~

都会有 double-free 或 leak 风险。

---

# 二十一、容量 API 必须同时定义 Ownership Transfer

## 64. 一个 Send API 至少要回答两个问题

第一：

~~~text
能不能接收？
~~~

第二：

~~~text
失败后谁拥有 payload？
~~~

只回答第一个是不完整接口。

---

# 二十二、Conflate 是完全不同的 Queue 语义

## 65. ZMQ_CONFLATE

options 注释明确：

~~~text
discard all incoming messages but the last one
cannot receive multipart
ignores HWM
~~~

所以它不是普通 HWM queue 的一个“小优化”。

---

## 66. Pipepair 会改用 ypipe_conflate_t

~~~text
normal:
ypipe_t<msg_t>

conflate:
ypipe_conflate_t<msg_t>
~~~

底层是：

~~~text
dbuffer
~~~

而不是普通 queue。

---

## 67. Conflate 的容量模型是 Latest-value

~~~text
old message
→ overwritten / discarded

latest message
→ retained
~~~

所以 backlog 不再是：

~~~text
0..HWM
~~~

而是接近：

~~~text
one latest state
~~~

---

## 68. 这非常适合状态型数据

例如：

- 最新机器人位姿；
- 最新 teleop command；
- 最新 UI state；
- 最新传感器摘要。

如果业务只关心 latest：

~~~text
queueing every intermediate value
~~~

反而没有意义。

---

## 69. 但不适合事件型数据

例如：

- motor fault event；
- financial transaction；
- discrete task command；
- safety transition。

这些不能：

~~~text
只保留最后一条
~~~

---

# 二十三、Conflate 仍然需要 Reader Wake Protocol

## 70. ypipe_conflate_t 有 `reader_awake`

因为即使不保留完整 backlog，

仍然必须解决：

~~~text
reader sleeping
+
new latest value arrived
~~~

---

## 71. flush()

~~~cpp
bool flush()
{
    return reader_awake;
}
~~~

如果 reader asleep：

~~~text
false
→ pipe sends activate_read
~~~

所以：

> 队列容量语义可以变，但 wakeup correctness 仍然必须保留。

---

# 二十四、inproc HWM 为什么还要 Boost

## 72. inproc 与 Network Transport 不同

inproc 两端都直接是 libzmq socket-side runtime。

创建 pipe 后，双方的：

~~~text
SNDHWM
RCVHWM
~~~

需要共同决定有效容量。

---

## 73. Context 会设置 HWM Boost

例如：

~~~cpp
connect_pipe->set_hwms_boost(
    bind_options.sndhwm,
    bind_options.rcvhwm);

bind_pipe->set_hwms_boost(
    connect_options.sndhwm,
    connect_options.rcvhwm);
~~~

再调用：

~~~text
set_hwms(...)
~~~

---

## 74. 有效 HWM 可以组合两端配置

设计意图：

~~~text
inproc total buffering
~~~

来自：

~~~text
sender-side allowance
+
receiver-side allowance
~~~

而不是只看一端配置。

---

## 75. 任一侧“无限”会改变组合语义

`set_hwms()`：

~~~text
if base hwm <= 0
or corresponding boost == 0
→ effective hwm = 0
~~~

在 libzmq 中：

~~~text
HWM <= 0
→ infinite
~~~

因此组合逻辑会保留“无限容量”语义。

---

# 二十五、HWM 更新本身也是 Control Command

## 76. send_hwms_to_peer()

~~~cpp
if (_state == active)
    send_pipe_hwm(
      _peer,
      inhwm,
      outhwm);
~~~

peer owner thread：

~~~text
process_pipe_hwm
→ set_hwms
~~~

同样不跨线程直接写 peer threshold。

---

# 二十六、Backpressure 不应该让 Producer 忙等

## 77. 错误设计

~~~cpp
while (!pipe.write(msg))
{
}
~~~

如果 peer 很慢：

~~~text
100% CPU
~~~

---

## 78. libzmq 的设计

~~~text
full
→ out_active=false
→ remove from scheduler active set

peer consumes batch
→ activate_write command
→ restore active set
~~~

本质是：

~~~text
event-driven backpressure
~~~

---

# 二十七、这和“Credit-based Flow Control”有什么关系

## 79. 可以把 HWM 看成初始 Credit

若：

~~~text
HWM = 100
~~~

writer 最多领先 consumer：

~~~text
100 complete messages
~~~

---

## 80. Reader Progress 相当于归还 Credit

consumer：

~~~text
msgs_read += Δ
~~~

通过 command 告诉 writer：

~~~text
新的已消费总数
~~~

等价于释放容量。

---

## 81. 但 libzmq 传的是 Absolute Progress，不是 Δ

它发送：

~~~text
msgs_read total
~~~

而不是：

~~~text
consumed 37
~~~

---

## 82. 为什么 Absolute Counter 更 Robust

如果用 delta：

~~~text
duplicate command
lost update
reordering
~~~

更难处理。

单调累计值：

~~~text
peer has consumed up to N
~~~

更容易建立 idempotent-ish progress view。

这里 command ordering 本身也由 owner/mailbox路径约束。

---

# 二十八、Progress Counter 与 Business Sequence Number 不同

## 83. `_msgs_read` 只是容量 accounting

它不是：

- message ID；
- reliability sequence；
- network sequence；
- application sequence。

它只回答：

~~~text
累计完成了多少个容量单位
~~~

---

# 二十九、为什么不每次读取都触发 `write_activated`

## 84. 因为 Activation 是昂贵 Control Transition

可能涉及：

~~~text
command enqueue
mailbox wake
owner thread dispatch
scheduler array mutation
~~~

所以要批量化。

---

## 85. 正确目标不是“最实时”

而是：

~~~text
足够及时恢复吞吐
+
不要产生过量调度开销
~~~

这正是 LWM 的意义。

---

# 三十、HWM 会把慢 Consumer 的压力向上传播

## 86. 完整链

~~~text
consumer slow
    ↓
_msgs_read grows slowly
    ↓
activate_write feedback sparse
    ↓
writer estimate backlog rises
    ↓
HWM reached
    ↓
_out_active=false
    ↓
LB/Dist excludes pipe
    ↓
socket send path sees EAGAIN/drop/policy
~~~

---

## 87. Backpressure 是 End-to-End Propagation

如果只在最底层 queue full：

~~~text
上层继续无限生产
~~~

系统仍然会在别处积压。

真正有效的 backpressure 必须：

~~~text
向上改变 producer scheduling / admission
~~~

---

# 三十一、Pipe 只提供机制，上层决定 Policy

## 88. HWM 命中后不同 Socket Pattern 行为不同

可能：

- EAGAIN；
- block；
- skip该 peer；
- drop；
- fan-out剔除；
- wait重新激活。

pipe 不应该决定全部策略。

---

## 89. Mechanism / Policy Separation

~~~text
pipe
→ capacity truth
→ activation events

lb/fq/dist/socket
→ selection / fairness / drop semantics
~~~

这样相同 pipe 可以服务不同 socket pattern。

---

# 三十二、Why Read Side Needs `_in_active`

## 90. Empty Queue 之后继续读没有意义

~~~text
check_read
→ no item
→ _in_active=false
~~~

上层 FQ 可以移除该 pipe。

---

## 91. 新数据时才重新激活

~~~text
peer flush detects passive
→ activate_read
~~~

所以读侧也避免 busy polling。

---

# 三十三、Backpressure 与 Wakeup 其实是镜像问题

写侧：

~~~text
capacity unavailable
→ deactivate writer
→ reader progress wakes writer
~~~

读侧：

~~~text
data unavailable
→ deactivate reader
→ writer publication wakes reader
~~~

两者都是：

> **资源不可进展时退出 active set；条件变化时通过事件重新加入。**

---

# 三十四、这就是 Event-driven Runtime 的基本形态

不是：

~~~text
不断尝试直到成功
~~~

而是：

~~~text
try
→ cannot progress
→ deactivate
→ await condition change
→ reactivate
~~~

---

# 三十五、Termination 为什么也必须进入 Pipe State Machine

## 92. 跨线程双端 Pipe 不能直接 delete

因为另一端可能仍在：

- read；
- write；
- flush；
- mailbox command；
- scheduler active set；
- in-flight multipart。

所以：

~~~text
close
~~~

必须是协议。

---

# 三十六、六个 Termination State

源码：

~~~text
active
delimiter_received
waiting_for_delimiter
term_ack_sent
term_req_sent1
term_req_sent2
~~~

---

## 93. active

普通数据传输。

---

## 94. delimiter_received

数据面 delimiter 先到，

control-plane `pipe_term` 还没到。

---

## 95. waiting_for_delimiter

control-plane `pipe_term` 已到，

但 `_delay=true`，

还要 drain data plane。

---

## 96. term_req_sent1

本端主动 terminate，

已经发送 term request，

等待 ack。

---

## 97. term_req_sent2

双方并发 terminate：

~~~text
我已经发 request
又收到你的 request
~~~

---

## 98. term_ack_sent

本端已经完成自己的关闭应答，

等待最终 peer ack / reclaim path。

---

# 三十七、为什么同时要 Data Delimiter 和 Control Term Command

## 99. 它们走不同 channel

delimiter：

~~~text
data ypipe
~~~

term command：

~~~text
mailbox command
~~~

---

## 100. 不同 Channel 就没有全局天然顺序

可能：

~~~text
delimiter first
~~~

也可能：

~~~text
term command first
~~~

还可能：

~~~text
two ends terminate concurrently
~~~

---

## 101. 状态机就是为“不同通道到达顺序”准备的

这是系统设计非常常见的问题。

一旦：

~~~text
data path
control path
~~~

分离，

就必须显式处理：

~~~text
cross-channel ordering
~~~

不能假设某一路总是先到。

---

# 三十八、Delay=true 的真实语义

## 102. Peer 请求 terminate

若：

~~~text
_delay=true
~~~

本端：

~~~text
state = waiting_for_delimiter
~~~

继续允许 read。

---

## 103. 等到 Delimiter 被读到

~~~text
process_delimiter()
→ rollback outbound incomplete tail
→ send term ack
→ term_ack_sent
~~~

说明：

~~~text
已发布在途数据先被处理
~~~

---

## 104. Delay=false

可以直接：

~~~text
drop pending path
→ send ack
~~~

更接近：

~~~text
abortive close
~~~

---

# 三十九、为什么 Delimiter 不受 HWM 限制

## 105. terminate()

源码明确：

~~~text
watermarks are not checked
~~~

因为 delimiter 是：

~~~text
protocol progress token
~~~

不是普通业务流量。

---

## 106. 如果 Delimiter 也被 Backpressure 阻塞

可能：

~~~text
queue full
→ delimiter cannot enter
→ peer waits delimiter to drain-close
→ sender waits peer ack
→ deadlock
~~~

---

## 107. 一般原则

> **终止、取消、释放 credit 等控制性进度消息，不能被普通业务背压永久阻塞。**

---

# 四十、为什么 Stop/Close Control 需要独立 Priority Plane

如果一个系统让：

~~~text
normal data queue full
~~~

同时也阻止：

~~~text
STOP
CANCEL
CREDIT_RETURN
SHUTDOWN
~~~

它就可能失去自我恢复能力。

---

# 四十一、process_pipe_term_ack() 中的 Ownership 非常明确

## 108. 每一侧删除自己的 Inbound Queue

~~~text
my _in_pipe
=
peer's _out_pipe
~~~

最终：

~~~text
我删除我的 inbound queue object

peer 删除它自己的 inbound queue object
~~~

避免两边争夺同一 queue 的 delete ownership。

---

## 109. Ordinary ypipe 中未读 msg_t 需要显式 close

因为 `msg_t`：

~~~text
不是依赖普通 C++ destructor 自动回收全部语义
~~~

所以：

~~~cpp
while (in_pipe->read(&msg))
    msg.close();
~~~

再 delete queue。

---

# 四十二、为什么 Conflate Cleanup 又不同

源码：

~~~cpp
if (!_conflate && in_pipe)
{
    ...
}
~~~

因为 `ypipe_conflate_t` / `dbuffer` 有不同的 storage ownership / destruction语义。

所以 cleanup 不能简单把所有 queue backend 当同一种。

---

# 四十三、Hiccup 是“替换通道”而不是普通 Wakeup

## 110. hiccup()

本端创建：

~~~text
new inbound pipe
~~~

然后 command peer：

~~~text
send_hiccup(peer, new_pipe)
~~~

---

## 111. Peer process_hiccup()

它先：

~~~text
flush old outpipe
drain unread output-side storage
adjust _msgs_written downward
delete old outpipe
install new outpipe
_out_active=true
~~~

---

## 112. 为什么 `_msgs_written--`

因为旧 outpipe 里仍残留、随后被丢弃的完整消息：

~~~text
不能继续算作 outstanding
~~~

否则 HWM accounting 会永久偏高。

---

# 四十四、Channel Replacement 必须修正 Capacity Accounting

任何：

~~~text
drop queued data
replace queue
reconnect
reset transport
~~~

操作都必须问：

~~~text
旧 backlog counter 怎么修正？
~~~

否则下一代 channel 会继承幽灵 debt。

---

# 四十五、Pipe Stats 也复用同一组 Counter

## 113. send_stats_to_peer()

~~~text
outbound queue estimate
=
_msgs_written - _peers_msgs_read
~~~

---

## 114. 为什么监控应该复用控制状态

如果 metrics 再单独维护一套：

~~~text
queue_depth_metric
~~~

很容易与真正 admission control 状态漂移。

更好的方式：

~~~text
observability reads same accounting model
~~~

---

# 四十六、Counter Overflow 为什么通常不成为实际问题

`uint64_t`：

~~~text
wraparound horizon
~~~

极大。

但设计上仍依赖：

~~~text
monotonic difference within practical lifetime
~~~

如果把类似模型做在 32-bit counter 上，高吞吐长期运行就必须专门处理 wrap。

---

# 四十七、为什么不用“剩余 Credit”字段

另一种设计：

~~~text
credits--
on write

credits += delta
on peer consume
~~~

也可以。

libzmq 选择：

~~~text
monotonic sent/read counters
~~~

优势：

~~~text
容易做差
容易报告绝对 progress
调试更直观
~~~

---

# 四十八、这和 TCP Window 有什么相似

概念上都在限制：

~~~text
sender can be ahead of receiver by how much
~~~

但 libzmq HWM 是：

~~~text
user-space message runtime capacity
~~~

不是：

~~~text
transport byte window
~~~

不要混为一层。

---

# 四十九、多层 Backpressure 可以同时存在

完整网络发送链可能有：

~~~text
application queue limit
↓
ZeroMQ pipe HWM
↓
engine output buffer
↓
kernel socket sndbuf
↓
TCP congestion / receive window
~~~

任何一层都可能成为瓶颈。

---

# 五十、只看 Kernel Socket Buffer 不够

即使 kernel 还能写，

ZeroMQ 也可能：

~~~text
pipe HWM full
~~~

因为它限制的是：

~~~text
message-runtime backlog
~~~

而不是 kernel bytes。

---

# 五十一、只看 Pipe HWM 也不等于“对端应用跟上了”

peer `_msgs_read` 表示：

~~~text
peer pipe consumer 已经读取
~~~

并不自动等于：

~~~text
最终远程业务逻辑已经处理
~~~

尤其跨 network engine 时还有更多层。

---

# 五十二、Backpressure 语义必须标明是哪一层

例如：

~~~text
queue accepted
transport accepted
remote received
remote application processed
~~~

是四种不同 guarantee。

---

# 五十三、HWM 不是 Reliability ACK

`activate_write(msgs_read)`：

~~~text
peer local runtime consumed queue items
~~~

不是：

~~~text
network delivery ACK
~~~

也不是：

~~~text
application semantic ACK
~~~

---

# 五十四、这对机器人控制命令尤其重要

一个 motor command：

~~~text
进入 middleware queue
~~~

不等于：

~~~text
motor controller executed it
~~~

如果需要动作确认，

必须另有：

~~~text
sequence
ack
deadline
state feedback
~~~

---

# 五十五、为什么 Backpressure 对控制命令不能一概照搬

高频 setpoint：

~~~text
latest value often matters more
~~~

可能适合：

~~~text
conflate/latest-value
~~~

离散安全命令：

~~~text
every event matters
~~~

可能需要：

~~~text
bounded FIFO + ACK
~~~

---

# 五十六、Pipe 其实提供了三类流控范式

普通 HWM：

~~~text
bounded backlog
~~~

conflate：

~~~text
latest value
~~~

termination delimiter：

~~~text
control progress bypass
~~~

这三种不是同一种数据。

---

# 五十七、把所有消息塞同一个 Queue Policy 是危险的

如果：

~~~text
sensor state
safety stop
trajectory chunk
heartbeat
~~~

全部共享：

~~~text
one FIFO + one HWM
~~~

很容易让安全消息被普通 backlog 阻塞。

---

# 五十八、Runtime 设计应按 Semantics 拆 Channel

例如：

~~~text
latest-state lane
bounded-command lane
priority-control lane
~~~

这比一味增加 HWM 更可靠。

---

# 五十九、一个写侧状态机

~~~text
               +----------------------+
               |                      |
               v                      |
           OUT_ACTIVE                 |
               |                      |
        check_hwm false               |
               |                      |
               v                      |
         OUT_INACTIVE                 |
               |                      |
               | activate_write(N)    |
               | update peer progress |
               +----------------------+
~~~

---

# 六十、一个读侧状态机

~~~text
               +----------------------+
               |                      |
               v                      |
            IN_ACTIVE                 |
               |                      |
           queue empty                |
               |                      |
               v                      |
          IN_INACTIVE                 |
               |                      |
               | activate_read        |
               +----------------------+
~~~

---

# 六十一、两个状态机是互补的

写侧等待：

~~~text
space
~~~

读侧等待：

~~~text
data
~~~

两种 condition change 都通过：

~~~text
cross-thread command
~~~

恢复。

---

# 六十二、为什么不使用 Condition Variable

因为 pipe peer 可能属于：

~~~text
socket event loop
I/O thread
~~~

它已经有 mailbox + poller execution model。

直接 condition_variable：

~~~text
会创建第二套 wait domain
~~~

不利于统一事件循环。

---

# 六十三、Activation Command 就是 Pipe-level Event Notification

它把：

~~~text
resource condition changed
~~~

送回：

~~~text
owner-thread scheduler
~~~

非常接近 Reactor 中：

~~~text
readiness event
~~~

只是来源是 peer progress。

---

# 六十四、Data-plane 与 Control-plane 的顺序为什么重要

例如 reader：

~~~text
先更新 _msgs_read
再发送 activate_write
~~~

writer收到后：

~~~text
先更新 _peers_msgs_read
再 scheduler activation
~~~

形成完整：

~~~text
truth-before-notify
~~~

协议。

---

# 六十五、与 Mailbox 的 Publish-before-Wake 是同一原则

Mailbox：

~~~text
publish command
→ signal
~~~

Pipe：

~~~text
publish progress state in command
→ owner processes
→ activation
~~~

共同原则：

> **通知只是“请重新检查”的触发器；权威状态必须先建立。**

---

# 六十六、为什么 `_out_active=true` 后不立即再次检查 HWM

`process_activate_write()`：

~~~text
update progress
_out_active=true
notify scheduler
~~~

真正是否还能写：

~~~text
下一次 check_write()
~~~

再次验证。

---

# 六十七、Activation 是 Opportunity，不是 Guarantee

这和 epoll readiness 一样：

~~~text
“值得尝试”
~~~

而不是：

~~~text
“保证一定成功”
~~~

因为从通知到真正执行之间，

状态可能继续变化。

---

# 六十八、Runtime Event 通常应该被理解成 Re-evaluation Hint

这个原则贯穿：

- Asio epoll readiness；
- libzmq mailbox wake；
- pipe activate_write；
- pipe activate_read；
- Cyber wake；
- Holoscan scheduling event。

---

# 六十九、为什么 `check_hwm()` 公开存在

`dist_t` 可以：

~~~text
预先检查所有 matching pipes
~~~

而不一定真正 write。

这用于：

~~~text
policy-level admission
~~~

---

# 七十、但 `check_hwm()` 与 `write()` 之间也不是原子 Transaction

它们都在 owner-thread execution model 下使用，

避免多个线程对同一 pipe 直接并发写。

否则：

~~~text
check passes
another writer consumes capacity
write exceeds
~~~

就需要更复杂同步。

---

# 七十一、Owner-thread 模型再次降低并发复杂度

因为：

~~~text
one pipe endpoint mutable state
~~~

由一个 owner执行，

`_msgs_written` 等字段不需要 atomic。

---

# 七十二、SPSC Queue + Owner Thread + Command Control 是整套设计

不能只摘其中一个。

真正的架构是：

~~~text
single-owner mutable endpoint
+
SPSC data path
+
cross-thread command control path
+
event-driven scheduler membership
~~~

---

# 七十三、如果从零设计类似 Channel

可以抽象：

~~~cpp
struct Channel
{
    Queue* in;
    Queue* out;

    bool in_active;
    bool out_active;

    uint64_t read_total;
    uint64_t write_total;
    uint64_t peer_read_total;

    size_t high_watermark;
    size_t low_watermark;

    LifecycleState state;
    EventSink* sink;
};
~~~

---

# 七十四、但字段本身不是最难的

最难的是定义：

~~~text
谁能改这些字段？
谁能看这些字段？
什么时候跨线程传进度？
通知是否会丢？
关闭时谁负责 queue lifetime？
~~~

---

# 七十五、对机器人 CAN Runtime 的直接映射

~~~text
control producers
      |
      v
TxChannel
  |
  +-- tx_written
  +-- peer/drain progress
  +-- HWM
  +-- active flag
      |
      v
CAN owner thread
~~~

当 SocketCAN / driver backlog 高：

~~~text
deactivate normal producer eligibility
~~~

driver drain 到阈值：

~~~text
reactivate
~~~

---

# 七十六、高频 Motor Setpoint 可以考虑 Latest-value Lane

如果 1 kHz 控制 loop产生：

~~~text
setpoint t
setpoint t+1
setpoint t+2
~~~

下游只能处理较慢，

旧 setpoint 的业务价值可能迅速下降。

这时：

~~~text
latest-state/conflate
~~~

往往比：

~~~text
huge FIFO
~~~

更合理。

---

# 七十七、但 E-stop 不应与 Setpoint 共用 Conflate 语义

安全事件：

~~~text
需要独立可靠控制路径
~~~

避免被：

- 覆盖；
- HWM；
- 普通数据 backlog；

阻塞。

---

# 七十八、对视觉 Pipeline 的迁移

Camera detector 30 Hz，

控制 loop 可能 1 kHz。

视觉检测结果通常更像：

~~~text
latest estimate
~~~

不是：

~~~text
必须逐帧排队执行
~~~

所以：

~~~text
conflate/latest-value
~~~

常比无界 backlog 更适合闭环控制。

---

# 七十九、对地图/日志又不一样

日志：

~~~text
允许 batch
但可能要求不丢
~~~

地图增量：

~~~text
可能需要顺序
~~~

所以 HWM policy 必须按数据语义选。

---

# 八十、为什么 HWM 不是“越大越好”

更大 HWM：

~~~text
less producer blocking
~~~

但代价：

- 更高内存；
- 更旧数据；
- 更长排队延迟；
- shutdown drain 更慢；
- fault recovery 更难。

---

# 八十一、实时系统更关心 Age，而不只是 Throughput

一个 10 秒前的控制 command：

~~~text
即使可靠送达
~~~

也可能已经没有价值。

所以容量策略最好结合：

~~~text
deadline
age
priority
latest-value
~~~

而不是只调大 HWM。

---

# 八十二、HWM 是 Admission Control，不是 Latency Guarantee

它只限制：

~~~text
最多领先多少 message
~~~

不会保证：

~~~text
每条 message 在 X ms 内处理
~~~

---

# 八十三、Backpressure 需要和 Deadline 一起设计

机器人 runtime 常见：

~~~text
if backlog age > deadline
→ drop / supersede / fail-safe
~~~

而不是：

~~~text
永远等队列慢慢清
~~~

---

# 八十四、为什么 LWM 大致选一半是工程折中

这不是数学最优常数。

它是在：

~~~text
wake frequency
vs
buffer utilization
~~~

之间取简单、稳定的折中。

---

# 八十五、最佳 LWM 取决于成本模型

如果：

~~~text
thread wake extremely expensive
~~~

可以更偏向大批量。

如果：

~~~text
latency更关键
~~~

可能更早反馈。

libzmq 选择通用 runtime 的中间点。

---

# 八十六、HWM 也不必永久固定

源码支持：

~~~text
set_hwms
send_hwms_to_peer
~~~

说明 runtime 可以更新阈值。

---

# 八十七、动态阈值更新也必须遵循 Owner-thread Protocol

不能 foreign thread：

~~~text
peer->_hwm = x
~~~

而要 command。

否则会重新引入共享 mutable state。

---

# 八十八、Pipe 的 Runtime Invariants

第一：

> **同一 endpoint 的 mutable flow-control state 只在 owner thread 中修改。**

第二：

> **outstanding capacity 用 complete-message counters 表达，不直接读取跨线程 queue size。**

第三：

> **writer 只相信最近收到的 peer progress，因此容量判断可以保守但不能乐观越界。**

第四：

> **HWM 命中必须让 pipe 离开上层 active scheduling set。**

第五：

> **reader progress 必须先更新容量 truth，再重新激活 writer。**

第六：

> **LWM 用于批量 progress feedback，避免一条 message 一次跨线程 wake。**

第七：

> **multipart publication、capacity accounting 和 scheduler routing 都必须保持 message atomicity。**

第八：

> **control-progress tokens（delimiter / term / credit return）不能被普通业务 backpressure 永久阻塞。**

第九：

> **data queue lifetime 与 pipe endpoint lifetime必须通过终止协议协调。**

第十：

> **queue mode（FIFO vs conflate）改变的是业务语义，不只是性能参数。**

---

# 八十九、完整 Write Path

~~~text
socket pattern
    |
    | choose active pipe
    v
pipe.check_write()
    |
    +-- state != active
    |      → reject
    |
    +-- out_active == false
    |      → reject
    |
    +-- estimated backlog >= HWM
    |      ↓
    |   out_active = false
    |      ↓
    |   scheduler removes pipe
    |      ↓
    |   reject
    |
    v
pipe.write(msg)
    |
    | ypipe.write(frame, more)
    |
    +-- intermediate multipart
    |      → no message count increment
    |
    +-- final frame
           ↓
       msgs_written++
           ↓
       later flush
           ↓
       peer activation if reader passive
~~~

---

# 九十、完整 Read / Credit-return Path

~~~text
peer owner scheduler
    |
    | picks readable pipe
    v
pipe.read(msg)
    |
    | ypipe.read
    v
complete message?
    |
   yes
    |
    v
msgs_read++
    |
msgs_read % LWM == 0 ?
    |
   yes
    |
    v
send_activate_write(
  peer,
  msgs_read)
    |
    v
peer mailbox
    |
    v
peer owner thread
    |
    v
process_activate_write(N)
    |
    +-- peers_msgs_read = N
    +-- out_active = true
    +-- sink->write_activated
    |
    v
LB / Dist active set
~~~

---

# 九十一、完整 Read Empty Path

~~~text
scheduler tries pipe
    |
    v
pipe.check_read()
    |
    v
ypipe.check_read()
    |
 no data
    |
    v
in_active = false
    |
    v
FQ removes pipe
    |
    | later peer writes + flush
    v
ypipe reports passive reader
    |
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
sink->read_activated
    |
    v
FQ re-adds pipe
~~~

---

# 九十二、完整 Termination Path

~~~text
Side A terminate()
    |
    +-- out_active=false
    +-- rollback incomplete multipart
    +-- send PIPE_TERM command
    +-- write delimiter bypassing HWM
    +-- flush
    |
    v
Side B may see:
    |
    +-- delimiter first
    |     → delimiter_received
    |
    +-- term command first
          → waiting_for_delimiter
             if delay=true
    |
    v
both conditions satisfied
    |
    v
send PIPE_TERM_ACK
    |
    v
each side receives ack
    |
    v
sink->pipe_terminated
    |
    v
drain/close inbound storage
    |
    v
delete own inbound queue
    |
    v
delete endpoint
~~~

---

# 九十三、为什么这套设计值得学

因为它把一个看起来简单的：

~~~text
bounded queue
~~~

展开成了完整 runtime 问题：

~~~text
capacity
ownership
cross-thread progress
scheduler eligibility
wakeup batching
multipart atomicity
shutdown progress
queue backend semantics
~~~

HWM 只是最外层那个数字。

---

# 九十四、最终心智模型

~~~text
               producer owner
                    |
                    | complete message
                    v
             _msgs_written++
                    |
                    | estimate backlog
                    v
       _msgs_written - _peers_msgs_read
                    |
              reaches HWM?
             /            \
           no              yes
           |                |
           v                v
         write        _out_active=false
                           |
                           v
                   leave scheduler set
                           |
                           |
                    consumer progresses
                           |
                    _msgs_read += N
                           |
                     reaches LWM
                           |
                           v
                 activate_write(N)
                           |
                           v
                   producer mailbox
                           |
                           v
                 _peers_msgs_read=N
                           |
                    _out_active=true
                           |
                           v
                   re-enter scheduler
~~~

如果只记一个结论：

> **libzmq 的 HWM 不是“队列满了就 return false”，而是一套跨线程 progress-feedback 控制协议：producer 用单调计数估计 backlog，HWM 把资源移出调度集合，consumer 按 LWM 批量归还容量，`activate_write` 把容量事实送回 owner thread，再把资源重新加入调度集合。真正的 backpressure，是“容量状态改变调度资格”。**
