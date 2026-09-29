# UCX 官方 Hello World 实验：把 Endpoint、Request 与 Progress 真正跑起来

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

这个实验直接使用上游 examples/ucp_hello_world.c。它比自己造最小 Demo 更有价值，因为同一个例子同时展示 UCP 初始化、Worker address 交换、Endpoint、tag send/recv、request callback、busy progress 与 eventfd wait。

## 先看程序到底初始化了什么

官方代码请求 TAG feature；在 wait/eventfd 模式下额外请求 WAKEUP，并把 Worker 设为 SINGLE：

~~~c
ucp_params.field_mask = UCP_PARAM_FIELD_FEATURES |
                        UCP_PARAM_FIELD_REQUEST_SIZE |
                        UCP_PARAM_FIELD_REQUEST_INIT |
                        UCP_PARAM_FIELD_NAME;
ucp_params.features   = UCP_FEATURE_TAG;

if (ucp_test_mode == TEST_MODE_WAIT ||
    ucp_test_mode == TEST_MODE_EVENTFD) {
    ucp_params.features |= UCP_FEATURE_WAKEUP;
}

status = ucp_init(&ucp_params, config, &ucp_context);

worker_params.field_mask  = UCP_WORKER_PARAM_FIELD_THREAD_MODE;
worker_params.thread_mode = UCS_THREAD_MODE_SINGLE;
status = ucp_worker_create(ucp_context, &worker_params, &ucp_worker);
~~~

例子用普通 TCP socket 做 OOB address exchange。这个 socket 只负责让双方获得 UCX worker address，不是之后 payload 的固定 transport。Endpoint 建好后，真正数据路径仍由 UCX 根据可用 transport 选择。

## 两个终端运行

在已经构建好 UCX examples 的 Linux 环境中，服务端直接启动：

~~~bash
./ucp_hello_world
~~~

客户端指定服务端主机：

~~~bash
./ucp_hello_world -n <server-host>
~~~

程序注释本身就给出了这组启动方式。先不要改任何 transport，观察 send/receive handler 和 completion 输出。

## 强制不同 transport，验证“API 不变，数据面变”

UCX_TLS 可以限制候选 transport。具体可用名称取决于本机 build 与硬件，可以先用 ucx_info 查看设备与 transport。

仅做同机共享内存方向的实验时，可根据本机 ucx_info 输出选择对应 shm transport；网络环境可选择 tcp；具备 IB/RDMA 环境时再选择相应 rc/ud/mlx5 transport。关键观察点不是背命令，而是保持 ucp_tag_send_nbx 业务代码不变，只改变候选 transport 集合。

例如 TCP 路径可以用：

~~~bash
UCX_TLS=tcp ./ucp_hello_world
UCX_TLS=tcp ./ucp_hello_world -n <server-host>
~~~

如果本机支持对应共享内存 transport，也可以把 UCX_TLS 换成 ucx_info 实际报告的 shm 项进行对照。

## 在源码里盯住一次 send

官方例子最终仍然只是：

~~~c
send_param.op_attr_mask = UCP_OP_ATTR_FIELD_CALLBACK |
                          UCP_OP_ATTR_FIELD_USER_DATA;
send_param.cb.send      = send_handler;
send_param.user_data    = (void*)addr_msg_str;

request = ucp_tag_send_nbx(server_ep, msg, msg_len, tag, &send_param);
status  = ucx_wait(ucp_worker, request, "send", addr_msg_str);
~~~

改变 UCX_TLS 后，这段业务代码不需要切换为 tcp_send、shm_send 或 rdma_send。差异发生在 Endpoint lane 与 protocol/UCT 层。

## 对照 busy progress

ucx_wait 的核心：

~~~c
while (!request->completed) {
    ucp_worker_progress(ucp_worker);
}
~~~

可以用 profiler 或简单计数观察循环频率。此模式 CPU 占用高，但能持续推进 completion。

## 对照 eventfd wait

官方例子还实现了 ucp_worker_get_efd + ucp_worker_arm + epoll_wait。关键顺序是：

~~~c
status = ucp_worker_arm(ucp_worker);
if (status == UCS_ERR_BUSY) {
    /* 已有事件，不能进入阻塞等待 */
}

err = epoll_wait(epoll_fd_local, &ev, 1, -1);
~~~

这能直观看到 UCX progress 与操作系统事件循环如何结合。把它映射到机器人 runtime，就是“通信线程忙轮询”与“通信线程睡眠等待唤醒”两种调度策略。

## 实验记录应该包含什么

至少记录：ucx_info 能看到的 transport、使用的 UCX_TLS、两端是否同机、Worker thread mode、busy/eventfd 模式，以及消息长度。若测试 CUDA/RDMA，还要记录 buffer memory type 与设备拓扑。

只有这些上下文齐全，延迟或带宽数字才可比较。单独写“UCX 多少微秒”没有意义，因为 UCX 的核心设计恰恰是让不同 hardware path 在同一 API 下被动态选择。
