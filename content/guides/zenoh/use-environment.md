# Zenoh 使用教程：C++ 绑定、后端与 Router

Zenoh C++ API 是 header-only binding，但实际运行仍依赖 `zenoh-c` 或 `zenoh-pico` 后端。桌面、服务器和完整路由功能通常选择 zenoh-c；资源受限设备可评估 zenoh-pico。两者不是仅改变链接库，支持能力也可能不同。

## 先理解四层依赖

```text
业务代码
  -> zenoh-cpp：C++17 类型、RAII 句柄、回调适配
  -> zenoh-c / zenoh-pico：稳定 C ABI 与具体后端
  -> Rust Zenoh runtime（zenoh-c 后端）或 pico runtime
  -> transport / router / peer
```

`zenoh-cpp` 是头文件绑定，不代表通信实现被编译进每个业务源文件。它把 C++ 对象和异常/结果转换映射到后端拥有的句柄。头文件与动态库版本不匹配时，问题可能表现为缺符号、选项缺失或 ABI 行为差异，因此必须把 cpp、C 后端和 router 的版本组合一起锁定。

## 安装关系

```text
application
  -> zenoh-cpp headers
       -> zenoh-c      full backend
          or
       -> zenoh-pico   constrained backend
```

官方 zenoh-cpp 要求 C++17，并通过 CMake 选择后端。[zenoh-cpp README](https://github.com/eclipse-zenoh/zenoh-cpp/blob/main/README.md)

## CMake 工程

将 binding 与 backend 在构建阶段明确配对：下面只是 CMake 形状示例，target 名称必须按所选 zenoh-cpp 与后端版本核对。
**示例身份：教学配置或命令；须按目标版本核对。**
```cmake
cmake_minimum_required(VERSION 3.16)
project(zenoh_demo LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)

find_package(zenohc REQUIRED)
find_package(zenohcxx REQUIRED)

add_executable(z_pub z_pub.cpp)
target_link_libraries(z_pub PRIVATE zenohcxx::zenohc)

add_executable(z_sub z_sub.cpp)
target_link_libraries(z_sub PRIVATE zenohcxx::zenohc)
```

zenoh-pico 后端则查找 pico package 并链接 `zenohcxx::zenohpico`。配置输出应明确显示实际找到的后端，避免意外链接另一套安装。

建议工程固定以下结构：

```text
zenoh_demo/
├── CMakeLists.txt
├── cmake/Dependencies.cmake
├── config/router.json5
├── config/client.json5
├── include/atlas/codec.hpp
└── src/
    ├── publisher.cpp
    ├── subscriber.cpp
    └── queryable.cpp
```

依赖文件记录精确 tag 或包版本，配置文件记录拓扑。不要让某个可执行文件通过源码常量连接测试 router，另一个则依赖自动 scouting；这种“功能都正常”的混合环境很难复现。

构建后检查最终链接对象，而不只检查 CMake 没报错。Linux 可查看动态依赖，Windows 可检查 DLL 搜索路径；运行日志至少输出 binding/backend 版本、mode、连接 endpoint 和 Session zid。

## 先运行官方示例

zenoh-c examples 包含 `z_pub`、`z_sub`、`z_get`、`z_queryable`、吞吐、延迟和 shared-memory 示例。[zenoh-c examples](https://github.com/eclipse-zenoh/zenoh-c/blob/main/examples/README.md)

**示例身份：教学配置或命令；须按目标版本核对。**
```bash
z_sub -k 'demo/**'
z_pub -k demo/example/test -p 'Hello World'
```

随后验证查询：

**示例身份：教学配置或命令；须按目标版本核对。**
```bash
z_queryable -k demo/example/queryable -p 'reply'
z_get -s 'demo/**'
```

官方示例先于自写程序，可以分离安装/网络问题和 C++ API 使用错误。

官方仓库把可同时运行于 zenoh-c 与 zenoh-pico 的例子放在 universal 目录，把 zenoh-c 特有能力放在 zenohc 目录。这也是业务代码应采用的能力分层：核心 pub/sub 不依赖后端特有 API，需要 SHM 或特定扩展的模块显式声明 zenoh-c 约束。[zenoh-cpp 官方仓库](https://github.com/eclipse-zenoh/zenoh-cpp)

## Peer、Client 与 Router 部署

开发机可从默认配置开始。跨子网、容器或受控拓扑时，启动 `zenohd` router，并让 client 显式连接 endpoint。Router 配置应进入版本控制，至少固定 mode、listen/connect endpoints、scouting、transport 与访问控制。

不要把“同机自动发现成功”当成跨网络部署依据。容器广播、NAT 与防火墙会改变 scouting 行为，生产环境通常更适合显式 endpoint。

### 一个最小可审计拓扑

```text
robot client --connect--> router tcp/10.0.0.10:7447 <--connect-- edge client
```

Router 使用 JSON5/YAML 配置文件启动，Client 明确 mode 与 connect endpoint。官方配置文档说明配置可以来自文件、命令行和 admin space；某些字段只在启动时读取，因此运行时写入配置并不等于行为已经改变。[Zenoh Configuration](https://zenoh.io/docs/manual/configuration/)

配置装载顺序也属于部署协议。应记录最终合并值，限制生产环境 admin space 的写权限，并把“配置已接受”和“transport 已建立”作为不同状态。

## C++ 句柄与移动语义

Session、Publisher、Subscriber 等句柄代表后端资源。它们通常应移动而非随意复制：

```cpp
auto session = zenoh::Session::open(std::move(config));
auto publisher = session.declare_publisher(zenoh::KeyExpr("robot/state"));
auto worker = PublisherWorker(std::move(publisher));
```

`std::move` 不会直接搬运网络连接，它把 C++ 句柄的所有权转交给新对象；被移动对象仍可析构，但不能再假设它拥有有效实体。若包装自己的服务类，应删除复制构造，提供 `noexcept` 移动，并让显式 `close()` 与析构的保证不同：析构负责不泄漏，`close()` 负责等待到达规定的关闭屏障。

## 资源生命周期

Session 应在普通程序作用域内显式销毁，Publisher/Subscriber/Queryable 先于 Session 释放。避免把 Session 作为静态全局对象留到 `atexit`，因为 C++ binding 后端和 Rust runtime 的退出顺序可能已经拆除线程局部状态。

```text
main scope
  Session
    Publisher / Subscriber / Queryable
  explicitly destroy entities
  close/destroy Session
return
```

如果 callback 捕获应用队列，顺序还要更精确：先停止新业务输入，撤销 Subscriber/Queryable 以阻止新 callback，再关闭并 join 应用 worker，最后关闭 Session。队列不能先析构，否则仍在运行的后端 callback 会访问悬空引用。

## 环境故障的分层判定

| 现象 | 所在层 | 首先核对 |
|---|---|---|
| CMake 找不到 target | 包/构建 | prefix、backend target、架构 |
| 启动缺符号 | ABI/动态链接 | cpp 与 C 后端版本、实际加载库 |
| Session 打开但不匹配 | 发现/拓扑 | mode、endpoint、scouting、router |
| 匹配但没有样本 | key/ACL/应用 | keyexpr、put 权限、callback 队列 |
| Query 有回复但不结束 | 查询生命周期 | Final、超时、仍被保存的 Query |

## 环境验收

- CMake 输出确认正确后端；
- `z_pub`/`z_sub` 在默认配置下互通；
- 显式 router endpoint 下仍能互通；
- `z_get` 能收到 queryable reply 并正常结束；
- Ctrl+C 后进程无挂起，Session 在 main 返回前关闭；
- 关闭 router 后 client 的断连与重连行为有日志可见。
