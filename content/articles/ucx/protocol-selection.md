# Protocol Selection：UCX 怎样把“选最快路径”变成可缓存的数据结构问题

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

“根据硬件自动选择最快协议”如果只写成一句宣传语，没有工程意义。真正困难的是：**选择依据是什么、多久计算一次、怎样避免每个包都遍历所有协议、消息尺寸变化怎么处理。**

UCX 的答案是 selection key + cache/hash + size threshold。

## Selection key 只有 64 bit 左右，却装进了关键维度

固定源码定义：

~~~c
struct ucp_proto_select_param {
    uint8_t op_id_flags;
    uint8_t op_attr;
    uint8_t dt_class;
    uint8_t mem_type;
    uint8_t sys_dev;
    uint8_t sg_count;
    union {
        struct {
            uint8_t mem_type;
            uint8_t sys_dev;
        } UCS_S_PACKED reply;
        uint8_t mem_flags;
        uint8_t padding[2];
    } UCS_S_PACKED op;
} UCS_S_PACKED;
~~~

这几个字段足以把“同一个 API”拆成不同通信情形：TAG SEND 与 RMA PUT 不是同一 operation；contiguous 与 IOV 不同；HOST 与 CUDA 不同；GPU0 附近与另一 NUMA/system device 不同；scatter/gather 段数也会影响可用协议。

ucp_proto_request_send_op 初始化 datatype iterator 后，就把这些信息组成 select_param：

~~~c
ucp_proto_select_param_init(&sel_param, op_id, param->op_attr_mask,
                            op_flags, req->send.state.dt_iter.dt_class,
                            &req->send.state.dt_iter.mem_info, sg_count);

msg_length = req->send.state.dt_iter.length + header_length;
~~~

## 先按“类型”缓存，再按“长度”找阈值

查找路径展示了一个很漂亮的数据结构组合：

~~~c
key.param = *select_param;

if (ucs_likely(proto_select->cache.key == key.u64)) {
    select_elem = proto_select->cache.value;
} else {
    khiter = kh_get(ucp_proto_select_hash, proto_select->hash, key.u64);
    if (ucs_likely(khiter != kh_end(proto_select->hash))) {
        select_elem = &kh_value(proto_select->hash, khiter);
    } else {
        select_elem = ucp_proto_select_lookup_slow(worker, proto_select, 0,
                                                   ep_cfg_index,
                                                   rkey_cfg_index,
                                                   &key.param);
    }

    proto_select->cache.key   = key.u64;
    proto_select->cache.value = select_elem;
}

return ucp_proto_select_thresholds_search(select_elem, msg_length);
~~~

最热的一种 key 先命中单项 cache；否则进入 kHash；只有没初始化过的 key 才走 slow path。拿到该 key 的 protocol set 后，再根据 msg_length 找所在区间。

如果用 C++ 表达，可以把它理解成：

~~~text
LastValueCache<SelectionKey, ProtocolTable>
            |
            + miss
            v
unordered_map<SelectionKey, ProtocolTable>
            |
            + ProtocolTable = piecewise ranges by message length
~~~

但 UCX 为 hot path 使用紧凑 packed key，并没有把高级容器抽象当成目标。

## 为什么长度不能直接塞进 hash key

如果把每一个 message length 都做 key，1 B、2 B、3 B……会产生巨大状态空间，而且大多数协议性能是分段规律：小消息受固定开销主导，中大消息受 bandwidth/registration/zcopy 成本主导。

因此协议 probe 形成分段性能模型，选择结果最终变成若干长度区间。运行时只需在阈值表里找当前长度所在区间。这是一种典型的 **把昂贵优化离线化/初始化化，再把运行时决策压成查表** 的思路。

对具身数据面而言，这意味着 64 B 控制元数据与 20 MB tensor 即使使用同一个 endpoint，也没有理由走同一协议。UCX 的 protocol selection 把这种差异变成可计算、可缓存、可复用的运行时计划。

## 这里其实有两级“编译”

把 UCX 的决策链展开：

~~~text
machine + peer capability
↓
Wireup
↓
Endpoint lane config

operation + datatype + memory information
↓
Protocol slow path
↓
threshold table

hot path
↓
cache/hash lookup
↓
length range lookup
~~~

第一层把“哪些路径可行”从每次发送的热路径移走；第二层把“不同长度应该选哪个 protocol”变成可缓存的执行计划。

因此最值得迁移的思想不是 UCX 具体有多少协议，而是：

> **不要在每一帧数据上重复解决已经可以提前求解的问题。**

## 为什么 Slow Path 可以复杂，而 Hot Path 必须简单

一个 selection key 第一次出现时，slow path 可以做：

~~~text
enumerate candidate protocols
↓
check endpoint / lane capability
↓
estimate performance
↓
find crossover points
↓
construct threshold ranges
↓
cache the result
~~~

只要这个 key 会被后续大量发送复用，这个复杂初始化成本就能被摊薄。

之后 hot path 只需要：

~~~text
packed key compare
↓ miss
hash lookup
↓
length threshold search
~~~

这是一种典型的系统结构：

~~~text
configuration / compilation plane
             ↓
         cached plan
             ↓
       execution hot path
~~~

同一思想可以迁移到机器人 topic route、GPU kernel plan、EtherCAT PDO mapping、sensor graph 和 serialization schema。周期越短、数据频率越高，就越不应该把复杂拓扑搜索和代价估计留在 hot path。

## Selection Key 定义的是“哪些差异足以改变执行计划”

设计 cache key 有两个相反的错误。

### Key 太少

如果只记录 operation 和 message size，却忽略 HOST vs CUDA、GPU0 vs GPU1/system device、contiguous vs IOV、scatter/gather count，就可能把一个针对 Host memory 的 plan 错误复用到 GPU buffer。

### Key 太多

反过来，如果把 exact pointer、exact message length、timestamp、request id 全部放进 key，cache 几乎永远不会命中。

所以好的 key 实际是在定义：

> **哪些输入属于同一个“执行计划等价类”。**

固定源码中的紧凑 ucp_proto_select_param 正是在做这种状态压缩：只保留会真正改变协议选择的维度。

## 为什么 Message Length 更适合做 Piecewise Range

假设有两种协议，先用一个简化模型：

~~~text
Eager:
T_eager(n) = A + n / B1

Zcopy:
T_zcopy(n) = C + n / B2
~~~

其中 A < C，而 B2 > B1。小消息时固定启动成本更重要，Eager 可能更合适；大消息时 copy 和带宽逐渐主导，Zcopy 可能更合适。

真实系统还需要考虑 registration、latency、bandwidth、CPU overhead、memory-type copy 和 fragment cost，但结果仍可以压成：

~~~text
[0, N0)         → protocol A
[N0, N1)        → protocol B
[N1, infinity)  → protocol C
~~~

于是“消息长度”不必进入 hash key 形成成千上万个离散状态，只需要在一个已经生成好的分段表里查区间。

这就是 threshold table 的第一性原理。

## 数据结构组合比“用 Hash”更值得学

这个 lookup 实际用了三层不同的数据结构思想：

~~~text
1. last-value cache
   最热 key O(1) 直接命中

2. hash
   处理多个已经出现过的 selection key

3. threshold table
   同一个 key 下按 length 选择 protocol
~~~

每一层都解决不同访问模式。

如果自己写 C++ 异构 Buffer Runtime，可以得到：

~~~cpp
struct PlanKey {
    Operation op;
    MemoryType src;
    MemoryType dst;
    DeviceId src_device;
    DeviceId dst_device;
    LayoutClass layout;
};

struct LengthRange {
    std::size_t max_length;
    ProtocolId protocol;
};

struct Plan {
    std::vector<LengthRange> ranges;
};
~~~

运行时可以组合 last PlanKey cache、flat_hash_map/unordered_map 与 compact range search，而不是所有状态都扔进一种容器。

## Last-value Cache 为什么值得单独存在

机器人 pipeline 常具有强烈的时间局部性：

~~~text
Camera frame:
CUDA
same shape
same endpoint
same operation

Camera frame:
CUDA
same shape
same endpoint
same operation

...
~~~

连续几百帧的 selection key 很可能完全一致。

如果每次都重新 hash/probe table，虽然每次开销不大，但仍然是高频 hot path 的固定成本。

单项 last-value cache：

~~~text
if current_key == last_key:
    use last_plan
~~~

就可以利用“刚用过的东西很可能马上再用”的时间局部性。

## Cache 必须有 Identity 和 Invalidation

所有预计算优化都有共同风险：

> **旧计划什么时候失效？**

Protocol plan 至少依赖 Endpoint config、lane capability、rkey config 和 memory information。

如果 peer 重连、lane config 被重新建立，却继续使用旧 plan，就会出现 stale configuration。

所以任何自己设计的 runtime cache 都要同时考虑：

~~~text
config identity
generation
invalidation
rebuild
~~~

这和 shared-memory generation counter、route cache version、GPU graph generation 本质上是同一问题。

缓存从来不是“把结果放 map 里就结束”，而是**结果 + 成立条件 + 失效协议**。

## Protocol Selection 不等于业务调度

UCX 解决：

~~~text
这一条 operation
在当前 endpoint/memory 条件下
该用哪一种协议搬
~~~

它不解决：

~~~text
Control tensor 和 Camera tensor 谁优先？
deadline 快到了是否取消旧请求？
submit queue 满时丢哪一帧？
不同业务流怎样分配 bandwidth？
~~~

因此完整的具身 runtime 仍然需要：

~~~text
business scheduling / deadline policy
↓
bounded queue / ownership
↓
UCX protocol selection
↓
UCT transport
~~~

不要把“底层自动选择最快协议”误解成“整个系统已经自动得到最优实时调度”。

## 从源码真正应该追的链

固定版本里，hot-path lookup 位于 [proto_select.inl](https://github.com/openucx/ucx/blob/8a6b06fb880accbb933a79cda893883872c68d9d/src/ucp/proto/proto_select.inl#L77)，slow path 位于 [proto_select.c](https://github.com/openucx/ucx/blob/8a6b06fb880accbb933a79cda893883872c68d9d/src/ucp/proto/proto_select.c#L539)。

阅读时可以按：

~~~text
ucp_proto_select_param
↓
ucp_proto_select_lookup
↓
single-entry cache
↓ miss
kHash
↓ miss
ucp_proto_select_lookup_slow
↓
threshold elements
↓
message-length search
~~~

把调用链画出来。

到这一步，“UCX 自动选择协议”就不再是一句性能宣传，而是一套可以复刻的数据结构、缓存与 plan-compilation 方法。
