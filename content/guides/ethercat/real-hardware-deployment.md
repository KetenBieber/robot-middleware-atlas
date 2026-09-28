# EtherCAT 真机部署：从独立 NIC 到第一个稳定 1 kHz 周期

这一页讨论真实 IgH Master，不再是 FakeEtherCAT。目标不是给出某一款伺服器的厂家参数，而是建立一套不会把“能通信”误当成“能实时控制”的部署顺序。

固定参考版本仍是 IgH EtherCAT Master 1.6.13。

## 第一步：给 EtherCAT 一块独立网卡

真实控制系统里最稳妥的做法是：

```text
NIC A -> 普通网络 / SSH / ROS / Internet
NIC B -> EtherCAT only
```

不要让 EtherCAT 链路同时承担普通 IP 流量。

原因不是 EtherCAT “不能和 IP 共存”，而是控制系统要尽量减少：

- 网络管理器重配接口；
- 普通协议流量；
- 不可控中断；
- qdisc/stack 干扰；
- 链路状态变化。

## 第二步：先记录网卡身份

建议先固定：

```bash
ip link
ethtool -i enp3s0
cat /sys/class/net/enp3s0/address
```

至少记录：

```text
interface name
MAC address
driver
PCI location
NUMA/CPU locality
```

生产环境里优先用 MAC 绑定 Master，而不是只依赖接口名。

## 第三步：构建真实 Master

上游 INSTALL 给出的主线是：

```bash
./bootstrap
./configure --sysconfdir=/etc
make all modules -j"$(nproc)"
sudo make modules_install install
sudo depmod
```

如果计划使用 generic driver，确认构建配置包含 generic。

如果要用 IgH 提供的特定 EtherCAT-aware NIC driver，则需要根据网卡和内核版本显式启用相应 driver。固定版本配置文件列出的可选模块包括：

```text
8139too
e100
e1000
e1000e
r8169
generic
ccat
igb
igc
genet
dwmac-intel
stmmac-pci
```

不要因为名字“看起来匹配”就直接启用。真正部署前要核对当前内核版本和该 driver 的支持矩阵。

## 第四步：配置 /etc/ethercat.conf

上游模板的两个核心字段是：

```bash
MASTER0_DEVICE="00:11:22:33:44:55"
DEVICE_MODULES="generic"
```

也可以把 `MASTER0_DEVICE` 写成接口名：

```bash
MASTER0_DEVICE="enp3s0"
```

但 MAC 在接口重命名场景下更稳定。

generic driver 还要求对应网卡本身处于 up 状态。配置模板专门提供：

```bash
UPDOWN_INTERFACES="enp3s0"
```

用于 service 启停时自动处理接口。

## 第五步：启动 Master 前先停掉会抢网卡的软件

需要重点检查：

```text
NetworkManager
systemd-networkd
DHCP client
普通 IP 地址配置
bridge/bond
container network
```

不要一刀切地把系统网络服务全部关掉；只隔离 EtherCAT 专用 NIC。

## 第六步：启动并确认字符设备

systemd 系统：

```bash
sudo systemctl start ethercat
sudo systemctl status ethercat
```

确认：

```bash
ls -l /dev/EtherCAT*
```

上游默认使用 root:root 和 0660。开发机如果希望普通用户访问，应使用 udev 规则明确授权，而不是长期 `sudo` 跑整个控制应用。

## 第七步：先用 CLI 看拓扑，不要直接启动控制器

先执行：

```bash
ethercat master
ethercat slaves
ethercat pdos
```

此时至少确认：

```text
从站数量正确
顺序正确
vendor/product 正确
AL state 合理
PDO mapping 与预期一致
```

如果 slave 顺序和代码里的 position 不一致，应用层再漂亮也会配错设备。

## 第八步：应用中的 vendor/product 必须来自真实设备

Fake 项目里使用的 vendor/product ID 是教学占位值。

真机必须换成：

```text
实际 Vendor ID
实际 Product Code
实际 PDO objects
实际 bit length
实际 SyncManager mapping
```

这些应来自：

- 从站厂商 ESI；
- `ethercat slaves -v`；
- `ethercat pdos`；
- 厂家对象字典。

尤其不能直接把 Fake 工程里的：

```text
0x6040
0x6071
0x6041
0x6064
0x606C
```

当作“所有伺服器都完全一样”。

这些对象在 CiA-402 设备中常见，但具体 PDO 是否映射、缩放、模式与 bit 定义仍由真实驱动器配置决定。

## 第九步：先做 process-data echo，再上控制律

第一次真机测试不要马上上复杂控制器。

推荐分三步：

```text
A. 只读
   status / position / velocity

B. 安全写
   control word / disabled torque / zero target

C. 闭环
   小幅度目标
   限幅
   watchdog
   emergency stop path
```

每一步都必须验证 WKC 和 slave state。

## 第十步：给周期线程建立绝对时间基准

典型 Linux 用户态周期不要写：

```c
usleep(1000);
```

因为“睡 1 ms + 执行时间”会累计漂移。

更合理的是：

```text
CLOCK_MONOTONIC
TIMER_ABSTIME
next += 1 ms
clock_nanosleep(next)
```

这只能解决主机调度时间基准，不能替代 EtherCAT DC。

## 第十一步：实时调度与内存

真机阶段至少评估：

```text
SCHED_FIFO
mlockall(MCL_CURRENT | MCL_FUTURE)
stack prefault
CPU affinity
IRQ affinity
PREEMPT_RT
CPU frequency governor
logging isolation
```

上游 userspace 示例本身就演示了 `sched_setscheduler`、`mlockall` 和 stack prefault。

但不要机械照抄最高优先级。

如果把控制线程设为系统最高 SCHED_FIFO 且内部出现死循环，可能把系统其他关键线程饿死。

优先级设计必须连同：

- EtherCAT NIC IRQ；
- 控制线程；
- ROS/规划线程；
- logging；
- watchdog；

一起分析。

## 第十二步：把每个 1 ms 周期拆成预算

不要只测总周期。

至少记录：

```text
T_wakeup_jitter
T_receive_poll
T_domain_process
T_state_read
T_control
T_command_write
T_domain_queue
T_master_send
```

然后关注：

```text
max
p99
p99.9
连续运行趋势
故障期间变化
```

平均值意义有限。

## 第十三步：验证 Working Counter

正常周期不能只看：

```text
position 在变化
```

还要看 Domain state：

```text
working_counter
wc_state
```

因为 WKC 能帮助判断：

- 某个从站是否真正执行了 datagram；
- 逻辑读写是否完整；
- 链路/状态是否异常。

应用层应该定义：

```text
WKC 不完整持续 N 个周期
    -> 降级 / 停机
```

而不是无限继续输出命令。

## 第十四步：再启用 DC

先把普通 PDO 周期跑稳，再配置 Distributed Clocks。

典型验证顺序：

```text
选择 reference clock
→ application time
→ sync reference
→ sync slaves
→ 配置 Sync0/Sync1
→ 观察同步误差
→ 调整 shift
```

DC 的目标不是让 Linux 线程更准，而是让多个从站的设备时间与动作相位对齐。

## 第十五步：故障测试必须主动做

至少人工制造：

```text
拔掉一个从站
断 EtherCAT 网线
停止 controller
重启 Master
让应用超周期
制造错误 PDO mapping
让从站掉出 OP
```

观察：

```text
WKC 多久变化
AL state 多久变化
应用多久进入安全状态
恢复时是否自动重新配置
控制输出是否会残留
```

如果从来没有做过故障测试，就不能说这套 EtherCAT 控制链“稳定”。

## 第十六步：真机代码与 Fake 项目哪些能复用

可以直接复用：

```text
Master/Domain 生命周期
PDO registration 结构
process image offset 访问
receive/process/control/queue/send 主循环
绝对周期调度
状态统计框架
```

必须替换或扩展：

```text
vendor/product
PDO map
CiA-402 状态机
单位缩放
DC 参数
故障状态机
watchdog
安全停机
实时优先级
NIC 配置
```

所以 Fake 项目不是玩具版“另一个项目”，而是把应用架构提前稳定下来。

## 最终验收

真正进入机器人控制前，至少能够回答：

1. 这块 NIC 由哪个 EtherCAT driver 接管？
2. process image 每个 offset 对应哪个从站、哪个 PDO entry？
3. 正常 WKC 应是多少，下降意味着什么？
4. 控制线程的最大 observed cycle time 是多少？
5. DC reference 是谁？
6. Sync0 在控制周期中的相位是什么？
7. 网线断开后多少周期进入安全态？
8. controller 崩溃后驱动器靠什么 watchdog 停止输出？

如果这八个问题没有答案，系统还处于“能跑”阶段，而不是“可以交给机器人运动控制”的阶段。
