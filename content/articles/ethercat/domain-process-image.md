# Domain 与 Process Image 源码：PDO Entry 怎样一路变成 offset、FMMU 和 LRD/LWR/LRW

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

理论部分已经知道：

```text
PDO entry -> SyncManager -> FMMU -> logical address -> process image
```

现在直接看 IgH 怎么把这条链写出来。

## 第一步：应用传入的 registration 本质是“帮我算 offset”

典型 API：

```c
ecrt_domain_reg_pdo_entry_list(domain, regs);
```

内核实现并没有马上发送 EtherCAT frame，而是逐项建立配置：

~~~c
int ecrt_domain_reg_pdo_entry_list(ec_domain_t *domain,
        const ec_pdo_entry_reg_t *regs)
{
    const ec_pdo_entry_reg_t *reg;
    ec_slave_config_t *sc;
    int ret;

    for (reg = regs; reg->index; reg++) {
        sc = ecrt_master_slave_config_err(domain->master, reg->alias,
                reg->position, reg->vendor_id, reg->product_code);
        if (IS_ERR(sc))
            return PTR_ERR(sc);

        ret = ecrt_slave_config_reg_pdo_entry(sc, reg->index,
                        reg->subindex, domain, reg->bit_position);
        if (ret < 0)
            return ret;

        *reg->offset = ret;
    }

    return 0;
}
~~~

这段代码有三个层次：

```text
registration item
  -> identify/create slave config
  -> locate PDO entry in that config
  -> prepare FMMU/domain mapping
  -> return byte offset
```

## 为什么数组用 index==0 作为终止哨兵

循环条件：

```c
for (reg = regs; reg->index; reg++)
```

这是 C API 常见的 sentinel array。

调用者最后放一个全零元素。

优点：

- 不需要额外 length 参数；
- 静态 initializer 很方便。

代价：

- 终止条件依赖约定；
- 不能把 index 0 当有效 entry；
- 忘记 sentinel 会越界读取。

现代 C++ API 可能更偏向 `span`/vector + size，但 kernel/user ABI 的 C 接口需要兼容性和简单布局。

## 第二步：在 SyncManager/PDO 中找目标 Entry

固定 `ecrt_slave_config_reg_pdo_entry()` 核心：

~~~c
for (sync_index = 0; sync_index < EC_MAX_SYNC_MANAGERS; sync_index++) {
    sync_config = &sc->sync_configs[sync_index];
    bit_offset = 0;

    list_for_each_entry(pdo, &sync_config->pdos.list, list) {
        list_for_each_entry(entry, &pdo->entries, list) {
            if (entry->index != index || entry->subindex != subindex) {
                bit_offset += entry->bit_length;
            } else {
                bit_pos = bit_offset % 8;
                if (bit_position) {
                    *bit_position = bit_pos;
                } else if (bit_pos) {
                    ...
                    return -EFAULT;
                }

                sync_offset = ec_slave_config_prepare_fmmu(
                        sc, domain, sync_index, sync_config->dir);
                if (sync_offset < 0)
                    return sync_offset;

                return sync_offset + bit_offset / 8;
            }
        }
    }
}
~~~

这个循环就是“从语义对象找到过程内存位置”的核心。

### bit_offset 为什么每个 SyncManager 重新从 0 开始

PDO 在某个 SyncManager 的 process area 内连续排列。

所以 entry offset 首先是：

```text
offset inside this SyncManager mapped area
```

然后再加 FMMU 在 Domain 内的 logical offset。

## 第三步：prepare_fmmu 避免为同一 SM 重复建映射

固定实现先找是否已有相同 Domain + sync_index：

~~~c
for (i = 0; i < sc->used_fmmus; i++) {
    fmmu = &sc->fmmu_configs[i];
    if (fmmu->domain == domain && fmmu->sync_index == sync_index)
        return fmmu->logical_start_address;
}
~~~

如果没有，才占用一个新 FMMU：

~~~c
fmmu = &sc->fmmu_configs[sc->used_fmmus++];

down(&sc->master->master_sem);
ec_fmmu_config_init(fmmu, sc, domain, sync_index, dir);
up(&sc->master->master_sem);

return fmmu->logical_start_address;
~~~

这里有一个重要不变量：

> 同一 slave config 的同一 SyncManager 在同一 Domain 中不应该因为注册多个 PDO entry 而重复创建 FMMU。

否则同一 process data 区域会被重复占空间。

## 第四步：FMMU Init 像一个线性分配器

`ec_fmmu_config_init()`：

~~~c
fmmu->sc = sc;
fmmu->sync_index = sync_index;
fmmu->dir = dir;

fmmu->logical_start_address = domain->data_size;
fmmu->data_size = ec_pdo_list_total_size(
        &sc->sync_configs[sync_index].pdos);

ec_domain_add_fmmu_config(domain, fmmu);
~~~

而 add：

~~~c
fmmu->domain = domain;

domain->data_size += fmmu->data_size;
list_add_tail(&fmmu->list, &domain->fmmu_configs);
~~~

因此 Domain 地址布局是单调增长：

```text
logical_start = old data_size
data_size += mapped SM size
```

没有 free-list，没有 hole reuse。

这是因为配置在 activate 前建立，布局稳定后长期使用；复杂 allocator 没有必要。

## 第五步：应用此时拿到的 Offset 仍是 Domain 内相对值

注册返回：

```c
return sync_offset + bit_offset / 8;
```

这里的 `sync_offset` 来自当前 Domain 的 `data_size` 体系。

Master 还可能有多个 Domain。

最终全局逻辑地址 base 要到 activate 时才确定。

所以要区分：

```text
application pointer offset
    relative to domain->data

EtherCAT logical address
    domain logical_base_address + relative offset
```

应用 mmap 后每个 Domain 自己有 data pointer，所以它只需要相对 offset。

## 第六步：activate 调用 ec_domain_finish

Master：

~~~c
domain_offset = 0;
list_for_each_entry(domain, &master->domains, list) {
    ret = ec_domain_finish(domain, domain_offset);
    ...
    domain_offset += domain->data_size;
}
~~~

Domain finish 首先保存：

~~~c
domain->logical_base_address = base_address;
~~~

如果 process image 使用 internal memory，则一次性分配：

~~~c
if (domain->data_size && domain->data_origin == EC_ORIG_INTERNAL) {
    if (!(domain->data =
                (uint8_t *) kmalloc(domain->data_size, GFP_KERNEL))) {
        ...
        return -ENOMEM;
    }
}
~~~

注意：这是 activate/configuration path，不是 1 kHz cyclic path。

## 第七步：Finish 把 FMMU 分组成 Datagram

核心循环：

~~~c
list_for_each_entry(fmmu, &domain->fmmu_configs, list) {

    fmmu->logical_start_address += base_address;

    if (datagram_size + fmmu->data_size > EC_MAX_DATA_SIZE) {
        ret = ec_domain_add_datagram_pair(domain,
                domain->logical_base_address + datagram_offset,
                datagram_size, domain->data + datagram_offset,
                datagram_used);
        ...
        datagram_offset += datagram_size;
        datagram_size = 0;
        ...
    }

    if (shall_count(fmmu, datagram_first_fmmu)) {
        datagram_used[fmmu->dir]++;
    }

    datagram_size += fmmu->data_size;
}
~~~

这里第一次看见“配置对象编译成 wire object”。

### 为什么按 EC_MAX_DATA_SIZE 切

一个 EtherCAT datagram payload 有协议长度上限，同时还要放进 Ethernet frame。

Domain process image 可能远大于单 datagram。

所以 activate 预先分块，而不是每个周期动态切。

## 第八步：shall_count 为什么要防重复计算 WKC

一个从站可能在同一 datagram 内有多个 FMMU。

expected WKC 的计算规则并不是简单“FMMU 数量”。

`shall_count()` 向前扫描当前 datagram 内已经出现过的 FMMU，如果同一 slave config + direction 已经计数，就不重复加。

这是一段典型的配置期 O(n²) 倾向逻辑：

```text
for each FMMU:
    scan previous FMMUs in current datagram
```

为什么可以接受？

因为它只发生在 activate，不是每周期。

实时系统经常愿意用更贵的初始化换更简单的 steady state。

## 第九步：ec_domain_add_datagram_pair 分配真正周期对象

固定代码：

~~~c
if (!(datagram_pair = kmalloc(sizeof(ec_datagram_pair_t), GFP_KERNEL))) {
    ...
    return -ENOMEM;
}

ret = ec_datagram_pair_init(datagram_pair, domain, logical_offset, data,
        data_size, used);
...
domain->expected_working_counter +=
    datagram_pair->expected_working_counter;

list_add_tail(&datagram_pair->list, &domain->datagram_pairs);
~~~

之后 1 kHz 周期看到的就主要是 `domain->datagram_pairs`，而不是 FMMU 配置链。

## 第十步：Datagram Pair 根据方向一次性选 LRD/LWR/LRW

固定 constructor：

~~~c
if (used[EC_DIR_OUTPUT] && used[EC_DIR_INPUT]) {
    ec_datagram_lrw_ext(&pair->datagrams[EC_DEVICE_MAIN],
            logical_offset, data_size, data);

    pair->expected_working_counter =
        used[EC_DIR_OUTPUT] * 2 + used[EC_DIR_INPUT];

} else if (used[EC_DIR_OUTPUT]) {
    ec_datagram_lwr_ext(&pair->datagrams[EC_DEVICE_MAIN],
            logical_offset, data_size, data);

    pair->expected_working_counter = used[EC_DIR_OUTPUT];

} else {
    ec_datagram_lrd_ext(&pair->datagrams[EC_DEVICE_MAIN],
            logical_offset, data_size, data);

    pair->expected_working_counter = used[EC_DIR_INPUT];
}
~~~

注意函数名后缀 `_ext`。

Main datagram 使用外部数据指针，也就是 Domain process image 的相应区间。

因此 main process datagram 并不需要另建一份 payload storage。

## Process Image 到 Main Datagram 是借用关系

构造时传入：

```c
data = domain->data + datagram_offset
```

然后 LRx ext datagram 保存这段外部 memory。

所以所有权关系：

```text
Domain owns process image
   |
   +-- Datagram Pair borrows subrange
```

这解释了为什么 Domain 必须活得比 datagram pair 长。

也解释了 cyclic `queue` 前后的数据复制语义要看 main/backup 分支区别。

## 冗余链路为什么需要 Datagram Pair 而不是单 Datagram

`pair->datagrams[dev_idx]` 为每个 device 保存一个 datagram。

主 device 可以直接引用 process image。

backup datagram 则需要自己的 buffer：

~~~c
for (dev_idx = EC_DEVICE_BACKUP;
        dev_idx < ec_master_num_devices(domain->master); dev_idx++) {
    ret = ec_datagram_prealloc(&pair->datagrams[dev_idx], data_size);
    ...
}
~~~

如果编译支持多设备，还额外分配 `send_buffer`。

所以 Pair 抽象不是为了代码好看，而是为了表达：

> 同一逻辑 process-data operation 在多物理链路上的多个 in-flight 副本。

## 最终编译过程

```text
PDO entry registrations
        |
        v
Slave Config PDO lists
        |
        v
prepare_fmmu
        |
        v
Domain fmmu_configs + data_size
        |
     activate
        |
        v
ec_domain_finish
        |
        +-- allocate process image
        +-- assign logical base
        +-- split by EC_MAX_DATA_SIZE
        +-- choose LRD/LWR/LRW
        +-- compute expected WKC
        |
        v
Domain datagram_pairs
```

这就是 IgH 把复杂设备配置“编译”成周期数据结构的核心。