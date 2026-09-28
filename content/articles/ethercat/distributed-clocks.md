# Distributed Clocks 源码：应用时间、参考时钟与从站时钟怎样进入同一条周期链

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

这一篇只回答一个控制系统问题：

> 1 kHz 控制线程“每 1 ms 醒一次”以后，为什么还需要 EtherCAT Distributed Clocks？主机定时器已经够准了吗？

答案是否定的。主机线程何时醒来，只决定**控制程序什么时候执行**；而伺服驱动内部何时锁存输入、何时更新输出，还受各从站本地时钟影响。Distributed Clocks（DC）的目标是让这些硬件时钟在同一时间基准下运行，并把周期事件对齐到确定的相位。

## 先区分三种时间

在 EtherCAT 控制系统里，至少存在三种不同的时间：

```text
Linux application time
    控制线程认为“现在是多少”

DC reference clock
    被选为总线参考的某个支持 DC 的从站时钟

other slave clocks
    其他支持 DC 的从站本地系统时间
```

如果把它们混成一个“时间”，很多 API 就会显得莫名其妙。

例如：

```c
ecrt_master_application_time(master, app_time);
ecrt_master_sync_reference_clock(master);
ecrt_master_sync_slave_clocks(master);
```

这三个调用并不是重复动作。

第一步只是把**应用侧时间样本**告诉 Master。第二步生成一个“参考时钟向应用时间靠拢”的 EtherCAT 写事务。第三步生成一个“其他从站向参考时钟靠拢”的广播式同步事务。

所以方向是：

```text
application clock
      |
      | sync reference
      v
DC reference slave
      |
      | sync slaves
      v
other DC slaves
```

## 参考时钟为什么必须先选出来

固定源码中的 `ec_master_find_dc_ref_clock()`：

~~~c
void ec_master_find_dc_ref_clock(ec_master_t *master)
{
    ec_slave_t *slave, *ref = NULL;

    if (master->dc_ref_config) {
        slave = master->dc_ref_config->slave;

        if (slave) {
            if (slave->base_dc_supported && slave->has_dc_system_time) {
                ref = slave;
            }
        }
    }
    else {
        for (slave = master->slaves;
                slave < master->slaves + master->slave_count;
                slave++) {
            if (slave->base_dc_supported && slave->has_dc_system_time) {
                ref = slave;
                break;
            }
        }
    }

    master->dc_ref_clock = ref;
    ...
}
~~~

这里有两种策略：

1. 应用显式指定 `dc_ref_config`；
2. 否则选第一个真正支持 DC system time 的从站。

这说明参考时钟不是一个抽象软件对象，而是**实际总线上的某个从站 ESC 时钟**。

如果没有符合条件的从站：

```c
master->dc_ref_clock = NULL;
```

后续同步 API 会返回错误，而不是假装“DC 已启用”。

## 为什么 reference sync datagram 是预分配对象

选好参考时钟后，源码立即把两个长期 datagram 配好：

~~~c
ec_datagram_fpwr(&master->ref_sync_datagram,
        ref ? ref->station_address : 0xffff, 0x0910, 4);

ec_datagram_frmw(&master->sync_datagram,
        ref ? ref->station_address : 0xffff, 0x0910, 4);
~~~

这里要注意的是：DC 周期同步不需要每个控制周期重新 `kmalloc` 一个请求对象。

Master 内部长期持有：

```text
ref_sync_datagram
sync_datagram
sync_mon_datagram
```

周期里只是更新 payload、清状态、重新入队。

这和 Domain datagram 的设计是同一种实时思路：

> 配置期准备对象，周期期复用对象。

如果每个 1 ms 周期都做动态分配：

```c
request = kmalloc(...);
prepare_dc_request(request);
queue(request);
```

即使平均只花几微秒，allocator 锁竞争、cache miss 与回收路径仍会制造不必要的 jitter。

## application_time 只是写一个 Master 字段

固定实现：

~~~c
int ecrt_master_application_time(ec_master_t *master, uint64_t app_time)
{
    master->app_time = app_time;

    if (unlikely(!master->dc_ref_time)) {
        master->dc_ref_time = app_time;
    }
    return 0;
}
~~~

所以 `ecrt_master_application_time()` 本身不发包。

它做的是：

```text
application time sample
        ↓
master->app_time
```

真正发 EtherCAT datagram 是下一步。

这很重要，因为控制程序可以明确决定**在哪一个周期位置采样主机时间**。

假设线程周期是：

```text
wake
receive
domain_process
control
application_time
sync_reference
sync_slaves
domain_queue
send
```

那么 app_time 的时间戳代表的是“控制计算后、发包前”的主机时间。

如果把它放在周期最前面，语义就不同。

因此 DC 精度不只是协议问题，还和应用在哪里调用 API 有关。

## 同步参考时钟：payload 写应用时间，然后入队

固定源码：

~~~c
int ecrt_master_sync_reference_clock(ec_master_t *master)
{
    if (master->dc_ref_clock) {
        EC_WRITE_U32(master->ref_sync_datagram.data, master->app_time);
        ec_master_queue_datagram(master, &master->ref_sync_datagram);
    } else {
        return -ENXIO;
    }
    return 0;
}
~~~

这里没有直接调用 NIC。

只是：

```text
master->app_time
    ↓
write 4 bytes into ref_sync_datagram
    ↓
queue datagram
```

真正发送仍由统一的 `ecrt_master_send()` 完成。

这保持了一个很重要的不变量：

> 所有需要上总线的协议工作最终都进入 Master datagram queue，由同一套 frame packing 与 Device 路径发送。

否则 DC 如果绕开 Master queue 自己发帧，就会破坏总线带宽预算与发送顺序。

## sync_slave_clocks 为什么先 zero 再 queue

固定实现：

~~~c
int ecrt_master_sync_slave_clocks(ec_master_t *master)
{
    if (master->dc_ref_clock) {
        ec_datagram_zero(&master->sync_datagram);
        ec_master_queue_datagram(master, &master->sync_datagram);
    } else {
        return -ENXIO;
    }
    return 0;
}
~~~

`sync_datagram` 是 FRMW 类型，运行中会读参考时钟并触发从站侧时钟同步逻辑。

这里调用 `ec_datagram_zero()` 的核心不是“重新构造对象”，而是把复用对象的工作区清到确定状态，再重新排队。

复用对象意味着你必须明确：

- 哪些字段跨周期保留；
- 哪些字段每次事务前重置；
- completion state 何时重新开始；
- payload 是否可能携带上一次事务残留。

所以“对象池/预分配”并不等于没有生命周期复杂度，只是把复杂度从 allocator 转成**状态重置协议**。

## reference_clock_time 为什么只有收到同步 Datagram 后才可信

源码：

~~~c
int ecrt_master_reference_clock_time(const ec_master_t *master,
        uint32_t *time)
{
    if (!master->dc_ref_clock) {
        return -ENXIO;
    }

    if (master->sync_datagram.state != EC_DATAGRAM_RECEIVED) {
        return -EIO;
    }

    *time = EC_READ_U32(master->sync_datagram.data) -
        master->dc_ref_clock->transmission_delay;

    return 0;
}
~~~

它不会返回“最近猜出来的时间”。

前提是：

```text
sync_datagram.state == RECEIVED
```

而且返回值还会减去参考从站的 `transmission_delay`。

这揭示 DC 同步中的另一个事实：

> 时钟偏差不是唯一误差，帧在拓扑中传播也需要时间。

如果完全忽略传播延迟，越远的从站天然会观察到不同相位。

## Sync0/Sync1 为什么属于 Slave Config，而不是 Master 全局开关

机器人驱动器通常不是“只要时钟一致就行”。

它真正需要的是：

```text
每 1 ms 的某个确定相位
    -> 锁存 RxPDO
    -> 执行内部控制
    -> 更新 TxPDO
```

所以每个从站可能需要自己的：

- cycle time；
- Sync0 shift；
- 是否启用 Sync1；
- watchdog；
- activation assignment。

这些属于**从站期望配置**，因此保存在 Slave Config，并由 slave configuration FSM 写进对应 ESC 寄存器。

Master DC 解决“全局时间基准”，Sync0/Sync1 解决“设备什么时候动作”。

## 一个更贴近控制系统的时间线

假设：

- 控制线程周期 1 ms；
- 驱动器 Sync0 也是 1 ms；
- Sync0 相位相对总线周期固定。

一个理想周期可以想成：

```text
t = kT

host wakes
  ↓
poll RX
  ↓
process image now contains cycle k sensor data
  ↓
control computes u[k]
  ↓
write target torque to process image
  ↓
queue PDO + DC datagrams
  ↓
send
  ↓
slaves receive target
  ↓
next Sync0 edge
  ↓
drive applies u[k] at deterministic hardware phase
```

真正重要的不是“线程每 1 ms 执行”，而是：

```text
measurement sampling phase
→ host receive phase
→ control compute phase
→ network transmit phase
→ actuator apply phase
```

这些相位关系是否稳定。

这就是为什么机器人控制里要讨论 **data age**，而不能只看平均通信延迟。

## 一个常见错误：把 DC 当成 Linux 实时调度的替代品

DC 不能解决：

- 控制线程被低优先级任务拖延；
- page fault；
- CPU frequency transition；
- 内核锁竞争；
- 算法 WCET 超预算；
- NIC 驱动不可控延迟。

同样，PREEMPT_RT 或 Xenomai 也不能自动让所有 EtherCAT 从站的硬件时钟一致。

两者解决的是不同层：

```text
Linux RT scheduling
    -> host software execution determinism

EtherCAT DC
    -> distributed device clock alignment
```

一个高质量控制系统通常需要同时处理两边。

## 数据结构角度：为什么这里不用复杂容器

DC 热路径几乎不需要 `list`、hash map 或动态 request queue。

原因是对象集合非常固定：

```text
one reference clock pointer
few preallocated datagrams
few scalar timestamps
```

固定成员比通用容器更适合：

- 无额外节点分配；
- 地址稳定；
- cache locality 更直接；
- 周期成本容易估算。

这是实时软件里很常见的设计原则：

> 如果运行时集合大小在设计时就已知，就不要为了“通用性”把它做成无界动态容器。

## 本篇结论

IgH 的 DC 实现可以压缩成一条很清晰的链：

```text
choose dc_ref_clock
    ↓
preconfigure reusable DC datagrams
    ↓
application_time()
    ↓
sync_reference_clock()
    ↓
sync_slave_clocks()
    ↓
Master datagram queue
    ↓
ordinary frame packing / Device transmit
```

DC 没有另造一套网络发送系统，而是复用主站已有的数据面；这让时间同步也受到同一个 queue、带宽预算和发送时序约束。

下一篇要继续追问：**周期线程、Operation kthread、FSM datagram 和普通 Domain datagram 同时存在时，谁能碰哪些数据结构，如何避免控制面把实时线程拖住？**
