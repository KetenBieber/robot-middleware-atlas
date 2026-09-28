# Distributed Clocks：SOEM 怎样从端口时间戳重建拓扑传播延迟，再把 DC Time 塞进周期 Frame

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

SOEM 的 DC 实现非常适合学习，因为它把三个层次放在同一个文件里：

1. 发现哪些 slave 支持 DC；
2. 计算拓扑和 propagation delay；
3. 配置 Sync0/Sync1；
4. 周期里读取 reference DC time。

## configdc 先让所有 DC receive time latch

固定入口：

~~~c
ecx_BWR(
   &context->port,
   0,
   ECT_REG_DCTIME0,
   sizeof(ht),
   &ht,
   EC_TIMEOUTRET);
~~~

这是 broadcast write，用于触发所有从站锁存 DC receive time。

随后每个 DC slave 可以读：

```text
DCTIME0
DCTIME1
DCTIME2
DCTIME3
```

对应四个端口的到达时间信息。

## 为什么先做 host epoch 转换

SOEM：

~~~c
mastertime = osal_current_time();

mastertime.tv_sec -= 946684800UL;

mastertime64 =
   ((uint64)mastertime.tv_sec *
    1000 * 1000 * 1000) +
   (uint64)mastertime.tv_nsec;
~~~

注释指出：

```text
Unix epoch      1970-01-01
EtherCAT epoch  2000-01-01
```

所以不能直接把 Unix time 原样写进 DC offset 计算。

## 第一个 DC slave 成为 group reference chain 起点

第一次遇到 `hasdc`：

~~~c
context->slavelist[0].hasdc = TRUE;
context->slavelist[0].DCnext = i;

context->slavelist[i].DCprevious = 0;

context->grouplist[
   context->slavelist[i].group
].hasdc = TRUE;

context->grouplist[
   context->slavelist[i].group
].DCnext = i;
~~~

后续 DC slave 则串起来：

~~~c
context->slavelist[prevDCslave].DCnext = i;
context->slavelist[i].DCprevious = prevDCslave;
~~~

所以这里直接构造了一条 DC slave chain。

## 为什么要读四个端口时间

固定字段：

```text
DCrtA
DCrtB
DCrtC
DCrtD
```

来自：

~~~c
ECT_REG_DCTIME0
ECT_REG_DCTIME1
ECT_REG_DCTIME2
ECT_REG_DCTIME3
~~~

SOEM把 active port 与 timestamp 放进两个小数组：

~~~c
int8 plist[4];
int32 tlist[4];
~~~

然后选最早到达的 active port 作为 `entryport`。

这实际上是在利用帧传播的物理顺序恢复：

> 帧是从哪个端口进入这个 ESC 的。

## propagation delay 为什么不是“slave index × 常数”

真实拓扑可能：

```text
Master
  |
Slave A
  |\
  | Slave B
  |
Slave C
```

不是简单线性链。

SOEM 会根据：

- parent；
- parentport；
- entryport；
- topology；
- 四端口时间差；
- previous child；

计算当前 slave 的累计 `pdelay`。

核心公式：

~~~c
context->slavelist[i].pdelay =
   ((dt3 - dt1) / 2) +
   dt2 +
   context->slavelist[parent].pdelay;
~~~

代码还明确写了一个假设：

~~~text
forward delay equals return delay
~~~

这就是传播延迟估计的模型条件，不能把它包装成无条件真值。

## 计算完以后直接写 DCSYSDELAY

~~~c
ht = htoel(
   context->slavelist[i].pdelay);

ecx_FPWR(
   &context->port,
   slaveh,
   ECT_REG_DCSYSDELAY,
   sizeof(ht),
   &ht,
   EC_TIMEOUTRET);
~~~

所以 `pdelay` 不是只拿来显示。

它最终被编程进 ESC 的 DC compensation register。

## System Offset 也在 configdc 中建立

SOEM先读从站 system offset/time：

~~~c
ecx_FPRD(
   &context->port,
   slaveh,
   ECT_REG_DCSOF,
   sizeof(hrt),
   &hrt,
   EC_TIMEOUTRET);
~~~

然后：

~~~c
hrt =
   htoell(-etohll(hrt) + mastertime64);

ecx_FPWR(
   &context->port,
   slaveh,
   ECT_REG_DCSYSOFFSET,
   sizeof(hrt),
   &hrt,
   EC_TIMEOUTRET);
~~~

这一步把 slave clock 拉到接近 Master 提供的时间基准。

## Sync0 什么时候真正开始

`ecx_dcsync0()` 先关闭 cyclic operation，再读 slave local DC time。

然后计算第一个触发点：

~~~c
t =
   ((t1 + SyncDelay) / CyclTime)
   * CyclTime
   + CyclTime
   + CyclShift;
~~~

其中：

~~~c
#define SyncDelay 100000000
~~~

也就是先把首次触发放在未来约 100 ms，再对齐到 cycle multiple，加 shift。

这避免“现在立刻写寄存器，但第一个 trigger 已经错过”的竞态。

## Sync1 是相对 Sync0 的周期关系

`ecx_dcsync01()`：

~~~c
TrueCyclTime =
   ((CyclTime1 / CyclTime0) + 1)
   * CyclTime0;
~~~

随后写：

```text
DCSTART0
DCCYCLE0
DCCYCLE1
DCSYNCACT
```

所以 SOEM 不只是“读取 DC time”，也直接负责配置 slave hardware sync events。

## 周期 DC time 不需要独立 Frame

如果 group `hasdc`，process-data send 的第一帧会：

~~~c
DCO = ecx_adddatagram(
   &context->port,
   &(context->port.txbuf[idx]),
   EC_CMD_FRMW,
   idx,
   FALSE,
   context->slavelist[
      context->grouplist[group].DCnext
   ].configadr,
   ECT_REG_DCSYSTIME,
   sizeof(int64),
   &context->DCtime);
~~~

receive 阶段按 `dcoffset` 拿回：

~~~c
memcpy(
   &le_DCtime,
   &(rxbuf[idx][idxstack->dcoffset[pos]]),
   sizeof(le_DCtime));

context->DCtime =
   etohll(le_DCtime);
~~~

这使 reference time acquisition 与过程数据共用一次 Ethernet frame。

## ec_sample 甚至用 DCtime 调整 Linux 周期相位

官方 sample：

~~~c
if (ctx.slavelist[0].hasdc && (wkc > 0))
{
   ec_sync(
      ctx.DCtime,
      cycletime,
      &toff);
}
~~~

`ec_sync()` 用一个 PI correction 调整下一次 host wakeup offset。

所以这里能清楚看到两层：

```text
slave DC hardware synchronization
+
host cyclic-thread phase correction
```

二者相关，但绝不是同一个东西。

## 与 IgH 对照

IgH 把 DC datagram 做成长生命周期 Master object，并在周期 send queue 中复用。

SOEM则：

- `ecx_configdc()` 直接同步探测/配置；
- process-data frame 运行时追加 FRMW；
- 用 `idxstack.dcoffset` 找回 DC time；
- application 自己决定怎么用 `context->DCtime` 调 host loop。

SOEM 再一次把“协议能力”提供给应用，而没有强制一个完整调度策略。
