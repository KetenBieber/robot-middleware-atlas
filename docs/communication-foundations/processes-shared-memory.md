# 进程间通信：共享物理页以后，为什么还需要 Offset、Pool、队列与回收协议

:::{contents} 本页目录
:depth: 3
:local:
:::

同一主机进程间通信最容易出现一句误导性的总结：

> “为了快，把 socket 换成 shared memory 就好了。”

shared memory 确实能让两个进程访问同一批物理页，但它只解决了“数据放在哪里”。一个完整 IPC 还必须解决地址转换、分配、发布、通知、借用、回收、慢消费者和崩溃恢复。

换句话说：

> mmap 是起点，不是协议。

## 第一问：为什么不能把裸指针直接发给另一个进程

两个进程有独立虚拟地址空间。

同一块共享物理页可能被映射成：

~~~text
Process A
segment base = 0x7000_0000
object       = 0x7000_1000

Process B
segment base = 0x4300_0000
object       = 0x4300_1000
~~~

同一个对象，在 A 看来地址是 **0x7000_1000**，在 B 看来却是 **0x4300_1000**。

所以 A 如果把：

~~~text
0x7000_1000
~~~

写进消息，B 拿到的只是一串对自己页表没有意义的数。

稳定的跨进程身份应该来自共享 segment 内的相对位置：

~~~text
offset = object - segment_base
~~~

接收端恢复：

~~~text
local_ptr = local_segment_base + offset
~~~

因此常见 descriptor 会长得像：

~~~cpp
struct ShmHandle {
    uint32_t segment_id;
    uint64_t offset;
    uint32_t length;
    uint32_t generation;
};
~~~

真正跨进程流动的是 **ShmHandle**，而不是 payload。

这也是 [iceoryx2 PointerOffset](../generated/iceoryx2/shared-memory-pointer-offset.md) 这类设计的根本原因。

## 第二问：为什么共享内存里不能随便塞 std::vector

考虑：

~~~cpp
struct Message {
    uint64_t timestamp;
    std::vector<float> points;
};
~~~

即使 **Message** 自己被 placement-new 到共享页里，**vector** 内部保存的数据指针仍可能指向 Producer 私有 heap。

于是共享页中的对象实际上是：

~~~text
shared segment
+----------------------+
| timestamp            |
| vector.ptr ----------+------> Producer private heap
| vector.size          |
| vector.capacity      |
+----------------------+
~~~

Subscriber 看到了同一份 vector 元数据，却无法合法访问那根指针。

因此 shared-memory payload 常要求以下之一：

- fixed-size/self-contained layout；
- offset pointer / relative pointer；
- segment-aware allocator；
- flat buffer；
- serialized-but-in-place layout；
- middleware 提供的 loaned sample 类型约束。

这不是“C++ 容器不够高级”，而是虚拟地址不具有跨进程全局意义。

## 第三问：如果 Publisher 还要先 malloc 再 copy，shared memory 的意义就被削弱了

一个最朴素的 SHM Publisher 可能写成：

~~~text
malloc local Frame
↓ fill
copy into shared segment
↓ publish offset
free local Frame
~~~

这确实省掉了内核 socket path，却仍然保留了一次大 payload copy。

更完整的 zero-copy 设计会把顺序反过来：

~~~text
shared pool
↓ loan
Publisher 直接获得 shared chunk
↓ fill in place
publish descriptor
↓
Subscriber borrow same chunk
↓
release
↓
pool reclaim
~~~

于是 allocation 与 ownership 变成核心协议。

iceoryx2 的 [Publisher loan](../generated/iceoryx2/publisher-loan.md)、[Subscriber receive/reclaim](../generated/iceoryx2/subscriber-receive-reclaim.md) 正是在实现这个状态机。

## 一个 Chunk 的完整生命周期

可以把共享块想成几个状态：

~~~text
FREE
 │ loan
 ▼
WRITING
 │ publish
 ▼
PUBLISHED
 │ delivered
 ▼
BORROWED by 1..N subscribers
 │ all released
 ▼
RECLAIMABLE
 │ recycle
 ▼
FREE
~~~

注意：**写入完成**和**可以回收**是两个不同事件。

如果 Publisher 发布以后马上复用这块内存：

~~~text
Subscriber still reading X
Publisher starts writing next frame into X
~~~

就会出现经典的读写覆盖。

因此 zero-copy 的本质不是“没有 memcpy”，而是：

> 用 ownership/lifetime protocol 代替 payload copy。

## 一对多 Fan-out：为什么引用计数会自然出现

一个 chunk 同时发给三个 Subscriber：

~~~text
          ┌→ A
Chunk X ──┼→ B
          └→ C
~~~

A 先读完，并不意味着 X 可以回收。

至少需要某种：

~~~text
remaining_readers = 3
A release → 2
B release → 1
C release → 0
reclaim
~~~

实际实现未必真是一个全局原子 refcount，也可能是：

- per-subscriber queue；
- used-chunk list；
- ownership bitmap；
- generation counter；
- acknowledgement/reclaim list。

但不管形式如何，都要解决同一个生命周期问题。

## 为什么共享内存通常还要有 Descriptor Queue

payload 在共享池里，并不等于 Consumer 知道“哪一块是新消息”。

典型结构实际上是两层：

~~~text
Shared payload pool
+---------+---------+---------+
| Chunk 0 | Chunk 1 | Chunk 2 |
+---------+---------+---------+

Descriptor queue
+-------+-------+-------+
| id=2  | id=0  | ...   |
+-------+-------+-------+
~~~

Producer 做的不是把大 payload 放进 queue，而是：

~~~text
fill Chunk 2
↓
enqueue descriptor{segment, offset, generation}
~~~

Consumer：

~~~text
dequeue descriptor
↓
resolve local address
↓
borrow Chunk 2
~~~

这正是“大数据不搬，小控制信息流动”的经典结构。

## 为什么 descriptor 里最好还有 generation

假设 slot 7 被循环复用了很多次：

~~~text
slot 7 generation 10
slot 7 generation 11
slot 7 generation 12
~~~

如果某个陈旧 descriptor 迟到，只带 **slot_id=7**，Consumer 无法区分“我拿到的是不是原来那一代对象”。

于是常见做法会把身份扩成：

~~~text
(slot_id, generation)
~~~

这和 lock-free 算法中的 ABA 防护是同一种思想：**地址/槽位相同，不代表还是同一个逻辑对象**。

## 通知和数据为什么经常分开

共享内存让 Consumer 可以读到 payload，但它并不会自动让睡眠中的线程醒来。

因此常见 IPC 是：

~~~text
Data path:
shared memory

Control/notification path:
futex / eventfd / semaphore /
pipe / unix socket / shared atomic
~~~

从机制上看：

~~~text
Producer writes big payload once
↓
publishes a tiny descriptor
↓
signals a cheap notification primitive
↓
Consumer wakes and resolves payload
~~~

这比“每次把几 MB 数据送进内核 pipe”更合理。

## 跨进程同步为什么比普通线程同步更挑实现

线程里的 mutex 默认属于一个进程内部。

如果把 pthread mutex 放在共享页里，还需要显式配置 process-shared 属性。某些场景还会使用 robust mutex，让下一位获得锁的人能够知道前一个持有者是否异常死亡。

原子变量同样需要考虑：

- 类型是否 lock-free；
- ABI 与对齐是否在进程间一致；
- 映射属性是否正确；
- crash 以后 metadata 是否仍处于可恢复状态。

所以共享内存库往往不会简单说“我们有一块 mmap，然后放几个 std::atomic 就结束”。

它们还需要对操作系统、平台和生命周期做严格约束。

## Slow Subscriber：共享内存并没有消灭背压

假设 pool 只有 8 个 chunk：

~~~text
Publisher 30 Hz
Subscriber 5 Hz
~~~

如果 Subscriber 长期持有旧 chunk，Publisher 最终会发现：

~~~text
FREE chunk = 0
~~~

这时系统仍然必须选择：

- Publisher block；
- drop new；
- discard oldest safe sample；
- disconnect/mark slow subscriber；
- 扩大 pool；
- 使用 latest-value semantics。

因此 SHM zero-copy 与 backpressure 是绑在一起的：payload 不 copy，并不意味着容量无限。

iceoryx2 的 [Fan-out / backpressure / history](../generated/iceoryx2/fanout-backpressure-history.md) 正好可以接着这一点读。

## 进程死亡：正常生命周期突然断在中间

正常路径：

~~~text
loan
→ fill
→ publish
→ borrow
→ release
→ reclaim
~~~

如果 Subscriber 在 borrow 后 SIGKILL：

~~~text
loan
→ fill
→ publish
→ borrow
→ X
~~~

Publisher 不能永远把这块 chunk 视为“有人正在读”。

生产级 IPC 因此还需要：

~~~text
participant identity
heartbeat / liveness
ownership metadata
dead participant detection
stale resource cleanup
reclaim policy
~~~

iceoryx2 的 [Dead node recovery](../generated/iceoryx2/dead-node-recovery.md) 就是在补这条异常路径。

## 一个最小共享内存通信骨架应该有哪些结构

为了把机制放到数据结构上，可以抽象成：

~~~cpp
struct SlotMeta {
    std::atomic<uint32_t> generation;
    std::atomic<uint32_t> readers;
    std::atomic<uint32_t> state;
};

struct SegmentHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t slot_count;
    // allocator / queue metadata ...
};

struct Descriptor {
    uint32_t slot;
    uint32_t generation;
    uint32_t size;
};
~~~

数据路径：

~~~text
Publisher
1. acquire FREE slot
2. mark WRITING
3. write payload
4. publish Descriptor
5. mark PUBLISHED
6. notify

Subscriber
1. dequeue Descriptor
2. validate generation
3. increment/claim reader ownership
4. read payload
5. release
6. last reader makes slot reclaimable
~~~

真正成熟的库会把这些步骤拆成更细的无锁结构、per-subscriber queue、allocator 和 cleanup protocol，但核心问题就是这些。

## 看 eCAL / Fast DDS / Cyber / iceoryx2 时应该追什么

以后看到“支持 shared memory”，不要只在功能表上打勾。

继续追：

~~~text
payload 是先写本地再 copy，还是直接 loan shared chunk？
跨进程传 offset、slot id 还是别的 descriptor？
一个 Publisher 对多个 Subscriber 如何跟踪生命周期？
慢 Subscriber 会耗尽 pool 吗？
通知机制是什么？
队列满时 drop 谁？
进程 crash 后怎样恢复？
~~~

Fast DDS 的 Data Sharing、Cyclone DDS 的 PSMX、Cyber SHM、eCAL SHM、iceoryx2 都可以用这张表比较。

这样“共享内存”才从一个功能名词，变成可以逐行看源码的通信协议。

## 从“同机不同地址空间”继续扩大到“连物理内存都不共享”

共享内存解决的是：

~~~text
不同 virtual address space
但仍可映射同一批 physical pages
~~~

跨主机以后，这个最后的共同基础也消失。

此时 payload 必须变成 wire representation，系统还要额外面对 fragmentation、reliability、discovery、routing、remote failure 与 clock boundary。

因此下一篇把同样的数据生命周期继续扩展到网络：

→ [Network & Distributed Communication](network-distributed.md)
