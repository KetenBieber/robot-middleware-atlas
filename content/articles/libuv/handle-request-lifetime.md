# Handle 与 Request 生命周期：为什么 uv_close() 之后仍然不能 free

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

libuv 把长期资源建模为 Handle，把一次异步动作建模为 Request。这个区分最终落到一个最重要的问题：**什么时候真正允许回收对象内存。**

## Handle 是长期资源

例如 TCP：

~~~text
init
→ bind/connect/listen/read
→ many async operations
→ close requested
→ close callback
→ memory reclaim
~~~

## Request 是一次操作

一次 write：

~~~text
create uv_write_t
→ queue
→ partial progress
→ EAGAIN / wait POLLOUT
→ completion
→ callback
→ request storage reusable
~~~

Connection lifetime 和 operation lifetime 是两条不同时间线。

## uv_close() 只进入第一阶段

~~~c
void uv_close(uv_handle_t* handle, uv_close_cb close_cb) {
  handle->flags |= UV_HANDLE_CLOSING;
  handle->close_cb = close_cb;

  switch (handle->type) {
    ...
  }

  uv__make_close_pending(handle);
}
~~~

它只做：

~~~text
state → CLOSING
stop type-specific backend activity
append closing list
~~~

没有直接 free。

## 为什么不能立即释放

错误代码：

~~~c
void on_read(...) {
  uv_close((uv_handle_t*) tcp, on_close);
  free(tcp);
}
~~~

当前 I/O dispatch、callback 栈、pending request 仍可能引用这个 handle。

所以生命周期必须是：

~~~text
request close
→ detach backend state
→ closing phase
→ close callback
→ user frees memory
~~~

## Closing List 就是 Deferred Reclamation

uv__make_close_pending() 把 handle 插入 loop 的 closing list：

~~~c
handle->next_closing = handle->loop->closing_handles;
handle->loop->closing_handles = handle;
~~~

loop 后续统一 finalization：

~~~c
p = loop->closing_handles;
loop->closing_handles = NULL;

while (p) {
  q = p->next_closing;
  uv__finish_close(p);
  p = q;
}
~~~

因为 close initiation 和 finalization 都由 loop thread 组织，所以不需要复杂的跨线程 reclamation protocol。

## CLOSING 仍可能 Active

源码专门指出：

~~~text
uv_shutdown()
↓ immediately
uv_close()
~~~

此时 shutdown Request 仍可能未完成。

所以状态至少是：

~~~text
ACTIVE
→ CLOSING
→ CLOSED
→ close callback
→ RECLAIMABLE
~~~

close request 已发出，不等于所有异步参与者都已经停止访问。

## uv__finish_close() 才是真正 Runtime 放手

它会：

~~~text
mark CLOSED
→ destroy type-specific state
→ unref loop
→ remove handle from handle_queue
→ invoke close callback
~~~

只有到这里，用户才可以安全释放 Handle storage。

## Buffer Lifetime 是第三条时间线

uv_write() 要求 payload buffer 一直有效到 write callback。

~~~text
uv_stream_t
  connection lifetime

uv_write_t
  operation lifetime

payload bytes
  must survive until completion
~~~

很多异步 C/C++ bug 都是把这三种生命周期误认为一个。

## C++ RAII 为什么不能机械地在析构里 uv_close()

如果 C++ object storage 随析构立即释放，而 libuv close callback 未来才执行，就会 use-after-free。

更合理的所有权：

~~~text
heap-backed state / runtime-owned storage
→ async close
→ close callback
→ final delete
~~~

## 可迁移的五层对象模型

~~~text
Resource
  long-lived capability

Operation
  one async transaction

Payload
  data used by operation

Completion
  proves operation no longer touches payload

Reclamation
  proves resource storage may be reused
~~~

Socket、设备句柄、异步文件、DMA、数据库连接都可以用这五层分析。

## Shutdown 顺序可以由 Lifetime 反推

~~~text
stop creating Requests
→ cancel/drain pending Requests
→ request Handle close
→ wait close callbacks
→ destroy loop/runtime
~~~

如果关闭顺序依赖“希望 callback 不要再来”，说明 ownership 还没有闭环。
