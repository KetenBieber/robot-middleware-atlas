# DEALER / ROUTER：显式路由、Routing-ID 生命周期与 Multipart 粘性

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

DEALER 与 ROUTER 看起来只是 ZeroMQ 的两种 socket pattern，但它们非常适合用来理解一个更一般的问题：

> **当同一套 Pipe Runtime 同时支持“任意选一个 peer”和“必须指定某个 peer”时，调度、路由索引、消息 envelope、背压和连接生命周期应该怎样分层？**

DEALER 的答案非常简单：

~~~text
inbound
→ fq_t

outbound
→ lb_t
~~~

ROUTER 则完全不同：

~~~text
inbound
→ fq_t
→ prepend routing-id view

outbound
→ routing-id lookup
→ pin one pipe for whole multipart
~~~

所以二者真正的差别不是 transport。

它们都可以复用：

- Pipe；
- ypipe；
- mailbox；
- Session；
- Engine；
- TCP/inproc transport。

区别集中在：

~~~text
who chooses the peer
and
how that choice survives a whole message
~~~

`fq_t / lb_t / dist_t` 怎样用 active prefix、intrusive index 和 multipart transaction 做多 Pipe 调度，见 [FQ / LB / DIST：Active Prefix、Multipart 原子性与消息调度器](fq-lb-dist-schedulers.md)。

---

# 一、先把 DEALER 压缩到最小模型

`dealer_t::xattach_pipe()`：

~~~cpp
_fq.attach(pipe_);
_lb.attach(pipe_);
~~~

同一个 `pipe_t` 同时进入：

~~~text
array_item_t<1>
→ inbound FQ

array_item_t<2>
→ outbound LB
~~~

这直接体现了 Pipe 多份 intrusive index 的意义。

---

## 1. DEALER 收消息

~~~cpp
int dealer_t::xrecv(msg_t *msg)
{
    return _fq.recvpipe(msg, NULL);
}
~~~

所以：

~~~text
many peers
→ fair queue
→ one complete message at a time
~~~

---

## 2. DEALER 发消息

~~~cpp
int dealer_t::xsend(msg_t *msg)
{
    return _lb.sendpipe(msg, NULL);
}
~~~

所以：

~~~text
one complete message
→ choose one writable peer
→ pin multipart to that peer
~~~

---

## 3. DEALER 的选择规则是“谁都可以”

它不要求业务指定：

~~~text
peer A
peer B
peer C
~~~

而是：

~~~text
从当前 writable set 中
round-robin 选择一个
~~~

这很适合：

- worker pool；
- homogeneous backend；
- 任意一个执行器都能处理的 job；
- request fan-in / balanced fan-out。

---

# 二、ROUTER 为什么不能直接用 LB

ROUTER 的业务 contract 是：

> **应用先给 routing id，Runtime 必须把剩余 message 发给那个指定 peer。**

这已经不是：

~~~text
choose any writable peer
~~~

而是：

~~~text
lookup exact peer
~~~

---

## 4. ROUTER 的发送 Envelope

应用看到：

~~~text
frame 0
= routing id

frame 1...
= payload
~~~

所以一条 outbound multipart：

~~~text
[routing-id][payload-1][payload-2]...
~~~

第一帧不是发送给 peer 的普通业务 payload。

它是：

~~~text
local routing instruction
~~~

---

## 5. 第一帧由 ROUTER Runtime 消费

`xsend()` 在 `_more_out == false` 时：

~~~cpp
out_pipe_t *out_pipe =
    lookup_out_pipe(
      blob_t(msg->data(),
             msg->size(),
             reference_tag_t()));

_current_out =
    out_pipe
      ? out_pipe->pipe
      : NULL;
~~~

然后 routing-id frame 本身：

~~~text
close
reinit
~~~

不会作为普通数据 frame继续向下发。

---

## 6. 所以 Routing ID 是 Socket-level Metadata

它不是：

~~~text
TCP header
~~~

也不是：

~~~text
用户 payload 的一部分
~~~

而是 socket pattern 在 Runtime 层构造出的 envelope。

---

# 三、ROUTER 的核心索引：Routing ID → Pipe

`routing_socket_base_t` 保存：

~~~cpp
typedef std::map<
    blob_t,
    out_pipe_t>
    out_pipes_t;

out_pipes_t _out_pipes;
~~~

其中：

~~~cpp
struct out_pipe_t
{
    pipe_t *pipe;
    bool active;
};
~~~

---

## 7. Routing Table 是“名字 → Runtime Endpoint”

逻辑：

~~~text
routing id
    ↓
std::map<blob_t, out_pipe_t>
    ↓
pipe_t*
~~~

所以应用看到的“peer identity”最终必须落到：

~~~text
一个具体 Pipe endpoint
~~~

---

## 8. 为什么 Key 是 blob_t

Routing ID：

- 可以是二进制；
- 不要求 NUL terminated；
- 长度显式；
- 不是普通 C string。

所以 key 不应该机械写成：

~~~text
std::string semantic identity
~~~

它首先是：

~~~text
byte string
~~~

---

# 四、Routing ID 不等于 Transport Address

这是最重要的语义边界之一。

~~~text
tcp://10.0.0.5:5555
~~~

描述：

~~~text
transport endpoint
~~~

而：

~~~text
worker-17
~~~

或某个二进制 ID 描述：

~~~text
logical routing identity
~~~

同一 transport connection 生命周期中，

routing identity 可能来自：

- peer handshake；
- `ZMQ_CONNECT_ROUTING_ID`；
- auto-generated integral ID；
- raw socket auto-ID。

---

## 9. 所以 Routing Layer 位于 Transport 之上

~~~text
TCP connection
    ↓
Session / Engine
    ↓
Pipe
    ↓
Routing ID association
    ↓
ROUTER application envelope
~~~

不能把：

~~~text
routing-id
~~~

等同为：

~~~text
socket fd
~~~

---

# 五、Peer 第一次什么时候获得 Routing ID

关键函数：

~~~cpp
router_t::identify_peer(
    pipe_t *pipe,
    bool locally_initiated)
~~~

它有多条来源。

---

# 六、路径 A：`ZMQ_CONNECT_ROUTING_ID`

如果：

~~~text
locally initiated
+
connect routing id already configured
~~~

则：

~~~cpp
extract_connect_routing_id()
~~~

直接拿到给下一次 connect 使用的 routing id。

这个配置是：

~~~text
one-shot next-connect state
~~~

取出以后：

~~~text
_connect_routing_id.clear()
~~~

---

## 10. 为什么是 One-shot

因为它表达：

> **下一条 outbound connection 应该以哪个逻辑名字注册。**

而不是：

~~~text
这个 socket 以后所有 connect 都用同一个 id
~~~

---

# 七、路径 B：Raw Socket 自动生成 Integral ID

raw 模式：

~~~text
0 byte prefix
+
32-bit generated value
~~~

形成一个内部 ID。

---

## 11. 为什么第一字节是 0

ZeroMQ 约定：

~~~text
leading zero
→ infrastructure-reserved routing id space
~~~

所以自动生成 ID 与普通用户 routing-id 空间区分。

---

# 八、路径 C：从 Peer Pipe 读 Routing-ID Frame

普通 ROUTER：

~~~cpp
pipe_->read(&msg)
~~~

尝试从刚建立的 pipe 读取 peer identity。

如果：

~~~text
size > 0
~~~

就：

~~~text
routing_id = peer-provided bytes
~~~

---

## 12. 如果 Peer 没给 ID

`msg.size()==0`：

~~~text
fallback
→ auto-generate integral routing id
~~~

所以 ROUTER 总要建立：

~~~text
routing id ↔ pipe
~~~

这个映射。

---

# 九、为什么识别失败的 Pipe 先进入 Anonymous Set

`xattach_pipe()`：

~~~cpp
if (identify_peer(...))
    _fq.attach(pipe);
else
    _anonymous_pipes.insert(pipe);
~~~

这说明：

~~~text
connected pipe
~~~

不一定立即：

~~~text
routable pipe
~~~

---

## 13. Anonymous 的真实语义

不是：

~~~text
没有对象
~~~

而是：

> **物理/Runtime endpoint 已经存在，但逻辑 routing identity 尚未建立。**

---

## 14. 为什么不能立刻放进 FQ

如果应用收到这条 pipe 的 payload，

ROUTER 必须先返回：

~~~text
routing-id frame
~~~

但此时没有 routing id。

所以它还不具备：

~~~text
ROUTER API-level receive eligibility
~~~

---

# 十、Anonymous Pipe 后续如何转正

`xread_activated(pipe)`：

~~~text
if not anonymous
→ FQ activated

if anonymous
→ try identify_peer again
~~~

识别成功：

~~~text
erase anonymous set
→ fq.attach(pipe)
~~~

---

## 15. 这是典型的 Two-stage Admission

第一阶段：

~~~text
transport/runtime attachment
~~~

第二阶段：

~~~text
logical identity admission
~~~

成熟系统经常需要分开：

- TCP connected；
- authenticated；
- identity known；
- protocol ready；
- application schedulable。

---

# 十一、`add_out_pipe()` 真正注册什么

~~~cpp
const out_pipe_t outpipe =
    {pipe, true};

_out_pipes.emplace(
    routing_id,
    outpipe);
~~~

这相当于：

~~~text
routing identity publication
~~~

一旦插入，

应用 outbound routing 就可能找到这个 peer。

---

# 十二、Routing Table 不是 Pipe Ownership

`_out_pipes` 保存：

~~~text
pipe_t*
~~~

但 routing table：

~~~text
owns lookup membership
~~~

不等于：

~~~text
owns physical pipe memory
~~~

Pipe 生命周期仍由 socket/session/termination 协议管理。

---

# 十三、Routing Membership 是 Lifetime Reachability

只要：

~~~text
routing_id
→ pipe*
~~~

仍然存在，

未来的 `xsend()` 就可能拿到这个 pointer。

因此：

> **删除 Pipe 之前，必须先切断 routing table 对它的未来可达性。**

---

# 十四、Pipe 终止时 ROUTER 做什么

`xpipe_terminated()`：

~~~cpp
if (anonymous)
{
    erase anonymous;
}
else
{
    erase_out_pipe(pipe);
    _fq.pipe_terminated(pipe);
    pipe->rollback();

    if (pipe == _current_out)
        _current_out = NULL;
}
~~~

这不是普通：

~~~text
delete one connection
~~~

而是在维护多个索引/transaction。

---

## 16. 必须同时修复四类状态

至少包括：

~~~text
anonymous membership
routing table membership
FQ membership
current outbound transaction
~~~

还要：

~~~text
rollback incomplete outbound multipart
~~~

---

# 十五、为什么 Termination 必须清 `_current_out`

如果当前发送：

~~~text
routing id → pipe A
payload frame 1 [more]
~~~

此时 A 终止，

而 `_current_out` 仍指向 A：

~~~text
下一帧
→ dangling / stale endpoint
~~~

所以终止必须切断：

~~~text
current transaction target
~~~

---

# 十六、ROUTER Outbound 是“两阶段状态机”

第一阶段：

~~~text
EXPECT_ROUTING_ID
~~~

第二阶段：

~~~text
SEND_PAYLOAD_TO_CURRENT_OUT
~~~

代码状态：

~~~text
_more_out
_current_out
~~~

组合起来表示。

---

## 17. 起始状态

~~~text
_more_out = false
_current_out = NULL
~~~

下一 frame 必须解释为：

~~~text
routing instruction
~~~

---

## 18. 读到 Routing ID Frame

如果它带：

~~~text
more
~~~

说明：

~~~text
后面还有 payload
~~~

于是：

~~~text
_more_out = true
_current_out = looked-up pipe or NULL
~~~

---

## 19. Payload 阶段

后续每个 frame：

~~~text
if current_out != NULL
    write to same pipe
else
    drop
~~~

直到：

~~~text
more flag clears
~~~

---

## 20. 最后一帧

成功：

~~~text
flush current pipe
_current_out = NULL
_more_out = false
~~~

transaction 完成。

---

# 十七、为什么不能每个 Payload Frame 都重新查 Routing Table

因为 routing table 可能在 multipart 中途发生：

- disconnect；
- duplicate ID；
- handover；
- replacement；
- HWM state change。

如果 frame 2 重新 lookup：

~~~text
routing id
→ new pipe B
~~~

就可能形成：

~~~text
frame 1 → old A
frame 2 → new B
~~~

一个 multipart message 被切成两半。

---

## 21. `_current_out` 是 Transaction Pin

所以：

~~~text
routing lookup
~~~

只发生在 message 开始。

后续依赖：

~~~text
pinned endpoint
~~~

而不是：

~~~text
latest routing table state
~~~

---

# 十八、这和 LB `_current + _more` 是同一种不变量

DEALER/LB：

~~~text
scheduler chooses pipe A
→ pin multipart
~~~

ROUTER：

~~~text
application chooses routing id
→ lookup pipe A
→ pin multipart
~~~

选择者不同，

但 transaction invariant 相同：

> **完整 message 的目的 endpoint 一旦确定，中途不得重新选择。**

---

# 十九、如果 Target 根本不存在

第一帧 lookup：

~~~text
routing id
→ not found
~~~

默认 ROUTER：

~~~text
_current_out = NULL
~~~

后续 payload：

~~~text
silently consumed/dropped
~~~

---

# 二十、为什么默认不是 Error

因为默认 ROUTER 语义允许：

~~~text
unroutable
→ drop
~~~

这和：

~~~text
reliable message delivery
~~~

完全不是一回事。

---

# 二十一、`ROUTER_MANDATORY` 改变的是应用可见错误语义

开启：

~~~text
ZMQ_ROUTER_MANDATORY = 1
~~~

如果 routing id 不存在：

~~~text
EHOSTUNREACH
~~~

---

## 22. 如果 Peer 存在但 HWM 满

第一帧找到 pipe 后：

~~~cpp
if (!pipe->check_write())
{
    const bool pipe_full =
        !pipe->check_hwm();

    ...
}
~~~

mandatory 时：

~~~text
pipe full
→ EAGAIN

peer gone / not writable for other reason
→ EHOSTUNREACH
~~~

---

# 二十二、这说明“可路由”和“有容量”是两个条件

目标成功至少需要：

\[
Routable
\land
Writable
\]

其中：

~~~text
Routable
→ routing map contains live target

Writable
→ Pipe flow-control admits message
~~~

---

# 二十三、ROUTER 没有一个简单 Global Writable Bool

这是显式路由系统和 LB 最大区别。

LB：

~~~text
只要 active set 非空
→ 可以选一个
~~~

ROUTER：

~~~text
是否能发
取决于你具体选哪个 routing id
~~~

---

# 二十四、为什么 `xhas_out()` 的语义天然较弱

非 mandatory：

~~~cpp
return true;
~~~

因为默认语义：

~~~text
即使目标不存在
也可以通过“drop”处理发送
~~~

---

## 24. Mandatory 模式

~~~cpp
return any_of_out_pipes(
    check_pipe_hwm);
~~~

也就是说：

~~~text
POLLOUT
→ 至少某一个 peer 有容量
~~~

---

# 二十五、但这不保证“你的目标 Peer”可写

假设：

~~~text
peer A writable
peer B HWM full
~~~

`xhas_out()`：

~~~text
true
~~~

因为 A 可写。

但应用下一条消息指定：

~~~text
routing id B
~~~

仍会：

~~~text
EAGAIN
~~~

---

# 二十六、Global Readiness 与 Target-specific Readiness 不同

这是显式路由 API 里非常重要的原则：

~~~text
socket writable
≠
chosen destination writable
~~~

---

# 二十七、`get_peer_state()` 为什么存在

它先：

~~~text
routing-id lookup
~~~

如果不存在：

~~~text
EHOSTUNREACH
~~~

存在则：

~~~text
pipe->check_hwm()
→ ZMQ_POLLOUT
~~~

这才是：

~~~text
per-peer readiness
~~~

---

# 二十八、对机器人系统的直接启发

假设一个控制节点管理：

~~~text
motor-1
motor-2
motor-3
~~~

全局：

~~~text
some actuator writable
~~~

不能推导：

~~~text
motor-2 writable
~~~

显式目标系统必须提供：

~~~text
target-specific readiness / health
~~~

---

# 二十九、Routing Table 中的 `out_pipe_t.active`

结构：

~~~cpp
struct out_pipe_t
{
    pipe_t *pipe;
    bool active;
};
~~~

Pipe HWM 失败时当前 ROUTER 路径会：

~~~text
out_pipe.active = false
~~~

peer progress 后：

~~~text
routing_socket_base_t::xwrite_activated
→ locate matching pipe
→ active = true
~~~

---

## 29. 但它不是容量 Truth 本身

真正 HWM truth 仍在：

~~~text
pipe_t
~~~

当前固定源码的 ROUTER 发送判断直接调用：

~~~text
pipe->check_write()
pipe->check_hwm()
~~~

所以：

> **routing-level active flag 不能替代 Pipe 的 resource-local flow-control state。**

---

# 三十、为什么重复维护 Routing-level State

一层是：

~~~text
resource-local state
~~~

另一层是：

~~~text
routing index / pattern-level state
~~~

它们服务不同抽象层。

成熟 Runtime 常见这种：

~~~text
source of truth
+
higher-level membership/cache
~~~

关键是要明确谁权威。

---

# 三十一、ROUTER Receive 为什么比 DEALER Receive 更复杂

DEALER：

~~~text
pipe payload
→ application payload
~~~

ROUTER：

~~~text
pipe payload
→ application must first see routing-id
→ then payload
~~~

所以必须做：

~~~text
message-view transformation
~~~

---

# 三十二、底层 Pipe 并没有真的多一个 Routing-ID Frame

接收时：

~~~cpp
_fq.recvpipe(msg, &pipe)
~~~

拿到的是：

~~~text
real payload frame
~~~

ROUTER 再从：

~~~text
pipe->get_routing_id()
~~~

构造一个新的 API-visible frame。

---

# 三十三、Prefetch 的真正原因

由于已经从 Pipe 读出了 payload，

但当前这次 `recv()` 必须先返回：

~~~text
routing-id frame
~~~

payload 不能丢。

所以：

~~~text
payload
→ _prefetched_msg
~~~

---

# 三十四、ROUTER Receive View

逻辑：

~~~text
underlying pipe:

[payload-1][payload-2...]

application sees:

[routing-id][payload-1][payload-2...]
~~~

routing-id 是 Runtime 注入的 envelope。

---

# 三十五、为什么 `_prefetched_msg` 必须是完整 `msg_t`

不能只缓存：

~~~text
void*
size
~~~

因为 payload 可能携带：

- refcounted storage；
- metadata；
- zero-copy release callback；
- group；
- flags；
- multipart state。

所以必须保持：

~~~text
msg_t ownership protocol
~~~

---

# 三十六、Prefetch 不是性能 Cache

它是：

> **跨 API 调用保存消息所有权的协议状态。**

---

# 三十七、`xrecv()` 的第一种路径：已有 Prefetch

如果：

~~~text
_prefetched=true
~~~

且 routing id 尚未返回：

~~~text
move _prefetched_id
~~~

之后：

~~~text
move _prefetched_msg
~~~

---

# 三十八、`xhas_in()` 也可能触发 Prefetch

这是另一个很容易忽略的点。

`xhas_in()`：

~~~text
不是只看一个 bool
~~~

它可能直接：

~~~text
_fq.recvpipe(
    &_prefetched_msg,
    &pipe)
~~~

把真实 payload 从 Pipe 消费出来。

---

## 38. 然后构造 `_prefetched_id`

~~~text
pipe routing id
→ _prefetched_id
~~~

设置：

~~~text
more
~~~

并保存：

~~~text
_current_in = pipe
~~~

---

# 三十九、所以 Readiness Probe 可以具有内部消费副作用

应用调用：

~~~text
poll / has_in
~~~

Runtime 为了确认：

~~~text
真的有一条完整 API-visible message
~~~

可能预取底层数据。

这要求：

~~~text
prefetch buffer
~~~

承担 ownership。

---

# 四十、为什么这个副作用对 API 仍然安全

因为数据没有：

~~~text
丢失
~~~

而是从：

~~~text
Pipe queue
~~~

移动到：

~~~text
socket-local prefetch state
~~~

对应用可见顺序保持不变。

---

# 四十一、Readiness 查询不能破坏 Observable Order

这是设计 prefetch 时必须守住的原则。

可以内部移动 ownership，

但不能：

- 跳过消息；
- 重排消息；
- 重复消息；
- 改变 multipart boundary。

---

# 四十二、Metadata 为什么复制到 Routing-ID Frame

构造 routing-id frame 时：

~~~cpp
if (_prefetched_msg.metadata())
    msg->set_metadata(
      _prefetched_msg.metadata());
~~~

所以应用看到 routing envelope 时，

仍可关联：

~~~text
peer metadata / transport metadata
~~~

---

# 四十三、API Envelope 不只是字节

它还要维护：

~~~text
metadata continuity
~~~

否则用户在第一帧看到的 peer identity 和后续 payload metadata 会断裂。

---

# 四十四、Inbound Multipart 也需要 `_current_in`

ROUTER 接收一条 multipart：

~~~text
[routing-id synthetic]
[payload 1 more]
[payload 2 more]
[payload 3 last]
~~~

必须记住：

~~~text
这些 frame 都来自哪个 pipe
~~~

所以：

~~~text
_current_in
_more_in
~~~

形成 inbound transaction state。

---

# 四十五、为什么后续 Frame 不再返回 Routing ID

只在：

~~~text
message boundary
~~~

插入一次 routing envelope。

后续：

~~~text
_more_in=true
~~~

直接继续返回 payload。

---

# 四十六、Routing ID 是 Message-level Header，不是 Frame-level Header

这再次体现：

~~~text
socket pattern semantic unit
= complete message
~~~

---

# 四十七、Duplicate Routing ID 为什么危险

假设旧连接：

~~~text
routing-id X
→ pipe A
~~~

新连接也声明：

~~~text
routing-id X
→ pipe B
~~~

Routing Table 不能同时拥有两个相同 key。

必须定义冲突策略。

---

# 四十八、默认策略：拒绝新 Peer

如果：

~~~text
existing_outpipe found
+
_handover == false
~~~

`identify_peer()`：

~~~text
return false
~~~

新 pipe 留在：

~~~text
anonymous path
~~~

不会抢走 X。

---

# 四十九、为什么默认不能自动替换

因为旧连接可能仍然：

- 在接收 multipart；
- 有 outbound queued data；
- 被 `_current_in` 引用；
- 被 application 当成同一逻辑 peer；
- 处于 termination/drain。

直接覆盖 map pointer：

~~~text
X → B
~~~

并删除 A，

可能破坏当前 transaction 和生命周期。

---

# 五十、`ZMQ_ROUTER_HANDOVER` 定义显式 Replacement Protocol

开启 handover：

~~~text
new pipe B
claims existing id X
~~~

ROUTER 不只是：

~~~text
_out_pipes[X] = B
~~~

而是先处理旧 pipe。

---

# 五十一、第一步：给旧 Pipe 临时改名

源码生成：

~~~text
new internal integral routing id Y
~~~

然后：

~~~text
erase X → old A

old A routing id = Y

add Y → old A
~~~

此时：

~~~text
X
~~~

已经腾出来。

---

## 51. 为什么要先改名

因为旧 pipe 的异步终止尚未完成。

它仍然需要：

~~~text
在 routing table 中拥有唯一 key
~~~

直到 lifecycle 结束。

---

# 五十二、这是一种 Rename-before-Retire

逻辑：

~~~text
old identity X
    ↓
rename old resource to private Y
    ↓
publish new resource as X
    ↓
retire old Y asynchronously
~~~

这是非常值得迁移的 replacement pattern。

---

# 五十三、为什么不能先 Delete Old，再 Publish New

旧 pipe 可能还有：

~~~text
in-flight inbound multipart
~~~

同步删除会破坏 transaction。

所以：

~~~text
identity handover
~~~

和：

~~~text
physical reclamation
~~~

必须分离。

---

# 五十四、如果旧 Pipe 正是 `_current_in`

源码：

~~~cpp
if (old_pipe == _current_in)
    _terminate_current_in = true;
else
    old_pipe->terminate(true);
~~~

这非常关键。

---

## 54. 为什么不能立即 terminate 当前 Inbound Pipe

应用已经开始接收：

~~~text
routing-id X
payload frame 1 [more]
...
~~~

如果中途直接杀旧 pipe，

当前 multipart 会被截断。

---

# 五十五、所以设置 Deferred Termination Flag

~~~text
_terminate_current_in = true
~~~

含义：

> **旧连接已经逻辑退休，但为了完成当前 message，物理终止延迟到 message boundary。**

---

# 五十六、最后一帧到达时才真正 terminate

当：

~~~text
_more_in becomes false
~~~

ROUTER：

~~~text
_current_in->terminate(true)
_terminate_current_in = false
_current_in = NULL
~~~

---

# 五十七、这就是 Transaction-aware Quiescence

不是：

~~~text
等所有线程退出
~~~

而是：

~~~text
等当前 message transaction 到达安全边界
~~~

再执行资源退役。

---

# 五十八、Handover 因此有两个不同的切换时刻

Outbound identity：

~~~text
X
→ 可以较早指向 new pipe B
~~~

Old inbound transaction：

~~~text
A
→ 允许把当前 multipart 读完
~~~

这两个时间点不必相同。

---

# 五十九、Logical Identity 与 Physical Connection 可以短暂分叉

Handover 期间：

~~~text
logical X
→ new B

old A
→ temporary internal Y
→ draining current inbound message
~~~

这是一种：

~~~text
versioned identity transition
~~~

---

# 六十、这比“map replace”高级在哪里

普通 map replace 只处理：

~~~text
key uniqueness
~~~

Runtime handover 必须同时处理：

- identity uniqueness；
- current transaction；
- queued data；
- endpoint lifetime；
- asynchronous close；
- future lookup。

---

# 六十一、Routing Registry 本质上也是 Lifecycle Registry

它不只是：

~~~text
dictionary
~~~

而是：

> **决定哪些物理 endpoint 仍然可被未来业务操作发现。**

因此 registry mutation 是生命周期协议的一部分。

---

# 六十二、`erase_out_pipe()` 为什么使用 Pipe 自己保存的 Routing ID

~~~cpp
_out_pipes.erase(
    pipe->get_routing_id());
~~~

所以 Pipe 内部的：

~~~text
_router_socket_routing_id
~~~

与 routing map key 必须保持一致。

---

# 六十三、Handover Rename 为什么必须同时更新 Pipe

顺序：

~~~text
erase old map key
pipe.set_router_socket_routing_id(Y)
add map[Y] = old pipe
~~~

如果只改 map，

Pipe termination 时：

~~~text
erase_out_pipe(pipe)
~~~

会用旧 key 查不到。

---

# 六十四、这是 Bidirectional Index Invariant

存在：

~~~text
map key → pipe
~~~

同时 Pipe 也保存：

~~~text
pipe → routing id
~~~

必须始终满足：

\[
map[pipe.routing\_id].pipe
=
pipe
\]

---

# 六十五、双向索引优化了什么

从 routing id 找 pipe：

~~~text
map lookup
~~~

从 pipe termination 找 routing id：

~~~text
pipe local field
~~~

避免反向扫描整个 map。

---

# 六十六、代价是要维护一致性

任何：

- rename；
- insert；
- erase；
- handover；

都必须同时更新两边。

这就是双向索引的典型 tradeoff。

---

# 六十七、Routing ID Collision 与数据库 Unique Key 很像

默认：

~~~text
reject duplicate
~~~

handover：

~~~text
rename old row
publish new row
defer old-row reclamation
~~~

但这里还多了：

~~~text
in-flight message transaction
~~~

所以比普通 CRUD 更复杂。

---

# 六十八、为什么 ROUTER 的 `_out_pipes` 用 `std::map`

当前实现是：

~~~text
ordered tree map
~~~

它需要：

~~~text
binary routing id lookup
~~~

而不是 hot-path active-prefix scanning。

这里操作模式是：

~~~text
exact key lookup
~~~

所以数据结构与 FQ/LB 不同。

---

# 六十九、调度器为什么用 Array，而 Routing 用 Map

FQ/LB：

~~~text
iterate candidates
frequent activate/deactivate
order not stable
~~~

适合：

~~~text
array + prefix
~~~

ROUTER：

~~~text
application gives exact key
need exact peer
~~~

适合：

~~~text
map-like index
~~~

---

# 七十、不要为了“统一数据结构”牺牲访问模式

这是源码设计非常好的对照：

~~~text
candidate selection
→ array partition

exact identity routing
→ keyed map
~~~

不是所有“Pipe 集合”都应该用同一种 STL 容器。

---

# 七十一、ROUTER Receive 为什么仍然使用 FQ

虽然 outbound 需要 exact routing，

inbound 并没有要求：

~~~text
应用指定从哪个 peer 收
~~~

所以：

~~~text
all readable peers
→ fair queue
~~~

依然合理。

---

# 七十二、这形成了不对称 Socket

~~~text
receive:
many peers → fair select

send:
explicit id → exact target
~~~

ROUTER 的名字也正来自这种显式 outbound routing。

---

# 七十三、DEALER 则是对称的 Scheduler Composition

~~~text
receive:
FQ

send:
LB
~~~

双方都由 Runtime 选择 peer。

---

# 七十四、DEALER ↔ ROUTER 为什么常被配对

DEALER：

~~~text
我不关心具体 server-side pipe identity
~~~

ROUTER：

~~~text
我要知道消息来自哪个 peer
并能回给它
~~~

ROUTER 接收时注入 routing-id，

应用回包时再把这个 routing-id 放回第一帧。

形成：

~~~text
receive:
peer → routing id + payload

reply:
routing id + payload → same peer
~~~

---

# 七十五、Routing Envelope 其实是 Continuation Token

应用收到：

~~~text
routing id
~~~

之后可以把它保存，

未来作为：

~~~text
“继续和这个 peer 对话”
~~~

的 token。

---

# 七十六、但 Token 不是永久有效

连接断开、handover、lifecycle 变化后：

~~~text
same routing id
~~~

可能：

- 不存在；
- 指向新 connection；
- 暂时 blocked；
- 已退休。

所以 routing id 是：

~~~text
logical lookup token
~~~

不是 raw stable pointer。

---

# 七十七、这正是为什么应用不应该拿 Pipe Pointer

应用只持有：

~~~text
routing id bytes
~~~

Runtime 自己处理：

- lookup；
- lifetime；
- replacement；
- HWM；
- termination。

这是一层很好的 abstraction barrier。

---

# 七十八、Raw Pointer 被限制在 Runtime 内部

内部：

~~~text
routing map
→ pipe*
~~~

但对外：

~~~text
opaque routing id
~~~

这防止应用跨生命周期保存：

~~~text
stale endpoint pointer
~~~

---

# 七十九、Mandatory 模式也不等于 Delivery Guarantee

即使第一帧：

~~~text
routing id exists
+
HWM check passes
~~~

后续 transaction 仍可能：

- peer disconnect；
- pipe terminate；
- transport fail。

---

# 八十、Mandatory 只把“立即不可路由”暴露给应用

它不是：

~~~text
remote application acknowledged
~~~

也不是：

~~~text
payload eventually delivered
~~~

所以不能把：

~~~text
send success
~~~

解释成业务可靠性 ACK。

---

# 八十一、Routing Success 的层级必须分清

可以分成：

~~~text
1. route lookup succeeded
2. local pipe admitted
3. transport accepted
4. remote runtime received
5. remote application consumed
6. remote business action completed
~~~

ROUTER_MANDATORY 主要强化前两层的错误暴露。

---

# 八十二、机器人控制尤其需要这个区分

例如：

~~~text
ROUTER send to motor-2 succeeds
~~~

最多说明：

~~~text
本地 runtime 找到 motor-2 对应 route
且当前 pipe 可接收
~~~

不等于：

~~~text
motor-2 已执行目标电流
~~~

动作确认仍需要：

- sequence；
- device feedback；
- ACK；
- deadline；
- state reconciliation。

---

# 八十三、Disconnect 与 Routing-ID Reuse 的安全边界

如果旧连接断开：

~~~text
erase_out_pipe
~~~

必须先发生，

以后新的相同 ID 才能安全注册。

否则：

~~~text
future lookup
~~~

可能仍命中 stale pointer。

---

# 八十四、这与前面 Callback Registry 的规律一致

共同原则：

~~~text
logical unregister
→ prevent future discovery
~~~

但它仍不自动等于：

~~~text
all in-flight users finished
~~~

---

# 八十五、ROUTER 自己如何处理 In-flight

它通过：

~~~text
_current_out
_current_in
_prefetched_msg
_terminate_current_in
~~~

把当前 transaction 显式保留下来。

所以这里的“quiescence”不是隐藏在 map 中。

---

# 八十六、Map 只解决 Future Discovery

~~~text
erase routing id
~~~

解决：

~~~text
new xsend cannot newly discover old pipe
~~~

当前已经拿到：

~~~text
_current_in/current_out
~~~

的 transaction，

还要单独处理。

---

# 八十七、这是 Registry 与 Transaction 的职责分离

Registry：

~~~text
future lookup
~~~

Transaction state：

~~~text
already admitted work
~~~

Reclamation：

~~~text
when both are done
~~~

---

# 八十八、ROUTER Handover 是一个完整例子

~~~text
future lookup:
X → new B

already admitted inbound:
old A continues current multipart

old lifetime:
terminate A at safe boundary
~~~

三者同时成立。

---

# 八十九、这比 Mutex 能解决的问题更高层

给 `_out_pipes` 加 mutex 只能保证：

~~~text
map mutation不数据竞争
~~~

不能回答：

~~~text
旧 multipart 能不能读完？
新 routing id 何时生效？
旧 pipe 何时可以 terminate？
~~~

这就是：

> **同步正确性 ≠ 协议正确性。**

---

# 九十、为什么 Owner-thread 模型让 ROUTER 简单很多

Socket pattern state：

- `_out_pipes`；
- `_anonymous_pipes`；
- `_current_out`；
- `_current_in`；
- `_prefetched_msg`；
- `_more_in/out`；

基本都在 socket owner thread 内修改。

---

## 90. Cross-thread Pipe 变化通过 Event 回来

例如：

~~~text
write capacity restored
→ pipe event
→ xwrite_activated
~~~

而不是其他线程直接修改：

~~~text
router._out_pipes
~~~

---

# 九十一、所以 Routing Map 不需要变成 Concurrent Map

这是一条很重要的 Runtime 原则：

> **先通过 execution ownership 限制谁能访问，再决定是否需要并发容器。**

---

# 九十二、如果不用 Owner Thread 会发生什么

你可能需要同时保护：

- map；
- anonymous set；
- current transaction；
- prefetch；
- handover；
- pipe termination callback。

然后还要处理：

~~~text
lock ordering
callback reentrancy
lifetime
~~~

复杂度会急剧上升。

---

# 九十三、ROUTER 的 Socket State 可以画成四层

~~~text
Layer 1
Physical Pipe Set

Layer 2
Routing Registry
routing-id → pipe

Layer 3
Scheduler / Availability
FQ + Pipe HWM

Layer 4
Current Message Transaction
current_in/current_out/prefetch/more
~~~

---

# 九十四、每一层回答不同问题

Physical Pipe：

~~~text
连接对象存在吗？
~~~

Routing Registry：

~~~text
这个 logical id 对应谁？
~~~

Availability：

~~~text
现在能不能进展？
~~~

Transaction：

~~~text
当前 message 已经绑定谁？
~~~

---

# 九十五、错误通常来自跨层混淆

例如：

~~~text
map has key
~~~

不等于：

~~~text
pipe writable
~~~

又不等于：

~~~text
peer alive forever
~~~

也不等于：

~~~text
current transaction should switch to latest mapping
~~~

---

# 九十六、ROUTER 的 Readiness 层级

可以写成：

~~~text
socket-level POLLOUT
        ↓
some peer may progress

routing-id lookup
        ↓
target exists

pipe HWM check
        ↓
target currently admits

transaction write
        ↓
current message continues
~~~

---

# 九十七、Global Poller API 的天然局限

`poll()` 通常对：

~~~text
socket object
~~~

给 readiness，

但 ROUTER 的真正资源有：

~~~text
many target pipes
~~~

所以一个 bool readiness 只能做：

~~~text
aggregate summary
~~~

---

# 九十八、这和 GPU Multi-stream / Multi-device 一样

如果一个 runtime 管：

~~~text
GPU0
GPU1
GPU2
~~~

“runtime writable”：

~~~text
至少一个 GPU 能收任务
~~~

不能保证：

~~~text
GPU2 能收任务
~~~

显式 target API 必须额外查询 target state。

---

# 九十九、Routing Handover 对设备重连特别有价值

机器人设备：

~~~text
logical actuator id = arm_joint_3
~~~

旧 CAN/TCP session：

~~~text
connection A
~~~

重连后：

~~~text
connection B
~~~

理想模型不是把业务逻辑绑定：

~~~text
fd / socket pointer
~~~

而是：

~~~text
logical id
→ current session
~~~

---

# 一百、但 Reconnect Replacement 必须考虑 In-flight Command

如果旧 session 正在完成：

~~~text
multi-frame protocol transaction
~~~

直接把 logical id 切到新 session，

旧 transaction 是否：

- finish；
- abort；
- replay；

必须有明确策略。

ROUTER handover 展示了一个：

~~~text
new identity publication
+
old transaction drain
~~~

范式。

---

# 一百零一、`ZMQ_CONNECT_ROUTING_ID` 又展示了 Client-side Naming

有时 server 不应总是：

~~~text
随机生成 peer ID
~~~

应用可以在 connect 前给下一条连接指定：

~~~text
known logical name
~~~

这样建立：

~~~text
stable application routing namespace
~~~

---

# 一百零二、但 Logical Name 需要 Collision Policy

一旦允许应用指定 ID，

就必须定义：

~~~text
duplicate identity
~~~

怎么处理。

只有：

~~~text
set identity
~~~

没有：

~~~text
collision / handover / lifetime
~~~

协议是不完整的。

---

# 一百零三、路由表就是一种名字服务

从系统设计视角：

~~~text
routing id
→ live endpoint
~~~

本质上类似小型：

~~~text
name service / session registry
~~~

---

# 一百零四、名字服务至少要处理四件事

- registration；
- lookup；
- replacement；
- unregister。

ROUTER 还多：

- message transaction pinning；
- HWM；
- asynchronous termination。

---

# 一百零五、ROUTER 的 Prefetch 与 Handover 为什么会相互作用

旧 pipe 如果已经：

~~~text
prefetched payload
~~~

这条 payload 已经离开 Pipe queue，

但尚未全部交给 application。

---

## 105. 这时立即 terminate old pipe 仍可能破坏 API transaction

所以：

~~~text
_current_in
~~~

不仅代表：

~~~text
pipe queue cursor
~~~

还代表：

~~~text
API-visible multipart continuity
~~~

---

# 一百零六、Prefetch 会延长 Message Ownership

底层 queue ownership：

~~~text
already transferred
~~~

但 application ownership：

~~~text
not yet transferred
~~~

中间由：

~~~text
router socket prefetch
~~~

持有。

---

# 一百零七、Runtime Layer 本身也可以成为 Temporary Owner

Ownership 不一定只有：

~~~text
producer
consumer
~~~

中间的 adapter / protocol layer 也可能成为：

~~~text
temporary strong owner
~~~

---

# 一百零八、这与 LCM C++ Binding 的问题形成反例

安全做法：

~~~text
adapter/runtime keeps object alive
until callback/message view completes
~~~

危险做法：

~~~text
logical unregister
→ immediately delete backing owner
~~~

ROUTER prefetch 明确把 ownership 延长到 API view 完成。

---

# 一百零九、ROUTER 的 Routing-ID Frame 本身也有 Ownership

它通过：

~~~text
msg_t::init_size
memcpy routing id
~~~

创建独立 message representation。

不是简单返回：

~~~text
pointer into map key
~~~

---

## 109. 为什么不返回 Map Key View

因为：

- map 可能变化；
- handover 可能 rename；
- connection 可能 terminate；
- API message 可以跨当前函数调用存在。

所以必须：

~~~text
copy stable routing-id bytes into msg_t
~~~

---

# 一百一十、Small Metadata Copy 换来清晰 Lifetime

Routing ID 通常很小。

为了避免：

~~~text
borrowed map-key lifetime
~~~

直接复制进 API message 是很合理的成本。

---

# 一百一十一、Zero-copy 不应教条化

对大 payload：

~~~text
share/refcount
~~~

对几十字节 routing id：

~~~text
copy
~~~

往往更简单、更安全、更快。

---

# 一百一十二、ROUTER Pattern 的三种“选择”

第一种：

~~~text
Inbound peer selection
→ FQ decides
~~~

第二种：

~~~text
Outbound logical target
→ application decides
~~~

第三种：

~~~text
Physical connection for logical target
→ routing registry decides
~~~

---

# 一百一十三、把三者混在一起会导致设计混乱

比如：

~~~text
application directly holds connection pointer
~~~

就把第二、三层合并了。

一旦 reconnect/handover：

~~~text
pointer失效
~~~

业务层也必须重建状态。

---

# 一百一十四、Logical Routing ID 是解耦层

业务：

~~~text
send to robot_arm
~~~

Runtime：

~~~text
robot_arm
→ current live pipe
~~~

Transport：

~~~text
current live pipe
→ current engine/fd
~~~

每层都可替换。

---

# 一百一十五、但 Routing ID 不应被误解成 Security Identity

一个 routing id：

~~~text
能用于 lookup
~~~

不自动证明：

- peer authenticated；
- peer authorized；
- peer is who it claims；
- identity cannot be spoofed。

安全认证属于：

~~~text
mechanism / ZAP / transport security
~~~

另一层。

---

# 一百一十六、Naming 与 Authentication 必须分层

机器人网络也一样：

~~~text
device name
≠
cryptographic identity
~~~

如果业务把：

~~~text
routing id == trusted device
~~~

会埋安全问题。

---

# 一百一十七、ROUTER 的错误语义可以整理成矩阵

~~~text
target exists?
target writable?
mandatory?
          |
          v

no / * / false
→ silently drop

no / * / true
→ EHOSTUNREACH

yes / no(HWM) / false
→ drop

yes / no(HWM) / true
→ EAGAIN

yes / yes / *
→ begin payload transaction
~~~

---

# 一百一十八、这比一句“mandatory 更可靠”准确得多

`mandatory` 做的是：

~~~text
turn some silent drops
into synchronous local errors
~~~

不是端到端可靠传输。

---

# 一百一十九、为什么 HWM Error 是 EAGAIN

因为：

~~~text
target identity仍有效
~~~

只是：

~~~text
temporary capacity unavailable
~~~

这属于：

~~~text
retryable resource condition
~~~

---

# 一百二十、为什么 Unknown Peer 是 EHOSTUNREACH

因为：

~~~text
当前 routing registry
没有可达目标
~~~

不是简单等一点 queue capacity 就一定恢复。

---

# 一百二十一、错误码其实编码恢复策略

~~~text
EAGAIN
→ maybe retry later

EHOSTUNREACH
→ routing/lifecycle problem
~~~

好的 Runtime API 应尽量让：

~~~text
failure class
~~~

映射到：

~~~text
caller action
~~~

---

# 一百二十二、但重试也必须在 Message Boundary

如果 routing-id frame已经接受，

后续 multipart 失败，

不能：

~~~text
重新 lookup another connection
~~~

然后继续 suffix。

---

# 一百二十三、Transaction Recovery 必须以整条 Message 为单位

可以：

- rollback unpublished frames；
- abort remaining frames；
-让应用重发整条 message。

不能：

~~~text
重新路由一半 message
~~~

---

# 一百二十四、ROUTER 与 LB 在这里再次统一

不同 routing policy，

相同 transaction rule：

~~~text
message atomicity
beats mid-message rescheduling
~~~

---

# 一百二十五、对机器人协议设计的直接迁移

假设：

~~~text
routing id = motor_controller_3
~~~

消息：

~~~text
[target]
[command header more]
[trajectory chunk more]
[footer last]
~~~

如果控制器 3 中途重连，

不要把：

~~~text
footer
~~~

发到新 session，

除非协议明确支持：

~~~text
transaction migration
~~~

---

# 一百二十六、默认应该 Abort，而不是“尽量发完”

因为：

~~~text
half old session
+
half new session
~~~

通常比整条失败更危险。

---

# 一百二十七、设备 Handover 应该有 Epoch / Sequence

ROUTER 自身只保证 socket message transaction 的粘性。

更复杂机器人协议可以再加入：

~~~text
device epoch
command sequence
session generation
~~~

让新 session 能拒绝旧 generation command。

---

# 一百二十八、这就是 Runtime 与业务 Protocol 的边界

Runtime 提供：

- logical routing；
- message atomicity；
- backpressure；
- connection handover primitive。

业务仍需定义：

- command idempotency；
- action ACK；
- replay；
- epoch；
- safety state。

---

# 一百二十九、完整 Outbound 路径

~~~text
application
    |
    | frame 0 = routing id
    v
router_t::xsend()
    |
    v
lookup_out_pipe(id)
    |
    +---- missing ----------------------+
    |                                   |
    | mandatory                         | nonmandatory
    |                                   |
    v                                   v
EHOSTUNREACH                         current_out=NULL
                                        |
                                        v
                                  drop payload frames

found
 |
 v
pipe->check_write()
 |
 +---- HWM/full ----+
 |                  |
 | mandatory        | nonmandatory
 |                  |
 v                  v
EAGAIN             drop
 |
 v
retry later
~~~

正常路径：

~~~text
routing id
    ↓
_current_out = pipe
    ↓
payload frame 1 [more]
    ↓
same pipe
    ↓
payload frame 2 [more]
    ↓
same pipe
    ↓
payload final
    ↓
flush
    ↓
_current_out = NULL
~~~

---

# 一百三十、完整 Inbound 路径

~~~text
peer pipe
   |
   v
fq_t::recvpipe()
   |
   v
real payload
   |
   +--> move to _prefetched_msg
   |
   +--> read pipe routing id
            |
            v
      construct synthetic
      routing-id msg_t
            |
            v
application recv #1
→ routing id [more]

application recv #2
→ prefetched payload

remaining multipart
→ directly continue from same _current_in
~~~

---

# 一百三十一、完整 Handover 路径

~~~text
old:
X → pipe A

new pipe B arrives
claims X
        |
        v
handover enabled?
   /          \
  no          yes
  |            |
reject B       |
               v
       generate internal Y
               |
               v
       erase X → A
               |
               v
       A.routing_id = Y
               |
               v
       add Y → A
               |
               v
       add X → B
               |
          +----+----+
          |         |
     A current_in?  no
          |         |
         yes        |
          |         |
          v         v
 defer terminate   terminate A
 until multipart
 boundary
~~~

---

# 一百三十二、这里最值得迁移的十条规则

第一：

> **逻辑 Routing ID 与物理 connection / fd 必须分层。**

第二：

> **精确目标路由与“任意选一个可用资源”是两种不同 scheduler，数据结构也应不同。**

第三：

> **routing registry 只控制 future discovery；已经开始的 message transaction 必须有独立 pinned state。**

第四：

> **multipart 一旦选定 endpoint，中途不得因为 map 更新、重连或 HWM 变化重新选目标。**

第五：

> **global writable 只代表某个资源可能可写；显式 target system 还需要 target-specific readiness。**

第六：

> **logical unregister / map erase 不等于 in-flight transaction quiescence。**

第七：

> **identity handover 不应直接覆盖旧 pointer；先切 future identity，再让旧 transaction 到安全边界，最后 reclaim。**

第八：

> **双向索引（id→pipe 与 pipe→id）换来快速 lookup/erase，但 rename 时必须维持一致性。**

第九：

> **API envelope 可以由 Runtime 合成；一旦跨 API 调用保存 payload，必须保存完整 ownership object，而不是裸指针。**

第十：

> **mandatory routing 只是把一部分 local silent drop 变成错误，不是端到端 delivery guarantee。**

---

# 一百三十三、最终心智模型

DEALER：

~~~text
             many peers
             /       \
            /         \
       inbound       outbound
          |             |
         FQ             LB
          |             |
   fair complete    choose one
      message       writable peer
~~~

ROUTER：

~~~text
                 many physical pipes
                        |
         +--------------+--------------+
         |                             |
      inbound                       outbound
         |                             |
        FQ                      routing-id map
         |                             |
   select source                 exact lookup
         |                             |
   synthesize id                pin _current_out
         |                             |
 [id][payload...]             multipart payload
         |                             |
         +-------------+---------------+
                       |
               message transaction
                       |
         lifecycle / HWM / handover
~~~

如果只记一个结论：

> **ROUTER 的本质不是“给消息前面加一个 identity frame”，而是维护一个从逻辑身份到物理 Pipe 的生命周期注册表，并在每条完整 multipart message 开始时把路由选择冻结成 transaction；map 更新决定未来消息去哪里，`_current_in/_current_out` 决定已经开始的消息怎样安全结束。**
