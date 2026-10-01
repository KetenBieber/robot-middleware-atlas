# ROS2 Service Runtime：Client/Service、RMW request_id 与 Executor 如何拼成 RPC

本文固定到 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 与 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。ROS2 Service 表面上仍然是 request/response，但底层已经从 ROS1 的“单 TCP connection + 同步 call queue”变成了 **RMW client/service entity + request_id correlation + WaitSet/Executor**。

## 1. ROS2 Service 不是一条 TCP connection

rcl 初始化 Service 时：

~~~text
rcl_service_init
  |
rmw_create_service
~~~

Client 则是：

~~~text
rcl_client_init
  |
rmw_create_client
~~~

所以 rclcpp 并不假设底层必须是 TCP、UDP 或某个固定 socket。

它依赖 RMW contract：

~~~text
rmw_send_request
rmw_take_request
rmw_send_response
rmw_take_response
~~~

这使 Service 语义与 transport implementation 解耦。

## 2. 一个 Service 实际上天然是双向数据流

从 RMW 视角看：

~~~text
Client request publisher
        |
        v
Server request subscription

Server response publisher
        |
        v
Client response subscription
~~~

因此 rcl 在初始化 Service/Client 时，会保存两套 actual QoS。

Server：

~~~text
actual_request_subscription_qos
actual_response_publisher_qos
~~~

Client：

~~~text
actual_request_publisher_qos
actual_response_subscription_qos
~~~

这比“RPC socket”更接近 DDS 的实体模型。

## 3. 请求如何获得 correlation id

`rcl_send_request()` 调用：

~~~cpp
rmw_send_request(
  client->impl->rmw_handle,
  ros_request,
  sequence_number);
~~~

返回的 `sequence_number` 被 rclcpp Client 用作 pending request 的 key。

因此 request/response 关系不是靠“同一个 TCP connection 当前只有一个请求”来保证，而是靠：

~~~text
request_id / sequence_number
~~~

进行 correlation。

这是 ROS2 Service 与 ROS1 persistent Service 最根本的结构差异之一。

## 4. rclcpp Client 维护 pending_requests_

`Client::async_send_request_impl()` 大致做两件事：

~~~text
lock pending_requests_mutex_
  |
rcl_send_request(...)
  |
pending_requests_[sequence_number] = callback/promise state
~~~

pending value 可能是：

- Promise；
- callback + SharedFuture；
- callback + original request + future。

response 到达时：

~~~text
request_header.sequence_number
        |
get_and_erase_pending_request()
        |
Promise::set_value()
or callback(...)
~~~

所以 ROS2 Client 本身就是一个 correlation table。

## 5. 为什么 timeout 后必须清理 pending request

如果 server 永远不回 response，那么这条：

~~~text
sequence_number -> Promise/callback state
~~~

不会自然消失。

因此 rclcpp 明确提供：

~~~text
remove_pending_request()
prune_pending_requests()
~~~

这不是 API 细节，而是生命周期问题：

> request 的网络生命周期可能已经结束，但 Client 仍然持有“等待 response 的本地状态”。

RPC timeout 必须同时处理 transport timeout 与 local pending-state reclamation。

## 6. Service server 如何进入 Executor

ROS2 Service 是 WaitSet entity。

Executor 发现 service ready 后：

~~~text
Executor::execute_service()
    |
service->create_request_header()
service->create_request()
    |
service->take_type_erased_request()
    |
rcl_take_request
    |
rmw_take_request
~~~

take 成功之后才进入：

~~~text
service->handle_request(...)
~~~

因此与 subscription 一样：

> ready 只是“middleware 有工作可取”，不是“用户 callback 已经排好队”。

## 7. handle_request 为什么需要 request_header

server response 必须回给正确的 request。

rclcpp Service callback 不只持有 request object，还保留：

~~~text
rmw_request_id_t
~~~

reply 时进入：

~~~text
Service::send_response
  |
rcl_send_response
  |
rmw_send_response
~~~

底层 RMW 利用 request header 把 response 关联回发起方。

所以 request header 是 RPC correlation 的协议元数据，不是普通业务 payload。

## 8. Client response 也由 Executor 驱动

Client response ready 后：

~~~text
Executor::execute_client()
    |
client->create_request_header()
client->create_response()
    |
take_type_erased_response()
    |
rcl_take_response
    |
rmw_take_response
    |
handle_response()
~~~

`handle_response()` 再用：

~~~text
request_header.sequence_number
~~~

查 pending request，并 fulfill promise 或调 callback。

完整闭环是：

~~~text
application async_send_request
        |
rcl_send_request
        |
RMW
        |
server WaitSet ready
        |
Executor execute_service
        |
service callback
        |
rcl_send_response
        |
RMW
        |
client WaitSet ready
        |
Executor execute_client
        |
pending_requests_[seq]
        |
future/callback complete
~~~

## 9. 为什么 ROS2 C++ 倾向 async API

若在同一个 SingleThreadedExecutor callback 中做同步等待：

~~~text
callback A
  |
wait for service future
  |
executor thread blocked
~~~

但 response 的处理又需要同一个 Executor 去执行 `execute_client()`。

于是出现经典自锁：

~~~text
future 等 executor
executor 被 future 等待占住
~~~

所以 ROS2 service 的 execution model 天然更适合 async request、future/callback，或者明确使用独立执行线程。

这不是 API 风格偏好，而是 WaitSet/Executor 调度模型带来的约束。

## 10. QoS 为什么对 Service 也重要

Service 使用 request/response 两个方向的 RMW entities，因此同样存在 reliability、durability、history 与 depth。

rcl 甚至显式警告：如果 Service server 使用 transient local durability，可能收到已经退出 client 的旧 request。

这说明 RPC 语义与 middleware history 并不是完全独立的。

## 11. ROS2 Service 的对象图

~~~text
Client<T>
  |
pending_requests_: map<seq, state>
  |
rcl_client_t
  |
rmw_client_t
  |
request publisher --------+
                           |
                           v
                    request subscription
                       rmw_service_t
                           |
                        Service<T>
                           |
                        Executor
                           |
                      user callback
                           |
                    response publisher
                           |
                           v
                  response subscription
                           |
                      rmw_client_t
                           |
                sequence_number lookup
                           |
                    future / callback
~~~

ROS2 Service 的核心设计可以概括为：

> **RPC 不再依赖一条连接上的严格请求顺序，而是依赖 RMW request_id 做关联；Executor 负责在 request/response readiness 与用户 callback 之间推进状态。**

因此分析 ROS2 RPC 时，最关键的三个对象是：**request_id、pending_requests_、WaitSet/Executor**。
