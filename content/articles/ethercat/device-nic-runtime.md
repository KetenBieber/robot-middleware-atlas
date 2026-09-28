# Device/NIC 源码：IgH 怎样绕到 net_device、预分配 TX skb，并用 Poll 把接收拉进控制周期

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

到这里，Master 已经有完整 EtherCAT frame。接下来必须回答一个无法再抽象的问题：**这些字节怎样真正进入网卡，又怎样回来？**

## Device 不是普通 Socket Wrapper

固定 `ec_device`：

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

它直接接触 Linux 内核 networking 对象：

```text
net_device
sk_buff
netdev_ops
kernel module
```

所以这是 EtherCAT Master 与 NIC driver 的硬边界。

## 为什么 EtherType 在 Device 初始化时就写好

初始化 TX ring 时：

~~~c
for (i = 0; i < EC_TX_RING_SIZE; i++) {
    if (!(device->tx_skb[i] =
                dev_alloc_skb(ETH_FRAME_LEN + EXTRA_HEADROOM))) {
        ...
    }

    skb_reserve(device->tx_skb[i], ETH_HLEN + EXTRA_HEADROOM);
    eth = (struct ethhdr *) skb_push(device->tx_skb[i], ETH_HLEN);
    eth->h_proto = htons(0x88A4);
    memset(eth->h_dest, 0xFF, ETH_ALEN);
}
~~~

0x88A4 是 EtherCAT EtherType。

这段初始化做了大量周期外工作：

- alloc skb；
- reserve headroom；
- push Ethernet header；
- 固定 EtherType；
- 固定 broadcast destination。

周期发送时不必重新分配整个 skb。

## 为什么 TX Ring 只有两个也比一个重要

固定：

```c
#define EC_TX_RING_SIZE 2
```

`ec_device_tx_data()`：

~~~c
device->tx_ring_index++;
device->tx_ring_index %= EC_TX_RING_SIZE;
return device->tx_skb[device->tx_ring_index]->data + ETH_HLEN;
~~~

源码注释说明：如果连续发送多个 frame，而 DMA 还没来得及处理前一个，用同一个 buffer 会有 race。

两槽体现最小 ping-pong buffer 思想：

```text
CPU fills A -> submit A
CPU fills B -> submit B
CPU returns A after enough progress assumption
```

这不是无限 queue。

它依赖底层 driver 与发送节奏满足这套复用协议。

## Attach 时才绑定真实 net_device

固定：

~~~c
device->dev = net_dev;
device->poll = poll;
device->module = module;

for (i = 0; i < EC_TX_RING_SIZE; i++) {
    device->tx_skb[i]->dev = net_dev;
    eth = (struct ethhdr *) (device->tx_skb[i]->data);
    memcpy(eth->h_source, net_dev->dev_addr, ETH_ALEN);
}
~~~

初始化 Device 与绑定具体 NIC 被拆成两步。

这让 Master 对象可以先存在，再等待符合条件的 driver/device attach。

同时保存 module pointer，request Master 时再通过 module refcount pin 住驱动代码生命周期。

## Open 为什么直接走 ndo_open

~~~c
ret = device->dev->netdev_ops->ndo_open(device->dev);
if (!ret)
    device->open = 1;
~~~

IgH 不走用户态 socket open。

它在内核里直接调用 net_device driver ops。

close 同理走 `ndo_stop`。

所以专用 EtherCAT device path 与普通用户态 UDP/TCP stack 的执行层完全不同。

## 真正发送就是 ndo_start_xmit

`ec_device_send()`：

~~~c
struct sk_buff *skb = device->tx_skb[device->tx_ring_index];

skb->len = ETH_HLEN + size;

if (device->dev->netdev_ops->ndo_start_xmit(skb, device->dev) ==
        NETDEV_TX_OK)
{
    device->tx_count++;
    ...
} else {
    device->tx_errors++;
}
~~~

这里已经非常接近硬件驱动。

从 Master 的角度，成功语义是：

> driver 接受了这个 skb 的 transmit request。

它不等于：

- 帧已经从 PHY 完整发完；
- 所有从站已经处理；
- 返回帧已经收到。

这些要由后续 receive completion 证明。

## 为什么 Poll 是 Device API 的一等成员

Device attach 接收：

```c
ec_pollfunc_t poll
```

Master receive 时：

~~~c
void ec_device_poll(ec_device_t *device)
{
    device->jiffies_poll = jiffies;
    device->poll(device->dev);
}
~~~

源码明确说 Master 本身不用中断驱动接收，而是“手动”调用 ISR/poll 路径处理收到数据与设备状态变化。

这使控制周期可以主动决定：

```text
现在处理 NIC RX
```

而不是完全等待异步中断后再等 scheduler。

## Poll 不等于 Busy Loop

`ecrt_master_receive()` 每周期显式调用一次或按应用设计调用 poll。

这和：

```c
while (1) poll_nic();
```

不是一回事。

是否 busy、CPU 使用率多高，取决于应用周期与 driver poll 实现。

## Driver 收到 Frame 后调用 ecdev_receive

接口注释：

> Accepts a received frame. Forwards the received data to the master.

固定实现：

~~~c
void ecdev_receive(
        ec_device_t *device,
        const void *data,
        size_t size)
{
    const void *ec_data = data + ETH_HLEN;
    size_t ec_size = size - ETH_HLEN;

    ...

    ec_master_receive_datagrams(device->master, device, ec_data, ec_size);
}
~~~

所以 receive callback chain：

```text
Master ecrt_receive
  -> device->poll(net_device)
      -> EtherCAT-aware driver
          -> ecdev_receive
              -> ec_master_receive_datagrams
```

这条链是同步嵌套还是 driver 内部还夹其他机制，要继续看具体设备驱动，但 Master API 的边界已经明确。

## RX Data 为什么不直接保存一个 Frame Queue

`ecdev_receive` 立刻把 frame 交给 Master parser。

Master parser再把 payload 写入对应长期 datagram object。

也就是说它主要依赖：

```text
NIC/frame buffer = transient
Datagram object = persistent transaction state
```

而不是把所有 Ethernet frame 排进一个通用 queue 等业务线程慢慢处理。

对于周期总线，这可以减少额外 frame-level buffering 和数据年龄。

## Link State 是怎样反馈的

Driver 可调用：

~~~c
ecdev_set_link(device, state);
~~~

实现只在变化时写：

~~~c
if (likely(state != device->link_state)) {
    device->link_state = state;
    ...
}
~~~

Master send 检查这个状态。

link down 时，相关 queue datagram 被转为 ERROR，并主动 poll device 检查状态。

所以 link state 不是纯监控指标，它直接影响 datagram 生命周期。

## Device Stats 为什么可能影响实时路径

Device 保存：

```text
tx_count/rx_count
tx_bytes/rx_bytes
rates
errors
```

每帧 send/receive 都会更新一部分 counters。

这些统计很有用，但也提醒我们：

> “非业务字段”照样在实时路径写内存。

如果追求极端低 jitter，还要考虑：

- cache line；
- atomicity；
- logging；
- debug ring。

所以 debug/stat feature 必须纳入性能测量，而不是默认无成本。

## Device 的 Ownership

```text
Master
  owns ec_device objects
      |
      +-- owns preallocated tx_skb ring
      |
      +-- borrows attached net_device
      |
      +-- stores poll function from driver
      |
      +-- pins driver module during Master request
```

`ec_device_clear()` 会释放 tx skb。

`ec_device_detach()` 解除 net_device 关系。

生命周期必须保证 Master 不再使用 driver callback 后才能 unload/detach。

## 为什么 source-audit 里有很多 devices/*

IgH 不只有一个通用“raw socket” backend。

源码树提供多种 NIC 适配/专用驱动版本。

这类工程的代价很高：

- 跟 Linux kernel version 紧密耦合；
- 驱动维护量大；
- 不同 NIC 行为差异；
- DMA/NAPI API 会变化。

但收益是能把 EtherCAT 数据路径深入控制到 net_device/driver 层。

这就是通用中间件和工业 fieldbus master 的根本工程差异之一。

## 从控制循环看 Device 的时间边界

发送：

```text
ecrt_master_send
  -> frame packing
  -> ec_device_send
  -> ndo_start_xmit
```

接收：

```text
ecrt_master_receive
  -> ec_device_poll
  -> driver
  -> ecdev_receive
  -> frame parser
```

所以测量实时性时，至少应该把：

```text
master_send CPU time
ndo_start_xmit submit time
wire round trip
poll time
parser time
```

分开。

否则“send 20 us”可能只表示提交 skb 需要 20 us，而不是完整 EtherCAT round trip。