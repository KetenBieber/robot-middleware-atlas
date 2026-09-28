# SOEM vs IgH EtherCAT Master：同一个 EtherCAT 协议，为什么会长成两套完全不同的软件系统

固定源码：

- SOEM v2.0.0：`304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`
- IgH EtherCAT Master stable-1.6 / 1.6.13：`61cc654f5b721ddd54df0f58bdd34106d91c5359`

现在可以做真正有意义的横向比较了。

不是比较：

```text
谁 API 少
谁文件多
```

而是比较：

> **同样要完成 Slave Discovery、PDO/FMMU、Process Image、Datagram、WKC、Mailbox、DC、Redundancy，两套系统把复杂度放在了哪里？**

## 第一层：部署边界完全不同

### SOEM

```text
Application process
├── control loop
├── SOEM C library
│   ├── ecx_contextt
│   ├── protocol
│   ├── OSAL
│   └── OSHW
└── Linux raw socket
        ↓
      NIC
```

### IgH

```text
Application
   ↓ ecrt_* userspace API
libethercat
   ↓ ioctl / mmap
Kernel EtherCAT Master
   ├── ec_master_t
   ├── FSM
   ├── domains
   ├── datagram queue
   └── devices
        ↓
      NIC
```

所以最根本的区别不是函数名。

而是：

```text
SOEM:
    EtherCAT runtime embedded in application

IgH:
    EtherCAT runtime is a kernel subsystem
```

## 第二层：root object 的含义不同

SOEM：

~~~c
struct ecx_context
{
   ecx_portt port;
   ec_slavet slavelist[EC_MAXSLAVE];
   ec_groupt grouplist[EC_MAXGROUP];
   ec_idxstackT idxstack;
   ec_mbxpoolt mbxpool;
   ...
};
~~~

IgH：

~~~c
struct ec_master
{
   ec_cdev_t cdev;
   ec_device_t devices[EC_MAX_NUM_DEVICES];

   ec_fsm_master_t fsm;
   ec_datagram_t fsm_datagram;

   ec_slave_t *slaves;

   struct list_head configs;
   struct list_head domains;
   struct list_head datagram_queue;

   struct task_struct *thread;
   ...
};
~~~

SOEM Context 是：

> library instance state。

IgH Master 是：

> kernel runtime object + execution engine + device boundary + configuration registry。

## 第三层：固定数组 vs 动态对象图

SOEM 高度依赖：

```text
slavelist[]
grouplist[]
txbuf[]
rxbuf[]
idxstack[]
mailbox pool[]
```

IgH 大量使用：

```text
Linux intrusive list_head
dynamic Domain
dynamic SlaveConfig
dynamic FMMU config
long-lived Datagram objects
```

这不是简单的 C 风格区别。

它代表：

### SOEM

```text
capacity is explicit
object lifetime mostly tied to Context
lookup often by index
```

### IgH

```text
runtime object graph
configuration objects linked dynamically
ownership follows kernel subsystem lifetime
```

## 第四层：Datagram 在两边是不是“对象”

IgH 明确有：

~~~c
typedef struct {
    struct list_head queue;
    ec_datagram_type_t type;
    uint8_t address[EC_ADDR_LEN];
    uint8_t *data;
    uint8_t index;
    uint16_t working_counter;
    ec_datagram_state_t state;
    ...
} ec_datagram_t;
~~~

Datagram 有长期生命周期和状态：

```text
INIT
QUEUED
SENT
RECEIVED
TIMED_OUT
ERROR
```

SOEM没有同等意义的长期 Datagram object。

它把一个 wire transaction 映射到：

```text
txbuf[idx]
rxbuf[idx]
rxbufstat[idx]
txbuflength[idx]
idxstack
```

所以：

```text
IgH:
    object-centric transaction

SOEM:
    slot-centric transaction
```

## 第五层：Process Image 谁拥有

IgH Domain：

~~~c
struct ec_domain
{
   struct list_head fmmu_configs;
   size_t data_size;
   uint8_t *data;
   struct list_head datagram_pairs;
   uint16_t expected_working_counter;
   ...
};
~~~

应用通过 Domain data/mmap 使用 process image。

SOEM：

~~~c
uint8 map[4096];

ecx_config_map_group(
   &ctx,
   map,
   0);
~~~

然后：

~~~c
slave->outputs
slave->inputs
group->outputs
group->inputs
~~~

都直接指向 application-owned IOmap。

因此：

```text
IgH:
    Domain is a first-class runtime object

SOEM:
    IOmap is application memory
```

## 第六层：周期 API 看起来相似，但中间层不同

IgH 官方 sample：

~~~c
ecrt_master_receive(master);
ecrt_domain_process(domain1);

... read/write process image ...

ecrt_domain_queue(domain1);
ecrt_master_send(master);
~~~

SOEM：

~~~c
ecx_receive_processdata(
   &ctx,
   timeout);

... read/write IOmap ...

ecx_send_processdata(
   &ctx);
~~~

表面都符合：

```text
receive
process
compute
queue/send
```

但内部：

### IgH

```text
Domain datagram pairs
→ Master datagram queue
→ frame packing
→ device
```

### SOEM

```text
IOsegment[]
→ fixed frame slot
→ build LRW/LRD/LWR
→ raw socket
→ idxstack reassembly
```

## 第七层：配置控制流

IgH 很多配置逻辑走 FSM：

```text
FSM step
→ prepare datagram
→ queue
→ return
→ next execution resumes
```

这样 Master thread 不必为了单个 SDO/EEPROM transaction 长时间阻塞。

SOEM很多基础配置 API 直接是：

```text
getindex
→ send
→ bounded wait/retry
→ return
```

例如：

- APRD；
- FPRD；
- EEPROM；
- SDOread；
- SDOwrite。

所以：

```text
IgH:
    runtime absorbs asynchronous state-machine complexity

SOEM:
    API often exposes synchronous call semantics
```

## 第八层：线程责任

IgH Master 本身有：

~~~c
struct task_struct *thread;
~~~

并持有 Master FSM、scan/config queues、device callbacks。

SOEM Context 没有强制 Master background thread。

官方 sample 自己创建：

~~~c
osal_thread_create_rt(
   &threadrt,
   ...,
   &ecatthread,
   NULL);

osal_thread_create(
   &thread1,
   ...,
   &ecatcheck,
   NULL);
~~~

因此：

```text
SOEM:
    application owns execution architecture

IgH:
    master subsystem owns part of execution architecture
```

## 第九层：NIC 边界

SOEM Linux backend：

```text
AF_PACKET/raw Ethernet socket
→ send/recv/ppoll
```

IgH：

```text
Master Device
→ kernel network-device path / native driver integration
```

这会直接影响：

- syscall boundary；
- scheduler involvement；
- driver integration；
- deployment permissions；
- debugging方式；
- jitter profile。

不能只根据“都用 Ethernet NIC”就认为两者 datapath 等价。

## 第十层：Mailbox 编程模型

SOEM：

```text
SDOread()
→ blocking caller API
→ mailbox pool/queue
→ optional cyclic handler
```

IgH：

```text
request/FSM
→ datagram
→ later state progression
```

因此如果应用想“业务线程像普通函数一样读 SDO”，SOEM很自然。

如果想把大量异步协议事务纳入长期 Master state machine，IgH架构更接近这个目标。

## 第十一层：故障恢复

SOEM：

```text
library provides primitives
application decides policy
```

例如 sample 明确自己判断：

```text
WKC bad count
SAFE_OP+ERROR
SAFE_OP
lost
reconfigure
recover
```

IgH：

```text
kernel Master/Slave FSM
continuously handles scanning/configuration states
```

但上层机器人安全策略依然不应该交给任一 Master 自动决定。

## 第十二层：哪一个“更实时”不能靠架构名回答

错误问题：

```text
kernel master 一定比 userspace library 实时吗？
```

也错误：

```text
SOEM 更轻，所以一定 jitter 更小吗？
```

实际要测：

```text
host scheduling
IRQ
NIC driver
frame size
number of slaves
mailbox load
DC strategy
CPU isolation
PREEMPT_RT
cache/memory
recovery events
```

架构只能告诉我们：

> jitter 可能从哪里进入，以及谁有责任控制它。

## 用“复杂度放在哪里”总结

| 问题 | SOEM | IgH |
| --- | --- | --- |
| Master runtime | 应用进程内 Library | Linux kernel subsystem |
| 主状态容器 | `ecx_contextt` | `ec_master_t` |
| Slave storage | 固定数组 | runtime slave array/object graph |
| Process image | application IOmap | Domain data |
| Wire transaction | fixed frame slot/index | `ec_datagram_t` |
| 周期调度 | application thread | application + master runtime |
| 配置事务 | 很多同步 blocking API | 大量 FSM/datagram progression |
| NIC | userspace raw socket | kernel device path |
| Recovery | application policy 很显式 | Master/Slave FSM 承担更多 |
| 代码入口 | 较短、容易整体读完 | 较大、对象与状态机更丰富 |

## 对学习顺序的意义

如果目标是理解 EtherCAT 主站从 0 怎么写，SOEM 很适合先回答：

```text
最少需要哪些状态？
Frame 如何发？
Index 如何匹配？
IOmap 如何编译？
WKC 如何检查？
DC 如何配置？
```

IgH 则进一步回答：

```text
如果我要把这些能力变成长期内核 runtime，
如何组织 object lifetime、FSM、queue、device、
userspace ABI 与并发？
```

所以两者不是重复项目。

它们正好构成：

```text
SOEM
    ↓
minimal mechanism

IgH
    ↓
industrial runtime architecture
```

这一对照，才是把 EtherCAT 从“会用 API”学到“会设计主站”的关键。
