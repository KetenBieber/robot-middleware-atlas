# 真实案例二：IPE EtherCAT ROS 2 Control 怎样把 SOEM 放进 1 ms PDO、CiA‑402 与 ros2_control 三层结构

案例仓库：

- `https://github.com/haomingyi/ipe-ethercat-ros2-control.git`
- 固定提交：`6a7ee41910d681e01c261f5b50abb69f2a94caaa`

仓库 README 将自己描述为针对一台 IPE IRGML-14-I EtherCAT 旋转关节的 hardware-tested engineering reference。

这里已经完整出现：

~~~text
Application / trajectory
↓
ROS 2 controller
↓
ros2_control HardwareInterface
↓
CiA-402 + device PDO
↓
SOEM
↓
Raw Ethernet
↓
joint
~~~

## 1. 最重要的分层：SOEM 没有直接塞进 ros2_control plugin

项目把低层拆成：

~~~text
ethercat_common.c
    SOEM 生命周期
    1 ms PDO loop
    WKC / DC
    slave recovery

ecat_motor_master.c
    device PDO
    CiA-402
    mode / command

ipe_three_mode_ros2_control_hardware.cpp
    ros2_control lifecycle
    command limits
    state/command interfaces
~~~

这条边界非常合理：EtherCAT Master 机制、驱动器协议、机器人控制框架不是一个层次的问题。

## 2. SOEM 启动链仍然非常直接

~~~c
if (ec_init(ethercat_ifname) > 0)
{
   if (ec_config_init(FALSE) > 0)
   {
      ec_configdc();

      for (uint16_t dc_slave =
              FIRST_SLAVE;
           dc_slave <= ec_slavecount;
           ++dc_slave)
      {
         ec_dcsync0(
            dc_slave,
            TRUE,
            DC_SYNC_CYCLE_MS *
               1000000,
            20000);
      }

      int iomap_size =
         ec_config_map(&IOmap);

      expected_wkc =
         (ec_group[0].outputsWKC * 2) +
         ec_group[0].inputsWKC;
   }
}
~~~

所以真实机器人项目仍然沿：

~~~text
init
→ discover
→ DC
→ IOmap
→ expected WKC
~~~

只是外围安全与设备语义更多。

## 3. SAFE_OP 先跑过程数据，再生成第一条运动命令

~~~c
for (int safeop_cycle = 0;
     safeop_cycle < 10;
     ++safeop_cycle)
{
   ec_send_processdata();
   ec_receive_processdata(
      EC_TIMEOUTRET);

   osal_usleep(1000);
}
~~~

源码注释说明：Inputs 在 SAFE_OP 已有效，所以先读实际状态，再准备第一帧输出，使 CSP 可以从测得的当前位置起步。

这条原则对机器人特别重要：

~~~text
先建立可信反馈
→ 再产生第一条运动命令
~~~

## 4. 1 ms PDO thread 的真实组织

~~~c
clock_nanosleep(
   CLOCK_MONOTONIC,
   TIMER_ABSTIME,
   &ts,
   &tleft);

if (dorun > 0)
{
   pthread_mutex_lock(
      &pdo_mutex);

   ec_send_processdata();

   int wkc =
      ec_receive_processdata(
         EC_TIMEOUTRET);

   last_wkc = wkc;

   pthread_mutex_unlock(
      &pdo_mutex);

   if ((expected_wkc > 0) &&
       (wkc < expected_wkc))
   {
      if (++bad_wkc_cycles >= 10)
         esc_pdo_error = true;
   }
   else
   {
      bad_wkc_cycles = 0;
      esc_pdo_error = false;
   }

   if (ec_slave[0].hasdc)
      ec_sync(
         ec_DCtime,
         cycletime,
         &toff);
}
~~~

这里直接出现四个工程选择：

1. absolute-time wakeup；
2. process data 在统一 PDO mutex 下完成；
3. 连续 WKC 异常达到阈值才置 PDO error；
4. DC time 修正 host phase。

## 5. 恢复线程为什么独立

另一条 `ecatcheck` 线程负责：

~~~c
ec_readstate();

...

ec_reconfig_slave(
   slave,
   EC_TIMEOUTMON);

...

ec_recover_slave(
   slave,
   EC_TIMEOUTMON);
~~~

它不是 1 ms PDO thread。原因正是这些操作可能包含 blocking transaction。

所以项目明确把：

~~~text
cyclic dataplane
≠
recovery control plane
~~~

## 6. 设备层把 SOEM 的 IOmap 指针解释成 CiA‑402 PDO

~~~c
TCiA402PDO1600 *output =
   (TCiA402PDO1600 *)
      ec_slave[slave].outputs;

TCiA402PDO1A00 *input =
   (TCiA402PDO1A00 *)
      ec_slave[slave].inputs;
~~~

读反馈：

~~~c
positions[...] =
   input->ObjPositionActualValue;

velocities[...] =
   input->ObjVelocityActualValue;

torques[...] =
   (int32_t)
      input->ObjTorqueActualValue;
~~~

写命令：

~~~c
output->ObjControlWord =
   CONTROLWORD_COMMAND_ENABLEOPERATION;
~~~

这是一种非常直接的 typed process-image adapter。

它的前提是 C struct layout 必须与实际 PDO byte layout 完全一致，所以项目继续做尺寸核对：

~~~c
if (ec_slave[slave].Obits !=
       sizeof(TCiA402PDO1600) * 8 ||
    ec_slave[slave].Ibits !=
       sizeof(TCiA402PDO1A00) * 8)
{
   ...
   return -13;
}
~~~

这和 ETH RSL 案例里的 PDO size check 是同一个工程原则。

## 7. Blocking SDO 被留在配置路径

~~~c
int write_wkc =
   ec_SDOwrite(
      slave,
      0x60C2,
      0x01,
      FALSE,
      sizeof(cycle_ms),
      &cycle_ms,
      EC_TIMEOUTRXM);

int read_wkc =
   ec_SDOread(
      slave,
      0x60C2,
      0x01,
      FALSE,
      &size,
      &pid_data,
      EC_TIMEOUTRXM);
~~~

而且做 write-read verification。

这正是 SOEM blocking SDO primitive 的正确位置：startup/configuration，而不是 1 ms hot path。

## 8. ros2_control HardwareInterface 先核设备身份

`on_configure()`：

~~~cpp
if (ecatm_set_interface(
       interface_name_.c_str()) < 0 ||
    ecatm_init_passive(
       "csp",
       1) < 0)
{
   ...
}
~~~

然后：

~~~cpp
if (ecatm_get_slave_identity(
       0,
       &vendor,
       &product,
       &revision) < 0 ||
    vendor != kIpeVendorId ||
    product != kIrgmlProductId)
{
   ...
}
~~~

机器人控制层不会因为“总线上有一个 slave”就默认它是目标执行器。

## 9. read() 把 PDO health 转成 ros2_control 错误语义

~~~cpp
if (!master_started_ ||
    !ecatm_is_pdo_healthy())
{
   disable_commanding();
   return
      hardware_interface::
         return_type::ERROR;
}

esc_get_states(
   &actual_count_,
   &actual_velocity_,
   &actual_torque_);
~~~

于是形成：

~~~text
wire WKC/state
→ bus health
→ HardwareInterface health
→ controller behavior
~~~

## 10. write() 不只是把 command 塞进 PDO

真实接口还检查：

- command finite；
- travel limit；
- following error；
- rated velocity；
- raw torque/velocity range；
- slew rate；
- active mode；
- drive enabled；
- PDO health。

例如 CST：

~~~cpp
if (!finite_int32(
       get_command<double>(
          torque_command_interface_name_),
       &requested) ||
    std::llabs(
       static_cast<int64_t>(
          requested)) >
       cst_max_raw_)
{
   disable_commanding();
   return
      hardware_interface::
         return_type::ERROR;
}
~~~

成熟 SOEM 使用者不会把 `ec_slave[].outputs` 直接暴露给 planner/controller。

## 11. 源码机制与真实工程落点

| SOEM 机制 | IPE 项目中的落点 |
| --- | --- |
| `ec_init` / raw NIC | `ethercat_common_start` |
| `ec_config_init` | 从站发现 |
| IOmap | `IOmap[4096]` |
| slave input/output pointer | CiA‑402 PDO struct |
| expected WKC | PDO health |
| DCtime | host phase PI correction |
| SDO blocking API | startup/config verification |
| reconfig/recover | 独立恢复线程 |
| process-data send/receive | 1 ms PDO worker |
| AL/PDO health | ros2_control `read()` gate |

## 12. 这个案例暴露的真正系统边界

SOEM 只解决 EtherCAT Master。

真实关节系统还需要：

~~~text
CiA-402
device identity
unit conversion
joint zero
gear ratio
mode switching
command slew
following error
ROS lifecycle
controller ownership
emergency stop
~~~

理解 SOEM 的边界，才能知道哪些责任应该留在 EtherCAT 层，哪些必须上移到设备层和机器人控制层。
