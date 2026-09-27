# LCM 功能与组件地图：小型运行时如何形成完整消息总线

一台移动机器人把关节状态以 1 kHz 发给记录器、可视化程序和状态估计器时，最先遇到的不是“该用哪个类”，而是三条进程各自怎样拿到同一份数据、网络丢掉一包后还能不能继续、记录器落盘变慢时会不会拖住控制回路。LCM 用少量模块回答这些边界问题；本专题沿一条状态消息从发布到回放的路径展开。

最朴素的做法是让每个进程各写一份 `sendto/recvfrom`，直接把本机 `struct` 发到网络，再在接收线程里调用业务函数。编译器填充和主机字节序不同会让另一种语言读错字段；一个 8 ms 的可视化回调会让同一接收线程错过后续 UDP 数据报；图像超过 MTU 后由 IP 拆分，任一 IP 分片丢失都会让整条 UDP 数据报失效；实验结束后也没有原始输入可以回放。LCM 因此把类型编码、传输、缓存和回调线程分开，并为过载与丢片保留明确边界。

## 使用场景与系统边界

LCM 适合受控网络中的机器人、车辆、仿真和实验系统：协议简单，跨语言类型生成稳定，UDP 多播无需中心 broker，日志格式让线上数据容易回放到离线算法。

它不提供端到端可靠交付、动态 QoS 协商或复杂全局发现。发布返回值的含义由 provider 决定：UDPM 短帧成功对应本机 `sendmsg()` 接受完整 datagram；固定版本的 UDPM 长帧即使 `sendmsg()` 失败也会返回 0；无论哪种路径，都不表示所有订阅者收到。应用必须决定丢包、旧数据、重复和消费者离线意味着什么。

## 功能域

| 功能 | 核心实现 | 关键语义 |
|---|---|---|
| 公共 API | `lcm_t`、C++ wrapper | 实例、channel、subscribe/handle |
| 传输抽象 | provider vtable | URL scheme 与实现隔离 |
| UDPM 发送 | LC02/LC03、iovec | 短消息、应用分片、序号 |
| UDPM 接收 | receive thread、buffer lists、ring、reassembly table | subscription 配额、重组淘汰与存储生命周期 |
| 分发 | regex subscription、callback | 锁外回调、延迟删除 |
| 类型与日志 | lcm-gen、fingerprint、eventlog | 跨语言协议与可回放输入 |

## 从机器人需求反推组件

| 需求 | 首先进入的模块 | 系统级限制 |
|---|---|---|
| 无 broker 的局域网状态广播 | UDPM provider、multicast socket | 交换机组播配置、丢包与 MTU |
| C/C++、Java、Python 使用同一消息 | lcm-gen、fingerprint | schema 演进仍需人为规则 |
| 接入已有 `poll/epoll` 主循环 | `get_fileno()`、`lcm_handle()` | handle 频率决定数据年龄 |
| 大消息跨 UDP 发送 | LC03 分片与 reassembly table | 任一片丢失导致整条失败 |
| 慢算法不进入网络接收线程 | receive thread、filled queue | 必须定义 subscription quota 与 drop policy |
| 回放机器人实验 | eventlog reader/provider | 外部时钟、随机性和副作用仍需控制 |
| 增加内存或文件传输实现 | provider vtable、scheme registry | provider 行为与阻塞语义可能不同 |

LCM 的强项是小而清晰，不是隐藏所有分布式问题。选择它时应主动接受并处理 UDP 丢失、单实例 callback 串行和较少 QoS；若需求本身要求可靠命令事务、动态授权或广域网路由，应在外围补协议或选择其他中间件。

## 固定源码入口

本系列的 C/C++ 运行时固定到 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`：

| 层 | 固定版本中的符号 | 先追踪的职责 |
|---|---|---|
| 公共核心 | `lcm_create()`、`lcm_publish()`、`lcm_subscribe()`、`lcm_handle()`、`lcm_destroy()` | 句柄创建、订阅和 provider 间接调用 |
| provider 契约 | `_lcm_provider_vtable_t` | scheme 选择后的操作分派 |
| UDP provider | `lcm_udpm_publish()`、`recv_thread()`、`lcm_udpm_handle()` | 发送、收包、重组、队列与分发 |
| wire 辅助 | `lcm2_header_short_t`、`lcm2_header_long_t` | LC02/LC03 固定字段与字节序 |
| C 类型生成 | `emit_c_struct_encode()`、`emit_c_struct_decode()` | 编码长度、字段布局与解码 |
| 日志 | `lcm_eventlog_read_next_event()`、`lcm_eventlog_write_event()` | event framing、时间戳、读写与 seek |
| C++ 门面 | `LCM::publish<MessageType>()`、`LCMMHSubscription::cb_func()` | RAII、模板发布与 callback trampoline |

从公共 API 进入可替换传输的最短路径是 `lcm_publish()`。调用者把 channel 与编码后的字节传入，核心持有当前 provider 和对应方法表；以下是固定提交中的完整函数。


```c
int lcm_publish(lcm_t *lcm, const char *channel, const void *data, unsigned int datalen)
{
    if (lcm->provider && lcm->vtable->publish)
        return lcm->vtable->publish(lcm->provider, channel, data, datalen);
    else
        return -1;
}
```

这里没有复制 payload，也没有排入核心统一队列：`data` 在同步 provider 调用期间仍由调用者拥有，实际发送、缓存或落盘由选中的 provider 决定。函数指针表把具体传输作为下一跳，却不把各 provider 的阻塞、可靠性和返回值语义自动变成相同契约。

阅读时从公共函数进入 provider，再回到公共分发，不要只在 `lcm_udpm.c` 内看 socket。provider 接收完整消息后还要进入 `lcm_t` 的订阅表，回调生命周期和缓存失效属于核心层。

## 完整路径

```text
generated message encode
  -> lcm_publish
  -> provider.publish
  -> LC02 or LC03 datagrams
  -> receive thread / reassembly
  -> filled-buffer linked list + notify pipe
  -> lcm_handle
  -> regex subscription callback
```

接收线程与用户 callback 被完整消息队列和通知 pipe 分开。网络线程负责尽快收包、重组并入队；应用线程通过 `lcm_handle()` 从队列取消息并调用 callback。UDPM 的 filled-buffer 链表不是固定容量 ring，也没有全局硬队列上限；默认每条 subscription 的待处理计数上限为 30，过载时后续消息不再为已满的 subscription 增加资格，因此消息可能被 provider 丢弃而不是对业务线程施加背压。将容量设为 0 或负数会关闭该上限。重组表另有项数和字节数预算，但超额检查在插入前发生，因此也不是瞬时硬上限。可直接对照 `lcm_try_enqueue_message()`、`lcm_buf_queue_*()` 与 `lcm_buf_allocate_data()`。

### 控制路径、数据路径和回放路径

```text
控制路径：lcm_create URL -> scheme -> provider factory -> resources
           subscribe regex -> provider subscribe + core registry/cache

数据路径：encode -> publish -> UDP -> receive/reassembly -> handle/callback

回放路径：eventlog read -> timestamp policy -> core dispatch -> same callback
```

理想情况下，实时网络输入与日志输入在“完整消息进入核心分发”之后共享同一条 callback 链。这样算法无需知道输入来自 socket 还是文件。时间推进必须显式选择：按原始间隔、倍速、逐帧或外部仿真时钟，不能让 logfile provider 隐式读取墙钟决定一切。

## 核心对象与所有权

```text
lcm_t
  |-- provider pointer + provider vtable
  |-- subscription registry
  |-- callback/handle state
  `-- core and handle recursive mutexes

UDPM provider
  |-- multicast socket / receive thread
  |-- fragment reassembly table
  |-- empty/filled buffer linked lists
  |-- shared expandable ring storage
  `-- notification pipe/fd

C++ LCM wrapper --default ctor owns--> lcm_t
Subscription callback --borrowed user pointer--> application object
```

公共核心只看 opaque `lcm_provider_t*` 与函数指针表；具体 UDPM 状态隐藏在 provider 翻译单元。固定版本的 C++ `lcm::LCM` 自己保存裸 `lcm_t*`，并在析构时按 `owns_lcm` 决定是否销毁；`lcm-cpp.hpp` 没有删除复制构造/赋值，也没有实现移动操作。因此拥有型实例在 C++11 中仍可浅复制：两个 wrapper 保存同一 `lcm_t*` 并在析构时重复销毁；若已有订阅，复制过来的 `subscriptions` 向量还会让两个析构函数重复 `delete` 同一组 wrapper 指针。应用应避免复制该句柄；自建包装时应显式禁用复制并实现安全移动。`class LCM` 与成员、`lcm`、`owns_lcm` 与 `subscriptions`、`LCM` 构造与析构。回调的 `void* user` 不拥有应用对象，subscription 存活期间调用者必须保证其地址稳定。

### 所有权与借用边界

| 对象/数据 | owner | 借用者 | 生命周期终点 |
|---|---|---|---|
| `lcm_t` | C++ wrapper 或 C 调用者 | publish/subscribe/handle 调用栈 | `lcm_destroy` 返回 |
| provider | `lcm_t` | vtable 公共入口、receiver thread | provider destroy 完成 |
| subscription 节点 | 核心 registry | match cache、当前 dispatch | 当前 dispatch 收尾后可延迟回收；这不是跨线程 callback 完成屏障 |
| reassembly entry | UDPM fragment table | receiver 当前分片处理 | 完成、容量压力触发的 LRU 淘汰或 provider 销毁 |
| receive message buffer | queue slot/provider | 一次 callback | handle 归还槽位 |
| callback user pointer | 应用 | trampoline | 停止并 join 唯一 handle 线程，确认没有在途 callback 后释放 |
| eventlog event payload | eventlog reader/event 对象 | decode/callback | event destroy/下一次复用 |

一个裸指针是否安全取决于这张外部协议。C 核心不会自动延长 user object 寿命；C++ `shared_ptr` 也不能在没有注销屏障时解决 C 层已经保存的地址。固定版本的 `lcm_unsubscribe()` 在发现 dispatch 已经登记该 subscription 时只标记延迟删除并立即返回；调用者若此刻释放 userdata，正在运行的 callback 仍可能访问它。应用应先停止并 join 唯一的 handle 线程，再取消订阅并销毁 userdata；`lcm_destroy()` 自身也没有等待另一线程正在执行的 `lcm_handle()` 的屏障。 `lcm_unsubscribe()` 与 `lcm_dispatch_handlers()`、`lcm_destroy()`。移动 callback owner 还会改变地址，因此常把 CallbackState 单独堆分配。

## Provider 是手写 C 多态

URL scheme 选择 provider，vtable 提供 create/destroy/subscribe/unsubscribe/publish/handle/get_fileno。第一个 provider 参数等价于 C++ `this`。这种结构稳定、易被 C 语言绑定调用，也避免核心层包含每种传输私有头文件。

代价是类型检查弱、vtable 布局属于内部 ABI、构造失败要手工逆序清理。所有函数指针签名必须精确一致，异常绝不能从 C++ bridge 穿过 C ABI。

Provider 接口的每个操作都要定义同步语义：publish 是完成本地入队、完成系统调用还是完成持久化；handle 一次处理一条还是一批；destroy 是否等待 receiver；get_fileno 的可读性是 level 还是 edge。相同函数签名并不保证相同延迟模型，URL 配置本身就是运行行为的一部分。

### 用实际构造代码看 URL 如何变成 provider 对象

如果按照错误的单一 UDP 实现设计，每加入一种传输，`publish/subscribe/handle` 三类公共 API 都得增加 `switch` 分支。LCM 把这个选择压到构造期。下面是固定提交中 `lcm_create` 的连续片段，展示 provider 清单注册、默认 URL、scheme 查找、内核 registry 初始化及 provider 私有对象创建：

~~~c
    // initialize the list of providers
    lcm_udpm_provider_init(providers);
    lcm_logprov_provider_init(providers);
    lcm_tcpq_provider_init(providers);
    lcm_mpudpm_provider_init(providers);
    lcm_memq_provider_init(providers);
    if (providers->len == 0) {
        fprintf(stderr, "Error: no LCM providers found\n");
        goto fail;
    }

    if (!url || !strlen(url))
        url = getenv("LCM_DEFAULT_URL");
    if (!url || !strlen(url))
        url = LCM_DEFAULT_URL;

    if (0 != lcm_parse_url(url, &provider_str, &network, args)) {
        fprintf(stderr, "%s:%d -- invalid URL [%s]\n", __FILE__, __LINE__, url);
        goto fail;
    }

    lcm_provider_info_t *info = NULL;
    /* Find a matching provider */
    for (unsigned int i = 0; i < providers->len; i++) {
        lcm_provider_info_t *pinfo = (lcm_provider_info_t *) g_ptr_array_index(providers, i);
        if (!strcmp(pinfo->name, provider_str)) {
            info = pinfo;
            break;
        }
    }
    if (!info) {
        fprintf(stderr, "Error: LCM provider \"%s\" not found\n", provider_str);
        g_ptr_array_free(providers, TRUE);
        free(provider_str);
        free(network);
        g_hash_table_destroy(args);
        return NULL;
    }

    lcm = (lcm_t *) calloc(1, sizeof(lcm_t));

    lcm->vtable = info->vtable;
    lcm->handlers_all = g_ptr_array_new();
    lcm->handlers_map = g_hash_table_new(g_str_hash, g_str_equal);

    g_rec_mutex_init(&lcm->mutex);
    g_rec_mutex_init(&lcm->handle_mutex);

    lcm->provider = info->vtable->create(lcm, network, args);
~~~

`providers` 是临时的工厂目录，`info->vtable` 则随 `lcm_t` 一起存活。`lcm->provider` 是具体传输实现返回的不透明句柄；同一个 `lcm_t` 同时持有函数表和实例状态，调用期不再需要知道当前是 UDP、日志还是进程内队列。`handlers_all` 保存权威 subscription 列表，`handlers_map` 保存已见具体 channel 的匹配缓存；二者并不包含 provider 的 socket 与 reassembly 私有状态。

这个步骤也揭示了手写 C 多态的失败责任：URL 不合法、scheme 不存在、provider 的 `create()` 返回空，都应释放构造期临时目录和已经创建的对象。`lcm_t` 初始化了互斥锁以后再创建 provider，因此 provider 可以回调核心内部的 `lcm_*` 函数；反过来，销毁时必须先停止 provider 的后台活动，再拆核心 subscription 与互斥锁。构造与销毁必须是一张方向相反的依赖图，不能只看 `publish` 的一行 vtable 间接调用。
## UDPM 发送协议

小消息可使用单 datagram 格式；超过阈值后使用分片格式，携带 sequence、总长度、fragment offset/number 等字段。发送端可用 `iovec/sendmsg` 把 header、channel 和 payload 分散写出，减少拼接 buffer。

分片数约为 `ceil(payload / fragment_payload)`。任一片丢失都会让整条消息无法重组，所以消息越大，成功概率越容易被网络丢包放大。MTU、内核 socket buffer 和组播网络配置比 C++ 函数调用开销更可能成为瓶颈。

wire header 必须明确 magic、固定宽度、网络字节序和最大尺寸。不能直接发送原生 struct，也不能在验证总长度以前按对端字段分配巨型缓冲区。

### 发送路径的数据移动

```text
typed object
  --generated encode--> contiguous payload buffer
  --iovec view---------> header + channel + payload
  --sendmsg------------> kernel socket buffer
  --network------------> receiver kernel buffer
```

scatter/gather 能避免把 header、channel、payload 再拼成一块临时 buffer，但生成类型编码通常已经把对象复制成连续 payload。所谓“零拷贝”必须说明省掉哪一次；sendmsg 并不意味着内核和网卡都不复制。

若 payload 需要 F 个分片，系统调用数、header 字节和丢片机会都约随 F 增长。巨大图像更适合专门大数据 transport 或压缩/降采样，而不是仅调大 UDP 分片上限。

## 接收、重组与过载准入

```text
recvmsg
  -> validate magic/channel/size
  -> single packet: take empty lcm_buf_t + attach ring storage
  -> fragment: lookup (sender, sequence) reassembly state
       -> copy fragment / decrement remaining-fragment counter
       -> all fragments present -> completed message
  -> append to filled-buffer linked list
  -> notify application fd
```

重组 key 不能只用 sequence，因为不同发送者可能使用相同序号。固定版本按来源地址与序号查找，并在新建条目时依据最近更新时间淘汰旧项；没有独立定时扫描，所以单个半包会留到后续容量压力触发淘汰或 provider 销毁。重组 store 的字节上限也不是严格瞬时硬上限：`lcm_frag_buf_store_add()` 在插入新项之前检查当前总量，刚插入的大项会让总量超过配置值。安全复刻应在分配前校验新项大小，并明确选择周期过期或严格字节配额。`lcm_frag_buf_store_add()`。

`inbufs_empty` 与 `inbufs_filled` 是保存 `lcm_buf_t` 指针的链表，不是固定容量 ring；ring 只管理报文的 backing storage，空间不足时可以扩容。每个 subscription 的待处理计数默认限制为 30，达到额度时新消息可能不为该订阅入队。通知 fd 表示 complete-message 队列非空；一次 pipe 字节不是消息 payload，也不能直接作为队列深度。

### 线程与数据移动矩阵

| 阶段 | 线程 | 共享状态 | 主要阻塞/失败 |
|---|---|---|---|
| `lcm_publish` | 调用者线程 | provider 发送状态 | encode、发送锁、socket buffer |
| `recvmsg`/重组 | UDPM receiver | fragment table、empty/filled buffer lists、ring storage | socket read、内存预算、subscription quota |
| notification fd | receiver → event loop | 队列非空状态 | pipe write error、队列与通知状态不一致 |
| `lcm_handle` | 应用 event-loop 线程 | subscription registry/cache | 等待消息、匹配、callback |
| callback | 同 handle 线程 | 应用对象 | 慢处理、重入 unsubscribe |
| eventlog write | 调用者/记录线程 | 文件 offset/buffer | 磁盘 I/O、flush 抖动 |
| destroy | 管理线程 | 全部 provider 状态 | wake receiver、join、在途 callback |

网络接收与用户回调分线程只隔离了第一层。若 callback 做磁盘 I/O 或重计算，handle 线程仍会积压所有 channel。重任务应复制必要数据后投递到有界 worker，同时保留序号和时间戳用于检测过期。

## Subscription 与回调语义

一个 channel 可匹配多个 regex subscription。消息 payload 可以在同一 handle 调用中共享，避免每订阅者复制；callback 不能把接收 buffer 裸指针保存到返回以后。

callback 内 unsubscribe 会修改当前遍历集合。安全实现可使用延迟删除或 subscription 快照，不能一边遍历链表一边立即释放当前节点。callback 由 `lcm_handle()` 调用线程串行执行，慢 callback 会推迟同实例其他 channel 的分发。

### 正则匹配缓存的读写取舍

若每条消息都扫描 `R` 个 regex subscription，分发成本为 `O(R × match_cost)`。核心可以在首次看到具体 channel 时计算匹配集合并缓存，后续变成近似 `O(1 + K)`，K 为命中订阅数。

subscribe/unsubscribe 会让 cache 失效。控制面变化少时，清空全部已知 channel cache 比维护复杂反向索引更可靠；代价是变更后的首批消息重新计算。缓存项若保存 subscription 指针，删除必须先使缓存不可命中，再等待当前 dispatch 离开或使用延迟回收。

## 类型生成与日志

`.lcm` schema 生成各语言编码/解码代码和 fingerprint。强类型只覆盖 payload schema；channel 字符串仍是运行时契约。Fingerprint 能检测不兼容类型，却不能自动完成向后兼容迁移。

Eventlog 记录时间戳、channel 和原始数据，回放可重建输入序列。确定性仍取决于算法是否读取墙钟、随机数、线程调度或外部设备；日志是输入证据，不是自动确定性证明。

### 类型层与传输层保持正交

```text
.lcm schema
  -> generated Message::encode/decode + fingerprint
  -> raw payload bytes
  -> live provider 或 logfile provider
  -> raw payload bytes
  -> generated decode
```

同一生成类型不应依赖 UDPM header，eventlog 也不需要理解消息字段。这使日志能原样保存未知类型，并让新语言绑定只实现 schema 编码，不必重写网络核心。代价是 channel 到类型的映射仍由应用约定，运行时不能单凭 channel 名推断正确 decoder。

回放时应保留原始事件时间、回放单调时间和处理时间三种概念。把它们混成一个 timestamp，会让延迟分析、倍速回放和算法时间输入互相污染。

## 学习顺序

《全景》解释系统边界；《Provider 抽象》解释 C 多态；《UDPM 发送》与《接收链》组成协议闭环；《订阅与分发》解释线程切换；《类型与日志》解释 wire compatibility 与复现。

具体源码任务可以固定为：

1. 从 `lcm_create` 找到 URL 解析、scheme 选择和 provider 半构造回滚；
2. 从 `lcm_publish` 进入 UDPM，分别画出 LC02 与 LC03 header、复制和系统调用；
3. 从 receiver 线程进入 fragment table，核对 source+sequence key、重复片处理、LRU 淘汰和字节限制；
4. 从 filled queue/notification fd 进入 `lcm_handle`，验证通知不会丢唤醒；
5. 从 channel cache 进入 callback，验证 unsubscribe 不释放当前遍历节点；
6. 从 lcm-gen 的 encoded-size/encode/decode 追踪动态数组边界；
7. 从 eventlog read 把消息重新接回同一分发链；
8. 最后沿 destroy 反向验证 receiver、fd、重组表、订阅和回调全部收敛。

完成这些任务后，读者可以自己写一个缩小版，而不只是调用 `publish/subscribe`。

## 设计取舍

专用接收线程加通知 fd 易于接入任意 event loop；单消息共享 payload 避免为每订阅者复制。代价是 UDP 不可靠、分片放大丢失概率、callback 由 handle 线程串行执行。

小型核心降低学习、部署和跨语言绑定成本，也意味着高级可靠性、服务发现、安全认证和流量治理留给外围系统。这个选择适合边界清晰的机器人网络，不适合把 LCM 当成任意不可信广域网的完整消息平台。

### 设计模式和代价

- 手写 vtable：以稳定 C ABI 实现 Strategy；易绑定多语言，但函数签名和布局需人工维护；
- Abstract Factory：URL scheme 选择 provider；部署简单，但静态 provider 新增通常需重新链接；
- Reactor bridge：notification fd 把内部 receiver 接入外部 event loop；组合性好，但必须正确维护队列—通知不变量；
- Observer：subscription callback 订阅 channel；解耦发布者，代价是回调寿命与重入删除；
- Cache Aside：首次 channel 匹配后缓存；热路径快，订阅变化后会有失效抖动；
- Record/Replay：eventlog 把 live 输入转成可重复字节流；利于分析，但不自动冻结外部时间与副作用。

## C/C++ 能力

LCM 是学习 opaque struct、函数指针 vtable、void* 类型擦除、iovec scatter/gather、网络字节序、正则缓存和回调期延迟删除的完整样本。每个语法点都对应明确的 ABI 或并发问题。

还应掌握 C 的统一 `goto fail` 回滚、fd 哨兵值、半构造对象可销毁、C++ custom deleter、trampoline callback、`extern "C"` 与 `noexcept` 边界，以及为什么 shared_ptr 不能自动解决 callback 注销同步。

## 性能与故障检查

关键变量包括 payload、分片数、发布频率、发送者数、reassembly 并发量、队列容量、subscription regex 数和 callback WCET。指标应包含 datagram 丢失、未完成重组项被 LRU 淘汰的次数、队列 drop、通知次数、handle 间隔、消息年龄和日志写入抖动。当前 UDPM 实现没有独立的重组超时计时器，不能把“长时间没收到剩余片”理解为会按固定时限立即释放内存。

故障测试要覆盖乱序/重复/缺失分片、伪造超大长度、sequence wrap、接收线程退出、通知 pipe 满、callback 内 unsubscribe、长时间不 handle、日志截断和销毁期间仍有网络数据到达。

## 性能与可行性预算

设发布频率 `f`、payload `S`、分片数 `F`、发送者数 `P`、并发未完成重组数 `M`、订阅数 `R`、单 channel 命中数 `K`：

| 路径 | 主要成本 | 空间主项 |
|---|---|---|
| 编码与发布 | `O(S) + O(F)` 系统调用/headers | 编码缓冲 `O(S)` |
| 重组 | 每片 hash 查找与复制，总体 `O(S)` | 上限近似 `O(M×Smax)` |
| 首次 channel 匹配 | `O(R×regex_cost)` | cache entry `O(K)` |
| 缓存命中分发 | `O(K + callback work)` | 共享一份 receive payload |
| receive ring | 常数索引操作 | `capacity × slot_size` |
| eventlog | header + channel + payload 顺序 I/O | 用户/系统文件缓冲 |

大消息的完整成功概率会随分片数下降；callback WCET 直接决定 handle 吞吐；重组 LRU 淘汰时机决定故障分片占用内存多久。可行性不能只测发送端 MB/s，还要同时观察消息年龄、queue drop、重组项淘汰、handle 间隔和事件循环公平性。

对于命令消息，应用应增加 sequence、deadline、去重或确认；对于状态流，应优先保证新鲜度；对于日志，应与控制路径隔离磁盘抖动。LCM 的简洁 wire 协议不会替应用做这些选择。

## 最小复刻路线

先实现进程内 channel→callback 与显式 handle；加入 provider vtable；再做单 datagram UDP 多播；随后添加接收线程、可增长 ring storage 和通知 fd，并明确完整消息队列的 subscription quota；再加入分片/重组、计数与过期策略；最后实现 schema generator 的最小编码和 eventlog。

完成条件包括：provider 部分构造失败不泄漏 fd；错误 datagram 在分配前拒绝；队列满载行为固定；callback 不在 provider 锁内执行；unsubscribe 不使遍历失效；destroy 能唤醒并 join 接收线程；相同日志能按记录顺序回放。
