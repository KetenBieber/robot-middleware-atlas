# 输入与 RPC 路径：InputUnit、PortReader 和回复通道

接收方向不是简单的 socket callback。InputUnit 驱动 Protocol 划分消息，PortCore 再把 ConnectionReader 交给用户 PortReader；RPC 则在同一连接上增加回复 writer 与请求-响应次序。

本文源码固定为 `robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`。下面直接摘录 `PortReader::read`、`PortCoreInputUnit::run`、`Protocol::beginRead/endRead/write`，把连接线程、应用回调与协议帧收尾放在一条路径上。

## 功能语义与对象边界

普通输入关心“收到一条完整消息并交给 Reader”。RPC 还要求请求和回复建立对应关系，并明确回复写完以前连接能否读取下一项请求。二者复用 `ConnectionReader`，是因为 framing、类型信息和远端 Route 都来自当前连接；差异通过 reader 是否携带 reply writer 表达。

```text
stream bytes
  -> Carrier 划定 frame
  -> Protocol 构造 ConnectionReader
  -> InputUnit 选择 PortReader
       |-- 单向消息：read 后结束当前 frame
       `-- RPC 请求：read 内取得 reply writer 并写回
  -> Protocol 完成 ack/reply framing
```

`PortReader` 处在协议驱动的借用作用域内，输入视图、回复 writer 和连接错误状态通常都只在这次 `read()` 有效。它不是可以随意保存参数的普通业务回调。

## 关键 C++ 接口与动态分派

`PortReader` 是 YARP 交给应用的动态分派边界。PortCore 不要求知道图像或关节消息的具体类型，只要求对象实现 `read(ConnectionReader&)`。

**固定提交源码摘录（`robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`，`PortReader`）：**

```cpp
class YARP_os_API PortReader
{
public:
    /**
     * Destructor.
     */
    virtual ~PortReader();

    /**
     * Read this object from a network connection.
     *
     * Override this for your particular class.
     *
     * @param reader an interface to the network connection for reading
     * @return true iff the object is successfully read
     */
    virtual bool read(ConnectionReader& reader) = 0;

    virtual Type getReadType() const;
};
```

`ConnectionReader&` 使用非 `const` 引用，因为读取会推进连接游标。析构函数是虚函数，因而通过 `PortReader*` 销毁派生对象时会进入派生析构。抽象基类让 Bottle、生成类型和用户协议读取同一种逻辑输入，而不必知道底层是 TCP 还是内存流。虚函数把业务类型变化限制在 Reader/Writer 边界；实际代价要和调用栈、系统调用及反序列化一起衡量。

## 输入调用链

```text
InputUnit thread
  -> Protocol::beginRead()
  -> PortCore dispatch
  -> PortReader::read(ConnectionReader&)
  -> Protocol::endRead()
  -> next message
```

用户 `read()` 返回前，ConnectionReader 通常只对当前帧有效。把 reader 引用保存到异步任务会悬空；需要异步处理时应在回调内把字段复制或移动到自有对象。

在固定实现中，InputUnit 线程每轮先取得 `ConnectionReader&`，再识别消息命令。普通数据命令 `d/D` 的分支会把同一个 reader 交给本地 reader 或 PortCore 的 reader 路径；下面摘录该 `switch` 分支。代码块从真实 switch 的 `case 'D'` 开始，省略前面的命令读取和后续其他 command cases。

**固定提交源码摘录（`robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`，`PortCoreInputUnit::run` 的数据分支）：**

```cpp
case 'D':
case 'd': {
    if (key == 'D') {
        ip->suppressReply();
    }

    std::string env = cmd.getText();
    if (env.length() > 2) {
        yCITrace(PORTCOREINPUTUNIT, getName(), "***** received an envelope! [%s]", env.c_str());
        std::string env2 = env.substr(2, env.length());
        man.setEnvelope(env2);
        ip->setEnvelope(env2);
    }
    if (localReader != nullptr) {
        localReader->read(br);
        if (!br.isActive()) {
            done = true;
            break;
        }
    } else {
        if (ip->getReceiver().acceptIncomingData(br)) {
            ConnectionReader* cr = &(ip->getReceiver().modifyIncomingData(br));
            yarp::os::impl::PortDataModifier& modifier = getOwner().getPortModifier();
            modifier.inputMutex.lock();
            if (modifier.inputModifier != nullptr) {
                if (modifier.inputModifier->acceptIncomingData(*cr)) {
                    cr = &(modifier.inputModifier->modifyIncomingData(*cr));
                    modifier.inputMutex.unlock();
                    man.readBlock(*cr, id, os);
                } else {
                    modifier.inputMutex.unlock();
                    skipIncomingData(*cr);
                }
            } else {
                modifier.inputMutex.unlock();
                man.readBlock(*cr, id, os);
            }
        } else {
            skipIncomingData(br);
        }
        if (!br.isActive()) {
            done = true;
            break;
        }
    }
} break;
```

若该连接有 `localReader`，回调直接在 InputUnit 的执行线程中运行。否则先经过 Receiver 的接受/修改，再取得 PortCore 的 reader 路径；`inputMutex` 只围住 modifier 指针检查和调用 `modifyIncomingData`，调用 `man.readBlock` 前就释放。阻塞的应用 Reader 因而会占住对应 InputUnit 的线程和当前连接的读取进度；同一 Port 上其他连接可以有各自的 InputUnit，但若共同进入同一个业务 Reader，其共享成员仍要由应用同步。

命令 switch 结束后，InputUnit 才在连接仍存在时结束当前 read。这个位置很重要：业务 Reader 的 bool 返回并不等于协议 ack 已发送。

**固定提交源码摘录（同一提交，`PortCoreInputUnit::run` 的帧收尾）：**

```cpp
        if (ip != nullptr) {
            ip->endRead();
        }
        if (ip == nullptr) {
            break;
        }
        if (closing || isDoomed() || (!ip->isOk())) {
            break;
        }
```

这段位于整个 command `switch` 之后。仍然存在的 `ip` 会调用 `endRead()`；然后 InputUnit 才检查连接是否已转交、正在 closing/doomed 或协议是否失效。这里不是把应用回调放进独立线程池：用户代码尚未返回时，当前 InputUnit 无法进入下一轮 `beginRead()`。

Protocol 负责消费 carrier-specific 的消息索引、调用 `respondToIndex()`，再把内部 `reader` 借给上层。下面是固定版本 `Protocol::beginRead()` 的完整函数体：

**固定提交源码摘录（同一提交，`Protocol::beginRead`）：**

```cpp
ConnectionReader& Protocol::beginRead()
{
    // We take care of reading the message index
    // (carrier-specific preamble), then leave it
    // up to caller to read the actual message payload.
    getRecvDelegate();
    if (delegate != nullptr) {
        bool ok = false;
        while (!ok) {
            ok = expectIndex();
            if (!ok) {
                if (!is().isOk()) {
                    // Go ahead, we'll be shutting down, it'll
                    // work out.
                    ok = true;
                }
            }
        }
        respondToIndex();
    }
    return reader;
}
```

其后 `endRead()` 刷新可能由 RPC reply 使用的 writer，再向 carrier 发送 ack：

**固定提交源码摘录（同一提交，`Protocol::endRead`）：**

```cpp
void Protocol::endRead()
{
    reader.flushWriter();
    sendAck(); // acknowledge after reply (if there is one)
}
```

所以一条接收消息至少有三种不同完成状态：reader 已交给业务；业务 reader 已返回；协议 ack/reply 已完成。用“收到并唤醒”概括它们会掩盖连接时序。

## Reader 返回值参与连接状态

`PortReader::read` 的 bool 不只是业务成功标志。失败可能让上层认为协议或连接不可继续。应用应区分可忽略的业务拒绝与无法解析的帧；若所有错误都返回 false，单个非法请求可能关闭长期连接。

反序列化必须验证长度和数量，再分配容器。对端提供的数组长度不能直接用于无界 `resize()`。

## Strict 与非 Strict 读取表达积压策略

缓冲型 Port 常需要决定慢消费者看到完整历史还是最新消息。Strict 模式倾向保持顺序并积压，非 Strict 模式可覆盖/合并旧数据以保持新鲜度。

这与中间件其他有界队列相同：完整性、新鲜度和有限内存不能同时无限满足。使用教程和接口文档必须明确满载行为，并提供 drop/overwrite 计数。

## RPC 在请求连接上提供 Reply Writer

RPC server 的 Reader 解析请求后，通过 ConnectionReader 取得回复 writer：

**教学代码（不是固定提交源码摘录）：**

```cpp
bool RpcHandler::read(ConnectionReader& request) {
    Command cmd;
    cmd.read(request);

    auto* reply = request.getWriter();
    if (reply != nullptr) {
        Result result = execute(cmd);
        result.write(*reply);
    }
    return true;
}
```

请求处理时间会占用 InputUnit 执行上下文。长任务应转交 worker，并设计 request id 或异步协议；不能保留临时 ConnectionWriter 指针到回调返回之后。

发送方这一侧，`Protocol::write(SizedWriter&)` 会在请求写完后查看 `SizedWriter` 是否登记了 reply handler；如有，它把同一连接上的回复 reader 喂给该 handler，然后等待 carrier ack。下面是这个 reply 收取控制流的固定源码摘录。

**固定提交源码摘录（同一提交，`Protocol::write` 的请求、回复与 ack）：**

```cpp
bool Protocol::write(SizedWriter& writer)
{
    // End any current write.
    writer.stopWrite();
    // Skip if this connection is not active (e.g. when there are several
    // logical mcast connections but only one write is actually needed).
    if (!getConnection().isActive()) {
        return false;
    }
    this->writer = &writer;
    bool replied = false;
    yCAssert(PROTOCOL, delegate != nullptr);
    getStreams().beginPacket(); // Message begins.
    bool ok = delegate->write(*this, writer);
    getStreams().endPacket(); // Message ends.
    PortReader* reply = writer.getReplyHandler();
    if (reply != nullptr) {
        if (!delegate->supportReply()) {
            // We are expected to get a reply, but cannot.
            yCInfo(PROTOCOL, "connection %s does not support replies (try \"tcp\" or \"text_ack\")", getRoute().toString().c_str());
        }
        if (ok) {
            // Read reply.
            reader.reset(is(), &getStreams(), getRoute(), messageLen, delegate->isTextMode(), delegate->isBareMode());
            replied = reply->read(reader);
        }
    }
    expectAck(); // Expect acknowledgement (carrier-specific).
    this->writer = nullptr;
    return replied;
}
```

这个 reply handler 是发送端随 `SizedWriter` 注册的 `PortReader`，读的是同一 Protocol 的 `reader`。实现顺序是先写请求 packet，再读 reply（若注册且 write 成功），最后等 carrier-specific ack。回复支持受 Carrier 能力约束；不能从 `PortReader` 接口本身推断所有 Carrier 都支持 RPC。

## 多请求并发取决于连接和 Unit 模型

同一连接上的消息通常按 Protocol 顺序读取，一个慢 RPC 阻塞后续请求。多个客户端连接可由不同 InputUnit 并发进入同一个 PortReader，因此 handler 内部共享状态仍需同步。

若需要严格串行，应把请求放入单 worker 队列；若需要并行，应使状态分片或加锁，并给每客户端/全局设置并发上限。

## Interrupt 必须唤醒 beginRead

InputUnit 可能阻塞在 stream read。仅设置 `closing=true` 不会让内核读取返回。`interrupt()` 需要传到 Protocol/Carrier/TwoWayStream，使 beginRead 失败并让线程检查关闭状态。

关闭测试应覆盖无数据阻塞、读到半帧、用户 Reader 正在执行和等待 RPC reply 四种位置。

## 输入性能

接收成本包含 frame 解析、反序列化、用户 handler 和 ack。若 handler 在 InputUnit 线程直接执行，吞吐和连接读取节奏由最慢 handler 决定。把任务交给队列能释放网络线程，但要重新定义 buffer 生命周期和过载策略。

## 借用期限与事务式反序列化

调用栈说明了 reader 为何不能跨回调保存：

**教学代码（不是固定提交源码摘录）：**

```cpp
bool InputUnit::RunOne() {
    auto frame = protocol_->beginRead();
    if (!frame) return false;
    const bool accepted = reader_->read(*frame);
    return protocol_->endRead(accepted);  // 此后 frame 失效
}
```

C++ 引用不会自动携带生命周期检查。异步任务不能保存 `ConnectionReader*`、指向内部缓冲区的 `string_view` 或 reply writer，而应保存解析后的拥有型值。

解析建议采用“临时对象 + 成功提交”：

**教学代码（不是固定提交源码摘录）：**

```cpp
bool JointState::read(ConnectionReader& in) {
    JointState candidate;
    std::uint32_t count = 0;
    if (!ReadU32(in, count) || count > kMaxJoints) return false;
    candidate.positions.resize(count);
    if (!ReadDoubles(in, candidate.positions)) return false;
    *this = std::move(candidate);
    return true;
}
```

第三个字段失败时，原对象保持不变。直接逐字段写入 `*this` 会留下半更新状态。提交时移动 vector 通常只转移缓冲区指针，不复制全部元素。

## RPC 异步化的所有权设计

若长任务必须离开 InputUnit，安全结构是让 worker 只处理拥有型对象，再由连接所属执行上下文写回：

```text
InputUnit: parse -> enqueue {request_id, owned_request}
Worker: execute -> enqueue {request_id, owned_result}
connection actor: serialize result -> reply writer
```

不能把临时 `ConnectionWriter*` 直接放进任务队列。另一方案是每个 RPC 使用独立连接，实现较简单，但握手、文件描述符和连接管理成本更高。

并发上限也不能只限制 worker 数量，还要限制等待队列。到达率长期高于服务率时，无界队列只是把过载推迟成内存耗尽。RPC 队列满时更适合明确返回 busy/error，而不是静默丢弃请求。

## 性能模型与取舍

若单连接平均处理时间为 `Tparse + Thandler + Treply`，串行吞吐上限约为其倒数。增加 InputUnit 只能提高不同连接之间的并行度，不能突破单连接的顺序约束。

| 方案 | 优点 | 代价与边界 |
|---|---|---|
| InputUnit 直接回调 | 生命周期简单，回复路径直接 | 慢 handler 阻塞连接读取 |
| 有界 worker 队列 | 隔离网络线程与业务耗时 | 必须复制请求并定义满载响应 |
| 每连接独立 Unit | 客户端之间隔离 | 线程、栈和调度成本增长 |
| 共享并行池 | 资源利用率较高 | 同一对象状态需要同步或分片 |

## 可迁移设计与最小复刻

可迁移原则是：网络线程拥有连接状态；跨线程只传拥有型业务对象；解析采用事务式提交；队列和并发均有上限；业务拒绝与协议损坏使用不同错误通道。

复刻时先实现限制在单帧内的 `LimitedReader`，再实现单向 `PortReader`，随后添加回调期间有效的 reply writer，最后引入有界 worker 队列与 request id。完成标准包括：截断帧不污染下一帧、超大长度不触发无界分配、慢 RPC 不无限扩张队列、interrupt 能唤醒半帧读取。
