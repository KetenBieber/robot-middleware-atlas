# Work、Lifetime 与 Cancellation：为什么 Queue 空了不代表异步系统已经结束

固定源码版本：`8806a6803cde7054c3049d3666d3ec36786568c5`。

异步系统必须分别管理 runtime liveness、operation lifetime、payload lifetime 与 object lifetime。

## outstanding work 是 Liveness Lease

Scheduler 中：

~~~cpp
void work_started() {
  ++outstanding_work_;
}

void work_finished() {
  if (--outstanding_work_ == 0)
    stop();
}
~~~

它统计的不是 ready queue 长度，而是系统仍承诺未来可能产生 completion 的逻辑工作。

## executor_work_guard 为什么存在

某些 service 需要即使当前没有 operation，也让 `io_context::run()` 保持存活，等待未来其他线程 post work。

Work guard 本质上就是一个显式 liveness lease。

## Buffer Lifetime 与 Handler Lifetime 不同

典型错误：

~~~cpp
std::string msg = make_message();
async_write(socket, buffer(msg), handler);
// msg is destroyed here
~~~

异步 operation 可能稍后才真正访问 buffer。

所以：

~~~text
payload storage lifetime
>=
async operation lifetime
~~~

Runtime 保存 handler，并不意味着外部 payload 自动被延长生命周期。

## Operation Object 是一次异步事务的 Ownership Capsule

具体派生 operation 通常可以同时保存：

~~~text
handler
allocator
buffers / state
error / bytes
~~~

完成时再通过 erased function pointer 恢复具体类型。

## 为什么 Callback 前先让 Runtime State 稳定

成熟 runtime 通常尽量：

~~~text
capture/move handler
↓
release scheduler/reactor internal state
↓
invoke user handler
~~~

因为 handler 可能重新提交 operation，甚至销毁自己所属对象。

## Cancellation 不是 Thread Preemption

必须区分：

~~~text
pending in userspace queue
pending in kernel/reactor
already executing handler
~~~

前两者可以标记/请求 abort；已经执行的任意 C++ handler 不能被 runtime 安全强杀。

所以 cancellation 应建模成状态转换，而不是“终止线程”。

## Close、Completion、Reclamation 是三个阶段

Socket close 可能导致 pending operation 以 `operation_aborted` 完成。

因此：

~~~text
logical close
physical fd close
completion delivery
object reclamation
~~~

可以发生在不同时间。

## 可迁移原则

1. Runtime liveness 应显式计数。
2. Payload lifetime 必须覆盖异步访问窗口。
3. Operation object 可以封装一次异步事务的资源。
4. Cancellation 是状态机，不是抢占。
5. Close、completion、reclamation 必须分层。