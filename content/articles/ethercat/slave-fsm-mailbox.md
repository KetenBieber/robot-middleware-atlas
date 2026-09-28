# Master/Slave FSM 与 Mailbox 源码：怎样把几十毫秒配置事务拆成不会阻塞周期的短步骤

固定源码：EtherLab / IgH EtherCAT Master 1.6.13，提交 `61cc654f5b721ddd54df0f58bdd34106d91c5359`。

这一篇从最关键的控制面问题开始：**如果一个 SDO 或状态切换要等待远端从站，IgH 为什么还能让 Master 持续运行？**

答案不是“开很多线程”，而是分层有限状态机。

## Master FSM 的核心只是一根 state 函数指针

固定结构：

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
    ec_fsm_sii_t fsm_sii;
};
~~~

没有 C++ virtual state class，也没有 coroutine runtime。

状态就是：

```c
void (*state)(ec_fsm_master_t *);
```

这是最直接的 C 状态模式。

## Exec 为什么先看 Datagram State

固定：

~~~c
int ec_fsm_master_exec(ec_fsm_master_t *fsm)
{
    ec_datagram_state_t state = smp_load_acquire(&fsm->datagram->state);

    if (state == EC_DATAGRAM_SENT || state == EC_DATAGRAM_QUEUED) {
        // datagram was not sent or received yet.
        return 0;
    }

    fsm->state(fsm);
    return 1;
}
~~~

这段是整个非阻塞设计的核心。

如果上一步 datagram：

- 还在 QUEUED；
- 已经 SENT 但没回来；

FSM 什么都不做，直接 return。

它不会：

```c
while (datagram not complete) {}
```

也不会 sleep 等这个请求。

下一次 thread iteration 再检查。

## 状态迁移是“修改下一次要调用的函数”

例如配置 slave：

```c
fsm->state = ec_fsm_master_state_configure_slave;
ec_fsm_slave_config_start(&fsm->fsm_slave_config, slave);
fsm->state(fsm); // execute immediately
```

第一次进入新状态可以立即执行一步。

如果这一步准备了 datagram，就返回。

等下次 datagram 完成后继续。

所以调用栈不会跨网络等待长期悬挂。

## 为什么 Master FSM 还要嵌套很多子 FSM

如果只用一个 `state`，也可以把全部逻辑写成几百个 state function。

但 CoE、SoE、PDO、state change 各自有独立协议。

固定 Master FSM 直接组合：

```text
fsm_coe
fsm_soe
fsm_pdo
fsm_eoe
fsm_change
fsm_slave_config
fsm_slave_scan
fsm_sii
```

这不是多余抽象，而是在缩小每个状态机的状态空间。

例如 Master 只需要知道：

> “当前在配置这个 slave”。

而 Slave Config FSM 再知道：

> “当前在应用第 3 个 SDO”。

CoE FSM 再知道：

> “这个 SDO 事务当前等 mailbox response”。

## Slave Config FSM 的状态列表就是配置 Pipeline

源码声明里可以看到一整条阶段链：

```text
INIT
clear sync
DC clear assign
mailbox sync
BOOT/PREOP
SDO config
SoE config
EoE IP
PDO config
watchdog
PDO sync
FMMU
DC cycle
DC sync check
DC start
DC assign
SAFEOP
OP
```

这几乎就是“一个 EtherCAT slave 如何从未知配置走到可运行”的 executable specification。

## Slave Config Exec 与 Master Exec 使用同一种等待协议

固定：

~~~c
int ec_fsm_slave_config_exec(ec_fsm_slave_config_t *fsm)
{
    if (fsm->datagram->state == EC_DATAGRAM_SENT
        || fsm->datagram->state == EC_DATAGRAM_QUEUED) {
        return ec_fsm_slave_config_running(fsm);
    }

    fsm->state(fsm);
    return ec_fsm_slave_config_running(fsm);
}
~~~

也就是说所有 FSM 都把 Datagram State 当作异步 I/O completion token。

这比每个协议自己发明 wait object 简洁很多。

## 一个 SDO Config 是怎样开始的

固定 `ec_fsm_slave_config_enter_sdo_conf()`：

~~~c
if (!slave->config) {
    ec_fsm_slave_config_enter_pdo_sync(fsm);
    return;
}

if (list_empty(&slave->config->sdo_configs)) {
    ec_fsm_slave_config_enter_soe_conf_preop(fsm);
    return;
}

fsm->state = ec_fsm_slave_config_state_sdo_conf;
fsm->request = list_entry(fsm->slave->config->sdo_configs.next,
        ec_sdo_request_t, list);

ec_sdo_request_copy(&fsm->request_copy, fsm->request);
ecrt_sdo_request_write(&fsm->request_copy);
ec_fsm_coe_transfer(fsm->fsm_coe, fsm->slave, &fsm->request_copy);
ec_fsm_coe_exec(fsm->fsm_coe, fsm->datagram);
~~~

这里出现一个很重要的 ownership 设计：

```c
ec_sdo_request_copy(&fsm->request_copy, fsm->request);
```

FSM 不直接让 in-flight 协议依赖配置 list 里对象的可变状态，而复制一份 request working state。

配置对象和执行中事务因此部分解耦。

## 为什么下一轮 State 函数只调用 CoE Exec

~~~c
if (ec_fsm_coe_exec(fsm->fsm_coe, fsm->datagram)) {
    return;
}
~~~

如果 CoE 还没结束，就 return。

结束后才判断：

~~~c
if (!ec_fsm_coe_success(fsm->fsm_coe)) {
    ...
    fsm->state = ec_fsm_slave_config_state_error;
    return;
}
~~~

所以 Slave Config FSM 把 CoE FSM 当作一个异步子程序。

这和普通同步函数：

```c
ret = write_sdo();
if (ret) ...
```

在控制流上完全不同。

## 多个 SDO 怎样串行推进

成功后：

~~~c
if (fsm->request->list.next != &fsm->slave->config->sdo_configs) {
    fsm->request = list_entry(fsm->request->list.next,
            ec_sdo_request_t, list);
    ...
    ec_fsm_coe_exec(...);
    return;
}
~~~

所以配置 list 本身承担“待执行程序”的角色。

这和数据结构设计直接相关：

- `list_head` 保存稳定顺序；
- iterator 可以自然走下一个；
- 配置期数量通常不大；
- 不需要 priority queue。

## 为什么配置被运行中删除时要重新检查

源码多处：

~~~c
if (!fsm->slave->config) {
    ec_fsm_slave_config_reconfigure(fsm);
    return;
}
~~~

这是生命周期防御。

FSM 是跨多个调度周期存在的。

开始事务时 `slave->config` 存在，不代表几毫秒后仍存在。

长生命周期状态机必须在关键边界重新验证它借用的对象。

## Master FSM 怎样决定是否需要重扫总线

固定 Master FSM 保存：

```text
link_state[]
slaves_responding[]
rescan_required
slave_states[]
```

当检测拓扑变化并允许 scan 时，会：

- 标记 `scan_busy`；
- 清理旧 slave objects；
- 根据响应数量分配新的 slave 数组；
- 重新扫描。

所以 `ec_slave_t *` 的地址不是永恒稳定的业务身份。

这再次解释了为什么 `Slave Config` 不能直接等同物理 `Slave`。

## Master Operation Thread 与 FSM 是怎样配合的

OP phase 的 kernel thread 并不直接 send 所有 frame。

它主要推进 FSM：

~~~c
if (seq_rt == master->injection_seq_fsm) {
    if (down_interruptible(&master->master_sem)) {
        break;
    }

    if (ec_fsm_master_exec(&master->fsm)) {
        smp_store_release(&master->injection_seq_fsm,
                master->injection_seq_fsm + 1);
    }

    ec_master_exec_slave_fsms(master);
    up(&master->master_sem);
}
~~~

FSM 准备出新 datagram 后，只推进 injection sequence。

真正 RT `ecrt_master_send()` 看到 sequence 改变后才把 `fsm_datagram` 放进主 queue。

因此线程分工：

```text
operation thread:
    decide/configure next control-plane datagram

RT application thread:
    inject + pack + physically send it
```

这样控制面不会独立抢网卡发送时刻。

## Slave FSM 还有 External Datagram Ring

多个 slave FSM 不能都争用唯一 `fsm_datagram`。

固定 `ec_master_exec_slave_fsms()` 会从 external ring 申请 free datagram。

如果没有空闲：

```text
No free datagrams at the moment
```

就不会动态 malloc 一个“救急”。

这体现了有界资源设计。

## Mailbox FSM 为什么特别适合这套模型

CoE 事务可能经历：

```text
send mailbox request
wait
poll/check mailbox
fetch response
parse SDO
maybe next segment
```

每一步都可表示为：

```text
prepare datagram
return
wait for datagram state
advance state
```

所以同一套 Datagram completion model 同时支撑：

- 总线扫描；
- AL state change；
- SDO；
- SoE；
- PDO config；
- DC config。

## 一个错误路径怎样冒泡

以 SDO 配置失败为例：

~~~c
if (!ec_fsm_coe_success(fsm->fsm_coe)) {
    EC_SLAVE_ERR(fsm->slave, "SDO configuration failed.\n");
    fsm->slave->error_flag = 1;
    fsm->state = ec_fsm_slave_config_state_error;
    return;
}
~~~

错误不只是 return -1。

因为调用早已跨很多周期，不存在原始同步 caller 栈等着接返回值。

所以错误必须写进长期对象状态：

```text
slave->error_flag
fsm->state = error
```

这是异步状态机和同步 API 最根本的错误传播差异之一。

## 这套 FSM 设计的代价

优点：

- 不因单个从站等待阻塞整个 thread；
- 协议可分层；
- datagram I/O 统一；
- 能做 retry/timeout；
- 配置对象可长期保持。

代价：

- 状态数量很多；
- 错误路径分散；
- 生命周期更难；
- 很难靠函数调用栈调试；
- 每个 borrowed pointer 都要考虑跨周期失效。

因此源码文章后续看到一个 `fsm->state = ...`，不要只问“下一状态是什么”，还要问：

> 哪个对象必须活到下一次 exec？哪个 datagram 正在 in-flight？失败后谁清理？