# yqueue / ypipe：把单 Writer、单 Reader 写进数据结构

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

这一页的重点不是“lock-free 一定比 mutex 快”，而是：**先规定 ownership topology，再让数据结构利用这个前提减少共享状态。**

## yqueue 的线程分工

源码注释明确：一个线程使用 push/back，另一个线程使用 pop/front。因此它不是通用 MPMC queue。

~~~text
writer: back() / push()
reader: front() / pop()
~~~

## chunk：降低分配频率

~~~cpp
struct chunk_t
{
    T values[N];
    chunk_t *prev;
    chunk_t *next;
};
~~~

不是每 push 一个元素就 malloc；而是 N 个元素一组申请 chunk，减少 allocator 调用和 cache 扰动。

## begin 与 end 的 ownership

源码明确说明 begin 只由 reader 使用，back/end 只由 writer 使用。于是大部分游标都不需要原子操作。只有真正跨线程交接的 `_spare_chunk` 使用 atomic pointer。

这体现一个很重要的原则：

> reader-only state 用普通字段；writer-only state 用普通字段；只有 cross-thread handoff point 才需要 atomic。

## ypipe 在 yqueue 上增加发布边界

`ypipe` 维护：

~~~text
_w  writer 未 flush 边界
_f  writer 下一次 flush 边界
_r  reader prefetch 边界
_c  writer/reader 共享的 atomic contention point
~~~

`write()` 只推进 writer-local queue；真正把一批数据发布给 reader 发生在 `flush()`。这等于允许多次 write 后只做一次跨线程发布。

## flush() 为什么会返回 false

源码注释直接说：如果 reader sleeping，flush 返回 false，caller 必须负责把 reader 唤醒。

~~~text
flush == true   reader active，不需要额外 wakeup
flush == false  reader passive，需要 signaler.send()
~~~

因此 ypipe 只负责数据发布状态，真正的 OS wakeup 交给上层 mailbox/signaler。

## `_c == NULL` 为什么不只是空指针

reader 在没有新元素时通过 CAS 把共享边界置为 NULL。这个 NULL 同时编码“reader 已进入 passive/sleeping”。writer 后续 flush 看到这个状态，就知道仅仅发布数据还不够，必须额外唤醒。

## 为什么不直接 std::queue + mutex

不是因为 std::queue 错，而是这里已知拓扑就是 one writer / one reader，所以可以利用私有 cursor、chunk reuse、单一 atomic contention point 和 batch flush。

如果场景变成 many writers，libzmq mailbox 并没有硬把 ypipe 改造成 MPMC，而是在入口加 mutex，把 many writers 序列化成 one logical writer。

## 迁移到机器人 Runtime

camera capture → inference owner 这种固定一对一通道适合 SPSC；多个 sensor → fusion owner 则可以选择 MPSC，或者每个 producer 一条 SPSC channel。先画 ownership topology，再选数据结构，而不是先决定“我要 lock-free”。
