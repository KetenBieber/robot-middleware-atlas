# PortCore 架构：名字解析、连接 Unit 与控制面

`PortCore` 是 YARP Port 的真正运行时。它管理公开名字、监听 Face、输入输出 unit 集合、Reader、报告器以及 interrupt/closing 状态。

本文源码固定为 `robotology/yarp@91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`。下面直接摘录 `Port::open`、`PortCore::sendHelper` 和 `PortCore::closeMain` 等符号的连续实现；摘录均来自这一提交。

## 功能边界：协调连接而不解释业务消息

公开 `Port` 只提供 open、read、write、interrupt、close 等门面。PortCore 把这些调用变成监听器、连接 Unit 和协议对象的生命周期，但不理解 Image、Bottle 或设备命令的字段。业务序列化留给 PortReader/PortWriter，wire framing 留给 Protocol/Carrier。

```text
Port facade             稳定用户 API
  -> PortCore           状态、连接表、线程与控制面
       -> Unit          每连接执行状态
            -> Protocol 连接状态机
                 -> Carrier / stream
```

这个分层避免 PortCore 随业务类型和传输协议膨胀。代价是关闭、错误和所有权必须跨多个对象协调，不能靠销毁一个 socket 就完成。

固定提交里 PortCore 与每个 Unit 都继承 `ThreadImpl`。PortCore 自己的线程负责阻塞监听与接入新连接；InputUnit 的线程负责各自连接的数据读取；OutputUnit 默认可由调用 `write()` 的线程直接发送，只有开启后台写才按需启动长期线程。这里的“线程”是操作系统可调度的执行上下文；阻塞在 `Face::read()` 或 socket read 的线程会变为 blocked，通知/中断只让它有机会变为 runnable，CPU 何时真正执行仍由 OS 调度器决定。


```cpp
class YARP_os_impl_API PortCore :
        public ThreadImpl,
        public yarp::os::PortReader
```

这个声明说明 PortCore 既是 `ThreadImpl` 的派生类，也实现 `PortReader`；具体状态成员在类的 private 区域。该摘录只展示类头，接下来直接看 registry 和锁字段。

接着看 `PortCore` 的真实实现：

```cpp
private:
    std::vector<PortCoreUnit *> m_units;  ///< list of connections
    std::mutex m_stateMutex;
    std::condition_variable m_stateCv;
    std::mutex m_packetMutex;      ///< control access to message cache
    std::condition_variable m_connectionChangeCv; ///< signal changes in connections
```

`m_units` 是裸指针表，不会通过 `shared_ptr` 引用计数保活 Unit。`m_stateMutex` 协调其访问；`m_packetMutex` 保护 packet cache。锁的作用域要从实际调用处确认，不能因为成员相邻就假定两把锁保护同一状态。

## 打开 Port 同时建立本地监听与全局注册

概念流程为：

```text
Port::open("/camera/out")
  -> PortCore 配置名字与状态
  -> Carriers::listen(contact)
       -> Carrier 创建 Face(listener)
  -> listener 得到实际 host/port
  -> NameClient.registerName(name, contact)
  -> server loop 接受新连接
```

监听成功但注册失败、注册成功后进程崩溃、同名端口竞争，都会形成不同的清理路径。实现应在每个成功步骤后记录所有权，并在后续失败时逆序回滚。

## Name Server 是注册表而不是代理

建立 `/camera/out -> /vision/in` 连接时，客户端先解析目标 Contact，再直连目标 listener：

```text
source asks Name Server: where is /vision/in?
Name Server returns tcp://10.0.0.8:10042
source Carrier connects directly to 10.0.0.8:10042
```

因此现有连接可以在 Name Server 短暂失效时继续工作；新连接、重连和名字管理会受影响。运维监控必须分别检查名字服务健康与数据连接健康。

## Unit 是一条连接的活动对象

固定版本在 `PortCore::sendHelper()` 中先持有 `m_stateMutex`，再锁住 `m_packetMutex` 更新 packet，随后遍历 `m_units` 并调用每个输出 Unit。下面从 state lock 建立处连续摘录到 fan-out 循环结束：


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

局部 `lock` 的析构点在整个函数作用域末尾，因此 `unit->send()` 执行期间 `m_stateMutex` 仍被持有；`m_packetMutex` 则只在 packet 引用计数和 cache 检查的小段代码里持有。普通输出默认同步配置时，序列化和连接写可能阻塞在 `unit->send()`，于是并发的连接增删也会等待 state lock。若相机以 30 Hz 写入，而某个控制器连接的 socket 缓冲区已满，`send()` 等待期间新增输出或断开管理都会排队；这是源码可推导出的取舍，不是对作者历史意图的断言。OutputUnit 内的 `op` 成员另有 `shared_ptr<OutputProtocol>`，不能把这个 Protocol 层所有权误说成 Unit 租约。

一种常见的重设计会复制共享句柄，在锁外做可能阻塞的 I/O：

```text
[lock] clone active unit handles [unlock]
for each handle:
    unit->send(...)     // 可能阻塞，在锁外
```

这并非本提交的写路径。`PortCore::sendHelper()` 持 `m_stateMutex` 遍历 `m_units` 并直接调用 `unit->send()`；默认同步设置下，序列化及连接写入可能发生在此临界区。这个实现用较长临界区维持 raw Unit 指针稳定性，代价是若一个连接阻塞在慢写，其他需要 `m_stateMutex` 的连接管理操作也要等它结束。重设计的 shared_ptr 快照可讨论为替代方案，不能写成 YARP 当前实现。

## Listener 接受连接后选择 Carrier

被动端从初始 bytes 识别 Carrier：

```text
Face::read initial header
  -> Carriers::chooseCarrier(header)
  -> create InputProtocol
  -> expect sender specifier / route
  -> respond to header
  -> create InputUnit
```

未知 header 必须在有限读取量内失败，不能无限等待攻击者补齐。握手阶段也需要 timeout、最大 header 长度和 Carrier 白名单。

## 管理命令与普通数据共用 Port 边界

PortCore 还处理连接管理、询问状态和可能的管理流量。管理命令不能绕过认证或把任意字符串直接解释为插件配置。工业封装应将管理面与数据面权限分开，并记录谁创建或拆除了连接。

## PortReport 提供连接事件

报告器可以观察 active/inactive 连接及 Route。事件由不同控制路径触发；在应用把 Reporter 回调写成会重入 Port 的逻辑前，应逐调用点确认通知时是否持有 `m_stateMutex`。不能仅凭 Observer 角色认定回调必定锁外运行。若回调阻塞，触发它的 server/Input/Output 执行上下文也会延后继续处理。

报告事件还不等于数据完整性。需要结合应用 sequence、timestamp 和 drop counter，才能区分连接存在但没有数据、OutputUnit busy skip，以及接收端处理过慢。

## 连接表的性能边界

添加和删除 unit 是控制面操作，使用互斥与线性清理；一次 fan-out 为 `O(C)`，固定默认同步写会把 Unit 调用纳入 `m_stateMutex` 临界区。输入连接启动自己的 Unit 执行线程；输出连接仅在后台写配置下按需启动线程，因此不能简单按“一条连接一个 worker”估算全部线程资源。

小规模机器人图中这种隔离简单可靠；数千连接时线程和遍历模型会成为瓶颈，更适合事件循环或批量 I/O 架构。

## 最小 PortCore 复刻

先只实现单 listener、一个输入和一个输出：


```cpp
class PortCore {
public:
    bool open(Contact);
    bool addOutput(Route);
    bool send(const PortWriter&, WriteOptions);
    void interrupt();
    void close();
private:
    State state_;
    std::shared_ptr<Face> listener_;
    std::vector<std::shared_ptr<Unit>> units_;
};
```

状态至少区分 Closed、Opening、Open、Interrupted、Closing。所有网络回调先取得稳定 unit/Protocol handle，再释放核心锁。之后再增加 NameClient 和多连接 fan-out。

## 打开过程是一笔跨本地与远端的事务

等价的 C++ 骨架如下：


```cpp
bool PortCore::Open(const Contact& requested) {
  if (!BeginOpening()) return false;
  auto new_face = carriers_.Listen(requested);
  if (!new_face) return FailOpen();

  const Contact actual = new_face->GetLocalAddress();
  if (!name_client_.Register(name_, actual)) {
    new_face->Close();
    return FailOpen();
  }
  if (!CommitOpen(std::move(new_face), actual)) {
    name_client_.Unregister(name_);
    return FailOpen();
  }
  return true;
}
```

局部 owner 在 commit 前持有 listener，提前返回自动清理本地资源；Name Server 注册属于外部副作用，仍需显式补偿。RAII 只能回收它拥有的对象，不能自动撤销远端目录状态。

Contact 是可连接地址，Route 还包含逻辑 from/to 与 Carrier。目标重新 open 后旧 Contact 可能过期，重连应重新解析并使用退避/抖动，不能无限攻击旧地址或在服务恢复时形成连接风暴。

## Unit 和 Protocol 的所有权处在不同层

Unit registry 是 raw pointer 表；同步删除依赖 PortCore 的状态锁以及 server-thread 完成后集中清理的顺序。OutputUnit 的 `std::shared_ptr<OutputProtocol>` 则保护连接协议对象。标准 shared_ptr 的引用计数允许不同 shared_ptr 实例并发增减同一控制块，但不等于同一个 shared_ptr 成员可被一个线程读取、另一个线程无锁 `reset()`。因此读源码时既要找到对象 owner，也要找到同一指针成员的同步规则。

连接表可用 vector，因为 fan-out 本来就要遍历全部连接；频繁按 ID 删除时可增加哈希索引。数据结构应匹配“读多写少、发送遍历全体”的工作负载，而不是机械换成哈希表。

Unit worker 若在退出时回调 PortCore 删除自身，必须在 Unit 锁外进行。否则 PortCore 持核心锁调用 close、worker 持 Unit 锁回调核心，就会形成 ABBA 死锁。

## 半构造连接不能进入公开 registry

对于自己实现的服务器，安全接入可采用：accept 后设置握手 deadline；只读取固定上限 header；从白名单选择 Carrier；验证 Route；创建完整 Protocol/Unit；最后提交 registry 并发布 active 报告。YARP 此版本的 PortCore server thread 从 `Face::read()` 取得 `InputProtocol*` 后调用 `addInput()`；`addInput()` 创建 `PortCoreInputUnit`、启动其执行上下文，再把 raw 指针放入 `m_units`。握手的具体上限和超时要回到 Carrier 与 stream 配置确认，不能从推荐顺序推导源码一定具备某个限制。

若一 accept 就把 Unit 暴露给 registry，发送线程可能取得仍在握手的 Protocol。攻击者还可建立大量连接却不补齐 header，耗尽 fd、线程和握手对象，所以半连接也必须计入并发与内存上限。

管理 disconnect 的完成点也要明确：只从 registry 移除响应快，但 worker 可能仍在退出；等待 interrupt + join 语义更强，却可能被远端阻塞拖慢。API 与 PortReport 必须区分“不可再发现”和“资源已完全回收”。

## Reporter 是观察者而非所有者

active/inactive 事件由 Unit 的报告路径发出。若事件处理器会反向 connect/disconnect，应检查对应的 `report()` 调用栈是否带着 PortCore 状态锁；只有源码能证明锁已释放，才可把“回调锁外执行”当作事实。

Reporter 若捕获 PortCore 强引用，而 PortCore 又拥有 Reporter，会形成所有权环。可使用弱引用，或在 close 开始时先注销 Reporter。连接事件只能证明拓扑变化，不能证明消息完整；仍需 sequence、timestamp、drop 与 busy 计数。

## 状态机与关闭证明

```text
closed/idle -> listening -> running
                    |          |
              interrupted   finishing
                               -> disconnect inputs/outputs
                               -> closing -> wake server read -> join
                               -> close units/listener -> unregister name
                               -> idle
```

这不是固定版里的单个 enum 状态机，而是由 `m_interrupted`、`m_finishing`、`m_closing`、`m_running`、`m_finished` 等字段共同表达的相位。`closeMain()` 先在状态锁下置 `m_finishing`，再逐个请求断开输入、拆除输出；随后设置 `m_closing`，通过连回本地 listener 的协议连接让阻塞的 server `Face::read()` 返回并 join。server thread 离开后，PortCore 才集中 close/join/delete Unit、关闭 Face、向 Reader 发送空消息并按 `m_controlRegistration` 注销名字。固定实现不是“快照全部 Unit 后锁外广播 interrupt”。

对于此实现，关闭后的主要观测点是：PortCore server thread 已 join；`closeUnits()` 已对每个非空 Unit 执行 close/join/delete 并清空 vector；Face 已关闭；Reader 收到结束更新后解绑；名字注销按配置执行。若要进一步声称注销失败不影响本地回收或重复并发 close 一定等待首个关闭者，还要继续核对 `NetworkBase::unregisterName()` 和上层互斥契约。

## 设计取舍、性能与复刻验收

PortCore 用 Facade 隐藏复杂状态，用每连接 Unit 隔离协议和输入执行状态。固定写路径仍在 `m_stateMutex` 下调用 Unit，默认同步时可能把 I/O 放在锁内；后台写才分配额外执行线程，并以跳过 busy 写而不是排队来限制每 Unit 同时工作。代价是调用者会感受到慢写，数据新鲜度可能因 busy skip 改变，跨对象关闭协议也较复杂。

除连接数外，应测 handshake 并发、核心锁等待、快照复制、Reporter callback、重连频率和 close/join p99。控制面平均负载低，不代表连接风暴不会阻塞数据面。

最小版之后依次加入握手 deadline、Unit 快照、Reporter 锁外通知、persistent reconnect 和两阶段 close。完成标准是：open 任一步失败无 listener/名字残留；未知连接不能无限占用资源；callback 内 disconnect 不死锁；Name Server 离线不阻止本地 close；并发 send/close 不产生 UAF。
