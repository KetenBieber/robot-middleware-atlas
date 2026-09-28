# OSAL 与实时线程：SOEM 到底提供了多少“实时性”，又把哪些责任明确留给应用

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

SOEM 经常被描述成可以用于实时 EtherCAT 控制。

但这句话如果不拆层，很容易被误解成：

> 只要调用 SOEM API，1 kHz 控制线程自然就是实时的。

源码完全不是这个意思。

SOEM 做的是：

```text
提供可被实时线程调用的协议库
+
提供一层薄 OSAL
```

而真正决定 jitter 的大部分 execution policy 仍然由应用和操作系统承担。

## OSAL 不是 Scheduler

Linux OSAL 主要提供：

```text
time
sleep
thread
mutex
malloc/free
```

它不是：

- Orocos Activity；
- Cyber Scheduler；
- 一个自动 deadline scheduler；
- 一个完整 RT executor。

所以要建立一个非常清楚的边界：

```text
SOEM:
    EtherCAT protocol/runtime primitives

OSAL:
    minimal OS portability wrappers

Application:
    owns control-loop scheduling policy
```

## timeout 为什么用 CLOCK_MONOTONIC

固定源码：

~~~c
void osal_get_monotonic_time(ec_timet *ts)
{
   clock_gettime(CLOCK_MONOTONIC, ts);
}
~~~

而 timer：

~~~c
void osal_timer_start(
   osal_timert *self,
   uint32 timeout_usec)
{
   struct timespec start_time;
   struct timespec timeout;

   osal_get_monotonic_time(&start_time);
   osal_timespec_from_usec(
      timeout_usec,
      &timeout);

   osal_timespecadd(
      &start_time,
      &timeout,
      &self->stop_time);
}
~~~

原因很重要：

> timeout 测量关心“经过了多久”，不是“现在墙上时间是多少”。

如果系统 NTP 调整 realtime clock，timeout 不应该突然变长或变短。

因此：

```text
CLOCK_MONOTONIC
→ interval / deadline measurement
```

是正确的时间基选择。

## absolute sleep 为什么比反复 relative sleep 更适合周期线程

OSAL：

~~~c
int osal_monotonic_sleep(ec_timet *ts)
{
   int result;

   result = clock_nanosleep(
      CLOCK_MONOTONIC,
      TIMER_ABSTIME,
      ts,
      NULL);

   return result == 0 ? 0 : -1;
}
~~~

官方 `ec_sample` 的 RT thread 不是：

~~~c
while (1)
{
   work();
   usleep(1000);
}
~~~

而是：

~~~c
add_time_ns(
   &ts,
   cycletime + toff);

osal_monotonic_sleep(&ts);
~~~

这是一个非常关键的实时编程习惯。

如果使用 relative sleep：

```text
实际周期 =
    work time + sleep time
```

每轮执行误差会累积成 phase drift。

absolute deadline 则是：

```text
deadline[n+1]
    = deadline[n] + T
```

即使本周期晚了，下一个理论基准仍然是预定时间线。

## RT thread helper 真正做了什么

固定：

~~~c
int osal_thread_create_rt(
   void *thandle,
   int stacksize,
   void *func,
   void *param)
{
   ...

   ret = pthread_create(
      threadp,
      &attr,
      func,
      param);

   ...

   schparam.sched_priority = 40;

   ret = pthread_setschedparam(
      *threadp,
      SCHED_FIFO,
      &schparam);

   ...
}
~~~

所以 Linux 下所谓 RT thread helper，核心只是：

```text
pthread
+
SCHED_FIFO
+
priority 40
```

它没有自动：

- pin CPU；
- `mlockall()`；
- page prefault；
- IRQ affinity；
- network IRQ isolation；
- PREEMPT_RT 配置；
- power-state tuning；
- frequency governor tuning；
- memory allocator elimination。

## SCHED_FIFO 为什么有用，也为什么危险

`SCHED_FIFO` 的基本语义是：

> 同优先级 runnable thread 不因为普通 time slice 自动轮转；更高优先级可以抢占更低优先级。

这有利于减少普通 CFS 调度的不确定性。

但如果 RT thread：

- 死循环；
- 长时间 blocking；
- priority 配错；
- 在持锁状态做慢操作；

它也更容易饿死系统其他线程。

所以：

```text
SCHED_FIFO
!=
实时性证明
```

它只是 scheduler policy 的一个组成部分。

## mutex 为什么打开 PTHREAD_PRIO_INHERIT

固定：

~~~c
pthread_mutexattr_setprotocol(
   &mutexattr,
   PTHREAD_PRIO_INHERIT);

pthread_mutex_init(
   mutex,
   &mutexattr);
~~~

考虑：

```text
High priority RT thread
        ↓ waits
mutex held by
        ↓
Low priority thread
```

如果此时 Medium priority thread 持续运行，Low 无法释放 mutex，High 就被间接阻塞。

这就是 priority inversion。

priority inheritance 让持锁的 Low 临时继承更高优先级，以便尽快跑完临界区。

但要注意：

> 它降低某类反转风险，不会消除所有 lock contention，也不会让 mutex 成为 lock-free。

## SOEM 2.0 周期热路径里确实会碰到锁

例如：

`ecx_getindex()`：

~~~c
pthread_mutex_lock(
   &(port->getindex_mutex));

...

pthread_mutex_unlock(
   &(port->getindex_mutex));
~~~

Linux receive 还有 `rx_mutex`。

mailbox pool/queue 使用 OSAL mutex。

所以不能把 SOEM 描述成：

```text
all hot paths lock-free
```

更准确的是：

> 大量内存容量和 frame slot 预先固定，但多线程共享网络与 mailbox 状态仍然通过锁协调。

## 为什么固定数组仍然有实时价值

实时系统不只怕 mutex。

还怕：

- unbounded allocation；
- container resize；
- page fault；
- unpredictable ownership；
- unbounded traversal。

SOEM 的：

```text
txbuf[EC_MAXBUF]
rxbuf[EC_MAXBUF]
slavelist[EC_MAXSLAVE]
grouplist[EC_MAXGROUP]
IOsegment[EC_MAXIOSEGMENTS]
```

让很多资源上界在启动前就可见。

这降低的是：

```text
memory-management uncertainty
```

而不是所有 scheduling uncertainty。

## ec_sample 的线程划分非常值得照着读

官方 sample 至少分成：

```text
RT EtherCAT cyclic thread
        +
normal error/recovery thread
        +
main/config/SDO work
```

RT thread：

~~~c
wkc = ecx_receive_processdata(
   &ctx,
   EC_TIMEOUTRET);

...

ecx_mbxhandler(&ctx, 0, 4);

ecx_send_processdata(&ctx);
~~~

错误检查线程：

~~~c
if (inOP &&
    ((dowkccheck > 2) ||
     ctx.grouplist[currentgroup]
        .docheckstate))
{
   ecx_readstate(&ctx);
   ...
}
~~~

这已经表达出一个非常重要的实时架构原则：

> **周期数据面与故障恢复控制面不应该无条件混在同一个时间预算里。**

## 1 kHz 控制循环应怎样理解 SOEM API

一个更合理的结构：

```text
T = 1 ms

absolute wakeup
    ↓
receive_processdata
    ↓
validate WKC / freshness
    ↓
read IOmap
    ↓
controller
    ↓
write IOmap
    ↓
bounded mailbox handler
    ↓
send_processdata
    ↓
sleep until next absolute deadline
```

后台线程：

```text
state scan
SDO configuration
logging
recovery
diagnostics
```

这并不是唯一设计，但它至少把不同 WCET 特征的工作分开。

## 还缺哪些系统级实时措施

如果目标是真正严肃的机器人伺服控制，应继续考虑：

### 1. PREEMPT_RT

降低 Linux kernel 中不可抢占区间。

### 2. CPU affinity

把 RT thread pin 到专用 core。

### 3. IRQ affinity

网卡 IRQ 与 RT core 的关系要明确，而不是交给默认 irqbalance 猜。

### 4. memory locking

避免周期中 major/minor page fault。

### 5. prefault stack / buffers

IgH sample 明确有 stack prefault；SOEM 应用也应按部署需求处理。

### 6. NIC offload

Generic Ethernet offload 可能和 EtherCAT raw frame 需求冲突，需要按 NIC/driver 实测。

### 7. logging isolation

不要在 1 kHz hot path 直接 printf 大量文本。

## 最后把“实时”拆成三个问题

不要问：

```text
SOEM 实时吗？
```

应该分别问：

```text
1. 协议路径是否 bounded？
2. 应用线程 scheduling 是否 bounded？
3. NIC/kernel/IRQ path 是否 bounded？
```

SOEM 能帮助第一项，并给第二项提供一些薄封装。

真正的端到端实时保证，仍然必须由完整系统设计和测量给出。
