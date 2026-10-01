# Subscription Queue 与 WaitSet：Zenoh 到包以后为什么还不能直接执行 ROS 回调

本文固定源码为 `rmw_zenoh@3b5b9bf424443f9800dd148b5f1cc2053bbc37fe`。这一篇只研究一个关键边界：Zenoh 已经收到消息以后，rmw_zenoh 怎样把“网络事件”变成“ROS Executor 可调度事件”。

## 1. 两个执行上下文必须分开

固定实现至少存在两个不同的执行上下文：

~~~text
Zenoh receive context
        |
        v
SubscriptionData::add_new_message
        |
        v
queue + readiness

ROS Executor thread
        |
        v
rmw_wait returns
        |
        v
rmw_take
        |
        v
user callback
~~~

如果把两者合并，transport thread 就会承担任意用户代码 WCET。

## 2. add_new_message 做了什么

`SubscriptionData::add_new_message` 在 `mutex_` 下依次检查 shutdown、读取 adapted QoS、根据 KEEP_LAST/depth 处理满队列、把拥有型 Message 放进 `message_queue_`，然后触发 data notification；若已附加 WaitSet，则设置 predicate 并 `notify_one`。

这里真正重要的是：消息 readiness 被记录为本地状态，而不是只依赖一次瞬时 notify。

## 3. 为什么“只 notify 不保存 predicate”会丢唤醒

经典竞态：

~~~text
Executor: check queue empty
                     |
                     | message arrives here
                     v
Zenoh callback: notify
                     |
Executor: start waiting
~~~

如果没有共享 predicate，notify 发生在 wait 之前，这次唤醒会永久丢失。

所以 rmw_zenoh 的 `rmw_wait_set_data_t` 维护：

~~~text
condition_variable
condition_mutex
triggered bool
~~~

生产侧先设置 `triggered = true`，再 notify；等待侧使用 predicate wait。

## 4. queue_has_data_and_attach_condition_if_not 为什么必须和 queue 共锁

Subscription 需要一个原子语义：

> 如果当前没有数据，就把这个 WaitSet 注册成未来的 waiter。

不能写成两个无锁步骤，否则消息可能在“检查空队列”和“注册 waiter”之间到达。

所以 `queue_has_data_and_attach_condition_if_not` 在同一 subscription mutex 下检查 queue，并写入 `wait_set_data_`。这是 check-and-register 原子区。

## 5. WaitSet 为什么不是消息队列

WaitSet 只回答：

~~~text
哪些实体现在 ready?
~~~

它不拥有 sample。真实消息仍在 `SubscriptionData::message_queue_`。

因此 Executor 被唤醒后仍需要调用 `rmw_take`。这与 DDS backend 的 Reader History -> WaitSet -> take 模型在语义上是一致的，只是底层 history 容器不同。

## 6. condition_variable 的真实语义

`notify_one()` 不会直接执行 ROS callback，也不保证 waiter 立刻获得 CPU。

真实链路是：

~~~text
producer sets predicate
   |
notify_one
   |
waiting thread becomes runnable
   |
OS scheduler selects it
   |
reacquire condition mutex
   |
rmw_wait returns
   |
Executor continues
~~~

所以 wakeup latency 仍受 OS scheduler、CPU affinity 与线程优先级影响。

## 7. data callback notification 也不是用户 callback

RMW 的 on-new-data callback 是“实体有新数据”的通知协议，不是 rclcpp Subscription 用户 callback。

~~~text
RMW notification
    |
marks upper runtime ready

rclcpp user callback
    |
executes application logic
~~~

如果在前者里做重业务，就会把 transport readiness path 变成 application execution path。

## 8. WaitSet detach 为什么也需要同步

Executor 每轮 wait 完成后，旧 WaitSet 不能继续被 producer 访问。

因此 entity 需要在自己的 mutex 下 detach condition pointer，否则可能出现：

~~~text
WaitSet object destroyed
       |
producer still holds stale pointer
       |
notify use-after-free
~~~

这类问题本质上是 waiter registration lifetime。

## 9. shutdown 与 wait 的关系

entity shutdown 后必须同时满足：

- 不再接受新 message；
- waiter 不能永久睡眠；
- callback 不能访问已释放 queue；
- detach/notify 路径必须收敛。

所以 shutdown 不只是 undeclare subscriber，还必须关闭本地同步状态。

## 10. 可迁移的 Runtime 模式

任何异步 I/O Runtime 接入用户调度器时，都可以采用同一模式：

~~~text
I/O callback
  -> take ownership
  -> enqueue
  -> set readiness predicate
  -> wake scheduler

scheduler
  -> observe ready
  -> dequeue/take
  -> run user work
~~~

网络库、设备中断线程、共享内存 notification、GPU completion event 都可以套用这个执行边界。
