# LCM 使用教程：类型生成、发布与订阅

移动底盘的固件用 C 发布 6 维速度，地面站用 C++ 画曲线，Python 脚本根据同一消息离线算里程。若三处各自按本地 `struct` 布局读写，一次编译器对齐变化就可能把 `timestamp` 读成速度分量；终端上仍有消息到达，但曲线出现跳变，控制端甚至会读入错误的方向。LCM 类型文件让三个语言从同一份 schema 生成编码器与解码器：先定义 schema，再用 `lcm-gen` 生成 C++ header；官方 C++ binding 是 header-only wrapper，但程序仍链接 LCM C runtime。[LCM C++ Tutorial](https://lcm-proj.github.io/lcm/content/tutorial-cpp.html)

本文涉及的 C/C++ 运行时行为固定到 `lcm-proj/lcm` commit `ad0c54cee0ec048ef12357c34349ec1443158864`：生成器在 `lcmgen/emit_cpp.c` 输出 typed encoder，`lcm/lcm-cpp-impl.hpp` 把它接到 C API，`lcm/lcm.c` 和 UDPM provider 决定准入与分发。

## 把一条状态消息变成可以验证的工程协议

先不要定义全机器人的所有 channel。用本指南的 `ATLAS_STATE` 一条消息完成三项验收：各端从同一份 schema 生成类型；每条消息具有 `sequence` 与 `timestamp_us`；消费者能识别旧消息、重复消息和非法负载。`joint_count` 必须等于 `joints.size()`，且发布前检查元素上限；业务通道约定单位、坐标系和序号回绕规则，这些约定不会由消息指纹自动生成。

~~~text
关节状态 -> sequence + source timestamp + 数组长度
          -> generated encode -> publish
                                  |
                             UDP/Provider
                                  |
                           handle + decode
                                  |
                         validate length/values
                                  |
                         check sequence/data age
                                  |
                        更新有界控制快照
~~~

用户的 `Handler::onPose` 收到的生成类型指针不具有永久寿命：固定版本 C++ trampoline 临时构造 `MessageType msg`，解码、同步调用业务 callback，然后销毁临时对象；`ReceiveBuffer::data` 也是临时借用。把这根指针直接保存到后台控制线程，可能在下一次队列复用以后读到悬空内容。正确的应用边界是回调内快速校验并**复制**所需数值到自己拥有的数据结构，再让控制线程在明确定义的采样点读取。退出时先协调 handle 停止与取消订阅，再销毁注册时借用的 Handler。语言机理与原始模板见[C++ callback 适配](../../articles/lcm/c-abi-cpp-design-lab.md)。

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

容量 4 可以限制待处理的准入计数，却不能解决 callback 每秒只能处理 50 条、发布者每秒发送 100 条的持续过载。尤其不能把它视为一只按消息 ID 精确记录投递资格的私有 FIFO：LCM 的单副本队列与订阅计数在特定接收/消费交错下可能错配，详见[订阅分发中的 M0/M1/M2 实验](../../articles/lcm/subscription-dispatch.md)。应测到达率、服务率与数据年龄；需要准确追踪每条命令时，使用自己的有界消息队列和业务确认协议。

## 集成现有事件循环

`getFileno()` 返回可监控 fd。fd readable 后调用 `handle()`，此时等待已经在外部 event loop 完成；API 文档把它定义为可接入 `select()`、`poll()` 等事件循环的通知描述符。`getFileno()` 文档与 wrapper、`LCM::getFileno()`。不要无条件从 GUI/控制线程调用阻塞 handle。

## Schema 兼容

LCM 类型指纹能检测不兼容定义。修改字段会产生新指纹；滚动升级时应使用新类型名或桥接进程，而不是假设旧消费者忽略未知字段。

桥接进程同时订阅 `ATLAS_STATE_V1`、发布 `ATLAS_STATE_V2`，显式完成单位、默认值和字段映射。这样兼容策略有可测试代码，而不是依赖二进制解析“碰巧没有失败”。

## 把 typed pub/sub 的生命周期完整走一遍

一条 typed 消息真正经过的是：

~~~text
state_t object
  -> getEncodedSize / encode
  -> 临时 byte buffer
  -> provider.publish
  -> 网络 / provider queue
  -> lcm_recv_buf_t
  -> typed trampoline decode
  -> callback 中的临时 state_t
  -> callback 返回，临时对象与借用视图失效
~~~

这条链上有三个最容易写出偶发 bug 的地方。第一，`publish()` 返回以后不要继续假设 provider 仍借用 C++ wrapper 生成的临时编码缓冲；固定 wrapper 会立即 `delete[]`。第二，callback 的 `ReceiveBuffer*` 和 `MessageType*` 都是同步调用期间的借用视图，后台线程要使用数据时必须复制业务需要的字段。第三，用户 Handler 的寿命必须长于所有相关 subscription 与 callback；最简单的关闭方式是先停止并 join 唯一 handle 线程，再 unsubscribe，最后销毁 Handler 和 `lcm::LCM`。

若你的应用需要把重活丢给 worker，推荐 callback 只做验证、复制/移动到**自己拥有**的有界队列，然后立即返回。worker 队列也必须有容量和溢出策略；把 LCM 的有限队列换成另一个无限 `std::queue` 并没有解决背压问题，只是把 OOM 推迟到自己的进程。


## 把 callback 借用期与取消订阅写成可执行约束

初学者最容易把回调参数和自己声明的长期对象混为一谈。固定 C++ wrapper 内，typed callback 创建临时的生成类型 `MessageType msg`，并把 `ReceiveBuffer` 指向 provider 正在分发的原始 bytes。**两个对象都只保证在这一次同步 callback 内有效**。因此，这种写法会产生悬空引用：

~~~cpp
// 错误教学示例：callback 返回后，后台线程仍拿着借来的地址。
void Handler::onState(const lcm::ReceiveBuffer* rbuf,
                      const std::string&, const state_t* msg) {
    worker_.submit([msg, rbuf] {
        use(*msg, rbuf->data);
    });
}
~~~

如果确实需要异步处理，应该在 callback 返回前**复制业务字段**，或者把编码字节复制进自己拥有的有界消息队列；不要仅仅再包一层 `shared_ptr` 指向借用的 `msg`。同时必须定义队列满时是丢最新、丢最旧还是阻塞；对于状态估计输入与执行命令，这三种策略的安全含义完全不同。

再把退出操作看成一个阶段性协议：请求停止后让唯一的 handle 线程离开消息循环；确认没有仍在运行的 callback；然后取消订阅并销毁 Handler；最后销毁它依赖的 LCM 句柄。**C 核心的“callback 内延迟删除订阅”不是 C++ 接收对象跨线程安全析构的证明。** 若业务确实支持在 callback 内取消同一订阅，需要事先明确 `Subscription*` 不能被再次解引用，且不能让其他线程在 handle 尚未退出时同时摧毁 callback 的上下文。

一个最小的回归案例应覆盖：正常生成类型编解码、不同 channel 不误匹配、旧 schema 指纹不兼容、回调内取消订阅、异步工作队列持有自有数据，以及退出时所有工作线程都完成 join。只收到一次消息，不能代表这一组生命周期约束都通过。

## 将整个教程放到一对可复用的发送与接收程序中

本仓库的 `examples/lcm/closed_loop/` 包含完整的 `.lcm` schema、CMake 配置、`atlas_sender` 和 `atlas_receiver`。建议先逐行读懂例子，再在已安装 LCM CMake package 和 C/C++ 开发依赖的环境中，从**仓库根目录**执行：

四个核心文件的完整源码、逐段调用链、对象生命周期、退出码设计与故障实验已经单独整理为[端到端闭环工程详解](closed-loop-project.md)。本节只保留最短的构建与验收入口；第一次实践建议直接按项目页从 schema 读到 receiver。

~~~bash
cmake -S examples/lcm/closed_loop -B build/lcm-closed-loop \
  -DCMAKE_PREFIX_PATH=/path/to/lcm/install
cmake --build build/lcm-closed-loop --parallel
~~~

先运行接收程序，再运行发送程序。发送端发布 20 条带序号的三关节状态；接收端检查数组长度、序号连续性与总数，只有全部收齐、内容有效时才返回 0。反过来先运行发送端，或者把两个程序的 UDP 端口改成不同值，验收应能报告缺帧或超时。这样测试的是**一次具体运行的结果**，不是借助一段永远返回 0 的演示程序误认为 UDP 不会丢包。

建议将每次实验的四份事实同时保存：原始 URL、发送/接收端实际序号、退出码和环境信息。只有发送端返回 0，并不能证明订阅端收到；即使一次收齐 20 条，也不构成未来可靠性保证。随后再人为让 callback 阻塞、调小队列容量，进入[运行与故障演练](use-operations.md)追踪“生产率超过消费率”时到底在哪里丢消息。

## 验收

- C++ 与另一语言生成代码互通；
- 故意使用旧 schema 时能观察指纹/解码失败；
- `joint_count` 为 0、最大值和非法不一致均有测试；
- handler 慢时队列和 UDP drop 可观测；
- sequence gap 与时间戳 age 进入指标。
- 关闭前停止唯一的 handle 分发者，再 unsubscribe 并销毁 Handler；
- callback 返回后没有保存 ReceiveBuffer 或生成消息裸指针；
- 满负载时 subscription 容量和 worker queue 均保持上界。
