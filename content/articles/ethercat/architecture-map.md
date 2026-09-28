# IgH EtherCAT Master 架构图：先分清用户态门面、内核 Master、Domain、Datagram、FSM 与 Device

本文开始进入真实源码。所有实现结论固定在 EtherLab / IgH EtherCAT Master stable-1.6，版本 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

读 IgH 最容易犯的第一个错误，是搜到 `ecrt_master_send()` 后只看到一个 `ioctl()`，然后以为“源码没什么东西”。原因是 IgH 同时提供 **用户态 libethercat 门面** 和 **内核 Master 实现**。同一个 public API 名字会在两层各出现一次。

## 先把一次用户态调用拆成两层

用户代码：

```c
ecrt_master_receive(master);
ecrt_domain_process(domain);
...
ecrt_domain_queue(domain);
ecrt_master_send(master);
```

用户态库中的 `ecrt_master_receive()` 实际只是：

~~~c
int ecrt_master_receive(ec_master_t *master)
{
    int ret;

    ret = ioctl(master->fd, EC_IOCTL_RECEIVE, NULL);
    if (EC_IOCTL_IS_ERROR(ret)) {
        return -EC_IOCTL_ERRNO(ret);
    }
    return 0;
}
~~~

`ecrt_domain_process()` 也一样通过 fd 和 domain index 进入内核：

~~~c
int ecrt_domain_process(ec_domain_t *domain)
{
    int ret;

    ret = ioctl(domain->master->fd, EC_IOCTL_DOMAIN_PROCESS, domain->index);
    if (EC_IOCTL_IS_ERROR(ret)) {
        return -EC_IOCTL_ERRNO(ret);
    }
    return 0;
}
~~~

所以真实架构是：

```text
application
    |
    | libethercat ecrt_*()
    v
/dev/EtherCAT... character device
    |
    | ioctl
    v
kernel master/*
    |
    +-- Master / Domain / Slave Config
    +-- Datagram Queue
    +-- Master + Slave FSMs
    +-- Device
    |
    v
EtherCAT NIC driver
```

这条边界非常重要：**用户态 API 对象与内核态对象不是同一个 C struct。**

## 用户态 Master 是“句柄 + 映射关系”，内核 Master 才是完整运行时

用户态库需要保存：

```text
fd
domain wrappers
slave config wrappers
mapped process data
```

内核态 `struct ec_master` 则承担真正运行时状态。固定源码中最核心的一段字段是：

~~~c
struct ec_master {
    unsigned int index;
    unsigned int reserved;

    struct semaphore master_sem;

    ec_device_t devices[EC_MAX_NUM_DEVICES];
    struct semaphore device_sem;

    ec_fsm_master_t fsm;
    ec_datagram_t fsm_datagram;
    ec_master_phase_t phase;
    unsigned int active;
    unsigned int config_changed;

    ec_slave_t *slaves;
    unsigned int slave_count;

    struct list_head configs;
    struct list_head domains;

    struct list_head datagram_queue;
    uint8_t datagram_index;

    struct list_head ext_datagram_queue;
    struct semaphore ext_queue_sem;

    ec_datagram_t ext_datagram_ring[EC_EXT_RING_SIZE];

    struct task_struct *thread;
    struct rt_mutex io_mutex;
    ...
};
~~~

把这些字段按职责重新分组，会比按文件顺序更容易读：

```text
身份/生命周期
  index reserved phase active

物理链路
  devices[] device_sem

配置模型
  slaves configs domains

数据面
  datagram_queue datagram_index
  ext_datagram_queue/ring

控制面
  fsm fsm_datagram config_changed

执行
  thread io_mutex

时间
  app_time dc_ref_time
  DC datagrams
```

Master 不是“大号 socket”。它更像整个 EtherCAT 总线实例的 root object。

## Domain 不是 Topic，而是过程数据调度组

内核 Domain 的定义非常紧凑：

~~~c
struct ec_domain
{
    struct list_head list;
    ec_master_t *master;
    unsigned int index;

    struct list_head fmmu_configs;
    size_t data_size;
    uint8_t *data;
    ec_origin_t data_origin;
    uint32_t logical_base_address;
    struct list_head datagram_pairs;
    uint16_t working_counter[EC_MAX_NUM_DEVICES];
    uint16_t expected_working_counter;
    unsigned int working_counter_changes;
    unsigned int redundancy_active;
    unsigned long notify_jiffies;
};
~~~

这几个字段形成一条非常清晰的因果链：

```text
fmmu_configs
    -> data_size
    -> data/process image
    -> logical_base_address
    -> datagram_pairs
    -> working_counter
```

所以 Domain 的本质不是“消息容器”，而是：

> 一组具有连续逻辑地址和共同 process image 的 FMMU 映射，以及承载它们的周期 datagram。

## 用户态 Domain 反而非常轻

libethercat 里的 Domain 只有：

~~~c
struct ec_domain {
    ec_domain_t *next;
    unsigned int index;
    ec_master_t *master;
    uint8_t *process_data;
};
~~~

它甚至不保存 FMMU/datagram。

为什么？

因为这些复杂结构都在内核里；用户态只需要：

- index：告诉 ioctl 操作哪个内核 Domain；
- process_data：直接访问 mmap 出来的过程映像。

这是经典的 control plane / data mapping 组合：

```text
control operations -> ioctl
high-frequency process bytes -> mmap
```

如果每次读一个 PDO 都通过 ioctl，系统调用开销和 API 复杂度都会更高。

## Activate 时用户态 Process Image 是怎样拿到的

用户态 `ecrt_master_activate()` 调用内核 activate 后，拿到总 process data 大小，然后执行共享映射：

~~~c
master->process_data = mmap(0, master->process_data_size,
        PROT_READ | PROT_WRITE, MAP_SHARED, master->fd, 0);
~~~

紧接着源码主动触碰第一页：

~~~c
// Access the mapped region to cause the initial page fault
master->process_data[0] = 0x00;
~~~

这行非常值得注意。

它体现的是实时系统中的“把首次 page fault 前移”思想。虽然这里只触碰第一字节，并不能自动证明整个区域所有页都已 fault-in，但作者明确意识到**第一次访问虚拟内存的异常路径不应该突然出现在周期代码里**。

随后每个用户态 Domain 通过 ioctl 获取自己的 offset：

~~~c
while (domain) {
    int offset = ioctl(domain->master->fd, EC_IOCTL_DOMAIN_OFFSET,
            domain->index);
    ...
    domain->process_data = master->process_data + offset;
    domain = domain->next;
}
~~~

所以多个 Domain 在用户态可能只是同一大块 mmap 区域里的不同子区间。

## Slave Config 为什么不能等同于 Slave

固定内核结构 `ec_slave_config` 有：

~~~c
struct ec_slave_config {
    struct list_head list;
    ec_master_t *master;

    uint16_t alias;
    uint16_t position;
    uint32_t vendor_id;
    uint32_t product_code;

    ec_slave_t *slave;

    ec_sync_config_t sync_configs[EC_MAX_SYNC_MANAGERS];
    ec_fmmu_config_t fmmu_configs[EC_MAX_FMMUS];
    uint8_t used_fmmus;

    uint16_t dc_assign_activate;
    ec_sync_signal_t dc_sync[EC_SYNC_SIGNAL_COUNT];

    struct list_head sdo_configs;
    struct list_head sdo_requests;
    struct list_head soe_requests;
    ...
};
~~~

最关键的字段其实是：

```c
ec_slave_t *slave;
```

注释明确说明：设备 offline 时它可以是 NULL。

这揭示了两个对象的边界：

```text
ec_slave_config_t
  = application intent
  = “alias/position/vendor/product 的这个设备应该怎样配置”

ec_slave_t
  = current physical bus observation
  = “现在扫描到的这一个从站”
```

应用配置可以长期存在，而物理 slave 因掉线/rescan 被重新构建。

如果把两者合成一个对象，掉线时就会失去期望配置，恢复时无法自动重放。

## Datagram 是整个数据面共享的“命令对象”

`ec_datagram_t` 不是一段裸字节。固定结构保存完整生命周期：

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

注意它同时包含：

- intrusive list 节点；
- 协议字段；
- payload 指针；
- memory ownership 标志；
- wire correlation index；
- WKC；
- 状态；
- 时间戳。

所以 datagram object 同时跨越：

```text
configuration
queueing
serialization
in-flight matching
receive completion
timeout
```

## Datagram State 是跨层契约

固定枚举：

~~~c
typedef enum {
    EC_DATAGRAM_INIT,
    EC_DATAGRAM_QUEUED,
    EC_DATAGRAM_SENT,
    EC_DATAGRAM_RECEIVED,
    EC_DATAGRAM_TIMED_OUT,
    EC_DATAGRAM_ERROR
} ec_datagram_state_t;
~~~

可以把每个状态映射到责任人：

| 状态 | 典型含义 | 下一层 |
|---|---|---|
| INIT | 尚未提交 | builder/FSM |
| QUEUED | 等待 frame packing | Master |
| SENT | 已提交链路，等待返回 | Device/receive |
| RECEIVED | 返回且匹配完成 | Domain/FSM |
| TIMED_OUT | 超时出队 | Domain/FSM/error path |
| ERROR | 发送/链路错误 | caller/FSM |

这是一条比函数调用栈更长的生命周期。Datagram 发送后函数栈早已返回，对象仍继续活着。

## Master FSM 是函数指针状态机，不是线程类

固定 `ec_fsm_master`：

~~~c
struct ec_fsm_master {
    ec_master_t *master;
    ec_datagram_t *datagram;
    unsigned int retries;

    void (*state)(ec_fsm_master_t *);
    ec_device_index_t dev_idx;
    int idle;

    unsigned int slaves_responding[EC_MAX_NUM_DEVICES];
    unsigned int rescan_required;
    ec_slave_state_t slave_states[EC_MAX_NUM_DEVICES];

    ec_slave_t *slave;
    ec_sdo_request_t *sdo_request;
    ec_soe_request_t *soe_request;

    ec_fsm_coe_t fsm_coe;
    ec_fsm_soe_t fsm_soe;
    ec_fsm_pdo_t fsm_pdo;
    ec_fsm_change_t fsm_change;
    ec_fsm_slave_config_t fsm_slave_config;
    ec_fsm_slave_scan_t fsm_slave_scan;
    ...
};
~~~

核心其实是：

```c
void (*state)(ec_fsm_master_t *);
```

每个状态就是一个函数。

状态迁移不是大 switch，而是：

```c
fsm->state = ec_fsm_master_state_configure_slave;
```

下一次 exec 再调用这个函数。

这种 C 写法是手写状态模式，但真正重要的是：**它把远端等待转成持久对象状态，而不是阻塞调用栈。**

## Device 是 NIC 与 Master 的窄接口

`ec_device` 保存：

~~~c
struct ec_device
{
    ec_master_t *master;
    struct net_device *dev;
    ec_pollfunc_t poll;
    struct module *module;

    uint8_t open;
    uint8_t link_state;

    struct sk_buff *tx_skb[EC_TX_RING_SIZE];
    unsigned int tx_ring_index;

    unsigned long jiffies_poll;
    ...
};
~~~

这个结构说明 IgH 没有把 Linux 网络层完全藏起来：

- 直接持有 `net_device *`；
- 保存设备提供的 poll function；
- 保存一组 TX `sk_buff`；
- 最终调用 `ndo_start_xmit`。

所以 EtherCAT 主站的实时性分析必须一直读到 net_device 边界。

## 最终对象图

```text
userspace
┌──────────────────────────────────────────────┐
│ ec_master wrapper                            │
│  fd + mmap process_data                      │
│   └─ ec_domain wrapper -> index + data ptr   │
└──────────────────────┬───────────────────────┘
                       │ ioctl / mmap
kernel                 v
┌──────────────────────────────────────────────┐
│ ec_master                                    │
│                                              │
│  configs ──> ec_slave_config ──attach─> slave│
│  domains ──> ec_domain                       │
│                ├─ fmmu_configs               │
│                ├─ process image              │
│                └─ datagram_pairs             │
│                                              │
│  fsm ──> master/slave/mailbox FSMs           │
│  datagram_queue <── Domain/FSM/DC/external   │
│  devices[] ──> net_device + tx_skb ring      │
└──────────────────────┬───────────────────────┘
                       v
                    wire/slaves
```

后面所有源码页都只是在这张图上沿某条箭头深入。