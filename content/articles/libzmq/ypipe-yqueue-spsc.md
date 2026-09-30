# yqueue / ypipe：SPSC Ownership、CAS 与发布边界

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

`yqueue_t` 和 `ypipe_t` 经常被一句“lock-free SPSC queue”带过，但真正值得研究的是它怎样把**线程所有权、内存分配、发布边界和睡眠状态**压进几个指针里。

它不是通用线程安全容器。设计成立的前提非常严格：

~~~text
exactly one logical writer
exactly one logical reader
~~~

在 libzmq mailbox 中，多个真实 sender 会先经过 mutex，被串行化成一个 logical writer，然后才进入 `ypipe_t`。

## 一个 ypipe 实例里哪些字段属于谁

一只 `ypipe_t<T, N>` 只有一个对象实例：

~~~text
                 one ypipe object
        +-----------------------------+
writer  | back/end, _w, _f            |
thread  |                             |
------->|          _c atomic           |<------- reader
        |                             |         thread
        | front/begin, _r             |
        +-----------------------------+
~~~

writer thread 独占：

~~~text
yqueue back/end
_w
_f
~~~

reader thread 独占：

~~~text
yqueue front/begin
_r
~~~

双方真正共享、需要原子同步的核心位置是：

~~~text
_c
~~~

`yqueue` 里还存在一个跨线程 chunk 回收点：

~~~text
_spare_chunk
~~~

并发设计的第一步不是给所有字段都加 atomic，而是先把状态按 owner 切开。

## yqueue 不是 ring buffer，而是 chunked queue

`yqueue_t<T, N>` 的 chunk：

~~~cpp
struct chunk_t
{
    T values[N];
    chunk_t *prev;
    chunk_t *next;
};
~~~

逻辑结构：

~~~text
chunk 0                 chunk 1                 chunk 2
+----------------+     +----------------+     +----------------+
| T T T ... T    |<--->| T T T ... T    |<--->| T T T ... T    |
+----------------+     +----------------+     +----------------+
      ^                                         ^
    begin                                      end
~~~

`N` 是一次 chunk 可容纳的元素数量。libzmq 对消息 pipe 使用：

~~~text
message_pipe_granularity = 256
~~~

对 command pipe 使用：

~~~text
command_pipe_granularity = 16
~~~

这意味着内存分配不是每 push 一条消息就发生一次，而是按 chunk 批量发生。

## chunk allocation 为什么也是并发设计的一部分

如果每条消息都：

~~~text
malloc
construct
enqueue
...
dequeue
destroy
free
~~~

allocator 的锁、metadata、cache miss 都会进入热路径。

yqueue 改成：

~~~text
allocate N slots once
consume N slots
reuse a recently freed chunk
~~~

源码还让 chunk 尽量按 cache line 对齐：

~~~cpp
posix_memalign (&pv, ALIGN, sizeof (chunk_t))
~~~

默认 `ALIGN` 使用 `ZMQ_CACHELINE_SIZE`。

目的不是让整个 queue“没有 cache miss”，而是避免相邻 chunk 偶然占据同一 cache line，从而减少不必要的 cache-line sharing。

## begin/back/end 为什么可以是普通指针

成员：

~~~text
_begin_chunk / _begin_pos
_back_chunk  / _back_pos
_end_chunk   / _end_pos
~~~

源码明确规定：

~~~text
begin        reader-only
back / end   writer-only
~~~

只要一个变量严格由单线程拥有，它就不需要为了“看起来线程安全”而改成 `std::atomic`。

更一般的原则是：

~~~text
先减少共享
再同步剩余共享点
~~~

## _spare_chunk：chunk 回收为什么需要 atomic exchange

reader 每跨过一个完整 chunk，就不再需要旧 chunk：

~~~cpp
chunk_t *o = _begin_chunk;
_begin_chunk = _begin_chunk->next;
_begin_chunk->prev = NULL;
_begin_pos = 0;

chunk_t *cs = _spare_chunk.xchg (o);
free (cs);
~~~

writer 在需要新 chunk 时：

~~~cpp
chunk_t *sc = _spare_chunk.xchg (NULL);

if (sc) {
    _end_chunk->next = sc;
    sc->prev = _end_chunk;
} else {
    _end_chunk->next = allocate_chunk ();
}
~~~

因此 `_spare_chunk` 是一个跨线程 handoff slot：

~~~text
reader                         writer
  |                              |
  | finish old chunk             |
  | xchg(spare, old_chunk)       |
  |                              |
  |                         xchg(spare, NULL)
  |                              |
  +----------- chunk ------------>
~~~

它不保存消息，只保存“最近释放、可以复用的 chunk”。

`xchg` 必须原子，因为 reader 和 writer 可能同时访问这个唯一 slot。

## atomic exchange 到底是什么

标准 C++ 的完整最小程序：

~~~cpp
#include <atomic>
#include <iostream>

int main()
{
    int a = 1;
    int b = 2;

    std::atomic<int*> p{&a};

    int* old = p.exchange(&b, std::memory_order_acq_rel);

    std::cout << "*old = " << *old << "\n";
    std::cout << "*p   = " << *p.load() << "\n";

    return 0;
}
~~~

`exchange(new_value)` 是一个不可分割的：

~~~text
old = p
p = new_value
return old
~~~

其他线程不会观察到中间状态。

libzmq 的 `atomic_ptr_t::xchg()` 在 C++11 路径使用：

~~~cpp
return _ptr.exchange (val_, std::memory_order_acq_rel);
~~~

## ypipe 在 yqueue 上增加什么

`yqueue` 只负责 storage 与 front/back 位置。

`ypipe` 再引入四个逻辑边界：

~~~text
_w  first un-flushed item
_r  first un-prefetched item
_f  flush boundary for completed messages
_c  shared publication pointer
~~~

角色图：

~~~text
reader side                                      writer side

front
  |
  v
[readable][readable] ... [not yet visible] ... [being written]
             ^               ^                     ^
             |               |                     |
             _r              _w / _c               _f / back
~~~

真实指针会随阶段移动，这张图只表达职责：`_w/_f` 属于 writer，`_r` 属于 reader，`_c` 是共享 publication boundary。

## write() 为什么不等于 publish

`write()`：

~~~cpp
void write (const T &value_, bool incomplete_)
{
    _queue.back () = value_;
    _queue.push ();

    if (!incomplete_)
        _f = &_queue.back ();
}
~~~

它只做两件事：

~~~text
1. 把数据放进 writer-owned storage
2. 如果一个完整 message 结束，推进 _f
~~~

reader 此时不一定能看到新元素。

所以：

~~~text
construct data
    !=
publish data to another thread
~~~

普通内存写和跨线程发布是两件不同的事情。

## incomplete_ 为什么参与 flush boundary

ZeroMQ message 可以是 multipart：

~~~text
frame A [more]
frame B [more]
frame C [last]
~~~

中间 frame 不能提前作为一条完整消息对上层可见。

因此：

~~~text
write(frame A, incomplete=true)
write(frame B, incomplete=true)
write(frame C, incomplete=false)
~~~

只有最后一帧会推进 `_f`。

publication boundary 与应用层 message boundary 对齐，而不是简单按“写入了几个 frame”对齐。

## flush() 的核心 CAS

固定源码：

~~~cpp
bool flush ()
{
    if (_w == _f)
        return true;

    if (_c.cas (_w, _f) != _w) {
        _c.set (_f);
        _w = _f;
        return false;
    }

    _w = _f;
    return true;
}
~~~

最难理解的是：

~~~cpp
_c.cas(_w, _f)
~~~

语义不是“把 `_c` 无条件改成 `_f`”，而是：

~~~text
if (_c == _w)
    _c = _f

return old _c
~~~

## CAS 用标准 C++ 怎样理解

完整最小程序：

~~~cpp
#include <atomic>
#include <iostream>

int main()
{
    int a = 1;
    int b = 2;
    int c = 3;

    std::atomic<int*> p{&a};

    int* expected = &a;
    const bool ok =
        p.compare_exchange_strong(
            expected,
            &b,
            std::memory_order_acq_rel);

    std::cout << "success = " << ok << "\n";
    std::cout << "*p = " << *p.load() << "\n";

    expected = &c;
    const bool ok2 =
        p.compare_exchange_strong(
            expected,
            &a,
            std::memory_order_acq_rel);

    std::cout << "success2 = " << ok2 << "\n";
    std::cout << "*expected = " << *expected << "\n";

    return 0;
}
~~~

第一次 CAS：

~~~text
p == expected(&a)
-> success
-> p becomes &b
~~~

第二次：

~~~text
p != expected(&c)
-> failure
-> expected is overwritten with actual p (&b)
~~~

libzmq 封装后的 `cas(cmp, val)` 返回旧值，因此调用端直接比较：

~~~text
returned old value == expected old pointer ?
~~~

## flush() 成功时发生了什么

writer 已知上一次 published boundary 是 `_w`。

如果：

~~~text
_c == _w
~~~

说明 reader 还处于正常 active 协议中，writer 可以原子地把共享边界推进到 `_f`：

~~~text
_c: old boundary
      |
      CAS
      v
_c: new boundary
~~~

然后：

~~~text
_w = _f
~~~

writer-local “未 flush 起点”也同步前移。

返回 `true` 表示：

~~~text
data published
reader not sleeping
no external wakeup required
~~~

## flush() CAS 失败为什么意味着 reader sleeping

reader 没有可读数据时，会在 `check_read()` 中执行：

~~~cpp
_r = _c.cas (&_queue.front (), NULL);
~~~

如果共享边界仍等于当前 front，CAS 把：

~~~text
_c = NULL
~~~

这个 `NULL` 不是“queue 不存在”，而是协议状态：

~~~text
reader found no published item
reader has moved into passive state
~~~

writer 后续执行：

~~~cpp
_c.cas(_w, _f)
~~~

时，实际旧值是 `NULL`，自然不等于 `_w`。

writer 因此知道：

~~~text
仅仅 publish 数据不够
reader 还需要 external wakeup
~~~

随后：

~~~cpp
_c.set (_f);
_w = _f;
return false;
~~~

返回 `false` 给 mailbox/pipe，让上层发 signal 或 activate command。

## 为什么 CAS 失败后可以非原子 set()

源码注释说明，失败原因是 `_c == NULL`，代表 reader 已经 passive。

协议保证 reader 此时不会继续并发修改 `_c`；它必须等待外部 wakeup 后才恢复。因此 writer 在这个窗口拥有 `_c` 的实际控制权，可以用非线程安全的 `set()`。

这是一种“状态机证明局部无并发”：

~~~text
不是因为 set 本身安全
而是因为 protocol 证明当前只有 writer 会访问
~~~

如果把这段代码脱离状态机单看，很容易误以为 atomic 旁边混用普通写一定错误。

## check_read() 怎样把 reader 变成 passive

核心逻辑：

~~~cpp
bool check_read ()
{
    if (&_queue.front () != _r && _r)
        return true;

    _r = _c.cas (&_queue.front (), NULL);

    if (&_queue.front () == _r || !_r)
        return false;

    return true;
}
~~~

存在两种状态。

### 已经 prefetch 过一批元素

如果：

~~~text
front != _r
~~~

说明 reader 本地已经知道还有元素可读，不需要碰共享 atomic。

这是 read-side fast path。

### 本地 batch 已读完

reader 尝试把：

~~~text
_c: current front boundary
~~~

改成：

~~~text
NULL
~~~

如果 CAS 返回的旧值仍等于当前 front，表示 writer 没有在这期间 publish 新 batch，于是 reader 可以安全进入 passive。

如果 writer 已经把 `_c` 推进到更远边界，CAS 会失败并返回那个新边界；reader 把它保存到 `_r`，继续读取，无需睡眠。

这正是 CAS 关闭 lost-wakeup 窗口的地方。

## 如果没有 CAS，会出现什么竞态

错误设计：

~~~text
reader:
    if no data:
        sleeping = true
        sleep

writer:
    publish data
    if sleeping:
        wake
~~~

危险时间线：

~~~text
reader checks: no data
                         writer publishes data
                         writer sees sleeping=false
                         no wake
reader sets sleeping=true
reader sleeps forever
~~~

ypipe 用同一个原子 `_c` 同时编码：

~~~text
publication boundary
+
reader passive state
~~~

reader 从“确认无数据”到“声明 passive”的转换通过 CAS 完成；writer 的 publish 也通过 CAS 竞争同一个状态，因此两者不能无序穿过彼此。

## acq_rel 为什么重要

C++11 路径：

~~~cpp
_ptr.compare_exchange_strong (
    cmp_,
    val_,
    std::memory_order_acq_rel);
~~~

writer 在 CAS 前已经写好了 queue element。成功的 release 部分把这些普通内存写发布出去。

reader 对同一 atomic 的 acquire 侧成功观察 publication 后，才能按 happens-before 关系读取对应 element。

抽象成：

~~~text
writer:
    construct T
    |
    release publication on _c
    |
    v
reader:
    acquire observation on _c
    |
    read T
~~~

原子指针不仅保证“指针更新不可撕裂”，还承担跨线程内存可见性的发布/获取边界。

## 为什么一个 atomic pointer 可以同时表达边界和状态

`_c` 有两类值：

~~~text
valid T* -> published boundary
NULL     -> reader passive
~~~

这是一种 tagged state，只是 tag 借用了 `NULL`。

优点是 publication 与 sleep state 共用同一个原子仲裁点，减少额外 atomic flag 以及两个原子之间的一致性问题。

代价是协议更难理解：指针值不再只是“地址”，还包含状态语义。

## ypipe 真正减少的是哪些同步

稳定数据流中，大量动作都是 owner-local：

~~~text
writer:
  back/end cursor
  _w
  _f

reader:
  front/begin cursor
  _r
~~~

跨线程只在 batch publication 或 passive transition 时竞争 `_c`。

优化来源不是一句“用了 CAS”，而是：

~~~text
大部分状态根本不共享
+
共享点被压缩成一个 atomic pointer
+
publication 可以 batch
~~~

## batch flush 为什么能降低共享成本

假设 writer 连续产生 32 个完整 command。

逐条同步：

~~~text
write -> atomic
write -> atomic
...
32 times
~~~

ypipe 可以：

~~~text
write
write
write
...
write
flush once
~~~

于是数据构造主要发生在 writer-local storage，对共享 `_c` 的原子操作次数显著降低。

## unwrite() 为什么只能回滚未发布部分

`unwrite()`：

~~~cpp
bool unwrite (T *value_)
{
    if (_f == &_queue.back ())
        return false;

    _queue.unpush ();
    *value_ = _queue.back ();
    return true;
}
~~~

当 `_f` 已经追到 back，说明没有 incomplete、未提交部分可以撤销。

它只能从 writer 尚未形成完整 message boundary 的尾部回滚。这使 multipart message 可以在失败或终止时丢弃半条消息，而不会撤销已经作为完整消息边界提交的数据。

## yqueue 与 ypipe 的职责边界

~~~text
yqueue_t
--------
storage
chunk allocation/reuse
front/back/end ownership

ypipe_t
-------
message completion boundary
cross-thread publication
reader passive encoding
lost-wakeup avoidance
~~~

把这两层分开，能避免把“队列存储”和“线程同步协议”混成同一个概念。

## 与 Folly SPSC queue 的一个关键差异

Folly 的 `ProducerConsumerQueue` 更接近固定容量 ring：

~~~text
producer cursor
consumer cursor
fixed records
~~~

libzmq 的 `yqueue` 则使用 chunk 链表，容量本身不由 yqueue 固定，真正的有界性由更上层 `pipe_t` 的 HWM 实现。

因此：

~~~text
ypipe/yqueue
  -> transmission/storage mechanism

pipe HWM
  -> admission/backpressure mechanism
~~~

不要因为底层 queue 可以继续分配，就误以为 ZeroMQ 整体没有容量控制。

## 对机器人线程通道的直接映射

适合直接利用 SPSC 的拓扑：

~~~text
camera capture thread -> one vision preprocessing thread

CAN RX owner thread -> one state estimator thread

logger producer stage -> one file writer stage
~~~

不适合强行套 SPSC 的拓扑：

~~~text
camera ----\
lidar ------+--> fusion queue
imu -------/
~~~

这里可以选择：

~~~text
A. one MPSC queue
B. one SPSC per producer + fusion owner polls/merges
C. producers first serialize through one dispatcher
~~~

libzmq mailbox 选择的是 C：先把多 writer 变成一个 logical writer，再复用 SPSC ypipe。

真正的设计顺序是：

~~~text
thread ownership topology
        ↓
shared-state boundary
        ↓
publication/wakeup protocol
        ↓
data structure
~~~
