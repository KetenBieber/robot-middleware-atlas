# 场景设计一：最新状态、事件流与历史——先别急着选 Queue

## 场景

机器人里最常见的数据流之一：

~~~text
Estimator 200 Hz
      ↓
Controller 1000 Hz
~~~

Controller 每个周期需要机器人当前状态。

第一反应很容易是：

~~~text
std::queue<State> q;
~~~

这里只是在写“第一反应中的候选容器”，不是一段需要编译的程序。在决定容器之前，必须先问业务语义。

## 先把几个词讲清楚：State、Event、History 不是“不同容器”，而是不同业务语义

在这个场景里最容易犯的错误，是一看到“线程 A 产生数据、线程 B 消费数据”就直接想到 Queue。

但 Queue 只是**数据结构**。在决定数据结构之前，先决定“数据到底代表什么”。

### State：描述“现在世界是什么样”

例如：当前姿态、当前速度、当前电池电压、当前模式、当前目标速度。

如果新状态来了，旧状态通常会迅速失去价值。Controller 在 110 ms 时更关心 105 ms 的速度，而不是要求先把 100 ms 的速度处理一遍。

> **State 的核心语义：Reader 关心一个一致、尽可能新的快照。**

### Event：描述“某件事发生过”

例如 ENABLE、START、STOP、FAULT_ACK、一次 ACK 到达。新 Event 到来以后，旧 Event 不一定失效；它的存在性和顺序本身可能就是业务语义。

### History：为了回放、统计和追溯保存序列

例如过去 10 秒轨迹、故障日志、传感器历史窗口。它的目标不是只读最新，而是保留时间范围内的序列。

~~~text
State   -> latest/snapshot
Event   -> ordered queue
History -> ring/log/timeseries
~~~

这三种语义可以共存在同一个系统里，不能用一个“万能 Queue”统一。

---

## Data Race、互斥和 Snapshot 是三个不同问题

### Data Race 是 C++ 语言层问题

一个线程写共享对象、另一个线程同时读，而没有同步，就可能形成 data race。`std::mutex` 首先解决的是：**同一时刻不要让多个线程无协议地访问同一份可变状态。**

### 但“没有 Race”不等于“拿到同一版本”

假设状态由 pose 和 velocity 组成，Writer 分两个临界区更新，Reader 也分两次读取，就可能得到：

~~~text
pose@100 + velocity@101
~~~

这没有 data race，却是业务级不一致快照。

所以要分清：

~~~text
mutex correctness
    = 不发生无同步并发访问

snapshot correctness
    = 一组字段来自同一个逻辑版本
~~~

---

## 一个可以直接运行的 Latest-State 小程序

先不要上 seqlock、double buffer。最容易理解且正确的版本，就是“一把 mutex 保护整个 State，一次性 copy”。

编译运行（Linux / macOS；Windows 使用支持标准线程库的 C++17 编译器即可）：

~~~text
g++ -std=c++17 -O2 -pthread latest_state_demo.cpp -o latest_state_demo
./latest_state_demo
~~~


~~~cpp
#include <chrono>
#include <iostream>
#include <mutex>
#include <thread>

struct State {
    int version = 0;
    double position = 0.0;
    double velocity = 0.0;
};

class LatestState {
public:
    void publish(State s) {
        std::lock_guard<std::mutex> lock(m_);
        state_ = s;
    }

    State read() {
        std::lock_guard<std::mutex> lock(m_);
        return state_;
    }

private:
    std::mutex m_;
    State state_;
};

int main() {
    LatestState latest;

    std::thread estimator([&] {
        for (int i = 1; i <= 5; ++i) {
            State s;
            s.version = i;
            s.position = i * 0.1;
            s.velocity = i * 1.0;
            latest.publish(s);
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    });

    std::thread controller([&] {
        for (int i = 0; i < 10; ++i) {
            State s = latest.read();
            std::cout << "version=" << s.version
                      << " position=" << s.position
                      << " velocity=" << s.velocity << "\n";
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
    });

    estimator.join();
    controller.join();
}
~~~

这里只存在一份 `LatestState latest`，里面也只有一份 mutex 和 State。Estimator 与 Controller 都通过引用访问它。

Controller 可能多次读到同一个 version，也可能直接从 version 1 读到 version 2；它**不要求每个 estimator update 都被恰好消费一次**。这就是 Latest State 与 FIFO Event Queue 最核心的区别。

---


## 何时需要比 mutex + copy 更复杂的方案

上面的 `LatestState` 已经把最基础的并发关系闭环了：一个对象、一把 mutex、一份 State，Writer 整体替换，Reader 整体复制。State 不大、频率和锁竞争可接受时，到这里完全可以停止，不要为了“高级”而主动换掉 mutex。

只有当 State 变大、Reader 变多、复制或锁竞争开始成为可测瓶颈，才需要继续比较下面这些方案。此时关注点从“能不能正确同步”升级为“怎样在保持一致快照的前提下降低 Reader/Writer 成本”。

---

## 候选方案空间

| 方案 | Reader 成本 | Writer 成本 | 历史 | 一致快照 | 适合 |
| --- | --- | --- | --- | --- | --- |
| mutex + copy | lock + copy | lock + copy | 否 | 可以 | 简单状态 |
| atomic scalar | 极低 | 极低 | 否 | 单字段 | flag/counter |
| double buffer | 指针/索引切换 | 写非当前 buffer | 否 | 可以 | 大 State |
| seqlock/version | 可能重试 | 单 writer 简单 | 否 | 可以 | 高频一写多读 |
| latest mailbox | 读最新 | 覆盖旧值 | 否 | 取决实现 | state stream |
| FIFO | pop | push | 是 | 单 item | event/history |

---

## Double Buffer 为什么自然

如果 State 很大：

~~~text
buffer A: Reader currently using
buffer B: Writer updating
~~~

Writer 完成以后发布 current index。

~~~text
write B completely
↓
release publish current=B
↓
Reader acquire current
↓
read B
~~~

这里的 release/acquire 是 C++ memory-order 术语：Writer 用 release 发布索引时，要求此前对 Buffer B 的写入不能被排到发布之后；Reader 用 acquire 读到这个新索引后，才能把 Buffer B 的这些写入当成已经可见。更完整的内存序推导见 [Threads & Memory Order](threads-memory-order.md)。

核心不是“两块内存”，而是：

> **写入中的对象与已发布对象物理分离。**

这样 Reader 不会看到半更新对象。

---

## Seqlock / Versioned Snapshot 什么时候更合适

一种典型模式：

~~~text
sequence = even
↓
Writer sequence++ → odd
↓
write fields
↓
sequence++ → even
~~~

Reader：

~~~text
read seq0
if odd → retry
copy state
read seq1
if seq0 != seq1 → retry
~~~

优点是 Reader 不阻塞 Writer。

适合：

~~~text
一个 Writer
很多 Reader
Writer 临界区短
Reader 可以偶尔重试
~~~

不适合：

~~~text
Writer 很慢
Reader 必须固定步骤完成
多 Writer 很复杂
~~~

---

## 为什么 FIFO 在 Latest State 场景可能是错误设计

Estimator 200 Hz，Controller 100 Hz。

无界 FIFO：

~~~text
每秒生产 200
每秒消费 100
→ backlog +100/s
~~~

10 秒以后 Controller 可能处理几秒前的状态。

程序没有丢包，也没有崩，但控制语义已经错误。

这就是 Data Age 比 queue loss 更重要的典型场景。

---

## 工业案例：Apollo Planning

Apollo Planning 的一些辅助输入不是“每条都必须触发一次 Planning”，而更像下一次规划要读取的 latest state。

因此工程使用 component-owned state + mutex，再在 Proc() 里构造 LocalView。

这个案例最值得学习的是：

~~~text
event trigger
和
latest auxiliary state
可以在同一个 Component 里使用不同通信语义
~~~

不要为了统一框架把所有输入都塞进同一种 queue。

详见： [工程案例页](industry-runtime-cases.md)。

---

## 工业案例：Holoscan AsyncBuffer

Holoscan/GXF 的 AsyncBuffer 使用 four-slot/latest-style 异步交换思想，目标不是可靠保存每条历史，而是让两个执行流尽量独立、读取一致已发布值。

这和普通 FIFO connector 是不同语义。

详见： [Condition、Connector 与 Backpressure](../generated/holoscan/conditions-connectors-backpressure.md)。

---

## 一个机器人 Runtime 的合理组合

~~~text
IMU samples
→ SPSC ordered ring

Estimated RobotState
→ latest/versioned snapshot

Mode Change / Fault Event
→ bounded MPSC event queue

Logs
→ MPSC batch queue
~~~

同一个程序同时存在四种数据结构完全正常。

---

## 什么时候应该换方案

如果出现以下变化，就重新选型：

~~~text
“每次更新都必须处理”
→ latest state 改 event/history queue

“Reader 数量暴增”
→ mutex 可能改 versioned/RCU-style

“State 变得很大”
→ copy snapshot 改 double buffer/handle

“需要跨进程”
→ 裸 pointer 改 SHM descriptor/generation
~~~

这里的 RCU-style 指“Reader 尽量只读一个已发布版本，Writer 发布新版本后不立刻销毁旧版本，而是等旧 Reader 都退出后再回收”。它不是本页必须掌握的实现细节，只是说明“多 Reader + 读多写少”时还有另一类 ownership 方案。

---

## 设计检查表

在写第一行容器代码前回答：

~~~text
[ ] 数据是 State / Event / History 哪一种？
[ ] 旧值在新值出现后还有意义吗？
[ ] 每条更新必须处理吗？
[ ] 是否需要跨字段一致快照？
[ ] Producer/Consumer 数量？
[ ] Reader 可以重试吗？
[ ] 对象 copy 成本多大？
[ ] 最大允许 Data Age？
~~~

深入机制：

- [Ownership & Address Space](ownership-address-space.md)
- [Threads & Memory Order](threads-memory-order.md)
- [Queues & Backpressure](queues-backpressure.md)
