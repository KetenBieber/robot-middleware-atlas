# Timer Heap：为什么 libuv 用最小堆，而不是每个 Timer 一条线程

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

Timer 是一个非常纯粹的数据结构问题：需要频繁插入/删除，而每轮最重要的查询只有一个——**最近到期的是谁？**

## 操作集合决定数据结构

~~~text
start(timer, deadline)
stop(timer)
restart(timer)
get nearest deadline
pop all expired timers
~~~

最小堆正好提供：

~~~text
insert O(log N)
remove O(log N)
min O(1)
~~~

## 比较规则是 Deadline + Start Order

~~~c
if (a->timeout < b->timeout)
  return 1;
if (b->timeout < a->timeout)
  return 0;

return a->start_id < b->start_id;
~~~

timeout 是第一 key，start_id 是第二 key。

两个 Timer 同 deadline 时仍然有稳定顺序，提高可预测性与可测试性。

## Start 就是 Heap Insert

~~~c
handle->timeout = handle->loop->time + timeout;
handle->repeat = repeat;
handle->start_id = handle->loop->timer_counter++;

heap_insert(timer_heap(handle->loop),
            &handle->node.heap,
            timer_less_than);
~~~

Timer Handle 内嵌 heap node，这是一种 intrusive container 设计。

## 最近 Deadline 如何直接变成 Poll Timeout

uv__next_timeout() 的语义：

~~~text
heap empty
→ -1, block indefinitely

root already expired
→ 0, don't block

root in future
→ root.timeout - now
~~~

Timer scheduler 与 epoll/kqueue wait 因而合并。

## 为什么不是 Sorted List

有序链表：

~~~text
insert O(N)
min O(1)
~~~

大量动态 Timer 下，插入太贵。

## 为什么不是普通 Vector

无序 vector：

~~~text
insert O(1)
min O(N)
~~~

每轮 poll 前都要全扫。

## 为什么不是红黑树

红黑树也能 O(log N) insert/remove，并支持 ordered/range query。

但 libuv 主要只需要 nearest deadline，heap 的语义更贴合。

后续 nginx 的 timer rbtree 可以做非常好的横向对照：相似目标，不同容器权衡。

## Expired Timer 为什么先搬到 Ready Queue

~~~c
for (;;) {
  heap_node = heap_min(...);
  if (heap_node == NULL)
    break;

  if (handle->timeout > loop->time)
    break;

  uv_timer_stop(handle);
  uv__queue_insert_tail(&ready_queue,
                        &handle->node.queue);
}
~~~

先改变 scheduler data structure，再执行用户 callback：

~~~text
phase 1:
extract expired timers

phase 2:
invoke arbitrary callbacks
~~~

这样 callback 即使 restart/stop Timer，也不会破坏当前 heap traversal。

## Repeat Timer 为什么先 Re-arm 再 Callback

~~~c
uv_timer_again(handle);
handle->timer_cb(handle);
~~~

callback 运行时看到的是已经更新后的 scheduler state，而不是半完成状态。

## 什么时候最小堆不一定最好

适合：

~~~text
大量动态 deadline
频繁 start/stop
主要查询 nearest deadline
~~~

不一定适合：

~~~text
hard RT static cyclic schedule
百万级粗粒度 timer bucket
需要大量 range query
~~~

程序设计的关键不是“Timer 就用 heap”，而是先列出操作集合与实时约束。

## 机器人 Runtime 的直接应用

~~~text
sensor watchdog
network heartbeat
planner timeout
device reconnect
retry deadline
~~~

可以统一成：

~~~text
deadline min-heap
→ nearest deadline controls poll timeout
→ expired entries move to ready queue
→ callbacks run after scheduler mutation
~~~

比每类 timeout 都创建 sleeping thread 更容易控制。
