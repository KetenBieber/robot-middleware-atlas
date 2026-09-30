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

## 一个共同设计模式

三个调度器都没有建立复杂链表或 heap，而是反复使用：

~~~text
vector/array + prefix partition + swap + cursor
~~~

这是很值得借鉴的数据结构策略：如果只关心“活跃集合/非活跃集合/匹配集合”，不要求稳定顺序，那么连续数组 + 边界索引通常比通用容器更直接。
