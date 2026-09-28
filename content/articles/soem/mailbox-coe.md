# Mailbox 与 CoE/SDO：SOEM 2.0 怎样把阻塞对象字典访问和周期 Mailbox Handler 放在同一套 Buffer Pool 上

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

PDO 是高频过程数据。

SDO 则解决另一类问题：

```text
读参数
写模式
配置 PDO
读取 identity
下载较大对象
```

这些事务不应该被理解成“另一个 PDO”。

SOEM 2.0 的 mailbox 代码很值得研究，因为它同时保留：

- blocking SDO API；
- 固定 mailbox buffer pool；
- cyclic mailbox handler；
- 多线程访问需要的 mutex/queue/ticket。

## Mailbox buffer 先做成固定池

Context 中：

~~~c
typedef uint8 ec_mbxbuft[EC_MAXMBX + 1];

typedef struct
{
   int listhead, listtail, listcount;
   int mbxemptylist[EC_MBXPOOLSIZE];
   osal_mutext *mbxmutex;
   ec_mbxbuft mbx[EC_MBXPOOLSIZE];
} ec_mbxpoolt;
~~~

初始化：

~~~c
mbxpool->mbxmutex =
   (osal_mutext *)osal_mutex_create();

for (int item = 0;
     item < EC_MBXPOOLSIZE;
     item++)
{
   mbxpool->mbxemptylist[item] = item;
}

mbxpool->listhead = 0;
mbxpool->listtail = 0;
mbxpool->listcount = EC_MBXPOOLSIZE;
~~~

所以大 mailbox buffer 的生命周期不是：

```text
SDO request
→ malloc
→ free
```

而是：

```text
preallocated pool
→ get index
→ use
→ return index
```

## get/drop 为什么需要 mutex

多个应用线程可能同时做 SDO/SoE/FoE。

因此 pool 的：

```text
head
tail
count
empty index ring
```

必须同步更新。

SOEM OSAL 的 Linux mutex 使用 priority inheritance，这至少避免一个低优先级持锁线程无限制造经典 priority inversion 的最简单情况。

但仍然要注意：

> priority inheritance 降低优先级反转风险，不等于临界区自动变成零等待。

## SDOread 是明确的 blocking API

源码注释直接写：

~~~text
CoE SDO read, blocking.
~~~

入口先取 mailbox：

~~~c
MbxOut = ecx_getmbx(context);
if (!MbxOut)
   return wkc;
~~~

随后构造 CoE SDO Upload Request：

~~~c
SDOp->MbxHeader.mbxtype =
   ECT_MBXT_COE + MBX_HDR_SET_CNT(cnt);

SDOp->CANOpen =
   htoes(ECT_COES_SDOREQ << 12);

SDOp->Command = ECT_SDO_UP_REQ;
SDOp->Index = htoes(index);
SDOp->SubIndex = subindex;
~~~

再：

~~~c
wkc = ecx_mbxsend(
   context,
   slave,
   MbxOut,
   EC_TIMEOUTTXM);
~~~

然后等 response。

这整个过程可以跨多个 EtherCAT frame 和多个 mailbox 周期。

## 小对象为什么有 expedited 模式

如果 SDO payload 很小，协议允许数据直接放在 SDO command frame 内。

对于 read，SOEM检查：

~~~c
if ((aSDOp->Command & 0x02) > 0)
{
   bytesize =
      4 - ((aSDOp->Command >> 2) & 0x03);

   memcpy(p,
          &aSDOp->ldata[0],
          bytesize);
}
~~~

write 方向则：

~~~c
if ((psize <= 4) && !CA)
{
   ...
   SDOp->Command =
      ECT_SDO_DOWN_EXP |
      (((4 - psize) << 2) & 0x0c);
}
~~~

这避免为了 1~4 byte 参数再跑 segmented transfer。

## 大对象为什么必须 segmented

当 SDO 数据比 mailbox payload 大：

```text
request/response
→ segment 0
→ segment 1
→ ...
→ last segment
```

SOEM自己处理：

- toggle bit；
- segment size；
- output buffer pointer；
- last segment；
- abort frame；
- timeout。

所以应用看到：

~~~c
ecx_SDOread(...)
~~~

像一个普通同步函数，但底下可能已经完成多轮总线交互。

这正是它不适合随意进入 1 kHz RT hot path 的原因。

## cyclic mailbox 模式解决什么

SOEM 2.0 增加/强化了 cyclic mailbox handling。

应用可以：

~~~c
ecx_slavembxcyclic(context, slave);
~~~

如果该 slave 有 mailbox status：

~~~c
context->slavelist[slave].mbxhandlerstate =
   ECT_MBXH_CYCLIC;
~~~

此后 `ecx_mbxsend()` 不一定直接自己 FPWR。

它会把 mailbox 放进 group queue：

~~~c
ticket = ecx_mbxaddqueue(
   context,
   slave,
   mbx);
~~~

然后等待 handler 把 ticket 标为 DONE。

## mailbox queue 为什么还需要 ticket

固定 queue 保存：

```text
mbx[]
mbxstate[]
mbxremove[]
mbxticket[]
mbxslave[]
head/tail/count
```

ticket 的作用是把：

```text
调用者持有的请求句柄
```

和：

```text
ring 中不断旋转的位置
```

分离开。

否则 queue rotation 以后，调用者记住的数组位置就不再稳定。

这是非常经典的：

> logical handle 与 physical queue slot 分离。

## ec_sample 怎样使用 cyclic mailbox

官方 sample 在配置结束后：

~~~c
for (int si = 1;
     si <= ctx.slavecount;
     si++)
{
   ec_slavet *slave = &ctx.slavelist[si];

   if (slave->CoEdetails > 0)
   {
      ecx_slavembxcyclic(&ctx, si);
   }
}
~~~

而 RT EtherCAT thread 每周期：

~~~c
ecx_mbxhandler(&ctx, 0, 4);
~~~

这里的 `limit=4` 非常有启发性。

它体现了：

> 即使把 mailbox 推进放进周期 thread，也应该给它明确预算，而不是让 acyclic work 无界吞掉整个周期。

## 但 SDOread 本身仍然可以阻塞另一个线程

官方 sample 还专门展示：

~~~c
int sdo_wkc =
   ecx_SDOread(
      &ctx,
      sdoslave,
      0x1018,
      0x02,
      FALSE,
      &size,
      &value,
      EC_TIMEOUTRXM);
~~~

注释写的是：

~~~text
Demonstrate SDO access from other threads
~~~

这说明 SOEM 2.0 的设计意图很明确：

```text
RT thread:
   process data
   bounded mailbox handler work

other thread:
   synchronous SDO API
```

这是比“所有事务都塞进同一个 RT loop”合理得多的线程划分。

## 与 IgH FSM 最大的差别

IgH 倾向于：

```text
mailbox operation
→ FSM step
→ datagram pending
→ return
→ later resume
```

SOEM则仍然为上层提供：

```text
ecx_SDOread()
→ caller blocks until done/timeout
```

但底层 mailbox handler/queue 允许实际 wire work 与其他线程协调。

因此：

- IgH 把异步状态机复杂度更多收进 Master runtime；
- SOEM 把“同步 API 是否放错线程”的责任更多留给应用。

这是两种完全不同的工程哲学。
