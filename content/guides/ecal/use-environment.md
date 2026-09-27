# eCAL 使用教程：SDK、CMake 与监控工具

eCAL 可通过系统安装包或源码构建获得 SDK。C++ 工程需要 CMake 找到 eCAL package config，并按数据类型链接对应 imported target。官方 6.1 文档要求 C++14 或更新编译器与 CMake 3.16 以上。[eCAL C/C++ Setup](https://eclipse-ecal.github.io/ecal/v6.1/getting_started/howto/setup/cpp.html)

## 先区分构建环境与运行环境

```text
构建期
  CMake package config -> include、编译定义、imported libraries

运行期
  eCAL config -> domain/discovery、network、SHM、logging
  dynamic loader -> 实际 eCAL 与序列化库
```

`find_package(eCAL)` 成功只证明构建期能找到一套 SDK。运行时仍可能加载 PATH/LD_LIBRARY_PATH 中另一版本动态库，或使用另一份配置文件加入错误通信域。启动日志应输出 eCAL 版本、进程名、配置来源、主机和关键 network/SHM 选项。

## 最小工程结构

```text
ecal_demo/
  CMakeLists.txt
  sender.cpp
  receiver.cpp
```

```cmake
cmake_minimum_required(VERSION 3.16)
project(ecal_demo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(eCAL REQUIRED)

add_executable(sender sender.cpp)
target_link_libraries(sender PRIVATE eCAL::string_core)

add_executable(receiver receiver.cpp)
target_link_libraries(receiver PRIVATE eCAL::string_core)
```

二进制 blob 只需 `eCAL::core`；string、protobuf、Cap’n Proto 和 FlatBuffers 使用相应扩展 target。不要手写 include/library 路径，imported target 会携带版本匹配的编译与链接信息。

### Imported target 的 CMake 含义

```cmake
target_link_libraries(sender PRIVATE eCAL::string_core)
```

这一行不只是增加一个 `.lib` 或 `.so`。Imported target 还会传播头文件目录、必要编译定义和间接依赖；`PRIVATE` 表示这些使用要求只服务于 sender 自身，不作为 sender 的公共接口继续传播。

如果公共头文件暴露 eCAL 类型，则依赖可能需要 `PUBLIC`；更稳妥的库设计是把中间件类型留在 `.cpp` 或 pImpl 内，让业务公共 API 使用自己的消息类型，降低 SDK 版本渗透范围。

## 配置与构建

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

若 `find_package(eCAL)` 失败，检查 SDK 是否包含 CMake config，并将安装前缀加入 `CMAKE_PREFIX_PATH`：

```bash
cmake -S . -B build -DCMAKE_PREFIX_PATH=/opt/ecal
```

Windows 使用安装器时，优先从与 SDK 架构一致的 Visual Studio generator 构建，避免 x86/x64 或 Debug/Release runtime 混用。

## 一个最小运行探针

在编写 Publisher 前，先确认 Runtime 能初始化并按退出信号收束：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
#include <chrono>
#include <iostream>
#include <thread>
#include <ecal/ecal.h>

int main(int argc, char** argv) {
  if (eCAL::Initialize(argc, argv, "atlas_probe") != 0) {
    std::cerr << "eCAL initialization failed\n";
    return 1;
  }
  while (eCAL::Ok()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  eCAL::Finalize();
}
```

初始化返回约定和头文件路径以所锁定主版本为准。探针的价值是把 SDK/配置/退出问题与 topic、schema 和 callback 问题分开。Monitor 应先看到这个进程，再继续发布订阅教程。

## 源码构建边界

官方稳定文档提供 Windows 与 Ubuntu 的源码构建依赖和 submodule 步骤；只需要通信 SDK 时可关闭不需要的 GUI/app，减少 Qt 等依赖。[eCAL Build from Source](https://eclipse-ecal.github.io/ecal/stable/development/building_ecal_from_source.html)

源码构建后应安装到独立 prefix，再让业务工程 `find_package`，不要直接依赖 eCAL build tree 内部 target 路径。

独立 prefix 使安装产物成为清晰边界：业务工程不会偶然链接 build tree 中尚未安装的私有 target，也能在 CI、开发机和部署镜像中使用同一 package config。源码 commit、构建选项和依赖版本应随 prefix 生成 manifest。

## 运行前的域与网络检查

同一 eCAL 生态中的进程需要一致的配置和可达网络。开发初期先在单机运行，再扩展到多机。排查顺序：

1. 两个进程能否正常 `Initialize`；
2. topic 名与类型是否相同；
3. monitor 是否看到 publisher/subscriber registration；
4. 同主机 SHM 路径是否建立；
5. 跨主机 discovery 与 UDP/TCP 防火墙是否允许；
6. hostname、network interface 与配置文件是否一致。

### 三个容易混淆的成功状态

```text
Initialize 成功
  != Monitor 已看到 registration
  != Publisher 与 Subscriber 已选择可用 transport
  != callback 正在消费新鲜数据
```

Registration 是软状态控制面；SHM/UDP/TCP 是数据面。Monitor 中实体可见但频率为零时，应查 Send、transport 和 callback，而不是继续调整 discovery。反过来，双方互不可见时先修配置域、网卡和防火墙，业务解码代码尚未进入路径。

## 配置纳入版本控制

建议保存基础配置和部署覆盖，而不是在每台机器手改安装目录中的默认文件：

```text
config/
├── ecal-base.ini
├── robot-07.ini
└── lab-multihost.ini
```

启动脚本明确选择最终配置，并打印解析后的 domain、主网卡、network transport、SHM 开关和 registration 周期。秘密信息不要进入普通配置仓库；网络接口等主机差异则通过受控模板生成。

## 工具基线

使用 eCAL Monitor 观察进程、topic、类型、频率与连接；使用 Recorder/Player 固化输入；必要时用命令行工具在无 GUI 环境检查注册。工具看到实体但没有数据时，重点查发送返回、subscriber callback 和 transport 层；实体完全不可见时，先查初始化与 discovery。

工具的职责不同：Monitor 显示当前控制面和速率快照；Recorder 保存实际到达它的样本；Player 复现日志中的数据。Recorder 没记录到的网络丢失无法由回放恢复，Monitor 看见的 topic 也不保证日志包含数据。

## 环境故障定位表

| 现象 | 首先检查 | 不要先做 |
|---|---|---|
| CMake 找不到 eCAL | package config 与 prefix | 手写全部 include/lib 路径 |
| 启动缺符号 | 实际加载库与构建架构 | 修改 topic 名 |
| Monitor 无进程 | Initialize、配置来源、进程寿命 | 调整 protobuf schema |
| 双方可见但零频率 | Send 返回、transport、callback | 重装 SDK |
| 同机成功跨机失败 | 网卡、discovery、防火墙 | 增大应用队列 |
| 大消息慢 | SHM 选层、复制、消费者速度 | 只提高发送频率 |

## 环境验收

- CMake 能找到 `eCAL::core` 和所需扩展 target；
- sender/receiver 在同机互通；
- monitor 能看到两个进程及 topic；
- 停止 receiver 后 publisher 仍能运行，订阅数变化可见；
- recorder 能记录并回放同一数据类型；
- 多机前先固定单机结果作为对照。
