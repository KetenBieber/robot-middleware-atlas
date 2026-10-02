# LCM C/C++ 订阅生命周期：Deferred Delete、Trampoline 与 Quiescence

本文固定到 LCM 提交 `ad0c54cee0ec048ef12357c34349ec1443158864`。

[订阅与分发](subscription-dispatch.md) 已经解释了 C core 为什么需要 `callback_scheduled` 与 `marked_for_deletion`：当一轮 `lcm_dispatch_handlers()` 已经冻结当前 handler 集合时，callback 内不能直接 free 其中的节点，否则后续遍历就可能访问悬空地址。

如果只读到这里，很容易得到一个过早的结论：

> LCM 已经支持 callback 内安全 `unsubscribe()`。

这个结论只对了一半。

C core 的确为 `lcm_subscription_t` 做了 deferred delete；但 C++ API 又在它之上增加了第二个对象：

```text
lcm_subscription_t
        |
        | userdata
        v
lcm::Subscription-derived adapter
```

C core 延迟回收的是上面的 C 节点。

`lcm::LCM::unsubscribe()` 却会立即删除下面的 C++ adapter。

这两层回收时刻不一致，正是本文要拆开的核心问题。

---

# 1. 先画出完整对象图

C++ typed subscription 不是直接把业务 Handler 塞进 C core。

以成员函数版本为例：

```text
Application Handler object
        ^
        | raw pointer
        |
LCMMHSubscription<MessageType, Handler>
        ^
        | userdata
        |
lcm_subscription_t
        ^
        |
lcm_t handlers_all / handlers_map
```

这里有三层对象：

1. `lcm_subscription_t`：C core subscription 节点；
2. `LCMMHSubscription<...>`：C++ callback adapter；
3. 用户自己的 `Handler`：业务对象。

三层对象没有同一个 owner。

因此“unsubscribe”必须分别回答：

```text
C subscription 何时不可见？
C subscription 何时 free？
C++ adapter 何时 delete？
业务 Handler 何时允许析构？
```

只回答第一问远远不够。

---

# 2. C core 的 subscription 节点保存什么

固定源码中的核心结构：

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

它只知道：

```text
handler  = C function pointer
userdata = opaque void*
```

C core 不知道 `userdata` 指向：

- C++ 对象；
- 栈变量；
- ref-counted state；
- 静态对象；
- 业务 Handler；
- 语言绑定 adapter。

因此 core 只能保护自己拥有的 subscription 节点。

---

# 3. C++ binding 把 adapter 地址放进 userdata

成员函数 typed subscribe 最终构造：

```cpp
auto *subs =
    new LCMMHSubscription<
        MessageType,
        MessageHandlerClass>();

subs->handler = handler;
subs->handlerMethod = handlerMethod;

subs->c_subs =
    lcm_subscribe(
        this->lcm,
        channel.c_str(),
        LCMMHSubscription<
            MessageType,
            MessageHandlerClass>::cb_func,
        subs);

subscriptions.push_back(subs);
```

所以 C core 保存的 `userdata` 实际是：

```text
LCMMHSubscription* subs
```

这个地址必须至少存活到：

```text
最后一次可能进入 cb_func 的调用结束
```

否则 `static_cast` 恢复出来的就是悬空指针。

---

# 4. C++ Subscription 本身还拥有 channel_buf

公共基类：

```cpp
class Subscription {
  protected:
    Subscription()
    {
        channel_buf.reserve(
            LCM_MAX_CHANNEL_NAME_LENGTH);
    }

    lcm_subscription_t *c_subs;
    std::string channel_buf;
};
```

`channel_buf` 不是装饰字段。

它是 C++ callback 的 `const std::string& channel` 的实际存储。

设计动机很明确：

```text
每次 callback
不重新 heap allocate std::string
```

而是复用 adapter 内的一块 workspace。

因此 adapter 生命周期同时决定了：

```text
userdata pointer lifetime
channel reference lifetime
```

---

# 5. typed trampoline 怎样进入用户代码

成员函数版本：

```cpp
static void cb_func(
    const lcm_recv_buf_t *rbuf,
    const char *channel,
    void *user_data)
{
    auto *subs =
        static_cast<
          LCMMHSubscription<
            MessageType,
            MessageHandlerClass> *>(
              user_data);

    MessageType msg;

    int status =
        msg.decode(
            rbuf->data,
            0,
            rbuf->data_size);

    if (status < 0)
        return;

    const ReceiveBuffer rb = {
        rbuf->data,
        rbuf->data_size,
        rbuf->recv_utime
    };

    subs->channel_buf = channel;

    (subs->handler
        ->*subs->handlerMethod)(
            &rb,
            subs->channel_buf,
            &msg);
}
```

这里出现了三类 lifetime：

```text
MessageType msg
  trampoline 栈上拥有

ReceiveBuffer rb
  trampoline 栈上拥有
  但 rb.data 借用 provider payload

channel argument
  引用 subs->channel_buf
```

其中第三项直接绑定 adapter 生命周期。

---

# 6. C core 为什么不能 callback 内立即 free subscription

`lcm_dispatch_handlers()` 首先冻结当前分发范围：

```c
int nhandlers = handlers->len;

for (int i = 0;
     i < nhandlers;
     i++)
{
    lcm_subscription_t *subscription =
        g_ptr_array_index(
            handlers, i);

    subscription->callback_scheduled = 1;
}
```

然后 callback 才真正执行。

如果 callback 中立即 free 自己：

```text
handlers[i] -> freed memory
```

回到 dispatch 后仍然会继续处理：

```text
handlers[0 ... nhandlers-1]
```

于是数组中可能留下悬空节点。

所以 LCM 没有这么做。

---

# 7. lcm_unsubscribe 的 deferred-delete 分支

固定源码：

```c
if (subscription->callback_scheduled) {
    subscription->marked_for_deletion = 1;
    foundit = 1;
    goto done;
}
```

也就是说 callback 正在当前 dispatch 集合中时：

```text
unsubscribe
    |
    v
mark logical deletion
    |
    v
return
```

不 free `lcm_subscription_t`。

这是非常典型的：

```text
retire now
reclaim later
```

---

# 8. dispatch 结束才真正回收 C 节点

callback 阶段结束后：

```c
subscription->callback_scheduled = 0;

if (subscription->marked_for_deletion)
    to_remove =
        g_list_prepend(
            to_remove,
            subscription);
```

随后统一：

```c
g_ptr_array_remove(
    lcm->handlers_all,
    subscription);

g_hash_table_foreach(
    lcm->handlers_map,
    map_remove_handler_callback,
    subscription);

lcm_handler_free(subscription);
```

这形成了一个小型 grace period：

```text
scheduled callbacks begin
        |
        v
retire requests accumulate
        |
        v
all current callbacks leave
        |
        v
reclaim retired nodes
```

虽然它没有叫 RCU，但生命周期思想是一致的。

---

# 9. C API 的 unsubscribe 文档也体现了这层语义

C API 文档写明：

```text
After this function returns,
handler is no longer valid
and should not be used anymore.
```

这里的 `handler` 指：

```text
lcm_subscription_t*
```

从 API 使用者角度，unsubscribe 后不能再拿这个 handle 调操作。

内部为了完成当前 dispatch，可以继续暂时保留内存。

这是：

```text
public logical lifetime
<
internal physical lifetime
```

的典型设计。

---

# 10. 问题出在 C++ wrapper 自己又有一层对象

C++ `LCM::unsubscribe()`：

```cpp
int status =
    lcm_unsubscribe(
        lcm,
        subscription->c_subs);

subscriptions.erase(iter);

delete subscription;

return status;
```

注意这里没有：

```text
if callback_scheduled
then defer delete C++ adapter
```

无论 C core 是立即删除还是仅做：

```text
marked_for_deletion = 1
```

C++ wrapper 都马上：

```text
erase
delete
```

于是两层生命周期分叉。

---

# 11. C core 的 grace period 没有覆盖 userdata

真正的关系是：

```text
C layer:
lcm_subscription_t
    callback_scheduled = 1
    marked_for_deletion = 1
    still alive

C++ layer:
Subscription adapter
    delete now
```

但 C node 中仍保存：

```text
userdata = deleted adapter address
```

C core 当前没有再次调用这个 userdata 时，可能暂时没出问题。

但“C 节点还活着”不代表：

```text
userdata 仍有效
```

---

# 12. 先分析最直观的 self-unsubscribe

假设业务 callback：

```cpp
void Handler::OnMessage(
    const lcm::ReceiveBuffer *,
    const std::string& channel,
    const Msg *)
{
    lcm.unsubscribe(sub_);
}
```

真实调用栈：

```text
lcm_handle
  |
  v
lcm_dispatch_handlers
  |
  v
LCMMHSubscription::cb_func
  |
  v
Handler::OnMessage
  |
  v
LCM::unsubscribe
```

C core 看到：

```text
callback_scheduled == 1
```

只标记删除。

但 C++ wrapper 随后立即：

```text
delete sub_
```

---

# 13. 当前 callback 的 channel 引用立即悬空

callback 参数：

```cpp
const std::string& channel
```

实际引用：

```text
sub_->channel_buf
```

而 `delete sub_` 会析构：

```text
Subscription
  |
  v
std::string channel_buf
```

所以 unsubscribe 返回以后：

```text
channel reference
    |
    v
dangling
```

如果 callback 后面继续：

```cpp
lcm.unsubscribe(sub_);

if (channel == "ARM_STATE") {
    ...
}
```

这已经不再有有效对象为 `channel` 提供存储。

---

# 14. “Subscription 已被销毁”其实写在 C++ API 文档里

C++ `unsubscribe()` 文档明确写：

```text
The Subscription object
is destroyed by this method.
```

因此：

```text
Subscription* sub
```

在调用成功后本来就不能再使用。

更隐蔽的是：

> callback 的某个参数本身就是这个 Subscription 对象内部成员的引用。

这使“销毁 Subscription”影响了当前仍未返回的 callback 参数。

---

# 15. self-unsubscribe 不只是 Subscription* 不能再用

容易误解成：

```text
unsubscribe 后
只要不再访问 sub 指针就行
```

实际还要检查所有从 adapter 派生出来的借用对象：

```text
channel reference
handler storage
context storage
callable storage
```

任何一个仍被当前调用栈借用，都可能受到 adapter delete 影响。

---

# 16. lambda subscription 的风险更深一层

C++11 lambda 版本：

```cpp
class LCMLambdaSubscription
    : public Subscription
{
    HandlerFunction handler;

    static void cb_func(...)
    {
        auto *subs =
            static_cast<
              LCMLambdaSubscription *>(
                user_data);

        // ...

        (subs->handler)(
            &rb,
            subs->channel_buf,
            &msg);
    }
};
```

这里真正被执行的 callable：

```text
std::function handler
```

就是 adapter 自己的成员。

---

# 17. lambda 自取消会销毁正在参与调用的 callable owner

调用链：

```text
subs->handler.operator()
        |
        v
user lambda
        |
        v
LCM::unsubscribe(subs)
        |
        v
delete subs
        |
        v
~std::function handler
```

此时：

```text
std::function::operator()
```

对应的对象正在参与当前调用，容器对象却被销毁。

不能依赖某个标准库实现“刚好在 target 返回后不再访问自身内部状态”。

因此 lambda self-unsubscribe 比单纯 dangling channel 更需要谨慎：

> 当前 callable 的 owner 正在它自己的执行期间被销毁。

---

# 18. 成员函数 adapter 与 lambda adapter 的风险不完全一样

成员函数 adapter 保存：

```text
Handler*
member-function pointer
channel_buf
```

调用进入业务 Handler 后，函数调用目标已经确定。

self-unsubscribe 主要直接破坏：

```text
adapter object
channel_buf
```

而 lambda adapter 还会析构：

```text
正在被调用的 std::function member
```

这说明语言绑定层不能只看“所有 adapter 都是一个 void*”。

内部成员的 ownership 形状也会影响 teardown 风险。

---

# 19. function + context 版本同样依赖 adapter

`LCMTypedSubscription<MessageType, ContextClass>` 保存：

```text
ContextClass context
function pointer handler
channel_buf
```

如果 `ContextClass` 是复杂 RAII 对象，而 callback 内 self-unsubscribe：

```text
delete adapter
→ destroy context member
```

具体是否影响正在运行的 handler，取决于 context 如何传递。

固定接口是按值传入 callback，因此参数构造完成后通常与 adapter 内原对象脱离；但 channel 仍然是 adapter 成员引用。

不同模板路径必须分别检查，不能用一个“userdata 安全”概括全部。

---

# 20. callback A 取消 callback B 是另一种情况

当前正在执行 A：

```text
A callback running
```

A 调用：

```text
unsubscribe(B)
```

C core 已经提前把 A/B/... 都标成：

```text
callback_scheduled = 1
```

所以 B：

```text
marked_for_deletion = 1
```

当 dispatch 循环走到 B：

```c
if (!subscription->marked_for_deletion
    && ...)
```

会直接跳过 B。

---

# 21. 因此 B 的 C++ adapter 立即 delete 通常不会被当前分发再使用

这是一个重要区别。

对于：

```text
A deletes B before B callback starts
```

C core 的 `marked_for_deletion` 会阻止：

```text
B->handler(...)
```

所以当前分发不会再进入 B 的 userdata。

而：

```text
A deletes A while A callback is already active
```

删除的是当前正在使用的 userdata owner。

不能把这两个场景混成一个“callback 内 unsubscribe”。

---

# 22. 于是需要区分 pre-entry、active、post-callback 三个阶段

对一个 subscription：

```text
SCHEDULED
   |
   | handler not entered yet
   v
ACTIVE CALLBACK
   |
   v
CALLBACK RETURNED
   |
   v
RECLAIM
```

C core 的 `callback_scheduled` 覆盖：

```text
SCHEDULED
+
ACTIVE CALLBACK
```

它故意比单纯：

```text
callback 正在业务代码中
```

更宽。

这正是 deferred deletion 能保护 pre-entry 节点的原因。

---

# 23. 为什么 naive in_flight++ 还不够

一个常见修复思路是：

```cpp
static void cb_func(...)
{
    ++adapter->in_flight;
    user_callback();
    --adapter->in_flight;
}
```

unsubscribe：

```text
mark stopping
wait in_flight == 0
delete adapter
```

看起来已经像 quiescence。

但还有一个窗口。

---

# 24. pre-entry race

C core：

```text
callback_scheduled = 1
```

之后会：

```text
unlock lcm mutex
→ call subscription->handler(...)
```

中间存在：

```text
C callback 已获得执行资格
但 C++ trampoline 尚未 active++
```

另一线程此时：

```text
unsubscribe
→ C sees callback_scheduled = 1
→ marks deletion
→ returns
→ C++ sees in_flight == 0
→ delete adapter
```

随后 handle thread：

```text
enters cb_func(userdata)
```

`userdata` 已经悬空。

所以：

> **只在 trampoline 入口维护 in-flight counter，无法覆盖 callback 已 scheduled 但尚未进入 binding 的窗口。**

---

# 25. 这就是为什么 grace period 必须跨抽象层

真正需要保护的是：

```text
C scheduled
        |
        v
C++ trampoline entry
        |
        v
user callback
        |
        v
C++ trampoline exit
```

wrapper 不能只看中间一段。

它必须知道：

```text
C core 何时保证
这个 userdata 再也不会被调用
```

当前公开 C++ wrapper 并没有这样的回收通知。

---

# 26. C API 的 thread-safe 也不能自动覆盖 C++ wrapper

C 头文件明确写：

```text
All LCM functions are internally
synchronized and thread-safe.
```

这个声明覆盖 C API 内部：

```text
lcm_t
handlers_all
handlers_map
queue counters
provider interaction
```

C++ wrapper 却额外增加：

```cpp
std::vector<Subscription *>
    subscriptions;
```

以及：

```text
new/delete adapter
std::string channel_buf
std::function handler
```

这些不是 `lcm_t::mutex` 自动保护的对象。

---

# 27. “pure header wrapper” 不等于同步语义自动继承

C++ API 文档称它是 C API 上的纯 header wrapper。

这描述的是：

```text
implementation / linking architecture
```

不是：

```text
all wrapper-local state
inherits C mutex
```

只要 binding 增加新的：

- container；
- object；
- cache；
- allocator；
- ownership；

就必须重新审计自己的同步与 lifetime。

---

# 28. C++ subscriptions vector 没有自己的 mutex

`subscribe()`：

```cpp
subscriptions.push_back(subs);
```

`unsubscribe()`：

```cpp
for (...) {
    ...
    subscriptions.erase(iter);
    delete subscription;
}
```

析构：

```cpp
for (...) {
    delete subscriptions[i];
}
```

这组 wrapper-local 操作没有额外 mutex。

因此即使底层：

```text
lcm_subscribe
lcm_unsubscribe
```

是 synchronized，也不能推出：

```text
concurrent C++ subscribe/unsubscribe/destructor
```

对 `subscriptions` vector 自动安全。

---

# 29. 跨线程 unsubscribe 的 adapter lifetime 更直接

假设：

```text
Thread H:
  lcm.handle()
  → cb_func
  → decoding / channel_buf / user handler

Thread C:
  lcm.unsubscribe(sub)
```

C core 在：

```text
callback_scheduled == 1
```

时不会 free C node。

但 C++ Thread C 会立即：

```text
delete sub
```

Thread H 的 trampoline 仍可能：

- 正在解码后准备写 `channel_buf`；
- 正在读取 `handler`；
- 正在使用 `channel_buf`；
- 正在调用 `std::function`；
- 正在从 callback 返回。

这是真正的跨线程 adapter lifetime 断裂。

---

# 30. C core 的 callback_scheduled 实际上已经给出了正确边界

C core 的意思是：

```text
只要 callback_scheduled == 1
subscription 物理内存就不能回收
```

语言绑定若把 userdata 生命周期和这个条件脱钩：

```text
C node still protected
userdata owner already deleted
```

就破坏了 core 原本试图维持的不变量。

---

# 31. 最自然的第一种修复：事件循环线程拥有订阅生命周期

LCM callback 本来就是：

```text
same thread that calls LCM::handle()
```

因此一个很自然的约束是：

> subscribe / unsubscribe / wrapper reclamation 统一交给 handle thread。

其他线程不直接：

```text
lcm.unsubscribe(sub)
```

而是提交：

```text
UnsubscribeCommand{sub}
```

给 event loop。

---

# 32. 同线程 self-unsubscribe 也不能立即 delete adapter

即使所有控制操作都在 handle thread：

```text
user callback
→ unsubscribe(self)
```

仍处于当前 trampoline 内。

所以 wrapper 应当：

```text
logical retire now
physical delete after current handle returns
```

而不是：

```text
delete immediately
```

---

# 33. 一个简单的 wrapper retired list

概念结构：

```text
active subscriptions
    vector<Subscription*>

retired adapters
    vector<Subscription*>
```

self-unsubscribe：

```text
lcm_unsubscribe(c_subs)
remove from active vector
append adapter to retired
return to callback
```

等最外层：

```text
LCM::handle()
```

返回后：

```text
delete all retired adapters
```

---

# 34. 为什么 handle 返回是一个自然 grace-period 边界

对于当前 C core：

```text
lcm_handle
→ provider handle
→ lcm_dispatch_handlers
→ all scheduled callbacks complete
→ deferred C nodes reclaimed
→ return
```

如果 C++ binding 把 adapter reclamation 放在：

```text
lcm_handle return
```

之后，那么：

```text
C node grace period
C++ userdata grace period
```

就重新对齐。

这比自己猜测 callback 是否完成稳健得多。

---

# 35. 但外部线程不能直接操作 retired vector

如果支持其他线程请求 unsubscribe，可以：

```text
control thread
    |
    v
MPSC command queue
    |
    v
handle thread
    |
    v
apply unsubscribe
```

这样：

- C++ `subscriptions` vector；
- retired list；
- adapter delete；

全部变成单 owner 状态。

这是一种 actor / event-loop ownership。

---

# 36. 这种设计的成本是什么

优点：

```text
no wrapper mutex around vector
no cross-thread adapter delete
self-unsubscribe naturally deferred
C/C++ grace period aligned
```

代价：

```text
unsubscribe from other thread
becomes asynchronous command
```

所以 API 需要明确：

```text
request submitted
≠
fully quiescent
```

如果控制线程需要等待，可以让 command 返回：

```text
future / completion event
```

在 handle thread 完成 reclamation 后 signal。

---

# 37. 第二种修复：让 C core 拥有 userdata 的 deferred destructor

更直接的跨语言设计是让 registration 同时保存：

```text
userdata
userdata_destroy
```

概念上：

```c
struct subscription {
    void *userdata;
    void (*destroy_userdata)(void *);
};
```

当：

```text
lcm_handler_free(subscription)
```

真正进入 grace-period 末端时，再调用：

```text
destroy_userdata(userdata)
```

---

# 38. 这样 adapter lifetime 自动继承 C core grace period

C++ binding 注册：

```text
userdata = new CallbackState
destroy_userdata = C++ deleter trampoline
```

unsubscribe 只做：

```text
retire C subscription
```

不直接：

```text
delete userdata
```

最终：

```text
callback_scheduled == 0
→ core removes node
→ core invokes userdata deleter
```

就能保证：

```text
userdata outlives all scheduled callbacks
```

---

# 39. 这和 shared_ptr 的本质区别

有人可能想到：

```text
把 adapter 改成 shared_ptr
```

但 C API 保存的是：

```text
void*
```

不是：

```text
shared_ptr<Adapter>
```

如果只是：

```text
userdata = adapter.get()
```

控制线程 reset 最后一份 shared_ptr：

```text
raw userdata
```

照样悬空。

必须有一个持有 shared ownership 的实体，其 lifetime 覆盖 C core scheduled window。

---

# 40. 一个 heap holder 也必须由 core grace period 回收

例如：

```text
userdata
  -> CallbackHolder
       -> shared_ptr<CallbackState>
```

如果 wrapper 自己提前：

```text
delete CallbackHolder
```

仍然没解决问题。

真正关键的不是：

```text
有没有 shared_ptr
```

而是：

```text
谁保证最后一个 owner
晚于最后一个可能 callback
```

这就是 quiescence。

---

# 41. 第三种修复：显式 registration token

更现代的 API 可以把：

```text
Subscription*
```

拆成：

```text
RegistrationHandle
CallbackState
CoreRegistration
```

其中 public handle 可以先失效：

```text
handle.disable()
```

但 callback state 保持到 core grace period 完成。

这样：

```text
API handle lifetime
```

不再等于：

```text
callback storage lifetime
```

---

# 42. 为什么 public handle 和 callback state 应该分离

当前 C++ wrapper 把很多职责装进同一个 adapter：

```text
public Subscription handle
C userdata target
channel workspace
handler storage
callback context
```

于是：

```text
unsubscribe destroys public handle
```

会顺便摧毁：

```text
callback 正在借用的内部状态
```

如果拆开：

```text
PublicHandle
   |
   v
RegistrationControl

CallbackState
   ^
   |
CoreRegistration userdata
```

public handle 可以立即变无效，但 callback state 延迟回收。

---

# 43. self-unsubscribe 的正确语义通常不是“立刻销毁一切”

更合理的是：

```text
unsubscribe linearization point:
  no new logical delivery
  from this registration

grace-period completion:
  no old callback can still run

reclaim:
  destroy callback state
```

这三个时刻不必相同。

当前 C core 已经区分前两个。

C++ binding 应继续保持这个区分。

---

# 44. 与 eCAL 的问题正好相反

[eCAL Callback 重入与 Quiescence](../ecal/callback-reentrancy-quiescence.md) 中的问题是：

```text
callback 在 mutex 内执行
```

所以：

```text
self-remove
→ re-lock same mutex
→ deadlock
```

LCM C core 选择：

```text
unlock before callback
+
deferred delete
```

解决了重入删除。

但 C++ wrapper 又提前 delete userdata owner。

因此两者形成非常好的对照：

```text
eCAL:
  execution lock too long

LCM C++:
  reclamation lifetime too short
```

正确 Runtime 需要同时做到：

```text
user code outside internal lock
+
state outlives all in-flight callbacks
```

---

# 45. “锁外 callback”不是完整正确性证明

LCM C core 很值得学习的一点：

```text
g_rec_mutex_unlock
→ user handler
→ g_rec_mutex_lock
```

这避免：

- callback WCET 污染核心 mutex；
- self-unsubscribe 因同锁死锁；
- callback publish 等重入路径被锁住。

但一旦锁外执行：

```text
对象回收协议
```

就必须更严格。

否则只是把问题从：

```text
deadlock
```

变成：

```text
use-after-free
```

---

# 46. deferred delete 的两个组成部分

真正的 deferred delete 至少需要：

```text
Retirement
  future lookup / execution
  no longer admits object

Grace period
  previously admitted readers
  all leave
```

然后才：

```text
Reclamation
```

LCM C core：

```text
marked_for_deletion
callback_scheduled
lcm_handler_free
```

刚好分别对应这三层。

---

# 47. C++ wrapper 当前只做了 retirement + immediate reclamation

`LCM::unsubscribe()`：

```text
call C retirement
erase wrapper registry
delete adapter
```

它缺少：

```text
wait / inherit C grace period
```

所以公共控制面与 callback 数据面的生命周期没有闭合。

---

# 48. callback_scheduled 不是普通 bool，而是 reader-presence 证明

从概念上，它表示：

```text
当前 frozen handler set
仍然可能访问这个 subscription
```

这比：

```text
callback 当前是否已经进入用户函数
```

更强。

因此它很像：

- epoch 中的 reader presence；
- RCU read-side critical section；
- in-flight request count；
- hazard publication。

只是实现更简单，专门服务单次 dispatch。

---

# 49. marked_for_deletion 是 retirement bit

一旦置位：

```text
当前分发尚未进入的 callback
会被跳过
```

且后续 authoritative containers 最终删除。

所以它表达：

```text
logically dead
but physically resident
```

这是并发回收里极其常见的一种状态。

---

# 50. 为什么不能 callback 返回前释放 channel_buf

因为当前 callback 参数：

```text
const std::string&
```

没有 ownership。

它只是一个 view。

view 正确性的基本不变量：

```text
owner lifetime
>
view lifetime
```

adapter delete 让这个不等式被破坏。

这和：

- `std::string_view`；
- span；
- protobuf arena view；
- SHM loan；
- tensor view；

完全是同一类问题。

---

# 51. ReceiveBuffer 还有另一条独立借用边界

`ReceiveBuffer rb` 自己在栈上，但：

```text
rb.data
```

借用 provider 当前 payload。

因此 callback 内如果把：

```cpp
rbuf->data
```

保存到异步线程，callback 返回后同样失效。

所以一只 LCM callback 同时持有至少两类 borrowed view：

```text
channel
  borrowed from C++ adapter

payload
  borrowed from provider buffer
```

两者 owner 完全不同。

---

# 52. 不能用一个 shared_ptr 把所有 lifetime 问题都抹平

即使把 C++ adapter 变成 shared_ptr：

```text
channel owner
```

变安全了，也不代表：

```text
provider payload owner
```

自动延长。

每个 view 都必须追到自己的 owner。

这就是源码级 lifetime analysis 必须逐字段做的原因。

---

# 53. 用户 Handler 又是第四个 lifetime

成员函数 adapter 只保存：

```cpp
MessageHandlerClass *handler;
```

它不拥有 Handler。

所以还需要：

```text
Handler lifetime
>
all possible callbacks
```

即使 adapter 本身安全延迟回收，如果应用提前：

```text
delete handler
```

仍然会 UAF。

---

# 54. 因此完整 lifetime 图有四层

```text
provider payload
    |
    | borrowed by ReceiveBuffer::data
    v
callback frame

C++ CallbackState / adapter
    |
    | owns channel_buf / callable metadata
    v
callback arguments

application Handler
    ^
    | raw pointer from adapter
    |
adapter

C subscription
    |
    | stores userdata raw pointer
    v
adapter / callback state
```

每条箭头都有不同 ownership 规则。

---

# 55. 安全 shutdown 必须按依赖方向倒序回收

如果依赖：

```text
core registration
→ adapter
→ Handler
```

关闭不能：

```text
delete Handler
→ delete adapter
→ unsubscribe
```

而应当：

```text
stop new delivery
→ wait / pass grace period
→ destroy callback state
→ destroy borrowed Handler
```

payload 则由每个 callback return 自动结束借用。

---

# 56. LCM::~LCM 也能看到相同层级问题

固定 C++ 析构：

```cpp
for (...) {
    delete subscriptions[i];
}

if (lcm && owns_lcm) {
    lcm_destroy(lcm);
}
```

也就是先删：

```text
C++ adapters
```

再销毁：

```text
C core
```

如果已经确保没有任何并发 `handle()`，这可以成立。

如果仍有 callback / handle 活跃，就再次出现：

```text
core may still reference userdata
wrapper already gone
```

因此 destructor 正确性依赖一个更高层 shutdown 前提：

> 没有并发 callback 仍在使用 binding state。

---

# 57. “C API thread-safe” 不等于 destroy 与任意活跃操作都有业务语义

线程安全通常只说明：

```text
内部共享数据不会无同步并发破坏
```

它不自动回答：

```text
destroy 是否会等待
另一个线程所有 callback 完成
```

也不回答：

```text
C++ adapter 是否参与同一同步协议
```

shutdown 必须单独定义 quiescence。

---

# 58. 一个更好的跨语言 subscription contract

可以显式定义：

## ACTIVE

```text
new messages may acquire callback
```

## RETIRED

```text
no new logical delivery
old scheduled invocation may remain
```

## QUIESCENT

```text
no scheduled / active invocation remains
```

## RECLAIMED

```text
callback state memory destroyed
```

然后要求：

```text
ACTIVE
 -> RETIRED
 -> QUIESCENT
 -> RECLAIMED
```

不能跳级。

---

# 59. C core 已经实现了 ACTIVE → RETIRED → QUIESCENT/RECLAIMED

当前 core 的字段：

```text
marked_for_deletion
callback_scheduled
```

足够表达单次 dispatch 中的核心 transition。

真正缺少的是 binding 层：

```text
adapter RETIRED
adapter QUIESCENT
adapter RECLAIMED
```

必须和 core transition 对齐。

---

# 60. 最小安全方案一：所有 subscription control 都回到 handle thread

这最符合 LCM 当前执行模型。

线程划分：

```text
network/provider thread
   -> enqueue payload

handle thread
   -> lcm_handle
   -> callback
   -> subscribe/unsubscribe control

other threads
   -> publish
   -> post control commands
```

这样 subscription 生命周期变成：

```text
single owner
```

---

# 61. self-unsubscribe 在 handle thread 内只做 retire

callback：

```text
request_unsubscribe(self)
```

handle thread：

```text
call lcm_unsubscribe(core)
remove public handle visibility
append adapter to retired list
```

然后 callback 继续安全使用：

```text
channel
handler-local data
```

直到返回。

---

# 62. handle() 返回后统一 reclaim wrapper

概念：

```text
LCM::handle()
{
    rc = lcm_handle(core);

    ReclaimRetiredAdapters();

    return rc;
}
```

这时 C core 已经完成当前：

```text
lcm_dispatch_handlers
```

所以 adapter 不再被当前 callback 集合使用。

这是把 binding 回收点直接绑定到底层 grace-period 边界。

---

# 63. 如果一个 handle() 内 provider 可能做多个 dispatch，要以真正外层边界为准

不能机械认为：

```text
某个 callback 返回
```

就是所有 C scheduled reader 都离开。

需要根据 provider 的 `handle()` contract 确认：

```text
lcm_handle return
```

是否覆盖当前 dispatch 生命周期。

固定实现的 core callback deferred free 在 `lcm_dispatch_handlers()` 末尾完成，因此把 wrapper delete 放到 `lcm_handle()` 返回后是保守边界。

---

# 64. 最小安全方案二：core 拥有 callback-state deleter

如果要允许跨线程同步 unsubscribe，最好让 core 的 retirement protocol 自己持有 userdata lifetime。

概念扩展：

```text
subscribe(
  callback,
  userdata,
  userdata_deleter)
```

C core 最终真正 free subscription 时：

```text
userdata_deleter(userdata)
```

这样不需要 binding 猜 grace period。

---

# 65. 为什么这是 FFI 设计中很常见的模式

很多 C ABI 都采用：

```text
void* context
+
destroy_context(context)
```

因为 C 本身不知道：

- C++ destructor；
- Rust Arc；
- Python refcount；
- Swift object；
- JVM global ref。

如果 callback registration 允许异步/延迟执行，就必须同时定义 context 的回收时刻。

只提供：

```text
callback + void*
```

而没有 destruction protocol，ownership 就会被推给调用者。

---

# 66. Rust / C++ / Python binding 都会遇到同一个问题

假设 C core 异步持有：

```text
void* user
```

不同语言都需要：

```text
register
retire
grace period
destroy language object
```

否则：

- C++：dangling object；
- Rust：drop 后 raw pointer；
- Python：Py_DECREF 过早；
- JVM：GlobalRef delete 过早。

LCM 这段源码非常适合用来理解 FFI callback lifetime。

---

# 67. 一个 RAII SubscriptionHandle 应该只拥有“控制权”，不必拥有“立即回收权”

理想 public handle：

```text
~SubscriptionHandle()
    |
    v
request retirement
```

不一定立刻：

```text
delete CallbackState
```

CallbackState 可以由：

- runtime；
- shared control block；
- retired queue；
- core deleter；

继续持有，直到 quiescent。

这让 RAII 与 deferred reclamation 并不冲突。

---

# 68. RAII 真正保证的是触发协议，不是必须同步 free

很多人把 RAII 理解成：

```text
destructor
= immediate resource destruction
```

更准确的是：

```text
scope exit
= deterministic release action
```

这个 release action 可以是：

```text
retire registration
schedule deferred close
decrement reference
signal shutdown
```

物理 memory free 可以晚于 destructor。

---

# 69. 为什么 vector<Subscription*> 本身不是问题核心

当前 vector 保存的是：

```text
heap adapter pointer
```

vector 扩容只移动 pointer slot，不移动 adapter。

所以：

```text
userdata = adapter address
```

在普通 push_back 下保持稳定。

真正的问题是：

```text
erase + delete
```

发生得太早，而不是 vector relocation。

---

# 70. 但 vector 仍然需要 owner-thread 或 mutex

如果支持：

```text
Thread A subscribe
Thread B unsubscribe
Thread C destructor
```

wrapper-local vector 就需要同步。

一种简单方案：

```text
subscription control confined
to handle thread
```

比在 callback/teardown 周围层层加锁更容易证明。

---

# 71. handle_mutex 不能保护 wrapper vector

C core 的：

```text
handle_mutex
```

只限制：

```text
one thread inside lcm_handle
```

它不会自动包住：

```text
LCM::subscribe
LCM::unsubscribe
LCM::~LCM
subscriptions vector
```

所以不能从：

```text
handle is serialized
```

推出：

```text
all C++ lifecycle operations serialized
```

---

# 72. lcm->mutex 也不能保护已经 delete 的 C++ state

C callback 调用前会：

```text
unlock lcm->mutex
```

这是正确的 user-code boundary。

但是：

```text
lcm->mutex
```

只保护 core structs。

当 callback 在锁外运行时，binding state 必须靠独立的 ownership protocol 存活。

这正是跨层并发设计的基本规则：

> mutex boundary 与 ownership boundary 必须配套。

---

# 73. 一条安全的 callback invocation 应满足什么

进入 trampoline 时需要保证：

```text
1. userdata address valid
2. callback metadata valid
3. channel storage valid
4. user Handler valid
5. payload view valid
```

执行完后才允许：

```text
1-4 对应 owner reclaim
```

其中 payload 通常由 provider 在 callback return 后自行释放。

---

# 74. unsubscribe 的线性化点应该定义清楚

至少有两个合理语义：

## weak unsubscribe

```text
return =>
future messages no longer
start new callback
```

但旧 callback 允许继续。

## strong unsubscribe

```text
return =>
no old or new callback
can still access callback state
```

后者必须等待 quiescence。

当前 C core 对 active callback 内自取消显然不能同步等待自己，所以内部采用 deferred reclaim。

---

# 75. self-unsubscribe 天然不能是同步 strong drain

如果当前 callback 自己要求：

```text
unsubscribe
and wait until
all callbacks returned
```

其中包含：

```text
当前 callback
```

就形成：

```text
callback waits for itself
```

因此 self-safe API 必须允许：

```text
retire now
reclaim later
```

这不是 LCM 特例，而是所有 callback Runtime 的通用约束。

---

# 76. 外部线程想要 strong unsubscribe，可以等待完成事件

一种 API：

```text
future = request_unsubscribe(handle)

future ready:
  registration retired
  grace period passed
  callback state reclaimable
```

self-callback 只发 request，不等待 future。

控制线程可以等待。

这样：

```text
self-remove safety
```

和：

```text
external strong quiescence
```

同时成立。

---

# 77. generation 可以处理快速 unsubscribe + resubscribe

如果同 channel 很快：

```text
A retire
B register
```

旧 callback 晚结束时，不应误影响 B。

可为 callback state 增加：

```text
generation
```

例如：

```text
ARM_STATE #17 retired
ARM_STATE #18 active
```

completion 只回收自己的 generation。

---

# 78. 为什么 “channel 字符串复制到局部变量”只能修一个症状

self-unsubscribe 前先：

```cpp
std::string stable_channel = channel;
```

确实能避免后续访问 dangling `channel_buf`。

但它不能修复：

- lambda std::function owner 自销毁；
- 跨线程 trampoline userdata UAF；
- subscriptions vector 并发；
- Handler raw pointer lifetime；
- destructor 与 handle 竞态。

所以这只能是应用侧局部规避，不是 binding-level 解法。

---

# 79. “callback 最后一行 unsubscribe”也不是完整协议

如果业务规定：

```text
unsubscribe 必须是 callback 最后一行
```

成员函数 adapter 在某些实现上可能看起来工作。

但：

- lambda adapter 仍有 callable self-destruction；
- 跨线程 unsubscribe 仍然存在；
- API contract 没表达该限制；
- 后续维护者很容易在 unsubscribe 后加日志。

正确性不应依赖这种脆弱约定。

---

# 80. 一个 Runtime 最好让错误用法在类型/协议层难以出现

比起：

```text
“请记住 unsubscribe 后不要碰 channel”
```

更好的 API 是：

```text
self callback can only request retirement

physical reclamation owned by runtime
```

这样生命周期正确性由框架承担，而不是靠调用者记忆一条隐藏规则。

---

# 81. 可迁移到机器人软件的场景

在机器人系统中，动态取消 callback 很常见：

- 传感器热插拔；
- 相机模式切换；
- 控制器 state transition；
- emergency-stop；
- 任务 graph 重配置；
- 设备掉线；
- lifecycle node teardown。

这些场景常常发生在 callback 自己检测到状态变化时。

因此：

```text
self-retirement
```

不是边缘用法，而是 Runtime 应主动设计的路径。

---

# 82. 对实时线程尤其要区分 retire 与 reclaim

控制 callback 中：

```text
delete adapter
```

可能触发：

- `std::string` free；
- `std::function` destructor；
- allocator；
- user capture destructor。

即使生命周期安全，也可能不适合实时路径。

deferred reclamation 还提供一个额外收益：

```text
把复杂 destructor
移到非实时安全点
```

所以 grace period 同时服务：

```text
correctness
+
latency control
```

---

# 83. 一个更适合实时系统的回收流程

实时 callback：

```text
set retired flag
enqueue reclaim token
return
```

非实时 control thread：

```text
wait quiescence
destroy callback state
release heap objects
```

这样 callback 热路径不承担：

```text
allocator / destructor tail latency
```

---

# 84. C core 当前 deferred delete 本身就有这种味道

当前 callback 内：

```text
lcm_unsubscribe
```

只：

```text
mark flag
```

真正：

```text
g_ptr_array_remove
regex_unref
free(channel)
free(subscription)
```

放到 dispatch 尾部。

虽然仍在 handle thread，但至少避免在用户 callback 调用栈中改变遍历结构。

语言 binding 可以进一步把复杂 C++ destructor 放到更明确的 reclaim phase。

---

# 85. 与 RCU 的相似和不同

相似：

```text
logical removal
+
read-side grace period
+
physical reclaim
```

不同：

```text
LCM 只需要覆盖
一轮显式 dispatch
```

不需要通用 epoch / per-CPU reader tracking。

所以它是一个非常轻量的 domain-specific reclamation protocol。

---

# 86. 与 hazard pointer 的相似和不同

hazard pointer：

```text
reader 显式发布
“我正在使用这个对象”
```

LCM 的：

```text
callback_scheduled = 1
```

也在表达：

```text
当前 dispatch 仍可能访问它
```

但 LCM 是集中式批量标记，而不是 lock-free reader 自己发布 hazard slot。

理解这些相似性有助于把中间件源码与通用并发算法联系起来。

---

# 87. 与引用计数的区别

refcount 可以回答：

```text
还有多少 owner
```

但如果 C core 只保存裸 `void*`，它没有自动：

```text
ref++
```

因此单纯把业务层改成 shared_ptr 并不能让 core 参与引用计数。

引用计数必须真正跨过 FFI 边界。

---

# 88. 与 mutex 的区别

mutex 可以阻止：

```text
同时修改结构
```

但 quiescence 需要回答：

```text
过去已经取得执行资格的 reader
是否全部退出
```

所以：

```text
mutex protected unsubscribe
```

不自动等于：

```text
safe reclaim
```

LCM C core 正是用状态机而不是一把长锁解决 callback reclaim。

---

# 89. 一个跨语言 binding 的四个必答问题

任何：

```text
C callback + void* userdata
```

设计都必须回答：

1. 谁拥有 userdata？
2. callback 什么时候可能开始？
3. unregister 返回时 callback 是否可能仍运行？
4. userdata 最早什么时候可以 destroy？

如果第 4 问的答案只是：

```text
“unsubscribe 之后应该可以吧”
```

协议还没有完成。

---

# 90. 对固定 LCM 实现的事实边界

可以确定：

```text
C subscription:
  callback_scheduled protects
  current dispatch lifetime

C unsubscribe:
  self / scheduled removal
  is deferred

C++ Subscription:
  heap allocated
  stored as raw pointer
  in subscriptions vector

C++ unsubscribe:
  calls C unsubscribe
  then immediately erases vector entry
  and deletes adapter

typed callback channel:
  reference aliases adapter.channel_buf

member handler:
  raw non-owning pointer

lambda handler:
  std::function member inside adapter
```

这些已经足够推出 binding lifetime gap。

---

# 91. 不需要把问题扩大成“LCM 所有多线程调用都不安全”

C API 明确声明 internally synchronized。

本文指出的是更窄、也更准确的边界：

> C core 的同步与 deferred reclamation，不会自动覆盖 C++ wrapper 新增的 vector、adapter、channel workspace、callable 与业务 Handler 生命周期。

不要把语言绑定层的问题错误泛化成整个 C core 的 thread-safety 结论。

---

# 92. 一个安全 binding 的推荐不变量

可以写成：

```text
Invariant A:
If C core may call userdata,
CallbackState must be alive.

Invariant B:
If user callback receives a view,
its owner must outlive callback use.

Invariant C:
Retired registration cannot admit
new logical callbacks.

Invariant D:
CallbackState is reclaimed only
after core grace period.

Invariant E:
Application Handler outlives
all callback invocations.
```

只要这五条可以逐行映射到代码，生命周期就比较容易证明。

---

# 93. 推荐的 Runtime 结构

```text
Public SubscriptionHandle
        |
        v
RegistrationControl
        |
        +---- ACTIVE / RETIRED
        |
        v
Core lcm_subscription_t
        |
        | userdata
        v
CallbackState
        |
        +---- channel workspace
        +---- callable metadata
        +---- app Handler reference
```

回收顺序：

```text
retire registration
→ pass core grace period
→ destroy CallbackState
→ invalidate public control storage
```

public handle 可以更早标记 invalid，但不能提前销毁 callback state。

---

# 94. 如果不改上游，应用层最稳健的约束

在固定 C++ wrapper 下，最保守的使用模型是：

```text
1. 一个线程拥有 handle()/subscription control
2. callback 内不直接销毁当前 C++ Subscription adapter
3. self-unsubscribe 通过 deferred command
4. 其他线程不直接并发 mutate C++ subscriptions vector
5. shutdown 先停止 handle loop
6. 确认 callback 退出
7. 再销毁 Subscription / Handler / LCM
```

这是对现有实现的安全使用约束，不是上游已经提供的自动保证。

---

# 95. 为什么 deferred command 比“加锁”更符合这里的模型

如果给 C++ vector 加一个 mutex：

```text
callback thread
holds no vector lock
control thread locks vector
delete adapter
```

adapter lifetime 仍然断裂。

如果 callback thread也长期拿 mutex：

```text
callback
→ self unsubscribe
→ same mutex
```

又可能回到 deadlock。

真正需要的是：

```text
single-owner mutation
+
deferred reclamation
```

而不是“再加一把大锁”。

---

# 96. 这篇源码最值得迁移的设计原则

第一条：

> **跨语言 callback registration 必须让 userdata lifetime 覆盖底层 Runtime 的完整 scheduled window，而不仅是用户函数正文。**

第二条：

> **logical unsubscribe 与 physical delete 必须分离。**

第三条：

> **语言 binding 新增的对象，不会自动继承 core 的 mutex、refcount 或 grace period。**

第四条：

> **self-unsubscribe 是生命周期协议的常规路径，不应靠“callback 最后一行再调用”之类约定维持。**

第五条：

> **borrowed reference 的安全性由 owner lifetime 决定；channel、payload、Handler 必须分别追 owner。**

---

# 97. 最后压成一条完整时间线

安全模型应该像：

```text
message admitted
    |
    v
C core freezes handler set
callback_scheduled = 1
    |
    v
C++ CallbackState guaranteed alive
    |
    v
trampoline enters
    |
    v
user callback
    |
    | request unsubscribe
    v
registration becomes RETIRED
CallbackState stays alive
    |
    v
trampoline returns
    |
    v
C core clears scheduled
and ends grace period
    |
    v
C++ runtime reclaims adapter/state
```

而不是：

```text
user callback
    |
    v
unsubscribe
    |
    +-- C core: defer C node
    |
    +-- C++ wrapper: delete userdata now
```

后者正是抽象层生命周期没有闭合的表现。

---

# 98. 结论

LCM C core 的订阅删除协议本身很值得学习：

```text
freeze iteration
→ callback outside lock
→ logical retire
→ grace period
→ deferred reclaim
```

它避免了“持内部锁运行任意用户代码”，也避免了 callback 内修改 handler 集合造成 iterator / pointer 失效。

真正需要继续追的是 C++ binding：

```text
C core protects lcm_subscription_t
```

并不意味着：

```text
C++ adapter
channel_buf
std::function
user Handler
```

也自动得到同样的 quiescence。

如果只记住一句：

> **C core 的 deferred delete 只有在 userdata owner 也延迟到同一个 grace period 之后回收时，才算真正跨语言闭环。**

这条原则可以直接迁移到任何 C ABI + C++/Rust/Python binding、机器人中间件 callback、驱动回调和异步 Runtime 的设计中。
