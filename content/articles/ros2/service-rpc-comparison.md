# ROS1 vs ROS2 Service：从“连接顺序”到“请求 ID”，RPC 语义被怎样重新实现

本文对照 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`、`rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 与 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。重点不是比较 API，而是解释同样的 request/response 语义为什么在两代 ROS 中使用完全不同的运行时结构。

## 1. 两代 ROS 都提供 RPC，但配对 response 的方法不同

ROS1 persistent Service 更像：

~~~text
one TCP stream
  |
call A -> response A
  |
call B -> response B
  |
call C -> response C
~~~

它依赖 `ServiceServerLink::call_queue_` 与 `current_call_` 保证同一 connection 上严格按顺序推进。

ROS2 更像：

~~~text
request(seq=41)
request(seq=42)
request(seq=43)

response(seq=42)
response(seq=41)
...
~~~

上层依赖 request_id / sequence_number 将 response 映射回 pending state。

所以两者分别是：

~~~text
ROS1:
connection ordering

ROS2:
explicit correlation id
~~~

## 2. Discovery 也不同

ROS1：

~~~text
lookupService
  |
ROS Master
  |
rosrpc://host:port
  |
TCP connect
~~~

ROS2：

~~~text
DDS/RMW discovery
  |
service/client endpoint matching
~~~

ROS1 先拿一个服务器地址，再建 connection。

ROS2 则把 client/service 建成 middleware entities，由 discovery 完成 endpoint matching。

## 3. ROS1 RPC 的状态集中在 connection object

客户端关键结构：

~~~text
ServiceServerLink
  - connection_
  - call_queue_
  - current_call_
  - persistent_
~~~

调用线程等待：

~~~text
CallInfo::finished_condition_
~~~

所以“一个 RPC call 的状态”主要围绕某条 connection 管理。

连接断开通常意味着当前及排队 call 都受影响。

## 4. ROS2 RPC 的状态集中在 request identity

客户端关键结构：

~~~text
Client<T>
  |
pending_requests_: map<sequence_number, callback state>
~~~

底层 connection 是否复用、transport 如何选、DDS 是否使用共享内存，都不是 rclcpp Client 的核心语义。

Client 只需要知道：

~~~text
我发出了 request N
什么时候 response N 回来
~~~

这就是 abstraction boundary 的变化。

## 5. 服务端 callback 调度：两者最终都受执行器控制

ROS1：

~~~text
network request
  -> ServicePublication::processRequest
  -> CallbackQueue
  -> Spinner
  -> callback
~~~

ROS2：

~~~text
request ready
  -> WaitSet
  -> Executor::execute_service
  -> take request
  -> callback
~~~

所以两代系统都不是“网络线程直接执行业务 callback”。

区别在于 ready work 的表示：

~~~text
ROS1:
CallbackQueue entry

ROS2:
middleware entity readiness
~~~

## 6. 同步 API 的实现方式不同

ROS1 C++：

~~~text
async I/O
+
condition_variable wait
=
synchronous call()
~~~

ROS2 rclcpp：

~~~text
async_send_request
+
future/promise
+
Executor progress
~~~

因此 ROS2 中如果阻塞 Executor 自己等待 future，很容易产生执行层 deadlock。

ROS1 也可能因 callback queue 与同步 service call 组合导致死锁，但触发条件和结构不同。

## 7. persistent 的意义为什么在 ROS2 中淡化

ROS1 persistent Service 是显式 API 概念：

~~~text
persistent = true
~~~

因为底层确实存在一条长期复用的 TCP connection。

ROS2 上层不再暴露“是否保持这个 TCP socket”的主要语义，因为 RMW/DDS 可以自行管理 participant、endpoint、transport connection、shared memory 与 reliability state。

也就是说 connection lifetime 被推到了 middleware implementation 下面。

## 8. Error model 的差异

ROS1 常见失败：

~~~text
lookupService failed
TCP connect failed
header/md5 mismatch
connection dropped
service callback returned false
~~~

ROS2 常见失败：

~~~text
service unavailable
QoS/entity matching issue
request sent but response never arrives
pending request timeout
Executor not spinning
server callback blocked
~~~

ROS2 增加了一个很重要的本地状态问题：

> response 没来时，pending request 必须回收。

## 9. 请求顺序与并发语义

ROS1 persistent link 上的 `call_queue_` 明确将同一 connection 上的请求串行化。

ROS2 request_id 允许 correlation 与 connection ordering 解耦，因此更自然地支持多个 outstanding request。

但最终并发度还受：

- RMW implementation；
- Service callback group；
- Executor workers；
- server callback 本身线程安全；
- DDS queue/history。

控制。

所以“可以 outstanding 多个 request”不等于“server callback 一定并行”。

## 10. 对机器人系统最重要的判断

Service 适合：

- 配置；
- 查询；
- 一次性命令；
- 有明确 response 的操作。

不适合拿来替代高频状态流。

原因不是 Service 一定慢，而是它的语义天然要求：

~~~text
request identity
response completion
timeout/failure handling
~~~

这比 latest-state topic 多了一个事务生命周期。

## 11. 一张职责对照表

| 职责 | ROS1 | ROS2 |
|---|---|---|
| 服务发现 | Master lookupService | RMW/DDS discovery |
| 数据通道 | TCP Connection | RMW client/service entities |
| 请求关联 | connection + call_queue 顺序 | request_id / sequence_number |
| 服务端调度 | CallbackQueue + Spinner | WaitSet + Executor |
| 客户端等待 | condition_variable | future/promise/callback |
| 长连接 | explicit persistent | middleware implementation detail |
| timeout 后状态 | connection/call failure | pending request cleanup |

真正的架构变化可以压缩成一句话：

> **ROS1 把 RPC 绑定在“这条连接上的下一次响应”，ROS2 把 RPC 绑定在“这个 request_id 的响应”。**
