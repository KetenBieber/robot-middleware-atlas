# PDO、SyncManager、FMMU 与 Process Image：把分散设备寄存器压成一块控制内存

固定参考实现：EtherLab / IgH EtherCAT Master 1.6.13，`61cc654f5b721ddd54df0f58bdd34106d91c5359`。

如果 EtherCAT 最难的概念只能挑一个，通常不是 Ethernet frame，而是这条链：

```text
Object Dictionary / PDO Entry
        ↓
PDO Mapping
        ↓
SyncManager
        ↓
FMMU
        ↓
Logical Address Space
        ↓
Master Process Image
```

这一篇从控制程序的内存需求出发，把它们逐层建立起来。

## 控制器想要的是变量，不是总线寄存器

机械臂控制代码希望写：

```cpp
actual_position = EC_READ_S32(pd + off_pos);
status_word     = EC_READ_U16(pd + off_status);

EC_WRITE_S16(pd + off_torque, target_torque);
```

这段代码非常“无聊”，而这恰恰是好事。1 kHz 周期路径不应该每次重新解析设备描述。

困难都应该被提前解决：

```text
off_pos 到底对应哪个 slave？
哪个 PDO？
PDO 在哪个 SyncManager？
Slave physical RAM 在哪里？
FMMU 如何映射？
这一段应该用 LRD、LWR 还是 LRW？
```

## PDO Entry：总线上真正有意义的数据字段

以伺服驱动为例，设备对象字典里可能有：

```text
0x6040 Controlword
0x6041 Statusword
0x6064 Position actual value
0x6071 Target torque
```

对象字典更像设备配置/语义数据库，不意味着这些对象天然都会出现在周期总线上。

PDO mapping 做的是：

> 从对象字典里挑出周期需要的数据，并规定它们按什么顺序打包。

例如：

```text
RxPDO:
  0x6040:00 16 bit
  0x6071:00 16 bit

TxPDO:
  0x6041:00 16 bit
  0x6064:00 32 bit
```

Rx/Tx 命名通常从从站视角理解：RxPDO 是 slave 接收，也就是 Master 输出；TxPDO 是 slave 发送，也就是 Master 输入。

机器人开发里这是一个很容易反过来的地方。

## SyncManager：不是“线程同步器”

EtherCAT 的 SyncManager（SM）是 ESC 中管理过程数据或 mailbox 缓冲区的硬件机制。

不要把它和操作系统 mutex、condition variable 混淆。

可以先这样理解：

```text
SM0/SM1  常用于 mailbox
SM2/SM3  常用于 process data
```

具体编号并非所有设备都必须完全一致，最终以设备 SII/配置为准。

SyncManager 关心的是：

- 从站物理内存区域；
- 长度；
- 方向；
- buffer/handshake 行为；
- watchdog 等。

PDO 被配置到某个 SyncManager 后，才拥有实际的从站物理数据承载区域。

## 为什么有 SyncManager 还不够

如果没有 FMMU，主站要控制 20 个从站，周期逻辑可能变成：

```text
read slave0 physical area
read slave1 physical area
read slave2 physical area
...
write slave0 physical area
write slave1 physical area
...
```

应用层很难得到统一连续内存，帧也会碎。

FMMU 的引入让主站可以定义：

```text
Master logical address 0x1000..0x1005
    -> Slave 0 physical SM3

Master logical address 0x1006..0x100b
    -> Slave 1 physical SM3
```

于是一个 LRD 访问逻辑 `0x1000..0x100b`，两个从站各自只处理属于自己的部分。

## Process Image：主站侧的“逻辑总线内存镜像”

Master 侧为 Domain 分配一块连续内存：

```text
domain->data

offset 0        Slave0 output
offset 4        Slave0 input
offset 12       Slave1 output
offset 16       Slave1 input
...
```

这块内存通常称 process image/process data memory。

它不是硬件共享内存；它是主站侧软件缓冲区，借助逻辑地址与 FMMU 映射，周期性地与各从站过程数据交换。

控制算法因此能把复杂网络交换降维成普通 load/store。

## Offset 为什么可以在初始化时算一次

IgH 的 PDO registration API 让应用传入 offset 指针：

```c
ec_pdo_entry_reg_t regs[] = {
    {
        .alias = 0,
        .position = 0,
        .vendor_id = VENDOR,
        .product_code = PRODUCT,
        .index = 0x6041,
        .subindex = 0,
        .offset = &off_status
    },
    {}
};
```

注册时主站找到目标 PDO entry，计算它在 Domain 内的字节位置，并把结果写进 `off_status`。

后续周期直接使用这个整数。

这是一个非常典型的实时设计：

```text
初始化：
  做名称/拓扑/对象搜索
  做链表遍历
  计算 offset
  构建映射

周期：
  pointer + offset
```

## bit_position 为什么必须单独存在

PDO entry 不一定按字节对齐。

例如：

```text
entry A: 1 bit
entry B: 1 bit
entry C: 6 bit
entry D: 16 bit
```

如果只返回 byte offset，就无法定位前 8 bit 内的具体字段。

所以 API 还允许返回 bit position。

IgH 固定实现里，如果 entry 没有字节对齐但调用者又没有提供 `bit_position` 指针，会直接报错。这种设计比静默向下取整安全得多。

## IgH 如何计算这个 Offset

固定代码 `ecrt_slave_config_reg_pdo_entry()` 的核心思路是：

```text
for each SyncManager:
    bit_offset = 0

    for each PDO:
        for each entry:
            if not target:
                bit_offset += entry.bit_length
            else:
                bit_pos = bit_offset % 8
                sync_offset = prepare_fmmu(...)
                return sync_offset + bit_offset / 8
```

这里的 `sync_offset` 不是从站物理地址，而是当前 SyncManager 对应 FMMU 在 Domain process image 里的逻辑偏移。

所以最终 offset 是：

```text
entry byte offset
  = FMMU logical offset inside Domain
  + entry bit offset / 8
```

## FMMU 构造时为什么会增长 Domain data_size

固定 `ec_fmmu_config_init()` 做了一个很关键的动作：

```c
fmmu->logical_start_address = domain->data_size;
fmmu->data_size = ec_pdo_list_total_size(...);

ec_domain_add_fmmu_config(domain, fmmu);
```

而 Domain 加入 FMMU 时会扩大 process data size。

这意味着配置期每加入一块新的 FMMU 映射，Domain 的逻辑地址空间就向后增长。

可以把它想成一个简单 bump allocator：

```text
domain data_size = 0

add FMMU A size 8
  A logical offset = 0
  data_size = 8

add FMMU B size 12
  B logical offset = 8
  data_size = 20
```

它不需要复杂的空闲区管理，因为稳定过程数据布局在 activate 前一次性构建，然后长期复用。

## Domain 为什么可以有多个

如果 process image 只是一个数组，为什么不全系统一个 Domain？

Domain 可以用来把过程数据按不同周期、不同调度组或应用边界组织。

例如：

```text
Domain fast:
  joint torque loop
  1 kHz

Domain slow:
  temperatures / diagnostics
  100 Hz
```

不同 Domain 可以分别 `process` 和 `queue`。

但要注意：多个 Domain 并不自动意味着多个独立网卡或物理总线。最终它们的 datagram 仍可能进入同一个 Master queue，由同一 Device 发送。

## Activate 是“配置模型”变成“周期数据结构”的冻结点

在 IgH 固定实现里，`ecrt_master_activate()` 会遍历所有 Domain，调用 `ec_domain_finish()`。

`ec_domain_finish()` 做三件非常关键的事：

- 为内部 process image 分配 `domain->data`；
- 修正每个 FMMU 的最终 logical base address；
- 把 FMMU 按最大 datagram payload 切成一个或多个 datagram pair，并计算 expected WKC。

也就是说，在 activate 之前：

```text
PDO/FMMU = configuration graph
```

activate 之后：

```text
Domain = concrete process memory
       + concrete logical addresses
       + concrete datagram pairs
       + expected WC
```

这就是为什么实时路径能够简单。

## 一个 Datagram 为什么可能覆盖多个 FMMU

假设：

```text
FMMU0  offset 0   size 8   output
FMMU1  offset 8   size 8   input
FMMU2  offset 16  size 16  input
```

只要总长度不超过 `EC_MAX_DATA_SIZE`，主站可以让一个逻辑 datagram 覆盖整个连续区域。

如果既有 input 又有 output，则可以选择 LRW。

如果只有 output，则 LWR。

只有 input，则 LRD。

IgH 在 `ec_datagram_pair_init()` 里预先决定这一点。

所以周期里不是：

```text
遍历 FMMU -> 判断方向 -> 新建 datagram
```

而是：

```text
遍历已经构建好的 datagram_pairs -> queue
```

这正是我们一直强调的“把结构推导前移到初始化期”。

## Process Image 的一致性边界在哪里

连续数组很方便，但不要自动推断为“线程安全快照”。

如果控制线程和另一个线程同时读写 `domain->data`：

```text
control thread: write torque
diagnostic thread: read same bytes
```

C/C++ 内存模型和 Linux 并不会因为它叫 process image 就自动提供同步。

Domain 解决的是**总线数据布局与交换**，不是应用多线程互斥。

同样，`ecrt_domain_process()` 和 `ecrt_domain_queue()` 的调用时序需要由应用周期明确管理。

## 从 Process Image 回到数据年龄

假设周期顺序是：

```text
receive
domain_process
read inputs
compute
write outputs
domain_queue
send
```

在时间语义上：

- 输入是“上一轮总线返回后刚被确认的 process data”；
- 输出是“本轮 compute 后准备在本次 send 发出的数据”。

如果应用在 `receive` 前就读 process image，可能读到更旧样本。

如果在 `queue` 后又修改 output，而 datagram 对主设备直接引用 process data，那么具体是否影响已排队数据要继续看实现的复制边界。

这类问题不能只靠 API 名称猜，需要进入源码。下一部分的 `domain-process-image` 和 `cyclic-send-receive` 会把它完全跑一遍。