# Cyber RT 实战：从 Publisher 到 DAG Component 再到 Observer

前面的文章已经分别拆过 Node、Reader/Writer、DAG、Component、DataVisitor、Dispatcher、Notifier、CRoutine 和 Processor。真正进入 Apollo 工程时，这些对象不会按章节顺序出现，而会同时落在一组 `.proto / BUILD / .cc / .h / .dag` 文件中。

本页用一个完整小工程把整条链闭起来：

~~~text
status_source
   |
   | Writer<Status>  /atlas/status/raw
   v
Cyber transport / dispatcher / DataVisitor
   |
   v
StatusTransformComponent::Proc
   |
   | Writer<Status>  /atlas/status/processed
   v
status_observer
~~~

工程真实文件位于：

~~~text
examples/cyber/closed_loop/
├── BUILD
├── README.md
├── status.proto
├── status_source.cc
├── status_transform_component.h
├── status_transform_component.cc
├── status_transform.dag
└── status_observer.cc
~~~

这组文件不是 Apollo 上游原样示例，但所有 Cyber API、Bazel 宏、Component 注册和 DAG 形状都按本地固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 对照。项目页展示的核心文件与 examples 目录保持逐字同步，并由 `tools/check_cyber_closure.py` 验证。

要先区分两个验证层级：

1. `cpp-implementation-lab.md` 里的 `mini_node.cc` 不依赖 Apollo，可以在当前机器直接编译运行，用来验证 Node/Reader/Writer、worker、weak lifetime 和 shutdown 机理。
2. 本页的真实 Cyber 工程必须放进 Apollo Bazel workspace 才能 `bazel build`。当前 Atlas 仓库不是 Apollo workspace，因此这里不会把静态检查冒充真实 Cyber E2E 运行。

## 我们到底想验证什么？

如果只写官方 talker/listener，能证明 API 会用，却还碰不到 Cyber 最关键的“业务 Component 不是 transport callback”。所以这个工程故意让消息穿过一个 DAG Component，再由第二个独立进程观察输出。

source 发送 20 条 Status，每条带：

- `sequence`：检查观察到的顺序是否连续；
- `timestamp_ns`：保留源时间，后续可算数据年龄；
- `text="raw"`：让 Component 明确改成 `processed:raw`。

observer 只有在收齐 20 条、sequence 连续、输出文本前缀正确时才返回 0。即使返回 0，也只说明这一次实验满足业务观测，不代表 Cyber RT 获得确定性实时保证。

## 整条消息链先画一遍

~~~text
status_source
  -> Node::CreateWriter
  -> Writer::Write
  -> transport
  -> Receiver / DataDispatcher
  -> CacheBuffer
  -> DataNotifier
  -> Scheduler::NotifyProcessor
  -> Processor::Run
  -> CRoutine::Resume
  -> Component::Process
  -> StatusTransformComponent::Proc
  -> output Writer::Write
  -> observer Reader
~~~

下面逐个文件解释。

## 文件一：status.proto —— 类型边界先于运行时边界

真实文件：

~~~proto
syntax = "proto2";
package atlas.cyber_demo;

message Status {
  required uint64 sequence = 1;
  required uint64 timestamp_ns = 2;
  required string text = 3;
}
~~~

`sequence` 用来发现缺号或乱序；`timestamp_ns` 用来计算数据年龄；`text` 让中间 Component 做一个可观察变换。

Cyber 的 Reader/Writer 模板参数保留编译期类型；channel 名是运行时身份。channel 名相同但 protobuf 类型不同，不应该被视为安全通信。Node 创建 endpoint 时还会把 message type / proto descriptor 放入 RoleAttributes，供 topology 与工具链使用。

## 文件二：BUILD —— 为什么工程必须进入 Apollo workspace？

真实文件：

~~~python
load("//tools/proto:proto.bzl", "proto_library")
load(
    "//tools:apollo_package.bzl",
    "apollo_cc_binary",
    "apollo_component",
    "apollo_package",
)

package(default_visibility = ["//visibility:public"])

proto_library(
    name = "status_proto",
    srcs = ["status.proto"],
)

apollo_component(
    name = "libstatus_transform_component.so",
    srcs = ["status_transform_component.cc"],
    hdrs = ["status_transform_component.h"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

apollo_cc_binary(
    name = "status_source",
    srcs = ["status_source.cc"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

apollo_cc_binary(
    name = "status_observer",
    srcs = ["status_observer.cc"],
    deps = [
        "//cyber",
        ":status_cc_proto",
    ],
)

filegroup(
    name = "conf",
    srcs = ["status_transform.dag"],
)

apollo_package()
~~~

这份 BUILD 使用 Apollo 自己的 `proto_library`、`apollo_component`、`apollo_cc_binary` 和 `apollo_package`，因此它不能在普通空 Bazel workspace 直接工作。

建议把本目录复制或挂载到固定 Apollo checkout：

~~~text
cyber/examples/atlas_closed_loop/
~~~

然后：

~~~bash
bazel build //cyber/examples/atlas_closed_loop/...
~~~

构建关系是：

~~~text
status.proto
    |
    | proto_library
    v
status_cc_proto
   /      |        \
source  component  observer
           |
           | apollo_component
           v
libstatus_transform_component.so
           |
           v
status_transform.dag
~~~

动态库产物名、DAG 的 `module_library`、注册宏生成的类名和 DAG 的 `class_name` 必须形成同一版本闭环。缺任何一环，错误都会从编译期推迟到启动时。

## 文件三：status_source.cc —— 普通业务代码只看 Node 和 Writer

真实文件：

~~~cpp
#include <cstdint>
#include <memory>

#include "cyber/cyber.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"
#include "cyber/time/rate.h"
#include "cyber/time/time.h"

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_source");
  if (!node) {
    return 2;
  }
  auto writer = node->CreateWriter<atlas::cyber_demo::Status>(
      "/atlas/status/raw");
  if (!writer) {
    return 3;
  }

  apollo::cyber::Rate rate(20.0);
  for (std::uint64_t sequence = 0;
       sequence < 20 && apollo::cyber::OK(); ++sequence) {
    auto message = writer->AcquireMessage();
    if (!message) {
      return 4;
    }
    message->set_sequence(sequence);
    message->set_timestamp_ns(apollo::cyber::Time::Now().ToNanosecond());
    message->set_text("raw");
    if (!writer->Write(message)) {
      return 5;
    }
    AINFO << "source seq=" << sequence;
    rate.Sleep();
  }
  return 0;
}
~~~

`Init(argv[0])` 建立进程级 Cyber 基础设施，不是创建某一个 channel。

`CreateNode("atlas_status_source")` 建立 endpoint 创建和 topology 身份的入口；Writer 再由：

~~~cpp
node->CreateWriter<atlas::cyber_demo::Status>("/atlas/status/raw")
~~~

创建。

source 每轮使用 `AcquireMessage()` 获取消息对象，再 `Write(shared_ptr)`。这给 transport 留下复用消息对象的入口，同时也意味着发布后不要继续改写同一 protobuf；异步路径或本地消费者仍可能共享它。

四个事件必须分开：

~~~text
Writer::Write 返回
DataVisitor 已获得输入
Component::Proc 已执行
Observer 已收到 processed 输出
~~~

source 的返回值只覆盖第一层本地 API 结果。最终业务闭环必须由 observer 单独验证。

## 文件四：StatusTransformComponent —— Cyber 的业务执行入口

头文件：

~~~cpp
#pragma once

#include <memory>

#include "cyber/component/component.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"

class StatusTransformComponent final
    : public apollo::cyber::Component<atlas::cyber_demo::Status> {
 public:
  bool Init() override;
  bool Proc(const std::shared_ptr<atlas::cyber_demo::Status>& input) override;

 private:
  std::shared_ptr<apollo::cyber::Writer<atlas::cyber_demo::Status>> writer_;
};

CYBER_REGISTER_COMPONENT(StatusTransformComponent)
~~~

实现：

~~~cpp
#include "cyber/examples/atlas_closed_loop/status_transform_component.h"

bool StatusTransformComponent::Init() {
  writer_ = node_->CreateWriter<atlas::cyber_demo::Status>(
      "/atlas/status/processed");
  return writer_ != nullptr;
}

bool StatusTransformComponent::Proc(
    const std::shared_ptr<atlas::cyber_demo::Status>& input) {
  auto output = std::make_shared<atlas::cyber_demo::Status>(*input);
  output->set_text("processed:" + input->text());
  return writer_->Write(output);
}
~~~

这里继承 `Component<Status>`，表示 Status 是业务 task 的主输入，而不是普通 inline transport callback。

在 reality mode 下，初始化会建立 Reader、DataVisitor 和 RoutineFactory/task；一次输入最终走到：

~~~text
transport receiver
   -> DataDispatcher
   -> ChannelBuffer / CacheBuffer
   -> DataNotifier
   -> Scheduler
   -> Processor
   -> CRoutine::Resume
   -> Component::Process
   -> StatusTransformComponent::Proc
~~~

因此 `Proc()` 不直接跑在收包线程里。

Writer 必须保存为成员：

~~~cpp
writer_ = node_->CreateWriter<Status>("/atlas/status/processed");
~~~

如果只保存在 Init 的局部变量里，Init 返回后 endpoint 就可能析构，Component 仍在却失去长期输出端。

Proc 复制输入构造新的 output，是最容易推理的教学写法。对大型 protobuf 要评估复制成本，但不能为了省复制直接修改一个可能被多个消费者共享的输入对象。

## 文件五：status_transform.dag —— 配置怎样变成对象？

真实 DAG：

~~~text
module_config {
  module_library: "cyber/examples/atlas_closed_loop/libstatus_transform_component.so"
  components {
    class_name: "StatusTransformComponent"
    config {
      name: "atlas_status_transform"
      readers {
        channel: "/atlas/status/raw"
      }
    }
  }
}
~~~

运行时链路：

~~~text
module_library
   -> ModuleController::LoadModule
   -> ClassLoaderManager::LoadLibrary
   -> class_name
   -> CreateClassObj<ComponentBase>
   -> ComponentBase::Initialize(config)
   -> Component<Status>::Initialize
   -> Init()
   -> Reader + DataVisitor + scheduler task
~~~

`module_library` 决定从哪里装载机器码，`class_name` 选择注册工厂中的派生类，`readers.channel` 决定主输入 channel。

DAG 不是“写了 channel 就自动有数据”。动态库、注册宏、类名、Reader、DataVisitor 和 task 都要成功建立。

## 文件六：status_observer.cc —— 最终验收必须放在另一个消费者

真实文件：

~~~cpp
#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <thread>

#include "cyber/cyber.h"
#include "cyber/examples/atlas_closed_loop/status.pb.h"

class Observer {
 public:
  void OnStatus(const std::shared_ptr<atlas::cyber_demo::Status>& message) {
    const std::int64_t sequence =
        static_cast<std::int64_t>(message->sequence());
    const std::int64_t previous = last_sequence_.exchange(sequence);
    if (previous >= 0 && sequence != previous + 1) {
      gaps_.fetch_add(1);
    }
    if (message->text().rfind("processed:", 0) != 0) {
      invalid_.fetch_add(1);
    }
    received_.fetch_add(1);
    AINFO << "observer seq=" << sequence << " text=" << message->text();
  }

  int received() const { return received_.load(); }
  int gaps() const { return gaps_.load(); }
  int invalid() const { return invalid_.load(); }

 private:
  std::atomic<int> received_{0};
  std::atomic<int> gaps_{0};
  std::atomic<int> invalid_{0};
  std::atomic<std::int64_t> last_sequence_{-1};
};

int main(int argc, char** argv) {
  if (!apollo::cyber::Init(argv[0])) {
    return 1;
  }
  auto node = apollo::cyber::CreateNode("atlas_status_observer");
  if (!node) {
    return 2;
  }

  Observer observer;
  auto reader = node->CreateReader<atlas::cyber_demo::Status>(
      "/atlas/status/processed",
      [&observer](
          const std::shared_ptr<atlas::cyber_demo::Status>& message) {
        observer.OnStatus(message);
      });
  if (!reader) {
    return 3;
  }

  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(8);
  while (observer.received() < 20 &&
         std::chrono::steady_clock::now() < deadline &&
         apollo::cyber::OK()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }

  AINFO << "received=" << observer.received()
        << " gaps=" << observer.gaps()
        << " invalid=" << observer.invalid();
  if (observer.invalid() != 0) {
    return 4;
  }
  if (observer.received() != 20 || observer.gaps() != 0) {
    return 5;
  }
  return 0;
}
~~~

observer 把 `received / gaps / invalid / last_sequence` 做成 atomic，因为 Reader callback 发生在 Cyber scheduler 的 Processor 线程，而 main 线程同时轮询完成条件与 deadline。普通 int 在这种一写一读下没有同步会构成 C++ data race。

callback 捕获 `Observer&` 能成立，是因为 reader 比 observer 晚构造；正常析构按逆序先销毁 reader，再销毁 observer。消息的 `shared_ptr` 只能保护消息本体，不会保护业务对象的生命周期。

主线程不调用 `handle()`，这和 LCM 不同。普通 Cyber Reader callback 已经被挂入 DataVisitor + scheduler task；应用主线程可以等待 shutdown 或执行其他控制逻辑。

但框架替你调度并不意味着 callback 可以无限慢。长 callback / Proc 会占用所在 Processor，增加同组其他 CRoutine 的等待。

observer 最终把程序结果变成机器可检查的退出码：

~~~text
0 : 20 条全部收到，sequence 连续，输出合法
1 : Init 失败
2 : Node 创建失败
3 : Reader 创建失败
4 : processed 内容非法
5 : deadline 到达但不完整或有 gap/reorder
~~~

## 把线程与对象重新放到一张图

~~~text
source 业务线程
  Status -> Writer::Write
      |
      v
transport / receiver 执行上下文
  deserialize / dispatch
      |
      v
DataDispatcher::Dispatch
  Fill(CacheBuffer)
      |
      v
DataNotifier::Notify
      |
      v
Scheduler::NotifyProcessor
      |
      v
Processor OS worker
  NextRoutine -> Resume
      |
      v
Component::Process -> Proc
      |
      v
processed Writer::Write

observer 进程：
transport -> DataVisitor -> Processor -> Reader callback
      |
      v
Observer::OnStatus
~~~

transport receipt、buffer ready、task wakeup、Proc start 和 observer receive 是不同时间点。延迟分析必须分别打点。

## 当前固定版本四个必须保持 OPEN 的边界

教学工程能跑通，也不能把上游已经披露的并发边界自动改成“已修复”。

### 1. Dispatcher 动态注册与 Dispatch

`DataDispatcher::AddBuffer` 修改 channel 下的 vector 时使用注册路径同步，`Dispatch` 遍历时没有同一把共同锁。这个工程采用“先把 topology 建好，再启动 source”的静态启动顺序，不把运行中热创建 Reader 当作已证明安全。

### 2. DataNotifier Add / Notify 与注销

Notifier 表也缺少完整的运行时 add/remove 静默期协议。weak_ptr 能避免部分反向续命，但不能自动保证“注销返回后绝无在途 callback”。

### 3. CRoutine::state_ 跨线程访问

固定提交的 `state_` 是普通枚举。通知侧读取、Processor/routine 侧修改，没有明显共同锁可以直接证明所有交错无 data race。因此文章可以分析结构，不能把它宣传成形式证明过的无竞争状态机。

### 4. 多输入 fusion 安装窗口

本工程故意从单输入开始。扩成 `Component<M0, M1>` 时，DataVisitor 注册 buffer 与 AllLatest 安装 fusion callback 之间存在初始化可见性窗口。若要支持热创建组件，必须另行定义发布注册和 callback 安装的同步协议。

这四项继续保留在 OPEN_ISSUES；文档闭环不等于上游并发问题关闭。

## 在固定 Apollo checkout 中运行

将本目录复制或挂载到：

~~~text
<apollo-root>/cyber/examples/atlas_closed_loop/
~~~

Apollo 根目录执行：

~~~bash
source cyber/setup.bash
bazel build //cyber/examples/atlas_closed_loop/...
~~~

终端 A：

~~~bash
mainboard -d cyber/examples/atlas_closed_loop/status_transform.dag
~~~

终端 B：

~~~bash
bazel run //cyber/examples/atlas_closed_loop:status_observer
~~~

终端 C：

~~~bash
bazel run //cyber/examples/atlas_closed_loop:status_source
~~~

先让 Component 和 observer 就绪，再启动 source，避免把“订阅端还没创建”混入正常链路验收。

observer 的设计成功条件是：

~~~text
received=20
gaps=0
invalid=0
exit code=0
~~~

当前 Atlas 仓库不是 Apollo Bazel workspace，所以这组真实 Cyber 工程只做源码形状、导航和文档同步检查，不能假称已在本机完成 Apollo E2E。

## 真正值得做的是故障实验

| 实验 | 如何制造 | 应观察什么 | 对应机制 |
|---|---|---|---|
| Component 晚启动 | source 先发 | 早期状态不能假装被处理 | discovery / transport / DAG |
| Proc 阻塞 | Proc sleep 200 ms | Processor 占用、data age 增长、pending 覆盖 | CRoutine / ring |
| Observer 慢 | callback sleep | observer backlog 与 gap | Reader / DataVisitor |
| 动态增 Reader | 运行中创建 | 只作为并发审查，不宣传安全热插拔 | Dispatcher registry |
| 双输入 | Component<M0,M1> | M0 触发与辅助输入 latest 语义 | AllLatest |
| 关闭竞争 | Proc 运行时请求 shutdown | 检查 Clear、Reader、RemoveTask 顺序 | Component lifecycle |
| class_name 写错 | 改 DAG | mainboard 应清楚启动失败 | ClassLoader |

最推荐先做 Proc sleep 200 ms。source 20 Hz，即每 50 ms 产生一次；Proc 服务率只有约 5 Hz。任何有限 pending queue 都只能吸收短时突发，不能消灭长期生产率大于消费率。

## 当前机器可直接跑的机理实验：mini_node.cc

真实 Cyber 工程依赖 Apollo workspace；本专题另有一个无外部依赖的 `mini_node.cc`，位于 [C++ 连续实现](../../articles/cyber/cpp-implementation-lab.md)。

它验证：

~~~text
Channel registry 锁外调用 callback
weak lifetime 不反向拥有 Reader
Reader worker 使用 condition_variable
shared_ptr 维持消息本体寿命
Node 析构先取得 Reader snapshot，再锁外 Stop
Stop() 唤醒并 join worker
~~~

这份 C++17 教学程序会进入 `tools/check_cpp_examples.py` 自动编译运行回归。它不是 Apollo 源码，但用来验证“如果从零实现类似边界，最小对象生命周期怎样成立”。

## 做完工程后回到源码文章

| 工程里的问题 | 深挖页面 |
|---|---|
| Node / Writer / Reader | [通信端点](../../articles/cyber/node-reader-writer.md) |
| DAG / class_name / .so | [DAG 到 Component](../../articles/cyber/dag-to-component.md) |
| Component 模板与 Proc | [C++ 类型运行时](../../articles/cyber/cpp-type-runtime.md) |
| pending queue / backlog | [有界缓存](../../articles/cyber/pending-queue-ring.md) |
| dispatch / notifier | [分发与通知](../../articles/cyber/dispatcher-notifier.md) |
| M0/M1 | [多输入融合](../../articles/cyber/multi-input-fusion.md) |
| task wakeup | [CRoutine 唤醒](../../articles/cyber/croutine-wakeup.md) |
| OS worker / Resume | [Processor 上下文切换](../../articles/cyber/processor-context-switch.md) |
| 从 transport 到 Proc | [完整消息链](../../articles/cyber/message-to-proc.md) |
| 从零重写一版 | [C++ 连续实现](../../articles/cyber/cpp-implementation-lab.md) |
| 真实大型消费者 | [Apollo Planning](case-study-apollo-planning.md) |

完成这个项目以后，应该能从 source、DAG、Component 或 observer 的任意一行继续追到真实源码，同时回答四个问题：**对象谁拥有、当前谁在执行、数据下一步放哪里、失败或关闭时怎样退出。**
