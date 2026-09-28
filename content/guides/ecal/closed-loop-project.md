# eCAL 实战：三进程 Relay、STL 数据结构与 OS 边界

学会 `Publisher::Send()` 和 `Subscriber::SetReceiveCallback()` 只代表会调用 API。真正理解 eCAL，要能沿一条消息持续回答四个问题：

1. 这一层为什么选择这个 STL 容器，而不是另一个？
2. 这个对象是谁拥有，生命周期由谁结束？
3. 当前代码运行在业务线程、接收线程还是后台维护线程？
4. 这里跨越的是线程、进程，还是 Linux/Windows 的内核资源边界？

本页用一个三进程工程把这些问题串起来：

~~~text
atlas_ecal_source
   |
   | /atlas/raw
   v
atlas_ecal_relay
   | Subscriber callback
   | Decode -> processed -> Publisher::Send
   v
/atlas/processed
   |
   v
atlas_ecal_observer
~~~

工程真实文件位于：

~~~text
examples/ecal/closed_loop/
├── CMakeLists.txt
├── README.md
├── wire_codec.h
├── wire_codec_test.cpp
├── source.cpp
├── relay.cpp
└── observer.cpp
~~~

这组代码按本地固定 eCAL 提交 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 的 Core API 编写，不是上游原样示例。项目页中的六个核心文件与 `examples/ecal/closed_loop/` 保持逐字同步，由 `tools/check_ecal_closure.py` 校验。

## 先把固定源码中的数据结构地图摆出来

| 位置 | 固定源码数据结构 | 它表达的语义 |
|---|---|---|
| `CSubGate` | `unordered_multimap<string, shared_ptr<CSubscriberImpl>>` | 一个 topic 可以对应多个 Subscriber，按 topic 高频查找 |
| `CPubGate` | `multimap<string, shared_ptr<CPublisherImpl>>` | 一个 topic 可以对应多个 Publisher |
| Publisher connection | `map<SampleIdentifier, SConnection>` | 每个远端 Subscriber 有稳定 identity 和连接状态 |
| Subscriber connection | `map<PublicationInfo, SConnection>` | 每个 Publisher 保存 datatype、layer state、连接状态 |
| SHM slots | `vector<shared_ptr<CSyncMemoryFile>>` | 固定槽位按整数下标轮转 |
| SHM process refs | `map<int32_t, set<EntityIdT>>` | 一个 OS 进程内可能有多个订阅 endpoint |
| SHM observer pool | `map<string, shared_ptr<CMemFileObserver>>` | memfile 名到 observer 生命周期 |
| Registration batch | `CExpandingVector<Sample>` | 周期 clear/reuse，减少重复构造和释放 |
| Registration expiry | `CExpirationMap = map + list` | map 做身份查找，list 做按时间淘汰 |
| blocking Read | `string + mutex + condition_variable` | 最近一条 mailbox，不是历史 FIFO |
| ID filter | `set<long long>` | 明确的成员资格集合 |
| duplicate cache | `map<Publisher, CounterCache<1024>>` | 每个 Publisher 独立维护序号窗口 |

这里最重要的不是记住容器名字，而是看到：**数据结构本身就是运行时协议的一部分。**

例如 SubGate 如果使用普通 `unordered_map<string, Subscriber>`，第二个同 topic Subscriber 会覆盖第一个；如果使用 `vector<pair<string, Subscriber>>`，每条消息都必须线性扫全部订阅者。固定实现选择 `unordered_multimap`，正好编码“一键多值 + `equal_range`”。

## 文件一：wire_codec.h —— 借用 buffer 与业务所有权之间的边界

~~~cpp
#pragma once

#include <charconv>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace atlas::ecal_demo {

struct Frame {
  std::uint64_t sequence{};
  std::int64_t timestamp_us{};
  std::string payload;
};

inline std::string Encode(const Frame& frame) {
  std::string wire;
  wire.reserve(48 + frame.payload.size());
  wire += std::to_string(frame.sequence);
  wire.push_back('|');
  wire += std::to_string(frame.timestamp_us);
  wire.push_back('|');
  wire += frame.payload;
  return wire;
}

inline std::optional<Frame> Decode(std::string_view wire) {
  const std::size_t first = wire.find('|');
  if (first == std::string_view::npos) {
    return std::nullopt;
  }
  const std::size_t second = wire.find('|', first + 1);
  if (second == std::string_view::npos) {
    return std::nullopt;
  }

  Frame frame;
  const auto sequence_text = wire.substr(0, first);
  const auto timestamp_text = wire.substr(first + 1, second - first - 1);

  const char* seq_begin = sequence_text.data();
  const char* seq_end = seq_begin + sequence_text.size();
  const auto seq_result =
      std::from_chars(seq_begin, seq_end, frame.sequence);
  if (seq_result.ec != std::errc{} || seq_result.ptr != seq_end) {
    return std::nullopt;
  }

  const char* ts_begin = timestamp_text.data();
  const char* ts_end = ts_begin + timestamp_text.size();
  const auto ts_result =
      std::from_chars(ts_begin, ts_end, frame.timestamp_us);
  if (ts_result.ec != std::errc{} || ts_result.ptr != ts_end) {
    return std::nullopt;
  }

  // The incoming string_view may point into eCAL callback memory. Copy the
  // payload so Frame owns everything after Decode returns.
  frame.payload.assign(wire.substr(second + 1));
  return frame;
}

}  // namespace atlas::ecal_demo
~~~

`std::string_view` 只保存地址和长度，不拥有底层字节。eCAL callback 的 `SReceiveCallbackData::buffer` 本质上是中间件在本次回调期间借给业务看的内存，所以不能把指向它的 `string_view` 直接放进后台线程。

`Decode()` 最终通过 `frame.payload.assign(...)` 把业务需要保留的内容复制进 `std::string`，把所有权关系变成：

~~~text
eCAL borrowed bytes
      |
      | Decode
      v
business-owned Frame
~~~

这里故意使用 `std::from_chars`，而不是 `std::stringstream`。协议只有两个整数和一段 payload，`from_chars` 可以直接在字符区间上解析，不引入 locale，也不用为了整数解析再分配临时字符串。

`std::optional<Frame>` 则把“解析是否成功”编码进类型。否则一个默认构造的 `Frame{}` 会让调用方无法区分合法 `sequence=0` 与解析失败。

## 文件二：wire_codec_test.cpp —— 先把协议从中间件剥离出来测试

~~~cpp
#include "wire_codec.h"

#include <cassert>
#include <string>

int main() {
  using atlas::ecal_demo::Decode;
  using atlas::ecal_demo::Encode;
  using atlas::ecal_demo::Frame;

  const Frame original{42, 123456789, "processed:raw"};
  const std::string wire = Encode(original);
  const auto decoded = Decode(wire);

  assert(decoded.has_value());
  assert(decoded->sequence == original.sequence);
  assert(decoded->timestamp_us == original.timestamp_us);
  assert(decoded->payload == original.payload);
  assert(!Decode("missing-separators").has_value());
  assert(!Decode("abc|123|raw").has_value());
  assert(!Decode("1|xyz|raw").has_value());
}
~~~

这份测试不依赖 eCAL。它已经在本机用 `-std=c++17 -Wall -Wextra -Werror -pedantic` 编译运行通过。它的价值是先证明：

~~~text
Frame -> Encode -> bytes -> Decode -> Frame
~~~

以及非法格式能够稳定拒绝。以后真实 eCAL 闭环失败时，就可以先把“codec 错误”和“discovery/transport/callback 错误”分开。

## 文件三：CMakeLists.txt —— 构建图也应该表达共享依赖

~~~cmake
cmake_minimum_required(VERSION 3.16)
project(atlas_ecal_closed_loop LANGUAGES CXX)

find_package(eCAL REQUIRED)

add_library(atlas_wire_codec INTERFACE)
target_include_directories(atlas_wire_codec INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}>)
target_compile_features(atlas_wire_codec INTERFACE cxx_std_17)

add_executable(atlas_wire_codec_test wire_codec_test.cpp)
target_link_libraries(atlas_wire_codec_test PRIVATE atlas_wire_codec)

add_executable(atlas_ecal_source source.cpp)
add_executable(atlas_ecal_relay relay.cpp)
add_executable(atlas_ecal_observer observer.cpp)

foreach(target IN ITEMS atlas_ecal_source atlas_ecal_relay atlas_ecal_observer)
  target_compile_features(${target} PRIVATE cxx_std_17)
  target_link_libraries(${target} PRIVATE eCAL::core atlas_wire_codec)
endforeach()
~~~

`atlas_wire_codec` 是 INTERFACE library，因为它只有 header，没有独立 translation unit。INTERFACE target 把 include path 和 C++17 要求统一传播给使用者，避免三个 executable 各写一遍同样配置。

构建图是：

~~~text
wire_codec.h
   |
   +--> atlas_wire_codec_test
   |
   +--> atlas_ecal_source   --+
   +--> atlas_ecal_relay     +--> eCAL::core
   +--> atlas_ecal_observer --+
~~~

还有一个容易忽略的 CMake 原则：必须先 `add_executable(target)`，再调用 `target_compile_features()` / `target_link_libraries()`。target 还没有进入构建图时，属性没有附着对象。

## 文件四：source.cpp —— soft-state discovery 与数据发送不是同一个阶段

~~~cpp
#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/publisher.h>
#include <ecal/time.h>

#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>

namespace {

eCAL::SDataTypeInformation FrameType() {
  return {"atlas.ecal.Frame", "text", "seq|timestamp_us|payload"};
}

bool WaitForSubscriber(eCAL::CPublisher& publisher,
                       std::chrono::milliseconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (eCAL::Ok() && std::chrono::steady_clock::now() < deadline) {
    if (publisher.GetSubscriberCount() > 0) {
      return true;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  return false;
}

}  // namespace

int main() {
  if (!eCAL::Initialize("atlas_ecal_source")) {
    return 1;
  }

  int result = 0;
  {
    eCAL::CPublisher publisher("/atlas/raw", FrameType());
    if (!WaitForSubscriber(publisher, std::chrono::seconds(5))) {
      std::cerr << "no relay subscriber discovered\n";
      result = 2;
    } else {
      for (std::uint64_t sequence = 0; sequence < 20 && eCAL::Ok();
           ++sequence) {
        atlas::ecal_demo::Frame frame;
        frame.sequence = sequence;
        frame.timestamp_us = eCAL::Time::GetMicroSeconds();
        frame.payload = "raw";

        const std::string wire = atlas::ecal_demo::Encode(frame);
        if (!publisher.Send(wire)) {
          std::cerr << "send failed at seq=" << sequence << '\n';
          result = 3;
          break;
        }
        std::cout << "source seq=" << sequence << '\n';
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }
    }
  }

  eCAL::Finalize();
  return result;
}
~~~

固定提交的主 API 是 `eCAL::Initialize("atlas_ecal_source")`，而不是早期版本常见的 `Initialize(argc, argv, ...)`。本专题后续示例统一按固定提交头文件写。

source 在发送前等待 `publisher.GetSubscriberCount() > 0`。这只能说明 registration 控制面已经收敛到“当前看见至少一个 Subscriber”，不是远端业务处理 ACK。

因此必须区分：

~~~text
registration 已发现 relay
    != Send 成功
    != relay callback 已执行
    != observer 已收到 processed
~~~

## 文件五：relay.cpp —— 第一版故意让 callback 直接 Send

~~~cpp
#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/publisher.h>
#include <ecal/pubsub/subscriber.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <string>
#include <string_view>
#include <thread>

namespace {

eCAL::SDataTypeInformation FrameType() {
  return {"atlas.ecal.Frame", "text", "seq|timestamp_us|payload"};
}

bool WaitForSubscriber(eCAL::CPublisher& publisher,
                       std::chrono::milliseconds timeout) {
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (eCAL::Ok() && std::chrono::steady_clock::now() < deadline) {
    if (publisher.GetSubscriberCount() > 0) {
      return true;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  return false;
}

}  // namespace

int main() {
  if (!eCAL::Initialize("atlas_ecal_relay")) {
    return 1;
  }

  int result = 0;
  {
    eCAL::CPublisher processed("/atlas/processed", FrameType());
    if (!WaitForSubscriber(processed, std::chrono::seconds(5))) {
      std::cerr << "no observer subscriber discovered\n";
      result = 2;
    } else {
      std::atomic<int> forwarded{0};
      std::atomic<int> invalid{0};
      std::atomic<int> send_failures{0};

      eCAL::CSubscriber raw("/atlas/raw", FrameType());
      raw.SetReceiveCallback(
          [&](const eCAL::STopicId&, const eCAL::SDataTypeInformation&,
              const eCAL::SReceiveCallbackData& data) {
            const auto* bytes = static_cast<const char*>(data.buffer);
            const std::string_view wire(bytes, data.buffer_size);
            auto frame = atlas::ecal_demo::Decode(wire);
            if (!frame) {
              invalid.fetch_add(1);
              return;
            }

            frame->payload = "processed:" + frame->payload;
            const std::string output = atlas::ecal_demo::Encode(*frame);
            if (!processed.Send(output)) {
              send_failures.fetch_add(1);
              return;
            }
            forwarded.fetch_add(1);
          });

      const auto deadline =
          std::chrono::steady_clock::now() + std::chrono::seconds(8);
      while (eCAL::Ok() && forwarded.load() < 20 &&
             std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }

      raw.RemoveReceiveCallback();
      std::cout << "forwarded=" << forwarded.load()
                << " invalid=" << invalid.load()
                << " send_failures=" << send_failures.load() << '\n';

      if (invalid.load() != 0 || send_failures.load() != 0) {
        result = 3;
      } else if (forwarded.load() != 20) {
        result = 4;
      }
    }
  }

  eCAL::Finalize();
  return result;
}
~~~

relay 先创建下游 Publisher，并等待 observer 被发现，再创建上游 Subscriber。这样 source 看见 relay 时，relay 的下游也已经就绪，减少启动时序对正常链路测试的干扰。

callback 中的 `std::string_view` 不复制消息，只建立借用视图；`Decode()` 再把真正要长期保存的 payload 转成拥有型 `std::string`。

### 为什么 forwarded / invalid / send_failures 用 atomic？

Subscriber callback 与 main 线程是不同执行上下文：callback 写计数，main 线程轮询计数决定何时退出。普通 `int` 在一个线程写、另一个线程读且没有同步时构成 C++ data race。

这里每个统计量只要求独立原子更新，所以 `std::atomic<int>` 足够。若以后要求多个字段共同满足一个事务不变量，例如 `forwarded + invalid + pending == total`，多个独立 atomic 就不能自动保证快照一致，应改用同一个 mutex 保护状态对象。

### 为什么这一版故意在 callback 里调用 processed.Send？

因为它可以把固定实现的线程耦合直接暴露出来。`CSubscriberImpl::ApplySample()` 一进入就持有 `m_receive_callback_mutex`，并一直到用户 callback 返回才释放。

因此当前 relay 实际形成：

~~~text
ApplySample
  lock m_receive_callback_mutex
    -> Decode
    -> transform
    -> processed.Send
  unlock
~~~

如果下游 `Send()` 因 SHM remap、ACK 等待、序列化或其他 transport 工作阻塞 20 ms，同一个 Subscriber 的下一条 `ApplySample()` 也无法进入 callback。

这就是为什么“callback 要短”不是风格建议，而是锁域直接推出来的约束。

## 第二版 relay 应该用什么容器？

不能笼统回答 `std::queue`。

### 状态流：latest-slot

只关心最新状态时，可以用：

~~~cpp
std::mutex mutex;
std::optional<Frame> latest;
std::uint64_t version = 0;
~~~

它的内存复杂度是 `O(S)`，消费者落后时旧状态被新状态覆盖。姿态、温度、最新感知结果通常更适合这种语义。

### 必须逐条处理的事件：bounded deque / ring

可以从 `std::deque<Frame>` 开始，但必须额外定义容量以及满时行为：丢最旧、拒绝最新、阻塞 producer，还是报告 overload。

`std::queue` 只是 adaptor，本身没有容量策略。长期生产率大于消费率时，无界 queue 只是把实时性问题转换成内存增长问题。

### 高频固定上限：预分配 array ring

如果需要减少稳态节点分配，可以继续演进成：

~~~text
std::array<Slot, N>
head
tail
sequence
~~~

但这会把覆盖、并发、ABA、对象析构和 backpressure 责任全部交给自己。STL 选择不是“哪个大 O 更漂亮”，而是哪个结构能最直接表达业务数据语义。

## 文件六：observer.cpp —— 最终成功必须由第三个进程确认

~~~cpp
#include "wire_codec.h"

#include <ecal/ecal.h>
#include <ecal/pubsub/subscriber.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <thread>

namespace {

eCAL::SDataTypeInformation FrameType() {
  return {"atlas.ecal.Frame", "text", "seq|timestamp_us|payload"};
}

}  // namespace

int main() {
  if (!eCAL::Initialize("atlas_ecal_observer")) {
    return 1;
  }

  int result = 0;
  {
    std::atomic<int> received{0};
    std::atomic<int> invalid{0};
    std::atomic<int> gaps{0};
    std::atomic<std::int64_t> last_sequence{-1};

    eCAL::CSubscriber subscriber("/atlas/processed", FrameType());
    subscriber.SetReceiveCallback(
        [&](const eCAL::STopicId&, const eCAL::SDataTypeInformation&,
            const eCAL::SReceiveCallbackData& data) {
          const auto* bytes = static_cast<const char*>(data.buffer);
          const std::string_view wire(bytes, data.buffer_size);
          const auto frame = atlas::ecal_demo::Decode(wire);
          if (!frame || frame->payload != "processed:raw") {
            invalid.fetch_add(1);
            return;
          }

          const auto sequence = static_cast<std::int64_t>(frame->sequence);
          const auto previous = last_sequence.exchange(sequence);
          if (previous >= 0 && sequence != previous + 1) {
            gaps.fetch_add(1);
          }
          received.fetch_add(1);
          std::cout << "observer seq=" << sequence << '\n';
        });

    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (eCAL::Ok() && received.load() < 20 &&
           std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    subscriber.RemoveReceiveCallback();
    std::cout << "received=" << received.load()
              << " gaps=" << gaps.load()
              << " invalid=" << invalid.load() << '\n';

    if (invalid.load() != 0) {
      result = 2;
    } else if (received.load() != 20 || gaps.load() != 0) {
      result = 3;
    }
  }

  eCAL::Finalize();
  return result;
}
~~~

`last_sequence.exchange(sequence)` 原子地取得旧 sequence 并写入新值，然后检查 gap。最终要求 `received == 20 && gaps == 0 && invalid == 0`。

它只能证明**这一轮业务观测闭环**完整，不能推出 transport 具有可靠交付或 deadline 保证。

## SubGate：为什么是 unordered_multimap，再临时复制 vector？

固定源码定义：

~~~cpp
using TopicNameSubscriberMapT =
    std::unordered_multimap<std::string,
                            std::shared_ptr<CSubscriberImpl>>;
~~~

消息到达后，SubGate 在 `shared_timed_mutex` 的 shared lock 下通过 `equal_range(topic)` 找到同 topic 的多个 Subscriber，但不会拿着 Gate 锁直接调用它们。

它先复制出：

~~~cpp
std::vector<std::shared_ptr<CSubscriberImpl>> readers_to_apply;
~~~

随后释放 registry lock，再逐个 `ApplySample()`。

这个设计同时解决了三件事：

- registry 允许并发读；
- 任意业务 callback 不直接处于 Gate registry 锁域内；
- copied `shared_ptr` 保证本轮分发期间对象不会因为并发 unregister 立刻析构。

代价是临时 vector 与 shared_ptr 引用计数更新。这里的选择是“分发期间对象寿命可证明”优先于少几次原子 refcount。

## Publisher / Subscriber connection table：为什么是 std::map？

两个实体都维护 `std::map<identity, SConnection>`。这是控制面连接状态，不是每帧 payload 热路径。

记录中包含 remote identity、datatype、layer states、selected/established state。这里更重要的是稳定身份、可遍历统计和更新删除，而不是机械追求 hash 的平均 `O(1)`。

## SHM Writer：vector 本身就是固定槽 ring

固定结构是：

~~~cpp
std::vector<std::shared_ptr<CSyncMemoryFile>> m_memory_file_vec;
size_t m_write_idx = 0;
~~~

每次通过 `m_write_idx % m_memory_file_vec.size()` 选槽，写完再递增取模。固定槽位 + 整数索引非常适合 vector。

若换成 linked list，不但没有按下标访问优势，还会增加节点指针追踪和 cache miss。

## map<process_id, set<entity_id>>：为什么不能 endpoint 一注销就断开进程？

同一个 OS 进程可以创建多个 Subscriber endpoint。SHM Writer 因此维护：

~~~text
process_id
   |
   +--> entity A
   +--> entity B
   +--> entity C
~~~

移除 A 时不能立刻 `Disconnect(process_id)`。只有这个 process 对应的 `set<EntityIdT>` 变空，才说明该进程再没有 endpoint 使用这些 memory files。

这相当于在 endpoint 生命周期和 OS 进程级共享资源之间增加了一层引用集合。

## Registration：为什么 CExpirationMap 是 map + list？

`CExpirationMap` 内部同时维护：

~~~text
map<Key, {value, iterator_into_list}>
list<{timestamp, key}>
~~~

`map` 负责身份查找，`list` 维护最近更新时间顺序。刷新一个 endpoint 时，`list::splice` 可以 O(1) 把已有节点移到末尾并更新 timestamp。

超时检查从 list 头开始，只扫描真正已经过期的前缀；遇到第一个未过期项即可停止。这样避免每个 timeout tick 都对整张连接表重新计算年龄。

## CExpandingVector：用常驻内存换周期 allocator 抖动

Registration Provider 周期性重新组装 Process、Publisher、Subscriber、Service 等 Sample 列表。

`CExpandingVector<Sample>` 保留底层 `std::vector` 已构造的槽位，`clear()` 时调用各元素自己的 `clear()`，下一轮 push 优先复用旧槽。

收益是减少周期性构造、析构和内部 string/vector 分配；代价是历史峰值容量会常驻，并且这个自定义容器本身不是线程安全的。

这体现了准实时运行时常见取舍：不一定追求最低常驻内存，而是追求更稳定的稳态分配行为。

## blocking Read 为什么只是 string mailbox？

固定 Subscriber 的同步 Read 使用：

~~~text
std::string m_read_buf
std::mutex m_read_buf_mutex
std::condition_variable m_read_buf_cv
bool m_read_buf_received
~~~

没有 receive callback 时，`ApplySample()` 会覆盖 `m_read_buf` 为最新 payload，再 `notify_one()`。

所以它是 latest-value mailbox，不是历史 FIFO。消费者慢时，中间样本可以被下一条覆盖。若业务要求每条命令都执行，不能把这个 Read 接口误解成可靠消息队列。

## Linux SHM：最终落到哪些系统调用？

固定源码直接使用：

~~~text
shm_open
fstat
ftruncate
mmap(MAP_SHARED)
flock
munmap
shm_unlink
close
~~~

而且定义 move-only RAII `Fd` 和 `FlockExclusive`：fd 在析构时 `close()`，文件锁在析构时 `LOCK_UN`。

RAII 管的不只是 heap，也包括 OS file descriptor 和跨进程锁。初始化阶段的 `flock(LOCK_EX)` 则用来避免其他进程看到半初始化共享区域。

## Windows SHM：同一抽象映射到另一组 kernel object

Windows 路径使用：

~~~text
CreateFileMapping
MapViewOfFile
UnmapViewOfFile
CloseHandle
~~~

Publisher/Subscriber 上层不应该知道 POSIX fd 和 Windows HANDLE 的差异；`CMemoryFile` 这一层就是 OS abstraction / Adapter 边界。

## zero-copy 的真正代价是 lease time

固定 SHM reader 在 zero-copy 路径里可以直接在打开的 memory file 上运行用户 callback，memory file 直到 callback 返回才释放。

因此少一次 memcpy 的代价是：

~~~text
lease time
= callback WCET
+ callback 内锁等待
+ callback 内 I/O
+ callback 内 downstream Send
~~~

Zero-copy 不是“拿到裸指针就免费”，真正要设计的是 buffer lease 谁结束、慢消费者最多允许占用多久。

## 固定版本的 callback-under-lock 警戒线

### Receive callback

`CSubscriberImpl::ApplySample()` 持有 `m_receive_callback_mutex` 执行用户 callback；`SetReceiveCallback()` 和 `RemoveReceiveCallback()` 也请求这把非递归 mutex。

因此用户 callback 内对同一个 Subscriber 自己调用 Set/Remove callback，会形成重入自死锁路径。

### Publisher / Subscriber event callback

`FireEvent()` 在 `m_event_id_callback_mutex` 内执行用户 event callback，而 Set/Remove event callback 使用同一把锁。

更进一步，固定代码在真正加锁前先读取 `std::function` 是否为空；如果另一个线程同时 Set/Remove callback，这个读写没有统一锁域。

### Registration callback registry

`CSampleApplier::ApplySample()` 持有 callback map mutex 遍历并执行所有 `std::function`。custom callback 如果修改同一 registry，也会请求同一把 mutex。

通用改进模式通常是：

~~~text
lock registry
  -> copy callback / owner snapshot
unlock
  -> invoke arbitrary user code
~~~

但如果 API 还要求“注销返回以后绝无已经复制出去的 callback”，就必须再设计 in-flight counter / quiescence barrier。简单把 callback 移到锁外并没有自动完成完整生命周期协议。

## 建议做的四个故障实验

### 1. relay callback sleep 200 ms

source 是 20 Hz，每 50 ms 一条；relay 服务率降到约 5 Hz。观察 callback 串行、数据年龄、丢失以及 transport 行为。

### 2. inline relay 改成 latest-slot

对比 callback WCET 和 end-to-end freshness，验证“状态流不需要保存每条历史”的结构收益。

### 3. payload 扩到数 MB

观察 SHM、UDP/TCP 同时激活后是否进入公共 `m_payload_buffer` staging。固定实现只有 SHM zero-copy 开启且 UDP/TCP 当前都不活跃时，才允许跳过这份 staging copy。

### 4. callback 内自移除

只在隔离测试进程里做，并配 watchdog / sanitizer。它是验证固定版本锁契约的并发实验，不是推荐业务写法。

## 构建与运行

安装了固定或兼容 eCAL CMake package 后：

~~~bash
cmake -S examples/ecal/closed_loop -B build/ecal-closed-loop
cmake --build build/ecal-closed-loop --parallel
~~~

先运行纯 C++ codec：

~~~bash
./build/ecal-closed-loop/atlas_wire_codec_test
~~~

再按顺序启动：

~~~text
1. atlas_ecal_observer
2. atlas_ecal_relay
3. atlas_ecal_source
~~~

正常成功条件：

~~~text
relay: forwarded=20 invalid=0 send_failures=0
observer: received=20 gaps=0 invalid=0
~~~

这仍然只是一次业务闭环，不是 transport reliability 或 realtime deadline 的证明。

## 从项目回到源码专题

| 工程问题 | 深挖页面 |
|---|---|
| 进程级 Initialize/Finalize | [全局生命周期](../../articles/ecal/global-lifecycle.md) |
| soft-state discovery | [Registration](../../articles/ecal/registration-soft-state.md) |
| transport 选择 | [Publisher 发送](../../articles/ecal/publisher-discovery-send.md) |
| callback / Read / 去重 | [Subscriber 交付](../../articles/ecal/subscriber-delivery.md) |
| SHM slot / ACK / zero-copy | [共享内存协议](../../articles/ecal/shm-memory-protocol.md) |
| RAII / shared_ptr / weak_ptr | [C++ 设计实验](../../articles/ecal/cpp-design-lab.md) |
| 全局对象与线程关系 | [架构骨架](../../articles/ecal/architecture-map.md) |
| 设计取舍总结 | [设计复盘](../../articles/ecal/design-recap.md) |

读完以后，对任何一个中间件对象都继续问：**为什么是这个容器？这把锁保护什么？谁拥有对象？当前在哪个线程？下一步是否跨进程或进入 OS kernel？**
