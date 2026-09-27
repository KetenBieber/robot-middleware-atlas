# 写入数据路径：PortWriter 序列化与多连接扇出

机器人控制进程每 10 ms 发送一次关节状态时，调试器、记录器和控制器可能同时订阅。最朴素的写法是逐个调用连接并等待结果；当记录器停读、TCP 发送缓冲区填满时，写线程可能在某个连接上阻塞，10 ms 控制周期就直接变成长周期。YARP 还要回答：一条消息如何跨多条连接，后台写究竟保留谁的对象，busy 的连接收到的是新值还是排队的旧值？这些问题都能从固定版本的 `PortCore::sendHelper()` 与 `PortCoreOutputUnit::send()` 一起追出来。

本文中的源码固定于 `robotology/yarp` 提交 `91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`。

## 功能语义：一次逻辑写入包含多个独立结果

Port 的逻辑消息只有一份，但每个 OutputUnit 拥有不同连接状态。固定版公开 API 只返回 `bool`，并不返回每 Route 报告；`sendHelper()` 中 `all_ok` 只会在等待模式发现 Unit 已 finished 时置为 false，RPC 还会检查 `gotReply`。后台模式不等待 Unit 完成，busy 的单条连接被跳过也不会形成逐 Route 失败列表。因此 `true` 不能解释成“所有接收者都消费了这条消息”。需要完整结果的机器人应用应额外携带序号并在业务层统计确认、超时与丢帧。

```text
logical sequence 42
  |-- route A/tcp          -> sent
  |-- route B/tcp+ack      -> timeout
  |-- route C/compressed   -> encode failed
  `-- route D/background   -> accepted, completion later
```

公开 bool 必须说明是“至少一个接受”“全部完成”还是“本地调度成功”。更丰富的诊断应通过 per-route counter、PortReport 或 completion result 提供。

## 公开调用链

```text
Port::write(PortWriter)
  -> PortCore::send
  -> PortCore::sendHelper
       -> 持 m_stateMutex 遍历 m_units
       -> for each unit: unit.send(writer, callback)
            -> synchronous sendHelper
               OR store work and wake unit thread
            -> PortWriter::write(ConnectionWriter)
            -> OutputProtocol / Protocol::write
            -> Carrier writes framed bytes to stream
```

PortWriter 的 `write()` 可能为每条连接调用一次。因此它应是可重复读取的 const-like 序列化命令，不能第一次调用后把内部数据破坏掉，除非框架明确使用缓存表示。

`m_stateMutex` 不是只保护一份短暂快照：本提交在它仍然加锁时逐个调用 `unit->send()`。要理解慢连接的后果，必须沿这个锁看完整函数，而不能只看 `fan-out` 是 `O(C)`。

接下来对照固定版本的实际代码：

```cpp
bool PortCore::sendHelper(const PortWriter& writer,
                          int mode,
                          PortReader* reader,
                          const PortWriter* callback)
{
    if (m_interrupted || m_finishing) {
        return false;
    }

    bool all_ok = true;
    bool gotReply = false;
    int logCount = 0;
    std::string envelopeString = m_envelope;

    writer.onCommencement();

    std::lock_guard<std::mutex> lock(m_stateMutex);
    if (m_finished.load()) {
        return false;
    }

    m_packetMutex.lock();
    PortCorePacket* packet = m_packets.getFreePacket();
    yCIAssert(PORTCORE, getName(), packet != nullptr);
    packet->setContent(&writer, false, callback);
    m_packetMutex.unlock();

    for (auto* unit : m_units) {
        if ((unit != nullptr) && unit->isOutput() && !unit->isFinished()) {
            bool log = (!unit->getMode().empty());
            if (log) {
                logCount++;
            }
            bool ok = (mode == PORTCORE_SEND_NORMAL) ? (!log) : (log);
            if (!ok) {
                continue;
            }
            bool waiter = m_waitAfterSend || (mode == PORTCORE_SEND_LOG);
            m_packetMutex.lock();
            packet->inc();
            m_packetMutex.unlock();

            bool gotReplyOne = false;
            void* out = unit->send(writer,
                                   reader,
                                   (callback != nullptr) ? callback : (&writer),
                                   reinterpret_cast<void*>(packet),
                                   envelopeString,
                                   waiter,
                                   m_waitBeforeSend,
                                   &gotReplyOne);
            gotReply = gotReply || gotReplyOne;
            if (out != nullptr) {
                m_packetMutex.lock();
                (static_cast<PortCorePacket*>(out))->dec();
                m_packets.checkPacket(static_cast<PortCorePacket*>(out));
                m_packetMutex.unlock();
            }
            if (waiter && unit->isFinished()) {
                all_ok = false;
            }
        }
    }

    m_packetMutex.lock();
    packet->dec();
    m_packets.checkPacket(packet);
    m_packetMutex.unlock();

    if (m_waitAfterSend && reader != nullptr) {
        all_ok = all_ok && gotReply;
    }
    return all_ok;
}
```

源码身份：`PortCore::sendHelper`（固定提交）。摘录只删去诊断日志与末尾 tracing；每条连接前的引用计数、Unit 调用、锁范围和返回条件保留原控制流。

这个入口还有一个与慢连接不同的并发边界：它在获取 `m_stateMutex` 之前先读 `m_interrupted` 和 `m_finishing`。判断为 false 后，即使等待 state mutex 期间 Port 开始关闭，进入锁后也只重新检查 atomic `m_finished`，没有重新检查这两个早期标志。要判断这是否受锁保护，必须看所有写入点。

接下来对照固定版本的实际代码：

```cpp
void PortCore::resume()
{
    m_interrupted = false;
}

void PortCore::interrupt()
{
    if (!m_listening.load()) {
        return;
    }

    m_interrupted = true;
    if (!m_interruptable) {
        return;
    }

    {
        std::lock_guard<std::mutex> lock(m_stateMutex);
        if (m_reader != nullptr) {
            StreamConnectionReader sbr;
            lockCallback();
            m_reader->read(sbr);
            unlockCallback();
        }
    }
}
```

源码身份：固定提交中的 `PortCore::resume` 与 `PortCore::interrupt`，省去日志和解释性注释。运行期对 `m_interrupted` 的两处写入都发生在 `m_stateMutex` 外；`interrupt()` 之后锁住 state mutex 是为了更新阻塞中的 reader，不会保护之前写入的标志。


```cpp
bool PortCore::isInterrupted() const
{
    return m_interrupted;
}
```

源码身份：固定提交中的 `PortCore::isInterrupted`。公开 `Port::write()` 在进入 `sendHelper()` 前也会通过这个无锁 accessor 读该普通 `bool`；输入线程的 `PortCore::readBlock()` 同样检查它。因而 `interrupt()/resume()` 与并发写入口或输入线程之间都没有由这个字段自身提供的同步。

`m_finishing` 的情况也需要分开看：`closeMain()` 第一次把它置为 true 时拿着 state mutex，但关闭流程末尾将它重置为 false 时没有重新加锁。下面摘录的是两个位置；中间的连接断开、server thread join 与 Unit 清理分支略去：

接下来对照固定版本的实际代码：

```cpp
{
    std::lock_guard<std::mutex> lock(m_stateMutex);
    if (m_finishing || !(m_running.load() || m_manual)) {
        return;
    }
    m_finishing = true;
}

// closeMain 中间的断开连接、join 与 closeUnits 分支省略

m_finishing = false;
```

源码身份：固定提交中的 `PortCore::closeMain`。


```cpp
std::atomic<bool> m_finished {false};
bool m_finishing {false};
bool m_interrupted {false};
```

源码身份：固定提交中的 `PortCore` 状态成员声明。`m_interrupted` 和 `m_finishing` 是普通 `bool`，而 `m_finished` 是 atomic。由于 sendHelper 对它们的读取早于 state lock，而 interrupt/resume 的写入不取这把锁，且 closeMain 的最后一次写入也不取锁，`Port::write()` 与 `interrupt()/resume()/close()` 并发时，这些读写没有共同的互斥或原子同步；这是根据固定提交字段类型与全部写入点作出的 C++ 内存模型推导，不是 YARP 作者对行为的承诺。

具体机器人时间线是：控制线程进入 `sendHelper()` 并在早期检查读到未中断；另一线程发出 `interrupt()` 或开始 `close()`；控制线程之后取得 state mutex，而函数只检查仍为 false 的 `m_finished` 并继续扇出。一个已经排队的关节命令便可能在停止/关闭启动后仍进入某个 OutputUnit。反向交错也可能让恢复后的新命令被当作旧中断拒绝。因为存在 data race，C++ 不保证是哪一种表象，也不应把日志中的“已经调用 interrupt”当成所有并发 `write()` 已经被同步拒绝的证明。若要设计可证明边界的缩小版，应让状态检查和开始交付在同一把锁内完成，或以受明确定义内存序的原子状态发布，并在取得连接锁后再次验证状态。

packet 中的数据并不是共享所有权。它存 `PortWriter*` 与 callback 指针、一个在 `m_packetMutex` 保护下递增/递减的整数；计数归零时调用完成接口：

接下来对照固定版本的实际代码：

```cpp
void setContent(const yarp::os::PortWriter* writable,
                bool owned = false,
                const yarp::os::PortWriter* callback = nullptr,
                bool ownedCallback = false)
{
    content = writable;
    this->callback = callback;
    ct = 1;
    this->owned = owned;
    this->ownedCallback = ownedCallback;
    completed = false;
}

// ...getContent() 与 getCallback() 两个访问器在这里省略...

void complete()
{
    if (!completed) {
        if (getContent() != nullptr) {
            getCallback()->onCompletion();
            completed = true;
        }
    }
}
```

源码身份：固定提交中的 `PortCorePacket::setContent` 与 `PortCorePacket::complete`。`getCallback()` 在显式 callback 为空时返回 `content`；`sendHelper()` 以 `setContent(&writer, false, callback)` 注册非拥有的借用指针，所以 packet 不会延长 Writer 的 C++ 生命周期。

引用计数减到零之后，packet 管理器先调用 `complete()`，再把对象回收到空闲链表。这里 `m_packetMutex` 不是只保护 `ct` 的短锁：真实调用者在整个 `checkPacket()` 期间持有它。


```cpp
bool PortCorePackets::checkPacket(PortCorePacket* packet)
{
    if (packet != nullptr) {
        if (packet->getCount() <= 0) {
            packet->complete();
            freePacket(packet);
            return true;
        }
    }
    return false;
}
```

源码身份：固定提交中的 `PortCorePackets::checkPacket`。`complete()` 调用应用虚函数 `onCompletion()`，之后 `freePacket()` 才将 packet 放回池中；两步都发生在调用方仍持有 `m_packetMutex` 时。

公开门面还有一条独立的失败分支：

接下来对照固定版本的实际代码：

```cpp
    result = core.send(writer, nullptr, callback);
    if (!result) {
        if (callback != nullptr) {
            callback->onCompletion();
        } else {
            writer.onCompletion();
        }
    }
    return result;
```

源码身份：固定提交中的 `Port::write(const PortWriter&, const PortWriter*)`；摘录保留 `core.send` 与失败 fallback，省去 interruption 检查和日志准备。若入口检测到 Port 已中断，它会在到达这段 fallback 之前直接返回；下面讨论的是 `core.send()` 已经执行的分支。

这里有两个由源码可重放的边界。第一，`sendHelper()` 在持有 `m_packetMutex` 时调用 `m_packets.checkPacket()`；`checkPacket()` 同步调用 `complete()`，于是用户 `onCompletion()` 在 packet 锁内运行。同步模式下外层 `sendHelper()` 也仍持有 `m_stateMutex`。若回调在这个调用栈里再次调用同一个 Port 的 `write()`，同步发送会先尝试获取同一把非递归 state mutex，当前线程因此自锁；若回调来自后台 worker，线程不持有 state mutex，但重入后会在创建/检查另一个 packet 时再次请求自己正持有的 packet mutex，同样自锁。可观察结果是 `write()` 或 close 等待一直不返回，而不是一个可恢复的短暂停顿。YARP 的 packet 锁保护计数与 packet 池链表，并没有把任意用户回调变成锁外回调。

第二，完成通知没有覆盖整个公开 `Port::write()` 的恰好一次契约。以一条同步 TCP 输出为例：`PortCoreOutputUnit::sendHelper()` 调用应用 Writer 的 `write()`；若序列化返回 false，Unit 将自己标记为 finished 并关闭协议；父级 `sendHelper()` 减掉该路由与调用本身的 packet 引用，最后一个引用归零后在 `checkPacket()` 内调用一次 `onCompletion()`；父级同时因为等待中的 Unit 已 finished 把 `all_ok` 置为 false，返回 facade 后，`Port::write()` 又按 false 分支直接调用一次。这个特定失败时间线可能让同一业务对象观察到两次 callback。`PortCorePacket::completed` 只能阻止同一个 packet 的 `complete()` 重复发出，拦不住 facade 绕过 packet 的直接调用。实用做法是 callback 轻量地投递带序号的完成事件，由业务侧幂等去重；若把“恰好一次”当安全条件，必须针对序列化失败、断连和关闭分别确认路径，不能从 `bool` 返回值推导。

下面是这条失败路径中 OutputUnit 真正调用 Writer 并判定连接已坏的代码。输入是 `cachedWriter`（同步时仍指向调用者传来的 Writer）；远端分支先将业务字段序列化到 `BufferedConnectionWriter`，再让当前连接的 Protocol 写出去。

接下来对照固定版本的实际代码：

```cpp
bool PortCoreOutputUnit::sendHelper()
{
    bool replied = false;

    std::shared_ptr<OutputProtocol> localOp = op;

    if (localOp) {
        bool done = false;
        BufferedConnectionWriter buf(localOp->getConnection().isTextMode(),
                                     localOp->getConnection().isBareMode());
        if (cachedReader != nullptr) {
            buf.setReplyHandler(*cachedReader);
        }

        if (localOp->getSender().modifiesOutgoingData()) {
            if (localOp->getSender().acceptOutgoingData(*cachedWriter)) {
                cachedWriter = &localOp->getSender().modifyOutgoingData(*cachedWriter);
            } else {
                return (done = true);
            }
        }

        if (localOp->getConnection().isLocal()) {
            // WARNING Cast away const qualifier.
            auto* pw = const_cast<yarp::os::PortWriter*>(cachedWriter);
            auto* p = dynamic_cast<yarp::os::Portable*>(pw);
            if (p == nullptr) {
                yCIError(PORTCOREOUTPUTUNIT, getName(), "cast failed.");
                return false;
            }
            buf.setReference(p);
        } else {
            yCIAssert(PORTCOREOUTPUTUNIT, getName(), cachedWriter != nullptr);
            bool ok = cachedWriter->write(buf);
            if (!ok) {
                done = true;
            }

            bool suppressReply = (buf.getReplyHandler() == nullptr);
            if (!done) {
                if (!localOp->getConnection().canEscape()) {
                    if (!cachedEnvelope.empty()) {
                        localOp->getConnection().handleEnvelope(cachedEnvelope);
                    }
                } else {
                    buf.addToHeader();
                    if (!cachedEnvelope.empty()) {
                        if (cachedEnvelope == "__ADMIN") {
                            PortCommand pc('a', "");
                            pc.write(buf);
                        } else {
                            PortCommand pc('\0', std::string(suppressReply ? "D " : "d ") + cachedEnvelope);
                            pc.write(buf);
                        }
                    } else {
                        PortCommand pc(suppressReply ? 'D' : 'd', "");
                        pc.write(buf);
                    }
                }
            }
        }

        if (!done) {
            if (localOp->getConnection().isActive()) {
                replied = localOp->write(buf);
                if (replied && localOp->getSender().modifiesReply() && cachedReader != nullptr) {
                    cachedReader = &localOp->getSender().modifyReply(*cachedReader);
                }
            }
            if (!localOp->isOk()) {
                done = true;
            }
        }

        if (buf.dropRequested()) {
            done = true;
        }
        if (done) {
            closeBasic();
            closing = true;
            finished = true;
            setDoomed();
        }
    }
    return replied;
}
```

源码身份：固定提交中的 `PortCoreOutputUnit::sendHelper`。摘录保留函数全部控制分支，省去说明注释。`write(buf)` 在此先表示 Carrier/stream 路径是否成功，不表示远端机器人控制器已经运行了对应业务逻辑。`done` 使本 Unit 进入关闭状态；回到 PortCore 后，等待分支会观察 `unit->isFinished()`，这才把公开 bool 改为 false，并引出上文的 facade fallback。

执行这段函数的线程就是调用 `Port::write()` 的线程。`std::lock_guard` 在构造时锁住 `m_stateMutex`，直到函数返回才解锁；锁内调用 `unit->send()`。默认 `m_waitBeforeSend` 与 `m_waitAfterSend` 都为 `true`，所以 OutputUnit 同步调用自己的 `sendHelper()`，在调用线程上序列化并写当前连接。若某个 stream 写阻塞，后续 Unit 还没轮到，持锁期间的连接管理也不能取得同一把锁。具体控制后果是：控制线程发送状态 → 慢记录器停止读取 → 发送缓冲区耗尽 → `write()` 超过 10 ms → 控制周期迟到；新连接登记或 Unit 清理也要等锁释放。这个锁让 raw `PortCoreUnit*` 在遍历期不被删掉，代价是慢 I/O 的尾延迟进入管理临界区。

## 扇出不能总是共享同一字节缓冲

若连接 A 使用普通 TCP，连接 B 使用带 modifier 的协议，两者 header、编码或 framing 可能不同：

```text
logical writer
  -> ConnectionWriter A -> bytes A
  -> ConnectionWriter B -> bytes B
```

只有序列化格式和 Carrier 要求相容时，cached writer 才能减少重复编码。缓存 key 至少应包含编码模式、文本/二进制选择、schema 与 modifier；错误共享会产生可连接但不可解析的报文。

## 同步写的所有权简单但尾延迟受慢连接影响

默认同步模式下，调用者栈上的 writer 在 `write()` 返回前有效。PortCore 可以借用它；每条连接按 `m_units` 顺序处理，调用时间会包含本地编码和各同步 OutputUnit 的发送成本。远端 reader 可能稍后才从内核 socket 接收缓冲区取走字节，因此“写调用返回”不等于“对端业务已经消费消息”。

固定版默认 `waitAfterSend=true`，逐个 Unit 的同步发送会让总延迟近似各连接执行成本之和；若显式开启后台写，则 PortCore 把交接给 Unit worker 后继续，而不会等待所有连接各自的协议写完成。两种模式改变的是调用线程何时返回，不改变网络带宽或远端应用处理时间。具体 ACK 是否参与等待还要看 Carrier 的 `write()` 实现，不能仅凭 PortCore 名称推断。

慢连接隔离策略需要明确：等待、超时、断开，或跳过该连接。不同策略分别偏向可靠性、可用性或新鲜度。

## 后台写需要 Tracker 延长生命周期

后台模式通过 `Port::enableBackgroundWrite(true)` 把 `waitAfterSend` 设为 false。OutputUnit 第一次遇到该设置时才启动自己的线程。它保存调用方对象指针并由 packet tracker 推迟完成通知；这不会复制消息，也不会让 C++ 对象自动活得更久：

```text
user thread: Port::write -> store &writer, reader/callback pointers -> return
Unit worker: semaphore wait ends -> serialize borrowed writer -> stream write
            -> PortCore::notifyCompletion(packet tracker)
```

固定实现的 `PortCorePacket::setContent(&writer, false, callback)` 明确将 Writer 设为非拥有；OutputUnit 的 `cachedWriter`、`cachedReader`、`cachedCallback` 也都是借用指针。应用必须让消息、reply reader 与 callback 存活且不被并发改写，直到完成通知。若 `write()` 返回后立即销毁局部消息，worker 随后读取 `cachedWriter` 就会访问已结束生命周期的对象，结果可以是崩溃、错误字段或被复用内存中的旧数据。一个最小安全做法是把消息作为应用对象成员保存，并在收到 completion 前不重用它；也可自行把消息复制进由任务共享持有的不可变 owner。YARP 普通 `Port::write` 后台路径不替应用做这份复制。


```cpp
    if ((!waitBefore) && waitAfter) {
        yCIError(PORTCOREOUTPUTUNIT, getName(), "chosen port wait combination not yet implemented");
    }
    if (!sending) {
        cachedWriter = &writer;
        cachedReader = reader;
        cachedCallback = callback;
        cachedEnvelope = envelopeString;

        sending = true;
        if (waitAfter) {
            replied = sendHelper();
            sending = false;
        } else {
            trackerMutex.lock();
            void* nextTracker = tracker;
            tracker = cachedTracker;
            cachedTracker = nextTracker;
            activate.post();
            trackerMutex.unlock();
        }
    } else {
        yCIDebug(PORTCOREOUTPUTUNIT, getName(), "skipping connection tagged as sending something");
    }

    if (waitAfter) {
        if (gotReply != nullptr) {
            *gotReply = replied;
        }
    }

    // return tracker that we no longer need
    return tracker;
```

源码身份：固定提交中的 `PortCoreOutputUnit::send`（连续摘录，保留缓存指针、忙状态、tracker 转交和返回分支）。worker 线程的下一跳在 `PortCoreOutputUnit::run`：它阻塞于 `activate.wait()`，返回后调用 `sendHelper()`，完成后将 tracker 交给 `PortCore::notifyCompletion()`。

这个通知经过几层才变成业务完成：`activate.post()` 更新 YARP Semaphore 的计数；其内部 mutex/condition_variable 唤醒一个阻塞等待者；该线程变为 runnable，但 Linux 调度器何时给它 CPU 仍由内核决定；线程运行到 `sendHelper()` 后才序列化和调用 stream；最后 packet 计数归零时才调用 `onCompletion()`。通知不是消息已送达，变为 runnable 不是线程已执行，stream 写完也不是对端业务回调已消费。

接下来对照固定版本的实际代码：

```cpp
void PortCoreOutputUnit::run()
{
    running = true;
    sending = false;

    if (!threaded) {
        runSingleThreaded();
        phase.post();
    } else {
        phase.post();
        Route r = getRoute();
        while (!closing) {
            activate.wait();
            if (!closing) {
                if (sending) {
                    sendHelper();
                    trackerMutex.lock();
                    if (cachedTracker != nullptr) {
                        void* t = cachedTracker;
                        cachedTracker = nullptr;
                        sending = false;
                        getOwner().notifyCompletion(t);
                    } else {
                        sending = false;
                    }
                    trackerMutex.unlock();
                }
            }
        }
        sending = false;
    }
}
```

源码身份：固定提交中的 `PortCoreOutputUnit::run`。摘录省去诊断日志，线程选择、wait、工作调用、tracker 取出和 completion 路径保留不变。注意 `notifyCompletion()` 在 `trackerMutex` 持有时调用；PortCore 随后还会在 packet 锁下减少 packet 引用并可能运行 callback。

`activate.wait()` 的 OS 机制来自同一提交 ：YARP Semaphore 用 `std::mutex` 保护 `count/wakeups`，用带谓词的 `condition_variable::wait` 睡眠；`post()` 在锁内增加计数并 `notify_one()`。条件变量的谓词重查状态，因此即使发生虚假唤醒，也只有 `wakeups > 0` 才会消耗一次通知。wait 在阻塞时释放自己的 semaphore mutex，返回前重新获得它。Linux 的标准库可能在 contended wait 中借助 futex 等内核等待原语，但 YARP 依赖的是 C++ 条件变量合同，而不是某个特定 syscall。通知把线程从 blocked 转成可运行，并不保留 CPU；运行队列、调度优先级与当前负载决定它何时执行。

还有一个需要准确标出的源码边界：这版 `PortCoreOutputUnit::sending` 在头文件中声明为普通 `bool`。`send()` 在测试与置位 `sending` 时没有拿 `trackerMutex`；后台 `run()` 在线程路径中读取它，并在 tracker 临界区内将它清零。父级 `m_stateMutex` 只串行化调用者线程，worker 不取这把锁；`trackerMutex` 只覆盖部分访问。因此这些代码片段没有展示出一把共同覆盖全部并发读写的锁。`activate.post()/wait()` 能传递首次任务发布，但不能把所有后续的 `sending` 读写都自动变成原子操作。按 C++ 内存模型，这些交错存在 data race 的风险；不能把 tracker 锁或 semaphore 描述成 `sending` 的完整保护。这是对所固定提交的代码推导，不应推广成其他 YARP 版本的结论。即使不考虑这个同步缺口，容量一忙即跳过也会造成下游序号洞；两种问题的症状不同：前者是线程间状态访问未同步，后者是明确的负载策略。

OutputUnit 的工作槽不是可增长多项队列。应用持续快于网络时，如果某 Unit 正在发送，后续消息到来会跳过该连接；内存保持有界，但序号会有洞。若业务不能丢轨迹点，就要在应用端采用有界队列并明确满载拒绝或阻塞策略，或者为关键事件使用独立通道。

## `sending` 防止同一 Unit 重入

OutputUnit 维护 `sending` 状态，避免在同一连接上重入写。固定版本没有 per-Unit 多项队列：后台旧消息正在写时，新一次 `send()` 进入 busy 分支并跳过该连接；父级随后归还此消息在 packet 中计入的 tracker。观察到的结果是一个接收者的序号会从 100 跳到 103，而其他较快连接可能收到了 101、102、103。对位姿类状态这减少过时数据；对不可丢失的关节轨迹点则会造成控制轨迹缺点，应用应改用明确的有界可靠队列。

这带来每连接而非全 Port 的交付差异：A 连接可成功，B 因 busy 跳过。固定 API 的单个 bool 难表达这种部分差异；若应用需要可观测性，应在消息序号与接收端确认中补 per-route 结果，而不能把 busy skip 当成重试或覆盖。

## 锁只保护任务交接

下面的锁边界是推荐复刻设计，不是 YARP 此版本源码：

```text
[unit mutex]
  check closing/sending
  attach writer/tracker
  mark sending
  signal worker
[unlock]

worker performs serialization and network I/O without unit control mutex

[unit lock]
  clear current work
  mark not sending
[unlock]
invoke completion callback outside lock
```

若序列化或 callback 在锁内执行，用户代码可重入 Port 并死锁；网络阻塞也会让 close 无法取得锁发出 interrupt。YARP 本提交的 PortCore 持 `m_stateMutex` 调到 OutputUnit；OutputUnit 的 `trackerMutex` 只保护 tracker 交换，不代表所有 `sending` 状态和 I/O 都由一把 Unit mutex 保护。

## 成本模型

设连接数为 `C`，不同编码组数为 `E`，payload 为 `S`：

```text
T_write ≈ O(C) traversal
        + Σ(E) encode(S)
        + per-connection frame/enqueue/write
        + optional ack wait
```

空间在同步借用时较小；后台写需要 `O(in_flight × serialized_size)` 或共享 buffer pool。大图像应测量实际编码次数与内存带宽，不能只看 socket 吞吐。

## 故障与关闭：先打断等待，再回收连接

扇出列表不是锁外创建的“快照”：`PortCore::sendHelper()` 持有 `m_stateMutex` 遍历 `m_units` 并调用每个 Unit。这样列表项不会在这段遍历中被另一条同锁的连接管理路径删除；代价是同步发送的慢 I/O 会延长锁持有时间。后台模式下，PortCore 返回后 Unit worker 仍可能持有自己的 Protocol 强引用并使用借来的消息对象，所以摘掉列表项之前必须先收束线程。

接下来对照固定版本的实际代码：

```cpp
void PortCoreOutputUnit::closeMain()
{
    if (finished) {
        return;
    }

    if (running) {
        std::shared_ptr<OutputProtocol> localOp = op;
        if (localOp) {
            localOp->interrupt();
        }

        closing = true;
        phase.post();
        activate.post();
        join();
    }

    closeBasic();
    running = false;
    closing = false;
    finished = true;
}
```

源码身份：固定提交中的 `PortCoreOutputUnit::closeMain`。局部 `shared_ptr` 保证调用 `interrupt()` 时 Protocol 仍活着；`activate.post()` 使阻塞在 worker 信号量上的线程重新检查 `closing`，`join()` 等 worker 退出后，`closeBasic()` 才关闭 Protocol 并清空 Unit 的 `op`。因此“设置 closing”“worker 变为 runnable”“worker 真正退出”“协议对象销毁”是四个不同时间点。

PortCore 只在 server thread 已结束的阶段集中删除 Unit。以下摘录保留这个销毁次序：

接下来对照固定版本的实际代码：

```cpp
void PortCore::closeUnits()
{
    for (auto& i : m_units) {
        PortCoreUnit* unit = i;
        if (unit != nullptr) {
            unit->close();
            unit->join();
            delete unit;
            i = nullptr;
        }
    }
    m_units.clear();
}
```

源码身份：固定提交中的 `PortCore::closeUnits`，删去日志与前置断言。Unit 的 `close()`/`join()` 在 `delete unit` 之前完成；其析构不会与仍在执行的 worker 并发。组合起来看，后台任务的 writer 仍是借用指针，关闭必须中断 I/O 并 join worker，之后才能释放 Unit 和 Protocol。应用若在 completion 前销毁 Writer，关闭顺序也不能补救此前已经发生的悬空引用。

完成 callback 的触发点必须沿 packet 计数、外层 Port bool 返回与 close/error 分支核查。packet 的 `completed` 标志只去重同一个 packet 的内部 `complete()`；`Port::write()` 在 core 返回 false 时还有一处直接调用 `onCompletion()`。此外正常 packet 回调发生在 `m_packetMutex` 内。因而不能在应用侧假定全路径一定恰好一次且总在锁外；适配层可把回调限制为轻量状态投递，并按当前提交核对同步错误路径的通知次数。

## `const` Writer 仍需满足并发可重复性

`PortWriter::write(ConnectionWriter&) const` 是双分派：Writer 动态类型决定业务编码，ConnectionWriter 动态类型决定 Carrier 输出。`const` 只限制普通成员写入，不会自动保护 mutable cache 或共享对象。

若 Writer 复用内部 scratch buffer，多 Unit 并行序列化仍会竞争。安全选择是 write 完全只读、每次调用使用局部 scratch，或 fan-out 前生成单次不可变编码。第一次调用移动走内部 vector 的 Writer 无法服务第二个订阅者。

## 按编码兼容性分组

不同连接只有在 wire mode、schema version 和 modifier chain 相同的情况下才能共享编码：

```text
group key = (wire_mode, schema_version, modifier_chain)
  -> encode once into shared_ptr<const EncodedMessage>
  -> each Unit adds its connection framing and sends
```

若 `C` 条连接形成 `E` 个组，业务序列化可从 `C` 次降到 `E` 次。缓存最好只活在一次逻辑 send 内，避免业务对象改变后误用旧 bytes。每 Unit framing scratch 和 ACK 状态仍不能共享。

## 复刻版可以用完成聚合器表达每路线结果

以下是推荐复刻版的教学代码，不是 YARP 的 `PortCorePacket`。它适用于未来要显式返回每条 route 状态的系统：

```cpp
class FanoutCompletion {
 public:
  void Complete(RouteId route, Status status) noexcept {
    std::function<void(Result)> fire;
    Result result;
    {
      std::lock_guard lock(mu_);
      if (!pending_.erase(route)) return;  // 每 route 最多一次
      results_.push_back({route, status});
      if (pending_.empty() && !fired_) {
        fired_ = true;
        result = BuildResult(results_);
        fire = callback_;
      }
    }
    if (fire) fire(std::move(result));
  }

 private:
  std::mutex mu_;
  std::unordered_set<RouteId> pending_;
  std::vector<RouteResult> results_;
  std::function<void(Result)> callback_;
  bool fired_ = false;
};
```

这个完成器在 mutex 内删除 pending route 并收集结果，最后把 callback 复制到局部变量，解锁后再执行，允许回调重入 Port。成功、busy、序列化错误、网络失败与 close cancellation 都必须删除对应 pending。漏掉会永久保活 payload，重复完成可能提前释放仍在发送的数据。

零连接时应立即完成还是返回失败也要定义。快照后某 Unit 被 close 时，关闭路径与 worker 必须竞争同一个完成门，只由一方提交 cancelled/failed。

## Busy 是容量为一的队列策略

固定 YARP 的 `sending` 状态只实现一种策略：保留正在发送的当前消息，并跳过到来的新消息。覆盖旧等待项或阻塞发送线程都是其他可选策略，不是当前 Unit 的语义。若改成覆盖，必须先对被覆盖任务完成 cancelled；若想阻塞调用者，则要定义最多等待多久和 close 如何唤醒它。

若覆盖旧任务，必须先完成旧 Tracker 的 cancelled。若状态还包含 closing/current_writer/protocol，多个 atomic bool 无法维护一致组合，更适合在一把 mutex 下使用 `Idle / Assigned / Sending / Cancelling / Closed` 状态机。

## worker 与 close 的租约边界

推荐复刻版可让 worker 在 Unit mutex 内把任务和 Protocol 强引用移动到局部对象，更新成员状态后解锁，再做编码与 I/O。YARP 这版实际将 borrowed Writer 指针存入成员、由 OutputUnit 线程执行 `sendHelper()`，且父级 `PortCore::sendHelper()` 仍持 `m_stateMutex` 调用 `unit->send()`；关闭经 Unit 的 interrupt、semaphore post 和 join 收口，不能概括为“registry 摘除即对象由租约保活”。

局部强引用是一次操作租约，只保证对象活着。Protocol 写入仍由单 worker 串行，避免两个任务交错 framing。完成 callback 最后执行，且必须发生在 Unit/PortCore 控制锁外。

## 完整空间和时间模型

若最大在途逻辑写为 `Q`，编码组数为 `E`，平均 payload 为 `S`，连接数为 `C`，应用空间近似：

```text
O(Q × E × S + C × framing_scratch + completion_state)
```

此外还有内核 socket buffer。时间应拆为 PortCore 持锁遍历、业务 encode、per-Carrier frame、可能的后台 semaphore 等待、系统调用、Carrier 可能的 ACK 与 completion callback。默认同步顺序发送还存在列表顺序偏差，后方连接承担前方慢连接的延迟；后台模式没有一项可多次排队的 Unit queue wait。

## 设计模式、限制与可迁移思想

写路径组合 Command（PortWriter）、Strategy（Carrier）、Active Object（后台 Unit）和 Countdown/Barrier（完成聚合）。不可变单次编码缓存把业务值与连接 framing 解耦。

限制是简单 `write()` 外观隐藏异步完成、部分成功和参数寿命；多 Carrier 使编码共享只能在兼容组内成立；ACK 与慢连接会扩大尾延迟。

可迁移原则是：若需要 per-destination 结果，就用显式结果结构；跨线程优先传拥有型不可变数据；每项任务只有一个可审计的完成终态；控制锁是否包 I/O 必须由实际临界区证明；队列容量与覆盖方向属于接口；关闭时要释放等待者。

## 最小复刻顺序

先实现同步单连接 writer；扩展为锁内快照、锁外顺序 fan-out；加入 per-route result；再做编码分组缓存；随后增加每 Unit 容量一 worker 与 completion；最后实现有界多项队列、busy 策略、ACK timeout 和 close cancellation。

验收条件包括：Writer 可重复编码；不兼容 Carrier 不共享缓存；任何错误路径都完成 Tracker；callback 可重入 Port；应用快于网络时内存有上界；快照后关闭不产生 UAF；close 后不存在借用调用者栈的任务。

