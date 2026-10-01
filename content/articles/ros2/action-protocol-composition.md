# ROS2 Action 不是一种新 Transport：它是 3 个 Service + 2 个 Topic 组成的长期任务协议

本文固定到 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 与 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。Action 经常被当成 ROS 的第三种通信原语，但从源码看，它更准确地说是一套 **由现有 Service 与 Topic 组合出的长期任务协议**。

## 1. 为什么普通 Service 不够

一个运动目标可能经历：

~~~text
send goal
  |
accepted
  |
running
  |---- feedback ---->
  |
cancel?
  |
succeeded / aborted / canceled
  |
result
~~~

如果只用一个 Service：

~~~text
call()
  |
block 20 seconds
  |
response
~~~

就很难同时表达：

- goal 是否被接受；
- 中途 feedback；
- cancel；
- 最终 terminal state；
- result 延迟获取。

所以 Action 必须把“开始任务”和“任务生命周期”拆开。

## 2. Action 的 wire-level 组成

rcl_action 为一个 action name 派生出多个名字：

~~~text
<action>/_action/send_goal
<action>/_action/cancel_goal
<action>/_action/get_result
<action>/_action/feedback
<action>/_action/status
~~~

一个 Action Server 实际创建：

~~~text
3 Services:
  send_goal
  cancel_goal
  get_result

2 Publishers:
  feedback
  status
~~~

Action Client 则创建对应的：

~~~text
3 Clients:
  send_goal
  cancel_goal
  get_result

2 Subscriptions:
  feedback
  status
~~~

Action 没有发明第六种 transport；它复用了 Service + Topic。

## 3. 为什么 send_goal 是 Service

send_goal 需要明确回答：

~~~text
accepted?
timestamp?
~~~

这是典型 request/response。

客户端不能仅仅发布一个 goal topic，因为它需要知道 server 是否接受这个 goal。

## 4. 为什么 feedback 是 Topic

feedback 是流式的：

~~~text
feedback 1
feedback 2
feedback 3
...
~~~

它不需要每条都对应一个独立 response transaction。

因此 feedback 更符合 Topic 语义。

## 5. 为什么 status 也是 Topic

status 表达 server 对 goal 集合的当前观察：

~~~text
goal A: executing
goal B: succeeded
goal C: canceling
~~~

这是状态广播，而不是某个单独 RPC 的 response。

因此它也自然落到 publisher/subscription。

## 6. 为什么 get_result 仍然是 Service

result 有明确的请求者与返回值：

~~~text
goal id
  |
get_result request
  |
result response
~~~

更重要的是，result 可能在请求时还没有准备好。

所以 server 必须记录：

~~~text
goal_results_
result_requests_
~~~

`rclcpp_action::ServerBaseImpl` 中可以直接看到：

~~~text
unordered_map<GoalUUID, shared_ptr<void>> goal_results_
unordered_map<GoalUUID, vector<rmw_request_id_t>> result_requests_
unordered_map<GoalUUID, shared_ptr<rcl_action_goal_handle_t>> goal_handles_
~~~

这说明 Action Server 不是一个无状态 RPC endpoint，而是一个带长期 goal state 的协议实体。

## 7. Client 为什么需要三个 pending map

`rclcpp_action::ClientBaseImpl` 保存：

~~~text
pending_goal_responses
pending_result_responses
pending_cancel_responses
~~~

三类请求分别有自己的 sequence number 与 callback。

因此 Action Client 的内部状态远比普通 Service Client 丰富：

~~~text
goal request transaction
cancel transaction
result transaction
feedback stream
status stream
~~~

这些最终都要按 GoalUUID 汇合到同一个 goal handle。

## 8. GoalUUID 是 Action 的真正 identity

Service 的核心 identity 是 request sequence number。

Action 的核心 identity 是：

~~~text
GoalUUID
~~~

因为一个 goal 的生命周期会跨越多个独立的 Service transaction 和 Topic message：

~~~text
send_goal request
feedback #1
feedback #2
cancel request
status update
get_result request
result response
~~~

这些都必须指向同一个 GoalUUID。

所以 Action 的本质不是“大号 Service”，而是：

> **以 GoalUUID 为主键，跨多个通信原语维护一个长期事务状态。**

## 9. WaitSet 里一个 Action 为什么会占多个 entity

rcl_action client/server 都能报告：

~~~text
num_subscriptions
num_clients
num_services
num_timers
num_guard_conditions
~~~

因为 Action 本来就是复合实体。

`rclcpp_action::ClientBase::add_to_wait_set()` 调：

~~~text
rcl_action_wait_set_add_action_client(...)
~~~

Server 同理。

对 Executor 来说，一个 Action Client/Server 是一个 Waitable，但内部展开成多个 rcl entities。

这是典型的 composite abstraction：

~~~text
rclcpp_action Waitable
       |
       +-- service/client entities
       +-- subscriptions
       +-- timer/status machinery
~~~

## 10. 为什么 Action 比 Service 更像一个协议状态机

普通 Service：

~~~text
request -> response
~~~

Action：

~~~text
goal identity
+
accept/reject
+
execution state
+
feedback stream
+
cancel protocol
+
result retention
+
expiration
~~~

因此 Action 的复杂度主要来自生命周期，而不是 transport。

理解 Action 时，最重要的问题不再是“消息怎么发”，而是：

> Goal 在哪个状态？谁拥有它？哪些通信事件允许把它转到下一个状态？
