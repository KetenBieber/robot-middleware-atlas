# 从函数调用到跨机器：通信首先是所有权与地址空间问题

一个机器人系统里，“A 把数据发给 B”可能代表完全不同的成本：

~~~text
同一个函数栈
→ 同进程另一个线程
→ 同主机另一个进程
→ 另一台主机
→ 另一块 GPU
~~~

如果先跳到 ROS、DDS、Zenoh 或 shared memory API，很容易把这些层混在一起。稳定的分析方法，是先问七个问题。

## 七问模型

对任意一条数据路径，都先回答：

1. payload 在哪里：栈、堆、共享页、socket buffer、GPU VRAM 还是远端主机？
2. 谁拥有它：producer、queue、middleware、consumer，还是一个共享 pool？
3. 地址空间是否相同：一个裸指针在对端是否仍有意义？
4. 通知怎样发生：函数调用、原子变量、condition variable、eventfd、socket readiness 还是中断？
5. 排队在哪里：用户态 ring、内核 socket buffer、NIC queue、远端 receive queue？
6. 过载怎么办：block、drop-new、drop-old、overwrite、retry 还是 backpressure？
7. 异常退出怎么办：谁知道对端已经死了，谁把借出去的资源收回来？

后面所有中间件文章都可以映射回这七个问题。

## Level 0：同线程函数调用

最简单：

~~~cpp
void consume(const Frame& frame);

Frame frame;
produce(frame);
consume(frame);
~~~

这里通常没有“通信协议”。producer 与 consumer 共享同一个地址空间、调用栈、线程和 C++ 对象生命周期。

如果传的是引用或指针，payload 根本没有移动。

### Ownership 为什么仍然重要

const Frame& 表示 consumer 借用对象；producer 必须保证 consumer 返回前对象仍存在。

如果改成 unique_ptr，则 ownership 可以显式转移。

所以 zero-copy 的第一个前提甚至不是 shared memory，而是：

> 能不能在不复制 payload 的情况下把“谁有权访问这块内存”说清楚。

## Level 1：跨线程

两个线程仍共享同一虚拟地址空间，因此 Thread A 的普通对象地址在 Thread B 中仍有意义。

问题从“地址能否访问”变成：

~~~text
什么时候可以访问？
数据是不是已经写完？
会不会同时写？
对象会不会提前析构？
~~~

于是引入 mutex、atomic、acquire/release、condition variable、semaphore、lock-free queue 和 object pool。

跨线程通信最容易被误判为“只要没有 memcpy 就够快”。真正的延迟可能来自 cache-line bouncing、锁竞争、线程唤醒和 OS 调度。

## Level 2：跨进程

两个进程拥有不同虚拟地址空间。

~~~text
Process A:
0x7f00_1000

Process B:
0x4a20_3000
~~~

即使两个地址最终映射同一物理页，A 的裸指针也不能直接发给 B。

因此共享内存 IPC 通常发送 offset、segment id、slot id 或 descriptor，而不是发送虚拟地址。

这也是后面 iceoryx2 的核心：

~~~text
ShmPointer
├─ data_ptr       当前进程使用
└─ PointerOffset  跨进程传递
~~~

## Level 3：跨主机

此时连物理内存也不共享。

payload 必须经过某种数据表示：

~~~text
object
→ serialized bytes
→ packet / frame
→ network
→ bytes
→ deserialization / zero-copy view
→ remote object
~~~

于是增加 schema/version、byte order、framing、fragmentation、packet loss、retransmission、ordering、congestion、discovery、routing 和 failure detection。

DDS、LCM、Zenoh、YARP 等主要在这一层提供不同答案。

## Level 4：异构内存

具身 AI 让“地址空间”问题再扩展一次。

~~~text
CPU RAM
GPU VRAM
NPU memory
pinned host memory
RDMA registered memory
~~~

即使在同一个进程，CPU pointer 也不代表 CPU 可以直接解引用 device pointer。

真正的问题变成 memory domain：

~~~text
数据现在属于哪一个计算设备？
谁能直接访问？
同步依赖哪个 stream/event？
跨进程或跨主机怎样导出这块 allocation？
~~~

所以现代中间件不能只问“是否 zero-copy”，还要问：

> zero-copy 发生在哪两个 memory domain 之间？

## 一张总图

~~~text
                     payload ownership

direct call
   │
   │ same stack / same thread
   ▼
shared object
   │
   │ same VA, different thread
   ▼
queue + synchronization
   │
   │ different VA, same host
   ▼
shared page + offset
   │
   │ different physical memory
   ▼
serialization + network
   │
   │ heterogeneous memory
   ▼
descriptor + device synchronization
~~~

以后评价任何“高性能通信”时，先标出它位于哪一级，再讨论性能才有意义。
