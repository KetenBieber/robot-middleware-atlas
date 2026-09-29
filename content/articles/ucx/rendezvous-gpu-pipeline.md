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
