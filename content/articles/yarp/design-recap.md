# YARP 设计总结：从名字到可插拔通信内核

本文按本地固定提交 `91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9` 综合前面源码章节。尤其要把 [PortCore 的所有权与锁](portcore-architecture.md)、[OutputUnit 的扇出/异步指针寿命](write-fanout.md)和[关闭时的线程静默点](close-lifecycle.md)放进同一张运行时图；下文描述设计模式时，只作为源代码结构的归纳，不代替真实调用链。

YARP 将名字解析、连接管理和传输协议分层：Name Server 维护 name-to-Contact 控制面，PortCore 管理本地连接图，Unit 隔离每条连接，Protocol 管理会话状态，Carrier 实现握手与 framing，PortReader/Writer 隔离应用对象和连接编码。

## 总体架构

```text
Name Server <--- NameClient ---> Contact / Route
                                  |
Application -> Port -> PortCore --+
                       ├─ Face listener -> InputUnit -> PortReader
                       └─ OutputUnit -> Protocol -> Carrier -> Stream
```

## 设计模式

- Facade：Port 隐藏 PortCore；
- Registry：Name Server 解耦名字与地址；
- Strategy/Abstract Factory：Carrier 选择 transport 并创建协议对象；
- Decorator：modifier 包装 sender/receiver；
- Active Object：每连接 Unit 拥有执行状态；
- Command：PortWriter 把序列化动作传给 ConnectionWriter；
- Observer：PortReport 接收连接事件。

## 优点

- 运行时连接与异构 Carrier 灵活，适合实验机器人集成；
- 名字服务不在正常 payload 路径，控制面和数据面分离；
- Reader/Writer 接口统一流、RPC 和多种序列化目标；
- 每连接 Unit 隔离协议状态和部分慢连接故障；
- 内存连接可用于序列化与协议测试。

## 缺点与风险

- Carrier/modifier 动态组合扩大兼容与安全测试矩阵；
- 每连接线程和 `O(C)` fan-out 不适合极大连接图；
- 后台写的 writer、tracker 和 buffer 所有权复杂；
- 名字注册、连接状态和实际数据新鲜度是三套不同事实；
- ack、慢 Reader 和阻塞 stream 会扩大尾延迟；
- YARP 不是硬实时调度器，微秒控制环仍需专门执行与内存分析。

## 数据结构与性能结论

| 热点 | 复杂度/资源 | 风险 |
|---|---|---|
| PortCore unit 遍历 | `O(C)` | 连接数线性放大发布成本 |
| 多格式序列化 | `O(E × S)` | modifier/carrier 破坏缓存共享 |
| per-connection worker | `O(C)` threads/stacks | 调度和内存开销 |
| background buffers | `O(in_flight × S)` | 慢链路耗尽 pool |
| Name lookup | 控制面 RPC | 影响新连接，不代表数据链健康 |

## 最小复刻顺序

1. `Contact`、`Route` 和 URI/名字解析；
2. `PortReader`、`PortWriter`、内存 ConnectionReader/Writer 与 golden tests；
3. 单 TCP Carrier、Protocol 握手、frame、ack 和 timeout；
4. 一个 InputUnit、一个 OutputUnit 与可 interrupt/join 的线程；
5. PortCore 连接 registry、fan-out 与部分失败结果；
6. 后台 write、有限 buffer pool、completion tracker；
7. NameClient 注册/查询/注销；
8. Carrier registry、插件、modifier、UDP/local 和管理权限。

## 最小接口骨架

**教学代码（不是固定提交源码摘录）：**

```cpp
class Carrier {
public:
    virtual bool checkHeader(Bytes) const = 0;
    virtual bool handshake(Protocol&, const Route&) = 0;
    virtual bool write(Protocol&, const PortWriter&) = 0;
    virtual std::unique_ptr<Carrier> clone() const = 0;
};

class Protocol {
public:
    bool open(Route);
    std::unique_ptr<ConnectionReader> beginRead();
    bool endRead();
    bool write(const PortWriter&);
    void interrupt();
};
```

Carrier 实例必须每连接独立，Protocol 的状态转换必须拒绝半握手后的数据操作。

## 完成判定

- Name Server 停止后现有直连仍能传输，新连接给出明确失败；
- 同一 writer 扇出到不同 Carrier 时编码与 framing 均正确；
- busy/断开的单连接不会造成其他连接状态损坏，并有 per-route 结果；
- 半握手、半帧和不返回 ack 均能超时清理；
- Reader 不能越过本帧边界，恶意长度不能无界分配；
- interrupt 能唤醒 read/write，close 能 join 全部 Unit；
- close 与 network error 并发时 callback 恰好一次，重复 close 幂等；
- 指标能区分名字解析失败、连接断开、发送 skip、网络丢失和消费者慢。

## 一条连接如何贯穿全部组件

学习 YARP 时最容易把 Name Server、PortCore 和 Carrier 看成三套系统。用 `/camera` 向 `/viewer` 建立 TCP 连接，可以把它们连成一条主线：

```text
应用请求 connect("/camera", "/viewer")
  -> NameClient 查询两个名字对应的 Contact
  -> 输出 PortCore 创建 OutputUnit
  -> OutputUnit 建立 Stream，并为该连接 clone Carrier
  -> Protocol 用 Route + Carrier 完成握手
  -> PortCore 把 Unit 提交到活动连接表
  -> write(writer) 对活动 Unit 扇出
  -> Carrier 定义帧；PortWriter 定义内容
  -> 对端 InputUnit 校验帧并调用 PortReader
```

控制面的查询只负责找到端点；握手完成后，已有连接的 payload 不再经过 Name Server。`Route` 描述“从谁到谁、使用什么 carrier”，`Protocol` 保存这一次会话的阶段，`Carrier` 则提供某一传输规则。三者若合并成一个全局 socket 管理器，就会让协议插件共享状态，并使半握手连接污染其他连接。

## 对象所有权与并发边界

| 对象 | 所有者 | 并发访问 | 关键不变量 |
|---|---|---|---|
| Port | 应用 | 应用线程 | 只作为门面，不直接持有裸工作线程 |
| PortCore | Port | 管理线程、读写调用者、Unit 回调 | 连接表遍历期间 Unit 使用 lease 保活 |
| InputUnit/OutputUnit | PortCore | 各自 worker 与管理线程 | close 后不再回调 PortCore 业务对象 |
| Protocol | 单个 Unit | 单连接线程 | 状态只能沿握手、数据、关闭方向前进 |
| Carrier | 单个 Protocol | 单连接线程 | 从原型 clone；实例不得跨连接共享可变状态 |
| PortWriter | 调用方或后台写任务 | 编码线程 | 生命周期覆盖全部异步发送 |
| completion tracker | 一次 fan-out | 多个 Unit 完成回调 | 每条路线至多完成一次，汇总也只完成一次 |

`PortCore::send()` 不能在持有连接表互斥锁时执行用户 `PortWriter`、socket I/O 或报告回调。常见实现是：在锁内取得活动 Unit 的稳定引用或 lease，立即解锁，再逐连接发送。关闭线程先从表中摘除 Unit，随后等待 lease 清零，避免“遍历拿到裸指针—另一线程删除连接”的 use-after-free。

## 最小扇出核心

下面的骨架展示同步与后台发送都需要保留的边界：

**教学代码（不是固定提交源码摘录）：**

```cpp
SendResult PortCore::send(std::shared_ptr<const Message> msg) {
    std::vector<UnitLease> targets;
    {
        std::lock_guard lock(units_mutex_);
        targets = acquire_active_leases(outputs_);
    } // 离开连接表锁后才编码和写 socket

    SendResult result;
    result.routes.reserve(targets.size());
    for (auto& target : targets) {
        if (!target.try_begin_write()) {
            result.routes.push_back({target.route(), SendCode::Busy});
            continue;
        }
        result.routes.push_back(target.write(*msg));
    }
    return result; // 部分成功不是布尔值能够表达的
}
```

真实系统还要处理多种编码目标。同一逻辑消息发给两个相同 carrier 时可以复用编码结果；发给文本 carrier 与二进制 carrier 时则不能盲目共享字节缓存。因此缓存键至少应包含协议/编码身份，并让缓存对象不可变。优化必须服从正确性，不能为了“只序列化一次”破坏每连接 framing。

后台写还需要一个有界所有权模型：

**教学代码（不是固定提交源码摘录）：**

```cpp
struct PendingWrite {
    std::shared_ptr<const Message> message;
    std::shared_ptr<Completion> completion;
    Route route;
};

bool OutputUnit::enqueue(PendingWrite job) {
    if (closing_.load() || !queue_.try_push(std::move(job))) return false;
    wake_worker();
    return true;
}
```

这里使用共享所有权不是因为它“方便”，而是因为调用返回后 worker 仍要访问消息与完成状态。队列必须有容量；满载时选择阻塞、丢最新、丢最旧或报告 busy，都是对外协议，不能藏在容器实现里。

## 控制面、数据面与故障面的观测

| 平面 | 成功事实 | 典型失败 | 应暴露的指标 |
|---|---|---|---|
| 名字控制面 | 名字解析为 Contact | 未注册、租约过期、Name Server 不可达 | lookup 延迟与失败原因 |
| 连接控制面 | 握手后 Unit 进入活动表 | carrier 不匹配、超时、认证失败 | 连接阶段、重试与断开原因 |
| 数据面 | 一帧被某条 route 接受 | busy、半帧、超长、写超时 | 每 route 字节、延迟、drop、queue depth |
| 应用消费面 | PortReader 完成处理 | Reader 慢、异常、拒绝消息 | 回调耗时与积压 |

“端口已注册”不表示“链路已连接”，“write 返回”也不必然表示“对端应用已消费”。接口和指标必须准确说明确认发生在哪一层，否则现场看到的绿色状态会掩盖真实数据中断。

## 从零实现的里程碑

第一阶段只做内存 `ConnectionReader/Writer`，用 golden bytes 固定整数、字符串、列表和错误边界；第二阶段实现单 TCP Carrier，并用分片输入验证半帧状态机；第三阶段加入单输入、单输出 Unit 与可中断线程；第四阶段才让 PortCore 管理多连接和部分失败；第五阶段加入有界后台发送；最后才增加 Name Server、插件 carrier 与远程管理。

每一步都应保留可运行样例：内存编码往返、单连接 echo、多连接 fan-out、慢消费者、握手超时、关闭竞态。这样协议错误、并发错误和发现错误不会在同一阶段混在一起。

达到这些条件，才说明复刻的是 YARP 的通信内核，而不只是一个带名字的 TCP 封装。
