# UCX 架构地图：Context、Worker、Endpoint、Request 怎样连成一张对象图

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

先不要从函数名记忆 UCX。把运行时还原成对象关系，会发现最重要的四个 UCP 对象分别对应四种寿命：进程级资源、执行上下文、peer 连接、一次在途操作。

~~~text
ucp_context_t
  |
  +-- transport resources / memory domains / config
  |
  +-- ucp_worker_t
       |
       +-- uct_worker_t
       +-- ucp_worker_iface_t* ifaces[]
       +-- request pool
       +-- endpoint list / maps
       |
       +-- ucp_ep_t  ---- lane[0] -> uct_ep_t
       |             ---- lane[1] -> uct_ep_t
       |             ---- lane[N] -> uct_ep_t
       |
       +-- ucp_request_t ...
~~~

## Worker 为什么是最值得看的结构体

固定源码中的 ucp_worker_t 不是一个薄句柄。删去与主题无关字段后，仍能看到非常明确的运行时骨架：

~~~c
typedef struct ucp_worker {
    ucs_async_context_t  async;
    ucp_context_h        context;
    uct_worker_h         uct;

    ucs_mpool_t          req_mp;
    ucs_mpool_t          rkey_mp;

    ucs_list_link_t      all_eps;
    ucp_worker_iface_t   **ifaces;
    unsigned             num_ifaces;

    ucs_queue_head_t     rkey_ptr_reqs;
    ucp_tag_match_t      tm;

    ucp_ep_h             mem_type_ep[UCS_MEMORY_TYPE_LAST];

    ucp_worker_rkey_config_hash_t rkey_config_hash;
    UCS_PTR_MAP_T(ep)             ep_map;
    UCS_PTR_MAP_T(request)        request_map;

    ucp_ep_config_arr_t  ep_config;
    ucp_rkey_config_arr_t rkey_config;
} ucp_worker_t;
~~~

这里没有 C++ STL，但设计问题与 STL 容器选择完全同构。高频、短寿命 request 用 memory pool，而不是每次 malloc/free；需要按 ID 找回 endpoint/request 的对象用 pointer map/hash；需要 FIFO 推进的 rkey_ptr request 用 queue；所有 endpoint 用 intrusive list 维护。**数据结构是按访问模式选的，而不是统一塞进一个 map。**

这也是研究 C 系统代码时很有价值的一点：STL 只是容器实现之一，真正要学的是 workload。若自己写 C++ runtime，同样的问题会对应 object pool、unordered_map、deque/list、flat vector 等不同选择。

## Endpoint 不是“一条 socket”

ucp_ep_t 表示一个 peer，但 peer 到 peer 并不只绑定一个 transport。Endpoint config 会保存多条 lane；一条 lane 关联一个 UCT endpoint，并标注它适合哪些语义。RMA、high-bandwidth RMA、atomics、tag offload、wireup、keepalive 可以落在不同 lane 上。

因此“连接建立成功”并不等于“选出一个 socket”。更准确的说法是：UCX 根据本地和对端 capability 建立一张 **peer-specific communication plan**，之后 protocol selection 再在这张 plan 上挑实际协议。

## Request 是协议状态机的承载体

一次发送若立即完成，可以直接返回成功；若不能立即完成，UCP 需要保存 datatype iterator、chosen protocol、stage、completion、callback 等状态。ucp_request_t 就是这个在途状态机的载体。

Request 被池化还有第二层意义：高频控制/感知 pipeline 中，一秒可能发出成千上万次小操作。若每一次都走通用 heap allocator，不但平均开销上升，尾延迟也更难控制。对象池把“动态生命周期”与“每次向操作系统申请内存”分开。

所以 UCX 的对象图本质上围绕三个问题组织：**资源发现归 Context，执行与 progress 归 Worker，peer capability 归 Endpoint，短期协议状态归 Request。** 这四种寿命分开以后，transport 与 protocol 才能在不污染应用 API 的情况下替换。
