# Distributed Clocks：为什么“每 1 ms 发一次帧”仍然不等于同步控制

固定参考实现：EtherLab / IgH EtherCAT Master 1.6.13，`61cc654f5b721ddd54df0f58bdd34106d91c5359`。

很多人第一次理解实时总线时，会把问题简化成：

> 我的控制线程严格 1 kHz，所以所有从站就每 1 ms 同时采样和输出。

这并不成立。

## 周期相同不代表相位相同

设三个驱动器都以 1 ms 为周期：

```text
A: 0, 1000, 2000, 3000 us
B: 80, 1080, 2080, 3080 us
C: 160, 1160, 2160, 3160 us
```

它们频率都一样，但相位不同。

对于高速机器人：

- A 的编码器值对应时刻 t；
- C 的值对应 t+160 us；

控制器却可能把它们当成同一时刻状态向量。

这会形成 cross-joint observation skew。

## 只同步 Linux 主机时钟也不够

假设主机使用 CLOCK_MONOTONIC，非常稳定。

从站内部仍然有自己的 oscillator。

每个 oscillator 都有：

- 初始 offset；
- frequency error；
- temperature drift；
- manufacturing tolerance。

于是：

```text
master clock:   t
slave A clock:  t + offset_A + drift_A(t)
slave B clock:  t + offset_B + drift_B(t)
```

即使启动时对齐，时间久了仍会漂开。

Distributed Clocks 的任务就是让支持 DC 的从站形成一个共同的分布式时间基准。

## DC 要解决两个变量：Offset 与 Drift

可以把 slave 时钟写成：

```text
C_s(t) = (1 + ε_s)t + b_s
```

其中：

- `b_s` 是初始 offset；
- `ε_s` 是频率误差。

只修 offset：

```text
C_s <- C_s - b_s
```

过一段时间仍会因为 `ε_s` 漂移。

所以必须周期性做 drift compensation。

这也是为什么 DC 不是一次“set time”。

## 参考时钟从哪里来

一个 EtherCAT 网络通常选一个支持 DC 的从站作为 reference clock。

其他 DC slave 相对它同步。

Master 自己还维护 application time，并通过同步 datagram 把应用时间与参考时钟关系建立起来。

概念上：

```text
Application time
      |
      v
Master synchronizes reference clock
      |
      v
Reference DC slave
      |
      v
other DC slaves
```

这并不意味着 Linux 系统时钟被强制改成 DC 时钟。应用时间与设备 DC 时间是可以单独管理的时间域。

## 为什么线缆传播延迟也要考虑

即使所有 oscillator 完全一样，帧到每个从站的时间也不同。

```text
Master -> A -> B -> C -> return
```

A 比 C 更早看到帧。

DC 初始化会利用传播路径测量/推导 transmission delay，从而在时钟校正时补偿拓扑传播差异。

因此同步不是：

```text
所有 slave = 收到 frame 时把 clock 写成同一个值
```

否则不同物理位置会天然留下传播偏差。

## Sync0 / Sync1 解决的是“什么时候采样/输出”

时钟同步后，还需要让设备在统一时间点触发事件。

例如关节控制：

```text
Sync0 at every 1 ms
    -> latch encoder
    -> update control input/output
```

如果 Master 只是“尽快发送 PDO”，执行时刻仍由通信到达时间决定。

Sync0/Sync1 可以让从站按照本地 DC clock，在预定相位触发。

这样就能把：

```text
通信到达时间
```

与：

```text
实际采样/执行事件时间
```

部分解耦。

## Application Time 为什么必须稳定地调用

IgH 提供：

```c
ecrt_master_application_time(master, app_time);
```

应用每周期或按设计节奏告诉 Master 当前 application time。

如果 app_time 自己抖得厉害：

```text
cycle 1: 1.000000000
cycle 2: 1.001120000
cycle 3: 1.001940000
```

DC 算法就会看到不稳定参考。

所以工程上通常选择单调、高精度的时间源，并用绝对周期调度，而不是：

```c
sleep(1ms);
app_time += 1ms;
```

因为后者把线程调度延迟和逻辑时间绑定在一起。

## 绝对时间周期为什么比相对 sleep 更合理

相对 sleep：

```text
work 100 us
sleep 1000 us
work 120 us
sleep 1000 us
```

实际周期变成：

```text
1100 us
1120 us
...
```

误差累积。

绝对调度：

```text
next = t0 + k*T
sleep_until(next)
```

本周期即使晚了 30 us，下一周期理论目标仍是固定网格，不会把这 30 us 永久累积到后面。

这和 DC 的相位思想是一致的：都在维护一个明确时间基准。

## Master 同步 Reference Clock 做了什么

固定 IgH 实现里，`ecrt_master_sync_reference_clock()` 并没有直接在函数里完成复杂时钟算法。

它做的是：

```c
EC_WRITE_U32(master->ref_sync_datagram.data, master->app_time);
ec_master_queue_datagram(master, &master->ref_sync_datagram);
```

也就是说 public API 只是**准备并排队一个 DC datagram**。

真正发出去仍经过：

```text
Master datagram queue
  -> frame packing
  -> Device
```

这再次说明 EtherCAT Master 的统一 datagram 数据面有多重要。

## 同步所有 Slave Clock 同样只是排队工作

`ecrt_master_sync_slave_clocks()` 会把 `sync_datagram` 清零并入队。

所以调用：

```c
ecrt_master_sync_slave_clocks(master);
```

并不等于这一行返回时所有设备已经同步。

它的语义更像：

> 为本轮链路交换安排一次时钟同步操作。

结果要等 datagram 实际发送、经过总线、返回。

## DC Monitor 为什么单独存在

“我发了同步命令”与“当前同步误差有多大”不是同一件事。

因此主站需要 monitor datagram：

```text
queue monitor
  -> send
  -> receive
  -> inspect returned value
```

监控是闭环的一部分。

没有 measurement 的 clock sync 只能算 open-loop command。

## DC 与控制循环的相位关系

一个典型周期可能被设计成：

```text
t_k:
  wake RT thread
  set application time
  receive previous frame
  domain process
  compute
  domain queue
  queue DC sync if needed
  master send

t_k + φ:
  Slave Sync0 event
  latch/apply process data
```

这里的 `φ` 是很重要的工程量。

你不只是要“在 1 ms 内发送完成”，还要确保 frame 有足够时间在 Sync0 之前到达各从站。

如果发送太晚：

```text
frame arrives after Sync0
```

设备可能只能等下一个同步事件使用新数据，等效增加一个完整周期延迟。

## 三种时间误差必须分开

### 周期 Jitter

```text
J_k = actual_wakeup_k - ideal_wakeup_k
```

这是主机调度层问题。

### Bus Latency Variation

```text
B_k = receive/send path variation
```

受 frame 数、NIC、驱动、poll 等影响。

### Device Phase Error

```text
D_i = slave_i_event_time - desired_event_time
```

这是 DC/Sync0 关注的设备级同步问题。

一个系统可能：

- 主机 jitter 很小，但 DC 没配置好；
- DC 很准，但主机偶尔 deadline miss；
- 两者都好，但算法 WCET 太长。

所以“EtherCAT 很实时”不能替代分层测量。

## 从控制理论角度看 Data Age

控制器在 `t_k` 使用的状态不是“当前真实状态”，而是某个更早时刻的观测：

```text
x_used(k) = x(t_k - age_k)
```

其中：

```text
age_k
 = sensor sampling phase
 + bus return delay
 + software receive delay
```

输出也有 actuation age：

```text
u_applied time
 - control compute time
```

DC 最重要的价值之一，就是让 sensor sampling phase 和 actuator application phase 更稳定、更可建模。

## 为什么 DC 对多关节控制比单轴更重要

单轴控制只关心一条状态序列。

多轴机器人希望：

```text
q = [q1, q2, ..., qn]
dq = [dq1, dq2, ..., dqn]
```

代表同一物理时刻。

如果每个关节采样偏差不同，高速运动时：

```text
q1(t)
q2(t + 100us)
q3(t + 200us)
```

拼出的状态向量并不存在于任何真实时刻。

DC 的设备相位同步能显著降低这种 temporal skew。

## 进入源码时要抓住什么

后面的 `distributed-clocks` 源码页会追：

```text
master->app_time
master->dc_ref_time
master->dc_ref_clock
ref_sync_datagram
sync_datagram
sync_mon_datagram
```

以及：

```text
ecrt_master_application_time
ecrt_master_sync_reference_clock
ecrt_master_sync_slave_clocks
ecrt_master_sync_monitor_queue/process
```

重点不是背函数，而是看：

- 时间值存在谁的对象里；
- DC 操作怎样变成 datagram；
- 什么时候 queue；
- datagram 与普通 PDO 怎样共享发送路径；
- 返回结果怎样被解释。

有了这些基础，再看 DC 源码就不会把它误解成“一个神秘的同步 API”。