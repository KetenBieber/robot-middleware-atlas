# Sharded 与 foreign_ptr：Owner-Shard、跨核调用与析构执行域

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Seastar 的 shard-per-core 很容易被一句话概括成：

~~~text
每个 CPU 一个 Reactor
每个 Reactor 一份状态
~~~

但真正决定代码能不能写对的不是“有几份状态”，而是：

> **每一个 mutable object 到底由哪个 shard 直接访问、在哪个 shard 构造、在哪个 shard 调用、又必须在哪个 shard 销毁。**

`sharded<Service>` 与 `foreign_ptr<Ptr>` 恰好从两个方向回答这个问题：

~~~text
sharded<Service>
→ 一个逻辑 Service
→ 每个 shard 一份 local instance
→ computation 移到 instance owner

foreign_ptr<Ptr>
→ 一个 pointer wrapper 可以移动到 foreign shard
→ object ownership domain 仍记住原 owner
→ destruction/reclaim 必须回 owner
~~~

所以它们不是两个孤立的工具类，而是一套一致的 ownership 哲学：

> **跨 shard 时优先移动 computation、handle 或 ownership token，不把任意 mutable object 当成普通共享内存对象。**

跨核 RPC 的 request/completion、credit、wakeup 与 final reclamation 已在 [SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure](smp-message-queue.md) 拆开；Future 如何承接这些 completion 并继续异步控制流，见 [Future / Continuation：状态迁移、Task 化 Continuation 与异步控制流](future-continuation-task.md)。

---

# 一、先建立两个不同问题

## 1. `sharded<Service>` 解决什么

假设有四个 shard：

~~~text
Shard 0 → Service 0
Shard 1 → Service 1
Shard 2 → Service 2
Shard 3 → Service 3
~~~

业务希望表达：

~~~text
“这是一个逻辑服务，
但每个 shard 都有自己的本地实例。”
~~~

这就是 `sharded<Service>`。

---

## 2. `foreign_ptr<Ptr>` 解决什么

另一个问题：

~~~text
对象最初在 Shard 2 创建
wrapper 后来被 move 到 Shard 0
~~~

Shard 0 可以持有这个 ownership handle，

但真正：

~~~text
refcount decrement
object destructor
allocator free
container unlink
~~~

可能仍必须回 Shard 2。

这就是 `foreign_ptr`。

---

# 二、为什么普通 C++ Pointer Model 不够

传统直觉：

~~~text
pointer value 能访问
→ 就能用
→ 就能析构
~~~

在 shard-per-core Runtime 里这个推理不成立。

因为 pointer 地址只回答：

~~~text
对象在哪里
~~~

却没有回答：

~~~text
谁有权修改它？
谁有权递减 refcount？
谁有权调用 destructor？
谁有权 free 这块 allocator memory？
~~~

---

# 三、Execution Ownership 是 Pointer Ownership 的一部分

更完整的 ownership 应写成：

~~~text
Memory Address
+
Logical Owner
+
Execution Domain
+
Reclamation Domain
~~~

Seastar 强调的是后两项。

---

# 四、为什么 Seastar `shared_ptr` 本身不能随便跨 CPU 用

源码 `shared_ptr.hh` 明确写：

~~~text
shared_ptr
lw_shared_ptr
都不是 thread-safe
~~~

原因之一：

~~~text
reference count
不是跨线程 atomic refcount
~~~

---

## 3. 为什么故意不用 Atomic Refcount

如果每一次：

~~~text
copy
destroy
~~~

都跨 CPU 原子更新同一 cache line，

高频路径会带来：

- cache-line bouncing；
- atomic RMW；
- inter-core coherence traffic。

Shard-per-core 的基本思路正相反：

~~~text
让 owner shard 自己维护 local refcount
~~~

---

# 五、问题不仅是 Refcount

即使你给 refcount 换成 atomic，

也不自动解决：

~~~text
destructor thread affinity
~~~

一个 destructor 可能：

- 从 owner-local intrusive list 删除；
- 修改 Reactor-local registry；
- free shard-local allocator；
- 注销 poller resource；
- 访问只能在 owner shard 操作的 subsystem。

---

# 六、因此“Atomic Shared Pointer”不是通用答案

需要区分：

~~~text
reference-count thread safety
~~~

和：

~~~text
object destruction-domain correctness
~~~

前者解决不了后者。

---

# 七、`sharded<Service>` 的真实存储结构

核心：

~~~cpp
struct entry {
    shared_ptr<Service> service;
};

std::vector<entry> _instances;
~~~

表面上像：

~~~text
vector<shared_ptr<Service>>
~~~

但真正重要的 invariant 是：

~~~text
_instances[i].service
必须由 shard i 的执行上下文构造/调用/销毁
~~~

---

# 八、Vector 存在在哪不等于 Service 可以在哪执行

`_instances` 是 container-level index。

它让 Runtime 能根据 shard id 找到：

~~~text
对应 instance handle
~~~

但业务逻辑并不是：

~~~cpp
_instances[remote].service->mutate();
~~~

---

# 九、跨 Shard 调用通过 `invoke_on()`

核心：

~~~cpp
return smp::submit_to(id, options,
    [this, func, args...] () mutable {
        auto inst = get_local_service();
        return func(*inst, ...);
    });
~~~

也就是：

~~~text
origin shard
    |
    | invoke_on(B, func)
    v
smp::submit_to(B)
    |
    v
Shard B
    |
    v
get_local_service()
    |
    v
func(Service&)
~~~

---

# 十、移动的是 Computation，不是 Mutable Pointer

应用表达：

~~~text
去 B 上执行 func
~~~

而不是：

~~~text
把 B 的 Service* 暴露给 A
然后 A 直接调用
~~~

这就是：

> **owner-computes。**

---

# 十一、为什么 `local()` 这么简单

~~~cpp
return *_instances[
    this_shard_id()].service;
~~~

因为 local path 已经满足：

~~~text
execution shard
==
object owner shard
~~~

不需要 RPC。

---

# 十二、`local()` 的简单建立在整个架构的强约束上

如果代码允许：

~~~text
任意 shard 直接解引用任意 _instances[i]
~~~

那么 `local()` 的轻量意义就消失了，

同时会重新引入：

- locking；
- atomic；
- ownership races。

---

# 十三、`start()` 为什么每个 Shard 都走 `submit_to`

源码：

~~~cpp
_instances.resize(smp::count);

sharded_parallel_for_each(... c ...) {
    return smp::submit_to(c, [this, args] {
        _instances[this_shard_id()].service =
            create_local_service(...);
    });
}
~~~

---

## 4. 为什么不能在 Shard 0 上一次性构造所有实例

因为 Service constructor 本身可能：

- 获取 shard-local allocator；
- 注册 local Reactor service；
- 创建 shard-local timer；
- 保存 `this_shard_id()`；
- 使用 local network stack。

---

# 十四、构造本身就是 Ownership Establishment

因此：

~~~text
construct on shard i
~~~

不仅是：

~~~text
执行位置
~~~

还在建立：

~~~text
owner = shard i
~~~

---

# 十五、`start()` 失败时为什么调用 `stop()`

源码：

~~~text
parallel start
→ some shard throws
→ then_wrapped catches
→ this->stop()
→ rethrow original exception
~~~

这意味着：

~~~text
start
~~~

不是简单 fan-out。

它是一个：

~~~text
distributed construction transaction
~~~

---

# 十六、部分构造必须 Rollback

假设：

~~~text
Shard 0 success
Shard 1 success
Shard 2 fail
Shard 3 maybe success
~~~

如果直接返回 error，

已经创建的实例会泄漏 logical lifecycle。

所以必须：

~~~text
stop all successfully-created instances
~~~

---

# 十七、这是 Best-effort Transactional Startup

并不意味着 ACID，

但拥有明确协议：

~~~text
partial success
→ cleanup
→ propagate original failure
~~~

---

# 十八、`start_single()` 是特殊 Deployment Topology

它只：

~~~text
_instances.resize(1)
submit_to(0)
construct Service on shard 0
~~~

这说明 `sharded<T>` 不只表示：

~~~text
严格 one-per-every-core
~~~

还可表达：

~~~text
container with explicitly limited shard topology
~~~

---

# 十九、`invoke_on_all()` 本质还是重复 `submit_to`

它没有创建：

~~~text
一个跨 CPU shared Service&
~~~

而是：

~~~text
for every shard c:
    submit_to(c)
    → get local Service&
    → invoke func
~~~

---

# 二十、Broadcast Call ≠ Shared Object Call

`invoke_on_all()` 看起来像：

~~~text
调用同一个对象
~~~

实际是：

~~~text
对 N 个不同 local instance
执行同一个 computation
~~~

---

# 二十一、这与 Actor Model 很接近

每个 shard instance：

~~~text
拥有自己的 mutable state
~~~

外部：

~~~text
发一个 computation/message
~~~

而不是：

~~~text
拿锁后直接修改远端内存
~~~

---

# 二十二、但 Seastar 不是纯 Actor Runtime

因为：

- local instance 可以用普通 C++ 引用；
- shard 内大量代码不是 mailbox actor；
-跨 shard transport 由 SMP queue 实现。

所以更准确：

~~~text
owner-shard execution model
~~~

---

# 二十三、`sharded_parameter` 解决参数“每 Shard 不同”的问题

如果 `invoke_on_all()` 参数只是普通值：

~~~text
同一份逻辑 argument
~~~

会被复制/移动到各 shard call。

但某些参数必须：

~~~text
在目标 shard 现场求值
~~~

---

# 二十四、为什么不能提前在 Origin 求值

例如你想传：

~~~text
target shard local service reference
~~~

origin 无法正确提前构造：

~~~text
Service& on future target
~~~

所以需要：

~~~text
sharded_parameter
~~~

让参数到 target 后再 unwrap。

---

# 二十五、`std::ref(sharded<T>)` 又有另一种语义

源码 `sharded_unwrap` 对：

~~~text
reference_wrapper<sharded<T>>
~~~

转换成：

~~~text
either_sharded_or_local<T>
~~~

目标函数可以按声明类型接：

~~~text
T&
~~~

或者：

~~~text
sharded<T>&
~~~

---

# 二十六、参数传递也必须尊重 Owner Context

核心原则：

> **跨 shard RPC 的参数不只是序列化/拷贝问题，还可能有“必须在目标 shard 解析”的 execution-local semantics。**

---

# 二十七、`stop()` 比 `start()` 更值得研究

源码大致：

~~~text
Phase 1:
on every shard:
    Service::stop()

Phase 2:
on every shard:
    track deletion
    local shared_ptr = nullptr

Phase 3:
wait tracked deletion
clear _instances
~~~

---

# 二十八、为什么 Stop 分成两阶段

第一阶段：

~~~text
告诉 service 停止业务
~~~

第二阶段：

~~~text
释放 container 持有的 ownership
~~~

不能混成：

~~~text
直接 delete object
~~~

---

# 二十九、Stop 与 Destruction 是两件事

`Service::stop()` 可以：

- stop admission；
- cancel timer；
- close socket；
- drain background task；
- wait child operation。

而 object destructor 是：

~~~text
最终 storage reclaim
~~~

---

# 三十、这是经典 Lifecycle 顺序

~~~text
quiesce behavior
→ release ownership
→ reclaim object
~~~

---

# 三十一、为什么 `stop()` 本身也在 Owner Shard 调用

源码：

~~~cpp
smp::submit_to(c, [this] {
    auto inst =
        _instances[this_shard_id()].service;
    return stop_sharded_instance(*inst);
});
~~~

所以：

~~~text
Service::stop()
~~~

和日常 mutation 一样，

也属于 owner-shard operation。

---

# 三十二、Lifecycle Method 不是例外

很多系统犯的错误是：

~~~text
平时 owner-thread
但 shutdown 从任意线程 delete
~~~

这会破坏同样的 affinity invariant。

---

# 三十三、`async_sharded_service` 为什么存在

有些 Service 的异步代码仍持有：

~~~text
shared_from_this()
~~~

即使 `sharded` container 把自己的 shared_ptr 清了，

object 也不会立即析构。

---

# 三十四、如果 `stop()` 只做 `service=nullptr`

可能发生：

~~~text
background continuation
still owns shared_ptr
~~~

然后：

~~~text
sharded::stop() returns
~~~

但 Service 对象还活着。

---

# 三十五、为什么这有时不够

调用者可能把：

~~~text
sharded.stop() ready
~~~

理解成：

~~~text
所有 Service instance 已经被真正销毁
~~~

如果还有内部 refs，

这个 contract 就不成立。

---

# 三十六、`async_sharded_service` 增加一个 Freed Completion

内部：

~~~cpp
promise<> _freed;

~async_sharded_service() {
    _freed.set_value();
}
~~~

以及：

~~~text
entry::track_deletion()
→ service->freed()
~~~

---

# 三十七、真正析构变成一个 Future Event

这样：

~~~text
drop container ref
~~~

之后可以：

~~~text
await object destructor actually happened
~~~

---

# 三十八、这是一种 Destruction Completion Primitive

普通 C++ destructor：

~~~text
没有 Future
~~~

但 async lifecycle 有时必须知道：

~~~text
最后一个 ref 何时归零
~~~

---

# 三十九、`stop()` 的 Phase 2 顺序非常关键

目标 shard 内：

~~~text
fut = track_deletion()
service = nullptr
return fut
~~~

先拿：

~~~text
freed future
~~~

再释放 container owner。

---

# 四十、为什么不能反过来

如果先：

~~~text
service = nullptr
~~~

而这就是 last ref，

destructor 立即执行。

之后再调用：

~~~text
service->freed()
~~~

已经没有对象了。

---

# 四十一、先建立 Completion Handle，再触发可能完成它的事件

这是很通用的异步规则：

~~~text
subscribe/wait handle
before
trigger
~~~

否则容易 lost completion。

---

# 四十二、这和 Lost Wakeup 同构

错误：

~~~text
trigger destruction
then register waiter
~~~

正确：

~~~text
register waiter
then release last owner
~~~

---

# 四十三、`peering_sharded_service` 为什么保存 Container 指针

Service 有时需要：

~~~text
从自己的 local instance
调用其他 shard peer
~~~

于是：

~~~cpp
sharded<Service>* _container;
~~~

提供：

~~~text
container()
~~~

---

# 四十四、为什么 `sharded<Service>` 禁止 Move

源码注释明确：

~~~text
如果 T 继承 peering_sharded_service
container pointer 指向当前 sharded object
~~~

如果 `sharded` 自己 move，

local Service 内部：

~~~text
_container
~~~

会变成旧地址。

---

# 四十五、这是 Self/Backpointer Invariant

结构：

~~~text
sharded container
    ↑
    |
local Service._container
~~~

一旦 object address 参与协议，

默认 move 就不再安全。

---

# 四十六、所以 `sharded` Copy/Move 都禁用

不是“作者懒得实现”。

而是：

~~~text
container identity
~~~

本身进入了 object graph。

---

# 四十七、这一点与 Future Move 相反

Future 为了允许 move，

源码显式修复：

~~~text
_promise/_future backpointer
~~~

而 `sharded` 选择：

~~~text
不允许 move
~~~

两者都是合理策略。

---

# 四十八、设计选择：修复所有 Backpointer，还是禁止 Move

如果 move 很常见：

~~~text
实现 move protocol
~~~

如果 object 本身就是长期 runtime anchor：

~~~text
直接 non-movable
~~~

通常更安全。

---

# 四十九、现在进入 `foreign_ptr`

定义：

~~~cpp
template <typename PtrType>
requires (!std::is_pointer_v<PtrType>)
class foreign_ptr
~~~

注意：

~~~text
不接受 raw pointer type
~~~

它包装的是：

- `shared_ptr<T>`；
- `lw_shared_ptr<T>`；
- `std::unique_ptr<T>`；
- 其他 pointer-like object。

---

# 五十、核心字段只有两个

~~~cpp
PtrType _value;
unsigned _cpu;
~~~

可以理解成：

~~~text
ownership handle
+
reclamation domain
~~~

---

# 五十一、构造时记录当前 Shard

~~~cpp
foreign_ptr(PtrType value)
    : _value(std::move(value))
    , _cpu(this_shard_id())
{}
~~~

因此：

~~~text
owner shard
~~~

不是从 pointer address 推导，

而是在 wrapper 建立时显式记录。

---

# 五十二、Owner Shard 是 Logical Metadata

`_cpu` 表达：

~~~text
wrapped pointer 的安全操作域
~~~

它跟随 wrapper move。

---

# 五十三、Wrapper Move 到别的 Shard 不会改变 `_cpu`

假设：

~~~text
Shard 2:
make_foreign(ptr)
→ _cpu = 2
~~~

wrapper 经 SMP move 到 Shard 0：

~~~text
foreign_ptr object now lives on 0
_cpu still 2
~~~

---

# 五十四、Physical Wrapper Location 与 Resource Owner 分离

这是整章最重要的区分：

~~~text
where handle lives
!=
where resource must die
~~~

---

# 五十五、为什么 Destructor 不能直接 `_value = {}`

如果 foreign_ptr 当前位于 Shard 0：

~~~text
_value last ref
owner shard = 2
~~~

直接在 Shard 0：

~~~text
_value={}
~~~

可能导致：

~~~text
real destructor executes on 0
~~~

违反 owner invariant。

---

# 五十六、Destructor 调 `destroy()`

~~~cpp
~foreign_ptr() {
    destroy(
      std::move(_value),
      _cpu);
}
~~~

---

# 五十七、`destroy()` 为什么又包一层 `destroy_on()`

`destroy_on()` 返回：

~~~text
future<>
~~~

因为跨 shard reclaim 可能异步。

但 C++ destructor：

~~~text
不能 co_await
不能 return future
~~~

所以同步 destructor 只能：

~~~text
发起 destruction operation
~~~

---

# 五十八、如果 Destruction Future 没立刻 Ready 怎么办

源码：

~~~cpp
auto f = destroy_on(...);

if (!f.available() || f.failed()) {
    internal::run_in_background(
        std::move(f));
}
~~~

---

# 五十九、Destructor 触发的是 Fire-and-accounted Async Cleanup

不是：

~~~text
阻塞直到 owner 真正 delete
~~~

也不是：

~~~text
丢掉 Future 不管
~~~

而是送到 Runtime 的：

~~~text
run_in_background
~~~

继续管理。

---

# 六十、这解决了 C++ Destructor 与 Async Reclaim 的矛盾

同步语法：

~~~text
~foreign_ptr()
~~~

底层现实：

~~~text
可能要跨核 RPC
~~~

只能通过：

~~~text
schedule cleanup obligation
~~~

桥接。

---

# 六十一、真正关键在 `destroy_on()`

如果：

~~~text
cpu == this_shard_id()
~~~

直接：

~~~cpp
p = {};
~~~

说明：

~~~text
已经在 owner
→ synchronous local reclaim
~~~

---

# 六十二、如果当前 Shard 不是 Owner

~~~cpp
return smp::submit_to(cpu,
    [v = std::move(p)] () mutable {
        v = {};
    });
~~~

---

# 六十三、为什么 Lambda 里必须显式 `v = {}`

源码注释非常关键：

~~~text
lambda is destroyed
in the shard that submitted the task
~~~

这很容易反直觉。

---

# 六十四、如果只写空 Lambda Body 会怎样

假设：

~~~cpp
smp::submit_to(owner,
    [v = std::move(p)] {});
~~~

虽然 callback body 在 owner shard 执行，

但跨 shard work item 最终会回 origin 做 completion/reclamation。

capture 的真实 destructor 未必发生在 owner。

---

# 六十五、所以必须在 Owner Execution 中主动清空

~~~cpp
[v = std::move(p)] () mutable {
    v = {};
}
~~~

强制：

~~~text
wrapped pointer destruction
~~~

发生在目标 shard body 内。

---

# 六十六、Callable Lifetime 与 Captured Resource Lifetime 不是同一件事

这是极其重要的 C++ Runtime 原则。

~~~text
lambda executes on shard B
~~~

不代表：

~~~text
lambda capture destructs on shard B
~~~

---

# 六十七、必须追完整 Work-item Lifetime

SMP 章节已经看到：

~~~text
origin creates work_item
target executes
completion returns origin
origin deletes work_item
~~~

因此 lambda object 作为 work item 一部分，

最终 storage reclamation 可能回 origin。

---

# 六十八、这就是为什么 `v={}` 是协议代码，不是多余代码

它在：

~~~text
target callback body
~~~

明确执行：

~~~text
resource release
~~~

而不是依赖：

~~~text
callable destructor side effect
~~~

---

# 六十九、可迁移到所有异步 Task Capture

如果 capture 包含：

- thread-affine handle；
- owner-local refcount；
- GPU-context object；
- event-loop-local resource；

不要只问：

~~~text
callback在哪运行？
~~~

还要问：

~~~text
callback object最终在哪销毁？
~~~

---

# 七十、`foreign_ptr` 为什么是 Move-only

源码直接删除：

~~~cpp
foreign_ptr(
  const foreign_ptr&) = delete;
~~~

原因不是：

~~~text
底层一定 unique
~~~

因为它也能包：

~~~text
shared_ptr
~~~

---

# 七十一、真正原因：Copy 可能是 Cross-shard Operation

如果 wrapper 当前在 Shard 0，

owner 在 Shard 2。

要复制一个非原子 `shared_ptr`：

~~~text
refcount++
~~~

必须在 Shard 2 做。

---

# 七十二、普通 Copy Constructor 没法表达 Future

C++ copy：

~~~cpp
foreign_ptr b = a;
~~~

看起来：

~~~text
同步、便宜、本地
~~~

但真实成本可能：

~~~text
cross-core message
+
scheduler
+
completion
~~~

---

# 七十三、API 因此禁止假装便宜

显式提供：

~~~cpp
future<foreign_ptr> copy() const;
~~~

这非常好。

---

# 七十四、`copy()` 怎么做

~~~cpp
return smp::submit_to(_cpu,
    [this] () mutable {
        auto v = _value;
        return make_foreign(
            std::move(v));
    });
~~~

---

# 七十五、真正底层 Pointer Copy 在 Owner Shard

`auto v = _value`：

~~~text
shared_ptr refcount++
~~~

发生于：

~~~text
_cpu owner
~~~

---

# 七十六、新 foreign_ptr 的 Owner 仍是那个 Shard

因为：

~~~text
make_foreign()
~~~

就在 `_cpu` 执行，

构造时：

~~~text
_cpu = this_shard_id()
~~~

仍然正确。

---

# 七十七、Cross-shard Cost 被 Type Signature 暴露

~~~text
copy()
→ future<foreign_ptr>
~~~

而不是：

~~~text
copy ctor
~~~

这是非常值得迁移的 API 原则：

> **如果一个“复制”动作本质需要 RPC，就让 API 长得像异步 RPC。**

---

# 七十八、`release()` 为什么危险

~~~cpp
PtrType release() {
    return exchange(_value,{});
}
~~~

wrapper 放弃 ownership protocol，

把原 pointer-like object 交给 caller。

---

# 七十九、源码警告什么

caller 现在必须：

~~~text
在 owner shard 销毁这个 pointer
~~~

否则 wrapper 提供的安全边界已经不存在。

---

# 八十、Raw/Plain Smart Pointer 本身不携带 Reclamation Domain

`release()` 后拿到：

~~~text
PtrType
~~~

但 `_cpu` 信息留在：

~~~text
foreign_ptr wrapper
~~~

里。

---

# 八十一、所以 release 是“协议逃生舱”

它不是普通 getter。

它表示：

> **我知道 owner-shard 约束，并愿意手工承担它。**

---

# 八十二、`get()` / `operator->` 又该怎么理解

API 允许：

~~~text
foreign_ptr->member
~~~

但这不意味着：

~~~text
任意 foreign shard
可以安全 mutate pointee
~~~

---

# 八十三、foreign_ptr 主要保证 Lifetime，不自动保证 Remote Access Correctness

它最核心保证的是：

~~~text
最终 pointer destruction
回到 owner shard
~~~

不是：

~~~text
自动把每个 member access RPC 到 owner
~~~

---

# 八十四、这点非常容易误解

`foreign_ptr` 不是：

~~~text
distributed shared_ptr proxy
~~~

它不会拦截：

~~~cpp
ptr->foo()
~~~

然后自动 `submit_to(owner)`。

---

# 八十五、因此远端操作仍要遵守 Owner-Computes

正确心智：

~~~text
foreign_ptr
→ can transport ownership safely
~~~

不等于：

~~~text
can transparently execute pointee methods remotely
~~~

---

# 八十六、`get_owner_shard()` 是重要 Escape Information

它让业务知道：

~~~text
这个 ownership 应该回哪里
~~~

例如：

~~~text
if owner == current
→ local fast path

else
→ submit_to(owner)
~~~

---

# 八十七、RPC 实际就有这种用法

`make_shard_local_buffer_copy()`：

~~~cpp
if (org.get_owner_shard()
    == this_shard_id()) {
    return std::move(*org);
}
~~~

Owner 已经是本地：

~~~text
直接 move resource
~~~

---

# 八十八、如果 Owner 在别的 Shard

源码没有复制 payload bytes，

而是构造新的 buffer view：

~~~text
same underlying memory
+
new deleter captures foreign_ptr
~~~

---

# 八十九、这是 Zero-copy Lifetime Bridging

新 shard 使用：

~~~text
本地 buffer facade
~~~

底层 data 仍属于 foreign owner。

最终 deleter 持有：

~~~text
foreign_ptr
~~~

确保原对象仍活着。

---

# 九十、最后一个 View 消失时发生什么

deleter 释放 foreign_ptr，

如果当前 shard != owner：

~~~text
destruction obligation
→ submit_to(owner)
~~~

于是：

~~~text
zero-copy sharing
~~~

与：

~~~text
owner-local reclaim
~~~

同时满足。

---

# 九十一、这里真正跨 Shard 的不是“对象可随便访问”

而是：

~~~text
data bytes remain valid
because ownership token survives
~~~

---

# 九十二、Data Lifetime 与 Mutable Ownership 再次分层

大 buffer 的 bytes：

~~~text
可被 remote reader view
~~~

但其：

~~~text
metadata owner / allocator reclaim
~~~

仍属于原 shard。

---

# 九十三、`reset(new_ptr)` 为什么会改变 Owner Shard

源码：

~~~text
old_ptr = move(_value)
old_cpu = _cpu

_value = new_ptr
_cpu = this_shard_id()

destroy(old_ptr, old_cpu)
~~~

---

# 九十四、一个 foreign_ptr Wrapper 可以换 Resource Domain

wrapper identity 不等于永久 owner identity。

每次：

~~~text
reset(new resource)
~~~

新资源属于：

~~~text
当前 shard
~~~

---

# 九十五、为什么先保存 old owner

因为 old resource：

~~~text
仍必须回 old_cpu 销毁
~~~

不能被新 `_cpu` 覆盖。

---

# 九十六、这是一种 Two-resource Transition

~~~text
old resource
→ retire on old owner

new resource
→ publish under current owner
~~~

---

# 九十七、Move Assignment 也是同样

~~~cpp
destroy(old_value, old_cpu);
_value = move(other._value);
_cpu = other._cpu;
~~~

先 retire 自己旧 obligation，

再接管 incoming ownership。

---

# 九十八、Move Assignment 不只是两个字段赋值

因为 target wrapper 可能已经持有：

~~~text
一个 foreign resource
~~~

它必须先正确退出旧协议。

---

# 九十九、`destroy()` 与 Destructor 不同

公开：

~~~cpp
future<> destroy()
~~~

可以让调用者明确等待：

~~~text
资源已在 owner shard 真正销毁
~~~

---

# 一百、为什么这个 API 很重要

Destructor：

~~~text
fire background cleanup
~~~

没有 completion handle。

而有时 caller 需要：

~~~text
shutdown barrier
~~~

必须知道：

~~~text
reclaim finished
~~~

---

# 一百零一、显式 `destroy()` 提供 Stronger Completion Semantics

~~~text
~foreign_ptr
→ initiate cleanup

destroy()
→ initiate + awaitable completion
~~~

---

# 一百零二、这是 Async RAII 的典型矛盾

RAII destructor 是同步语法，

但真正释放可能异步。

因此成熟设计通常同时提供：

~~~text
best-effort destructor cleanup
+
explicit async close/destroy
~~~

---

# 一百零三、这和网络 Connection Close 很像

~~~text
~Connection()
→ cannot naturally await drain

co_await connection.close()
→ strong completion
~~~

---

# 一百零四、`foreign_ptr` 不接受 Raw Pointer 的意义

约束：

~~~cpp
requires (!is_pointer_v<PtrType>)
~~~

说明 wrapper 想包装的是：

~~~text
有明确 ownership semantics 的 pointer object
~~~

而不是：

~~~text
裸地址
~~~

---

# 一百零五、Raw Pointer 没有 Destroy Protocol

一个 `T*` 无法单靠类型知道：

- delete？
- free？
- custom allocator？
- intrusive ref decrement？
- no ownership？

---

# 一百零六、因此先要求 Smart/Owned Pointer，再加 Foreign Domain

层次：

~~~text
PtrType
→ ownership semantics

foreign_ptr<PtrType>
→ cross-shard reclamation semantics
~~~

---

# 一百零七、这是一种 Protocol Composition

不是重新发明：

~~~text
shared ownership
unique ownership
~~~

而是在已有 pointer abstraction 外再叠：

~~~text
execution-domain ownership
~~~

---

# 一百零八、`foreign_ptr<unique_ptr<T>>`

表示：

~~~text
unique object ownership
+
owner shard affinity
~~~

---

# 一百零九、`foreign_ptr<shared_ptr<T>>`

表示：

~~~text
shared ownership participation
+
this refcount operation/reclaim
必须遵守 owner shard
~~~

---

# 一百一十、Move-only Foreign Wrapper 与底层 Shared Ownership 不矛盾

外层 move-only 约束的是：

~~~text
跨 shard ownership handle 的复制成本
~~~

不是说底层 object 只能有一个 reference。

---

# 一百一十一、这正是 `copy()` 存在的原因

底层如果可 copy：

~~~text
可以产生另一个 foreign ownership token
~~~

只是必须：

~~~text
在 owner shard 执行 copy
~~~

---

# 一百一十二、Owner Identity 为什么是 CPU/Sharded ID 而不是 Thread ID

Seastar 的执行模型把：

~~~text
logical shard
~~~

作为稳定 execution domain。

它比：

~~~text
native std::thread::id
~~~

更符合 Runtime 抽象。

---

# 一百一十三、Reactor Thread 是实现载体，Shard 才是 Ownership Domain

上层逻辑应该说：

~~~text
owned by shard 2
~~~

而不是：

~~~text
owned by pthread 0x1234
~~~

---

# 一百一十四、这使得 Runtime 可以统一 `submit_to(owner)`

Owner metadata 直接就是：

~~~text
routing key
~~~

---

# 一百一十五、Sharded 与 foreign_ptr 是两种不同方向的数据流

`sharded`：

~~~text
computation
→ owner object
~~~

`foreign_ptr`：

~~~text
ownership handle
→ foreign shard
→ reclaim message eventually returns owner
~~~

---

# 一百一十六、一个 Push Computation，一个 Pull Reclamation Back

可以画成：

~~~text
Origin A
   |
   | invoke_on(B)
   v
Owner B
   |
   | execute
   v
Service B
~~~

以及：

~~~text
Owner B creates resource
   |
   | move foreign_ptr
   v
Shard A holds handle
   |
   | destroy
   v
submit reclaim
   |
   v
Owner B
~~~

---

# 一百一十七、两者共同避免什么

避免：

~~~text
cross-core shared mutable object
~~~

变成系统默认。

---

# 一百一十八、Shard-local Allocator 为什么尤其需要这种设计

每个 shard allocator 往往：

~~~text
优化 local allocation/free
~~~

如果 foreign core 任意 free：

- allocator metadata 需要加锁；
- cross-core freelist 增多；
- cache locality 变差。

---

# 一百一十九、foreign_ptr 的策略是把复杂性显式放到边界

平时 local pointer：

~~~text
便宜
~~~

只有真正跨 shard 时：

~~~text
foreign_ptr
~~~

承担额外协议。

---

# 一百二十、不要把所有 Pointer 都改成 Thread-safe Shared Pointer

那会把：

~~~text
rare cross-shard case cost
~~~

扩散到：

~~~text
every local reference
~~~

Seastar 选择相反：

> **Local fast path 默认廉价；cross-shard path 显式付费。**

---

# 一百二十一、这是 Shard-per-core 的核心经济学

~~~text
optimize the common local case
make remote ownership explicit
~~~

---

# 一百二十二、`invoke_on()` 的返回值为什么仍是 Future

远端 computation：

~~~text
可能异步完成
~~~

而 SMP transport 又需要：

~~~text
completion 回 origin
~~~

所以 caller 拿：

~~~text
Future<R>
~~~

正好统一：

- local fast path；
- remote path；
- exception；
- async result。

---

# 一百二十三、调用位置与结果消费位置可以不同

target B：

~~~text
execute Service method
~~~

origin A：

~~~text
consume Future completion
~~~

---

# 一百二十四、这与 foreign_ptr 的销毁路径镜像

foreign_ptr：

~~~text
current A
→ owner B reclaim
→ completion optionally back A
~~~

---

# 一百二十五、SMP Queue 是所有这些高层 API 的共同 Transport

`invoke_on`、`foreign_ptr::copy()`、`destroy_on()`：

~~~text
都依赖 submit_to
~~~

因此：

~~~text
service-group credit
SPSC request/completion
target scheduling
origin completion
~~~

并不是只服务业务 RPC，

也服务 Runtime 自己的 lifetime protocol。

---

# 一百二十六、Runtime Control Traffic 与 Business RPC 共用机制

这是很常见的系统设计：

~~~text
same transport primitive
~~~

承载：

- computation；
- reclamation；
- control；
- lifecycle。

但上层必须区分各自 completion semantics。

---

# 一百二十七、Destruction RPC 失败意味着什么

`destroy_on()`：

~~~text
return future<>
~~~

destructor path 如果 future failed：

~~~text
run_in_background
~~~

至少让 Runtime 有机会观察 failure。

---

# 一百二十八、为什么不能静默丢弃 Failed Reclaim Future

因为这可能意味着：

~~~text
owner shard cleanup 没完成
~~~

会变成：

- leak；
- stale local registry；
- shutdown hang。

---

# 一百二十九、Future 章节里的 Error Observation 在这里再次出现

如果 background cleanup 没人：

~~~text
observe exception
~~~

Future Runtime 会报告 orphan failure。

---

# 一百三十、Lifecycle Protocol 与 Error Propagation 必须结合

不能只说：

~~~text
eventually delete
~~~

还要问：

~~~text
如果 delete operation 自己失败怎么办
~~~

---

# 一百三十一、`sharded::stop()` 也保留原始 Stop Failure

源码：

~~~text
Phase 1 Service::stop()
→ future fut

Phase 2 drop instances

finally:
→ return original fut
~~~

即使 teardown 仍要继续，

原 stop exception 不能被 cleanup 覆盖。

---

# 一百三十二、Cleanup 与 Original Error 是两条义务

常见正确模式：

~~~text
try operation
capture original error
run cleanup anyway
propagate original error
~~~

---

# 一百三十三、Startup Failure 同样

~~~text
construct some shards
one fails
stop partial state
rethrow construction error
~~~

---

# 一百三十四、Sharded Lifecycle 本质是 Multi-shard Saga

每个 shard：

~~~text
独立创建/停止 local instance
~~~

整体没有中央事务内存快照。

失败处理依赖：

~~~text
compensating cleanup
~~~

---

# 一百三十五、这比“distributed object”更准确

`sharded<Service>` 不是：

~~~text
一个对象被复制到多个核
~~~

而是：

~~~text
多个 owner-local objects
+
一个协调它们生命周期/调用的 facade
~~~

---

# 一百三十六、Facade 自己不能替代 Local Ownership

即使有：

~~~text
sharded<Service> container
~~~

Service 本身仍应该遵守：

~~~text
local shard direct access
~~~

---

# 一百三十七、为什么 `local_shared()` 返回的是 Seastar `shared_ptr`

只在：

~~~text
当前 shard
~~~

拿到 local owner。

它不是鼓励：

~~~text
拿了以后跨核传播
~~~

---

# 一百三十八、如果确实要传播 Ownership

应该显式：

~~~text
make_foreign(local_shared())
~~~

这样把：

~~~text
owner shard
~~~

一起编码。

---

# 一百三十九、Type Transition 本身表达语义提升

~~~text
shared_ptr<T>
→ local shared ownership

foreign_ptr<shared_ptr<T>>
→ cross-shard movable ownership token
~~~

---

# 一百四十、这比注释“请勿跨线程”强得多

类型系统直接区分：

~~~text
local pointer
foreign-safe ownership wrapper
~~~

---

# 一百四十一、机器人 Runtime 的直接映射：Per-device Owner Shard

假设：

~~~text
Shard 0
→ camera ingest

Shard 1
→ lidar

Shard 2
→ motion planner

Shard 3
→ telemetry
~~~

每个模块拥有本地：

- queues；
- timers；
- allocator；
- protocol state。

---

# 一百四十二、跨模块操作不要共享裸对象

不要：

~~~text
planner thread
直接拿 lidar mutable map*
~~~

更合理：

~~~text
submit computation to lidar owner
~~~

或者传：

~~~text
immutable/refcounted data ownership token
~~~

---

# 一百四十三、GPU Context 也是典型 Owner Domain

CUDA object：

~~~text
memory allocation
stream
event
context-bound handle
~~~

常常有明确 device/context affinity。

---

# 一百四十四、可以借鉴 foreign_ptr 思路

wrapper 记录：

~~~text
device id
executor id
owner thread/shard
~~~

destructor：

~~~text
enqueue reclaim to owner executor
~~~

而不是：

~~~text
任意 host thread cudaFree
~~~

---

# 一百四十五、但 GPU API 可能支持 Thread-safe Destroy，也要区分

是否需要 owner-domain reclaim，

必须来自真实 API contract。

不要机械套 foreign_ptr。

---

# 一百四十六、Thread-affine GUI/Event Loop 也一样

对象：

~~~text
can move handle across threads
~~~

但：

~~~text
destroy must happen on event-loop thread
~~~

foreign_ptr 就是通用模式：

~~~text
mobile handle
+
fixed destruction domain
~~~

---

# 一百四十七、io_uring / Reactor-local Request 也一样

completion object 可能关联：

~~~text
specific ring
specific reactor
~~~

把 handle move 出去，

不代表：

~~~text
任意线程 cancel/free 都安全
~~~

---

# 一百四十八、Ownership 应该回答五个问题

看到一个 pointer wrapper，至少问：

1. 谁拥有 pointee？
2. 谁可以直接读？
3. 谁可以直接写？
4. 谁执行最后一次 release？
5. 谁执行 destructor/free？

---

# 一百四十九、普通 `unique_ptr` 只强表达第一和第五的一部分

它默认：

~~~text
最后 holder thread
执行 deleter
~~~

这隐含假设：

~~~text
deleter thread-insensitive
~~~

---

# 一百五十、foreign_ptr 修改了这个默认

~~~text
last wrapper holder shard
~~~

可以与：

~~~text
deleter execution shard
~~~

不同。

---

# 一百五十一、所以它是“Execution-affine Smart Pointer”

比简单说：

~~~text
跨核 smart pointer
~~~

更准确。

---

# 一百五十二、为什么不直接在 Destructor 阻塞 `submit_to().get()`

因为 Reactor thread 不能：

~~~text
blocking wait
~~~

否则会死锁/卡住 progress。

---

# 一百五十三、Async Runtime 的 Destructor 不能随便同步等自己 Runtime

这是非常普遍的规则。

如果 destructor 在 Reactor thread：

~~~text
等待另一个 shard completion
~~~

但 completion 本身需要当前 Reactor progress，

可能形成循环等待。

---

# 一百五十四、所以 Destructor 只能 Initiate

强等待必须让调用者显式：

~~~text
co_await destroy()
~~~

---

# 一百五十五、这就是 Explicit Async Close Pattern

资源类最好提供：

~~~text
close()/destroy()
→ Future
~~~

让生命周期关键路径显式等待。

Destructor 只作为：

~~~text
fallback cleanup
~~~

---

# 一百五十六、`sharded::stop()` 就是这种显式 Async Close

调用者必须：

~~~text
co_await service.stop()
~~~

才能保证 multi-shard lifecycle 完成。

---

# 一百五十七、`~sharded()` 为什么要求已经 Stop

文档：

~~~text
Must not be in a started state
~~~

因为 destructor 无法优雅执行整个：

~~~text
multi-shard async stop protocol
~~~

---

# 一百五十八、这是 Async RAII 的边界

C++ RAII 很适合：

~~~text
local synchronous resource
~~~

但 multi-shard async lifecycle 必须增加：

~~~text
explicit stop
~~~

---

# 一百五十九、如果用户忘记 Stop

Runtime 应尽量：

- assert；
- debug detect；
- documentation contract。

不能指望 destructor 自动：

~~~text
跨核 await all
~~~

---

# 一百六十、Sharded Stop 的 Quiescence 有两层

第一：

~~~text
Service::stop()
→ no new work / drain subsystem
~~~

第二：

~~~text
shared reference count reaches zero
→ object physically destructed
~~~

---

# 一百六十一、`async_sharded_service` 只增强第二层可观测性

它不替你实现：

~~~text
业务 stop logic
~~~

Service 自己仍需正确：

- close gate；
- cancel work；
- track refs。

---

# 一百六十二、Shared-from-this 责任仍在 Service 自己

源码注释明确：

~~~text
service must track its references
in asynchronous code
by shared_from_this()
~~~

---

# 一百六十三、为什么 Runtime 不能自动知道所有 Async Capture

C++ lambda 可以把：

~~~text
this*
~~~

偷偷 capture 到任意 future chain。

Container 无法推断：

~~~text
哪些 callback 仍在使用 object
~~~

---

# 一百六十四、因此 Service 需要显式 Lifetime Discipline

如果 async callback 要跨 `stop()` 存活：

~~~text
capture shared_from_this()
~~~

否则可能：

~~~text
use-after-free
~~~

---

# 一百六十五、这与 Callback Quiescence 的问题完全一致

需要分：

~~~text
logical stop
lifetime hold
in-flight completion
physical reclaim
~~~

---

# 一百六十六、foreign_ptr 又是另一种 Lifetime Hold

它让 remote shard 持有：

~~~text
一个不会在错误 shard 释放的 owner token
~~~

---

# 一百六十七、`shared_ptr` 与 `foreign_ptr` 解决不同问题

`shared_ptr`：

~~~text
how many owners?
~~~

`foreign_ptr`：

~~~text
where may ownership operations/reclaim safely execute?
~~~

---

# 一百六十八、组合后才完整

~~~text
foreign_ptr<shared_ptr<T>>
~~~

表示：

~~~text
multi-owner
+
cross-shard mobile token
+
owner-shard refcount/destruction
~~~

---

# 一百六十九、为什么 `foreign_ptr::copy()` 捕获 `this` 值得注意

源码：

~~~cpp
smp::submit_to(_cpu,
    [this] {
        auto v = _value;
        ...
    });
~~~

这意味着调用者必须保证：

~~~text
*this
在 copy future 完成前仍有效
~~~

---

# 一百七十、Async Member API 的 Self Lifetime 不能忽略

一个返回 Future 的 member function：

~~~text
并不自动延长 *this lifetime
~~~

除非：

- capture ownership；
- API contract 要求 caller keep alive；
- object本身被更高层 ownership 管理。

---

# 一百七十一、这是通用 C++ Async 风险

~~~cpp
auto f = obj.copy();
destroy obj;
await f;
~~~

如果 implementation 还 capture raw `this`：

~~~text
危险
~~~

---

# 一百七十二、API 使用时应让 Wrapper 活到 Completion

这不是 foreign_ptr 特有，

是：

~~~text
async member operation
+
raw this capture
~~~

的共同规则。

---

# 一百七十三、`destroy_on()` 则避免 Capture `this`

它把：

~~~text
PtrType p
unsigned cpu
~~~

作为值传递，

生命周期独立于 wrapper。

---

# 一百七十四、这正适合 Destructor Path

Destructor 一旦返回：

~~~text
this storage gone
~~~

任何 background cleanup 都不能依赖：

~~~text
this*
~~~

---

# 一百七十五、Destructor 发起的异步任务必须自包含

这是可以直接写进代码规范的原则：

> **析构函数启动的异步 cleanup 必须把所需状态按值搬走，不能继续引用即将销毁的对象。**

---

# 一百七十六、foreign_ptr 正确做到这一点

~~~text
move _value out
copy _cpu
→ destroy_on(value,cpu)
~~~

之后 cleanup 不需要 wrapper。

---

# 一百七十七、Sharded Start Lambda 为什么可以 Capture `this`

因为：

~~~text
sharded::start()
~~~

的 Future 未完成前，

调用者必须保持 sharded container 存活。

这属于 API lifecycle contract。

---

# 一百七十八、Container 自身是 Long-lived Runtime Anchor

通常：

~~~text
construct sharded
co_await start
...
co_await stop
destroy sharded
~~~

---

# 一百七十九、这就是显式 Lifecycle Object

比：

~~~text
随手返回几个 free-floating futures
~~~

更容易管理整个 distributed service。

---

# 一百八十、完整 `sharded` 对象图

~~~text
                sharded<Service>
                       |
             vector<entry>
                       |
        +--------------+--------------+
        |              |              |
      entry0          entry1          entry2
        |              |              |
 shared_ptr S0    shared_ptr S1   shared_ptr S2
        |              |              |
      Shard0          Shard1         Shard2
       owner           owner          owner
~~~

---

# 一百八十一、完整 Invocation 路径

~~~text
Shard A
call invoke_on(B, func)
        |
        v
smp::submit_to(B)
        |
        v
request SPSC
        |
        v
Shard B Reactor
        |
        v
get_local_service()
        |
        v
func(Service B&)
        |
        v
Future/result
        |
        v
completion SPSC
        |
        v
Shard A Promise
~~~

---

# 一百八十二、完整 `foreign_ptr` 生命周期

~~~text
Shard B
create Ptr
  |
  v
make_foreign
  |
  | _cpu=B
  v
foreign_ptr
  |
  | move through SMP
  v
Shard A
holds wrapper
  |
  | destructor/reset
  v
destroy_on(value, B)
  |
  v
submit_to(B)
  |
  v
Shard B callback
  |
  | explicit v={}
  v
real ref decrement/destructor/free
  |
  v
completion returns A
~~~

---

# 一百八十三、两条链最终都依赖 Owner-shard Execution

`sharded`：

~~~text
remote method
→ owner
~~~

`foreign_ptr`：

~~~text
remote reclamation
→ owner
~~~

---

# 一百八十四、不要把 Owner Shard 理解成“所有访问都必须 Message Passing”

一些 data buffer 可以：

~~~text
remote read
~~~

取决于具体 memory contract。

关键是：

~~~text
mutable state / refcount / allocator metadata / destructor side effects
~~~

必须按真实 owner contract处理。

---

# 一百八十五、Memory Visibility 仍是另一个问题

即使 lifetime 安全，

remote CPU 直接读 bytes 还要考虑：

- publication；
- synchronization；
- immutable-after-publish；
- DMA/cache coherence。

foreign_ptr 不解决这些。

---

# 一百八十六、Lifetime Safety ≠ Data-race Safety

这条必须单独记住。

`foreign_ptr` 确保：

~~~text
resource不会在错误 shard 销毁
~~~

不保证：

~~~text
两个 shard 同时 mutate pointee 是安全的
~~~

---

# 一百八十七、如果要共享 Mutable Data

要么：

~~~text
真正 thread-safe abstraction
~~~

要么：

~~~text
owner-computes
~~~

foreign_ptr 不是 mutex。

---

# 一百八十八、机器人共享感知数据的合理模式

例如 PointCloud：

~~~text
producer shard
build immutable frame
publish ownership token
consumer shards read immutable bytes
last release returns owner allocator
~~~

这个模型很适合 foreign_ptr-like wrapper。

---

# 一百八十九、控制器状态则不一样

PID integrator / actuator state：

~~~text
mutable
~~~

不应该因为有 foreign lifetime wrapper 就允许：

~~~text
多个 shard 直接写
~~~

更适合：

~~~text
submit command to owner
~~~

---

# 一百九十、Immutable Data 与 Mutable Service 应用不同机制

~~~text
large immutable payload
→ move/share lifetime handle

mutable service state
→ move computation
~~~

这几乎就是：

~~~text
foreign_ptr
vs
sharded::invoke_on
~~~

的分工。

---

# 一百九十一、这可以作为 Runtime 设计决策树

先问：

~~~text
数据是否 mutable？
~~~

如果 mutable：

~~~text
是否有明确 single owner？
→ 把 computation 发给 owner
~~~

如果 immutable：

~~~text
能否跨核安全读取？
→ 传 ownership/lifetime token
~~~

---

# 一百九十二、`foreign_ptr` 为什么不是 Serialization

它并没有：

~~~text
copy object state
~~~

到另一个 shard。

只是：

~~~text
移动 pointer ownership wrapper
~~~

---

# 一百九十三、所以它适合同地址空间 SMP

跨进程/跨机器：

~~~text
pointer address 无意义
~~~

不能直接使用同模型。

---

# 一百九十四、跨进程要替换成

- shared-memory offset；
- handle id；
- object key；
- RPC object id。

但：

~~~text
owner-domain reclaim
~~~

的思想仍然成立。

---

# 一百九十五、Shared Memory 的 Foreign Handle 也可记录 Owner Process

例如：

~~~text
segment offset
+
owner pid/runtime
+
release channel
~~~

最后一个 consumer：

~~~text
send release to owner
~~~

与 foreign_ptr 很像。

---

# 一百九十六、Distributed Object Reference 也一样

~~~text
object id
+
home node
~~~

remote holder：

~~~text
does not directly free home object
~~~

而是：

~~~text
release protocol
~~~

---

# 一百九十七、foreign_ptr 是一个小型 Distributed Lifetime Protocol

虽然只跨 CPU，

但结构已经包含：

- owner identity；
- movable handle；
- asynchronous release；
- explicit copy RPC；
- explicit strong destroy completion。

---

# 一百九十八、这就是为什么它比普通 Smart Pointer 值得研究

它把：

~~~text
execution topology
~~~

编码进：

~~~text
ownership abstraction
~~~

---

# 一百九十九、最容易写错的五种用法

第一：

~~~text
拿 foreign_ptr
在 foreign shard 直接 mutate pointee
~~~

误把 lifetime-safe 当 data-race-safe。

---

# 二百、第二种错误

~~~text
release()
然后让 PtrType 在当前 foreign shard 析构
~~~

直接丢掉 owner metadata。

---

# 二百零一、第三种错误

~~~text
copy() 后立刻销毁 source wrapper
~~~

而实现仍 capture `this`，

没有等待 returned Future。

---

# 二百零二、第四种错误

~~~text
在 destructor 里 blocking wait destroy()
~~~

阻塞 Reactor progress。

---

# 二百零三、第五种错误

~~~text
sharded.stop() 前还有未纳入 shared_from_this/gate 的裸 this async callback
~~~

container 看不到这类生命周期。

---

# 二百零四、`async_sharded_service` 也不是万能 UAF 防护

如果 callback 捕获：

~~~text
raw this
~~~

而没有强 owner，

`track_deletion()` 也无法知道这个 pointer 仍会被使用。

---

# 二百零五、Lifetime Tracking 必须从任务创建处建立

异步任务如果需要 object：

~~~text
task capture
→ strong owner
~~~

或：

~~~text
gate entry
~~~

必须在启动时登记。

---

# 二百零六、不能在 Shutdown 时再猜还有谁使用对象

这和 registry quiescence 的规律完全一致。

---

# 二百零七、Sharded Stop 的正确业务模板

概念上：

~~~text
Service::stop():
    close admission
    cancel producers
    close gate
    await gate drain
    return
~~~

然后 container：

~~~text
drop shared_ptr
await destruction if requested
~~~

---

# 二百零八、Foreign Resource 的正确业务模板

~~~text
if lifetime only:
    move foreign_ptr

if need remote mutation:
    submit_to(owner)

if need explicit reclaim barrier:
    co_await foreign.destroy()
~~~

---

# 二百零九、这一套与 Seastar Scheduler/Future 的关系

`invoke_on()`：

~~~text
cross-shard work item
→ target task
→ Future completion
~~~

`foreign_ptr destroy()`：

~~~text
cross-shard cleanup work
→ target body releases pointer
→ Future completion
~~~

---

# 二百一十、Ownership Protocol 最终还是 Runtime Scheduling Problem

所谓：

~~~text
在哪销毁
~~~

最终意味着：

~~~text
把 destructor-triggering operation
安排到哪个 scheduler domain执行
~~~

---

# 二百一十一、所以 Memory Management 与 Scheduler 不是完全独立

Owner-shard allocator 的正确性依赖：

~~~text
reclaim operation
被 schedule 回 owner
~~~

---

# 二百一十二、这为下一章 Cross-shard Memory Reclaim 做铺垫

`foreign_ptr` 是：

~~~text
object-level
explicit smart-wrapper reclaim
~~~

下一章 allocator 里的：

~~~text
xcpu_freelist
~~~

解决的是：

~~~text
raw allocation-level
bulk cross-CPU free handoff
~~~

---

# 二百一十三、两者解决同一根问题的不同层级

~~~text
foreign_ptr
→ C++ ownership object level

xcpu_freelist
→ allocator block level
~~~

共同目标：

> **foreign CPU 可以发起释放，但真正 owner-local bookkeeping 应回归所属 CPU。**

---

# 二百一十四、最终统一模型

~~~text
                 SHARD OWNERSHIP
                       |
          +------------+------------+
          |                         |
      Mutable Service            Mobile Handle
          |                         |
   sharded<Service>             foreign_ptr<Ptr>
          |                         |
 move computation               move ownership token
          |                         |
 smp::submit_to(owner)          holder may be foreign
          |                         |
 execute Service&               destruction requested
          |                         |
 result Future                  submit_to(owner)
          |                         |
 origin continuation            explicit pointer release
~~~

---

# 二百一十五、源码作者要守住的核心不变量

第一：

> **`sharded<Service>` 中 shard i 的实例必须在 shard i 的执行上下文构造、调用、stop 和释放 container ownership。**

第二：

> **跨 shard 调用优先移动 computation 到 owner，而不是直接暴露 remote mutable pointer。**

第三：

> **Seastar local shared_ptr 的 refcount 非跨核原子；即使 refcount 改成 atomic，也不能自动解决 destructor execution affinity。**

第四：

> **`foreign_ptr` 记录的不只是 pointer value，还记录 reclamation domain。**

第五：

> **wrapper 当前在哪个 shard 与 pointee 最后应在哪个 shard release 是两件事。**

第六：

> **跨 shard cleanup callback 必须在 owner callback body 内显式释放 resource；不能假设 lambda capture 会在 callback 执行 shard 析构。**

第七：

> **如果 copy 本质是跨核 RPC，API 应返回 Future，而不是伪装成普通 copy constructor。**

第八：

> **`release()` 会移除 cross-shard lifetime guard；caller 必须继承 owner-shard reclaim 责任。**

第九：

> **foreign_ptr 主要解决 lifetime/reclamation，不自动提供 remote mutation safety。**

第十：

> **同步 destructor 只能发起异步 reclaim；需要强 completion 时必须使用显式 async destroy/stop。**

第十一：

> **`sharded::stop()` 应先 quiesce service，再释放 ownership；对于 async_sharded_service，还要等最后一个 shared reference 真正释放。**

第十二：

> **异步析构任务必须自包含，不能继续 capture 已经销毁的 wrapper `this`。**

---

# 二百一十六、最终心智模型

`sharded<Service>`：

~~~text
一个逻辑服务
        ↓
N 个 owner-local instance
        ↓
local()
→ only current shard instance

invoke_on(B)
→ computation crosses shard
→ B local instance executes
~~~

`foreign_ptr<Ptr>`：

~~~text
resource created on B
        ↓
wrapper records owner=B
        ↓
wrapper can move to A
        ↓
A may hold lifetime token
        ↓
last release requested on A
        ↓
reclaim operation goes back B
        ↓
real pointer destruction on B
~~~

如果只记一个结论：

> **Seastar 的 shard-per-core 并不是“避免锁”这么简单，而是把 execution ownership 贯彻到对象构造、方法调用、引用计数和最终析构：`sharded<T>` 把计算送回对象 owner，`foreign_ptr` 把释放送回资源 owner。地址可以跨核传，ownership obligation 不能因为地址可见就随便迁移。**
