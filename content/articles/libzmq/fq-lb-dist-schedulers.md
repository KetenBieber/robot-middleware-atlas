# FQ / LB / DIST：Active Prefix、Multipart 原子性与消息调度器

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

ZeroMQ 的 socket pattern 表面上很多：

- PUSH / PULL；
- DEALER / ROUTER；
- PUB / SUB；
- XPUB / XSUB；
- CLIENT / SERVER；
- RADIO / DISH。

但真正决定“从哪条 Pipe 收”“向哪条 Pipe 发”“一份消息发给哪些 Pipe”的核心调度机制，高度集中在三个小类：

~~~text
fq_t
→ fair queue
→ 多输入选择

lb_t
→ load balancer
→ 多输出单选

dist_t
→ distributor
→ 多输出多选
~~~

它们代码量不大，但里面有一组非常值得迁移的 Runtime 设计：

~~~text
连续数组
+
prefix partition
+
intrusive index
+
O(1) activate/deactivate
+
message-level cursor
+
multipart transaction boundary
+
backpressure-driven membership
+
shared-payload fan-out
~~~

所以这篇真正要回答的问题不是：

> FQ、LB、DIST 各自有哪些成员函数？

而是：

> **一个高频消息 Runtime，怎样把“资源可用性、选择集合、消息原子性和背压”编码成极少量连续状态，从而避免通用容器、重复搜索和跨层语义破坏？**

---

# 一、先从一个共同结构开始：Array + Boundary + Cursor

三个调度器都没有用：

~~~text
std::list
std::set
priority_queue
unordered_set
~~~

它们大量依赖：

~~~text
array_t<pipe_t, ID>
~~~

也就是：

~~~text
std::vector<pipe_t*>
+
每个 pipe 自己记住 array index
~~~

然后用几个整数边界：

~~~text
_active
_matching
_eligible
_current
~~~

解释同一块连续数组。

---

## 1. `array_t` 为什么能 O(1) 找到元素位置

普通 `std::vector<T*>`：

~~~text
erase(pipe)
~~~

如果只有 pointer，

通常要：

~~~text
linear search
~~~

先找到 index。

`array_t` 反过来让元素继承：

~~~cpp
array_item_t<ID>
~~~

元素自己保存：

~~~cpp
int _array_index;
~~~

所以：

~~~text
pipe
→ get_array_index()
→ O(1) location
~~~

---

## 2. `push_back()` 会把 Index 写回对象

~~~cpp
void push_back(T *item)
{
    item->set_array_index(
        _items.size());

    _items.push_back(item);
}
~~~

所以：

~~~text
container owns membership
object owns its location metadata
~~~

这是一种 intrusive container。

---

## 3. erase 为什么 O(1)

~~~cpp
_items[index] = _items.back();
_items.back()->set_array_index(index);
_items.pop_back();
~~~

也就是：

~~~text
[A B C D E]
     ^
   remove C
~~~

变成：

~~~text
[A B E D]
~~~

不保持原顺序，

但删除只需要：

~~~text
copy last pointer
update one index
pop_back
~~~

---

## 4. 这类容器的前提：不要求稳定顺序

如果业务要求：

~~~text
严格 insertion order
~~~

这种 erase 不合适。

但消息调度器真正关心：

~~~text
active / inactive
matching / nonmatching
current round-robin cursor
~~~

不关心数组物理顺序。

所以：

> **放弃稳定顺序，换 O(1) membership transition。**

---

# 二、为什么 `pipe_t` 要同时继承三份 array_item_t

源码：

~~~cpp
class pipe_t :
    public object_t,
    public array_item_t<1>,
    public array_item_t<2>,
    public array_item_t<3>
~~~

注释也明确：

~~~text
pipe can be stored in three arrays:

1. inbound pipes
2. outbound pipes
3. generic deallocation pipes
~~~

---

## 5. 一个 Index 不够

同一个 pipe 可能同时属于：

~~~text
inbound scheduler
+
outbound scheduler
+
lifecycle/deallocation registry
~~~

如果只有：

~~~text
one _array_index
~~~

三个容器会互相覆盖。

---

## 6. Template ID 就是“多重 Intrusive Slot”

~~~text
array_item_t<1>
→ inbound slot

array_item_t<2>
→ outbound slot

array_item_t<3>
→ generic lifecycle slot
~~~

这比在 `pipe_t` 手写：

~~~text
fq_index
lb_index
reaper_index
~~~

更通用。

---

## 7. FQ 与 LB/DIST 使用不同 ID

`fq_t`：

~~~cpp
typedef array_t<pipe_t, 1> pipes_t;
~~~

`lb_t`：

~~~cpp
typedef array_t<pipe_t, 2> pipes_t;
~~~

`dist_t`：

~~~cpp
typedef array_t<pipe_t, 2> pipes_t;
~~~

含义是：

~~~text
一条 pipe
可以同时参加 inbound scheduler
和某一种 outbound scheduler
~~~

而一个 socket pattern 通常不会让同一 outbound pipe 同时受：

~~~text
LB + DIST
~~~

两套输出调度器管理。

所以它们可以共享 ID 2。

---

# 三、Prefix Partition：把集合 Membership 编码成数组区间

最重要的技巧是：

> **不维护多个容器，而是用一块数组 + 边界，把不同逻辑集合编码成连续区间。**

---

# 四、FQ：一个 Active Prefix 就够了

`fq_t`：

~~~text
_pipes
_active
_current
_more
~~~

数组：

~~~text
0                       _active               size
|-------------------------|--------------------|
|      active pipes       |   inactive pipes   |
|-------------------------|--------------------|
~~~

---

## 8. Active 的含义

不是：

~~~text
连接还活着
~~~

而是：

~~~text
当前值得继续尝试 read
~~~

具体 condition 来自 `pipe_t`：

~~~text
queue has data
or
pipe state allows reading
~~~

---

## 9. 某 Pipe 读空后怎样失活

`recvpipe()`：

~~~cpp
if (!pipe->read(msg))
{
    _active--;
    _pipes.swap(
        _current,
        _active);

    if (_current == _active)
        _current = 0;
}
~~~

假设：

~~~text
[A B C | D E]
     ^
 current=C
 active=3
~~~

C 读空：

~~~text
[A B | C D E]
~~~

只需：

~~~text
active--
+
swap
~~~

---

## 10. 为什么不从 Vector 里 erase

因为 C 只是：

~~~text
暂时无数据
~~~

不是：

~~~text
生命周期结束
~~~

它还应该留在调度器中等待：

~~~text
activate_read
~~~

未来恢复。

所以：

~~~text
inactive
!=
removed
~~~

---

# 五、FQ 的 Fairness Unit 是 Message，不是 Frame

这是理解 `_more` 的关键。

假设有：

~~~text
Pipe A:
A1 [more]
A2 [more]
A3 [last]

Pipe B:
B1 [last]
~~~

如果按 frame round-robin：

~~~text
A1
B1
A2
A3
~~~

上层会看到：

~~~text
multipart A 被 B 插入
~~~

消息边界被破坏。

---

## 11. FQ 在 multipart 中不推进 `_current`

源码：

~~~cpp
_more =
  (msg->flags()
   & msg_t::more) != 0;

if (!_more)
    _current =
      (_current + 1) % _active;
~~~

所以：

~~~text
A1 [more]
→ current stays A

A2 [more]
→ current stays A

A3 [last]
→ current moves to B
~~~

---

## 12. Round-robin 的真正单位

不是：

~~~text
frame
~~~

而是：

~~~text
complete multipart message
~~~

因此 `fq_t` 更准确可以理解成：

> **message-granularity fair scheduler。**

---

# 六、为什么 multipart 中途读不到下一帧会 assert

源码：

~~~cpp
if (!fetched)
{
    zmq_assert(!_more);
    ...
}
~~~

如果 `_more=true`，

表示：

~~~text
上一帧已经声明：
后面还有当前 message 的 frame
~~~

而 ypipe publication 又保证：

~~~text
incomplete multipart
不会在最后 frame 之前暴露给 reader
~~~

所以：

~~~text
读到 A1[more]
但 A2 突然 unavailable
~~~

不是普通暂时无数据。

它意味着底层 atomic publication invariant 被破坏。

---

## 13. FQ 依赖 Pipe/Ypipe 的更强 Contract

上层 FQ 可以写：

~~~text
assert remaining frame immediately available
~~~

是因为下层保证：

~~~text
multipart publication is atomic
~~~

这就是 abstraction contract 的价值。

---

# 七、`has_in()` 为什么也会修改调度状态

它不只是 query：

~~~cpp
while (_active > 0)
{
    if (_pipes[_current]
          ->check_read())
        return true;

    deactivate current pipe;
}
~~~

所以：

~~~text
has_in()
~~~

会主动清理：

~~~text
stale active membership
~~~

---

## 14. Query 也可以是 State Maintenance

这在 Runtime 中很常见：

~~~text
is_ready()
has_work()
poll_ready()
~~~

为了给出准确结果，

可能顺便：

~~~text
purge dead entries
deactivate unavailable resource
advance cursor
~~~

因此不能机械认为：

~~~text
名字是 has_xx
→ 一定 const/pure
~~~

---

# 八、`activated()` 怎样让 Pipe O(1) 回到 Active Prefix

~~~cpp
_pipes.swap(
    _pipes.index(pipe),
    _active);

_active++;
~~~

假设：

~~~text
[A B | C D E]
           ^
           D becomes readable
~~~

如果 D 当前 index=3：

~~~text
swap(D, index 2)
active++
~~~

得到：

~~~text
[A B D | C E]
~~~

不需要：

- erase；
- insert；
- list splice；
- tree rebalance。

---

# 九、FQ Fairness 的真实边界

`_current` round-robin 可以防止：

~~~text
one sender continuously ready
~~~

永久占住读取路径。

源码注释直接说明目的：

~~~text
senders gone berserk
should not cause denial of service
for decent senders
~~~

---

## 15. 但这不是字节级公平

如果 A 一条 message：

~~~text
10 MB multipart
~~~

B 一条 message：

~~~text
10 B
~~~

FQ 仍按：

~~~text
one complete message
~~~

轮换。

所以公平单位决定公平语义。

---

## 16. Message Fairness 不等于 Byte Fairness

这是任何 scheduler 都必须明确的问题：

~~~text
what is one quantum?
~~~

可能是：

- request；
- packet；
- byte；
- frame；
- complete message；
- CPU time。

不同 quantum 会产生完全不同公平性。

---

# 十、LB：输出端同样使用 Active Prefix + Cursor

`lb_t`：

~~~text
_pipes
_active
_current
_more
_dropping
~~~

结构：

~~~text
0                       _active               size
|-------------------------|--------------------|
|      writable pipes     | blocked/inactive   |
|-------------------------|--------------------|
~~~

---

## 17. 为什么 `_current` 只在完整消息结束后推进

逻辑：

~~~cpp
_more =
  (msg->flags()
   & msg_t::more) != 0;

if (!_more)
{
    pipe->flush();

    if (++_current >= _active)
        _current = 0;
}
~~~

因此：

~~~text
multipart
→ pinned to one pipe
~~~

---

# 十一、LB 不是“每个 Frame 轮流发送”

假设：

~~~text
A1 [more]
A2 [more]
A3 [last]
~~~

如果：

~~~text
A1 → Pipe 0
A2 → Pipe 1
A3 → Pipe 2
~~~

每个 peer 都只收到残片。

所以 LB 的调度 transaction 是：

~~~text
one complete message
~~~

---

# 十二、为什么 `has_out()` 在 `_more=true` 时直接返回 true

源码：

~~~cpp
if (_more)
    return true;
~~~

表面看似：

~~~text
为什么不再 check HWM？
~~~

原因是：

> **multipart 已经开始，就不能把后续 frame 改调度到另一条 pipe。**

---

## 18. 已开始的 Transaction 优先于普通 Admission

第一帧被接受以后：

~~~text
destination selected
~~~

接下来的 frame 必须维持：

~~~text
transaction affinity
~~~

不能重新做普通 load balancing。

---

# 十三、如果 Multipart 中途 Pipe 失败怎么办

有两种不同情形：

## 情形 A：Pipe 仍存在，但 write 失败

`lb_t`：

~~~cpp
if (_more)
{
    pipe->rollback();
    ...
    errno = EAGAIN;
    return -2;
}
~~~

---

## 19. rollback() 的语义

只撤销：

~~~text
尚未形成完整 publication boundary
~~~

的 multipart tail。

也就是：

~~~text
这些 frame peer 还看不到
~~~

因此可以安全回滚。

---

## 20. 情形 B：当前 Pipe 已被 terminate

`pipe_terminated()`：

~~~cpp
if (index == _current
    && _more)
{
    _dropping = true;
}
~~~

这时剩余 multipart frame 已经不能送给原 peer。

---

# 十四、为什么不能把剩余 Frame 换一个 Pipe

如果：

~~~text
frame 1
frame 2
~~~

已属于 peer A，

然后：

~~~text
frame 3
~~~

改送 peer B，

B 会收到：

~~~text
一条没有开头的 message
~~~

所以唯一可维护原子性的行为是：

~~~text
drop remainder
~~~

---

## 21. `_dropping` 是 Transaction Abort State

进入后：

~~~cpp
if (_dropping)
{
    _more =
      msg->flags() & more;

    _dropping = _more;

    msg->close();
    msg->init();
    return 0;
}
~~~

也就是：

~~~text
consume application frames
but do not deliver
until multipart ends
~~~

---

## 22. 为什么返回 success-like 路径

这是历史/API compatibility 语义的一部分。

源码注释也承认：

~~~text
application may not be told
that delivery failed
without breaking compatibility
~~~

所以这部分不能简单抽象成：

~~~text
reliable delivery guarantee
~~~

---

# 十五、LB 的 Backpressure 如何进入 Active Prefix

普通发送：

~~~cpp
while (_active > 0)
{
    if (pipe->write(msg))
        break;

    _active--;

    move pipe out of active prefix;
}
~~~

write 失败通常意味着：

~~~text
HWM
or
pipe no longer writable
~~~

所以：

~~~text
capacity state
→ membership state
~~~

---

# 十六、Peer Progress 如何重新激活 LB Pipe

前文的 Pipe HWM 协议：

~~~text
reader consumes batch
→ activate_write
→ pipe._out_active=true
→ sink->write_activated(pipe)
~~~

对于 DEALER/PUSH 等：

~~~cpp
_lb.activated(pipe);
~~~

于是：

~~~text
inactive suffix
→ active prefix
~~~

---

# 十七、LB 与 Pipe 形成两级状态机

Pipe：

~~~text
local resource condition
~~~

LB：

~~~text
global candidate set
~~~

链条：

~~~text
Pipe HWM full
    ↓
pipe inactive
    ↓
LB removes candidate
~~~

恢复：

~~~text
peer progress
    ↓
pipe active
    ↓
LB adds candidate
~~~

---

# 十八、为什么 Scheduler 不自己读 HWM Counter

因为这样会让：

~~~text
lb_t
~~~

依赖：

~~~text
pipe internal capacity implementation
~~~

现在它只看：

~~~text
pipe->write()
pipe->check_write()
activated(pipe)
~~~

机制与策略分层更干净。

---

# 十九、DIST 比 FQ/LB 多了一层：Matching

PUB/XPUB 之类不是：

~~~text
从 N 个 candidate 中选 1
~~~

而是：

~~~text
从 N 个 pipe 中挑出 matching subset
然后 fan-out
~~~

所以需要更多区间。

---

# 二十、DIST 的四段数组

不变量：

\[
0
\le
\_matching
\le
\_active
\le
\_eligible
\le
|\_pipes|
\]

数组：

~~~text
0          matching        active        eligible          size
|-------------|--------------|--------------|---------------|
|  matching   | active non-  | eligible but |   passive     |
|             | matching     | not active   |               |
|-------------|--------------|--------------|---------------|
~~~

更精确地：

~~~text
[0, matching)
→ 当前这条 message 的目标

[matching, active)
→ 当前可写，但本次没选中

[active, eligible)
→ 理论上下一条完整 message 可参与，
  但本条 multipart 已经开始，不能中途加入

[eligible, size)
→ passive / HWM blocked
~~~

---

# 二十一、为什么需要 `eligible`，不能只有 active/passive

这是 DIST 最值得学的地方。

假设一条 multipart：

~~~text
frame 1 [more]
frame 2 [more]
frame 3 [last]
~~~

frame 1 发送时：

~~~text
Pipe A
Pipe B
~~~

是 active matching。

frame 2 之前：

~~~text
Pipe C 刚 attach
~~~

---

## 23. C 能不能立刻 active？

不能。

否则：

~~~text
C 从 frame 2 开始收到
~~~

它会看到：

~~~text
缺少 frame 1 的残缺 message
~~~

---

## 24. C 不是 passive

因为：

~~~text
C 本身完全可写
~~~

只是：

~~~text
当前 transaction 已经开始
不具备中途加入资格
~~~

所以需要第三种状态：

~~~text
eligible
~~~

---

# 二十二、Eligible 的真正语义

不是：

~~~text
现在可发送
~~~

而是：

> **下一条完整消息开始时，可以成为 active candidate。**

这是 transaction-aware scheduling state。

---

# 二十三、attach() 在 Multipart 中的行为

~~~cpp
if (_more)
{
    push_back(pipe);
    swap(pipe, _eligible);
    _eligible++;
}
else
{
    push_back(pipe);
    swap(pipe, _active);
    _active++;
    _eligible++;
}
~~~

所以：

~~~text
during multipart
→ eligible only

between messages
→ active + eligible
~~~

---

# 二十四、刚从 HWM 恢复的 Pipe 也一样

`activated()`：

~~~cpp
if (_eligible < size)
{
    move pipe to eligible boundary;
    _eligible++;
}

if (!_more
    && _active < size)
{
    move it to active boundary;
    _active++;
}
~~~

如果当前 multipart 正在进行：

~~~text
只能恢复到 eligible
~~~

不能直接 active。

---

# 二十五、完整 Message 结束后统一提升

`send_to_matching()`：

~~~cpp
if (!msg_more)
    _active = _eligible;

_more = msg_more;
~~~

最后一帧结束：

~~~text
current transaction closes
~~~

此时：

~~~text
all eligible
→ active
~~~

可以参与下一条 message。

---

# 二十六、这是一个非常典型的“Generation Boundary”

multipart 开始以后，

当前参与集合相当于被冻结。

期间新恢复的资源只能进入：

~~~text
next generation
~~~

最后一帧：

~~~text
generation commit
~~~

然后：

~~~text
next generation becomes current
~~~

---

# 二十七、这种模式在很多系统里都存在

例如：

- barrier epoch；
- render frame；
- distributed transaction；
- GPU command batch；
- robotics control cycle；
- collective communication group。

共同原则：

> **一个 transaction 开始后，参与者集合不能任意中途变化；新参与者进入下一代。**

---

# 二十八、DIST 的 `match()` 也不建立新容器

~~~cpp
if index < _matching:
    already matching

if index >= _eligible:
    ignore

swap(index, _matching)
_matching++
~~~

所以：

~~~text
match result
~~~

直接编码在：

~~~text
[0, _matching)
~~~

---

## 29. 为什么 passive pipe 不能 match

如果：

~~~text
index >= _eligible
~~~

说明：

~~~text
HWM blocked / unavailable
~~~

即使 topic 语义匹配，

它也没有本次发送资格。

所以：

~~~text
semantic match
+
resource eligibility
~~~

必须同时满足。

---

# 二十九、Selection 不是单一维度

一个 pipe 要收到一条 PUB message，

至少需要：

~~~text
subscription/topic match
AND
capacity eligible
AND
transaction membership valid
~~~

这三个维度被 DIST 的区间状态共同编码。

---

# 三十、`reverse_match()` 为什么也能原地完成

它先记：

~~~text
prev_matching
~~~

然后：

~~~text
matching = 0
~~~

再把：

~~~text
[prev_matching, eligible)
~~~

逐步 swap 到前缀。

所以：

~~~text
eligible set - previous matching set
~~~

变成新的 matching。

---

## 30. Set Complement 不一定要建 Hash Set

因为所有 candidate 已经在：

~~~text
[0, eligible)
~~~

这个 universe 内。

只要 previous matching 又是 prefix，

补集就是：

~~~text
[prev_matching, eligible)
~~~

天然连续。

---

# 三十一、这就是 Partition-based Algorithm 的威力

当集合关系被物理布局编码：

~~~text
set union
set complement
activate
deactivate
match
unmatch
~~~

很多操作都变成：

~~~text
boundary move
+
swap
~~~

而不是通用集合算法。

---

# 三十二、Pipe 终止时为什么要依次修正三个 Boundary

`pipe_terminated()`：

~~~text
if in matching:
    matching--

if in active:
    active--

if in eligible:
    eligible--

erase
~~~

不能只：

~~~text
erase pointer
~~~

因为数组区间就是逻辑状态。

---

## 33. Boundary 是数据结构不变量的一部分

任何 remove 必须保持：

\[
matching
\le
active
\le
eligible
\le
size
\]

否则下一次发送会把：

~~~text
passive pipe
~~~

误当 active，

或者把：

~~~text
nonmatching pipe
~~~

误当 matching。

---

# 三十三、DIST 写失败时为什么连续做三次 swap

源码：

~~~cpp
if (!pipe->write(msg))
{
    move out of matching;
    matching--;

    move out of active;
    active--;

    move out of eligible;
    eligible--;

    return false;
}
~~~

这不是啰嗦。

失败 pipe 要从：

~~~text
matching
active
eligible
~~~

三个嵌套集合全部退出。

---

# 三十四、Nested Prefix Set 的成员关系

如果一个元素：

~~~text
index < matching
~~~

则一定同时：

~~~text
index < active
index < eligible
~~~

所以：

~~~text
matching ⊆ active ⊆ eligible
~~~

失败后要逐层移出。

---

# 三十五、为什么每次 swap 后都重新取 `_pipes.index(pipe)`

因为第一次 swap：

~~~text
pipe physical index changed
~~~

所以后续不能继续使用旧 index。

intrusive index 会被 swap 自动更新，

下一层直接：

~~~text
_pipes.index(pipe)
~~~

取得新位置。

这正是 intrusive index 的价值。

---

# 三十六、DIST 的 Fan-out 不是“循环 N 次深拷贝”

消息类型决定成本。

---

# 三十七、VSM：小消息内联在 `msg_t`

`msg_t` 固定：

~~~text
64 bytes
~~~

VSM payload 直接内联在这个 representation 中。

所以分发到多个 pipe 时：

~~~text
copy msg_t representation
~~~

也就带着内联小 payload 一起复制。

---

# 三十八、大消息：Payload 在共享 `content_t`

long/zero-copy message：

~~~text
msg_t handle
    ↓
content_t*
    ↓
payload
refcount
~~~

多个 pipe 可以复制：

~~~text
message representation / handle
~~~

同时共享大 payload。

---

# 三十九、DIST 先一次性预留引用

~~~cpp
msg->add_refs(
    _matching - 1);
~~~

为什么减 1？

因为当前原始 `msg_t` 已经代表：

~~~text
one reference
~~~

若要给 N 条 pipe：

~~~text
total N refs
~~~

只需新增：

~~~text
N - 1
~~~

---

# 四十、然后逐 Pipe 写入

~~~text
for each matching pipe
    write(pipe, msg)
~~~

成功 pipe：

~~~text
持有一个 message representation
+
共享 payload reference
~~~

---

# 四十一、失败时为什么 `rm_refs(failed)`

一开始按：

~~~text
所有 matching pipe 都会成功
~~~

预留了 refs。

如果 F 条失败：

~~~text
这些 refs 不再需要
~~~

必须：

~~~cpp
msg->rm_refs(failed);
~~~

否则 payload 永远多 F 个虚假 owner。

---

# 四十二、这是“Optimistic Bulk Reservation + Repair”

模式：

~~~text
1. 预估全部成功
2. 一次性建立 ownership debt
3. 执行 fan-out
4. 对失败数量做补偿
~~~

比每条成功后都：

~~~text
atomic refcount++
~~~

可以减少部分热点操作。

---

# 四十三、但 VSM 为什么不用 Refcount

因为 payload 已经：

~~~text
inline in msg_t
~~~

每个 pipe 拿自己的 representation copy 即可。

没有共享 heap payload owner。

---

# 四十四、Fan-out 成本应该拆开看

假设 N 个订阅者。

真实成本不是简单：

\[
N \times payload\_size
\]

而更像：

\[
C
=
C_{\text{matching}}
+
N \cdot C_{\text{msg-handle}}
+
C_{\text{refcount}}
+
C_{\text{downstream transport}}
\]

大 payload 在 ZeroMQ 内部 Pipe fan-out 阶段不必 N 次 memcpy。

---

# 四十五、但最终网络发送仍可能复制 N 次

共享 payload 只说明：

~~~text
middleware internal ownership
~~~

不等于：

~~~text
one network packet magically serves all peers
~~~

不同 TCP connection 最终仍各有自己的 transport path。

所以要区分：

~~~text
in-process fan-out copy
vs
network transmission copy
~~~

---

# 四十六、`msg_t::add_refs()` 为什么只对 LMSG/ZC 特别处理

VSM：

~~~text
representation contains data
~~~

constant/delimiter 等也可以直接复制 representation。

真正共享 payload storage 的主要是：

~~~text
LMSG
ZCMSG
~~~

所以只有它们需要 refcount。

---

# 四十七、Ownership Optimisation 依赖 Message Representation

这也是为什么：

~~~text
消息对象布局
~~~

和：

~~~text
scheduler fan-out
~~~

不能完全分开理解。

DIST 的性能来自：

~~~text
msg_t storage model
+
pipe ownership model
+
scheduler selection model
~~~

三者协同。

---

# 四十八、DIST 没有 Matching Pipe 时为什么直接消费消息

~~~cpp
if (_matching == 0)
{
    msg->close();
    msg->init();
    return;
}
~~~

也就是说：

~~~text
no subscriber target
→ message considered consumed by distributor
~~~

---

## 34. 为什么不是 EAGAIN

PUB 型语义通常是：

~~~text
没有目标
→ drop
~~~

不是：

~~~text
等待未来 subscriber
~~~

这就是 pattern policy。

DIST 只是实现这一策略的一部分。

---

# 四十九、`dist_t::has_out()` 为什么永远 true

~~~cpp
bool dist_t::has_out()
{
    return true;
}
~~~

这乍看很奇怪。

原因是 Distributor 模型允许：

~~~text
无可写 target
→ drop
~~~

所以从 PUB 发送 API 的角度：

~~~text
永远可以“处理”一条消息
~~~

不代表：

~~~text
一定有 subscriber 收到
~~~

---

# 五十、这再次说明 Ready 的语义取决于 Pattern

LB：

~~~text
has_out
→ 至少一条可写 pipe
~~~

DIST：

~~~text
has_out
→ runtime 可以接受并处理消息
  即便结果是 drop
~~~

相同函数名：

~~~text
has_out
~~~

背后 contract 可以不同。

---

# 五十一、为什么 XPUB_NODROP 需要额外逻辑

普通 PUB/XPUB 常允许：

~~~text
慢 subscriber 达到 HWM
→ drop / deactivate
~~~

而 NODROP 类策略会要求：

~~~text
所有 matching HWM 可接受
~~~

这就会用到：

~~~cpp
dist_t::check_hwm()
~~~

遍历：

~~~text
matching prefix
~~~

确认容量。

---

# 五十二、Matching Prefix 让 HWM Admission 很直接

~~~cpp
for i in [0, matching):
    if !pipe[i]->check_hwm():
        return false;
~~~

不需要：

~~~text
重新 topic match
~~~

因为目标集合已经物化成 prefix。

---

# 五十三、Selection 与 Admission 是两个阶段

可以理解为：

~~~text
topic routing
→ build matching set

capacity admission
→ check matching set

distribution
→ send matching set
~~~

这种分阶段设计更清楚。

---

# 五十四、为什么 `match()` 只允许 Eligible Pipe

即使某 subscriber topic 匹配，

如果它当前：

~~~text
HWM blocked
~~~

普通 lossy PUB 不应该让它进入：

~~~text
matching send set
~~~

否则分发时还是立即失败。

---

# 五十五、Backpressure 与 Routing 在 DIST 里真正汇合

Pipe HWM 决定：

~~~text
eligible
~~~

Subscription routing 决定：

~~~text
matching
~~~

最终：

~~~text
deliver
=
eligible
∩
semantic match
∩
transaction-valid membership
~~~

---

# 五十六、FQ / LB / DIST 的共同本质

都可以抽象为：

~~~text
resource set
+
fast membership partition
+
current transaction state
+
event-driven activation
~~~

区别只是：

~~~text
FQ:
many → one input selection

LB:
one message → one output

DIST:
one message → many outputs
~~~

---

# 五十七、为什么不用 Generic Scheduler Base Class

三个类虽然结构像，

但真正 invariant 不同：

FQ：

~~~text
fair read cursor
~~~

LB：

~~~text
single destination
multipart pinning
abort/drop state
~~~

DIST：

~~~text
matching subset
active subset
eligible next-generation subset
fan-out ownership
~~~

强行抽象成一个通用 base scheduler：

~~~text
可能让不变量变得更难看懂
~~~

---

# 五十八、小而专门的结构有时比“大一统抽象”更好

源码设计不是：

~~~text
消灭所有重复
~~~

而是：

~~~text
让每个核心 invariant 局部而明确
~~~

这是 Runtime 代码中很重要的取舍。

---

# 五十九、Active Prefix 为什么比 `std::set` 更适合 Hot Path

假设每次消息都要：

~~~text
pick current
advance
deactivate
reactivate
~~~

连续数组带来：

- cache locality；
- O(1) index；
- O(1) swap；
- 无 node allocation；
- 无 tree/hash metadata；
- branch pattern简单。

---

# 六十、代价是什么

- 不保持稳定顺序；
- 对象必须 intrusive；
- index invariant 必须严格维护；
- element move/delete 生命周期必须受控；
- container ID 规划要清楚。

---

# 六十一、Intrusive Container 的最大风险不是性能，而是 Invariant Corruption

如果某路径：

~~~text
swap pointers
但忘了更新 array index
~~~

之后：

~~~text
array.index(pipe)
~~~

会返回错误位置。

再一次 erase/swap：

~~~text
直接破坏别的对象 membership
~~~

所以 `array_t::swap()` 必须同时更新两个 object index。

---

# 六十二、为什么 `has_pipe()` 还要二次验证

DIST：

~~~cpp
claimed_index =
    _pipes.index(pipe);

if (claimed_index >= size)
    return false;

return _pipes[claimed_index]
       == pipe;
~~~

它不只相信 object 内的 index。

还验证：

~~~text
该 index 当前真的指回这个 pipe
~~~

这是对 stale intrusive metadata 的轻量防御。

---

# 六十三、Pipe 生命周期与 Scheduler Membership 必须同步

`pipe_terminated(pipe)`：

~~~text
先从 scheduler 容器移除
~~~

之后 pipe 才能继续销毁。

否则：

~~~text
scheduler array
→ dangling pipe*
~~~

---

# 六十四、这和之前的 Quiescence 主线一致

逻辑 retire：

~~~text
no longer schedulable
~~~

必须先于：

~~~text
physical reclaim
~~~

虽然 FQ/LB/DIST 本身不是完整 lifetime protocol，

但它们是 retirement chain 的必要一环。

---

# 六十五、DEALER 为什么同时拥有 FQ 和 LB

源码：

~~~text
DEALER
  |
  +-- _fq
  +-- _lb
~~~

DEALER 的 outbound 是“任选一个 writable peer”；ROUTER 则完全不同，它先用 routing-id 做精确索引，再把解析到的 Pipe 冻结为整条 multipart 的 current target。duplicate identity、mandatory 与 handover 如何让 routing registry 变成生命周期状态机，见 [DEALER / ROUTER：显式路由、Routing-ID 生命周期与 Multipart 粘性](dealer-router-routing.md)。

attach pipe：

~~~cpp
_fq.attach(pipe);
_lb.attach(pipe);
~~~

同一 pipe：

~~~text
inbound side
→ array_item_t<1>

outbound side
→ array_item_t<2>
~~~

所以它可以同时在：

~~~text
read scheduler
+
write scheduler
~~~

中存在。

---

# 六十六、这就是 Pipe 多个 Intrusive Index Slot 的实际用途

不是理论上的“也许”。

DEALER 直接证明：

~~~text
same pipe object
participates in two independent scheduler arrays
~~~

因此：

~~~text
ID 1 / ID 2
~~~

不能共享一份 index。

---

# 六十七、PUSH 为什么只需要 LB

PUSH：

~~~text
send only
~~~

所以只维护：

~~~text
_lb
~~~

---

# 六十八、PULL 为什么只需要 FQ

PULL：

~~~text
receive only
~~~

所以只维护：

~~~text
_fq
~~~

---

# 六十九、PUB/XPUB 为什么需要 DIST

它需要：

~~~text
one input message
→ zero/one/many matching subscriber pipes
~~~

所以：

~~~text
matching subset
~~~

比 LB 的：

~~~text
choose one
~~~

复杂。

---

# 七十、Socket Pattern 可以看成 Scheduler Composition

例如：

~~~text
DEALER
= FQ + LB

PUSH
= LB

PULL
= FQ

XPUB
= FQ-like subscription intake
  + DIST output
~~~

很多 ZeroMQ pattern 的差异，

并不是完全不同的 transport engine，

而是：

~~~text
如何组合 Pipe scheduler 与 routing rule
~~~

---

# 七十一、这对自己设计 Middleware 很有价值

不要一上来：

~~~text
每种 socket pattern
写一整套网络栈
~~~

可以先拆：

~~~text
transport
queue
ownership
input scheduler
output scheduler
routing policy
~~~

再组合。

---

# 七十二、调度器不拥有 Pipe

FQ/LB/DIST 保存：

~~~text
pipe_t*
~~~

但 pipe 的物理生命周期由更大的 socket/session/termination 协议管理。

所以 scheduler 是：

~~~text
membership owner
~~~

不是：

~~~text
memory owner
~~~

---

# 七十三、Container Ownership 与 Object Ownership 必须分开

从某个 scheduler `erase(pipe)`：

~~~text
只表示：
不再属于这个 selection set
~~~

不等于：

~~~text
delete pipe
~~~

这点与标准容器装 `unique_ptr` 的心智模型不同。

---

# 七十四、`_current` 在 erase 时为什么需要修正

假设：

~~~text
current == active boundary
~~~

某 pipe 被 swap 出 active 区后，

cursor 可能指到：

~~~text
刚刚失效的位置
~~~

所以代码：

~~~cpp
if (_current == _active)
    _current = 0;
~~~

保持：

\[
0 \le current < active
\]

---

# 七十五、Cursor 本身也是调度不变量

Prefix 只定义：

~~~text
candidate set
~~~

Cursor 定义：

~~~text
下一次公平选择从哪里开始
~~~

两者必须同步维护。

---

# 七十六、Round-robin 不应该因“探测无数据”永久偏斜

`fq_t::has_in()` 注释强调：

如果没有消息，

即使 temporary 修改 `_current`，

最终也会回到原位置。

如果找到消息，

cursor 停在第一个真正可读 pipe。

---

# 七十七、这是一种 Lazy Cleanup + Fair Cursor

probe 的过程：

~~~text
skip/deactivate empty pipe
~~~

同时：

~~~text
preserve fairness among remaining active set
~~~

---

# 七十八、LB 中 `_dropping` 为什么与 `_more` 分开

`_more`：

~~~text
当前 multipart transaction
是否尚未结束
~~~

`_dropping`：

~~~text
当前 transaction 已经失去目的地
剩余 frame 应被消费但不发送
~~~

两者是不同维度。

---

# 七十九、一个 Bool 不足以表达两种状态

可能：

~~~text
_more=true
_dropping=false
→ 正常 multipart

_more=true
_dropping=true
→ aborting multipart

_more=false
_dropping=false
→ ordinary boundary
~~~

所以即使只是两个 bool，

组合后已经形成小状态机。

---

# 八十、Runtime 中很多“Bool 堆积”其实暗藏状态机

看到：

~~~text
_more
_dropping
_active
_eligible
~~~

不能只逐字段解释。

应该问：

~~~text
它们组合起来有多少合法状态？
状态之间怎样转移？
哪些组合永远不允许？
~~~

---

# 八十一、DIST 的四段 Partition 其实也是状态机压缩

不是给每个 pipe 一个：

~~~cpp
enum {
  Matching,
  Active,
  Eligible,
  Passive
}
~~~

而是通过：

~~~text
物理 index 相对边界的位置
~~~

推导状态。

---

# 八十二、优点：不需要 Per-pipe State Byte

状态编码在：

~~~text
container layout
~~~

里。

这样遍历 matching set：

~~~text
for i < matching
~~~

天然连续。

---

# 八十三、代价：Mutation 必须维护全局 Layout Invariant

Per-object enum：

~~~text
更新一个字段
~~~

Prefix partition：

~~~text
必须 swap + boundary update
~~~

但换来 hot-path iteration 更简单。

---

# 八十四、这就是 AoS State vs Set Partition 的设计选择

方案 A：

~~~text
vector<PipeState>
for every item:
    if state == MATCHING ...
~~~

方案 B：

~~~text
matching items already contiguous
for i < matching:
    ...
~~~

高频遍历时 B 可以减少：

~~~text
branch/filter
~~~

---

# 八十五、为什么 DIST 特别适合这种布局

PUB fan-out 每条 message 都可能：

~~~text
遍历很多 subscriber
~~~

所以：

~~~text
matching set contiguous
~~~

直接提升 cache/locality。

---

# 八十六、Subscription Matching 与 DIST 是上下游

topic trie/mtrie 负责：

~~~text
哪些 pipe semantic match
~~~

DIST 负责：

~~~text
把这些 pipe 收进 matching prefix
并完成 fan-out
~~~

所以：

~~~text
routing algorithm
~~~

和：

~~~text
distribution scheduler
~~~

仍是两层。

---

# 八十七、为什么 Matching 不等于 Active

语义上匹配，

但资源上可能：

~~~text
HWM full
~~~

因此：

~~~text
semantic eligibility
~~~

和：

~~~text
resource eligibility
~~~

必须分开。

---

# 八十八、这和机器人任务调度很像

一个 actuator：

~~~text
逻辑上适合执行 task
~~~

但可能：

~~~text
当前 bus backlog 太高
device unavailable
safety gate closed
~~~

所以真正 candidate：

~~~text
semantic match
∩
resource ready
∩
lifecycle active
~~~

---

# 八十九、FQ 的公平策略可以迁移到多传感器输入

例如：

~~~text
Camera A
Camera B
LiDAR
Telemetry
~~~

如果所有 source 都可读，

可以：

~~~text
one complete packet/message per source
round-robin
~~~

避免高频 source 独占 reactor。

---

# 九十、但要先定义 Fairness Quantum

视觉：

~~~text
one frame
~~~

LiDAR：

~~~text
one scan
~~~

CAN：

~~~text
one frame
~~~

网络 RPC：

~~~text
one request
~~~

必须先定义业务原子单位。

---

# 九十一、LB 可以迁移到多 Worker / 多 Device 选择

例如：

~~~text
Inference Worker 0
Inference Worker 1
Inference Worker 2
~~~

如果 worker queue full：

~~~text
deactivate
~~~

capacity 恢复：

~~~text
reactivate
~~~

在 active prefix 中 round-robin。

---

# 九十二、但 Worker 中途故障时要处理 Transaction Affinity

如果 task 是多阶段 multipart：

~~~text
header
tensor chunks
footer
~~~

中途 worker 消失，

不能把 footer 改发给另一 worker。

必须：

~~~text
abort whole transaction
~~~

LB `_dropping` 正是同类问题。

---

# 九十三、DIST 可以迁移到机器人事件广播

例如：

~~~text
localization update
→ planner
→ logger
→ visualization
→ safety monitor
~~~

可以先建立：

~~~text
eligible subscribers
~~~

再根据 topic / interest：

~~~text
matching subset
~~~

---

# 九十四、但慢 Subscriber 该怎样处理是 Policy

可选：

- block producer；
- drop for slow subscriber；
- latest-value；
- bounded backlog；
- disconnect subscriber。

DIST 只提供机制。

---

# 九十五、共享 Payload 在机器人数据面尤其重要

PointCloud/Image：

~~~text
MB-level payload
~~~

如果 fan-out N 个 module：

~~~text
N full copies
~~~

非常贵。

更合理：

~~~text
shared payload
+
small handle per consumer
+
refcount / loan lifetime
~~~

libzmq 的 LMSG fan-out 就体现了这个思想。

---

# 九十六、但 Refcount 不是 Zero-copy 的全部

即使 middleware 内部不复制 payload，

后续：

- serialization；
- kernel；
- TCP；
- GPU transfer；

仍可能复制。

所以“共享引用”只能证明：

~~~text
这一层 ownership duplication avoided
~~~

---

# 九十七、真正 Zero-copy 需要逐层证明

要逐层问：

~~~text
application → middleware
middleware → transport
transport → kernel
kernel → NIC
receiver NIC → kernel
kernel → middleware
middleware → application
~~~

哪一步只是 handle transfer，

哪一步 memcpy。

---

# 九十八、三个调度器的时间复杂度

FQ：

~~~text
activate/deactivate
O(1)

one successful selection
amortized around active scan
~~~

LB：

~~~text
activate/deactivate
O(1)

selection
may skip blocked pipes
~~~

DIST：

~~~text
match
O(1) once pipe known

fan-out
O(number of matching pipes)
~~~

---

# 九十九、为什么 DIST Fan-out 不可能 O(1)

如果要向：

~~~text
N independent outputs
~~~

发布，

至少要建立 N 个 output ownership/transport obligations。

所以：

\[
\Omega(N)
\]

是语义决定的。

优化重点应该是：

~~~text
减少每个 target 的固定成本
~~~

而不是幻想总成本与 N 无关。

---

# 一百、Prefix Partition 优化的是 Per-target 常数

例如：

- 不做 hash lookup；
- 不做 node allocation；
- contiguous scan；
- shared payload；
- O(1) removal。

这些都在减少：

\[
C_{\text{per target}}
\]

---

# 一百零一、这比“换一个更高级算法”更贴近 Runtime 优化

很多系统吞吐瓶颈不在：

~~~text
Big-O
~~~

而在：

- cache miss；
- allocation；
- atomic；
- branch；
- lock；
- pointer chasing。

ZeroMQ 这些小类非常典型。

---

# 一百零二、一个统一抽象

可以把三者写成：

~~~text
Resource Set
    |
    +-- semantic membership
    +-- resource readiness
    +-- transaction epoch
    +-- fairness cursor
    +-- ownership action
~~~

---

# 一百零三、FQ

~~~text
semantic membership
= all inbound pipes

readiness
= active prefix

transaction epoch
= current multipart

fairness
= round-robin current

ownership action
= move msg out of one pipe
~~~

---

# 一百零四、LB

~~~text
semantic membership
= all outbound pipes

readiness
= active prefix

transaction epoch
= multipart pinned destination

fairness
= round-robin current

ownership action
= transfer msg to one pipe
~~~

---

# 一百零五、DIST

~~~text
semantic membership
= matching prefix

readiness
= active/eligible prefix

transaction epoch
= multipart generation

fairness
= not single-choice fairness;
  fan-out over target set

ownership action
= duplicate msg handle /
  share payload refs
~~~

---

# 一百零六、Scheduler 与 Queue 的边界

FQ/LB/DIST 不自己存业务 payload backlog。

它们只存：

~~~text
pipe pointers
~~~

真正 payload 在：

~~~text
pipe / ypipe
~~~

所以：

~~~text
scheduler
~~~

和：

~~~text
queue storage
~~~

分离。

---

# 一百零七、为什么这是好设计

如果 scheduler 也拥有 payload：

~~~text
HWM
routing
lifetime
queue
fairness
~~~

会全部耦在一个类里。

现在：

~~~text
pipe
→ resource-local queue/capacity

scheduler
→ multi-pipe selection policy
~~~

职责更清楚。

---

# 一百零八、Runtime 作者应该先分清两类问题

第一：

> 单个资源现在能不能 progress？

由：

~~~text
pipe
~~~

回答。

第二：

> 多个可 progress 资源中选谁？

由：

~~~text
FQ / LB / DIST
~~~

回答。

---

# 一百零九、这与 Reactor / Scheduler 的分层相同

Asio：

~~~text
descriptor state
→ can I/O progress?

Scheduler
→ which completion executes?
~~~

libzmq：

~~~text
pipe
→ can message progress?

FQ/LB/DIST
→ which pipe participates?
~~~

结构上非常相似。

---

# 一百一十、为什么 Activation Event 只传 Pipe Pointer 就够

因为容量/可读状态已经在：

~~~text
pipe owner-local state
~~~

更新完成。

上层只需知道：

~~~text
this resource should re-enter candidate set
~~~

所以：

~~~text
write_activated(pipe)
read_activated(pipe)
~~~

不必附带一大堆状态。

---

# 一百一十一、这是 State-before-Notify 的再次体现

底层：

~~~text
update pipe state
~~~

然后：

~~~text
notify scheduler membership change
~~~

Scheduler收到事件后不需要猜：

~~~text
为什么 active
~~~

它只执行 membership transition。

---

# 一百一十二、Scheduler Event 最好表达“Action”，不是复制全部 State

例如：

~~~text
ACTIVATE(pipe)
DEACTIVATE(pipe)
REMOVE(pipe)
~~~

比：

~~~text
send huge snapshot
~~~

更轻。

前提是权威状态由正确 owner 管理。

---

# 一百一十三、终止为什么一定先从 Scheduler 移除

如果 pipe 已进入：

~~~text
termination
~~~

却还留在 active prefix，

下一次 cursor 可能：

~~~text
继续 dereference
~~~

生命周期就会出问题。

---

# 一百一十四、所以 Scheduler Membership 是 Lifetime Reachability

只要：

~~~text
pipe pointer remains in active/all scheduler array
~~~

就意味着：

~~~text
scheduler still holds a potential future reference
~~~

物理 reclaim 前必须切断这些 reachability。

---

# 一百一十五、这与 Quiescence 不是同一个概念

从数组 erase：

~~~text
prevent future lookup
~~~

但如果当前调用栈已经拿着：

~~~text
pipe*
~~~

还需要更高层保证：

~~~text
in-flight execution finished
~~~

所以：

~~~text
unregister
!=
quiescence
~~~

这个原则再次成立。

---

# 一百一十六、如何从零实现一个 Active-prefix Scheduler

概念骨架：

~~~cpp
template<class T>
class ActiveSet
{
    std::vector<T*> items;
    size_t active;
    size_t current;
};
~~~

对象内部：

~~~cpp
size_t scheduler_index;
~~~

失活：

~~~text
swap(index, active-1)
active--
~~~

激活：

~~~text
swap(index, active)
active++
~~~

---

# 一百一十七、什么时候不该这么做

如果需要：

- priority order；
- deadline order；
- stable iteration order；
- arbitrary concurrent mutation；
- lock-free multi-writer membership；
- millions of sparse IDs。

那么：

~~~text
prefix vector
~~~

可能不是最佳结构。

---

# 一百一十八、它适合什么

- candidate 数量中小；
- mutation 高频；
- iteration 更高频；
- 单 owner；
- 顺序可变；
- active/inactive transition 多；
- cache locality重要。

这正是 libzmq socket scheduler。

---

# 一百一十九、机器人 Executor 里也很常见

例如：

~~~text
all controllers
[ runnable | blocked ]
~~~

或：

~~~text
all devices
[ ready | waiting ]
~~~

如果单线程 owner 维护，

active prefix 往往非常有效。

---

# 一百二十、但不要把 Deadline Scheduler 强行改成 Prefix

实时控制任务若核心是：

~~~text
earliest deadline
~~~

可能需要：

~~~text
heap / timing wheel / RB tree
~~~

数据结构应该服从调度语义。

---

# 一百二十一、FQ/LB/DIST 最值得迁移的不是类名

而是思考顺序：

第一问：

~~~text
调度原子单位是什么？
~~~

第二问：

~~~text
哪些资源当前 eligible？
~~~

第三问：

~~~text
eligibility 是否会在 transaction 中途变化？
~~~

第四问：

~~~text
需要一选一还是一选多？
~~~

第五问：

~~~text
资源失活/恢复是否高频？
~~~

第六问：

~~~text
payload ownership如何 fan-out？
~~~

然后再选数据结构。

---

# 一百二十二、一个多相机推理调度例子

假设：

~~~text
GPU Worker A
GPU Worker B
GPU Worker C
~~~

普通任务：

~~~text
LB active prefix
~~~

worker queue full：

~~~text
deactivate
~~~

完成一批任务：

~~~text
reactivate
~~~

---

# 一百二十三、如果一个推理请求由多个 Chunk 组成

例如：

~~~text
prefill chunk 1
prefill chunk 2
decode state
~~~

一旦选定 worker：

~~~text
multipart affinity
~~~

不能中途切 worker，

除非有显式 state migration。

这和 LB `_more` 一样。

---

# 一百二十四、一个多订阅机器人数据总线例子

PointCloud 发布：

~~~text
planner
mapper
logger
visualizer
~~~

先按 topic / capability 建：

~~~text
matching
~~~

再按：

~~~text
backpressure / lifecycle
~~~

得到：

~~~text
eligible
~~~

最终 fan-out。

---

# 一百二十五、如果 Subscriber 在当前消息 Generation 中途刚恢复

如果消息由多个 chunk 构成：

~~~text
不能从 chunk 3 开始加入
~~~

应：

~~~text
eligible now
active next message
~~~

这正是 DIST `_eligible` 的意义。

---

# 一百二十六、一个控制周期也可以理解成 Generation

例如 1 kHz 控制循环：

~~~text
cycle k
~~~

参与本周期的 actuator set 应在开始时确定。

中途设备恢复：

~~~text
加入 cycle k+1
~~~

避免 half-cycle state。

---

# 一百二十七、最终统一图

~~~text
                         pipe_t
                           |
          +----------------+----------------+
          |                                 |
     input readiness                   output capacity
          |                                 |
   activate_read                       activate_write
          |                                 |
          v                                 v
        fq_t                         lb_t / dist_t
          |                                 |
          |                                 |
 [ active | inactive ]          LB:
          |                     [ active | inactive ]
          |                                 |
          |                     DIST:
          |          [ matching | active | eligible | passive ]
          |                                 |
          +---------------+-----------------+
                          |
                          v
                  complete-message
                   scheduling unit
                          |
             +------------+------------+
             |                         |
          one target                 many targets
             |                         |
            LB                       DIST
             |                         |
    multipart destination       shared payload refs
       remains pinned          + matching membership
~~~

---

# 一百二十八、源码作者要守住的核心不变量

第一：

> **调度单位必须与业务原子单位一致；ZeroMQ 的公平/负载均衡单位是完整 multipart message，而不是 frame。**

第二：

> **资源 readiness 应改变 candidate-set membership，而不是让 scheduler 持续撞一个必然失败的资源。**

第三：

> **高频 activate/deactivate 若不要求稳定顺序，可以用连续数组 + prefix + intrusive index 实现 O(1) 迁移。**

第四：

> **一个对象属于多个独立容器时，需要多个独立 intrusive index slot。**

第五：

> **multipart transaction 开始以后，目的地或参与者集合不能随意中途变化；新恢复资源进入下一 generation。**

第六：

> **DIST 的不变量是 `matching ⊆ active ⊆ eligible ⊆ all`，任何失败、终止、恢复都必须维护这些边界。**

第七：

> **fan-out 的 payload ownership 与 scheduler selection 是同一条执行链的一部分；大 payload 可以共享 storage，但失败引用必须修正。**

第八：

> **scheduler container 只管理 membership，不自动拥有 pipe 生命周期；erase 与 physical reclaim 是两件事。**

第九：

> **availability notification 应发生在权威资源状态更新之后。**

第十：

> **数据结构不是从 STL 菜单里选的，而应由调度不变量反推。**

---

# 一百二十九、最终心智模型

FQ：

~~~text
many inbound pipes
        ↓
active prefix
        ↓
round-robin cursor
        ↓
one complete message
        ↓
move cursor
~~~

LB：

~~~text
many outbound pipes
        ↓
active prefix
        ↓
round-robin cursor
        ↓
pin one pipe for multipart
        ↓
flush
        ↓
move cursor
~~~

DIST：

~~~text
all outbound pipes
        ↓
eligible prefix
        ↓
active prefix
        ↓
semantic matching prefix
        ↓
fan-out message handle
        ↓
shared payload refs
        ↓
complete multipart generation
        ↓
eligible → active
~~~

如果只记住一个结论：

> **libzmq 的 FQ/LB/DIST 本质上是在把“消息原子性 + 资源可用性 + 路由集合”压缩成连续数组的边界与 cursor；调度器之所以小，不是问题简单，而是大量复杂状态被前缀分区、owner-thread 和 Pipe activation protocol 提前结构化了。**
