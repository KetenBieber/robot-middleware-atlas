# CPUThreadPoolExecutor：线程池真正需要设计的是 Queue Policy、Wakeup Policy 与 Worker Lifecycle

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

线程池常被简化成 `std::queue + mutex + condition_variable + N workers`。这只是最小实现。

Folly 的 CPUThreadPoolExecutor 展示了工业线程池真正需要分离的几个维度。

## Executor 与 Queue 解耦

构造函数接收：

~~~cpp
std::unique_ptr<BlockingQueue<CPUTask>>
~~~

因此 worker lifecycle 和 task queue policy 不是一回事。

同一个 Executor 可以组合不同 queue 实现。

## 默认 Queue 为什么不是 std::queue

当前版本默认倾向：

~~~text
UnboundedBlockingQueue
+
ThrottledLifoSem
~~~

也支持 LifoSem、priority queue、bounded priority MPMC queue。

所以线程池的性能不只由“有几个线程”决定，还由：

~~~text
queue policy
+
wakeup primitive
~~~

共同决定。

## LIFO Waiter 的目标是 Cache Locality

LIFO 风格 waiter 让最近睡下的 worker 更可能先被复用。

潜在收益：

- 最近运行过，cache/TLB 更热；
- 不需要让所有 worker 轮流活跃；
- 长期空闲线程可以持续休眠。

这是一种 locality-aware wakeup policy。

## 为什么还要 Throttle Wakeup

任务 burst 时一次性唤醒大量线程会造成：

~~~text
thundering herd
context-switch spike
cache disruption
~~~

ThrottledLifoSem 控制 worker 唤醒扩散速度。

所以 thread pool 的 wakeup policy 还要回答：

~~~text
wake how many?
wake how fast?
~~~

## Dynamic Thread Count

Worker loop 根据配置调用 `take()` 或 `try_take_for(timeout)`。

如果允许缩容，空闲线程 timeout 后可以退出。

这换来了资源弹性，但也引入 thread creation cost、cache 冷启动和 latency spike。

## Poison Task 是关闭协议

停止线程时，Executor 会向 queue 放入特殊空 CPUTask。

Worker 取到后识别它不是业务任务，而是 control message。

~~~text
normal task
or
poison / shutdown task
~~~

这让 shutdown 复用普通 work-delivery 路径。

## Priority Queue 不等于 OS Thread Priority

Folly 的 task priority 主要通过多条 queue 实现：worker 先取高优先级任务。

但 worker 的 Linux scheduling class 并不会因此自动变成 SCHED_FIFO。

所以：

> application task priority 与 OS scheduler priority 是两个不同层次。

这对机器人实时系统尤其重要。

## 为什么 Blocking Task 需要单独 Policy

CPU pool 的基本假设是 task 主要消耗 CPU 后返回。

如果所有 worker 都去等 blocking I/O，会产生 thread-pool starvation。

因此较好的结构通常是：

~~~text
I/O Event Loop
→ CPU Executor
→ Serial State Owner
~~~

而不是一个万能线程池承担所有事情。

## Worker 数量为什么不是越多越好

CPU-bound workload 线程过多会增加 context switch、LLC contention、NUMA remote access 和 cache thrash。

线程数必须和 core count、affinity、task duration、blocking ratio 一起分析。

## 机器人 Runtime 映射

~~~text
sensor/network EventBase
→ CPU preprocess / perception pool
→ planner serial executor
→ control owner thread
~~~

每一层的执行语义不同，不应该只用一个 global pool。

## 可迁移原则

1. Executor policy 与 Queue policy 分开。
2. Wakeup 策略直接影响 cache locality 与 burst latency。
3. CPU pool 与 blocking-I/O pool 应区分。
4. Application priority queue 不等于 OS real-time priority。
5. Shutdown、resize、expiration 都应是显式 Runtime protocol。