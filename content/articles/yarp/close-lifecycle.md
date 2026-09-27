# Interrupt 与 Close：连接并发、线程退出和名字注销

YARP 的关闭路径要同时处理 listener、多个 Unit、阻塞 Protocol、用户 Reader、后台 writer 和 Name Server 注册。正确目标不是“socket 最终被系统回收”，而是 close 返回后没有线程再访问 PortCore 成员。

本文源码固定为 `robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`。本节直接摘录 `Port::close`、`PortCore::closeMain`、`PortCore::closeUnits` 与 `PortCoreOutputUnit::closeMain` 的相关连续实现。

## 功能语义与对象图

`interrupt()` 服务于仍然活着的 Port：唤醒阻塞读写，让应用退出等待。`close()` 终止服务并回收连接、线程和注册。析构函数是最后保险，必须安全调用 close，但不适合暴露复杂错误处理。

```text
Port facade
  -> PortCore
       |-- Face/listener -> accept thread
       |-- InputUnit[]  -> Protocol -> stream -> reader callback
       |-- OutputUnit[] -> Protocol -> stream -> pending writer
       |-- NameClient registration
       `-- reporter / administrative reader
```

PortCore 是协调者，却不能在一把全局锁下逐一关闭这些对象。Unit 退出时可能回调 PortCore，Protocol 的 interrupt 会唤醒其他线程，Name Server 调用又可能等待网络。关闭过程必须把“切断共享对象关系”和“执行阻塞动作”分成两个阶段。

## 关闭状态机

```text
Port::interrupt()
  -> PortCore::interrupt(): m_interrupted = true
     -> 若设置了 interruptable 且 reader 正在配置，持 m_stateMutex 调 reader 空消息

Port::close()
  -> finishReading()
  -> finishWriting()                 最多轮询等待约 3 秒
  -> PortCore::close() / closeMain()
       -> m_finishing = true         在 m_stateMutex 下设置
       -> 请求拆除输入与输出连接   每轮只在锁内查找目标，锁外发起操作
       -> m_closing = true
       -> 向本机 listener 地址写消息，随后 join server thread
       -> close/join/delete units   仅 server thread finished 后
       -> 关闭 Face、通知 Reader、注销名字
  -> core.join()
```

这里的状态图是固定版本调用顺序的摘要。`interrupt()` 主要设置标志并在特定条件下调用 reader；完整资源收口由 `close()` 的另一条路径完成。源码没有先把所有 Unit 复制成共享句柄、清空 registry，再统一锁外关闭的步骤，因此不能将这种常见的两阶段设计写成当前实现。


```cpp
void Port::close()
{
    if (!owned) {
        return;
    }

    PortCoreAdapter& core = IMPL();
    core.finishReading();
    core.finishWriting();
    core.close();
    core.join();
    core.active = false;

    // In fact, open flag means "ever opened", so don't reset it
    // core.setOpened(false);
}
```

`Port` façade 先处理适配器上的读和写，再调用 PortCore 关闭并 join。`finishWriting()` 不持有 PortCore 的 Unit registry；其最多约三秒的轮询是 `PortCoreAdapter` 对仍在发送状态的等待策略。若一次机械臂状态广播卡在慢连接，调用者可观察到 close 先等待写状态变化，超时后记录错误并继续进入关闭流程。

接着看 `PortCore::closeMain` 的真实实现：

```cpp
void PortCore::closeMain()
{
    yCITrace(PORTCORE, getName(), "closeMain");

    {
        // Critical section
        std::lock_guard<std::mutex> lock(m_stateMutex);

        // We may not have anything to do.
        if (m_finishing || !(m_running.load() || m_manual)) {
            yCITrace(PORTCORE, getName(), "closeMain - nothing to do");
            return;
        }

        yCITrace(PORTCORE, getName(), "closeMain - Central");

        // Move into official "finishing" phase.
        m_finishing = true;
        yCIDebug(PORTCORE, getName(), "now preparing to shut down port");
    }
```

函数在锁内检查是否正在 finishing，以及 server/manual 模式是否存在；随后置位 `m_finishing` 并离开该临界区。后续断连不是复制所有 Unit 句柄：它逐轮在锁内扫描 `m_units` 找到一个 Route，解锁后调用网络断连或 `removeUnit`，再重新扫描。这样的次序使网络操作不在这个查找锁内执行，但其准确保护范围仍应按每个调用点逐一分析。

## listener 停止与 Unit 回收的真实顺序

输入连接先请求对端协商断开，输出连接则从本地拆除。完成这些管理步骤之后，PortCore 才把 server thread 置为 closing，通过 listener 向自己的地址写入消息，使阻塞的 server loop 有机会返回；随后 join server thread。server thread 退出时设置 `m_finished`。只有这个状态成立后，`closeUnits()` 才逐个调用 Unit 的 close、join、delete。

接着看 `PortCore::closeMain` 的真实实现：

```cpp
    bool stopRunning = m_running.load();

    // If the server thread is still running, we need to bring it down.
    if (stopRunning) {
        // Let the server thread know we no longer need its services.
        m_closing.store(true);

        // Wake up the server thread the only way we can, by sending
        // a message to it.  Then join it, it is done.
        if (!m_manual) {
            OutputProtocol* op = m_face->write(m_address);
            if (op != nullptr) {
                op->close();
                delete op;
            }
            join();
        }

        // We should be finished now.
        yCIAssert(PORTCORE, getName(), m_finished.load());

        // Clean up our connection list. We couldn't do this earlier,
        // because the server thread was running.
        closeUnits();
```

这里所谓 server “被唤醒”是向 listener 地址写入本地消息并让阻塞循环重新运行；它不等于 OS 已分配 CPU。`join()` 等待 server thread 结束，`closeUnits()` 再处理每条连接的 Unit。慢输入 Unit 或其用户 Reader 若迟迟不响应关闭，Unit join 会延长 Port close；这是连接线程退出协议的代价。

接着看 `PortCore::closeUnits` 的真实实现：

```cpp
void PortCore::closeUnits()
{
    // Empty the PortCore#units list. This is only possible when
    // the server thread is finished.
    yCIAssert(PORTCORE, getName(), m_finished.load());

    // In the "finished" phase, nobody else touches the units,
    // so we can go ahead and shut them down and delete them.
    for (auto& i : m_units) {
        PortCoreUnit* unit = i;
        if (unit != nullptr) {
            yCIDebug(PORTCORE, getName(), "closing a unit");
            unit->close();
            yCIDebug(PORTCORE, getName(), "joining a unit");
            unit->join();
            delete unit;
            yCIDebug(PORTCORE, getName(), "deleting a unit");
            i = nullptr;
        }
    }

    // Get rid of all our nulls.  Done!
    m_units.clear();
}
```

这里的 raw pointer 由 PortCore 明确关闭、join 并 `delete`。不会因为 `vector` 清空自动析构 Unit。代码注释给出的安全前提是 server thread 已 finished 且此阶段无人再访问 Unit 表，所以线程汇合是裸指针回收前的关键边界。

接着看 `PortCore::closeMain` 的真实实现：

```cpp
    // There should be no other threads at this point and we
    // can stop listening on the network.
    if (m_listening.load()) {
        yCIAssert(PORTCORE, getName(), m_face != nullptr);
        m_face->close();
        delete m_face;
        m_face = nullptr;
        m_listening.store(false);
    }

    // Check if the client is waiting for input.  If so, wake them up
    // with the bad news.  An empty message signifies a request to
    // check the port state.
    if (m_reader != nullptr) {
        yCIDebug(PORTCORE, getName(), "sending end-of-port message to listener");
        StreamConnectionReader sbr;
        m_reader->read(sbr);
        m_reader = nullptr;
    }

    // We may need to unregister the port with the name server.
    if (stopRunning) {
        std::string name = getName();
        if (name != std::string("")) {
            if (m_controlRegistration) {
                NetworkBase::unregisterName(name);
            }
        }
    }
```

Face 在 server thread 结束后关闭和删除；Reader 收到空消息后被清空；名字仅在 `stopRunning` 且启用了 control registration 时注销。Name Server 注销是否成功不改变此函数后续把 finishing 标志复位的本地清理路径。

## OutputUnit 的协议对象与 Unit 是两种生命周期

PortCore 表里仍是需要显式删除的 `PortCoreUnit*`；OutputUnit 内部的协议成员 `op` 则是 `std::shared_ptr<OutputProtocol>`。固定版本的 `closeMain()` 会尝试取得一份局部 `shared_ptr` 并调用 interrupt，再置 closing、发 semaphore 并 join worker；`closeBasic()` 也会在局部副本上做断连、等待可选回复和 close，之后 reset 成员。这说明作者显式区分 Unit 管理与 Protocol 引用寿命，但它本身不能证明任意并发读同一 `shared_ptr` 成员与 reset 都安全；要判断并发是否可能，还必须核对每个调用点及其锁/线程序。


```cpp
void PortCoreOutputUnit::closeMain()
{
    if (finished) {
        return;
    }

    yCIDebug(PORTCOREOUTPUTUNIT, getName(), "closing");

    if (running) {
        // give a kick (unfortunately unavoidable)

        // Local copy so that even if another thread concurrently clears
        // `op` inside closeBasic(), the object we call interrupt() on
        // (if any) remains valid for the duration of this call.
        std::shared_ptr<OutputProtocol> localOp = op;
        if (localOp) {
            localOp->interrupt();
        }

        closing = true;
        phase.post();
        activate.post();
        join();
    }

    yCIDebug(PORTCOREOUTPUTUNIT, getName(), "internal join");

    closeBasic();
    running = false;
    closing = false;
    finished = true;

    yCIDebug(PORTCOREOUTPUTUNIT, getName(), "closed");
}
```

拷贝后的 `localOp` 能在其有效期间增加控制块强引用计数，从而让 Protocol 对象的析构至少等到这份副本退出作用域；它不负责保护 `op` 这个 shared_ptr 成员本身的并发读写。`phase.post()` 与 `activate.post()` 是 Unit 自己的 semaphore 通知，`join()` 才等 worker 线程退出。这个顺序还意味着这里不能把 semaphore 通知、worker 可运行和 worker 已经结束当成同一事件。

## 幂等性与恰好一次完成通知

析构、用户 close、网络错误和对端管理命令都可能触发清理。状态转换必须让只有第一个调用者取得实际 teardown 权；后续调用观察 CLOSING/CLOSED 并等待或快速返回。

后台 write 的 completion callback 在取消时也必须恰好执行一次。重复 callback 会导致 buffer pool double release，缺失 callback 会永久占用槽位。

## 名字注销不能阻止本地回收

Name Server 不可用时，unregister 可能失败。PortCore 仍必须释放本地 socket 和线程，不能让控制面 RPC 阻塞完整关闭。失败可记录并交给名字服务租约/过期机制清理陈旧条目。

## 关闭后的证明条件

应能检查：listener 已关闭、unit registry 为空、所有 worker 已 join、无 in-flight writer/tracker、Reader 不再执行、名字注销已成功或明确记录失败、重复 close 不产生新副作用。

压力测试要让 connect/disconnect、write、interrupt 和 close 在受控 barrier 上并发交错，并使用 ASan/TSan 检测 UAF 与数据竞争。

## 两阶段关闭的 C++ 骨架


```cpp
void PortCore::Close() noexcept {
    std::vector<std::shared_ptr<Unit>> units;
    std::shared_ptr<Face> face;
    {
        std::lock_guard lock(mu_);
        if (state_ == State::Closed) return;
        if (state_ == State::Closing) {
            // 只记录需要等待；不能在这把锁下等待完成。
            return;
        }
        state_ = State::Closing;
        face = std::exchange(face_, {});
        units.assign(units_.begin(), units_.end());
        units_.clear();
    }

    if (face) face->Interrupt();
    for (auto& unit : units) unit->Interrupt();
    for (auto& unit : units) unit->Join();
    UnregisterBestEffort();
    PublishClosed();
}
```

锁内只改变 registry 和可见状态，锁外才 interrupt、join 和访问网络。第二个关闭者若需要等待，应在解锁后使用独立条件变量；如果在 `mu_` 下等待，首个关闭者可能无法取得同一把锁发布 `Closed`，造成死锁。

局部 `shared_ptr` 是一次并发租约：registry 删除对象后，新操作无法再找到它；已经取得租约的操作仍能完成。最后一个租约释放才析构。管理路径适合这种模式，高频实时数据面则可能使用固定槽位、epoch 或严格线程归属来避免原子计数竞争。

## 恰好一次完成门

析构、网络错误和用户 close 可能同时取消后台写。完成回调可用原子门收敛：


```cpp
class Completion {
 public:
  void Finish(Status status) noexcept {
    bool expected = false;
    if (!done_.compare_exchange_strong(expected, true)) return;
    callback_(status);
  }
 private:
  std::atomic<bool> done_{false};
  std::function<void(Status)> callback_;
};
```

原子门保证 callback 最多一次，但 `Completion` 本身仍必须活到所有竞争路径退出，通常由待发送任务共享持有。若回调可能抛异常，必须在工作线程或 C ABI 边界捕获，不能让清理路径终止进程。

## 析构、超时与自连接问题

C++ 析构不能把网络注销失败作为异常抛出；栈展开期间再次抛异常会触发 `std::terminate`。需要调用方处理的失败应由显式 `Close()` 返回，析构只做 best-effort 回收和记录。

线程也不能 join 自身。若 close 从 Unit 回调内触发，应只请求停止，把 join 交给外层协调者或后台 reaper。close 超时返回时必须说明对象仍由谁持有，不能让调用方误以为内存已可释放。

## 性能与工程取舍

关闭不是吞吐热路径，却决定服务能否可靠重启。先向所有 Unit 广播 interrupt，再逐个 join，可让多个连接的退出等待重叠；逐个 interrupt 后立即 join 会把最坏等待时间相加。

| 设计 | 优点 | 风险与代价 |
|---|---|---|
| registry 快照后锁外关闭 | 避免重入死锁 | 在途对象需要稳定所有权 |
| `shared_ptr` 租约 | UAF 防护直观 | 原子计数与所有权环风险 |
| 广播 interrupt 后 join | 多连接等待可重叠 | Unit 必须可靠响应中断 |
| 注销 best-effort | 本地回收不依赖网络 | 目录可能短时保留陈旧名字 |
| 幂等状态机 | 多入口清理安全 | 后续调用需明确等待或返回 |

## 可迁移设计与最小复刻

先关闭入口，再唤醒阻塞点，再等待执行上下文退出，最后释放被它们访问的对象；共享表只在锁内切断关系，阻塞操作在锁外执行；完成通知必须恰好一次；远端控制面失败不能破坏本地回收。

最小复刻可让 listener、InputUnit 和 OutputUnit 分别阻塞在 accept、read 和 completion wait，然后验证一次 close 能全部唤醒并 join。随后加入并发第二次 close、回调内 close、名字服务超时和半握手连接。只有这些交错都满足关闭后的证明条件，生命周期实现才算完整。
