# YARP C++ 设计实验：Port、虚接口与并发关闭

想象一台双臂机器人，一条状态消息要同时送给控制面板、可视化工具和远端记录器。三条连接可能使用不同的 Carrier：一种为低延迟本地传输，另一种按网络协议编码。最直接的 C++ 写法是在每一种状态消息中都加入 `sendUdp()`、`sendTcp()`，再让每一种 Carrier 都认识所有状态类型。假设有 `M` 种消息、`C` 种 Carrier，要维护的专用组合就逼近 `M × C`；新增一个 Carrier 时，会触碰本不该变化的关节状态类型。

正确的第一步不是记住某个模式名称，而是把变化拆成两次互不依赖的决定：**消息决定字段顺序和含义；Writer 决定这些字段怎样进入当前连接。** 本章先用一段完整的 C++17 小程序验证这个分离，再回到 YARP 的 `Portable`、`ConnectionWriter`、`PortCoreOutputUnit`，最后讨论跨线程时哪个对象拥有消息内存。所有实际行为以本地 YARP 提交 `91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9` 为基准；这里独立编写的示例不是上游源代码。

## 从 `M × C` 个专用函数走向两条动态分派边界

先写出最可能的失败设计。设有 `JointState`、`CameraFrame` 两种消息，以及 UDP、文件两种输出；把 `sendUdp(JointState)`、`sendFile(JointState)` 等四组函数写在同一个业务目录里，看上去容易调用，但当接入第三种 Carrier 时，两个消息类都得重新理解网络细节。若后续允许插件动态装载一个未知消息类型，传输库甚至无法提前编译出它的所有重载。

可以改为“字段生产者只调用一个抽象 Writer，抽象 Writer 不知道究竟是哪一种业务消息”。以下是独立、可编译的 C++17 教学程序，故意只保存两个整数来排除序列化格式干扰：

~~~cpp
#include <array>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

struct Writer {
    virtual ~Writer() = default;
    virtual bool appendInt32(std::int32_t value) = 0;
};

struct InspectWriter final : Writer {
    std::vector<std::int32_t> fields;
    bool appendInt32(std::int32_t value) override {
        fields.push_back(value);
        return true;
    }
};

struct RejectWriter final : Writer {
    bool appendInt32(std::int32_t) override { return false; }
};

struct Message {
    virtual ~Message() = default;
    virtual bool write(Writer& writer) const = 0;
};

struct JointState final : Message {
    std::array<std::int32_t, 2> q{12, 34};

    bool write(Writer& writer) const override {
        return writer.appendInt32(q[0]) &&
               writer.appendInt32(q[1]);
    }
};

bool encode(const Message& message, Writer& writer) {
    return message.write(writer);
}

int main() {
    JointState state;
    InspectWriter inspect;
    assert(encode(state, inspect));
    assert((inspect.fields == std::vector<std::int32_t>{12, 34}));

    RejectWriter reject;
    assert(!encode(state, reject));
}
~~~

现在像编译器一样逐句执行 `encode(state, inspect)`。`Message&` 指向 `JointState` 对象，但其静态类型并不包含 `q` 字段；第一个虚调用在运行时选择 `JointState::write()`。函数拿到的是抽象 `Writer&`，`writer.appendInt32` 再沿实际 `InspectWriter` 对象作第二次独立虚调用。成功路径产生两个字段，`RejectWriter` 则让第一次写入就失败，`&&` 短路阻止后续字段继续写入。

这不是经典 Visitor 模式要求的“两种具体类型互相选择一组专用重载”；这里发生的是**两层彼此独立的虚接口分派**。它把 `M × C` 的静态组合约束降为 `M` 个消息实现加 `C` 个 Writer 实现，代价是接口冻结、虚调用和无法在这里直接跨方法优化。归纳为设计模式时，`Message` 是消息字段的多态接口，`Writer` 是编码/传输策略边界；“有两个虚调用”本身不能证明整个实现遵循 Visitor。

### 回到固定源码：Portable 实际继承两个不同的契约

YARP 没有让应用手动传上面这个玩具 `Message`。固定版本真正的 `Portable` 定义如下：

~~~cpp
class YARP_os_API Portable : public PortReader, public PortWriter
{
public:
    // reiterate the key inherited virtual methods, just as a reminder
    bool read(ConnectionReader& reader) override = 0;
    bool write(ConnectionWriter& writer) const override = 0;

    virtual Type getType() const;
    static bool copyPortable(const PortWriter& writer, PortReader& reader);
};
~~~

这里 `PortReader` 与 `PortWriter` 分别承诺解码和编码，`Portable` 把两者合在一个双向可传输对象里。`override = 0` 有两个作用：编译器检查它们确实覆盖了父类签名，同时让 Portable 自身保持抽象，强制具体业务类型给出字段实现。它不能自动证明 read 和 write 在所有错误路径上对称；需要消息开发者自己保证协议版本、字段顺序和边界校验一致。

基于这个接口，可以构造一个只保留类型关系的业务类。下面的声明只用于说明继承与序列化接口之间的关系：

~~~cpp
class State final : public yarp::os::Portable {
public:
    std::int64_t sequence{};
    std::vector<double> joints;

    bool write(yarp::os::ConnectionWriter& writer) const override;
    bool read(yarp::os::ConnectionReader& reader) override;
};
~~~

`public` 继承允许 `State&` 隐式转换成 `Portable&`，PortCore 只需调用抽象序列化接口，不必包含 `State` 的字段定义。`final` 阻止继续从 `State` 派生，却不能禁止开发者修改本类的字段和 wire 格式；真正的网络兼容还需显式版本和兼容策略。`override` 会捕获少了 `const` 或参数类型不同造成的假重写。`write(...) const` 约束单次调用不通过普通成员修改状态，但不阻止 `mutable`、指针间接修改或并发的另一个线程，因此它**不提供线程安全保证**。

### 为什么不直接把 Writer 做成模板

`template<class Writer> bool write(Writer&)` 也能消除一部分虚调用，但调用点需要同时知道消息类型和 Writer 类型。两者以模板实例编译组合，插件装载未知新 Carrier 时还要重新构造可见的模板实例与链接边界。`Portable` 使用非模板虚接口的代价是调用间接性，换取消息模块与具体 Carrier 的独立编译。

还有一层比虚调用更容易成为实际瓶颈：固定源码中的 `ConnectionWriter::appendExternalBlock(const char*, size_t)` 明确让当前连接借用外部字节块，调用者必须保证传输完成前那段内存不消失。相比之下，`appendBlock` 承担复制语义。即便消息接口上的 `write` 只有一次虚调用，跨线程的 buffer 借用、序列化次数和 socket I/O 才决定能否安全地进行多连接扇出。`const` 只约束逻辑写入接口，并不是“可以随便把栈对象交给异步 Worker”的许可证。
## `const` 是可重复扇出的契约

`write(...) const` 表示序列化不应修改逻辑消息。一个 PortWriter 可能被多个 OutputUnit 依次调用；若第一次 write 消耗内部 vector，第二条连接会收到空数据。

`mutable` 可以用于缓存编码，但需要同步：多个连接线程可能并发调用 const write。更安全的方式是由 PortCore 创建一次不可变 serialized buffer，并只在编码配置相容时共享。

## 读取应采用构造事务

不要边解析边修改现有对象后再返回 false：


```cpp
bool State::read(ConnectionReader& r) {
  State next;
  next.sequence = r.expectInt64();
  const auto n = r.expectInt32();
  if (n < 0 || n > kMaxJoints) return false;
  next.joints.resize(static_cast<std::size_t>(n));
  for (double& x : next.joints) x = r.expectFloat64();
  if (r.isError()) return false;
  *this = std::move(next);
  return true;
}
```

只有完整成功才提交 `*this`，失败时旧对象保持有效。长度在分配前验证，防止恶意报文触发巨量内存。

这里还包含五个容易被忽略的 C++ 细节：

1. `State next;` 在栈上创建候选值，作用域结束会自动释放其中的 vector；任何提前 `return false` 都不会泄漏。
2. `static_cast<std::size_t>(n)` 放在 `n < 0` 检查之后。若先转换，负数会变成巨大的无符号整数。
3. 范围 `for (double& x : next.joints)` 使用引用，写入的是 vector 元素本身；若写成 `double x`，只会修改局部副本。
4. `std::move(next)` 允许 vector 的堆缓冲转移给当前对象，通常不复制每个关节值；移动后 `next` 仍可析构，但内容未指定。
5. “先解析、后提交”提供的是对象级强失败保证，不代表 wire 操作可以回滚。Reader 已消费的字节仍然被消费，连接层必须决定丢弃当前 frame 还是断开。

若消息字段较多，可以把解析事务抽成纯函数：


```cpp
std::optional<State> decode_state(ConnectionReader& reader) {
  State value;
  // 逐字段验证并填充 value
  if (reader.isError()) return std::nullopt;
  return value;
}
```

`optional` 同时携带“有值/失败”，但不适合表达细分错误。工业实现通常返回 `expected<State, DecodeError>` 一类结果，使上层区分截断、类型不符、长度越界和版本不兼容，并决定记录、计数还是断开连接。

## `BufferedPort<T>` 的引用不是所有权


```cpp
auto& slot = port.prepare();
slot = message;
port.write();
```

`slot` 是 buffer pool 中的借用引用。write 后后台线程可以使用或回收它，应用不得继续修改。下一次 prepare 可能返回同一对象，所以必须覆盖或 clear 所有字段。

接收端 `T* value = port.read()` 同样是借用指针，只保证到下一次 read。跨线程处理需要复制、移动到自有 storage，或采用明确的 loan ownership API。

### 模板参数解决类型，不解决寿命

`BufferedPort<State>` 让编译器知道缓冲元素是 `State`，从而省去运行时向下转换；它没有承诺返回引用永久有效。可把内部结构想成：


```cpp
template<class T>
class TinyBufferedPort {
  std::vector<T> slots_;
  std::size_t prepared_{};
  std::mutex mutex_;
  // ready/free 队列以及后台连接线程
};
```

`prepare()` 借出一个 free slot，`write()` 把索引从 prepared 状态移入 ready 队列，发送完成后再放回 free 队列。引用没有携带 slot 所处状态，因此 API 的正确性依赖调用顺序。若应用需要把对象跨异步阶段保存，接口应返回带自定义 deleter 的 loan handle，让析构负责归还槽位；裸 `T&` 无法表达这种所有权转移。

缓冲池大小为 `C`、单条消息保留容量为 `S` 时，payload 常驻内存近似 `O(C × S)`。即使 vector 的 `size()` 被清零，`capacity()` 也可能继续占用峰值内存；这能减少下一次分配，却会让偶发巨帧长期抬高进程 RSS。实现者应区分“固定上限以保证时延”和“无限保留以追求吞吐”两种策略。

## RAII Port owner


```cpp
class StatePort {
public:
  explicit StatePort(std::string name) {
    if (!port_.open(name)) throw std::runtime_error("open failed");
    open_ = true;
  }

  ~StatePort() {
    if (open_) {
      port_.interrupt();
      port_.close();
    }
  }

  StatePort(const StatePort&) = delete;
  StatePort& operator=(const StatePort&) = delete;

private:
  yarp::os::BufferedPort<State> port_;
  bool open_{false};
};
```

删除 copy 防止两个 owner 对同一逻辑 Port 重复 close。是否允许 move 要看 YARP Port 类型本身是否支持安全移动；不能因为现代 C++ 习惯就默认 `= default`。

析构只能做兜底，显式 close 才能返回错误和等待结果。

构造函数抛异常时，已经完成构造的成员会逆序析构，但 `StatePort` 自身的析构函数不会运行。因此 `port_.open()` 之前取得的每一项资源都应由成员 RAII 对象持有，不能只靠 `open_` 在最终析构中清理。若初始化包含“注册名字 → 打开 socket → 启动线程”三个步骤，每一步失败都要撤销前面已经提交的步骤。

成员声明顺序也就是构造顺序，和初始化列表书写顺序无关；析构顺序与声明顺序相反。若 worker 保存对 `port_` 的引用，成员应设计成先构造 port、后构造 worker，从而析构时先停 worker、后销毁 port。仅在析构函数体中调用 `interrupt()` 仍不够：函数体结束后成员才开始析构，必须保证线程已 join，不能让它与后续成员析构并发。

### move 语义必须重新证明不变量

包含 mutex、condition variable、后台线程和注册身份的对象通常不应自动移动。移动内存地址可能使线程捕获的 `this`、事件循环注册的回调或底层 C handle 的 user-data 指向旧对象。只有实现采用稳定堆内核，例如 `unique_ptr<Impl>`，并且所有异步路径只引用 Impl 时，外层门面移动才较容易成立。

## PImpl 稳定公开 ABI

YARP 公开类常用实现指针隐藏 PortCore 等内部类型：


```cpp
class Port {
public:
  Port();
  ~Port();
private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};
```

头文件无需暴露大量锁、线程和平台 socket 类型，修改 Impl 布局也不改变 Port 对象大小。代价是一次间接访问和 heap allocation。

析构函数必须在 `.cpp` 中看到完整 Impl 类型，否则 `unique_ptr<Impl>` 的默认 deleter 无法实例化完整 delete。

典型定义方式如下：


```cpp
// Port.h
class Port {
public:
  Port();
  ~Port();                 // 只声明
  Port(Port&&) noexcept;
  Port& operator=(Port&&) noexcept;
private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

// Port.cpp
class Port::Impl {
public:
  PortCore core;
};

Port::~Port() = default;   // 此处 Impl 已完整
```

`noexcept` 对移动很重要：标准容器扩容时，若移动可能抛异常，可能退回复制；而拥有唯一系统资源的 Port 往往根本不能复制。即便外层可移动，仍需由 Impl 保证活动线程、回调和 Name Server 注册都绑定稳定地址。

## 虚工厂与 clone

Carrier registry 保存 prototype，再为每条连接 clone：


```cpp
class Carrier {
public:
  virtual ~Carrier() = default;
  virtual bool checkHeader(Bytes) const = 0;
  virtual std::unique_ptr<Carrier> create() const = 0;
};
```

不能让所有连接共享 prototype 的可变握手状态。工厂返回 unique_ptr 表示新实例只有 Protocol owner；若多个对象需要共享，应该明确提升为 shared_ptr，而不是返回裸指针。

基类必须有虚析构，因为实际对象会通过 `unique_ptr<Carrier>` 删除；没有虚析构时只执行基类析构，派生类持有的 socket、压缩器或认证状态将得不到正确释放。`create() const` 又表示 prototype 本身只负责制造对象，不应在创建连接时累积可变握手状态。

这里的 `unique_ptr` 不只是防泄漏，还记录架构关系：registry 借用 prototype，Protocol 独占连接实例。如果代码到处改成 `shared_ptr`，关闭责任会变模糊，连接可能因监控器或回调残留引用而延迟销毁。

## 并发 close 的局部强引用

假设 OutputUnit 保存 `shared_ptr<Protocol> protocol_`。worker 与 close 可能并发：


```cpp
std::shared_ptr<Protocol> local;
{
  std::lock_guard lock(mutex_);
  if (closing_) return false;
  local = protocol_;
}
return local->write(writer);
```

close 在线程安全区把 `protocol_` reset，也不会销毁 worker 的 local。网络 write 必须在锁外，否则 close 无法取得锁调用 interrupt。

shared_ptr 解决对象寿命，不解决 Protocol 内部并发；仍需规定同一连接是否只允许一个 writer。

这段代码采用“锁内取得稳定快照，锁外执行未知时长操作”的结构。临界区保护的不变量是：只要 `closing_ == false`，就能取得一个非空 Protocol 引用。离开临界区以后，close 可以把成员指针清空，但局部 shared_ptr 仍使对象存活。代价是当前 write 可能在 close 开始后继续一小段时间；若协议要求 close 返回后绝无写入，close 还必须等待 active-writer 计数归零或 join 唯一 writer 线程。

不要把 `local->write()` 放进 mutex：write 可能阻塞于内核发送缓冲，close 则需要同一把锁取得 Protocol 并调用 interrupt，于是形成“写等待网络、关等待锁”的无限阻塞。也不要简单先取裸指针再解锁；成员 reset 后裸指针可能悬空。

## callback 的恰好一次完成

后台 write 通常有 tracker：


```cpp
class CompletionGuard {
public:
  explicit CompletionGuard(Callback cb) : cb_(std::move(cb)) {}
  ~CompletionGuard() { complete(Error::Cancelled); }
  void complete(Error e) {
    if (!done_.exchange(true)) cb_(e);
  }
private:
  std::atomic<bool> done_{false};
  Callback cb_;
};
```

真实实现还需处理 callback 抛异常和 owner 寿命。原子 flag 只保护“调用一次”，不能保证 cb_ 本身在并发销毁时安全；guard 必须由共享任务状态拥有。

`exchange(true)` 是一个原子读—改—写：它返回旧值并写入 true。只有看见旧值为 false 的线程调用 callback。默认的顺序一致内存序通常足够但偏强；若 callback 还要读取其他线程在完成前写入的结果，应明确建立 release/acquire 关系，而不能只把内存序改成 relaxed。更简单的实现常让所有完成事件进入同一个串行执行器，从结构上避免多个线程竞争终态。

## `std::atomic` 与 mutex 的边界

`closing_` 单独作为 atomic 只能回答一个瞬时布尔值，不能原子保护“检查 closing + 取得 protocol + 标记 sending”这一组不变量。多字段状态转换使用 mutex 或单个原子状态机；不要用多个 atomic 拼出貌似无锁但存在中间状态的协议。

## 从零复刻一个最小 Port 内核

下面的骨架不实现 YARP 协议，却保留最重要的设计关系：用户线程提交不可变消息，单一 worker 串行调用多个连接，关闭时拒绝新消息、唤醒等待并 join。


```cpp
class IConnection {
public:
  virtual ~IConnection() = default;
  virtual bool send(std::span<const std::byte> frame) = 0;
  virtual void interrupt() noexcept = 0;
};

class MiniPort {
public:
  explicit MiniPort(std::size_t capacity)
      : capacity_(checked_capacity(capacity)),
        worker_([this] { run(); }) {}

  MiniPort(const MiniPort&) = delete;
  MiniPort& operator=(const MiniPort&) = delete;

  bool add(std::shared_ptr<IConnection> connection) {
    if (!connection) return false;
    std::lock_guard lock(mutex_);
    if (state_ != State::Open) return false;
    connections_.push_back(std::move(connection));
    return true;
  }

  bool try_write(std::vector<std::byte> frame) {
    std::lock_guard lock(mutex_);
    if (state_ != State::Open || queue_.size() == capacity_) return false;
    queue_.push_back(std::move(frame));
    ready_.notify_one();
    return true;
  }

  void close() noexcept {
    std::call_once(close_once_, [this] {
      std::vector<std::shared_ptr<IConnection>> snapshot;
      {
        std::lock_guard lock(mutex_);
        state_ = State::Closing;
        snapshot = connections_;
      }
      ready_.notify_all();
      for (const auto& c : snapshot) c->interrupt();
      if (worker_.joinable()) worker_.join();
      {
        std::lock_guard lock(mutex_);
        connections_.clear();
        queue_.clear();
        state_ = State::Closed;
      }
    });
  }

  ~MiniPort() { close(); }

private:
  enum class State { Open, Closing, Closed };

  static std::size_t checked_capacity(std::size_t value) {
    if (value == 0) throw std::invalid_argument("zero capacity");
    return value;
  }

  void run() noexcept {
    for (;;) {
      std::vector<std::byte> frame;
      std::vector<std::shared_ptr<IConnection>> targets;
      {
        std::unique_lock lock(mutex_);
        ready_.wait(lock, [this] {
          return state_ != State::Open || !queue_.empty();
        });
        if (state_ != State::Open && queue_.empty()) break;
        frame = std::move(queue_.front());
        queue_.pop_front();
        targets = connections_;
      }
      for (const auto& target : targets) {
        target->send(frame);
      }
    }
  }

  const std::size_t capacity_;
  std::mutex mutex_;
  std::condition_variable ready_;
  State state_{State::Open};
  std::deque<std::vector<std::byte>> queue_;
  std::vector<std::shared_ptr<IConnection>> connections_;
  std::thread worker_;
  std::once_flag close_once_;
};
```

### 按执行顺序阅读这段代码

`checked_capacity` 在 thread 成员构造前验证容量；如果验证失败，不会留下 joinable thread 导致异常展开时终止进程。成员按声明顺序构造，因此 worker 启动前，mutex、条件变量、状态和容器都已经存在。更复杂的派生类或需要注册回调的实现仍应使用静态 `create()`，完整构造后再调用 `start()`，避免构造期间发布 `this`。

`try_write` 在锁内同时检查状态和容量并入队，这三个动作构成一个事务。队列达到 `capacity_` 就返回 false，因此内存上界可估算；调用者必须选择丢弃、重试或升级故障。把队列换成无界容器只会把背压变成延迟和内存问题。

worker 使用带谓词的 `condition_variable::wait`。条件变量允许虚假唤醒，所以不能写成“醒来就 pop”。谓词也把关闭事件纳入同一个等待条件。锁内把 frame 和连接表复制成局部快照，随后解锁做发送；这样连接管理不会被慢网络长期阻塞。

`close` 通过 `call_once` 选出唯一关闭执行者，再在锁内发布 Closing、锁外 interrupt。此后 `try_write` 和 `add` 都会失败。interrupt 负责让正在阻塞的 send 尽快返回，join 则建立线程终止边界。并发或重复 close 会等待同一轮关闭完成，返回时都能观察到 Closed。

这个骨架仍有明确缺口：`send` 的错误没有汇总；关闭时会排空已入队数据而不是立即丢弃；连接快照让刚删除的连接仍可能收到当前帧；每帧、每扇出都复制 vector/shared_ptr；callback 和输入方向尚未实现。这些不是小修饰，而是下一阶段必须先定义的语义。

### 数据结构与复杂度

设队列容量为 `C`、连接数为 `F`、帧大小为 `S`：

| 操作 | 时间 | 额外空间 | 主要常数成本 |
|---|---:|---:|---|
| `try_write` | 摊销 `O(1)` | 总队列 `O(C×S)` | vector 移动、一次加锁 |
| 取得目标快照 | `O(F)` | `O(F)` | shared_ptr 原子引用计数 |
| 单帧扇出 | `O(F)` 加各连接 I/O | 取决于 Carrier | 编码、拷贝、系统调用 |
| `close` | `O(F + C)` 加 join | `O(F)` | interrupt 与未完成 I/O |

若每个 Carrier wire format 相同，可以先编码一次并共享 `shared_ptr<const Frame>`，把应用层编码从 `O(F×S)` 降到 `O(S)`；但只要压缩、文本/二进制或连接头不同，仍需每连接生成相应 frame。若 F 很大，复制 shared_ptr 表也会产生原子流量，可改成版本化不可变连接快照，使发送线程只复制一个 snapshot 指针。

### 从骨架演进到 YARP 风格结构

第一步把 `IConnection` 拆成 Protocol 与 Carrier：Protocol 管理一条连接生命周期，Carrier 负责握手和编码策略。第二步给 Port 增加 InputUnit/OutputUnit，使每条连接有自己的阻塞和错误边界。第三步引入 Name Service，只让它解析逻辑名和建立连接，不让数据帧绕经中心服务。第四步增加 Portable/ConnectionWriter，使消息类型不依赖具体 Carrier。第五步再实现 strict/latest、RPC reply、envelope 和管理接口。

每一步都要保留同一组不变量：队列有界；用户回调不在全局锁内执行；连接拥有者唯一；借用数据寿命可说明；关闭后不接收新工作；close 返回时所有 worker 已停止；失败路径最终完成或取消每个任务。

## ABI 与插件边界

Carrier 插件跨动态库传递虚对象时，编译器、标准库、编译选项和 allocator 必须相容。最安全做法是由创建对象的库同时导出 destroy，或统一 YARP 插件 ABI；不要在另一个 CRT 中直接 delete 插件分配的对象。

## 完成标准

- Portable write 可重复调用且线程语义明确；
- read 先验证再提交，不接受无界长度；
- BufferedPort 借用对象不越过下一次 prepare/read；
- PImpl 析构看到完整类型；
- Carrier 每连接独立，基类有虚析构；
- close 锁外 interrupt/join，worker 使用稳定局部引用；
- 所有成功、失败、busy 和取消路径完成 callback 恰好一次。
- 能从最小 Port 骨架指出队列上界、扇出复杂度、快照代价与关闭线性化点；
- 能解释 `final`、`override`、`const`、`unique_ptr`、`shared_ptr`、move、条件变量谓词各自在系统不变量中的作用。
