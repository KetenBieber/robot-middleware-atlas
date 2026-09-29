# Master 生命周期：从 ecrt_request_master 到 activate，谁在什么时候拥有总线

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

这一篇只追一件事：**应用调用 request/activate/deactivate 时，Master 内部到底发生了什么 phase 变化？**

## request 不是“new 一个 Master”

内核里的 Master 实例并不是在 `ecrt_request_master()` 里临时 malloc 出来的。

固定代码首先从全局 Master 数组取对象：

~~~c
master = &masters[master_index];
~~~

随后用全局 `master_sem` 保护 reserved 标志：

~~~c
if (down_interruptible(&master_sem)) {
    errptr = ERR_PTR(-EINTR);
    goto out_return;
}

if (master->reserved) {
    up(&master_sem);
    EC_MASTER_ERR(master, "Master already in use!\n");
    errptr = ERR_PTR(-EBUSY);
    goto out_return;
}
master->reserved = 1;
up(&master_sem);
~~~

所以 `request_master` 的核心语义是：

> 独占取得一个已经由模块管理的 Master 实例。

不是创建任意数量的 Master 对象。

## 为什么还要检查 Device Module

拿到 reserved 还不够。源码继续遍历 Master 的设备：

~~~c
for (; dev_idx < ec_master_num_devices(master); dev_idx++) {
    ec_device_t *device = &master->devices[dev_idx];
    if (!try_module_get(device->module)) {
        ...
        errptr = ERR_PTR(-ENODEV);
        goto out_module_put;
    }
}
~~~

`try_module_get` 增加网卡驱动模块引用，防止应用正在使用 EtherCAT 时对应 kernel module 被卸载。

这是典型的跨模块 lifetime protocol：

```text
Master owns logical Device relation
Linux module refcount owns driver code lifetime
```

只保存函数指针而不 pin module，会出现：

```text
device->poll points into module
module unloaded
Master calls dangling function pointer
```

## request 时为什么要求 Master 已经处于 IDLE

固定源码检查：

~~~c
if (master->phase != EC_IDLE) {
    up(&master->device_sem);
    EC_MASTER_ERR(master, "Master still waiting for devices!\n");
    errptr = ERR_PTR(-ENODEV);
    goto out_release;
}
~~~

之后调用：

~~~c
if (ec_master_enter_operation_phase(master)) {
    ...
}
~~~

这里的“operation phase”不要和 EtherCAT slave 的 OP state 混为一谈。

Master phase 是主站内部生命周期；Slave AL state 是从站协议状态。

两个 OP/operation 概念层次不同。

## request 成功后 Master 的职责发生变化

在应用尚未 request Master 时，Master 自己需要维护总线扫描等内部工作。

应用取得 Master 后，应用准备配置：

```text
request
  -> create Domain
  -> create Slave Config
  -> configure PDO/DC/SDO
  -> register PDO entries
  -> activate
```

这段时间属于配置阶段。

重要特征是：允许 kmalloc、list 操作和复杂查找。

## Create Domain 为什么用 Master Semaphore

内核 `ecrt_master_create_domain_err()`：

~~~c
if (!(domain =
            (ec_domain_t *) kmalloc(sizeof(ec_domain_t), GFP_KERNEL))) {
    ...
    return ERR_PTR(-ENOMEM);
}

down(&master->master_sem);

if (list_empty(&master->domains)) {
    index = 0;
} else {
    last_domain = list_entry(master->domains.prev, ec_domain_t, list);
    index = last_domain->index + 1;
}

ec_domain_init(domain, master, index);
list_add_tail(&domain->list, &master->domains);

up(&master->master_sem);
~~~

这里非常适合观察配置期 STL 对应物：

- C++ 可能用 `std::list<Domain>`；
- Linux kernel C 使用 intrusive `struct list_head`；
- Domain 节点本身携带 `list` 字段；
- 不需要单独分配 list node。

为什么 intrusive list 很常见？

内核对象经常：

- 生命周期由对象本身控制；
- 不能依赖 C++；
- 希望避免额外节点分配；
- 需要 `container_of` 从节点回到对象。

## Activate 是整个主站最重要的 Phase Barrier

固定 `ecrt_master_activate()` 第一件大事是 finish all domains：

~~~c
down(&master->master_sem);

// finish all domains
domain_offset = 0;
list_for_each_entry(domain, &master->domains, list) {
    ret = ec_domain_finish(domain, domain_offset);
    if (ret < 0) {
        up(&master->master_sem);
        ...
        return ret;
    }
    domain_offset += domain->data_size;
}

up(&master->master_sem);
~~~

这一步把之前的“配置图”编译成周期运行结构。

可以类比编译器：

```text
before activate:
  declarative config / lists / mappings

activate:
  compile

after activate:
  process image / datagram pairs / offsets
```

所以 activate 不只是“start = true”。

## 为什么 Domain 要分配连续 Base Address

多个 Domain 可能分别有自己的 `data_size`。

activate 通过：

```c
domain_offset += domain->data_size;
```

给每个 Domain 分配逻辑 base。

概念上：

```text
Domain0 size 64
  base = 0

Domain1 size 32
  base = 64

Domain2 size 16
  base = 96
```

每个 Domain 内部 FMMU 原先是相对地址，finish 时再加上最终 base。

这保证整个 Master 的逻辑过程地址空间不冲突。

## Activate 为什么停止再重启 Master Thread

finish Domain 后，源码：

~~~c
ec_master_thread_stop(master);
...
master->send_cb = master->app_send_cb;
master->receive_cb = master->app_receive_cb;
master->cb_data = master->app_cb_data;
...
ret = ec_master_thread_start(master, ec_master_operation_thread,
            "EtherCAT-OP");
~~~

这是非常重要的执行模型切换。

Master 在不同 phase 使用不同 thread behavior 和 callback。

activate 后：

- 应用负责周期调用 send/receive；
- Master operation thread 仍推进 FSM 等后台控制面；
- send/receive callback 可以切换成应用提供的回调。

因此不能把 “Master 有 kernel thread” 理解成“用户不需要周期调用 ecrt_master_send/receive”。

公开 API 文档也明确要求 activate 后实时应用负责周期通信。

## Datagram Injection Sequence 为什么在 Activate 重置

源码：

~~~c
master->injection_seq_fsm = 0;
master->injection_seq_rt = 0;
~~~

后面 `ecrt_master_send()` 会比较两者，决定是否把 FSM datagram 注入实时发送侧。

这是一个跨执行上下文 handshake。

设计问题是：

```text
operation thread/FSM produces datagram
RT application calls master_send
```

两个执行路径不能同时无协议修改同一 queue。

sequence number 用来表达“FSM 侧产生了新工作，RT 侧还没消费”。

这一 producer-consumer 交接通过 acquire/release 内存序建立可见性关系，避免 FSM 侧的新 datagram 状态被 RT 发送路径以错误顺序观察。

## active 与 config_changed 为什么分开

activate 最后：

~~~c
master->allow_scan = 1;
master->active = 1;

// notify state machine, that the configuration shall now be applied
master->config_changed = 1;
~~~

`active` 表示主站进入应用运行阶段。

`config_changed` 则告诉 Master FSM：

> 现在应该把应用配置真正推进到 slave。

因此 activate 返回不意味着所有 slave 已经瞬间 OP。

这点对应用状态机非常重要。

正确认知是：

```text
activate returns
  -> cyclic communication responsibility switches
  -> FSM begins/applies configuration
  -> slave states converge toward requested state
```

应用仍应监测 slave config state。

## Deactivate 为什么不是简单 active=0

固定实现先停 operation thread，然后恢复内部 send/receive callback：

~~~c
ec_master_thread_stop(master);

master->send_cb = ec_master_internal_send_cb;
master->receive_cb = ec_master_internal_receive_cb;
master->cb_data = master;
~~~

接着：

~~~c
ec_master_clear_config(master);
~~~

然后把所有 slave 请求到 PREOP，并强制标记未来需要重配置：

~~~c
for (slave = master->slaves;
        slave < master->slaves + master->slave_count;
        slave++) {
    ec_slave_request_state(slave, EC_SLAVE_STATE_PREOP);
    slave->force_config = 1;
}
~~~

最后重启 IDLE thread。

所以 deactivate 是一次**运行时所有权交还**：

```text
application-driven operation
   ↓
stop OP thread
   ↓
clear app configuration
   ↓
slave -> PREOP / mark reconfigure
   ↓
internal idle master resumes
```

## 为什么清零 App/DC Time

deactivate：

~~~c
master->app_time = 0ULL;
master->dc_ref_time = 0ULL;
~~~

这避免下一次 activation 继续沿用旧时间基准。

时间域也是 lifecycle state，不只是一个全局 clock getter。

## Release 与 Deactivate 的层次差异

Deactivate 的语义是：

> 结束当前应用配置/运行阶段，但 Master 对象仍被请求者持有。

Release 则是：

> 应用不再占用这个 Master，撤销 reserved 与 driver module 引用。

这和文件系统很像：

```text
deactivate ~ stop session
release    ~ close handle
```

应用清理顺序应明确，而不是进程退出时赌内核自动回收所有高层状态。

## 生命周期状态图

```text
module/device setup
      |
      v
EC_IDLE
      |
 ecrt_request_master
      |
      v
operation phase, not active
      |
 configure domains/slaves/PDO
      |
 ecrt_master_activate
      |
      v
active application phase
  |        |
  |        +-- operation thread advances FSM
  |        +-- RT app calls receive/send
  |
 ecrt_master_deactivate
      |
      v
clear config / slaves PREOP
      |
      v
idle thread
      |
 ecrt_release_master
      |
      v
reserved = 0 / module refs released
```

后面 Domain 文章会深入 activate 期间最关键的 `ec_domain_finish()`，看配置图怎样真正变成 process image 和 datagram pair。