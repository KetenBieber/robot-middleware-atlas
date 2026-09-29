# Tag Send 到 Request：ucp_tag_send_nbx 为什么不是“把 buffer 丢给网卡”

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

从 public API 看，一次 tagged send 很简单：

~~~c
ucs_status_ptr_t ucp_tag_send_nbx(ucp_ep_h ep, const void *buffer,
                                  size_t count, ucp_tag_t tag,
                                  const ucp_request_param_t *param);
~~~

但它必须同时支持立即完成、异步完成、用户自带 request、不同 datatype、不同 memory type 和不同 protocol。真正的设计重点因此是“怎样把一次函数调用转化成可持续推进的状态机”。

## 先尝试最短路径

固定实现会先检查参数并进入 Worker 的条件临界区。对于普通 contiguous buffer，它会先尝试 inline fast path；只有没有立即完成时才分配 request。

~~~c
if (ucs_likely(attr_mask == 0)) {
    status = UCS_PROFILE_CALL(ucp_tag_send_inline, ep, buffer, count, tag,
                              param);
    ucp_request_send_check_status(status, ret, goto out);
    datatype      = ucp_dt_make_contig(1);
    contig_length = count;
}
~~~

高频小消息最怕“所有请求都先创建大对象再发现其实一条指令就能发完”。UCX 把 immediate completion 放在 request allocation 之前，就是典型 hot-path 设计。

## Request 来自池，而不是默认 malloc

当操作不能立即结束时：

~~~c
req = ucp_request_get_param(worker, param, {
    ret = UCS_STATUS_PTR(UCS_ERR_NO_MEMORY);
    goto out;
});
~~~

ucp_request_get_param 的宏进一步说明：若用户没有提供 request，就走 Worker 的 request pool；若用户提供，则把 public request 指针换回内部 header。

~~~c
#define ucp_request_get_param(_worker, _param, _failed)     ({         ucp_request_t *__req;         if (!((_param)->op_attr_mask & UCP_OP_ATTR_FIELD_REQUEST)) {             __req = ucp_request_get(_worker);             if (ucs_unlikely((__req) == NULL)) {                 _failed;             }         } else {             __req = ((ucp_request_t*)(_param)->request) - 1;             ucp_request_id_reset(__req);         }         __req;     })
~~~

这是一种很典型的 C runtime 布局技巧：内部 header 与用户扩展区连续放置，库内部拿 req，API 向用户返回 req + 1。

## 现代路径把请求交给 Protocol Framework

tag 被写入 request 后，真正的策略选择从这里发生：

~~~c
req->send.msg_proto.tag = tag;

ret = ucp_proto_request_send_op(ep, &ucp_ep_config(ep)->proto_select,
                                UCP_WORKER_CFG_INDEX_NULL, req, 0,
                                UCP_OP_ID_TAG_SEND, buffer, count,
                                datatype, contig_length, param, 0, 0);
~~~

注意此时还没有写死 TCP、RDMA 或 CUDA IPC。传下去的是 operation ID、buffer、datatype、长度与 Endpoint 的 proto_select。

因此一次 ucp_tag_send_nbx 可以有三种返回形态：错误指针、立即完成、request 指针。调用者必须把这三种情况区分开。所谓 non-blocking 不是“必定产生异步对象”，而是 **API 不要求阻塞到远端完成**；如果工作已经同步完成，最便宜的行为就是直接返回。

这条链很适合迁移到自己的 runtime 设计：先做 cheapest-fast-path，再分配 in-flight state；request 承担状态机，而 transport 只承担具体动作。这样 hot path、生命周期和硬件适配三个问题不会纠缠在一个 send 函数里。
