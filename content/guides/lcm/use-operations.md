# LCM 使用教程：日志回放、队列容量与丢包诊断

机械臂偶发一次关节超调后，现场程序已经退出，操作者只剩终端里几行“收到命令”的输出；他们既不知道错误命令之前的状态，也不能在不开动硬件的情况下重放故障。若只增加更多 `printf`，高频输出还可能拖慢 callback 并掩盖时序。本章把一次运行变成可解释的输入样本：先记录总线，再理解日志为何不依赖消息类型，随后完成隔离回放与离线解码，最后建立队列容量、命令确认和关闭流程。

下文对应 LCM 源码提交 lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864。LCM 的事件日志格式很小：每个 event 保存同步字、递增 event number、微秒时间戳、channel 长度、data 长度以及两段原始字节。[日志格式说明](https://lcm-proj.github.io/lcm/content/log-file-format.html)适合先建立整体认识；真正决定工具行为的是 `lcm_logger.c`、`lcm_logplayer.c` 和 C++ `LogFile` 封装。

## 记录与回放

先把在线记录和离线回放放到两个不同的 multicast 地址，避免回放报文重新进入仍在运行的生产系统：


```bash
LIVE_URL='udpm://239.255.76.67:7667?ttl=0'
REPLAY_URL='udpm://239.255.76.68:7668?ttl=0'

lcm-logger --lcm-url="$LIVE_URL" \
  --channel='^(ATLAS_STATE|ATLAS_EVENT)$' \
  -i --split-mb=512 --flush-interval=100 \
  --max-unwritten-mb=256 run.lcm

lcm-logplayer --lcm-url="$REPLAY_URL" --speed=1.0 \
  --regexp='^(ATLAS_STATE|ATLAS_EVENT)$' run.lcm.00
```

`--channel` 是传给订阅的正则表达式；`--regexp` 是播放器再次筛选日志事件的表达式。记录阶段收窄 channel 能减少磁盘流量，但也永久丢掉未记录的上下文；通常应同时记录 heartbeat、版本和故障事件。`-i` 防止覆盖同名文件，且是 `--split-mb` 的前置条件之一。另一种长期运行策略是 `--rotate=N --split-mb=M`，它只保留固定数量的分片；`--increment` 与 `--rotate` 不能同时使用。

命令行只是控制面，logger 的数据面更值得理解：LCM callback 收到消息后复制数据并放入待写队列，写线程再把 event 编码到文件。网络接收与磁盘抖动因此被一段有限内存隔开。

```text
LCM receive/handle
  -> logger callback 复制 channel 与 payload
  -> unwritten queue（--max-unwritten-mb）
  -> writer thread
  -> stdio buffer
  -> fflush（--flush-interval）
  -> 文件系统与存储设备
```

`--max-unwritten-mb` 限制“已收到但尚未写完”的消息内存，默认值为 100 MB。到达速率持续高于磁盘可写速率时，队列到顶后 logger 会丢消息；把限制调大只能延后这一时刻。`--flush-interval` 越短，进程异常时可能丢失的缓冲数据越少，但同步开销更频繁。它不是每条消息都做耐久写入的承诺，文件系统和设备仍可能缓存数据。

`--disk-quota=10GB` 表示至少保留 10 GB 可用空间，而不是允许日志最多写 10 GB；低于保留线时 logger 退出。该选项在这里固定的源码版本中不支持 Windows。无人值守部署必须监视 logger 进程是否退出，因为“主应用仍运行”不等于“数据仍在记录”。

日志保存的是接收端观察到的原始报文，不会恢复曾在网络上丢失的包；它也不会自动保存业务二进制版本和 `.lcm` schema，二者需要一并归档。

### 用容量预算判断 logger 能否撑过磁盘抖动

设总输入速率为 `R_in` MB/s，磁盘在抖动期间的持续写入速率为 `R_disk` MB/s，待写队列上限为 `Q` MB。若 `R_in > R_disk`，队列大约在

```text
T_full = Q / (R_in - R_disk)
```

秒后耗尽。例：输入 180 MB/s、磁盘暂时只能写 100 MB/s、队列 256 MB，约 3.2 秒后就可能开始丢日志。这一计算没有包含 event header、内存分配和文件系统波动，因此只能作为上限估计。真正的设计动作是降低记录数据量、提供持续写入能力更高的介质，或者明确接受并观测丢失，而不是盲目增加内存。

## 日志为什么能与类型系统解耦

LCM 日志保存 channel 和原始 wire payload，不需要在记录时解码具体类型。这样 logger 即使没有安装所有生成类也能忠实保存字节；回放时由当前订阅者完成 fingerprint 检查和解码。

这种设计的代价是日志本身不能解释业务语义。归档目录应包含：

```text
experiment_042/
├── bus.lcm
├── schemas/                 # 原始 .lcm 文件
├── manifest.yaml            # URL、版本、主机、开始时间
├── binaries.sha256
└── notes.md                 # 场景与已知故障
```

只保存 `.lcm` 日志文件，几年后可能仍能读出 channel 和 bytes，却无法确认用哪一版生成代码解释它。

## 可重复回放协议

回放不是简单点击播放。固定以下变量：Provider URL、channel filter、起止 event、倍率、是否按原时间间隔、消费者构建版本和输出比较规则。

```text
读取 event N
  -> 以日志 timestamp 计算相对目标时间
  -> 使用 monotonic clock 等待（倍率可调）
  -> publish 原 channel + 原 payload
  -> 记录实际发布时间与业务输出
```

墙上时钟可能因 NTP 跳变，不适合调度回放；日志 timestamp 可用于计算相对间隔，等待应落在单调时钟。高倍率回放用于压力测试时，要明确它改变了突发性，不能把所得延迟直接当作实时运行延迟。

### 把回放当作一次独立实验

一个可复现流程包含四个阶段：

1. **冻结输入**：保存日志、schema、生成器版本、业务二进制哈希和运行参数；
2. **隔离总线**：播放器发布到专用 `REPLAY_URL`，被测进程只订阅该 URL；
3. **捕获输出**：把被测进程的决策结果发布到独立 channel，并用第二个 logger 记录；
4. **语义比较**：忽略 wall-clock、随机 id 等非确定字段，比较关键状态转换、数值容差和事件顺序。

“同一日志能够播放”不是可重复性的充分条件。消费者若读取真实时间、随机数、外部文件或另一条在线总线，同一输入仍会产生不同结果。工程上应把这些依赖也注入或记录下来。

## 使用 `lcm::LogFile` 离线读取原始事件

播放器适合把事件重新注入总线；批量统计、数据转换和回归断言不需要网络，可直接遍历日志。下面保留了官方 `read_log.cpp` 的核心控制流，并加入 channel 筛选与严格解码检查：


```cpp
#include <cstdint>
#include <iostream>
#include <lcm/lcm-cpp.hpp>

#include "atlas/state_t.hpp"

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: inspect_log LOGFILE\n";
    return 2;
  }

  lcm::LogFile log(argv[1], "r");
  if (!log.good()) {
    std::cerr << "cannot open log: " << argv[1] << '\n';
    return 1;
  }

  std::int64_t accepted = 0;
  while (const lcm::LogEvent* event = log.readNextEvent()) {
    if (event->channel != "ATLAS_STATE") {
      continue;
    }

    atlas::state_t message;
    const int consumed =
        message.decode(event->data, 0, event->datalen);
    if (consumed != event->datalen) {
      std::cerr << "decode failed at event "
                << event->eventnum << '\n';
      continue;
    }

    ++accepted;
    std::cout << event->timestamp << ','
              << message.sequence << ','
              << message.position[0] << '\n';
  }

  return accepted == 0 ? 3 : 0;
}
```

这里有四个需要逐句读懂的 C++ 语义：

- `lcm::LogFile log(..., "r")` 是栈对象，离开作用域时析构并关闭底层文件，体现 RAII；
- `readNextEvent()` 返回 `const lcm::LogEvent*`，调用方只借用该对象，不拥有它，不应 `delete`，也不要跨下一次读取长期保存指针；
- `event->data` 是无类型字节，只有选定正确的生成类后才获得业务含义；fingerprint 不匹配会使生成的 `decode` 失败；
- `decode` 返回实际消费的字节数，要求它等于 `datalen` 能同时排除解码错误与尾部出现意外数据。只判断“返回值非负”会放过部分解码。

这段程序的时间复杂度为 `O(E + B)`：`E` 是扫描的 event 数量，`B` 是被解码 payload 的总字节数。它不为所有消息建立内存索引，所以额外空间接近单条消息大小，适合顺序处理大日志；代价是每次查找靠后的事件都要从前向后扫描。若需要大量按时间范围随机查询，应一次性转换到带索引的分析格式，而不是在业务工具里反复全表扫描。

## 接收队列不是可靠日志

UDPM provider 的 `inbufs_filled` 是可增长的链表，不是固定容量队列；真正控制消息是否继续保留的是每个匹配 subscription 的计数配额，默认 30。应用长时间不调用 `handle()` 时，达到配额的 subscription 不再获得新消息；如果同一 channel 还有未满的其他 subscription，provider 仍可把共用 payload 入队供它们处理。只有没有可接收的 subscription 时，这个消息才会被 provider 丢弃。扩大配额只能吸收突发，不能修复持续过载；链接队列与准入计数分别见 `lcm_buf_queue_*()` 与 `lcm_try_enqueue_message()`。

```text
network receive thread -> filled-buffer linked list -> notification pipe -> handle -> callback
```

监控至少包含：消息 sequence gap、handle 调用间隔、callback 时间、subscription quota 拒收、内核 socket drop，以及未完成重组项的创建与 LRU 淘汰。固定版本没有周期性重组超时扫描，若运维需要观测“半包停留多久”，必须在复刻或打点版本中额外记录。

### 用到达率判断扩容是否有意义

设发布到达率为 `λ`，callback 平均服务率为 `μ`。若长期 `λ > μ`，任何有限队列最终都会满；从 100 扩到 10,000 只会把丢包变成长延迟和更高内存。只有当平均 `λ < μ`、但存在短突发时，扩大容量才可能吸收抖动。

平均值仍不够。业务若要求数据年龄小于 50 ms，应直接测 oldest queued age，并在超过阈值时丢弃旧状态或进入降级，而不是等内存水位告警。

## 大消息与分片

超过单 datagram 路径的消息会分片。任一片丢失会使整条消息无法重组，丢包概率随片数放大。大图像应压缩、降低频率、换可靠 transport，或拆为应用可独立处理的块。

不要只测平均吞吐；在背景流量和接收端限速时测完整消息成功率与 p99 数据年龄。

## Channel 规划

```text
ATLAS_STATE       高频状态，可容忍旧值丢失
ATLAS_COMMAND     命令，需要应用确认/序号
ATLAS_EVENT       审计事件，考虑持久可靠通道
ATLAS_HEARTBEAT   存活与版本
```

LCM 自身不提供命令确认。关键命令应携带 command id，并在独立 ack channel 回应；发送方实现超时、幂等重试和去重。

一个最小命令协议包含：`command_id`、发送者 id、目标、deadline、operation 和参数。接收端维护最近已完成 id 的有界缓存：


```cpp
void CommandHandler::onCommand(const command_t& command) {
  if (command.deadline_us < synchronized_now_us()) {
    publish_ack(command.command_id, Ack::Expired);
    return;
  }
  if (auto previous = completed_.find(command.command_id)) {
    publish_ack(command.command_id, previous->result); // 幂等重试
    return;
  }
  if (!work_.try_push(to_owned(command))) {
    publish_ack(command.command_id, Ack::Busy);
    return;
  }
  publish_ack(command.command_id, Ack::Accepted);
}
```

`Accepted` 只说明命令进入有界工作队列；执行完成应再发 `Succeeded/Failed`。发送方超时后重发相同 id，而不是生成新 id，否则接收端无法识别重复副作用。完成缓存需要容量和过期时间，避免长期运行无界增长。

## 故障定位

| 现象 | 重点 |
|---|---|
| spy 无 channel | Provider URL、multicast、sender 是否发布 |
| spy 有而应用无 | subscription regex/type、handle 是否被调用 |
| 小消息正常大消息丢 | MTU、分片、socket buffer、网络 loss |
| 延迟逐渐增大 | callback 慢、handle 不及时、队列 backlog |
| 回放无法解码 | schema/type fingerprint 与生成代码版本 |

## 关闭

停止生产者，通知 handle loop 退出，等待 callback/worker 完成，再析构 subscription、LCM 和 log 文件。若主循环阻塞在 handle，可使用带 timeout 的接口、fd event loop 或 provider notification 机制，而不是从另一线程直接释放正在使用的对象。

推荐把所有权集中在一个运行时对象：

```text
request_stop
  -> 发布者停止产生新消息
  -> handleTimeout 循环观察 stop 并退出
  -> unsubscribe（此后无新 callback）
  -> 关闭 worker queue 并 join worker
  -> flush/close log
  -> 析构 LCM handle
```

若 signal handler 参与关闭，它只设置原子标志或写 self-pipe；不要在异步信号上下文直接调用复杂 C++、锁、日志或 LCM API。

## 故障演练

1. 限制接收循环每秒只 handle 10 条，观察 subscription drop 与 age；
2. 发送刚好低于和高于分片阈值的 payload，比较完整消息成功率；
3. 丢弃某个 command ack，验证相同 id 重试不会重复执行；
4. 回放旧 schema 日志，验证 fingerprint mismatch 明确可见；
5. 回放过程中发送 SIGINT，验证日志关闭且文件仍可读取；
6. 切换错误 Provider URL，确认启动日志足以解释为什么 spy 看不到数据。

## 工程验收

- 满负载下 sequence gap、重组失败和队列 drop 可区分；
- 慢 subscriber 不会无界积压内存；
- 命令重发不会重复执行；
- log 回放可稳定复现业务输出；
- provider URL 和 schema commit 被记录；
- 网络断开与恢复不导致旧重组状态无限保留。
