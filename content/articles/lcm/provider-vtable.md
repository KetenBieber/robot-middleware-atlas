# Provider 抽象：用 C vtable 隔离传输实现

机械臂控制程序希望用同一份 `publish("ARM_STATE", bytes)` 把状态送到 UDP 多播；离线回归又希望同一调用写入日志；单元测试则希望消息只进入进程内队列。最朴素的设计是在每个公共 API 里按 transport 枚举写一串 `switch`，每加入一种传输就要改动所有调用点，还会让核心头文件依赖各实现的私有状态。LCM 的公共 API 只有一套，底层却能连接这些不同 provider；本章从这个变化压力推导出它使用的 C 函数指针表 `lcm_provider_vtable_t`。

这张表是理解 LCM 架构的最小单元。它定义了核心层能够向传输层提出哪些请求，也定义了 provider 可以把哪些差异留在自己的私有状态中。

本章所有运行时事实固定到 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`。

下面的 `switch` 是**错误的教学示例**，不是固定提交源码：


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

## 使用者真正看到的第一处“可替换”：只改 URL，为什么整套通信方式都变了？

回到实际调用。我们已经用下面的代码在局域网里收发机械臂关节状态：

~~~cpp
lcm::LCM bus("udpm://239.255.76.67:7667?ttl=1");
bus.publish("ARM_STATE", &state);
~~~

现在控制算法暂时不需要改，却要测试“断开真实网络以后，消息分发是否仍正确”。于是把构造时的 URL 换成 §memq://§；实验录制工具可能又使用日志 provider。**代码中的 §publish("ARM_STATE", ...)§ 不需要因为切换传输而增删一个参数。**

但仅仅看到 API 一致，并不能直接证明底层用了“策略模式”。假如你来写这套库，第一版完全可以在 §lcm_publish()§ 内写一个 switch，让所有请求转发给具体实现。问题是这个 switch 会慢慢扩散：§handle()§ 要区分是读文件还是等 UDP，§get_fileno()§ 要区分返回 socket 还是通知 pipe，§destroy()§ 要知道究竟释放哪些资源。传输种类每增加一次，公共层的许多函数都得一起修改。

先停下来识别一个**真正的设计边界**：公共层只关心“我要发送这一段字节”，具体传输层负责“怎么发、等什么事件、如何释放自己”。公共层不能把“所有实现都一样快、都会重试、都有同一个文件描述符”也包装成承诺。函数**签名一致**与函数**行为等价**是两回事。

### 我们先用 C 写一个最小运行时分派，而不是背 vtable 的定义

下面是一份完全独立的 C11 教学程序。它只有两个后端，§Memory§ 把最后一条消息复制到固定数组，§Drop§ 故意丢弃；不依赖 LCM，也不模拟真实网络。先观察最重要的事实：调用者只知道 §Bus§，但一个间接调用却可以走进两段不同的函数。

~~~c
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    char last[64];
    unsigned calls;
} Memory;

typedef struct {
    unsigned dropped;
} Drop;

typedef struct {
    int  (*publish)(void *self, const char *channel, const char *payload);
    void (*destroy)(void *self);
} Ops;

typedef struct {
    void *state;       /* 具体后端的对象地址 */
    const Ops *ops;    /* 借用静态函数表 */
} Bus;

static int memory_publish(void *self, const char *channel,
                          const char *payload) {
    Memory *m = self;
    int n = snprintf(m->last, sizeof m->last, "%s:%s", channel, payload);
    if (n < 0 || (size_t)n >= sizeof m->last) return -1;
    ++m->calls;
    return 0;
}

static int drop_publish(void *self, const char *channel,
                        const char *payload) {
    Drop *d = self;
    (void)channel;
    (void)payload;
    ++d->dropped;
    return 0;
}

/* 这里状态由 main 的栈对象拥有，因此销毁钩子无需 free。 */
static void borrowed_destroy(void *self) { (void)self; }

static const Ops memory_ops = {memory_publish, borrowed_destroy};
static const Ops drop_ops = {drop_publish, borrowed_destroy};

static int bus_publish(Bus *b, const char *channel, const char *payload) {
    return b->ops->publish(b->state, channel, payload);
}

int main(void) {
    Memory memory = {{0}, 0};
    Drop drop = {0};
    Bus a = {&memory, &memory_ops};
    Bus b = {&drop, &drop_ops};

    assert(bus_publish(&a, "ARM", "42") == 0);
    assert(bus_publish(&b, "ARM", "42") == 0);
    assert(strcmp(memory.last, "ARM:42") == 0);
    assert(memory.calls == 1 && drop.dropped == 1);
    puts("same API, different provider");
}
~~~

使用 §gcc -std=c11 -Wall -Wextra -Werror -pedantic§ 编译这份程序。§Bus a§ 和 §Bus b§ 的布局完全相同：都是“状态地址 + 函数表地址”。§Memory§ 与 §Drop§ 的布局完全不同：一个含字符串缓存，另一个只有计数器。核心不需要知道它们的定义。

这不是使用 §if (a==...)§ 来分派；真正的调用点是：

~~~c
b->ops->publish(b->state, channel, payload)
~~~

第一次 §bus_publish(&a, ...)§：§b->ops§ 指向 §memory_ops§，取到 §memory_publish§，§b->state§ 被恢复为 §Memory*§；第二次用同一段调用代码处理 §b§，取得的则是 §drop_publish§。从编译期角度看，§bus_publish§ 只编译一次。它的二进制不需要有 §Memory§ 和 §Drop§ 的字段访问指令，这些访问只在具体后端函数内出现。

### 代码里的函数指针，和你熟悉的普通函数到底差在哪？

§int (*publish)(void *, const char *, const char *)§ 不是“返回一个函数指针的函数”。从变量名 §publish§ 往外读：星号说明它是一个指针，圆括号让这颗星号和名字先绑定，后面参数列表说明指向的是一类函数，最外层 §int§ 是被指函数的返回值。

~~~c
int  publish(void *, const char *);     /* 函数声明 */
int (*publish)(void *, const char *);   /* 指向函数的变量 */
~~~

§Ops§ 中每个字段都固定了签名。具体函数地址装入 §memory_ops§ 这张静态表以后，§Bus§ 只需要记住表的位置，而不需要在每个实例里复制两份函数指针。表本身属于静态存储期；上面的 §Memory memory§ 则属于 §main§ 的自动存储期。因而不能把两者误认为相同的“拥有者”：§ops§ 是借用，§state§ 由谁释放还得额外约定。

这也揭示了 vtable 最危险的一种错误：如果拿 §Drop*§ 冒充 §Memory*§，却同时把 §memory_ops§ 传给 Bus，那么 §memory_publish§ 会把错误布局当成 Memory 读取，触发未定义行为。§void*§ 让公共 API 避免依赖具体实现，也同时把“状态类型与函数表必须配对”的责任交给**唯一的构造入口**。因此实际框架里还要有 URL 解析和 provider 工厂，而不能让用户任意拼两根地址。

### 从自己的两字段 Bus，映射回 LCM 真正的三个对象

固定源码里的第一份证据是 §lcm_t§，它确实同时存有：

~~~c
lcm_provider_vtable_t *vtable;
lcm_provider_t *provider;
~~~

第二份证据是 §lcm_create()§。它先调用内建的 provider 注册函数，把各实现的 §name§ 和 §vtable§ 收集到 §providers§ 数组；解析 URL 得到 scheme 后，线性寻找匹配名称。找到 §info§ 以后，才装配下面这对字段：

~~~c
lcm->vtable = info->vtable;
lcm->provider = info->vtable->create(lcm, network, args);
~~~

这与我们的教学 §Bus§ 还差一个重要环节：**工厂必须把“正确的对象”与“正确的表”绑定起来。** §info->vtable§ 来自匹配的 scheme，§create()§ 再产生那一类 provider 的实例。没有这一层，类型擦除会失去安全配对的来源。

以 UDPM 为例，源码中 §lcm_udpm_t§ 实际是 §struct _lcm_provider_t§ 的 typedef；其函数表静态初始化为 §create/destroy/subscribe/publish/handle/get_fileno§ 等对应函数。它的私有对象内部拥有 §sendfd§、§recvfd§、两条控制/通知 pipe、接收线程、消息缓存队列、§transmit_lock§ 和其他状态。你在调用 §publish§ 时无需知道这些成员；真正访问它们的，是表项指向的 §lcm_udpm_publish§。

最后把整个构造和发送放到同一条时间轴上：

~~~text
构造：lcm::LCM("udpm://...")
    |
    v
lcm_create -> parse URL -> find udpm_info
    |
    | 保存 udpm_vtable
    v
udpm_vtable.create -> 申请 UDPM 实例
    |
    | 返回 provider 指针
    v
lcm_t{vtable, provider}

发送：LCM::publish("ARM_STATE", &state)
    |
    | generated encode -> 临时 byte buffer
    v
lcm_publish -> vtable.publish(provider, ...)
    |
    | provider 为 UDPM 实例
    v
lcm_udpm_publish -> socket send path
~~~

此刻才可以给它命名：具体 provider 承担传输 **Strategy**，§lcm_create§ 里的 scheme 匹配和实例创建承担 **Factory**。不过 LCM 的内建 provider 是编译期注册，不是任意动态库插件；§Ops§ 所有字段也是源码内部约定，不等于对外承诺了一套可无缝升级的第三方 ABI。


## vtable 的完整契约

固定版本的 C 核心用以下方法表约定 provider 的操作：


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

如果第一次看到 `(*publish)(...)`，先不要把它当成“复杂 C 语法”。我们其实只需要解决两个问题：`publish` 的调用者不知道当前装的是 UDP 还是日志；不同实现又必须保留各自的 socket、文件句柄或队列。前者要求**统一操作签名**，后者要求**实例状态仍属于具体实现**。

把这两件事分开，就得到 `(vtable, provider)` 这一对值。`vtable` 指向一组同签名的函数，`provider` 指向实际状态。调用时，核心把实例指针显式传回函数；这恰好对应 C++ 非静态成员函数隐含的 `this`：

~~~c
/* 教学缩写：展示 C API 的运行时分派，不是 LCM 逐字源码。 */
int bus_publish(lcm_t *bus, const char *channel,
                const void *bytes, unsigned int length) {
    return bus->vtable->publish(bus->provider, channel, bytes, length);
}
~~~

这里有两次间接访问，而不是把消息“复制进一个多态对象”：先从 `bus` 取方法表，再按 `publish` 的固定偏移拿到函数地址，最后把同一个 `bus->provider` 原样传进去。方法表本身可以放在静态存储区，每个 Bus 无需保存一份函数指针数组。真正的序列化已经在调用公共 `publish` 之前完成，函数指针这一跳不会因此再复制 payload。

### 先写一个能运行的 C++17 缩小版

C++ 的虚函数也能完成运行期分派，但如果只展示 `virtual Publish() = 0`，容易把**动态选择行为**和**负责对象寿命**混在一起。下面刻意保留 C 风格的函数表，并把资源所有权放进另一只 C++ 对象。它与 LCM 使用同一个设计原理，但它是独立的教学实现，不依赖 GLib 或 LCM。

~~~cpp
#include <cassert>
#include <string>
#include <string_view>
#include <utility>

// 方法表仅说明可以执行的动作，不拥有具体传输状态。
struct Ops {
    bool (*publish)(void*, std::string_view channel,
                    std::string_view payload);
    void (*destroy)(void*) noexcept;
};

struct MemoryProvider {
    std::string last;
};

bool memory_publish(void* raw, std::string_view channel,
                    std::string_view payload) {
    auto& self = *static_cast<MemoryProvider*>(raw);
    self.last = std::string(channel) + ":" + std::string(payload);
    return true;
}

void memory_destroy(void* raw) noexcept {
    delete static_cast<MemoryProvider*>(raw);
}

const Ops memory_ops{&memory_publish, &memory_destroy};

class Bus {
public:
    Bus(void* state, const Ops* ops) noexcept
        : state_(state), ops_(ops) {}

    ~Bus() { reset(); }

    Bus(const Bus&) = delete;
    Bus& operator=(const Bus&) = delete;

    Bus(Bus&& other) noexcept
        : state_(std::exchange(other.state_, nullptr)),
          ops_(std::exchange(other.ops_, nullptr)) {}

    Bus& operator=(Bus&& other) noexcept {
        if (this != &other) {
            reset();
            state_ = std::exchange(other.state_, nullptr);
            ops_ = std::exchange(other.ops_, nullptr);
        }
        return *this;
    }

    bool publish(std::string_view channel, std::string_view payload) {
        return state_ && ops_->publish(state_, channel, payload);
    }

private:
    void reset() noexcept {
        if (state_) ops_->destroy(state_);
        state_ = nullptr;
        ops_ = nullptr;
    }

    void* state_ = nullptr;      // 只有持有者负责交给 destroy
    const Ops* ops_ = nullptr;   // 借用静态方法表
};

int main() {
    auto* memory = new MemoryProvider;
    Bus bus(memory, &memory_ops);
    assert(bus.publish("ARM_STATE", "42"));
    assert(memory->last == "ARM_STATE:42");

    Bus next = std::move(bus);
    assert(!bus.publish("ARM_STATE", "43"));  // 旧句柄已失效
    assert(next.publish("ARM_STATE", "44"));
}  // next 析构一次，memory_destroy 删除 MemoryProvider
~~~

这份代码可以直接使用 `g++ -std=c++17 -Wall -Wextra` 编译。先看最容易被忽略的四条对象不变量：

1. `Bus` 是唯一 owner：禁用复制，允许移动。`std::move` **只把左值转换成可移动的值类别**，实际转移由移动构造里的 `std::exchange` 完成；它在取走源指针的同时把源句柄置空，保证只有新 owner 负责调用 `destroy`。
2. `Ops` 不拥有 `MemoryProvider`；这里它是静态生存期的 `memory_ops`，所以 `Bus` 内保存 `const Ops*` 安全。如果把指向局部变量 `Ops local` 的地址塞给 Bus，再从函数返回，就会留下悬空方法表指针，即使 `state_` 仍然有效也无法调用。
3. `void*` 是刻意进行的类型擦除：调用端不知道 `MemoryProvider` 的大小和成员；`memory_publish` 用 `static_cast` 恢复真实类型，但它**没有运行时类型检查**。若把另一类对象和这张方法表拼成一对，编译器不会发现，解引用就是未定义行为。因此“哪个 factory 返回的状态配哪张表”是必须由构造层守住的不变量。
4. `std::string_view` 只借用消息内存；此处 `memory_publish` 立即复制成 `std::string`，所以调用方局部字符串可以在返回后销毁。若把 view 放进后台队列，它就可能在调用方销毁 payload 后悬空。LCM 的公共边界使用 `const void* + length`，同样不能凭参数类型推断异步传输是否已经取得独立 payload 所有权。

这里 `destroy` 是普通函数指针而不是 C++ `virtual` 析构函数，所以不依赖对象内部的隐式 vptr；但整个 `Ops` 表格仍须按约定的布局编译。上述 C++ 玩具接口包含 `std::string_view`，**不应用作跨编译器的 C ABI**：它只是用来验证移动、类型擦除和析构；LCM 的实际边界使用 C 指针、整数和不透明类型。

### 什么时候使用虚函数，什么时候保留显式方法表

若运行时只在一个统一 C++ ABI 的进程里扩展传输，基类 `virtual publish` 加 `std::unique_ptr<Provider>` 更容易维护：对象的动态类型决定虚函数表，析构自动沿虚析构函数释放正确的派生类。但对外提供稳定 C ABI、需要 C/Python/Java 绑定，或希望核心完全隐藏各 provider 头文件时，`void*/lcm_provider_t*` 加显式函数表的边界更清楚。

这一步才可以把设计归纳为 **Strategy**：同一个 Bus 可以通过不同 provider 满足同一组操作。URL 在创建时选择 `provider_info` 和方法表，又对应 **Factory**。两者并不等价：Factory 决定对象如何出生，Strategy 决定出生后 `publish` 如何执行，RAII 再决定什么时候安全销毁。LCM 的 provider 随 URL 创建后不会因每条消息自动切换；要替换它，应新建相应实例并协调旧实例的收尾，而不是在有活跃回调时直接重写 `lcm->vtable`。
## 不透明 provider 指针完成类型擦除

现在已经看过可运行的双后端例子，再回到 LCM，确认类型擦除究竟怎样落在 C 语言里。公共内部头文件先只声明一个尚未给出布局的结构体：

~~~c
typedef struct _lcm_provider_t lcm_provider_t;
~~~

这里有个看似细小、实际上关系到 C 类型系统的问题：UDPM 实现不是定义一个无关结构再做不安全的函数指针强制转换，而是给**同一个结构体标签**取了一个本地别名。固定提交的原始结构体声明以如下字段开头：

~~~c
typedef struct _lcm_provider_t lcm_udpm_t;
struct _lcm_provider_t {
    SOCKET recvfd;
    SOCKET sendfd;
    struct sockaddr_in dest_addr;
    lcm_t *lcm;
    // 其后省略原结构体中的参数、队列、接收线程、锁及 pipe 等成员。
};
~~~

上面特意只展示真实声明的**连续前缀**；省略符号是教学注释，不应把它当作完整的 UDPM 对象。因为两个 typedef 指向同一标签，`lcm_provider_t*` 与 `lcm_udpm_t*` 在 UDPM 实现单元里是兼容的指针类型。实际函数 `lcm_udpm_publish(lcm_udpm_t*, const char*, const void*, unsigned int)` 可以直接赋给方法表的 `publish`，不需要绕过编译器的函数指针强制转换。

非 Windows 构建分支的真实方法表是：

~~~c
static lcm_provider_vtable_t udpm_vtable = {
    .create = lcm_udpm_create,
    .destroy = lcm_udpm_destroy,
    .subscribe = lcm_udpm_subscribe,
    .unsubscribe = NULL,
    .publish = lcm_udpm_publish,
    .handle = lcm_udpm_handle,
    .get_fileno = lcm_udpm_get_fileno,
};
~~~

Windows 分支因编译器兼容性问题在初始化函数里逐字段赋值，但对应操作相同。注意 `unsubscribe = NULL`：这是一种**可选操作**，公共层必须先检查函数指针是否存在才可调用。UDPM 使用共享多播接收资源，单个 channel 的逻辑取消订阅仍交给核心订阅表，而不是逐 channel 断开网络连接。
这种不透明句柄有三项价值：

- `lcm.c` 不需要包含每种 provider 的私有头文件；
- provider 可以自由改变内部布局，不破坏公共 `lcm_t` ABI；
- 公共调用路径不需要 `switch(provider_kind)`。

代价是类型检查较弱。函数指针声明与实际函数签名若通过不安全 cast 拼接，编译器可能无法阻止 ABI 错误。因此方法表定义应集中、开启严格警告，并避免把不兼容函数强制转换进去。

## provider_info 连接 URL scheme 与方法表

每个实现向临时数组加入一项：


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


```c
struct provider_vtable {
  uint32_t abi_version;
  uint32_t struct_size;
  // function pointers...
};
```

新核心可以根据 `struct_size` 判断尾部方法是否存在；旧 provider 仍保持前缀兼容。也可以让 provider 导出单一入口：


```c
const provider_vtable* provider_get_api(uint32_t requested_version);
```

LCM 当前内建 provider 随同核心一起编译，版本错配风险较低。自行复刻为外部插件系统时，版本字段不应省略。

## 如果我们替换成一个新的 provider，哪些地方必须一起检查？

现在回到真正的使用者需求：原程序通过 `udpm://` 向局域网广播状态，离线测试想改用内存队列。这不只是替换一只 `publish` 函数指针。一个合格的 provider 至少要回答三个配套问题：**谁持有自己的状态、怎样让应用感知可读事件、何时能够安全销毁状态**。

在 C 层，`lcm_t` 保存 provider 实例与其 vtable。工厂通过 URL scheme 创建这对相互匹配的对象，此后公共代码只经函数指针进入 `publish / subscribe / handle / get_fileno / destroy`。这些方法的签名稳定，但实现语义可能不同：UDPM 的 `handle` 取网络线程准备好的完整消息，日志 provider 可能从文件读取事件，内存 provider 则可能从本进程队列取数据。**统一接口不能推导出统一阻塞行为、数据可靠性或消息时间来源。**

再做一次朴素实现反例。假设新 provider 的 `publish` 把调用者提供的 `const void* data` 保存进内部队列，准备等 `handle` 才交付。调用者可能是 C++ 模板发布函数；该函数调用底层 `publish` 返回后立刻执行 `delete[] buf`。队列里留下的地址于是指向已经释放的内存。新 provider 必须在返回前**复制 payload 或取得等价的独立所有权**，不能因为函数签名含 `const` 就以为指针永远有效。

销毁时同样不能只写 `free(provider)`：如果内部有接收线程，先关闭/唤醒并等待线程结束，再回收线程仍可能访问的队列、socket、pipe 与其他状态；如果 callback 可能持有业务 userdata，调用者还须在销毁业务对象之前完成 `handle` 循环的停机协调。C 的不透明指针、函数表与 C++ RAII 只能明确资源归属，不能自动替应用建立跨线程的停止协议。

因此本章的复刻验收不是“能从两个函数里任选一个”。先使用内存与丢弃两个后端复用同一公共 API，再加入能够被外部事件循环观察的 ready 条件，然后故意在 `publish` 返回后立即销毁调用者原始缓冲，验证队列内容仍正确。最后让接收循环在阻塞状态接到停止请求，证明自己设计的销毁协议不会造成悬空地址与未退出线程。

## 最小复刻顺序

可以用以下结构实现最小 provider 层：


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
