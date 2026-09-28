# EtherCAT Frame 与 Datagram：为什么一个 Ethernet 帧里要再设计一层命令协议

固定参考实现：EtherLab / IgH EtherCAT Master 1.6.13，`61cc654f5b721ddd54df0f58bdd34106d91c5359`。

这一篇只解决一个问题：**EtherCAT 已经跑在 Ethernet 上了，为什么还需要自己的 frame header、datagram header、index、address、Working Counter 和多种命令？**

## Ethernet 只负责把帧送上链路，不知道“我要读哪个从站”

Ethernet frame 能告诉网卡：

```text
destination MAC
source MAC
EtherType
payload
FCS
```

但它无法表达：

- 读第 3 个从站寄存器 `0x0130`；
- 把逻辑地址 `0x00001000` 开始的 64 字节写入 PDO；
- 同一帧先读输入，再写输出；
- 返回时告诉主站究竟有多少 FMMU 成功处理了数据。

这些都属于 EtherCAT 协议层，因此 Ethernet payload 内还要有 EtherCAT 自己的结构。

## 从最小 Datagram 推导字段

假设我们只设计一个最小请求：

```text
READ(address, length)
```

至少需要：

```text
command
address
length
```

但总线上可以同时存在多个请求，返回 frame 也要让主站识别哪个请求对应哪个本地对象，因此还需要 `index`。

同一 frame 还能放多个 datagram，因此还要告诉解析器后面是否还有下一个 datagram。

从站处理完成后，主站还要知道“有多少预期对象真正响应”，于是需要 Working Counter。

最终抽象就接近：

```text
Datagram
  command
  index
  address
  length + follows flag
  IRQ/reserved
  data[length]
  working_counter
```

IgH `ec_master_send_datagrams()` 在固定版本里正是按这个布局写入发送 buffer：type、index、address、长度/follows、payload，最后把 WKC 置零等待从站更新。

## 为什么一个 Frame 可以塞多个 Datagram

假设一个机器人有三类工作：

```text
1. 周期 PDO：joint data
2. DC 同步：clock correction
3. 配置状态检查：read AL status
```

如果每项都独占一整个 Ethernet frame：

```text
Ethernet header + EtherCAT header + tiny payload + padding
```

小数据时协议开销和最小帧填充会占很大比例。

把多个 datagram 合并：

```text
EtherCAT frame
  Datagram A: process data
  Datagram B: DC
  Datagram C: state check
```

可以减少 frame 数和链路开销。

但这引入一个工程问题：**谁负责决定一帧还能不能塞下下一个 datagram？**

这就是主站 frame packing 的职责。IgH 固定实现会计算：

```c
datagram_size =
    EC_DATAGRAM_HEADER_SIZE
    + datagram->data_size
    + EC_DATAGRAM_FOOTER_SIZE;
```

若再加入当前 frame 会超过 `ETH_DATA_LEN`，就停止填充，发送当前 frame，再构造下一帧。

## Datagram Index 为什么只是 8 bit 仍然能工作

IgH 的 `ec_datagram_t::index` 是 `uint8_t`。

直觉上会担心 256 次后回绕：

```text
0, 1, 2, ... 255, 0, 1 ...
```

如果仅凭 index 匹配，旧帧晚到就可能误撞新请求。

所以接收端实际匹配不只看 index。固定源码同时检查：

```c
datagram->index == datagram_index
&& datagram->state == EC_DATAGRAM_SENT
&& datagram->type == datagram_type
&& datagram->data_size == data_size
```

这仍然不是密码学唯一标识，而是依赖“有限数量在途 datagram + 周期超时 + 命令/长度约束”的工程协议。

这一点很值得学习：实时通信系统不一定追求全局永不重复 ID，而可能利用**有界在途窗口**来压缩 header。

## Working Counter 不是 CRC，也不是 ACK

WKC 经常被误解成“包成功了就是 1”。

更准确地说，它是 datagram 在经过从站时，由符合条件的处理单元按 EtherCAT 语义递增的计数。

例如 IgH 在构造 Domain datagram pair 时，对逻辑读写计算预期值：

- input-only LRD：每个 input FMMU 贡献预期计数；
- output-only LWR：每个 output FMMU 贡献预期计数；
- LRW：output 与 input 的贡献规则不同，IgH 固定代码中 output 乘 2、input 乘 1。

因此 WKC 的意义更接近：

> 本次逻辑操作有多少预期映射真正参与处理？

它能发现很多拓扑/状态异常，但不能单独证明：

- 业务动作已经按物理目标执行；
- 数据值一定合理；
- 从站没有内部故障；
- 控制命令语义正确。

所以机器人安全控制仍需要 status word、驱动 fault、序号、状态机和应用级检查。

## 物理寻址为什么有三套

EtherCAT 主站必须经历“还没配置好”到“稳定运行”的过程，所以需要不同寻址模式。

### Auto Increment：我只知道环上的相对位置

扫描初期，主站可能不知道每个从站的 station address。

Auto Increment Physical 命令让帧沿拓扑经过设备时，根据位置参与寻址，适合发现与初始访问。

### Configured Address：设备已经有 station address

完成地址配置后，可以用 FPRD/FPWR/FPRW 精确访问某个从站寄存器。

### Logical Address：稳定过程数据不想逐设备访问

过程数据希望：

```text
logical 0x1000..0x10ff
```

由各从站 FMMU 自己决定哪些字节属于自己。

因此 LRD/LWR/LRW 特别适合周期 PDO。

可以把三者理解为：

```text
Auto Increment -> 拓扑身份
Configured     -> 从站身份
Logical        -> 过程数据身份
```

## LRW 为什么有价值

如果一个周期既要：

- 从从站读传感器输入；
- 又要向从站写执行器输出；

最朴素是两个 datagram：

```text
LRD input
LWR output
```

LRW 允许同一个逻辑 datagram 在链路经过时完成读写，从而降低协议开销。

但它不是无条件最佳。设备、FMMU 配置和方向布局会影响能否合并；主站实现还要根据一个 datagram 覆盖的 FMMU 方向决定选 LRD、LWR 还是 LRW。

IgH 的 `ec_datagram_pair_init()` 正是在 activate 阶段做这件事：根据当前数据块包含 output/input 的数量预先选择 datagram type，而不是每周期重新判断。

## 为什么发送后 Datagram 还留在 Master Queue

常见队列模型是：

```text
pop -> send -> free
```

EtherCAT 不能这么简单，因为发送只是请求生命周期的中点。

发送之后主站仍要保存：

```text
index
type
data_size
state = SENT
send timestamp
payload destination
```

等返回帧时再在 queue 中匹配。

因此 IgH 的状态机是：

```text
INIT
  -> QUEUED
  -> SENT
      -> RECEIVED
      -> TIMED_OUT
      -> ERROR
```

`SENT` 状态仍然留在 `master->datagram_queue`，收到后才 `list_del_init()`。

这也是为什么“Master datagram queue”既不是纯发送队列，也不是普通消息队列：它同时承担**待发送集合 + in-flight 请求表**。

## 接收路径为什么要先做严格边界检查

收到 Ethernet payload 后，解析器面对的是外部输入。

固定实现至少检查：

```text
frame >= EtherCAT header
declared frame_size <= received size
each datagram header/data/footer fits in frame
```

然后才读取 datagram data 和 WKC。

这是系统代码必须具备的基本顺序：

```text
先证明长度合法
再解引用字段
再复制 payload
```

不能先根据 header 中的 length 做 memcpy，再回头判断长度。

## 从“帧”回到控制系统时间

一个 1 kHz 周期真正消耗的总线时间不是只看 payload 字节数。

粗略拆成：

```text
T_cycle_bus
  = T_tx_frame
  + T_propagation
  + Σ T_slave_forwarding
  + T_return
  + T_rx_processing
```

而软件时间还包括：

```text
T_software
  = queue
  + frame packing
  + NIC submit
  + poll
  + frame parse
  + domain process
```

因此优化 frame packing 不只是“省带宽”，还会影响 frame 数、网卡提交次数、poll 后的解析工作和最终 jitter。

下一篇会继续向上走：为什么逻辑 Datagram 能直接覆盖多从站 PDO，以及 FMMU、SyncManager、PDO 与 process image 到底是什么关系。