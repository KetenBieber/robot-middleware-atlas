# 真实用法基线：SOEM 官方 ec_sample 怎样组织周期、Mailbox 与故障恢复

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

这一页不是“教你照抄 sample”，而是把官方 `samples/ec_sample/ec_sample.c` 当成 **SOEM 原生使用方式的基准线**。后面看机器人项目时，所有工程抽象都可以追问：它最终是不是仍然在组织初始化、IOmap、WKC、DC、Mailbox、周期收发与恢复？

## 1. 启动阶段就是 SOEM 主线的最短版本

~~~c
int rv = ecx_init(&ctx, ifname);
if (rv)
{
   ecx_config_init(&ctx);
   if (ctx.slavecount > 0)
   {
      ec_groupt *group = &ctx.grouplist[0];

      ecx_config_map_group(&ctx, IOmap, 0);
      expectedWKC =
         (group->outputsWKC * 2) +
         group->inputsWKC;

      ecx_configdc(&ctx);
      ...
   }
}
~~~

这几行已经把前面源码专题压成：

~~~text
ecx_contextt
→ NIC
→ Slave Discovery
→ PDO/SM/FMMU
→ IOmap
→ expected WKC
→ Distributed Clocks
~~~

工程里无论包多少层，最终都绕不开这条链。

## 2. 周期线程不是“send 然后睡觉”

官方 RT thread 的主线：

~~~c
osal_get_monotonic_time(&ts);
ecx_send_processdata(&ctx);

while (1)
{
   add_time_ns(&ts, cycletime + toff);
   osal_monotonic_sleep(&ts);

   if (dorun > 0)
   {
      cycle++;

      wkc = ecx_receive_processdata(
         &ctx,
         EC_TIMEOUTRET);

      if (wkc != expectedWKC)
         dowkccheck++;
      else
         dowkccheck = 0;

      if (ctx.slavelist[0].hasdc &&
          (wkc > 0))
      {
         ec_sync(
            ctx.DCtime,
            cycletime,
            &toff);
      }

      ecx_mbxhandler(&ctx, 0, 4);
      ecx_send_processdata(&ctx);
   }
}
~~~

顺序其实是：

~~~text
上一周期 send
↓
绝对时刻唤醒
↓
receive
↓
WKC
↓
DC phase correction
↓
有界 mailbox work
↓
下一周期 send
~~~

这就是 SOEM split-phase process-data 在应用层最直接的落地。

## 3. Mailbox 为什么只给一个明确预算

~~~c
ecx_mbxhandler(&ctx, 0, 4);
~~~

这里的 `4` 表达的是：周期线程可以推进 mailbox，但 acyclic work 不能无限吞掉一个控制周期。

## 4. WKC 为什么直接进入恢复线程

周期线程只做快速计数：

~~~c
if (wkc != expectedWKC)
   dowkccheck++;
else
   dowkccheck = 0;
~~~

另一条监控线程再做重检查：

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

这已经是最小的：

~~~text
cyclic data plane
+
recovery control plane
~~~

分离。

## 5. 恢复动作分级

官方 sample 后续按状态分别处理：

~~~text
SAFE_OP + ERROR → ACK
SAFE_OP         → request OP
reachable       → ecx_reconfig_slave
lost            → ecx_recover_slave
~~~

而不是看到一个异常就重启整个 Master。

## 6. 为什么它仍然不是机器人控制架构

官方 sample 没有替你解决：

- 每个 slave 的设备抽象；
- CiA‑402；
- 多执行器 ownership；
- ROS/控制器接口；
- 数据单位；
- command limit；
- fault policy；
- 设备生命周期；
- 安全联锁。

所以它最适合作为 **SOEM 原生 runtime semantics 的基准线**。后面的真实工程案例，就是看项目怎样在不破坏这条语义主线的前提下，把 SOEM 包装成更高层软件架构。
