# Cyber RT 使用教程：Node、Writer 与 Reader

最小发布订阅程序由四步组成：初始化进程级 runtime、创建 Node、从 Node 创建 Writer/Reader、让 SIGINT 驱动运行状态退出。看起来只有几行 API，背后却有三条不同的生命周期：runtime 是进程级单例集合，Node 在拓扑中注册进程内逻辑节点，Reader/Writer 则分别注册 channel 角色并持有数据通路。

本章固定到 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，聚焦业务代码怎样创建 Node、发布消息和订阅 channel；下方直接给出可照着编写的 C++ 与构建配置。

## 对象关系与创建失败边界

```text
Init(binary_name)
  -> logger / scheduler / discovery / transport 等全局设施
  -> CreateNode(name)                        unique_ptr<Node>
       -> NodeChannelImpl 注册 ROLE_NODE
       -> CreateWriter<T>(channel)           shared_ptr<Writer<T>>
            -> FillInAttr: node/channel/type/proto descriptor
            -> Writer::Init -> transmitter + topology role
       -> CreateReader<T>(config, callback)  shared_ptr<Reader<T>>
            -> ChannelBuffer + DataVisitor + scheduler task
            -> receiver + topology role
```

`CreateNode` 返回 `std::unique_ptr<Node>`，表达调用方独占 Node；Writer/Reader 返回 `std::shared_ptr`，因为它们需要跨越创建语句并与运行时内部异步工作协作。任何一步都可能失败，示例必须检查 `Init`、Node 和 endpoint，而不是等到第一条消息才从空指针崩溃。

## 定义消息

```proto
syntax = "proto2";
package atlas.demo;

message Status {
  required uint64 sequence = 1;
  required uint64 timestamp_ns = 2;
  required string text = 3;
}
```

Bazel target 需要先生成 protobuf C++ 类型，再让 publisher/listener 依赖该 target。仓库现有示例使用的具体宏以同目录 `BUILD` 为模板，避免复制跨版本失效的外部宏名。

## Publisher

```cpp
#include <cstdint>
#include <memory>
#include <string>

#include "cyber/cyber.h"
#include "demo/proto/status.pb.h"

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_writer");
  if (!node) {
    return 2;
  }
  auto writer = node->CreateWriter<atlas::demo::Status>("/atlas/status");
  if (!writer) {
    return 3;
  }

  uint64_t sequence = 0;
  apollo::cyber::Rate rate(10.0);
  while (apollo::cyber::OK()) {
    auto message = writer->AcquireMessage();
    message->set_sequence(sequence++);
    message->set_timestamp_ns(apollo::cyber::Time::Now().ToNanosecond());
    message->set_text("alive");

    if (!writer->Write(message)) {
      AERROR << "failed to write /atlas/status";
    }
    rate.Sleep();
  }

  return 0;
}
```

`AcquireMessage()` 先询问 transmitter 是否能提供合适的消息对象，失败时再退回 `std::make_shared<MessageT>()`。这为某些 transport 的内存复用留下优化入口，而业务代码仍只依赖 `shared_ptr`。

`Writer::Write` 有两个重载：

```cpp
bool Write(const MessageT& message);                  // 先 make_shared 复制
bool Write(const std::shared_ptr<MessageT>& message); // 直接交给 Transmit
```

按值对象重载简单，但每次额外复制完整 protobuf；共享指针重载省去这次复制，却把不可变约束交给调用方。`Write(message)` 返回后仍可能存在异步消费者，因此不要再修改同一对象，也不要在多次发布之间反复改写它。每一轮获取新对象，所有权最容易推理。

`Node` 应在 Writer 之前创建并活得更久。变量按逆序析构时，Writer 先离开 channel topology，随后 Node 离开 node topology。固定提交的官方 talker 在循环退出后直接从 `main` 返回：`Init()` 已注册幂等的 `atexit(Clear)`，不需要调用一个不存在的 `Shutdown()` API。若应用显式调用 `Clear()`，应先释放自建 Reader、Writer、Node 和 worker，再清理全局 scheduler、discovery 与 transport。

## Listener

```cpp
#include <memory>
#include "cyber/cyber.h"
#include "demo/proto/status.pb.h"

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_reader");
  if (!node) {
    return 2;
  }

  auto reader = node->CreateReader<atlas::demo::Status>(
      "/atlas/status",
      [](const std::shared_ptr<atlas::demo::Status>& message) {
        AINFO << "seq=" << message->sequence()
              << " age_ns="
              << apollo::cyber::Time::Now().ToNanosecond()
                   - message->timestamp_ns()
              << " text=" << message->text();
      });
  if (!reader) {
    return 3;
  }

  apollo::cyber::WaitForShutdown();
  return 0;
}
```

Reader handle 不能是创建后立即销毁的临时对象。`CreateReader` 返回的共享指针析构时会执行 `Reader::Shutdown()`：离开拓扑、释放 receiver，并按 routine 名从 scheduler 移除任务。若写成一条未保存返回值的表达式，Reader 会在语句末尾析构，看起来就像“channel 存在但 callback 永远不来”。

### callback 不是在 transport 收包线程直接执行

reality mode 下，`Reader::Init()` 创建 `DataVisitor<MessageT>`，再用 `CreateRoutineFactory` 包装处理函数，并交给 Cyber scheduler 创建任务。transport dispatcher 负责把消息放入共享的 channel buffer 和通知 routine；scheduler 的 Processor 线程最终恢复 routine 并执行 callback。

```text
transport receiver/dispatcher
  -> DataDispatcher 写 ChannelBuffer
  -> DataNotifier 唤醒 reader routine
  -> Processor 恢复 CRoutine
  -> Reader::Enqueue(message)
  -> user callback(message)
```

这比笼统地说“回调在线程池里”更准确：同一个 callback 的长耗时会占用调度 Processor，并推迟同一调度组中的其他 routine。数据库写、模型推理或阻塞 I/O 应转移到有界 worker 队列，并定义队列满时丢旧状态、拒绝命令或触发降级的策略。

callback 参数是 `const std::shared_ptr<MessageT>&`。引用本身只在调用期间有效；若 worker 需要延长消息生命周期，应复制一份 `shared_ptr`，而不是保存对这个参数变量的引用：

```cpp
[&queue](const std::shared_ptr<atlas::demo::Status>& message) {
  auto owned = message;                 // 增加共享引用计数
  if (!queue.tryPush(std::move(owned))) {
    // 状态流可记录 drop；控制命令应返回明确的 overload 结果
  }
}
```

复制 `shared_ptr` 是 `O(1)`，但通常包含一次原子引用计数更新；复制 protobuf 则是 `O(payload bytes)` 并可能分配内存。若后台只使用 sequence、timestamp 和少量字段，转换成小型业务 DTO 往往比长期持有整条传感器消息更节省内存。

## ReaderConfig：区分 QoS history 与 pending queue

```cpp
apollo::cyber::ReaderConfig config;
config.channel_name = "/atlas/status";
config.pending_queue_size = 8;
config.qos_profile.set_history(
    apollo::cyber::proto::HISTORY_KEEP_LAST);
config.qos_profile.set_depth(4);
config.qos_profile.set_reliability(
    apollo::cyber::proto::RELIABILITY_RELIABLE);
config.qos_profile.set_durability(
    apollo::cyber::proto::DURABILITY_VOLATILE);

auto reader = node->CreateReader<atlas::demo::Status>(
    config,
    [](const std::shared_ptr<atlas::demo::Status>& message) {
      consume(*message);
    });
```

两个“深度”不是同一个容器：

| 配置 | 固定源码中的直接使用位置 | 主要含义 |
|---|---|---|
| `qos_profile.depth` | `BlockerAttr(depth, channel)` | Reader 的 publish/observe 历史容量 |
| `pending_queue_size` | `DataVisitor(channel_id, size)` | scheduler callback 尚未消费的数据窗口 |

字符串重载创建 Reader 时，`pending_queue_size` 默认是 1；`ReaderConfig` 构造函数的默认 pending 同样是 1，并给出 keep-last、depth 1、reliable、volatile 的 QoS。callback 慢于输入时，旧消息因此很快被覆盖。这对“只关心最新状态”很合理，对不能跳过的命令却不够：命令通道必须在应用协议中加入 id、确认、幂等和过载反馈，不能把增大队列当作可靠执行保证。

容量为 `N`、平均每条消息及其对象开销为 `S` 字节时，单 Reader 的缓存量级至少为 `O(N × S)`；同一 channel 上多个 Reader 还各自拥有消费窗口。若到达率 `λ` 长期高于 callback 服务率 `μ`，任何有限队列最终都会覆盖旧数据，增加深度只会把丢失推迟并增大数据年龄。

## callback 与 Observe 两种消费方式

不传 callback 时，Reader 仍会把消息 `Enqueue` 到内部 Blocker。调用方显式执行 `Observe()`，把 publish queue 的可见数据更新到 observe queue，然后通过 `GetLatestObserved()`、迭代器等接口读取。它适合按某个控制周期获取最近快照：

```cpp
auto reader = node->CreateReader<atlas::demo::Status>(role_attributes);

while (apollo::cyber::OK()) {
  reader->Observe();
  if (auto latest = reader->GetLatestObserved()) {
    consume_snapshot(*latest);
  }
  loop_rate.Sleep();
}
```

callback 模式由消息到达驱动；Observe 模式由调用者周期驱动。两者表达的是不同调度需求，不应同时拿来处理同一业务动作。Observe 读取的是内部保留窗口，不是对远端 Writer 的同步查询；控制周期慢时仍可能跳过中间状态。

## BUILD 依赖形状

如果已经能独立写出本章的 Writer/Reader，不要立刻跳到大型 Planning 源码。下一步先做[Publisher → DAG Component → Observer 闭环工程](closed-loop-project.md)：它把 Proto、Bazel、Writer、Component、DAG、输出 channel 和严格 observer 放在同一条链中，正好可以验证“Reader callback”和“Component::Proc”为什么属于两种不同的业务入口。

固定提交的官方示例使用 Apollo 自己的 `proto_library` 与 `apollo_cc_binary` 宏。把 schema 放在独立 Bazel package，可以让生成代码同时被 writer、reader 和其他模块复用。

`demo/proto/BUILD`：

```python
load("//tools/proto:proto.bzl", "proto_library")
load("//tools:apollo_package.bzl", "apollo_package")

package(default_visibility = ["//visibility:public"])

proto_library(
    name = "status_proto",
    srcs = ["status.proto"],
)

apollo_package()
```

该宏为 C++ 消费者生成约定名称 `status_cc_proto`。随后在 `demo/BUILD` 中声明两个二进制：

```python
load("//tools:apollo_package.bzl", "apollo_cc_binary", "apollo_package")

apollo_cc_binary(
    name = "status_writer",
    srcs = ["status_writer.cc"],
    deps = [
        "//cyber",
        "//demo/proto:status_cc_proto",
    ],
)

apollo_cc_binary(
    name = "status_reader",
    srcs = ["status_reader.cc"],
    deps = [
        "//cyber",
        "//demo/proto:status_cc_proto",
    ],
)

apollo_package()
```

依赖方向是 `binary -> generated C++ proto -> schema`，而不是让运行时在启动时寻找 `.proto` 文件。`status.pb.h` 是构建产物，C++ 源码包含它；Cyber 同时把 message type 和 proto descriptor 填入 endpoint 的 `RoleAttributes`，用于拓扑发现和工具展示。

运行：

```bash
bazel build -c opt //demo:status_writer //demo:status_reader
./bazel-bin/demo/status_reader
./bazel-bin/demo/status_writer
```

Apollo 官方 API 文档给出了 Node、Writer、Reader 与 talker/listener 的同类路径。[Cyber RT API for Developers](https://apollo.baidu.com/docs/apollo/9.x/md_cyber_2docs_2cyber__api__for__developers.html)

## 读懂日志中的延迟

`now - timestamp_ns` 包含发布排队、传输、接收调度和 callback 启动延迟，但要求两端时钟可比较。跨主机测试要先同步时钟；否则负值或巨大偏差不代表 Cyber 出错。

同时记录 sequence。sequence 跳变表示应用观察到数据缺口；延迟增加但 sequence 连续，通常是排队或调度拥塞。

还应把三个时间点分开记录：消息内的生产时间、callback 开始时间、业务处理完成时间。前两者之差近似输入年龄，后两者之差是本地处理时间。只统计端到端总数时，无法判断问题来自 transport、pending queue、scheduler 等待还是 callback 本身。

## 常见失败

| 现象 | 优先检查 |
|---|---|
| channel 不出现 | Node/Writer 创建、进程是否退出、环境脚本 |
| channel 出现但 listener 无日志 | 类型、名字、Reader handle 生命周期 |
| 一段时间后延迟上升 | callback 阻塞、消费者队列、CPU 调度 |
| 跨主机不可见 | `CYBER_IP`、RTPS 配置、网卡/防火墙 |
| 大消息延迟异常 | transport 选择、SHM 配置、序列化与拷贝 |

## 退出验收

固定源码中，SIGINT handler `OnShutdown()` 只把全局状态从 initialized 改为 shutting-down。随后 `OK()` 变为 false，publisher 循环退出；`WaitForShutdown()` 每 200 ms 检查一次状态并返回。真正的全局资源清理位于 `Clear()`，顺序包含 SysMo、TaskManager、TimingWheel、scheduler、TopologyManager、Transport 和 logger。

若自建 worker，建议按以下顺序关闭：

```text
SIGINT / stop request
  -> callback 不再接受新业务任务
  -> 释放 Reader，移除其 scheduler task 与 topology role
  -> drain 或 cancel 应用 worker queue
  -> join worker threads
  -> 释放 Writer 与 Node
  -> 进程退出，由 atexit 调用幂等 Clear
```

验收时不仅检查“进程退出”，还要确认没有 callback 在业务对象析构后继续访问捕获的 `this`，worker 都能 join，最后一条日志可见，并且重复清理不会崩溃。
