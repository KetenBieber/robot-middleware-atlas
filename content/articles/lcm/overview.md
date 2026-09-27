# LCM 全景：以最小运行时完成低延迟消息分发

想象一台移动底盘以 1 kHz 发布关节和里程计状态：控制进程需要最新值，界面进程需要画曲线，记录进程要保存可回放的数据。最朴素的程序会把 C++ 结构体直接 `sendto()`，另一端按本机结构体强制转换，再在接收线程里调用所有业务函数。换一台不同字节序或编译选项的计算机，字段偏移就可能不一致；记录回调一次阻塞 20 ms，后面的消息便排队，控制回调拿到越来越旧的状态；一次 UDP 分片丢失后，大消息不完整，应用只能观察到序列号跳变或解码失败。

这组失败把问题拆成三件事：怎样让不同语言对同一消息字节达成一致；怎样替换网络、内存队列与日志而不改订阅语义；怎样把网络接收与业务回调分开，并明确队列溢出时丢什么。LCM 的回答是一条短而清晰的数据链：类型生成器负责消息编码，provider 负责传输，`lcm_handle()` 在调用者线程中分发回调。它不给慢回调另开线程，所以应用还必须自行决定隔离策略。

固定源码版本为 lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864。本系列以 C 运行时为主线，因为 C++、Python、Java 等绑定最终都要落到相同的 provider 协议与线格式上。

## LCM 在机器人系统中的位置

一个机器人程序通常需要同时传输：

- 高频但允许丢失的状态、姿态和传感器数据；
- 需要跨语言共享的结构化消息；
- 可记录、回放并离线分析的数据流；
- 同一局域网内多个进程之间的一对多广播。

LCM 对这类需求给出的答案是：使用 `.lcm` 类型描述生成各语言编码器，用 channel 字符串标识数据流，再由可替换 provider 把字节发送到 UDP multicast、TCP queue、内存队列或日志文件。

```text
typed object
  -> generated encode()
  -> lcm_publish(channel, bytes)
  -> provider vtable
  -> UDPM / TCPQ / MEMQ / LOGFILE
  -> provider receive queue
  -> lcm_handle()
  -> channel handler
  -> generated decode()
```

LCM 不提供组件生命周期、分布式参数服务器或协程调度器。算法线程怎样组织、callback 在哪个线程执行、慢消费者如何隔离，都由应用明确决定。功能较少也意味着边界更容易推导。

## 适用场景

LCM 适合以下系统：

- 受控局域网中的实验机器人；
- 需要 C、C++、Python、Java 等多语言互通的研究平台；
- 传感器流和状态流更关注低延迟而非逐包可靠；
- 需要通过日志记录复现一次实验；
- 希望把通信库嵌入自有事件循环，而不是接受一套完整执行框架。

典型程序只需要几行：


```cpp
lcm::LCM bus;
bus.subscribe("POSE", &Handler::OnPose, &handler);

while (running) {
  bus.handleTimeout(10);
}
```

这一小段代码隐藏了网络接收线程、完整消息 buffer list、subscription 配额、通知 pipe、正则订阅表和 provider 虚表，但没有隐藏回调执行线程：`OnPose` 在调用 `handleTimeout()` 的线程中执行。

## 不适用场景

LCM 的默认 UDPM provider 不适合把“消息必达”视为安全条件的链路。UDP multicast 没有端到端确认、重传和流量控制；一个分片丢失会使整条大消息无法重组。

以下需求通常需要额外设计或其他传输：

- 跨不可靠广域网的可靠命令；
- 需要认证、加密和细粒度访问控制的生产网络；
- 必须向慢订阅者施加背压而不能丢数据；
- 需要服务发现、请求响应和复杂 QoS 协商；
- 需要框架负责线程池、优先级和组件生命周期。

LCM 的优势来自约束明确，而不是覆盖所有分布式系统问题。

## 四个核心组成部分

### 类型生成器

`.lcm` 文件描述结构体、字段、数组和嵌套类型。`lcm-gen` 为目标语言生成：

- 数据结构定义；
- `encode` 与 `decode`；
- 编码长度计算；
- 类型 fingerprint/hash；
- 部分语言的发布订阅便利封装。

类型生成器让运行时只处理字节数组。provider 不需要理解 `pose_t` 或 `laser_t`，因此传输层与消息 schema 解耦。

### 核心句柄 lcm_t

固定版本的 `lcm_t` 保存订阅、channel 匹配缓存、provider 接口和 handle 并发状态。它不是抽象图，而是核心实际持有的对象布局：


```c
struct _lcm_t {
    GRecMutex mutex;
    GRecMutex handle_mutex;

    GPtrArray *handlers_all;
    GHashTable *handlers_map;

    lcm_provider_vtable_t *vtable;
    lcm_provider_t *provider;

    int default_max_num_queued_messages;
    int in_handle;
};
```

这个结构把“与传输无关的订阅语义”和“provider 私有网络状态”分开。顶层只持有不透明 `lcm_provider_t*`，具体对象可以是 `lcm_udpm_t`、`lcm_tcpq_t` 或 `lcm_memq_t`。

### provider

Provider 是 C 风格的策略对象。URL 的 scheme 选择实现：

```text
udpm://239.255.76.67:7667?ttl=1
tcpq://127.0.0.1:7700
memq://
file://experiment.lcm
```

每个 provider 实现同一组函数：创建、销毁、订阅、取消订阅、发布、处理一条消息和暴露可等待文件描述符。

### 用户驱动的事件循环

LCM 不会自动选择业务 callback 线程。应用调用：


```c
lcm_handle(lcm);
```

provider 取出一条完整消息，再由核心订阅表依次调用匹配 handler。回调执行完，`lcm_handle()` 才返回。调用边界如下；参数 `lcm` 是已创建句柄，返回值来自 provider 的一次 `handle` 操作。

对应的上游实现如下：

```c
int lcm_handle(lcm_t *lcm)
{
    if (lcm->provider && lcm->vtable->handle) {
        int ret;
        g_rec_mutex_lock(&lcm->handle_mutex);
        assert(!lcm->in_handle);  // recursive calls to lcm_handle are not allowed
        lcm->in_handle = 1;
        ret = lcm->vtable->handle(lcm->provider);
        lcm->in_handle = 0;
        g_rec_mutex_unlock(&lcm->handle_mutex);
        return ret;
    } else
        return -1;
}
```

`handle_mutex` 只串行化同一实例的 handle 调用，`in_handle` 断言拒绝 callback 递归进入第二轮分发；它没有锁住 provider 的网络接收线程，也不会让 callback 并行运行。下一跳是所选 vtable 的 `handle`，因此真实等待和一次调用消费多少消息要继续看 provider。

这给出非常直接的执行模型：若 callback 运行 20 ms，同一 LCM 实例上的下一条 callback 至少晚 20 ms。网络接收线程可能继续收包，但有限队列会积压并最终丢弃。

## C 与 C++ 两层 API

LCM 的核心用 C 编写。C++ 类 `lcm::LCM` 主要是 RAII 与模板适配层：

对应的上游实现如下：

```cpp
class LCM {
 public:
  explicit LCM(const std::string& url = "");
  ~LCM();

  int publish(const std::string& channel,
              const void* data, unsigned int size);
  int handle();
  int handleTimeout(int timeout_millis);

 private:
  lcm_t* lcm;
};
```

构造函数调用 `lcm_create()`，析构函数调用 `lcm_destroy()`。模板订阅把 C++ 成员函数包装成 C callback 加 `void* userdata`。

这是一种薄封装，而不是另一套运行时。阅读 C++ API 时遇到性能或并发问题，应继续下钻到 `lcm.c` 和选中的 provider。

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
