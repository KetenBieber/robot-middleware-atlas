# DEALER / ROUTER：公平队列、负载均衡与显式路由怎样组合

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

DEALER 和 ROUTER 很适合用来观察：socket pattern 的语义并不是藏在 transport 里，而是由 Pipe + scheduler + routing metadata 组合出来。

## DEALER 几乎就是 FQ + LB

`dealer_t::xattach_pipe()` 同时执行：

~~~cpp
_fq.attach (pipe_);
_lb.attach (pipe_);
~~~

收消息走 `_fq.recvpipe()`，发消息走 `_lb.sendpipe()`。因此 DEALER 的核心语义可以压缩成：

~~~text
inbound  = fair queue
outbound = load balance
~~~

Pipe 的 HWM 决定某条连接是否 writable，LB 只在 active pipes 中轮询。

## ROUTER 为什么不能直接用 lb_t

ROUTER 的发送目标不是“任意一个可写 peer”，而是 routing id 指定的 peer。因此它维护 routing-id → out pipe 的查找关系，并用 `_current_out` 锁定当前 multipart 的目标 pipe。

`xsend()` 的第一帧被解释为 routing id：

~~~cpp
out_pipe_t *out_pipe = lookup_out_pipe (blob_t (...));
_current_out = out_pipe->pipe;
~~~

后续 frames 都写到同一个 `_current_out`，直到 `more` 结束才 flush 并清空 current。

## ROUTER 接收为什么要预取

真实 pipe 中读到的是 payload frame，但 ROUTER API 需要先把 routing id 交给用户。于是 `xrecv()` 先从 `_fq.recvpipe()` 取 payload，把 payload move 到 `_prefetched_msg`，再人为构造一个 routing-id frame 返回。下一次 recv 才把真正 payload 返回。

~~~text
pipe payload
   |
prefetch
   +--> return routing-id frame first
   +--> next recv returns payload
~~~

这说明 API 可见的 message envelope 不一定等同于底层 transport 原始 frame；socket pattern 可以在 Runtime 层重写消息视图。

## mandatory 模式怎样影响错误语义

如果 routing id 不存在或目标 pipe 不可写，ROUTER 默认可以静默丢弃；启用 mandatory 后会返回 `EHOSTUNREACH` 或 `EAGAIN`。

因此“可靠性/错误暴露”并不是只由 TCP 决定，socket pattern 自己也定义应用可观察语义。

## 对机器人调度的启发

如果是多执行器命令分发：

~~~text
DEALER-like
    适合任意一个 worker 都能处理的任务池

ROUTER-like
    适合必须送往指定设备/会话/控制器的消息
~~~

二者底层都可以复用同一 Pipe Runtime，区别只是选择哪个 pipe 的策略。
