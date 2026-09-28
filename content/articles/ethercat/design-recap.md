# IgH EtherCAT Master 设计总复盘：如果从零写一个主站，这些抽象为什么会按这个顺序长出来

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

前面已经分别拆过协议、Domain、FMMU、Datagram、FSM、Device、DC 与实时并发。这一篇不再增加新名词，而是把整个主站重新从零推一遍。

目标不是背 IgH 的文件结构，而是回答：

> 如果今天没有 IgH，我们自己要为一台 1 kHz 机器人控制器设计 EtherCAT Master，哪些对象会因为哪些失败被一步步逼出来？

## 第 0 版：控制线程直接发 raw Ethernet frame

最天真的系统只有：

```text
control loop
  build frame
  send NIC
  wait RX
  parse
```

伪代码：

```c
while (1) {
    build_frame(tx);
    nic_send(tx);
    nic_wait_receive(rx);
    parse(rx);
    control();
}
```

它很快遇到四个问题：

1. 每周期都重新计算从站地址与字段布局；
2. 同一个帧里既有过程数据又有管理请求，难以组织；
3. mailbox/状态切换会等待几十毫秒；
4. 应用算法被 EtherCAT 协议细节完全污染。

所以第一层抽象不是“类”，而是**把稳定数据面与动态控制面分开**。

## 第 1 版：PDO 不能让应用每周期自己找

控制算法真正想写的是：

```c
pos = READ_S32(pd + off_pos);
WRITE_S16(pd + off_tau, tau);
```

而不是：

```c
find_slave(3);
find_pdo(0x1a00);
find_entry(0x6064, 0);
serialize();
```

于是必须有：

```text
PDO entries
    ↓ configuration-time compilation
Process Image
```

这就是 Domain 出现的第一性原理。

## 第 2 版：Process Image 只是主机内存，还需要 FMMU

主机把所有变量排成连续数组以后，从站并不知道：

```text
logical byte 24
```

对应自己哪个 SyncManager 物理区间。

于是需要：

```text
logical process image
    ↓
FMMU mapping
    ↓
slave physical process RAM
```

IgH 在配置期通过 `ec_fmmu_config_t` 保存这种映射意图。

`ec_fmmu_config_init()` 的关键逻辑：

~~~c
fmmu->logical_start_address = domain->data_size;
fmmu->data_size = ec_pdo_list_total_size(
        &sc->sync_configs[sync_index].pdos);

ec_domain_add_fmmu_config(domain, fmmu);
~~~

这一步很漂亮：新的 FMMU 直接接在当前 Domain 尾部，因此配置阶段就逐步形成逻辑过程映像。

## 第 3 版：映射完成后还不能每周期重新拼协议

即使 logical address 已确定，如果每周期仍然：

```text
walk all FMMUs
choose command
split MTU
allocate request
```

实时成本仍然太大。

所以 `activate()` 必须成为 phase barrier。

固定源码：

~~~c
domain_offset = 0;
list_for_each_entry(domain, &master->domains, list) {
    ret = ec_domain_finish(domain, domain_offset);
    ...
    domain_offset += domain->data_size;
}
~~~

而 `ec_domain_finish()` 又会：

~~~c
if (datagram_size + fmmu->data_size > EC_MAX_DATA_SIZE) {
    ec_domain_add_datagram_pair(...);
    ...
}
~~~

于是配置图被编译成：

```text
FMMU list
    ↓ activate
bounded datagram pairs
    ↓
reused every cycle
```

这和编译器“IR → executable layout”的思想非常接近。

## 第 4 版：Datagram 不能只是一段字节

如果 datagram 只是：

```c
struct {
    uint8_t bytes[1500];
};
```

主站无法知道它现在：

- 等待入队；
- 已发出；
- 已收到；
- 超时；
- 失败；
- 属于哪个 device；
- WKC 是多少。

于是 `ec_datagram_t` 实际是一个**长期事务对象**。

它同时包含：

```text
protocol command/address
payload ownership
wire transaction index
working counter
state
timestamps
intrusive list nodes
```

这让同一个对象从“待发送任务”一路活到“完成记录”。

## 第 5 版：发送队列为什么也是 in-flight table

普通消息队列的典型模型：

```text
push
pop
object leaves queue
```

IgH 的 Master datagram queue 不是这样。

它既保存：

- QUEUED；
- SENT；

直到 response parser 匹配并完成对象。

所以更准确地说：

> `datagram_queue` 是“本轮待发 + 当前在途事务集合”。

这就是为什么 `ecrt_master_receive()` 还会遍历它找超时的 SENT datagram。

数据结构名称如果只按 API 习惯理解，很容易误读。

## 第 6 版：管理事务不能同步等待

EtherCAT 主站不仅发 PDO。

它还必须：

- 扫描从站；
- 配置 AL state；
- 读取 SII；
- 做 CoE/SDO；
- 配置 PDO；
- 写 FMMU；
- 配置 DC；
- 做错误恢复。

任何一个过程都可能跨多个网络往返。

同步写法：

```c
configure_slave()
{
    send();
    wait();
    send();
    wait();
    ...
}
```

会让线程长期阻塞。

所以 IgH 选择 C 风格函数指针 FSM：

```c
void (*state)(ec_fsm_master_t *);
```

一次只推进一步。

datagram 未完成时直接 return。

这不是为了炫耀“状态模式”，而是由异步总线事务逼出来的控制流结构。

## 第 7 版：后台 FSM 不能直接抢 RT 发送队列

有了 Operation kthread 后，又出现新的并发问题。

如果 FSM thread 和 RT thread 都直接操作主发送队列，需要一把跨线程锁。

这会把后台复杂度传给周期线程。

IgH 的解决方式是：

```text
OP thread prepares fsm_datagram
    ↓ release sequence
RT send thread observes sequence
    ↓
RT thread queues the datagram itself
```

于是发送队列写入责任更集中。

这比“两个线程都锁住 queue 然后随便 push”多了一套协议，但实时边界更清楚。

## 第 8 版：网卡发送不能每帧 malloc skb

数据已经完全准备好，如果 NIC 层仍然每周期分配发送 buffer，之前所有工作都白做了。

所以 Device 使用固定 TX ring：

```text
tx_skb[2]
```

这体现一个非常值得迁移到机器人系统的原则：

> 热路径对象集合如果上界固定，优先使用固定容量、预分配、可复用结构。

它常见于：

- CAN frame pool；
- LiDAR packet pool；
- RT command buffer；
- inference input/output workspace；
- lock-free ring slots。

## 第 9 版：周期实时和设备同步不是同一件事

即使控制线程严格每 1 ms 醒来，从站内部仍可能各用各的晶振。

所以最后还需要 DC：

```text
host application time
    ↓
reference slave
    ↓
other DC slaves
    ↓
Sync0/Sync1 event
```

这一步把“软件什么时候算完”进一步连接到“设备硬件什么时候真正采样与执行”。

到这里主站才真正从网络程序变成工业控制运行时。

# 从对象图重新看整个系统

现在可以画出完整 mental model：

```text
Application RT loop
    |
    | ecrt_* userspace API
    v
libethercat
    |
    | ioctl + mmap
    v
+--------------------------------------------------+
| Kernel Master                                    |
|                                                  |
|  lifecycle / config                              |
|    Master ---- Slave Config ---- Domain          |
|      |              |             |              |
|      |              +--> PDO/FMMU +--> image     |
|      |                            |              |
|      |                            +--> datagrams  |
|      |                                           |
|  control plane                                   |
|    Master FSM -> Slave FSM -> CoE/SoE/etc.       |
|      |                                           |
|      +---- fsm_datagram --handoff----+           |
|                                      |           |
|  cyclic data plane                  v           |
|    Domain datagrams ----------> datagram_queue   |
|                                      |           |
|                                  frame packing   |
|                                      |           |
|                                  Device/NIC      |
+--------------------------------------|-----------+
                                       v
                                  EtherCAT wire
                                       |
                            FMMU / SM / mailbox / DC
                                       |
                                    slaves
```

## Ownership 再整理一次

### Master

系统 root object，拥有或关联：

- Device；
- Domain list；
- Slave Config list；
- Slave inventory；
- Master FSM；
- reusable special datagrams；
- Master thread。

### Domain

拥有：

- FMMU config list；
- process-image region；
- datagram-pair list；
- working-counter state。

### Slave Config

代表应用期望配置，不等同于当前扫描到的 `ec_slave_t`。

### Datagram

长期事务对象；payload 可能指向 Domain data，也可能拥有独立 internal memory。

### Device

连接 Master 与 `net_device`/driver。

### FSM

不是线程，而是由线程反复调用的可暂停控制流对象。

这个区分非常重要：

```text
object != thread
state machine != thread
domain != queue
datagram != frame
process image != slave physical RAM
```

## 最值得借鉴的数据结构选择

### intrusive list

适合：

- 生命周期由对象自身管理；
- 对象需要同时进入多个链表；
- 不能为 wrapper node 再分配内存；
- 经常已知对象指针后 O(1) remove。

代价是：

- pointer chasing；
- cache locality 一般；
- duplicate insertion 风险高；
- 遍历 O(n)。

IgH 用它组织 configs、domains、FMMU、datagram queue 等动态但有界集合。

### fixed array

用于：

- devices；
- TX skb ring；
- 某些预分配特殊对象。

当最大数量已知时，固定数组比通用链表更容易给出容量与实时上界。

### process-image byte buffer

用于高频数据。

核心优势不是“好看”，而是让周期访问退化为：

```text
base pointer + offset
```

这比对象树查找稳定得多。

## Linux 内核机制里最值得学的几处

这个专题不是 Linux 内核课程，但 IgH 恰好把很多基础设施用在真实控制系统里：

- `struct list_head`：intrusive list；
- `container_of` 风格对象恢复；
- `kmalloc(..., GFP_KERNEL)`：配置期对象分配；
- semaphore：复杂共享对象图保护；
- `rt_mutex`：I/O 互斥；
- `kthread_create` / `kthread_stop`；
- `smp_load_acquire` / `smp_store_release`；
- `struct sk_buff`；
- `struct net_device`；
- module reference counting；
- mmap + character-device ioctl。

这些机制不是散乱的 API。

它们分别回答：

```text
对象怎么组织？
对象怎么活着？
线程怎么停？
CPU 之间怎么发布状态？
网卡 buffer 谁拥有？
用户态怎样看到内核过程映像？
```

## 对机器人控制最关键的五个指标

读完源码后，不应该只问“能不能通信”。

至少要问：

### 1. data age

控制算法读到的数据距离实际采样过去多久。

### 2. output age

算法写下的命令什么时候真正被驱动器锁存。

### 3. jitter

上述两个时间关系每周期波动多少。

### 4. failure detection latency

WKC、link down、AL state error 多久被上层观察到。

### 5. recovery interference

扫描、mailbox、重新配置时，正常周期数据是否受到影响。

这些才是 EtherCAT 对运动控制的核心价值与风险。

## IgH 的几个明显取舍

### 优点：周期数据路径非常“编译化”

配置阶段复杂，运行阶段复用预生成结构。

这是实时系统常用且有效的思路。

### 优点：控制面用 FSM 拆成小步

避免长网络事务同步阻塞。

### 优点：直接进入 Linux net_device 层

减少普通 socket/IP stack 的不必要层级，并能和专用 EtherCAT driver 配合。

### 代价：实现高度依赖 Linux 内核机制

学习门槛更高，版本适配成本也更明显。

### 代价：intrusive list 与共享长期对象要求严格生命周期纪律

重复入队、错误状态重置、shutdown 顺序都可能造成难查问题。

### 代价：实时保证仍需要整机工程配合

Master 的热路径设计得再好，也不能替代：

- 正确调度策略；
- CPU/IRQ 隔离；
- 合适 NIC driver；
- 有界控制算法；
- 内存与日志纪律。

## 如果自己写缩小版主站，建议按这个顺序

不要一开始实现 CoE、DC、冗余。

先做：

```text
1. one NIC
2. one datagram object
3. queue + index + state
4. frame serialize/parse
5. timeout
6. one slave physical read/write
7. logical addressing
8. process image
9. fixed cyclic loop
10. working counter
```

然后再加：

```text
11. slave config model
12. FMMU compilation
13. master/slave FSM
14. mailbox
15. DC
16. redundancy
17. diagnostic/recovery
```

这个顺序和 IgH 最终源码结构并不一一相同，但符合依赖关系。

如果反过来先做“完整对象体系”，很容易写出大量 class/struct，却没有一条可验证的数据链。

## 本专题最终应该留下的能力

完成这一组文章以后，读者应该能够：

- 解释 EtherCAT 为什么不是“快一点的 Ethernet”；
- 画出 PDO → FMMU → Domain process image → Datagram → Frame → Device/NIC 链；
- 区分 Master phase 与 Slave AL state；
- 解释 mailbox 为什么必须状态机化；
- 根据 `ec_datagram_t` 理解一次 wire transaction 生命周期；
- 判断一个内核数据结构是否位于实时热路径；
- 解释 RT application thread 与 Operation kthread 的责任边界；
- 解释 DC 与 Linux RT scheduling 的不同；
- 从 Working Counter、timeout、link state 推导故障可观测性；
- 把这些设计原则迁移到自己的机器人通信与控制运行时。

最重要的是，不再把 `ecrt_master_send()` 理解为“发个包”。

真正发生的是：

```text
process-image state
    ↓
precompiled Domain datagrams
    ↓
mixed with bounded control-plane work
    ↓
frame packing
    ↓
preallocated Device TX buffers
    ↓
NIC
    ↓
on-the-fly slave processing
    ↓
returned frame
    ↓
datagram completion + WKC
    ↓
process image becomes next control state
```

这条闭环，才是 IgH EtherCAT Master 源码最值得掌握的主线。
