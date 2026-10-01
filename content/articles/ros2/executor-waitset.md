# Executor 与 WaitSet：数据 ready 以后，为什么 callback 还不一定立刻执行

本篇固定 `rclcpp@cfdb3b7dcea4a503c0acaa304d033636beeb1dba` 与 `rcl@cbaee7c905e3276bb7a629eb1e18d8d3c781f194`。这里研究的是 ROS2 执行平面，而不是 DDS 网络线程。

## 先拆掉一个常见直觉

subscriber 收到数据以后，用户常把过程想成：

~~~text
network packet arrives
      |
      v
callback()
~~~

ROS2 更接近：

~~~text
middleware says entity ready
      |
      v
rcl_wait returns
      |
      v
Executor chooses work
      |
      v
take
      |
      v
callback executes
~~~

中间多出的 WaitSet 与 Executor，正是 ROS2 能把 subscription、timer、service、client、guard condition 统一等待的代价。

## WaitSet 为什么存在

如果每个 subscription 都创建一个独立线程：

~~~text
subscription A -> thread A
subscription B -> thread B
timer C        -> thread C
service D      -> thread D
~~~

对象一多，线程数量、context switch、优先级和 shutdown 都很难控制。

WaitSet 采用另一种模型：

~~~text
subscriptions \
timers         \
services        > one wait set -> one blocking wait
clients        /
events        /
~~~

内核或 middleware 唤醒以后，Executor 再决定执行哪个实体。

## Executor 每轮等待前会重建可等待集合

固定源码中的 `Executor::wait_for_work` 先收集当前 callback groups：

~~~cpp
memory_strategy_->clear_handles();

bool has_invalid_weak_groups_or_nodes =
  memory_strategy_->collect_entities(weak_groups_to_nodes_);
~~~

随后清空并调整 wait set：

~~~cpp
rcl_wait_set_clear(&wait_set_);

rcl_wait_set_resize(
  &wait_set_,
  memory_strategy_->number_of_ready_subscriptions(),
  memory_strategy_->number_of_guard_conditions(),
  memory_strategy_->number_of_ready_timers(),
  memory_strategy_->number_of_ready_clients(),
  memory_strategy_->number_of_ready_services(),
  memory_strategy_->number_of_ready_events());

memory_strategy_->add_handles_to_wait_set(&wait_set_);
~~~

这说明 Executor 并不是永远盯着一个静态 fd 数组；节点、callback group 和实体变化会反映到等待集合。

## rcl_wait 是真正的阻塞边界

然后：

~~~cpp
rcl_ret_t status =
  rcl_wait(
    &wait_set_,
    std::chrono::duration_cast<std::chrono::nanoseconds>(
      timeout).count());
~~~

rcl 层再把 wait set 中的 RMW handles 交给 `rmw_wait`。

因此执行线程的典型状态是：

~~~text
collect entities
     |
build wait set
     |
rcl_wait()  <---- blocking here
     |
wake up
     |
select executable
~~~

这与 ROS1 `ros::spin()` 的“poll + callback queue”有相似目标，但抽象边界已经不同：ROS2 把 middleware readiness 统一纳入 RMW wait contract。

## ready 只是候选，不是 callback

`get_next_executable` 的逻辑非常直接：

~~~cpp
success = get_next_ready_executable(any_executable);

if (!success) {
  wait_for_work(timeout);

  if (!spinning.load()) {
    return false;
  }

  success = get_next_ready_executable(any_executable);
}
~~~

这段代码把两步分得很清楚：

1. 等待某些 entity ready；
2. 从 ready entity 中挑一个 executable。

所以一个 subscription 在 middleware 里 ready，不等于它马上获得 CPU。

## execute_any_executable 才进入真正业务执行

固定源码：

~~~cpp
if (any_exec.timer) {
  execute_timer(any_exec.timer);
}

if (any_exec.subscription) {
  execute_subscription(any_exec.subscription);
}

if (any_exec.service) {
  execute_service(any_exec.service);
}

if (any_exec.client) {
  execute_client(any_exec.client);
}

if (any_exec.waitable) {
  any_exec.waitable->execute(any_exec.data);
}
~~~

Executor 把不同 entity 统一成 `AnyExecutable`，然后根据实际类型进入具体执行路径。

这是一个经典的 Runtime 设计：

~~~text
heterogeneous event sources
        |
        v
common schedulable envelope
        |
        v
dispatch
~~~

## CallbackGroup 解决的是“哪些 callback 允许同时执行”

MultiThreadedExecutor 并不意味着任何 callback 都可以并发。

对于 MutuallyExclusive group，选中 executable 时会：

~~~cpp
any_executable.callback_group
  ->can_be_taken_from()
  .store(false);
~~~

执行完成后：

~~~cpp
any_exec.callback_group
  ->can_be_taken_from()
  .store(true);
~~~

可以把它理解成逻辑上的 group-level execution token：

~~~text
can_be_taken_from = true
      |
Executor acquires callback
      v
false
      |
callback running
      v
true
~~~

它不是 OS mutex 的简单别名，而是 Executor 在调度阶段就避免同组 callback 同时被选中。

## CallbackGroup 不等于“指定 CPU 线程”

CallbackGroup 描述的是哪些 callbacks 可以并发。

它本身不等于：

- CPU affinity；
- SCHED_FIFO priority；
- 固定 thread ownership。

如果需要这些实时属性，还要进一步控制 Executor worker、线程优先级、affinity 和系统调度策略。

## SingleThreadedExecutor 的核心限制

一个执行线程意味着：

~~~text
callback A 20 ms
      |
      v
callback B ready but waits
      |
      v
timer C ready but waits
~~~

假设控制 callback 周期 5 ms，而视觉 callback 最坏 20 ms：

\[
T_{blocking}=20\text{ ms} > T_{control}=5\text{ ms}
\]

那控制 callback 可以连续错过多个期望执行时刻。

网络层 QoS 再好也无法修复这一点。

## MultiThreadedExecutor 也不是自动实时

增加 worker 可以减少某些 head-of-line blocking，但会引入：

- callback 共享数据竞争；
- cache contention；
- lock contention；
- OS scheduler jitter；
- 同 callback group 的串行限制。

因此：

~~~text
more threads != deterministic scheduling
~~~

对于强实时/准实时控制，通常需要把关键 callback 与高耗时感知 callback 分开到不同 Executor/线程，甚至不同进程。

## GuardCondition 为什么重要

Executor 执行结束后会触发 interrupt guard condition：

~~~cpp
interrupt_guard_condition_.trigger();
~~~

原因是等待集合或 callback-group 可执行状态可能已经发生变化。

这是一种典型的控制面唤醒：

~~~text
work state changed
      |
      v
guard condition
      |
      v
wake rcl_wait
      |
      v
rebuild / reschedule
~~~

它与真正业务 topic 数据不是同一种 event，却可以统一进入 WaitSet。

## WaitSet 允许 spurious wakeup

Executor 的接收代码明确承认：middleware 可以因为内部原因打断 wait，但随后 take 可能拿不到消息。

因此 Runtime 不能假设：

~~~text
wait returned => data definitely exists
~~~

而应该：

~~~text
wait returned
   |
attempt take
   |
taken?
 /    \
yes    no
 |      |
run    ignore/retry
~~~

这和 condition_variable 的经典使用方式非常相似：wakeup 是“重新检查条件”的提示，不是条件本身。

## ROS1 Spinner 与 ROS2 Executor 的对照

ROS1：

~~~text
Transport/Poll
    |
SubscriptionQueue
    |
CallbackQueue
    |
Spinner
~~~

ROS2：

~~~text
RMW readiness
    |
rcl_wait_set
    |
Executor
    |
CallbackGroup
    |
take + callback
~~~

ROS2 把 middleware readiness 和多种 entity 统一得更彻底，但也让“通信”和“执行”之间的层次更多。

## 对机器人控制最重要的结论

一个传感器时间戳为 \(t_s\) 的样本，控制 callback 在 \(t_c\) 才开始处理：

\[
Age=t_c-t_s
\]

其中 Executor scheduling delay 是真实的数据年龄组成部分。

因此实时系统设计不能只测 DDS latency，还要测：

- WaitSet wakeup latency；
- Executor selection latency；
- callback blocking；
- callback group contention；
- worker scheduling jitter。

下一篇继续沿接收方向，把 ready 之后真正的 `take -> callback` 链拆开。
