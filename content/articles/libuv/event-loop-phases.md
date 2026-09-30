# Event Loop Phase：为什么 uv_run() 不是简单的 epoll_wait()

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

成熟 event loop 的难点是：**不同类别的工作以什么顺序执行、什么时候允许睡眠、如何避免一种 callback 饿死其他工作。**

## 固定源码里的执行顺序

~~~c
while (r != 0 && loop->stop_flag == 0) {
  can_sleep =
      uv__queue_empty(&loop->pending_queue) &&
      uv__queue_empty(&loop->idle_handles);

  uv__run_pending(loop);
  uv__run_idle(loop);
  uv__run_prepare(loop);

  timeout = 0;
  if ((mode == UV_RUN_ONCE && can_sleep) ||
      mode == UV_RUN_DEFAULT)
    timeout = uv__backend_timeout(loop);

  uv__io_poll(loop, timeout);

  for (r = 0;
       r < 8 && !uv__queue_empty(&loop->pending_queue);
       r++)
    uv__run_pending(loop);

  uv__run_check(loop);
  uv__run_closing_handles(loop);
  uv__update_time(loop);
  uv__run_timers(loop);
}
~~~

这是一套 phase scheduler，而不是一个 callback queue。

## Pending Queue：先收束内部状态，再执行用户 Callback

某些底层事件已经发生，但 callback 不适合立即在内部状态更新的深层栈帧里执行。

~~~text
kernel/internal event
→ finish internal state transition
→ pending queue
→ stable phase
→ user callback
~~~

这种 deferred execution 能减少重入和深层递归。

## Idle Handle 为什么让 Loop 不睡

源码直接把 idle queue 纳入 can_sleep。只要 idle handle 存在，就等价于“当前始终有 runnable work”，event loop 不应长时间阻塞。

因此 idle handle 是一种主动 busy-loop 请求；用得不当会显著提高 CPU 占用。

## Prepare / Check 是 Poll 前后的固定 Hook

~~~text
prepare
→ before blocking I/O wait

check
→ after I/O polling
~~~

更高层 runtime 可以把控制逻辑固定在 I/O 边界两侧，而不是随机插入 callback。

## Poll Timeout 来自最近 Deadline

典型情况：

~~~text
nearest timer = 17 ms
→ epoll_wait(..., 17)
~~~

如果 4 ms 时 socket ready，I/O 提前唤醒；否则 17 ms 后由 timer 触发下一轮。

## 为什么 Poll 后最多再跑 8 轮 Pending

固定小预算是 fairness mechanism。

~~~text
无限 drain
→ callback 不断产生 callback
→ timer / I/O starvation
~~~

一次只做一个又会增大 pending latency。固定 batch budget 与 NAPI budget、scheduler quantum 属于同一类取舍。

## Closing Handle 为什么有独立 Phase

uv_close() 不直接调用 close callback，而是：

~~~text
mark CLOSING
→ stop backend resource
→ append closing list
→ closing phase
→ final close
→ user callback
~~~

当前 callback 不会在调用栈中途把自己依赖的对象释放掉。

## 三种 Run Mode 是三种调度契约

UV_RUN_DEFAULT：持续运行直到 loop 不再 alive。

UV_RUN_ONCE：最多一轮；当前无立即工作时可以阻塞等待一个事件。

UV_RUN_NOWAIT：最多一轮，但不允许阻塞。

NOWAIT 使 libuv 可以嵌入其他 scheduler：

~~~text
outer runtime tick
→ uv_run(NOWAIT)
→ control work
→ other subsystem
~~~

## 对普通 Runtime 的启发

可以显式设计：

~~~text
1. ingest pending events
2. apply deferred state changes
3. run ready tasks
4. wait/poll
5. dispatch I/O
6. run deadline timers
7. reclaim closing objects
~~~

明确 phase 后，callback 上下文、timer 顺序、关闭时机和 starvation 才真正可推理。
