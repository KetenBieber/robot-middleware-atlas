# PDO、SyncManager 与 FMMU：SOEM 怎样把设备对象字典一步步编译成应用 IOmap

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

`ecx_config_map_group()` 最终给应用一块可以按字节访问的 IOmap。

但这个结果不可能凭空出现。

它背后至少需要解决三件事：

```text
设备有哪些 PDO？
这些 PDO 由哪些 SyncManager 搬运？
这些物理区间如何映射到 Master logical address？
```

SOEM 把这条“配置编译链”集中在 `ec_config.c`。

## 第一步：优先从 CoE/SoE 获取运行时映射

固定 `ecx_map_coe_soe()`：

~~~c
if (context->slavelist[slave].mbx_proto &
    ECT_MBXPROT_COE)
{
   rval = 0;

   if (context->slavelist[slave].CoEdetails &
       ECT_COEDET_SDOCA)
   {
      rval = ecx_readPDOmapCA(
         context,
         slave,
         thread_n,
         &Osize,
         &Isize);
   }

   if (!rval)
   {
      rval = ecx_readPDOmap(
         context,
         slave,
         &Osize,
         &Isize);
   }
}
~~~

也就是说，如果从站支持 CoE：

```text
read PDO assignment/mapping through SDO
→ calculate output/input bits
```

这比单纯读 ESI/SII 更接近设备当前实际配置。

## 第二步：拿不到动态映射时回退到 SII

`ecx_map_sii()`：

~~~c
if (!Isize && !Osize)
{
   (void)ecx_lookup_mapping(
      context,
      slave,
      &Osize,
      &Isize);
}

if (!Isize && !Osize)
{
   Isize = ecx_siiPDO(
      context,
      slave,
      &eepPDO,
      0);

   ...

   Osize = ecx_siiPDO(
      context,
      slave,
      &eepPDO,
      1);
}
~~~

这里还有一个优化：

> 如果前面已经发现一个 identity 相同的 slave，可以复用 mapping 信息。

否则才真正读当前 slave 的 SII PDO category。

## 第三步：把 PDO bit size 回写进 Slave Record

最终：

~~~c
context->slavelist[slave].Obits = (uint16)Osize;
context->slavelist[slave].Ibits = (uint16)Isize;
~~~

这两个字段直接参与决定：

- IOmap 占多少 bit/byte；
- 需要几个 FMMU；
- group 的 Obytes/Ibytes；
- expected WKC。

所以配置阶段是在逐渐把“语义对象字典”压缩成“运行时尺寸与地址”。

## 第四步：真正编程 SyncManager

`ecx_map_sm()` 遍历从站 SM：

~~~c
for (nSM = 2; nSM < EC_MAXSM; nSM++)
{
   if (context->slavelist[slave].SM[nSM].StartAddr)
   {
      if (context->slavelist[slave].SM[nSM].SMlength == 0)
      {
         ...
      }
      else
      {
         ...
      }

      ecx_FPWR(
         &context->port,
         configadr,
         (uint16)(ECT_REG_SM0 +
            (nSM * sizeof(ec_smt))),
         sizeof(ec_smt),
         &context->slavelist[slave].SM[nSM],
         EC_TIMEOUTRET3);
   }
}
~~~

因此 `SM[]` 不是只供软件看的 metadata。

最终这些结构会被序列化写到 ESC SyncManager register。

## SMtype 为什么重要

SOEM 约定：

```text
0 unused
1 mailbox write
2 mailbox read
3 outputs
4 inputs
```

后面创建 FMMU 时会按这个类型寻找参与 output/input mapping 的 SM。

所以：

```text
PDO semantic direction
→ SM type
→ FMMU physical region
```

是连着的。

## 第五步：为 Output 建 FMMU

`ecx_config_create_output_mappings()` 先找到 output 类型 SM：

~~~c
while ((SMc < (EC_MAXSM - 1)) &&
       (context->slavelist[slave].SMtype[SMc] != 3))
{
   SMc++;
}
~~~

然后把该 SM 的物理起始地址写进 FMMU：

~~~c
context->slavelist[slave]
   .FMMU[FMMUc].PhysStart =
      context->slavelist[slave].SM[SMc].StartAddr;
~~~

同时根据当前 `LogAddr/BitPos` 决定 logical mapping。

所以 FMMU 的本质再次可以写成：

```text
Master logical byte/bit
        ↕
Slave physical process-memory byte/bit
```

## 连续 SM 为什么可以合并进一个 FMMU

源码会继续扫描后续同方向 SM。

如果：

~~~c
next_SM.StartAddr <= EndAddr
~~~

就把连续物理区间合并进当前 FMMU。

如果出现物理地址空洞，就拆新的 FMMU。

这背后的原则是：

> 一个 FMMU 描述一个连续映射窗口；不连续物理区间不能假装成一个连续窗口。

## bit-oriented slave 为什么更麻烦

如果 PDO 总量不足 1 byte，SOEM 不能只增加整字节地址。

它同时维护：

```text
LogAddr
BitPos
LogStartbit
LogEndbit
```

例如：

```text
slave A output 3 bits
slave B output 2 bits
```

packedMode 下完全可能共享一个逻辑 byte。

这也是为什么业务代码不应该随便把 IOmap cast 成自然对齐 C struct。

## 第六步：把 IOmap pointer 固定下来

创建完第一个 input FMMU 后：

~~~c
if (!context->slavelist[slave].inputs)
{
   context->slavelist[slave].inputs =
      (uint8 *)(pIOmap) +
      etohl(context->slavelist[slave]
               .FMMU[FMMUc].LogStart)
      - context->grouplist[group].logstartaddr;

   context->slavelist[slave].Istartbit =
      context->slavelist[slave]
         .FMMU[FMMUc].LogStartbit;
}
~~~

output 方向同理。

因此应用最后得到的：

~~~c
context->slavelist[i].inputs
context->slavelist[i].outputs
~~~

已经是**编译后的直接访问指针**。

周期里不再需要：

```text
PDO index lookup
→ SM lookup
→ FMMU lookup
```

## mapping 甚至可以并行

`ecx_config_find_mappings()` 在 `EC_MAX_MAPT > 1` 时可以为不同 slave 启动 mapper thread：

~~~c
osal_thread_create(
   &(ecx_threadh[thrn]),
   128000,
   &ecx_mapper_thread,
   &(ecx_mapt[thrn]));
~~~

这进一步说明 mapping 是典型 cold path：

- 可以执行 SDO；
- 可以启动线程；
- 可以等待；
- 可以做复杂对象字典查询。

然后再把结果压成热路径需要的：

```text
pointer
size
segment
WKC expectation
```

## 与 IgH 的对应关系

IgH：

```text
PDO registration
→ Slave Config
→ FMMU config
→ Domain
→ activate
→ process image + datagram pairs
```

SOEM：

```text
CoE/SII mapping discovery
→ SM programming
→ FMMU programming
→ application IOmap pointers
→ IOsegment[]
```

两者最深层的共同原则是：

> **周期控制前，必须先把设备语义编译成稳定的内存布局。**

区别只是这份编译结果由谁拥有。
