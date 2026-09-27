# LCM 全景：以最小运行时完成低延迟消息分发
我们先不看源码，只把 LCM 当成一个刚装上的机器人通信库来用。

假设定位进程每 10 ms 产生一次位姿，控制器要读它，可视化工具也要读它，晚上还要把整场实验录下来。你第一次接触 LCM 时，最先写出来的代码其实很朴素：

~~~cpp
lcm::LCM lcm;

pose_t pose;
pose.x = 1.2;
pose.y = 0.4;

lcm.publish("POSE", &pose);
~~~

订阅端也没有什么“框架味”：

~~~cpp
class Handler {
public:
    void onPose(const lcm::ReceiveBuffer*,
                const std::string& channel,
                const pose_t* pose) {
        std::cout << channel << ": "
                  << pose->x << ", " << pose->y << "\n";
    }
};

Handler handler;
lcm::LCM lcm;
lcm.subscribe("POSE", &Handler::onPose, &handler);

while (true) {
    lcm.handle();
}
~~~

如果只停在 API 层，LCM 很像一个很小的 topic 消息总线：

~~~text
Publisher                         Subscriber

pose_t
  |
  | publish("POSE")
  v
+---------------- LCM ----------------+
                                      |
                                      | callback("POSE")
                                      v
                                   pose_t
~~~

真正值得研究的地方从这里才开始。因为这几行代码一下子隐藏了六个问题：

1. `pose_t` 是 C++ 对象，网络只认识字节，它什么时候被编码？
2. `"POSE"` 只是一个字符串，订阅者是怎样匹配到它的？
3. 如果底层今天走 UDP，明天改成日志回放，为什么 `publish()` 可以不变？
4. 谁负责收包？`onPose()` 又到底在哪条线程里执行？
5. 如果 callback 卡住 50 ms，网络接收会不会跟着停？
6. 为什么 LCM 要求我们自己调用 `handle()`，而不是自动给我们开 worker thread？

本文就沿着这六个问题往下钻。顺序不是“先背模块名，再查源码”，而是从使用体验出发，先猜最简单的内部实现，再用具体失败把下一层设计逼出来，最后回到固定源码确认作者真正怎么做。

源码固定到 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`。后面明确称为固定版本源码的片段都来自这个提交；为了拆机制而写的小程序会明确说明是教学实现。

## 第一问：`publish("POSE", &pose)` 到底发送了什么？

我们传进去的是一个 C++ 对象，但 socket 不认识“对象”，最终只能接收一段字节。

最容易写出的第一版，是直接把对象内存发出去：

~~~cpp
// 错误思路：把 C++ 对象布局直接当 wire format
send(sock, &pose, sizeof(pose), 0);
~~~

只要消息里有 `std::string`、`std::vector`、指针，或者两边编译器布局不同，这个方案就会出问题。比如：

~~~cpp
struct Pose {
    double x;
    std::string frame_id;
};
~~~

`std::string` 里保存的是本进程自己的实现状态，其中可能包含指向堆内存的地址。把整个对象的内存镜像发到另一进程，并不会把字符串内容“神奇地搬过去”。

所以第一个必须被单独解决的问题是：

> 业务对象要先转换成双方都认可的 wire bytes。

这就是 `.lcm` 类型描述和 `lcm-gen` 的意义。生成器为 C++、Python、Java 等语言生成相同协议对应的编码/解码代码。LCM 核心后面只需要认识：

~~~text
channel + byte pointer + byte length
~~~

它不必理解 `pose_t` 的字段。

### C++ 模板 publish() 先把类型“消掉”

固定版本 C++ 包装层的模板实现非常直接：

~~~cpp
template <class MessageType>
inline int LCM::publish(const std::string &channel, const MessageType *msg)
{
    unsigned int datalen = msg->getEncodedSize();
    uint8_t *buf = new uint8_t[datalen];
    msg->encode(buf, 0, datalen);
    int status = this->publish(channel, buf, datalen);
    delete[] buf;
    return status;
}
~~~

逐句执行一次：

~~~text
pose_t
  |
  | getEncodedSize()
  v
需要 N 字节
  |
  | new uint8_t[N]
  v
临时 byte buffer
  |
  | encode()
  v
真正的 wire bytes
  |
  | publish(channel, void*, N)
  v
进入无类型 C runtime
~~~

这几行代码里有两个很值得注意的设计信息。

第一，模板只存在于 C++ 这一层。进入 `publish(channel, const void*, length)` 以后，消息的 C++ 类型已经不再参与传输。

第二，`delete[] buf` 紧跟在底层 `publish()` 返回之后。这意味着 provider 如果想异步继续使用 payload，就不能偷偷长期保存这根裸指针；它必须在返回前完成消费，或者取得自己的独立副本/所有权。

也就是说，**仅仅看调用方的生命周期，就能反推出被调用层必须遵守什么约束。**

## 第二问：为什么 publish() 不直接写死 UDP？

如果 LCM 永远只有 UDP，我们完全可以写：

~~~cpp
int publish(const char* channel,
            const void* data,
            std::size_t size) {
    return udp_send(channel, data, size);
}
~~~

但机器人项目很快会出现不同需求：

~~~text
在线实验    -> UDP multicast
离线记录    -> logfile
单元测试    -> memory queue
某些部署    -> TCP queue
~~~

最自然的第二版通常是 `switch`：

~~~c
switch (transport_kind) {
case UDPM:    return udpm_publish(...);
case TCPQ:    return tcpq_publish(...);
case MEMQ:    return memq_publish(...);
case LOGFILE: return logfile_publish(...);
}
~~~

真正的问题不是这一处 switch 有多丑，而是 transport 还需要 create、destroy、subscribe、handle、get_fileno。每增加一种传输，公共核心都会越来越了解本不属于自己的 socket、文件和队列细节。

于是我们才真正得到一个设计问题：

~~~text
稳定的东西：
    publish / subscribe / handle 这些公共语义

变化的东西：
    UDP / TCPQ / MEMQ / logfile 的具体实现
~~~

这时才需要 provider。

运行时只保存：

~~~text
lcm_t
  |
  +--> provider vtable  : “能做哪些动作”
  |
  `--> provider object  : “这一个具体 transport 的状态”
~~~

后面的[Provider 抽象](provider-vtable.md)会把函数指针、`void*`/不透明指针、C++ 虚函数、Strategy 与 Factory 逐层拆开。此处只需要建立一个直觉：provider 不是为了“使用设计模式”，而是为了让公共 API 不跟着 transport 种类一起膨胀。

## 第三问：为什么创建 `lcm::LCM` 后，真正的运行时还是 C 对象？

我们平时只写：

~~~cpp
lcm::LCM lcm;
~~~

固定版本构造函数实际上只是：

~~~cpp
inline LCM::LCM(std::string lcm_url) : owns_lcm(true)
{
    this->lcm = lcm_create(lcm_url.c_str());
}
~~~

所以 C++ `LCM` 更像一层语言友好的外壳，核心对象仍然是 `lcm_t*`：

~~~text
C++ user
   |
   v
lcm::LCM
   |
   | wraps
   v
lcm_t
   |
   +--> subscription registry
   +--> provider vtable
   `--> provider instance
~~~

为什么保留这两层？

因为 C runtime 很适合做多语言绑定，而 C++ 层可以额外提供模板编码、成员函数 callback 和 RAII。

`owns_lcm` 也不是多余字段。另一个构造函数允许包装外部传进来的 `lcm_t*`，这时它不能在析构时替别人销毁对象。这里正好说明一个非常基础但经常被忽略的 C/C++ 原则：

> 裸指针只说明“对象在哪里”，不说明“谁负责销毁它”。

## 第四问：subscribe 以后，callback 到底在哪条线程？

这是第一次使用 LCM 时最容易误判的地方。

很多人看到：

~~~cpp
lcm.subscribe("POSE", &Handler::onPose, &handler);
~~~

会下意识想成：

~~~text
网络线程收包
  -> LCM worker
  -> onPose()
~~~

但固定版本 C++ API 的说明明确指出：callback 在调用 `LCM::handle()` 的同一线程中执行。

所以真正的应用侧时间线是：

~~~text
应用线程
│
│ lcm.handle()
│    │
│    ├── 取得一条完整消息
│    ├── 匹配 subscription
│    ├── onPose() ───────── 业务代码可能运行 20 ms
│    └── return
│
│ 下一次 lcm.handle()
v
~~~

这意味着 callback 里写磁盘 20 ms，同一 LCM 实例后续业务分发至少会晚 20 ms。

### 那慢 callback 会不会直接卡住 socket recv？

又不能简单回答“会”。

UDPM provider 的接收侧和应用 callback 是两条执行流。先用一张最小图理解：

~~~text
网络接收侧                             应用侧

UDP socket
    |
receiver thread
    |
    | 收包 / 重组
    v
完整消息队列
    |
    | notify
    +--------------------------> lcm.handle()
                                     |
                                     v
                              subscription dispatch
                                     |
                                     v
                                user callback
~~~

这层隔离的意义是：业务 callback 不直接占住 socket receive loop。

但这不等于慢 callback 没有代价。应用侧消费速度低于网络侧生产速度时，完整消息会积压在有限缓存中，最终仍会出现丢弃或旧数据。

所以对控制系统来说，更合理的结构往往是：

~~~text
LCM callback
    |
    | 只做快速 decode / copy / swap
    v
有界最新值缓存
    |
control thread
~~~

而不是把几十毫秒算法直接塞进 callback。

## 第五问：为什么要我自己调用 handle()？

如果 LCM 自动替你执行 callback，它就必须顺便替你决定：

- callback 用几条线程；
- 和控制循环谁优先；
- GUI loop 怎么整合；
- backlog 一次处理多少；
- shutdown 时先停谁。

LCM 选择把这一层控制权留给应用。

固定 C 层 `lcm_handle()`：

~~~c
int lcm_handle(lcm_t *lcm)
{
    if (lcm->provider && lcm->vtable->handle) {
        int ret;
        g_rec_mutex_lock(&lcm->handle_mutex);
        assert(!lcm->in_handle);
        lcm->in_handle = 1;
        ret = lcm->vtable->handle(lcm->provider);
        lcm->in_handle = 0;
        g_rec_mutex_unlock(&lcm->handle_mutex);
        return ret;
    } else
        return -1;
}
~~~

这里真正发生的事情很少：

~~~text
检查 provider
  -> 串行化 handle
  -> 标记正在 handle
  -> 进入当前 provider 的 handle()
  -> 清标记
  -> 返回
~~~

`handle_mutex` 串行化的是同一个 LCM 实例的 handle 调用，不是“给整个 LCM 加了一把大锁”。它也不会让 callback 自动并行。

这种设计让 `getFileno()` 变得很有用：应用可以把 LCM 的 ready fd 合入自己的 `select/poll`、GUI 或机器人事件循环，而不必把主线程交给 LCM。

## 现在再看 LCM，已经不是三个 API 了

从最初的：

~~~text
publish / subscribe / handle
~~~

一路追问以后，我们已经自然推出：

~~~text
typed object
   |
generated codec
   |
byte buffer
   |
C++ wrapper
   |
lcm_t
   |
   +--> provider vtable --> UDPM/TCPQ/MEMQ/LOGFILE
   |
   `--> subscription registry
             ^
             |
receiver thread -> complete-message queue -> ready notification
                                             |
                                             v
                                         lcm.handle()
                                             |
                                             v
                                        user callback
~~~

注意这些层不是为了“架构看起来漂亮”才存在的。每一层都对应一个具体失败：

| 新的一层 | 它解决的直接问题 |
|---|---|
| generated codec | 不能把 C++ 对象布局当 wire protocol |
| C++ wrapper | 手工管理 C 指针、编码和成员函数 callback 太繁琐 |
| provider | transport 变化污染公共 API |
| receive queue | 业务 callback 不应直接占住 socket receive path |
| ready fd | 中间件不应强迫应用接受它自己的 event loop |
| subscription object | 一个字符串到一个函数不足以表达匹配、配额与生命周期 |

接下来再读 `lcm_create()`、`lcm_publish()`、接收线程和 subscription dispatch，源码里的对象就有了来路：我们已经知道作者为什么需要它们，而不是只认识它们的名字。
## lcm_create 的装配过程

`lcm_create(url)` 先建立 provider 描述列表：


```c
lcm_udpm_provider_init(providers);
lcm_logprov_provider_init(providers);
lcm_tcpq_provider_init(providers);
lcm_mpudpm_provider_init(providers);
lcm_memq_provider_init(providers);
```

然后解析 URL，将 scheme 与 provider 名称匹配：

```text
"udpm://239.255.76.67:7667?ttl=0"
    |        |                  |
 provider   target             args
```

找到对应 `lcm_provider_info_t` 后，顶层对象保存它的 vtable，并调用 provider 的 `create`：


```c
lcm->vtable = info->vtable;
lcm->provider = info->vtable->create(lcm, network, args);
```

这里使用组合而非把 `lcm_t` 做成巨大的 union。核心层不知道 UDP socket、TCP connection 或 logfile cursor 的布局，provider 也通过传入的 `lcm_t*` 调用统一订阅分发接口。

## 发布路径的最短调用链

顶层发布函数几乎只是一次间接调用：


```c
int lcm_publish(lcm_t *lcm, const char *channel,
                const void *data, unsigned int datalen)
{
    if (lcm->provider && lcm->vtable->publish)
        return lcm->vtable->publish(
            lcm->provider, channel, data, datalen);
    else
        return -1;
}
```

`lcm` 与 `data` 都是借用的输入指针；公共层没有把 payload 收进一个自有对象，所以 UDPM 可以在同步系统调用期间直接读取调用方缓冲区。返回值只来自 provider，不能跨 provider 推导“远端已执行”。

它没有统一复制队列、线程池或 retry 层。因此 `publish()` 的具体阻塞和复制语义由 provider 决定：

- UDPM 在调用线程中构造报文并执行 `sendmsg()`；
- TCPQ 可能把消息送入面向 server 的队列；
- MEMQ 在进程内排队；
- logfile provider 写入或回放事件。

抽象接口统一，不代表所有实现具有相同实时特征。选择 provider 是运行时策略选择，也是性能语义选择。

## 接收路径分成两个执行上下文

UDPM 的接收不是在 `lcm_handle()` 中直接 `recvfrom()`。它使用专用接收线程持续读取 UDP socket：

```text
UDPM receive thread
  -> recvmsg()
  -> parse LC02 / LC03
  -> fragment reassembly if needed
  -> enqueue complete lcm_buf_t
  -> write one byte to notify pipe

application thread
  -> select(lcm_get_fileno())
  -> lcm_handle()
  -> read notify pipe
  -> dequeue one lcm_buf_t
  -> dispatch handlers
```

网络接收和业务 callback 被队列隔开。这样 callback 不会直接阻塞 socket 读取，但应用仍必须足够频繁地调用 `handle()`，否则接收队列和每订阅者计数达到上限。

## 文件描述符让 LCM 接入外部 reactor

Provider vtable 包含 `get_fileno`，`lcm_get_fileno()` 将其暴露给应用。UDPM 返回通知 pipe 的读端，而不是 UDP socket 本身。

应用可以把它放进 `select`、`poll` 或其他 reactor：


```c
int fd = lcm_get_fileno(lcm);
pollfd pfd = {fd, POLLIN, 0};

if (poll(&pfd, 1, timeout_ms) > 0) {
    lcm_handle(lcm);
}
```

返回 pipe 的好处是：只有“完整且已入队的消息”才算可读事件。分片尚未重组完成时不会错误唤醒应用；接收线程的退出控制也能独立处理。

## 订阅使用正则表达式

`lcm_subscribe()` 将 channel 字符串编译成锚定正则：


```c
char *regexbuf = g_strdup_printf("^%s$", channel);
subscription->regex = g_regex_new(regexbuf, 0, 0, &error);
```

因此订阅可以匹配一组 channel，而不局限于精确字符串。`handlers_all` 保存所有订阅，`handlers_map` 缓存某个实际 channel 已匹配出的 handler 数组。

第一次看见新 channel 时，需要遍历全部订阅执行 regex match；之后可直接查 hash table。这里用空间换取稳定热路径：

```text
first message on channel C:  O(number of subscriptions)
later messages on C:         average O(1) lookup + O(matching handlers)
```

新增或移除订阅时，代码会更新已知 channel 的匹配数组，避免缓存与真实订阅集合分离。

## 每订阅者队列上限不是独立消息队列

`lcm_subscription_t` 保存：


```c
int max_num_queued_messages;
int num_queued_messages;
```

这里的计数不是每个订阅者各自保存一份 payload。provider 只保留一份完整消息 buffer，核心在消息入队前为愿意接收的订阅者增加待处理计数。

当某订阅者达到上限时，新消息不会为它增加计数；同一 channel 的其他订阅者仍可能接收。这避免了慢 handler 无限扩大内存，但也意味着丢弃可以发生在订阅者粒度。

默认上限为 30。它控制积压数量，不控制数据年龄；若 1 kHz channel 上积压 30 条，最老数据只晚约 30 ms，若 1 Hz channel 上积压 30 条，则可能是半分钟前的数据。

## callback 在锁外执行

`lcm_dispatch_handlers()` 先在互斥区中把相关 subscription 标记为 `callback_scheduled`，再逐个释放全局 mutex 并调用用户代码：


```c
subscription->num_queued_messages--;
g_rec_mutex_unlock(&lcm->mutex);
subscription->handler(buf, channel, subscription->userdata);
g_rec_mutex_lock(&lcm->mutex);
```

这条设计规则非常重要：中间件不能持有内部全局锁执行未知业务 callback。否则 callback 中再次 publish、subscribe 或访问其他资源时容易死锁，并把所有管理操作的尾延迟绑定到用户代码。

释放锁也带来生命周期问题。handler 在执行期间可能取消自身订阅，因此不能立即 free subscription。LCM 用 `callback_scheduled` 与 `marked_for_deletion` 实现延迟删除，callback 全部返回后再统一回收。

## handle 的串行化边界

`lcm_t` 有两把递归 mutex：

- `mutex` 保护订阅表、计数和核心数据结构；
- `handle_mutex` 保证同一实例只有一个线程进入 `lcm_handle()`。


```c
g_rec_mutex_lock(&lcm->handle_mutex);
assert(!lcm->in_handle);
lcm->in_handle = 1;
ret = lcm->vtable->handle(lcm->provider);
lcm->in_handle = 0;
g_rec_mutex_unlock(&lcm->handle_mutex);
```

这意味着增加多个线程同时调用同一实例的 `handle()`不会并行执行 callback，只会在 `handle_mutex` 上串行等待。需要真正并行处理时，应在 callback 中把解码后的任务转交工作队列，或按 channel/职责拆分多个 LCM 实例。

递归调用 `lcm_handle()` 被明确禁止。callback 可以 publish，但不能再次进入 handle 分发循环，否则 handler 数组、删除哨兵和 provider buffer 生命周期都难以保持不变量。

## 数据复制与所有权概览

一条常见 C++ 发布链包含：

```text
message object
  -> generated encode into byte buffer       一次序列化写
  -> sendmsg iovec                            短消息无需拼接 channel+payload
  -> kernel UDP buffer                        内核复制
  -> receiver ring/fragment buffer            接收存储
  -> callback receives pointer view           callback 期间借用
  -> generated decode                         构造目标对象
```

UDPM 短消息用 `iovec` 分别引用 header、channel 和 payload，避免在用户态再分配一块连续发送缓冲。但这不等于端到端零拷贝：编码、内核网络栈和接收仍会产生数据移动。

接收 callback 获得的 `lcm_recv_buf_t::data` 只在本次 handler 调用期间有效。需要异步处理时必须复制 bytes，或在 callback 内完成 decode 并把拥有自身存储的对象交给下游。

## 性能模型

对短消息，发布成本近似为：

```text
Tpublish = Tencode + Ttransmit_mutex + Tsendmsg
```

接收回调延迟近似为：

```text
Lcallback = Lnetwork
          + Lrecv_thread_schedule
          + Lprovider_queue
          + Lapplication_poll
          + Lprevious_callbacks
          + Ldispatch
          + Tdecode
```

LCM 自身没有复杂路由计算，最容易主导尾延迟的是：应用多长时间调用一次 handle、前序 callback 运行多久、UDP 是否发生分片，以及队列是否因突发流量溢出。

## 设计优势

- C 核心小，调用链短，容易定位每次复制和锁；
- provider vtable 让传输策略与订阅语义分离；
- 生成式类型系统支持多语言和确定线格式；
- 通知 fd 能自然接入现有事件循环；
- 用户控制 callback 线程，执行上下文清晰；
- 日志记录与回放建立在同一 channel/type 模型上。

## 设计限制

- 默认 UDPM 不可靠，大消息分片会放大丢包风险；
- 没有内建发现、权限、加密和丰富 QoS；
- 单实例 handle 串行，慢 callback 会阻塞后续分发；
- callback buffer 是借用视图，异步使用需要复制；
- regex 订阅和 channel cache 增加动态管理复杂度；
- provider 语义不同，统一 API 不能保证统一阻塞行为。

## 真正学完 LCM 的一条路线：每次使用都带出下一层问题

读完总览以后，不要把所有源码文件同时打开。选自己的机器人里最普通的一条 `JOINT_STATE`，依次完成这些实验：

~~~text
同机：生成类型 publish / subscribe
   | 结构体的内存布局为什么不是网络协议？
   v
切换：不改业务 publish，只改 provider URL
   | 公共层怎么保存不同 provider 的私有状态？
   v
压测：大点云和 1 kHz 小状态共享一个实例
   | LC03 / IP 分片分别在哪层？发送锁阻塞谁？
   v
停顿：让应用 callback 阻塞 15 ms
   | receiver、完整消息队列、各订阅准入计数怎么变化？
   v
故障：在一个 callback 里取消另一个订阅
   | 遍历边界、删除标志和 userdata 寿命怎样协调？
   v
回放：记录一次运行，到另一 multicast 地址播放
   | 日志时间、传感器时间和墙钟回放时间相同吗？
   v
集成：把同一输入接入仿真控制图
   | 网络 callback 为什么不能随意修改仿真状态？
~~~

这些问题分别进入[类型编码与日志](types-and-eventlog.md)、[Provider](provider-vtable.md)、[UDPM 分片](udpm-publish-protocol.md)、[接收与缓冲](receive-reassembly.md)、[订阅与分发](subscription-dispatch.md)、[C ABI/C++ 对象设计](c-abi-cpp-design-lab.md)与[设计复盘](design-recap.md)。对应的实际操作则见[安装与网络](use-environment.md)、[类型与收发](use-pubsub-types.md)、[日志与故障演练](use-operations.md)和[Drake 仿真](case-study-drake.md)。

每完成一环，都试着不用背类名回答四个问题：**字节现在在哪里、由谁拥有、下一次执行在哪条线程、发生丢弃或关闭后谁负责释放。** 这四个答案能把“会用 LCM”与“能从零设计同类运行时”真正连起来。

## 源码阅读路线

建议按一条消息的真实生命周期阅读以下固定版本符号：

1. `lcm_create()`、`lcm_publish()`、`lcm_subscribe()`、`lcm_handle()`：URL 选择、句柄与公共 API；
2. `_lcm_provider_vtable_t`：provider 操作契约；
3. `lcm_udpm_publish()`、`recv_thread()`、`lcm_udpm_handle()`：发送、接收线程与分发；
4. `lcm2_header_short_t`、`lcm2_header_long_t`：协议 header 与常量；
5. `lcm_buf_allocate_data()`、`lcm_buf_queue_*()`、`lcm_ringbuffer_*()`：filled queue、扩容 ring 与消息存储所有权；
6. `emit_encode()`、生成类型的 `encode()`/`decode()`：fingerprint 与多语言编码；
7. `lcm_eventlog_read_next_event()`、`lcm_eventlog_write_event()`、`lcm_file_handle()`：日志格式、seek 与回放基础。

后续章节从最小的 provider 接口开始，再分别拆开发送协议、接收队列、分片重组、订阅分发和类型生成。最终把这些单元组合成一个可自行实现的简化 LCM。
