# 一个 1 kHz 周期怎样真正跑完：receive → process → control → queue → send

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

这一篇不按文件讲，而是拿官方用户态示例的一次 `cyclic_task()` 当主线，把数据从 NIC 返回一直追到下一帧发出。

## 官方示例给出的顺序就是课程主线

固定 `examples/user/main.c`：

~~~c
void cyclic_task()
{
    // receive process data
    ecrt_master_receive(master);
    ecrt_domain_process(domain1);

    // check process data state
    check_domain1_state();

    ...

    // write process data
    EC_WRITE_U8(domain1_pd + off_dig_out, blink ? 0x06 : 0x09);

    // send process data
    ecrt_domain_queue(domain1);
    ecrt_master_send(master);
}
~~~

这五步不能随意交换：

```text
1 receive
2 domain_process
3 read/write process image
4 domain_queue
5 master_send
```

## 第 1 步：用户态 receive 只是 ioctl

libethercat：

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

真正接收逻辑在内核 `master/master.c`。

这意味着一次周期至少跨一次 user/kernel boundary。

但 process data 本身不需要每字段 ioctl，因为已经 mmap。

## 第 2 步：内核 receive 主动 Poll Device

固定内核实现：

~~~c
int ecrt_master_receive(ec_master_t *master)
{
    unsigned int dev_idx;
    ec_datagram_t *datagram, *next;

    // receive datagrams
    for (dev_idx = EC_DEVICE_MAIN; dev_idx < ec_master_num_devices(master);
            dev_idx++) {
        ec_device_poll(&master->devices[dev_idx]);
    }
    ec_master_update_device_stats(master);

    // dequeue all datagrams that timed out
    list_for_each_entry_safe(datagram, next, &master->datagram_queue, queue) {
        if (datagram->state != EC_DATAGRAM_SENT) continue;
        ...
    }
    ...
}
~~~

关键不是“receive 调 socket”。

它首先：

```text
application calls receive
  -> Master calls device poll
  -> driver performs receive work
  -> driver calls ecdev_receive
```

这是显式 polling 设计。

## Device Poll 为什么像“手动调用 ISR”

`ec_device_poll()` 的源码注释非常直接：

> The master itself works without using interrupts. Therefore the processing of received data and status changes of the network device has to be done by the master calling the ISR "manually".

实现：

~~~c
void ec_device_poll(ec_device_t *device)
{
#ifdef EC_HAVE_CYCLES
    device->cycles_poll = get_cycles();
#endif
    device->jiffies_poll = jiffies;
    device->poll(device->dev);
}
~~~

这把 poll 时间点记录在 Device 上，后面收到 datagram 时可用作 receive timestamp。

## Driver 把 Frame 交回 Master

设备侧调用：

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

到这里 Ethernet header 被跳过，Master parser 接手 EtherCAT payload。

数据路径现在是：

```text
NIC
 -> driver poll
 -> ecdev_receive
 -> ec_master_receive_datagrams
```

## Frame Parser 怎样找到原 Datagram

`ec_master_receive_datagrams()` 读出：

~~~c
datagram_type  = EC_READ_U8(cur_data);
datagram_index = EC_READ_U8(cur_data + 1);
data_size      = EC_READ_U16(cur_data + 6) & 0x07FF;
cmd_follows    = EC_READ_U16(cur_data + 6) & 0x8000;
~~~

然后扫描 Master queue：

~~~c
list_for_each_entry(datagram, &master->datagram_queue, queue) {
    if (datagram->index == datagram_index
        && datagram->state == EC_DATAGRAM_SENT
        && datagram->type == datagram_type
        && datagram->data_size == data_size) {
        matched = 1;
        break;
    }
}
~~~

这里再次证明 Master queue 同时是 in-flight table。

## 收到数据时什么时候 memcpy

源码：

~~~c
if (datagram->type != EC_DATAGRAM_APWR &&
        datagram->type != EC_DATAGRAM_FPWR &&
        datagram->type != EC_DATAGRAM_BWR &&
        datagram->type != EC_DATAGRAM_LWR) {
    memcpy(datagram->data, cur_data, data_size);
}
~~~

纯 write datagram 不需要把返回 payload 覆盖到本地 data。

读或读写命令则复制收到的数据。

对于 Main Domain datagram，`datagram->data` 正指向 Domain process image 的子区间。

因此 receive parser 的 memcpy 可能直接更新 process image。

## WKC 和 State 在 payload 之后更新

固定源码：

~~~c
datagram->working_counter = EC_READ_U16(cur_data);
...
list_del_init(&datagram->queue);
smp_store_release(&datagram->state, EC_DATAGRAM_RECEIVED);
~~~

顺序很重要：

```text
copy payload
write WKC
remove from queue
release-store RECEIVED
```

release store 给并发读取方一个“完成标志”。

先写数据，再发布完成状态。

## 那为什么还要 ecrt_domain_process

即使 payload 已经进了 process image，Domain 还要处理：

- datagram pair 是否 RECEIVED；
- Working Counter；
- 冗余链路；
- Domain state 变化与统计。

固定 `ecrt_domain_process()`：

~~~c
list_for_each_entry(pair, &domain->datagram_pairs, list) {
#if EC_MAX_NUM_DEVICES > 1
    datagram_pair_wc = ec_datagram_pair_process(pair, wc_sum);
#else
    ec_datagram_pair_process(pair, wc_sum);
#endif
    ...
}
~~~

所以 `receive` 更像“完成网络 datagram”，`domain_process` 更像“把这些 datagram completion 解释成 Domain 状态”。

## 控制算法现在读的是哪一份内存

用户态 `domain1_pd` 指向 mmap 的 process data。

activate 后用户态做：

~~~c
domain->process_data = master->process_data + offset;
~~~

因此：

```text
kernel process data backing
      ^
      | shared mmap
      v
userspace domain1_pd
```

控制算法不需要 copy 一个完整 message object。

它直接访问 process image。

## 第 3 步：应用读输入、计算、写输出

典型操作：

```c
int32_t pos = EC_READ_S32(pd + off_pos);
...
EC_WRITE_S16(pd + off_torque, tau);
```

这一步的时间完全属于应用 WCET。

EtherCAT Master 无法替你保证控制算法是否 100 us 内完成。

## 第 4 步：Domain Queue 并不发送

用户态 `ecrt_domain_queue()` 仍然只是 ioctl。

内核：

~~~c
int ecrt_domain_queue(ec_domain_t *domain)
{
    ec_datagram_pair_t *datagram_pair;
    ec_device_index_t dev_idx;

    list_for_each_entry(datagram_pair, &domain->datagram_pairs, list) {

#if EC_MAX_NUM_DEVICES > 1
        memcpy(datagram_pair->send_buffer,
                datagram_pair->datagrams[EC_DEVICE_MAIN].data,
                datagram_pair->datagrams[EC_DEVICE_MAIN].data_size);
#endif
        ec_master_queue_datagram(domain->master,
                &datagram_pair->datagrams[EC_DEVICE_MAIN]);

        ...
    }
    return 0;
}
~~~

单主设备典型路径就是：

```text
for each prebuilt pair:
    master_queue(main_datagram)
```

没有重新解析 PDO，没有重新 malloc datagram。

## Queue 为什么必须在应用写完输出后

Main datagram 的 data 指针借用 process image。

如果应用在 queue 前修改输出，send 时 frame packing 会从 datagram->data memcpy 到 TX frame。

如果应用在 queue 后、send 前又并发修改同一 process data，那么 frame packing 读到哪个值会形成竞争。

因此推荐时序不仅是 API 风格，而是数据一致性协议：

```text
write output
  -> queue
  -> send
  -> don't concurrently mutate same bytes
```

应用多线程访问 process image 时必须自己同步。

## 第 5 步：Master Send 汇合 FSM 与外部工作

`ecrt_master_send()` 开头：

~~~c
seq_fsm = smp_load_acquire(&master->injection_seq_fsm);
if (master->injection_seq_rt != seq_fsm) {
    // inject datagram produced by master FSM
    ec_master_queue_datagram(master, &master->fsm_datagram);

    smp_store_release(&master->injection_seq_rt, seq_fsm);
}

ec_master_inject_external_datagrams(master);
~~~

所以一次实时 send 不只发 Domain。

它会把控制面 datagram 以受控方式注入统一 queue。

## Link Down 时为什么直接把 Datagram 标 ERROR

固定代码：

~~~c
if (unlikely(!master->devices[dev_idx].link_state)) {
    list_for_each_entry_safe(datagram, n,
            &master->datagram_queue, queue) {
        if (datagram->device_index == dev_idx) {
            list_del_init(&datagram->queue);
            smp_store_release(&datagram->state, EC_DATAGRAM_ERROR);
        }
    }

    ...
    ec_device_poll(&master->devices[dev_idx]);
    ...
    continue;
}
~~~

链路已经 down，就不应该让 datagram 永久停留 QUEUED/SENT。

明确转换到 ERROR，FSM/Domain 上层才能观察失败。

## 真正 Frame Packing 在 ec_master_send_datagrams

这一层才：

```text
take QUEUED datagrams
  -> get tx buffer
  -> fit as many as possible
  -> write EtherCAT headers
  -> memcpy datagram payload
  -> write WKC=0
  -> write frame header
  -> device send
  -> state=SENT
```

具体字段下一篇单独拆。

## 一个周期的数据位置变化

```text
上一轮返回 frame
  |
  v
NIC RX buffer
  |
ecdev_receive
  |
ec_master_receive_datagrams
  | memcpy read data
  v
Kernel Domain process image
  ||
  || mmap shared
  \/
Userspace domain_pd
  |
control reads/writes
  |
domain_queue references same process bytes
  |
master_send frame packing memcpy
  v
TX skb
  |
ndo_start_xmit
  v
wire
```

这张图比一句“EtherCAT 零拷贝”更准确。

固定实现至少存在：

- RX frame -> datagram/process data 的 memcpy；
- process data -> TX skb 的 memcpy。

mmap 消除了 kernel/user process image 之间额外整块复制，但不是整条链 zero-copy。

## 时间语义重新回放

设本周期 wake 在 `t_k`：

```text
t_k
  receive
    poll NIC
    parse previous return frame
  domain_process
  read x_k
  compute u_k
  write u_k
  domain_queue
  master_send
    pack u_k
    NIC transmit
```

所以算法里的 `x_k` 是“最近完成总线交换得到的输入”。

`u_k` 是“本轮即将发出的输出”。

这就是 EtherCAT 控制环最基础的数据年龄模型。