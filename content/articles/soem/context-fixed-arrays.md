# Context 与固定数组：SOEM 为什么大量用 bounded array，而不是动态对象图

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

SOEM 的数据结构风格非常统一：

```text
容量先定上界
→ Context 内嵌数组
→ 运行时用 index/pointer
→ 周期里复用
```

如果只把它看成“老 C 写法”，会错过其实时系统意义。

## 容量来自 build options

`include/soem/ec_options.h.in` 中直接把容量做成构建参数：

~~~c
#define EC_BUFSIZE (@EC_BUFSIZE@)
#define EC_MAXBUF (@EC_MAXBUF@)
#define EC_MAXSLAVE (@EC_MAXSLAVE@)
#define EC_MAXGROUP (@EC_MAXGROUP@)
#define EC_MAXIOSEGMENTS (@EC_MAXIOSEGMENTS@)
#define EC_MBXPOOLSIZE (@EC_MBXPOOLSIZE@)
#define EC_MAXSM (@EC_MAXSM@)
#define EC_MAXFMMU (@EC_MAXFMMU@)
~~~

这意味着“最大从站数”“并发 frame slot 数”“mailbox pool 大小”不是运行时容器自己悄悄增长，而是系统配置的一部分。

## slavelist[] 为什么适合连续数组

从站在 discovery 完成后有天然的 1..N 顺序。

因此：

~~~c
ec_slavet slavelist[EC_MAXSLAVE];
~~~

提供：

- O(1) index；
- 连续存储；
- 无节点分配；
- 生命周期和 Context 一致；
- 遍历简单。

对应的缺点：

- 预留空间；
- 上限固定；
- 删除中间元素没有通用容器语义。

但 EtherCAT 拓扑不是普通业务数据库。Master 通常在启动时发现拓扑，随后长期稳定运行。

所以这里更关心：

```text
stable address + bounded memory
```

而不是任意动态增删。

## ec_slavet 本身是“设备状态快照 + 编译结果”

每个 slave object 同时保存：

```text
identity
  eep_man / eep_id / eep_rev

address
  configadr / aliasadr

process data
  Obits / Obytes / outputs
  Ibits / Ibytes / inputs

mapping
  SM[]
  SMtype[]
  FMMU[]

mailbox
  mbx_l / mbx_wo / mbx_rl / mbx_ro
  protocol capability

DC
  topology / ports / pdelay
  DCnext / DCprevious
  DCcycle / DCshift

recovery
  islost / state / ALstatuscode
```

所以它不只是“从站描述信息”。

配置完成后它还成为周期与诊断路径读取的运行时表。

## ec_group 的 IOsegment[] 为什么也是固定数组

过程数据可能超过一个 EtherCAT datagram 可承载的大小。

SOEM 在配置阶段把 IOmap 切成 segment：

~~~c
uint16 nsegments;
uint32 IOsegment[EC_MAXIOSEGMENTS];
~~~

周期里就不需要重新做复杂切包决策：

```text
for each IOsegment
    get frame index
    build LRD/LWR/LRW
    send
```

这和 IgH activate 阶段预生成 Domain datagram pair 有同一个思想：

> **把结构计算尽量放到配置期，把周期期压成遍历预计算结果。**

实现形式不同而已。

## mailbox pool 是固定 buffer pool

SOEM 2.0 里 mailbox 也不是每次 SDO 都 malloc 一个最大 mailbox。

固定：

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

这就是典型 object pool：

```text
preallocated mbx[]
        ↓
empty-index ring
        ↓
getmbx / dropmbx
```

SOEM 2.0 新增/强化了多线程 mailbox cyclic handling，所以这里才需要 mutex 和 ticket/queue。

## 为什么不是 std::vector

如果我们用 C++ 重写，不代表应该机械把：

```c
ec_slavet slavelist[EC_MAXSLAVE];
```

换成：

```cpp
std::vector<Slave>
```

要先问运行时是否真的需要动态增长。

如果 topology 在启动后冻结，更合理的现代 C++ 选择可能是：

```text
configuration stage:
    vector<SlaveConfig>

runtime stage:
    fixed-capacity storage / reserved vector / span
```

重点不是 STL 与否，而是：

- 是否会 reallocate；
- object address 是否稳定；
- capacity 是否可证明；
- hot path 是否有 allocator；
- cache layout 是否连续。

## Context 很大是不是坏事

不一定。

一个大的 Context 用空间换来了：

- 明确所有权；
- 无全局 singleton；
- 多实例可能性；
- 可测试；
- 不需要大量 heap pointer chasing。

真正应该分析的是：

```text
哪些字段周期高频访问？
哪些字段配置后几乎不动？
是否产生 cache footprint 问题？
```

这比单纯说“大 struct 不优雅”更有工程意义。

## 与 IgH 对照

IgH：

```text
master
  list_head domains
  list_head configs
  list_head datagram_queue
  kmalloc objects
```

SOEM：

```text
context
  slavelist[]
  grouplist[]
  frame buffers[]
  index stack[]
```

IgH 更像长期内核 runtime object graph。

SOEM 更像一个把上限显式化的 embedded protocol runtime。

二者都可以做实时控制，但对象管理成本被放在了完全不同的位置。
