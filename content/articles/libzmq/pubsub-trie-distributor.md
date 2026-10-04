# PUB / SUB：订阅 Trie、反向控制面与 Distributor

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

PUB/SUB 看起来像最简单的消息模式：

~~~text
publisher
   ↓
many subscribers
~~~

但如果只把它理解成“广播”，会错过 libzmq 里最值得学习的一组设计：

~~~text
数据流向下游
订阅控制流反向上游
本地订阅状态可重放
Publisher 侧保存 prefix → Pipe 集合
匹配结果直接进入 dist_t matching prefix
HWM 决定 Pipe 资源资格
multipart 冻结匹配集合
Pipe 终止要同步撤销订阅关系
~~~

所以完整的数据面不是：

~~~text
PUB
→ copy to everyone
~~~

而更接近：

~~~text
SUB local intent
      ↓
subscription command
      ↓
reverse control plane
      ↓
XPUB mtrie
prefix → Pipe set
      ↓
topic matching
      ↓
DIST matching prefix
      ↓
Pipe/HWM
      ↓
fan-out
~~~

这篇真正回答的问题是：

> **一个可动态连接、可重连、可背压的发布订阅 Runtime，怎样把“谁想要什么”“谁现在可写”“当前完整消息发给谁”拆成不同状态层，而不是每发一条消息都遍历全部连接做字符串判断？**

匹配后的 Pipe 如何进入 `dist_t` 的 matching / active / eligible 前缀、multipart 中途恢复的 Pipe 为什么只能参加下一条完整消息，见 [FQ / LB / DIST：Active Prefix、Multipart 原子性与消息调度器](fq-lb-dist-schedulers.md)。

---

# 一、先建立完整对象图

## 1. 两端不是同一种“订阅表”

SUB/XSUB 一侧：

~~~text
SUB / XSUB
   |
   +-- local trie
   |     prefix → refcount
   |
   +-- FQ
   |     inbound data
   |
   +-- DIST
         outbound subscription/control
~~~

PUB/XPUB 一侧：

~~~text
PUB / XPUB
   |
   +-- mtrie
   |     prefix → set<pipe_t*>
   |
   +-- DIST
         outbound data
~~~

所以两端虽然都叫“subscription tree”，实际保存的东西完全不同。

---

# 二、SUB 端 Trie 保存的是“本地意图”

`trie_t` 节点核心字段：

~~~cpp
uint32_t _refcnt;
unsigned char _min;
unsigned short _count;
unsigned short _live_nodes;

union {
    trie_t *node;
    trie_t **table;
} _next;
~~~

最关键的是：

~~~text
_refcnt
~~~

它不是 subscriber 数量。

它表示：

> **本地同一个 prefix 被登记了多少次。**

---

# 三、同一个 Prefix 为什么需要 Refcount

假设应用：

~~~text
subscribe("robot/")
subscribe("robot/")
~~~

如果只存一个 bool：

~~~text
robot/ = subscribed
~~~

第一次 unsubscribe：

~~~text
unsubscribe("robot/")
~~~

就无法知道：

~~~text
是不是还有第二个逻辑订阅者依赖它
~~~

所以节点：

~~~text
_refcnt = 2
~~~

第一次 rm：

~~~text
2 → 1
~~~

不真正删除 prefix。

第二次：

~~~text
1 → 0
~~~

才让这条 prefix 失效。

---

# 四、`trie_t::add()` 返回值表示什么

到达 prefix 节点：

~~~cpp
++_refcnt;
return _refcnt == 1;
~~~

所以返回：

~~~text
true
~~~

只表示：

> **这是从“没人订阅”变成“至少有人订阅”的第一次。**

并不是：

~~~text
操作成功/失败
~~~

---

# 五、`trie_t::rm()` 返回值也不是普通成功码

节点：

~~~cpp
if (!_refcnt)
    return false;

_refcnt--;

return _refcnt == 0;
~~~

返回 true 表示：

> **这次删除让整个 prefix 从有效变成无效。**

这对控制面传播非常重要。

---

# 六、为什么重复订阅不一定要重复通知上游

如果本地：

~~~text
subscribe A
subscribe A
~~~

对 upstream Publisher 来说，

只需要知道：

~~~text
这个 downstream Pipe 对 A 有兴趣
~~~

而不需要知道本地有几个逻辑调用者。

所以 local multiplicity 可以：

~~~text
refcount collapse
~~~

成一条 upstream interest。

---

# 七、但 XSUB 又为什么允许转发重复 Subscribe

当前源码注释明确说明：

过去 XSUB 曾过滤 duplicate subscribe，

但这样会破坏：

~~~text
ZMQ_XPUB_VERBOSE
+
forwarding device
~~~

场景。

因此 XSUB：

~~~text
local trie
→ 仍记录 refcount

wire/control path
→ subscribe command 仍可继续向上游广播
~~~

---

# 八、Local State 与 Wire Observability 是两个问题

本地 trie 负责：

~~~text
当前逻辑订阅 truth
~~~

上游是否看到重复命令：

~~~text
由 XPUB verbose policy 决定
~~~

不能因为本地状态能去重，就假设控制面事件也必须去重。

---

# 九、XPUB 端 MTrie 保存的是“Prefix → Pipe 集合”

`mtrie_t` 实际是：

~~~cpp
generic_mtrie_t<pipe_t>
~~~

每个 prefix 节点有：

~~~cpp
typedef std::set<value_t *> pipes_t;

pipes_t *_pipes;
~~~

所以：

~~~text
prefix "robot/"
    |
    +-- pipe A
    +-- pipe C
    +-- pipe F
~~~

---

# 十、SUB Trie 与 XPUB MTrie 的 Multiplicity 完全不同

SUB：

~~~text
prefix
→ integer refcount
~~~

含义：

~~~text
同一个本地 prefix 被登记几次
~~~

XPUB：

~~~text
prefix
→ set<pipe_t*>
~~~

含义：

~~~text
哪些不同远端 Pipe 订阅了这个 prefix
~~~

这是两种完全不同的“多”。

---

# 十一、为什么 XPUB 不能只存 `prefix → count`

假设：

~~~text
robot/
→ 3 subscribers
~~~

如果只有 count=3，

发布时无法知道：

~~~text
具体该向哪三条 Pipe 写
~~~

所以必须保存：

~~~text
prefix
→ actual endpoint set
~~~

---

# 十二、MTrie 的 Prefix Match 为什么非常适合 PUB/SUB

订阅：

~~~text
robot/
~~~

消息：

~~~text
robot/arm/joint/3/state
~~~

应该匹配。

也就是说：

~~~text
subscription
~~~

是：

~~~text
message topic prefix
~~~

不是 exact equality。

---

# 十三、`generic_mtrie_t::match()` 怎样工作

逻辑：

~~~cpp
for (current = root; current; ...)
{
    if (current->_pipes)
        for each pipe:
            callback(pipe);

    consume next topic byte;
}
~~~

关键点：

> **沿消息 topic 从 root 往下走时，每经过一个带订阅集合的节点，就把那个节点的 Pipe 都判为匹配。**

---

# 十四、所以它天然支持多级 Prefix

例如 subscriptions：

~~~text
""
"robot/"
"robot/arm/"
"robot/arm/joint/"
~~~

消息：

~~~text
robot/arm/joint/3/state
~~~

沿路径会依次命中：

~~~text
root
robot/
robot/arm/
robot/arm/joint/
~~~

对应 Pipe 集合全部进入结果。

---

# 十五、空 Prefix 为什么代表 Subscribe All

根节点：

~~~text
prefix length = 0
~~~

如果挂了 Pipe，

那么所有消息在 match 的第一步就会调用它。

所以：

~~~text
subscribe("")
~~~

自然就是：

~~~text
match all topics
~~~

不需要特殊字符串分支。

---

# 十六、为什么不是每条消息遍历所有 Subscription

朴素做法：

~~~text
for each subscription:
    starts_with(topic, prefix)
~~~

如果有：

~~~text
N 个 subscription
~~~

每条消息都可能变成：

\[
O(N \cdot L)
\]

而 Trie：

~~~text
沿消息前缀路径前进
~~~

大体只访问：

~~~text
topic path
+
actual matched pipe sets
~~~

---

# 十七、复杂度更接近业务语义

可近似写成：

\[
O(L + M)
\]

其中：

- \(L\)：topic byte 长度；
- \(M\)：实际命中的 Pipe 数。

当然节点 lookup 的实现细节还会影响常数。

---

# 十八、Trie 节点为什么不是固定 256 叉数组

byte alphabet 有：

~~~text
0..255
~~~

最简单可以每个节点：

~~~text
child[256]
~~~

但绝大多数 topic prefix 非常稀疏。

那会浪费大量 pointer 空间。

---

# 十九、libzmq 使用 `_min + _count` 表示连续字符窗口

例如当前孩子 byte：

~~~text
'a'
'b'
'c'
~~~

可以表示：

~~~text
_min = 'a'
_count = 3
~~~

索引：

~~~text
table[c - _min]
~~~

---

# 二十、如果只有一个 Child，更进一步压缩

~~~text
_count == 1
~~~

不分配 pointer table。

直接：

~~~cpp
_next.node
~~~

保存唯一 child。

---

# 二十一、什么时候才使用 Table

当：

~~~text
_count > 1
~~~

才：

~~~cpp
_next.table
~~~

分配连续 pointer 数组。

---

# 二十二、这是 Small-state Optimization

可以类比：

- small vector；
- small string；
- inline storage。

常见形态：

~~~text
0 children
→ no pointer storage

1 child
→ direct pointer

many children
→ pointer table
~~~

---

# 二十三、为什么 Table 仍可能有空洞

假设 child bytes：

~~~text
'a'
'z'
~~~

范围：

~~~text
'a' ... 'z'
~~~

之间很多 slot 可能是 NULL。

所以这不是压缩 radix tree，

而是：

~~~text
bounded contiguous byte range table
~~~

---

# 二十四、它优化的是常见局部分支密度

topic 通常不是随机 256-way fanout。

很多节点：

~~~text
只有一个后继
~~~

或者少量近邻字符。

这使 direct-node optimization 很有效。

---

# 二十五、删除 Subscription 后为什么要主动 Compact

当 child 被删除：

~~~text
_live_nodes
~~~

减少。

如果：

~~~text
只剩一个 live child
~~~

会从：

~~~text
table
~~~

压缩回：

~~~text
single node pointer
~~~

---

# 二十六、边界 Child 被删时还会缩 `_min/_count`

例如：

~~~text
[a b c d e]
~~~

只剩：

~~~text
[c d e]
~~~

可以把：

~~~text
_min: a → c
_count: 5 → 3
~~~

而不是一直保留空的 a/b slot。

---

# 二十七、所以 Trie Memory Layout 是动态适应的

插入：

~~~text
expand character window
~~~

删除：

~~~text
prune redundant nodes
shrink character window
collapse table → single node
~~~

这比固定 256-way table 更适合长期动态订阅。

---

# 二十八、为什么 `trie_t::check()` 特意不用递归

源码注释：

~~~text
critical path
deliberately doesn't use recursion
~~~

因为 SUB 收消息时：

~~~text
每条第一帧
~~~

都可能做本地 prefix filter。

这是 hot path。

---

# 二十九、`check()` 的 Prefix 语义

每走一个节点先判断：

~~~cpp
if (current->_refcnt)
    return true;
~~~

所以只要当前路径上任何 prefix 有订阅：

~~~text
立即匹配
~~~

不需要走到 topic 结尾。

---

# 三十、这正好对应 Prefix Subscription

如果：

~~~text
subscribed "robot/"
~~~

收到：

~~~text
robot/camera/front
~~~

走到 `robot/` 节点：

~~~text
_refcnt > 0
→ true
~~~

立即返回。

---

# 三十一、MTrie Remove 为什么比普通 Trie 更复杂

XPUB 节点不是一个 refcount，

而是：

~~~text
set<pipe_t*>
~~~

删除有两种问题：

第一：

~~~text
从某个 prefix 删除一个具体 Pipe
~~~

第二：

~~~text
Pipe 整体终止
→ 要从整个 Trie 的所有 prefix 中删掉它
~~~

---

# 三十二、`rm(prefix, pipe)` 的三种结果

~~~text
not_found
values_remain
last_value_removed
~~~

分别表示：

~~~text
这个 Pipe 本来就不在这里

删掉这个 Pipe 后
还有其他 Pipe 订阅同一 prefix

删掉后
这个 prefix 已经没人订阅
~~~

---

# 三十三、为什么 `last_value_removed` 很重要

XPUB 不只是维护本地 routing truth。

它还可能需要向更上游暴露：

~~~text
这个 prefix 已经没有任何下游 interest
~~~

所以：

~~~text
最后一个 Pipe 离开
~~~

是一个控制面事件。

---

# 三十四、`xpipe_terminated()` 为什么要遍历删除整个 Pipe 的所有 Prefix

一条 subscriber Pipe 可能订阅：

~~~text
robot/
camera/
imu/
~~~

连接终止后必须把它从所有 prefix set 中删掉。

否则：

~~~text
mtrie
→ dangling pipe*
~~~

未来 topic match 就会引用失效 Pipe。

---

# 三十五、Subscription Registry 同样是 Lifecycle Registry

只要：

~~~text
prefix node
→ pipe*
~~~

还存在，

未来消息 match 就可能发现该 Pipe。

所以：

> **Pipe 物理销毁之前，必须先从订阅索引中撤销未来 discoverability。**

---

# 三十六、这与 ROUTER Routing Registry 完全同构

ROUTER：

~~~text
routing-id → pipe*
~~~

PUB/SUB：

~~~text
prefix → set<pipe*>
~~~

两者都不是普通容器。

它们都是：

~~~text
future work discovery index
~~~

---

# 三十七、Registry Remove 与 Quiescence 仍然不同

从 MTrie 删掉 Pipe：

~~~text
未来 match 不再找到它
~~~

但当前 `dist_t` 的 multipart matching set 可能已经建立。

已经开始的消息如何结束：

~~~text
由 DIST 的 transaction state
~~~

单独处理。

---

# 三十八、SUBSCRIBE 命令为什么要反向传播

数据方向：

~~~text
XPUB/PUB
→ XSUB/SUB
~~~

但发布者想做 source-side filtering，

必须知道：

~~~text
下游分别对哪些 topic 有兴趣
~~~

所以控制面反向：

~~~text
XSUB/SUB
→ XPUB/PUB
~~~

---

# 三十九、完整方向图

~~~text
        subscription control
      <----------------------
PUB/XPUB                    XSUB/SUB
      ---------------------->
             data
~~~

---

# 四十、这是 Credit/Interest 的反向传播模式

消费者向生产者发送：

~~~text
interest
~~~

生产者据此：

~~~text
减少无意义发送
~~~

这种模式也出现在：

- flow-control credit；
- demand propagation；
- reactive streams；
- multicast membership；
- cache invalidation interest。

---

# 四十一、SUB 设置订阅时实际发生什么

`sub_t::xsetsockopt(ZMQ_SUBSCRIBE)`：

~~~text
construct subscribe msg_t
→ xsub_t::xsend()
~~~

所以 socket option 并不是只改一个本地变量。

它变成：

~~~text
control-plane message
~~~

进入同一套 Pipe Runtime。

---

# 四十二、为什么把 Subscription 也做成 Message

好处：

- 复用 Pipe；
- 复用 HWM；
- 复用 multipart/control framing；
- 复用 transport；
- 复用 reconnect path；
- 不另建一条控制连接。

---

# 四十三、Data Plane 与 Control Plane 共享 Transport，但语义不同

物理上：

~~~text
same Pipe/connection
~~~

逻辑上：

~~~text
business data
vs
subscription control
~~~

必须从 message flags / command encoding 区分。

---

# 四十四、XSUB 的 `_subscriptions` 同时承担两种职责

第一：

~~~text
local filtering truth
~~~

第二：

~~~text
replayable control-plane state
~~~

这是理解 XSUB 的核心。

---

# 四十五、为什么新 Pipe Attach 时必须 Replay 全部 Subscription

`xattach_pipe()`：

~~~cpp
_subscriptions.apply(
    send_subscription,
    pipe_);

pipe_->flush();
~~~

也就是说：

~~~text
new upstream
~~~

不会只收到“attach 之后发生的新订阅”。

它需要知道：

~~~text
当前完整 interest state
~~~

---

# 四十六、Subscription 是 State，不只是 Event

如果订阅只是：

~~~text
subscribe event happened once
~~~

新连接永远无法恢复过去历史。

所以 XSUB 保存：

~~~text
current state
~~~

然后在需要时重放。

---

# 四十七、这就是 State Replication

可以抽象为：

~~~text
authoritative local state
        ↓
on link establishment
        ↓
replay snapshot
~~~

---

# 四十八、为什么 Hiccup 后也要重放

`xhiccuped(pipe)`：

~~~cpp
_subscriptions.apply(
    send_subscription,
    pipe_);

pipe_->flush();
~~~

hiccup 意味着底层 outbound queue/link 状态发生替换。

新的 peer-side状态不能假设：

~~~text
旧 subscription state 仍完整存在
~~~

所以重新同步。

---

# 四十九、这与重连后的 Session Resynchronization 一样

任何协议只要远端状态是：

~~~text
由历史增量事件累计出来
~~~

重连后就面临：

~~~text
remote state lost?
~~~

最稳妥方案之一：

~~~text
replay current snapshot
~~~

---

# 五十、为什么 XSUB 必须保存 Trie，而不能只广播 Subscribe 命令

因为未来：

- 新 upstream attach；
- hiccup；
- reconnect；

都需要：

~~~text
恢复当前订阅集合
~~~

如果只转发事件：

~~~text
历史已经丢了
~~~

无法重建。

---

# 五十一、`apply()` 做的是什么

Trie 遍历所有有效 prefix，

对每个：

~~~text
send_subscription(prefix, pipe)
~~~

构造新的订阅 msg 并写给指定 Pipe。

所以这是：

~~~text
state → control-message snapshot
~~~

转换。

---

# 五十二、为什么 `apply_helper()` 需要临时 Buffer

Trie 节点只存：

~~~text
当前 byte + child structure
~~~

遍历时要把整条 prefix：

~~~text
robot/arm/
~~~

重新拼出来。

因此维护一个：

~~~text
dynamic byte buffer
~~~

跟随 DFS 路径。

---

# 五十三、这个 Buffer 每次增长 256 Byte

代码：

~~~text
if buffsize >= maxbuffsize
    maxbuffsize = buffsize + 256
~~~

说明它不是为 topic 每个字符频繁 realloc。

---

# 五十四、MTrie 为什么避免递归 Remove

`generic_mtrie_t::rm(pipe, ...)` 的源码注释非常关键：

过去使用递归遍历。

问题是：

~~~text
remote clients
→ can influence trie depth
→ therefore influence stack usage
~~~

---

# 五十五、所以改成显式 `std::list<iter>` Stack

也就是：

~~~text
recursive DFS
~~~

改为：

~~~text
heap-backed explicit traversal state
~~~

避免：

~~~text
untrusted remote prefix depth
→ process stack exhaustion
~~~

---

# 五十六、这是非常值得迁移的安全设计

只要：

~~~text
input controls tree depth
~~~

递归算法就不能只从“代码更漂亮”考虑。

还要考虑：

~~~text
adversarial depth
~~~

---

# 五十七、算法复杂度与 Stack Safety 是两回事

即使：

~~~text
O(depth)
~~~

时间复杂度完全合理，

递归调用栈仍可能被恶意输入放大。

Runtime parser / routing tree / protocol tree 都要注意。

---

# 五十八、XPUB Attach 为什么立刻调用 `xread_activated(pipe)`

流程：

~~~cpp
_dist.attach(pipe);
xread_activated(pipe);
~~~

原因：

~~~text
Pipe attach 时
subscription commands 可能已经排队
~~~

如果只等未来 activation event，

可能延迟建立订阅状态。

---

# 五十九、所以 Attach 不一定意味着“队列为空”

一个 endpoint 被注册进 socket pattern 时，

其队列可能已经包含：

~~~text
handshake/control data
~~~

需要立即 drain。

---

# 六十、XPUB `xread_activated()` 做什么

它循环：

~~~text
pipe->read(msg)
~~~

解析：

- subscribe；
- cancel；
- upstream user message；
- multipart subscription command。

然后更新：

~~~text
_subscriptions
~~~

和：

~~~text
_pending_data
~~~

---

# 六十一、为什么 XPUB 还能把 Subscription 事件交给 Application

XPUB 与普通 PUB 的区别之一：

~~~text
application can receive subscription notifications
~~~

所以控制面事件可以向上暴露。

---

# 六十二、Internal Command Representation 与 API Notification Representation 不一定相同

较新的 ZMTP 订阅命令可能使用：

~~~text
msg command flag/body
~~~

但 XPUB API 历史上期待：

~~~text
byte 0 = unsubscribe
byte 1 = subscribe
+
topic bytes
~~~

所以 Runtime 会：

~~~text
decode internal representation
→ reconstruct legacy API-visible representation
~~~

---

# 六十三、这与 ROUTER Synthetic Routing-ID Frame 是同一类设计

底层 message representation：

~~~text
不一定等于用户看到的 API envelope
~~~

Socket pattern 可以做：

~~~text
protocol-view translation
~~~

---

# 六十四、XPUB Pending Queue 为什么要保存 Metadata

当订阅事件要稍后：

~~~text
xrecv()
~~~

交给 application，

不能只保存 bytes。

还要保持：

~~~text
metadata ref
flags
~~~

直到真正交付。

---

# 六十五、这又是一个 Runtime Temporary Ownership 层

和 ROUTER `_prefetched_msg` 类似：

~~~text
底层 Pipe 已消费
应用尚未消费
~~~

中间 Runtime 必须持有完整语义状态。

---

# 六十六、普通 XPUB 的 Duplicate Subscribe 通知策略

`ZMQ_XPUB_VERBOSE=0` 默认：

~~~text
同一 prefix 第一次出现
→ notify app

后续 duplicate subscribe
→ 不重复 notify
~~~

---

# 六十七、为什么“第一次”看的是整个 MTrie Prefix，不是某一 Pipe

`mtrie.add(prefix, pipe)` 返回：

~~~text
!node->_pipes before insertion
~~~

也就是：

> **这个 prefix 之前在整个 XPUB 上有没有任何 Pipe。**

不是：

~~~text
这个 Pipe 自己是不是第一次
~~~

---

# 六十八、开启 `ZMQ_XPUB_VERBOSE`

duplicate subscribe 也：

~~~text
全部交给 application
~~~

这适合：

- proxy；
- subscription monitor；
- upstream aggregation；
- topology-aware logic。

---

# 六十九、`ZMQ_XPUB_VERBOSER` 更进一步

不仅 duplicate subscribe，

连 duplicate / non-final unsubscribe 也暴露。

所以：

~~~text
control-plane observability
~~~

可以从：

~~~text
collapsed state change
~~~

切换为：

~~~text
rawer event stream
~~~

---

# 七十、状态流和事件流是两个不同接口需求

有些 application 只关心：

~~~text
prefix 从 0→1 或 1→0
~~~

有些则关心：

~~~text
每个 subscriber 的 subscribe/unsubscribe
~~~

XPUB verbose 选项就是在两种语义间切换。

---

# 七十一、Pipe 终止时为什么可能产生 Unsubscribe Notification

如果某 Pipe 是某 prefix 的最后 subscriber：

~~~text
remove pipe
→ prefix set becomes empty
~~~

那么对 XPUB application 来说：

~~~text
该 topic interest 消失
~~~

这相当于逻辑 unsubscribe。

---

# 七十二、连接生命周期会产生控制面状态变化

也就是说：

~~~text
unsubscribe
~~~

不只来自显式用户 command。

还可能来自：

~~~text
Pipe termination
~~~

---

# 七十三、这在分布式系统非常常见

一个 membership state：

~~~text
member removed
~~~

来源可以是：

- explicit leave；
- timeout；
- disconnect；
- crash；
- lease expiration。

上层应该看到统一的：

~~~text
state transition
~~~

而不是只监听显式命令。

---

# 七十四、PUB 为什么继承 XPUB，但禁用 Receive

`pub_t` 基于 `xpub_t`，

但：

~~~text
xrecv()
→ ENOTSUP

xhas_in()
→ false
~~~

所以它仍复用：

- subscription tracking；
- distributor；
- HWM；
- matching。

只是不给 application 看 control-plane事件。

---

# 七十五、PUB 不是“没有订阅控制面”

即使应用不能 recv，

内部仍然必须处理：

~~~text
SUB → PUB subscription commands
~~~

否则就无法 source-side filtering。

---

# 七十六、PUB `xattach_pipe()` 为什么 `set_nodelay()`

源码说明：

~~~text
Don't delay pipe termination
as there is no one
to receive the delimiter.
~~~

因为 PUB application 不会走 receive 路径处理 delimiter。

生命周期策略必须适配 socket pattern。

---

# 七十七、这说明同一 Pipe Runtime 的 termination policy 也会被 Pattern 调整

抽象复用不等于：

~~~text
所有模式行为完全一样
~~~

Pattern 仍可以配置：

~~~text
nodelay / receive capability / drop policy
~~~

---

# 七十八、发布一条消息时 XPUB 实际做什么

`xsend()` 第一帧：

~~~text
if !_more_send
    dist.unmatch()
    subscriptions.match(topic, mark_as_matching)
~~~

---

# 七十九、MTrie Callback 不建立临时 Vector

命中的每个 Pipe：

~~~text
callback
→ dist.match(pipe)
~~~

直接把 Pipe swap 进：

~~~text
DIST matching prefix
~~~

---

# 八十、这是 Producer-to-Consumer Fusion

传统写法：

~~~text
Trie match
→ vector<pipe*>
→ iterate vector
→ distributor
~~~

这里：

~~~text
Trie traversal
→ callback
→ mutate distributor partition
~~~

少一层临时结果容器。

---

# 八十一、为什么这很适合 Hot Path

发布每条 message 都做匹配。

避免：

- allocation；
- temporary vector growth；
- second pass；
- duplicate Pipe handling structure。

---

# 八十二、DIST 自己负责去重 Matching

如果一个 Pipe 同时订阅：

~~~text
robot/
robot/arm/
~~~

一条：

~~~text
robot/arm/state
~~~

MTrie 沿路径可能两次 callback 同一 Pipe。

`dist_t::match()`：

~~~text
if index < _matching
    do nothing
~~~

因此同一 Pipe 最终只进入 matching prefix 一次。

---

# 八十三、这就是跨层分工

MTrie：

~~~text
一个 prefix 节点命中了哪些 Pipe
~~~

DIST：

~~~text
本条消息最终每个 Pipe 只能发送一次
~~~

---

# 八十四、为什么不让 MTrie 自己去重整个 Match Result

因为那又需要：

~~~text
per-query visited set
~~~

DIST 已经拥有 membership partition。

利用现有状态：

~~~text
O(1) duplicate suppression
~~~

更便宜。

---

# 八十五、发布匹配只在 Multipart 第一帧执行

~~~cpp
if (!_more_send)
    subscriptions.match(...)
~~~

后续 frame：

~~~text
不重新 match
~~~

---

# 八十六、为什么必须冻结 Matching Set

假设第一帧：

~~~text
A、B subscribed
~~~

frame 2 前：

~~~text
C subscribe
~~~

如果重算：

~~~text
C receives frame 2..
~~~

得到残缺 message。

---

# 八十七、反过来，中途 Unsubscribe 也不能立刻切断当前 Multipart

否则：

~~~text
A receives frame 1
unsubscribe
A misses frame 2
~~~

同样产生残缺消息。

---

# 八十八、Subscription State 属于 Future Message

当前 multipart 开始以后：

~~~text
matching set frozen
~~~

新 subscription/unsubscription：

~~~text
影响下一条完整 message
~~~

这和 DIST eligible generation 是同一种 transaction boundary。

---

# 八十九、所以完整消息是 PUB/SUB 的一致性单位

不是：

~~~text
frame
~~~

而是：

~~~text
complete multipart message
~~~

---

# 九十、为什么 Topic 通常取第一帧

XPUB 在 multipart 首帧：

~~~text
msg.data()
~~~

上做 prefix matching。

后续 frames 不再参与 topic decision。

所以应用协议通常约定：

~~~text
first frame contains routing/topic prefix
~~~

---

# 九十一、这是 Application Framing Contract

libzmq 不理解：

~~~text
JSON field "topic"
protobuf schema
ROS message type
~~~

它只看：

~~~text
first frame bytes
~~~

---

# 九十二、Topic Match 本质上是 Byte-prefix Match

没有 Unicode、路径分隔符等高级语义。

~~~text
"robot/"
~~~

只是 byte sequence。

---

# 九十三、这意味着 Topic Namespace 设计属于 Application

例如：

~~~text
robot/arm/
robot/arm2/
~~~

prefix 设计是否容易误匹配，

由应用命名规范决定。

---

# 九十四、为什么常用 `/` 分层

不是 ZeroMQ 强制。

而是因为：

~~~text
human-readable hierarchical prefix
~~~

方便利用 prefix matching。

---

# 九十五、SUB 接收端为什么还要 Local Filter

理论上 Publisher 已经 source-side filtering。

为什么 SUB 还做：

~~~text
_subscriptions.check()
~~~

？

因为数据路径可能经过：

- XSUB/XPUB proxy；
- raw forwarding；
- subscription propagation delay；
- invert matching；
-不同 pattern 组合。

---

# 九十六、Source Filtering 与 Sink Filtering 可以同时存在

source-side：

~~~text
减少无意义网络/queue流量
~~~

sink-side：

~~~text
保证最终 API 语义
~~~

---

# 九十七、不要把“重复过滤”简单视为浪费

两层可能承担不同责任：

~~~text
upstream optimization
vs
local correctness
~~~

---

# 九十八、SUB 与 XSUB 的 Filtering 行为不同

SUB constructor：

~~~cpp
options.filter = true;
~~~

XSUB 默认可以：

~~~text
不过滤数据
~~~

因此 XSUB 更适合：

~~~text
proxy / forwarding device
~~~

---

# 九十九、`xsub_t::match()` 很简单

~~~cpp
matching =
    subscriptions.check(
       msg->data(),
       msg->size());

return matching
       ^ invert_matching;
~~~

也就是：

~~~text
Trie prefix match
XOR invert flag
~~~

---

# 一百、`ZMQ_INVERT_MATCHING` 本质上是什么

正常：

~~~text
send/receive matching prefixes
~~~

invert：

~~~text
send/receive everything except matching prefixes
~~~

---

# 一百零一、XPUB 端如何实现 Invert

正常先：

~~~text
dist.match(matching pipes)
~~~

之后：

~~~cpp
_dist.reverse_match();
~~~

把：

~~~text
eligible - previous matching
~~~

变成新的 matching prefix。

---

# 一百零二、为什么 `reverse_match()` 可以这么便宜

DIST 已经把：

~~~text
matching
active
eligible
~~~

编码成 prefix partition。

所以补集操作不需要：

~~~text
new set
~~~

只需重排 boundary。

---

# 一百零三、但 PUB 与 SUB 两端 Invert 必须协调

如果 PUB：

~~~text
invert matching = true
~~~

而 SUB：

~~~text
normal filter
~~~

可能发生：

~~~text
Publisher intentionally sends nonmatching
Subscriber then rejects all of them
~~~

官方文档明确提示：

~~~text
both sides must be configured consistently
~~~

---

# 一百零四、这说明 Filter Policy 是 End-to-end Contract

不能只局部打开一个 option 就假设协议自动成立。

---

# 一百零五、SUB Local Filter 的 Fairness 风险

`xrecv()`：

~~~text
while true:
    fq.recv
    if matches:
        return
    else:
        discard whole multipart
        continue
~~~

---

# 一百零六、连续 Non-matching 流会怎样

理论上：

~~~text
永远有数据可读
~~~

但全部不匹配。

这个 while：

~~~text
可能长时间不返回
~~~

源码也明确指出：

~~~text
continuous stream of non-matching messages
can break non-blocking recv semantics
~~~

---

# 一百零七、这是很典型的 Owner-thread Starvation

逻辑上每次工作都很小：

~~~text
read
check
discard
~~~

但没有：

~~~text
per-call budget
~~~

连续 workload 可以无限延长一次调用。

---

# 一百零八、Non-blocking 不只意味着“不等 IO”

真正的响应性还需要：

~~~text
bounded local work
~~~

否则：

~~~text
没有 blocking syscall
~~~

仍然可以占住线程很久。

---

# 一百零九、Runtime Fairness 要同时考虑 I/O 与 CPU Filtering

低延迟系统不能只测：

~~~text
queue wait
~~~

还要测：

~~~text
owner-thread local loop budget
~~~

---

# 一百一十、机器人 Sensor Bus 中尤其明显

假设：

~~~text
高速 camera topic 500 MB/s
~~~

某 subscriber 实际只要：

~~~text
low-rate control topic
~~~

如果大量无关消息已经到达 subscriber owner thread，

本地 filter 会烧掉：

- CPU；
- cache；
- scheduling budget。

---

# 一百一十一、最优过滤位置通常越靠近 Source 越好

前提：

~~~text
source knows receiver interest
~~~

这正是 subscription control 反向传播的价值。

---

# 一百一十二、但 Source-side Filtering 有状态同步成本

Publisher 必须维护：

~~~text
remote interest state
~~~

并处理：

- connect；
- disconnect；
- replay；
- duplicate；
- race；
- handover；
- proxy。

所以没有免费的优化。

---

# 一百一十三、PUB/SUB 的本质是“用控制面状态换数据面效率”

没有 subscription propagation：

~~~text
source broadcasts all
sink filters
~~~

简单但浪费带宽。

有 propagation：

~~~text
control state more complex
data path more selective
~~~

---

# 一百一十四、慢 Subscriber 遇到 HWM 时会发生什么

MTrie 只回答：

~~~text
语义上应该发给谁
~~~

DIST 再看：

~~~text
这个 Pipe 当前是否 eligible / writable
~~~

---

# 一百一十五、默认 XPUB `_lossy = true`

`xsend()`：

~~~cpp
if (_lossy || _dist.check_hwm())
    send_to_matching(...)
~~~

所以默认：

~~~text
不要求所有 matching pipe 都有容量
~~~

---

# 一百一十六、某个 Matching Pipe HWM 满

DIST write 失败：

~~~text
该 Pipe 从 matching/active/eligible 集合移出
~~~

其他订阅者仍继续收到。

---

# 一百一十七、这就是 Slow Subscriber Isolation

一个慢 subscriber：

~~~text
不把整个 publisher 拖死
~~~

代价：

~~~text
它自己可能丢消息
~~~

---

# 一百一十八、`ZMQ_XPUB_NODROP` 改变什么

打开后：

~~~text
_lossy = false
~~~

发送前：

~~~text
_dist.check_hwm()
~~~

要求：

~~~text
所有 matching Pipe
都能通过 HWM admission
~~~

---

# 一百一十九、任意一个 Matching Pipe 满

返回：

~~~text
EAGAIN
~~~

消息不进入 fan-out。

---

# 一百二十、NODROP 把语义从“独立降级”改成“组 Admission”

默认：

~~~text
per-subscriber loss isolation
~~~

NODROP：

~~~text
all matching receivers
must be currently admissible
~~~

---

# 一百二十一、这类似 Barrier-style Fan-out Admission

可以理解：

~~~text
select group
→ check all capacity
→ only then commit
~~~

---

# 一百二十二、但 `check_hwm()` 与真正 Write 之间仍然是 Runtime 协议

它不是远端 ACK。

只是当前 owner-thread 下：

~~~text
local Pipe capacity check
~~~

---

# 一百二十三、NODROP 仍然不是 End-to-end Reliability

它没有保证：

- TCP 永不失败；
- remote process 不崩；
- application 一定 consume；
- message 不在未来关闭时丢失。

---

# 一百二十四、它只是强化 Local Admission Contract

和 ROUTER_MANDATORY 类似：

~~~text
更多 silent loss
→ 转化为 application-visible retry condition
~~~

---

# 一百二十五、PUB 为什么 `has_out()` 总是 True

DIST：

~~~text
has_out()
→ true
~~~

因为 lossy PUB 可以：

~~~text
没有 subscriber
→ drop

subscriber blocked
→ drop for blocked targets
~~~

所以 application-level send 总能“被处理”。

---

# 一百二十六、Ready 不等于 Delivered

这一点和 ROUTER 默认模式一样。

~~~text
API writable
~~~

只代表：

~~~text
socket pattern 有定义好的处理路径
~~~

包括：

~~~text
drop
~~~

---

# 一百二十七、为什么应用必须理解 Pattern Semantics

同一个：

~~~text
zmq_send success
~~~

在不同 pattern 下不代表相同可靠性。

---

# 一百二十八、Subscription Notification 也有 Backpressure

XSUB `send_subscription()`：

~~~cpp
bool sent =
    pipe->write(&msg);

if (!sent)
    msg.close();
~~~

源码注释明确：

~~~text
SNDHWM reached
→ drop subscription message
~~~

---

# 一百二十九、这意味着 Control Plane 也不是无限可靠

这是一个非常重要的现实：

> **订阅状态虽然是控制面，但它仍然复用有界 Pipe，因此本身也受 HWM 影响。**

---

# 一百三十、为什么这不一定永久错误

因为：

~~~text
new attach
hiccup
~~~

会执行：

~~~text
full subscription replay
~~~

所以增量控制消息可以丢，

后续 snapshot replay 仍有机会重新同步。

---

# 一百三十一、这是 Delta + Snapshot Recovery Pattern

平时：

~~~text
incremental subscribe/unsubscribe
~~~

异常/重建：

~~~text
replay complete current state
~~~

---

# 一百三十二、这个模式非常常见

例如：

- service registry；
- routing table replication；
- configuration watch；
- actor membership；
- cache coherence。

---

# 一百三十三、为什么状态型控制面最好能 Replay

如果只靠：

~~~text
每个 delta exactly once
~~~

系统恢复会非常脆弱。

更稳健的是：

~~~text
authoritative state
+
best-effort/ordered deltas
+
snapshot resync
~~~

---

# 一百三十四、XPUB Manual 模式做什么

`ZMQ_XPUB_MANUAL=1`：

~~~text
收到 subscription request
~~~

Runtime 不再自动把它直接加入真实 `_subscriptions`。

而是先记录：

~~~text
_manual_subscriptions
_pending_pipes
~~~

交给 application 决策。

---

# 一百三十五、为什么需要 Manual Subscription

某些系统想在订阅生效前做：

- ACL；
- authentication-derived policy；
- LVC；
- resource admission；
- dynamic authorization。

---

# 一百三十六、Manual 模式把 Control-plane Decision 上交给 Application

默认：

~~~text
request
→ Runtime accepts
~~~

manual：

~~~text
request
→ application observes
→ application chooses subscribe/unsubscribe
~~~

---

# 一百三十七、`_last_pipe` 为什么危险

Manual API 后续：

~~~text
setsockopt(ZMQ_SUBSCRIBE)
~~~

需要知道：

~~~text
刚才那条 request 来自哪条 Pipe
~~~

所以 XPUB 保存：

~~~text
_last_pipe
~~~

---

# 一百三十八、Pipe 终止时必须清 `_last_pipe`

否则 application 稍后接受 subscription：

~~~text
可能把已经销毁的 Pipe
重新插回 mtrie
~~~

所以：

~~~cpp
if (pipe == _last_pipe)
    _last_pipe = NULL;
~~~

---

# 一百三十九、这是 Callback/Deferred Decision 常见 Lifetime 风险

一旦：

~~~text
event arrives now
decision happens later
~~~

就必须确保：

~~~text
event source still alive
~~~

或：

~~~text
later detect source retired
~~~

XPUB 用：

~~~text
dist.has_pipe(last_pipe)
~~~

再次验证。

---

# 一百四十、为什么 `xrecv()` 还要 `dist.has_pipe()`

pending subscription event 排队后，

对应 Pipe 可能已经 terminated。

所以取出事件时：

~~~text
if last_pipe no longer in distributor
→ clear last_pipe
~~~

防止后续 manual action 使用 stale endpoint。

---

# 一百四十一、这就是 Deferred-event Revalidation

不要假设：

~~~text
event creation time valid
→ event consumption time仍 valid
~~~

异步 Runtime 必须重新验证。

---

# 一百四十二、`ZMQ_XPUB_MANUAL_LAST_VALUE` 又解决什么

LVC：

~~~text
Last Value Cache
~~~

常见逻辑：

~~~text
新 subscriber 出现
→ 立即发该 topic 最新值
~~~

---

# 一百四十三、如果普通 Manual 模式直接 Broadcast Cached Value

可能：

~~~text
已有 subscribers
也再次收到同一 cached value
~~~

造成 duplicate。

---

# 一百四十四、MANUAL_LAST_VALUE 的思路

保存：

~~~text
last requesting pipe
~~~

随后第一次 publish：

~~~text
只 match last pipe
~~~

而不是所有当前 subscription pipes。

---

# 一百四十五、所以 `_send_last_pipe` 是一次性 Target Override

它不是长期 routing policy。

更像：

~~~text
next matching operation
→ restrict to one recently admitted pipe
~~~

---

# 一百四十六、这再次体现“Current Operation Context”

很多 Runtime 都需要：

~~~text
global registry
+
one-shot operation-local override
~~~

不要把两者混成长期状态。

---

# 一百四十七、Subscription Disconnect 清理顺序

普通 XPUB：

~~~text
pipe terminated
    ↓
remove pipe from all trie prefixes
    ↓
for prefixes becoming empty:
    emit unsubscription notification
    ↓
dist.pipe_terminated(pipe)
~~~

---

# 一百四十八、为什么先清 Subscription Registry 再清 Distributor

两者都保存：

~~~text
pipe*
~~~

但职责不同。

MTrie：

~~~text
future semantic discovery
~~~

DIST：

~~~text
current scheduling membership
~~~

---

# 一百四十九、理想 Retirement 顺序

~~~text
remove semantic discoverability
→ remove scheduling eligibility
→ continue higher lifecycle termination
~~~

这与其他 registry/quiescence 章节形成同一规律。

---

# 一百五十、`mtrie.rm(pipe, callback)` 为什么要遍历整棵 Trie

没有：

~~~text
pipe → prefixes reverse index
~~~

所以要查：

~~~text
所有节点
~~~

删除这个 Pipe。

---

# 一百五十一、这是空间换时间的选择

如果维护反向索引：

~~~text
pipe → set<prefix>
~~~

终止会更快。

但：

- insert/remove 更复杂；
- 双向一致性成本更高；
- 内存更多。

当前设计选择：

~~~text
termination cleanup cost
换更简单热路径
~~~

---

# 一百五十二、为什么这是合理取舍

通常：

~~~text
publish/match
~~~

频率远高于：

~~~text
subscriber connection termination
~~~

所以优化 hot path。

---

# 一百五十三、不要平均优化所有路径

Runtime 设计应先区分：

~~~text
per-message hot path
connection lifecycle cold path
~~~

然后决定数据结构。

---

# 一百五十四、MTrie Remove 用显式 Stack 的另一原因

Pipe termination 时：

~~~text
整个 Trie 扫描
~~~

可能很深。

使用 heap-backed stack：

~~~text
避免深 subscription namespace
打爆 C++ call stack
~~~

---

# 一百五十五、为什么 `std::set<pipe_t*>`

同一 prefix：

~~~text
一个 Pipe 只能出现一次
~~~

需要去重。

`set` 自动保证：

~~~text
unique endpoint membership
~~~

---

# 一百五十六、它并不是最 cache-friendly 的结构

`std::set`：

~~~text
node-based tree
~~~

pointer chasing 较多。

但每个 prefix 实际 subscriber 数通常可能不大，

且 mutation/uniqueness semantics 简单。

---

# 一百五十七、如果极端高 fan-out，可考虑不同 Value Container

例如：

- small_vector；
- flat_set；
- sorted vector；
- intrusive vector；
- bitmap by stable endpoint id。

但需要重新评估：

- insert/remove；
- pointer stability；
- lifecycle；
- matching iteration。

---

# 一百五十八、源码学习重点不是“std::set 一定最好”

而是：

> **先看访问模式：prefix lookup 是 Trie，节点内 endpoint uniqueness 才交给 set。**

两层解决不同维度。

---

# 一百五十九、Subscription Pipeline 可以拆成五层

第一层：

~~~text
Intent
SUB local trie
~~~

第二层：

~~~text
Replication
subscribe/cancel reverse messages
~~~

第三层：

~~~text
Registry
XPUB mtrie prefix → pipes
~~~

第四层：

~~~text
Selection
DIST matching prefix
~~~

第五层：

~~~text
Resource
Pipe HWM / active
~~~

---

# 一百六十、每层回答一个问题

Intent：

~~~text
我想要什么？
~~~

Replication：

~~~text
对端知道了吗？
~~~

Registry：

~~~text
哪些 endpoint 对这个 prefix 有兴趣？
~~~

Selection：

~~~text
这条完整 message 的目标集合是谁？
~~~

Resource：

~~~text
这些 endpoint 当前能不能接？
~~~

---

# 一百六十一、PUB/SUB 的错误往往来自跨层混淆

比如：

~~~text
prefix exists in mtrie
~~~

不代表：

~~~text
pipe currently writable
~~~

也不代表：

~~~text
current multipart should dynamically add it
~~~

更不代表：

~~~text
remote app一定最终收到
~~~

---

# 一百六十二、Publisher-side Matching 是 Semantic State

~~~text
topic prefix
→ interested pipes
~~~

---

# 一百六十三、Distributor 是 Scheduling State

~~~text
interested + eligible
→ current message target set
~~~

---

# 一百六十四、Pipe 是 Resource State

~~~text
queue capacity
lifecycle
~~~

三层必须分开。

---

# 一百六十五、为什么 Matching Callback 先 MTrie 后 DIST

因为只有：

~~~text
semantic target
~~~

才有资格进入：

~~~text
resource/scheduler selection
~~~

---

# 一百六十六、Slow Subscriber 为什么不会被 MTrie 删除

HWM full 是：

~~~text
temporary resource condition
~~~

不是：

~~~text
subscription semantic revoked
~~~

所以：

~~~text
mtrie membership still exists
~~~

但 DIST：

~~~text
temporarily removes pipe from eligible/active
~~~

---

# 一百六十七、这就是 Semantic Membership 与 Resource Membership 分离

subscriber 仍然：

~~~text
wants topic
~~~

只是：

~~~text
currently cannot keep up
~~~

---

# 一百六十八、Peer Progress 后如何恢复

Pipe 收到：

~~~text
activate_write
~~~

然后：

~~~text
dist.activated(pipe)
~~~

重新进入：

~~~text
eligible / active
~~~

---

# 一百六十九、不需要重新发 Subscribe

因为：

~~~text
semantic mtrie state没有消失
~~~

只恢复 scheduler membership。

---

# 一百七十、这正是分层的收益

如果把：

~~~text
subscription
+
capacity
~~~

混成一个 bool，

HWM full 时就可能错误地删除订阅语义。

---

# 一百七十一、订阅状态什么时候才真正删除

- explicit unsubscribe；
- Pipe termination；
- manual policy reject/remove。

不是：

~~~text
temporary HWM
~~~

---

# 一百七十二、Multipart Send 与 Subscription Change 的时间关系

当前 message：

~~~text
matching snapshot already materialized in DIST
~~~

同时收到 subscribe/unsubscribe：

~~~text
MTrie 可以更新
~~~

但：

~~~text
当前 DIST matching
~~~

保持 transaction consistency。

---

# 一百七十三、这是 Snapshot-at-Transaction-Start

不一定真的复制一个 vector snapshot。

而是：

~~~text
DIST prefix partition
~~~

充当当前 transaction 的 materialized target snapshot。

---

# 一百七十四、物理复制不是实现 Snapshot 的唯一办法

只要：

~~~text
future registry changes
不会改变 current participant set
~~~

就具备 snapshot 语义。

---

# 一百七十五、这对高性能系统很重要

可以做到：

~~~text
logical snapshot
without copying whole set
~~~

通过：

- generation；
- prefix partition；
- epoch；
- immutable pointer；
- versioning。

---

# 一百七十六、为什么新恢复 Pipe 在 Multipart 中只进 Eligible

前一章已经分析：

~~~text
DIST _more=true
~~~

时：

~~~text
activated pipe
→ eligible only
~~~

而不是 active。

这保证：

~~~text
current message target set stable
~~~

---

# 一百七十七、PUB/SUB 把这个机制用在 Interest + Capacity 交叉点

新 subscriber：

~~~text
semantic registry can update
~~~

但当前 multipart：

~~~text
不会半途加入
~~~

---

# 一百七十八、Invert Matching 也只在 First Frame 计算

所以：

~~~text
complement set
~~~

同样被冻结到完整 message boundary。

---

# 一百七十九、完整 Publication Path

~~~text
application send first frame
        ↓
XPUB _more_send == false
        ↓
dist.unmatch()
        ↓
mtrie.match(topic)
        ↓
for each matching Pipe:
    dist.match(pipe)
        ↓
optional reverse_match()
        ↓
optional NODROP check_hwm()
        ↓
dist.send_to_matching(frame)
        ↓
matching Pipe writes
        ↓
more?
   yes → keep current matching generation
   no  → unmatch, next message can recompute
~~~

---

# 一百八十、完整 Subscription Path

~~~text
application:
SUBSCRIBE "robot/"
        ↓
SUB constructs control msg
        ↓
XSUB local trie add/refcount
        ↓
DIST send control to upstream Pipe(s)
        ↓
XPUB xread_activated
        ↓
decode subscribe
        ↓
mtrie.add("robot/", pipe)
        ↓
future publication match sees this Pipe
~~~

---

# 一百八十一、完整 Reconnect/Resync Path

~~~text
XSUB already has:
robot/
camera/
imu/
        ↓
new Pipe attach / hiccup
        ↓
trie.apply()
        ↓
reconstruct subscribe msgs
        ↓
send all current prefixes
        ↓
XPUB rebuilds per-Pipe interests
~~~

---

# 一百八十二、完整 Disconnect Path

~~~text
subscriber Pipe terminates
        ↓
XPUB mtrie.rm(pipe)
        ↓
remove Pipe from every prefix
        ↓
emit final-unsubscribe events where needed
        ↓
dist.pipe_terminated(pipe)
        ↓
future publications cannot discover/schedule it
~~~

---

# 一百八十三、这与 Service Discovery 很像

Subscription：

~~~text
subscriber interest registration
~~~

Pipe termination：

~~~text
lease/member disappearance
~~~

MTrie：

~~~text
indexed registry
~~~

Publisher match：

~~~text
query registry
~~~

---

# 一百八十四、但 PUB/SUB 是 Data-plane Optimized Registry

它不是通用数据库。

数据结构和 API 都围绕：

~~~text
每条消息快速 prefix match
~~~

优化。

---

# 一百八十五、为什么机器人 Topic Bus 很适合借鉴这层结构

例如 topic：

~~~text
robot/pose
robot/camera/front
robot/lidar/points
robot/control/status
~~~

subscription：

~~~text
robot/camera/
~~~

Trie 非常自然。

---

# 一百八十六、但高频大数据 Topic 更要 Source-side Filtering

Image/PointCloud：

~~~text
payload huge
~~~

如果先广播后 filter：

~~~text
网络/IPC/memory bandwidth 已经浪费
~~~

---

# 一百八十七、反向 Interest Propagation 的收益在大 Payload 上更明显

控制面：

~~~text
几十 byte subscription
~~~

换来：

~~~text
避免 MB 级 payload 无效传输
~~~

---

# 一百八十八、但不能忽略 Subscription Churn

如果每毫秒：

~~~text
大量 subscribe/unsubscribe
~~~

Trie mutation + control replication 也会变成成本。

---

# 一百八十九、系统要看两个速率

\[
R_{data}
\]

与：

\[
R_{subscription}
\]

PUB/SUB 设计默认通常：

~~~text
data rate ≫ subscription churn
~~~

因此优化 match hot path。

---

# 一百九十、如果 Interest 变化比 Data 还快

可能更适合：

- query-based pull；
- shared state table；
- latest-value store；
- direct routing；
- request/reply。

---

# 一百九十一、不要把 PUB/SUB 当成万能消息模式

它适合：

~~~text
one-to-many
topic-prefix interest
asynchronous delivery
possible lossy semantics
~~~

不适合天然表达：

- exactly-once；
- request completion；
- command ACK；
- per-message transaction commit。

---

# 一百九十二、控制命令尤其不能盲目使用默认 PUB

机器人控制：

~~~text
set motor current
emergency stop
mode switch
~~~

如果不能容忍 silent drop，

默认 lossy PUB 可能不是合适语义。

---

# 一百九十三、Telemetry 与 Command 应区分

Telemetry：

~~~text
最新状态重要
偶尔丢一帧可接受
~~~

PUB/SUB 很合适。

Command：

~~~text
每条必须确认
~~~

更适合：

- ROUTER/DEALER + ACK；
- request/reply；
- sequence + state reconciliation。

---

# 一百九十四、Slow Subscriber Policy 必须按 Topic 类型设计

Camera：

~~~text
drop old
keep latest
~~~

Logger：

~~~text
可能希望完整
~~~

Safety event：

~~~text
不能无声丢
~~~

单一 queue policy 不一定适合全部 topic。

---

# 一百九十五、这也是为什么 Runtime 机制与业务策略要分层

Runtime 提供：

- HWM；
- lossy；
- NODROP；
- conflate；
- matching。

应用决定：

~~~text
哪类数据用哪个 policy
~~~

---

# 一百九十六、一个可迁移的机器人 Topic Runtime 结构

~~~text
Local Interest Trie
        ↓
Interest Replication
        ↓
Publisher Prefix Registry
        ↓
Target Set Materialization
        ↓
Per-link Capacity
        ↓
Payload Fan-out
~~~

---

# 一百九十七、其中每层可以独立替换

Interest Trie：

~~~text
prefix trie
→ regex / exact key / semantic label
~~~

Replication：

~~~text
Pipe command
→ gossip / central registry
~~~

Target Set：

~~~text
DIST prefix partition
→ immutable vector
~~~

Capacity：

~~~text
HWM
→ credits / token bucket
~~~

---

# 一百九十八、真正值得学的是边界，不是具体类名

边界一：

~~~text
intent state
vs
wire event
~~~

边界二：

~~~text
semantic membership
vs
resource readiness
~~~

边界三：

~~~text
future registry
vs
current transaction
~~~

边界四：

~~~text
control-plane state
vs
application-visible notification
~~~

---

# 一百九十九、PUB/SUB 最值得记住的十二条规则

第一：

> **Subscriber 本地 Trie 与 Publisher MTrie 保存的是不同维度：前者是 prefix refcount，后者是 prefix→Pipe set。**

第二：

> **Subscription 是可恢复状态，不只是一次性事件；新连接和 hiccup 必须能从本地 truth 重放完整订阅集合。**

第三：

> **数据向下游流，interest/control 反向上游流，用控制面复杂度换数据面选择性。**

第四：

> **Trie 只负责语义匹配；DIST 负责当前消息 target-set materialization；Pipe 负责容量。**

第五：

> **语义订阅不能因为临时 HWM 满而删除；resource ineligible 与 semantic unsubscribe 是两种状态。**

第六：

> **匹配集合在 multipart 第一帧冻结，新订阅/退订只影响下一条完整消息。**

第七：

> **同一 Pipe 命中多个 prefix 时，DIST matching prefix 提供 O(1) 级的重复抑制，不必额外建 visited set。**

第八：

> **控制面也可能受 HWM；因此状态型协议最好有 snapshot replay，而不是只依赖每个 delta 永不丢失。**

第九：

> **Pipe termination 必须从 subscription registry 删除未来 discoverability，再退出 distributor scheduling membership。**

第十：

> **本地 filtering 的 while-loop 也可能造成 owner-thread starvation；non-blocking 不等于本地工作量有界。**

第十一：

> **XPUB verbose/manual/NODROP 改变的是控制面观测、订阅决策和本地 admission contract，不应被误解为端到端可靠性。**

第十二：

> **高性能 Trie 的价值不仅在 Big-O，还在节点布局：0-child / 1-child / compact range-table 根据实际分支动态切换。**

---

# 二百、最终心智模型

~~~text
SUB / XSUB
==========
application intent
      |
      v
 local trie
prefix → refcount
      |
      +------ local receive filter
      |
      +------ replay snapshot
      |
      v
subscribe/cancel control
      |
      |  reverse direction
      v

XPUB / PUB
==========
 xread_activated
      |
      v
    mtrie
prefix → set<pipe*>
      |
      | first frame topic match
      v
   dist_t
matching ⊆ active ⊆ eligible
      |
      | HWM / fan-out
      v
    Pipe(s)
      |
      v
subscriber data
~~~

如果只记一个结论：

> **libzmq 的 PUB/SUB 不是“广播 + 字符串过滤”，而是一套状态复制系统：SUB 保存可重放的本地 interest，XPUB 把 interest 物化成 prefix→Pipe 注册表，DIST 在完整消息边界上把语义匹配转成当前 target set，Pipe 再用 HWM 决定资源资格；连接变化、重复订阅和慢消费者都分别在自己的状态层处理。**
