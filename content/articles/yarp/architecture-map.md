# YARP 功能与组件地图：名字、连接与可插拔协议

YARP 从逻辑 Port 名开始，经 Name Server 找到地址，再由 PortCore、Unit、Protocol 与 Carrier 建立实际数据连接。

## 使用场景与非目标

YARP 适合机器人实验系统中动态连接模块、设备和异构传输：逻辑名称解耦地址，Carrier 允许按连接替换协议，Port/Bottle/生成类型支持从快速交互到稳定接口的不同层次。

它不自动提供严格 schema 演进、中心化事务或无限可靠队列。名字能解析不等于远端语义兼容；TCP Carrier 可靠传字节不等于业务命令恰好执行一次；动态连接灵活性也意味着拓扑错误多在运行期出现。

## 功能域

| 功能 | 核心组件 | 关键问题 |
|---|---|---|
| 命名控制面 | Name Server、NameClient、Contact | 名字注册、解析与陈旧条目 |
| Port 门面 | Port、BufferedPort、PortCore | 连接集合、Reader/Writer、关闭 |
| 连接执行 | InputUnit、OutputUnit | 输入线程、按需输出线程、busy 与 fan-out |
| 协议 | Protocol、Carrier、TwoWayStream | 握手、framing、ack、modifier |
| 对象编码 | PortReader、PortWriter、Bottle/Portable | 类型与连接编码解耦 |
| 管理 | PortReport、admin、persistent connection | 动态拓扑与可观测性 |

## 从机器人需求反推组件

| 需求 | 首先进入的组件 | 不能只看这一层的原因 |
|---|---|---|
| 运行时按名字连接相机、控制器和记录器 | NameClient、Contact、Route | 名字解析成功后仍要由 Protocol 建真实连接 |
| 一个状态源同时发送给控制器和可视化 | PortCore、OutputUnit | 每条连接的 Carrier、阻塞和错误相互独立 |
| 同一消息走 TCP、UDP 或自定义链路 | Carrier registry、Protocol | Carrier 决定 framing，不负责业务对象字段 |
| C++ 对象与 wire bytes 解耦 | PortWriter、PortReader、Portable | Reader/Writer 是借用接口，寿命受一次传输约束 |
| 慢消费者不拖住控制状态 | BufferedPort、后台写策略 | 必须选择 latest、strict、丢弃或反压语义 |
| 请求—响应设备操作 | RpcClient/RpcServer、reply writer | 字节可靠不等于请求幂等或业务动作完成 |
| 运行期重连与运维检查 | admin port、PortReport、persistent connection | 控制操作仍会与正常 I/O、关闭并发 |

这张表用于选择入口。若问题是“为什么连接不上”，应从 Name/Route 和握手开始；若问题是“发送偶尔卡顿”，应从 OutputUnit 和 Carrier I/O 开始；若问题是“关闭时崩溃”，应从 PortCore registry、Unit 寿命和 interrupt 开始。不要从最熟悉的 `BufferedPort<T>` API 猜所有故障。

## 源码目录与阅读入口

本系列固定 YARP 源码版本见《阅读基础》。阅读时可按抽象层进入对应实现目录和符号：

| 层 | 入口对象 | 先找的函数族 | 阅读目标 |
|---|---|---|---|
| 门面 | `Port`、`BufferedPort<T>` | open/write/read/interrupt/close | 公开调用如何进入内核，借用对象活多久 |
| 端口内核 | `PortCore` | listen、add/remove unit、send、close | raw Unit registry、状态锁和扇出临界区 |
| 单连接 | `PortCoreInputUnit`、`PortCoreOutputUnit` | run、send、interrupt、close | 输入执行线程、按需输出 worker、完成通知和错误隔离 |
| 协议 | `Protocol` | open、beginWrite、beginRead、close | 握手后如何建立一帧的读写边界 |
| Carrier | `Carrier` 及 registry | checkHeader、respondToHeader、create | 协议识别、工厂 clone、modifier |
| 流 | `TwoWayStream` | read/write/interrupt | OS I/O、超时和半关闭 |
| 命名 | `NameClient`、`Contact`、`Route` | register/query/connect | 逻辑名如何变成连接参数 |

推荐每次只沿一条调用链，并同时记录四列：当前线程、当前 owner、payload 是借用还是拥有、失败返回给谁。只画类依赖图会漏掉 Unit worker 与 close 的竞态。

## 完整路径

```text
NameClient resolve Route
  -> PortCore creates OutputUnit
  -> Protocol + Carrier handshake
Port::write(PortWriter)
  -> PortCore 持 m_stateMutex 遍历输出 Unit
  -> each OutputUnit serializes through ConnectionWriter
  -> Carrier frames bytes
  -> remote InputUnit -> PortReader
```

这条路径包含三次解耦：Name Server 只解析地址而不代理数据；PortCore 管理逻辑连接而不理解具体 wire framing；PortWriter/Reader 处理业务编码而不拥有 socket。问题定位也应按这三层区分控制面、连接协议和业务序列化。

### 建连路径与发送路径必须分开

```text
建连：
Route(from, to, carrier)
  -> NameClient 解析 Contact
  -> 创建 stream / Protocol
  -> Carrier header 检测与握手
  -> OutputUnit 加入 PortCore registry

发送：
PortWriter
  -> 在 PortCore 状态锁保护的连接表上逐个调用 OutputUnit
  -> 每个 Unit 调用 ConnectionWriter
  -> framing / socket write
  -> completion
```

建连是低频控制面，允许字符串解析、工厂查找和能力协商；发送是高频数据面，应复用 Unit 和 Protocol。若每次 write 都重新解析名字和创建 Carrier，Name Server 延迟会直接进入数据时延，连接状态也无法稳定复用。

## 核心对象与所有权

```text
Port / BufferedPort facade
  -> PortCore
       |-- Face / listener
       |-- vector<PortCoreUnit*> m_units  (m_stateMutex 协调登记、遍历和清理)
       |     |-- InputUnit  --shared_ptr--> InputProtocol / stream
       |     `-- OutputUnit --shared_ptr--> OutputProtocol / stream
       |-- PortReader / PortReport / admin handler
       `-- finishing / interrupted / close state

NameClient -> Name Server   仅注册/解析 Contact 与 Route
```

一个 Unit 对应一条连接的活动状态，隔离各连接的协议状态。固定版本 `robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9` 在 `PortCore` 声明里使用 `std::vector<PortCoreUnit*> m_units`；这些裸指针由 PortCore 手动关闭和删除，并非 `shared_ptr<Unit>` 租约。

**固定提交源码摘录（`robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`，`PortCore` 的 registry 与锁字段）：**

```cpp
private:
    std::vector<PortCoreUnit *> m_units;  ///< list of connections
    std::mutex m_stateMutex;
    std::condition_variable m_stateCv;
    std::mutex m_packetMutex;      ///< control access to message cache
    std::condition_variable m_connectionChangeCv; ///< signal changes in connections
```

再看发送端如何用锁：

**固定提交源码摘录（同一提交，`PortCore::sendHelper` 的 state lock 与 packet 初始化）：**

```cpp
std::lock_guard<std::mutex> lock(m_stateMutex);

// If the port is shutting down, abort.
if (m_finished.load()) {
    return false;
}

yCITrace(PORTCORE, getName(), "------- send in");
// Prepare a "packet" for tracking a single message which
// may travel by multiple outputs.
m_packetMutex.lock();
PortCorePacket* packet = m_packets.getFreePacket();
yCIAssert(PORTCORE, getName(), packet != nullptr);
packet->setContent(&writer, false, callback);
m_packetMutex.unlock();
```

`lock` 是函数作用域内的 RAII 对象，在 `sendHelper()` 返回前都不会析构。下面的逐 Unit `send()` 因此也处在 `m_stateMutex` 临界区；与之不同，`m_packetMutex` 在每段 packet 操作完成后立即释放。

**固定提交源码摘录（同一提交，`PortCore::sendHelper` 的连接 fan-out 循环）：**

```cpp
// Scan connections, placing message everywhere we can.
for (auto* unit : m_units) {
    if ((unit != nullptr) && unit->isOutput() && !unit->isFinished()) {
        bool log = (!unit->getMode().empty());
        if (log) {
            // Some connections are for logging only.
            logCount++;
        }
        bool ok = (mode == PORTCORE_SEND_NORMAL) ? (!log) : (log);
        if (!ok) {
            continue;
        }
        bool waiter = m_waitAfterSend || (mode == PORTCORE_SEND_LOG);
        yCITrace(PORTCORE, getName(), "------- -- inc");
        m_packetMutex.lock();
        packet->inc(); // One more connection carrying message.
        m_packetMutex.unlock();
        yCITrace(PORTCORE, getName(), "------- -- pre-send");
        bool gotReplyOne = false;
        // Send the message off on this connection.
        void* out = unit->send(writer,
                               reader,
                               (callback != nullptr) ? callback : (&writer),
                               reinterpret_cast<void*>(packet),
                               envelopeString,
                               waiter,
                               m_waitBeforeSend,
                               &gotReplyOne);
        gotReply = gotReply || gotReplyOne;
        yCITrace(PORTCORE, getName(), "------- -- send");
        if (out != nullptr) {
            // We got back a report of a message already sent.
            m_packetMutex.lock();
            (static_cast<PortCorePacket*>(out))->dec(); // Message on one fewer connections.
            m_packets.checkPacket(static_cast<PortCorePacket*>(out));
            m_packetMutex.unlock();
        }
        if (waiter) {
            if (unit->isFinished()) {
                all_ok = false;
            }
        }
        yCITrace(PORTCORE, getName(), "------- -- dec");
    }
}
```

这让 raw Unit 指针在遍历和同步 `send()` 期间不被另一个持 `m_stateMutex` 的连接表操作移除；代价是慢 socket 写可能延迟 connect/disconnect 管理。若需要锁外发送，就必须另行设计 Unit 稳定租约或线程归属，不能仅把裸指针复制到临时 vector。

输出 Unit 内部的 `op` 是 `std::shared_ptr<OutputProtocol>`，局部副本可以在成员随后 reset 时继续保活 Protocol；这条局部对象寿命规则不能推广成 Unit registry 的共享所有权。关闭路径必须先通过 PortCore/Unit 的同步协议结束在途访问，再删除 raw Unit。

### 所有权边不等于调用边

PortCore 拥有 Unit，不代表 Unit worker 只在 PortCore 调用栈内执行。线程入口可能保存 PortCore 回指，用于报告断开或完成事件，于是形成双向调用：

```text
ownership: PortCore -> Unit -> Protocol -> Stream
callback:  Unit worker -----------------> PortCore report/remove
```

固定版本中的 Unit 通过 `getOwner()` 回指所属 `PortCore`，PortCore 则保有 Unit raw pointer。这个非对称关系避免 shared_ptr 环，但把正确性放在同步和关闭次序上：PortCore 不能释放 Unit 时仍有线程回调 owner。`PortCore::closeMain()` 会结束 server thread，并在 finished 阶段关闭、join、delete Units。文章中“共享租约快照后锁外 I/O”可作为推荐的重设计方向，不能当作本版源码描述。

`PortReader*`、`PortWriter&` 和 ConnectionReader 等接口也不自然拥有对象。源码阅读时遇到裸指针不能立即判定为错误，要寻找其外部寿命协议：是 PortCore 配置期固定、一次 callback 借用，还是 completion tracker 保活。真正危险的是协议未写出或关闭路径破坏它。

## 命名控制面与连接数据面

Name Server 保存逻辑名到 Contact 的映射，连接建立后 payload 通常在端点间直传。持久连接配置可以在端点暂时不可用时保留意图，但需要重试、退避与陈旧条目处理。

Route 至少包含 from、to 与 carrier。相同两个 Port 可因 carrier/modifier 不同产生不同传输语义。日志与诊断若只记录端点名而不记录完整 Route，无法解释压缩、确认或协议差异。

### Name Server 故障的影响范围

Name Server 不可用时，已有端到端连接可能继续传输，因为数据不必绕经命名服务；新 open、resolve 或 reconnect 会失败。恢复后还要处理进程重启复用旧名字、注册陈旧和持久连接重试风暴。

因此健康检查至少分三层：名字是否注册；Contact 是否可达且握手成功；应用消息是否持续产生且时间戳新鲜。只用名字列表不能证明控制链健康。

## 写入扇出与所有权

```text
Port::write(const PortWriter&)
  -> PortCore 在 m_stateMutex 下逐个访问 OutputUnits
  -> per Unit: synchronous write or background task
  -> PortWriter::write(ConnectionWriter&)
  -> Protocol/Carrier frame and send
  -> PortCorePacket 计数归零后调用 onCompletion（只通知，不拥有 Writer）
```

默认同步扇出在一个调用中依次经过各 OutputUnit，慢连接会延长持有 `m_stateMutex` 的时间。后台写可以让 `Port::write` 不等写线程完成，但 OutputUnit 暂存的是调用方对象指针；调用方必须遵守完成通知前不销毁或改写数据的约定，通知本身不会延长 Writer 的 C++ 对象寿命。`const` write 契约意味着同一对象可重复序列化；若 writer 在第一次调用移动走内容，后续连接会收到错误数据。

每连接 Unit 隔离慢节点，但连接数 `N` 会带来 `O(N)` 状态、可能的线程栈和扇出工作。大规模连接需要测量的不是单连接峰值，而是总线程数、队列、context switch 和关闭时间。

### 实际代码：后台发送不是复制一份 Writer 后再排队

PortCore 遍历各输出 Unit，决定是否同步等待。具体 Unit 的 `send()` 并没有“后台复制用户消息对象”这个步骤。固定源码从 `PortCoreOutputUnit::send` 开始可以直接读到同步与后台两种分支：

~~~cpp
void* PortCoreOutputUnit::send(const yarp::os::PortWriter& writer,
                               yarp::os::PortReader* reader,
                               const yarp::os::PortWriter* callback,
                               void* tracker,
                               const std::string& envelopeString,
                               bool waitAfter,
                               bool waitBefore,
                               bool* gotReply)
{
    bool replied = false;

    {
        std::shared_ptr<OutputProtocol> localOp = op;
        if (localOp) {
            if (!localOp->getConnection().isActive()) {
                return tracker;
            }
        }
    }

    if (!waitBefore || !waitAfter) {
        if (!running) {
            // we must have a thread if we're going to be skipping waits
            threaded = true;
            yCIDebug(PORTCOREOUTPUTUNIT, getName(), "starting a thread for output");
            start();
            yCIDebug(PORTCOREOUTPUTUNIT, getName(), "started a thread for output");
        }
    }

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
}
~~~

先检查 `OutputProtocol` 当前连接是否仍 active。若调用者要求跳过前置或完成等待，而该 Unit 没有运行中的 worker，就按需启动一条发送线程。`cachedWriter = &writer` 保存的是**调用者对象的地址**，不是值副本；`cachedReader` 和 `cachedCallback` 也只是暂存句柄。`waitAfter` 为真时，本函数在当前线程调用 `sendHelper()`；否则通过 `activate.post()` 唤醒 worker，让它稍后序列化数据。

这里有两个必须保留的行为边界。第一，若 `sending` 已经为真，Unit 会跳过本次连接，不会在内部建立无限待办队列。因此“异步”不等于“缓存所有样本”，过载时业务必须观察发送统计或完成通知。第二，在完成回调之前，应用不能析构、移动或并发改写传给异步 Unit 的 `PortWriter`：指针寿命由调用方的等待/完成协议维持，而不是由 `cachedWriter` 自动管理。即使 PortCore 的 packet tracker 能记录一条消息经过了几只输出 Unit，它也不能凭空延长任意 C++ 栈上 Writer 的寿命。

控制系统应按连接区分两个时刻：`Port::write` 返回，以及最后一只异步 Unit 完成。多连接扇出还要区分“某连接忙所以跳过”与“socket 已成功写出”；二者都不能用远端控制器已经消费样本来替代。
### 线程与数据移动矩阵

| 阶段 | 常见线程 | 输入的所有权 | 可能的复制/阻塞 |
|---|---|---|---|
| `Port::write` 入口 | 应用线程 | 借用 PortWriter | `m_stateMutex` 后访问 raw Unit 表 |
| 默认同步 OutputUnit 写 | 应用线程 | 借用 writer | Writer 序列化、Protocol 和 stream 写都在调用链中 |
| 后台 OutputUnit 写 | 按需启动的 Unit worker | 保存调用方 Writer/Reader/Callback 指针与 tracker | 应用须等完成回调再复用对象；busy 到来的消息不排队 |
| InputUnit 接收 | 每连接的输入执行线程 | Protocol 提供当前帧 reader | socket read、命令解析与 modifier |
| PortReader callback | 对应 InputUnit 线程 | 借用 ConnectionReader | 慢回调阻止该连接读取下一帧；可配置 callback mutex |
| BufferedPort read | 应用线程 | 借用池中对象 | 等待、复制到自有对象 |
| interrupt/close | 管理线程 | PortCore/Unit owner | 唤醒 I/O、join |

这张表解释了为什么“write 是 const”不等于线程安全：同一个 PortWriter 可能被多个 Unit 调用，writer 内部缓存若 mutable 就需要同步；也解释了为什么回调不能保存 Reader 指针：对应 frame 由 InputUnit 在 callback 返回后复用或释放。

## 输入、BufferedPort 与 RPC

InputUnit 通过 Protocol 的 `beginRead/endRead` 限定一帧，再把临时 ConnectionReader 交给 PortReader。Reader 借用只在 callback 内有效；异步处理必须复制成拥有型对象。

BufferedPort 把网络接收与应用读取解耦，但必须选择 strict FIFO 还是低延迟覆盖。状态流偏向最新值，命令/事件流需要顺序和拒绝反馈。RPC 在请求 reader 上附加 reply writer，同一连接通常顺序处理；长任务不应占住 InputUnit，应转成有界任务与 request id。

### 状态流、事件流与 RPC 不能共享默认策略

| 数据类别 | 合理默认 | 过载时要避免 |
|---|---|---|
| 位姿、关节状态、图像预览 | latest/覆盖旧值 | 消费已经过期的长队列 |
| 任务事件、轨迹段 | strict、有界拒绝 | 静默覆盖尚未执行的事件 |
| 速度/力矩命令 | 序号、deadline、失效安全值 | 重连后重放陈旧命令 |
| RPC 查询 | request ID、超时、幂等定义 | 把 TCP ACK 当业务完成 |
| 日志/记录 | 批量、独立慢路径 | 让 recorder 反压控制输出 |

中间件提供的是连接与缓冲机制，业务必须为每类数据选择语义。一个全局 strict 开关无法同时满足低延迟状态和不丢事件。

## Protocol/Carrier 的扩展边界

Protocol 保存连接阶段和 Route，Carrier 负责识别、握手、framing 与 ack，TwoWayStream 持有实际 I/O。Carrier registry 使用 prototype/factory 为每连接创建独立实例，modifier 使用 Decorator 组合压缩、监视等行为。

插件 ABI、modifier 顺序、未知 header 与半握手关闭都是安全边界。wire format 必须使用固定宽度与端序，不能直接发送 C++ struct。ACK 还要说明确认点是协议接收、Reader 返回还是业务动作完成。

### 设计模式是扩展约束

- Facade：Port 隐藏 PortCore、Unit 和 Protocol，但必须暴露足够的失败与关闭语义；
- Abstract Factory/Prototype：Carrier registry 按名字制造每连接实例，prototype 不得承载连接私有状态；
- Strategy：不同 Carrier 替换 framing 与 I/O 策略，PortCore 不出现逐协议 switch；
- Decorator：modifier 包装连接行为，组合顺序必须定义且可诊断；
- Observer：PortReport 接收连接事件；若应用回调会重入 Port 管理操作，必须核对报告发生时是否持有 PortCore 锁；
- Active Object：后台 write 把发送移到按需建立的 OutputUnit 线程；本版 busy 时跳过新写而非将工作加入有界多项队列。

判断模式是否实现正确，要看所有权、失败和关闭，而不是只看类图形状。

## 章节顺序

《阅读基础》先建立对象层次；《PortCore 架构》讲控制面和连接表；《写入数据路径》讲扇出；《Protocol 与 Carrier》讲 wire 行为；《输入与 RPC》讲反向路径；《Interrupt 与 Close》收束并发资源。

更具体的阅读任务如下：

1. 在 Port 门面中找到 `write()` 进入 PortCore 的唯一入口，记录参数是借用还是复制；
2. 在 PortCore 中确认 `m_stateMutex` 包围 `m_units` 遍历与 `unit->send()`；
3. 进入一个 OutputUnit，确认同步/后台执行、busy 处理和 completion 所在线程；
4. 沿 Protocol 找到一帧开始、payload 写入、结束和 ACK 的边界；
5. 回到 InputUnit，确认 frame 限界、Reader callback 和 reply writer 寿命；
6. 最后沿 interrupt/close 验证所有阻塞点都能被唤醒，所有 worker 都被等待。

完成这一轮后再读 BufferedPort 和 RPC，才能把高级 API 放回正确内核路径。

## 设计取舍

Carrier 插件提供异构协议灵活性，也扩大 ABI、安全和组合测试矩阵；每连接 Unit 隔离慢连接，但线程与 fan-out 成本随连接数线性增长；BufferedPort 降低时序耦合，但必须选择低延迟丢旧或 strict 积压。

Name Server 提供动态拓扑与交互式调试，代价是启动顺序、注册陈旧和配置拼写在运行期暴露。Bottle 适合探索，生成 Thrift/Portable 类型更适合稳定设备协议；前者不应因为方便而成为所有长期接口的默认格式。

## C++ 能力

重点是 Facade 隐藏复杂状态、Carrier 注册表创建连接协议、modifier 组合连接行为、PortWriter 双分派，以及以 PortCore 状态锁协调 raw Unit 表。OutputProtocol 的 shared_ptr 局部副本只保护 Protocol 对象，不表示 Unit 由共享所有权管理。

还需要理解 PImpl 稳定公开 ABI、虚析构保证插件派生类完整释放、删除复制保护资源 owner、`std::exchange` 实现移动所有权、借用指针的 callback 生命周期，以及 atomic 完成门为何不能替代跨字段 mutex。

## 性能与故障边界

关键变量包括连接数、payload 大小、同步/后台写、Carrier header、modifier 缓冲、ACK RTT、BufferedPort 容量和 Reader WCET。需要分别测逻辑 payload 与 wire bytes、序列化/复制次数、最慢 Unit、drop/overwrite、RPC 排队年龄和 close/join 时间。

故障测试应覆盖 Name Server 不可用、握手半途断开、frame 截断、对端不 ACK、Reader 抛错/返回失败、后台 write 取消、callback 内 close 和插件缺失。正常 demo 无法证明生命周期安全。

## 可行性估算

设连接数为 `N`、payload 大小为 `S`、发送频率为 `f`、每连接编码/复制成本为 `E(S)`：朴素逐连接扇出的 CPU 主项约为 `N × f × E(S)`，wire 带宽约为各 Carrier 实际 frame 大小之和。若编码结果相同，可共享不可变 serialized buffer，把应用编码降为一次，但不同 Carrier header、压缩或文本模式仍需逐连接处理。

每条活动输入连接有自己的 InputUnit 执行上下文；OutputUnit 只在 wait 配置启用后台写时按需启动 worker。因而线程数不是简单固定为连接数，也不能用 `Σ(C_i × Smax)` 描述写队列内存：OutputUnit 对同一连接只允许一个在途写，busy 时新消息被跳过。关闭时间仍受最慢阻塞 I/O 的 interrupt 响应和各 Unit join 影响。

用于机器人控制时，应把数据年龄、最慢连接、drop/overwrite、队列高水位和安全输出延迟作为一等指标。平均吞吐很高但偶尔积压两秒旧命令，系统仍不可用。

## 最小复刻路线

先实现固定地址、单 TCP 连接和长度帧；加入 `LimitedReader` 与 PortReader/Writer；再做 PortCore 多连接扇出；随后增加 BufferedPort 和 RPC reply；再加入 Name Server/Route；最后才做 Carrier registry、modifier、后台写和动态插件。

复刻版验收条件可以包括：错误帧不污染下一帧；若采用 Unit 快照设计，慢连接不会持有 registry 锁；后台 payload 在完成前有效；过载语义明确；interrupt 唤醒阻塞 I/O；重复 close 幂等；Name Server 失败不阻止本地资源回收；插件卸载时不存在任何活动 Carrier。前两项是可选择的复刻策略，不应反向写成固定版 YARP 已有的行为。
