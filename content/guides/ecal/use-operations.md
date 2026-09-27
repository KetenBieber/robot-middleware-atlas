# eCAL 使用教程：Protobuf、录制回放与多机排查

生产系统应使用带 schema 的消息、可复现的 measurement 和分层诊断，而不是停留在 string demo。

## Protobuf 工程

```proto
syntax = "proto3";
package atlas.demo;
message State {
  uint64 sequence = 1;
  int64 timestamp_us = 2;
  repeated double joints = 3;
}
```

```cmake
find_package(eCAL REQUIRED)
find_package(Protobuf REQUIRED)

set(PROTO_FILES state.proto)
PROTOBUF_TARGET_CPP(atlas_state_proto ${CMAKE_CURRENT_BINARY_DIR} ${PROTO_FILES})

add_executable(state_sender state_sender.cpp)
target_link_libraries(state_sender PRIVATE
  eCAL::protobuf_core
  atlas_state_proto
)
```

官方 C++ setup 文档列出 `eCAL::protobuf_core` 和 `PROTOBUF_TARGET_CPP` 的集成方式。[eCAL C/C++ Setup](https://eclipse-ecal.github.io/ecal/v6.1/getting_started/howto/setup/cpp.html)

Publisher/Subscriber 使用生成的 `atlas::demo::State` 类型。新增字段使用新 field number，禁止复用已删除字段编号；发布与订阅方的 schema 版本应进入构建元数据。

## 生成类型在 C++ 中解决了什么

Protobuf 生成类同时提供字段访问、序列化和解析，但它不自动提供业务不变量。`joint_count` 不再需要手工维护，因为 repeated 字段自身保存长度；“关节数必须等于机器人型号”“时间戳必须递增”仍需要应用验证。

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
#include <ecal/ecal.h>
#include <ecal/msg/protobuf/publisher.h>
#include "state.pb.h"

int main(int argc, char** argv) {
  eCAL::Initialize(argc, argv, "state_sender");
  {
    eCAL::protobuf::CPublisher<atlas::demo::State> pub("atlas/state");
    std::uint64_t sequence = 0;
    while (eCAL::Ok()) {
      atlas::demo::State msg;
      msg.set_sequence(sequence++);
      msg.set_timestamp_us(system_time_us());
      for (double q : read_joints()) msg.add_joints(q);
      if (!pub.Send(msg)) ++send_failures;
      sleep_until_next_period();
    }
  }
  eCAL::Finalize();
}
```

循环内重新创建 protobuf 对象语义清晰，但 repeated 字段可能反复分配。高频路径可把消息放在循环外，通过 `clear_joints()` 重用容量，再填充固定最大关节数；优化前要用 allocator 指标确认分配确实是瓶颈。复用对象时必须重置每个非默认字段，否则上一周期值会泄漏到新消息。

订阅者先验证业务边界，再把不可变快照交给 worker：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
void on_state(const atlas::demo::State& msg, long long receive_us) {
  if (msg.joints_size() != expected_joint_count_) {
    ++schema_valid_but_business_invalid_;
    return;
  }
  if (have_sequence_ && msg.sequence() <= last_sequence_) {
    ++duplicate_or_reordered_;
  }
  const auto gap = have_sequence_
      ? msg.sequence() - last_sequence_ - 1 : 0;
  last_sequence_ = msg.sequence();
  have_sequence_ = true;

  StateSnapshot snapshot;
  snapshot.sequence = msg.sequence();
  snapshot.source_time_us = msg.timestamp_us();
  snapshot.receive_time_us = receive_us;
  snapshot.joints.assign(msg.joints().begin(), msg.joints().end());
  if (!worker_queue_.try_push(std::move(snapshot))) ++application_drops_;
}
```

这里的复制是有意的所有权转换：生成消息由 callback 调用上下文借用，worker 需要在 callback 返回后继续使用。若追求零复制，必须让底层 buffer lease 覆盖 worker 生命周期，并处理关闭与慢消费者；这比一次有限关节数组复制复杂得多。

## Schema 演化规则

| 修改 | Protobuf wire 层 | 业务风险 |
|---|---|---|
| 新增可选/普通字段 | 旧端通常忽略 | 新端必须为缺失字段定义默认语义 |
| 删除字段并 reserve 编号 | 可控 | 旧端仍可能发送该字段 |
| 复用旧 field number | 禁止 | 同一 wire tag 被解释成不同含义 |
| 改变单位但保留字段 | wire 可解析 | 最危险，数值看似正常但语义错误 |
| 改 repeated/单值或不兼容类型 | 可能解析异常 | 应使用新字段或新消息版本 |

因此兼容测试不能只检查 `ParseFromArray` 成功，还要用旧版本 measurement 驱动新消费者，并验证单位、默认值、边界和业务输出。

## 数据年龄与丢帧

消息中保留 `sequence` 与发送时间。订阅端同时计算：

```text
gap = current.sequence - previous.sequence - 1
age = receive_clock - message.timestamp
```

gap 表示应用观察到的缺口，age 表示端到端新鲜度。跨主机计算 age 前需要时钟同步；否则只比较单调序列和本机接收间隔。

## 录制与回放

用 eCAL Recorder 选择关键 topic 生成 measurement；Player 回放时固定速率与循环设置。回归流程为：

```text
record real input
  -> freeze measurement + schema + config
  -> start consumer build A, replay, collect outputs
  -> start consumer build B, replay, compare sequence/latency/results
```

measurement 文件不是完整环境快照，还需保存 eCAL 配置、应用版本、schema commit 和播放参数。

## 把回放变成可比较实验

每个回放实验保存一个 manifest：

```yaml
measurement: run_042
schema_commit: 8f31c2a
ecal_config: configs/lab.ini
player_rate: 1.0
loop: false
consumer_build: estimator-2.4.1
expected_topics:
  - atlas/state
```

消费者输出也应记录 sequence 与输入 event id。比较 build A/B 时，先对齐输入身份，再比较业务值；按墙上时钟逐行比较会把调度抖动误当算法差异。

回放速度提高到 2× 或 10× 可以测试过载策略，但不能直接推断实时延迟，因为 Player 的调度、突发行为和原始采集节拍都会改变。应分别报告功能确定性与时序性能。

## 多机问题按平面拆分

```text
registration/discovery plane: 双方是否互相看见实体
transport selection plane: 实际选择 SHM/UDP/TCP 中哪条路径
data plane: bytes 是否到达且能反序列化
application plane: callback 是否及时处理
```

先用 monitor 看 registration，再用小 payload 排除 MTU/大块传输，再检查类型描述与 callback。不要在实体都不可见时先调 subscriber 业务代码。

### 一个可复用的诊断决策表

| Monitor 看到发布者 | 看到订阅者 | 连接/频率 | 下一步 |
|---|---|---|---|
| 否 | 否 | 无 | 初始化、配置域、进程存活 |
| 是 | 否 | 无 | 订阅者 topic/类型/配置 |
| 是 | 是 | 无数据 | transport 选层、发送返回、网络/SHM 资源 |
| 是 | 是 | 有频率但业务无输出 | callback、反序列化、应用队列 |
| 是 | 是 | 频率正常但 age 增长 | worker 过载、时钟同步、旧数据积压 |

同机问题可强制或观察 SHM 路径，再用网络 transport 做对照；跨机先用小 string topic 验证 discovery 和防火墙，再恢复 protobuf 与大 payload。每次只改变一个平面，才能定位失败来自发现、传输还是业务。

## 工程配置原则

- 同主机大数据优先验证 SHM，跨主机明确网络 transport；
- topic 命名包含稳定域，不把临时 hostname 写入业务名字；
- callback 到 worker 的队列设置容量、drop 指标和告警；
- Recorder 按时间/大小轮转并监控磁盘；
- 关闭时先停止 publisher，再停止 subscriber worker，最后 `Finalize`；
- 升级 eCAL 时用跨版本 sender/receiver 与 measurement 做兼容测试。

## 验收清单

- C++ 与至少一个其他语言客户端能按同一 protobuf schema 互通；
- subscriber 重启后能重新发现 publisher；
- 单个慢 subscriber 不造成其他 subscriber 无指标地停顿；
- 多机断网与恢复行为被记录，陈旧 registration 能过期；
- measurement 回放产生确定的业务输出；
- shutdown 时没有 callback 访问已析构队列或 logger。

## 从实验到部署的最低证据

交付包至少包含：解析后的 eCAL 配置、topic/type 清单、schema 版本、每条关键 topic 的频率与最大 payload、队列容量与丢弃策略、一次正常 measurement、一次断网恢复时间线，以及关闭时序。这样下一位工程师可以重新建立系统，而不需要从“Monitor 里看起来有数据”反推隐含配置。
