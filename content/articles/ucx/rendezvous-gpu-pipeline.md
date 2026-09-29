# Rendezvous 与 GPU Pipeline：大 Tensor 为什么不该按“小消息放大版”发送

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

Eager 协议的直觉是“发送方直接把数据推过去”。对几十字节到几 KB 很自然；对几十 MB tensor，若仍先复制进中间 buffer，再由网络搬走，内存带宽与临时空间都会成为额外成本。

Rendezvous 的核心思想是先交换控制信息，再决定大 payload 真正怎样移动。

## RTS/RTR 先交换“数据在哪里、能有多大”

固定源码里的 RTS header：

~~~c
typedef struct {
    uint64_t          hdr;
    ucp_request_hdr_t sreq;
    uint64_t          address;
    size_t            size;
    uint8_t           opcode;
    /* packed rkeys follow */
} UCS_S_PACKED ucp_rndv_rts_hdr_t;
~~~

RTR 则带 receiver request ID、目标地址、size 与 offset。也就是说，控制消息先让两端建立“谁拥有 buffer、地址是什么、remote key 是什么”的共同状态，然后再选择真正的数据动作。

## GET ZCOPY：接收端直接拉

固定 GET 路径最终落到底层 UCT：

~~~c
uct_ep_h uct_ep = ucp_ep_get_lane(ep, lpriv->super.lane);
uct_rkey_t tl_rkey =
        ucp_rkey_get_tl_rkey(req->send.rndv.rkey,
                             lpriv->super.rkey_index);
uint64_t remote_address = req->send.rndv.remote_address + offset;

status = uct_ep_get_zcopy(uct_ep, iov, 1,
                          remote_address, tl_rkey, comp);
~~~

这里的数据面已经不是“把 payload 包进消息”。接收端通过 remote address + rkey 直接拉取发送端内存，完成后再用 ATS 等控制消息闭环 request 生命周期。

## rkey_ptr：如果远端内存能映成本地可读指针

有些同机共享内存或 CUDA IPC 情形，最便宜的动作甚至不是网络 GET。rkey_ptr 协议可以得到可访问的 pointer，然后按 segment unpack：

~~~c
src = UCS_PTR_BYTE_OFFSET(req->send.rndv.rkey_ptr_addr, offset);

status = ucp_datatype_iter_unpack(&req->send.state.dt_iter, worker,
                                  seg_size, offset, src);
~~~

这与 iceoryx2 的共享内存 offset ownership 有相似的“不要搬 payload”目标，但抽象不同：iceoryx2 围绕 IPC sample 生命周期组织；UCX 把 direct pointer 作为众多 rendezvous transport/protocol 之一。

## Pipeline：GPU staging 与网络传输可以重叠

当目标 transport 不能直接处理当前 memory type，最坏做法是：

~~~text
完整 GPU -> host copy
等待全部完成
完整 host -> network transfer
~~~

Pipeline 会把大 payload 分成 fragment。固定源码为 pipeline fragment 构造带 UCP_PROTO_SELECT_OP_FLAG_PPLN_FRAG 的 selection key，并再次做 protocol selection；每段 copy 与 network transfer 才有机会形成流水。

~~~text
fragment 0: GPU -> staging -> NIC
fragment 1:      GPU -> staging -> NIC
fragment 2:           GPU -> staging -> NIC
~~~

真正的优化对象因此不是“copy 次数”一个指标，而是 **copy、registration、network、completion 能否重叠，以及每段 fragment 的固定开销**。源码甚至给 fragment overhead 单独建立 performance factor。

对具身大模型很有启发：大 tensor 传输不应被当成大号 ROS message。它更像一个 memory movement plan，需要知道源/目的设备、可直接访问关系、分片大小和并行流水。Rendezvous 正是把这类计划从普通 eager send 中分离出来。

## 为什么 Eager 会在大消息上遇到结构性问题

先不考虑 UCX，自己设计最简单的 eager send：

~~~text
application buffer
↓ copy / pack
transport buffer
↓
NIC
↓
receiver transport buffer
↓ copy / unpack
application buffer
~~~

如果 payload 只有 64 B，固定控制开销通常比 memcpy 更重要。

如果 payload 是 64 MB Tensor，一次额外 host copy 就意味着 64 MB read + 64 MB write，还会占用 memory bandwidth、cache/NUMA 流量和临时 buffer 容量。如果数据本来在 GPU，再强制 staging 到 host，代价会更高。

因此 Rendezvous 的根本动机不是“消息大了换一个协议名”，而是：

> **先交换足够的 metadata，再为大 payload 单独制定 memory movement plan。**

## RTS → 数据搬运 → Completion：控制面和数据面分开

可以把 Rendezvous 看成两层：

~~~text
control plane:
small header / request id / address / size / rkey

data plane:
the actual large payload movement
~~~

一条概念化状态机：

~~~text
Sender
  SEND_RTS
     │
     ▼
Receiver learns:
  size
  sender request id
  remote address
  packed rkey
     │
     ▼
choose data movement
  GET / PUT / rkey_ptr / pipeline
     │
     ▼
DATA MOVES
     │
     ▼
ATS / RTR / protocol-specific completion
     │
     ▼
both sides can retire request
~~~

小控制消息先交换的意义在于，系统可以把：

~~~text
源 memory type
目标 memory type
remote access capability
registration / rkey
message length
available lane
~~~

都纳入计划，再决定是不是值得支付 registration、remote access 或 pipeline 的启动成本。

## RKey 本质上是远端访问能力，而不是“另一个指针”

一台机器上的虚拟地址单独交给另一台机器通常没有意义。RDMA 类路径还需要：

~~~text
remote address
+
registered memory
+
remote key / access capability
~~~

所以 RTS 后面会携带 packed rkey。

这暴露了消息 API 经常隐藏的一层事实：

> **大数据传输的对象不是普通字节串，而是带 Memory Domain、registration 与访问能力的 Buffer。**

如果 buffer 来自长期 tensor pool，registration 可以复用；如果每帧都临时 malloc/free，再每次 register/deregister，所谓 zero-copy 的收益可能被 registration 开销抵消。

因此研究 RDMA/GPU path 时，不能只画“有没有 memcpy”，还要画：

~~~text
allocate
register
export / pack key
transfer
completion
deregister
free
~~~

## Buffer Pool 为什么和 Rendezvous 天然配套

一个感知 pipeline 很适合预分配：

~~~text
Tensor / Staging Pool

slot 0
slot 1
slot 2
slot 3
~~~

每个 slot 除了 pointer，还应该保存：

~~~text
memory type
device id
capacity
registration / exported handle
generation
state
~~~

生命周期可以明确写成：

~~~text
FREE
↓ producer acquires
WRITING
↓ camera/inference completion
READY
↓ communication request acquires transfer right
IN_FLIGHT
↓ transport completion
RECLAIMABLE
↓
FREE
~~~

这里最关键的一条不变量是：

> **IN_FLIGHT 的 Buffer 不能因为 send API 已经返回，就被 Producer 重新写入。**

这和共享内存的 loan/send/reclaim、DMA buffer ownership 完全是同一个问题，只是异步访问者变成了 GPU/NIC/远端 peer。

## GET 和 PUT 不只是“数据方向反过来”

GET：

~~~text
Sender exposes source buffer
Receiver schedules pull
~~~

PUT：

~~~text
Receiver exposes destination buffer
Sender schedules push
~~~

二者会改变：

~~~text
哪一侧需要准备 destination
哪一侧掌握搬运时机
哪一侧先知道 completion
哪一侧需要 remote key
~~~

所以协议选择不能只看 peak bandwidth，也要看当前 buffer ownership 与哪一端已经有可注册的最终存储。

## rkey_ptr 是“直接可访问”时的特殊快路径

同机情况下，某些 Memory Domain 可以把远端 memory handle 翻译成本进程可访问的 pointer：

~~~text
remote descriptor
↓
rkey_ptr
↓
locally accessible address
↓
unpack / consume
~~~

但这里仍不能把“direct pointer”自动等价为全链 zero-copy。

如果后续仍调用 datatype unpack，把数据从这个映射地址复制到另一个 receive buffer，那么只是省掉了网络传输或中间 staging，并不是没有 payload copy。

更准确的做法是逐边标注：

~~~text
source storage
→ descriptor exchange
→ direct mapping
→ optional unpack/copy
→ final consumer storage
~~~

这样“零复制”才是可审计的。

## GPU Pipeline 的核心其实是 Fragment Ownership

当目标 transport 不能直接处理 GPU memory 时，典型 fallback 是：

~~~text
GPU
↓ D2H
pinned host staging buffer
↓ NIC
network
~~~

如果只有一个 staging slot：

~~~text
copy F0
wait
send F0
wait
copy F1
wait
send F1
...
~~~

copy 和 network 基本串行。

引入多个 fragment slot 才可能形成流水：

~~~text
time ─────────────────────────────>

GPU copy:   F0   F1   F2   F3
             \    \    \    \
NIC send:         F0   F1   F2   F3
~~~

于是每个 fragment 都要拥有自己的状态：

~~~text
STAGING_FREE
↓
GPU_COPY_IN_FLIGHT
↓ CUDA event/fence
READY_FOR_NETWORK
↓
NETWORK_IN_FLIGHT
↓ transport completion
STAGING_FREE
~~~

“Pipeline”因此不是把一个 for-loop 切成几段，而是允许多份 fragment 同时处于不同异步阶段。

## Fragment Size 为什么不能拍脑袋

fragment 太小：

~~~text
更多 protocol dispatch
更多 header / request
更多 completion
更多 GPU event
~~~

fragment 太大：

~~~text
首段更晚进入网络
staging slot 更大
copy/network overlap 粒度更粗
~~~

因此可以用一个简化模型理解：

~~~text
T_total
≈
startup
+
N_fragments × fixed_fragment_cost
+
pipeline_bottleneck_time
~~~

真正需要优化的是：

~~~text
固定分片开销
copy bandwidth
network bandwidth
可并行 in-flight 数
staging pool size
~~~

固定源码把 pipeline fragment 单独带上 UCP_PROTO_SELECT_OP_FLAG_PPLN_FRAG，再次进入 protocol selection，就是因为“一整个大消息”和“流水线中的一个 fragment”具有不同的性能模型。

## Completion 也是 Ownership 边界

CPU 调用异步 GPU copy 后，host staging buffer 何时可以交给 NIC，必须由 CUDA event / stream completion 一类同步点证明。

反过来，NIC/transport 报告 completion 后，也要问：

~~~text
只证明 local DMA 不再读取 buffer？
还是远端已经取得 payload？
协议是否仍可能为了重试保留数据？
业务 Consumer 是否已经处理？
~~~

这些 completion 的语义层级不同。

因此每个 completion 都要问：

> **它究竟证明哪一个参与者已经放弃了这块 Buffer 的访问权？**

这和 DDS transport ACK 不等于业务动作 ACK、共享内存 descriptor 被 dequeue 不等于 payload 可回收，是完全相同的分层思路。

## Backpressure 最终会落到有限 Buffer Pool

假设 staging/tensor pool 只有 4 个 slot：

~~~text
slot 0 IN_FLIGHT
slot 1 IN_FLIGHT
slot 2 IN_FLIGHT
slot 3 IN_FLIGHT
~~~

下一帧又来了，系统仍然必须选择：

~~~text
block producer
drop new frame
drop/cancel old transfer
reduce producer rate
allocate emergency buffer
~~~

Rendezvous、RDMA、GPU Direct 都没有消灭 backpressure；只是把压力从“字节 FIFO 堆了多少”变成：

> **有限 Buffer ownership 是否被长时间占住。**

因此大 Tensor 数据面值得持续观测：

~~~text
free pool slots
in-flight slots
oldest buffer age
registration cache hit rate
staging wait time
GPU-copy completion latency
network completion latency
~~~

## 一个具身 VLA 数据面的具体分层

假设：

~~~text
Camera
↓
GPU vision encoder
↓
visual embeddings
↓
second GPU / remote VLA policy
~~~

不应该默认变成：

~~~text
GPU tensor
↓ copy to CPU vector
↓ protobuf serialize
↓ generic middleware
↓ deserialize
↓ copy to GPU
~~~

更值得设计的是一个 Buffer/Tensor descriptor：

~~~text
TensorHandle
  memory handle / pointer
  memory type
  device id
  shape / dtype / stride
  generation
  completion event
        │
        ▼
data-plane planner
        │
        ├── CUDA IPC / local direct path
        ├── RDMA / direct-capable path
        └── staged pipeline fallback
~~~

上层消息系统负责：

~~~text
tensor identity
shape/schema
request id
deadline
route
~~~

大 payload 的实际搬运则交给 heterogeneous data plane。

这就是 UCX 对具身 runtime 最值得迁移的一条设计：**消息语义和 memory movement 不必由同一个抽象承担。**

## 从 Rendezvous 真正应该带走的四个问题

无论以后换成 CUDA IPC、DMA-BUF、RDMA、共享内存还是其他 accelerator runtime，都可以继续问：

~~~text
1. Payload 当前属于哪个 Memory Domain？
2. 对端凭什么获得访问它的 descriptor / key？
3. 谁真正搬数据：CPU、GPU、NIC 还是 direct mapping？
4. 哪一个 completion 才允许原 Buffer 被复用？
~~~

只要这四个问题能画清楚，一条大数据路径的 ownership 和性能边界通常就不会再模糊。
