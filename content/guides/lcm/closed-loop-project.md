# LCM 实战：从 schema 到双进程收发闭环

前面的文章已经分别拆过类型系统、Provider、UDPM、接收缓存、订阅分发和 C++ callback。真正开始做项目时，这些知识不会按照文章章节一个个出现，而会在几十行程序里同时发生。本页把一个完整的小工程逐文件摊开：一条三关节状态怎样从 schema 生成 C++ 类型，怎样被 sender 编码并发送，怎样进入 receiver 的 handleTimeout()，以及怎样在 callback 返回后安全结束其借用生命周期。

工程真实文件位于仓库：

~~~text
examples/lcm/closed_loop/
├── CMakeLists.txt
├── README.md
├── types/
│   └── joint_state_t.lcm
└── src/
    ├── sender.cpp
    └── receiver.cpp
~~~

本页展示的四份核心代码与这些真实文件保持同步，并由 tools/check_lcm_closure.py 检查。它们不是上游 LCM 示例，而是本专题为了把前面讲过的机制连接成一个工程而写的教学项目。

当前环境仍缺可用的 LCM CMake package 与 GLib2 package，所以这里区分“项目代码已经形成”与“真实 UDPM sender ↔ receiver 已在本机跑通”。后文的预期输出只表示程序的成功条件，不冒充实测结果。

## 先明确这个小工程到底要验证什么

如果目标只是打印 Hello World，真正的机制很容易被隐藏。本工程故意加入 sequence、timestamp、数组长度校验、订阅容量、deadline 和非零退出码，让一次运行至少能回答：

~~~text
sender：
  Provider 是否创建成功？
  每条 typed message 是否走完本地 publish？

receiver：
  是否真正收到 20 条？
  sequence 是否连续？
  joint_count 是否符合业务约束？
  handle loop 是否在 deadline 内完成？
  退出前是否明确解除订阅？
~~~

即使这些都通过，也只能说明这一次运行中的 20 条消息完整到达，不能把 UDPM 推断成可靠传输。

## 整条消息链先画一遍

~~~text
joint_state_t 对象
      |
      | generated getEncodedSize / encode
      v
临时 wire bytes
      |
      | lcm::LCM::publish
      v
lcm_publish -> provider vtable -> lcm_udpm_publish
      |
      | UDP multicast
      v
recv_thread -> 协议检查 / LC03 重组 -> inbufs_filled
      |
      | notify pipe
      v
receiver main thread -> handleTimeout()
      |
      v
lcm_dispatch_handlers
      |
      v
typed trampoline decode
      |
      v
Handler::onState()
      |
      v
callback 返回 -> payload / 描述符进入回收路径
~~~

下面逐个文件解释这条链。

## 文件一：joint_state_t.lcm —— 通信协议从这里开始

真实文件：examples/lcm/closed_loop/types/joint_state_t.lcm

~~~text
package atlas;
struct joint_state_t {
    int64_t timestamp_us;
    int64_t sequence;
    int32_t joint_count;
    double joints[joint_count];
}
~~~

### 为什么不能直接发送 C++ struct？

如果定义一个含 std::vector 的 C++ struct，再把 sizeof(struct) 个字节送进 socket，发出去的是本进程对象布局，其中可能包含指针、size、capacity 和 padding；另一个进程无法把这些地址解释成自己的数据。

LCM schema 把“业务字段”与“语言对象布局”分开。生成器为 C++ 产生 atlas::joint_state_t，同时生成编码大小计算、encode、decode 和类型指纹逻辑。网络上传输的是稳定的 wire bytes，而不是 C++ 对象镜像。

### 四个字段分别解决什么问题？

timestamp_us 是源端业务时间，用来计算数据年龄。它和接收端什么时候收到数据不是同一个时间。

sequence 用来发现缺帧或乱序。只看“现在有数据”无法知道中间是不是漏过一条。

joint_count 和 joints.size() 构成业务冗余不变量。本工程要求：

~~~text
joint_count == joints.size() == 3
~~~

LCM 能负责按 schema 解码，却不知道你的机械臂一定有三个关节，所以业务仍要自己校验。

joints[joint_count] 表明 wire format 中数组长度由 joint_count 决定。发布前把 count 写错，不应被视为小问题。

## 文件二：CMakeLists.txt —— 把 schema 生成纳入构建图

真实文件：examples/lcm/closed_loop/CMakeLists.txt

~~~cmake
cmake_minimum_required(VERSION 3.16)
project(atlas_lcm_closed_loop LANGUAGES CXX)
find_package(lcm REQUIRED)
include(${LCM_USE_FILE})
lcm_wrap_types(CPP_HEADERS atlas_generated_headers types/joint_state_t.lcm)
lcm_add_library(atlas_messages_cpp CPP ${atlas_generated_headers})
target_include_directories(atlas_messages_cpp INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>)
add_executable(atlas_sender src/sender.cpp)
add_executable(atlas_receiver src/receiver.cpp)
target_compile_features(atlas_sender PRIVATE cxx_std_17)
target_compile_features(atlas_receiver PRIVATE cxx_std_17)
lcm_target_link_libraries(atlas_sender atlas_messages_cpp ${LCM_NAMESPACE}lcm)
lcm_target_link_libraries(atlas_receiver atlas_messages_cpp ${LCM_NAMESPACE}lcm)
~~~

它建立的是：

~~~text
joint_state_t.lcm
      |
      | lcm_wrap_types
      v
生成 atlas/joint_state_t.hpp
      |
      | lcm_add_library
      v
atlas_messages_cpp
      |                     |
      +----------+----------+
                 |
        +--------+--------+
        v                 v
  atlas_sender      atlas_receiver
        \                 /
         +----- LCM runtime
~~~

find_package(lcm REQUIRED) 不只是找一个头文件。后面使用的 lcm_wrap_types、lcm_add_library、lcm_target_link_libraries 都来自 LCM CMake package。缺少正确安装的 package 时，工程应该尽早配置失败，而不是偷偷链接到未知版本。

生成头文件放在 CMAKE_CURRENT_BINARY_DIR，是因为它属于构建产物，不应该手工复制进源码树。atlas_messages_cpp 则把 schema 的生成结果变成 sender 和 receiver 的共同依赖，让两端从同一份协议来源构建。

## 文件三：sender.cpp —— typed object 怎样走到 Provider

真实文件：examples/lcm/closed_loop/src/sender.cpp

~~~cpp
#include <lcm/lcm-cpp.hpp>
#include "atlas/joint_state_t.hpp"
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    const std::string url =
        argc > 1 ? argv[1] : "udpm://239.255.76.67:7667?ttl=0";
    lcm::LCM bus(url);
    if (!bus.good()) {
        std::cerr << "Failed to create LCM provider: " << url << '\n';
        return 1;
    }
    // 先启动接收端：UDPM 不为尚未在线的订阅者保存历史。
    for (int64_t seq = 0; seq < 20; ++seq) {
        atlas::joint_state_t state;
        state.timestamp_us = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        state.sequence = seq;
        state.joints = {0.01 * static_cast<double>(seq),
                        0.02 * static_cast<double>(seq),
                        -0.01 * static_cast<double>(seq)};
        state.joint_count = static_cast<int32_t>(state.joints.size());
        const int rc = bus.publish("ATLAS_JOINT_STATE", &state);
        if (rc < 0) {
            std::cerr << "Local publish failed at seq=" << seq << '\n';
            return 2;
        }
        std::cout << "publish seq=" << seq
                  << " bytes=" << state.getEncodedSize() << '\n';
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    // 本地 publish 成功不是订阅者的交付确认。
    return 0;
}
~~~

### URL 实际上在选择运行时策略

~~~text
udpm://239.255.76.67:7667?ttl=0
        |
        v
lcm_create()
        |
        v
解析 scheme = udpm
        |
        v
provider registry -> udpm vtable -> create()
~~~

所以业务代码始终调用 bus.publish()，而不是自己写 UDPM / MEMQ / LOGFILE 的 switch。更深的函数指针和实例状态设计见 [Provider 与 vtable](../../articles/lcm/provider-vtable.md)。

### state 与真正发送的 bytes 不是同一对象

循环里每次构造 atlas::joint_state_t state。固定版本 C++ wrapper 发布 typed message 时会经历：

~~~text
state.getEncodedSize()
        |
申请临时 byte buffer
        |
state.encode(buffer)
        |
进入无类型 publish(channel, bytes, length)
        |
provider.publish
        |
返回
        |
释放临时 byte buffer
~~~

这说明 sender 保留 state 并不意味着 provider 永远借用这只 C++ 对象，也不意味着编码 buffer 在 publish 返回后仍然有效。

### timestamp 和 sequence 为什么要同时存在？

sequence 回答“中间有没有缺号”；timestamp 回答“这条虽然连续的数据是不是已经太旧”。

真实控制输入通常至少检查：

~~~text
sequence 是否连续？
now - timestamp_us 是否超过 freshness deadline？
~~~

本小工程实现了 sequence 检查，freshness 可作为下一步练习。

### publish 返回 0 不等于远端 ACK

sender 只把负返回值视为本地 publish 失败。它不能证明：

~~~text
receiver 已经收到
receiver 已经 decode
callback 已经执行
真实执行器已经采用
~~~

因此运动命令若需要可靠业务语义，必须另外设计 COMMAND(sequence=N) / ACK(sequence=N, result=...)，并加入 deadline 与失效状态。

## 文件四：receiver.cpp —— 订阅真正难的是生命周期

真实文件：examples/lcm/closed_loop/src/receiver.cpp

~~~cpp
#include <lcm/lcm-cpp.hpp>
#include "atlas/joint_state_t.hpp"
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>

class Handler {
public:
    void onState(const lcm::ReceiveBuffer* raw, const std::string& channel,
                 const atlas::joint_state_t* msg) {
        if (msg->joint_count != static_cast<int32_t>(msg->joints.size()) ||
            msg->joint_count != 3) {
            std::cerr << "Rejected inconsistent joint_count\n";
            ++invalid;
            return;
        }
        if (last_seq >= 0 && msg->sequence != last_seq + 1) {
            std::cerr << "gap/reorder: previous=" << last_seq
                      << " new=" << msg->sequence << '\n';
            ++gaps;
        }
        last_seq = msg->sequence;
        ++received;
        std::cout << channel << " seq=" << msg->sequence
                  << " raw_bytes=" << raw->data_size
                  << " joint0=" << msg->joints[0] << '\n';
        // raw->data 和 msg 都只借用本次回调的内存；
        // 异步转交之前必须复制为业务自己拥有的消息。
    }
    int received{};
    int invalid{};
    int gaps{};
    int64_t last_seq{-1};
};

int main(int argc, char** argv) {
    const std::string url =
        argc > 1 ? argv[1] : "udpm://239.255.76.67:7667?ttl=0";
    // Handler 先构造、后析构，确保整个 LCM 订阅生命周期内对象存活。
    Handler handler;
    lcm::LCM bus(url);
    if (!bus.good()) {
        std::cerr << "Failed to create LCM provider: " << url << '\n';
        return 1;
    }
    auto* sub = bus.subscribe("ATLAS_JOINT_STATE", &Handler::onState, &handler);
    if (!sub || sub->setQueueCapacity(4) < 0) {
        std::cerr << "subscribe/configuration failed\n";
        return 2;
    }
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(12);
    while (handler.received < 20 && std::chrono::steady_clock::now() < deadline) {
        const int rc = bus.handleTimeout(250);
        if (rc < 0) {
            std::cerr << "handle failed\n";
            return 3;
        }
    }
    std::cout << "received=" << handler.received
              << " gaps=" << handler.gaps
              << " invalid=" << handler.invalid << '\n';
    bus.unsubscribe(sub);
    if (handler.invalid != 0)
        return 4;
    if (handler.received != 20 || handler.gaps != 0) {
        // 完整交付验收：UDP 本身仍然不保证可靠交付。
        std::cerr << "Incomplete delivery: expected 20 ordered messages\n";
        return 5;
    }
    return 0;
}
~~~

receiver 同时暴露了五个关键问题：Handler 生命周期、typed trampoline、callback 借用期、订阅配额和退出验收。

### Handler 为什么在 bus 前构造？

局部对象按构造的逆序析构：

~~~cpp
Handler handler;
lcm::LCM bus(url);
~~~

因此 main 退出时先析构 bus，再析构 handler。这样 C++ LCM wrapper 清理内部 subscription 期间，Handler 仍然存在。正常路径又显式调用 bus.unsubscribe(sub)，让订阅结束点更加明确。

这和 C 核心的 callback_scheduled / marked_for_deletion 不是同一个层次。C 核心能延迟释放 subscription，却不能替你保证用户 Handler* 指向的 C++ 对象还活着。

### callback 里的 raw 和 msg 都只是借用

typed trampoline 的逻辑可以压缩成：

~~~text
lcm_recv_buf_t
      |
      v
LCMMHSubscription::cb_func
      |
创建临时 joint_state_t msg
      |
decode(raw bytes)
      |
Handler::onState(..., &msg)
      |
callback 返回
      |
临时 msg 析构
provider 回收原始 payload
~~~

所以 callback 中把 msg 指针直接放进后台队列是生命周期错误。需要异步处理时，应复制成自己拥有的 StateSnapshot 再交给 worker。

### setQueueCapacity(4) 并不是 std::queue<4>

LCM 维护共享完整消息队列和每 subscription 的准入计数。容量 4 更接近“这个订阅最多还有多少条已经准入但尚未消费的工作”。

因此它不能从根本上解决持续过载，也不能解释为四条消息的私有 FIFO。详细交错见 [订阅与分发](../../articles/lcm/subscription-dispatch.md)。

### handleTimeout(250) 为什么比永久 handle 更适合作为验收程序？

callback 仍在调用 handleTimeout 的这条线程执行。250 ms 表示单次等待最多多久返回，程序因而可以周期性重新检查：

~~~text
是否收齐 20 条？
deadline 是否达到？
provider 是否报错？
~~~

如果网络断开，永久阻塞 handle 会让测试程序没有机会报告“已经超时”。

### 为什么必须用退出码区分失败？

本工程约定：

~~~text
0 : 收齐 20 条，序号连续，字段合法
1 : Provider 创建失败
2 : subscribe / queue 配置失败
3 : handle 报错
4 : 消息违反业务结构不变量
5 : 超时、缺帧或乱序
~~~

这比“终端里打印了一些消息就返回 0”严格得多。测试是否成功可以由脚本直接判断。

## receiver 内部再往下追一次

把工程与源码对起来：

~~~text
UDP socket readable
      |
recv_thread()
      |
短消息直接得到完整 payload
大消息进入 LC03 fragment store
      |
inbufs_filled
      |
队列空 -> 非空时写 notify pipe
      |
receiver 调 handleTimeout()
      |
provider.handle()
      |
pop 一只完整 lcm_buf_t
      |
lcm_dispatch_handlers()
      |
按 channel 找 subscription
      |
typed trampoline decode
      |
Handler::onState()
      |
callback 返回
      |
释放 payload / 归还描述符
~~~

sender 与 receiver 两边各有一次重要的对象边界：

~~~text
typed C++ object -> wire bytes
wire bytes -> callback 期间临时 typed object
~~~

LCM 跨进程传输的是 bytes，不是同一个 C++ 对象。

## 如何构建和运行

在已经正确安装 LCM、GLib2 及 CMake package 的环境中，从仓库根目录执行：

~~~bash
cmake -S examples/lcm/closed_loop -B build/lcm-closed-loop \
  -DCMAKE_PREFIX_PATH=/path/to/lcm/install
cmake --build build/lcm-closed-loop --parallel
~~~

终端 A 先启动 receiver：

~~~bash
./build/lcm-closed-loop/atlas_receiver
~~~

终端 B 再启动 sender：

~~~bash
./build/lcm-closed-loop/atlas_sender
~~~

两端默认使用：

~~~text
udpm://239.255.76.67:7667?ttl=0
~~~

ttl=0 只限制 multicast 不跨路由转发，不是安全隔离。

程序设计上的成功条件是 sender 发出 sequence 0 到 19，receiver 最终报告：

~~~text
received=20 gaps=0 invalid=0
~~~

这只是验收条件，不代表当前机器已经产生过这组实测输出。

## 不要只测成功路径

| 实验 | 怎样制造 | 观察什么 | 对应机制 |
|---|---|---|---|
| 订阅者晚启动 | sender 先启动 | 早期消息不会自动补发 | UDPM 无历史存储 |
| URL 不一致 | 两端使用不同端口 | sender 可能仍本地成功，receiver 超时 | publish 成功不是 delivery ACK |
| 慢 callback | onState 睡眠 300 ms | 积压、gap、数据龄期增长 | handle 单分发者 / subscription capacity |
| schema 改动 | 只重编一端 | fingerprint / typed decode 不兼容 | generated codec |
| 大 payload | 扩大 joints | 进入 LC03 路径 | UDPM 应用层分片 |
| 错误异步处理 | 保存 msg 裸指针给 worker | 生命周期错误 | typed trampoline 临时对象 |
| 运动命令 | 把状态改为命令 | publish 返回值不足以确认执行 | ACK / deadline / fail-safe |

最推荐的是慢 callback 实验。sender 每 50 ms 产生一条，即 20 Hz；callback 若每次睡 300 ms，理论服务率最多约 3.3 Hz。任何有限队列都只能吸收短期突发，不能消灭长期生产率大于消费率的矛盾。

## 怎样把这个工程继续扩成机器人程序

真实控制系统通常会进一步拆成：

~~~text
LCM I/O / handle thread
        |
        | callback 快速校验并复制
        v
bounded owned queue 或 latest-state slot
        |
        v
control / estimation thread
        |
        v
算法与执行器
~~~

状态类数据常常更适合 latest-state；命令类数据则通常需要 sequence、ACK、去重、deadline 和 fail-safe。不能因为两者都能放进 LCM channel 就使用同一套缓存策略。

下一步可定义：

~~~cpp
struct StateSnapshot {
    std::int64_t timestamp_us;
    std::int64_t sequence;
    std::vector<double> joints;
};
~~~

callback 中构造 StateSnapshot，把自己拥有的数据交给有界队列。随后分别记录 source timestamp、callback timestamp、control-consume timestamp，就能把源数据年龄、LCM dispatch 排队与业务处理延迟拆开测量。

## 从项目代码返回源码文章

| 项目里的问题 | 深挖页面 |
|---|---|
| bus(url) 为什么只改 URL 就能换 transport？ | [Provider 与 vtable](../../articles/lcm/provider-vtable.md) |
| typed object 为什么先变 bytes？ | [类型与 EventLog](../../articles/lcm/types-and-eventlog.md) |
| 大消息如何进入 LC03？ | [UDPM 发送协议](../../articles/lcm/udpm-publish-protocol.md) |
| callback 慢时为什么网络线程还能收？ | [接收、重组与缓存](../../articles/lcm/receive-reassembly.md) |
| setQueueCapacity(4) 为什么不是私有 FIFO？ | [订阅与分发](../../articles/lcm/subscription-dispatch.md) |
| C++ 成员函数怎样注册给 C runtime？ | [C ABI 与 C++ 设计实验](../../articles/lcm/c-abi-cpp-design-lab.md) |
| 整体应该守住哪些不变量？ | [设计复盘](../../articles/lcm/design-recap.md) |
| 怎样继续做日志、丢包和回放实验？ | [运行与故障演练](use-operations.md) |

完成这个项目以后，再读 LCM 源码时就不需要把 Provider、ring、subscription、trampoline 当成互不相干的名词。你可以从 sender 或 receiver 的任意一行出发，沿着调用、线程和所有权继续钻进真实实现，再回到工程验证自己的理解。
