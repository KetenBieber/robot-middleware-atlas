# SOEM 总览：同样是 EtherCAT Master，为什么它不像 IgH 那样需要一个内核运行时

固定源码：OpenEtherCATsociety / SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

SOEM 的全名是 **Simple Open EtherCAT Master**。官方 README 对它的定位不是“一个独立 EtherCAT 服务”，而是：

> 用于开发 EtherCAT MainDevice 的轻量软件库，尤其面向实时嵌入式通信。

这句话决定了整个源码的气质。

IgH 的第一性问题是：

```text
如何把 EtherCAT 做成 Linux 内核里的长期 Master runtime？
```

SOEM 的第一性问题则是：

```text
如何把 EtherCAT 的发现、配置、过程数据、邮箱和时钟能力
压进一套可以被应用直接调用的 C Library？
```

因此不要带着 IgH 的对象模型来读 SOEM。

## 先看两个主站最外层的差异

IgH：

```text
application
    ↓ ecrt_* userspace API
libethercat
    ↓ ioctl / mmap
kernel EtherCAT Master
    ↓ net_device / driver
NIC
```

SOEM：

```text
application / RT thread
    ↓ ecx_*()
SOEM C library
    ↓ ecx_contextt
    ↓ OSHW
Linux raw socket / Windows packet backend
    ↓
NIC
```

SOEM 没有一个必须独立存在的 kernel Master 对象。

你的应用线程本身就在推动 Master。

## 官方 simple_ng 已经给出整条学习主线

固定源码 `samples/simple_ng/simple_ng.c` 的启动顺序非常清楚：

~~~c
if (!ecx_init(context, fieldbus->iface)) {
    ...
}

if (ecx_config_init(context) <= 0) {
    ...
}

ecx_config_map_group(context, fieldbus->map, fieldbus->group);
ecx_configdc(context);

ecx_statecheck(context, 0, EC_STATE_SAFE_OP, EC_TIMEOUTSTATE * 4);

fieldbus_roundtrip(fieldbus);

slave = context->slavelist;
slave->state = EC_STATE_OPERATIONAL;
ecx_writestate(context, 0);
~~~

而一次过程数据 roundtrip：

~~~c
ecx_send_processdata(context);
wkc = ecx_receive_processdata(context, EC_TIMEOUTRET);
~~~

所以本专题的主线可以直接写成：

```text
ecx_init
    ↓
ecx_setupnic
    ↓
raw Ethernet socket

ecx_config_init
    ↓
slave discovery
    ↓
station address / SII / mailbox / topology

ecx_config_map_group
    ↓
PDO / SM / FMMU
    ↓
application IOmap

cyclic loop
    ↓
ecx_send_processdata
    ↓
frame buffers + index stack
    ↓
raw socket
    ↓
ecx_receive_processdata
    ↓
WKC + copy back into IOmap
```

## SOEM 最值得学的不是“API 更少”

SOEM 的源码体量比 IgH 更容易进入，但不能把“简单”理解成“没有系统设计”。

它把很多复杂度压进了固定容量数据结构：

```text
ecx_contextt
├── ecx_portt
│   ├── txbuf[EC_MAXBUF]
│   ├── rxbuf[EC_MAXBUF]
│   └── rxbufstat[EC_MAXBUF]
├── slavelist[EC_MAXSLAVE]
├── grouplist[EC_MAXGROUP]
├── idxstack
├── EEPROM cache
└── mailbox pool
```

这里最值得反复追问：

- 为什么是固定数组而不是链表？
- 为什么 frame index 同时承担 TX/RX 匹配？
- 为什么 process-data send 是非阻塞、receive 再回收？
- 为什么配置原语大量是 blocking，而周期过程数据被拆成 send/receive 两段？
- 为什么 IOmap 是应用提供的 byte buffer？
- 为什么 Linux backend 用 raw socket 而不是内核专用 EtherCAT driver？
- 哪些 mutex 会进入周期热路径？
- 多线程 mailbox 与 process-data 如何避免把同一组资源踩坏？

## 本专题与 IgH 不重复讲协议理论

EtherCAT Frame、Datagram、PDO、FMMU、WKC、AL State、Mailbox 与 Distributed Clocks 的协议理论已经在 IgH 专题前半系统解释。

SOEM 专题把重点放在：

> **同一套 EtherCAT 语义，在另一种软件架构里如何被实现。**

因此每篇都会把 SOEM 与 IgH 的设计选择并排讨论，而不是重新背一遍 EtherCAT 名词。

## 第一阶段阅读顺序

先完成六个问题：

1. `ecx_contextt` 为什么可以成为整个 Master 的 root context？
2. Linux 下 `ecx_setupnic()` 怎样拿到 raw Ethernet socket？
3. `txbuf[]/rxbuf[]/rxbufstat[]` 如何组成固定容量事务表？
4. `ecx_config_init()` 怎样从一张空网卡发现并编号所有从站？
5. `ecx_config_map_group()` 怎样把 PDO/FMMU 编译到应用 IOmap？
6. `ecx_send_processdata_group()` 与 `ecx_receive_processdata_group()` 怎样完成一个周期？

完成这六步以后，再进入 CoE、DC、冗余、OSAL/OSHW 和实时并发。

## 一个控制工程上的提醒

SOEM 是 Library，意味着：

```text
SOEM 不会替应用自动拥有你的周期线程
```

应用必须自己决定：

- 线程周期；
- `SCHED_FIFO`；
- CPU affinity；
- 内存锁定；
- mailbox 是否另起线程；
- WKC 异常策略；
- 状态恢复；
- 控制算法最坏执行时间。

这比 IgH 更“自由”，也意味着更多实时责任直接落到应用架构上。

后面源码解剖都会围绕这条边界展开。
