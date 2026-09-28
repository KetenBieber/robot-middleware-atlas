# EtherCAT 实时性：1 kHz、低延迟、确定性和硬实时到底分别是什么意思

固定参考实现：EtherLab / IgH EtherCAT Master 1.6.13，`61cc654f5b721ddd54df0f58bdd34106d91c5359`。

“EtherCAT 可以跑 1 kHz”是一句非常弱的信息。

它没有告诉你：

- 偶尔会不会 3 ms；
- 输入数据多旧；
- 输出什么时候真正生效；
- 配置流量是否会抢链路；
- Linux 调度是否可控；
- 网卡驱动是否会排队；
- 控制算法 WCET 是否超预算。

这一篇建立后续源码分析使用的实时系统语言。

## Frequency 不是 Real-Time

系统平均每秒运行 1000 次：

```text
mean frequency ≈ 1 kHz
```

并不能推出：

```text
every deadline <= 1 ms
```

例如：

```text
周期间隔：
0.9ms, 0.9ms, 0.9ms, 3.3ms, 0.9ms ...
```

平均值仍可能接近 1 ms，但控制已经发生 deadline miss。

实时系统首先关心的是时间约束，而不是平均吞吐。

## Latency 与 Jitter 要分开

设某条路径延迟：

```text
L_k = output_time_k - input_time_k
```

平均延迟：

```text
mean(L)
```

只告诉你中心位置。

jitter 描述延迟波动，例如：

```text
J_k = L_k - L_nominal
```

控制系统通常比 Web 服务更害怕 jitter，因为 jitter 会让离散控制模型的采样周期变成时变。

## 数据年龄（data age）与输出年龄比“通信延迟”更贴近控制问题

控制线程在时刻 t_k 读到一个 position，并不意味着这个 position 就是在 t_k 采样的。

更完整的时间线是：

~~~text
slave samples sensor
    ↓
frame carries value back
    ↓
NIC receives
    ↓
ecrt_master_receive()
    ↓
ecrt_domain_process()
    ↓
controller reads process image
~~~

如果从站实际采样时刻是 t_sample，而控制器读取时刻是 t_read，那么：

~~~text
data_age = t_read - t_sample
~~~

这才是控制器真正面对的“状态有多旧”。

同理，控制器在 t_write 把 target torque 写进 process image，也不等于驱动器在这一刻已经执行。命令还要经历：

~~~text
domain_queue
→ master_send
→ NIC
→ EtherCAT wire
→ slave receives
→ Sync0 / drive internal latch
~~~

如果最终执行时刻是 t_apply：

~~~text
output_age = t_apply - t_write
~~~

因此一个系统即使 send/receive API 本身非常快，只要采样相位、总线周期或驱动器锁存相位不稳定，控制器看到的 data age 和 actuator 看到的 output age 仍然可能抖动。

Distributed Clocks、固定周期调度和明确的 Sync0 相位，本质上都在帮助稳定这些时间关系。


## Hard Real-Time 与 Soft Real-Time

Soft real-time：

> 大多数周期及时，偶尔 miss 会降性能，但系统仍能接受。

Hard real-time：

> deadline miss 本身就是系统级失败，必须有可证明或足够严格的 worst-case 保证。

Linux PREEMPT_RT 能显著改善调度延迟，但“使用 PREEMPT_RT”仍不自动证明你的整个 EtherCAT 控制链是 hard real-time。

证明链至少包含：

```text
task scheduling
 + lock blocking
 + algorithm WCET
 + master processing
 + NIC/driver
 + bus transfer
 + slave timing
```

任何一层没有 bound，端到端 hard deadline 就不能凭口号成立。

## 一个 1 ms 周期怎样做预算

可以先用简单预算：

```text
T_period = 1000 us

T_wakeup_jitter
+ T_receive
+ T_domain_process
+ T_control
+ T_domain_queue
+ T_master_send
+ T_driver
+ margin
<= 1000 us
```

假设：

```text
wake jitter       20 us
receive           25 us
domain process    10 us
control          300 us
queue              5 us
send              20 us
driver/bus       120 us
-----------------------
used             500 us
margin           500 us
```

这比“CPU 只用了 50%”更有意义，因为每项都能映射到具体链路。

## 为什么 Worst-Case 比平均值重要

如果 `T_control` 平均 100 us，但每 10 秒一次 1.5 ms：

```text
mean 很漂亮
deadline 已经失败
```

同样：

- kmalloc 平均很快；
- mutex 平均不竞争；
- printk 平均不执行；
- page fault 平均没有；
- NIC queue 平均为空。

实时设计要研究的是它们**最坏时会怎样**。

## 配置期与周期期分离为什么是实时系统基本功

初始化阶段允许：

- kmalloc；
- 链表构建；
- 设备扫描；
- SDO；
- 文件读取；
- 字符串解析。

周期阶段理想上只使用：

- 预先分配对象；
- 固定内存布局；
- 有界循环；
- 可分析同步；
- 已确定的 datagram。

IgH 的 `ecrt_master_activate()` 与 `ec_domain_finish()` 正体现这种 phase separation。

Domain 在 activate 时完成 process memory 和 datagram pair 构造，之后周期只 queue 已有对象。

## Dynamic Allocation 为什么会制造 Jitter

`malloc/kmalloc` 的耗时不是数学常数。

它可能涉及：

- allocator metadata；
- per-CPU cache miss；
- reclaim；
- page allocation；
- fragmentation；
- 锁竞争。

所以 hard/firm real-time 路径常用：

```text
preallocate
pool
ring
fixed-size objects
```

但不能看到“用了 ring”就自动宣布实时安全。

还要检查：

- ring 满了怎么办；
- producer/consumer 同步；
- 是否会 fallback allocation；
- cache line contention；
- shutdown lifetime。

## Page Fault 为什么危险

用户空间第一次触碰虚拟内存页时，可能触发 page fault。

因此实时控制程序常见准备动作包括：

```text
mlockall()
prefault stack/heap
avoid late dlopen
avoid first-time allocation in RT loop
```

主站内核部分没有用户态 page fault 这个完全相同的问题，但内核内存分配、调度和锁仍然有自己的不可预测边界。

实时性必须按执行层分别分析。

## Lock 不是实时系统的敌人，Unbounded Blocking 才是

很多文章看到 mutex 就说“不实时”。

这太粗糙。

如果一个锁：

- 临界区 2 us；
- 只有两个已知线程；
- 使用 priority inheritance；
- 没有 I/O；

它可能完全可分析。

真正危险的是：

```text
RT thread waits lock
  -> low-priority thread owns lock
  -> low-priority thread gets preempted
  -> blocking becomes unbounded
```

这叫 priority inversion。

所以源码阅读要记录：

- 锁是什么类型；
- 谁获取；
- 临界区多长；
- 是否跨 I/O；
- 是否可能睡眠；
- RT/non-RT 线程是否共享。

IgH Master 里能看到 semaphore、rt_mutex、wait queue、atomic/smp ordering 等多种同步原语，它们的用途不能混为一谈。

## Polling 为什么在 EtherCAT 里很重要

传统网卡路径常依赖中断：

```text
packet arrives
  -> hardware interrupt
  -> ISR/NAPI
  -> scheduler
  -> application eventually runs
```

中断驱动省 CPU，但 arrival-to-processing latency 受更多调度因素影响。

IgH Device 层明确支持由 Master 调用设备 poll function：

```text
RT cycle
  -> ecrt_master_receive
  -> ec_device_poll
  -> driver poll
  -> ecdev_receive
```

这把接收工作拉回到应用周期的显式时序附近。

代价是：

- CPU 消耗更高；
- 驱动需要适配；
- poll 仍可能有执行时间波动；
- 需要仔细处理 NIC/DMA。

这不是“poll 永远优于 interrupt”，而是典型的确定性与资源利用率取舍。

## 为什么 TX Ring 要预先准备多个 skb

如果每次发 frame 都：

```c
skb = alloc_skb(...);
fill(skb);
ndo_start_xmit(skb);
```

就把分配带进周期路径。

IgH Device 保存：

```text
tx_skb[EC_TX_RING_SIZE]
tx_ring_index
```

发送前轮换 ring entry。

固定源码注释直接指出原因之一：连续发送多个 frame 时，如果 DMA 尚未调度，同一个 buffer 会有 race 风险。

因此多 buffer 不只是性能优化，也是 ownership 时间问题：

```text
CPU has submitted buffer A
NIC/DMA may still consume A
CPU must write buffer B
```

这和任何异步 I/O ring 的设计原则相同。

## Cache 与 False Sharing 也可能影响 Jitter

即使没有锁，两个 CPU 反复写同一 cache line：

```text
CPU0 write state
CPU1 write stats
```

也会发生 cache coherency traffic。

所以高级实时优化还会考虑：

- CPU affinity；
- per-CPU data；
- cache line alignment；
- 避免 RT loop 写大量共享统计；
- NUMA locality。

源码里看到 atomic 并不意味着“没有成本”。原子操作只是定义并发语义，cache coherence 仍要付费。

## 内存屏障解决的是可见性，不是调度

IgH 固定源码对 datagram state 等位置使用：

```c
smp_store_release(...)
smp_load_acquire(...)
```

release/acquire 用于建立内存可见性顺序。

它能表达：

> 在 state 变成 RECEIVED 前，payload/WKC 的写入应对随后 acquire 读取可见。

它不能保证：

- 另一个线程何时获得 CPU；
- 某个函数在 10 us 内完成；
- 没有锁竞争。

内存序与实时调度是两个不同维度。

## Logging 为什么必须小心

`printk`、格式化字符串和日志 I/O 都可能带来巨大 jitter。

所以实时源码里常看到：

```c
if (unlikely(debug_level > 1)) {
    ...
}
```

以及统计节流。

但“默认不打印”不代表错误路径实时安全。

如果系统在故障时开始大量日志，而故障恰恰是最需要控制器稳定降级的时候，日志本身可能放大故障。

生产系统需要明确：

```text
fault logging budget
rate limit
buffered telemetry
out-of-band diagnostics
```

## CPU Affinity 与 Priority 只是必要条件之一

将控制线程绑核：

```text
CPU 3 only
SCHED_FIFO priority 90
```

可以减少迁核和普通任务竞争。

但如果同一 CPU 上还有：

- NIC IRQ；
- kernel worker；
- softirq；
- high-priority watchdog；

仍可能影响 jitter。

因此部署时要联合设计：

```text
RT task affinity
NIC IRQ affinity
kernel isolation
RCU/nohz settings
memory locking
power management
CPU frequency
```

源码只解决主站实现；系统实时性最终是 OS + hardware + application 的共同属性。

## 总线利用率为什么也属于实时性

即使软件零 jitter，如果周期里 datagram 太多：

```text
frame1
frame2
frame3
...
frameN
```

线速就是硬下界。

100 Mbit/s 意味着：

```text
100 ns/bit?
```

更准确地换算：

```text
100 Mbit/s = 10 ns/bit
             = 80 ns/byte
```

但真实线时还包含 Ethernet preamble、IFG、headers、返回路径和从站转发延迟。

所以应用要对 PDO layout 和周期分组做容量规划。

## 控制链应该测哪些时间戳

建议至少区分：

```text
t_wakeup
t_receive_begin/end
t_process_end
t_control_end
t_queue_end
t_send_end
t_next_wakeup
```

如果设备支持 DC，再加入：

```text
slave sample time
Sync0 phase
reference clock error
```

这样才能把 jitter 归因：

```text
scheduler?
master receive?
algorithm?
send?
bus?
DC?
```

## “实时 EtherCAT”最终应该怎样表述

比“系统是实时的”更精确的说法是：

> 在指定硬件、内核、CPU 隔离、PDO 配置、从站数量和控制算法负载下，周期任务的 wake-to-send 延迟、bus round-trip、DC phase error 与 deadline miss rate 被测量在某个范围内。

如果有形式化 WCET/bound，再进一步声称 hard real-time。

后面的源码解剖会把这些理论指标逐一映射到实际字段、锁、list、skb、poll 与 FSM 上。