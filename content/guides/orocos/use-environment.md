# Orocos RTT 使用教程：工作区、组件库与 Deployer

移动机器人上架时，开发机能够编译控制器，却在 Deployer 创建组件时出现“找不到类型”或“undefined symbol”。这不是一个故障：源码编译、链接、操作系统装载、RTT 工厂注册和实例构造是连续但不同的阶段。下面沿着组件从 C++ 类变成运行实例的路径逐步检查。

本文用 RTT 固定源码 [orocos-toolchain/rtt commit `600102e8be9c81905b20930e32d43b28244ab173`](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173) 解释组件对象与 Activity 生命周期；部署工具 OCL 的命令名称需以本机安装版本为准。

## 开发环境边界

安装需要匹配的 RTT、OCL、typekit 和目标操作系统插件。所有组件必须使用兼容的编译器 ABI 与 RTT build configuration；不能把不同发行版编译的插件随意混装。

建议使用独立工作区和安装前缀：

```text
workspace/
  src/atlas_controller/
  build/
  install/
    lib/orocos/...
    share/orocos/...
```

加载前确认 `RTT_COMPONENT_PATH` 或部署器搜索路径包含安装目录。

## 构建产物是什么

普通可执行文件由操作系统装载其映像并从 `main()` 开始；RTT Component 通常是共享库，由部署程序在运行期装载并通过注册宏发现类型。共享库映射到进程地址空间后，其代码与只读数据可被调用；卸载前必须先停止还可能进入该库函数的线程，并销毁依赖其虚函数表或析构代码的对象。下面是动态组件的概念路径，不是 RTT loader 源码摘录：

```text
Controller.cpp
  -> compiler 生成 position-independent object
  -> linker 生成 component shared library
  -> ORO_CREATE_COMPONENT 注册构造入口
  -> Deployer dlopen/LoadLibrary
  -> 按类型名创建 TaskContext 实例
```

因此“库文件存在”只证明链接成功，不证明部署器能找到包、导出宏存在、依赖动态库可解析或 ABI 兼容。故障要按搜索路径、动态依赖、注册类型和实例构造四层定位。

一个最小构建文件可写成：

```cmake
# 教学最小例子：组件库 target 的构建关系示意
cmake_minimum_required(VERSION 3.16)
project(atlas_controller LANGUAGES CXX)

find_package(OROCOS-RTT REQUIRED)
include(${OROCOS-RTT_USE_FILE_PATH}/UseOROCOS-RTT.cmake)

orocos_component(atlas_controller src/Controller.cpp)
target_include_directories(atlas_controller PRIVATE include)
target_compile_features(atlas_controller PRIVATE cxx_std_17)
orocos_generate_package()
```

具体宏参数随安装版本和工作区工具链调整，但依赖方向不变：业务 target 链接 RTT 导出的编译定义与库，安装步骤把组件库和包元数据放到 Deployer 可搜索的位置。

## ABI 为什么必须一致

插件边界会跨过 C++ 对象、虚函数表、异常、标准库容器和 RTT 模板类型。下面任一差异都可能使“能装载的库”在创建或析构时崩溃：

- 编译器主版本或 C++ ABI 选项不同；
- Debug/Release 使用不同 runtime heap；
- RTT 编译选项、target OS plugin 或 RTT 版本不同；
- typekit 对同一消息生成了不一致的类型信息；
- 组件依赖库从另一个 prefix 被优先加载。

不要用复制 DLL/so 到可执行目录的方式掩盖依赖解析。部署清单应记录编译器、RTT/OCL commit 或包版本、构建类型、安装 prefix 和实际加载的动态库路径。

## 最小组件类

```cpp
// 教学最小例子：只展示组件接口和 hook 的组织，不是固定提交摘录
#include <rtt/Component.hpp>
#include <rtt/Port.hpp>
#include <rtt/TaskContext.hpp>

class Controller : public RTT::TaskContext {
 public:
  explicit Controller(const std::string& name)
      : TaskContext(name), input_("state"), output_("command") {
    addPort(input_);
    addPort(output_);
    addProperty("gain", gain_);
  }

  bool configureHook() override { return gain_ > 0.0; }
  bool startHook() override { return input_.connected(); }

  void updateHook() override {
    double state;
    if (input_.read(state) == RTT::NewData) {
      output_.write(-gain_ * state);
    }
  }

 private:
  double gain_{1.0};
  RTT::InputPort<double> input_;
  RTT::OutputPort<double> output_;
};

ORO_CREATE_COMPONENT(Controller)
```

构造函数只声明接口；资源预分配放入 `configureHook`；`startHook` 检查运行条件；`updateHook` 保持有界。

官方 Component Builder Manual 从 TaskContext、接口和部署器逐层介绍这一模型。[Orocos Components Manual](https://orocos.org/stable/documentation/rtt/current/doc-xml/orocos-components-manual.html)

### 逐行理解这个类

`Controller` 继承 `TaskContext`，因此对象不仅是算法，还带状态机与反射接口。`InputPort<double>` 和 `OutputPort<double>` 是长期成员，构造函数通过 `addPort` 把它们登记到接口；Property 保存的是对 `gain_` 的管理入口，配置修改最终写回同一个成员。

`configureHook()` 只验证静态参数，成功后 TaskContext 才进入 Stopped；`startHook()` 检查运行前条件，成功后 Activity 才能反复调用 `updateHook()`。`read()` 返回 `NewData/OldData/NoData`，示例只在 NewData 时计算，因此不会把同一测量反复当成新事件。

这些 hook 的状态提交不是组件自己调用的普通顺序函数：固定版 [TaskCore::configure/start](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L96-L128) 先检查旧状态并调用虚 hook，只有成功后才提交目标状态；Activity 的工作线程再由 ExecutionEngine 检查状态后调用 updateHook，见 [ExecutionEngine::processHooks](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L360-L392)。

这里仍是教学组件，不是完整控制器：缺少输入超时、安全零输出、固定周期 Activity、端口策略和数值有效性检查。这些内容在后续组件教程中补齐。

## 配置、构建与安装闭环

```bash
# 教学命令：配置、构建并安装一个独立工作区
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/opt/atlas-orocos
cmake --build build --parallel
cmake --install build
```

然后在同一 shell 明确设置组件搜索路径并启动 Deployer。不要只在 IDE 中配置环境变量，否则命令行、服务管理器和容器会加载另一套路径。

环境验收分两步：先让动态加载器列出所有依赖均可解析，再让 Deployer 列出 `Controller` 类型。若前者失败，问题在操作系统动态库层；若前者成功而类型不可见，问题在 Orocos 包元数据、注册宏或搜索目录。

## Deployer 启动基线

部署脚本的核心动作是：导入组件包、加载实例、设置属性、连接端口、配置 Activity、configure、start。先用 TaskBrowser 手工执行并观察返回值，再固化为部署脚本。

每个转换都检查结果。`configure()` 或 `start()` 返回 false 时，不继续执行后续步骤；查询组件状态和日志，定位具体 hook。

### 手工基线与脚本化

第一次装载时按如下事务执行：

```text
import package
  -> loadComponent("controller", "Controller")
  -> 检查 ports/properties/operations
  -> controller.gain = 2.0
  -> set Activity(period, scheduler, priority)
  -> connect input/output with explicit ConnPolicy
  -> controller.configure()
  -> controller.start()
```

命令拼写以安装版本的 Deployer/TaskBrowser 为准。关键是保留每一步的返回值和最终解析配置。脚本失败时执行逆序回滚：已经 start 的先 stop，已经 configure 的再 cleanup，最后卸载实例和组件库。

## 类型与 Typekit

基本类型通常可直接用于 Port；自定义消息要让 RTT 知道其类型名、构造/复制方式和可能的 transport 表示。Typekit 连接 C++ 类型与运行时类型系统，使 Deployer 能检查两个端口是否兼容，并让脚本/序列化层认识字段。

仅仅让两个组件包含同一个头文件还不够：动态部署时它们可能分别链接不同版本的消息库。消息类型、typekit 和组件必须作为同一个版本集合发布；修改字段布局后需要重新生成并重新部署全部相关二进制。

## 最短故障定位表

| 现象 | 层次 | 首先检查 |
|---|---|---|
| Deployer 找不到包 | 搜索路径 | 安装 prefix、RTT_COMPONENT_PATH |
| 包可见但类型不可创建 | 注册 | 导出宏、组件名、加载日志 |
| 装载时报缺符号 | ABI/依赖 | 实际动态库路径、编译器与 RTT 版本 |
| Port 不能连接 | 类型系统 | typekit、端口方向、消息版本 |
| configure 返回 false | 组件状态机 | 属性值、设备资源、hook 日志 |
| start 返回 false | 运行前条件 | 连接、Activity、设备健康 |

## 环境验收

- Deployer 能发现组件库和 typekit；
- 创建实例后能列出 Port、Property 与 Operation；
- configure/start/stop/cleanup 状态转换正确；
- 组件库卸载前 Activity 已停止；
- Debug 与 Release、编译器和 RTT ABI 保持一致。
