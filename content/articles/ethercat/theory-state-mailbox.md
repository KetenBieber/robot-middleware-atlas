# AL 状态机与 Mailbox：为什么 EtherCAT 控制面不能塞进 1 kHz PDO 循环

固定参考实现：EtherLab / IgH EtherCAT Master 1.6.13，`61cc654f5b721ddd54df0f58bdd34106d91c5359`。

周期 PDO 给人的错觉是：EtherCAT 只要不停地收发 process data 就够了。真正的工业主站远比这复杂，因为从站必须被发现、识别、配置、切状态、做 mailbox 事务，还要在掉线后恢复。

这一篇专门把 **AL 状态机** 与 **Mailbox 协议** 的职责边界讲清楚。

## 为什么一个驱动器不能上电就直接收力矩命令

假设伺服驱动器刚通电，Master 立即写目标力矩。

此时至少有这些问题尚未解决：

- 从站是否已经被扫描到；
- station address 是否建立；
- SII 信息是否可用；
- mailbox SyncManager 是否配置；
- PDO assignment 是否与应用期望一致；
- FMMU 是否配置；
- watchdog 是否设置；
- Distributed Clocks 是否建立；
- 驱动当前是否允许进入 OP。

所以设备运行必须有生命周期。

EtherCAT Application Layer 常见状态：

```text
INIT
  ↓
PREOP
  ↓
SAFEOP
  ↓
OP
```

还可能出现 BOOT、ERROR/ACK 等相关状态。

这不是“软件枚举值好看”，而是在总线协议里明确不同阶段可以做什么。

## INIT、PREOP、SAFEOP、OP 应该怎样直觉理解

### INIT：先恢复到最小可控状态

INIT 阶段不应该假设正常 mailbox 或 PDO 已经可用。

主站常需要清理旧 SyncManager/FMMU 状态、重新建立基础配置。

可以把它理解成：

> 先把设备收敛到一个已知起点。

### PREOP：控制面准备好了，过程数据还没正式跑

PREOP 常用于 mailbox 通信和进一步参数配置。

例如：

```text
SDO write
PDO mapping configuration
parameter setup
device-specific initialization
```

此时特别适合 CoE 等 mailbox 协议工作。

### SAFEOP：输入可以工作，输出仍受安全约束

SAFEOP 的意义是让过程数据链路先建立并验证，但输出侧保持更安全的状态。

这是工业控制非常重要的思想：

> “通信已经通了”不等于“执行器应该立即动作”。

### OP：正式运行

OP 才是应用期望的完整周期 process data 状态。

但即使已经 OP，也不代表设备永远不会掉出来。链路错误、watchdog、配置变化或设备故障都可能让状态改变。

## 为什么状态切换必须检查，而不能直接写一个目标值就算结束

一个朴素函数可能是：

```c
write_al_state(OP);
return 0;
```

但写寄存器只是“提出请求”。

真实设备可能：

```text
request OP
  -> device still PREOP
  -> device configuring internally
  -> SAFEOP
  -> error
  -> needs ACK
```

所以主站必须：

1. 发状态请求；
2. 等 datagram 返回；
3. 周期性读 AL status；
4. 检查 timeout/error；
5. 只有状态达到目标才继续。

这天然就是状态机问题。

## 为什么不能用阻塞 while 等待

最容易写出的代码是：

```c
while (slave_state != OP) {
    send_read_state();
    sleep_ms(1);
}
```

如果这是初始化工具，也许还能工作。

如果它运行在 Master 的实时路径里，就很危险：

- 一个从站卡住会拖住所有其他从站；
- mailbox 超时可能是几十毫秒；
- sleep/wait 会破坏周期预算；
- 无法同时推进多个独立请求；
- shutdown 时很难安全打断。

所以工业主站更适合 **cooperative state machine**：

```text
exec():
    if current datagram still SENT/QUEUED:
        return

    switch(state):
        INIT:
            prepare next datagram
            state = WAIT_INIT
            return

        WAIT_INIT:
            inspect result
            state = NEXT
            ...
```

每次调用只做有限工作，不等待外部事件完成。

## “非阻塞状态机”到底是什么意思

这里的非阻塞不是说：

- 完全没有锁；
- 完全没有内核调度；
- 所有操作都是 lock-free。

它的核心语义是：

> 状态机不会为了某个远端从站响应，在一次 exec 调用里同步睡眠直到完成。

它把等待表示成 **对象状态**。

例如：

```text
fsm->state = wait_response
datagram->state = SENT
return
```

下一次调度再检查：

```text
if datagram still SENT:
    return

if RECEIVED:
    advance
if TIMED_OUT:
    retry/error
```

“时间”被展开到多次周期调用，而不是压在一个函数栈里。

## 为什么 Master FSM 与 Slave Config FSM 要分层

Master 面临全局问题：

```text
多少个 slave 在线？
是否需要 rescan？
哪个 slave 下一步需要检查？
配置是否改变？
是否有外部 request？
```

单个 Slave Config 则关心：

```text
这个 slave 怎样从 INIT 配到 OP？
mailbox SM 如何配置？
PDO/FMMU 怎样写入？
watchdog 怎样写？
DC 怎样设置？
```

如果全部塞进一个巨型 switch：

```text
master_state × slave_index × slave_config_state × mailbox_state
```

组合状态会迅速爆炸。

分层 FSM 等价于把复杂状态空间分解：

```text
Master FSM
  chooses slave / global action
      |
      v
Slave Config FSM
  executes one slave configuration
      |
      +-> Change FSM
      +-> CoE FSM
      +-> SoE FSM
      +-> PDO FSM
      +-> EoE FSM
```

这是比“状态模式”标签更重要的设计原因。

## Mailbox 为什么存在

PDO 适合：

- 小；
- 周期；
- 固定布局；
- 高频。

但工业设备还需要大量非周期操作：

- 参数读写；
- firmware/文件；
- 诊断；
- servo parameter；
- 对象字典查询；
- 厂商特定数据。

不可能把所有内容都塞进 1 kHz PDO。

所以 EtherCAT 还提供 mailbox 通道。

可以把它类比为：

```text
PDO      = 实时数据面
Mailbox  = 管理/配置事务面
```

## CoE、SoE、FoE、EoE 分别解决什么

### CoE：CAN application protocol over EtherCAT

CoE 最常见。

它让 EtherCAT 从站暴露类似 CANopen 的 Object Dictionary 和 SDO/PDO 语义。

伺服领域大量对象如 0x6040/0x6041 都来自 CiA 402 生态。

### SoE：Servo over EtherCAT

SoE 面向 IEC 61800-7/SERCOS 风格的驱动参数访问。

### FoE：File over EtherCAT

适合文件传输、固件等。

### EoE：Ethernet over EtherCAT

把 Ethernet 数据封装进 EtherCAT mailbox，用于需要 IP/Ethernet 语义的设备功能。

这些协议虽然都“走 mailbox”，事务状态和报文格式却不同，因此主站通常为它们分别实现状态机。

## SDO 为什么不适合直接放在硬实时控制线程

一个 SDO read 可能经历：

```text
prepare mailbox request
  -> send
  -> wait slave mailbox
  -> check mailbox
  -> fetch response
  -> segmented transfer?
  -> decode abort code
```

如果对象很大，还可能分段。

这与 PDO 的期望完全不同：

```text
PDO:
  bounded layout
  repeated every cycle

SDO:
  request/response transaction
  variable progress
  timeout/retry
```

所以工程上常把：

```text
hard/soft realtime loop:
    PDO only

configuration/service thread:
    SDO/mailbox requests
```

主站内部则用 FSM 把这些异步请求逐步推进。

## Mailbox 与 SyncManager 的关系

Mailbox 本身也要有物理 buffer。

常见从站会用一对 SyncManager 管理：

```text
Master -> Slave mailbox
Slave  -> Master mailbox
```

状态机在 PREOP 前后要先配置这些 SyncManager，后面的 CoE/SoE 才有承载通道。

因此“SDO 失败”有时根本不是对象字典问题，而可能更早：

- mailbox SM 未配置；
- slave 不在允许 mailbox 的状态；
- mailbox 正忙；
- WKC 不对；
- response timeout。

源码阅读时必须沿层次排查，不能看到 SDO 就直接钻进 CoE parser。

## 状态机如何与 Datagram 共用数据面

FSM 自己并不拥有网卡。

它做的是：

```text
state decides:
    create/configure datagram
    return

Master send:
    picks queued FSM datagram
    sends with other datagrams

Master receive:
    updates datagram state

next FSM exec:
    consumes result
```

这是一种很漂亮的分层：

- FSM 负责“下一步要做什么”；
- Datagram 负责“这一步怎样变成 EtherCAT 命令”；
- Master queue 负责“怎样和其他工作一起发出去”；
- Device 负责“怎样上网卡”。

## 配置流量会不会干扰实时 PDO

答案不是简单的“不会”。

如果 mailbox/FSM datagram 与 process data 共用：

- Master queue；
- Ethernet frame；
- NIC；
- 链路带宽；

它们理论上都可能增加负载。

好的主站要控制：

- 外部 datagram 队列大小；
- 每周期注入多少非应用 datagram；
- frame packing 顺序；
- mailbox 事务推进速率。

IgH 里专门有 external datagram ring、FSM datagram injection sequence 等机制，就是为了把不同来源接进同一数据面时仍保持可控。

后面源码页会逐层看这些边界。

## 掉线恢复为什么也是状态机问题

一个从站运行中掉线：

```text
OP
  -> link/topology change
  -> WKC changes
  -> rescan
  -> slave objects rebuilt/reattached
  -> configuration replay
  -> PREOP/SAFEOP
  -> OP
```

这不是一次函数调用能解决的。

更重要的是，在恢复过程中应用看到的 process image 可能仍有旧值。

所以控制系统不能只问“Master 是否正在恢复”，还要建立：

```text
data freshness
WKC
slave state
drive status
command fail-safe
```

的联合判定。

## 对机器人控制最重要的结论

PDO 与 mailbox 不是“两个 API 类别”，而是两种完全不同的时间语义。

```text
PDO:
  fast
  cyclic
  bounded
  latest process state

Mailbox:
  transactional
  asynchronous
  retry/timeout
  configuration/service
```

如果把 mailbox 事务直接混进硬实时控制线程，等于主动把不确定延迟带进 deadline。

而状态机的意义，就是让这些慢事务能继续前进，同时不把整个主站执行栈卡在等待上。

进入源码后，我们会重点看 `ec_fsm_master_t`、`ec_fsm_slave_config_t` 和它们共用的 datagram 如何完成这套分层。