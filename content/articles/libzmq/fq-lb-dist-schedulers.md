# FQ / LB / DIST：消息模式背后的三个调度器

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

ZeroMQ 的 socket pattern 并不是每个类型都从零实现。真正复用度很高的是三种调度器：`fq_t` 负责输入公平队列，`lb_t` 负责输出负载均衡，`dist_t` 负责一对多分发。

## fq_t：从多个 Pipe 公平读取

`fq_t` 维护 `_pipes / _active / _current / _more`。active pipes 被放在数组前半段，inactive pipes 被交换到后半段。

~~~text
[ active pipes | inactive pipes ]
0             _active
~~~

`recvpipe()` 对 active 区间 round-robin。某 pipe 读空，就通过 swap 把它移出 active 区。multipart 消息期间 `_more=true`，不会轮换到下一 pipe，从而保证一条 multipart message 的原子性。

## lb_t：从多个可写 Pipe 中轮询

`lb_t` 同样维护 active prefix 与 current cursor，但方向相反：它选择一个可写 pipe。写完整条 multipart 后才 `flush()` 并推进 current。若中途 peer 消失，则 rollback，必要时进入 `_dropping`，把剩余 multipart frames 丢掉，避免后续新连接收到残缺消息。

## dist_t：一份消息复制到多个 Pipe

`dist_t` 更有意思。它把同一个 `array_t<pipe_t>` 划成多个连续区域：

~~~text
[ matching | active nonmatching | eligible inactive | passive ]
 0       _matching          _active            _eligible
~~~

`match()` 不创建新集合，而是通过 `swap()` 把 pipe 移进 matching prefix。`send_to_matching()` 只遍历 matching 区。

## array_t 为什么能 O(1) erase/swap

`array_t` 底层是 `std::vector<T*>`，但元素继承 `array_item_t<ID>`，对象内部保存自己在数组中的 index。删除时用最后一个元素覆盖目标位置，再 `pop_back()`；因此不保持顺序，换来 O(1) erase。

这正适合 Runtime 的 active/inactive partition：顺序不重要，集合边界和快速搬移更重要。

## multipart 为什么会影响调度

公平调度的单位不是 frame，而是完整 message。否则 A 的 frame1、B 的 frame1、A 的 frame2 交错后，上层无法恢复原子消息语义。

因此 `_more` 看似只是 bool，实际上把底层 frame scheduler 提升成 message scheduler。

## active prefix 为什么能把激活/失活做成 O(1)

`fq_t`、`lb_t` 和 `dist_t` 都依赖 `array_t<T, ID>`。它底层仍然是 `std::vector<T*>`，但元素通过 `array_item_t<ID>` 在对象内部保存自己当前位于数组中的 index。

因此 pipe 失活时不需要线性搜索：

~~~text
[ A B C | D E ]
      ^
   active=3
~~~

如果 B 失活，只需把 B 与 active 区最后一个元素 C 交换，再执行 `active--`：

~~~text
[ A C | B D E ]
    ^
 active=2
~~~

重新激活也对称：拿到 pipe 自己保存的 index，和 `_active` 位置交换，再 `active++`。这是一种 intrusive container 设计：元素牺牲稳定顺序，换来 O(1) 的集合迁移。

`ID` 的作用也很具体。同一个 `pipe_t` 可能同时存在于 FQ、LB、DIST 等多个 `array_t` 中，每个容器都需要自己独立的一份 index；不同模板 ID 就是为同一对象提供多组 array slot。

## FQ 的公平单位为什么必须是完整 message

`fq_t::recvpipe()` 成功读到 frame 后会更新 `_more`。只要当前 frame 还有 `more` 标志，就不会推进 `_current`。

这意味着：

~~~text
A1 [more]
A2 [more]
A3 [last]
~~~

必须完整从同一条 pipe 取完，才能切到 B。否则若按 frame round-robin：

~~~text
A1 -> B1 -> A2 -> C1 -> A3
~~~

上层已经失去 multipart message 的原子边界。

源码里如果已经处于 `_more=true`，后续 frame 却突然拿不到，会直接断言，因为这不再是普通的“暂时无消息”，而意味着底层 message publication 协议被破坏。

## LB 的 `_dropping` 解决断线中的半条消息

发送侧更棘手。假设：

~~~text
frame 1 [more] -> pipe A
frame 2 [more] -> pipe A
pipe A terminates
frame 3 [last] -> ?
~~~

不能把 frame 3 改投 pipe B，否则 B 会收到一条缺头的 multipart message。

所以 `lb_t` 在当前 multipart 的目标 pipe 消失后进入 `_dropping`，继续消费并丢弃剩余 frame，直到最后一帧结束。这不是简单的重试策略，而是在保护“完整 message 只能属于一个 peer”的不变量。

若 pipe 尚未销毁，但中途 write 失败，则先调用 `rollback()` 撤销尚未形成 completed publication boundary 的尾部；这又依赖 ypipe 的 `incomplete` / `flush` 机制。

## DIST 的四段数组为什么不是四个容器

`dist_t` 用三个边界把同一数组切成：

~~~text
[ matching | active nonmatching | eligible inactive | passive ]
0       _matching          _active            _eligible
~~~

`matching` 表示当前 message 的目标集合；`active` 表示此刻可写；`eligible` 表示下一条完整 message 可以参与；`passive` 表示达到 HWM、等待重新激活。

multipart 发送过程中刚 attach 或刚恢复的 pipe 只能进入 eligible 区，不能立刻进入 active/matching。否则它会从 frame 2 或 frame 3 才开始收到消息，破坏 multipart 原子性。完整 message 结束后，`_active = _eligible`，新 pipe 才参与下一条消息。

`match()` 也不创建新的 `std::vector<pipe_t*>`，只是把命中的 pipe swap 到 matching prefix。topic match 结果因此直接编码在数组区间 `[0, _matching)` 中。

## DIST 广播为什么不等于 N 次深拷贝

普通大消息 fan-out 时，`dist_t` 会先给 message buffer 增加引用计数，再把共享 payload handle 写入多条 pipe；某条 write 失败时再相应 `rm_refs()`。

所以广播成本应拆成：

~~~text
matching / scheduling
+ per-pipe msg handle
+ refcount
+ transport-specific copy
~~~

不能看到 N 个订阅者就直接推断一定发生 N 次大 payload memcpy。


## 一个共同设计模式

三个调度器都没有建立复杂链表或 heap，而是反复使用：

~~~text
vector/array + prefix partition + swap + cursor
~~~

这是很值得借鉴的数据结构策略：如果只关心“活跃集合/非活跃集合/匹配集合”，不要求稳定顺序，那么连续数组 + 边界索引通常比通用容器更直接。
