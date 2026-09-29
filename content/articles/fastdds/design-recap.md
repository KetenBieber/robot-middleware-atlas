# Fast DDS 设计复盘：从 DataWriter 到 Transport，再从 Reader 回到 ROS Executor

固定源码：39303846fb8534ef69fa65f9fa4bcc9e6a7c995a。

## 一次写调用最终跨越哪些对象

~~~text
DataWriter
→ DataWriterImpl
→ writer mutex
→ loan / serialize
→ PayloadPool
→ CacheChange_t
→ WriterHistory
→ StatefulWriter
→ ReaderProxy
→ FlowController
→ RTPSMessageGroup
→ Transport
~~~

每个箭头都可能引入 lock、allocation、queue、timer、copy 或 backpressure。

因此 middleware latency 不是一个单函数问题。

## 一次接收跨越哪些对象

~~~text
Transport ReceiverResource
→ MessageReceiver
→ StatefulReader
→ WriterProxy
→ fragment assembly
→ ReaderHistory
→ DataReaderImpl
→ StatusCondition
→ WaitSet / Listener
→ application
~~~

控制面通过 PDP/EDP 创建 Proxy；数据面消费这些 Proxy。

Discovery 与 Data path 从来不是完全独立模块。

## Fast DDS 最有代表性的几个设计选择

### 1. CacheChange 作为协议数据单元

把 sequence、GUID、payload、fragment、instance handle 聚合成一个对象，让 History/Writer/Reader 都能围绕同一生命周期工作。

### 2. Pool Abstraction

PayloadPool / ChangePool 让内存策略从算法里抽离。

因此普通 heap、预分配、Data Sharing、loan 可以复用同一套 endpoint protocol。

### 3. Stateful Endpoint + Proxy

Reliable 的本质是：

~~~text
为每个远端 endpoint
保存协议进度
~~~

所以 ReaderProxy / WriterProxy 是可靠性的核心，不是 discovery 附件。

### 4. ResourceEvent

大量 heartbeat、deadline、liveliness timer 共享一个 event scheduling thread，而不是无限创建 timer thread。

### 5. FlowController

Async send 不只是后台线程，还显式建模 Writer queue、priority、bandwidth period 与 fragment budget。

这让 Fast DDS 很适合研究中间件内部调度。

## 最容易误解的三件事

第一：

~~~text
SHM Transport
!= Data Sharing
!= loan_sample
~~~

第二：

~~~text
asynchronous publish
!= write() 没有
serialization/history 工作
~~~

第三：

~~~text
Reliable
!= 业务动作已经执行
~~~

## 机器人控制系统真正应该测什么

对控制链建议记录：

~~~text
sensor timestamp
DDS receive timestamp
ReaderHistory ready
rmw_wait wake
callback start
controller end
rmw_publish
DataWriter write return
wire send
actuator receive
~~~

再计算 data age、compute time、publish call WCET、transport delay、executor wait 与 end-to-end age。

这比只测 ping latency 更能解释闭环抖动。

## 下一步进入真实工程

源码本体读完后，专题不再自制 Demo。

第一条真实链使用 Fast DDS 官方 delivery_mechanisms 示例，对照 UDP、SHM Transport、Data Sharing 与 loan_sample。

第二条固定到 ROS 2 rmw_fastrtps，直接追：

~~~text
ROS Publisher
→ Fast DDS DataWriter

ROS wait set
→ Fast DDS WaitSet
~~~

最后将 Fast DDS 与 Cyclone DDS 放在同一张实现坐标图里，形成 ROS 2 DDS 底层的完整双实现对照。
