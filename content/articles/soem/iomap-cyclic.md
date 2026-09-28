# IOmap 与周期收发：SOEM 怎样把 PDO/FMMU 编译成一块应用内存，再用 LRW 循环刷新

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

这是 SOEM 最核心的一篇。

最终控制器只想做：

```text
read input bytes
compute control
write output bytes
```

SOEM 要解决的问题是：

> 怎样把一堆从站的 PDO/SyncManager/FMMU，变成应用里一块连续 IOmap，并在每个周期把它与总线同步？

## IOmap 由应用提供

官方 `simple_ng`：

~~~c
typedef struct
{
   ecx_contextt context;
   ...
   uint8 map[4096];
} Fieldbus;
~~~

随后：

~~~c
ecx_config_map_group(context,
                     fieldbus->map,
                     fieldbus->group);
~~~

这和 IgH 很不一样。

IgH Domain 最终通过 mmap 暴露 process image。

SOEM 则让应用直接提供 buffer。

## mapping 后 slave 直接保存 IOmap pointer

每个 `ec_slavet` 有：

~~~c
uint8 *outputs;
uint32 Ooffset;
uint8 Ostartbit;

uint8 *inputs;
uint32 Ioffset;
uint8 Istartbit;
~~~

因此配置完成后：

```text
slave semantic PDO
    ↓
FMMU logical mapping
    ↓
application IOmap offset
    ↓
slavelist[i].outputs / inputs
```

应用无需周期里再查询 PDO tree。

## group 同时保存周期调度元数据

`ec_groupt`：

~~~c
uint32 Obytes;
uint8 *outputs;

uint32 Ibytes;
uint8 *inputs;

uint16 nsegments;
uint16 Isegment;
uint16 Ioffset;
uint32 IOsegment[EC_MAXIOSEGMENTS];

uint16 outputsWKC;
uint16 inputsWKC;
~~~

这说明 `ecx_config_map_group()` 不只是算地址。

它还提前生成：

- 周期要发多少数据；
- 要切几段；
- input 从哪开始；
- expected WKC 怎么算。

## 为什么 segment 不能随便切

源码注释：

~~~c
/** IO segmentation list. Datagrams must not break SM in two. */
uint32 IOsegment[EC_MAXIOSEGMENTS];
~~~

因此 segment 边界不仅受 Ethernet/datagram 最大尺寸限制，还受 SyncManager 语义约束。

不能为了凑 MTU 在一个 SM 过程区中间任意断开。

## 周期 Send 为什么是非阻塞的

`ecx_send_processdata_group()` 的注释明确说明：

> 与 base LRW primitive 不同，这个函数是 non-blocking；如果 process data 装不进一个 datagram，就使用多个 datagram，并用 stack 在 receive 阶段重组。

核心路径：

~~~c
idx = ecx_getindex(&context->port);

ecx_setupdatagram(
    &context->port,
    &(context->port.txbuf[idx]),
    EC_CMD_LRW,
    idx,
    w1,
    w2,
    sublength,
    data);

ecx_outframe_red(&context->port, idx);

ecx_pushindex(
    context,
    idx,
    (data + iomapinputoffset),
    sublength,
    DCO);
~~~

这里完成：

```text
precomputed segment
→ acquire frame slot
→ build LRW
→ send
→ remember how response maps back to IOmap
```

然后立刻继续下一 segment，而不是每发一帧就同步等回来。

## blockLRW 时为什么拆成 LRD + LWR

某些设备/配置不能使用 LRW。

SOEM 检查：

~~~c
if (context->grouplist[group].blockLRW)
{
   ...
}
else
{
   ... EC_CMD_LRW ...
}
~~~

blocked 分支会分别构造：

```text
LRD inputs
LWR outputs
```

所以应用的 IOmap abstraction 不变，wire command strategy 可以变化。

这就是好的层次隔离：

```text
control code
    sees IOmap

SOEM data plane
    chooses LRW or LRD/LWR
```

## receive 阶段根据 idxstack 回填

`ecx_receive_processdata_group()`：

~~~c
pos = ecx_pullindex(context);

while (pos >= 0)
{
   idx = idxstack->idx[pos];

   wkc2 = ecx_waitinframe(
       &context->port,
       idx,
       timeout);

   if (wkc2 > EC_NOFRAME)
   {
      if ((rxbuf[idx][EC_CMDOFFSET] == EC_CMD_LRD) ||
          (rxbuf[idx][EC_CMDOFFSET] == EC_CMD_LRW))
      {
         memcpy(idxstack->data[pos],
                &(rxbuf[idx][EC_HEADERSIZE]),
                idxstack->length[pos]);

         wkc += wkc2;
      }
   }

   ecx_setbufstat(
       &context->port,
       idx,
       EC_BUF_EMPTY);

   pos = ecx_pullindex(context);
}

ecx_clearindex(context);
~~~

所以过程数据闭环真正是：

```text
IOmap output bytes
    ↓ send
txbuf slots
    ↓ wire
rxbuf slots
    ↓ idxstack tells destination
IOmap input bytes
```

## WKC 为什么直接从 receive 返回

official sample：

~~~c
ecx_send_processdata(context);
wkc = ecx_receive_processdata(context, EC_TIMEOUTRET);
~~~

随后：

~~~c
expected_wkc =
    grp->outputsWKC * 2 +
    grp->inputsWKC;

if (wkc < expected_wkc)
{
   ...
}
~~~

SOEM 把一次周期的 WKC 直接交给 application。

这也意味着：

> 是否降级、是否触发 recovery、连续多少周期不完整才停机，是应用策略。

## Distributed Clock Time 怎样顺手进入同一帧

如果 group 有 DC，send path 会在第一帧追加一个 FRMW datagram：

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

receive 再按 `dcoffset` 取回：

~~~c
memcpy(&le_DCtime,
       &(rxbuf[idx][idxstack->dcoffset[pos]]),
       sizeof(le_DCtime));

context->DCtime = etohll(le_DCtime);
~~~

所以 DC time 并不是必须单独发一整个 Ethernet frame。

它可以作为 additional datagram 拼进过程数据 frame。

## 与 IgH 最关键的结构对照

IgH：

```text
PDO/FMMU
→ Domain
→ activate builds datagram pairs
→ domain_queue
→ master_send
```

SOEM：

```text
PDO/FMMU
→ application IOmap
→ config builds IOsegment[]
→ send_processdata dynamically fills fixed frame slots
→ receive uses idxstack to copy back
```

两者都把昂贵的语义映射工作放到配置期。

区别是：

- IgH 更进一步长期保存 Datagram object；
- SOEM 周期里仍会根据预计算 segment 重新写 frame buffer header/datagram fields，但复用固定 slot，不做 heap allocation。

这正是后续实时性对照最值得分析的地方。
