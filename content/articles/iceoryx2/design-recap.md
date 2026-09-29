# iceoryx2 设计复盘：Zero-copy IPC 的本质不是少一次 memcpy，而是把 Ownership 做成协议

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 完整发送链

~~~text
Node
↓
Service / PortFactory
↓
Publisher
↓
DataSegment
↓
loan shared Chunk
↓
application writes final storage
↓
SampleMut::send
↓
PointerOffset
↓
ZeroCopyConnection::Sender
↓
Subscriber connection
~~~

真正大 payload 从头到尾留在 shared memory。

## 完整接收链

~~~text
ZeroCopyConnection::Receiver
↓
PointerOffset
↓
DataSegmentView
↓
register_and_translate_offset
↓
local mapped address
↓
Chunk
↓
Sample
↓
application read
↓
Sample drop
↓
release offset
↓
Publisher reclaim
~~~

所以 zero-copy 不是“什么都没发生”。

发生了大量 descriptor、ownership 与状态管理，只是没有重复搬大 payload。

## 五个最值得复用的设计思想

### 1. Storage 与 Connection 分开

~~~text
SharedMemory
存 payload

ZeroCopyConnection
传 descriptor
~~~

这让 allocator 与 queue/backpressure 可以独立演化。

### 2. PointerOffset 代替跨进程裸指针

跨进程稳定的是 segment-relative identity，而不是某个进程的虚拟地址。

### 3. Loan 把 Copy 变成 Ownership 转移

应用直接写最终存储。

代价是必须严格控制 send 后写权限与 borrow lifetime。

### 4. Capacity 是 Contract

max loan、buffer、history、borrow limit、safe overflow 都在公开配置中。

过载行为因此可以分析，而不是依赖“内存够不够”。

### 5. Crash Recovery 属于 Runtime

Node monitoring 与 stale cleanup 说明：

> 共享内存系统的生命周期不能只靠正常析构。

## Rust 在这里真正提供了什么

Rust 不会自动让跨进程通信安全。

但它能把一部分 ownership 规则压进类型系统：

~~~text
SampleMut::send(self)
~~~

通过消耗 self，阻止应用继续以原对象语义持有发送前的可写 handle。

而真正跨进程的 borrow/release 仍需要 runtime metadata。

所以：

~~~text
Rust ownership
+
IPC ownership protocol
~~~

两层缺一不可。

## 对具身智能最直接的价值

大图像、点云、tensor-like 数据最怕：

~~~text
producer private buffer
→ middleware copy
→ consumer copy
→ accelerator copy
~~~

iceoryx2 解决的是其中“同机进程间 CPU/shared-memory”这一层。

下一步 UCX / CUDA IPC / GXF 要继续解决：

~~~text
CPU ↔ GPU
GPU ↔ GPU
host ↔ host
distributed accelerator
~~~

## Atlas 的下一层已经自然出现

~~~text
Communication Foundations
↓
iceoryx2
  shared-memory ownership

↓
UCX
  heterogeneous transport

↓
GXF / Holoscan
  dataflow scheduling

↓
Embodied AI pipeline
~~~

因此 iceoryx2 不是新增一个普通中间件，而是 Atlas 从“消息系统”走向“具身运行时基础设施”的第一步。
