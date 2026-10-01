# ROS1 Service RPC Runtime：从 Master 查找到阻塞调用、CallbackQueue 与持久连接

本文固定到 `ros_comm@30483a9f218f1545eec16d3934bf3cb042e2cb5b`。ROS1 Service 经常被描述成“同步 RPC”，但源码里真正值得理解的是：它如何把 **中心化服务发现、TCPROS 风格握手、请求/响应 framing、服务端 callback 调度、客户端阻塞等待** 拼成一次 RPC。

## 1. Service 不是 Topic 上加一个返回值

Topic 的基本形态是：

~~~text
Publisher -> N Subscribers
~~~

Service 的基本形态是：

~~~text
Client --request--> Server
Client <--response-- Server
~~~

因此 Service 必须额外解决两个问题：

1. 当前 response 属于哪一次 request；
2. 客户端何时认为一次 call 已经完成。

ROS1 的答案不是“在 CallbackQueue 里等待 response”，而是在客户端连接对象里维护独立的 call queue 与 condition variable。

## 2. 服务发现：Master 返回 rosrpc://host:port

服务端调用 `ServiceManager::advertiseService()` 后，会向 Master 注册：

~~~text
registerService(
  caller_id,
  service_name,
  rosrpc://host:tcp_port,
  xmlrpc_api
)
~~~

`ServiceManager` 同时在本地保存一个 `ServicePublication`。

客户端第一次建立到服务端的连接时，会调用：

~~~text
lookupService(service_name)
    |
Master
    |
rosrpc://host:port
~~~

然后创建：

~~~text
TransportTCP
Connection
ServiceServerLink
~~~

因此 Service discovery 与 Topic 一样仍然由 Master 负责，但业务数据不经过 Master。

## 3. 一个容易混淆的命名：ServerLink 在客户端，ClientLink 在服务端

ROS1 这组类型名称从“远端角色”命名：

~~~text
客户端进程
  ServiceServerLink
       |
       v
    server

服务端进程
  ServiceClientLink
       |
       v
    client
~~~

也就是说：

- `ServiceServerLink`：客户端持有的“通往 server 的连接”；
- `ServiceClientLink`：服务端持有的“来自 client 的连接”。

看源码时如果把类名按本地角色理解，调用链会完全看反。

## 4. 建链：Service header 与 Topic header 相似，但语义不同

客户端 `ServiceServerLink` 发送 header：

~~~text
service
md5sum
callerid
persistent
...
~~~

服务端 `ServiceClientLink::handleHeader()` 校验 service、md5sum、callerid 与 persistent。

通过后，服务端回写：

~~~text
request_type
response_type
type
md5sum
callerid
~~~

所以 ROS1 Service 并不是在 XML-RPC 上承载业务 request。XML-RPC 只做 discovery，真正 RPC payload 仍走 roscpp 的 TCP connection。

## 5. 请求 framing：4 字节长度 + request bytes

服务端完成 header 后进入：

~~~text
read(4)
  -> onRequestLength()
  -> read(len)
  -> onRequest()
~~~

`onRequest()` 最终调用：

~~~text
ServicePublication::processRequest(...)
~~~

此时仍然处在 I/O 交付路径，用户 service callback 还没有运行。

## 6. ServicePublication 把请求转成 CallbackQueue 工作项

`ServicePublication::processRequest()` 会构造 `ServiceCallback`，然后：

~~~cpp
callback_queue_->addCallback(cb, (uint64_t)this);
~~~

所以服务端执行链是：

~~~text
TCP request bytes
    |
ServiceClientLink
    |
ServicePublication::processRequest
    |
CallbackQueue
    |
Spinner
    |
ServiceCallback::call()
    |
user service callback
~~~

这意味着 **Service callback 与 Topic callback 共用同一套 CallbackQueue/Spinner 执行机制**，除非应用显式给它们分配不同 queue。

因此一个耗时 service callback 完全可以阻塞同一 SingleThreadedSpinner 上的 topic callback。

## 7. response framing：success bit + 长度 + payload

`ServiceCallback::call()` 调用用户 callback 后，序列化 service response。

客户端 `ServiceServerLink::onRequestWritten()` 先读固定 5 字节：

~~~text
1 byte: ok
4 bytes: response length
~~~

然后再读 response payload。

可以抽象成：

~~~text
+------+----------+--------------------+
| ok   | length   | response bytes     |
+------+----------+--------------------+
 1 B      4 B          N B
~~~

如果 `ok == 0`，payload 可以是错误字符串，而不是正常 response object。

## 8. 为什么 ROS1 C++ service call 是同步阻塞的

`ServiceServerLink::call()` 会创建一个 `CallInfo`：

~~~text
request
response*
success
finished
condition_variable
caller_thread_id
~~~

随后把它放入 `call_queue_`。

真正发送完成后，调用线程进入：

~~~cpp
while (!info->finished_) {
  info->finished_condition_.wait(lock);
}
~~~

response 到达后：

~~~text
onResponse()
  |
callFinished()
  |
finished_ = true
notify_all()
~~~

调用线程才返回。

因此 ROS1 同步 Service 的核心不是“网络 read() 阻塞当前线程”，而是：

> I/O 仍由 Connection/PollManager 异步推进，调用线程通过 condition variable 等待这次 CallInfo 完成。

这是典型的“异步 transport + 同步 API façade”。

## 9. persistent service 为什么需要 call_queue_

非持久 Service：

~~~text
connect
  -> request
  -> response
  -> close
~~~

persistent Service：

~~~text
connect once
  -> request A / response A
  -> request B / response B
  -> ...
~~~

同一 TCP connection 上不能让多次同步 RPC 的 response 顺序失控，因此 `ServiceServerLink` 维护：

~~~text
call_queue_
current_call_
~~~

`processNextCall()` 保证同一 connection 上一次只推进一个 current call。

这相当于把一个 TCP byte stream 上的 RPC pipeline 串行化。

## 10. persistent 提升了什么，又付出了什么

persistent 模式省掉了每次 TCP connect、TCP handshake 和 Service header handshake。

但它也引入更明显的 connection lifetime：

- server 重启后旧 connection 会失效；
- stale persistent link 需要被 drop；
- 所有 call 共享一个 connection；
- 故障恢复不再等价于“下一次调用重新连接”。

因此 persistent 不是单纯的“更快”，而是把连接生命周期从 call-level 提升到 client-level。

## 11. Service 与 Topic 最大的语义差异

Topic subscriber 的 backlog 在很多状态流场景中可以丢旧样本。

Service request 不一样：

~~~text
request A
request B
request C
~~~

这些通常都具有独立业务意义，不能简单用“只保留最新请求”的思路覆盖。

所以 Service 更接近 event/RPC 语义，而不是 latest-state stream。

## 12. ROS1 Service 的完整对象图

~~~text
Master
  |
lookupService / registerService
  |
  +------------------------------+
  |                              |
Client process                Server process
  |                              |
ServiceServerLink          ServiceClientLink
  |                              |
Connection <==== TCP ====> Connection
  |                              |
call_queue_                ServicePublication
  |                              |
condition_variable          CallbackQueue
                                 |
                               Spinner
                                 |
                            user callback
~~~

ROS1 Service 的关键设计可以概括为：

> **Master 负责找服务，Connection 负责搬字节，ServiceServerLink 负责把异步 response 转成同步 call，ServicePublication 则把网络请求重新交给 CallbackQueue。**

这也解释了为什么 Service 的实时性不能只看 TCP latency：CallbackQueue 和 Spinner 同样处在关键路径上。
