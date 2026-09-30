# Timer Rbtree：为什么 nginx 不用 libuv 的 Min-Heap，还要做 300 ms Lazy Update

固定源码版本：`b74b5c961e687c76489482b44cedff63acd18c84`。

nginx 与 libuv 都需要管理大量 Timer，但选择不同：libuv 用 min-heap，nginx 用 red-black tree。

固定源码中的全局 Timer 容器就是 `ngx_event_timer_rbtree`，每个 `ngx_event_t` 通过内嵌的 rbtree node 加入这棵树。

真正值得学的不是“谁更好”，而是**相同问题在不同操作模式下为什么可以选择不同容器。**

## nginx 的 Timer 操作

~~~text
add event timer
delete event timer
find nearest deadline
expire all due timers
check remaining non-cancelable timers
~~~

`ngx_event_t` 直接内嵌：

~~~text
ngx_rbtree_node_t timer
~~~

所以 timer delete 不需要先查 node。

## 最近 Deadline 仍然来自最小 Node

~~~c
node = ngx_rbtree_min(root, sentinel);
timer = (ngx_msec_int_t) (node->key - ngx_current_msec);
~~~

event loop 用这个值作为 poll timeout。

逻辑仍然是：

~~~text
nearest timer deadline
→ epoll_wait timeout
~~~

## Duplicate Key 为什么允许

源码明确写：

> the event timer rbtree may contain duplicate keys

因为 nginx 主要只关心最小 timer value。

同一毫秒有多个事件到期不要求 key 全局唯一。

## 为什么 rbtree 适合频繁 delete

如果对象内嵌 tree node：

~~~text
insert O(log N)
delete O(log N)
min O(log N) traversal, tree height bounded
~~~

相比 heap，删除任意已知 node 不需要做额外 index bookkeeping。

nginx 大量 timer 会因为连接状态变化被取消/重设，这个特性很实用。

## 最有价值的优化：Lazy Timer Update

重新设置 timer 时：

~~~c
diff = key - ev->timer.key;

if (ngx_abs(diff) < NGX_TIMER_LAZY_DELAY) {
  return;
}
~~~

`NGX_TIMER_LAZY_DELAY = 300` ms。

也就是说新 deadline 与旧 deadline 差不到 300 ms，nginx 直接保留旧 tree position。

## 为什么允许“不完全准确”

许多网络 timeout：

~~~text
keepalive timeout
read timeout
write timeout
upstream timeout
~~~

本身不是硬实时 deadline。

如果连接持续有数据，每次读写都把 deadline 从：

~~~text
now + 60s
~~~

向后平移几十毫秒，而每次都 delete+insert rbtree，会产生大量无意义维护。

300 ms lazy window 用轻微 timer 精度换 tree update 成本。

## 这是一种 Approximate Data Structure 思维

严格语义：

~~~text
每次 deadline 变化都精确重排
~~~

nginx 语义：

~~~text
deadline drift < tolerance
→ keep old order
~~~

如果业务容许 epsilon 误差，就不必为 exactness 支付全部成本。

这个思想可以迁移到：

- telemetry aggregation；
- cache refresh；
- lease renewal；
- watchdog buckets；
- approximate LRU。

## Expire Timer 为什么直接调用 Handler

nginx 从 rbtree 取最小 node，确认到期后先：

~~~text
delete from rbtree
timer_set = 0
timedout = 1
~~~

再：

~~~text
ev->handler(ev)
~~~

也就是先完成 scheduler ownership mutation，再进入任意业务 callback。

这和 libuv 先从 heap 摘出到 ready queue 再 callback 虽实现不同，但都遵守：

> callback 之前先把 scheduler structure 放到一致状态。

## `ngx_event_no_timers_left()` 为什么遍历整棵树

graceful shutdown 时要判断：

~~~text
是否只剩 cancelable timer
~~~

这不是 nearest-deadline query，而是全局属性查询。

rbtree 可以按序遍历 node。

这正是它比纯 heap 更自然的另一类访问模式。

## Min-Heap vs Rbtree

| 维度 | libuv Min-Heap | nginx Rbtree |
| --- | --- | --- |
| nearest deadline | 非常直接 | 取最左节点 |
| 任意 node 删除 | 需要 heap node/index 维护 | 内嵌 tree node 很自然 |
| ordered traversal | 不自然 | 自然 |
| duplicate deadline | 可以 | 可以 |
| lazy update | 可做 | nginx 明确实现 |
| 典型 workload | 通用 loop timers | connection event timers |

## 程序设计结论

数据结构选型不应该只比较 Big-O 表。

要同时写出：

~~~text
查询模式
更新模式
删除模式
是否需要遍历
是否允许近似
对象是否可 intrusive
~~~

这些信息比“heap O(logN)、tree O(logN)”更能决定真实实现。
