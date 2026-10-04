# Future / Continuation：状态迁移、Task 化 Continuation 与异步控制流

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

## 核心问题

Seastar 的 `future<T>` 很容易被理解成：

~~~text
一个以后会装入 T 的盒子
~~~

这个理解只够写 API，不够理解 Runtime。

真正值得研究的是：

> **一个尚未 ready 的值，在 promise、future、continuation 之间究竟存在哪里？`.then()` 为什么有时直接 inline 执行、有时必须动态创建 task？promise 被 move 或提前析构时，等待链为什么还能正确继续？异常为什么不会悄悄丢失？**

固定源码给出的答案可以压缩成：

~~~text
one logical future_state
        |
        +-- promise-local storage
        +-- future storage
        +-- continuation storage
             ↑
        only one active
~~~

以及：

~~~text
dependency not ready
        ↓
continuation object
        ↓
continuation : task
        ↓
promise becomes ready
        ↓
schedule(task)
        ↓
reactor executes
        ↓
continuation resolves next promise
        ↓
delete continuation
~~~

所以 Future 系统不只是 value transport。

它同时承担：

- 异步状态存储；
- producer/consumer 直接链接；
- continuation allocation；
- task scheduling；
- exception propagation；
- lifecycle error reporting；
- background-work liveness discipline。

---

# 一、同步调用栈为什么不适合 Reactor

同步程序：

~~~text
func A
  ↓
wait I/O
  ↓
OS thread sleeps
  ↓
I/O ready
  ↓
same stack continues
~~~

如果一个 shard 只有一个 Reactor thread，

任何普通 blocking wait 都会让整个 shard：

~~~text
停止处理
network
timer
other requests
SMP messages
~~~

所以 Seastar 不保存：

~~~text
blocked OS thread
~~~

而是保存：

~~~text
what to do next
~~~

---

# 二、Continuation 就是“以后继续执行什么”

例如：

~~~cpp
read().then([] (auto data) {
    return parse(data);
});
~~~

当 `read()` 未完成时，

当前 C++ stack 可以结束。

未来真正需要保存的只有：

~~~text
result dependency
+
continuation function
+
next promise
+
scheduler metadata
~~~

---

# 三、Seastar 把 Continuation 直接变成 Task

源码：

~~~cpp
template <typename T = void>
class continuation_base : public task
~~~

这条继承关系非常关键。

它说明：

~~~text
async dependency node
=
scheduler runnable node
~~~

---

# 四、为什么不再包一层 `Task{Continuation*}`

如果 continuation 自己就是 task，

Promise ready 时可以直接：

~~~cpp
schedule(task*)
~~~

不需要：

- global callback registry；
- wrapper allocation；
- second dispatch object。

---

# 五、Continuation 自身保存什么

~~~cpp
future_state _state;
~~~

派生类再保存：

~~~text
next promise
original Func
Wrapper
~~~

于是一个 continuation object 自己就拥有：

~~~text
input state
+
callback
+
output promise
+
scheduler identity
~~~

---

# 六、这是一次性异步状态机节点

生命周期：

~~~text
allocate
→ wait
→ schedule
→ run once
→ fulfill next promise
→ delete this
~~~

---

# 七、`run_and_dispose()` 为什么最后 `delete this`

源码：

~~~cpp
virtual void run_and_dispose() noexcept override {
    try {
        _wrapper(
          std::move(this->_pr),
          _func,
          std::move(this->_state));
    } catch (...) {
        this->_pr.set_to_current_exception();
    }
    delete this;
}
~~~

---

# 八、Continuation 是 One-shot Object

执行以后：

- dependency 已消费；
- callback 已执行；
- output promise 已转移；
- input state 已移动。

它没有第二次运行的合法状态。

---

# 九、Self-delete 在这里为什么合理

因为 ownership 协议明确：

~~~text
new continuation
→ scheduler owns execution obligation
→ run_and_dispose is terminal operation
~~~

不是任意 callback 在执行中：

~~~text
随便 delete this
~~~

---

# 十、Promise/Future 的核心问题不是“共享一个值”

源码文档明确：

> 一个 future/promise pair 维护一个逻辑 `future_state`，物理上最多有三个可存储位置，但任意时刻只有一个是 active。

三个位置：

~~~text
1. promise._local_state

2. future._state

3. continuation._state
~~~

---

# 十一、为什么 Promise 自己必须有 `_local_state`

Promise 可以先于 Future 存在：

~~~cpp
promise<T> p;
~~~

此时用户甚至还没调用：

~~~cpp
p.get_future();
~~~

但 producer 已经可能：

~~~cpp
p.set_value(x);
~~~

---

# 十二、如果 Promise 不带本地 State

那么：

~~~text
set_value before get_future
~~~

就没有地方保存 result。

所以：

~~~text
new promise
_state → _local_state
~~~

---

# 十三、初始状态图

~~~text
promise
  |
  +-- _local_state [future/unavailable]
  |
  +-- _state ------^
  |
  +-- _future = nullptr
  |
  +-- _task = nullptr
~~~

---

# 十四、调用 `get_future()` 后发生什么

源码：

~~~cpp
future<T>
promise<T>::get_future() noexcept {
    SEASTAR_ASSERT(
      !this->_future
      && this->_state
      && !this->_task);

    return future<T>(this);
}
~~~

---

# 十五、Future Constructor 做两件事

逻辑：

~~~text
move promise._local_state
→ future._state

promise._state
→ &future._state

promise._future
→ future
~~~

---

# 十六、为什么状态要移动到 Future

只要 Future 存在且尚未 `.then()`，

消费方最自然的访问位置就是：

~~~text
future._state
~~~

例如：

~~~cpp
future.available()
future.failed()
future.get()
~~~

都能直接访问本地字段。

---

# 十七、Promise 不再拥有 Result Storage

但 Promise 还必须知道：

~~~text
set_value 应该写到哪里
~~~

所以它保存：

~~~cpp
future_state_base* _state;
~~~

这是一根“当前 active storage 指针”。

---

# 十八、Promise 的 `_state` 是间接寻址

不是：

~~~text
promise 永远存 result
~~~

而是：

~~~text
promise knows current result storage
~~~

---

# 十九、调用 `.then()` 且 Future 尚未 Ready

这是第三次状态迁移。

源码会：

~~~text
allocate continuation
~~~

continuation 内也有：

~~~text
future_state _state
~~~

---

# 二十、Future 把 Promise 从自己身上 Detach

`future_base::schedule()`：

~~~cpp
promise_base* p =
    detach_promise();

p->_state = state;
p->set_task(tws);
~~~

---

# 二十一、`detach_promise()` 做什么

~~~cpp
_promise->_state = nullptr;
_promise->_future = nullptr;
return exchange(_promise, nullptr);
~~~

先断掉：

~~~text
promise ↔ future
~~~

关系。

---

# 二十二、随后重新绑定到 Continuation

~~~text
promise._state
→ continuation._state

promise._task
→ continuation
~~~

Future 本身：

~~~text
被消费
~~~

---

# 二十三、第三种合法布局

~~~text
Promise
   |
   +-- _future = null
   |
   +-- _task -------> Continuation
   |
   +-- _state ------> Continuation._state
~~~

---

# 二十四、为什么 Continuation State 永远不 Move

源码文档明确指出：

~~~text
future may move
promise may move
continuation is dynamically allocated and never moved
~~~

---

# 二十五、这让等待态地址稳定

一旦 Promise `_state` 指向：

~~~text
continuation._state
~~~

就不需要以后因为：

~~~text
continuation move
~~~

重新修指针。

---

# 二十六、动态分配不是纯性能损失

它换来：

~~~text
stable address
+
scheduler object lifetime
~~~

---

# 二十七、为什么 Promise 与 Future 要互相保存 Pointer

Future：

~~~cpp
promise_base* _promise;
~~~

Promise：

~~~cpp
future_base* _future;
~~~

这看起来像双向耦合。

---

# 二十八、因为双方都可能 Move

如果只有 Promise 知道 Future，

Future 被 move 后：

~~~text
promise._future
~~~

会变成旧地址。

---

# 二十九、Future Move Constructor 会修反向指针

源码：

~~~cpp
void move_it(
    future_base&& x,
    future_state_base* state) noexcept {

    _promise = x._promise;

    if (auto* p = _promise) {
        x.detach_promise();
        p->_future = this;
        p->_state = state;
    }
}
~~~

---

# 三十、Future Move 不是普通 Memcpy

除了移动：

~~~text
future._state
~~~

还必须更新：

~~~text
promise._future
promise._state
~~~

---

# 三十一、这叫 Relocatable Endpoint Protocol

对象地址变化时，

所有保存它地址的 peer 必须修复链接。

---

# 三十二、Promise Move 同理

`promise_base::move_it()`：

~~~text
copy _task
copy _state
copy _future
~~~

如果 `_future != nullptr`：

~~~text
future detach old promise
future._promise = new promise
~~~

---

# 三十三、Promise 自己的 `_local_state` 更特殊

如果：

~~~text
_state == &old_promise._local_state
~~~

move 后还要：

~~~text
_state → &new_promise._local_state
move old local state into new local state
~~~

---

# 三十四、否则 Pointer 会指向已移动对象内部

这是经典 self-relative pointer 问题。

---

# 三十五、一个通用规则

> **如果对象内部保存“指向自己成员”的 pointer，move constructor 不能默认生成。**

---

# 三十六、Future/Promise 为什么禁止 Copy

如果允许 copy：

~~~text
one producer
→ multiple independent Future objects
~~~

就必须决定：

- result storage 在哪；
- continuation 可注册几个；
- exception 谁消费；
- refcount 怎么办。

---

# 三十七、Seastar 普通 Future 是 Single-consumer

源码文档：

~~~text
Only one continuation may be scheduled.
~~~

所以：

~~~text
move-only
~~~

是自然类型语义。

---

# 三十八、需要多消费者怎么办

应该使用：

~~~text
shared_future
~~~

而不是让普通 future 偷偷变 shared state。

---

# 三十九、`future_state` 的逻辑状态

`future_state_base::state`：

~~~text
invalid
future
result_unavailable
result
exception...
~~~

---

# 四十、`future` 状态不是 C++ Future Object 的意思

这里枚举值：

~~~text
state::future
~~~

表示：

~~~text
尚未有 result / exception
~~~

也就是 pending。

---

# 四十一、`result`

表示：

~~~text
value ready
~~~

---

# 四十二、`exception_min` 及以上

表示：

~~~text
failed future
~~~

异常本身：

~~~text
std::exception_ptr
~~~

被存进 union。

---

# 四十三、为什么 Exception 也属于 Future State

异步计算结果不是：

~~~text
T only
~~~

而是：

\[
Result =
T
\cup
Exception
\]

---

# 四十四、这使异常传播不依赖调用栈

同步：

~~~text
throw
→ stack unwinding
~~~

异步：

~~~text
throw
→ exception_ptr
→ future_state
→ later continuation
~~~

---

# 四十五、Future Chain 是 Heap/Object Graph，不是 Stack Chain

同步控制流：

~~~text
A stack frame
→ B frame
→ C frame
~~~

Seastar async：

~~~text
Future state
→ Continuation object
→ Next Promise
→ Next Future
~~~

---

# 四十六、为什么 `future_state<T>` 要求 T Noexcept Move

源码：

~~~cpp
static_assert(
  is_nothrow_move_constructible_v<T>);

static_assert(
  is_nothrow_destructible_v<T>);
~~~

---

# 四十七、因为 State 会频繁迁移

例如：

~~~text
promise local
→ future
→ continuation
→ next callback
~~~

如果每次 move 都可能 throw，

Future Runtime 本身的状态转换就会产生：

~~~text
half-moved protocol state
~~~

极难恢复。

---

# 四十八、Runtime Internal State Transition 倾向 Noexcept

这是非常通用的设计原则。

尤其：

- scheduler node；
- promise state；
- completion record；
- ownership wrapper。

---

# 四十九、Ready Future 的 Fast Path

release build 的 `then_impl()`：

~~~cpp
if (failed()) {
    return make_exception_future(...);
}
else if (available()) {
    return futurator::invoke(
      func,
      ...);
}
~~~

---

# 五十、这意味着 Ready Future 不创建 Continuation

也不：

~~~text
schedule task
~~~

而是：

~~~text
inline invoke
~~~

---

# 五十一、为什么这样做

如果 dependency 已经 ready，

完整走：

~~~text
new continuation
→ scheduler queue
→ reactor
→ run
→ delete
~~~

只会制造额外：

- allocation；
- task enqueue；
- context switch-like handoff；
- cache traffic。

---

# 五十二、Fast Path 的真实语义

~~~text
already available
→ consume now
~~~

而不是：

~~~text
always asynchronous next tick
~~~

---

# 五十三、旧文章里的一个常见误解必须纠正

当前固定源码的 `future.hh` 在这条 release fast path 上：

~~~text
没有调用 need_preempt()
~~~

所以不能说：

~~~text
ready Future 一定会因为 need_preempt 而 yield
~~~

---

# 五十四、Data Dependency Ready 时确实可能继续 Inline

这意味着长链：

~~~text
ready.then(...)
     .then(...)
     .then(...)
~~~

可能在当前 call stack 内连续推进多个节点。

---

# 五十五、那公平性谁负责

不是这一条 `then_impl()` fast path 本身。

Seastar 的 cooperative scheduler 公平性来自更大的系统：

- task boundaries；
- explicit yielding；
- I/O completion scheduling；
- scheduling groups；
- preemption points；
- coroutine/future utilities。

---

# 五十六、不要把一个全局设计原则强行投射到每个 Fast Path

源码分析必须区分：

~~~text
framework overall supports preemption/fairness
~~~

和：

~~~text
this exact branch performs preemption check
~~~

---

# 五十七、Future 尚未 Ready 时才进入 Continuation Path

`then_impl_nrvo()`：

~~~text
create output future
→ obtain output promise
→ allocate continuation
→ attach it to input promise
→ return output future
~~~

---

# 五十八、为什么先创建 Output Future

`.then(func)` 自己必须立即返回：

~~~text
future<func-result>
~~~

即使 func 还没运行。

---

# 五十九、所以 Continuation 必须持有 Output Promise

结构：

~~~text
Input Promise
   |
Continuation
   |
   +-- input future_state
   +-- func
   +-- output promise
~~~

---

# 六十、当 Input Promise Ready

`promise_base::make_ready()`：

~~~cpp
if (_task) {
    schedule(exchange(_task, nullptr));
}
~~~

---

# 六十一、为什么 `_task` 要 exchange 到 nullptr

因为 Promise 只能把 continuation：

~~~text
schedule once
~~~

schedule 后：

~~~text
Promise no longer owns waiting task pointer
~~~

---

# 六十二、这同时关闭 Double-schedule 风险

如果某条错误路径重复调用：

~~~text
make_ready
~~~

原 `_task` 已被清空。

---

# 六十三、Urgent Ready Path

源码还有：

~~~cpp
schedule_urgent(...)
~~~

用于：

~~~text
urgent::yes
~~~

例如 `set_urgent_state()`。

---

# 六十四、普通 `set_value()` 走普通 Scheduler Queue

~~~cpp
make_ready<urgent::no>();
~~~

---

# 六十五、Promise Ready ≠ Inline Run Continuation

如果 Future 尚未 ready 时已经注册 continuation：

~~~text
Promise set_value
→ schedule task
~~~

而不是：

~~~text
producer callback stack 直接执行 continuation
~~~

---

# 六十六、这能避免 Completion Producer 被用户 Continuation 拖住

例如 I/O completion path：

~~~text
set promise
~~~

只需要把 continuation 变 runnable。

用户逻辑：

~~~text
由 Reactor scheduler 执行
~~~

---

# 六十七、这叫 Completion Publication 与 Continuation Execution 分离

非常通用。

---

# 六十八、Continuation 的 Scheduling Group 从哪里来

`task` 构造时绑定：

~~~text
current scheduling group
~~~

所以注册 continuation 时：

~~~text
当前执行上下文的 scheduling identity
~~~

会跟着 task 保存。

---

# 六十九、因此 Future Chain 不是“无所属 callback”

它最终仍属于：

~~~text
某个 Reactor scheduling group
~~~

---

# 七十、Cross-shard SMP Work Item 也继承 Task

前一章：

~~~text
remote queue arrival
→ work_item::process
→ schedule(this)
~~~

和 Future：

~~~text
promise ready
→ schedule(continuation)
~~~

本质相同。

---

# 七十一、统一 Runtime 模型

~~~text
external event
remote message
future dependency
timer
I/O
~~~

最终都尽量变成：

~~~text
task
~~~

交给同一个 scheduler。

---

# 七十二、`run_and_dispose()` 中的 Exception Boundary

Continuation wrapper：

~~~cpp
try {
    wrapper(...);
} catch (...) {
    output_promise.set_to_current_exception();
}
~~~

---

# 七十三、用户 Callback Throw 不会穿透 Reactor

而是：

~~~text
catch
→ convert to future exception
→ next future fails
~~~

---

# 七十四、这保持 Future Chain 的 Error Channel

~~~text
value
or
exception
~~~

都沿相同异步链传递。

---

# 七十五、`.then()` 与 `.then_wrapped()` 差别

`.then()`：

~~~text
input failed
→ func not called
→ exception propagated
~~~

`.then_wrapped()`：

~~~text
func always receives an available future
→ user can inspect success/failure
~~~

---

# 七十六、`.then()` 是 Happy-path Composition

伪逻辑：

~~~text
if input failed:
    output failed same exception
else:
    output = func(value)
~~~

---

# 七十七、`.then_wrapped()` 是 Full-state Composition

伪逻辑：

~~~text
output = func(
    future{value or exception})
~~~

---

# 七十八、为什么 Error Handling 需要 `then_wrapped`

因为它让 user callback 能：

- inspect exception；
- recover；
- transform exception；
- branch by status。

---

# 七十九、`handle_exception()` 最终也是基于 Wrapped Future

也就是：

~~~text
error combinator
~~~

不是另一套 runtime。

---

# 八十、`finally()` 也建立在 Future Composition 上

语义：

~~~text
原 Future 成功/失败
        ↓
运行 cleanup
        ↓
cleanup success
→ 保留原结果

cleanup failure
→ propagate cleanup exception
~~~

---

# 八十一、如果原 Future 和 Finally 都失败怎么办

源码支持：

~~~text
nested exception
~~~

把：

~~~text
callback exception
+
original exception
~~~

保留关系。

---

# 八十二、异步 Cleanup 不是 Destructor 替代品

`finally()` callback 本身也可以返回 Future。

因此 cleanup：

~~~text
可以异步
~~~

---

# 八十三、这对资源生命周期很重要

例如：

~~~text
async request
→ finally async release lease
~~~

比在同步 destructor 里：

~~~text
blocking wait
~~~

更适合 Reactor。

---

# 八十四、Broken Promise 是什么

如果 Promise 在：

~~~text
还有 attached Future/Continuation
~~~

时析构，

又没有：

~~~text
set_value
set_exception
~~~

下游不能永远等。

---

# 八十五、Promise Destructor 会主动制造 Terminal Failure

`promise_base::clear()`：

如果 `_task`：

~~~text
set state = broken_promise exception
schedule continuation
~~~

如果 `_future`：

~~~text
set future state = broken_promise
detach future
~~~

---

# 八十六、这是一条极重要的 Liveness 规则

> **Producer 消失不能让 Consumer 永久 Pending。**

---

# 八十七、错误设计

~~~text
promise destroyed
→ future stays pending forever
~~~

会导致：

- gate 永远关不掉；
- shutdown hang；
- semaphore credit leak；
- request leak。

---

# 八十八、正确设计

~~~text
producer died
→ explicit terminal error
~~~

让下游状态机继续收敛。

---

# 八十九、Broken Promise 是 Failure Completion

不是：

~~~text
异常情况之外的空状态
~~~

而是：

~~~text
合法 terminal state
~~~

---

# 九十、Future 被丢弃又是什么

源码对 `future` 标记：

~~~cpp
[[nodiscard]]
~~~

文档解释了两个风险。

---

# 九十一、第一个风险：异常无人观察

如果 Future 最终：

~~~text
failed
~~~

而调用者已经丢掉它，

Exception 无法被业务链处理。

---

# 九十二、Seastar 会做 Runtime Warning

`future_state` 析构时：

~~~text
failed state
→ check_failure()
→ report_failed_future
~~~

---

# 九十三、异常观察本身会改变 State

`get_exception()`：

~~~text
move exception out
→ mark state invalid
~~~

所以 destructor 知道：

~~~text
exception 已经被消费
~~~

---

# 九十四、这类似“must inspect error”

不是靠 GC，

而是通过：

~~~text
state consumption
~~~

判断。

---

# 九十五、第二个风险更严重：Background Work 无界

如果反复：

~~~cpp
launch_async();
~~~

然后把 Future 丢掉，

调用者无法知道：

~~~text
还有多少工作在跑
~~~

---

# 九十六、这会导致

- unbounded requests；
- memory growth；
- file/socket exhaustion；
- shutdown 不知道等谁；
- exception orphan。

---

# 九十七、所以 `[[nodiscard]]` 不只是代码风格

它在提示：

> **Future 是 async liveness handle。**

---

# 九十八、Background Task 仍需要 Accounting

源码建议：

~~~text
gate
semaphore
~~~

等机制。

---

# 九十九、Gate 解决什么

Gate 模型：

~~~text
enter
→ one outstanding async operation

leave
→ operation completed

close
→ reject new entry
   and wait count zero
~~~

---

# 一百、这和前面所有 Runtime 的 Quiescence 一样

~~~text
stop admission
+
drain outstanding
+
completion barrier
~~~

---

# 一百零一、Future 本身不是 Gate

Future 只表示：

~~~text
one async result
~~~

它不自动管理：

~~~text
一组 background work
~~~

---

# 一百零二、Future Chain 与 Gate 分工

Future：

~~~text
data/control dependency
~~~

Gate：

~~~text
group lifecycle accounting
~~~

---

# 一百零三、Semaphore 又是另一种约束

Semaphore：

~~~text
bound concurrency
~~~

Gate：

~~~text
track shutdown quiescence
~~~

有时一个 background task 同时需要两者。

---

# 一百零四、`future_state` 为什么会在析构时 Report Failed Future

`future_state::clear()`：

~~~cpp
if (has_result) {
    destroy value;
} else {
    _u.check_failure();
}
~~~

---

# 一百零五、Success Value 可以静默销毁

因为：

~~~text
caller不关心 value
~~~

通常不会破坏 correctness。

---

# 一百零六、Failure 不应静默销毁

因为：

~~~text
error signal lost
~~~

通常意味着 bug。

---

# 一百零七、这是 Result 与 Error 不对称设计

非常常见：

~~~text
unused value
→ okay

unused error
→ suspicious
~~~

---

# 一百零八、为什么 `future_state` 有 `result_unavailable`

它用于：

~~~text
结果曾经存在
但已经被提取/消费
~~~

并与真正：

~~~text
invalid object
~~~

区分。

---

# 一百零九、为什么要区分

因为：

~~~text
结果对象 destructor
~~~

和：

~~~text
防止重复 get/then
~~~

是不同问题。

---

# 一百一十、状态机里要区分“没有”与“已经消费”

这在很多 ownership API 里都很重要：

- optional empty；
- moved-from；
- consumed token；
- invalid handle。

---

# 一百一十一、Future 只能被消费一次

调用：

~~~text
get()
then()
then_wrapped()
~~~

本质都在：

~~~text
take/move state
~~~

---

# 一百一十二、这与 Rust Future/Result 的 Move Semantics 很相似

虽然 C++ 类型系统不同，

但 runtime 设计也尽量保证：

~~~text
one result
→ one consuming path
~~~

---

# 一百一十三、Promise `set_value` 为什么可以在 `get_future()` 前调用

初始：

~~~text
_state → _local_state
~~~

直接把 result 放 promise 本地。

以后：

~~~text
get_future()
~~~

把 ready state move 进 Future。

---

# 一百一十四、反过来也可以

先：

~~~text
get_future
~~~

此时：

~~~text
_state → future._state
~~~

再 `set_value`：

~~~text
直接写 future._state
~~~

---

# 一百一十五、Producer 不需要知道 Consumer 目前在哪个阶段

它只看：

~~~text
_state pointer
~~~

---

# 一百一十六、这是一种 Indirection-based State Relocation

Consumer topology 可以变化：

~~~text
no future
→ future
→ continuation
~~~

Producer API：

~~~text
set_value
~~~

不变。

---

# 一百一十七、为什么不使用 Heap Shared State

传统 `std::future` 常用：

~~~text
heap allocated shared state
~~~

然后：

~~~text
promise/future
→ shared-state pointer
~~~

---

# 一百一十八、Seastar 选择把 State 嵌入对象并迁移

收益：

- ready/local path 少一次 heap allocation；
- value cache locality 更好；
- continuation 本来就需要 allocation，可直接成为 state host。

---

# 一百一十九、代价

需要复杂维护：

~~~text
_state pointer rewiring
_future backpointer
move constructors
continuation storage
~~~

---

# 一百二十、这是一种“用复杂对象协议换 Heap Allocation”

在高频 async runtime 中很常见。

---

# 一百二十一、Continuation Allocation 什么时候不可避免

Future 未 ready 且调用 `.then()`：

~~~text
当前 stack 要结束
但 callback/state 必须继续活
~~~

需要某种：

~~~text
persistent storage
~~~

---

# 一百二十二、Continuation 就承担这个 Storage

所以 allocation 不只是 scheduler node，

还是：

~~~text
async stack frame
~~~

---

# 一百二十三、Future Chain 可以看成 Heap-allocated Stackless Frames

例如：

~~~text
read
→ parse
→ lookup
→ send
~~~

未 ready 点把后续：

~~~text
拆成 continuation frame
~~~

---

# 一百二十四、C++ Coroutine 只是另一种状态机生成方式

Coroutine compiler 把：

~~~text
局部变量 + resume point
~~~

装进 coroutine frame。

Seastar 手写 Future continuation：

~~~text
func + promise + state
~~~

本质类似。

---

# 一百二十五、区别是 Continuation 粒度

Future chain：

~~~text
每个 `.then`
→ 一个逻辑节点
~~~

Coroutine：

~~~text
整个 coroutine
→ 一个较大 frame
~~~

---

# 一百二十六、`task::waiting_task()` 为什么存在

Task 可以表达：

~~~text
我当前还依赖谁
~~~

用于：

- debug backtrace；
- stall diagnosis；
- task graph introspection。

---

# 一百二十七、Continuation-with-promise 的 `waiting_task()`

返回：

~~~text
output promise.waiting_task()
~~~

所以调试工具可以沿链：

~~~text
current continuation
→ downstream waiting task
~~~

---

# 一百二十八、这是 Async Stack Trace 的基础之一

同步 stack：

~~~text
CPU stack frames
~~~

异步 stack：

~~~text
heap task/continuation linkage
~~~

需要 runtime 显式记录。

---

# 一百二十九、为什么 Constructor 调 `task::make_backtrace()`

Continuation 建立时记录调试信息，

因为等未来真正运行时：

~~~text
原始 C++ call stack 已经不存在
~~~

---

# 一百三十、异步系统的 Observability 必须主动构造

不能指望：

~~~text
gdb backtrace
~~~

天然恢复整个 logical request chain。

---

# 一百三十一、Broken Promise 也会 Schedule Continuation

这点很关键。

Producer 析构：

~~~text
不只是写 exception
~~~

还必须：

~~~text
schedule waiting task
~~~

否则 exception 虽然已经写入 state，

consumer 仍然睡着。

---

# 一百三十二、State Change 与 Wakeup 必须同时发生

这和：

- condition variable；
- mailbox；
- SMP wakeup；

完全同构。

---

# 一百三十三、Future Ready Protocol

Producer：

~~~text
write result/exception
        ↓
make_ready()
        ↓
schedule waiting task
~~~

Consumer：

~~~text
task runs
        ↓
reads already-written state
~~~

---

# 一百三十四、为什么 Promise `_task` 只能有一个

普通 Future：

~~~text
single consumer
~~~

所以：

~~~text
one waiting continuation
~~~

足够。

---

# 一百三十五、这减少了 Synchronization

不需要：

~~~text
vector<callbacks>
lock
fanout
~~~

---

# 一百三十六、Shared Future 才需要多消费者语义

不同 abstraction，

不同成本。

---

# 一百三十七、Future Move 后为什么 Producer 仍正确

因为 Promise 不把 result 发送给：

~~~text
old object identity
~~~

而是：

~~~text
current _state pointer
~~~

Future move 会同步修 pointer。

---

# 一百三十八、这是 Handle Relocation 与 Underlying Obligation 分离

逻辑 obligation：

~~~text
eventually produce one T
~~~

不随 C++ variable 地址改变。

---

# 一百三十九、同类问题在 Runtime 到处存在

- registration handle move；
- coroutine frame move；
- intrusive owner move；
- file descriptor wrapper move。

只要外部保存 self address，

move 就需要协议。

---

# 一百四十、Why Future Move 是 Noexcept

因为移动正在重写：

~~~text
bidirectional ownership graph
~~~

如果中途 throw，

旧/新对象可能各保存一半 linkage。

---

# 一百四十一、所以 Future Types 强烈依赖 Noexcept Move

不仅 payload，

future/promise 自身也把 move 当：

~~~text
protocol transition
~~~

---

# 一百四十二、Future 被 Destroy 时发生什么

`future_base::~future_base()`：

~~~text
if still attached to promise
→ detach
~~~

Promise：

~~~text
_state = nullptr
_future = nullptr
~~~

---

# 一百四十三、如果 Future 被 Destroy 但 Producer 以后 Set Exception

Promise `set_exception_impl()`：

如果 `_state == nullptr`：

~~~text
report_failed_future(exception)
~~~

---

# 一百四十四、为什么不能再保存这个 Exception

Consumer Future 已经不存在。

没有任何 API 让用户以后来：

~~~text
clear/observe exception
~~~

所以只能立刻 report。

---

# 一百四十五、如果以后 Set Value 呢

`set_value()`：

~~~text
if get_state() != nullptr
    store
else
    ignore value
~~~

---

# 一百四十六、Value 与 Exception 再次不对称

Future 已丢：

~~~text
success result
→ can disappear

failure
→ warn/report
~~~

---

# 一百四十七、这反映 Runtime 的错误哲学

未处理错误：

~~~text
值得暴露
~~~

未使用成功值：

~~~text
通常可以接受
~~~

---

# 一百四十八、Cross-shard Future Completion

前一章 `smp_message_queue`：

target：

~~~text
execute Func
→ save result
→ respond
~~~

origin：

~~~text
wi->complete()
→ promise.set_value/exception
~~~

---

# 一百四十九、Future Runtime 与 SMP Runtime 在 Origin 汇合

SMP 只负责：

~~~text
把 result transport 回 origin
~~~

真正 continuation wakeup：

~~~text
仍由 Promise/Future subsystem
~~~

完成。

---

# 一百五十、这形成层次

~~~text
SMP Transport
        ↓
origin promise completion
        ↓
Future continuation scheduler
        ↓
next local task
~~~

---

# 一百五十一、远端 RPC Completion 不是直接调用 Caller Lambda

而是：

~~~text
completion queue
→ promise
→ scheduler
→ continuation
~~~

---

# 一百五十二、这让 Transport 与 Control-flow 解耦

SMP 不需要知道：

~~~text
用户后面 then 了什么
~~~

Future 不需要知道：

~~~text
result 是本地 I/O 还是 remote shard 回来的
~~~

---

# 一百五十三、异步 Runtime 最重要的解耦之一

Producer：

~~~text
只负责 resolve promise
~~~

Consumer：

~~~text
只负责 compose future
~~~

---

# 一百五十四、为什么 `.then()` 返回的始终还是 Future

Callback 可以返回：

~~~text
T
future<T>
void
future<>
~~~

`futurize` 把它们统一。

---

# 一百五十五、如果 Callback 返回普通 T

Runtime 包成：

~~~text
ready future<T>
~~~

---

# 一百五十六、如果返回 Future<T>

直接：

~~~text
flatten
~~~

避免：

~~~text
future<future<T>>
~~~

---

# 一百五十七、这就是 Monadic Composition

从系统角度：

~~~text
async step
→ async step
→ async step
~~~

可以统一连接。

---

# 一百五十八、为什么 Flatten 很重要

否则每层都要：

~~~text
.then([](future<future<T>>...) ...)
~~~

控制流迅速失控。

---

# 一百五十九、Future Runtime 是控制流 Algebra

它把：

- value；
- async value；
- exception；

统一成一种可组合接口。

---

# 一百六十、但性能仍取决于路径

Ready：

~~~text
inline fast path
~~~

Pending：

~~~text
allocate continuation
→ schedule later
~~~

两者成本差很多。

---

# 一百六十一、所以 Benchmark Future 不能只测一个 Case

至少分：

~~~text
ready chain
pending chain
cross-shard completion
I/O completion
~~~

---

# 一百六十二、Ready Chain 的风险：长 Inline Chain

如果每一步都 ready：

~~~text
then
→ inline
→ then
→ inline
~~~

可能增加：

- current task duration；
- stack depth；
- cooperative latency。

---

# 一百六十三、Pending Chain 的风险：Allocation + Scheduling

每个 suspend point：

~~~text
continuation allocation
+
scheduler enqueue
~~~

---

# 一百六十四、两种路径没有免费的午餐

Fast path 优化：

~~~text
latency
~~~

但可能延长单 task run。

Scheduled path：

~~~text
fairness / decoupling
~~~

但增加 overhead。

---

# 一百六十五、不要把“Always Async”当天然正确

有些框架刻意：

~~~text
callback always next tick
~~~

Seastar release Future 不是这种语义。

---

# 一百六十六、这意味着调用者需要知道 Reentrancy/Inline Execution

例如：

~~~cpp
auto f = make_ready_future<>();
f.then(callback);
~~~

callback 可能：

~~~text
在当前函数返回前执行
~~~

---

# 一百六十七、Inline Completion 会影响哪些设计

- mutex/lock assumptions；
- recursion；
- object lifetime；
- callback reentrancy；
- latency accounting。

---

# 一百六十八、因此 Async API 需要明确 Completion Semantics

至少要问：

~~~text
ready callback inline?
or
always scheduled?
~~~

---

# 一百六十九、Seastar Future 的答案

release build 普通 `.then()`：

~~~text
ready
→ inline

not ready
→ continuation task
~~~

---

# 一百七十、这是 Hybrid Execution Model

它用 fast path 优化 ready case，

用 scheduler 管 pending case。

---

# 一百七十一、Urgent Scheduling 又增加第三种 Path

某些 internal forwarding：

~~~text
urgent state
→ schedule_urgent
~~~

所以真正 runtime path 至少：

~~~text
inline
normal scheduled
urgent scheduled
~~~

---

# 一百七十二、为什么 Internal API 需要 Urgent

某些 state forwarding 希望：

~~~text
尽快完成 dependency propagation
~~~

但不能把用户所有 continuation 都默认 urgent。

---

# 一百七十三、Urgent Queue 是 Scheduler Policy，不是 Future State

Future 只表达：

~~~text
ready
~~~

Promise 可以选择：

~~~text
normal wake
urgent wake
~~~

---

# 一百七十四、State 与 Scheduling Policy 再次分层

相同 result：

~~~text
可以用不同 runnable priority 发布
~~~

---

# 一百七十五、Promise Destructor 的 Broken Promise 是正常调度

即使是 lifecycle failure，

也：

~~~text
schedule continuation normally
~~~

让下游：

~~~text
以标准 Future failure 方式处理
~~~

---

# 一百七十六、这比特殊 Shutdown Callback 更统一

不用：

~~~text
if producer died
call special handler
~~~

而是：

~~~text
same async chain
receives broken_promise
~~~

---

# 一百七十七、统一 Failure Channel 降低组合复杂度

I/O error：

~~~text
exception future
~~~

remote error：

~~~text
exception future
~~~

broken producer：

~~~text
exception future
~~~

用户逻辑可以统一处理。

---

# 一百七十八、机器人系统中的对应关系

例如：

~~~text
request camera exposure change
→ future<ack>
~~~

如果 driver object 在 ACK 前被销毁，

不要：

~~~text
让 future 永久 pending
~~~

而应该：

~~~text
complete with cancellation/broken-session error
~~~

---

# 一百七十九、这样 Supervisor 才能收敛

否则 shutdown：

~~~text
await ack
~~~

会永久挂住。

---

# 一百八十、Future 是 Liveness Protocol 的一部分

它不仅传 value。

它还必须保证：

> **每个已承诺的异步操作最终到达 success 或 failure terminal state。**

---

# 一百八十一、这个原则和 Promise Algebra 一样重要

异步系统最危险的不是：

~~~text
error
~~~

而是：

~~~text
永远不完成
~~~

---

# 一百八十二、永远 Pending 会造成

- leaked gate count；
- leaked semaphore credit；
- shutdown hang；
- request timeout chain；
- stale control state。

---

# 一百八十三、所以 Broken Promise 是一种 Quiescence Mechanism

它把：

~~~text
producer disappearance
~~~

转换成：

~~~text
consumer-visible terminal event
~~~

---

# 一百八十四、为什么 Gate 比“保存所有 Future Vector”更好

如果后台任务很多：

~~~text
std::vector<future<>>
~~~

会复杂维护：

- completed removal；
- exception processing；
- shutdown join。

Gate 只维护：

~~~text
count + closed state
~~~

---

# 一百八十五、但 Gate 不传 Result

所以：

~~~text
Future
→ per-operation result

Gate
→ group lifetime
~~~

---

# 一百八十六、这是两个 orthogonal dimensions

和前面：

~~~text
SMP service group
→ concurrency/backpressure
~~~

又是第三个维度。

---

# 一百八十七、一个完整 Background RPC 可能同时需要

~~~text
SMP credit
+
Future
+
Gate
~~~

分别解决：

~~~text
how many?
what result?
when all done?
~~~

---

# 一百八十八、这就是成熟 Runtime 的 Mechanism Composition

不要试图用：

~~~text
一个 Future 类型
~~~

同时解决所有资源问题。

---

# 一百八十九、Future 与 Coroutine 的关系

Coroutine 的 `co_await future` 最终仍要：

~~~text
dependency not ready
→ suspend coroutine
→ promise ready
→ schedule resume task
~~~

---

# 一百九十、底层问题没变

只是 continuation 不再是：

~~~text
user lambda object
~~~

而是：

~~~text
coroutine resume point/frame
~~~

---

# 一百九十一、所以学 Future Runtime 仍然很有价值

因为它揭示的是：

~~~text
async dependency
→ runnable computation
~~~

这个底层桥梁。

---

# 一百九十二、Future 的完整对象状态图

~~~text
              promise created
                    |
                    v
         +---------------------+
         | promise._local_state|
         +---------------------+
                    |
              get_future()
                    |
                    v
         +---------------------+
         |   future._state     |
         +---------------------+
          ^                 |
          |                 | then()
          |                 | not ready
 promise._state             v
 points here       +----------------------+
                   | continuation._state  |
                   | func                 |
                   | output promise       |
                   +----------------------+
                             ^
                             |
                      promise._state
                      promise._task
                             |
                       set_value/error
                             |
                             v
                         schedule
                             |
                             v
                     run_and_dispose
                             |
                    resolve output promise
                             |
                         delete this
~~~

---

# 一百九十三、Move 时的指针修复图

Future move：

~~~text
Promise._future
   |
   v
old Future

move
   |
   v

Promise._future
   |
   v
new Future

Promise._state
   |
   v
new Future._state
~~~

---

# 一百九十四、Promise move：

~~~text
Future._promise
   |
   v
old Promise

move
   |
   v

Future._promise
   |
   v
new Promise
~~~

如果 state 在 promise-local：

~~~text
_state
old._local_state
→
new._local_state
~~~

---

# 一百九十五、Continuation Ready Path

~~~text
producer
  |
  | set_value
  v
write continuation._state
  |
  | make_ready
  v
promise._task
  |
  | exchange nullptr
  v
schedule(task)
  |
  v
Reactor queue
  |
  v
continuation.run_and_dispose()
  |
  +--> if input failed:
  |      output promise exception
  |
  +--> if input value:
  |      invoke func
  |      futurize result
  |      satisfy output promise
  |
  v
delete continuation
~~~

---

# 一百九十六、Ready Fast Path

~~~text
future already has value
        |
        v
then(func)
        |
        v
no continuation allocation
        |
        v
func invoked inline
        |
        v
return ready/pending future
~~~

---

# 一百九十七、为什么 Debug Build 会不同

源码：

~~~text
debug build may schedule ready futures
~~~

用于更强的调试一致性/检查。

因此：

> **性能路径分析必须以具体 build configuration 为准。**

---

# 一百九十八、不要拿 Debug 行为推导 Release Latency

同理：

- asserts；
- tracing；
- scheduling；
- sanitizer；

都可能改变 runtime path。

---

# 一百九十九、源码作者必须守住的十二条不变量

第一：

> **一个 Future/Promise pair 只有一个逻辑 result state，允许 storage relocation，但任意时刻只能有一个 active storage。**

第二：

> **Promise 的 `_state` 必须始终指向当前有效 storage：promise-local、future 或 continuation。**

第三：

> **Future/Promise move 必须修复所有反向指针和 self-relative state pointer。**

第四：

> **Future 尚未 ready 时注册 continuation，必须把 output promise、callback 和 input state 绑定成稳定的 task object。**

第五：

> **Promise publish result 后，若存在 waiting continuation，必须同时 schedule；写 state 而不唤醒 task 会造成永久 pending。**

第六：

> **Continuation 是 one-shot scheduler object，terminal execution 后必须释放，并且异常要转换回 output promise。**

第七：

> **Ready Future 的 release fast path 可以 inline 执行 callback；不能假设所有 `.then()` 都经过 scheduler。**

第八：

> **普通 Future 是 single-consumer；禁止 copy 是语义约束，不只是性能选择。**

第九：

> **Producer 在未完成 Promise 时死亡必须产生 broken-promise terminal failure，而不是让 consumer 永久等待。**

第十：

> **未观察的 failed Future 必须可诊断；`[[nodiscard]]` 与 runtime failed-future report 都在保护错误可见性。**

第十一：

> **Future 只追踪单个异步结果；background-work 的总体生命周期仍需要 gate，容量仍可能需要 semaphore。**

第十二：

> **异步 Runtime 的核心不是“没有线程”，而是把原本保存在 blocking stack 中的 continuation、state、error 与 lifetime obligation 显式对象化。**

---

# 二百、对机器人 Runtime 的具体映射

假设：

~~~text
Camera driver
→ set exposure
→ wait sensor ACK
→ update state
~~~

不要：

~~~text
control thread blocks waiting ACK
~~~

可以：

~~~text
future<ack>
→ continuation task
~~~

---

# 二百零一、但要同时明确三件事

结果：

~~~text
Future
~~~

并发上限：

~~~text
Semaphore
~~~

shutdown：

~~~text
Gate
~~~

---

# 二百零二、一个更完整的模式

~~~text
gate.enter
    |
semaphore.wait
    |
launch async command
    |
future<ack>
    |
then
    |
release semaphore
    |
gate.leave
~~~

---

# 二百零三、Driver 被关闭时

所有未完成 promise：

~~~text
complete with cancellation/broken-session
~~~

而不是：

~~~text
forget them
~~~

---

# 二百零四、这能保证 Shutdown 收敛

~~~text
close admission
→ fail/cancel pending producers
→ futures become terminal
→ continuations drain
→ gate count reaches zero
~~~

---

# 二百零五、最终心智模型

不要把 Seastar Future 理解成：

~~~text
一个异步 value holder
~~~

更准确地说：

> **它是一条可移动的单消费者 dependency edge：逻辑 result state 会根据 consumer 阶段在 promise、future 与 continuation 中迁移；当 dependency 尚未完成时，continuation 本身被物化成 Reactor task；当 dependency 已经完成时，release fast path可以直接 inline 推进；broken-promise、exception state 与 `[[nodiscard]]` 则保证异步链不会因为 producer 消失或 caller 丢弃 handle 而无声失控。**

如果只记一句：

> **Seastar Future 的核心不是“以后拿到一个值”，而是把“值在哪里、谁在等、什么时候变 runnable、错误如何继续传播、producer 消失后怎样终止等待”全部编码成一个显式、可调度、可迁移的对象协议。**
