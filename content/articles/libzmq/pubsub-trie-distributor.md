# PUB / SUB：订阅 Trie、匹配集合与分发器

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

PUB/SUB 的关键不是广播本身，而是：订阅状态怎样从 Subscriber 反向传播到 Publisher，再让 Publisher 只选择匹配的 pipes。

## XPUB attach 后为什么先读订阅

`xpub_t::xattach_pipe()` 先 `_dist.attach(pipe_)`，随后主动调用 `xread_activated(pipe_)`，把 pipe 中已经排队的 subscribe/cancel command 读出来。

订阅关系被存进 `_subscriptions`，其值最终关联具体 pipe。

## 发布时不是遍历所有订阅者再做字符串 if

`xsend()` 在 multipart 首帧到来时：

~~~cpp
_dist.unmatch ();
_subscriptions.match (msg_->data (), msg_->size (), mark_as_matching, this);
~~~

Trie 匹配阶段只负责找出哪些 pipes 命中 topic prefix；callback `mark_as_matching` 再把这些 pipe 通过 `dist_t::match()` 移到 matching prefix。

随后：

~~~cpp
_dist.send_to_matching (msg_);
~~~

因此路径是：

~~~text
topic bytes
  |
subscription trie
  |
matching pipe set
  |
dist_t
  |
pipe/HWM
~~~

## 为什么订阅命令是反向流动的

XSUB 端保存本地 subscription cache，新 pipe attach 或 hiccup 后会把所有 cached subscriptions 重新发给 upstream。

这说明 PUB/SUB 不只是单向数据流：

~~~text
data:          XPUB -> XSUB
subscription:  XSUB -> XPUB
~~~

控制信息反向传播，才能让发布端建立按 pipe 的匹配表。

## SUB 接收端为什么还会过滤

`xsub_t::xrecv()` 从 `_fq` 公平读取消息，再用本地 subscription tree 检查。未匹配消息会连同剩余 multipart frames 一起丢弃。

也就是说过滤可以发生在不同层，取决于 transport、proxy 和 socket 组合。

## XSUB 为什么必须缓存 subscription，而不能只转发一次命令

`xsub_t::xattach_pipe()` 会把本地已经存在的全部 subscriptions 重放给新 upstream pipe；发生 hiccup 后也会再次发送。

因此 subscription tree 同时承担两种角色：

~~~text
local filtering truth
+
replayable control-plane state
~~~

如果 subscribe 只是“一次性网络包”，重连后新 XPUB 根本无法恢复当前订阅关系。

## XPUB 保存的是 prefix -> pipe 集合

XPUB 侧需要回答的不只是“某个 prefix 是否有人订阅”，还要知道**具体哪些 pipe**订阅了它。所以使用 multi-value trie，把 prefix 节点关联到多个 `pipe_t*`。

发布一条 `robot/arm/joint/3/state` 时，不必线性遍历所有 subscription 做字符串 `starts_with`。Trie 沿 topic bytes 前进，并在每个命中的 prefix 节点上枚举关联 pipe。

这些 pipe 不会另外复制到临时 vector；callback 直接调用 `dist_t::match()`，把目标交换进 matching prefix。于是：

~~~text
MTrie 决定谁命中
DIST 决定哪些命中 pipe 当前可写
Pipe 决定单连接容量与 ownership
Socket option 决定过载时对应用暴露什么语义
~~~

## multipart publish 为什么不能每帧重新匹配

假设第一帧匹配出 A、B 两个 subscriber。发送到 frame 2 之前，C 新订阅了同一 topic。

如果每帧重新匹配，C 会只收到后半条 multipart message；反过来，某个 subscriber 中途退订也可能只收到前半条。

因此 matching set 必须在完整 message 边界上保持稳定。这里再次出现同一个设计原则：调度单位是完整 message，不是 frame。

## 本地过滤为什么可能消耗 owner-thread 公平性

`xsub_t::xrecv()` 对不匹配消息会继续循环读取，甚至把该 multipart 的剩余 frame 全部弹出。源码保留了一个 TODO：持续到来的 non-matching 流可能让这个循环长时间占据执行线程，从而破坏 non-blocking 调用的直觉。

这说明“过滤放在哪里”不仅影响 CPU 成本，也直接影响 Runtime fairness。把 filter 留到 subscriber owner thread，就必须把最坏过滤工作量纳入延迟分析。


## lossy 与 HWM

XPUB 最终调用 dist，而 dist 中某个 pipe 可能因 HWM 不可写。PUB/XPUB 的 `_lossy` 选项决定是否允许在慢订阅者处丢消息，或要求 HWM 检查通过。

因此 PUB/SUB 的“慢消费者策略”并不是一句“PUB 会丢消息”就能概括，而是 subscription matching、dist active set、pipe HWM 和 socket option 共同决定。

## 最值得借鉴的结构

这里再次出现职责分离：

~~~text
trie       决定谁匹配
dist_t     决定向哪些 active pipes 分发
pipe_t     决定单条连接是否还能写
socket     决定过载时暴露什么语义
~~~

机器人系统里的 topic/filter/event bus 也应该尽量把匹配、调度、容量控制和错误语义分层，而不是全部塞进一个 Publisher 类。
