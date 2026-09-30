# ThreadPool Work Queue：为什么 libuv 不把所有异步工作都交给 Event Loop

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

Event loop 适合处理短 callback 和 non-blocking I/O readiness，但普通文件 I/O、DNS 或用户提交的 blocking work 可能直接卡住 loop。

libuv 的解决方式是把 blocking work 放进一个全局 worker pool。

## 核心数据结构

固定实现使用：

~~~c
static uv_cond_t cond;
static uv_mutex_t mutex;
static unsigned int idle_threads;
static unsigned int nthreads;

static struct uv__queue wq;
static struct uv__queue slow_io_pending_wq;
static struct uv__queue run_slow_work_message;
static struct uv__queue exit_message;
~~~

这里不是一个 queue，而是多条语义不同的 queue + sentinel message。

## 为什么使用 Intrusive Queue

每个 uv__work 自己内嵌 queue node。

~~~text
work object
├─ function pointers
├─ owning loop
└─ queue node
~~~

这样 push/remove 不需要额外分配 list node。

对于高频 runtime bookkeeping 很合适。

## Worker 为什么围绕 mutex + condition_variable

Worker 主循环：

固定实现用 `uv_cond_wait(&cond, &mutex)` 在没有可运行工作时释放 mutex 并睡眠；被唤醒后重新持锁，再次检查 queue predicate。

~~~text
lock global mutex
↓
while no runnable work
  cond_wait
↓
take work
↓
unlock
↓
execute blocking function
↓
publish completion
~~~

真正耗时的 work() 明确在 mutex 外执行。

锁只保护调度数据结构，而不是保护业务执行。

## 为什么不能持有 Global Mutex 执行 Work

如果这样做：

~~~text
worker0 lock
→ blocking file read 100 ms
→ unlock
~~~

所有其他 worker 都无法 dequeue，整个 pool 退化成单线程。

所以必须把：

~~~text
scheduler state critical section
与
business blocking section
~~~

严格分离。

## Slow I/O 为什么有单独 Pending Queue

libuv 不希望所有 worker 都被慢 I/O 占满。

源码定义：

~~~c
static unsigned int slow_work_thread_threshold(void) {
  return (nthreads + 1) / 2;
}
~~~

最多大约一半线程同时执行 slow I/O。

剩余 worker 仍可处理普通 work。

这实际上是一种 class-based admission control。

## run_slow_work_message 为什么是 Sentinel

slow I/O 不直接全部插进主 wq。

而是：

~~~text
slow_io_pending_wq
  stores actual slow jobs

run_slow_work_message
  one marker in main wq
~~~

Worker 遇到 marker 后才去 slow queue 取真正 job。

这带来一个很有意思的效果：

> 主 queue 中只需要一个“slow work class 仍有任务”的代表，不需要让 slow jobs 淹没普通 jobs。

## 为什么 Slow Work 达到阈值时 Marker 要重新排尾部

源码：

~~~text
slow work running >= threshold
→ move marker to tail
→ let normal work run first
~~~

这是一种轻量级 fairness / class scheduling。

没有复杂 priority queue，也能防止 slow class 独占 pool。

## Completion 为什么不直接在线程池 Callback

worker 做完以后：

~~~c
uv_mutex_lock(&w->loop->wq_mutex);
uv__queue_insert_tail(&w->loop->wq, &w->wq);
uv_async_send(&w->loop->wq_async);
uv_mutex_unlock(&w->loop->wq_mutex);
~~~

完成结果被放进**目标 loop 自己的 completion queue**，然后用 async wakeup 叫醒 loop。

最终 done callback 回到 loop thread。

这重新建立了单 owner execution model：

~~~text
blocking work
  worker thread

stateful completion callback
  loop thread
~~~

## Global Pool + Loop-local Completion Queue 为什么合理

多个 event loop 可以共享 worker pool：

~~~text
Loop A ─┐
Loop B ─┼→ global workers
Loop C ─┘
~~~

但完成时：

~~~text
work A completion → Loop A queue
work B completion → Loop B queue
~~~

全局资源共享与局部 state ownership 同时成立。

## 为什么源码特别强调两把锁不能同时持有

注释：

> To avoid deadlock with uv_cancel() it's crucial that the worker never holds the global mutex and the loop-local mutex at the same time.

存在：

~~~text
global pool mutex
loop->wq_mutex
~~~

如果 worker 和 cancel path 以不同顺序同时拿两把锁，就会形成 ABBA deadlock。

所以实现通过明确 lock ordering / non-overlap 规避。

## Cancel 为什么只能取消“还没开始执行”的 Work

uv__work_cancel() 判断：

~~~text
work node still in queue
AND work function not NULL
~~~

一旦 worker 已经取走并执行，普通 userspace runtime 很难安全强制终止任意 blocking function。

所以 cancel 语义实际上是：

> cancel pending work, not preempt running code。

这和线程池、future、task scheduler 中最常见的 cancellation 边界一致。

## ThreadPool Size 为什么有上限

默认 4，环境变量可调，最大 1024。

线程越多并不意味着越快。

还要考虑：

~~~text
context switch
stack memory
file-system/device parallelism
lock contention
CPU saturation
~~~

## fork 后为什么要重新初始化

Unix fork 只复制调用 fork 的那条线程。

原来 threadpool 的 mutex/cond/workers 状态不能直接当作子进程仍然有效。

libuv 用 pthread_atfork 重置 once，让 child 重新初始化 pool。

这是多线程程序 fork 的经典陷阱。

## 对普通程序的迁移

可以抽象成：

~~~text
event/runtime thread
→ submit blocking task
→ shared worker pool
→ execute outside scheduler lock
→ loop-local completion queue
→ eventfd/async wakeup
→ callback on owner thread
~~~

数据库访问、压缩、磁盘日志、设备阻塞 ioctl 都可以使用类似边界。

## 最关键的设计原则

1. blocking work 与 event loop 隔离。
2. scheduler lock 不覆盖业务 work。
3. 不同 workload class 可以有独立 admission limit。
4. global workers 可以共享，但 completion 应回到 state owner。
5. cancellation 语义必须明确区分 pending 与 running。
6. 多把锁必须定义全局 lock-order 规则。
