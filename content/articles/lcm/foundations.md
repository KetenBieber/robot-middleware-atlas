# LCM 的 C 语言阅读桥梁：让指针、回调与消息链重新连起来

假设姿态估计器以 200 Hz 发布 `POSE`，控制器在自己的主循环中调用 `lcm_handle()`，记录程序同时把数据写入日志。应用代码看起来只有发布、订阅和回调，向下进入 LCM 后，却会很快遇到结构体指针、`void*`、函数指针、mutex、socket、pipe 和 ring buffer。

这些词不是互不相干的 C 语言考点。它们共同完成一件事：把一段网络字节安全地送到应用选定的线程，并在回调结束后正确回收内存。若只背语法，读到 `provider->vtable->publish(provider, ...)` 时仍然不知道消息去了哪里；若只看调用链，又容易误判指针寿命、回调线程和 buffer 所有权。

这一章因此是一座语言桥梁。它不重复前面各章已经展开的 provider 状态机，而是把读懂那些源码所需的 C 和操作系统机制重新放回同一条消息链。阅读时始终保留三个问题：当前变量保存的是对象还是地址，谁负责释放它，以及下一步是直接函数调用还是跨线程唤醒。

本文与其余 LCM 章节一样固定阅读提交 lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864。下面的短代码先解释语言形式；涉及真实运行行为时，再链接到这个版本中的具体函数。

## 先建立一张整体地图

发布者发送一条消息时，实际经过：

```text
业务对象
  -> 编码为 bytes
  -> lcm_publish()
  -> UDPM provider
  -> UDP socket
  -> 网络
```

订阅者接收时，实际经过：

```text
UDP socket
  -> 接收线程
  -> 完整消息队列
  -> notify pipe
  -> 应用调用 lcm_handle()
  -> 找到匹配 subscription
  -> 执行用户 callback
  -> 解码为业务对象
```

后续遇到任何函数，先判断它属于五个位置中的哪一个：公共 API、传输、接收缓存、订阅分发或类型编码。类名和指针就不会成为孤立信息。

## 对象与地址构成运行时的地基

先从最小单位开始。LCM 的顶层句柄、provider 和接收 buffer 在 C 中都不是“会自动管理自己的对象”，而是结构体与指针共同表达的状态。理解这一层，后面的接口替换和生命周期才不会变成猜测。

### C 结构体表示一组相关状态

C 没有类，但可以用 `struct` 把数据放在一起：


```c
struct Message {
    const char *channel;
    const void *data;
    unsigned int size;
};
```

它与简单 C++ 类的数据部分相似：


```cpp
class Message {
 public:
  const char* channel;
  const void* data;
  unsigned int size;
};
```

LCM 的 `lcm_t` 是公共运行时状态，UDPM 的 `lcm_udpm_t` 是 UDP provider 状态。把它们分成两个结构体，意味着“订阅表”和“UDP socket”属于不同模块。

### 指针表示对象的位置


```c
lcm_t *bus;
```

可以先把它读成：“`bus` 保存一只 `lcm_t` 对象的地址。”箭头运算符访问该地址所指对象的成员：


```c
bus->provider
```

等价于：


```c
(*bus).provider
```

中间件大量使用指针有三个原因：

- 对象通常比一个机器字大，传地址比复制整个对象便宜；
- 多个模块需要访问同一份运行时状态；
- 具体结构可以隐藏在 `.c` 文件中，公共头文件只暴露不透明指针。

指针本身不说明所有权。看到指针时还要继续判断：谁创建对象、谁释放对象、指针可以保存多久。

### const 描述通过当前指针不能修改数据


```c
const void *data
```

表示函数通过 `data` 只能读取这块内存。它不表示其他线程一定不会修改，也不表示内存会永久有效。

LCM callback 收到的 payload 就是只读借用：callback 可以读取和解码，但不能把这只指针当成长期存储。UDPM 的 `lcm_udpm_handle()` 会先把同一份 `lcm_recv_buf_t` 交给全部匹配回调，所有回调结束后才回收底层 `lcm_buf_t`。若要把数据交给后台线程，应用必须在回调期间复制，或者解码到自己拥有的对象中。

### void 指针抹去具体类型

`void*` 是“某个地址，但此处不声明它指向哪种类型”：


```c
void *userdata;
```

订阅时，调用者可以把自己的状态地址交给 LCM：


```c
struct Controller controller;
lcm_subscribe(bus, "POSE", on_pose, &controller);
```

回调再转回原类型：


```c
void on_pose(const lcm_recv_buf_t *buf,
             const char *channel,
             void *userdata)
{
    struct Controller *controller = userdata;
    // use controller
}
```

LCM 只负责保存和传回地址，不会复制或释放 `controller`。因此 controller 必须比 subscription 活得更久。

这是一种运行时类型擦除：不同应用状态都能放进同一 callback 接口，代价是 cast 正确性由程序员保证。

## 动手搭第一座桥：C callback 怎样再次找到 C++ 对象？

你已经注册了一个成员函数：

~~~cpp
bus.subscribe("POSE", &Handler::onPose, &handler);
~~~

读到 C 核心时却发现，它只保存普通 C 函数指针和一个 `void* userdata`。这是语言边界逼出来的结构：普通 C 函数没有隐含 `this`，C++ 成员函数指针却必须绑定具体对象，二者不能直接互换。

### 一个能运行的 callback trampoline 实验

下面是一份**完整可独立编译的 C++17 教学程序**。它不依赖 LCM，只复刻“C callback + userdata + trampoline”这一个机制：

~~~cpp
#include <cassert>
#include <cstddef>
#include <string>

using CCallback = void(*)(const char*, const void*, std::size_t, void*);

struct CSubscription {
    CCallback callback = nullptr;
    void* userdata = nullptr;
};

void dispatch(CSubscription subscription, const char* channel,
              const void* bytes, std::size_t size) {
    subscription.callback(channel, bytes, size, subscription.userdata);
}

class Receiver {
public:
    void onMessage(const char* channel, const void* bytes, std::size_t n) {
        latest_.assign(static_cast<const char*>(bytes), n);
        last_channel_ = channel;
    }

    static void trampoline(const char* channel, const void* bytes,
                           std::size_t n, void* context) {
        auto* self = static_cast<Receiver*>(context);
        self->onMessage(channel, bytes, n);
    }

    CSubscription subscribe() { return {&trampoline, this}; }
    const std::string& latest() const { return latest_; }
    const std::string& channel() const { return last_channel_; }

private:
    std::string latest_;
    std::string last_channel_;
};

int main() {
    Receiver receiver;
    CSubscription subscription = receiver.subscribe();
    const char bytes[] = {'4', '2'};
    dispatch(subscription, "JOINT", bytes, sizeof(bytes));
    assert(receiver.channel() == "JOINT");
    assert(receiver.latest() == "42");
}
~~~

运行链是：

~~~text
dispatch()
   |
   v
Receiver::trampoline()      // 普通静态函数，满足 C callback 形状
   |
   | static_cast<Receiver*>(userdata)
   v
原来的 Receiver 对象
   |
   v
Receiver::onMessage()
~~~

这里真正需要掌握的不是“static_cast 怎么写”，而是三条寿命约束。

第一，`userdata` 只是借用地址，**不会延长 Receiver 的寿命**。若 Receiver 已经析构，trampoline 仍把旧地址当成有效对象，后果是未定义行为。第二，`bytes` 只在当前同步调用中借用；真实 LCM 中的 `rbuf->data` 也不能被 callback 原样保存到后台线程长期使用。第三，`static_cast` 不做运行时类型检查，注册时把错误对象地址放进 userdata，就破坏了整个约定。

回到固定版本 LCM，`LCMMHSubscription<MessageType, MessageHandlerClass>` 做的事情比这个教学程序多两步：它把 `userdata` 恢复成 subscription 适配对象；然后用 `MessageType::decode()` 从 wire bytes 构造临时消息，最后通过保存的成员函数指针调用用户 Handler。也就是说，真正的桥梁同时跨过了**C callback ABI**和**无类型 bytes → C++ typed message**两层边界。

后面的[C ABI 与 C++ 设计实验](c-abi-cpp-design-lab.md)会进一步解释为什么适配对象必须有稳定地址、为什么 C++ wrapper 拥有适配对象但不拥有用户 Handler，以及 unsubscribe 为什么必须与 callback 生命周期协调。

## 接口与回调把行为延迟到合适的时刻

对象只回答“状态放在哪里”，还没有回答“不同 provider 怎样提供同一组操作”以及“用户函数何时执行”。LCM 使用函数指针表达可替换行为，再用 callback 把应用逻辑交还给调用 `lcm_handle()` 的线程。

### 函数指针把行为保存为数据

普通函数：


```c
int send_message(const char *channel,
                 const void *data,
                 unsigned int size);
```

对应的函数指针类型可以写成：


```c
int (*send_fn)(const char *, const void *, unsigned int);
```

把函数地址赋给它后，可以间接调用：


```c
send_fn = send_message;
int result = send_fn("POSE", bytes, size);
```

LCM provider vtable 正是一组函数指针。UDPM、TCPQ 和 MEMQ 分别把自己的函数地址放进同样布局的表，公共层只通过表调用。

### vtable 是 C 语言中的接口对象

先看一个极小版本：


```c
struct TransportOps {
    int (*publish)(void *self,
                   const char *channel,
                   const void *data,
                   unsigned int size);
    void (*destroy)(void *self);
};

struct Transport {
    void *self;
    struct TransportOps *ops;
};
```

调用时：


```c
transport.ops->publish(
    transport.self, "POSE", bytes, size);
```

如果 `ops` 指向 UDP 方法表，实际调用 UDP；若指向内存队列方法表，实际写内存队列。

它对应 C++ 虚函数：


```cpp
class Transport {
 public:
  virtual int Publish(...) = 0;
  virtual ~Transport() = default;
};
```

“函数指针表 + self 指针”与“虚基类 + this 指针”解决的是同一个设计问题：调用者依赖稳定接口，不依赖具体实现。

### opaque struct 隐藏模块内部字段

公共头文件可以只写：


```c
typedef struct _lcm_t lcm_t;
```

这告诉编译器 `lcm_t` 是一种结构，但没有展示字段。用户只能持有 `lcm_t*`，不能写 `bus->handlers_map`。

真实定义留在 `lcm.c`：


```c
struct _lcm_t {
    GRecMutex mutex;
    GPtrArray *handlers_all;
    // ...
};
```

这就是不透明对象。它带来类似 C++ private 成员的封装，也让库可以修改内部布局而不要求调用者重新理解所有字段。

### callback 是稍后被框架调用的函数

应用把函数交给 LCM：


```c
lcm_subscribe(bus, "POSE", on_pose, userdata);
```

此时不会立刻执行 `on_pose`。当应用以后调用 `lcm_handle()`，且队列中有匹配消息时，框架才执行：


```c
subscription->handler(buffer,
                      channel,
                      subscription->userdata);
```

因此 callback 有两个时间点：注册时间和调用时间。两者之间，函数代码、userdata 和 subscription 都必须仍然有效。

## 操作系统把网络到达转换为应用可处理的事件

函数指针解决了“调用哪种传输”，却没有解决“网络线程收到数据后，怎样通知应用线程”。socket、pipe、mutex 和有限 buffer 共同组成了 LCM 接收侧的生产者—消费者边界。

### socket 是内核中的通信端点

UDP socket 可以看作操作系统提供的网络收发句柄：


```c
int fd = socket(AF_INET, SOCK_DGRAM, 0);
sendmsg(fd, &message, 0);
recvmsg(fd, &message, 0);
```

`sendmsg` 把用户态 bytes 交给内核；返回成功只表示本机内核接收了这次发送请求，不表示远端收到。

`recvmsg` 通常会阻塞，直到网络包到达。它通过系统调用进入内核，数据报先排在该 socket 的内核接收缓冲区，再被复制到用户提供的 ring 区域；`recvmsg()` 返回并不代表 callback 已运行。因此 LCM 把读取放在专用接收线程中，避免应用主线程一直等待 socket。缓冲区满时，UDP 不会替发送者保存无限历史，后续数据报可能直接丢失。

### pipe 是进程内的唤醒通道

POSIX pipe 有两个文件描述符，是一个由内核维护的单向字节流：

```text
pipe[1] --write--> kernel buffer --read--> pipe[0]
```

接收线程把完整消息放入用户态队列后，向 pipe 写一个字节。应用的 `select()` 或 `poll()` 发现 `pipe[0]` 可读，就知道可以调用 `lcm_handle()`。这里有五个不同事件：消息数据先已经在用户态队列；通知字节后来进入内核 pipe 缓冲；等待中的应用线程因此从 blocked 变成 runnable；OS 何时真正调度它取决于其他可运行线程与优先级；它拿到 CPU 后才从 `lcm_handle()` 取消息并开始 callback。通知可读不能简化成“业务 callback 已唤醒并执行”。

为什么不直接等待 UDP socket：因为一个 UDP 包可能只是大消息的一个分片。只有全部分片重组完成、消息真正进入应用队列后，notify pipe 才变为可读。

pipe 只传递状态提示，payload 仍在内存队列中。固定版本在 POSIX 上把 `lcm_internal_pipe_create/read/write/close` 映射到系统 `pipe/read/write/close`，并把通知 pipe 的写端设为非阻塞；Windows 用 `WinPorting.cpp` 创建 loopback TCP socket pair 来模拟这条可被 `select()` 监听的通道。`lcm_internal.h` 没有引入跨进程共享内存、Windows event 或 semaphore。Windows 的本机 socket pair 仍经过内核 socket 缓冲，不等于进程间通信协议。

### mutex 保护多步不变量

两个线程同时操作同一队列时，需要 mutex：


```c
lock(queue_mutex);
enqueue(queue, message);
unlock(queue_mutex);
```

锁保护的不是某一行语句，而是“检查状态并修改状态”的完整事务：


```c
lock(mutex);
if (queue_is_empty(queue))
    notify();
enqueue(queue, message);
unlock(mutex);
```

若检查 empty、写标记和 enqueue 不在同一锁域，消费者可能读掉旧标记并发现空队列，生产者却根据过时的“非空”判断不写新标记，造成队列有消息而事件循环继续阻塞。固定实现用 provider 的 `GRecMutex` 包住这些状态变化，也包住 `handle()` 的 dequeue 与必要的补写；核心 `lcm_t` 的另一把递归锁保护 subscription/cache/计数。两把锁属于不同对象，不能用“LCM 有一把锁”概括。入队通知、消费和重新通知。

读源码时，应圈出 lock 与 unlock 之间的全部语句，再总结它维护的整体不变量。

### ring buffer 优先复用存储，但不是固定内存上限

如果每个 UDP 报文都单独 `malloc/free`，高频消息会反复进入分配器。LCM 先用 ring buffer 为未分片报文提供可循环复用的连续空间：

可以把它想象成一条首尾相接的传送带：

```text
[used A][used B][free........][used wrapped]
          head ->             <- tail
```

这里必须避免一个常见误解：这个 ring 是分配优化，不是永不增长的硬容量。固定版本的 `lcm_buf_allocate_data()` 在当前 ring 放不下新报文时，会创建容量约为原来 1.5 倍的新 ring；旧 ring 因为仍可能被排队消息引用，要等那些 buffer 释放后才能销毁。因此，慢消费者仍可能带来更高的内存占用。

`inbufs_filled` 本身是链表，不是固定容量 ring；真正限制“某个订阅还愿意保留多少条消息”的是 subscription 的 `max_num_queued_messages`（默认 30，设为非正数可不限制）。分片重组另有按项数和总字节数触发的 LRU 淘汰，但源码不是严格瞬时硬字节上限。把这些机制统称为一只固定队列，会同时误判分配行为和丢弃位置。

ring 中的一段空间要等 `lcm_udpm_handle()` 完成分发后才能归还。应用若把 callback 中的裸 payload 指针保存到回调之外，后续复用会使它指向已经失效或被覆盖的数据。

## 字节布局与所有权决定数据能否安全跨过边界

消息进入 socket 以前必须拥有稳定的线格式；消息离开接收队列以后，又必须有明确的借用期限。网络字节序、`iovec` 和所有权描述分别约束“字节怎样排列”“怎样少做一次拼接”和“这块内存还能使用多久”。

### 网络字节序统一整数表示

不同 CPU 可能用不同字节顺序保存多字节整数。协议发送前调用：


```c
header.magic = htonl(magic);
```

接收后调用：


```c
magic = ntohl(header.magic);
```

`htonl` 表示 host-to-network long，`ntohl` 表示 network-to-host long。它们让相同 header 在不同处理器上得到一致字节布局。

字符串和单字节数组不需要转换；16、32、64 位整数和浮点编码必须遵守协议规则。

### iovec 表示多段连续输出

发送报文需要依次放置 header、channel 和 payload。最直接做法是分配大 buffer，再复制三段：

```text
[header][channel][payload]
```

`iovec` 允许描述原本分散的内存：


```c
struct iovec parts[3] = {
    {&header, sizeof(header)},
    {channel, channel_size},
    {payload, payload_size}
};
```

一次 `sendmsg()` 接收这些视图，避免应用先手工拼接。它减少一次用户态复制，但数据仍会进入内核网络栈。

### 所有权必须用完整句子描述

看到某个指针，不要只写“这里用了指针”。应写成：

```text
UDPM provider 的接收队列拥有 lcm_buf_t；
一轮分发中的 callback 依次借用其中的 payload；
全部匹配 callback 返回后，provider 才回收 buffer；
需要跨调用保存时由应用复制。
```

常见关系只有几种：

- owning：负责最终释放；
- borrowing：临时使用，不释放；
- shared owning：多个使用者共同延长寿命；
- weak observing：能检查对象是否仍存在，但不延长寿命；
- transferred：所有权从一个对象移动到另一个对象。

LCM 是 C 代码，这些关系不会由类型系统全部表达，必须结合 create/free 和调用时序判断。

## 用线程图重新播放一次接收过程

阅读接收链时先画两个线程：

```text
Receive Thread                     Kernel / Application Thread
--------------                     ---------------------------
recvmsg -> user ring buffer        socket/pipe bytes buffered
parse / reassemble / enqueue       poll/select wait ends
write notify pipe  ------------->  app thread becomes runnable
                                     OS later schedules it on CPU
                                     lcm_handle reads marker + dequeue
                                     invoke callback on app thread
```

然后逐条标注：哪把锁、哪个 buffer、何时复制、谁唤醒谁。函数调用名改变时，这张并发结构仍然成立。

## 用四个问题继续阅读源码

阅读 Provider 章节时，重点识别函数指针表与 opaque self。阅读 UDPM 发送时，重点识别 iovec、网络字节序和 transmit mutex。阅读接收重组时，重点识别两个线程、两组 pipe 和 buffer 所有权。阅读订阅分发时，重点识别 callback 注册时间、执行时间和延迟删除。

每个复杂实现都可以拆回四个基本问题：

1. 数据现在存在哪里；
2. 当前代码在哪个线程执行；
3. 哪个对象拥有这块数据；
4. 下一步由函数调用、队列还是唤醒事件触发。

只要这四个问题能连续回答，就已经理解了中间件的真实运行机制，而不只是记住 API 名称。
