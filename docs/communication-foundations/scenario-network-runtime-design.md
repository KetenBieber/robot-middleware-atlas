# Scenario：怎样从零设计一套机器人网络 Runtime

> **知识依赖：** 多线程 Runtime 与流式 Backpressure 提供本页所需的 Queue、Worker、Data Age 和容量控制基础。核心骨架只有 Event Loop、Owner Thread、Worker Executor、Queue + Wakeup、Backpressure 五个结构；Asio Operation、Strand、Seastar shard-per-core 是在这套骨架上的进一步实现。

## 分层理解这套 Runtime

| 阅读层级 | 先看什么 | 暂时可以跳过什么 | 读完应回答的问题 |
| --- | --- | --- | --- |
| 第一遍：建立骨架 | 一连接一线程、Reactor/Event Loop、Timer、Owner Thread、Executor、Queue + Wakeup、Backpressure、Shutdown | Asio Operation、Strand、Seastar、nginx 资源池细节 | 为什么一个网络 Runtime 需要“等待、执行、跨线程唤醒、容量控制”四条线？ |
| 第二遍：理解对象设计 | Operation、Strand、资源池、active/passive queue state | 多核 sharding 的工程细节 | 为什么不能把所有 callback、锁和生命周期都塞进一个 EventLoop 类？ |
| 第三遍：理解多核扩展 | MPMC 热点、shard-per-core、shared-nothing、cooperative scheduling | 无 | 什么时候优化共享 Queue，什么时候直接减少共享？ |

如果第一遍读到 Asio/Seastar 的名词开始吃力，直接跳到“Backpressure”“Shutdown”和“最小架构”继续主线即可；这些高级实现不会改变前面已经建立的基本模型。


这篇文章讨论的不是“某个库怎么用”，而是一个更接近工程设计的问题：

> 如果你要自己设计一套承载机器人遥测、命令、日志和网络连接的 Runtime，应该怎样从需求一步步推导出 event loop、任务队列、跨线程唤醒、Timer、worker、backpressure 和多核拓扑？

为了把问题说具体，先假设系统里有四类工作：

| 工作 | 频率/规模 | 主要约束 |
| --- | --- | --- |
| 控制命令 | 100–1000 Hz，小消息 | 低延迟、不能长时间排队 |
| 状态遥测 | 10–200 Hz，中等消息 | 允许覆盖旧值，强调新鲜度 |
| 日志/诊断 | bursty，大量小消息 | 吞吐优先，可异步 |
| TCP/UDP 连接 | 数百到数千 | 不希望一连接一线程 |

真正困难的地方不在 socket API，而在于：

~~~text
谁拥有连接状态？
谁等待 fd？
谁执行 callback？
跨线程任务怎么投递？
队列满了怎么办？
Timer 放在哪里？
慢 handler 会拖住谁？
多核到底是共享队列还是分片？
shutdown 时谁先停？
~~~

这正是 libuv、Asio、Folly、Seastar 和 nginx 分别给出的不同答案。

---

## 1. 第一版最朴素实现：一连接一线程

第一次写服务器时，很容易写成：

下面只是“一连接一线程”的控制流伪代码，不是可编译示例：

~~~text
void serve(int fd) {
    while (running) {
        Message msg = blocking_read(fd);
        handle(msg);
    }
}
~~~

每个连接创建一个线程。

优点非常明显：

- 控制流直观；
- 一条连接上的状态天然被一个线程顺序修改；
- blocking I/O 很好理解。

但连接一多，问题立刻出现。

### 1.1 Thread 数量不是免费资源

每个线程都需要：

- stack；
- kernel scheduling state；
- context switch；
- cache working set。

1000 个连接并不意味着真的有 1000 个连接同时在工作，但“一连接一线程”仍然要维护 1000 个调度实体。

机器人系统更麻烦，因为网络线程还会和：

- 控制线程；
- 感知线程；
- 日志线程；
- ROS/中间件线程；

争 CPU。

于是第一个设计转折出现：

> **连接状态很多，不代表执行线程也必须很多。**

---

## 2. Reactor：把“等待事件”和“执行业务”分开

Reactor 是一种事件驱动组织方式：一个或少量 owner thread 阻塞等待“哪些 fd / timer / wakeup 已经 ready”，醒来后再把对应事件分发给 handler。它不等于 epoll；epoll 只是 Linux 上实现 readiness wait 的一种 OS primitive。

先看一个**真正可运行的 Linux epoll 程序**。为了避免引入 TCP 协议细节，这里用 `eventfd` 充当一个可被 epoll 等待的 fd；换成 socket fd 后，Reactor 的等待模型不变。

这个示例是 **Linux-only**，因为直接使用 `epoll` 和 `eventfd`：

~~~text
g++ -std=c++17 -O2 -pthread epoll_eventfd_demo.cpp -o epoll_eventfd_demo
./epoll_eventfd_demo
~~~


~~~cpp
#include <chrono>
#include <cstdint>
#include <iostream>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <thread>
#include <unistd.h>

int main() {
    const int epfd = ::epoll_create1(0);
    const int wake_fd = ::eventfd(0, EFD_NONBLOCK);

    if (epfd < 0 || wake_fd < 0) {
        std::cerr << "failed to create epoll/eventfd\n";
        return 1;
    }

    epoll_event ev{};
    ev.events = EPOLLIN;
    ev.data.fd = wake_fd;

    if (::epoll_ctl(epfd, EPOLL_CTL_ADD, wake_fd, &ev) != 0) {
        std::cerr << "epoll_ctl failed\n";
        return 1;
    }

    std::thread producer([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        const std::uint64_t one = 1;
        ::write(wake_fd, &one, sizeof(one));
    });

    epoll_event ready[4]{};
    const int n = ::epoll_wait(epfd, ready, 4, 1000);

    for (int i = 0; i < n; ++i) {
        if (ready[i].data.fd == wake_fd) {
            std::uint64_t value = 0;
            ::read(wake_fd, &value, sizeof(value));
            std::cout << "event loop woke up, value="
                      << value << "\n";
        }
    }

    producer.join();
    ::close(wake_fd);
    ::close(epfd);
}
~~~

编译：

~~~text
g++ -std=c++17 epoll_demo.cpp -pthread -o epoll_demo
./epoll_demo
~~~

这个程序里只有一个 Event Loop Owner：主线程。Producer Thread 不执行 Event Loop 内部逻辑，它只把 `wake_fd` 变成 readable；主线程从 `epoll_wait()` 返回后再处理 ready event。

Reactor 的核心不是 `epoll` 这个 API，而是一个 ownership 决策：

> 大量 fd 可以由一个 execution owner 统一等待；只有 ready 的连接才产生工作。

这会把系统从：

~~~text
connection -> thread
~~~

改成：

~~~text
many connections
      ↓
   epoll
      ↓
single event-loop owner
      ↓
 ready callbacks
~~~

nginx 和 libuv 都建立在这种思想上，但它们继续解决了“裸 epoll loop”还没解决的问题。

---

## 3. 为什么真正的 Event Loop 绝不只是 epoll_wait()

如果 Event Loop 只有：

~~~text
wait fd
dispatch callback
~~~

那 Timer、跨线程任务、deferred close、异步文件工作应该放在哪里？

libuv 的设计价值就在这里：它把不同来源的 ready work 组织成多个 phase。

下面是 **libuv 真实源码片段**，依赖 libuv 工程上下文，不是独立程序。固定版本源码里，loop 主体会先计算 timeout，再进入 I/O poll：

~~~c
if ((mode == UV_RUN_ONCE && can_sleep) ||
    mode == UV_RUN_DEFAULT)
  timeout = uv__backend_timeout(loop);

uv__io_poll(loop, timeout);

for (r = 0;
     r < 8 && !uv__queue_empty(&loop->pending_queue);
     r++)
  uv__run_pending(loop);
~~~

这段代码说明一个很重要的 Runtime 设计原则：

> **I/O readiness 只是 ready work 的一种来源。**

Event Loop 同时可能要消费：

- fd readiness；
- Timer；
- pending callback；
- async wakeup；
- close callback；
- threadpool completion。

所以设计 Runtime 时，不应该先问：

> “我要不要用 epoll？”

而应该问：

> “系统里有哪些 ready source，它们怎样进入同一个 execution owner？”

---

## 4. Timer 为什么必须成为 Runtime 的一等公民

机器人网络 Runtime 里 Timer 很多：

- heartbeat；
- reconnect；
- request timeout；
- watchdog；
- delayed retry；
- periodic telemetry。

如果每个模块自己创建线程 sleep：

下面是设计反例的伪代码：

~~~text
while (running) {
    sleep_for(10ms);
    check_timeout();
}
~~~

Runtime 会重新退化成很多线程。

更合理的结构是：

~~~text
Timer registration
      ↓
central timer structure
      ↓
nearest deadline
      ↓
event-loop sleep timeout
~~~

libuv 使用 min-heap；nginx 使用 rbtree；Folly 的 `HHWheelTimer` 使用 hierarchical timing wheel。

这三个实现说明：Timer 容器没有唯一答案。

| 结构 | 更适合 |
| --- | --- |
| min-heap | 动态 Timer，主要关心最小 deadline |
| rbtree | 任意删除较多，希望节点内嵌 |
| timing wheel | Timer 数量非常大、允许分桶 |

选择依据不是“哪个 Big-O 最漂亮”，而是：

~~~text
Timer 数量
插入/取消比例
精度要求
是否频繁 reschedule
是否需要 ordered traversal
~~~

---

## 5. 单线程 Event Loop 的真正优势：状态天然串行

假设一个连接对象里有：

这里只画对象成员关系，不把未定义的 Parser / SendQueue 冒充成独立 C++ 程序：

~~~text
struct Connection {
    Parser parser;
    SendQueue send_queue;
    State state;
};
~~~

如果 read callback、write callback、timeout callback 都只在同一个 Event Loop 线程里执行，那么：

~~~text
parser
send_queue
state
~~~

通常都不需要 mutex。

这也是 nginx 的核心设计之一：

~~~text
master
  ├─ worker 0 -> event loop
  ├─ worker 1 -> event loop
  ├─ worker 2 -> event loop
  └─ worker 3 -> event loop
~~~

每个 worker 自己拥有一批连接。

这不是“单线程性能差”，而是：

> **先用 ownership 消除共享，再用多个 owner 扩展。**

这和后面 Seastar 的 shard-per-core 思想高度一致。

---

## 6. Event Loop 最大的敌人：长 callback

如果一个 callback 做：

下面是“长 callback”的控制流示意：

~~~text
void on_message(const Message& m) {
    run_neural_network();
    write_database();
    compress_log();
}
~~~

那么 Event Loop 在这段时间里无法继续：

- 收包；
- 发包；
- 处理 Timer；
- accept；
- close。

因此 Event Loop 有一个非常硬的设计约束：

> callback 必须短。

网络 Runtime 通常应该把工作分成两类：

~~~text
I/O / state-machine work
    -> event loop

CPU-heavy / blocking work
    -> worker executor
~~~

这一步开始引出第二个核心模块：Executor。

---

## 7. Executor：为什么“线程池”还不够

Executor 先不要理解成“线程池的高级名字”。它更抽象：**接收一个可执行 Task，并决定这个 Task 在哪里、何时、以什么串行/并行策略执行。** ThreadPool 只是 Executor 的一种实现；单线程 Event Loop、Strand、按优先级的 Worker Pool 都可以实现 Executor 语义。

这里先明确一件事：下面的 `m / cv / q` 仍然不是三个“各线程自己的局部变量”。真实线程池通常会把它们作为**同一个 ThreadPool 对象的成员**，所有 worker 线程都通过 `this` 访问同一份 queue、同一把 mutex 和同一个 condition variable。

如果对 `std::mutex / std::lock_guard / std::unique_lock / std::condition_variable / wait / notify_one` 的对象关系还不熟，先完整阅读 [线程间通信：共享地址不等于共享时序](threads-memory-order.md)。那里用两个真正的 `std::thread`、完整 `main()` 和逐步时序解释了它们为什么必须这样组合。

把这里的简化模型写成对象关系：

~~~text
                  ThreadPool pool
             /          |          \
            v           v           v
        pool.m       pool.cv      pool.q
           ^            ^           ^
           |            |           |
        worker 0     worker 1    producer/post()
            \          /
             \        /
             same shared objects
~~~

因此下面代码表达的是“多个线程共享同一个线程池内部状态”：

这里不再重复一份半完整 ThreadPool；**完整可运行版本**见 [从“一组件一线程”到真正的多线程 Runtime](scenario-thread-runtime.md)。下面只保留 worker 的关键控制流：

~~~text
std::mutex m;
std::condition_variable cv;
std::queue<Task> q;

void worker() {
    while (true) {
        Task t;

        {
            // unique_lock 在这里不是为了比 lock_guard 更高级，
            // 而是因为 cv.wait() 必须能够暂时 unlock(m)，
            // 睡眠，然后在返回前重新 lock(m)。
            std::unique_lock lock(m);

            cv.wait(lock, [&] {
                return !q.empty();
            });

            t = std::move(q.front());
            q.pop();
        } // 离开作用域，worker 释放 m

        // 用户任务故意在 mutex 外执行。
        // 否则一个长任务会让其他 worker 连 q 都不能访问。
        t();
    }
}
~~~

这里 `cv.wait(...)` 的含义不是“注册一个 callback”。它表示：**当前 worker 在线程层面阻塞，条件可能变化后被唤醒，再重新竞争 mutex，并重新检查 queue。**

这能工作，但 Runtime 还要回答：
- task completion 回到哪里？

Folly 的价值就在于：把这些策略拆开。

它的 Executor 设计告诉我们：

> **Queue policy、worker policy、wakeup policy 不应该天然绑死。**

这对机器人 Runtime 很重要，因为：

- 日志任务可以吞吐优先；
- 控制相关辅助任务可能要低延迟；
- blocking I/O 不能和 CPU-heavy 任务共用同一 pool。

---

## 8. 跨线程投递：Queue + Wakeup 缺一不可

假设网络线程正在：

~~~text
epoll_wait(...);
~~~

另一个线程往 Event Loop 的任务队列塞了一个 callback：

~~~text
queue.push(task);
~~~

Event Loop 并不会自动醒来。

所以真正的跨线程 post 需要：

~~~text
1. publish task
2. update queue state
3. wake event loop
~~~

常见做法是 eventfd/pipe。

但如果每次 push 都 write eventfd：

~~~text
10000 producers
   ↓
10000 wakeups
~~~

系统调用成本又会很高。

Folly `AtomicNotificationQueue` 的关键思想是给 Queue 增加状态：

~~~text
Empty
  ↓
Armed
  ↓
Non-empty
~~~

这里 Armed 可以读成“Consumer 已经声明：我准备睡了，如果状态从空变成有任务，请负责叫醒我”。这样 Producer 不必对每次 push 都无条件执行一次系统调用。

只有“消费者可能正在睡眠”时才真正触发 wakeup。

这带来一个很重要的设计结论：

> **notification 不是数据；Queue state 才是真值。**

因此不要把“收到 eventfd”理解成“有且只有一个任务”。

正确逻辑是：

~~~text
wakeup
  ↓
drain queue
  ↓
重新判断状态
  ↓
决定是否再次 sleep
~~~

---

> **第一遍可以在这里暂时跳过实现细节。** Reactor、Owner Thread、Executor、Queue + Wakeup 已经形成网络 Runtime 的基本骨架。Asio Operation、Strand、Seastar 和 nginx 是用真实工程验证这套骨架，不需要第一次阅读就掌握。


## 9. Asio 为什么要把 Operation 做成对象

如果 Runtime 只传 lambda，看起来很简单：

下面只是接口形态示意：

~~~text
post([&]{
    socket.write(...);
});
~~~

但一个真实异步操作还包含：

- handler；
- error/result；
- cancellation；
- allocator；
- executor；
- continuation；
- 生命周期。

Asio 把异步动作表示成 operation object。


这里 allocator 决定 operation 自身的内存从哪里来；executor 决定 completion 以后在哪个执行上下文继续；continuation 是“当前异步操作完成后紧接着要继续执行的下一段工作”。第一次阅读只要知道这些信息必须跟着 operation 生命周期走，不必先掌握 Asio 的模板实现。
这让 scheduler 不必理解业务类型，只需要处理：

~~~text
operation*
~~~

典型执行链可以抽象为：

~~~text
operation queue
    ↓
pop operation
    ↓
unlock scheduler mutex
    ↓
operation.complete(...)
~~~

这里“先 unlock 再 complete”非常关键。

如果 scheduler 在持锁状态执行用户 callback：

~~~text
scheduler lock
     ↓
 user callback
     ↓
 callback 再 post
     ↓
 reentrant lock / deadlock / long critical section
~~~

因此 Runtime 的一个通用规则是：

> **内部调度锁保护队列，不应该包住用户代码执行。**

---

## 10. Strand：不用给业务对象到处加 mutex

Strand 可以先理解成“逻辑串行执行通道”：很多线程都可以向它 post handler，但同一个 Strand 保证这些 handler 不会并发执行。它不要求固定只有一个 OS thread，而是保证**同一时刻只有一个属于该 Strand 的 handler 拿到执行权**。

假设一个 Session 可能收到：

~~~text
on_read
on_timeout
on_write_done
on_close
~~~

它们来自不同异步操作。

朴素办法是在 Session 里加 mutex：

这里是锁住业务状态机的伪代码：

~~~text
void Session::on_read(...) {
    std::lock_guard lock(m_);
    ...
}
~~~

但状态机越复杂，锁越难维护。

Asio Strand 的思路是：

> 不保护对象，而是保证属于这个对象的 handler 串行执行。

于是：

~~~text
many producer threads
       ↓
     strand
       ↓
serial handler lane
       ↓
 Session mutable state
~~~

这是一种非常值得借鉴的设计：

> 如果一个对象本质需要串行状态机，优先串行“执行权”，而不是给每个字段上锁。

---

## 11. Backpressure：Queue 满了以后才看出 Runtime 是否设计完整

很多 Runtime 原型都有：

下面只是反模式的容器声明：

~~~text
std::queue<Task> tasks;
~~~

但没有最大长度。

在机器人系统里这非常危险。

假设遥测生成速度 2 kHz，而网络只能发送 500 Hz：

~~~text
producer 2 kHz
     ↓
unbounded queue
     ↓
consumer 500 Hz
~~~

Queue 会不断增长。

更糟糕的是，消息到达网络时已经非常旧。

所以 Queue 设计至少要明确：

~~~text
capacity
overflow policy
deadline / max-age
drop policy
~~~

不同数据应该使用不同策略：

| 数据 | 合理策略 |
| --- | --- |
| robot pose | latest-only / drop-old |
| motor command | bounded + reject/fail-fast |
| event log | bounded FIFO + batch |
| RPC request | admission control |
| reconnect task | coalesce |

admission control 是在工作进入系统之前先判断容量，满了就拒绝/限流；coalesce 是把多个等价的待处理请求合并，例如已经排着一个“重连”任务时，后续十个重连请求不必再排十份。

Backpressure 不是 Queue 的附加功能，而是业务语义。

---

## 12. 为什么 MPMC Queue 往往不是最终答案

如果所有线程共享一个：

~~~text
global MPMC queue
~~~

逻辑确实简单。

但高负载下会出现：

- shared tail/head cache-line contention；
- CAS retry；
- NUMA traffic；
- wakeup storm。

Folly 的 MPMC Queue 展示了如何用 ticket/turn 解决 slot 复用，但 Seastar 给出了更激进的答案：

这里 ticket 可以理解成每次 enqueue/dequeue 获得的逻辑序号，turn 则记录某个物理 slot 当前属于第几轮复用；二者组合避免不同轮次错误地同时占用同一个 slot。

> **不要优化共享，直接减少共享。**

---

## 13. Seastar：shard-per-core 重新定义 Runtime 拓扑

Seastar 的核心模型可以画成：

~~~text
CPU 0
  Reactor
  local state
  local queues

CPU 1
  Reactor
  local state
  local queues

CPU 2
  Reactor
  local state
  local queues
~~~

mutable state 尽量固定属于一个 core。

跨 core 访问不直接加锁，而是：

~~~text
shard A
   ↓ message
SPSC queue
   ↓
shard B
   ↓
execute on owner core
~~~

这个模型非常适合理解高性能 Runtime 的一个本质：

> 多核扩展不一定意味着“更多线程一起碰同一份状态”；也可以是“更多独立 owner，各自处理自己的状态”。

这会把问题从：

~~~text
如何让 shared map 更快？
~~~

变成：

~~~text
能不能让这份 map 根本只属于一个 shard？
~~~

---

## 14. Shared-nothing 并不是没有代价

shard-per-core 的代价也很明确：

### 14.1 Cross-shard operation 必须显式

例如连接属于 shard 2：

~~~text
shard 0 wants update
      ↓
message queue
      ↓
shard 2 executes
~~~

调用不再是普通函数调用。

### 14.2 长任务更危险

如果 Reactor 采用 cooperative scheduling，而 task 不主动让出 CPU：

cooperative scheduling 的意思是 Runtime 不会在任意指令位置强制抢占当前 Task；Task 必须在约定的 await/yield/返回点主动把执行权交回 Reactor。

~~~text
long task
   ↓
reactor cannot progress
   ↓
I/O latency rises
~~~

所以 shared-nothing 不是免费性能，而是用更严格的 ownership discipline 换取 locality。

---

## 15. nginx：资源池设计同样属于 Runtime

网络 Runtime 除了调度，还必须管理连接生命周期。

如果每个 accept 都：

~~~text
auto* c = new Connection;
~~~

close 后再：

~~~text
delete c;
~~~

高频 churn 会带来：

- allocator contention；
- fragmentation；
- unpredictable latency。

nginx 采用预分配 connection slot + free list。

结构上更像：

~~~text
connection pool
  ├─ free list
  ├─ active connections
  └─ reusable queue
~~~

新连接从 free list 取 slot；关闭后归还。

这体现了另一个 Runtime 设计原则：

> **高频核心对象的 allocator policy 应该显式设计，而不是默认交给通用 heap。**

同样思想也能用于：

- request object；
- message descriptor；
- task node；
- buffer。

---

## 16. Shutdown 为什么必须从一开始设计

网络 Runtime 最容易被忽略的是关闭顺序。

错误关闭流程：

~~~text
destroy Session
   ↓
pending callback fires
   ↓
use-after-free
~~~

或者：

~~~text
stop worker
   ↓
Event Loop still waiting completion
   ↓
shutdown hangs
~~~

一个比较清晰的关闭协议应该分阶段：

~~~text
1. stop accepting new work
2. mark endpoints closing
3. cancel timers / I/O
4. drain or reject queued work
5. wait in-flight completion
6. destroy owned objects
7. stop executor/event loop
~~~

这里 again 体现 ownership：

> 谁创建异步操作，谁必须知道它什么时候完全不可能再回调。

---

## 17. 从五个项目提炼出的统一对象图

现在可以把 libuv、Asio、Folly、Seastar 和 nginx 抽象成统一 Runtime：

~~~text
            external events
     socket / timer / other threads
                │
                v
        +----------------+
        | readiness layer|
        | epoll/timer/...|
        +----------------+
                │
                v
        +----------------+
        | event-loop owner|
        +----------------+
          │            │
 short work         heavy work
          │            v
          │       +----------+
          │       | executor |
          │       +----------+
          │            │
          │       completion
          │            │
          +------<-----+
                │
                v
        state-machine objects
                │
                v
       queues / buffers / pools
~~~

如果多核扩展，再变成：

~~~text
shard 0 runtime  <----message----> shard 1 runtime
      │                               │
 local state                       local state
      │                               │
 local I/O                          local I/O
~~~

---

## 18. 一个适合机器人网络 Runtime 的最小架构

如果今天自己写一套中型机器人 Runtime，一个比较稳妥的起点是：

~~~text
                    +------------------+
                    |   Control Thread |
                    +---------+--------+
                              |
                       bounded MPSC
                              |
                              v
+--------------------------------------------------+
|               Network Event Loop                 |
|                                                  |
| epoll + timer heap + async eventfd               |
|                                                  |
| Connection registry                             |
| Session state machines                          |
| TX queues / timeout / reconnect                 |
+---------------------+----------------------------+
                      |
               heavy / blocking work
                      |
                      v
             +------------------+
             | Worker Executor  |
             +------------------+
                      |
                 completion
                      |
                    eventfd
                      |
                      v
               Network Event Loop
~~~

这里建议明确以下约束：

### Event Loop

只做：

- socket I/O；
- parser/state-machine；
- Timer；
- Queue drain；
- lightweight routing。

### Worker Executor

做：

- 压缩；
- 文件 I/O；
- 大日志处理；
- expensive serialization；
- 非实时业务。

### Control Thread

不要直接进入网络内部状态。

只通过有界 mailbox：

~~~text
control
   ↓ command descriptor
network owner
~~~

这样网络卡顿不会把 mutex contention 直接传播进控制环。

---

## 19. Queue 应该按语义拆，而不是所有消息共用一个队列

错误设计：

~~~text
global_queue<Message>
~~~

更好的设计：

~~~text
control_cmd_queue     bounded / fail-fast
telemetry_latest      latest-value
log_queue             bounded FIFO
completion_queue      MPSC
reconnect_queue       coalesced
~~~

为什么？

因为不同消息的：

- loss semantics；
- freshness semantics；
- capacity；
- priority；

根本不同。

一个通用 Queue 很容易把所有业务都降级成“FIFO + backlog”。

---

## 20. 多核扩展的三个阶段

不要一开始就直接做复杂 MPMC。

### Phase 1：单 Event Loop

适合：

- 几百连接；
- callback 很短；
- 主要瓶颈不在网络线程。

~~~text
one loop
many connections
~~~

### Phase 2：Event Loop + Worker Pool

把 CPU-heavy 工作移走：

~~~text
loop
  ↕
executor
~~~

### Phase 3：多个 owner / shard

当单 loop 本身已经成为瓶颈：

~~~text
loop 0 owns connection set A
loop 1 owns connection set B
loop 2 owns connection set C
~~~

再使用：

- SO_REUSEPORT；
- hash dispatch；
- explicit cross-shard message passing。

这比“所有线程共享所有 connection object”更容易扩展。

---

## 21. 什么时候应该借鉴哪套实现

### libuv

当你想学习：

- 一个可嵌入 Event Loop 怎样组织 phase；
- fd、Timer、async、threadpool completion 如何汇合；
- handle/request 生命周期。

继续阅读：[libuv 源码专题](../generated/libuv/index.rst)。

### Asio

当你想学习：

- operation abstraction；
- executor；
- completion；
- strand；
- cancellation/lifetime。

继续阅读：[Asio 源码专题](../generated/asio/index.rst)。

### Folly

当你想学习：

- SPSC/MPMC Queue；
- AtomicNotificationQueue；
- executor wakeup；
- IOBuf；
- timer wheel；
- concurrent data structure。

继续阅读：[Folly 源码专题](../generated/folly/index.rst)。

### Seastar

当你想学习：

- shard-per-core；
- shared-nothing；
- cooperative scheduling；
- cross-shard queue；
- owner-side reclamation。

继续阅读：[Seastar 源码专题](../generated/seastar/index.rst)。

### nginx

当你想学习：

- 完整高并发 server runtime；
- worker/event loop；
- connection pool；
- Timer rbtree；
- posted event；
- graceful shutdown。

继续阅读：[nginx 源码专题](../generated/nginx/index.rst)。

---

## 22. 最终设计检查表

真正开始写 Runtime 前，至少把下面这些答案写出来：

~~~text
Execution ownership
-------------------
谁拥有 connection/session state？
用户 callback 在哪里执行？
哪些对象允许跨线程修改？

Queue
-----
每条 queue 是 SPSC/MPSC/MPMC？
bounded capacity 是多少？
满了 block/drop/reject 哪一种？

Wakeup
------
consumer 怎样 sleep？
producer 怎样 wake？
是否允许 wakeup coalescing？

Timer
-----
Timer 数量？
取消频率？
deadline 精度？
选择 heap/rbtree/wheel 的依据？

Backpressure
------------
哪些数据允许丢？
哪些必须保序？
max-age 是多少？

Memory
------
哪些对象需要 pool？
payload copy/move/loan？
谁负责最后释放？

Multicore
---------
共享状态还是 shard owner？
跨核调用怎样表达？
NUMA 是否重要？

Shutdown
--------
如何停止新任务？
如何取消 I/O？
如何 drain completion？
什么时候对象才安全销毁？
~~~

如果这些问题没有答案，换成更高级的异步 API 也不会让 Runtime 自动变正确。

真正成熟的 Runtime 设计，通常不是“用了多少 lock-free、epoll、协程”，而是：

> **每一份 mutable state 都有清晰 owner；每一条 Queue 都有容量与语义；每一次 wakeup 都有状态机；每一个异步对象都有可证明的 lifetime；每一种 overload 都有明确牺牲项。**
