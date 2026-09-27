# YARP 使用教程：Carrier 选择、持久连接与运维诊断

这一章解决“端口已经能收发以后，系统怎样长期运行”的问题。主线不是罗列命令，而是把四类状态分开：Name Server 保存的名字与持久连接意图、`PortCore` 保存的活动连接、每条 Carrier 保存的协议状态，以及应用保存的数据新鲜度。只有知道状态属于哪一层，重连、监控与关闭才不会互相打架。

下文固定到 YARP 源码提交 [`91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`](https://github.com/robotology/yarp/tree/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9)。公开 API 契约以 [`Contactable.h`](https://github.com/robotology/yarp/blob/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9/src/libYARP_os/src/yarp/os/Contactable.h)、[`PortReport.h`](https://github.com/robotology/yarp/blob/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9/src/libYARP_os/src/yarp/os/PortReport.h) 为准，运行时行为继续追入 [`PortCore.cpp`](https://github.com/robotology/yarp/blob/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9/src/libYARP_os/src/yarp/os/impl/PortCore.cpp)。

## Carrier 选择

| 场景 | 起点选择 | 必须验证 |
|---|---|---|
| 可靠命令/RPC | TCP | timeout、慢对端、断连 |
| 可丢状态流 | UDP | MTU、丢包、乱序 |
| 同机大数据 | local/共享路径 | fallback、所有权、复制 |
| 多接收者 | mcast 或多 TCP | 网络支持、fan-out CPU |

Carrier 名和 modifier 纳入部署清单。动态组合会改变 framing、ack 和性能，不能只记录逻辑 Port 名。

## 持久连接

**教学代码（不是固定提交源码摘录）：**

```bash
yarp connect --persist /atlas/state:o /atlas/state:i tcp
```

端口暂时不存在时，Name Server 保留连接意图，并在两端出现时建立。持久连接适合启动顺序不确定的模块，但要清理废弃配置，避免旧模块上线后意外接入。

持久连接不是一条永不关闭的 TCP socket，而是一条由名字服务维护的期望关系：

```text
desired route in Name Server
        │ 两端名字当前均可解析
        ▼
request actual Carrier connection
        │ 任一端退出
        ▼
actual connection disappears
        │ 两端再次出现
        └───────────────> retry desired route
```

这一区分解释了两个常见现象：命令在端口尚未启动时也可能返回成功，因为保存意图已经成功；端口重启后连接重新出现，也不是旧 socket 复活，而是名字服务根据意图创建了新连接。可用以下命令完成完整生命周期，而不是只会“添加”：

**教学代码（不是固定提交源码摘录）：**

```bash
# 添加；两端尚不存在也可以登记成功
yarp connect --persist /atlas/state:o /atlas/state:i tcp

# 查看全部持久连接，或只看涉及某一端口的条目
yarp connect --persist
yarp connect --persist /atlas/state:o

# 删除期望关系；普通 yarp disconnect 不能替代这一步
yarp disconnect --persist /atlas/state:o /atlas/state:i
```

源码中的 `ContactStyle::persistent` 把“操作真实连接”切换为“调用 NameSpace 的 persistent API”。因此它是控制面配置，不应由每个业务进程在高速循环里反复提交。部署系统应拥有这份期望拓扑，并负责增删；业务进程只报告自己的 Port 是否打开、实际连接数是否达到预期。

## 管理接口

查看端口连接：

**教学代码（不是固定提交源码摘录）：**

```bash
echo "[list] [in]" | yarp admin rpc /atlas/state:i
echo "[list] [out]" | yarp admin rpc /atlas/state:o
```

管理接口能 add/del/list connection。[YARP port administration](https://yarp.it/latest/port_admin.html)

生产网络应限制谁能发管理命令；名字可见不等于获得重配权限。

## 诊断层次

```text
Name: yarp name list / exists / where
Connection: ping / admin list / PortReport
Protocol: carrier, modifier, ack, timeout
Data: sequence, envelope timestamp, schema
Application: BufferedPort pending, isWriting, callback time
```

Name Server 能解析但 connect 失败，重点查目标 listener 与 Carrier；连接 active 但无新鲜数据，查 sender、busy/strict policy 和 consumer。

## 用 PortReport 保存连接事实

命令行适合临时排查，长期运行还需要把连接事件变成指标。`PortReport` 可以接收连接建立和断开报告。官方接口注释明确指出：报告发生时 Port 仍可能被锁住，回调不能再操作同一个 Port，否则可能死锁；`PortCore::report()` 还说明回调运行在输入或输出连接线程中。因此回调只做一次有界复制，实际日志、指标和重连决策交给别的线程。

### 一份可以直接使用的连接事件快照

**教学代码（不是固定提交源码摘录）：**

```cpp
#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

#include <yarp/os/PortInfo.h>
#include <yarp/os/PortReport.h>

struct RouteEvent {
  bool incoming;
  bool created;
  std::string source;
  std::string target;
  std::string carrier;
};

template <std::size_t Capacity>
class EventRing {
public:
  bool tryPush(RouteEvent event) {
    std::lock_guard lock(mutex_);
    if (size_ == Capacity) {
      ++dropped_;
      return false;
    }
    slots_[(head_ + size_) % Capacity] = std::move(event);
    ++size_;
    return true;
  }

  std::optional<RouteEvent> tryPop() {
    std::lock_guard lock(mutex_);
    if (size_ == 0) return std::nullopt;
    auto result = std::move(slots_[head_]);
    head_ = (head_ + 1) % Capacity;
    --size_;
    return result;
  }

  std::uint64_t dropped() const noexcept {
    return dropped_.load(std::memory_order_relaxed);
  }

private:
  mutable std::mutex mutex_;
  std::array<RouteEvent, Capacity> slots_;
  std::size_t head_{0};
  std::size_t size_{0};
  std::atomic_uint64_t dropped_{0};
};

class ConnectionReporter final : public yarp::os::PortReport {
public:
  explicit ConnectionReporter(EventRing<256>& events)
      : events_(events) {}

  void report(const yarp::os::PortInfo& info) override {
    if (info.tag != yarp::os::PortInfo::PORTINFO_CONNECTION) {
      return;
    }
    events_.tryPush(RouteEvent{
        info.incoming,
        info.created,
        info.sourceName,
        info.targetName,
        info.carrierName});
  }

private:
  EventRing<256>& events_;
};
```

这段代码刻意没有在 `report()` 中打印日志。`std::string` 复制仍可能分配内存，但工作量有上限；若连接抖动也必须满足硬实时约束，可把名字映射成预注册的整数 id，再使用固定大小字符数组。这里使用普通互斥锁而不是声称“无锁”：连接事件频率通常远低于数据频率，可证明的简单正确性比复杂的 lock-free 环形队列更重要。

`PortInfo` 是回调参数的借用引用，回调返回后不能保存其地址。`RouteEvent` 逐字段复制，把生命周期从 YARP 连接线程转移到应用队列。`ConnectionReporter` 自身也不是由 Port 所有；调用 `port.setReporter(reporter)` 后，它必须活得比所有可能回调更久。安全的成员声明顺序是让 reporter 在 port 之后析构，并让它一直存活到连接线程全部关闭：

**教学代码（不是固定提交源码摘录）：**

```cpp
port.interrupt();
port.close();          // 连接线程收口期间仍可能报告断开
port.resetReporter();  // 此时 reporter 仍然存活
```

固定源码中的 `PortCore::report()` 从连接线程直接读取 reporter 指针，源码注释要求该地址在输入/输出线程生命周期内保持稳定。因此不要在端口仍活跃时频繁替换 reporter；更稳妥的设计是启动时安装一次，关闭完成后再解除。

`getReport(reporter)` 和 `setReporter(reporter)` 不是同一个动作：前者同步枚举当前连接，后者只订阅未来的连接/断开事件。启动监控时应先安装未来事件回调，再获取当前快照，并使用 `(source, target, carrier)` 作为键做幂等合并；否则两次调用之间建立的连接可能漏记。

应为每条 route 保存：源端、目标端、carrier、连接时间、最后一次数据时间、断开原因和重连次数。这样可以区分“连接从未建立”“连接刚断开”和“连接仍在但发送端不再产生数据”。

### 事件表的数据结构与代价

活动 route 适合存入 `std::unordered_map<RouteKey, RouteState>`：平均插入和查询为 `O(1)`，连接数为 `C` 时空间为 `O(C)`。哈希键必须包含 source、target 和 carrier；只按目标名索引会把同一目标上的 TCP 与 UDP 连接错误合并。需要稳定展示顺序时，在输出快照上排序，代价为 `O(C log C)`，不要为了页面排序把所有热路径更新换成树结构。

事件 ring 的容量应由“最大连接抖动速率 × 后台线程最长暂停时间”决定。例如最多 200 次事件/秒、指标线程可能暂停 2 秒，容量至少 400；再留出安全系数。队列溢出时递增 `dropped` 并触发一次全量 `getReport()` 重同步，比无界增长更安全。注意 `getReport()` 会遍历 `PortCore` 的 unit 集合，并在同步调用期间执行 reporter，所以它适合低频校准，不适合逐周期轮询。

## 多接收者下的部分失败

一个输出 Port 连接到记录器、可视化和控制器时，三条连接具有独立状态。慢记录器不应把系统状态简化成一个全局 `write_failed`。至少暴露：

| 事实 | 需要回答的问题 |
|---|---|
| output count | 当前到底连接了几个接收者 |
| write busy/skip | 本次是否因为后台发送尚未完成而跳过 |
| per-route disconnect | 哪条连接在何时断开 |
| sequence gap | 丢失发生在发送前、传输中还是消费端 |
| sample age | 即使有连接，数据是否仍然新鲜 |

若业务要求所有接收者确认同一命令，仅靠普通 streaming Port 不足，应在应用协议中加入命令 id、逐节点 ack、截止时间和汇总策略。中间件的 TCP ack 只确认传输层事实，不等于机器人已经执行动作。

## 关闭

停止生产，调用 `interrupt()` 唤醒阻塞 read，再让工作线程退出，最后 `close()` Port 并等待后台 write 完成。不要从另一线程直接析构仍被 read/write 使用的 Port。

一个可复用的接收器外壳应把这个顺序固化：

**教学代码（不是固定提交源码摘录）：**

```cpp
class Receiver {
public:
  bool start(const std::string& name) {
    if (!port_.open(name)) return false;
    stopping_.store(false);
    worker_ = std::thread([this] { run(); });
    return true;
  }

  void stop() noexcept {
    if (stopping_.exchange(true)) return; // 幂等
    port_.interrupt();                    // 唤醒阻塞 read
    if (worker_.joinable()) worker_.join();
    port_.close();                        // 不再有线程访问 port
  }

  ~Receiver() { stop(); }

private:
  void run() {
    while (!stopping_.load()) {
      auto* message = port_.read();
      if (message == nullptr) break;
      consume(*message);
    }
  }

  std::atomic_bool stopping_{true};
  yarp::os::BufferedPort<yarp::os::Bottle> port_;
  std::thread worker_;
};
```

`interrupt()` 是取消阻塞调用，`join()` 是线程已经退出的证据，`close()` 才释放端口资源。三者不能互相替代。析构函数调用幂等 `stop()`，使正常关闭和异常展开共享同一条资源路径。

还要理解 `read()` 返回值的所有权。`BufferedPort<T>::read()` 返回的 `T*` 指向端口内部缓冲对象，接收线程借用它完成当前处理即可；不要把裸指针塞进异步 worker 队列。若数据必须跨过下一次读取或 Port 关闭，应复制成自有对象，或者把需要的字段转换成业务 DTO：

**教学代码（不是固定提交源码摘录）：**

```cpp
while (auto* message = port_.read()) {
  WorkItem item{
      .sequence = message->get(0).asInt64(),
      .command = message->get(1).asString(),
  };
  if (!work_.tryPush(std::move(item))) {
    ++overload_drops_;
  }
}
```

`interrupt()` 之后，公开契约规定新的 read/write 会失败，除非显式 `resume()`。因此 `interrupt()` 不等于“只唤醒一次”；把它当成端口状态转换更准确。停止流程不应在 join 之前调用 `resume()`，否则刚被唤醒的工作线程可能再次进入阻塞读取。

在固定源码版本中，`PortCore::isWriting()` 会在状态锁下遍历所有未结束 unit 并检查 `isBusy()`，查询成本是 `O(C)`。它适合关闭时或低频指标，不适合每条消息都轮询。`PortCoreAdapter::finishWriting()` 最多等待约三秒并采用递增休眠，之后仍在写会记录错误；应用若需要严格的停机截止时间，应在自身状态机里提前停止生产并监视未完成发送，而不是把析构阶段当作可靠排空协议。

## 故障演练顺序

1. 杀死 Name Server：记录既有连接与新连接的差异；
2. 杀死单个接收者：确认其他 route 继续工作且报告断开；
3. 让接收者每条消息睡眠 500 ms：比较 strict 与非 strict 的内存、延迟和跳号；
4. 在 TCP 帧中途断网：确认 read 被唤醒、连接可清理；
5. 保留 persistent 规则后重启进程：确认恢复到预期实例；
6. stop 与断连同时发生：确认报告回调不重复、线程均可 join。

演练结果应包含时间线，而不只是“恢复成功”：故障发生时间、检测时间、安全动作时间、重新连接时间和第一条新鲜数据时间，分别对应不同恢复能力。

## 运维验收

- 单个慢 receiver 不让其他连接无指标地停顿；
- background write 的 buffer 生命周期正确，busy/skip 可见；
- Name Server 重启和网络分区有明确恢复行为；
- persistent connection 清单可审计；
- admin 命令有权限边界；
- interrupt 能唤醒阻塞 I/O，重复 close 幂等；
- 应用指标能区分 producer 慢、Carrier 丢包和 consumer 慢。
