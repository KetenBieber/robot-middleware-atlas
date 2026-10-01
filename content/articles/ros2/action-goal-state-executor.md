# ROS2 Action Goal 状态机：ACCEPTED、EXECUTING、CANCELING 与 Executor 如何共同推进长期任务

本文固定到 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 与 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。如果把 Action 只理解成“Service + feedback”，最容易漏掉的就是它真正的核心：**Goal 状态机与长期对象生命周期**。

源码中的三个关键非终态枚举分别是 `GOAL_STATE_ACCEPTED`、`GOAL_STATE_EXECUTING` 与 `GOAL_STATE_CANCELING`；下面的简写图与这三个真实枚举一一对应。


## 1. Goal 不是一条消息，而是一个有寿命的实体

一个 goal 被接受后，server 必须持续记住它：

~~~text
GoalUUID
  |
GoalHandle
  |
state
  |
result / cancel / expiration
~~~

因此 rcl_action 为 goal 定义了显式状态机，而不是依靠应用自己随意组合 bool。

## 2. 主要状态

可以先压缩成：

~~~text
ACCEPTED
   |
   | EXECUTE
   v
EXECUTING
   |
   +---- SUCCEED ----> SUCCEEDED
   |
   +---- ABORT ------> ABORTED
   |
   +---- CANCEL_GOAL -> CANCELING
                         |
                         +---- CANCELED -> CANCELED
~~~

另外 ACCEPTED 也可以直接进入 CANCELING。

terminal states：

~~~text
SUCCEEDED
ABORTED
CANCELED
~~~

这些状态决定 goal 是否还需要执行、是否可以取消、是否应该保留 result。

## 3. 状态转移不是 if-else 拼出来的

`goal_state_machine.c` 直接定义二维 transition table：

~~~text
_goal_state_transition_map[state][event]
~~~

例如：

~~~text
ACCEPTED + EXECUTE       -> EXECUTING
ACCEPTED + CANCEL_GOAL   -> CANCELING
EXECUTING + SUCCEED      -> SUCCEEDED
EXECUTING + ABORT        -> ABORTED
EXECUTING + CANCEL_GOAL  -> CANCELING
CANCELING + CANCELED     -> CANCELED
~~~

`rcl_action_transition_goal_state()` 通过二维表索引 state/event；未登记处理函数的组合映射为 `GOAL_STATE_UNKNOWN`。

这种写法比散落在 callback 中的条件判断更重要，因为它强制把合法转移显式化。

## 4. cancel 为什么不是“把 state 设成 CANCELED”

Cancel 是两阶段的：

~~~text
EXECUTING
   |
cancel requested
   v
CANCELING
   |
application completes cancellation work
   v
CANCELED
~~~

中间的 CANCELING 很关键。

因为机器人不能在收到 cancel request 的同一瞬间假装执行器已经物理停止。

对于机械臂或底盘：

~~~text
cancel request
  |
stop planner
  |
decelerate
  |
wait actuator safe state
  |
mark CANCELED
~~~

这正是状态机语义比一个 bool 更可靠的原因。

## 5. succeed、abort、canceled 是 server-side 事件

这些 terminal transition 并不是 client 直接决定。

client 可以：

~~~text
send goal
request cancel
request result
~~~

但：

~~~text
SUCCEED
ABORT
CANCELED
~~~

由 server 端 GoalHandle 生命周期推进。

因此 Action 把“请求控制权”和“执行结果决定权”分开。

## 6. ServerBase 为什么保存 goal_handles_

`rclcpp_action::ServerBaseImpl` 有：

~~~text
goal_handles_
goal_results_
result_requests_
~~~

其中 `goal_handles_` 保证 rcl goal handle 在需要发送 result 或处理 cancel 时仍然有效。

`goal_results_` 保存 terminal result。

`result_requests_` 保存“客户端已经问 result，但结果还没出来”的 request header。

这三张表分别对应：

~~~text
execution lifetime
result lifetime
pending RPC lifetime
~~~

它们不是重复状态。

## 7. get_result 为什么可能被延迟回复

客户端可以在 goal 尚未完成时就请求 result。

server 此时不能返回伪结果，于是：

~~~text
get_result request
   |
result not ready
   |
store request_id in result_requests_[GoalUUID]
   |
goal reaches terminal state
   |
store result in goal_results_
   |
reply all pending result requests
~~~

这是一种 deferred RPC。

所以 Action Server 本质上把一个短生命周期 Service request 绑定到了更长生命周期的 Goal object 上。

## 8. Action 如何进入 Executor

`rclcpp_action::ServerBase` 与 `ClientBase` 都是 Waitable。

它们把内部多个 rcl entities 加进同一个 wait set。

Server ready 事件包括：

~~~text
GoalService
CancelService
ResultService
Expired
~~~

Client ready 事件包括：

~~~text
FeedbackSubscription
StatusSubscription
GoalResponse
CancelResponse
ResultResponse
~~~

`is_ready()` 先判断哪个内部 entity ready，再把其类型存入 `next_ready_event`。

随后：

~~~text
take_data()
  |
take_data_by_entity_id()
  |
execute(...)
~~~

这说明 Action 在 rclcpp 层使用的是“复合 Waitable + 单事件逐次推进”模型。

## 9. 为什么 next_ready_event 是必要的

Executor 的 Waitable 接口要求一个统一对象：

~~~text
is_ready()
take_data()
execute()
~~~

但 Action 内部有多个实体。

所以 Action Client/Server 必须自己完成一次 multiplex：

~~~text
many internal ready flags
      |
choose one
      |
next_ready_event
      |
take correct entity
~~~

这是一种典型 adapter pattern：

> 把多个底层 readiness source 折叠成一个 Executor 可调度对象。

## 10. feedback 与 result 为什么生命周期不同

feedback 是 transient stream：

~~~text
goal executing
  -> feedback
  -> feedback
  -> feedback
~~~

result 则是 terminal artifact：

~~~text
goal terminal
  -> result retained
  -> client may fetch later
~~~

因此 server 需要 result retention/expiration，却不需要永久保存 feedback。

这也是 Action 里 timer/expiration 逻辑存在的原因。

## 11. Goal expiration 解决什么

如果 result 永远保留：

~~~text
goal_results_
goal_handles_
~~~

会无限增长。

所以 terminal goal 需要在保留一段时间后过期。

Action Server 将 expiration 也作为 Waitable 事件：

~~~text
GoalExpired
~~~

到期后清理 goal handle、retained result 与 associated state。

这是长期协议必须具备的 reclamation 机制。

## 12. 一个机器人动作为什么应该用 Action 而不是 Service

例如“移动机械臂到抓取位姿”：

Service 很难自然表达：

~~~text
accepted?
current progress?
cancel?
aborted?
final result?
~~~

Action 则直接提供：

~~~text
GoalUUID
state machine
feedback stream
cancel protocol
result retention
~~~

所以它适合的是 **长事务、可取消、需要中间反馈的操作**。

Action 的本质不是“功能更多的 RPC”，而是：

> **在 Executor/RMW 上实现的长期分布式状态机。**
