# eCAL 使用教程：String 发布订阅与生命周期

eCAL Core 在底层传递二进制数据，string API 是其轻量封装。官方教程也建议先理解 binary/blob，再使用 string 或结构化类型。[eCAL Pub/Sub](https://eclipse-ecal.github.io/ecal/v6.1/getting_started/howto/pubsub.html)

## 先明确进程内对象关系

```text
eCAL Runtime（进程级）
  ├── Publisher("atlas/status")
  └── Subscriber("atlas/status") -> transport callback -> application queue
```

`Initialize()` 与 `Finalize()` 管理进程级运行时，Publisher/Subscriber 是依赖它的实体。局部对象应在 `Finalize()` 之前析构；如果把实体做成静态全局变量，就可能出现 C++ 静态析构顺序与 eCAL Runtime 顺序相反的问题。

可以用作用域明确依赖：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
eCAL::Initialize(argc, argv, "atlas_process");
{
  eCAL::string::CPublisher<std::string> publisher("atlas/status");
  run(publisher);
} // publisher 先析构
eCAL::Finalize();
```

这不是语法风格问题，而是资源拓扑：底层 registration、transport 和 callback 基础设施必须活得比其实体更久。

## Publisher

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
#include <chrono>
#include <cstdint>
#include <string>
#include <thread>

#include <ecal/ecal.h>
#include <ecal/msg/string/publisher.h>

int main(int argc, char** argv) {
  eCAL::Initialize(argc, argv, "atlas_sender");
  eCAL::string::CPublisher<std::string> publisher("atlas/status");

  std::uint64_t sequence = 0;
  while (eCAL::Ok()) {
    publisher.Send("seq=" + std::to_string(sequence++));
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  eCAL::Finalize();
}
```

先初始化，再创建实体；先销毁或停止使用实体，再 Finalize。循环条件使用 `eCAL::Ok()`，使 Ctrl+C 等关闭请求能结束发送。

`Send()` 的返回值应进入指标。一次发送调用成功只说明本地发布路径接受了数据，不等于某个远端业务 callback 已处理；subscriber 数、传输层状态与应用确认是不同层次的事实。

## Subscriber

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
#include <iostream>
#include <string>
#include <ecal/ecal.h>
#include <ecal/msg/string/subscriber.h>

int main(int argc, char** argv) {
  eCAL::Initialize(argc, argv, "atlas_receiver");
  eCAL::string::CSubscriber<std::string> subscriber("atlas/status");

  subscriber.SetReceiveCallback(
      [](const eCAL::STopicId& topic_id,
         const std::string& message,
         long long time_usec) {
        std::cout << topic_id.topic_name << " " << time_usec
                  << " " << message << '\n';
      });

  while (eCAL::Ok()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  eCAL::Finalize();
}
```

API 的精确 callback 签名会随主版本调整，编译时以安装版本头文件和官方对应版本示例为准。核心原则不变：callback 由接收执行上下文调用，应快速返回。

回调闭包捕获的对象必须比 Subscriber 活得更久。下面的成员声明顺序使队列先构造、后析构；关闭函数先移除/停止 callback，再销毁队列消费者：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class Receiver {
public:
  explicit Receiver(std::string topic)
      : subscriber_(std::move(topic)) {
    subscriber_.SetReceiveCallback(
        [this](const auto& id, const std::string& value, auto time) {
          on_message(id, value, time);
        });
  }

  void stop() {
    if (stopping_.exchange(true)) return;
    // 使用当前 eCAL 版本提供的 callback removal/实体销毁接口，
    // 确认不再进入 on_message 后，再停止 worker。
    queue_.close();
    worker_.join();
  }

private:
  BoundedQueue<Sample> queue_;       // 先构造，最后析构
  Worker worker_{queue_};
  eCAL::string::CSubscriber<std::string> subscriber_;
  std::atomic_bool stopping_{false};
};
```

C++ 成员按声明顺序构造、按相反顺序析构。如果 `subscriber_` 声明在队列之前，它可能在队列已经析构后仍触发回调。仅仅在 lambda 中捕获 `this` 不会延长对象生命周期。

## 回调中的有界交接

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
subscriber.SetReceiveCallback([&queue, &drops](auto&, const auto& msg, auto) {
  if (!queue.try_push(msg)) {
    ++drops;
  }
});
```

把数据库、磁盘和耗时推理移到 worker。队列必须有界，并明确满时丢新、覆盖旧或阻塞。状态流通常覆盖旧值，命令事件通常不能静默丢弃。

### 有界交接的数据结构

状态流可以使用单槽 mailbox：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
class LatestSample {
public:
  void publish(Sample sample) {
    std::lock_guard lock(mutex_);
    value_ = std::move(sample);
    ++version_;
  }

  std::optional<Sample> read_after(std::uint64_t& seen) {
    std::lock_guard lock(mutex_);
    if (!value_ || seen == version_) return std::nullopt;
    seen = version_;
    return *value_;
  }
private:
  std::mutex mutex_;
  std::optional<Sample> value_;
  std::uint64_t version_{};
};
```

它的内存为 `O(S)`，过载时覆盖旧样本，适合姿态、温度和感知结果；事件队列则是 `O(C*S)`，需要容量 `C` 和明确丢弃方向。不要把二者都叫“异步队列”，因为它们对历史完整性的承诺完全不同。

## 从 string 迁移到结构化消息

string 示例的目标是验证环境，不应逐步演化成用分隔符拼接的生产协议。迁移步骤是：定义 schema、生成类型、把 schema target 加入 CMake、替换 publisher/subscriber 模板类型，再为跨版本兼容建立回放样本。

结构化消息至少包含源时间戳、单调 sequence 和 frame/source identity。接收时间只说明本进程何时看到数据，不能替代数据产生时间；sequence gap 则能区分“值没变化”和“中间样本没有被观察到”。

## 运行与观察

```bash
./build/receiver
./build/sender
```

同时打开 eCAL Monitor，确认 topic 名、类型、publisher/subscriber 数和频率。string 是 UTF-8 字节语义；复杂结构应换 protobuf 等 schema，不要自行拼接难以版本化的字符串。

## 常见失败

| 现象 | 检查 |
|---|---|
| monitor 无进程 | `Initialize`、配置文件、进程是否立即退出 |
| 有实体无数据 | topic 拼写、发送返回、callback 是否注册 |
| 同机可用跨机不可用 | discovery、网卡选择、防火墙、host 配置 |
| 大消息吞吐低 | SHM 是否启用、序列化、copy 与 subscriber 速度 |
| 停止时崩溃 | callback 捕获对象生命周期、Finalize 顺序 |

官方 String Hello World 给出了多语言对照，可用于验证跨语言互操作。[String Hello World](https://eclipse-ecal.github.io/ecal/v6.1/getting_started/howto/pubsub/string_hello_world.html)

## 关闭顺序

```text
停止业务生产者
  -> 停止接受新的后台任务
  -> 注销/销毁 Subscriber，阻止新 callback
  -> 关闭队列并 join worker
  -> 销毁 Publisher/Subscriber 实体
  -> eCAL::Finalize()
```

如果 worker 仍使用 eCAL API，就必须在 Finalize 前 join。若 callback 正在运行，关闭过程还需要等待在途 callback 退出；仅设置一个布尔变量无法证明它已经离开捕获对象。

## 完整验收

- 发送端无订阅者时仍能稳定运行，并正确报告本地发送结果；
- 接收端晚启动后能被发现，停止后 registration 能收敛；
- callback 洪泛时队列内存有上界，drop/overwrite 可观测；
- 慢 worker 不在 transport callback 中反向阻塞磁盘或推理；
- schema 不匹配、空 payload 和超大 payload 都有明确失败；
- 重复停止不会重复 join，Finalize 后不再进入 callback。
