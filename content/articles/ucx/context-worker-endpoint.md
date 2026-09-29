# Context、Worker 与 Endpoint：UCX 怎样把资源、执行上下文和 Peer 分开

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

如果从零写一个高性能通信 runtime，很容易把“有哪些网卡”“哪个线程在推进”“我要发给谁”揉进同一个 Connection 类。UCX 刻意把它们拆开，因为三者的变化频率完全不同。

## Context：一次发现，长期复用

ucp_init_version 的核心不是创建 socket，而是读取配置、建立 Context、发现可用 transport 与 Memory Domain，并初始化 registration cache：

~~~c
context = ucs_calloc(1, sizeof(*context), "ucp context");
ucs_list_head_init(&context->cached_key_list);

status = ucp_fill_config(context, params, config);
if (status != UCS_OK) {
    goto err_free_ctx;
}

UCP_THREAD_LOCK_INIT(&context->mt_lock);

status = ucp_fill_resources(context, config);
if (status != UCS_OK) {
    goto err_thread_lock_finalize;
}

if (config->enable_rcache != UCS_NO) {
    status = ucp_mem_rcache_init(context, &config->rcache_config);
}
~~~

这里的设计顺序很自然：先决定“系统允许使用什么”，再让 Worker 与 Endpoint 在这组资源上工作。Memory registration cache 也属于 Context 级，因为同一片长期 buffer 不应被每次 send 重复注册。

## Worker：执行上下文，而不是业务线程

Worker 内部有 uct_worker、ifaces、req_mp、ep_config、event fd 与各类 pending 状态。它更像一个 **通信 progress domain**。多个 Endpoint 可以挂在同一个 Worker 上，共享底层接口和进度循环。

从数据结构上看，Worker 同时需要几种容器：

~~~text
req_mp / rkey_mp     -> 高频对象池
all_eps              -> 遍历 endpoint
ep_map/request_map   -> ID 到对象的快速定位
rkey_ptr_reqs        -> FIFO progress queue
ep_config            -> 可复用的 endpoint configuration
ifaces[]             -> transport resource index 直接寻址
~~~

如果用 C++ 重写，不能机械地把这些全换成 std::unordered_map。接口数组是资源索引，数组/O(1) 直接访问最合适；request 是高频生命周期，对象池比通用 map 更贴合；progress queue 需要稳定的队首语义。

## Endpoint：一名 Peer，多条 Lane

Endpoint config 中真正关键的是 lane 表：

~~~c
struct ucp_ep_config_key {
    ucp_lane_index_t         num_lanes;
    ucp_ep_config_key_lane_t lanes[UCP_MAX_LANES];

    ucp_lane_index_t         am_lane;
    ucp_lane_index_t         tag_lane;
    ucp_lane_index_t         wireup_msg_lane;
    ucp_lane_index_t         cm_lane;
    ucp_lane_index_t         keepalive_lane;

    ucp_lane_index_t         rma_lanes[UCP_MAX_LANES];
    ucp_lane_index_t         rma_bw_lanes[UCP_MAX_LANES];
    ucp_lane_index_t         rkey_ptr_lane;
    ucp_lane_index_t         amo_lanes[UCP_MAX_LANES];
    ucp_lane_index_t         am_bw_lanes[UCP_MAX_LANES];
};
~~~

这解释了为什么“Endpoint = socket”会误导。一个 peer 可能同时有低延迟 lane、高带宽 lane、RMA lane，甚至能够通过 rkey pointer 直接访问某类共享/GPU 映射。上层仍只持有一个 ucp_ep_h。

## 生命周期为什么这样分

Context 适合进程级长寿命；Worker 适合线程/执行域级长寿命；Endpoint 随 peer connection 变化；Request 只活到操作完成。它们的 teardown 也因此必须按依赖反向发生。

对于机器人软件，这种分层比“每个 topic 创建一套网络对象”更适合大吞吐数据面：相机、点云、模型 tensor 可以共享一套 transport/resource discovery，而各业务流只建立自己的 endpoint/request 状态。真正的隔离边界不是 topic 名，而是资源域、progress 域和 peer capability。
