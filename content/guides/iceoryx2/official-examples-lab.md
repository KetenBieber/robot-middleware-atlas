# 官方示例实验：从 Pub/Sub 到 Blackboard 的真实运行路径

本实验统一基于 iceoryx2 v0.10.0，固定源码提交 135d09dd8b29f321f1725920d434864c4e512378。

所有案例直接使用上游仓库 examples/Cargo.toml 注册的官方 Rust examples，不额外构造教学 Demo。

## 运行位置

命令均在 iceoryx2 仓库根目录执行。

仅检查示例能否编译时，可使用：

~~~console
cargo check --example publish_subscribe_publisher
cargo check --example publish_subscribe_subscriber
~~~

实际运行时使用两个终端分别启动通信双方。

## 实验一：Publish / Subscribe

终端 A：

~~~console
cargo run --example publish_subscribe_publisher
~~~

终端 B：

~~~console
cargo run --example publish_subscribe_subscriber
~~~

这组示例对应的核心数据链是：

~~~text
Publisher
↓
loan shared Chunk
↓
SampleMut::send
↓
PointerOffset
↓
ZeroCopyConnection
↓
Subscriber::receive
↓
DataSegmentView
↓
Sample
↓
release / reclaim
~~~

对应源码文件：

~~~text
iceoryx2/src/port/publisher.rs
iceoryx2/src/sample_mut.rs
iceoryx2/src/port/details/sender.rs
iceoryx2/src/port/subscriber.rs
iceoryx2/src/port/details/receiver.rs
~~~

观察重点不是终端打印内容，而是确认 payload 没有进入连接队列；连接中传递的是共享 chunk 的 descriptor。

## 实验二：Event

终端 A：

~~~console
cargo run --example event_listener
~~~

终端 B：

~~~console
cargo run --example event_notifier
~~~

Event 路径不携带共享 payload chunk。

其运行模型为：

~~~text
Notifier
↓
Event primitive
↓
Listener
↓
EventActivation
  ├─ id
  └─ count
~~~

同一个 EventId 可以被连续触发多次，Listener 得到的是事件激活信息，而不是一条业务 payload。

这适合表达：

~~~text
frame ready
state changed
deadline reached
shutdown requested
~~~

## 实验三：WaitSet / Event Multiplexing

官方 example 名称：

~~~text
event_multiplexing_notifier
event_multiplexing_wait
~~~

启动多个 notifier 后，再启动 wait 端即可形成一个多事件源等待场景。

WaitSet 的关键语义是：

~~~text
source ready
↓
Reactor wakes
↓
WaitSet callback
↓
application consumes pending event
~~~

Linux IPC variant 下，Reactor 最终映射到 epoll。

官方 wait.rs 中会真正读取 Listener pending event。若 callback 只知道“fd ready”却不消费事件，fd 会持续保持 ready，事件循环可能立即再次被唤醒。

因此：

> readiness notification 和 event consumption 是两个步骤。

## 实验四：Request / Response

终端 A：

~~~console
cargo run --example request_response_server
~~~

终端 B：

~~~console
cargo run --example request_response_client
~~~

Client 的 request 可以直接通过 loan API 构造：

~~~rust
let request = client.loan_uninit()?;
let request = request.write_payload(...);
let pending_response = request.send()?;
~~~

Server 侧接收的是 ActiveRequest：

~~~rust
while let Some(active_request)
    = server.receive()?
{
    ...
}
~~~

这组示例对应的状态关系是：

~~~text
Client
↓ request
PendingResponse
↓
Server
↓
ActiveRequest
↓
one or more Response
↓
Client receives response stream
~~~

观察重点包括：

- ChannelId 如何区分并行 active request；
- RequestId 如何防止旧 response 被错误匹配；
- ActiveRequest 如何产生多个 response；
- PendingResponse 生命周期结束后，response channel 如何关闭；
- disconnect hint 如何让 Server 感知 Client 不再需要后续 response。

iceoryx2 的 Request/Response 因而不是单纯的一问一答 RPC，而是带显式关联状态的双向流。

## 实验五：Blackboard

终端 A：

~~~console
cargo run --example blackboard_creator
~~~

终端 B：

~~~console
cargo run --example blackboard_opener
~~~

Creator 在 Service 创建阶段定义 key/value layout，例如：

~~~text
BlackboardKey 0 → i32
BlackboardKey 1 → f64
~~~

Reader/Writer 的共享状态路径是：

~~~text
key
↓
management map
↓
entry metadata
↓
offset
↓
shared-memory UnrestrictedAtomic<T>
↓
EntryHandle
~~~

这里没有 sample history queue。

Blackboard 的语义是：

~~~text
key → current value
~~~

而不是：

~~~text
sample 1
sample 2
sample 3
~~~

因此它更接近一个跨进程 latest-state register。

## 实验六：Blackboard + Event

官方 examples：

~~~text
blackboard_event_based_creator
blackboard_event_based_opener
~~~

这组示例把共享状态与通知拆成两条路径：

~~~text
Blackboard
保存当前状态

Event
通知某个状态已更新

WaitSet
负责阻塞等待
~~~

数据本身留在 Blackboard 共享内存中，Event 只承担唤醒语义。

这类结构非常适合：

~~~text
最新机器人状态
最新标定参数
当前控制模式
共享配置
~~~

业务线程无需为了“状态变了”再复制完整状态对象。

## 实验七：Backpressure

官方 examples：

~~~text
publish_subscribe_backpressure_publisher
publish_subscribe_backpressure_subscriber
~~~

该案例用于观察慢 Subscriber 对 Publisher 的反向影响。

需要关注：

~~~text
subscriber buffer
RetryUntilDelivered
DiscardData
safe overflow
publisher latency
data age
~~~

共享内存 zero-copy 并没有消除队列满的问题，只是把队列元素从 payload 换成 descriptor。

## 故障与资源实验

正常路径只验证数据模型和基本生命周期。生产级 IPC 还需要验证资源耗尽与异常退出。

### 长时间持有 Sample

让 Subscriber 持有收到的 Sample，不立即释放。

资源变化会沿着下面的方向发展：

~~~text
borrowed chunks ↑
available pool chunks ↓
new loan pressure ↑
~~~

这直接说明 zero-copy 的成本从 memcpy 转移到了 ownership duration。

### Subscriber 处理速度低于 Publisher

人为降低 Subscriber 处理速度。

观察：

~~~text
queue occupancy
drop / retry behavior
publisher blocking
data age
~~~

如果业务只关心最新状态，保持完整 FIFO 反而可能导致稳定但持续过时的数据链。

### 非正常结束 Endpoint

在 Publisher 或 Subscriber 运行期间强制结束进程。

对应 runtime 路径：

~~~text
normal exit
→ RAII cleanup

abnormal exit
→ NodeState::Dead
→ stale resource cleanup
~~~

共享内存 IPC 必须能够从第二条路径恢复，否则一次局部进程故障就可能永久占住共享资源。

## 运行正确与实时性是两个问题

官方 examples 能验证：

- Service 建立；
- endpoint 连接；
- payload/event/request/state 交付；
- ownership 生命周期；
- backpressure 与异常清理机制。

它们不能直接证明具体机器人系统满足实时约束。

部署评估仍需要测量：

~~~text
p50 / p99 / p99.99 latency
data age
CPU utilization
context switches
page faults
pool occupancy
borrow duration
scheduler latency
~~~

## 六类通信语义的关系

~~~text
Publish / Subscribe
共享 payload stream

Event
轻量 notification

WaitSet
多个 waitable source 的事件复用

Request / Response
带关联状态的双向通信

Blackboard
共享 latest-state

Blackboard + Event
状态存储与通知分离
~~~

这些官方案例共同展示了 iceoryx2 不只是一个共享内存 Pub/Sub 库，而是一组围绕同机低延迟通信组织起来的 runtime building blocks。
