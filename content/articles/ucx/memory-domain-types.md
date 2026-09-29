# Memory Domain 与异构内存：为什么 GPU Buffer 不是换一个指针就能发送

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

一块 CUDA device pointer 从 C 语言语法上看仍然是地址，但“有地址”不等于 CPU、NIC 或另一块 GPU 都能直接访问。高性能异构通信首先要回答的是访问域，而不是 memcpy API。

UCT 的 Memory Domain 正是这层能力边界。

## MD 会公开自己能处理哪些 memory type

固定源码的 uct_md_attr_v2_t 有一组非常直接的 bitmap：

~~~c
typedef struct {
    uint64_t field_mask;
    uint64_t max_alloc;
    size_t   max_reg;
    uint64_t flags;

    uint64_t reg_mem_types;
    uint64_t reg_nonblock_mem_types;
    uint64_t cache_mem_types;
    uint64_t gva_mem_types;
    uint64_t detect_mem_types;
    uint64_t alloc_mem_types;
    uint64_t access_mem_types;
    uint64_t dmabuf_mem_types;

    ucs_linear_func_t reg_cost;
    char              component_name[UCT_COMPONENT_NAME_MAX];
    size_t            rkey_packed_size;
    ucs_cpu_set_t     local_cpus;
} uct_md_attr_v2_t;
~~~

这些字段把几个常被混为一谈的问题拆开了：能检测某种 memory type，不代表能分配；能 access，不代表能 register；能 register，也不代表 registration 没成本。

## Registration 是“让设备知道这块内存”

以 RDMA 为例，NIC 不能凭一个普通虚拟地址就安全地远程 DMA。内存通常需要 pin/register，并生成 local/remote key。Registration 有固定成本和随长度变化的成本，所以结构体里甚至直接携带 reg_cost。

因此高频发送同一块大 buffer 时，registration cache 很重要：把“每次消息的协议状态”与“长期 buffer 的注册状态”分开，避免重复 pin/register。

## Memory type 会一路进入 protocol selection

UCP 初始化 request 的 datatype iterator 时得到 mem_info，随后 selection key 中直接保存：

~~~c
struct ucp_proto_select_param {
    uint8_t op_id_flags;
    uint8_t op_attr;
    uint8_t dt_class;
    uint8_t mem_type;
    uint8_t sys_dev;
    uint8_t sg_count;
    ...
};
~~~

所以 HOST 8 MB 与 CUDA 8 MB 即使长度相同，也完全可能选择不同协议。sys_dev 进一步表达设备拓扑：同一种 CUDA memory，靠近哪张 GPU/NIC 也会改变数据路径的代价。

## “GPU Direct”不能被理解成一个布尔开关

实际候选可能包括 CUDA IPC、GPU copy、host staging、RDMA、rkey pointer 等。哪条可用取决于设备、驱动、MD capability、peer 拓扑与消息规模。

对于 VLA/世界模型部署，这一点很现实。假设视觉 encoder 的 feature tensor 已在 GPU，如果中间件先强制序列化到 host heap，再走普通 socket，前面所有 GPU pipeline 优化都可能被一次 D2H copy 抵消。反过来，若小控制命令也强行做昂贵 registration，同样不合理。

因此设计具身数据面时，应把 payload location 当成一等类型信息。UCX 的 memory type + Memory Domain 给出了一种成熟做法：**地址只是值，访问能力必须由内存域与设备能力共同证明。**
