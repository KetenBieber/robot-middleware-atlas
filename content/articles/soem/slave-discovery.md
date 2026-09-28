# Slave Discovery：SOEM 怎样从一块空网卡建立 slavelist[]

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

`ecx_config_init()` 是理解 SOEM 配置面的第一扇门。

它做的不是“读一个 XML”，而是直接和总线上真实 ESC 交互，建立 `slavelist[]`。

## 入口先重置 Context 再探测

固定实现：

~~~c
int ecx_config_init(ecx_contextt *context)
{
   ...

   ecx_init_context(context);
   wkc = ecx_detect_slaves(context);

   if (wkc > 0)
   {
      ecx_set_slaves_to_default(context);

      for (slave = 1;
           slave <= context->slavecount;
           slave++)
      {
          ...
      }
   }
   ...
}
~~~

所以 discovery 不是在旧状态上增量 append。

先把 Master runtime 恢复到配置初始状态，然后重新探测。

## 为什么 WKC 能拿来当“发现了几个从站”

EtherCAT broadcast/auto-increment 命令经过每个满足条件的 ESC 时会影响 Working Counter。

SOEM 利用这种协议特性做 topology discovery。

因此这里的 WKC 不只是周期健康指标，也能作为配置阶段的探测结果。

## 从站使用 Auto Increment Address 遍历

循环中：

~~~c
ADPh = (uint16)(1 - slave);

val16 = ecx_APRDw(&context->port,
                  ADPh,
                  ECT_REG_PDICTL,
                  EC_TIMEOUTRET3);
~~~

Auto Increment addressing 允许 Master 不提前知道 station address，就按物理顺序访问从站。

随后 SOEM 给每个从站写 station address：

~~~c
ecx_APWRw(&context->port,
          ADPh,
          ECT_REG_STADR,
          htoes(slave + EC_NODEOFFSET),
          EC_TIMEOUTRET3);
~~~

然后重新读回来：

~~~c
configadr = ecx_APRDw(... ECT_REG_STADR ...);
context->slavelist[slave].configadr = etohs(configadr);
~~~

于是：

```text
物理顺序
→ auto increment position
→ configured station address
→ slavelist[index]
```

建立了稳定关联。

## discovery 不是只数节点

每个 `ec_slavet` 最终还要填：

- interface type；
- alias；
- EEPROM/SII identity；
- mailbox offsets 与协议；
- SyncManager；
- FMMU capability；
- process-data bit size；
- DC capability；
- topology/ports；
- E-bus current；
- name。

所以 `ecx_config_init()` 更准确的理解是：

> **把 wire topology 编译成一组可供后续 mapping 使用的 slave records。**

## 为什么读 SII

Slave Information Interface 中保存设备的静态描述。

SOEM 后续需要知道：

```text
这个从站有哪些 SM？
支持哪些 mailbox protocol？
默认 PDO 如何映射？
FMMU 能做什么？
```

否则 Master 不可能凭空构造 IOmap。

所以 discovery 是：

```text
wire-level presence detection
+
SII/EEPROM capability discovery
```

## 配置地址与数组下标不要混

SOEM 程序常写：

~~~c
context->slavelist[slave]
~~~

这里的 `slave` 是本地数组编号。

真正 wire 上 fixed-position command 使用的是：

~~~c
context->slavelist[slave].configadr
~~~

二者不能混为一谈。

例如第 3 个被发现的 slave：

```text
array index = 3
configured address = 某个写入 ESC STADR 的值
```

## 与 IgH 对照

IgH 有长期 `ec_slave_t` 对象和 Master FSM 周期性扫描/重新扫描。

SOEM 的主路径则更显式：

```text
application calls ecx_config_init
    ↓
blocking discovery/config operations
    ↓
slavelist[] prepared
```

这说明 SOEM 把更多“何时重新扫描/恢复”的策略交给应用。

官方 `simple_ng` 也明确在 WKC/state 异常时调用：

```text
ecx_reconfig_slave
ecx_recover_slave
```

而不是依赖一个永远运行的内核 Master FSM 自动替应用处理全部策略。
