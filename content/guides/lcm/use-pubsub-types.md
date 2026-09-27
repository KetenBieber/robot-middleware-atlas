# LCM 使用教程：类型生成、发布与订阅

移动底盘的固件用 C 发布 6 维速度，地面站用 C++ 画曲线，Python 脚本根据同一消息离线算里程。若三处各自按本地 `struct` 布局读写，一次编译器对齐变化就可能把 `timestamp` 读成速度分量；终端上仍有消息到达，但曲线出现跳变，控制端甚至会读入错误的方向。LCM 类型文件让三个语言从同一份 schema 生成编码器与解码器：先定义 schema，再用 `lcm-gen` 生成 C++ header；官方 C++ binding 是 header-only wrapper，但程序仍链接 LCM C runtime。[LCM C++ Tutorial](https://lcm-proj.github.io/lcm/content/tutorial-cpp.html)

本文涉及的 C/C++ 运行时行为固定到 `lcm-proj/lcm` commit `ad0c54cee0ec048ef12357c34349ec1443158864`：生成器在 `lcmgen/emit_cpp.c` 输出 typed encoder，`lcm/lcm-cpp-impl.hpp` 把它接到 C API，`lcm/lcm.c` 和 UDPM provider 决定准入与分发。

## 从 schema 到 wire bytes

生成的 C++ 类型大致提供编码大小、编码、解码与类型哈希。发布调用不是把 C++ 对象地址发到网络，而是：

```text
state_t object
  -> getEncodedSize()
  -> encode(buffer)
  -> type fingerprint + fields in defined order
  -> lcm.publish(channel, bytes)
```

接收端先核对 fingerprint，再按相同字段布局构造对象。`std::vector`、`std::string` 和指针从不直接跨进程；它们只是生成类在本进程中的表示。

## 类型定义


```text
package atlas;

struct state_t {
  int64_t timestamp_us;
  int64_t sequence;
  int32_t joint_count;
  double joints[joint_count];
  string frame;
}
```

生成 C++：


```bash
lcm-gen -x state_t.lcm
```

得到 `atlas/state_t.hpp`。变量数组的长度字段必须与 vector 实际元素数一致；编码器按长度字段读取，错误值可能导致越界或截断。

后续 Publisher 示例需要生成类型头文件和微秒时钟 helper：


```cpp
#include <chrono>
#include <cstdint>

inline std::int64_t now_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
}
```

`joint_count` 同时存在于 schema 和 C++ vector，是一种冗余不变量。把赋值封装起来，避免每个调用点各自维护：


```cpp
#include <cstdint>
#include <stdexcept>
#include <vector>

#include "atlas/state_t.hpp"

constexpr std::size_t kMaxJoints = 16;

atlas::state_t make_state(const std::vector<double>& joints,
                          std::int64_t sequence) {
  if (joints.size() > kMaxJoints) throw std::length_error("too many joints");
  atlas::state_t result;
  result.timestamp_us = now_us();
  result.sequence = sequence;
  result.joints.assign(joints.begin(), joints.end());
  result.joint_count = static_cast<std::int32_t>(result.joints.size());
  result.frame = "arm_base";
  return result;
}
```

形参以 `const&` 借用调用者 vector，不发生入口复制；返回对象通过 `assign` 拥有自己的元素。若工程使用 C++20，可改用 `std::span<const double>` 接受 vector、array 和其他连续缓冲区，同时仍保持不拥有的只读视图语义。

官方类型教程列出各语言生成参数和数组映射。[LCM-gen Tutorial](https://lcm-proj.github.io/lcm/content/tutorial-lcmgen.html)

## Publisher


```cpp
#include <lcm/lcm-cpp.hpp>
#include <cstdint>

#include "atlas/state_t.hpp"

int main() {
  lcm::LCM lcm;
  if (!lcm.good()) return 1;

  atlas::state_t msg;
  msg.timestamp_us = now_us();
  msg.sequence = 1;
  msg.joints = {0.1, 0.2, 0.3};
  msg.joint_count = static_cast<int32_t>(msg.joints.size());
  msg.frame = "arm_base";
  return lcm.publish("ATLAS_STATE", &msg) == 0 ? 0 : 2;
}
```

`publish` 在调用中编码消息。固定版本的模板重载会先分配临时 byte buffer，调用生成类的 `encode()`，同步调用 C API 后释放该 buffer；消息对象仍由调用方拥有，provider 收到的是编码 bytes。`LCM::publish<MessageType>()` 不要在 `joint_count` 与 vector 不一致时发送。channel 约定使用稳定常量，并约定单一类型。

此例默认 URL 使用 UDPM。短消息返回 0 表示本地 `sendmsg()` 接受了完整 datagram，不表示有订阅者或远端解码成功；固定版本的长消息分支即使某片发送失败也仍返回 0。需要业务确认的命令必须单独设计 ack channel，并给 ACK 绑定 sequence 与 deadline。`lcm_udpm_publish()`。

## Subscriber


```cpp
#include <iostream>

#include <lcm/lcm-cpp.hpp>

#include "atlas/state_t.hpp"

class Handler {
 public:
  void onState(const lcm::ReceiveBuffer*, const std::string& channel,
               const atlas::state_t* msg) {
    std::cout << channel << " seq=" << msg->sequence << '\n';
  }
};

int main() {
  lcm::LCM lcm;
  if (!lcm.good()) return 1;
  Handler handler;
  auto* subscription =
      lcm.subscribe("ATLAS_STATE", &Handler::onState, &handler);
  if (subscription == nullptr) return 2;
  for (int i = 0; i < 100; ++i) {
    if (lcm.handleTimeout(100) < 0) break;
  }
  lcm.unsubscribe(subscription);
}
```

`LCMMHSubscription::cb_func()` 在 C 核心 dispatch 中构造局部的解码对象和 `ReceiveBuffer`，再同步调用成员函数；因此 callback 在调用 `handle()` 的线程执行，局部对象只在回调期间有效。C++ trampoline、核心锁外 callback 调用。回调阻塞会停止该 LCM 实例的后续 dispatch；耗时任务应复制所需字段到有界 worker queue。

### 回调参数的所有权

`ReceiveBuffer*` 与生成消息指针是 LCM 在本次分发中提供的借用对象。回调返回后不能保存指针：


```cpp
void Handler::onState(const lcm::ReceiveBuffer*, const std::string&,
                      const atlas::state_t* msg) {
  StateSnapshot owned;
  owned.sequence = msg->sequence;
  owned.timestamp_us = msg->timestamp_us;
  owned.joints = msg->joints;           // 深复制动态数组
  if (!queue_.try_push(std::move(owned))) ++drops_;
}
```

复制不是因为回调“速度不够”，而是把借用生命周期转换为 worker 可持有的所有权。把 `msg` 指针直接放进队列，即使通常能运行，也是 use-after-free。

Handler 对象自身也必须比 subscription 活得更久。一个安全的声明与关闭次序是：先构造 Handler，再订阅；关闭时停止 handle loop、取消订阅，然后才能销毁 Handler。

## `handle()` 决定线程模型

LCM 的 callback 不由任意内部线程并发调用；哪个线程调用 `handle()`，哪个线程执行一个消息的 callback。`handleTimeout()` 只是把超时参数传入 C 核心，并不把 callback 投递到线程池。`LCM::handleTimeout()` 这给应用明确控制，也意味着同一实例上的慢 callback 会串行阻塞其他 channel。


```cpp
std::atomic_bool stop{false};
while (!stop.load()) {
  const int rc = lcm.handleTimeout(100); // 周期醒来检查退出和维护任务
  if (rc < 0) {
    report_provider_error();
    break;
  }
}
```

与永久阻塞的 `handle()` 相比，超时接口让主循环有机会响应停止，但 100 ms 也成为最坏关闭检测延迟之一。若已有 epoll、Qt 或机器人调度循环，则监控 `getFileno()`；fd readable 后再调用 `handle()`，把通信纳入统一事件循环。

不要让两个线程同时对同一 LCM 实例调用 `handle()`，除非所用版本明确保证这种用法；单分发者模型更容易推理 callback 顺序和 unsubscribe 生命周期。

## Subscription 容量与取消

订阅返回的 `Subscription*` 由 `lcm::LCM` 保存并管理；它用于配置每订阅的待处理计数和取消订阅。固定版本默认容量为 30，设为 0 或负数表示不限。它不是可靠性保证：核心只给仍低于额度的匹配 subscription 增加待处理计数；如果没有任何一个订阅能接收，UDPM provider 就丢弃这条消息。`setQueueCapacity()`、订阅容量默认值、`lcm_try_enqueue_message()`：


```cpp
auto* sub = lcm.subscribe("ATLAS_STATE", &Handler::onState, &handler);
sub->setQueueCapacity(4);
// 停止 handle loop 后
lcm.unsubscribe(sub);
```

容量 4 适合吸收短调度抖动，却不能解决 callback 每秒只能处理 50 条、发布者每秒发送 100 条的持续过载。应测到达率、服务率、队列容量与允许的数据年龄，再选择丢弃或把重工作移出 callback。

## 集成现有事件循环

`getFileno()` 返回可监控 fd。fd readable 后调用 `handle()`，此时等待已经在外部 event loop 完成；API 文档把它定义为可接入 `select()`、`poll()` 等事件循环的通知描述符。`getFileno()` 文档与 wrapper、`LCM::getFileno()`。不要无条件从 GUI/控制线程调用阻塞 handle。

## Schema 兼容

LCM 类型指纹能检测不兼容定义。修改字段会产生新指纹；滚动升级时应使用新类型名或桥接进程，而不是假设旧消费者忽略未知字段。

桥接进程同时订阅 `ATLAS_STATE_V1`、发布 `ATLAS_STATE_V2`，显式完成单位、默认值和字段映射。这样兼容策略有可测试代码，而不是依赖二进制解析“碰巧没有失败”。

## 验收

- C++ 与另一语言生成代码互通；
- 故意使用旧 schema 时能观察指纹/解码失败；
- `joint_count` 为 0、最大值和非法不一致均有测试；
- handler 慢时队列和 UDP drop 可观测；
- sequence gap 与时间戳 age 进入指标。
- 关闭前停止唯一的 handle 分发者，再 unsubscribe 并销毁 Handler；
- callback 返回后没有保存 ReceiveBuffer 或生成消息裸指针；
- 满负载时 subscription 容量和 worker queue 均保持上界。
