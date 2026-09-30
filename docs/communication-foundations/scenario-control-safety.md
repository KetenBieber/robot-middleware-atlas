# 场景设计三：控制命令、模式切换与 Emergency Stop，为什么不能和普通消息混成一条 Queue

## 场景

机器人控制程序同时收到：

~~~text
1 kHz state feedback
100 Hz command setpoint
偶发 mode change
diagnostic event
Emergency Stop
logs
~~~

它们看起来都可以叫“message”。

但业务 criticality、顺序语义、deadline 完全不同。

---
## 先把 Safety 场景里的几个词分开

### Criticality

Criticality 表示一条工作失败、延迟或丢失以后，后果有多严重。debug log 丢一条和 Emergency Stop 延迟 200 ms，不是同一等级的问题。

### Deadline

Deadline 不是“希望尽快”，而是：**某条工作在什么时间以后即使完成，也已经失去正确性。**

例如命令 created_at=10.000 s、deadline=10.020 s；如果 10.100 s 才执行，可靠执行旧命令可能反而更危险。

### Priority

Priority 只表达多个可运行工作之间谁先获得执行机会。它不自动保证 deadline。

### Preemption

Preemption 指当前正在 CPU 上运行的工作，是否可以被更高优先级线程中断并让出 CPU。

所以：

~~~text
priority queue
!=
preemptive scheduler
~~~

Queue priority 只能决定“下一项拿谁”，通常不能中断已经开始执行的长 callback。

---

## 为什么“一条高优先级 Queue”仍可能不够

假设普通 Task A 正在执行 50 ms，而 ESTOP 在第 1 ms 到达。即使 ESTOP 已经排到 priority queue 最前面，Worker 仍要等 A 返回后才能重新取 Queue。

因此安全路径必须同时看：

~~~text
queue ordering
callback WCET
OS thread priority
preemption
shared locks
~~~

`WCET` 是 Worst-Case Execution Time，即一段代码在最坏情况下可能执行多久。实时设计关心的通常不是平均 1 ms，而是“最坏会不会偶尔跑到 30 ms”。

---

## Priority Inversion 是怎样一步步发生的

设 Logger Thread priority=10，Safety Thread priority=90。

Logger 先持有 mutex；Safety 随后想拿同一把 mutex，于是高优先级 Safety 被低优先级 Logger 阻塞。

如果中间还有 priority=50 的 Medium Thread 持续 runnable，它可能不断抢占 Logger，使 Logger 更迟才能运行到 unlock。

~~~text
High waits Low
Medium prevents Low from running
~~~

这就是经典 priority inversion。

### Priority Inheritance

一种策略是：高优先级线程等待低优先级 mutex owner 时，临时提升 owner 的调度优先级，让它尽快运行到 unlock。

### Priority Ceiling

另一类实时协议是为共享资源规定优先级上限，线程进入临界区时按规则提升优先级，限制不可控的优先级反转。

但工程里更根本的办法往往是：**Safety path 尽量不要和 Logger 共享这把锁。**

---

## 一个最小“安全状态 + 普通事件”结构

下面故意把 persistent safety state 与普通 event queue 分开。

~~~cpp
#include <atomic>
#include <mutex>
#include <queue>
#include <string>

class SupervisorInput {
public:
    void assert_estop() {
        estop_active_.store(true, std::memory_order_release);
    }

    bool estop_active() const {
        return estop_active_.load(std::memory_order_acquire);
    }

    void push_event(std::string e) {
        std::lock_guard<std::mutex> lock(m_);
        events_.push(std::move(e));
    }

    bool try_pop_event(std::string& out) {
        std::lock_guard<std::mutex> lock(m_);
        if (events_.empty()) {
            return false;
        }
        out = std::move(events_.front());
        events_.pop();
        return true;
    }

private:
    std::atomic<bool> estop_active_{false};
    std::mutex m_;
    std::queue<std::string> events_;
};
~~~

这里 ESTOP 没有进入普通 FIFO。Safety Thread 可以每个周期直接读取持久状态 `estop_active`。

即使某个线程错过了一次“ESTOP asserted”边沿通知，也不会因此把系统误认为安全。

这就是：

~~~text
edge/event
+
persistent truth
~~~

组合的意义。

---


## 第一步：把 Command 分成两类

### State-like Command

例如：

~~~text
target velocity = 0.5 m/s
target yaw rate = 0.2 rad/s
~~~

如果 10 ms 后来了新 setpoint，旧 setpoint 通常已经没有独立价值。

它更像 latest desired state。

### Event-like Command

例如：

~~~text
ENABLE
CALIBRATE
RESET_FAULT
CHANGE_MODE
~~~

顺序和是否执行可能非常重要。

因此 command channel 也不能一律 FIFO。

---

## Naive 方案：所有消息进一个 MPMC Queue

~~~text
camera event
log event
planner event
mode command
emergency stop
        ↓
one global queue
        ↓
control/supervisor
~~~

问题：

~~~text
普通 backlog
可能排在 emergency stop 前面
~~~

即使 queue 是 lock-free，也没有解决业务优先级。

这说明：

> **并发数据结构正确 ≠ 安全语义正确。**

---

## Priority Queue 能否解决

可以给 Emergency Stop 高优先级。

但还要问：

~~~text
Consumer 当前是否正在执行一个长任务？
Queue priority 能否抢占正在运行的 callback？
高优先级 item 是否会被 mutex owner 阻塞？
~~~

普通 priority_queue 只决定“下一项取谁”，不能抢占当前正在执行的 code。

这和 ROS Executor / Holoscan worker 的 priority 问题一样。

---

## 更强的方案：Dedicated Safety Path

关键安全信号可以独立于普通业务队列：

~~~text
Emergency source
      ↓
dedicated atomic/event/channel
      ↓
safety supervisor / control thread
~~~

普通命令：

~~~text
Planner/UI
   ↓
bounded command queue/mailbox
~~~

日志/诊断再走第三条支路。

优点是普通 backlog 不会遮挡 safety path。

这是一种 topology isolation。

---

## Emergency Stop 应该是 Event 还是 State

很多系统最好把它同时建模成两层：

~~~text
edge/event:
ESTOP asserted

persistent state:
safety_state = ESTOP_ACTIVE
~~~

event 用来触发即时处理；persistent state 防止 Consumer 因错过一次边沿就认为安全状态已解除。

这是 control system 中常见的：

> **瞬时通知 + 持久状态真值。**

---

## Setpoint 为什么通常更适合 Latest Mailbox

如果 command producer 100 Hz、controller 1 kHz，Controller 每周期读取最新 desired state 即可。

FIFO 保存所有 100 Hz setpoint 反而可能导致：

~~~text
controller 处理历史 setpoint
→ command lag
~~~

所以：

~~~text
desired trajectory segment
可能需要 ring/window

instant velocity target
更像 latest state
~~~

仍然要按语义分。

---

## Deadline 必须成为 Command 的一部分

命令结构可以包含：

~~~cpp
struct Command {
    Sequence seq;
    TimePoint created_at;
    TimePoint deadline;
    CommandPayload payload;
};
~~~

Consumer 取出后先判断：

~~~text
now > deadline ?
~~~

如果已过期，再“可靠地执行”反而可能更危险。

实时系统中：

> **可靠传达旧命令，不一定比丢弃旧命令更正确。**

---

## Sequence 解决什么

如果网络重试/多来源可能造成重复或乱序：

~~~text
seq 101
seq 102
seq 101 retry arrives late
~~~

Consumer 可以识别 stale command。

对于 latest-state command，还可以直接只接受更大的 generation/sequence。

---

## OS Priority 为什么必须和 Channel Design 一起看

假设 safety thread 用 SCHED_FIFO 90。

但它需要获取一把 mutex，而 mutex 被低优先级 logger thread 持有：

~~~text
Safety high priority
↓ waits mutex
Logger low priority
↓ 没有 CPU
~~~

形成 priority inversion。

因此 hard/firm RT 路径应尽量避免：

- 与非关键线程共享长临界区；
- 动态 allocation；
- 阻塞 I/O；
- 不受控 logging；
- 大对象拷贝。

如果必须共享锁，要考虑 priority inheritance/ceiling 或重新划分 ownership。

---

## 工业案例：EtherCAT Process Image

SOEM/IgH 的周期控制不是每个 Slave 想发就发。

典型结构：

~~~text
each slave stages PDO
↓
contiguous process image
↓
one cyclic exchange
↓
validate WKC
↓
publish new device state
~~~

这个设计把设备级局部状态 staging 与总线周期 commit 分开。

WKC 还提供了一道“这一周期数据是否可信”的 validity gate。

详见： [SOEM 工程案例](../generated/soem/case-study-leggedrobotics-soem-interface.md)。

---

## 为什么 SDO/诊断不应和周期 PDO 热路径随便混锁

慢 SDO/diagnosis 如果和 1 kHz process data 共用粗 mutex：

~~~text
diagnostic holds lock
↓
cyclic thread waits
↓
deadline miss
~~~

所以工业系统常把：

~~~text
control/data plane
和
management/control plane
~~~

做线程、queue、lock domain 隔离。

---

## Watchdog 与 Timeout 是另一种安全通道

安全不只来自 Emergency Event。

还要检测：

~~~text
command age > threshold
state feedback missing
WKC invalid N cycles
controller heartbeat missing
~~~

这些都是“没有消息”本身构成事件。

所以 runtime 需要 timer/clock/watchdog，不只是 queue。

---

## 一个推荐拓扑

~~~text
Estimator
→ latest RobotState mailbox

Planner
→ latest Setpoint / bounded trajectory window

Mode/UI
→ bounded ordered command queue

E-Stop / Safety IO
→ dedicated safety state + event

Diagnostics
→ separate low-priority MPSC

Control thread
→ fixed CPU / RT priority / no blocking I/O
~~~

这比“统一 message bus”更接近控制系统真实需要。

---

## Shutdown 也属于安全语义

正常关闭应该：

~~~text
stop accepting non-safety commands
↓
drive actuator to safe state
↓
confirm/control cycle settlement
↓
stop cyclic communication
↓
release runtime resources
~~~

不是直接 kill thread。

---

## 设计检查表

~~~text
[ ] Command 是 latest state 还是 ordered event？
[ ] 是否有 deadline/sequence？
[ ] safety signal 是否与普通 backlog 隔离？
[ ] queue priority 是否只是“下一项优先”，有没有误以为可抢占？
[ ] RT thread 是否会等待低优先级 mutex？
[ ] 是否存在动态 allocation / blocking I/O？
[ ] feedback 丢失时 watchdog 如何进入安全状态？
[ ] shutdown 如何把执行器带到 safe state？
~~~

深入机制：

- [Queues & Backpressure](queues-backpressure.md)
- [Threads & Memory Order](threads-memory-order.md)
- [Thread Communication Lab](thread-dataflow-lab.md)
