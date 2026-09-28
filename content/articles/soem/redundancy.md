# 双网口冗余：SOEM 怎样利用两条路径的返回 MAC 标记判断断点，并在必要时重发半条链路

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

SOEM 的 redundancy 不是简单：

```text
同一个 frame 两张网卡各发一次
谁先回来用谁
```

它真正利用的是 EtherCAT ring/line 的传播路径。

## `ecx_init_redundant()` 先建立两套 receive stack

~~~c
context->port.redport = redport;

ecx_setupnic(
   &context->port,
   ifname,
   FALSE);

rval = ecx_setupnic(
   &context->port,
   if2name,
   TRUE);
~~~

primary port 持有主 TX buffer。

secondary port 有自己独立的：

```text
socket
rxbuf[]
rxbufstat[]
rxsa[]
temp buffer
```

## secondary 为什么准备一个 dummy BRD frame

固定：

~~~c
ehp =
   (ec_etherheadert *)
   &(context->port.txbuf2);

ehp->sa1 = oshw_htons(secMAC[0]);

zbuf = 0;

ecx_setupdatagram(
   &context->port,
   &(context->port.txbuf2),
   EC_CMD_BRD,
   0,
   0x0000,
   0x0000,
   2,
   &zbuf);
~~~

正常 operation 下：

- primary 发真正 EtherCAT work frame；
- secondary 发 dummy frame。

通过两边回来时的 source marker，可以推断 frame 实际从哪条路径绕回来。

## 为什么 source MAC 被当 route marker

前面已经看到 SOEM 的 source MAC 不是普通“主机身份”。

冗余模式里会记录：

~~~c
(*stack->rxsa)[idx] =
   ntohs(ehp->sa1);
~~~

随后 `ecx_waitinframe_red()` 得到：

~~~c
primrx = port->rxsa[idx];
secrx = port->redport->rxsa[idx];
~~~

于是：

```text
哪个 socket 收到
+
收到的 frame 携带哪个 source marker
```

共同描述路径。

## 正常冗余路径是什么

源码判断：

~~~c
if ((primrx == RX_SEC) &&
    (secrx == RX_PRIM))
{
   memcpy(
      &(port->rxbuf[idx]),
      &(port->redport->rxbuf[idx]),
      port->txbuflength[idx] -
         ETH_HEADERSIZE);

   wkc = wkc2;
}
~~~

这意味着两个 frame 经过完整 ring 后交叉回到另一侧，是预期的正常路径。

## partial connection 时为什么要重发

更有意思的分支：

~~~c
if (((primrx == 0) &&
     (secrx == RX_SEC)) ||
    ((primrx == RX_PRIM) &&
     (secrx == RX_SEC)))
{
   ...
   ecx_outframe(port, idx, 1);
   ...
}
~~~

如果 primary/secondary 各自只走了总线的一部分，SOEM会把已经从 primary 收到的部分结果复制回 TX，然后从 secondary 再发。

目标是让第二次 frame：

> 从另一端继续穿过剩余从站，最终形成包含完整处理结果的 frame。

这比“任取一条链路返回结果”复杂得多。

## 为什么代码需要复制 primary RX 回 TX

固定：

~~~c
memcpy(
   &(port->txbuf[idx][ETH_HEADERSIZE]),
   &(port->rxbuf[idx]),
   port->txbuflength[idx] -
      ETH_HEADERSIZE);
~~~

因为 EtherCAT 是 on-the-fly processing。

primary 已经经过的那一半 slave 可能已经修改了 payload/WKC。

如果 secondary 重发仍然使用原始 TX frame，就会丢掉那一半已经执行过的结果。

所以要：

```text
partial processed RX
→ become new TX
→ send through other half
```

这就是 EtherCAT 冗余算法和普通 IP failover 很不同的地方。

## ppoll 为什么以 50 us 为小步

Linux 实现中：

~~~c
timeout_spec.tv_nsec = 50 * 1000;
poll_err = ppoll(...);
~~~

它不是一次把整个 EtherCAT timeout 睡完。

而是短步 poll，并在每轮检查：

- primary 是否已到；
- secondary 是否已到；
- absolute timer 是否超时。

这减少纯 busy polling，但仍保留较细响应粒度。

## redundancy 会增加哪些实时成本

至少包括：

- 第二 socket；
- 第二 receive buffer set；
- 两边 poll；
- route decision；
- 必要时 payload copy；
- 故障情况下 secondary resend；
- 更长的异常周期。

所以冗余不是“免费可靠性”。

正常周期和故障周期应分别做 WCET/jitter 测量。

## 与 IgH 的设计差异

IgH 也有 main/backup device 和 datagram pair，但其实现围绕：

```text
Datagram Pair
→ main/backup payload
→ domain processing
```

SOEM更直接地把 redundancy 收进：

```text
port
rx slot
source marker
waitinframe decision tree
```

这再次体现两个主站不同的核心抽象：

- IgH：事务对象；
- SOEM：固定 frame slot。
