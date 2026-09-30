# Stream Write Backpressure：non-blocking socket 为什么仍然需要 write_queue_size

固定源码版本：`2b4b918d3381100854250c89d5159d4206daafb7`。

non-blocking socket 解决的是：

> write() 不能把 event loop 卡住。

它没有解决：

> Producer 长期比 NIC/peer 更快时，数据存到哪里？

这个问题最终仍然会变成 queue 与 backpressure。

## uv_write() 的第一层策略：能立即写就先写

新 Request 加入 write_queue 后，如果之前 queue 为空：

~~~text
try write immediately
~~~

如果 kernel send buffer 有空间，可能直接完成。

这样低负载时不需要先等下一次 POLLOUT。

## 写不完时才进入 Reactor

uv__try_write() 可能返回：

~~~text
partial bytes
or
UV_EAGAIN
~~~

如果不是 blocking stream，libuv：

~~~c
uv__io_start(stream->loop,
             &stream->io_watcher,
             POLLOUT);
~~~

把剩余 Request 留在 userspace write_queue，等 socket 再次 writable。

## write_queue 是 Operation Queue，不是 Byte Buffer

内部保存 uv_write_t Request。

每个 Request 再持有：

~~~text
buf array
current write_index
already written bytes
callback
optional send_handle
~~~

因此 partial write 可以从同一个 Request 继续。

## 为什么 write_queue_size 单独统计 Byte 数

Request 数量不足以表示压力。

~~~text
100 requests × 32 B
和
100 requests × 1 MB
~~~

完全不是一个资源规模。

所以 stream 维护：

~~~text
write_queue_size = total queued bytes
~~~

这是上层做 high-water mark 的关键观测量。

## libuv 为什么不自动限制 write_queue_size

不同业务的 overload policy 不同。

聊天消息：

~~~text
prefer lossless + disconnect very slow client
~~~

实时 telemetry：

~~~text
drop old / keep latest
~~~

文件上传：

~~~text
block/rate-limit producer
~~~

runtime 无法替业务选择。

所以 libuv 暴露机制，而不偷偷决定 policy。

## 为什么 Buffer 必须活到 Callback

uv_write() 会复制 uv_buf_t descriptor 数组，但不会深复制 payload。

因此：

~~~text
uv_write returns
≠
kernel has consumed all bytes
~~~

payload memory 必须保持有效直到 write callback。

这是 operation ownership 的一部分。

## uv__write 为什么有 count=32

源码：

~~~c
count = 32;

for (;;) {
  ...
  if (request_completed) {
    if (count-- > 0)
      continue;
    return;
  }
}
~~~

注释直接说明是为了避免 loop starvation。

如果 socket 永远 writable，而且 queue 很长：

~~~text
drain all writes
→ timers/read callbacks/other connections starve
~~~

所以单次 write progression 有 budget。

这和 uv_run() pending budget 是同一个 fairness 模式。

## 为什么完成 Request 先进入 write_completed_queue

write request 完成后并不立刻在内部深层路径调用用户 callback。

而是：

~~~text
write_queue
→ complete internal state
→ write_completed_queue
→ stable callback phase
~~~

这样 completion callback 不会破坏当前 write traversal。

## write_queue_size 为什么可能暂时非零而 write_queue 已空

源码明确解释：

~~~text
error-state request
已经移到 write_completed_queue
但最终 callback/cleanup 尚未修正 write_queue_size
~~~

因此两个变量表达不同状态：

~~~text
write_queue empty?
= 是否还有待发送 Request

write_queue_size
= 仍由 stream accounting 的未完成 byte 压力
~~~

这说明 runtime metrics 不应该用一个变量猜全部状态。

## uv_try_write 与 uv_write 的语义不同

uv_try_write()：

~~~text
如果不能立即完成
→ return UV_EAGAIN
→ 不排队
~~~

uv_write()：

~~~text
不能立即完成
→ queue
→ wait POLLOUT
→ async callback
~~~

一个是 caller-managed backpressure，一个是 runtime-managed asynchronous progression。

## Producer 怎样建立 High-Water Mark

上层可以：

~~~text
if write_queue_size > HIGH_WATER:
    stop reading upstream
    pause producer

when write_queue_size < LOW_WATER:
    resume
~~~

需要 high/low 两个阈值而不是单阈值，避免状态在边界附近来回抖动。

这是 hysteresis。

## 为什么暂停 Read 可以形成 TCP Backpressure Chain

应用停止从上游读取：

~~~text
application RX buffer fills
↓
kernel receive buffer fills
↓
TCP advertised window shrinks
↓
remote sender slows
~~~

这才是完整的 end-to-end backpressure。

单纯把本地 write_queue 放大，只是把压力藏进内存。

## 慢连接为什么应该隔离

一个 server 有 1000 个 connection。

如果每条连接都有独立 write_queue：

~~~text
slow client A
→ only A queue grows
~~~

如果所有连接共享一个无边界全局发送 queue：

~~~text
one slow destination
→ can pollute global backlog
~~~

per-connection queue 是一种 failure/backpressure isolation。

## 实时系统为什么更关心 Data Age

如果 telemetry 每 10 ms 产生一次，socket backlog 已经积累 3 秒：

~~~text
lossless transmission
but
information is uselessly stale
~~~

因此实时业务可能选择：

~~~text
bounded latest-state queue
drop old
disconnect/reconnect
sample rate reduction
~~~

而不是无限 uv_write。

## 对普通 Runtime 的迁移

任何异步输出路径都可以维护：

~~~text
request queue
queued bytes/work units
high water
low water
producer pause/resume
per-destination isolation
fairness budget
~~~

网络 socket、串口、CAN gateway、磁盘 logger、RPC client 都适用。

## 最重要的结论

non-blocking I/O 只保证线程不因为一次 syscall 长时间阻塞。

它没有消灭容量问题。

只要：

~~~text
producer rate > service rate
~~~

系统最终一定需要：

~~~text
bounded queue
+
backpressure / drop / admission policy
~~~
