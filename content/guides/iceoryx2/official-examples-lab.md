# 官方示例实验：把 Pub/Sub、Event、Request/Response 与 Blackboard 真正跑起来

本实验统一基于 iceoryx2 v0.10.0，固定源码提交 135d09dd8b29f321f1725920d434864c4e512378。

这里不再造一套教学 Demo，而是直接使用上游仓库 examples/Cargo.toml 中登记的官方 Rust examples。

## 先确认你站在源码根目录

官方端到端测试本身就是这样启动示例：

~~~console
cargo run --example publish_subscribe_publisher
cargo run --example publish_subscribe_subscriber
~~~

因此下面所有命令都在 iceoryx2 仓库根目录执行。

如果只是先验证编译：

~~~console
cargo check --example publish_subscribe_publisher
cargo check --example publish_subscribe_subscriber
~~~

## 实验一：Publish/Subscribe

终端 A：

~~~console
cargo run --example publish_subscribe_publisher
~~~

终端 B：

~~~console
cargo run --example publish_subscribe_subscriber
~~~

观察重点不是打印内容，而是把现象映射回源码：

~~~text
Publisher
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
release/reclaim
~~~

建议在源码里同时打开：

~~~text
iceoryx2/src/port/publisher.rs
iceoryx2/src/sample_mut.rs
iceoryx2/src/port/details/sender.rs
iceoryx2/src/port/subscriber.rs
iceoryx2/src/port/details/receiver.rs
~~~

## 实验二：Event

终端 A：

~~~console
cargo run --example event_listener
~~~

终端 B：

~~~console
cargo run --example event_notifier
~~~

Notifier 每秒发一个 EventId。

Listener 使用 timed_wait，回调拿到 EventActivation：

~~~text
event.id
event.count
~~~

这里没有 payload chunk。

应该重点观察：

~~~text
Notifier
↓
Event primitive
↓
Listener wait
↓
EventActivation
~~~

它和 Pub/Sub 是不同数据模型。

## 实验三：WaitSet / Event Multiplexing

先启动多个 notifier，例如：

~~~console
cargo run --example event_multiplexing_notifier -- --service camera
cargo run --example event_multiplexing_notifier -- --service lidar
~~~

再启动等待端：

~~~console
cargo run --example event_multiplexing_wait -- --services camera --services lidar
~~~

官方 wait.rs 做了一个非常重要的动作：

~~~rust
listener.try_wait(|event| {
    ...
})?;
~~~

WaitSet 只告诉应用某个 attachment ready。

真正 pending event 仍必须被消费。

否则底层 fd 持续 ready，event loop 会变成 busy loop。

Linux 下这条链最终落到：

~~~text
WaitSet
↓
Service::Reactor
↓
recommended IPC Reactor
↓
epoll
~~~

## 实验四：Request / Response

终端 A：

~~~console
cargo run --example request_response_server
~~~

终端 B：

~~~console
cargo run --example request_response_client
~~~

Client 第一条请求故意使用 send_copy，后续改用：

~~~rust
let request = client.loan_uninit()?;
let request = request.write_payload(...);
let pending_response = request.send()?;
~~~

Server：

~~~rust
while let Some(active_request)
    = server.receive()?
{
    ...
}
~~~

它先发送一个普通 response，然后可能继续 loan 多个 response。

因此实验重点不是“RPC 能不能通”，而是观察：

~~~text
Request
↓
PendingResponse

Server receives
↓
ActiveRequest

ActiveRequest
↓
Response stream

drop ActiveRequest
↓
Client eventually sees stream end
~~~

## 实验五：Blackboard

终端 A：

~~~console
cargo run --example blackboard_creator
~~~

看到 Blackboard created. 以后，终端 B：

~~~console
cargo run --example blackboard_opener
~~~

Creator 创建：

~~~text
BlackboardKey 0 → i32
BlackboardKey 1 → f64
~~~

然后 Writer 周期更新。

Opener 缓存 EntryHandle 并直接 get 当前值。

这里没有：

~~~text
sample queue
history queue
serialization
~~~

核心链是：

~~~text
key
↓
management map
↓
offset
↓
shared-memory UnrestrictedAtomic<T>
↓
EntryHandle::get
~~~

## 实验六：用 Blackboard + Event 理解“状态与通知分离”

继续查看官方：

~~~text
examples/rust/blackboard_event_based_communication
~~~

这是特别值得机器人开发者学习的一种架构：

~~~text
Blackboard
保存当前状态

Event
只表示某个 entry 发生更新

WaitSet
负责阻塞等待
~~~

也就是：

> data plane 不搬状态，notification plane 只发送小事件。

## 建议做的故障实验

不要只验证正常路径。

### 1. Subscriber 很慢

在 Subscriber 处理中人为 sleep。

观察 queue 是否增长到上限、safe overflow、backpressure 和 Publisher 行为。

### 2. 持有 Sample 不释放

让 Subscriber 长期保存收到的 Sample。

观察：

~~~text
borrowed chunks ↑
available pool ↓
loan eventually fails / backpressure
~~~

这能直观看到 zero-copy 的真正成本是 ownership。

### 3. 强制结束进程

让某个 endpoint 非正常退出，再重新启动。

结合 NodeState::Dead 与 stale cleanup 文章理解：

~~~text
normal:
RAII cleanup

abnormal:
another process detects dead node
→ stale resource cleanup
~~~

这是两条不同生命周期。

### 4. Request Client 提前结束 PendingResponse

让 Client 对 response stream 提前失去兴趣。

观察 Server 的 ActiveRequest::is_connected / disconnect hint 语义。

## 编译通过不等于实时性通过

即使示例完全运行正确，也只能证明 API、资源建立、数据路径和生命周期基本闭环。

它不能证明：

- 你的线程调度有界；
- NUMA 合理；
- cache miss 可接受；
- memory pool 容量合理；
- deadline 不会被业务代码破坏；
- WCET 满足控制周期。

真正用于机器人部署时，还要继续测：

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

## 建议实验顺序

~~~text
publish_subscribe
↓
event
↓
event_multiplexing
↓
request_response
↓
blackboard
↓
blackboard_event_based_communication
~~~

这条顺序正好对应专题源码阅读路径：

~~~text
共享 payload
↓
轻量 notification
↓
事件复用
↓
双向关联状态机
↓
共享 latest-state
↓
状态 + 通知组合
~~~

跑完这几组以后，iceoryx2 就不再只是“一个支持 zero-copy 的库”，而是一套由多种通信语义拼成的本机 runtime building blocks。
