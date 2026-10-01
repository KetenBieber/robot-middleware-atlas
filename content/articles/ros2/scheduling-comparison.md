# ROS1 Spinner vs ROS2 Executor：ready 以后，谁决定 callback 何时运行

本文固定 ROS2 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba`，并对照 ROS1 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。核心问题只有一个：**数据已经 ready 以后，哪个线程、按什么规则、在什么时候执行用户 callback？**

Supporting source: `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`，用于核对 `rcl_wait` 与 Executor wait set 的边界。


## 1. 通信与执行必须分开

~~~text
I/O makes work ready
        |
runtime records readiness/work
        |
execution policy picks it
        |
user callback
~~~

ROS1 和 ROS2 的重要差异之一，是 ready work 如何表示。

## 2. ROS1：CallbackQueue 把工作实体显式排队

`CallbackQueue::addCallback` 把 `CallbackInfo` 放进容器并通知 condition variable。

Spinner 随后调用：

~~~cpp
queue->callAvailable(timeout);
~~~

或：

~~~cpp
queue->callOne(timeout);
~~~

因此执行面是：

~~~text
SubscriptionQueue
      |
CallbackQueue
      |
Spinner thread(s)
      |
callback
~~~

`AsyncSpinner` 多线程模式并不代表同一 subscription 必然并发。SubscriptionQueue 还可以通过自身 callback mutex 保留串行语义。

## 3. ROS2：WaitSet 保存 ready 条件

ROS2 Executor 的关键函数：

~~~text
Executor::wait_for_work()
Executor::get_next_ready_executable()
Executor::execute_any_executable()
~~~

`wait_for_work` 等待 rcl wait set。wait set 可以同时包含 subscription、timer、client、service、guard condition 与 middleware event。

ready 后，Executor 才选择 `AnyExecutable`。

~~~text
middleware/entity readiness
        |
      WaitSet
        |
  AnyExecutable selection
        |
     Executor
        |
     callback
~~~

这不是 ROS1 CallbackQueue 的简单改名。

## 4. ready 不等于消息已经搬进 Executor

subscription ready 后，Executor 才进入 take：

~~~text
execute_subscription
   |
take_type_erased
   |
rcl_take
   |
rmw_take_with_info
~~~

因此样本可能仍在 DDS Reader History 中。

这与 ROS1 常见的“SubscriptionQueue 已经有待执行 delivery work”不同。分析 backlog 时必须知道当前系统是数据积压还是 callback work 积压。

## 5. SingleThreadedExecutor 的确定性问题

单线程执行器本质：

~~~text
while (ok) {
  get_next_executable()
  execute_any_executable()
}
~~~

如果一个 vision callback 执行 40 ms，同一线程上的 timer、subscription、service 都可能被阻塞。

这与 ROS1 SingleThreadedSpinner 的本质问题相同：长 callback 会占住唯一 worker。

## 6. MultiThreadedExecutor 也不是线程越多越实时

多线程 Executor 增加 worker，但还要经过 CallbackGroup。

常见语义：

~~~text
MutuallyExclusive
Reentrant
~~~

若两个关键 callback 在同一个 MutuallyExclusive group，即使有多个 worker，也不会并发执行。

此外还受应用 mutex、allocator contention、cache contention、OS scheduler、thread priority 与 CPU affinity 影响。

真正并发度是：

\[
Concurrency=f(ExecutorWorkers,CallbackGroup,Locks,OS).
\]

## 7. 调度抽象对照

| 问题 | ROS1 | ROS2 |
|---|---|---|
| ready work 表示 | CallbackQueue item | WaitSet entity readiness |
| worker | Spinner | Executor |
| 多线程 | AsyncSpinner / MultiThreadedSpinner | MultiThreadedExecutor |
| 同组互斥 | SubscriptionQueue / 应用锁 | CallbackGroup |
| take 时机 | delivery work 已入 queue | execute 时显式 take |
| 非 subscription 事件统一 | 较弱 | timer/service/client/guard 共用 WaitSet |

ROS2 的优势是执行资源被更明确抽象，代价是调度链更复杂。

## 8. 对实时控制更可控的组织

不要把所有工作默认塞进一个全局执行器：

~~~text
high-priority control callback
      |
dedicated callback group / executor / thread

vision / logging / diagnostics
      |
separate executor or process
~~~

同时固定 worker 数、CPU affinity、thread priority、callback WCET 与 history/queue depth。

## 9. 1 kHz 控制回路的判断

若控制 timer 周期 1 ms，而同一 SingleThreadedExecutor 上存在 8 ms 图像 callback，那么无论 DDS transport 多快，1 kHz 都不可能稳定。

问题发生在 Executor scheduling，而不是 DDS latency。

因此中间件性能测试不能只测 ping-pong latency：对机器人控制，**执行调度和通信传输是两个不同的实时性问题**。
