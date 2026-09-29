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
