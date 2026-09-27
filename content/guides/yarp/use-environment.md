# YARP 使用教程：Name Server、网络检查与 CMake

YARP 开发首先需要可用的 Name Server。应用 Port 以逻辑名字注册，连接方解析 Contact 后直接建立数据连接。本章先建立控制面与数据面的边界，再搭出可重复构建的 C++ 工程，最后给出启动检查和最短故障定位路径；后续的 Port、Carrier 与运维章节都依赖这张网络图。

示例固定到 YARP 提交 [`91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`](https://github.com/robotology/yarp/tree/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9)。`Network` 的初始化契约来自 [`Network.h`](https://github.com/robotology/yarp/blob/91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9/src/libYARP_os/src/yarp/os/Network.h)，CMake imported target 的用法与该版本官方教程一致。

## 先建立正确的网络模型

YARP 网络由两段路径组成：

```text
控制面：进程 -> Name Server，注册或查询 /atlas/state:o
数据面：发送进程 -> 接收进程，按查询到的 Contact 直接建立 Carrier 连接
```

因此 `yarp name list` 能看到端口，只证明名字注册存在；它不证明目标进程还活着、Carrier 握手成功或应用正在产生新数据。反过来，Name Server 短暂停止时，已经建立的 TCP 数据连接仍可能继续工作，只是新端口无法注册、新连接无法解析。

端口后缀 `:o`、`:i` 是帮助人阅读的命名约定，不是 Name Server 强制的类型系统。连接方向最终由应用打开的端口和 connect 请求决定。把方向写入名字仍然很有价值，因为它让部署文件和诊断输出可以静态检查。

## 启动网络

终端 A：

**教学代码（不是固定提交源码摘录）：**

```bash
yarpserver
```

终端 B：

**教学代码（不是固定提交源码摘录）：**

```bash
yarp check
yarp name list
```

若 server 地址不正确，检查 `yarp conf` 输出。官方入门示例从启动 yarpserver 开始。[YARP first example](https://www.yarp.it/latest/companion_use.html)

## CMake

**教学代码（不是固定提交源码摘录）：**

```cmake
cmake_minimum_required(VERSION 3.16)
project(yarp_demo LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(YARP REQUIRED COMPONENTS os)

add_executable(sender sender.cpp)
target_link_libraries(sender PRIVATE YARP::YARP_os YARP::YARP_init)

add_executable(receiver receiver.cpp)
target_link_libraries(receiver PRIVATE YARP::YARP_os YARP::YARP_init)
```

两个 target 含义不同：`YARP::YARP_os` 提供 Port、Bottle、NetworkBase 等通信实现；`YARP::YARP_init` 提供完整 `Network` 初始化与插件注册相关部分。代码里构造 `yarp::os::Network` 时应显式链接后者。`PRIVATE` 表示这两个依赖只用于当前可执行文件的编译与链接，不会被当作接口继续传播给下游 target。

不要把 include 路径和 `.lib/.so` 路径手工写死。`find_package` 选定的 YARP 安装同时决定头文件、库和可用组件；imported target 会携带这些属性，从而避免“编译使用 A 版本头文件、运行加载 B 版本动态库”的隐蔽 ABI 问题。配置后可检查 CMake 输出中的 `YARP_DIR`，部署时再确认动态链接器实际加载的库来自同一安装前缀。

建议把第一个工程保持为可重复构建的最小目录：

```text
yarp_demo/
├── CMakeLists.txt
├── sender.cpp
├── receiver.cpp
└── scripts/connect.sh
```

构建与运行顺序：

**教学代码（不是固定提交源码摘录）：**

```bash
cmake -S . -B build
cmake --build build
# 另一个终端已运行 yarpserver
./build/receiver
./build/sender
yarp connect /atlas/state:o /atlas/state:i tcp
```

先启动接收者再启动发送者不是协议要求，而是让第一次消息不会在尚未连接时消失。生产系统不应依赖人工启动速度，应使用持久连接或显式部署编排，并让发布者报告当前输出连接数。

### `Network` 对象为什么应当先构造、最后析构

**教学代码（不是固定提交源码摘录）：**

```cpp
int main() {
  yarp::os::Network network;
  if (!network.checkNetwork(2.0)) {
    std::cerr << "YARP name server unavailable\n";
    return 1;
  }

  yarp::os::BufferedPort<yarp::os::Bottle> port;
  if (!port.open("/atlas/state:o")) {
    return 2;
  }

  // 使用 port……
  port.close();
  return 0;
}
```

局部变量按构造的逆序析构：先构造 `network`、后构造 `port`，函数退出时就先销毁 Port，再由 `Network::~Network()` 完成进程级清理。这是 RAII 在中间件生命周期中的直接应用。如果把 Network 放进比 Port 更短的作用域，Port 析构时依赖的全局通信设施可能已被回收。

`checkNetwork(2.0)` 只验证给定超时内能否联系名字服务，不会预先证明未来的 Port 打开、Carrier 握手和业务数据都成功。它应是启动状态机中的一个明确检查点，而不是“网络健康”的总开关。

## 命令行建立基线

**教学代码（不是固定提交源码摘录）：**

```bash
yarp read /atlas/in
yarp write /atlas/out
yarp connect /atlas/out /atlas/in tcp
yarp ping /atlas/in
```

`yarp connect` 可指定 tcp、udp、mcast 等 Carrier，并支持 persistent connection。[YARP command interface](https://yarp.it/latest/group__yarp.html)

先用命令行证明名字和 Carrier 可用，再调试 C++ 对象。

## 名字与 Contact 的边界

应用通常只把逻辑名交给 `Port::open()`；库向 Name Server 注册监听地址。直接使用 `Contact` 可以绕过名字查询，适合封闭系统或定位控制面故障，但会失去逻辑命名与重新部署的灵活性。

命名建议包含系统、功能和方向，例如 `/robot1/localization/pose:o`，而不要包含会频繁变化的 IP、PID 或容器编号。多个实例必须从命令行或配置获得前缀，不能把同一个全局名字硬编码进二进制。

**教学代码（不是固定提交源码摘录）：**

```cpp
yarp::os::Network yarp;
if (!yarp.checkNetwork(2.0)) {
    std::cerr << "YARP name server unavailable\n";
    return 1;
}
```

这里的超时属于启动策略。运行期不要在高速循环里重复 `checkNetwork()`；名字服务健康和现有数据链路健康是不同信号，应分别监控。

把名字解析结果想成带时效的控制面事实，而不是永久地址。进程重启后，同一个逻辑名可以注册到新的 host/port；持久缓存旧 Contact 会绕过重新部署能力。只有在隔离测试或没有名字服务的封闭部署中，才应把显式 Contact 作为配置，并同时承担地址冲突、变更和证书/防火墙规则的管理成本。

## 启动状态机

把一串返回值折叠成“启动失败”会丢掉最有价值的诊断边界。更清晰的状态机是：

```text
ProcessInitialized
  -> NameServerReachable       checkNetwork 成功
  -> PortRegistered            open 成功且 where() 可报告 Contact
  -> RouteConnected            getOutputCount/getInputCount 达到预期
  -> FreshDataObserved         sequence 或 envelope 时间持续更新
  -> Ready                     业务依赖全部满足
```

控制程序只有到 `FreshDataObserved` 才能确认上游真的在工作。若某条输入不是启动必需项，应为它定义 `Degraded` 路径和恢复条件，而不是无限阻塞整个进程。每次转换记录单独的超时与错误码，运维人员便能区分“名字服务不可达”“名字冲突”“连接未建”和“连接存在但生产者静默”。

## 常见故障的最短诊断路径

| 现象 | 首先检查 | 原因边界 |
|---|---|---|
| `open()` 失败 | 同名端口、配置的 Name Server | 注册失败发生在控制面 |
| 名字存在但 connect 失败 | `yarp ping`、监听地址、Carrier | 端点或握手失败 |
| connect 成功但无数据 | 发布频率、输出连接数、sequence | 应用可能没有 write |
| 间歇性旧数据 | strict policy、消费者耗时、时间戳 | 连接存在不代表数据新鲜 |
| 重启后连到旧实例 | persistent 规则和陈旧注册 | 部署状态未清理 |

## 环境验收

- `yarp check` 成功；
- 两个临时 Port 能 connect/disconnect；
- Name Server 停止后现有连接行为与预期一致；
- 重启 Name Server 后陈旧注册可清理；
- 使用的 Carrier 在两端都可用且版本一致。
