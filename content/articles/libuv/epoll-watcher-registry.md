# epoll Watcher Registry：为什么 libuv 用 fd→watcher 稠密数组，再用 watcher_queue 延迟同步到内核

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

Linux Reactor 最容易被简化成一句：

~~~text
把所有 fd 放进 epoll
~~~

真正的 runtime 还必须解决：

- 一个 fd 当前对应哪个用户态 watcher；
- interest mask 改变以后何时调用 epoll_ctl；
- fd 被关闭/复用以后怎样识别 stale event；
- callback 里 stop watcher 时，当前批次事件怎样处理；
- 为什么不每次 start/stop 都立即 syscall。

libuv 的答案是一套两级状态：**用户态 watcher registry + 待同步 watcher queue**。

## 第一层：fd 直接索引 watcher

loop 保存：

~~~text
watchers[fd] -> uv__io_t*
~~~

这不是 hash map，而是稠密数组。

当 epoll 返回：

~~~text
event.data.fd = 42
~~~

libuv 直接：

~~~c
w = loop->watchers[fd];
~~~

不需要再 hash lookup。

## 为什么数组适合这里

Linux fd 本身已经是非负整数 ID。

典型查询：

~~~text
kernel gives fd
→ runtime wants watcher
~~~

所以数组提供真正 O(1) 直接寻址。

代价是：

~~~text
最大 fd 很大但实际 watcher 很少
→ 稀疏内存浪费
~~~

libuv 用按 2 次幂扩容的策略减少 realloc 次数。

## maybe_resize 为什么扩到 next_power_of_two

源码：

~~~text
requested len = fd + 1
→ round up to next power of two
~~~

这样连续出现 fd 1001、1002、1003 时，不会每次都 realloc。

这是典型动态数组 amortization。

## 第二层：watcher_queue 保存“interest mask 有变化”的 watcher

uv__io_start() 并不立即 epoll_ctl。

它先：

~~~text
update w->pevents
→ if kernel state differs
  enqueue watcher_queue
~~~

真正 epoll_ctl 在 uv__io_poll() 进入 epoll_wait 前统一执行。

## 为什么不每次 start/stop 都立即 epoll_ctl

一次业务 callback 里可能发生：

~~~text
start POLLIN
stop POLLIN
start POLLOUT
stop POLLOUT
start POLLIN
~~~

如果每次都 syscall，会制造大量 control-plane 开销。

延迟合并后：

~~~text
userspace changes desired mask pevents
↓
one queued watcher
↓
before poll
compare events vs pevents
↓
epoll_ctl final state
~~~

这是一种 write coalescing。

## events 与 pevents 为什么分成两个字段

~~~text
pevents
= desired events in userspace

events
= events currently installed in kernel backend
~~~

如果：

~~~text
events == pevents
~~~

说明 backend 已同步，不需要 syscall。

这和数据库 dirty flag、GPU descriptor update、network route cache pending-update 是同类模式。

## uv__io_start 的关键路径

~~~c
w->pevents |= events;
maybe_resize(loop, w->fd + 1);

if (w->events == w->pevents)
  return 0;

if (uv__queue_empty(&w->watcher_queue))
  uv__queue_insert_tail(&loop->watcher_queue,
                        &w->watcher_queue);

if (loop->watchers[w->fd] == NULL) {
  loop->watchers[w->fd] = w;
  loop->nfds++;
}
~~~

这里同时维护：

~~~text
identity index
desired interest
dirty watcher list
active fd count
~~~

## uv__io_poll 如何把 Dirty Watcher Flush 到 epoll

~~~c
while (!uv__queue_empty(&loop->watcher_queue)) {
  w = head(...);
  remove(w);

  op = (w->events == 0) ? EPOLL_CTL_ADD
                         : EPOLL_CTL_MOD;

  w->events = w->pevents;
  e.events = w->pevents;
  e.data.fd = w->fd;

  epoll_ctl(epollfd, op, fd, &e);
}
~~~

用户态 state mutation 与 kernel synchronization 被明确分成两个 phase。

## 为什么 event.data 只存 fd，而不是直接存 watcher pointer

epoll 支持 data.ptr。

libuv 选择 data.fd，然后再查 watchers[fd]。

这样可以在用户态通过 registry 判断：

~~~text
这个 fd 对应的 watcher 还存在吗？
~~~

如果 watcher 已 stop/close，watchers[fd] 会变 NULL。

## Stale epoll Event 为什么真实存在

事件可能已经被 kernel 放进 ready list，随后用户代码关闭 fd 或 stop watcher。

epoll_wait 返回的仍可能是旧事件。

所以 poll loop 会检查：

~~~c
w = loop->watchers[fd];

if (w == NULL) {
  epoll_ctl(epollfd, EPOLL_CTL_DEL, fd, pe);
  continue;
}
~~~

这就是 generation/stale-handle 问题在 fd runtime 中的版本。

## callback 可能在当前 batch 中 Stop 自己

同一次 epoll_wait 返回很多事件。

处理 event A 的 callback 可能 stop watcher B。

所以真正 dispatch 前还会：

~~~c
pe->events &= w->pevents | POLLERR | POLLHUP;
~~~

把 kernel 返回的旧 readiness 与**当前用户态 interest**重新取交集。

这样 B 已经 stop POLLIN 后，即使当前 epoll batch 里还有 POLLIN，也不会错误回调。

## 为什么 async eventfd 特殊使用 EPOLLET

普通 watcher 以 level-triggered 语义工作。

async_io_watcher 的 eventfd 只是 wakeup hint，真正 pending state 在用户态 atomic bit。

因此 libuv 对它加 EPOLLET，减少不必要的 eventfd read/syscall。

这再次说明：

~~~text
kernel event
不一定就是业务数据
~~~

有时只是“请重新检查用户态状态”。

## 为什么 POLLERR / POLLHUP 总要保留

即使用户只关注 POLLIN/POLLOUT，socket error/hangup 仍然必须被 runtime 看见。

所以过滤时保留：

~~~text
requested events
+ POLLERR
+ POLLHUP
~~~

## 这套结构的通用抽象

可以压成：

~~~text
ResourceId -> Object Registry

Object.desired_state
Object.backend_state

DirtyList<Object>

flush dirty state before blocking
~~~

这不只适合 epoll。

还可以用于：

- device subscription registry；
- network routing table；
- GUI event source；
- hardware interrupt mask manager；
- runtime fd multiplexer。

## 数组、Hash、Tree 应该怎么选

如果 ResourceId：

~~~text
稠密整数且由 OS 返回
→ array/direct index

稀疏大整数/复杂 key
→ hash map

需要 ordered/range query
→ tree
~~~

容器选择来自 key space 与 query pattern，而不是“统一使用 unordered_map”。

## libuv 这一页最值得迁移的四个原则

1. OS 返回的稠密整数 ID 可以直接做数组索引。
2. desired state 与 applied backend state 分开保存。
3. 高频状态变化先在 userspace 合并，再批量 syscall。
4. kernel event 到达时仍要重新验证当前 userspace ownership/interest。
