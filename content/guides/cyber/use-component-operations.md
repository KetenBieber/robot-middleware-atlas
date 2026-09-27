# Cyber RT 使用教程：Component、DAG、Launch 与日常运维

涉及 Cyber RT 实现行为时以 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 为准；本篇直接展示业务配置与示例代码。

独立 Node 程序适合工具和简单进程；Apollo 业务模块通常实现 Component，由 mainboard 按 DAG 动态加载，再用 launch 文件组合多个模块。

## 最小工程由哪些文件组成

```text
demo/status_component/
├── BUILD
├── status.proto
├── status_component.h
├── status_component.cc
├── conf/status_component.pb.txt
├── dag/status_component.dag
└── launch/status_component.launch
```

这些文件分成四层：`.proto` 定义跨进程数据；C++ 类实现业务；DAG 把类、共享库和 Reader 绑定；Launch 决定进程与重启策略。任何一层单独正确都不够，部署时它们必须来自同一构建版本。

## 最小 Component

```cpp
#include <memory>
#include "cyber/component/component.h"
#include "demo/status.pb.h"

class StatusConsumer final
    : public apollo::cyber::Component<atlas::demo::Status> {
 public:
  bool Init() override {
    writer_ = node_->CreateWriter<atlas::demo::Status>("/atlas/processed");
    return writer_ != nullptr;
  }

  bool Proc(const std::shared_ptr<atlas::demo::Status>& input) override {
    auto output = std::make_shared<atlas::demo::Status>(*input);
    output->set_text("processed:" + input->text());
    return writer_->Write(output);
  }

 private:
  std::shared_ptr<apollo::cyber::Writer<atlas::demo::Status>> writer_;
};

CYBER_REGISTER_COMPONENT(StatusConsumer)
```

`Init()` 建立长期资源，`Proc()` 处理一次调度输入。不要在每次 Proc 中重复创建 Writer。复制 protobuf 便于理解，但大消息应评估复制成本和可变所有权。

### 逐行理解 C++ 类型关系

```cpp
class StatusConsumer final
    : public apollo::cyber::Component<atlas::demo::Status>
```

`public` 继承让框架可以通过 `ComponentBase*` 多态调用生命周期接口；模板参数 `Status` 则在编译期固定主输入类型。`final` 表示这个部署组件不再作为基类，既表达设计意图，也避免继续继承后误解析构和注册行为。

`Init() override` 的 `override` 要求基类存在同签名虚函数，拼错参数时编译器立即报错。`Proc(const shared_ptr<Status>&)` 以 const 引用接收智能指针：不增加一次不必要的引用计数，消息对象又通过 `shared_ptr` 在本次调用期间保持存活。

`writer_` 作为成员保存长期实体。若它只是 `Init()` 中的局部变量，函数返回后 Writer 可能析构，组件仍能运行却不再拥有有效输出端。成员声明顺序也参与析构顺序；依赖 Node/runtime 的实体必须在底层运行时仍然存在时释放。

`CYBER_REGISTER_COMPONENT(StatusConsumer)` 把具体类注册到运行时工厂。DAG 的 `class_name` 最终要命中这个注册名；宏不是创建全局组件实例，而是提供从字符串到构造函数的桥梁。

## 从单输入扩展到“触发输入 + 辅助状态”

仿照 Planning 的常见模式，让 `/atlas/status` 触发 Proc，让低频 `/atlas/config` 更新辅助状态：

```cpp
struct LocalView {
  std::shared_ptr<const atlas::demo::Status> status;
  std::shared_ptr<const atlas::demo::Config> config;
};

class StatusConsumer final
    : public apollo::cyber::Component<atlas::demo::Status> {
 public:
  bool Init() override {
    writer_ = node_->CreateWriter<atlas::demo::Status>("/atlas/processed");
    config_reader_ = node_->CreateReader<atlas::demo::Config>(
        "/atlas/config",
        [this](const std::shared_ptr<atlas::demo::Config>& message) {
          auto snapshot = std::make_shared<atlas::demo::Config>(*message);
          std::lock_guard<std::mutex> lock(config_mutex_);
          latest_config_ = std::move(snapshot);
        });
    return writer_ != nullptr && config_reader_ != nullptr;
  }

  bool Proc(const std::shared_ptr<atlas::demo::Status>& input) override {
    LocalView view;
    view.status = input;
    {
      std::lock_guard<std::mutex> lock(config_mutex_);
      view.config = latest_config_;
    }
    if (!view.config) return PublishNotReady(*input, "missing config");
    return RunOnce(view);
  }

 private:
  std::mutex config_mutex_;
  std::shared_ptr<const atlas::demo::Config> latest_config_;
  std::shared_ptr<apollo::cyber::Reader<atlas::demo::Config>> config_reader_;
  std::shared_ptr<apollo::cyber::Writer<atlas::demo::Status>> writer_;
};
```

这里有两次不同目的的所有权动作：callback 深复制 Config，使快照不依赖输入对象后续用途；Proc 在锁内只复制 `shared_ptr`，用较短临界区冻结本轮 LocalView。算法运行在锁外，因此新的 Config callback 不会等待整个业务计算结束。

`shared_ptr<const Config>` 让 Proc 只能读取快照。`const shared_ptr<Config>` 只会禁止修改指针本身，仍允许修改对象；二者语义不同。若 Config 很大且 callback 高频，可直接保存框架传入的不可变 shared pointer，但必须确认上游不会在 Write 后修改对象。

lambda 捕获 `[this]` 不会延长组件寿命。关闭时框架必须先撤销 Reader callback，再析构 mutex 和 `latest_config_`。自行封装组件时，可让 callback 捕获 `weak_ptr<State>`，但 Cyber Component 本身通常由插件工厂管理，不能假设可直接对 `this` 使用 `shared_from_this()`。

## 数据新鲜度不是“指针存在”

Config 快照还应保存 sequence 和源时间：

```cpp
struct TimedConfig {
  std::shared_ptr<const atlas::demo::Config> value;
  std::uint64_t sequence{};
  std::int64_t source_time_ns{};
  std::int64_t receive_time_ns{};
};
```

`value != nullptr` 只说明曾收到过配置。机器人运行数小时后，这份配置可能已经失效。Proc 应根据业务选择源时间、接收时间或版本号判断 stale，并把 not-ready 原因发布给下游，而不是只写日志后返回 false。

## DAG 绑定类、共享库与 Reader

```protobuf
module_config {
  module_library: "demo/libstatus_component.so"
  components {
    class_name: "StatusConsumer"
    config {
      name: "status_consumer"
      readers {
        channel: "/atlas/status"
      }
    }
  }
}
```

`class_name` 必须和注册宏暴露的类一致，`module_library` 必须能在运行环境解析。Reader 顺序对应模板参数顺序；多输入 Component 配错顺序可能类型检查失败或无法建立预期融合。

### DAG 如何变成对象

```text
module_library
  -> ClassLoader 装载 libstatus_component.so
class_name
  -> 工厂创建 StatusConsumer
config.name
  -> 组件实例/调度任务身份
readers[0]
  -> Component<Status> 的主输入 Reader
```

辅助 `/atlas/config` 不出现在模板主输入列表中，因为它由 `Init()` 显式创建。这个区别决定调度语义：Status 到达会让组件 routine 就绪；Config 到达只更新状态，不自动执行完整 Proc。

多输入模板的 Reader 顺序是协议的一部分。例如 `Component<A, B, C>` 的 DAG 必须按框架预期对应 A、B、C。不要依靠三个 channel 恰好使用相同 protobuf 类型来掩盖顺序错误；名称、类型和功能角色都应在部署检查中输出。

## Launch 组合进程

```xml
<cyber>
  <module>
    <name>atlas_status_pipeline</name>
    <dag_conf>/apollo/demo/status.dag</dag_conf>
    <process_name>atlas_pipeline</process_name>
    <exception_handler>respawn</exception_handler>
  </module>
</cyber>
```

启动：

```bash
cyber_launch start demo/status.launch
```

也可直接调试 DAG：

```bash
mainboard -d demo/status.dag
```

launch 中相同 `process_name` 的组件可被放入同一进程，影响 transport 选择和故障隔离。吞吐敏感模块可受益于进程内路径；需要崩溃隔离的模块应分进程。官方文档说明 launch 可装载 DAG 或启动子进程，并支持异常处理策略。[Launch API 文档](https://apollo.baidu.com/docs/apollo/9.x/md_cyber_2docs_2cyber__api__for__developers.html)

## BUILD 目标关系

不同 Apollo 分支的 Bazel 宏会演进，最可靠做法是复制同一分支附近组件的 BUILD 形状。依赖关系应保持为：

```text
status.proto -> generated C++ proto target
status_component.cc
  -> component shared library
     -> //cyber
     -> status proto
     -> config proto / algorithm library
DAG + launch + config
  -> runtime data/install targets
```

组件库不能只依赖头文件路径；链接阶段要包含注册宏、protobuf 实现和算法符号。Apollo 的组件构建宏若负责保留注册对象，不要擅自改成普通 `cc_library` 后再猜为什么运行时工厂为空。

业务算法最好单独做普通 C++ library：

```cpp
class StatusAlgorithm {
 public:
  Result Run(const LocalView& view) const;
};
```

这样单元测试可直接构造 LocalView，不需要启动 mainboard、ClassLoader 或 transport。Component 只承担配置、快照、调用算法和发布结果。

## 配置错误定位顺序

1. Bazel 是否生成目标 `.so`；
2. DAG 中 library 路径是否为运行环境内路径；
3. `class_name` 与注册名是否一致；
4. protobuf target 是否链接进共享库；
5. Reader channel 和输入模板顺序是否一致；
6. mainboard 日志是否出现 class loader/ABI 错误；
7. `cyber_monitor` 是否看到输入数据。

可以把启动看成一笔事务：库装载、实例构造、`Init()`、主 Reader 建立和 Scheduler task 创建全部成功后，组件才算可服务。任何阶段失败都要逆序撤销已建立实体，并保持动态库装载到最后一个对象析构之后。

## 组件运行状态与错误协议

`Proc()` 返回 false 通常表示本次处理失败，但下游未必能看到原因。对安全关键流水线，建议发布显式状态：

```text
READY / NOT_READY / DEGRADED / FAILED
reason
input_sequence
input_age
component_timestamp
```

状态 channel 与业务输出可以分开，也可嵌入业务消息 header。关键是让监控和下游区分“没有触发”“触发后输入无效”“算法失败”和“输出发送失败”。

异常不应越过插件和调度边界。能够恢复的业务错误转成状态；不可恢复的资源/配置错误让 Init 失败；真正异常在组件边界捕获、记录并交由进程级策略处理。

## SHM、INTRA 与 RTPS 的使用判断

- 同进程组件优先利用 INTRA，减少序列化和跨进程同步；
- 同主机跨进程大消息常使用 SHM；
- 跨主机使用 RTPS 等网络路径。

改变 transport 后要重新测量延迟、CPU、内存和数据年龄。SHM 减少 payload 拷贝，不代表 notifier 和调度没有延迟。官方 FAQ 提供 `cyber.pb.conf` 中 notifier/transport 配置方向。[Cyber RT FAQ](https://apollo.baidu.com/docs/apollo/latest/md_cyber_2docs_2cyber__faqs.html)

## Record 驱动的组件回归

```bash
cyber_recorder play -f captured.record -c /atlas/status
```

同时运行 mainboard 和只订阅输出的检查程序，验证输出 sequence、时间戳与业务字段。回归输入固定后，才能比较 Component 改动前后的处理时间和丢帧。

回归包还要保存 DAG、Launch、组件配置、proto 版本和 Apollo commit。Record 固定了消息字节与时间序列，却不会自动固定插件类名、调度策略或算法参数。

验证“仿 Planning”组件时至少准备三组记录：正常输入；缺失/过期 Config；主输入突发并伴随慢算法。分别检查输出正确性、not-ready 协议、buffer 覆盖和 Proc 长尾。

## 关闭顺序

```text
停止上游或阻止新触发
  -> 从 Scheduler 撤销组件 task
  -> 停止主 Reader 与辅助 Reader callback
  -> 等待在途 Proc/callback 返回
  -> 释放 Writer、Reader、算法与快照
  -> 析构 Component
  -> 最后卸载 component shared library
```

若先卸载 `.so`，在途虚函数、lambda 或析构函数的机器码都会失效。若只设置 stopping 标志却不等待 callback 返回，`[this]` 捕获仍可能访问已析构 mutex。关闭完成必须由 task/callback barrier 证明，而不是由布尔值推断。

## 运维检查表

- launch 进程和 mainboard PID 可追踪；
- 每个关键输入/输出有 frequency、sequence、age；
- callback/Proc 超时和调度 backlog 有指标；
- record 文件轮转，不耗尽磁盘；
- respawn 有频率限制，避免崩溃循环；
- stop 时先停止数据源，再停止消费者并等待在途 Proc 收束；
- 升级 Apollo 时重新验证 DAG schema、插件 ABI 与 transport 配置。
