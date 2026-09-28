# 订阅与分发：正则缓存、队列配额和回调期删除

上一章我们已经会写 `lcm.subscribe("ARM_STATE", ...)`，也知道真正运行 callback 的是自己调用 `handle()` 的线程。作为使用者，接下来很可能遇到一个比“怎样订阅”更具体的问题：**同一条消息交给好几个 callback，其中一个取消了另一个订阅，会发生什么？**

假设关节状态同时送给诊断、记录、可视化三个模块。诊断 callback 发现设备异常，于是取消可视化订阅；而 LCM 此刻正在分发同一条 `ARM_STATE`。如果内部只是一个存放 callback 指针的 `vector`，诊断函数一旦删除数组里的可视化节点，分发循环的下一个下标可能移动，原先保存的指针也可能悬空。若在整个循环里始终持锁，用户 callback 又可能反过来调用 `unsubscribe()`，形成另一种锁和重入难题。

把问题稍微扩大：如果可视化偶尔绘图 15 ms，而机械臂状态每 1 ms 到一条，我们是否应该为每个订阅保留一份 payload？如果不复制，又怎样只丢弃可视化的新消息，同时保留控制器的消息？

本章就从这两个真实使用疑问逐层反推 LCM 的订阅系统：先构造一个会失效的 callback 数组，再引入“冻结本轮遍历 + 延迟回收”；接着把一条消息交给多个订阅，发现需要**公共 payload 队列 + 各订阅自己的准入计数**；最后才看 channel 正则匹配缓存和核心的三种锁边界。阅读完之后，`callback_scheduled`、`num_queued_messages` 不应再只是需要记忆的字段，而是上一版设计失败后不得不出现的状态。

源码固定到 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`，下面出现的两个关键函数是 `lcm_try_enqueue_message()` 和 `lcm_dispatch_handlers()`。先把它们当成运行时想回答的两个问题：**“这条消息还值得留下吗？”**和**“留下之后，当前有哪些 callback 真正会被调用？”**

在固定实现中，核心 `lcm_t::mutex` 保护订阅集合、匹配缓存、配额和删除标志；`handle_mutex` 串行化同一实例的 `handle()` 调用；UDPM provider 的另一把 mutex 保护接收缓冲区。三把锁各有不同的职责。理解它们的区别，需要先看它们各自保护了什么状态，而不是先背锁名。

## subscription 的状态

先看固定提交里的 `lcm_subscription_t`，把匹配、回调、配额和删除状态放在同一个对象里：

```c
struct _lcm_subscription_t {
    char *channel;
    lcm_msg_handler_t handler;
    void *userdata;
    lcm_t *lcm;
    GRegex *regex;

    int callback_scheduled;
    int marked_for_deletion;

    int max_num_queued_messages;
    int num_queued_messages;
};
```

这段定义揭示两个不同的所有权约定：`channel` 和 `regex` 是 subscription 自己创建并由 `lcm_handler_free()` 释放的；`handler` 是函数地址；`userdata` 只借给 LCM 使用，不复制、不析构。末尾两个计数保护消息准入，两个标记则保护 callback 遍历期间的节点生命期。

userdata 的寿命由调用者负责。LCM 不知道它指向栈、堆还是静态对象；`unsubscribe()` 返回不等于已经没有 callback 正在使用这个指针。若该订阅正在 dispatch，固定实现只设置延迟删除标记并返回，物理回收要等这一轮 dispatch 结束；跨线程释放 userdata 前必须停止并 join 唯一 handle 线程，或另行建立能够等待在途 callback 结束的屏障。下面逐行看订阅节点装配与取消的真实控制流。

## 两级订阅容器

`lcm_t` 同时维护：

```text
handlers_all:  [subscription*, ...]
handlers_map:  actual channel -> [matching subscription*, ...]
```

`handlers_all` 是权威集合。`handlers_map` 是按已经出现过的具体 channel 建立的匹配缓存。

例如订阅：

```text
"POSE_.*"
"POSE_FRONT"
"LIDAR_.*"
```

第一次收到 `POSE_FRONT` 时，核心遍历全部 subscription 做 regex match，并缓存两个结果。后续同名消息直接查询 hash table。

这避免每条消息都执行所有正则，但缓存大小随实际出现过的 channel 名数量增长。若外部可以发送无限随机 channel，必须考虑 channel cache 的内存上界。

## subscribe 的装配顺序

`lcm_subscribe()` 先通知 provider，再分配核心 subscription：

```text
provider->subscribe(channel)
  -> allocate subscription
  -> duplicate channel
  -> compile ^pattern$
  -> append handlers_all
  -> update all existing handlers_map entries
```

正则通过 `^` 与 `$` 锚定，确保匹配整个 channel。调用者传入的 channel 字符串本身可以包含正则语法。

provider subscribe 成功、但后续正则编译失败时，需要对应的 provider rollback。固定提交摘录如下；输入是调用者传入的 channel、handler 与 userdata，函数先进入 provider，再创建核心节点，最后把节点登记到两级容器。


```c
lcm_subscription_t *lcm_subscribe(lcm_t *lcm, const char *channel, lcm_msg_handler_t handler,
                                  void *userdata)
{
    dbg(DBG_LCM, "registering %s handler %p\n", channel, handler);

    if (lcm->provider && lcm->vtable->subscribe) {
        if (0 != lcm->vtable->subscribe(lcm->provider, channel)) {
            return NULL;
        }
    }

    // create and populate a new message handler struct
    lcm_subscription_t *subscription = (lcm_subscription_t *) calloc(1, sizeof(lcm_subscription_t));
    subscription->channel = strdup(channel);
    subscription->handler = handler;
    subscription->userdata = userdata;
    subscription->callback_scheduled = 0;
    subscription->marked_for_deletion = 0;
    subscription->max_num_queued_messages = lcm->default_max_num_queued_messages;
    subscription->num_queued_messages = 0;
    subscription->lcm = lcm;

    char *regexbuf = g_strdup_printf("^%s$", channel);
    GError *rerr = NULL;
    subscription->regex =
        g_regex_new(regexbuf, (GRegexCompileFlags) 0, (GRegexMatchFlags) 0, &rerr);
    g_free(regexbuf);
    if (rerr) {
        fprintf(stderr, "%s: %s\n", __FUNCTION__, rerr->message);
        dbg(DBG_LCM, "%s: %s\n", __FUNCTION__, rerr->message);
        g_error_free(rerr);
        free(subscription);
        return NULL;
    }
    g_rec_mutex_lock(&lcm->mutex);
    g_ptr_array_add(lcm->handlers_all, subscription);
    g_hash_table_foreach(lcm->handlers_map, map_add_handler_callback, subscription);
    g_rec_mutex_unlock(&lcm->mutex);

    return subscription;
}
```

输入 `channel` 被复制到 `subscription->channel`，handler 和 userdata 则按值保存地址；`regexbuf` 是临时字符串，在编译后释放，编译出的 `GRegex` 由节点持有。成功时 mutex 把“加入权威数组并更新已有 channel 缓存”作为一个事务提交。错误路径值得特别检查：provider hook 已经成功后，正则失败分支只 `free(subscription)`，没有释放 `subscription->channel`，也没有调用 provider 的 `unsubscribe`。因此 TCPQ 可能继续向服务器声明一个核心层已拒绝的 channel，且本地 channel 副本泄漏。复刻时应把后半段装配写成事务：任何失败都按已获取资源的逆序释放，并撤销之前成功的 provider 操作。

## 新订阅反向更新缓存

如果 `handlers_map` 已缓存 `POSE_FRONT`，之后新增 `POSE_.*` 订阅，不能等下一次 channel 首见才更新。代码遍历 map，对所有已知 channel 检查新 regex 并追加匹配项。

因此复杂度分配为：

```text
subscribe: O(number of known concrete channels)
first message of new channel: O(number of subscriptions)
steady message dispatch: O(1) lookup + O(matches)
```

这是典型的读多写少优化。机器人数据面消息远多于运行期 subscribe 操作时，代价分配合理。

## lcm_try_enqueue_message 只预留资格

Provider 完整接收一条消息后，先调用核心层 `lcm_try_enqueue_message()`。输入是目标 channel；函数查已缓存的匹配列表，在同一把核心 mutex 下逐个检查该订阅的待处理计数并为有空间者预留资格。以下是固定版本的完整函数：


```c
int lcm_try_enqueue_message(lcm_t *lcm, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);
    GPtrArray *handlers = lcm_get_handlers(lcm, channel);
    int num_keepers = 0;
    for (unsigned int i = 0; i < handlers->len; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        if (subscription->num_queued_messages < subscription->max_num_queued_messages ||
            subscription->max_num_queued_messages <= 0) {
            subscription->num_queued_messages++;
            num_keepers++;
        }
    }
    g_rec_mutex_unlock(&lcm->mutex);
    return num_keepers > 0;
}
```

这里的 `num_keepers` 是“至少一个订阅有空间”的资格判断，不是 payload 副本数。若它为 0，UDPM provider 释放当前 buffer；若大于 0，接收线程把唯一的完整消息加入 filled list。

这不是传统的“每 subscription 一条 payload queue”。`num_queued_messages` 是资格计数，实际 buffer 位于 provider 的全局 FIFO。分发到该消息时，只调用计数仍大于零的 handler。

### 同一份 payload，为什么三个订阅者能有三种丢帧结果？

先用一个有数字的实验看懂 `lcm_try_enqueue_message()`，再回头看上面那段源码。假设三个订阅匹配同一个 `ARM_STATE`，应用线程暂时没有调用 `handle()`：

| 订阅者 | 配额 | 用途 |
|---|---:|---|
| A | 1 | 控制器，只愿意留一条尚未处理的消息 |
| B | 2 | 记录模块，愿意保留两条 |
| C | 0 | 不设上限（LCM 的 `<=0` 约定） |

现在接收线程连续取得 M1、M2、M3，并分别对每条消息调用一次 `lcm_try_enqueue_message()`。我们先假定每个订阅初始 pending=0：

| 到来的消息 | A 的 pending | B 的 pending | C 的 pending | 这条消息是否进入公共队列 |
|---|---:|---:|---:|---|
| M1 | 1（保留） | 1（保留） | 1（保留） | 是 |
| M2 | 1（已满，放弃 M2） | 2（保留） | 2（保留） | 是 |
| M3 | 1（已满，放弃 M3） | 2（已满，放弃 M3） | 3（保留） | 是 |

注意这张表有一个反直觉的事实：**公共 FIFO 里仍然只有 M1、M2、M3 各一份，A/B/C 并没有各自保存消息副本。** 订阅者的 pending 只是一种尚未处理的准入资格，并不指向某个独占 payload。随后应用线程依次消费 FIFO，分发函数面对 M1 时发现三个订阅 pending 都大于零，分别减一并调用 callback；面对 M2 时只给 B、C 执行；面对 M3 时只给 C 执行。

~~~text
公共 FIFO:       [M1] -> [M2] -> [M3]
                   |       |       |
控制器 A:          √       ×       ×
记录器 B:          √       √       ×
无上限 C:          √       √       √
~~~

这样节省 payload 内存，但并不等于每个订阅都有独立消费线程：如果 A 在 M1 的 callback 里耗费 20 ms，B 和 C 连 M1 都还没处理，更不可能越过它先处理 M2。**独立配额隔离的是消息准入，并非 CPU 时间。**

真实代码里还有一个时序约束：上述对应关系依赖 provider 按接收顺序将完成的消息入队，再由单个 `handle()` 调用链按该顺序分发；它不是可在任意应用并发调度和订阅变更条件下使用的事务性投递承诺。要实现“控制器永远拿最新帧”，不能只把 A 配额设为 1；固定代码在已满时拒绝的是**新消息**，因此需要在应用自己的有界覆盖槽中进一步实现 drop-oldest/latest-only。
### 再追问一步：准入计数并没有记住“是哪一条消息”

上面的 M1/M2/M3 表有一个**隐藏前提**：三条消息都已经入队以后，应用才依次处理；接收线程没有在两次 handle 之间继续给同一订阅增加新资格。把这个前提去掉，固定源码中的两段计数操作会产生很值得研究的边界。

`lcm_try_enqueue_message()` 只给有空位的 subscription 增加 `num_queued_messages`，并没有在消息描述符中保存“B 订阅被准入”这个事实；`lcm_dispatch_handlers()` 面对当前公共 FIFO 消息时，只检查 B **当时的计数**是否大于零，决定是否调用 B。于是，刚刚为 M2 预留的计数有机会被队首的 M1 消费。

让 A 始终接收，B 只允许一条 pending，按照下面的时序安排 receiver 与 handle：

| 时刻 | 动作 | B pending | 公共 FIFO | 对 B 的真实影响 |
|---|---|---:|---|---|
| t0 | 接收 M0，A/B 接受 | 1 | M0 | M0 预留 |
| t1 | 接收 M1，B 满，A 接受 | 1 | M0,M1 | M1 被 B 拒绝 |
| t2 | handle M0，B 计数减一 | 0 | M1 | B 执行 M0 |
| t3 | 接收 M2，B 获得新资格 | 1 | M1,M2 | M2 预留 |
| t4 | handle M1，发现 B 计数大于零 | 0 | M2 | **B 实际执行 M1** |
| t5 | handle M2，B 已无资格 | 0 | 空 | **B 反而跳过 M2** |

公共 FIFO 并没有乱序，错配的是**“哪条消息被允许交给 B”与“B 实际处理了哪条”之间的对应关系**。若只需要按容量限制大致采样，这种只存计数的方案很简洁；若每个被准入的业务命令都必须严格匹配 message ID，它不能提供这种逐消息的投递证明。

用下面一份完全独立的 C++17 教学程序，在一条线程里按同样的交错次序执行，就能看到这个差异。`admitted` 是为了教学观测而额外保存的字段；固定 LCM 的共享消息队列没有对应的逐订阅标记。

~~~cpp
#include <cassert>
#include <deque>
#include <iostream>
#include <string>

struct Message {
    std::string id;
    bool admitted;
};
std::deque<Message> fifo;
int b_pending = 0;

void receive(const char* id) {
    const bool admitted = b_pending < 1;
    if (admitted) ++b_pending;
    // A 无限制，总会为这条消息保留公共 payload。
    fifo.push_back({id, admitted});
}

void handle_one() {
    Message m = fifo.front();
    fifo.pop_front();
    const bool called = b_pending > 0;
    if (called) --b_pending;
    std::cout << m.id << ": reserved=" << m.admitted
              << " called=" << called << '\n';
    if (m.id == "M1") assert(!m.admitted && called);
    if (m.id == "M2") assert(m.admitted && !called);
}

int main() {
    receive("M0");
    receive("M1");
    handle_one();
    receive("M2");
    handle_one();
    handle_one();
    assert(b_pending == 0 && fifo.empty());
}
~~~

如果重新设计一个**需要严格逐消息准入**的总线，可以把每条消息对应的订阅资格保存为位图/引用集合，或为重要订阅建立独立有界队列。两种方案都会增加索引和回收成本，但换来了“哪条消息被准入”可以独立验证的语义。工业控制的执行命令通常还需要跨进程 ACK、deadline 和幂等处理；订阅计数绝不等价于可靠命令投递。
## 单副本与订阅者隔离的折中

一份 payload 对多个 handler 的设计节省内存：

```text
one MessageBuffer
  +-> subscription A pending count
  +-> subscription B pending count
  `-> subscription C full, no count increment
```

但全局 FIFO 仍意味着 handler 执行顺序串行。A 很慢时，B 即使有独立配额，也要等待 A callback 返回。

配额隔离的是“为某订阅者保留多少历史”，不是 CPU 执行隔离。需要并行时，callback 应快速转交到应用自有队列；转交时必须复制或 decode，因为 provider payload 在 handle 返回后失效。

## 先自己实现一次：为什么不能在 callback 内直接删除订阅？

此刻先不要考虑网络。假设一条 `ARM_STATE` 已经成功接收，匹配到了三个订阅 A、B、C；A 的职责是监测设备状态，B 负责可视化，C 负责日志。我们最容易写出这样的代码：

~~~cpp
// 反例：callback 有权直接 erase 时，for 循环持有的下标和指针会失效。
for (auto* subscription : handlers) {
    subscription->callback(message);
}
~~~

A 的 callback 若直接删除 B，会触发两种可能：若 B 的对象被释放，下一轮可能解引用悬空指针；若用 `vector::erase` 删除 B，后面的 C 又向前移动，基于旧下标的循环可能跳过它。把循环完全放在 mutex 里也不是答案：业务 callback 可能试图再次修改订阅表，还可能进行耗时 I/O。

### 可以编译运行的订阅删除实验

我们暂时**不使用线程**，只复刻 LCM 的生命周期核心。以下是完整的 C++17 教学程序，刻意让 A 在处理当前消息时注销 B，并登记一个新订阅 D。固定上游并不使用 `std::unique_ptr` 来保存 subscription；这里用它是为了消除实验本身的内存泄漏干扰。

~~~cpp
#include <algorithm>
#include <cassert>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

struct Subscription {
    std::string name;
    std::function<void()> callback;
    int pending = 0;
    bool scheduled = false;
    bool removed = false;
};

class Dispatcher {
public:
    Subscription* add(std::string name, std::function<void()> callback) {
        auto item = std::make_unique<Subscription>();
        item->name = std::move(name);
        item->callback = std::move(callback);
        auto* raw = item.get();
        handlers_.push_back(std::move(item));
        return raw;
    }

    void unsubscribe(Subscription* item) {
        if (item->scheduled) {
            item->removed = true;       // 本轮仍持有指针，只标记
            return;
        }
        handlers_.erase(std::remove_if(handlers_.begin(), handlers_.end(),
            [item](const auto& p) { return p.get() == item; }),
            handlers_.end());
    }

    void admit() {
        for (auto& p : handlers_)
            if (!p->removed) ++p->pending;  // 这里只模拟准入，未设置上限
    }

    void dispatch() {
        const auto count = handlers_.size(); // 冻结本轮数量
        for (std::size_t i = 0; i < count; ++i)
            handlers_[i]->scheduled = true;

        for (std::size_t i = 0; i < count; ++i) {
            auto* p = handlers_[i].get();
            if (!p->removed && p->pending > 0) {
                --p->pending;
                p->callback();           // 不在锁内运行用户代码
            }
        }

        for (std::size_t i = 0; i < count; ++i)
            handlers_[i]->scheduled = false;
        handlers_.erase(std::remove_if(handlers_.begin(), handlers_.end(),
            [](const auto& p) { return p->removed; }), handlers_.end());
    }

private:
    std::vector<std::unique_ptr<Subscription>> handlers_;
};

int main() {
    Dispatcher bus;
    std::vector<std::string> calls;
    Subscription* b = nullptr;
    bool first = true;

    bus.add("A", [&] {
        calls.push_back("A");
        if (first) {
            first = false;
            bus.unsubscribe(b);          // B 尚未执行，但仍在本轮快照内
            bus.add("D", [&] { calls.push_back("D"); });
        }
    });
    b = bus.add("B", [&] { calls.push_back("B"); });
    bus.add("C", [&] { calls.push_back("C"); });

    bus.admit();
    bus.dispatch();
    assert((calls == std::vector<std::string>{"A", "C"}));

    bus.admit();
    bus.dispatch();
    assert((calls == std::vector<std::string>{"A", "C", "A", "C", "D"}));
}
~~~

使用 `g++ -std=c++17 -Wall -Wextra -Werror -pedantic` 编译。第一次分发时，我们冻结 `count=3` 并把 A、B、C 都标记为 scheduled。A 取消 B 时，B **没有立即析构**，只记录 `removed=true`；A 新登记 D，D 排在原有三个元素之后，不属于本次冻结的范围。随后 B 被跳过、C 正常执行。循环结束，旧 B 才被集中回收，下一次消息才轮到 D。

~~~text
刚入队:     A(pending=1)  B(1)  C(1)
冻结本轮:   [A scheduled] [B scheduled] [C scheduled]
执行 A:     B.removed=true; append D
执行 B:     已标记删除，跳过
执行 C:     正常运行
安全点:     清 scheduled，物理删除 B
下一轮:     A、C、D
~~~

现在再回到真实 `lcm_dispatch_handlers()`，读者就能预测它为什么有三个循环：第一个冻结本轮并设置 `callback_scheduled`，第二个解锁执行符合准入条件的 callback，第三个清标记、收集并销毁延期删除对象。这里的 `scheduled` **不是正在占用 CPU**，只是“本轮仍有可能访问这个对象”的生命周期承诺。

教学例子故意省略了并发：它没有 mutex，假定只有一个 dispatch 调用栈。真实 LCM 的 `lcm_t::mutex` 必须保护登记/取消/计数和逻辑删除；独立的 `handle_mutex` 则串行化完整的 `handle()`。把教学例子里的单线程成功直接推广为跨线程安全，是错误的。
## 分发时如何冻结迭代边界，却在锁外执行业务

此时 provider 已经接收完整消息，核心层也知道这条具体 channel 应交给哪些 handler。新的矛盾是：业务 callback 可能很慢，还可能在内部执行 `unsubscribe()`，因此不能一直持有核心订阅表的互斥锁；但如果解锁后让 callback 直接删除正在遍历的 handler，又会发生迭代器失效和悬空指针。

一个朴素的错误版本是 `for (handler : vector) handler(message)`。假设 A、B 两只订阅收到同一帧图像；A 回调中取消 B 并释放它的对象，for 循环下一次访问 B 的指针就可能触发 use-after-free。给整轮循环加锁也不是完整答案，因为用户代码可能反过来调用订阅接口、执行文件 I/O，或与接收线程争同一把锁。

LCM 采用**冻结当前迭代边界、逻辑删除、最后集中回收**三阶段。下面给出固定提交的完整连续 `lcm_dispatch_handlers()`，没有把重要的锁域和回收分支藏进教学伪代码：

~~~c
int lcm_dispatch_handlers(lcm_t *lcm, lcm_recv_buf_t *buf, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);

    GPtrArray *handlers = lcm_get_handlers(lcm, channel);

    // ref the handlers to prevent them from being destroyed by an
    // lcm_unsubscribe.  This guarantees that handlers 0-(nhandlers-1) will not
    // be destroyed during the callbacks.  Store nhandlers in a local variable
    // so that we don't iterate over handlers that are added during the
    // callbacks.
    int nhandlers = handlers->len;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        subscription->callback_scheduled = 1;
    }

    // now, call the handlers.
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        if (!subscription->marked_for_deletion && subscription->num_queued_messages > 0) {
            subscription->num_queued_messages--;
            g_rec_mutex_unlock(&lcm->mutex);
            subscription->handler(buf, channel, subscription->userdata);
            g_rec_mutex_lock(&lcm->mutex);
        }
    }

    // unref the handlers and check if any should be deleted
    GList *to_remove = NULL;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        subscription->callback_scheduled = 0;
        if (subscription->marked_for_deletion)
            to_remove = g_list_prepend(to_remove, subscription);
    }
    // actually delete handlers marked for deletion
    for (; to_remove; to_remove = g_list_delete_link(to_remove, to_remove)) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) to_remove->data;
        g_ptr_array_remove(lcm->handlers_all, subscription);
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        lcm_handler_free(subscription);
    }
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
}
~~~

第一段在核心 mutex 下得到 channel 的缓存 handler 数组，保存 `nhandlers`，再给这批 subscription 标记 `callback_scheduled=1`。这个标志不是引用计数，也不是“当前就在执行”的状态，而是通知 `unsubscribe()`：本轮迭代仍可能解引用这个对象，所以现在只能逻辑删除，不能移动数组槽位或 `free`。`nhandlers` 冻结数量而非完整副本；在 callback 中新增的订阅不会意外插入本次消息的遍历范围。

第二段逐个判断 `marked_for_deletion` 和配额 `num_queued_messages`。对有资格的节点，先扣掉配额，再 **释放 `lcm->mutex` 执行 `subscription->handler(buf, channel, userdata)`**，完成后重新加锁。这样业务 callback 能够修改订阅表；但本次处理仍发生在调用 `lcm_handle()` 的应用线程上。A 耗时 20 ms，则同一条消息的 B 仍必须等这 20 ms，并不会因独立配额就获得独立线程。真正的 CPU 隔离需要业务 callback 快速复制或 decode 数据后，把工作交给另一个有界应用队列。

第三段解除所有本轮迭代标志，将 `marked_for_deletion` 的对象收集到 `to_remove`，然后从 `handlers_all`、所有 channel 缓存及堆内存中一次性移除。这一安全点保护的是核心内部 `lcm_subscription_t` 节点，不拥有用户提供的 `userdata`。外部管理线程若取消订阅后马上释放 userdata，而 handle 线程仍在执行该回调，依然可能发生悬空访问；需要先停 handle 循环并等待它退出。

这里同时解释了为什么 `lcm_handle()` 用独立 `handle_mutex` 串行化完整处理，又用 `in_handle` 禁止递归 handle：核心允许 callback 调用部分订阅管理函数，但不允许同一 handle 过程递归消费下一条消息来绕过当前批次的回收边界。
## callback 内 unsubscribe 使用延迟删除

如果 handler 在自身 callback 中调用 unsubscribe，立即从数组删除并 free 会让 dispatch 循环持有悬空指针。LCM 检查：


```c
if (subscription->callback_scheduled) {
    subscription->marked_for_deletion = 1;
    return success;
}
```

当前批次结束后，dispatch 清除 scheduled 标记，收集 marked subscription，再统一从 `handlers_all` 和所有 channel cache 删除并 free。

这是 deferred reclamation：逻辑删除立即生效，物理回收推迟到已知没有迭代者的安全点。这里保护的是 C 核心的 `lcm_subscription_t`。下面的固定实现还显示了另一个边界：当前批次中的 unsubscribe 只设置标记并跳过 provider hook。此时输入是被取消的节点；核心 mutex 覆盖标记与容器操作，provider 私有状态并未因此同步改变。


```c
int lcm_unsubscribe(lcm_t *lcm, lcm_subscription_t *subscription)
{
    g_rec_mutex_lock(&lcm->mutex);

    int foundit = 0;

    // when called from within an lcm_handle callback, tampering with the
    // handlers_map will throw off the indices in lcm_dispatch_handlers, so
    // check for the dispatch sentinels and skip the body of this function if
    // they're present.
    if (!subscription || subscription->marked_for_deletion)
        goto done;
    if (subscription->callback_scheduled) {
        subscription->marked_for_deletion = 1;
        foundit = 1;
        goto done;
    }

    // remove the handler from the master list
    foundit = g_ptr_array_remove(lcm->handlers_all, subscription);

    if (lcm->provider && lcm->vtable->unsubscribe) {
        lcm->vtable->unsubscribe(lcm->provider, subscription->channel);
    }

    if (foundit) {
        // remove the handler from all the lists in the hash table
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        if (!subscription->callback_scheduled)
            lcm_handler_free(subscription);
        else
            subscription->marked_for_deletion = 1;
    }

done:
    g_rec_mutex_unlock(&lcm->mutex);

    return foundit ? 0 : -1;
}
```

`callback_scheduled` 分支在设置 `marked_for_deletion` 后直接跳到 `done`，所以这个路径不调用 `vtable->unsubscribe`。这对没有取消 hook 的 UDPM 不明显；TCPQ provider 会保留服务器侧订阅，直到 provider 被重建或销毁。于是“核心不再调用 callback”与“传输层停止接收/转发”是两个不同状态，不能把当前 callback 内 unsubscribe 的成功返回理解成两边都已完成。

C++ wrapper 还有一层更短的寿命：`lcm::LCM::unsubscribe()` 调用 C API 后马上从自己的 `subscriptions` vector 擦除并 `delete` C++ wrapper。该 wrapper 同时保存 `channel_buf` 和成员 handler；成员回调 trampoline 把 `channel_buf` 以 `const std::string&` 传入用户函数。因此若 C++ handler 自行 unsubscribe 后还读 `channel`，引用就指向已释放的 wrapper 成员。工程上可把 unsubscribe 请求投递到 handle loop，在当前 callback 返回后处理；关闭时则先停止并 join handle 线程，再取消剩余订阅。

C++ 中可以用 `shared_ptr<Subscription>` 简化对象寿命，但仍需要定义“当前消息是否继续调用刚取消的订阅”。内存安全不会自动给出事件语义。

把一次 dispatch 从查表直到回收连起来看，才能看到锁保护的状态和对象何时安全释放。`lcm_udpm_handle()` 已从 provider 队列取出一条消息，然后在 provider 锁外调用核心 `lcm_dispatch_handlers()`；因此回调运行在线程调用 `lcm_handle()` 的调用栈中。下面从构造接收视图处摘取连续语句，输入 `lcmb` 是刚从 filled queue 取出的唯一完整消息 buffer。

把 provider 队列中的完整消息交给核心前，`lcm_udpm_handle()` 先构造一个只在本次同步分发期间有效的接收视图：

```c
    lcm_recv_buf_t rbuf;
    rbuf.data = (uint8_t *) lcmb->buf + lcmb->data_offset;
    rbuf.data_size = lcmb->data_size;
    rbuf.recv_utime = lcmb->recv_utime;
    rbuf.lcm = lcm->lcm;

    if (lcm->creating_read_thread) {
        // special case:  If we're creating the read thread and are in
        // self-test mode, then only dispatch the self-test message.
        if (!strcmp(lcmb->channel_name, SELF_TEST_CHANNEL))
            lcm_dispatch_handlers(lcm->lcm, &rbuf, lcmb->channel_name);
    } else {
        lcm_dispatch_handlers(lcm->lcm, &rbuf, lcmb->channel_name);
    }

    g_rec_mutex_lock(&lcm->mutex);
    lcm_buf_free_data(lcmb, lcm->ringbuf);
    lcm_buf_enqueue(lcm->inbufs_empty, lcmb);
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
```

`rbuf` 是栈上视图，`data` 借用 `lcmb` 中的 payload；这段函数在调用 `lcm_dispatch_handlers()` 时没有持有 provider mutex，callback 因而不会长时间阻塞接收线程队列锁，但仍阻塞当前 `lcm_handle()` 调用。所有 callback 返回后 provider 才重新加锁，释放 payload 对 ring 的占用并把 `lcmb` 节点放回空闲链表。回调保存 `rbuf.data` 到返回之后会读到过期存储。


```c
int lcm_dispatch_handlers(lcm_t *lcm, lcm_recv_buf_t *buf, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);

    GPtrArray *handlers = lcm_get_handlers(lcm, channel);

    // ref the handlers to prevent them from being destroyed by an
    // lcm_unsubscribe.  This guarantees that handlers 0-(nhandlers-1) will not
    // be destroyed during the callbacks.  Store nhandlers in a local variable
    // so that we don't iterate over handlers that are added during the
    // callbacks.
    int nhandlers = handlers->len;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        subscription->callback_scheduled = 1;
    }

    // now, call the handlers.
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        if (!subscription->marked_for_deletion && subscription->num_queued_messages > 0) {
            subscription->num_queued_messages--;
            g_rec_mutex_unlock(&lcm->mutex);
            subscription->handler(buf, channel, subscription->userdata);
            g_rec_mutex_lock(&lcm->mutex);
        }
    }

    // unref the handlers and check if any should be deleted
    GList *to_remove = NULL;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        subscription->callback_scheduled = 0;
        if (subscription->marked_for_deletion)
            to_remove = g_list_prepend(to_remove, subscription);
    }
    // actually delete handlers marked for deletion
    for (; to_remove; to_remove = g_list_delete_link(to_remove, to_remove)) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) to_remove->data;
        g_ptr_array_remove(lcm->handlers_all, subscription);
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        lcm_handler_free(subscription);
    }
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
}
```

入口先在 `lcm->mutex` 下取得 channel 的匹配数组并固定 `nhandlers`，再把这一批订阅标成 scheduled，保证 callback 中新增 handler 不会插入当前遍历。随后每个有待处理资格的订阅先减计数，再暂时解锁执行业务 callback；provider 的 `lcm_buf_t` 在这期间仍由当前 handle 调用持有，所以所有 handler 共用同一份 payload view。callback 返回后重新加锁，把删除标记清除出 authoritative list 与所有 channel cache，最后才释放 C subscription 节点。这只保护核心 subscription；`userdata` 和 C++ wrapper 的所有权仍由应用负责，且该 mutex 不会使跨线程 `lcm_destroy()` 等待外部 handle loop。

## 取消订阅对当前消息的语义

回调 A 若取消尚未执行的回调 B，B 的 `marked_for_deletion` 会变为 true。dispatch 到 B 时检查标记并跳过。

因此取消可以影响同一条消息中排在后面的 handler。实际顺序来自匹配数组插入顺序，不应被业务当作强协议保证。

需要事务式 fan-out 的系统可以先对 handler 强引用快照，再规定当前消息一定投递给快照中的全部订阅；LCM 选择了更直接的即时逻辑删除。

## handle_mutex 与递归限制

同一 `lcm_t` 的所有 `handle()` 由 `handle_mutex` 串行化。另有 `in_handle` 断言禁止 callback 递归调用 handle。

如果允许递归，内层 handle 可能：

- 处理下一条 provider buffer；
- 改变 subscription pending count；
- 删除外层循环仍标记 scheduled 的对象；
- 让 callback 调用栈深度由消息链无界增长。

禁止递归把事件循环保持为明确的一层。callback 可以 publish，因为发布不进入同一分发状态机。

## 正则与缓存的线程安全

订阅增删、首次 channel 缓存建立、pending count 和删除标记都受 `lcm->mutex` 保护。该锁是递归 mutex，因为内部帮助函数可能在已经持锁时再次进入 `lcm_get_handlers()`。

递归锁降低函数组合难度，却也可能掩盖层次不清和意外重入。自行实现时可以采用非递归 mutex，并明确区分：

```text
GetHandlers()        // acquires lock
GetHandlersLocked()  // caller already owns lock
```

这样锁契约更容易静态审查，也能发现同线程重复获取导致的设计问题。

## 队列上限的语义

`max_num_queued_messages <= 0` 表示不设上限。默认值为 30，可按 subscription 修改。

上限为 N 的直接含义是：该 subscription 至多累计 N 次尚未消费的**准入计数**。达到上限时，接收端不再给它增加计数；但只要另一订阅仍接纳消息，这条消息仍可能进入公共 FIFO。由于计数并没有与具体消息 ID 绑定，在接收线程与 handle 交错运行时，刚刚为后续消息增加的计数也可能被队首另一条消息先消费。前面的 M0/M1/M2 实验给出了确切时序，因此这里不能把配额描述成严格的“逐消息保留 N 条”。

这是 drop-newest 策略，而许多控制系统更希望 drop-oldest、只保留最新状态。LCM 的全局单副本 FIFO 很难为不同订阅者同时执行不同的 payload 淘汰策略；若需要 latest-only，应在 callback 后的应用队列中实现覆盖槽。

## 分发复杂度

已知 channel 的单条消息成本近似为：

```text
O(1) hash lookup
+ O(H) pending-count admission
+ O(H) callback scan
+ sum(callback execution time)
```

其中 H 是匹配 subscription 数。两次 O(H) 通常远小于业务 callback，但大量通配订阅会扩大每条消息成本。

全局 mutex 在 admission 和 dispatch bookkeeping 阶段竞争。callback 在锁外，因此锁持有时间不直接包含业务 WCET。

## 可复刻的数据结构


```cpp
struct Subscription {
  Regex pattern;
  Callback callback;
  std::size_t pending = 0;
  std::size_t limit = 30;
  bool dispatching = false;
  bool remove_requested = false;
};

class SubscriptionIndex {
  std::vector<std::shared_ptr<Subscription>> all_;
  std::unordered_map<std::string,
      std::vector<std::weak_ptr<Subscription>>> cache_;
};
```

使用 weak pointer 的 channel cache 可避免缓存反向延长已取消订阅寿命。dispatch 先锁定一组 shared pointer 快照，再释放 index mutex 执行 callback。

仍需单独实现 pending quota，因为 shared pointer 只解决内存寿命，不解决过载策略。

## 最后一道边界：C 层延迟删除并不等于 C++ 用户对象自动安全

上一节的 `callback_scheduled` 与 `marked_for_deletion` 解决了一个非常具体的问题：当本轮分发已经保存了一组 subscription 指针时，不能在某只 callback 里立即删除其中一只，令下一次循环访问悬空地址。但这套协议保护的是 **LCM 自己的 subscription 节点**，不是用户自己分配的全部对象。

假设 A 的 callback 调用 `unsubscribe(B)`，同时另一条应用线程认为 B 已经注销，于是马上 `delete b_handler`。如果正在执行中的 B callback 或另一次尚未完成的用户工作仍持有该对象地址，LCM 对 subscription 指针的延迟释放并不能让那根裸指针恢复安全。固定 C++ wrapper 的适配对象中仍然存放用户 `Handler*`；它不是自动管理 Handler 生命周期的共享所有权句柄。

因此需要把应用的两层协议明确分开：第一层是 LCM 在一次 `lcm_dispatch_handlers()` 内冻结当前迭代范围、锁外执行 callback、最后回收标记的节点；第二层由应用负责停止其他 `handle` 调用、退出所有在途 Handler、管理异步队列及最终销毁业务对象。不能把 `unsubscribe()` 返回视为另一条线程上的所有用户操作都已结束，更不能从“持有 `handle_mutex`”推导出整个进程不存在其他并发访问。

一个完整的订阅语义测试应同时记录：A 与 B 的注册顺序、回调内 A 取消 B、B 是否在本轮仍被跳过、回调中新加入 C 是否延迟到下一条消息、A/B/C 不同容量下哪条消息得到准入，以及正常和异常退出时有没有仍在使用 `userdata` 的业务代码。这些都是源码里的状态字段实际维护的因果关系，不是简单的“正则匹配成功”。

## 分发层的设计结论

LCM 订阅层通过两级索引把正则成本移到首次 channel 和订阅变更，通过每订阅者计数限制积压，再用锁外 callback 与延迟删除解决可重入生命周期。

最值得复用的不是 GLib 容器，而是四条规则：权威集合与热路径缓存分离；payload 单副本但配额按订阅者统计；永不持内部锁调用用户代码；取消订阅的逻辑生效与物理回收分开处理。
