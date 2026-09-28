# EtherCAT 软件栈总图：从 1 kHz 控制问题反推主站为什么必须分层

本文先不读 IgH 的具体函数，而是先回答一个更基本的问题：**一台 Linux 工控机为什么需要一整套 EtherCAT Master 软件栈，不能让控制程序直接往网卡里写几个字节？**

后续源码固定参考 EtherLab / IgH EtherCAT Master stable-1.6，版本 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。这一页讨论的重点是 EtherCAT 软件栈的职责边界；具体实现会在后半课程逐个落到这个固定提交。

## 从一个机械臂周期开始，而不是从协议名词开始

假设 7 轴机械臂以 1 kHz 运行。每个 1 ms 周期大致需要：

```text
t_k:
    读取 7 个关节的位置 / 速度 / 力矩 / 状态字
    -> 状态估计
    -> 控制律
    -> 写入 7 个目标力矩 / control word
    -> 等待下一周期
```

如果只看算法，这像一块共享数组：

```cpp
struct JointIo {
    int32_t position;
    int32_t velocity;
    int16_t torque;
    uint16_t status_word;
    int16_t target_torque;
    uint16_t control_word;
};

JointIo joints[7];
```

但物理世界里，这些字段分散在 7 个驱动器内部的寄存器、SyncManager、PDO 和本地时钟上。控制程序看到的连续内存，与总线真实交换的 Ethernet frame 之间至少还缺五层工作：

```text
控制变量
  ↓
PDO entry / process image
  ↓
FMMU logical addressing
  ↓
EtherCAT datagram
  ↓
EtherCAT Ethernet frame
  ↓
NIC + wire + slave ESC
```

主站软件栈的核心价值，就是把上面五层之间的映射**在配置期建立好，在周期期低成本重复使用**。

## EtherCAT 与普通 Ethernet 的根本差别不在“更快”

100 Mbit/s 的 Ethernet 本身并不神奇。真正决定 EtherCAT 控制特性的，是它改变了设备交换数据的方式。

普通基于 IP 的通信更像：

```text
Master -> packet to slave A -> reply
Master -> packet to slave B -> reply
Master -> packet to slave C -> reply
```

随着从站数增加，包头、协议栈、排队和往返次数都会重复。

EtherCAT 的基本思想更接近：

```text
Master sends one frame
      |
      v
 [Slave A ESC] -- frame continues -->
      | read/write relevant bytes on the fly
      v
 [Slave B ESC] -- frame continues -->
      |
      v
 [Slave C ESC]
      |
      +---- frame returns to master
```

从站的 EtherCAT Slave Controller（ESC）在帧经过时，根据 datagram 的寻址方式对相应数据做读写，然后帧继续向后传播。主站因此可以把多个设备的数据交换压进少量 frame。

这里要特别避免一个误解：**EtherCAT 并不是“所有 slave 共享一块真的物理 RAM”**。所谓 process image 是主站和逻辑寻址层给应用制造出的连续视图。底层仍然是各从站自己的物理地址空间，再由 FMMU 做逻辑地址映射。

## 软件栈第一层：应用 API 只描述意图

控制应用不应该知道：

- 当前 PDO 数据被拆成几个 datagram；
- 某个 datagram 在 Ethernet frame 的第几个位置；
- 本周期网卡用了哪个 skb；
- 某个从站的物理 SyncManager 起始地址；
- Working Counter 应该加多少；
- DC 同步帧何时排队。

应用真正需要的是稳定的控制语义：

```c
master = ecrt_request_master(0);
domain = ecrt_master_create_domain(master);

/* configure slaves / PDOs */

ecrt_master_activate(master);
process_data = ecrt_domain_data(domain);

for (;;) {
    ecrt_master_receive(master);
    ecrt_domain_process(domain);

    /* read process_data */
    /* compute control */
    /* write process_data */

    ecrt_domain_queue(domain);
    ecrt_master_send(master);
}
```

这套 API 的设计非常关键：**配置期 API 与周期 API 被明显分开**。

配置期允许建立对象、扫描 PDO、构建 FMMU、分配内存；周期期则反复操作已经冻结的结构。实时系统里最重要的工程方法之一，就是把不可预测成本从周期路径前移。

## 软件栈第二层：Slave Config 是“我希望这个从站变成什么样”

一个物理从站当前是什么状态，与应用希望它配置成什么状态，是两件事。

主站需要保存一份期望配置，例如：

```text
Slave Config
  alias / position
  vendor_id / product_code
  SyncManager configuration
  PDO assignment
  PDO mapping
  FMMU requirements
  SDO startup configuration
  DC Sync0 / Sync1
  watchdog
```

为什么不直接在应用初始化时同步写完所有寄存器？

因为设备可能：

- 此刻还没上线；
- 从 PREOP 切 SAFEOP 需要等待；
- mailbox 请求必须等待响应；
- 链路中途断开又恢复；
- 配置过程中某个 datagram 超时；
- 运行时重新扫描到设备。

所以“期望配置”必须被保存成对象，而真正把配置逐步施加到从站，要交给状态机。

这也是后面 IgH 中 `ec_slave_config_t` 和 `ec_slave_t` 必须分开的原因：前者属于应用配置模型，后者属于当前总线观测到的实体。

## 软件栈第三层：Domain 把离散 PDO 变成控制算法可读的连续内存

应用希望的是：

```text
byte 0..3    joint0.position
byte 4..5    joint0.torque
byte 6..7    joint0.status
byte 8..11   joint1.position
...
```

但真实从站侧可能是：

```text
Slave 0:
  SM2 outputs -> physical 0x1000...
  SM3 inputs  -> physical 0x1100...

Slave 1:
  SM2 outputs -> physical 0x1200...
  SM3 inputs  -> physical 0x1300...
```

Domain 的任务不是“保存一堆消息”，而是组织一个 **process image**：把多个从站的 PDO 映射到逻辑地址空间，再让应用通过 offset 直接读写字节。

这个抽象非常适合周期控制，因为在 steady state 中控制算法不需要：

```cpp
find_slave();
find_pdo();
find_entry();
serialize();
send_request();
wait_reply();
```

而只需要：

```cpp
position = EC_READ_S32(domain_pd + off_position);
EC_WRITE_S16(domain_pd + off_target_torque, target);
```

这是一种典型的“配置期复杂、周期期简单”的设计。

## 软件栈第四层：FMMU 解决逻辑地址与从站物理地址的错位

如果 Domain 只是连续内存，还缺一个问题：

> 主站逻辑地址 `0x00001020` 的这 4 个字节，为什么会落到第 3 个从站的某个 SyncManager？

EtherCAT 的 FMMU（Fieldbus Memory Management Unit）承担这个映射。

可以把它先理解成一个从站侧的小型地址转换表：

```text
logical [L, L+n)
     |
     | FMMU mapping
     v
slave physical [P, P+n)
```

FMMU 使主站能发送 Logical Read / Logical Write / Logical ReadWrite，而不用每周期为每个从站分别构造物理地址请求。

这和操作系统 MMU 的思想有相似之处：上层使用统一地址空间，底层由映射机制决定真实落点。但两者用途完全不同；EtherCAT FMMU 不是虚拟内存页表，也不提供进程隔离。

## 软件栈第五层：Datagram 是协议工作的最小单位

一个 Ethernet frame 可以承载多个 EtherCAT datagram。Datagram 才是“读哪里、写哪里、多少字节”的具体命令。

IgH 固定版本中列出的 datagram 类型包括：

```text
APRD / APWR / APRW    自动递增物理寻址
FPRD / FPWR / FPRW    配置地址物理寻址
BRD  / BWR  / BRW     广播
LRD  / LWR  / LRW     逻辑寻址
ARMW / FRMW            Read Multiple Write 类命令
```

周期过程数据最值得关注的是 LRD/LWR/LRW，因为 Domain + FMMU 已经把各从站 PDO 放进逻辑地址空间。

为什么还要有物理寻址和广播？

因为主站在“还不知道这是谁”时，就不能依赖已经配置好的逻辑映射。扫描、读取状态、分配 station address、配置寄存器等控制面工作，需要另一套寻址方式。

所以 EtherCAT 软件栈天然分成：

```text
稳定数据面：
    Domain -> logical datagram -> PDO

发现 / 配置 / 诊断控制面：
    physical/broadcast datagram -> slave registers/mailbox
```

## 软件栈第六层：Master 负责把不同来源的 Datagram 汇成真实链路

在一个周期里，待发 datagram 可能来自：

- Domain 过程数据；
- Master FSM；
- Slave 配置 FSM；
- SDO/mailbox 请求；
- DC 同步；
- 用户异步寄存器请求；
- 冗余链路。

如果每个模块都直接调用网卡，发送顺序、帧利用率、并发和状态匹配都会失控。

因此需要 Master 层作为统一调度点：

```text
Domain datagrams --------FSM datagram -------------DC datagrams --------------> Master datagram queue
external requests --------/           |
                                      v
                              frame packing
                                      |
                                      v
                                  Device/NIC
```

这不是普通线程池调度器。它主要调度的是“哪个 EtherCAT datagram 在何时被组织进哪一个 frame”。

## 软件栈第七层：Device/NIC 是实时性不能继续抽象掉的边界

很多中间件文章读到 `send()` 就结束了，但 EtherCAT 不能。

控制周期真正面对的是：

```text
application
  -> master queue
  -> frame packing
  -> tx buffer
  -> NIC driver
  -> DMA / MAC
  -> wire
  -> slaves
  -> return path
  -> NIC poll
  -> frame parse
  -> process image
```

如果这一层有动态分配、长队列、不可控中断延迟或普通网络栈竞争，前面所有“1 kHz”都只是平均频率。

IgH 之所以值得源码级学习，就是它把 Device 抽象、专用网卡驱动路径、TX skb ring、主动 poll 等细节直接摆在实现里，让我们可以继续追到 Linux net_device 边界。

## 软件栈第八层：FSM 让慢配置事务不阻塞周期交换

SDO、SII、AL 状态切换、PDO 重配置不可能都在一个函数调用里同步完成。

如果写成：

```c
configure_slave() {
    send_request();
    while (!reply) {
        wait();
    }
}
```

一个从站响应慢 20 ms，就能把 1 ms 控制周期卡住 20 次。

正确思路是：

```text
cycle k:
  state A -> prepare datagram -> return

cycle k+1:
  datagram received?
      no  -> return
      yes -> state B -> prepare next datagram -> return
```

状态机把“长事务”拆成很多短步骤，让每次执行只推进有限工作。这是 EtherCAT 主站源码里最重要的控制流设计之一。

## 软件栈第九层：Distributed Clocks 处理的不是吞吐，而是相位

即便 Master 每隔 1 ms 发送一次，多个驱动器也可能在不同瞬间采样。

例如：

```text
Slave A sample: 0 us
Slave B sample: 80 us
Slave C sample: 160 us
```

控制算法收到的是同一轮 frame，却不代表三个观测属于同一个物理时刻。

Distributed Clocks 的目标是建立设备间共享时间基准，并通过 Sync0/Sync1 等事件让采样/输出发生在可控相位。

所以实时控制要区分三件事：

- **周期频率**：多久跑一次；
- **jitter**：实际时刻偏离理想周期多少；
- **phase alignment**：不同设备的采样/执行时刻是否对齐。

## 一张图收束整个软件栈

```text
┌──────────────────────────────────────────────┐
│ Control Application                          │
│ offsets + control law + 1 kHz loop           │
└──────────────────────┬───────────────────────┘
                       │ ecrt_* API
┌──────────────────────v───────────────────────┐
│ Application Configuration                    │
│ Slave Config / Domain / PDO / DC             │
└──────────────┬──────────────────┬────────────┘
               │                  │
        process image       state/mailbox requests
               │                  │
┌──────────────v──────────┐  ┌────v───────────────┐
│ FMMU / logical mapping │  │ Master/Slave FSM   │
└──────────────┬──────────┘  └────┬───────────────┘
               └──────────┬────────┘
                          v
                 Datagram objects/queue
                          |
                     frame packing
                          |
┌─────────────────────────v────────────────────┐
│ Device / NIC / driver                         │
└─────────────────────────┬────────────────────┘
                          |
                    EtherCAT wire
                          |
             ESC + FMMU + SM + mailbox + DC
```

后面五篇理论文章会分别拆开这里最容易混淆的几层，然后再进入 IgH 源码。