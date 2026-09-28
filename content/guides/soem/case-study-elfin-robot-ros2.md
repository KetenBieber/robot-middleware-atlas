# 真实案例：Elfin ROS2 机械臂怎样直接把 SOEM 放进 1 ms EtherCAT 驱动线程

案例仓库：[`huayan-robotics/elfin_robot_ros2`](https://github.com/huayan-robotics/elfin_robot_ros2)

本页固定案例提交：`fddb7d0813ce4bac9a11fd3dd8f9e8828a0926cb`。

核心源码：[`elfin_ethercat_driver/src/elfin_ethercat_manager.cpp`](https://github.com/huayan-robotics/elfin_robot_ros2/blob/fddb7d0813ce4bac9a11fd3dd8f9e8828a0926cb/elfin_ethercat_driver/src/elfin_ethercat_manager.cpp)。

这个案例比 `soem_interface` 更接近“最终机器人产品代码”：它是 Elfin 机械臂 ROS2 包中的 EtherCAT Manager，自己创建周期线程，并直接读写 SOEM 的 IOmap。

它也使用较老的 SOEM global `ec_*` API，而不是 2.0 的 Context-only `ecx_*` API。我们关注的是软件结构和实际调用位置。

## 第一层：EtherCatManager 自己拥有 IOmap 和周期线程

类里直接保存：

~~~text
uint8_t iomap_[4096]
boost::thread cycle_thread_
boost::mutex iomap_mutex_
bool stop_flag_
~~~

构造过程是：

~~~text
initSoem(interface)
→ if success
→ spawn cycleWorker
~~~

这正是 SOEM Library 架构的直接后果：

> Master 没有强制后台执行器，应用自己决定谁来驱动周期。

## 第二层：启动链几乎就是 SOEM 设计图的工程展开

`initSoem()` 依次完成：

~~~text
ec_init
→ ec_config_init
→ PRE_OP check
→ ec_config_map(iomap_)
→ ec_configdc
→ SAFE_OP check
→ send/receive valid process data
→ request selected slaves OP
~~~

所以我们前面拆的：

~~~text
NIC → discovery → IOmap → DC → AL state → process data
~~~

在真实机器人项目中并没有消失，只是被压进一个 manager 初始化函数。

## 第三层：1 ms 周期里真正做什么

项目周期常量是 1 ms。

循环里的 SOEM 核心只有：

~~~cpp
sent = ec_send_processdata();
wkc = ec_receive_processdata(EC_TIMEOUTRET);
~~~

这再次证明 SOEM 的使用方式非常直接：周期线程本身就是 Master execution loop。

然后它立即比较：

~~~text
actual WKC
vs
outputsWKC * 2 + inputsWKC
~~~

如果 WKC 不足，就进入 `handleErrors()`。

## 第四层：真实项目真的会把 recovery policy 写在应用里

`handleErrors()` 对从站状态进行分层处理：

~~~text
SAFE_OP + ERROR
→ ACK

SAFE_OP
→ request OP

still reachable but wrong state
→ `ec_reconfig_slave(...)`

state missing
→ mark lost

lost + no state
→ `ec_recover_slave(...)`
~~~

这几乎就是我们 `fault-recovery` 一文讨论的 SOEM sample recovery tree，被真正搬进了机器人驱动。

它说明：

> SOEM 提供 recovery primitive，但何时重配、哪些 slave 允许进入 OP、哪些设备例外，最终都是机器人项目自己的 policy。

这个案例甚至对特定 slave address 有特殊状态处理，进一步说明 fault policy 往往和具体硬件拓扑绑定。

## 第五层：IOmap 没有再抽象一层 Device DTO

这个项目的读写接口直接使用：

~~~text
ec_slave[slave_no].outputs[channel]
ec_slave[slave_no].inputs[channel]
~~~

并用 `Obits/Ibits` 检查 channel 是否超界。

这是一种更直接、更薄的设计。

优点：

- 代码少；
- EtherCAT memory layout 非常直观；
- 调试时容易看到具体 byte。

代价：

- 设备业务语义和 byte offset 耦合更紧；
- 如果 PDO layout 改变，上层 API 很容易受到影响；
- typed data、endianness、bit packing 更依赖调用者自己管理。

这和 ETH RSL `EthercatSlaveBase` 的抽象形成很好的对照。

## 第六层：SDO 为什么被做成模板函数

项目把同步 SDO 包成 C++ template：

~~~text
writeSDO<T>(slave, index, subindex, value)
readSDO<T>(slave, index, subindex)
~~~

底层仍然调用 SOEM blocking SDO。

这正好验证我们对 SOEM 的判断：

> Library API 很适合被 C++ 包装成易用同步接口，但这不改变它可能阻塞当前线程的执行语义。

因此这类 SDO 适合配置线程/服务调用，不应该不加分析地进入 1 ms hot loop。

## 第七层：它用了绝对时间睡眠，但时间基选择仍值得审视

周期逻辑使用 `TIMER_ABSTIME`，所以不会简单采用“工作时间 + 相对 sleep”不断累积漂移。

但案例使用的是 `CLOCK_REALTIME`。

而 SOEM 2.0 Linux OSAL 的周期/timeout 基础更偏向 `CLOCK_MONOTONIC`。

两者的区别是：

~~~text
CLOCK_REALTIME
    会受系统墙钟校时影响

CLOCK_MONOTONIC
    适合描述经过时间和周期 deadline
~~~

所以这个真实案例非常适合作为代码审查素材：

> “用了 absolute sleep”只是第一步，还要继续检查 clock source。

## 第八层：Mutex 的粒度也值得审

send/receive 与应用读写 IOmap 共用同一把 `boost::mutex`。

这保证：

~~~text
cycle thread
和
ROS/application thread
不会同时修改 IOmap
~~~

但它也意味着非实时线程如果长时间持锁，会直接影响 1 ms EtherCAT 周期。

所以进一步工程化时应继续问：

- 能否用 command snapshot / double buffer？
- 非 RT 写命令是否只更新 lock-free/latest-value buffer？
- SOEM Context lock 和业务 command lock 是否应该分开？

这正是从“能运行”走向“可证明实时”的下一层。

## 第九层：这个案例把我们前面的设计结论全部放进机器人场景

| SOEM 机制 | Elfin ROS2 中的实际体现 |
| --- | --- |
| Library-owned protocol, app-owned execution | Manager 自己创建 1 ms thread |
| IOmap | 固定 `iomap_[4096]` |
| process-data split phase | 每周期 send + receive |
| WKC | 直接触发错误处理 |
| AL states | PRE_OP / SAFE_OP / OP 显式推进 |
| SDO | C++ template 包装同步读写 |
| recovery primitive | `reconfig` / `recover` 进入项目策略 |
| application synchronization | IOmap mutex |

## 为什么这个案例比自制 Demo 更适合专题结尾

因为它让我们看到的不是“为了教学设计出来的理想结构”，而是真实机器人项目不得不面对的妥协：

- 旧版 global API；
- 设备地址特判；
- ROS 线程与 EtherCAT 周期线程共享数据；
- WKC 后的恢复状态机；
- 绝对 sleep 但 clock source 仍可改进；
- 直接 byte IOmap 带来的简单与耦合。

这些真实取舍，才是阅读 SOEM 源码之后最值得拿来做设计审查的材料。