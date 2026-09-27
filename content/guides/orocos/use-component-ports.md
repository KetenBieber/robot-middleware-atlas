# Orocos RTT 使用教程：Activity、Port 与 ConnPolicy

室内底盘控制器每 1 ms 更新一次速度指令，但上游导航节点可能以不规则速率写入：若每个输入都排队，控制器会逐条执行已经过期的速度；若只保留最新值，导航状态更及时，却会跳过中间命令。Port connection 的 ConnPolicy 正是把这一选择写成数据合同的地方，Activity 则决定控制代码在哪个执行上下文运行。

本文涉及的 RTT 固定到 [orocos-toolchain/rtt commit `600102e8be9c81905b20930e32d43b28244ab173`](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。连接策略的满载语义见 [`ConnPolicy.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ConnPolicy.hpp#L50-L105)，InputPort 的 `read()`/`readNewest()` 路径见 [`InputPort.hpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/InputPort.hpp#L136-L166)。

## 配置周期 Activity

为控制器选择 scheduler、priority、period 和 CPU affinity。周期为 1 ms 不代表一定满足 1 kHz；需要测量 release jitter、Engine step WCET 和 deadline miss。

这是部署上的执行顺序示意，不是 RTT 内部源码调用图：

```text
load -> set properties -> set Activity -> connect ports
     -> configure -> start
```

不要在组件 Running 时任意替换 Activity。

## DATA 与 BUFFER

状态流使用 DATA：消费者读取当前值，允许跳过中间样本。事件流使用有界 BUFFER：保留顺序，但必须设置容量和满载策略。

```cpp
// 教学最小例子：状态保留最新值，离散事件保存有界 FIFO
RTT::ConnPolicy latest = RTT::ConnPolicy::data();
RTT::ConnPolicy events = RTT::ConnPolicy::buffer(64);
```

`ConnPolicy::data()` 与 `buffer()` 是该固定版本提供的工厂；普通 BUFFER 满时拒绝新样本，CIRCULAR_BUFFER 满时丢弃最旧样本。容量只限制排队项数，不限制控制线程每轮要处理多少回调，也不限制动态字段内存。若到达率长期高于消费率，两种 BUFFER 最终都会满；要避免执行旧命令，控制器还需限制样本最大 age。

## 读取 FlowStatus

```cpp
// 教学最小例子：将 FlowStatus 映射为控制器可观察的安全策略
State state;
switch (state_in_.read(state)) {
  case RTT::NewData:
    last_update_ = now();
    process(state);
    break;
  case RTT::OldData:
    if (now() - last_update_ > timeout_) enter_safe_state();
    break;
  case RTT::NoData:
    enter_safe_state();
    break;
}
```

不能把 OldData 当作稳定的新测量。状态消息还应带来源时间戳和 sequence。

## Event Port

InputPort 可以注册为 EventPort。固定源码的 `DataFlowInterface::addEventPort()` 为输入端设置 data-on-port callback，默认 callback 调用 `TaskCore::trigger()`；它请求 Engine/Activity 处理后续工作，不代表用户 callback 已经开始运行，也不代表 OS 已经给线程分配 CPU。周期 Activity 仍按周期进入工作步骤；输入突发可以累积多个通知或队列项，结果取决于连接与 Engine 的队列语义。源码见 [`DataFlowInterface.cpp`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/DataFlowInterface.cpp#L97-L160) 及 [`ExecutionEngine::process(PortInterface*)`](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L265-L277)。事件触发与周期调度的 API 细节可再对照 [Orocos Components Manual](https://orocos.org/stable/documentation/rtt/v2.x/doc-xml/orocos-components-manual.html)。

## Operation 线程选择

修改组件状态的 Operation 使用 OwnThread，让调用进入组件 Engine；纯只读且线程安全的短操作可使用 ClientThread。高优先级线程不要同步等待低优先级组件的长 OwnThread Operation。

## 完整示例：周期速度限幅组件

这个组件接收上游速度命令，检查新鲜度并输出限幅后的命令。它足够小，却同时覆盖生命周期、DATA 端口、属性、预分配和安全停止。

```text
velocity_limiter/
├── CMakeLists.txt
├── include/velocity_limiter/VelocityLimiter.hpp
├── src/VelocityLimiter.cpp
├── config/limiter.cpf
└── deploy/limiter.ops
```

消息类型在真实工程中通常由 typekit 提供。为了突出组件逻辑，这里先使用固定大小、无堆分配的类型：

```cpp
// 教学最小例子：固定大小消息让赋值成本不随样本长度扩张
struct VelocityCommand {
    double linear{};
    double angular{};
    std::uint64_t sequence{};
    std::int64_t source_time_ns{};
};
```

头文件声明端口和生命周期 hook。端口是长期成员，而不是在 `updateHook()` 中临时创建；这样连接图可以在 configure/start 之前完成。

```cpp
// 教学最小例子：组件接口与预先构造的端口/缓存
#pragma once
#include <cstdint>
#include <rtt/Port.hpp>
#include <rtt/TaskContext.hpp>
#include <rtt/os/TimeService.hpp>

class VelocityLimiter final : public RTT::TaskContext {
public:
    explicit VelocityLimiter(const std::string& name);

    bool configureHook() override;
    bool startHook() override;
    void updateHook() override;
    void stopHook() override;
    void cleanupHook() override;

private:
    RTT::InputPort<VelocityCommand> command_in_{"command_in"};
    RTT::OutputPort<VelocityCommand> command_out_{"command_out"};

    double max_linear_{0.5};
    double max_angular_{1.0};
    double timeout_s_{0.1};
    VelocityCommand input_{};
    VelocityCommand output_{};
    RTT::os::TimeService::ticks last_new_data_{};
    bool have_sample_{false};
};
```

构造函数只登记接口，不打开设备，也不启动线程：

```cpp
// 教学最小例子：构造阶段仅登记接口，不启动设备 I/O
VelocityLimiter::VelocityLimiter(const std::string& name)
    : RTT::TaskContext(name) {
    ports()->addPort(command_in_)
        .doc("Latest commanded base velocity");
    ports()->addPort(command_out_)
        .doc("Bounded command; zero when input is stale");

    addProperty("max_linear", max_linear_);
    addProperty("max_angular", max_angular_);
    addProperty("timeout_s", timeout_s_);
}
```

`configureHook()` 验证可部署参数。属性可能来自配置文件，不能假设默认值永远有效：

```cpp
// 教学最小例子：验证部署属性并准备首次周期所需状态
bool VelocityLimiter::configureHook() {
    if (!(max_linear_ > 0.0) || !(max_angular_ > 0.0)) return false;
    if (!(timeout_s_ > 0.0 && timeout_s_ <= 5.0)) return false;
    input_ = {};
    output_ = {};
    have_sample_ = false;
    return true;
}

bool VelocityLimiter::startHook() {
    if (!command_in_.connected() || !command_out_.connected()) return false;
    last_new_data_ = RTT::os::TimeService::Instance()->getTicks();
    return true;
}
```

是否强制端口已连接是业务选择。控制输出通常选择“缺连接则拒绝启动”，监控输出则可能允许无人订阅。这个判断应写进组件契约，而不是交给部署者猜测。

核心循环区分 NewData、OldData 与 NoData。下面用 `TimeService` 表达单调时钟思想；时间换算函数以所用 RTT 版本为准：

```cpp
// 教学最小例子：检查新鲜度，再输出限幅或安全速度
void VelocityLimiter::updateHook() {
    const auto status = command_in_.read(input_);
    const auto* clock = RTT::os::TimeService::Instance();

    if (status == RTT::NewData) {
        have_sample_ = true;
        last_new_data_ = clock->getTicks();
        output_ = input_;                         // 固定大小复制
        output_.linear = std::clamp(input_.linear,
                                    -max_linear_, max_linear_);
        output_.angular = std::clamp(input_.angular,
                                     -max_angular_, max_angular_);
    }

    const double age_s = clock->secondsSince(last_new_data_);
    if (!have_sample_ || status == RTT::NoData || age_s > timeout_s_) {
        output_.linear = 0.0;
        output_.angular = 0.0;
    }
    command_out_.write(output_);
}

void VelocityLimiter::stopHook() {
    output_.linear = 0.0;
    output_.angular = 0.0;
    command_out_.write(output_);                  // 尽力发布最终安全值
}

void VelocityLimiter::cleanupHook() {
    have_sample_ = false;
}
```

这里没有在循环中构造字符串、扩容容器或写日志。`std::clamp` 只做比较；固定大小结构复制的成本有明确上界。若消息含 `std::vector`，即使对象是成员，赋值也可能扩容；需要在 configure 阶段 reserve，或改用固定容量类型并验证最大长度。

## 组件注册与构建

源文件末尾把类型导出为可由部署器加载的组件：

```cpp
// 教学最小例子：导出组件工厂入口
#include <rtt/Component.hpp>
ORO_CREATE_COMPONENT(VelocityLimiter)
```

典型 CMake 结构如下。不同发行版对宏和包名可能略有差异，应以本机 `orocos-rtt` 导出的 CMake 配置为准：

```cmake
# 教学最小例子：示意 component target 的 RTT 构建依赖
cmake_minimum_required(VERSION 3.16)
project(velocity_limiter LANGUAGES CXX)

find_package(OROCOS-RTT REQUIRED COMPONENTS rtt-scripting)
include(${OROCOS-RTT_USE_FILE_PATH}/UseOROCOS-RTT.cmake)

orocos_component(velocity_limiter
  src/VelocityLimiter.cpp)
target_include_directories(velocity_limiter PRIVATE include)
target_compile_features(velocity_limiter PRIVATE cxx_std_17)
orocos_generate_package()
```

构建步骤通常是建立独立 build 目录、运行 CMake、编译并把生成的组件路径加入部署器搜索路径。先用普通调度策略验证功能，再配置实时调度；实时权限错误与业务逻辑错误不应在第一次运行时混在一起。

## 部署脚本如何装配运行时

下面是部署意图，不承诺所有 RTT 发行版的脚本拼写完全相同；应对照所用 Deployer 版本调整命令：

```text
import("velocity_limiter")
loadComponent("limiter", "VelocityLimiter")
loadComponent("source",  "CommandSource")
loadComponent("sink",    "CommandSink")

limiter.max_linear = 0.8
limiter.max_angular = 1.2
limiter.timeout_s = 0.10

setActivity("limiter", period=0.001, scheduler=REALTIME, priority=80)
connect("source.command", "limiter.command_in", policy=DATA)
connect("limiter.command_out", "sink.command", policy=DATA)

assert(limiter.configure())
assert(source.configure())
assert(sink.configure())
start consumers, then limiter, then source
```

启动顺序从下游到上游，避免生产者先发数据而消费者尚未就绪；停止顺序反过来，从上游切断新数据，再停止控制器和下游。部署脚本还应打印解析后的周期、优先级、属性和连接策略，便于现场确认“实际运行的配置”而非模板内容。

## 从功能测试走到时序测试

先验证数值：输入 `(2.0, -3.0)` 时输出被限制为 `(0.8, -1.2)`。再验证时间：停止 source 超过 100 ms，输出必须变为零。最后验证资源边界：以高于控制周期的速度写入 DATA，组件只取最新状态，内存和延迟不随测试时间增长。

对控制组件，推荐至少记录：输入 sequence 跳变、样本 age、单次 update 耗时、最大 update 耗时、deadline miss 和安全状态进入次数。只看平均周期会掩盖长尾抖动。

## 验收

- DATA 写入多次后读取到最新值；
- BUFFER/CIRCULAR 满载时丢弃方向符合设计；
- `readNewest` backlog 成本不破坏周期；
- EventPort 洪泛时 update 有明确预算；
- stop 后不再执行 updateHook，Activity 线程已退出。
