# 进程间通信：共享物理页以后，为什么还需要 Offset、队列和回收协议

同一主机的两个进程不共享普通堆对象。每个进程都有自己的虚拟地址空间。

## 虚拟地址不是全局身份

假设共享内存中的一个对象距离 segment 起点 4096 字节。

Process A：

~~~text
segment base = 0x7000_0000
object       = 0x7000_1000
~~~

Process B：

~~~text
segment base = 0x4300_0000
object       = 0x4300_1000
~~~

同一份物理数据，在两个进程里虚拟地址不同。

所以跨进程不能传：

~~~text
0x7000_1000
~~~

而应传：

~~~text
offset = 0x1000
~~~

接收端再计算：

~~~text
local_ptr = local_segment_base + offset
~~~

## mmap 只解决“能看见同一页”

建立共享页以后：

~~~text
Process A ─┐
           ├─ physical shared pages
Process B ─┘
~~~

仍然没有解决谁可以写、什么时候写完、哪个 slot 是新数据、consumer 是否还在读、slot 什么时候能复用、queue 满时怎么办，以及某个进程 SIGKILL 后谁清理。

因此：

> shared memory 只是存储机制，不是完整消息协议。

## 为什么共享对象不能随便放 std::vector

考虑：

~~~cpp
struct Message {
    std::vector<float> points;
};
~~~

Message 自己可能放在共享页，但 vector 内部保存的 pointer 往往指向 producer 私有 heap。

Subscriber 映射到同一个 Message 后，看到的 pointer 数值依旧指向另一个进程的虚拟地址。

因此共享内存 payload 常要求：

- self-contained layout；
- 固定数组；
- offset pointer；
- relative pointer；
- segment-aware allocator；
- serialization-free schema。

## Pool + Loan 是自然结果

如果 Publisher 每次：

~~~text
malloc
→ fill
→ copy into shm
→ free
~~~

仍然做了一次大 payload copy。

更合理的是：

~~~text
shared pool
   ↓ loan
Publisher 直接写共享 chunk
   ↓ publish descriptor/offset
Subscriber 借用同一个 chunk
   ↓ release
pool reclaim
~~~

于是 zero-copy 真正变成 ownership protocol。

## 一对多为什么更难

一个 Publisher 对三个 Subscriber：

~~~text
          ┌→ Subscriber A
Chunk X ──┼→ Subscriber B
          └→ Subscriber C
~~~

Chunk X 不能在 A 读完以后立刻复用，因为 B、C 可能仍在读。

必须维护某种 reference count、per-subscriber used-chunk tracking、acknowledgement/reclaim list 或 generation/tag。

所以 fan-out zero-copy 的难点是生命周期，而不是 mmap。

## 通知通常与数据分离

payload 放共享页，不意味着 consumer 会自动醒来。

常见组合：

~~~text
data:
shared memory

notification:
futex / eventfd / semaphore /
pipe / socket / shared atomic
~~~

这正是很多高性能 IPC 的基本结构：

> 大数据不搬，小控制消息负责通知。

## 进程死亡为什么棘手

正常路径：

~~~text
loan
→ publish
→ borrow
→ release
→ reclaim
~~~

如果 Subscriber 在 borrow 后被 SIGKILL：

~~~text
loan
→ publish
→ borrow
→ X process died
~~~

Publisher 不能永远认为这块 chunk 仍被借用。

因此生产级 IPC 还需要 process liveness、ownership metadata、stale resource detection、dead participant cleanup、version/permission checks。

iceoryx2 的 NodeState::Dead、DeadNodeView 与 stale resource cleanup 就属于这一层。

## 对现有项目的映射

~~~text
eCAL SHM
Fast DDS Data Sharing
Cyclone DDS PSMX
Cyber SHM
iceoryx2
~~~

都可以问同样的问题：

1. payload 放哪；
2. descriptor 是什么；
3. subscriber 如何获得本地地址；
4. borrow/release 怎么跟踪；
5. 慢 consumer 如何处理；
6. crash 后资源如何恢复。

这样比较会比“谁支持 shared memory”更有意义。
