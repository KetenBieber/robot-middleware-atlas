# LCM C ABI 与 C++ 封装：从函数指针表到类型安全句柄

LCM 的核心运行时主要使用 C。它要在很小的依赖面上支持不同 provider、事件循环和语言绑定，因此没有依赖 C++ 虚函数，而是手工组织对象、方法表和生命周期。理解这一层以后，C++ 包装器为何薄、哪里必须做 RAII、哪里绝不能抛异常才会变得清楚。

如果“函数指针表”“类型擦除”“不透明句柄”这些词还不熟悉，可以先把它们暂时翻译成一个朴素目标：**LCM 想让同一个 `lcm_publish()` 调用，既能交给 UDP multicast 实现，也能交给日志、测试或其他 provider 实现。** C 语言没有成员函数和虚函数，项目只能把“对象的数据”和“对象会做的事”手工放在一起。

可以先把 C++ 写法和 C 写法并排看：


```cpp
// C++ 的直觉写法
provider->publish(channel, bytes);
```


```c
/* C 中把隐含的 this 显式写成第一个参数 */
provider->ops->publish(provider, channel, bytes, size);
```

这里的 `provider` 保存实例状态，类似 C++ 的 `this`；`ops` 指向一张共享方法表，类似虚函数表；`publish` 槽位保存具体实现的函数地址。先看懂这一次间接调用，后面的 opaque type、首成员转换和 vtable 才有落脚点。

本章用一条消息贯穿两种语言边界：C++ 生成类型先编码成字节，C 核心通过 provider 发出；接收时，C 核心先获得一块借用的 payload，再经 trampoline 找回 C++ 对象并调用成员函数。每跨一次边界，都要重新回答“谁拥有内存、错误怎样返回、回调结束后地址还是否有效”。

本文固定源码为 `ad0c54ce`。建议按下面的纵向关系阅读，而不是先遍历目录：

| 层次 | 源码入口 | 主要职责 |
|---|---|---|
| 公共 C API | `lcm.c` | 创建、发布、订阅、handle 与分发 |
| provider 契约 | `lcm_internal.h` | 函数指针表和不透明 provider 句柄 |
| UDPM 实现 | `lcm_udpm.c` | socket、分片、重组、接收通知 |
| C++ 包装 | `lcm-cpp.hpp` | RAII、成员函数回调和生成类型适配 |
| 类型生成 | `emit_c.c` | 编解码、哈希、大小计算和清理函数 |

一条消息经过两条不同方向的链：

```text
publish:
typed C++ message -> generated encode -> lcm_publish
  -> provider->publish -> UDPM short frame / fragments -> socket

receive:
socket -> provider receive/reassembly -> notification fd
  -> lcm_handle -> channel match -> C callback trampoline
  -> generated decode -> C++ member handler
```

前一条链关注编码与线格式，后一条链关注事件循环、借用 payload、回调寿命和可重入注销。只有两条都闭环，才算理解 LCM 的对象模型。

## C 中如何构造“对象”

一个最小 provider 抽象由两部分组成：私有状态和操作表。


```c
typedef struct lcm_provider lcm_provider_t;

typedef struct {
    lcm_provider_t *(*create)(const char *target);
    void (*destroy)(lcm_provider_t *self);
    int (*publish)(lcm_provider_t *self, const char *channel,
                   const void *data, unsigned int size);
    int (*handle)(lcm_provider_t *self);
    int (*get_fileno)(lcm_provider_t *self);
} provider_vtable_t;
```

先逐字段读这段声明。`typedef struct lcm_provider lcm_provider_t;` 只告诉调用者“存在这样一种类型”，却不公开字段，因此它是一个不透明类型。方法表里的每一项都是函数指针：括号里的 `*publish` 表示该字段保存函数地址，而不是立即调用函数。所有操作都显式接收 `self`，因为 C 没有隐含的 `this`。

若暂时只实现 create、publish 和 destroy，这套结构已经能工作：工厂创建一个具体 provider，把对应方法表地址放进对象；公共 API 只经由方法表调用；销毁时再调用同一实现提供的 destroy。事件循环、订阅与回调是后续叠加的能力，不是理解第一层动态分派的前置条件。

函数的第一个参数 `self` 就是 C++ 非静态成员函数中隐含的 `this`。`lcm_provider_t` 只做前置声明，核心层看不到内部字段；UDPM provider 可以拥有 socket、接收线程和重组表，日志 provider 则可以拥有文件句柄和播放时钟。

这种设计同时完成了动态分派与类型擦除，但类型安全弱于 C++ 虚函数。每个表项的声明、初始化和调用必须严格一致，不能用不兼容的函数指针强制转换来“消除警告”。

### 不透明类型与具体对象如何关联

典型具体实现会把公共基部放在首字段，或让外层单独保存 vtable 与私有指针：


```c
struct lcm_provider {
    const provider_vtable_t *ops;
};

typedef struct {
    lcm_provider_t base;   /* 必须保持契约位置 */
    int fd;
    pthread_t receiver;
    fragment_table_t fragments;
} udpm_t;
```

当 `base` 是首成员时，C 标准保证结构地址与首成员地址相同，因而可在受控边界把 `udpm_t*` 转成 `lcm_provider_t*`。反向转换只有在动态对象确实是 udpm_t 时才成立；C 没有 dynamic_cast，正确性依赖工厂为该 ops 创建对应对象。

更稳健的接口把 `void *impl` 放在公共壳中，避免依赖首成员约定；代价是每次访问多一次间接寻址。无论选择哪一种，都应提供单一转换 helper，不要在代码各处散落强制转换。

### 函数指针调用如何发生


```c
int lcm_publish(lcm_t *lcm, const char *channel,
                const void *data, unsigned int size) {
    if (!lcm || !channel || (!data && size != 0)) return -1;
    return lcm->provider->ops->publish(
        lcm->provider, channel, data, size);
}
```

公共入口先验证 ABI 边界参数，再通过 ops 间接分派。`const void*` 只说明 publish 不应修改输入字节，不携带元素类型、长度或寿命；size 必须与实际缓冲一致，provider 不能在函数返回后继续保存裸指针，除非 API 明确把所有权转移给它。

## 方法表为何应是只读静态对象


```c
static const provider_vtable_t udpm_ops = {
    .create = udpm_create,
    .destroy = udpm_destroy,
    .publish = udpm_publish,
    .handle = udpm_handle,
    .get_fileno = udpm_get_fileno,
};
```

方法表不包含单个连接的状态，因此所有 UDPM 实例可以共享一份。`static` 把符号限制在当前翻译单元，`const` 防止运行期误改函数指针。指定初始化器让字段与函数按名字对应，比依赖结构体字段顺序更容易审查。

新增 vtable 字段会改变结构大小和布局。如果 provider 作为独立动态库加载，这就是 ABI 变更；仅在同一版本内静态编译则风险较低。稳定插件 ABI 常使用 `abi_version`、`struct_size`，并只读取双方都理解的前缀。

一个可演进的表头可以写成：


```c
typedef struct {
    uint32_t abi_version;
    uint32_t struct_size;
    void (*destroy)(lcm_provider_t *);
    int (*publish)(lcm_provider_t *, const char *,
                   const void *, unsigned int);
    /* 新字段只追加在尾部 */
} provider_api_v1;
```

加载方先检查主 ABI 版本，再用 `struct_size >= offsetof(...)+sizeof(field)` 判断可选尾字段是否存在。字段只能追加，不能在中间插入、重排或改变签名。还要固定整数宽度、调用约定、结构对齐和谁分配谁释放；仅有版本号不能弥补两边 ABI 不同。

C 指定初始化器在这里尤其重要。若只按位置初始化，尾部新增字段虽然默认置零，但一次字段重排就可能把 destroy 函数放进 publish 槽位，编译器未必能在所有强转场景发现。

## 创建失败必须回滚已经取得的资源

C 没有析构函数，构造过程要显式维护“已经成功到哪一步”：


```c
static lcm_provider_t *udpm_create(const char *target) {
    udpm_t *p = calloc(1, sizeof(*p));
    if (!p) return NULL;

    p->fd = -1;
    if (parse_target(target, &p->addr) < 0) goto fail;
    p->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (p->fd < 0) goto fail;
    if (start_receiver(p) < 0) goto fail;
    return (lcm_provider_t *)p;

fail:
    udpm_destroy((lcm_provider_t *)p);
    return NULL;
}
```

把无效文件描述符初始化为 `-1`，使 `destroy` 能对半构造对象安全执行。`calloc` 的零初始化不是完整的状态设计：零有时是合法文件描述符，所以仍需显式哨兵值。

`goto fail` 在这里不是失控跳转，而是把多出口错误处理汇聚到一个逆序清理点。若每一步分别 `free`，以后在中间新增资源时更容易漏掉某条失败路径。

### destroy 必须接受每一种半构造状态


```c
static void udpm_destroy(lcm_provider_t *base) {
    if (!base) return;
    udpm_t *p = udpm_from_base(base);

    if (p->receiver_started) {
        atomic_store(&p->stopping, true);
        wake_receiver(p);
        pthread_join(p->receiver, NULL);
    }
    fragment_table_destroy(&p->fragments);
    if (p->fd >= 0) close(p->fd);
    free(p);
}
```

关闭顺序是依赖图的逆序：先阻止线程再访问状态，再清重组表，最后关 fd 和释放对象。若先 close fd 而线程仍在 poll/read，描述符编号可能被进程其他代码复用，旧线程会错误访问一个全新的文件。这比单纯返回 EBADF 更危险。

每个布尔标志必须只在对应资源真正取得后设置。`pthread_t` 的全零字节不一定表示“未启动”，不能靠 calloc 猜测线程状态。destroy 要幂等还是只能调用一次也必须写进契约；常见 C destroy 只允许一次，C++ RAII wrapper 则负责确保唯一调用。

## C 错误码与 C++ 异常的边界

C ABI 不能让 C++ 异常穿过边界。一个 C++ provider 回调若抛出异常并越过 C 栈帧，调用方没有对应的异常约定，行为不可移植。边界包装器必须捕获全部异常并转换：


```cpp
extern "C" int publish_bridge(lcm_provider_t* raw,
                              const char* channel,
                              const void* data,
                              unsigned int size) noexcept {
  try {
    return AsCpp(raw).Publish(channel, data, size);
  } catch (const std::exception&) {
    return -1;
  } catch (...) {
    return -1;
  }
}
```

`extern "C"` 控制链接名，不会把函数体变成 C，也不会自动禁用异常。`noexcept` 说明桥接函数承诺不传播异常；若异常逃出，程序会终止，所以捕获逻辑仍不可省略。

边界还不能返回指向临时 `what()` 的指针。若需要错误文本，应把错误码写入 provider 状态，或由调用者提供缓冲区。异常捕获中的日志也要谨慎：回调可能处于锁内、关闭期或低内存失败路径，复杂日志可能再次抛异常或死锁。

## 用 RAII 包装裸句柄

公开 C API 通常提供 `create/destroy` 对。C++ 层可以把它转换成唯一所有权：


```cpp
struct LcmDeleter {
  void operator()(lcm_t* p) const noexcept {
    if (p) lcm_destroy(p);
  }
};

using UniqueLcm = std::unique_ptr<lcm_t, LcmDeleter>;

class Lcm final {
 public:
  explicit Lcm(std::string_view url)
      : handle_(lcm_create(std::string(url).c_str())) {
    if (!handle_) throw std::runtime_error("lcm_create failed");
  }

  Lcm(const Lcm&) = delete;
  Lcm& operator=(const Lcm&) = delete;
  Lcm(Lcm&&) noexcept = default;
  Lcm& operator=(Lcm&&) noexcept = default;

 private:
  UniqueLcm handle_;
};
```

自定义 deleter 是 `unique_ptr` 类型的一部分，因此不会在每个对象中额外保存一个可变的 `std::function`。删除复制阻止两个包装器同时销毁同一 `lcm_t`；默认移动会把所有权转移并把源指针置空。

构造函数中的临时 `std::string` 在完整表达式结束前仍存在，所以 `c_str()` 对 `lcm_create` 调用期间有效。前提是 C 函数复制 URL；若它保存传入指针，则包装器必须让字符串成员活得与句柄一样久。

### 自定义 deleter、对象大小与 move

无状态 deleter 通常可被空基类优化，`UniqueLcm` 的大小常接近一个指针，但标准不要求具体字节数，不能把它直接放进公开 C ABI 结构。若 deleter 保存 allocator 或动态库句柄，unique_ptr 对象会相应变大。

默认 move assignment 会先销毁目标当前拥有的 handle，再接管源对象。因此移动赋值也可能执行 `lcm_destroy()`，不是一个纯指针交换；若 destroy 会 join 线程，它可能长时间阻塞。实时线程不应执行 wrapper 的移动赋值或析构。

外层类若还保存 Subscription 成员，成员声明顺序必须让 subscriptions 先析构、LCM handle 后析构：


```cpp
class Node {
  UniqueLcm lcm_;                       // 先构造，后析构
  std::vector<Subscription> subscriptions_; // 后构造，先析构
};
```

否则 subscription deleter 会拿已经销毁的 lcm_t 调用 unsubscribe。更清楚的方式是显式 `Close()`：先停止 handle loop，清订阅，再销毁 LCM。

### 真实模板为什么要为每一组消息类型和接收类实例化 trampoline？

C API 只接受一个普通函数指针和一个 `void* userdata`；C++ 用户写的却可能是：

~~~cpp
void Handler::onState(const ReceiveBuffer*,
                      const std::string&,
                      const state_t*);
~~~

成员函数指针需要一个具体 `this`，不能直接塞进普通 C callback 槽位。固定版本 `LCMMHSubscription<MessageType, MessageHandlerClass>` 的关键代码如下：

~~~cpp
MessageHandlerClass *handler;
void (MessageHandlerClass::*handlerMethod)(const ReceiveBuffer *rbuf,
                                           const std::string &channel,
                                           const MessageType *msg);

static void cb_func(const lcm_recv_buf_t *rbuf,
                    const char *channel,
                    void *user_data)
{
    LCMMHSubscription<MessageType, MessageHandlerClass> *subs =
        static_cast<LCMMHSubscription<MessageType, MessageHandlerClass> *>(
            user_data);

    MessageType msg;
    int status = msg.decode(rbuf->data, 0, rbuf->data_size);
    if (status < 0) {
        fprintf(stderr, "error %d decoding %s!!!\n",
                status, MessageType::getTypeName());
        return;
    }

    const ReceiveBuffer rb = {
        rbuf->data, rbuf->data_size, rbuf->recv_utime
    };
    subs->channel_buf = channel;
    (subs->handler->*subs->handlerMethod)(
        &rb, subs->channel_buf, &msg);
}
~~~

模板参数解决的是**编译期类型**问题：每一组 `<MessageType, MessageHandlerClass>` 都能生成一个知道该调用哪种 `decode()`、哪种成员函数签名的静态 trampoline。运行时 `userdata` 恢复出的不是用户 Handler 本体，而是一个 C++ subscription 适配对象；这个对象再保存用户 Handler 的非拥有指针和成员函数指针。

~~~text
LCM wrapper
   |
   | owns
   v
LCMMHSubscription<state_t, Handler>
   |-- owns channel_buf
   |-- borrows Handler*
   |-- stores member-function pointer
   +-- address is stored by C runtime as userdata
~~~

这里有一个很重要的地址稳定性问题。C++ wrapper 的 `subscriptions` 容器保存的是 `Subscription*`，适配对象本体由 `new` 单独分配；即使 vector 扩容，vector 移动的是指针值，不会搬迁适配对象本身。C runtime 里已经保存的 userdata 因而仍然指向原地址。若把适配对象按值塞进会搬迁元素的 vector，扩容就可能让 C runtime 持有悬空地址。

取消订阅时还要区分两层生命周期。C `lcm_unsubscribe()` 在当前 dispatch 正在使用 subscription 时可以设置延迟删除标志；C++ wrapper 随后还要从自己的 vector 擦除并 `delete` 适配对象。固定代码并没有把“另一线程正在执行 callback”自动变成跨线程安全的引用计数协议。工程上更容易证明的关闭顺序是：**先停止唯一 handle 循环并 join，再取消剩余订阅，最后销毁 Handler 与 LCM wrapper。**

同理，`MessageType msg` 只是 trampoline 栈上的临时对象；用户 callback 拿到的 `msg*` 只在该回调期间有效。若后台 worker 还要使用字段，应在 callback 内复制业务需要的数据，不能把这根指针直接保存起来。

最后，解码失败和业务失败属于不同边界。固定 trampoline 在 `decode()<0` 时打印错误并返回，不调用用户 Handler；而用户 Handler 如果抛出 C++ 异常，固定代码没有 catch 将其转换为 C 错误码。应用最好在自身回调边界捕获异常并转成明确的停止/降级状态，而不是依赖异常跨越 C runtime 和资源回收路径传播。
## 回调桥接需要上下文指针

C 回调不能直接保存捕获 lambda。常见桥接形式是函数指针加 `void* user`：


```cpp
class Subscription {
 public:
  using Handler = std::function<void(const lcm_recv_buf_t&, std::string_view)>;

  static void Trampoline(const lcm_recv_buf_t* buf,
                         const char* channel, void* user) noexcept {
    auto* self = static_cast<Subscription*>(user);
    try {
      self->handler_(*buf, channel);
    } catch (...) {
      self->last_callback_failed_.store(true, std::memory_order_relaxed);
    }
  }

 private:
  Handler handler_;
  std::atomic<bool> last_callback_failed_{false};
};
```

`void*` 擦除了类型，`static_cast` 的正确性完全依赖注册时传回同一个对象地址。更隐蔽的风险是寿命：只要 C 层还可能回调，`Subscription` 就不能析构。安全关闭需要先 unsubscribe，再等待正在执行的回调退出，最后销毁 handler。

不能在回调中无条件销毁自己，除非底层明确支持重入注销。通用方案是回调只设置取消标志，事件循环在当前分发结束后执行真正注销。

### 稳定地址比“对象还活着”多一层要求

注册时传给 C 的 user pointer 是 `Subscription*`，因此对象不仅要存活，地址还必须保持不变。把 Subscription 直接放进会扩容的 `std::vector<Subscription>`，后续 push_back 可能移动元素，使 C 层保存的地址悬空。可使用：

- `std::unique_ptr<Subscription>`，对象在堆上保持稳定地址；
- `std::list` 等节点稳定容器，但局部性较差；
- 预留容量并禁止超过上限，但 API 必须严格维护这一不变量；
- 单独分配共享 CallbackState，只把 state 指针交给 C。

最后一种最容易把句柄对象移动与回调状态寿命解耦：


```cpp
struct CallbackState {
  std::mutex mutex;
  std::condition_variable idle;
  std::size_t active{};
  bool stopping{};
  Subscription::Handler handler;
  std::exception_ptr failure;
  std::atomic<bool> bridge_failed{};
};

static void Trampoline(const lcm_recv_buf_t* buf,
                       const char* channel, void* user) noexcept {
  auto* state = static_cast<CallbackState*>(user);
  Subscription::Handler handler;
  try {
    std::lock_guard lock(state->mutex);
    if (state->stopping) return;
    handler = state->handler;  // 复制也可能分配并抛异常
    ++state->active;
  } catch (...) {
    state->bridge_failed.store(true, std::memory_order_relaxed);
    return;
  }

  try {
    handler(*buf, channel);
  } catch (...) {
    std::lock_guard lock(state->mutex);
    state->failure = std::current_exception();
  }

  {
    std::lock_guard lock(state->mutex);
    if (--state->active == 0) state->idle.notify_all();
  }
}
```

回调先在锁内登记 active 并复制 handler，随后在锁外执行用户代码。关闭方先从 LCM 订阅表移除，使新的 dispatch 无法取得 state，再设置 stopping 并等待 active 为零，最后释放 state。具体顺序取决于 `lcm_unsubscribe` 对并发 handle 的保证；如果底层只允许与 handle 同线程操作，应把 unsubscribe 命令投递回事件循环，而不是跨线程直接调用。

异常保存在 `exception_ptr` 中，由事件循环外层在安全的 C++ 上下文读取和处理，不能从 trampoline 重新抛过 C 边界。同一 state 多次失败时，还要决定保留首次错误还是最新错误。

## `get_fileno()` 是事件循环的组合接口

provider 返回文件描述符后，LCM 可以接入 `select/poll/epoll`：

```text
network receiver -> internal queue -> notification fd readable
application poll -> lcm_handle -> pop one message -> callback
```

这把“网络接收线程”和“用户回调线程”分开。文件描述符的可读性只是通知，不等于业务数据本身就在该 fd 中；一次通知与队列条目之间的计数必须避免丢唤醒。典型做法是在空队列变为非空时写通知，并在取空后清除，而不是对每个分片都写一次。

### edge-trigger 与 level-trigger 的不变量

若通知语义类似 level-trigger，只要内部队列非空，fd 就必须保持可读；handle 取走最后一项时才清通知。若使用 edge-trigger，消费者必须持续 drain 到 EAGAIN，否则队列仍有数据却不会出现新边沿。把两种语义混用会产生“消息已经入队，事件循环永远不再醒来”的故障。

线程间状态可抽象为：

```text
producer lock:
  was_empty = queue.empty
  queue.push(message)
  if was_empty: signal(fd)

consumer lock:
  message = queue.pop_front
  if queue.empty: clear(fd)
```

检查 empty、入队与决定 signal 必须在同一同步域内。否则消费者可能恰好清除通知，生产者又因为观察到旧的“非空”而不 signal，形成丢唤醒。eventfd 的计数或 pipe 字节也要处理饱和、非阻塞写失败与关闭唤醒。

### 与外部事件循环组合


```cpp
pollfd descriptors[] = {
  {.fd = lcm_get_fileno(handle.get()), .events = POLLIN},
  {.fd = shutdown_fd, .events = POLLIN},
};

while (!stopping) {
  const int rc = poll(descriptors, 2, timeout_ms);
  if (rc < 0 && errno == EINTR) continue;
  if (rc < 0) break;
  if (descriptors[1].revents & POLLIN) break;
  if (descriptors[0].revents & POLLIN) {
    for (std::size_t n = 0; n < max_batch; ++n) {
      if (lcm_handle_timeout(handle.get(), 0) <= 0) break;
    }
  }
}
```

`max_batch` 防止消息洪泛让定时器、关闭信号和其他 fd 永久饥饿。timeout 不是越小越好：零超时会形成忙轮询，过大则增大关闭和低频消息延迟。外部 loop 还应分别记录接收队列年龄和 callback 耗时，而不只记录 poll 唤醒次数。

## 内存布局与性能

一次 vtable 调用通常是一个间接函数调用，成本与 C++ 虚调用同量级，远小于系统调用和网络 I/O。真正的性能热点是 payload 复制、分片、哈希查找和回调耗时。

公开 ABI 中避免直接暴露可变结构体字段，可以让私有状态重新布局而不破坏调用方。代价是访问必须经过函数，且编译器跨动态库边界较难内联。对中间件控制面而言，这是可接受的封装成本。

## 生成类型如何接到无类型 payload

C 核心只认识 channel、byte pointer 和 size，类型安全由生成代码在边界外恢复。概念上的 C++ 发布包装如下：


```cpp
template<class Message>
int PublishTyped(lcm_t* lcm, std::string_view channel,
                 const Message& message) {
  const auto encoded_size = message.getEncodedSize();
  if (encoded_size < 0 || encoded_size > kMaxMessageBytes) return -1;

  std::vector<std::byte> buffer(static_cast<std::size_t>(encoded_size));
  const int written = message.encode(buffer.data(), 0, buffer.size());
  if (written != encoded_size) return -1;

  const std::string owned_channel(channel);
  return lcm_publish(lcm, owned_channel.c_str(),
                     buffer.data(), buffer.size());
}
```

模板要求 Message 提供约定接口，编译器在实例化时检查；C 核心仍保持稳定。这里每次分配 vector，适合解释但不是高频最优实现。可以让 Publisher 持有可复用缓冲，先检查所需容量，再编码；多线程共用 Publisher 时则需要每线程 buffer、池或锁。

`string_view` 不是以零结尾的字符串，所以不能直接把 `channel.data()` 交给 C API；先构造 owned string 才保证末尾 NUL。若上层 API 直接接收 `const std::string&`，可以省掉这次临时分配。

接收方向先验证类型 hash 和长度，再 decode 到候选对象，成功后才提交给业务：


```cpp
template<class Message>
bool Decode(const lcm_recv_buf_t& input, Message& output) {
  if (input.data_size > kMaxMessageBytes) return false;
  Message candidate;
  const int used = candidate.decode(input.data, 0, input.data_size);
  if (used < 0 || static_cast<std::size_t>(used) != input.data_size) {
    return false;
  }
  output = std::move(candidate);
  return true;
}
```

候选对象保证失败时旧 output 保持有效。生成类型若包含动态数组，decode 前仍要验证每个长度字段；“总报文不超过上限”不能阻止畸形长度导致整数溢出或过量分配。类型 hash 能检测 schema 不同，不替代协议版本迁移策略。

## 从零复刻一个两 provider 核心

最小实现需要把 URL 解析、provider 选择、公共句柄和事件分发连接起来：


```c
typedef struct provider_factory {
    const char *scheme;
    lcm_provider_t *(*create)(const char *target);
} provider_factory_t;

static const provider_factory_t factories[] = {
    {"udpm", udpm_create},
    {"file", file_create},
};

struct lcm {
    lcm_provider_t *provider;
    subscription_table_t subscriptions;
    channel_cache_t cache;
    bool handling;
};

lcm_t *lcm_create(const char *url) {
    parsed_url_t parsed = {0};
    lcm_t *ctx = NULL;

    if (parse_url(url, &parsed) < 0) goto fail;
    ctx = calloc(1, sizeof(*ctx));
    if (!ctx) goto fail;
    if (subscription_table_init(&ctx->subscriptions) < 0) goto fail;
    if (channel_cache_init(&ctx->cache) < 0) goto fail;

    const provider_factory_t *factory = find_factory(parsed.scheme);
    if (!factory) goto fail;
    ctx->provider = factory->create(parsed.target);
    if (!ctx->provider) goto fail;

    parsed_url_destroy(&parsed);
    return ctx;

fail:
    parsed_url_destroy(&parsed);
    lcm_destroy(ctx);
    return NULL;
}
```

### 工厂表只决定构造，不承担实例状态

scheme 查找规模很小时线性扫描足够，成本只发生在 create；为了几项 provider 引入哈希表反而增加初始化与错误面。factory 返回的每个实例拥有独立状态和同一份静态 vtable。公共 lcm_t 拥有 provider，并在 destroy 时唯一释放。

`parse_url` 的输出也要有 destroy，因为解析过程可能分配 scheme、target 和参数。把它初始化为全零并让 destroy 接受半解析状态，能复用同一个 fail 路径。

### 订阅表与匹配缓存

若每条消息都对所有正则订阅执行匹配，单次分发成本为 `O(R × match_cost)`，R 是订阅数。可把具体 channel 映射到匹配订阅列表：首次出现时扫描 R 项并缓存，之后命中近似 `O(1 + K)`，K 为匹配回调数。

新增或删除订阅会使哪些 cache entry 失效？最简单方式清空整个 channel cache，成本 `O(C)`，C 为已见 channel 数；控制面变更少时通常可接受。更精细的增量失效需要反向索引，并增加注销正确性难度。LCM 的设计价值正在于把复杂度放在低频订阅变化，而让高频已知 channel 快速分发。

缓存中不能保存会被立即释放的 subscription 裸指针。删除订阅时要先让相关 cache 失效，且不能与正在遍历 cache 的 handle 并发释放。单线程 handle 模型可用 deferred delete：当前 dispatch 标记 removed，循环结束后统一回收。

### handle 的可重入状态


```c
int lcm_handle(lcm_t *ctx) {
    if (!ctx || ctx->handling) return -1;
    ctx->handling = true;

    /* provider 取得一条完整消息，并回调核心 dispatch */
    int rc = ctx->provider->ops->handle(ctx->provider);
    reclaim_removed_subscriptions(ctx);
    ctx->handling = false;
    return rc;
}
```

这个缩小模型显式拒绝递归 handle。真实实现若允许多个线程同时 handle，需要保护 provider receive、订阅表、cache 和 callback 删除；其语义会复杂许多。更小而可证明的约束是：一个 lcm_t 只有一个事件循环线程，跨线程只允许 publish，所有 subscribe/unsubscribe 都通过命令队列回到 loop。

`handling` 必须在所有错误出口复位；C 可用统一 `goto out`，C++ 可用 scope guard。callback 可以发布新消息，但是否允许订阅或取消自身要由 dispatch 的 deferred mutation 机制决定。

## 数据结构与性能预算

设订阅数为 `R`、不同 channel 数为 `C`、某 channel 匹配数为 `K`、重组中的消息数为 `M`、平均分片数为 `F`、payload 为 `S`：

| 数据结构或路径 | 典型成本 | 空间主项 | 性能边界 |
|---|---:|---:|---|
| provider scheme 线性表 | 创建时 `O(provider_count)` | 常量 | provider 很少，通常不是热点 |
| 首次 channel 匹配 | `O(R × regex_cost)` | 新 cache entry `O(K)` | 恶意/复杂正则可放大成本 |
| 缓存命中分发 | `O(K)` | callback 快照或节点 | 慢 callback 串行阻塞后续消息 |
| UDPM 分片发送 | `O(S)` 加 `O(F)` syscall | 发送 frame | MTU、内核缓冲与丢片 |
| 重组表查找 | 平均 `O(1)` | `O(M×S)` 上限必须限制 | 缺片导致状态滞留 |
| typed encode/decode | `O(S)` | 临时或复用 buffer `O(S)` | 动态数组分配、边界验证 |
| handle batch | `O(B×(dispatch+callback))` | 小量临时状态 | B 无界会饿死其他事件源 |

LCM 不提供端到端可靠传输、全局顺序或内建背压。UDP 报文丢失、分片缺失和慢 callback 都应通过应用指标显现。机器人状态流可以选择丢旧保新；命令流需要序号、超时与确认；控制安全信号不应只依赖普通组播报文。

内存上限至少要覆盖：重组表的消息数与总字节、每 channel 匹配 cache、provider 接收队列、生成类型最大动态数组、event log 缓冲和 callback 自行复制的 payload。若任何一项无界，低平均流量也不能证明故障流量下可行。

## 可迁移的设计能力

LCM 展示了一种非常克制的分层：稳定 C ABI 负责对象与 provider 多态；生成代码负责类型；C++ 只负责 RAII 和成员回调；文件描述符负责事件循环组合。每层只引入自己必需的机制，因此易于绑定其他语言，也容易把网络 provider 换成日志回放 provider。

可以迁移的模式包括：首参数 self 的手工多态、静态只读方法表、半构造安全 destroy、scheme factory、函数指针加 context 的回调、锁外用户代码、channel 匹配缓存和 deferred mutation。不能照搬的是具体 UDP 可靠性假设；网络、时延与安全要求不同，provider 协议就要重新设计。

## 最小复刻路线

1. 实现只有 publish/handle/destroy 的 provider vtable 和内存 loopback provider；
2. 增加公共 lcm_t、URL scheme 工厂和完整失败回滚；
3. 实现单线程订阅表、精确 channel 分发与回调 context；
4. 增加正则订阅、channel cache 和回调内延迟注销；
5. 增加 pipe/eventfd 通知并接入 poll，同时限制 handle batch；
6. 实现 UDPM 短帧，再增加有总量与超时上限的分片重组；
7. 加入生成类型 hash/encode/decode 和 C++ RAII 包装；
8. 最后实现 event log provider，用相同回调链验证实时输入与回放输入可替换。

每一步都应主动破坏：构造中途失败、回调抛异常、回调注销自己、通知 fd 写满、分片永不齐全、关闭与 handle 竞态。只有这些路径也能收敛，核心才不只是一个正常流量 demo。

## 最小复刻的完成标准

最小实现应至少提供两个 provider；通过 URL scheme 选择方法表；构造中途失败不会泄漏资源；C++ 句柄可移动不可复制；异常不会穿越 C 回调；subscription 关闭后不再收到回调；`get_fileno()` 能与应用事件循环组合。完成这些，才真正复刻了 LCM 的可替换传输边界与语言绑定基础。

还应能够从代码中指出四个线性化点：provider create 成功提交；subscription 对新 dispatch 可见；unsubscribe 对后续 dispatch 不可见；destroy 返回后不再有 receiver 或 callback 访问对象。若这些时刻只能靠“通常先发生”解释，就说明生命周期协议仍未完成。
