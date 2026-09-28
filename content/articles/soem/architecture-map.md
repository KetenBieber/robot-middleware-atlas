# SOEM 架构图：从 ecx_contextt 到 OSAL/OSHW，把用户态 Master 的边界一次画清楚

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

读 SOEM 最先要解决的不是“哪个函数发帧”，而是：

> **SOEM 把哪些东西当状态，哪些东西当平台能力，哪些东西完全留给应用？**

## 仓库先按职责拆

固定仓库最核心的目录：

```text
include/soem/
    public types / API

src/
    ec_base.c      EtherCAT datagram primitives
    ec_main.c      lifecycle, mailbox, process data
    ec_config.c    discovery, PDO/SM/FMMU, IOmap
    ec_coe.c       CoE/SDO
    ec_dc.c        Distributed Clocks
    ec_foe.c ...
    ec_soe.c ...

osal/
    OS abstraction layer

oshw/
    hardware/network abstraction
    linux/nicdrv.c
    win32/...

samples/
    simple_ng
    slaveinfo
    eepromtool
    ...
```

可以把它理解成四层：

```text
Application
    ↓
SOEM protocol/core
    ↓
OSAL + OSHW
    ↓
OS / NIC
```

## ecx_contextt 是整个实例的 root object

SOEM 2.0 明确把所有 `ecx_*` API 指向一个 context。

固定定义：

~~~c
struct ecx_context
{
   ecx_portt port;
   ec_slavet slavelist[EC_MAXSLAVE];
   int slavecount;
   ec_groupt grouplist[EC_MAXGROUP];
   boolean ecaterror;
   int64 DCtime;

   uint8 esibuf[EC_MAXEEPBUF];
   uint32 esimap[EC_MAXEEPBITMAP];
   uint16 esislave;
   ec_eringt elist;
   ec_idxstackT idxstack;

   ec_SMcommtypet SMcommtype[EC_MAX_MAPT];
   ec_PDOassignt PDOassign[EC_MAX_MAPT];
   ec_PDOdesct PDOdesc[EC_MAX_MAPT];

   ec_eepromSMt eepSM;
   ec_eepromFMMUt eepFMMU;
   ec_mbxpoolt mbxpool;

   ec_enit *ENI;
   ...
   boolean overlappedMode;
   boolean packedMode;
};
~~~

这个对象和 IgH 的 `ec_master_t` 有相似之处，但差异更重要。

IgH Master 是内核长期运行对象，里面还拥有线程、Domain list、Datagram queue、Device。

SOEM Context 更像：

> **应用内部的一份 EtherCAT runtime state bundle。**

它没有强制 background thread，也没有 character-device boundary。

## port 是协议库和网卡之间的桥

`ecx_portt` 在 Linux 下直接嵌入：

~~~c
typedef struct
{
   ec_stackT stack;
   int sockhandle;

   ec_bufT rxbuf[EC_MAXBUF];
   int rxbufstat[EC_MAXBUF];
   int rxsa[EC_MAXBUF];
   ec_bufT tempinbuf;
   int tempinbufs;

   ec_bufT txbuf[EC_MAXBUF];
   int txbuflength[EC_MAXBUF];
   ec_bufT txbuf2;
   int txbuflength2;

   uint8 lastidx;
   int redstate;
   ecx_redportt *redport;

   pthread_mutex_t getindex_mutex;
   pthread_mutex_t tx_mutex;
   pthread_mutex_t rx_mutex;
} ecx_portt;
~~~

所以 SOEM 的网络运行时没有隐藏在 socket wrapper 后面。

你可以直接看到：

```text
socket handle
frame buffers
buffer state
redundancy
mutex
```

全部属于 port。

## slavelist 不是 STL vector，而是固定数组

`ec_slavet slavelist[EC_MAXSLAVE]` 保存每个从站：

- AL state；
- station address；
- vendor/product/revision；
- input/output bit/byte 数；
- IOmap pointer/offset；
- SyncManager；
- FMMU；
- mailbox；
- DC 拓扑与传播延迟；
- protocol capability；
- recovery state。

这意味着从站编号天然就是数组 index：

```c
context->slavelist[slave]
```

没有：

```text
map<slave_id, object>
list<Slave*>
heap allocated Slave nodes
```

这是一种非常明确的实时/嵌入式取舍：

> 最大容量由 build option 给出，运行时用连续数组换取简单寻址和确定的对象生命周期。

代价也很直接：

- 内存按上限预留；
- 超过 `EC_MAXSLAVE` 就不是动态扩容能解决；
- Context 体积会随 build option 增长。

## grouplist 是过程数据调度单元

`ec_groupt` 不是 ROS namespace。

它保存：

~~~c
uint32 logstartaddr;
uint32 Obytes;
uint8 *outputs;
uint32 Ibytes;
uint8 *inputs;

boolean hasdc;
uint8 blockLRW;

uint16 nsegments;
uint16 Isegment;
uint16 Ioffset;

uint16 outputsWKC;
uint16 inputsWKC;

uint32 IOsegment[EC_MAXIOSEGMENTS];
~~~

这一组字段已经把周期数据面讲完一半：

```text
logical address
→ IOmap output/input region
→ frame segmentation
→ expected WKC
```

后面 `ecx_send_processdata_group()` 基本就是消费这些预计算结果。

## idxstack 是“本周期返回帧的回填计划”

固定定义：

~~~c
typedef struct ec_idxstack
{
   uint8 pushed;
   uint8 pulled;
   uint8 idx[EC_MAXBUF];
   void *data[EC_MAXBUF];
   uint16 length[EC_MAXBUF];
   uint16 dcoffset[EC_MAXBUF];
   uint8 type[EC_MAXBUF];
} ec_idxstackT;
~~~

它不是传统调用栈，也不是网络队列。

发送阶段每构造一帧，就记住：

```text
frame index
返回数据应该 copy 到哪
copy 多长
DC time 在返回帧哪个 offset
```

receive 阶段按这个表把返回数据重新拼回 IOmap。

因此它更像：

> **scatter/gather 的逆向 bookkeeping table。**

## OSAL 和 OSHW 分别隔离什么

OSAL 提供：

- monotonic time；
- sleep；
- thread；
- realtime thread helper；
- mutex；
- malloc/free。

OSHW 提供：

- 网卡枚举；
- socket；
- frame send/receive；
- platform endian/low-level network 差异。

这个分层很重要。

如果把 pthread、clock_gettime、AF_PACKET 全写进 `ec_main.c)，协议层很快就会和 Linux 绑定死。

## 应用仍然拥有什么

SOEM 没有替应用拥有：

- 主周期线程；
- 控制器对象；
- RT priority policy；
- CPU isolation；
- process-image schema；
- 安全停机策略；
- WKC fault policy；
- logger。

所以完整系统其实是：

```text
Application owns execution policy
SOEM owns EtherCAT protocol/runtime state
OSAL/OSHW adapt the platform
```

这也是后面做 SOEM 与 IgH 对照时最核心的一条线。
