# 从函数调用到跨机器：通信首先是所有权、地址空间与数据路径问题

:::{contents} 本页目录
:depth: 3
:local:
:::

机器人系统里经常会出现一句很轻描淡写的话：

> “相机把图像发给感知，感知再把结果发给控制器。”

真正落到机器上，这一句话可能代表五种完全不同的机制。

~~~text
同线程函数调用
→ 同进程跨线程
→ 同主机跨进程
→ 跨主机
→ CPU / GPU / NPU 等异构 memory domain
~~~

它们表面上都可以包装成 **publish / subscribe**，但成本模型和正确性条件完全不同。要把通信真正看懂，第一步不是看 API，而是先把“数据在哪里、谁拥有它、谁能直接访问它”说清楚。

## 先建立三条线：数据线、控制线、所有权线

假设一个 Camera Frame 从采集线程一路进入 VLA：

~~~text
Camera
  ↓
Preprocess
  ↓
Vision Encoder
  ↓
VLA
  ↓
Controller
~~~

如果只画 node/topic，这张图几乎没有性能信息。更有价值的画法是同时标三条线。

### 数据线：payload 实际在哪里

~~~text
camera DMA buffer
→ CPU heap
→ middleware buffer
→ socket buffer
→ remote heap
→ pinned host memory
→ GPU VRAM
~~~

每跨一步，都可能有一次 copy、一次映射、一次 DMA，或者只是传一个 descriptor。

### 控制线：谁告诉谁“数据已经好了”

~~~text
direct call
mutex + condition_variable
atomic flag
eventfd
futex
socket readiness
interrupt
CUDA event
~~~

很多所谓“zero-copy”只解决了数据线，并没有消除控制线上的线程唤醒和调度。

### 所有权线：谁有权访问，什么时候可以复用

~~~text
Producer owns
   ↓ publish
Queue / pool owns
   ↓ borrow
Consumer borrows
   ↓ release
Pool reclaims
~~~

只要这个状态机不清楚，就算一行 memcpy 都没有，也可能读到未写完的数据、Use-After-Free，或者让共享块永远无法回收。

## 七问模型：看任何通信实现都先问这七个问题

对任意一条数据路径，先不要被类名和 API 带走，固定问：

1. **payload 在哪里？** 栈、堆、共享页、socket buffer、NIC ring、GPU VRAM，还是远端机器？
2. **谁拥有它？** producer、queue、middleware、consumer，还是共享 pool？
3. **地址空间是否相同？** producer 里的裸指针，到 consumer 那边还有意义吗？
4. **完成事件怎么传播？** 函数调用、atomic、condition variable、eventfd、socket、interrupt 还是 device event？
5. **排队在哪里？** 用户态 queue、middleware history、内核 socket buffer、NIC ring、远端 receive queue？
6. **过载怎么办？** block、drop-new、drop-old、overwrite、retry 还是 credit/backpressure？
7. **异常退出怎么办？** 谁知道对端已经死了？借出去的资源怎么收回来？

后面的线程、共享内存、网络、GPU，本质上只是对这七问给出不同答案。

## Level 0：同线程函数调用——通信几乎退化成对象生命周期

最简单的情况：

~~~cpp
void consume(const Frame& frame);

Frame frame;
produce(frame);
consume(frame);
~~~

这里通常没有消息协议，没有线程同步，也没有跨地址空间。**Frame** 没有“被发送”，只是同一个对象被另一个函数借用。

如果 consumer 只读，那么 const reference 已经表达了一个非常强的约束：

~~~text
Producer owns Frame
↓
Consumer temporarily borrows
↓
consume() returns
↓
borrow ends
~~~

把接口改成 unique ownership：

~~~cpp
void consume(std::unique_ptr<Frame> frame);
~~~

语义就变了：

~~~text
Producer owns
↓ move
Consumer owns
~~~

所以 zero-copy 的最原始形态甚至不是共享内存，而是：

> 不搬 payload，只改变“谁有权继续访问这块内存”。

这也是理解 loaned message、shared-memory chunk、CUDA IPC handle 的起点。

## Level 1：跨线程——地址仍然有效，但时间顺序不再天然成立

两个线程共享同一个虚拟地址空间，因此 Thread A 中的普通 heap pointer 在 Thread B 中通常仍能解引用。

于是最容易产生一个错觉：

~~~text
“地址都一样，那把指针塞进 queue 就行了。”
~~~

问题在于，跨线程以后必须额外回答：

~~~text
Producer 什么时候写完？
Consumer 什么时候能看到这些写？
两边会不会同时修改？
对象会不会在 Consumer 使用前被析构？
~~~

例如：

~~~cpp
Frame frame;
bool ready = false;

// producer
frame = capture();
ready = true;

// consumer
if (ready) {
    process(frame);
}
~~~

这段代码的问题不是“偶尔慢一点”，而是它没有建立合法的跨线程同步关系。普通 **ready** 存在 data race，编译器和 CPU 的重排也不能靠人的阅读顺序来约束。

因此跨线程通信真正引入的不是“queue API”，而是两个机制：

~~~text
shared address
+
happens-before
~~~

这会在下一篇里展开成 mutex、atomic acquire/release、condition variable、SPSC ring、false sharing 与线程调度。

## Level 2：跨进程——虚拟地址空间开始断裂

同一台机器上的两个进程拥有不同的页表和虚拟地址空间。

假设同一块共享物理页被映射成：

~~~text
Process A:
segment base = 0x7000_0000
object       = 0x7000_1000

Process B:
segment base = 0x4300_0000
object       = 0x4300_1000
~~~

物理数据是同一份，但虚拟地址不同。

所以 A 不能把 **0x7000_1000** 当消息发给 B。这个数只在 A 自己的页表语境里有意义。

真正可以跨进程稳定传递的是“相对身份”：

~~~text
offset = object - segment_base
       = 0x1000
~~~

B 再做：

~~~text
local_ptr = local_segment_base + offset
~~~

这也是 shared-memory IPC 中 offset pointer、relative pointer、segment id、slot id、chunk id 会反复出现的原因。

iceoryx2 的 **PointerOffset** 就是这一思想的具体化；可以继续看 [共享内存指针与 offset](../generated/iceoryx2/shared-memory-pointer-offset.md)。

### mmap 只完成“共享页”，没有完成“消息”

假设两个进程都已经映射同一片物理页：

~~~text
Process A ─┐
           ├── shared physical pages
Process B ─┘
~~~

仍然有一大串问题没有解决：

~~~text
哪个 slot 是空闲的？
Publisher 什么时候可以写？
写完以后怎样发布？
Subscriber 怎样知道有新数据？
Subscriber 读完以前 slot 能否复用？
多个 Subscriber 怎么跟踪？
进程突然 SIGKILL 怎么清理？
~~~

因此：

> shared memory 是存储机制，不是完整通信协议。

真正生产级的共享内存中间件一定会在 shared pages 之上再构造 metadata、allocator/pool、descriptor queue、notification 与 reclaim protocol。

## Level 3：跨主机——不再共享内存，只能共享协议

跨机器以后，连物理页都不共享。

于是对象必须先变成双方都能解释的数据表示：

~~~text
C++ / Rust object
↓
serialization
↓
wire bytes
↓
framing / fragmentation
↓
transport
↓
remote bytes
↓
decode / zero-copy view
↓
remote object
~~~

这时才会出现：

- schema 与版本兼容；
- byte order 与 alignment；
- MTU 与 fragmentation；
- packet loss、reordering、retransmission；
- discovery 与 endpoint matching；
- congestion 与 flow control；
- routing 与 reconnect；
- failure detection。

LCM、DDS、Zenoh、YARP 都在这一层给出不同答案。

这里一个非常重要的变化是：**传输成功不再等于业务成功**。

~~~text
send() returned
≠ remote kernel received
≠ middleware delivered
≠ callback ran
≠ actuator executed
~~~

因此“Reliable transport”和“业务 ACK”必须分开。

## Level 4：异构内存——同一个进程里也可能存在不可直接访问的地址

具身 AI 把“地址空间”问题再次扩大。

~~~text
CPU pageable RAM
CPU pinned RAM
GPU VRAM
NPU memory
camera DMA buffer
RDMA registered memory
~~~

这时即使在同一个 Linux 进程里，也不能简单说“大家共享地址空间”。

一个 CUDA device pointer 对 GPU kernel 有意义，并不代表 CPU 可以像普通 heap pointer 一样解引用它。类似地，相机驱动交给你的 DMA buffer 可能带有专门的 ownership 与 fence 约束。

所以分析对象要从 address space 升级成 **memory domain**：

~~~text
数据在哪个 domain？
哪些执行单元可以直接访问？
是否需要 staging？
谁发起 DMA？
完成事件由什么表示？
allocation 怎样跨进程导出？
~~~

CUDA IPC 与 CPU shared memory 在抽象上非常相似：

~~~text
CPU SHM:
segment + offset

GPU IPC:
allocation + exported handle
~~~

两者传递的都不是“大 payload 本身”，而是一个能在另一个执行环境中重新定位同一 allocation 的 descriptor。

## “Zero-copy”到底省掉了哪一次 copy

工程里最容易混淆的词就是 zero-copy。

假设一个相机帧最终进入 GPU：

~~~text
Camera DMA
→ driver buffer
→ application buffer
→ middleware buffer
→ remote process buffer
→ pinned buffer
→ GPU VRAM
~~~

如果某个中间件只省掉：

~~~text
application buffer → middleware buffer
~~~

它确实完成了一次 zero-copy 优化，但端到端仍然可能存在多次 copy。

因此讨论 zero-copy 时必须把边界写全：

| 说法 | 真正应该追问 |
| --- | --- |
| intra-process zero-copy | 是否只是共享同一个 C++ 对象？ |
| SHM zero-copy | Publisher 是否直接写共享 chunk？Subscriber 是否借用同一 chunk？ |
| network zero-copy | 是减少用户态 copy，还是 NIC 真正直接 DMA？ |
| GPU zero-copy | 省掉 host copy，还是 device allocation 本身被复用？ |
| end-to-end zero-copy | 从传感器到最终 kernel 是否始终没有 payload staging？ |

## 把通信成本拆成可分析的项

一条通信路径的端到端延迟，可以先粗略拆成：

$$
T_{e2e}
=
T_{produce}
+
T_{copy}
+
T_{queue}
+
T_{sync}
+
T_{schedule}
+
T_{transport}
+
T_{consume}
$$

这里真正危险的是 **T_queue** 与 **T_schedule**：它们往往不是固定值，而是产生尾延迟的主要来源。

同样地，CPU 成本也不只来自 memcpy：

~~~text
serialization
cache miss
cache-line bouncing
system call
context switch
allocator
checksum
packet processing
wake-up
~~~

所以“copy 少”并不自动推出“延迟低”。

## 读一个中间件时，先画一张这样的图

以后看到任何 **publish(msg)**，不要先问“这个 API 怎么用”，而是先把内部路径补成：

~~~text
Application object
   │
   ├── ownership transfer / borrow?
   │
   ▼
Middleware data structure
   │
   ├── queue / history / pool?
   │
   ▼
Transport or shared memory
   │
   ├── notification?
   │
   ▼
Receiver-side queue
   │
   ├── scheduler / callback thread?
   │
   ▼
Consumer
~~~

然后逐层标出：

~~~text
address space
memory domain
copy point
queue point
synchronization point
ownership transition
failure boundary
~~~

做到这一步，LCM 的 UDP、DDS 的 History、iceoryx2 的 loan、Cyber 的 Dispatcher、UCX 的 transport selection 才会落在同一张机制地图上，而不是一堆互不相关的名词。
