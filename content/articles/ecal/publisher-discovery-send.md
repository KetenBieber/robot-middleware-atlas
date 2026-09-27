# Publisher 发送链：发现匹配、选层与多传输写入

eCAL 的 `CPublisher::Send()` 不是简单把 bytes 交给固定 socket。发送前，Publisher 已通过 registration 知道有哪些 Subscriber、它们位于本机还是远端、双方共同支持哪些 transport。发送时再用紧凑计数决定实际驱动 SHM、UDP 和 TCP writer。

本章固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`；TCP 传输子模块固定为 `eclipse-ecal/tcp_pubsub@352e711b9ef10fec42ba7536bda244f43bf092cc`。

本章沿公共 facade、PubGate、connection map、`DetermineTransportLayer()` 和 `CPublisherImpl::Write()` 还原完整路径。

## facade 不直接拥有实现对象

`CPublisher` 构造时创建 `shared_ptr<CPublisherImpl>`，随后把强引用交给全局 PubGate，自己只保存 weak pointer：

```text
CPublisher facade
  `-- weak_ptr<CPublisherImpl>
                 ^
                 |
CPubGate ---- shared_ptr
```

这种 Registry-owned Implementation 模式把实现对象寿命与全局注册状态绑定。析构 facade 时，它先 lock weak pointer，再请求 Gate unregister；Gate 删除强引用后才真正析构实现及 writer。

如果 Gate 尚未启动，注册失败会让局部 shared pointer 释放，facade 随即只剩过期 weak pointer。因而初始化顺序是公共 API 可用性的前置条件。

## PublisherImpl 初始不创建全部 writer

`CPublisherImpl` 构造时生成 entity id、topic identity 和统计状态，但 UDP、TCP、SHM writer 可以保持为空。

当 registration 表明存在需要某层的 Subscriber 时，才执行 `StartUdpLayer()`、`StartTcpLayer()` 或 SHM 初始化。

延迟创建的好处：

- 没有订阅者时不占用 socket、端口和共享内存；
- 只启用实际需要的传输；
- 同一 Publisher 可以随发现状态动态增加 layer。

代价是第一次连接存在启动延迟，writer 参数变化后还要通过下一轮 registration 告知 Subscriber。

## WriterAttributes 冻结构造期配置

全局配置与调用者覆盖项会被压平进 `eCALWriter::SAttributes`。其中包含：

- host/process/entity identity；
- network/local communication mode；
- 本机和远端 layer priority；
- UDP multicast 与最大 datagram size；
- TCP executor 参数；
- SHM buffer 数、reserve、zero-copy 和 ACK 设置。

属性在构造后主要只读。发送热路径不反复解析配置文件，也不在每条消息上拼装层参数。

不可变运行时配置减少锁和分支，但动态改配置通常意味着重建 Publisher 或显式更新对象，不能假设文件变化会自动生效。

## Registration 进入 PubGate

Registration receiver 解码 Subscriber 样本后，把它交给 `CPubGate::ApplySubscriberRegistration()`。Gate 按 topic name 找出全部同名 PublisherImpl，再把订阅者 identity 和 layer capability 传入每个实现。

```text
subscriber registration
  -> RegistrationSampleApplier
  -> CPubGate
  -> equal_range(topic_name)
  -> PublisherImpl::ApplySubscriberRegistration(...)
```

这里的 `CPubGate` 容器是 `std::multimap<std::string, std::shared_ptr<CPublisherImpl>>`。定位 topic 范围约为 `O(log P)`，遍历同 topic 的 K 个 PublisherImpl 约为 `O(K)`。与后文 `CSubGate` 使用的 `unordered_multimap` 不同，不能把 SubGate 的平均哈希查找复杂度搬到这里。

固定提交的 `CPubGate::ApplySubscriberRegistration()` 与注销函数都在 `std::shared_lock` 作用域中直接遍历迭代器并调用 `PublisherImpl`，没有先复制 shared pointer 快照。读锁允许其他读者并行，但会阻止 `Register`/`Unregister` 取得写锁；若首次匹配触发 writer 初始化、注册或等待，Publisher 索引的增删就会排队。这里的 iterator 所属 map 节点和其 `shared_ptr` 由仍持有的 map 锁保护，锁释放后函数也不再使用该 iterator。

## Layer capability 的双方能力集合

Subscriber registration 携带各传输层的 read-enabled 状态。旧协议节点可能没有动态 enable 字段，兼容代码需要根据协议版本填默认能力。

Publisher 自己也有 write-enabled 配置。某层可选的前提为：

```text
publisher can write layer
AND subscriber can read layer
AND deployment relation allows layer
```

SHM 只适合同一主机；跨主机即使双方代码都支持 SHM，也没有共同物理内存可映射。

## DetermineTransportLayer 把优先级表变成一次短扫描

`DetermineTransportLayer` 根据是否同机选择 local 或 remote priority vector，再从高到低寻找第一个双方支持层。

**教学伪代码（不是固定提交源码）：**

```cpp
for (Layer layer : configured_priority) {
  if (publisher_supports(layer) && subscriber_supports(layer)) {
    return layer;
  }
}
return none;
```

优先级放在配置，交集算法留在代码，使部署可以把本机首选 SHM、远端首选 TCP 或 UDP，而无需重新编译。

层数量很小，线性扫描比复杂 priority structure 更清楚。这里的主要成本不在算法，而在选择语义必须稳定、可诊断。

## 每个连接独立选择 layer

Connection key 通常包含 subscriber 的 host、process 和 entity identity。Value 保存 selected layer 与连接状态。

```text
Publisher camera/image
  +-- subscriber local viewer -> SHM
  +-- subscriber remote logger -> TCP
  `-- subscriber remote monitor -> UDP
```

Publisher 不是全局选择一个 transport。一次 Write 可能同时写三层，以服务不同连接集合。

这也解释了为什么 Subscriber 需要根据 publisher id/clock 去重：发现或切换期间，同一逻辑消息可能从多条路径到达。

## 两阶段连接状态

源码中的 Subscriber connection 从 absent 进入 pending，再在后续 registration refresh 中进入 established：

```text
absent --first sample--> pending --next refresh--> established
   ^                         |                         |
   `------ unregister/timeout+-------------------------+
```

首次样本已经选择 layer，并可能启动 writer；第二次连续看见同一实体才增加公共 connection count 并触发 connected event。

这给 writer 建立和反向 registration 传播留出窗口，但也使“内部 layer 已准备”和“公共 GetSubscriberCount 大于零”不是同一时刻。

它不是可靠握手协议。Registration 是周期软状态；网络丢包只会延后状态转换。

## Writer 启动后需要重新注册 Publisher

TCP writer 创建时监听端口可能由操作系统动态分配。SHM writer 也会产生实际 memfile name。Subscriber 要连接它们，必须通过 Publisher registration 获得这些参数。

```text
choose TCP
  -> start TCP writer
  -> obtain listening port
  -> Publisher Register()
  -> registration contains LayerParTcp.port
  -> remote Subscriber creates TCP reader connection
```

因此发现不是一次性“找到 topic”，而是控制状态循环：新连接触发资源创建，资源创建又改变下一轮发现信息。

## 公共 Send 的提前返回

`CPublisher::Send()` 先 lock weak pointer。实现已被 finalize 时返回 false；没有 established Subscriber 时，可能只更新统计并提前返回。

接着看 `Send` 的真实实现：

```cpp
bool CPublisher::Send(const void* const buf_, const size_t len_, const long long time_ /* = DEFAULT_TIME_ARGUMENT */)
{
  CBufferPayloadWriter payload{ buf_, len_ };
  return Send(payload, time_);
}

bool CPublisher::Send(CPayloadWriter& payload_, long long time_)
{
  auto publisher_impl = m_publisher_impl.lock();
  if (!publisher_impl) return false;
  // in an optimization case the
  // publisher can send an empty package
  // or we do not have any subscription at all
  // then the data writer will only do some statistics
  // for the monitoring layer and return
  if (GetSubscriberCount() == 0)
  {
    publisher_impl->RefreshSendCounter();
    // we return false here to indicate that we did not really send something
    return false;
  }

  // send content via data writer layer
  const long long write_time = (time_ == DEFAULT_TIME_ARGUMENT) ? eCAL::Time::GetMicroSeconds() : time_;
  return publisher_impl->Write(payload_, write_time, 0);
}

bool CPublisher::Send(const std::string& payload_, long long time_)
{
  return(Send(payload_.data(), payload_.size(), time_));
}
```

这三层重载把“应用给出的字节借用”沿同步调用栈送到 Impl：`CBufferPayloadWriter` 保存地址与长度，`weak_ptr::lock()` 让本次调用取得临时强引用；零订阅时只刷新发送统计，不调用 writer。若有订阅者，函数计算或沿用时间戳，再把 payload writer 引用传给 `CPublisherImpl::Write()`。因此 `Send()` 返回之前 payload 必须保持有效；能否更早复制、在哪个 transport 复制，要继续看下一节的 `Write()`，这个门面本身没有异步队列，也没有延长原始 buffer 的所有权。源码范围固定为 `CPublisher::Send()`。

这一优化避免无接收者时编码以后的传输成本，但也意味着 Publisher 不能被当作无条件日志 sink。调用者若要求始终保存数据，需要独立 recorder 或 file transport。

返回 true 只表示至少一个 writer 接受写入，不表示所有 Subscriber 已经执行 callback。

## PrepareWrite 生成共同消息身份

进入多层发送前，`PrepareWrite()`：

- 递增 Publisher clock；
- 更新频率与 payload size；
- 根据 publisher entity id 与 clock 计算 send hash；
- 保存发送时间。

随后 SHM、UDP、TCP 使用同一组：

```text
publisher id
clock
hash
timestamp
payload length
```

共同 identity 让 Subscriber 能识别不同层传来的同一逻辑样本，也让监控把一条发送关联到连接和统计。

这里的 hash 是运行时去重键，不应直接当作跨实现持久协议标识。

## Write 的多层调度

`CPublisherImpl::Write()` 的分支骨架可以简化为下面的教学伪代码；它有意不模拟首次写入时的 writer 准备、registration 更新和逐层错误处理，不能当作固定提交原文：

**教学伪代码（不是固定提交源码摘录）：**

```cpp
bool use_shm = shm_connections.load() > 0;
bool use_udp = udp_connections.load() > 0;
bool use_tcp = tcp_connections.load() > 0;

PrepareWrite(metadata);

bool written = false;
if (use_shm) written |= shm_writer->Write(payload, metadata);
if (use_udp) written |= udp_writer->Write(payload, metadata);
if (use_tcp) written |= tcp_writer->Write(payload, metadata);
return written;
```

热路径读取原子计数，不遍历 connection map。复杂度相对连接数近似常数，相对启用 layer 数最多三次 writer 调用。

`|=` 表示任一层成功即可返回 true。部分成功需要监控按 layer 观察，否则应用只看 bool 无法知道 TCP 成功而 UDP 失败。

## 网络层存在时共享 payload 副本

纯 SHM zero-copy 模式可以让回调直接写入或查看共享内存 buffer。UDP/TCP 则需要一段在发送期间稳定的连续内存。

当任一网络层启用时，Publisher 通常先建立共享 payload buffer，供多层复用：

```text
caller bytes
  -> one stable shared buffer
       +-> UDP framing
       +-> TCP envelope/queue
       `-> SHM copy path if zero-copy disabled
```

这避免为 UDP 和 TCP 分别复制原始 payload，但 TCP 子库仍可能为了自己的 framing 和异步队列再次复制。

“支持 zero-copy”必须附带条件：仅 SHM、具体 API 模式、buffer 生命周期和 callback 锁域。只看产品功能列表无法推断一次实际 Send 是否复制。

## UDP writer 的语义

UDP writer 序列化 eCAL sample envelope，再交给 ecaludp 子模块。超过最大 datagram size 时，子模块做应用层分片。

发送成功表示同步 socket 操作接受了一些/全部数据报，不表示远端完成重组。任一分片丢失会使完整大消息不可用。

UDP 适合重视新鲜度、允许丢包的监控和状态流。大图像若经 UDP 分成很多片，完整成功概率与网络拥塞风险明显恶化。

## TCP writer 的每连接最新待发样本

从 eCAL `CDataWriterTCP::Write()` 继续向下，调用落到固定子模块 `eclipse-ecal/tcp_pubsub` commit `352e711b9ef10fec42ba7536bda244f43bf092cc`。主仓库在 eCAL commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 的 gitlink 固定了这个子仓库版本。每个 `PublisherSession` 同时至多有一个异步写入进行，并以一个 `next_buffer_to_send_` 保存待发 buffer；新的 buffer 会替换这个待发位置。

在进入会话层之前，`Publisher_Impl::send()` 从 buffer pool 取得拥有型 `vector<char>`，为 TCP 子协议头和所有 payload 段计算总长，随后把 eCAL writer 给出的 header/payload 指针对应的 bytes 同步 `memcpy` 到这块帧存储，再把同一个 `shared_ptr` 交给每个连接会话。因而异步写开始后，Asio 持有的 `buffer` 捕获保证它所读的字节仍存在；上层原始 payload 不需要一直活到网络完成，但它必须活到这个同步复制结束。这个复制步骤位于固定 `eclipse-ecal/tcp_pubsub@352e711b9ef10fec42ba7536bda244f43bf092cc` 的 `Publisher_Impl::send()` 调用中。

**固定提交源码摘录（`eclipse-ecal/tcp_pubsub@352e711b9ef10fec42ba7536bda244f43bf092cc`，`PublisherSession::sendDataBuffer()` 与 `sendBufferToClient()`，完整连续函数，无省略）：** 输入是已经组装好的 `shared_ptr<vector<char>>` 网络帧；函数按会话状态决定立即发起异步写，还是覆盖唯一的 pending 指针；写完成 handler 再取出当时最新 pending buffer。

```cpp
  void PublisherSession::sendDataBuffer(const std::shared_ptr<std::vector<char>>& buffer)
  {
    if (state_ == State::Canceled)
      return;

#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
    std::stringstream buffer_pointer_ss;
    buffer_pointer_ss << "0x" << std::hex << buffer.get();
    const std::string buffer_pointer_string = buffer_pointer_ss.str();
#endif

    {
      const std::lock_guard<std::mutex> next_buffer_lock(next_buffer_mutex_);

      if ((state_ == State::Running) &&  !sending_in_progress_)
      {
        // If we are not sending a buffer at the moment, we can directly trigger sending the given buffer
#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
        log_(logger::LogLevel::DebugVerbose, "PublisherSession " + endpointToString() + ": Trigger sending buffer " + buffer_pointer_string + ".");
#endif
        sending_in_progress_ = true;
        sendBufferToClient(buffer);
      }
      else
      {
#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
        log_(logger::LogLevel::DebugVerbose, "PublisherSession " + endpointToString() + ": Saved buffer " + buffer_pointer_string + " as next buffer.");
#endif
        // Store the new buffer as next buffer
        next_buffer_to_send_             = buffer;
      }
    }
  }

  void PublisherSession::sendBufferToClient(const std::shared_ptr<std::vector<char>>& buffer)
  {
    if (state_ == State::Canceled)
      return;

    asio::async_write(data_socket_
                , asio::buffer(*buffer)
                , asio::bind_executor(data_strand_,
                  [me = shared_from_this(), buffer](asio::error_code ec, std::size_t /*bytes_to_transfer*/)
                  {
#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
                    std::stringstream buffer_pointer_ss;
                    buffer_pointer_ss << "0x" << std::hex << buffer.get();
                    const std::string buffer_pointer_string = buffer_pointer_ss.str();
#endif
                    if (ec)
                    {
                      me->log_(logger::LogLevel::Warning, "PublisherSession " + me->endpointToString() + ": Failed sending data: " + ec.message());
                      me->sessionClosedHandler();
                      return;
                    }

                    if (me->state_ == State::Canceled)
                      return;

                    {
                      const std::lock_guard<std::mutex> next_buffer_lock(me->next_buffer_mutex_);

                      if (me->next_buffer_to_send_)
                      {
#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
                        me->log_(logger::LogLevel::DebugVerbose, "PublisherSession " + me->endpointToString() + ": Successfully sent buffer " + buffer_pointer_string + ". Next buffer is available, trigger sending it.");
#endif
                        // We have a next buffer!

                        // Copy the next buffer to send from the member variable
                        // to a temporary variable. Then delete the member variable,
                        // so when adding a new buffer as next buffer, it is clear
                        // that we now have taken ownership of that buffer.
                        auto next_buffer_tmp             = me->next_buffer_to_send_;

                        me->next_buffer_to_send_         = nullptr;

                        // Send the next buffer to the client
                        me->sendBufferToClient(next_buffer_tmp);
                      }
                      else
                      {
#if (TCP_PUBSUB_LOG_DEBUG_VERBOSE_ENABLED)
                        me->log_(logger::LogLevel::DebugVerbose, "PublisherSession " + me->endpointToString() + ": Successfully sent buffer " + buffer_pointer_string + ". No next buffer available.");
#endif
                        me->sending_in_progress_ = false;
                      }
                    }
                  }
                ));
  }
```

锁保护的是 `sending_in_progress_` 与 `next_buffer_to_send_` 之间的会话发送不变量：正在写时只保留一个后继帧。异步 handler 通过 `me = shared_from_this()` 保持会话对象存活，通过 `buffer` 捕获保持 Asio 正在读取的字节存活；handler 被投递到 `data_strand_`，同一 strand 上的 handler 不会并发执行。Asio 完成 handler 只表示本地异步写操作完成或报错，TCP 对端应用何时读取和处理仍未知。

慢速机器人遥测订阅者若让一次帧写入持续 100 ms，期间 30 Hz 相机的约三个新帧会反复替换同一个 pending 槽，完成后只接着发最新帧。可观察结果是中间帧跳过而队列内存不会按帧数无界增长；代价是该策略不适合必须逐条交付的控制命令或事件日志。TCP 字节流的可靠性仅覆盖实际被提交到流的字节，不能取消中间件在提交前主动覆盖旧样本的语义。

这意味着：

- TCP 可靠保证的是实际进入流的 bytes；
- `CDataWriterTCP::Write()` 把同步序列化出的 eCAL 头与 payload 交给子模块后返回；子模块还会复制成其拥有的帧 buffer，再交给每个会话；
- 每个慢会话只有一个可被覆盖的待发样本，因此新鲜度优先，不能视为 FIFO；
- Publisher `Send()` 返回时，远端未必已收到；
- 慢 Subscriber 不一定反压整个 Publisher，而可能看到跳帧。

可靠传输与可靠应用交付是两层不同保证。

## SHM writer 的语义

SHM writer 把 header 与 payload 写入命名共享内存 buffer，再通过命名事件通知观察者。可选 ACK 会让发布者等待订阅进程确认。

无 ACK 时更偏向吞吐与最新值；有 ACK 时更接近“已通知的订阅者完成读取”，但发布端阻塞上界受 ACK timeout 和订阅者健康状态影响。

零拷贝读还可能让 subscriber callback 持有跨进程 named mutex，使业务 WCET 进入 publisher 的写阻塞时间。

## 层计数维护必须与 map 更新一致

Connection 新建、selected layer 改变、unregister 或 timeout 时，都要同步增减对应原子计数。

```text
map says 2 SHM connections
counter says 1
```

这种不一致会导致 Send 漏写或无谓写入。更新应当在同一控制面临界区形成事务，并为计数 underflow 设置断言。

这是冗余摘要结构的共同代价：读路径更快，写路径必须严格维护一致性。

## 发送性能账本

设 payload 大小 S，启用层数量 L：

```text
PrepareWrite                   O(1)
connection decision            O(L), L <= 3
stable payload copy            O(S), when network layer requires it
SHM copy                       O(S), unless eligible zero-copy path
UDP framing/send               O(S) + datagram syscalls
TCP envelope/queue             O(S) + session queue cost
```

连接数量主要影响 registration 控制面，不应直接线性进入每次 Send；真正影响热路径的是启用几层、是否复制和 writer 内部背压。

## “零拷贝”需要说明省掉的是哪一次复制

`CPublisherImpl::Write()` 里的 `allow_zero_copy` 指的是能否绕过成员缓冲区 `m_payload_buffer`，让 `CPayloadWriter` 直接把内容写向 SHM 映射目标。它不等于原始应用 buffer 被直接映射到另一个进程：普通 `CPublisher::Send(buf, len)` 包装的是 `CBufferPayloadWriter`，后续仍会把 bytes 写进映射。接收端是否把共享映射复制到本地缓冲，是另一个独立开关；两端必须分开讨论。

发送调用已经进入 `CPublisherImpl::Write()`，此处的输入是调用者提供的 payload writer，函数先根据当前连接计数和配置判断能否直接走 SHM：

接着看 `CPublisherImpl::Write()` 的真实实现：

```cpp
bool allow_zero_copy(false);
#if ECAL_CORE_TRANSPORT_SHM
allow_zero_copy = m_attributes.shm.zero_copy_mode;
#endif
#if ECAL_CORE_TRANSPORT_UDP
allow_zero_copy &= !udp_send_enabled;
#endif
#if ECAL_CORE_TRANSPORT_TCP
allow_zero_copy &= !tcp_send_enabled;
#endif

if (!allow_zero_copy)
{
  m_payload_buffer.resize(payload_buf_size);
  payload_.WriteFull(m_payload_buffer.data(), m_payload_buffer.size());
}
```

这里的 `m_payload_buffer` 是 `CPublisherImpl` 的成员 `std::vector<char>`，不是本次调用的局部临时对象。网络层启用时，代码先把调用者 payload 写入这块可复用的连续内存，再将同一段地址交给 UDP/TCP，并在 SHM 路径中用 `CBufferPayloadWriter` 包装后写入映射。仅 SHM 且配置允许时，才跳过这一步 staging copy，直接把原始 `payload_` 传给 SHM writer。`CPayloadWriter::WriteFull(target, size)` 的职责是把对象内容写进给定目标；对于 `CBufferPayloadWriter`，这仍然是一次内存复制。这里还有一个错误传播边界：接口返回 `bool`，但固定 `CPublisherImpl::Write()` 没有检查它，仍继续调用 writer。若自定义 `CPayloadWriter` 返回 false 或只写了前半段，短 payload 的后半段可能残留上次 Send 的字节，调用者却仍可能观察到某层 writer 成功；默认 buffer writer 正常路径会完整 memcpy，但这不能替自定义派生类兜底。复刻时应检查并传播该返回值，或者在接口中明确失败如何中止整次多层发送。完整 receiver 侧的借用语义见[SHM 数据路径](shm-memory-protocol.md)。

另一个容易漏掉的约束是，同一 `CPublisher` 并发调用 `Send()` 时，固定提交没有一把覆盖整次 `CPublisherImpl::Write()` 的串行锁。两个线程会共同访问 `m_payload_buffer`、`m_clock` 等成员；SHM writer 还会递增普通 `size_t m_write_idx`。例如两个相机线程同时发送 8 MB 帧，其中一个线程扩容并覆盖 staging vector，另一个线程仍把 `data()` 交给 writer，结果可能是数据竞争、帧内容混杂或未定义行为。需要多线程发布时，应在应用侧串行化同一个 Publisher，或给不同生产者使用独立 Publisher，再明确合并顺序；原子计数器只保护连接数，不能替代对整条 Send 状态的互斥。

## 可复刻的选层结构

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
enum class Layer { Shm, Tcp, Udp };

struct Capabilities {
  bool shm;
  bool tcp;
  bool udp;
};

std::optional<Layer> SelectLayer(
    const std::vector<Layer>& priority,
    Capabilities writer,
    Capabilities reader,
    bool same_host);
```

先把它写成无锁纯函数，用表格覆盖全部组合。再建立：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct Connection {
  EndpointIdentity reader;
  Layer selected;
  enum { Pending, Established } state;
  TimePoint last_seen;
};
```

最后才加入 layer counters 和按需 writer 生命周期。每引入一种缓存摘要，都增加对应一致性测试。

## 发送链的设计结论

eCAL Publisher 的核心能力是把发现结果转成数据路径：PubGate 将 registration 送到具体实现，connection map 为每个订阅者选择 layer，原子计数把丰富控制状态压缩成发送热路径判断，Write 再按需驱动多条 writer。

它的优势是部署自适应与连接级选层；复杂度来自软状态、延迟 writer 创建、多层去重和不同传输的背压差异。理解这些条件后，`Send()` 的 bool 才不会被误读成端到端交付确认。
