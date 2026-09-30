# 场景设计五：大图像/点云跨进程，怎样从 Copy IPC 走到 Shared-Memory Data Plane

## 场景

~~~text
Camera Process
    ↓
Perception Process
~~~

每帧 1920×1080×RGBA8：

~~~text
≈ 8.3 MB/frame
~~~

60 FPS：

~~~text
≈ 500 MB/s raw payload
~~~

如果跨进程每次 serialize + copy 多遍，memory bandwidth 很快成为主要成本。

---

## 先建立进程、地址空间和共享内存的基本模型

### 每个进程都有自己的 Virtual Address Space

两个进程即使运行同一个程序，也有各自独立的虚拟地址空间。

例如 Process A 可能把某一物理页映射到：

~~~text
0x7000_0000 -> physical page P
~~~

Process B 则可能把同一物理页映射到：

~~~text
0x9200_0000 -> physical page P
~~~

底层 physical page 相同，但 virtual address 不同。

> **共享内存共享的是底层 pages，不代表两个进程里的 pointer value 必须相同。**

这就是为什么把一个进程中的裸指针整数值直接发给另一个进程通常没有意义。

---

## Payload、Descriptor、Notification 是三层不同东西

### Payload

真正的大数据，例如 8 MB image、point cloud、tensor bytes。

### Descriptor

描述 payload 在哪里、大小多少、属于哪个版本的小对象，例如：

~~~cpp
struct FrameDescriptor {
    uint32_t slot;
    uint32_t generation;
    uint32_t bytes;
    uint64_t timestamp_ns;
};
~~~

Descriptor 很小，可以放进 socket、pipe、ring queue 或其他控制通道。

### Notification

只负责告诉另一边：**共享状态可能变化了，值得醒来检查一下。**

例如 eventfd、futex wake、condition_variable、socket readiness。

因此常见结构是：

~~~text
Payload Pool
    存大数据

Descriptor Queue
    存 slot/generation/metadata

Notification
    负责 wakeup
~~~

这和线程 Producer/Consumer 中的 `queue = truth, condition_variable = wakeup` 是同一条设计原则。

---

## 为什么 Offset 能跨进程，而 Pointer 通常不能

假设 shared segment 的逻辑布局：

~~~text
base
 |
 +-- offset 0
 +-- offset 4096
 +-- offset 8192
~~~

Producer 只传：

~~~text
offset = 8192
~~~

Consumer 在自己的地址空间中计算：

~~~cpp
void* ptr =
    static_cast<char*>(local_base) + offset;
~~~

于是两个进程虽然 `local_base` 不同，却都能定位到共享 segment 中同一个逻辑位置。

这就是 relative pointer / offset pointer 的基本思想。

---

## 为什么固定 Pool 比“每帧 mmap 一次”更自然

实时 data plane 希望把昂贵和不确定的资源操作尽量移出 hot path。

初始化阶段：

~~~text
allocate N large blocks
map/register
建立 metadata
~~~

运行阶段：

~~~text
loan slot
write payload
publish descriptor
consumer read
release slot
~~~

这样避免每帧 malloc/mmap/setup/free 带来的 allocator contention、page fault、fragmentation 与不可预测延迟。

---

## Loan 到底是什么意思

Loan 不是“某个 API 看起来像零拷贝”。

> **Loan 表示 Runtime 暂时把一个可写 slot 的独占使用权交给 Producer。**

状态可以写成：

~~~text
FREE
  | loan()
  v
WRITING
  | publish()
  v
READY
~~~

Producer 在 WRITING 期间可以直接填共享 payload；publish 后，Consumer 才应该观察这块内容。

所以 zero-copy 真正困难的是 ownership protocol，而不只是拿到一个 pointer。

---

## Refcount 为什么能支持 Fan-out

如果同一帧同时交给 Perception、Recorder、Visualizer，不能第一个 Consumer release 后就立即复用 slot。

一种直接方案：

~~~text
refcount = 3
Perception done  -> 2
Recorder done    -> 1
Visualizer done  -> 0
slot recyclable
~~~

但 atomic refcount 也会带来共享写、cache contention，并且慢 Consumer 会长期占住 buffer。

所以工业 Runtime 也会采用 per-consumer cursor、ownership bitmap，或者给慢支路单独复制。

---

## Generation 是怎样防 Stale Descriptor 的

只用 `slot=3` 不够，因为 slot 会循环复用。

~~~text
slot 3, generation 100
consumer holds old descriptor

slot recycled

slot 3, generation 101
new frame written
~~~

旧 Consumer 如果只记住 slot=3，就可能把 generation 101 的新数据误认成 generation 100。

所以更完整的 identity 是：

~~~text
(slot, generation)
~~~

这与 ABA 防护中的 version/tag 思想本质相同。

---

## Naive 方案：Socket + Serialization

~~~text
camera object
↓ serialize
temporary bytes
↓ kernel/socket copy
receiver bytes
↓ deserialize
new image object
~~~

优点：

~~~text
边界清楚
容易跨主机
crash isolation 简单
~~~

小消息时非常合理。

大 payload 高频时问题是 copy 与 allocator。

---

## 为什么 Shared Memory 自然出现

目标不是“让两个进程共享 C++ object”，而是：

~~~text
payload storage
只放一份在 shared physical pages
~~~

然后消息只传：

~~~text
slot id / offset
size
generation
metadata
~~~

这就是 descriptor-based data plane。

---

## 为什么不能把裸指针直接放进 SHM

Process A：

~~~text
shared segment mapped at 0x7000...
~~~

Process B：

~~~text
same pages mapped at 0x9000...
~~~

A 中的 pointer value 到 B 不一定有意义。

所以需要：

~~~text
offset from shared base
slot index
relative pointer
handle
~~~

iceoryx2 的 PointerOffset 类设计就是在解决这个问题。

---

## SHM Data Plane 的标准分层

~~~text
Shared Payload Pool
  large image/pointcloud chunks

Descriptor Channel
  slot/offset + metadata

Notification Channel
  eventfd/futex/socket/event
~~~

三者不要混在一起。

payload 很大，descriptor 很小，notification 只负责 wakeup。

---

## Pool 为什么优于每帧共享内存 malloc

实时 pipeline 如果每帧：

~~~text
allocate shared object
construct
publish
free
~~~

仍然会引入 allocator contention、fragmentation 和失败路径。

固定 chunk pool：

~~~text
slot0
slot1
...
slotN
~~~

更容易实现 bounded memory 与 backpressure。

---

## Loan / Publish / Reclaim 是核心状态机

~~~text
FREE
↓ producer loan
WRITING
↓ publish
READY
↓ consumer borrow
READING
↓ all consumers release
RECLAIMABLE
↓
FREE
~~~

zero-copy 真正难的不是拿到 pointer，而是**什么时候可以安全复用 slot**。

---

## Fan-out 为什么需要 Refcount 或 Per-Consumer Cursor

一个 Camera frame：

~~~text
Perception
Recorder
Visualizer
~~~

如果共享同一 chunk：

~~~text
slot can recycle
only after
all required consumers release
~~~

候选：

~~~text
atomic refcount
per-consumer cursor
generation + ownership bitmap
copy for slow branch
~~~

慢 recorder 可能拖住整个 pool。

这时可能需要把 recorder 从主 pool 隔离。

---

## Generation 为什么重要

固定 slot 会循环复用：

~~~text
slot 3 generation 100
↓ recycle
slot 3 generation 101
~~~

Consumer 如果拿着旧 descriptor：

~~~text
slot=3, generation=100
~~~

就能识别已经 stale。

这和 lock-free queue 的 per-slot sequence、ABA 防护是同一种模式。

---

## Notification 为什么不能代替共享状态

错误理解：

~~~text
eventfd fired
→ 一定有一个完整 frame
~~~

正确：

~~~text
eventfd/wakeup
只表示“值得重新检查共享状态”
~~~

真实状态仍然在 descriptor queue/cursor。

这与 condition_variable predicate 规则相同。

---

## Crash Recovery 为什么跨进程必须额外设计

Thread crash 通常意味着整个 process 结束。

跨进程共享 pool 时：

~~~text
Consumer dies while holding slot
~~~

Producer/other consumers 还活着。

谁回收？

可能需要：

~~~text
process registry
heartbeat/liveness
owner epoch
generation
lease
central daemon
~~~

这也是 SHM 比普通 in-process queue 难得多的地方。

---

## 工业方案空间

| 方案 | 数据路径 | 优点 | 适合 |
| --- | --- | --- | --- |
| Unix socket | copy/serialize | 简单 | 小消息 |
| memfd/mmap + custom descriptor | SHM | 可控 | 自研 runtime |
| iceoryx2 | chunk pool + ownership protocol | zero-copy/lifetime 完整 | large IPC |
| eCAL SHM | middleware-integrated SHM | 工程易用 | robotics/process IPC |
| Fast DDS Data Sharing/SHM | DDS semantics + local optimization | 保留 DDS API | DDS system |
| Cyclone DDS PSMX | plugin/offload path | 可扩展 | DDS local data plane |

不是所有系统都需要自研 SHM。

---

## 什么时候 Copy 反而更好

payload 小、频率低时：

~~~text
copy 2 KB
vs
复杂 loan/refcount/crash recovery
~~~

copy 可能更便宜、更安全。

另外对于安全/隔离边界，copy 还能减少共享可变状态。

zero-copy 应该由 payload size × frequency × latency budget 驱动，而不是作为目标本身。

---

## 一个机器人图像 IPC 推荐结构

~~~text
Camera Process
  loan slot from SHM pool
  DMA/copy into slot
  publish descriptor
  eventfd notify
        ↓
Perception Process
  wake
  pop descriptor
  validate generation
  borrow slot
  process
  release
~~~

Recorder 如果很慢：

~~~text
copy/secondary pool
~~~

避免它长期占主实时 pool。

---

## 指标

必须测：

~~~text
copy count
pool free slots
loan wait time
descriptor queue depth
oldest chunk age
consumer hold time
crash recovery time
stale descriptor count
~~~

只测 IPC throughput 不够。

---

## 什么时候继续升级到网络/UCX

如果 Producer/Consumer：

~~~text
不在同一主机
或
payload 已经在 GPU/NPU memory
~~~

普通 SHM 模型又不够。

下一场景进入 device/remote data plane。

深入机制：

- [Processes & Shared Memory](processes-shared-memory.md)
- [Heterogeneous Memory](heterogeneous-memory.md)
- [iceoryx2 实现专题](../generated/iceoryx2/index.rst)
