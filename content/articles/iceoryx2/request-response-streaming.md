# Request/Response：为什么一次请求需要 RequestId、ChannelId、PendingResponse 与 ActiveRequest

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

RPC 表面上看很简单：

~~~text
Client
request
↓
Server
response
↓
Client
~~~

但 iceoryx2 的 Request/Response 不是“调用一个函数等一个返回值”。

它支持：

- 一个 request 发给多个 Server；
- 一个 Server 对同一 request 发多个 response；
- 多个 request 同时 active；
- Client 可以中途不再关心 response；
- Server 可以感知 graceful disconnect。

因此必须有一套显式 correlation state。

## Client 同时有 Sender 与 Receiver

固定源码中的 ClientSharedState 同时保存：

~~~text
request_sender
response_receiver
available_channel_ids
active_request_counter
~~~

所以 Request/Response 实际是两个方向的数据面：

~~~text
Client request_sender
    ↓
Server request_receiver

Server response_sender
    ↓
Client response_receiver
~~~

## 为什么 Request 需要 ChannelId

Client loan request 时，不只是拿 Chunk。

固定 API：

~~~rust
let (chunk, channel_id) =
    self.loan_chunk(1)?;

RequestMutUninit::new(
    &self.client_shared_state,
    chunk,
    channel_id,
)
~~~

ChannelId 用来给 Client response side 分配一条逻辑 response channel。

这让多个 active requests 可以共享同一套 endpoint connection，而又不会把 response 混在一起。

## RequestId 又解决什么

发送前：

~~~rust
self.prepare_channel_to_receive_responses(
    channel_id,
    request_id,
);
~~~

真正 request payload header 里还携带 RequestId。

因此 response correlation 有两级：

~~~text
ChannelId
当前 Client 内部逻辑通道

RequestId
确认 response 属于哪次 request generation
~~~

即使 channel 被复用，旧 response 也不能冒充新 request 的 response。

## Request 真正怎样发送

固定源码：

~~~rust
let number_of_recipients =
    self.request_sender.deliver_offset(
        chunk,
        ChannelId::new(0),
    )?;
~~~

所有 request data 用 request sender 的同一个底层 channel 发送。

真正区分 response path 的 channel state 在 Client response receiver 中准备。

这说明：

> Request transport channel 与 response correlation channel 不是同一个概念。

## 为什么有 Active Request 上限

发送前：

~~~rust
if max_active_requests
    <= active_request_counter
{
    return ExceedsMaxActiveRequests;
}
~~~

这不是随意限制。

每个 PendingResponse 都意味着 runtime 需要保留 response channel state、request correlation、response buffer capacity，以及 Server 侧可能仍持有的 ActiveRequest。

因此 active request count 也是资源预算。

## RequestMut::send 为什么返回 PendingResponse

固定源码：

~~~rust
match s.send_request(...) {
    Ok(number_of_server_connections) => {
        ...
        let active_request =
            PendingResponse {
                number_of_server_connections,
                request: self,
                ...
            };
        Ok(active_request)
    }
}
~~~

所以 send 完成后，请求没有“结束”。

它转化成 PendingResponse。

这个对象代表：

> Client 对这次 request 后续 response stream 的兴趣仍然存在。

## Server 收到的是 ActiveRequest

Server：

~~~rust
while let Some(active_request)
    = server.receive()?
{
    ...
}
~~~

ActiveRequest 保留：

~~~text
request_id
channel_id
connection_id
request chunk
response loan counter
~~~

因此 Server 才知道要给哪个 Client、哪一次 request、哪条 response channel 发送 response。

## 为什么一个 Request 可以得到多个 Response

ActiveRequest::send_copy 或：

~~~rust
let response =
    active_request.loan_uninit()?;

response
    .write_payload(value)
    .send()?;
~~~

可以调用多次。

官方 example 明确演示 streaming response。

所以 iceoryx2 Request/Response 更接近：

~~~text
request
→ response stream
~~~

而不是固定的一问一答。

## Response 只发给对应 Connection

固定 ResponseMut::send(self)：

~~~rust
s.response_sender
    .deliver_offset_to_connection(
        &self.chunk,
        self.channel_id,
        self.connection_id,
    )?;
~~~

request 可以 fan-out 给多个 Server。

但某个 Server 的 response 需要回到原 Client 的特定 connection/channel。

## Client 收 Response 时还要检查 RequestId

PendingResponse::receive：

~~~rust
if response.header().request_id
    != self.request.header().request_id
{
    continue;
}
~~~

这是非常典型的 stale-message 防护。

即使底层 channel 曾被复用，也必须通过 RequestId 再做 generation check。

## Drop PendingResponse 为什么是协议动作

PendingResponse Drop：

~~~rust
s.active_request_counter
    .fetch_sub(1, Ordering::Relaxed);

self.close();
~~~

close：

~~~rust
s.response_receiver.close_channel(
    self.request.channel_id,
    self.request.header().request_id,
);
~~~

所以 RAII Drop 不只是释放本地对象。

它会改变跨进程协议状态：

~~~text
Client 不再对后续 response 感兴趣
↓
channel state closed
↓
Server ActiveRequest eventually
sees disconnected
~~~

## Graceful Disconnect Hint

如果 Client 不是立刻硬关闭，而是想让 Server 再发最后一个 response：

~~~rust
pending_response
    .set_disconnect_hint();
~~~

Server：

~~~rust
active_request
    .has_disconnect_hint()
~~~

可以检测后：

~~~text
send final response
↓
drop ActiveRequest
↓
Client sees response stream complete
~~~

这比简单 close 更适合 streaming protocol。

## 官方 Example 的完整运行模型

Client：

~~~text
send request 0
↓
PendingResponse

loop:
  drain all responses
  ↓
  loan next request
  ↓
  send
  ↓
  replace PendingResponse
~~~

Server：

~~~text
receive ActiveRequest
↓
send first response
↓
possibly send additional responses
↓
drop ActiveRequest
~~~

这套对象设计把 request 生命周期直接暴露给应用。

## 对机器人系统的映射

很适合：

~~~text
规划请求
→ 多阶段候选轨迹流

地图查询
→ 多批结果

模型服务请求
→ progressive response
~~~

如果业务需要严格“一问一答”，仍可以只发送一个 response。

但底层模型已经能覆盖更一般的 stream semantics。
