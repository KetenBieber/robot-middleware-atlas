# Datagram 原语：SOEM 为什么把 APRD/FPRD/LRW 写成阻塞函数，却把周期过程数据拆成两段

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

读到 `ec_base.c` 时最容易出现一个误解：

> SOEM 既然面向实时通信，为什么 `ecx_APRD()`、`ecx_FPRD()`、`ecx_LRW()` 这些最底层函数反而是 blocking 的？

答案是：**“EtherCAT primitive”与“高频过程数据调度”是两个不同层级的问题。**

SOEM 先提供一套“发一个 Datagram 并等它回来”的同步原语；配置、EEPROM、状态和很多诊断代码可以直接使用它们。真正进入周期过程数据以后，SOEM再绕开这套同步封装，显式拆成：

```text
build
→ send
→ remember
→ build next
→ send next
...
→ receive/reassemble
```

## 先看一个最简单的 APRD

固定源码：

~~~c
int ecx_APRD(ecx_portt *port,
             uint16 ADP,
             uint16 ADO,
             uint16 length,
             void *data,
             int timeout)
{
   int wkc;
   uint8 idx;

   idx = ecx_getindex(port);

   ecx_setupdatagram(
      port,
      &(port->txbuf[idx]),
      EC_CMD_APRD,
      idx,
      ADP,
      ADO,
      length,
      data);

   wkc = ecx_srconfirm(port, idx, timeout);

   if (wkc > 0)
   {
      memcpy(data,
             &(port->rxbuf[idx][EC_HEADERSIZE]),
             length);
   }

   ecx_setbufstat(port, idx, EC_BUF_EMPTY);

   return wkc;
}
~~~

它的生命周期非常完整：

```text
get slot
↓
serialize APRD
↓
send and wait
↓
copy return payload
↓
release slot
↓
return WKC
```

对调用者来说，这就是一个同步 remote register read。

## ecx_setupdatagram 做了什么

固定实现：

~~~c
datagramP->elength =
   htoes(EC_ECATTYPE + EC_HEADERSIZE + length);

datagramP->command = com;
datagramP->index = idx;
datagramP->ADP = htoes(ADP);
datagramP->ADO = htoes(ADO);
datagramP->dlength = htoes(length);

ecx_writedatagramdata(
   &frameP[ETH_HEADERSIZE + EC_HEADERSIZE],
   com,
   length,
   data);

frameP[ETH_HEADERSIZE + EC_HEADERSIZE + length] = 0x00;
frameP[ETH_HEADERSIZE + EC_HEADERSIZE + length + 1] = 0x00;

port->txbuflength[idx] =
   ETH_HEADERSIZE +
   EC_HEADERSIZE +
   EC_WKCSIZE +
   length;
~~~

这个函数本质上是一个 wire serializer：

```text
command
index
ADP
ADO
payload length
payload
WKC=0
```

被直接写进固定 frame slot。

SOEM 没有为 Datagram 建一个类似 IgH `ec_datagram_t` 的长期对象。

Datagram 的“对象状态”被拆散在：

```text
txbuf[idx]
rxbuf[idx]
rxbufstat[idx]
txbuflength[idx]
wire index
```

里面。

## ecx_adddatagram 为什么重要

一个 Ethernet frame 里可以携带多个 EtherCAT Datagram。

SOEM 提供：

~~~c
uint16 ecx_adddatagram(...)
~~~

它会：

1. 取当前 frame 长度；
2. 给前一个 Datagram 设置 `EC_DATAGRAMFOLLOWS`；
3. 在 frame 尾部写新 Datagram header；
4. 写 payload；
5. 清新 Datagram 的 WKC；
6. 扩大 `txbuflength[idx]`；
7. 返回新 Datagram 在 RX frame 内的数据 offset。

所以：

```text
Ethernet Frame
├─ Datagram A
├─ Datagram B
└─ Datagram C
```

在 SOEM 里可以在同一块 `txbuf[idx]` 上原地长出来。

周期 DC time 的 FRMW 就是这样被附加到第一帧上的。

## srconfirm 才是 blocking 的核心

`ecx_srconfirm()`：

~~~c
osal_timer_start(&timer1, timeout);

do
{
   ecx_outframe_red(port, idx);

   if (timeout < EC_TIMEOUTRET)
   {
      osal_timer_start(&timer2, timeout);
   }
   else
   {
      osal_timer_start(&timer2, EC_TIMEOUTRET);
   }

   wkc = ecx_waitinframe_red(port, idx, &timer2);

} while ((wkc <= EC_NOFRAME) &&
         !osal_timer_is_expired(&timer1));
~~~

因此 blocking 不等于：

```text
sleep forever until response
```

而是：

```text
bounded retry until absolute timeout
```

但它仍然会占用调用线程的时间预算。

所以如果把一个 `ecx_SDOread()` 或 `ecx_FPRD()` 随意塞进 1 kHz RT loop，最坏执行时间会立刻受到 mailbox/timeout 路径影响。

## APRD、FPRD、LRD 的差别首先是寻址语义

这些函数的骨架非常相似：

```text
getindex
→ setupdatagram(command)
→ srconfirm
→ copy return if needed
→ EMPTY
```

真正变化的是 command/address：

### APRD / APWR

Auto Increment Position Addressing。

适合：

- 尚未分配 station address 时；
- 按物理拓扑位置访问从站。

### FPRD / FPWR

Configured Addressing。

适合：

- discovery 完成后；
- 已知某个 slave 的 configured station address。

### LRD / LWR / LRW

Logical Addressing。

适合：

- FMMU 已经把逻辑地址映射到各从站过程区；
- process image / IOmap 访问。

这正好对应 EtherCAT Master 从启动到周期运行的地址语义演进：

```text
unknown topology
    ↓
Auto Increment

configured slaves
    ↓
Fixed Position

compiled process image
    ↓
Logical Address
```

## 为什么 process data 不直接循环调用 ecx_LRW()

`ecx_LRW()` 本身也是 blocking primitive。

如果 IOmap 被切成 3 段，然后写：

~~~c
ecx_LRW(segment0);
ecx_LRW(segment1);
ecx_LRW(segment2);
~~~

就变成：

```text
send 0
wait 0

send 1
wait 1

send 2
wait 2
```

总线 pipeline 被人为串行化。

SOEM 的 process-data path 则是：

```text
send 0
send 1
send 2

wait/copy 0
wait/copy 1
wait/copy 2
```

因此 `ecx_send_processdata_group()` 不调用同步 `ecx_LRW()`，而是直接使用：

```text
ecx_getindex
ecx_setupdatagram
ecx_outframe_red
ecx_pushindex
```

这个设计非常关键：

> **底层同步原语是方便的控制面积木；周期数据面则拆掉同步封装，以允许多帧在途。**

## 这和 IgH 有什么不同

IgH 的思想更统一：

```text
long-lived ec_datagram_t
→ queue
→ send
→ receive state transition
```

配置 FSM 也尽量围绕异步 Datagram 推进。

SOEM 则明确保留两种编程风格：

```text
configuration / simple acyclic access:
    blocking primitive

cyclic process data:
    split-phase send/receive
```

所以 SOEM 的源码更短，但“不要在 RT loop 调错 API”的责任也更直接落给应用开发者。

## 对机器人控制代码的直接规则

1 kHz loop 里优先只保留：

```text
receive_processdata
read IOmap
control
write IOmap
mailbox handler with explicit bounded budget, if designed
send_processdata
```

不要随意加入：

```text
SDOread
EEPROM read
slave scan
reconfigure
blocking FP read
```

除非你已经把这些操作的 worst-case latency 纳入周期预算，并确认这是你真正想要的执行语义。
