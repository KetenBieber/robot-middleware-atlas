# LCM 使用教程：安装、Provider URL 与网络基线

一台移动机器人在实验室里用 `udpm://239.255.76.67:7667` 发布关节状态，控制主机收不到消息时，初学者往往先怀疑 `subscribe()` 或生成类型。可观察的现象可能其实是两台主机 URL 不同、交换机没转发组播，或接收程序没调用 `handle()`；把这些因素混在一起会让一次配置错误看起来像随机丢包。本指南先编译一对 typed pub/sub 程序，再按 URL、网卡和接收循环逐层定位。

LCM 的核心依赖很少。Unix 源码构建主要需要 GLib 与 CMake 或 Meson；官方文档建议使用 out-of-source build。[LCM Build Instructions](https://lcm-proj.github.io/lcm/content/build-instructions.html)

本文命令和 API 对照源码提交 `ad0c54ce`。教程先建立一个能够生成类型、运行 typed pub/sub、记录 Provider URL 的最小工程，再解释跨主机和日志 provider；这样网络问题、schema 问题与业务问题不会一开始就混在一起。

## 运行时模型

LCM 没有独立的名字服务。使用相同 Provider URL 的进程加入同一通信域，发布者直接把带 channel 名的报文交给 provider：

```text
LCM handle
  -> Provider URL 选择 udpm / file / other provider
  -> publish(channel, bytes)
  -> 组播网络
  -> 接收 provider 的完整消息 buffer list + subscription quota
  -> 应用调用 handle() 才执行 callback
```

因此“进程创建了 LCM 对象”不会在中心目录留下可查询实体；`lcm-spy` 只能看到实际经过总线的消息。订阅者也不向发布者建立逐连接会话，这使系统简单，但没有天然的接收者数量、可靠确认或慢订阅者背压。

## 构建与安装


```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
cmake --install build --prefix "$HOME/.local"
```

非标准 prefix 下，业务工程通过 `CMAKE_PREFIX_PATH` 找到 LCM，并确保运行时动态库路径可解析。

安装后先确认三类产物，而不只检查头文件：

```text
bin/lcm-gen                 schema 编译器
lib/liblcm.*                C runtime/provider 实现
lib/cmake/lcm/*             find_package 配置和 lcmUtilities.cmake
```

`lcm-gen` 能运行但 `find_package(lcm)` 失败，通常是 CMake prefix 问题；CMake 能配置但程序启动找不到动态库，则是运行时 loader path 问题。两者属于不同阶段。

## 业务工程

固定提交提供 `LCM_USE_FILE`、`lcm_wrap_types`、`lcm_add_library` 和 `lcm_target_link_libraries`。一个同时生成 C++ 类型并构建发送/接收程序的工程可写成：


```cmake
cmake_minimum_required(VERSION 3.16)
project(atlas_lcm_demo LANGUAGES CXX)

find_package(lcm REQUIRED)
include(${LCM_USE_FILE})

lcm_wrap_types(
  CPP_HEADERS atlas_lcm_headers
  types/atlas_state_t.lcm)

# C++ 生成物是 header-only，逻辑 target 实际是 INTERFACE library。
lcm_add_library(atlas_messages_cpp CPP ${atlas_lcm_headers})
target_include_directories(atlas_messages_cpp INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>)

add_executable(atlas_sender src/sender.cpp)
lcm_target_link_libraries(atlas_sender atlas_messages_cpp ${LCM_NAMESPACE}lcm)

add_executable(atlas_receiver src/receiver.cpp)
lcm_target_link_libraries(atlas_receiver atlas_messages_cpp ${LCM_NAMESPACE}lcm)
```

`lcm_wrap_types` 创建生成规则并把输出 header 路径写入变量；`lcm_add_library(... CPP ...)` 创建承载 include path 与生成依赖的 INTERFACE target；`lcm_target_link_libraries` 同时把消息 target 与 C runtime 连接到应用。仅写 `target_link_libraries(sender lcm)` 会遗漏类型生成和 include 路径，且未必匹配安装包导出的 target 名。

CMake 3.3 以后普通 `target_link_libraries` 能传播 INTERFACE target 的生成依赖；helper 仍能兼容更老生成器。对于新工程，可以继续使用 helper 保持与官方示例一致，但应理解它解决的是“消费者编译前 header 必须已生成”的 build graph，而不是运行时功能。

完整项目通常还包含类型生成：

```text
lcm_demo/
├── CMakeLists.txt
├── types/atlas_state_t.lcm
├── src/sender.cpp
├── src/receiver.cpp
└── config/bus.env
```

生成文件可以在构建目录产生，但所有语言必须从同一份 `.lcm` 源生成。不要手工修改生成 header；重新生成会覆盖修改，也会使其他语言没有对应逻辑。

推荐将 `.lcm` 文件作为协议模块单独建 target，业务可执行文件只链接 `atlas_messages_cpp`。多个可执行文件各自运行 `lcm-gen` 容易生成重复、不一致或互相覆盖的 header。

## Provider URL

默认构造：


```cpp
lcm::LCM lcm;
if (!lcm.good()) return 1;
```

也可显式指定 URL，例如 UDPM 地址、TTL 与接收缓冲选项。所有进程必须使用相容的 multicast group/port；开发环境不要让不同项目误共享默认总线。

构造参数为空时，固定提交的选择顺序是：

```text
显式传入 lcm::LCM(url)
  > 环境变量 LCM_DEFAULT_URL
  > 编译内置默认 udpm://239.255.76.67:7667?ttl=0
```

默认 `ttl=0` 只允许本机接收，不会把组播报文送上局域网。跨主机最常见的起点是：

```text
udpm://239.255.76.67:7667?ttl=1
```

TTL 控制 IP 组播能跨越多少路由跳数，不决定使用哪块本地网卡，也不是访问控制。多网卡机器仍由路由表选择出口；VPN、容器 bridge 和 Wi-Fi/Ethernet 并存时要确认实际接口。

UDPM 支持 `recv_buf_size=N` 请求内核接收缓冲，例如：

```text
udpm://239.255.76.67:7667?ttl=1&recv_buf_size=4194304
```

这是请求值，内核可能因系统上限而只给较小缓冲。扩大 socket buffer 只能吸收短时调度抖动，不能修复长期接收速率低于发送速率。

建议通过配置或环境注入 URL，并在启动日志打印最终值。Provider URL 是部署接口，不应散落为源码常量。

一个进程中所有需要互通的 LCM 实例也必须使用一致 URL。推荐只在组合根读取环境并向下传递：


```cpp
int main() {
  const std::string url = require_config("ATLAS_LCM_URL");
  std::cout << "LCM provider=" << redact_if_needed(url) << '\n';
  lcm::LCM bus(url);
  if (!bus.good()) return 2;
  return run(bus);
}
```

这种依赖注入让单元测试能传入内存或日志 provider，也避免库内部悄悄读取不同环境值。URL 若包含凭据，日志需脱敏；默认 UDPM 参数仍应完整记录 group、port 和 TTL。

### `file://` Provider 把记录与应用 API 对齐

除独立 logger/player 外，LCM runtime 自身还支持日志文件 provider：

```text
file:///data/run.lcm?mode=w
file:///data/run.lcm?mode=r&speed=1.0
file:///data/run.lcm?mode=r&speed=0
```

写模式将 publish 事件写入日志；读模式在 `handle()` 时交付日志事件。`speed<=0` 表示尽快回放，正数按比例缩放事件间隔，还可用 `start_timestamp` 从指定微秒时间开始。这样业务代码仍依赖 `lcm::LCM`，部署配置就能在 live UDPM 与 replay 之间切换。

但 file provider 不是 UDPM 的完美替身：它没有真实网络丢包、分片和多主机时序。适合复现业务处理，不足以证明网络容量。

## 网络验证

UDPM 依赖组播。排查顺序：网卡是否支持 multicast、路由是否选择正确接口、容器是否允许组播、交换机 IGMP 策略、防火墙、两端 group/port/TTL。

先在同一主机运行，再跨主机；用小消息和单 channel 建基线。LCM 不提供端到端可靠重传，功能成功不代表无丢包。

按层次增加变量：

```text
阶段 1：同一进程或同机，默认 ttl=0
阶段 2：同网段双机，显式 ttl=1、固定 group/port
阶段 3：加入真实消息大小和频率
阶段 4：启用 logger/spy 等额外订阅者
阶段 5：进入容器、VPN、跨路由或混合速率交换机
```

前一阶段不成立时不要继续叠加后一阶段。尤其是把 ttl 从 0 改为 1 之后才有跨主机意义。

组播尤其容易受多网卡与容器网络影响。发送主机抓到报文、接收主机抓不到，优先查路由/交换机；接收主机抓到而进程看不到，优先查 group/port、socket buffer 与本机防火墙；进程能收但 callback 不运行，则检查是否持续调用 `handle()`。

交换机启用 IGMP snooping 时，需要周期性 IGMP querier 维护端口成员关系；没有 querier 的网络可能在一段时间后退化成泛洪或出现难以复现的组播行为。混合 10/100/1000 Mbps 设备时，发送给整个 VLAN 的高速组播还可能拖累慢端口和交换机队列。LCM 的简单 API 不会隐藏这些二层网络约束。

### 内核缓冲与应用队列是两层容量

```text
NIC/switch
  -> kernel UDP receive buffer (`recv_buf_size` / sysctl upper bound)
  -> LCM provider receive/reassembly structures
  -> subscription queue capacity
  -> handle() + user callback
```

提高 `recv_buf_size` 只作用于第一层；callback 阻塞导致 LCM subscription queue 满，仍会丢消息。诊断时需要同时观察内核 UDP drop、LCM sequence gap、handle 调用间隔和 callback 执行时间。

## 工具

使用 `lcm-spy` 观察 channel、类型指纹和数据；使用 logger/player 固化总线事件。工具与应用必须使用同一 Provider URL。

`lcm-spy` 能解码的前提是其运行环境加载了对应生成类型；只能看到 channel 而不能展开字段时，先核对 schema 生成物和 fingerprint。Spy/logger 都是实际订阅者，会增加主机和网络负载，不能假设观测完全免费。

## 部署配置文件

把 Provider URL、channel 前缀、schema commit 和运行模式放入一个部署清单，而不是只设置终端临时环境变量：


```yaml
middleware:
  provider_url: "udpm://239.255.80.10:7800?ttl=1&recv_buf_size=4194304"
  channel_prefix: "ROBOT_A_"
  schema_commit: "<protocol repository commit>"
  mode: "live"
runtime:
  handle_timeout_ms: 20
  max_payload_bytes: 1048576
  shutdown_deadline_ms: 1000
```

应用启动时打印归一化后的非敏感配置，并将同一清单复制进日志归档。这样“两个程序看起来都用了默认值”不会成为排障依据。

## 环境验收

- `lcm.good()` 为真；
- sender/receiver 同机互通；
- spy 能解码已安装的类型；
- 跨机 sequence 无异常大缺口；
- logger 生成文件并可回放；
- SIGINT 后 handle 循环可退出，不遗留线程或文件。

## 多项目隔离

开发机上不同项目共享默认组播地址时，相同 channel 名会互相污染，即使类型指纹最终拒绝解码，也会消耗网络与日志。为每个实验分配明确的 group/port，并把 channel 前缀、Provider URL 和 schema commit 一同归档。TTL 控制组播跨越的路由范围，不是访问控制；需要隔离或认证时必须依赖网络分段、隧道或上层安全机制。
