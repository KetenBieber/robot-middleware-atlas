# Datagram 与 Frame Packing 源码：Master Queue 为什么同时是发送队列和在途请求表

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

前一篇已经把周期追到 `ecrt_master_send()`。这一篇继续下钻：一个 `ec_datagram_t` 怎样进入 Master queue、被拼进 Ethernet frame、变成 SENT，然后又在返回 frame 中被匹配并结束生命周期。

## 先看 Datagram 对象为什么比“packet struct”复杂

固定结构里最关键的字段：

~~~c
typedef struct {
    struct list_head queue;
    struct list_head ext_queue;
    struct list_head sent;

    ec_device_index_t device_index;
    ec_datagram_type_t type;
    uint8_t address[EC_ADDR_LEN];

    uint8_t *data;
    ec_origin_t data_origin;
    size_t mem_size;
    size_t data_size;

    uint8_t index;
    uint16_t working_counter;
    ec_datagram_state_t state;

    unsigned long jiffies_sent;
    unsigned long jiffies_received;
    unsigned int skip_count;
    char name[EC_DATAGRAM_NAME_SIZE];
} ec_datagram_t;
~~~

三个 list node 已经说明它可能同时参与不同组织结构：

- `queue`：Master 主 datagram queue；
- `ext_queue`：非应用 datagram 的外部队列；
- `sent`：frame packing 时的临时 sent list。

这就是 intrusive data structure 的优势：同一个对象可以同时嵌入多个链表，而不用为每个容器再分配 wrapper node。

## Queue Datagram 为什么先检查“已经在队列里没有”

固定代码：

~~~c
void ec_master_queue_datagram(
        ec_master_t *master,
        ec_datagram_t *datagram)
{
    ec_datagram_t *queued_datagram;

    list_for_each_entry(queued_datagram, &master->datagram_queue, queue) {
        if (queued_datagram == datagram) {
            datagram->skip_count++;
            smp_store_release(&datagram->state, EC_DATAGRAM_QUEUED);
            return;
        }
    }

    list_add_tail(&datagram->queue, &master->datagram_queue);
    smp_store_release(&datagram->state, EC_DATAGRAM_QUEUED);
}
~~~

为什么不能无脑 `list_add_tail`？

因为状态机可能重新初始化同一个 datagram object，而旧实例还在 queue 中。

如果同一个 intrusive node 被重复插入链表，链表结构会被破坏，甚至形成无限循环。

所以这里用 O(n) 扫描换取结构安全。

这也说明 Master queue 的规模必须保持受控；如果它能无限增长，这个 O(n) duplicate check 也会放大周期成本。

## State 为什么用 Release Store

入队最后：

```c
smp_store_release(&datagram->state, EC_DATAGRAM_QUEUED);
```

语义是：

> 在其他执行上下文看到 QUEUED 之前，构造 datagram 时写入的 type/address/data_size/data 等状态应当已经可见。

这不是“锁替代品”，而是发布状态的内存序。

后面 operation thread/FSM 会用 acquire load 配对读取。

## 外部 Datagram 为什么另有 Ring

Master 除了实时 Domain，还要接受 FSM/mailbox 等非应用工作。

固定 Master 保存：

```text
ext_datagram_ring[EC_EXT_RING_SIZE]
ext_ring_idx_rt
ext_ring_idx_fsm
max_queue_size
```

FSM 侧通过 ring 取得空闲 datagram，RT send 侧再注入主 queue。

这避免 operation thread 直接和实时线程任意并发操作主 queue。

## External Injection 为什么有“本周期容量预算”

`ec_master_inject_external_datagrams()` 先统计当前 QUEUED payload：

~~~c
list_for_each_entry(datagram, &master->datagram_queue, queue) {
    if (datagram->state == EC_DATAGRAM_QUEUED) {
        queue_size += datagram->data_size;
    }
}
~~~

然后只有：

~~~c
new_queue_size <= master->max_queue_size
~~~

才注入。

否则延迟到后续周期，超时再 ERROR。

这是一个很值得借鉴的实时设计：

> 非实时/管理流量不能无限把本周期总线预算吃光。

`max_queue_size` 又由 send interval 和字节传输时间估算：

~~~c
master->max_queue_size =
    (send_interval * 1000) / EC_BYTE_TRANSMISSION_TIME_NS;
master->max_queue_size -= master->max_queue_size / 10;
~~~

最后减 10%，相当于保留一定裕量。

它不是严格 WCET 证明，但至少把“一个周期能塞多少额外数据”显式建模。

## Frame Packing 的第一步：只看 QUEUED 且 Device 匹配

`ec_master_send_datagrams()`：

~~~c
list_for_each_entry(datagram, &master->datagram_queue, queue) {
    if (datagram->state != EC_DATAGRAM_QUEUED ||
            datagram->device_index != device_index) {
        continue;
    }

    if (!frame_data) {
        frame_data =
            ec_device_tx_data(&master->devices[device_index]);
        cur_data = frame_data + EC_FRAME_HEADER_SIZE;
    }
    ...
}
~~~

一个 Master queue 可以包含：

- 已 SENT 的 in-flight datagram；
- 不同 device 的 datagram；
- 当前待发 QUEUED datagram。

所以 send 不能简单 pop front。

它是在一个“生命周期表”里筛选当前可发送对象。

## 为什么要先拿 TX Buffer 再往里写 Datagram

只有找到第一个可发送 datagram 时才调用：

```c
ec_device_tx_data(...)
```

避免没有任何 datagram 时白白取一个 TX ring buffer。

然后 `cur_data` 跳过 EtherCAT frame header，逐个追加 datagram。

## MTU 边界是怎样判断的

固定代码：

~~~c
datagram_size = EC_DATAGRAM_HEADER_SIZE + datagram->data_size
    + EC_DATAGRAM_FOOTER_SIZE;

if (cur_data - frame_data + datagram_size > ETH_DATA_LEN) {
    more_datagrams_waiting = 1;
    break;
}
~~~

这段比抽象的“支持分帧”更具体：

- 它不会把一个 datagram 切成两半；
- 它只决定“下一个 datagram 能否放进当前 Ethernet payload”；
- 放不下就结束当前 frame，下一轮继续。

Domain 在 activate 时已经保证单个 datagram 不超过 `EC_MAX_DATA_SIZE`。

所以有两级切分：

```text
Domain finish:
  large process image -> several datagrams

Master send:
  queued datagrams -> several Ethernet frames
```

## Index 在真正发出前才分配

固定代码：

~~~c
list_add_tail(&datagram->sent, &sent_datagrams);
datagram->index = master->datagram_index++;
~~~

这意味着 index 表达的是这次 wire transaction，而不是 datagram object 的永久 ID。

同一个长期复用的 Domain datagram 每个周期会获得新的 index。

## Previous Datagram 的 Follows Bit 为什么要回头修改

每个 datagram header 都要告诉解析器后面是否还有 datagram。

但写第一个 datagram 时还不知道最终是否有第二个。

固定实现保存：

```c
void *follows_word;
```

当真正准备写下一个时，再回头：

~~~c
if (follows_word) {
    EC_WRITE_U16(follows_word,
            EC_READ_U16(follows_word) | 0x8000);
}
~~~

这是流式 serialization 里很常见的“延迟确定前一个 header 标志”。

## Datagram Header 是逐字段写入，不是直接 memcpy C struct

固定代码：

~~~c
EC_WRITE_U8(cur_data, datagram->type);
EC_WRITE_U8(cur_data + 1, datagram->index);
memcpy(cur_data + 2, datagram->address, EC_ADDR_LEN);
EC_WRITE_U16(cur_data + 6, datagram->data_size & 0x7FF);
EC_WRITE_U16(cur_data + 8, 0x0000);
~~~

为什么不定义：

```c
struct Header { ... };
memcpy(frame, &header, sizeof(header));
```

因为 wire format 要明确控制：

- field width；
- endian；
- padding；
- bit layout；
- ABI/compiler alignment。

协议代码应按 wire format 写，不要依赖宿主 C struct layout。

## Payload 一定会复制进 TX SKB

发送端：

~~~c
memcpy(cur_data, datagram->data, datagram->data_size);
cur_data += datagram->data_size;
~~~

所以即使 Domain main datagram 直接借用 process image，最终发送到 skb 仍有一次 copy。

这就是为什么“mmap process image”不能被宣传成端到端 zero-copy。

## Footer 先把 WKC 置零

~~~c
EC_WRITE_U16(cur_data, 0x0000);
~~~

从站经过时会根据命令语义更新 WKC。

Master 不应带着上周期旧 WKC 发出去。

## Frame Header 在所有 Datagram 完成后写

固定：

~~~c
EC_WRITE_U16(frame_data, ((cur_data - frame_data
                - EC_FRAME_HEADER_SIZE) & 0x7FF) | 0x1000);
~~~

长度只有在 datagram packing 结束后才知道。

然后如果 frame 太短，还要 padding 到 Ethernet 最小要求。

## 发送后才把 Datagram 统一标 SENT

发送：

~~~c
ec_device_send(&master->devices[device_index],
        cur_data - frame_data);
~~~

记录 timestamp 后：

~~~c
list_for_each_entry_safe(datagram, next, &sent_datagrams, sent) {
    datagram->jiffies_sent = jiffies_sent;
    list_del_init(&datagram->sent);
    smp_store_release(&datagram->state, EC_DATAGRAM_SENT);
}
~~~

注意它没有从 `master->datagram_queue` 删除。

只从临时 `sent_datagrams` 删除。

因为主 queue 还承担回包匹配。

## 接收 parser 首先证明 Frame 长度合法

`ec_master_receive_datagrams()`：

~~~c
if (unlikely(size < EC_FRAME_HEADER_SIZE)) {
    ...
    return;
}

frame_size = EC_READ_U16(cur_data) & 0x07FF;
...
if (unlikely(frame_size > size)) {
    ...
    return;
}
~~~

随后每个 datagram 还继续检查：

~~~c
if (unlikely(cur_data - frame_data
             + data_size + EC_DATAGRAM_FOOTER_SIZE > size)) {
    ...
    return;
}
~~~

这是安全解析的正确顺序。

## 回包匹配为什么不是 Hash Map

固定代码直接遍历 `master->datagram_queue`。

匹配条件：

~~~c
if (datagram->index == datagram_index
    && datagram->state == EC_DATAGRAM_SENT
    && datagram->type == datagram_type
    && datagram->data_size == data_size) {
    matched = 1;
    break;
}
~~~

为什么不用 hash table？

因为系统设计预期在途 datagram 数量有界且较小，而 intrusive list：

- 无额外分配；
- 生命周期简单；
- 插入 O(1)；
- 顺序天然保留。

这是一种典型实时工程取舍：小 N 时，简单线性结构可能比复杂容器更可预测。

## 接收完成的顺序非常关键

匹配后：

~~~c
memcpy(datagram->data, cur_data, data_size);
...
datagram->working_counter = EC_READ_U16(cur_data);
...
list_del_init(&datagram->queue);
smp_store_release(&datagram->state, EC_DATAGRAM_RECEIVED);
~~~

可以把它看成 publication protocol：

```text
write payload
write WKC
write timestamp
remove in-flight linkage
release-store RECEIVED
```

观察方 acquire 看到 RECEIVED 后，才应该把 payload/WKC 当作本轮完成数据。

## Timeout 为什么在 receive 中扫

`ecrt_master_receive()` poll 完设备后扫描所有 SENT datagram，比较发送时间与当前 poll 时间。

超时则：

```c
list_del_init(&datagram->queue);
smp_store_release(&datagram->state, EC_DATAGRAM_TIMED_OUT);
```

也就是说“没有收到”本身也是一个 completion state。

FSM 不会永远等一个 SENT datagram。

## Datagram 内存有 Internal 与 External 两种

`ec_datagram_prealloc()` 对内部 payload：

~~~c
if (!(datagram->data = kmalloc(size, GFP_KERNEL))) {
    ...
}
datagram->mem_size = size;
~~~

但 Domain main datagram 用 `ec_datagram_lrw_ext()`：

~~~c
datagram->data = external_memory;
datagram->data_origin = EC_ORIG_EXTERNAL;
...
datagram->type = EC_DATAGRAM_LRW;
~~~

所以同一个 Datagram 类型支持两种 ownership：

```text
internal:
  datagram owns payload storage

external:
  datagram borrows caller storage
```

这也是为什么 `data_origin` 必须显式存在。

## 一条 Datagram 生命周期最终闭环

```text
builder/FSM
  INIT
    |
queue
  QUEUED
    |
frame packing + ndo_start_xmit
  SENT
    |
    +-- matching frame -> RECEIVED
    |
    +-- timeout -> TIMED_OUT
    |
    +-- link/send error -> ERROR
```

状态机、Domain、DC 和外部请求虽然业务完全不同，但最终都复用这一条生命周期。