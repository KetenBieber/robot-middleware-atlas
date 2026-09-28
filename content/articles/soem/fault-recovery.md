# WKC、掉站与恢复：SOEM 为什么把故障策略放在应用线程，而不是隐藏进一个后台 Master FSM

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

过程数据通信最危险的误区之一是：

> 这一周期 `receive_processdata()` 返回了，就代表所有从站都正常。

不对。

应用首先要看 WKC，然后决定：

- 是一次偶发异常；
- 某个 slave 退到 SAFE_OP；
- slave 进入 SAFE_OP + ERROR；
- station address 丢失；
- slave 真正掉线；
- slave 又重新出现。

SOEM 把这套 policy 很明确地留在应用层。

## expected WKC 怎样得到

官方 sample：

~~~c
expectedWKC =
   (group->outputsWKC * 2) +
   group->inputsWKC;
~~~

这里 output 乘 2 不是随意常数。

在 LRW 语义下，成功写与成功读会对 WKC 有不同贡献；SOEM 用这个公式建立正常过程数据期望。

因此：

```text
actual WKC
vs
expected WKC
```

是每周期最便宜的完整性信号之一。

## 不要一次 WKC 异常就立刻做重配置

官方 `ec_sample`：

~~~c
if (wkc != expectedWKC)
   dowkccheck++;
else
   dowkccheck = 0;
~~~

error thread 则：

~~~c
if (inOP &&
    ((dowkccheck > 2) ||
     ctx.grouplist[currentgroup]
        .docheckstate))
{
   ...
}
~~~

也就是说 sample 至少要求连续多个异常周期才进入重检查。

这体现一个工程事实：

> 故障恢复策略应该有时间维度，而不是把单个异常样本直接升级成重置整个设备。

阈值 `2` 只是 sample 策略，不是普适安全标准。

## 第一层恢复：SAFE_OP + ERROR → ACK

~~~c
if (slave->state ==
    (EC_STATE_SAFE_OP + EC_STATE_ERROR))
{
   slave->state =
      (EC_STATE_SAFE_OP +
       EC_STATE_ACK);

   ecx_writestate(
      &ctx,
      slaveix);
}
~~~

这是：

```text
设备仍在线
+
进入错误状态
→
先 acknowledge error
```

并没有立刻重新 discovery。

## 第二层：SAFE_OP → OP

~~~c
else if (slave->state ==
         EC_STATE_SAFE_OP)
{
   slave->state =
      EC_STATE_OPERATIONAL;

   ecx_writestate(
      &ctx,
      slaveix);
}
~~~

如果只是退回 SAFE_OP，尝试重新推进到 OP。

这比重新配置整台 slave 更轻。

## 第三层：仍然能访问，但配置丢失 → reconfig

~~~c
else if (slave->state >
         EC_STATE_NONE)
{
   if (ecx_reconfig_slave(
          &ctx,
          slaveix,
          EC_TIMEOUTMON)
       >= EC_STATE_PRE_OP)
   {
      slave->islost = FALSE;
   }
}
~~~

`ecx_reconfig_slave()` 会做真正的重新配置工作。

## reconfig 到底重写什么

固定源码先把 slave 拉回 INIT：

~~~c
ecx_FPWRw(
   &context->port,
   configadr,
   ECT_REG_ALCTL,
   htoes(EC_STATE_INIT),
   timeout);
~~~

随后重新写 enabled SyncManager：

~~~c
for (nSM = 0;
     nSM < EC_MAXSM;
     nSM++)
{
   if (context->slavelist[slave]
          .SM[nSM].StartAddr)
   {
      ecx_FPWR(
         &context->port,
         configadr,
         ECT_REG_SM0 +
           nSM * sizeof(ec_smt),
         sizeof(ec_smt),
         &context->slavelist[slave]
             .SM[nSM],
         timeout);
   }
}
~~~

之后推进 PRE_OP / SAFE_OP，并重新执行：

- ENI init commands；
- `PO2SOconfig` hook；
- configured FMMU programming。

所以 reconfig 利用的是 Context 中已经保存的配置编译结果。

它不是从零重新做完整 topology discovery。

## 第四层：slave 看起来完全消失 → 标 lost

sample：

~~~c
ecx_statecheck(
   &ctx,
   slaveix,
   EC_STATE_OPERATIONAL,
   EC_TIMEOUTRET);

if (slave->state ==
    EC_STATE_NONE)
{
   slave->islost = TRUE;
   slave->mbxhandlerstate =
      ECT_MBXH_LOST;

   if (slave->Ibytes)
   {
      memset(
         slave->inputs,
         0x00,
         slave->Ibytes);
   }
}
~~~

这里特别值得注意：

> slave lost 后，sample 主动把该 slave input 清零。

为什么？

因为 IOmap 是一块长期内存。

如果通信断了却不处理：

```text
旧 input value
仍然留在 RAM
```

控制器可能把 stale data 当成最新数据。

所以：

```text
communication invalid
→ data validity policy
```

必须明确。

清零只是 sample 的一个选择；真实系统可能需要：

- valid flag；
- timestamp；
- hold-last-value；
- safe substitute；
- immediate stop。

## recover_slave 与 reconfig_slave 不是一回事

`ecx_recover_slave()` 更底层的问题是：

> 物理位置上的这个从站是不是之前那个设备？它的 configured address 能不能恢复？

固定函数先用 Auto Increment 地址访问目标物理位置：

~~~c
ADPh = (uint16)(1 - slave);

wkc = ecx_APRD(
   &context->port,
   ADPh,
   ECT_REG_STADR,
   sizeof(readadr),
   &readadr,
   timeout);
~~~

如果 station address 丢失，SOEM先给它临时地址：

~~~c
ecx_APWRw(
   &context->port,
   ADPh,
   ECT_REG_STADR,
   htoes(EC_TEMPNODE),
   timeout);
~~~

然后核 identity：

~~~c
alias
EEPROM product id
manufacturer
revision
~~~

只有身份匹配，才恢复原 configured address。

## 为什么必须核 identity

假设机器人维护时把第 4 个 servo 换成了不同型号。

如果 recovery 只看到：

```text
第4个物理位置重新出现
```

就无条件恢复旧地址和旧映射，可能把旧控制命令解释到不兼容设备上。

SOEM 至少检查：

```text
alias
product identity
manufacturer
revision
```

再认定它是预期 slave。

这是一条很重要的安全边界。

## recovery thread 为什么不该和 1 kHz loop 合并

`reconfig_slave()` 包含：

- blocking fixed-position writes；
- statecheck；
- `osal_usleep(5000)`；
- mailbox/ENI hooks；
- SM/FMMU programming。

这显然不是 1 ms hot path 工作。

所以官方 sample 单独开：

~~~c
osal_thread_create(
   &thread1,
   128000,
   &ecatcheck,
   NULL);
~~~

这就是为什么：

```text
data plane
!=
fault recovery control plane
```

## 应用还需要补一层机器人安全状态机

SOEM sample 的 recovery 是通信恢复示例，不是机器人 safety controller。

真实系统还要定义：

```text
WKC drop
→ torque command怎么办？

one joint lost
→ 其余关节继续吗？

DC unlock
→ 是否立即 disable drive？

recovery success
→ 可以自动重新上使能吗？
```

这些都不是 EtherCAT library 可以替机器人应用做出的决定。

## 与 IgH 对照

IgH 内核 Master 自带 Master FSM / Slave FSM，会持续推进扫描、配置与 mailbox 事务。

SOEM则提供：

```text
readstate
statecheck
reconfig_slave
recover_slave
```

再由应用决定：

```text
什么时候调用
调用几次
什么条件停机
什么条件自动恢复
```

因此 SOEM 更透明，也更要求控制软件把 fault policy 写清楚。
