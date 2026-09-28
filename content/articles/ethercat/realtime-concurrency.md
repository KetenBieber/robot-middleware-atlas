# 实时并发模型：RT 周期线程、Operation kthread 与 FSM Datagram 怎样不互相踩踏

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

前面的文章分别看了 Domain、Datagram、FSM、Device 与 DC。现在把它们放进同一个问题：

> 一个 1 kHz 应用线程正在执行 `receive → process → control → queue → send`，与此同时 Master 还必须扫描总线、配置从站、跑 mailbox 和处理状态机。为什么这些后台工作不会直接把周期线程变成一个不可预测的大函数？

理解 IgH 的关键不是记住“它有线程”，而是分清**两个推进器**。

## 两个推进器分别负责什么

激活以后，最值得区分的是：

```text
应用 RT 周期线程
    ecrt_master_receive()
    ecrt_domain_process()
    control
    ecrt_domain_queue()
    ecrt_master_send()

Master Operation kthread
    ec_fsm_master_exec()
    ec_master_exec_slave_fsms()
    prepare FSM datagram
```

前者决定高频 process data 什么时候进入和离开网卡。

后者推进低频但复杂的控制面状态。

这不是“线程 A 做网络，线程 B 做算法”的普通 producer-consumer。

真正边界是：

```text
RT thread owns timing of wire send/receive
OP thread owns progression of management FSMs
```

## Operation thread 是怎么创建的

固定的线程启动函数：

~~~c
int ec_master_thread_start(
        ec_master_t *master,
        int (*thread_func)(void *),
        const char *name)
{
    master->thread = kthread_create(thread_func, master, name);
    if (IS_ERR(master->thread)) {
        ...
    }

    if (0xffffffff != master->run_on_cpu) {
        kthread_bind(master->thread, master->run_on_cpu);
    }

    (void) wake_up_process(master->thread);
    return 0;
}
~~~

这里有三个 Linux 内核概念。

### kthread_create

它创建的是 kernel thread，不是 pthread。

它拥有自己的 `task_struct`，由 Linux scheduler 调度。

### kthread_bind

如果配置了 `run_on_cpu`，Master thread 可以固定到某个 CPU。

CPU affinity 的作用不是“自动实时”，而是减少迁核带来的 cache 失效和调度位置变化。

### wake_up_process

创建完线程还不等于运行；随后显式把 task 变为 runnable。

## Operation thread 的主循环

固定源码：

~~~c
static int ec_master_operation_thread(void *priv_data)
{
    ec_master_t *master = (ec_master_t *) priv_data;
    unsigned int seq_rt;

    while (!kthread_should_stop()) {
        seq_rt = smp_load_acquire(&master->injection_seq_rt);

        if (seq_rt == master->injection_seq_fsm) {
            if (down_interruptible(&master->master_sem)) {
                break;
            }

            if (ec_fsm_master_exec(&master->fsm)) {
                smp_store_release(&master->injection_seq_fsm,
                        master->injection_seq_fsm + 1);
            }

            ec_master_exec_slave_fsms(master);
            up(&master->master_sem);
        }

        ...
    }
}
~~~

先不要急着看 semaphore。

这里最重要的是：

```c
seq_rt == injection_seq_fsm
```

只有上一份 FSM datagram 已经被 RT 线程接走以后，FSM 才继续准备下一份。

## 为什么不能让 Operation thread 直接往主发送队列里塞

最朴素实现可能是：

```c
// OP thread
lock(master_queue);
queue_fsm_datagram();
unlock(master_queue);

// RT thread
lock(master_queue);
queue_domain_datagrams();
send_all();
unlock(master_queue);
```

这看起来很正常，但对 1 kHz 控制线程不友好。

只要 OP thread 在锁内：

- 遍历复杂状态；
- 准备 mailbox；
- 分配或复制较多数据；
- 被 scheduler 抢占；

RT thread 就可能在同一把锁前等待。

于是后台控制面直接进入周期线程的 worst-case latency。

IgH 采取的是另一种交接：

```text
OP thread
    prepare one FSM datagram
    increment injection_seq_fsm
                |
                | publish work
                v
RT ecrt_master_send()
    notices seq mismatch
    queues fsm_datagram
    updates injection_seq_rt
```

这相当于把“谁修改发送队列”的责任尽量收束到 RT send path。

## ecrt_master_send 中的交接点

前面已经见过：

~~~c
seq_fsm = smp_load_acquire(&master->injection_seq_fsm);
if (master->injection_seq_rt != seq_fsm) {
    ec_master_queue_datagram(master, &master->fsm_datagram);

    smp_store_release(&master->injection_seq_rt, seq_fsm);
}
~~~

这段很小，但它就是两个执行上下文之间的协议。

对象不是复制过去的。

双方共享同一个：

```text
master->fsm_datagram
```

真正变化的是**谁现在有资格推进它**。

## acquire/release 为什么比普通 ++ 更严谨

如果只写：

```c
master->injection_seq_fsm++;
```

另一个 CPU 看到 sequence 变化时，不一定可以仅凭 C 语言层面的直觉假设前面所有对 datagram 字段的写入都已经以需要的顺序可见。

固定版本显式使用：

```c
smp_store_release(...)
smp_load_acquire(...)
```

建立 publish/observe 顺序：

```text
OP CPU:
write datagram fields
release-store new seq

RT CPU:
acquire-load seq
then consume datagram fields
```

这不是为了“防止两个线程同时执行所有代码”，而是为了建立**内存可见性与顺序关系**。

因此不能把内存屏障和 mutex 混成一个概念。

## master_sem 又保护什么

Operation thread 在执行 Master/Slave FSM 前：

~~~c
down_interruptible(&master->master_sem);
...
ec_fsm_master_exec(&master->fsm);
ec_master_exec_slave_fsms(master);
...
up(&master->master_sem);
~~~

`master_sem` 保护的是 Master 配置与对象图等共享状态。

它不是整个周期发送路径每一步都必须持有的“全球大锁”。

这类分工值得注意：

```text
sequence/acquire-release
    -> RT/FSM datagram handoff

master_sem
    -> complex shared control-plane object graph
```

如果所有共享状态都只用一把大 semaphore，设计会更简单，但实时边界更差。

## 为什么 FSM 不能阻塞等 Datagram

状态机执行器的基本规则是：

```c
if (datagram is QUEUED or SENT)
    return;
```

所以 OP thread 不会因为某个 SDO 响应还没回来就睡在“这个 SDO 的同步调用”里面。

相反：

```text
prepare request
return

下一轮:
check completion
if not done -> return

再下一轮:
continue state transition
```

因此一个几十毫秒 mailbox 事务被拆成很多非常短的推进步骤。

这就是 cooperative state machine 的实际意义。

## 但 Operation thread 本身是不是实时线程

不能因为名字里有 OP 就误判。

它是 Linux kthread；固定源码可以把它绑定 CPU，但并没有在这里证明它就是高优先级 SCHED_FIFO 实时线程。

它的职责也不是硬实时 process-data 周期。

恰恰相反，设计目标之一是把复杂控制面从应用实时线程中拆走。

所以在系统设计时应该把：

```text
application cyclic task priority
Master OP kthread scheduling
NIC IRQ/poll execution
other kernel work
```

分别分析。

## HRTIMER 分支为什么限制 OP thread 速度

固定代码在 `EC_USE_HRTIMER` 下：

~~~c
ec_master_nanosleep(master->send_interval * 1000);
~~~

注释很直接：

```text
the op thread should not work faster than the sending RT thread
```

原因很好理解。

即使 FSM 能很快产生很多管理 datagram，线上的真正发送节奏仍由 RT 周期控制。

OP thread 跑得更快只会：

- 更频繁争用共享状态；
- 产生无意义检查；
- 让“待注入工作”堆积。

因此后台推进速度要和数据面的消费速度匹配。

## 没有 HRTIMER 时为什么有 schedule/schedule_timeout

固定路径：

~~~c
if (ec_fsm_master_idle(&master->fsm)) {
    set_current_state(TASK_INTERRUPTIBLE);
    schedule_timeout(1);
}
else {
    schedule();
}
~~~

如果 FSM 空闲，可以睡一个 tick。

如果还有状态机工作，就主动让出 CPU。

这说明 Operation thread 不是 busy-spin loop。

这有利于 CPU 占用，但也意味着它的推进粒度受 Linux 调度影响。

对于 mailbox/configuration 这种控制面通常是合理交换。

## Device Poll 又在哪个执行上下文

周期路径：

```text
application thread
  ecrt_master_receive()
    ec_device_poll()
      device->poll(net_device)
```

因此至少从 Master 这一层看，设备轮询是在**调用 receive 的应用执行上下文**里同步发生。

这和传统“RX interrupt 随时进入，然后另一个线程被唤醒”有明显区别。

好处是控制程序可以把网络接收处理相位放进自己的周期时间线。

代价是 poll 路径的成本直接计入该周期。

所以驱动 poll 的 worst-case time 也必须进入实时预算。

## TX ring 为什么是固定 2 槽而不是动态队列

Device 保存：

```c
struct sk_buff *tx_skb[EC_TX_RING_SIZE];
```

且：

```c
#define EC_TX_RING_SIZE 2
```

这不是 STL `std::queue` 风格的“来了多少就 push 多少”。

它表达的是：

```text
fixed bounded reusable transport buffers
```

实时路径里固定数组的优势：

- 无扩容；
- 无节点分配；
- 地址稳定；
- 上界明确；
- cache footprint 更可控。

这正好体现实时系统里非常关键的数据结构原则：**容器选择首先受 worst-case 和生命周期约束，而不是 API 是否方便。**

## Master Datagram Queue 为什么偏偏是 intrusive list

另一个方向，Master 的 `datagram_queue` 却使用 Linux `list_head`。

为什么不用固定数组？

因为这里对象来源很多：

- Domain datagram pairs；
- Master FSM datagram；
- external datagrams；
- DC datagrams；
- mailbox/control work。

这些对象本身生命周期不同，但都需要短暂进入同一发送/在途集合。

intrusive list 的优势是：

```text
no wrapper-node allocation
stable object address
O(1) remove when object already known
one object can embed multiple list nodes
```

代价是：

- 顺序查找 O(n)；
- cache locality 不如连续数组；
- 重复插入会破坏链表，因此 queue 函数必须防御。

这也是为什么 datagram queue 必须保持**有界、规模较小**，不能把它当无界消息积压池。

## 实时边界：配置期可以 kmalloc，不等于周期期可以

源码里确实能看到很多：

```c
kmalloc(...)
list_add_tail(...)
wait_event_interruptible(...)
```

但必须看它出现在哪个 phase。

例如 Create Domain：

```text
configuration phase
    kmalloc ec_domain_t
```

Domain finish：

```text
activate phase
    allocate process image/datagram structures
```

周期：

```text
reuse process image
reuse datagram
reuse skb ring
```

判断“这个主站是否实时友好”不能 grep 到一个 `kmalloc` 就下结论；必须先区分 cold/config path 与 hot/cyclic path。

## Linux 调度层还需要应用自己做什么

IgH 不可能替应用自动决定整个机器人进程的实时策略。

真正的系统集成仍需考虑：

- `SCHED_FIFO` / PREEMPT_RT 或 Xenomai/RTAI；
- CPU affinity；
- IRQ affinity；
- memory locking；
- page fault 预热；
- 控制算法 WCET；
- 日志和文件 I/O 隔离；
- CPU governor；
- NIC driver 支持质量。

Master 提供的是一个**可以被实时线程调用的有界数据路径设计**，不是一键实时保证。

## 一个 1 kHz 周期应该怎样做预算

不要只测：

```text
ecrt_master_send() 平均 8 us
```

更有意义的是拆成：

```text
T_receive_poll
+ T_domain_process
+ T_control
+ T_domain_queue
+ T_frame_pack
+ T_nic_xmit
+ scheduler/IRQ interference
< 1 ms budget
```

然后看：

- p99.9；
- 最大值；
- 连续压力下 worst observed；
- 丢链路、WKC 异常、mailbox 活跃时是否恶化。

平均值很漂亮并不能证明控制周期可靠。

## 一个危险反例：把诊断工作直接塞进 RT loop

错误示例：

```c
for (;;) {
    receive();
    process();

    if (fault)
        synchronous_sdo_read_all_diagnostics();

    control();
    send();
}
```

故障恰恰是最需要保持控制周期确定性的时刻。

如果这时同步做 mailbox 事务，周期可能从 1 ms 变成几十毫秒。

IgH 的 FSM 分层就是在避免这种耦合：

```text
RT loop keeps process-data cadence
control-plane FSM progresses diagnostics/config asynchronously
```

## 本篇结论

IgH 的并发模型不是“大量线程并行跑”。

它真正精巧的是**责任收敛**：

```text
RT/application thread
    owns cyclic receive/send timing

Operation kthread
    advances complex management FSMs

acquire/release sequence
    hands one FSM datagram toward RT send path

master_sem
    protects complex control-plane shared state

fixed buffers + reused objects
    keep hot path bounded
```

下一篇把整个专题收束成“如果我们自己从零写一个 EtherCAT Master，应该按什么顺序长出这些对象”，并总结 IgH 最值得借鉴、也最需要警惕的设计取舍。
