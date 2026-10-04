# Proxy / Device：双向 Backpressure、动态 Wait-Set 与控制面存活性

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

ZeroMQ 的 socket pattern 解决的是：

> 单个 socket 怎样把消息组织成某种通信语义。

但真实系统还经常需要一个更高层的数据面组件：

~~~text
clients
   |
   v
frontend
   |
   v
 proxy
   |
   v
backend
   |
   v
workers
~~~

最直觉的第一版：

~~~cpp
while (true) {
    recv(frontend, msg);
    send(backend, msg);
}
~~~

看起来已经足够。

真正进入 Runtime 之后，问题马上变成：

- multipart 不能被拆开；
- 两个方向可以同时有流量；
- 任意一个输出方向都可能被 HWM 卡住；
- 不能因为输出堵塞就无限从输入继续读；
- 也不能永久监听几乎总是 ready 的 `POLLOUT` 把 CPU 打满；
- 一个繁忙方向不能无限 drain，饿死另一个方向和控制面；
- capture 不能破坏消息边界；
- PAUSE 以后控制命令仍必须可达；
- `frontend == backend` 时同一个对象不能被当成两套独立 event source；
- statistics 的计数单位必须明确；
- control socket 自己仍受它所属 socket pattern 的协议约束；
- 一个巨大 multipart 不能因为存在 `proxy_burst_size` 就被误认为拥有固定 WCET。

所以 `proxy.cpp` 更准确的定位不是“消息转发小工具”，而是：

> **建立在两个完整 socket Runtime 之上的双向 flow-control coordinator。**

它不重新实现 TCP、ZMTP、Pipe 或 routing，而是在 socket API 层继续把底层 backpressure 向上游传播。

在 XSUB/XPUB 组合中，Proxy 还承接反向订阅控制流：业务数据向订阅者方向流，而 subscribe/cancel interest 反向传播。订阅 Trie、状态重放与 Distributor 见 [PUB / SUB：订阅 Trie、反向控制面与 Distributor](pubsub-trie-distributor.md)。

---

# 一、先建立完整层次

## 1. Proxy 不拥有新的 Transport

它没有重新实现：

~~~text
TCP
ZMTP
Session
Engine
Pipe
HWM
routing
subscription trie
~~~

这些能力都已经存在于 frontend/backend socket 内部。

Proxy 站在更上层：

~~~text
             socket Runtime A
                    |
                    v
                 frontend
                    |
                    v
              +-----------+
              |   proxy   |
              +-----------+
                    |
                    v
                 backend
                    |
                    v
             socket Runtime B
~~~

所以这是：

~~~text
composition layer
~~~

而不是：

~~~text
new transport layer
~~~

---

# 二、Public API 只是薄 Wrapper

`zmq_proxy()`：

~~~cpp
int zmq_proxy(void *frontend,
              void *backend,
              void *capture)
{
    if (!frontend || !backend) {
        errno = EFAULT;
        return -1;
    }

    return zmq::proxy(
      frontend,
      backend,
      capture);
}
~~~

---

## 2. frontend/backend 是必需对象

API 显式检查：

~~~text
frontend != NULL
backend  != NULL
~~~

而：

~~~text
capture
control
~~~

可以为空。

---

# 三、普通 Proxy 就是 Steerable Proxy 的特例

内部：

~~~cpp
int proxy(frontend,
          backend,
          capture)
{
    return proxy_steerable(
      frontend,
      backend,
      capture,
      NULL);
}
~~~

所以没有两套独立算法。

真正的核心实现只有：

~~~text
proxy_steerable
~~~

普通 proxy 只是：

~~~text
control = NULL
~~~

的退化形态。

---

# 四、对象职责图

~~~text
frontend socket
    |
    | recv
    v
 reusable msg_t
    |
    +------ copy ------> optional capture socket
    |
    +------ move/send -> backend socket

backend socket
    |
    | recv
    v
 same reusable msg_t
    |
    +------ copy ------> optional capture socket
    |
    +------ move/send -> frontend socket

control socket
    |
    v
proxy state machine

stats
    |
    v
control reply
~~~

---

# 五、`msg_t` 为什么只复用一个

`proxy_steerable()`：

~~~cpp
msg_t msg;
msg.init();
~~~

然后 request/reply 两个方向都把同一个临时 `msg_t` 传给 `forward()`。

原因不是：

~~~text
两个方向并发执行
~~~

恰恰相反。

Proxy loop 是单线程顺序执行：

~~~text
process request burst
then maybe process reply burst
~~~

所以一个 reusable envelope 就够。

---

# 六、一个 Runtime Buffer 能复用的前提

必须满足：

~~~text
no overlapping use
~~~

也就是：

> **执行所有权已经保证同一时间只有一条路径占用该对象。**

如果未来改成两个 worker 并行转发两个方向，

这个单 `msg_t` 设计就不能原样保留。

---

# 七、`forward()` 的两层循环非常关键

源码骨架：

~~~cpp
for (unsigned i = 0;
     i < proxy_burst_size;
     ++i) {

    while (true) {
        recv one frame;

        read ZMQ_RCVMORE;

        capture frame;

        send frame;

        if (!more)
            break;
    }
}
~~~

外层：

~~~text
complete-message burst
~~~

内层：

~~~text
all frames of one multipart
~~~

---

# 八、`proxy_burst_size` 真正限制什么

固定值：

~~~cpp
proxy_burst_size = 1000;
~~~

源码注释：

~~~text
larger batch
→ throughput ↑
→ latency / fairness ↓
~~~

但必须精确看循环层级。

`i++` 发生在：

~~~text
inner multipart loop fully completes
~~~

之后。

所以它限制的是：

\[
N_{\text{complete messages}}
\le 1000
\]

不是：

\[
N_{\text{frames}}
\le 1000
\]

---

# 九、一个 Multipart 可以包含很多 Frame

假设：

~~~text
message 1
  frame 1
  frame 2
  ...
  frame 50000
~~~

这在 burst 预算里仍然只是：

~~~text
i = 0
~~~

的一条完整 message。

---

# 十、所以 Burst 不是 Frame Budget

即使：

~~~text
proxy_burst_size = 1000
~~~

单次 `forward()` 仍可能处理：

~~~text
1000 × many frames
~~~

---

# 十一、也不是 Byte Budget

两条 message：

~~~text
A = 10 bytes
B = 100 MB
~~~

在 outer loop 看来：

~~~text
都算 1
~~~

所以：

\[
\text{burst count}
\]

不能直接推导：

\[
\text{bytes processed}
\]

---

# 十二、更不是 Time Budget

执行时间至少受：

- multipart frame 数；
- payload 大小；
- capture；
- destination send；
- memory/cache；
- socket pattern；
- scheduler；
- OS 状态；

影响。

因此：

> **`proxy_burst_size` 是 fairness quantum，不是 WCET bound。**

---

# 十三、这对实时系统尤其重要

不能因为源码里有：

~~~text
for i < 1000
~~~

就得出：

~~~text
这个循环执行时间有确定上界
~~~

真正硬实时 budget 应同时约束：

- message count；
- frame count；
- byte count；
- wall-clock / cycle count。

---

# 十四、为什么仍然需要 Burst

如果每次只处理一条：

~~~text
poll
forward 1
poll
forward 1
poll
forward 1
~~~

poller 往返、状态判定、branch 开销增加。

---

# 十五、如果无限 Drain

另一极端：

~~~text
while source readable:
    forward
~~~

当 frontend 永远繁忙：

~~~text
frontend
  |
  v
never return to event loop
  |
  +--> reply starvation
  +--> control starvation
~~~

所以 burst 是：

~~~text
amortize event-loop overhead
while preserving scheduling opportunities
~~~

---

# 十六、这和 NAPI Budget 是同类思想

类似：

- NAPI poll budget；
- executor callback batch；
- reactor task quantum；
- NIC completion batch；
- GPU submission batch。

共同问题：

> **一次 wakeup 后应该做多少工作再把执行机会还给调度器？**

---

# 十七、但真正公平单位必须明确

Proxy 的 quantum：

~~~text
complete multipart message
~~~

不是：

~~~text
frame
byte
CPU time
~~~

因此不同大小 message 间不是 byte-fair。

---

# 十八、Request/Reply 也不必然 Byte-fair

每一轮可能：

~~~text
request forward ≤ 1000 messages
reply   forward ≤ 1000 messages
~~~

看似 1:1。

但如果：

~~~text
request messages = 1 KB
reply messages   = 100 MB
~~~

实际 CPU/网络占用完全不对称。

---

# 十九、源码自己声明了一个负载假设

`proxy_steerable()` 注释：

~~~text
under full load
request/reply processed ratio
assumed approximately 1:1
~~~

这说明当前 fairness policy 不是：

~~~text
general weighted scheduler
~~~

而是围绕典型双向 proxy workload 设计。

---

# 二十、`forward()` 为什么必须转完整 Multipart

假设逻辑消息：

~~~text
A [more]
B [more]
C [last]
~~~

不能：

~~~text
read A
switch to opposite direction
read X
then continue B/C
~~~

即使底层 socket 能维持 frame 顺序，

上层执行语义也会把一个 transaction 拆开。

---

# 二十一、所以 Inner Loop 是 Transaction Boundary

一旦：

~~~text
first frame of message admitted
~~~

Proxy 必须：

~~~text
finish all remaining frames
~~~

再回到 outer scheduling point。

---

# 二十二、Multipart Atomicity 比 Burst Fairness 优先

即：

~~~text
transaction integrity
>
direction fairness quantum
~~~

如果一条 multipart 特别大，

它可以突破直觉上的“每轮小批量”延迟。

这是有意取舍。

---

# 二十三、这和 FQ/LB/DIST 的原则一致

前面三个调度器同样以：

~~~text
complete message
~~~

而不是 frame 作为调度原子。

Proxy 没有在更高层破坏这个 invariant。

---

# 二十四、`forward()` 为什么使用 Non-blocking Receive

~~~cpp
from_->recv(
  msg,
  ZMQ_DONTWAIT);
~~~

因为：

~~~text
blocking wait
~~~

已经由外层 poller 完成。

---

# 二十五、Wait 与 Drain 分层

~~~text
poller
→ decide when some dependency may progress

forward
→ drain currently available bounded work
~~~

内层再做阻塞 recv 会让：

~~~text
一个方向
~~~

把整个 proxy owner thread 卡住。

---

# 二十六、`EAGAIN` 为什么要看 `i`

源码：

~~~cpp
if (rc < 0) {
    if (errno == EAGAIN
        && i > 0)
        return 0;

    return -1;
}
~~~

---

# 二十七、`i > 0` 的 EAGAIN

意味着：

~~~text
至少已经完整转发过一条 message
~~~

然后 source queue 被 drain 空。

这是：

~~~text
normal end of burst
~~~

---

# 二十八、`i == 0` 的 EAGAIN

意味着：

~~~text
caller认为方向 ready
但刚进入就无法取得第一条完整工作
~~~

因此向外暴露失败/状态变化。

---

# 二十九、同一个 errno 在不同 Progress Context 下含义不同

这是一条重要 Runtime 原则：

> **错误解释不能脱离“当前调用是否已经取得进展”。**

类似逻辑常见于：

- nonblocking socket；
- completion queue；
- storage iterator；
- batch dequeue。

---

# 三十、Destination Send 并没有显式 `ZMQ_DONTWAIT`

源码：

~~~cpp
to_->send(
  msg,
  more ? ZMQ_SNDMORE : 0);
~~~

没有附加：

~~~text
ZMQ_DONTWAIT
~~~

---

# 三十一、所以 Poll Readiness 不是 Hard Real-time Proof

外层尽量在：

~~~text
destination observed POLLOUT
~~~

时才进入 forward。

但：

~~~text
poll readiness
~~~

只说明：

~~~text
某时刻 runtime 认为可以进展
~~~

不是严格 WCET 证明。

---

# 三十二、为什么仍然这样设计

因为 socket send 本身已经拥有：

- HWM；
- multipart；
- pattern-specific admission；
- timeout；
- error semantics。

Proxy 不想重新实现一套：

~~~text
partial-send state machine
~~~

它选择复用 socket contract。

---

# 三十三、Proxy Backpressure 的核心不是额外 Buffer

一个错误设计：

~~~text
source readable
→ keep recv forever
→ destination full
→ store in proxy-local queue
~~~

会产生：

~~~text
unbounded intermediate buffering
~~~

---

# 三十四、libzmq Proxy 采用 Dependency Gating

逻辑上：

~~~text
request direction may progress
=
frontend readable
AND
backend writable
~~~

reply：

~~~text
backend readable
AND
frontend writable
~~~

---

# 三十五、输出堵塞时不再把对应输入当“有用工作”

也就是：

~~~text
destination blocked
→ stop waking just because source still readable
~~~

压力继续保留在：

~~~text
source socket / upstream Pipe
~~~

---

# 三十六、这就是 Backpressure Propagation

~~~text
downstream HWM
    ↓
backend not writable
    ↓
proxy stops draining frontend
    ↓
frontend queue fills
    ↓
pressure propagates upstream
~~~

---

# 三十七、为什么这一层仍需要 Backpressure 逻辑

有人会问：

> Pipe 已经有 HWM，为什么 Proxy 还要处理？

因为 Pipe 只知道：

~~~text
one socket / one endpoint local capacity
~~~

Proxy 还必须决定：

~~~text
何时继续从另一个独立 socket 消费
~~~

---

# 三十八、底层 Capacity 与上层 Consumption Policy 不同

Pipe：

~~~text
can destination accept?
~~~

Proxy：

~~~text
given destination state,
should I drain source?
~~~

这是两个层级。

---

# 三十九、旧 Poll 实现为什么把 POLLIN 与 POLLOUT 分开

Fallback：

~~~cpp
poll(POLLIN, blocking);

poll(POLLOUT, timeout=0);
~~~

源码注释很直接：

~~~text
combining POLLIN + POLLOUT
in blocking poll
can max CPU
because POLLOUT is ready most of time
~~~

---

# 四十、为什么 POLLOUT 特别危险

多数正常 socket：

~~~text
output capacity available
~~~

是长期状态。

所以：

~~~text
POLLOUT
~~~

经常：

~~~text
always ready
~~~

---

# 四十一、把它放进 Blocking Wait Set

会形成：

~~~text
poll
→ POLLOUT immediately ready
→ loop
→ poll
→ POLLOUT immediately ready
~~~

即使：

~~~text
没有任何 input work
~~~

也会 spin。

---

# 四十二、Reactor 中 Interest Registration 应该是需求驱动的

通用原则：

> **不要永久等待一个长期为真的 readiness condition。**

POLLOUT 真正有意义的时机通常是：

~~~text
之前因为不可写而停住
现在等它恢复
~~~

---

# 四十三、新 Poller 实现进一步把这个原则状态机化

它预构造多套 wait set：

~~~text
poller_all
poller_in
poller_receive_blocked
poller_send_blocked
poller_both_blocked
poller_frontend_only
poller_backend_only
~~~

---

# 四十四、为什么不每轮动态 Add/Remove Interest

可以每次：

~~~text
poller.modify(...)
~~~

但会不断重建/修改 registration。

libzmq 选择：

~~~text
pre-build several stable pollers
+
switch poller_wait pointer
~~~

---

# 四十五、这是“预编译 Wait Policy”

每个 poller 对应一种：

~~~text
dependency state
~~~

运行时只：

~~~text
poller_wait = another_set
~~~

---

# 四十六、初始 `poller_wait = poller_in`

即：

~~~text
blocking wait
只关心 input
~~~

避免无意义 POLLOUT 唤醒。

---

# 四十七、Wake 后为什么还要再 Poll `poller_all`

流程：

~~~cpp
poller_wait->wait(
    events,
    nevents,
    -1);

poller_all->wait(
    events,
    nevents,
    0);
~~~

---

# 四十八、这是 Two-phase Readiness Collection

第一阶段：

~~~text
minimal blocking dependency set
~~~

只负责：

~~~text
sleep until something useful may have changed
~~~

第二阶段：

~~~text
nonblocking full snapshot
~~~

收集当前：

~~~text
POLLIN + POLLOUT + control
~~~

真实状态。

---

# 四十九、为什么不直接使用第一阶段返回的 Event

因为：

~~~text
wait set 被有意裁剪
~~~

它可能只监听：

~~~text
恢复条件
~~~

而不是所有当前 work。

Wake 后需要重新获得：

~~~text
complete current readiness snapshot
~~~

---

# 五十、这和 Doorbell + Drain 模式很像

Mailbox：

~~~text
doorbell wakes
→ drain memory queue
~~~

Proxy：

~~~text
minimal condition wakes
→ snapshot all readiness
~~~

共同原则：

> **Wakeup 只证明“值得重新检查”，不一定携带完整工作集合。**

---

# 五十一、Request Direction 的 Dependency

~~~text
frontend_in
AND
(backend_out
 OR frontend==backend)
~~~

正常两个 socket：

~~~text
frontend POLLIN
+
backend POLLOUT
~~~

---

# 五十二、Reply Direction

~~~text
backend_in
AND
frontend_out
~~~

同一 socket 模式下：

~~~text
second direction intentionally suppressed
~~~

---

# 五十三、为什么要记录 `request_processed`

它不是简单：

~~~text
frontend_in
~~~

而是：

~~~text
当前调用真的成功 forward 过
~~~

---

# 五十四、Progress Event 与 Readiness Event 不同

~~~text
readiness
→ may be able to work

processed
→ work actually completed
~~~

Reply 方向使用镜像状态 `reply_processed`；两者记录的是实际 forward 是否成功，而不是单纯的 `ZMQ_POLLIN/ZMQ_POLLOUT` readiness。

Wait-set 恢复应该依据：

~~~text
actual progress
~~~

而不是只依据之前观察到的 readiness。

---

# 五十五、成功 Request 后 Wait Set 怎样放宽

例如：

~~~text
poller_both_blocked
~~~

说明两个方向都曾受输出依赖阻塞。

request 成功：

~~~text
backend capacity 已恢复
~~~

因此：

~~~text
both_blocked
→ send_blocked
~~~

只保留另一个方向的约束。

---

# 五十六、如果原来只是 Receive-blocked

request 成功：

~~~text
receive_blocked
→ poller_in
~~~

恢复普通：

~~~text
wait for inputs
~~~

---

# 五十七、Reply 成功是镜像路径

~~~text
both_blocked
→ receive_blocked

send_blocked
→ poller_in
~~~

---

# 五十八、如果当前调用没有任何 Progress

代码开始推导：

~~~text
为什么没有 forward?
~~~

常见原因：

~~~text
input present
but corresponding output absent
~~~

---

# 五十九、Frontend 有 Input，但 Backend 无容量

则不应继续：

~~~text
block waiting frontend POLLIN
~~~

因为 frontend 已经有数据。

再被它唤醒没有新信息。

---

# 六十、此时真正缺的 Condition 是

~~~text
backend becomes writable
~~~

所以 wait set 会：

~~~text
suppress frontend POLLIN
~~~

转向输出恢复条件。

---

# 六十一、这叫 Dependency-directed Waiting

不要等待：

~~~text
已经满足的条件
~~~

而要等待：

~~~text
阻止 progress 的那个条件
~~~

---

# 六十二、一个方向的 Progress Predicate

可以写成：

\[
P_{req}
=
I_f \land O_b
\]

其中：

- \(I_f\)：frontend 有输入；
- \(O_b\)：backend 有输出容量。

---

# 六十三、如果 \(I_f=1, O_b=0\)

下一次值得阻塞等待：

\[
O_b:0\rightarrow1
\]

而不是：

\[
I_f
\]

因为 \(I_f\) 已经成立。

---

# 六十四、这和 Condition Variable 的谓词思维一致

等待的不是：

~~~text
某个事件名字
~~~

而是：

~~~text
让 predicate 从 false 变 true 的缺失条件
~~~

---

# 六十五、`poller_both_blocked` 是什么

两方向都有 pending input，

但两边 destination 都无容量：

~~~text
frontend_in = 1
backend_out = 0

backend_in = 1
frontend_out = 0
~~~

---

# 六十六、此时只等待两个 POLLOUT

~~~text
frontend POLLOUT
backend  POLLOUT
~~~

因为两边输入都已经不缺。

---

# 六十七、`poller_frontend_only / backend_only`

用于更特殊组合：

~~~text
一端同时需要等待
input 或 output 的变化
而另一端当前没有有用依赖
~~~

它们是 wait-policy 的压缩状态。

---

# 六十八、多套 Poller 本质上是显式状态机

状态不是 enum：

~~~text
NORMAL
REQ_BLOCKED
REP_BLOCKED
BOTH_BLOCKED
...
~~~

而是：

~~~text
poller_wait pointer identity
~~~

编码。

---

# 六十九、Pointer 本身就是 Scheduling State

~~~text
poller_wait == poller_in
~~~

就代表：

~~~text
normal input-driven state
~~~

其它 pointer 代表不同 dependency mask。

---

# 七十、优点

不需要额外：

~~~text
enum + switch + rebuild poll mask
~~~

当前 wait object 自带：

~~~text
interest set
~~~

---

# 七十一、代价

必须保证：

- 所有 poller 都正确初始化；
- aliasing 分支和 control 分支一致；
- cleanup 能安全处理 NULL；
- transition 只指向已存在对象。

这会引出后面的固定源码风险。

---

# 七十二、为什么 Poller 对象不放 Stack

源码注释：

~~~text
these pollers
take more than 900 kB of stack
~~~

Windows 默认线程栈约：

~~~text
1 MB
~~~

会直接逼近/突破上限。

---

# 七十三、所以使用 Heap Allocation

~~~cpp
new socket_poller_t
~~~

而不是局部：

~~~cpp
socket_poller_t poller_all;
~~~

---

# 七十四、这是 Runtime 内存设计很典型的一课

对象“语义上局部”：

~~~text
function-local lifetime
~~~

不代表：

~~~text
适合放 stack
~~~

还必须看：

~~~text
object footprint
~~~

---

# 七十五、大对象局部变量可能造成非业务崩溃

尤其：

- Windows 1 MB thread stack；
- RT thread 手动小栈；
- embedded runtime；
- coroutine/fiber stack。

---

# 七十六、Stack Budget 本身就是系统资源

实时/嵌入式系统不只要算：

~~~text
heap bytes
~~~

也要算：

~~~text
worst-case stack depth
+
large local objects
~~~

---

# 七十七、Capture 是怎么插入数据面的

每个 frame：

~~~text
recv source
   |
   v
capture(copy)
   |
   v
send destination
~~~

注意顺序：

> **Capture 发生在主 destination send 之前。**

---

# 七十八、`msg_t::copy()` 为什么不会破坏原消息

~~~cpp
ctrl.copy(*msg)
~~~

构造另一个 message representation。

对于大 refcounted payload：

~~~text
small envelope copy
+
shared payload reference
~~~

不一定复制整块大 buffer。

---

# 七十九、但 Capture 仍然不是 Free

至少增加：

- `msg_t` ownership；
- refcount operation；
- capture socket queue；
- capture routing；
- capture transport；
- capture application processing。

---

# 八十、更关键：Capture 在 Critical Path

~~~cpp
rc = capture(...);
if (rc < 0)
    return -1;

rc = to_->send(...);
~~~

capture 失败：

~~~text
destination send 根本不会执行
~~~

---

# 八十一、所以 Observability 会改变 Data-plane Failure Surface

一个慢/失败的 capture path：

~~~text
可以让整个 proxy forward 返回失败
~~~

它不是完全 out-of-band。

---

# 八十二、这条原则很重要

> **任何同步插入主数据面的 observability hook，都必须被视为业务 critical path 的一部分。**

---

# 八十三、日志/Trace 也一样

如果：

~~~text
process packet
→ synchronous log
→ continue
~~~

那么 logger 的：

- lock；
- disk；
- queue；
- formatter；

都进入业务延迟路径。

---

# 八十四、Capture 成功不证明 Destination 成功

顺序：

~~~text
capture send success
        ↓
destination send
        ↓
may fail
~~~

所以 capture 可能记录到：

~~~text
一个随后没有成功发到主 destination 的 frame
~~~

---

# 八十五、因此 Capture 语义更接近

~~~text
observed/attempted forwarding stream
~~~

而不是：

~~~text
authoritative delivered stream
~~~

---

# 八十六、如果需要“成功交付审计”

应该在：

~~~text
destination success / downstream ACK
~~~

之后建立独立语义。

不能直接把 capture feed 当成：

~~~text
delivery ledger
~~~

---

# 八十七、为什么 Capture 必须保留 `SNDMORE`

~~~cpp
capture_->send(
  &ctrl,
  more ? ZMQ_SNDMORE : 0);
~~~

否则原本：

~~~text
[A more][B more][C last]
~~~

会在 capture 侧变成三个独立 message。

---

# 八十八、Observability 必须保留 Protocol Boundary

不是只复制：

~~~text
payload bytes
~~~

还必须复制：

~~~text
message framing semantics
~~~

---

# 八十九、Statistics 的 Count 单位是什么

每个 frame：

~~~cpp
recving.count += 1;
sending.count += 1;
~~~

所以：

~~~text
count
=
message part / frame count
~~~

不是：

~~~text
complete multipart message count
~~~

---

# 九十、为什么这很容易误解

同一篇源码里：

~~~text
proxy_burst_size
~~~

的 outer quantum 是：

~~~text
complete messages
~~~

而 stats：

~~~text
count
~~~

是：

~~~text
frames
~~~

两种 count 单位不同。

---

# 九十一、指标名没有单位就很危险

看到：

~~~text
count = 100000
~~~

必须问：

- packet？
- frame？
- complete message？
- request？
- sample？

否则性能图可以被完全误读。

---

# 九十二、Bytes 统计什么时候增加

source recv 成功后：

~~~text
recv.count++
recv.bytes += nbytes
~~~

destination send 成功后：

~~~text
send.count++
send.bytes += nbytes
~~~

---

# 九十三、所以 Recv 与 Send Counters 可以分叉

如果：

~~~text
capture or destination send fails
~~~

已经：

~~~text
recv count incremented
~~~

但：

~~~text
send count未必增加
~~~

---

# 九十四、这个差值本身可以反映异常路径

但源码没有自动把它解释成：

~~~text
drop metric
~~~

应用如果读取 statistics，

应该理解计数点的位置。

---

# 九十五、STATISTICS 为什么是 8 个 Frame

数据结构：

~~~cpp
struct stats_socket {
    uint64_t count;
    uint64_t bytes;
};

struct stats_endpoint {
    stats_socket send;
    stats_socket recv;
};

struct stats_proxy {
    stats_endpoint frontend;
    stats_endpoint backend;
};
~~~

维度：

\[
2_{\text{endpoint}}
\times
2_{\text{direction}}
\times
2_{\text{metric}}
=8
\]

---

# 九十六、Flatten 顺序

源码：

~~~text
frontend recv count
frontend recv bytes
frontend send count
frontend send bytes
backend  recv count
backend  recv bytes
backend  send count
backend  send bytes
~~~

---

# 九十七、为什么用 Multipart Reply

仍然复用普通 ZeroMQ message contract。

不需要：

~~~text
自定义二进制 struct ABI
~~~

这样跨语言 API 更容易消费。

---

# 九十八、Control Plane 是普通 Socket

Steerable proxy 支持：

~~~text
PAUSE
RESUME
TERMINATE
STATISTICS
~~~

通过：

~~~cpp
control_->recv(
  &cmsg,
  ZMQ_DONTWAIT);
~~~

处理。

---

# 九十九、Control Command 不是共享 Atomic Flag

可以跨：

- thread；
- process；
- language binding；

传递。

---

# 一百、优点

~~~text
same message abstraction
same poller
same transport capability
same test surface
~~~

---

# 一百零一、代价

控制命令：

~~~text
不是硬中断
~~~

仍要经过：

~~~text
socket readiness
→ proxy event loop
→ handle_control
~~~

---

# 一百零二、PAUSE 只是 Forwarding State

状态：

~~~cpp
enum proxy_state_t {
    active,
    paused,
    terminated
};
~~~

PAUSE：

~~~text
active → paused
~~~

---

# 一百零三、Pause 不关闭任何 Endpoint

不会：

- close frontend；
- close backend；
- terminate Session；
- discard Pipe；
- destroy proxy。

---

# 一百零四、所以 Pause 是 Scheduling Gate

~~~text
connections alive
queues alive
control alive
forwarding disabled
~~~

---

# 一百零五、为什么 Control 必须在所有 Wait Policy 中保持可见

当业务方向被 HWM 卡住时：

~~~text
proxy may switch away from input-driven poller
~~~

如果 control 不在新的 wait set：

~~~text
TERMINATE
~~~

可能永远叫不醒 proxy。

---

# 一百零六、源码意图非常明确

control 被添加到：

~~~text
poller_all
poller_in
poller_receive_blocked
poller_send_blocked
poller_both_blocked
poller_frontend_only
poller_backend_only
~~~

---

# 一百零七、这是一条非常重要的系统原则

> **控制面必须在数据面背压状态下仍然有进展路径。**

---

# 一百零八、Pipe Termination Delimiter 也遵循同一原则

前面看到：

~~~text
business queue full
~~~

不能永久阻止：

~~~text
termination delimiter
~~~

这里同样：

~~~text
data direction blocked
~~~

不能阻止：

~~~text
TERMINATE/RESUME
~~~

---

# 一百零九、Control-plane Liveness 应独立于 Data-plane Saturation

机器人系统中：

- E-stop；
- shutdown；
- mode switch；
- reset；
- diagnostics；

尤其不能被：

~~~text
camera backlog
~~~

饿死。

---

# 一百一十、REP Control 为什么必须回空 Reply

如果 control socket 本身是：

~~~text
ZMQ_REP
~~~

它拥有自己的 FSM：

~~~text
recv
→ send
→ recv
→ send
~~~

---

# 一百一十一、即使命令没有 Payload Reply

例如：

~~~text
PAUSE
~~~

也必须：

~~~text
send empty message
~~~

完成 REP duty。

---

# 一百一十二、内部控制用途不会取消 Socket Pattern Contract

这是非常容易忽略的边界：

> **拿一个 protocol object 做“内部用途”，并不会关闭它原本的协议状态机。**

---

# 一百一十三、STATISTICS 不走空 Reply

因为 8-frame statistics 本身已经构成：

~~~text
REP reply
~~~

所以函数发送完后直接 return。

---

# 一百一十四、未知 Control Command

如果 payload 不匹配四个已知字符串：

~~~text
state unchanged
~~~

如果 control 是 REP：

~~~text
仍然发送 empty reply
~~~

因为 REP duty 仍必须满足。

---

# 一百一十五、Control Protocol 是 Single-message Command Protocol

源码按当前 `cmsg.data()/size()` 直接比较整个命令。

所以它期望：

~~~text
one command message
~~~

而不是复杂 streaming control transaction。

---

# 一百一十六、`frontend == backend` 为什么是特殊拓扑

API 有两个参数：

~~~text
frontend
backend
~~~

不代表两个 pointer 一定不同。

测试中确实存在：

~~~text
same REP socket
used as both frontend and backend
~~~

的普通 proxy 用法。

---

# 一百一十七、同一个 Socket 不能当两个独立 Event Source

如果：

~~~text
frontend == backend
~~~

同一 readiness event：

~~~text
不能解释两次
~~~

否则可能重复处理。

---

# 一百一十八、源码先判断 Frontend

event loop：

~~~text
if socket == frontend
    update frontend state
else if socket == backend
    update backend state
~~~

所以同一 pointer：

~~~text
永远走 frontend branch
~~~

`backend_in` 保持 false。

---

# 一百一十九、第二方向因此被自然抑制

request path：

~~~text
covers same-socket forwarding
~~~

reply path：

~~~text
backend_in == false
→ skipped
~~~

---

# 一百二十、Poller Allocation 也做了 Alias Specialization

当：

~~~text
frontend == backend
~~~

源码不分配：

- `poller_send_blocked`；
- `poller_both_blocked`；
- `poller_frontend_only`；
- `poller_backend_only`。

因为这些状态在单对象拓扑里冗余。

---

# 一百二十一、这里只保留

~~~text
poller_all
poller_in
poller_receive_blocked
~~~

---

# 一百二十二、这是 Aliasing-aware State-space Reduction

不是先构造完整两端状态机，

再在运行时不断判断：

~~~text
if same socket
~~~

而是在初始化阶段直接删掉不存在的状态。

---

# 一百二十三、固定源码存在一个 Alias + Control 静态风险

随后：

~~~cpp
if (control_) {
    ...
    poller_send_blocked->add(...);
    poller_both_blocked->add(...);
    poller_frontend_only->add(...);
    poller_backend_only->add(...);
}
~~~

这几条调用没有在这里再次判断：

~~~text
frontend_equal_to_backend
~~~

---

# 一百二十四、但 Same-socket 分支中这些 Pointer 是 NULL

初始化明确：

~~~text
poller_send_blocked = NULL
poller_both_blocked = NULL
poller_frontend_only = NULL
poller_backend_only = NULL
~~~

且只在：

~~~text
frontend != backend
~~~

时分配。

---

# 一百二十五、因此组合条件值得警惕

固定源码静态路径：

~~~text
frontend == backend
AND
control != NULL
        ↓
unallocated optional pollers
        ↓
control registration block
        ↓
unconditional ->add()
~~~

这形成：

~~~text
null-object dereference risk
~~~

---

# 一百二十六、需要精确区分已覆盖场景

本地测试里有：

~~~text
test_proxy_single_socket
~~~

覆盖：

~~~text
frontend == backend
+
ordinary zmq_proxy
+
control == NULL
~~~

---

# 一百二十七、这不能推出 Steerable Same-socket 也被覆盖

另有：

~~~text
test_proxy_steerable
~~~

覆盖 control，

但使用：

~~~text
distinct frontend/backend
~~~

---

# 一百二十八、所以静态结论应该限定为

> **固定源码中，`frontend==backend` 与非空 `control_` 的组合路径存在明显的 NULL poller 调用风险；现有本地测试检索到的 same-socket 与 steerable 场景分别覆盖了两个条件，但没有证明这个交叉组合安全。**

不需要把它扩大成：

~~~text
所有 proxy 都有问题
~~~

---

# 一百二十九、正确修复方向

初始化 control registration 时也按 topology：

~~~text
if frontend != backend:
    register control in all 7 pollers
else:
    register only in 3 actually allocated pollers
~~~

---

# 一百三十、或者使用统一 Optional Registration Helper

例如概念上：

~~~cpp
add_if_present(
  poller,
  control,
  ZMQ_POLLIN);
~~~

确保：

~~~text
allocation topology
~~~

与：

~~~text
later registration topology
~~~

来自同一条件源。

---

# 一百三十一、这里的根因不是“忘判 NULL”这么简单

更深的模式是：

> **状态空间裁剪以后，所有后续针对完整状态空间的批量操作都必须同步裁剪。**

---

# 一百三十二、这在很多系统里会发生

例如：

- optional subsystem；
- compile-time feature；
- single-device fast path；
- NUMA-disabled path；
- no-GPU fallback；
- read-only mode。

一旦某些对象：

~~~text
不创建
~~~

后面的：

- registration；
- cleanup；
- iteration；
- metrics；

都必须认同同一拓扑。

---

# 一百三十三、好消息：Cleanup Macro 本身可以 Delete NULL

~~~text
delete nullptr
~~~

在 C++ 中安全。

真正风险发生在：

~~~text
dereference before cleanup
~~~

---

# 一百三十四、Control 在所有 Poller 中注册本来是正确目标

设计目标没有错：

~~~text
regardless of data wait state
control should wake owner
~~~

问题是：

~~~text
aliasing topology
~~~

和 registration loop 没有统一。

---

# 一百三十五、Fallback Poll 路径没有多 Poller NULL 问题

无 `ZMQ_HAVE_POLLER` 时：

~~~text
one input poll array
+
one output poll array
~~~

没有预构造七个 poller object。

---

# 一百三十六、Fallback 算法结构

~~~text
blocking:
POLLIN(frontend, backend, control)

then nonblocking:
POLLOUT(frontend, backend)

then:
if input && corresponding output
    forward
~~~

---

# 一百三十七、为什么先 Blocking Input

因为：

~~~text
input is work arrival
~~~

而 output writable 往往长期为真。

---

# 一百三十八、为什么 Output Poll timeout=0

它只是：

~~~text
current capacity snapshot
~~~

不是主要 wake source。

---

# 一百三十九、Fallback 的局限

它没有新 poller 版本那样：

~~~text
blocked dependency-specific wait-set switching
~~~

所以表达力更简单。

源码也明确带着：

~~~text
full-load request/reply 1:1
~~~

的假设。

---

# 一百四十、新 Poller 版本本质上更接近 Dependency Scheduler

它不是：

~~~text
每轮固定 poll same mask
~~~

而是：

~~~text
根据上轮没有进展的原因
选择下一轮阻塞条件
~~~

---

# 一百四十一、这可以抽象成状态转移

普通：

~~~text
WAIT_INPUT
~~~

request blocked：

~~~text
WAIT_BACKEND_CAPACITY
~~~

reply blocked：

~~~text
WAIT_FRONTEND_CAPACITY
~~~

both blocked：

~~~text
WAIT_ANY_OUTPUT_CAPACITY
~~~

特殊单边：

~~~text
WAIT_FRONTEND_ONLY
WAIT_BACKEND_ONLY
~~~

---

# 一百四十二、状态变化由“缺哪个依赖”驱动

不是：

~~~text
time
~~~

也不是：

~~~text
round robin counter
~~~

而是：

~~~text
progress predicate
~~~

---

# 一百四十三、这是一种 Runtime Dependency Graph

每个方向：

~~~text
input resource
+
output capacity resource
~~~

组成一条 edge。

---

# 一百四十四、Request Edge

~~~text
frontend input
   |
   +---- requires ----> backend output capacity
~~~

---

# 一百四十五、Reply Edge

~~~text
backend input
   |
   +---- requires ----> frontend output capacity
~~~

---

# 一百四十六、Poller State 就是在选择当前阻塞的 Dependency

这比：

~~~text
busy check four booleans
~~~

更节能。

---

# 一百四十七、PAUSE 时为什么仍然不会退出 Loop

外层：

~~~text
while state != terminated
~~~

而 forwarding：

~~~text
if state == active
~~~

所以 paused：

~~~text
仍然 wait
仍然 process control
不 forward data
~~~

---

# 一百四十八、Pause 后数据可能继续在 Socket 内排队

因为：

~~~text
connection remains alive
~~~

底层 Session/Engine 仍可能推进网络收包，

直到 HWM/backpressure 生效。

---

# 一百四十九、所以 Pause 不是“停止网络”

它是：

~~~text
停止 proxy application-level consumption/forwarding
~~~

---

# 一百五十、这会自然把 Backpressure 推回 Peer

如果长期 paused：

~~~text
proxy stops draining
→ socket queues fill
→ Pipe HWM
→ upstream pressure
~~~

---

# 一百五十一、Pause 因此也参与 Flow-control

即使它的目的可能是：

~~~text
administrative control
~~~

结果仍会改变数据面容量。

---

# 一百五十二、TERMINATE 做什么

只设置：

~~~text
state = terminated
~~~

然后外层 while：

~~~text
exits
~~~

---

# 一百五十三、它不是 Context-wide Shutdown

不会自动：

- terminate all sockets；
- close Context；
- wait Reaper；
- drain linger。

这些属于后面的 socket/context lifecycle。

---

# 一百五十四、Proxy Termination 与 Socket Termination 要分层

~~~text
proxy loop termination
≠
socket object destruction
≠
Pipe termination
≠
Context termination
~~~

---

# 一百五十五、这也是为什么下一章需要 Linger/Termination

Proxy 只停止：

~~~text
forwarding coordinator
~~~

之后 socket 如何：

~~~text
drain or drop pending work
~~~

由更底层生命周期协议决定。

---

# 一百五十六、XSUB/XPUB Proxy 为什么特殊

普通 request/reply：

~~~text
frontend → backend
backend  → frontend
~~~

两边都像业务 data。

---

# 一百五十七、XSUB/XPUB 中两个方向语义不同

一个方向：

~~~text
published data
~~~

另一个方向：

~~~text
subscribe/cancel control
~~~

---

# 一百五十八、Proxy 本身不需要理解 Topic Trie

因为：

~~~text
XSUB / XPUB socket pattern
~~~

已经实现：

- local trie；
- mtrie；
- matching；
- replay。

Proxy 只负责：

~~~text
把两个 socket 的消息完整转发
~~~

---

# 一百五十九、这是 Composition 的力量

高层 Proxy 不需要知道：

~~~text
subscribe command internal format
~~~

仍可以正确转发控制流，

前提是：

~~~text
preserve message framing
~~~

---

# 一百六十、为什么 XSUB 端不能随便吞 Duplicate Subscribe

转发设备上游可能开启：

~~~text
XPUB_VERBOSE
~~~

需要观察：

~~~text
每个下游订阅事件
~~~

所以中间 XSUB 的本地去重不能擅自改变：

~~~text
wire observability semantics
~~~

这一点前一章已经从 XSUB 源码验证。

---

# 一百六十一、Proxy 只保证“透明转发”，不保证持久化

它没有：

- durable log；
- disk journal；
- consumer offsets；
- replication；
- consensus；
- exactly-once transaction。

---

# 一百六十二、所以它不是 Kafka / RabbitMQ 类 Broker

更准确：

~~~text
transient in-memory forwarding device
~~~

---

# 一百六十三、适合的场景

- worker broker；
- topology bridge；
- XSUB/XPUB forwarder；
- capture tap；
- local fan-in/fan-out；
- protocol composition point。

---

# 一百六十四、不适合直接承担

- durable audit；
- financial transaction；
- guaranteed command history；
- crash-recoverable queue。

---

# 一百六十五、机器人系统怎么使用

例如：

~~~text
camera nodes
    |
   XSUB
    |
   proxy
    |
   XPUB
    |
 perception / logger / UI
~~~

Proxy 可以作为：

~~~text
telemetry bridge
~~~

---

# 一百六十六、为什么不放 1 kHz 控制主线程

一次 loop 的工作量依赖：

- event count；
- burst；
- multipart size；
- capture；
- socket send；
- control；
- allocator/cache。

没有固定 WCET。

---

# 一百六十七、合理分层

~~~text
1 kHz RT control thread
    |
    +-- bounded mailbox / latest-state handoff
    |
network/proxy thread
    |
    +-- routing
    +-- telemetry
    +-- capture
    +-- external clients
~~~

---

# 一百六十八、不要把 High Throughput 当 Hard Real-time

Proxy 的很多设计：

~~~text
burst batching
shared payload
event-driven wait
~~~

非常高效。

但：

~~~text
高吞吐
~~~

不等于：

~~~text
确定性执行时间
~~~

---

# 一百六十九、如果要做更严格实时 Proxy

至少要重新约束：

- max frames/message；
- max bytes/message；
- max burst bytes；
- max wall time per turn；
- nonblocking output；
- capture isolation；
- bounded control handling；
- allocator behavior。

---

# 一百七十、Capture 最好移出硬实时 Critical Path

一种更稳健设计：

~~~text
main forwarding
    |
    +-- bounded nonblocking telemetry queue
            |
            v
      capture/logger thread
~~~

---

# 一百七十一、代价是允许 Capture Loss

如果观测队列满：

~~~text
drop telemetry
~~~

而不是：

~~~text
stall business forwarding
~~~

这是 observability policy 的选择。

---

# 一百七十二、如果 Capture 必须无损

那就必须接受：

~~~text
capture backpressure
becomes business backpressure
~~~

没有免费午餐。

---

# 一百七十三、控制面则相反

对于：

- TERMINATE；
- E-stop；
- reset；

通常更希望：

~~~text
control liveness > data throughput
~~~

因此它应该拥有独立、始终可达的 progress path。

---

# 一百七十四、Proxy 的设计把两类 Auxiliary Path 区分得很有启发

Capture：

~~~text
同步数据面 hook
~~~

Control：

~~~text
独立 wakeup source
~~~

二者完全不同。

---

# 一百七十五、一个通用设计矩阵

~~~text
             may block data?   must wake under data stall?
capture         yes/depends              no
control             no                   yes
~~~

工程上应该显式定义。

---

# 一百七十六、Error Propagation 也要分层

`forward()` 任意：

- source recv error；
- getsockopt error；
- capture error；
- destination send error；

都会：

~~~text
return -1
~~~

一路使 proxy 退出。

---

# 一百七十七、Proxy 没有自动 per-frame Retry Queue

它选择：

~~~text
surface error
~~~

而不是：

~~~text
偷偷保留 partially forwarded transaction
~~~

---

# 一百七十八、这简化了 Ownership

因为没有新增：

~~~text
proxy-local persistent pending message queue
~~~

---

# 一百七十九、代价是调用方要理解 Socket Error Semantics

例如：

~~~text
send failure
~~~

是否重试整条业务 transaction，

由更高层决定。

---

# 一百八十、Proxy 的性能优化主要不在算法 Big-O

核心动作都是：

~~~text
poll
recv
copy handle
send
~~~

瓶颈更多来自：

- wakeup；
- syscalls；
- queue；
- cache；
- refcount；
- serialization；
- memory bandwidth。

---

# 一百八十一、Burst 优化的是 Fixed Overhead Amortization

如果每次 wake 只做一个 message：

\[
C
=
N(C_{wake}+C_{msg})
\]

批量后：

\[
C
\approx
\frac{N}{B}C_{wake}
+
NC_{msg}
\]

其中 \(B\) 是 burst 中 complete-message 数。

---

# 一百八十二、但 B 越大，Tail Latency 越差

一个方向连续工作更久：

~~~text
other direction
control
~~~

获得调度机会更晚。

---

# 一百八十三、所以 Batch Size 是 Throughput/Latency Knob

这不是 ZeroMQ 独有。

适用于：

- packet processing；
- log consumer；
- GPU batching；
- database write batch；
- inference server。

---

# 一百八十四、Forwarding Loop 的核心不变量

第一：

> **一旦开始一条 multipart，就完整转完这条 message。**

第二：

> **一个方向必须同时满足 input-ready 与 opposite-output-capacity 才有 progress。**

第三：

> **输出堵塞时，不继续把已经满足的 source-input 当成 blocking wake condition。**

第四：

> **burst 只限制完整 message 数，不是 byte/frame/time bound。**

第五：

> **control 必须在数据面堵塞与 paused 状态下仍有 wakeup 路径。**

第六：

> **capture 处于主数据面 critical path，不能假定它是“免费旁路”。**

第七：

> **same-socket aliasing 必须压缩状态空间，不能把一个对象解释成两个独立 endpoint。**

第八：

> **readiness 与 actual progress 必须区分；wait policy 根据实际缺失依赖调整。**

第九：

> **stats count 的单位是 frame/message-part，而 burst quantum 是完整 message。**

第十：

> **proxy loop termination 与 socket/context termination 是不同生命周期层。**

---

# 一百八十五、完整 Wait-set 心智图

~~~text
                         +------------------+
                         |    poller_in     |
                         | wait all POLLIN  |
                         +---------+--------+
                                   |
                       wake + full readiness snapshot
                                   |
                    +--------------+--------------+
                    |                             |
          req predicate true            reply predicate true
                    |                             |
                    v                             v
             forward request                forward reply
                    |                             |
             actual progress                 actual progress
                    |                             |
                    +-------------+---------------+
                                  |
                         relax wait policy

no progress:
-----------------------------------------------

frontend input present
backend output absent
        |
        v
 suppress frontend POLLIN
 wait backend capacity

backend input present
frontend output absent
        |
        v
 suppress backend POLLIN
 wait frontend capacity

both blocked
        |
        v
 wait only useful POLLOUT recovery
~~~

---

# 一百八十六、完整 Data Path

~~~text
source socket
     |
     | recv DONTWAIT
     v
    msg_t
     |
     +-------- copy --------> capture
     |                         |
     |                         +-- may fail/block
     |
     +-------- send --------> destination
                               |
                               +-- stats send++ only after success
~~~

---

# 一百八十七、完整 Control Path

~~~text
control socket
      |
      v
POLLIN in every valid wait policy
      |
      v
handle_control
      |
      +-- PAUSE      → paused
      |
      +-- RESUME     → active
      |
      +-- TERMINATE  → terminated
      |
      +-- STATISTICS → 8-frame reply

if control type == REP
and command is not STATISTICS:
      |
      v
empty reply
to satisfy REP FSM
~~~

---

# 一百八十八、完整 Backpressure Path

~~~text
backend Pipe HWM
      |
      v
backend no POLLOUT
      |
      v
request predicate false
      |
      v
proxy stops draining frontend
      |
      v
frontend queue accumulates
      |
      v
frontend HWM propagates pressure upstream
~~~

没有：

~~~text
unbounded proxy-local queue
~~~

---

# 一百八十九、固定源码 Same-socket + Control 风险图

~~~text
frontend == backend
        |
        v
skip allocating:
  poller_send_blocked
  poller_both_blocked
  poller_frontend_only
  poller_backend_only
        |
        v
control != NULL
        |
        v
control registration block
calls ->add() on all poller pointers
        |
        v
NULL dereference risk
~~~

这是固定源码的静态路径问题，不改变普通：

~~~text
single-socket + control=NULL
~~~

proxy 已有专门测试这一事实。

---

# 一百九十、对自己设计 Runtime 的迁移建议

如果写一个双向 forwarding runtime，可以先定义：

~~~text
DirectionState:
    input_ready
    output_ready
    progress_made
~~~

再定义：

~~~text
WaitPolicy
=
missing dependency set
~~~

而不是：

~~~text
forever poll every possible event
~~~

---

# 一百九十一、进一步把 Budget 做成多维

比单：

~~~text
max_messages
~~~

更稳健：

~~~text
max_messages
max_frames
max_bytes
max_cycles / max_duration
~~~

满足任意上限就：

~~~text
yield
~~~

---

# 一百九十二、Capture Policy 也应该显式

可选：

~~~text
SYNC_LOSSLESS
→ capture can backpressure business

ASYNC_BOUNDED
→ business isolated
→ capture may drop

SAMPLE
→ only some traffic

METADATA_ONLY
→ avoid large payload duplication
~~~

---

# 一百九十三、Control Policy 应与 Data Policy 分离

例如：

~~~text
data queue full
~~~

仍应允许：

~~~text
shutdown
resume
reset
~~~

进入。

---

# 一百九十四、不要把所有消息都塞进同一个 Priority-less Queue

否则：

~~~text
large telemetry burst
~~~

可能延迟：

~~~text
critical control
~~~

---

# 一百九十五、Proxy 与下一层 Lifecycle 的边界

Proxy 自己最终只：

~~~text
stop event loop
~~~

它没有回答：

~~~text
pending outbound messages
close 时发完还是丢？
~~~

这个问题由：

~~~text
linger
termination
Reaper
~~~

处理。

---

# 一百九十六、阅读下一章时应带着这个问题

假设：

~~~text
TERMINATE
→ proxy loop exits
~~~

此时：

~~~text
frontend/backend Pipe 中仍有 backlog
~~~

接下来 socket close：

~~~text
究竟等多久？
什么条件才能真正 destroy？
谁通知 peer？
~~~

这正是 [Linger 与终止协议](linger-termination-protocol.md) 的主题。

---

# 一百九十七、最终心智模型

Proxy 不应理解为：

~~~text
recv A
send B
~~~

而应该理解成：

~~~text
               +----------------------+
               |   dependency-aware   |
               |    event scheduler   |
               +----------+-----------+
                          |
             +------------+------------+
             |                         |
     request direction           reply direction
             |                         |
   input + output-capacity    input + output-capacity
             |                         |
             +------------+------------+
                          |
                    bounded burst
                          |
                 complete multipart
                          |
              +-----------+-----------+
              |                       |
           capture                 destination
              |                       |
        observability             business path
              |
       critical-path cost

control:
independent wake source
→ ACTIVE / PAUSED / TERMINATED
~~~

如果只记一个结论：

> **libzmq Proxy 的核心不是“转发”，而是根据两条方向各自缺失的 progress dependency 动态选择下一次值得等待的事件；它用 complete-message burst 摊薄调度成本，用输出 readiness 把 HWM 继续传播回输入端，同时必须保证 control 在数据面饱和时仍可唤醒。真正值得警惕的是：burst 不是时间上界，capture 不是免费旁路，alias-specialized 状态空间必须与后续 registration 逻辑保持一致。**
