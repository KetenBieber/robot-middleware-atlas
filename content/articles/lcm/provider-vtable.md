# Provider 抽象：用 C vtable 隔离传输实现

机械臂控制程序希望用同一份 `publish("ARM_STATE", bytes)` 把状态送到 UDP 多播；离线回归又希望同一调用写入日志；单元测试则希望消息只进入进程内队列。最朴素的设计是在每个公共 API 里按 transport 枚举写一串 `switch`，每加入一种传输就要改动所有调用点，还会让核心头文件依赖各实现的私有状态。LCM 的公共 API 只有一套，底层却能连接这些不同 provider；本章从这个变化压力推导出它使用的 C 函数指针表 `lcm_provider_vtable_t`。

这张表是理解 LCM 架构的最小单元。它定义了核心层能够向传输层提出哪些请求，也定义了 provider 可以把哪些差异留在自己的私有状态中。

本章所有运行时事实固定到 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`。

下面的 `switch` 是**错误的教学示例**，不是固定提交源码：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
/* 错误示例：每个公共操作都要知道所有传输类型。 */
int publish(Bus *bus, const char *channel, const void *data, size_t size) {
    switch (bus->kind) {
    case UDP: return udp_publish(bus->udp, channel, data, size);
    case LOG: return log_write(bus->log, channel, data, size);
    case MEM: return memq_push(bus->mem, channel, data, size);
    }
    return -1;
}
```

当选择依据稳定但实现可替换时，核心只需保存“实例状态”和“该实例对应的一组操作”。接下来的 C 指针与调用表就是解决这个具体变化点的最小方案；它并不自动统一三种 provider 的阻塞、丢弃或持久化语义。

## vtable 的完整契约

固定版本的 C 核心用以下方法表约定 provider 的操作：

**代码身份：固定提交源码摘录，来自 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`，符号 `_lcm_provider_vtable_t`，逐字连续定义。**

```c
struct _lcm_provider_vtable_t {
    lcm_provider_t *(*create)(lcm_t *, const char *target, const GHashTable *args);
    void (*destroy)(lcm_provider_t *);
    int (*subscribe)(lcm_provider_t *, const char *channel);
    int (*unsubscribe)(lcm_provider_t *, const char *channel);
    int (*publish)(lcm_provider_t *, const char *, const void *, unsigned int);
    int (*handle)(lcm_provider_t *);
    int (*get_fileno)(lcm_provider_t *);
};
```

可以把它看作手写的接口类：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
class Provider {
 public:
  virtual ~Provider() = default;
  virtual int Subscribe(std::string_view channel) = 0;
  virtual int Publish(std::string_view channel,
                      std::span<const std::byte> data) = 0;
  virtual int HandleOne() = 0;
  virtual int FileDescriptor() = 0;
};
```

C 版本把对象指针与方法表分开保存。每个函数的第一个参数相当于 C++ 隐式的 `this`。

## 不透明 provider 指针完成类型擦除

核心层只声明：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
typedef struct _lcm_provider_t lcm_provider_t;
```

它不知道结构体字段。UDPM 实现可以把自己的 `lcm_udpm_t*` 转成 `lcm_provider_t*` 返回，调用时再转回：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
static int lcm_udpm_publish(lcm_udpm_t *udpm, ...);

static lcm_provider_vtable_t udpm_vtable = {
    .create = lcm_udpm_create,
    .destroy = lcm_udpm_destroy,
    .publish = lcm_udpm_publish,
    // ...
};
```

这种不透明句柄有三项价值：

- `lcm.c` 不需要包含每种 provider 的私有头文件；
- provider 可以自由改变内部布局，不破坏公共 `lcm_t` ABI；
- 公共调用路径不需要 `switch(provider_kind)`。

代价是类型检查较弱。函数指针声明与实际函数签名若通过不安全 cast 拼接，编译器可能无法阻止 ABI 错误。因此方法表定义应集中、开启严格警告，并避免把不兼容函数强制转换进去。

## provider_info 连接 URL scheme 与方法表

每个实现向临时数组加入一项：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
struct _lcm_provider_info_t {
    char *name;
    lcm_provider_vtable_t *vtable;
};
```

`lcm_create()` 顺序调用各 provider 的初始化函数，再按 URL scheme 查找名称。传入的是可选 URL；没有显式值时先读 `LCM_DEFAULT_URL`，仍未设置才使用内置 UDPM 地址：

```text
udpm://... -> "udpm" -> &udpm_vtable
tcpq://... -> "tcpq" -> &tcpq_vtable
memq://... -> "memq" -> &memq_vtable
```

这里是一个静态 provider 表：实现被编译进 liblcm，初始化函数显式加入数组。它不提供运行期扫描任意 `.so` 的扩展点；若系统需要独立部署第三方 transport，工程上可以改为动态库工厂，但还必须定义 ABI 版本、符号发现、依赖与卸载期间对象存活规则。

静态注册的优点是装载失败面小、部署简单、调用符号确定。缺点是新增 provider 需要修改核心初始化列表并重新链接。

## create 同时接收核心对象与 URL 参数

provider `create` 的输入包括：

- `lcm_t*`：回调核心订阅管理时使用；
- `target`：URL 中 `://` 与 `?` 之间的主体；
- `args`：查询参数 hash table。

UDPM 可以从 target 解析 multicast 地址和端口，从 args 读取 TTL、接收缓冲大小等选项。核心解析 URL 语法，provider 解释参数语义。

这种分层避免每个传输重复实现字符串切分，也避免核心层理解 `ttl`、TCP reconnect 或日志 replay speed。

`create` 返回空表示装配失败。顶层随后调用 `lcm_destroy()` 回收已经初始化的核心容器，所以 provider 构造函数也必须能清理部分创建的 socket、mutex 和 pipe。固定实现的 `lcm_create()` 连续代码同时展示资源装配和失败路径：

## publish 是同步策略入口

公共函数：

**代码身份：固定提交源码摘录，来自 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`，符号 `lcm_publish()`，逐字连续函数体。**

```c
return lcm->vtable->publish(
    lcm->provider, channel, data, datalen);
```

没有额外 worker 或统一队列。这使函数指针边界非常清晰，却让不同 provider 的行为差异直接暴露：

| Provider | publish 的主要动作 | 常见阻塞来源 |
|---|---|---|
| UDPM | 组 header、分片、`sendmsg` | transmit mutex、socket buffer |
| TCPQ | 向 queue server 发送 | 连接状态、TCP 缓冲 |
| MEMQ | 写进程内队列 | queue mutex/容量 |
| LOGFILE | 写 event log | 文件系统 I/O |

因此调用者不能只凭 `lcm_publish()` 的统一签名假设实时上界。provider URL 是部署配置，也是执行模型的一部分。

## subscribe 不等于创建独立网络订阅

核心层先调用 provider `subscribe`，再创建 `lcm_subscription_t`。对 UDPM 来说，所有 channel 通常共享同一个 multicast socket，`subscribe` 的主要作用是确保接收资源已经建立；channel 过滤在核心层完成。

TCPQ 或其他 provider 可以把 subscription 转发给远端 server，让 server 只发送相关 channel。相同接口允许“本地过滤”和“网络侧过滤”两种实现。

这说明接口语义应描述结果，而非内部动作：调用成功意味着 provider 已准备好让该 channel 的消息进入核心，不意味着一定创建了新 socket。

## handle 的单位是一条完整消息

Provider `handle()` 不负责无限事件循环。一次调用通常消费一条已经准备好的消息并触发核心分发，然后返回。

这个粒度允许应用控制公平性：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```cpp
while (running) {
  HandleGuiEvents();
  lcm_handle_timeout(bus, 2);
  RunControlStepIfDue();
}
```

如果 handle 一次排空全部 backlog，突发消息可能长期饿死 GUI 或控制定时器。一次一条虽然增加函数调用次数，却给上层 reactor 留出调度边界。

Provider 在调用核心 handler 前必须保证 payload 已完整、channel 已解析，并且 buffer 在全部 callback 返回前有效。

## get_fileno 抽象 ready 事件

`get_fileno()` 暴露 provider 选择的 ready 事件源，而不是规定好的网络 socket。对 UDPM，返回 notify pipe 读端；当它可读，队列至少有一条完整消息，调用一次 `handle()` 不必等待新网络数据。其他 provider 的 ready 与阻塞语义需按具体实现核对，vtable 的函数签名本身没有编码“永不阻塞”保证。

UDPM 返回 notify pipe；日志 provider 可以返回自己的通知 fd；其他实现也可使用 socket 或 eventfd。核心层只把整数交给 `select()`。

这是一种 readiness abstraction：API 暴露可组合事件源，而不泄漏 provider 内部接收线程和队列。相比让每个 provider 提供 `Wait(timeout)`，文件描述符更容易与 Unix 现有 reactor 组合。

Windows 上没有完全相同的 POSIX pipe/select 语义，因此内部封装 `lcm_internal_pipe_*` 处理平台差异。跨平台抽象的真实成本往往集中在等待原语，而不只是 socket API。

## 核心回调接口形成反向依赖

Provider 收到消息后需要使用核心订阅表，但不应知道 `handlers_map` 的布局。`lcm_internal.h` 暴露三个窄函数：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
int lcm_try_enqueue_message(lcm_t*, const char* channel);
int lcm_has_handlers(lcm_t*, const char* channel);
int lcm_dispatch_handlers(lcm_t*, lcm_recv_buf_t*,
                          const char* channel);
```

依赖方向因此为：

```text
public API -> provider vtable -> provider implementation
                       |
                       `-> narrow core callbacks for subscriptions
```

Provider 不直接修改 subscription 结构，核心也不直接修改 UDP ring。这种双向协作通过小接口完成，而不是互相包含全部内部状态。

## 错误码保持 C ABI 简单

方法大多返回 `int`，零表示成功，负值或系统调用结果表示失败。它避免跨语言、跨编译器传播 C++ 异常，也使 provider ABI 易于绑定。

但单个整数通常缺少上下文。工业实现应在 provider 边界补充：

- 可查询的最后错误类别；
- provider 名称和 target；
- 系统 errno；
- 丢包、坏报文和队列溢出计数；
- 当前连接或接收线程状态。

否则上层只能看到 `publish returned -1`，无法区分地址无效、socket buffer 满或连接已断。

## 销毁顺序由顶层统一控制

`lcm_destroy()` 先尝试取消订阅，再调用 provider `destroy`：

```text
unsubscribe handlers (fixed loop may skip entries as its array shrinks)
  -> provider unsubscribe hooks where invoked
provider destroy
  -> stop receive thread
  -> wake blocking select
  -> join thread
  -> close socket/pipes
free handler maps and mutexes
free lcm_t
```

provider 内部线程持有 `lcm_t*`，所以核心对象不能先释放。反过来，subscription 的 provider 资源又需要在 provider 销毁前撤销。固定版 `lcm_destroy()` 的 `for` 循环在 `handlers_all` 上递增索引，同时 `lcm_unsubscribe()` 会缩短该数组，所以多订阅时可能跳过部分 provider unsubscribe hook；随后 provider 的 destroy 必须负责最终收尾。`lcm_destroy()`、`lcm_unsubscribe()`。此外，`lcm_destroy()` 不等待另一线程正在执行的 `lcm_handle()`，应用要先停止并 join 自己的 handle loop。

如果接收线程阻塞在 socket 上，destroy 必须先让其等待对象可读，再 join；只设置布尔变量而不改变等待对象状态会让 join 一直等下去。UDPM 正常路径在控制 pipe 写入退出字节，等待 `select()` 返回后 join；但固定版本在 pipe 写失败时跳过 join，仍继续释放队列与 fragment store，因此错误路径并没有完整的生命周期屏障。`_destroy_recv_parts()`。

## C vtable 与 C++ 多态的比较

C vtable 的优点：

- ABI 布局显式；
- 可被 C 和多语言 FFI 直接调用；
- 对象可以完全不透明；
- 不依赖 RTTI、异常或 C++ 标准库 ABI；
- 方法表可按版本扩展和检查。

它的缺点：

- 缺少自动类型检查和析构规则；
- 每个函数都要手动传 self；
- 空函数指针必须逐项检查；
- 资源安全依赖人工 goto/cleanup；
- 接口演化需要维护 struct size/version。

C++ 虚基类提供更强语言支持，但跨共享库、跨编译器或跨语言时，C ABI 往往更稳定。LCM 选择 C core，也为多语言绑定减少了边界复杂度。

## 接口扩展的 ABI 规则

若外部 provider 可以独立编译，直接在 vtable 中间插入字段会改变后续函数指针偏移。更稳健的设计通常加入：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
struct provider_vtable {
  uint32_t abi_version;
  uint32_t struct_size;
  // function pointers...
};
```

新核心可以根据 `struct_size` 判断尾部方法是否存在；旧 provider 仍保持前缀兼容。也可以让 provider 导出单一入口：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
const provider_vtable* provider_get_api(uint32_t requested_version);
```

LCM 当前内建 provider 随同核心一起编译，版本错配风险较低。自行复刻为外部插件系统时，版本字段不应省略。

## 最小复刻顺序

可以用以下结构实现最小 provider 层：

**代码身份：教学摘录（节选或改写以解释机制，不是固定提交的逐字连续源码）。**

```c
typedef struct bus bus_t;
typedef struct transport transport_t;

typedef struct {
  transport_t* (*create)(bus_t*, const char* target);
  void (*destroy)(transport_t*);
  int (*send)(transport_t*, const char*, const void*, size_t);
  int (*handle_one)(transport_t*);
  int (*ready_fd)(transport_t*);
} transport_ops_t;
```

先实现 `memq`，验证 vtable、订阅和 handle 语义；再实现 UDP 单报文；随后加入接收线程与通知 pipe；最后才加入分片、日志和远端订阅优化。

每一步只增加一种复杂度：

```text
function dispatch
  -> byte transport
  -> cross-thread queue
  -> network framing
  -> fragmentation and recovery
```

Provider 层的成功标准不是支持多少协议，而是公共核心无需知道任何具体传输结构，同时每种传输的阻塞、队列和关闭语义仍然能够被准确说明。
