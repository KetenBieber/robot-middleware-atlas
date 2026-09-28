# SOEM 设计复盘：如果从零写一个轻量 EtherCAT Master，源码为什么会自然长成 Context + IOmap + Frame Slots

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

现在不再按文件顺序复述 SOEM。

换成源码作者的视角：

> **如果什么都没有，只知道我要在 Linux 用户态做一个可嵌入机器人控制进程的 EtherCAT MainDevice，最少要发明哪些对象？**

## 第一个问题：我需要保存什么长期状态

最粗糙实现可能全是 global：

~~~c
int socket_fd;
Slave slaves[128];
uint8_t tx[1500];
uint8_t rx[1500];
~~~

很快就会遇到：

- 不能多实例；
- ownership 模糊；
- 测试困难；
- 配置状态与网络状态散落。

自然的下一步就是 root context：

```text
ecx_contextt
```

把：

- port；
- slaves；
- groups；
- EEPROM cache；
- error ring；
- process-data bookkeeping；
- mailbox pool；

收进同一个实例。

## 第二个问题：为什么不每发一帧 malloc

因为 EtherCAT 周期运行天然是：

```text
same maximum frame size
same finite number of in-flight frames
repeat forever
```

最直接的实时设计就是：

```text
txbuf[EC_MAXBUF]
rxbuf[EC_MAXBUF]
state[EC_MAXBUF]
```

把动态 allocation 从 hot path 移出去。

然后新的问题来了：

> 多个 frame 在途时，回来的是哪一个？

于是 frame index 自然同时成为：

- wire identifier；
- local slot identifier。

## 第三个问题：如果返回乱序怎么办

不能假设：

```text
send A
send B
receive A
receive B
```

所以：

```text
wire idx
→ rxbuf[idx]
```

如果现在等 A 却收到 B：

```text
store B
mark RCVD
continue wait A
```

一个固定 transaction table 就形成了。

## 第四个问题：启动时甚至不知道有几个从站

不能一上来就 FPRD，因为 fixed station address 还不存在。

于是必须：

```text
Auto Increment Addressing
→ discover physical position
→ assign station address
→ fill slavelist[]
```

这解释了为什么 AP 命令不是协议边角，而是 bootstrap 必需品。

## 第五个问题：控制器不可能每周期理解 PDO/FMMU

业务线程真正想看到：

~~~c
actual_position =
   read(input bytes);

write(output bytes,
      target_torque);
~~~

所以启动期必须做一次“编译”：

```text
PDO semantics
→ SyncManager
→ FMMU
→ logical address
→ IOmap pointer
```

这一步完成以后，周期里才能只操作 bytes。

## 第六个问题：IOmap 大了，一个 frame 放不下怎么办

不能周期里临时做复杂 search。

于是 mapping 阶段继续输出：

```text
IOsegment[]
```

周期里只遍历已经切好的 segment。

这就是经典：

> cold path computes structure, hot path consumes structure。

## 第七个问题：多个 segment 为什么不要逐个同步等待

如果：

```text
send 0 wait 0
send 1 wait 1
send 2 wait 2
```

总线吞吐和 host latency 都被串行化。

于是 process data API 自然拆成：

```text
send_processdata
receive_processdata
```

发送阶段把所有 frame 推出去。

但 receive 时又需要知道每个 frame 的 payload 应该写回 IOmap 哪里。

于是：

```text
idxstack
```

出现了。

## 第八个问题：为什么 SDO 不也全部异步化

可以，但代码复杂度会大幅增加。

SOEM选择：

```text
simple acyclic operation
→ blocking API with timeout
```

这样：

~~~c
ecx_SDOread(...)
~~~

对普通应用线程非常易用。

代价是开发者必须理解：

> blocking convenience API 不能随便进入 hard RT hot path。

SOEM 2.0 再通过 mailbox pool、queue、cyclic handler 改善多线程与周期协调，而没有彻底把整个 API 改造成 FSM framework。

## 第九个问题：为什么需要 OSAL/OSHW 两层

如果把：

~~~c
pthread
clock_gettime
AF_PACKET
send
recv
~~~

直接写进 protocol core，SOEM 就只能是 Linux library。

因此：

```text
OSAL
→ thread/time/mutex/memory

OSHW
→ NIC/frame I/O
```

让核心 EtherCAT 逻辑与平台实现分开。

这也是 SOEM 能出现在多个 RTOS/OS backend 上的结构基础。

## 第十个问题：为什么 WKC 必须一直传回应用

Library 无法知道机器人业务语义。

例如：

```text
少一个从站回应
```

对不同系统可能意味着：

- 立即切 torque off；
- 继续剩余轴；
- hold last command；
- 等 3 周期；
- 触发冗余；
- 进入安全速度。

所以 SOEM只提供：

```text
actual WKC
state
AL status
recover/reconfig primitives
```

最终 policy 留给应用。

## 第十一个问题：DC 为什么不能只做“统一时钟值”

工业伺服需要的是：

```text
多个 slave 在物理时间上同步触发
```

因此 Master 要知道：

- port receive timestamp；
- topology；
- propagation delay；
- system offset；
- Sync0/Sync1 cycle/shift。

SOEM 的 `ecx_configdc()` 正是在把物理拓扑转成 clock compensation parameters。

## 第十二个问题：双网口冗余为什么进 port 层

冗余影响的是：

```text
frame 从哪张 NIC 发
从哪边回来
返回路径是否完整
是否需要从另一边补发
```

它不应该污染 PDO/SDO 语义。

所以 SOEM 把它收进：

```text
ecx_portt
ecx_redportt
ecx_waitinframe_red
```

这是一条很合理的分层边界。

## 数据结构选择总结

### 固定数组

用于：

```text
bounded resources
stable address
O(1) index lookup
no hot-path reallocation
```

### Ring/index table

用于：

```text
frame slots
mailbox pool
mailbox queue
```

### Pointer into IOmap

用于：

```text
compile device mapping once
direct access in cyclic loop
```

### Mutex

用于：

```text
shared fixed resources across threads
```

而不是因为固定数组本身需要动态容器同步。

## 一条完整运行链

最后把整个 SOEM runtime 压成一条链：

```text
ecx_init
↓
raw socket + mailbox pool

ecx_config_init
↓
discover physical slaves
↓
slavelist[]

ecx_config_map_group
↓
CoE/SII PDO discovery
↓
SM program
↓
FMMU program
↓
IOmap + IOsegment[] + expected WKC

ecx_configdc
↓
topology timing + propagation compensation

OP
↓
absolute cyclic wakeup
↓
receive_processdata
↓
idx → rx slot
↓
idxstack → IOmap
↓
WKC check
↓
controller
↓
write IOmap
↓
bounded mailbox work
↓
send_processdata
↓
segment → frame slot → raw socket
```

后台：

```text
SDO
state monitor
reconfig
recover
logging
```

## SOEM 最值得带走的设计思想

不是：

```text
EtherCAT 要用这些 API
```

而是下面几条。

### 1. 配置期与周期期必须彻底分层

把 discovery、对象字典、mapping、allocation 尽量留在 cold path。

### 2. 热路径资源要有明确上界

固定 frame slot、segment 数、mailbox pool 都体现了 bounded-resource 思维。

### 3. wire identifier 最好直接服务本地 bookkeeping

frame index 同时做 wire/local slot key，减少额外 lookup。

### 4. process image 是协议语义和控制算法之间的 ABI

上层算法不应该每周期理解 EtherCAT 对象字典。

### 5. 通信恢复不是机器人安全策略

Master library 能恢复链路，但不能替业务决定恢复后是否重新使能执行器。

### 6. “实时库”不等于“实时系统”

线程、内核、IRQ、NIC、内存、控制算法 WCET 都必须一起分析。

## 读完 SOEM 后应该能回答什么

如果现在让你不看代码解释 SOEM，至少应该能完整回答：

```text
为什么有 ecx_contextt？
为什么 slavelist 是固定数组？
为什么 frame 有 index？
为什么 return frame 可以乱序？
为什么 IOmap 由应用提供？
为什么 mapping 要先找 PDO 再写 SM/FMMU？
为什么 process-data send/receive 拆开？
为什么 SDO 可以 blocking？
为什么 mailbox 要 pool + queue + ticket？
为什么 DC 要端口时间戳？
为什么 redundancy 需要两边 route marker？
为什么 WKC 异常处理属于应用 policy？
```

如果这些问题都能从第一性原理回答，就已经不再只是“会用 SOEM”。

你已经开始站在 Master 实现者的角度理解它。
