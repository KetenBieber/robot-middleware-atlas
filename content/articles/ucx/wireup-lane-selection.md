# Wireup 与 Lane Selection：一个 Peer 为什么可以同时拥有多条数据路径

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

UCP 创建 Endpoint 后必须回答一个比 connect 更复杂的问题：本地有 shared memory、TCP、IB、GPU transport，对端也报告了自己的 capability，哪些组合真的可达，又应该分别承担什么角色？

这就是 wireup 与 lane selection 的职责。

## Lane 不是“网卡编号”

每条 lane 的 key 同时记录 resource、远端 Memory Domain、system device 与用途：

~~~c
typedef struct ucp_ep_config_key_lane {
    ucp_rsc_index_t      rsc_index;
    ucp_md_index_t       dst_md_index;
    ucs_sys_device_t     dst_sys_dev;
    uint8_t              path_index;
    ucp_lane_type_mask_t lane_types;
    uint8_t              port_speed;
    size_t               seg_size;
} ucp_ep_config_key_lane_t;
~~~

lane_types 才是理解它的关键。同一条物理 transport 可能承担多个语义角色；同一 peer 也可能为 AM、RMA、RKEY_PTR、TAG 等角色选择不同 transport。

Endpoint configuration 还会把这些角色解析成可直接索引的 lane：

~~~c
struct ucp_ep_config_key {
    ucp_lane_index_t rma_lanes[UCP_MAX_LANES];
    ucp_lane_index_t rma_bw_lanes[UCP_MAX_LANES];
    ucp_lane_index_t rkey_ptr_lane;
    ucp_lane_index_t amo_lanes[UCP_MAX_LANES];
    ucp_lane_index_t am_bw_lanes[UCP_MAX_LANES];
};
~~~

所以高层协议不必再次遍历所有 transport；需要 RMA 时读取优先级排序后的 rma_lanes，需要 direct pointer 能力时读取 rkey_ptr_lane。Wireup 已经把 capability matching 的结果压缩成运行时可直接消费的索引。

## 先搜索，再构造 Endpoint 配置

固定源码的主函数很短，但透露了算法框架：

~~~c
ucs_status_t
ucp_wireup_select_lanes(ucp_ep_h ep, unsigned ep_init_flags,
                        ucp_tl_bitmap_t tl_bitmap,
                        const ucp_unpacked_address_t *remote_address,
                        unsigned *addr_indices, ucp_ep_config_key_t *key,
                        int show_error)
{
    ucp_worker_h worker = ep->worker;
    ucp_tl_bitmap_t scalable_tl_bitmap = worker->scalable_tl_bitmap;

    UCS_STATIC_BITMAP_AND_INPLACE(&scalable_tl_bitmap, tl_bitmap);

    if (!UCS_STATIC_BITMAP_IS_ZERO(scalable_tl_bitmap)) {
        ucp_wireup_select_params_init(&select_params, ep, ep_init_flags,
                                      remote_address, scalable_tl_bitmap, 0);
        status = ucp_wireup_search_lanes(&select_params, key->err_mode,
                                         &select_ctx);
        if (status == UCS_OK) {
            goto out;
        }
    }

    ucp_wireup_select_params_init(&select_params, ep, ep_init_flags,
                                  remote_address, tl_bitmap, show_error);
    status = ucp_wireup_search_lanes(&select_params, key->err_mode,
                                     &select_ctx);
    if (status != UCS_OK) {
        return status;
    }

out:
    return ucp_wireup_construct_lanes(&select_params, &select_ctx,
                                      addr_indices, key);
}
~~~

这里先尝试 scalable transport 集合；失败再回退到完整集合。真正的选择依据还会考虑 capability、distance、bandwidth 等因素。最后得到的不是“transport name”，而是完整的 ucp_ep_config_key。

## 为什么把连接选择和每次发送选择分成两层

Wireup 处理的是 **peer 级可达性**：这台机器和那台机器之间有哪些候选能力。Protocol selection 处理的是 **operation 级最优策略**：这次是 64 B host buffer，还是 32 MB CUDA tensor；需要 TAG、RMA 还是 AM。

把两层合并会导致每次发送重复做昂贵拓扑匹配；完全只在连接时选一次又会错过消息长度和 memory type 差异。UCX 的做法是先把 Endpoint 的可行空间压缩成 lane config，再让每次操作在这个空间内走缓存化 protocol selection。

## 同机与跨机只是候选集不同

同进程/同机时，shared-memory、CMA、CUDA IPC 等 transport 可能进入候选；跨机时这些路径自然不可达，IB/RDMA 或 TCP 等路径留下。应用并不需要把自己的算法改成两套 send API。

这和消息中间件常见的“SHM fast path + network fallback”看起来相似，但 UCX 的粒度更底层：它不只在 host shared memory 与 UDP/TCP 间切换，还把 Memory Domain、system device、RMA capability 和 GPU direct-access 纳入 lane 计划。对于多 GPU、多 NIC 的具身计算节点，这种 lane 模型才真正开始体现价值。
