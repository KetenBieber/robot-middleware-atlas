# EtherCAT 环境准备：先把 FakeEtherCAT 跑通，再碰真实网卡

这一页不从“装驱动”开始，而是先把学习环境拆成两层：

```text
层 1：无硬件
    libfakeethercat + RtIPC
    用来验证应用 API、PDO 映射、process image 和周期代码

层 2：真实 EtherCAT
    kernel master + EtherCAT-capable NIC path + slaves
    用来验证 WKC、AL state、DC、链路时序和真实设备行为
```

这两层不能混为一谈。FakeEtherCAT 可以让你在没有伺服器、没有 EtherCAT 专用网卡的机器上练习 IgH 用户态 API，但它不会模拟真实 NIC、EtherCAT frame 往返、Working Counter 错误或 Distributed Clocks。

## 为什么先做 FakeEtherCAT

上来就碰真机，调试变量太多：

- 内核版本；
- Master 模块是否加载；
- NIC driver 是否兼容；
- 网卡是否被 NetworkManager 抢占；
- 从站拓扑；
- vendor/product ID；
- ESI/PDO mapping；
- AL state；
- DC；
- 进程实时调度；
- 控制代码本身。

如果应用一启动就报错，你很难判断是“代码错”还是“现场环境错”。

因此第一阶段只验证：

```text
ecrt_request_master
→ create_domain
→ slave_config
→ configure PDO
→ register offsets
→ activate
→ domain_data
→ receive/process
→ read/write process image
→ queue/send
```

## FakeEtherCAT 的真实能力边界

固定到 IgH 1.6.13 的 upstream FakeEtherCAT 文档中，明确支持：

- 创建 Master 与 Domain；
- activate、send、receive；
- process/queue Domain；
- 配置 PDO；
- 记录 SDO 配置；
- 用 RtIPC 做 process-data 级别的双应用仿真。

但它也明确说明，很多 API 只是返回成功，并不模拟真实总线故障。

所以这一层适合验证：

```text
应用对象关系
PDO 方向
offset
process image
周期代码结构
双进程闭环
```

而不适合验证：

```text
真实 WKC 失配
真实 AL transition
真实 mailbox timeout
真实 DC drift
真实网卡 latency/jitter
```

## Linux 是首选环境

IgH EtherCAT Master 本身面向 Linux 内核，FakeEtherCAT 也依赖 RtIPC。因此这套实践默认在 Linux 下完成。

推荐使用：

```text
Ubuntu / Debian 类发行版
gcc / g++
cmake
autoconf / automake / libtool
pkg-config
Linux kernel headers
```

真实主站阶段再额外关注：

```text
PREEMPT_RT
CPU isolation
IRQ affinity
NIC driver
systemd service
udev permissions
```

## 构建 FakeEtherCAT

上游源码自身给出的要求是先安装 RtIPC，然后在 IgH configure 时打开：

```bash
--enable-fakeuserlib
```

一个典型的 userspace-only 构建思路是：

```bash
./bootstrap
./configure \
  --disable-kernel \
  --enable-fakeuserlib \
  --prefix=/usr/local
make -j"$(nproc)"
sudo make install
sudo ldconfig
```

这里的 `--disable-kernel` 是为了当前阶段只构建用户态库，不要求你先解决真实内核模块。

如果 configure 提示找不到 `librtipc`，先确认：

```bash
pkg-config --modversion librtipc
```

能找到安装结果。

## 为什么官方推荐用 LD_LIBRARY_PATH 重定向

FakeEtherCAT 的目标之一是：

> 同一份应用，不改源码、不重新编译，只替换运行时加载的 EtherCAT 用户态库。

正常情况下应用链接：

```text
libethercat.so.1
```

Fake 模式下准备一个目录：

```text
fake-lib-shim/
└── libethercat.so.1 -> libfakeethercat.so.1
```

然后：

```bash
export LD_LIBRARY_PATH=/path/to/fake-lib-shim
```

这样同一个可执行文件仍然调用：

```c
ecrt_master_receive(...)
ecrt_domain_process(...)
ecrt_domain_queue(...)
ecrt_master_send(...)
```

但底层从真实 ioctl/mmap Master 换成 FakeEtherCAT/RtIPC。

这其实是非常值得学习的 ABI 设计：应用依赖稳定的 C API，而运行时实现可以替换。

## FakeEtherCAT Home 为什么必须单独设置

FakeEtherCAT 通过 RtIPC 保存运行时配置和 process-data 变量，需要：

```bash
export FAKE_EC_HOMEDIR=/tmp/FakeEtherCAT
mkdir -p "$FAKE_EC_HOMEDIR"
```

如果同时跑 controller 和 simulator，还建议设置不同：

```bash
FAKE_EC_NAME=controller ...
FAKE_EC_NAME=plant ...
```

但两边必须使用相同的 `FAKE_EC_HOMEDIR` 与 `FAKE_EC_PREFIX`，才能连接到同一组过程数据变量。

## 为什么 simulator 要交换 PDO 方向

控制应用看一个驱动器：

```text
RxPDO / EC_DIR_OUTPUT
    Master -> Drive
    target torque

TxPDO / EC_DIR_INPUT
    Drive -> Master
    position / velocity
```

模拟器要扮演 Drive，所以必须反过来：

```text
Controller EC_DIR_OUTPUT
        ↕ shared variable
Plant EC_DIR_INPUT

Controller EC_DIR_INPUT
        ↕ shared variable
Plant EC_DIR_OUTPUT
```

这不是“随便改方向”，而是在同一 process-data channel 上把 producer/consumer 对接起来。

## 下一步

环境准备完成后直接进入 [FakeEtherCAT 双进程闭环](closed-loop-project.md)。

等这个工程能稳定跑通，再进入 [真实网卡与从站部署](real-hardware-deployment.md)。这样真机出现问题时，你至少已经知道应用侧对象图、PDO offset 与周期调用顺序本身是怎么工作的。
