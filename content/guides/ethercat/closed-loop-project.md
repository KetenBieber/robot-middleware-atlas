# FakeEtherCAT 双进程闭环：把 PDO、Process Image 和 1 kHz 控制周期真正连起来

理论部分已经把 PDO、FMMU、Domain、Datagram 与周期收发拆开；源码部分也已经追到 FakeEtherCAT 的 RtIPC 实现。现在用一个完整小工程把这些机制重新组合起来。

工程位于：

~~~text
examples/ethercat/closed_loop/
├── CMakeLists.txt
├── README.md
├── include/
│   └── virtual_servo.h
├── scripts/
│   └── run_fake.sh
└── src/
    ├── controller.c
    └── plant_sim.c
~~~

它不是 Hello World，而是一个最小单关节闭环：

~~~text
controller
    读取 position / velocity / status
    ↓
PD control
    ↓
写 target torque / control word
    |
    | FakeEtherCAT + RtIPC
    v
plant_sim
    读取 torque
    ↓
积分 J*qdd + b*qd = tau
    ↓
回写 position / velocity / status
~~~

这套工程的目的不是模拟一台真实伺服器，而是让应用侧最重要的 EtherCAT 结构真正动起来：Slave Config、SyncManager、RxPDO/TxPDO、Domain、PDO entry offset、process image，以及 receive/process/queue/send。

FakeEtherCAT 模拟的是 process-data level。它不会替你验证真实 Ethernet frame、WKC 故障、AL state 转换、DC 或网卡抖动。

## 先解决最容易混淆的方向问题

从控制器角度看：

~~~text
RxPDO = slave receives from master = master writes = EC_DIR_OUTPUT
TxPDO = slave transmits to master = master reads = EC_DIR_INPUT
~~~

controller 因此把 SM2 配成 OUTPUT、SM3 配成 INPUT。

plant simulator 扮演从站，必须完全反过来：

~~~text
Controller OUTPUT  <----> Plant INPUT
Controller INPUT   <----> Plant OUTPUT
~~~

这正是 upstream FakeEtherCAT back-to-back process-data emulation 的核心。

## virtual_servo.h —— PDO、SyncManager 与方向反转

~~~c
#pragma once

#include <ecrt.h>

#define ATLAS_SERVO_ALIAS 0
#define ATLAS_SERVO_POSITION 0
#define ATLAS_SERVO_VENDOR_ID 0x00000001u
#define ATLAS_SERVO_PRODUCT_CODE 0x00000001u

#define ATLAS_PERIOD_NS 1000000L
#define ATLAS_POSITION_SCALE 1000000.0
#define ATLAS_VELOCITY_SCALE 1000000.0

static const ec_pdo_entry_info_t atlas_rxpdo_entries[] = {
    {0x6040, 0x00, 16},  /* synthetic control word */
    {0x6071, 0x00, 16},  /* synthetic target torque */
};

static const ec_pdo_info_t atlas_rxpdos[] = {
    {0x1600, 2, atlas_rxpdo_entries},
};

static const ec_pdo_entry_info_t atlas_txpdo_entries[] = {
    {0x6041, 0x00, 16},  /* synthetic status word */
    {0x6064, 0x00, 32},  /* position */
    {0x606C, 0x00, 32},  /* velocity */
};

static const ec_pdo_info_t atlas_txpdos[] = {
    {0x1A00, 3, atlas_txpdo_entries},
};

/*
 * Controller perspective:
 *   SM2 / RxPDO: master writes commands to the virtual drive.
 *   SM3 / TxPDO: master reads state from the virtual drive.
 */
static const ec_sync_info_t atlas_controller_syncs[] = {
    {2, EC_DIR_OUTPUT, 1, atlas_rxpdos},
    {3, EC_DIR_INPUT, 1, atlas_txpdos},
    {0xff},
};

/*
 * Plant-emulator perspective required by libfakeethercat:
 * swap EC_DIR_OUTPUT and EC_DIR_INPUT so the two processes connect
 * back-to-back through RtIPC.
 */
static const ec_sync_info_t atlas_plant_syncs[] = {
    {2, EC_DIR_INPUT, 1, atlas_rxpdos},
    {3, EC_DIR_OUTPUT, 1, atlas_txpdos},
    {0xff},
};
~~~

两边使用同一组 PDO index、entry index、slave address 和 vendor/product 教学值。真正不同的只有 SyncManager direction。这样 FakeEtherCAT 会把同一个 RtIPC process-data key 的生产者和消费者接起来。

这里使用的是固定数组，而不是动态容器。PDO schema 在程序启动以后不会变化，因此静态只读数组最合适：没有扩容、没有节点分配、地址稳定，也更接近真实 EtherCAT 配置期冻结的思路。

## CMakeLists.txt —— 同一份应用连接稳定 EtherCAT C ABI

~~~cmake
cmake_minimum_required(VERSION 3.16)
project(atlas_ethercat_closed_loop LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

find_path(ECRT_INCLUDE_DIR
    NAMES ecrt.h
    HINTS
        ${ETHERCAT_ROOT}
        ENV ETHERCAT_ROOT
    PATH_SUFFIXES include)

find_library(ETHERCAT_LIBRARY
    NAMES ethercat
    HINTS
        ${ETHERCAT_ROOT}
        ENV ETHERCAT_ROOT
    PATH_SUFFIXES lib lib64)

if(NOT ECRT_INCLUDE_DIR)
    message(FATAL_ERROR
        "ecrt.h was not found. Set ETHERCAT_ROOT or -DECRT_INCLUDE_DIR=...")
endif()

if(NOT ETHERCAT_LIBRARY)
    message(FATAL_ERROR
        "libethercat was not found. Set ETHERCAT_ROOT or -DETHERCAT_LIBRARY=...")
endif()

add_library(atlas_ethercat_api INTERFACE)
target_include_directories(atlas_ethercat_api INTERFACE
    ${ECRT_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(atlas_ethercat_api INTERFACE
    ${ETHERCAT_LIBRARY})

add_executable(atlas_ethercat_controller src/controller.c)
target_link_libraries(atlas_ethercat_controller PRIVATE atlas_ethercat_api m)

add_executable(atlas_ethercat_plant src/plant_sim.c)
target_link_libraries(atlas_ethercat_plant PRIVATE atlas_ethercat_api m)

target_compile_options(atlas_ethercat_controller PRIVATE -Wall -Wextra -Wpedantic)
target_compile_options(atlas_ethercat_plant PRIVATE -Wall -Wextra -Wpedantic)
~~~

工程正常寻找 ecrt.h 与 libethercat。Fake 模式不要求改 controller 源码；运行时通过 LD_LIBRARY_PATH 把 libethercat.so.1 解析到 libfakeethercat.so.1。这个设计让同一份应用代码可以先跑 fake，再切到真实 Master。

## controller.c —— 1 kHz 控制循环

~~~c
#define _POSIX_C_SOURCE 200809L

#include "virtual_servo.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    unsigned int control_word;
    unsigned int target_torque;
    unsigned int status_word;
    unsigned int position;
    unsigned int velocity;
} atlas_offsets_t;

static void add_ns(struct timespec *t, long ns)
{
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) {
        t->tv_nsec -= 1000000000L;
        ++t->tv_sec;
    }
}

static int16_t clamp_i16(double value, double limit)
{
    if (value > limit) value = limit;
    if (value < -limit) value = -limit;
    return (int16_t) llround(value);
}

int main(int argc, char **argv)
{
    const long cycles = argc > 1 ? strtol(argv[1], NULL, 10) : 5000;
    if (cycles <= 0) {
        fprintf(stderr, "cycles must be positive\n");
        return 2;
    }

    ec_master_t *master = ecrt_request_master(0);
    if (!master) {
        fprintf(stderr, "ecrt_request_master(0) failed\n");
        return 3;
    }

    ec_domain_t *domain = ecrt_master_create_domain(master);
    if (!domain) {
        fprintf(stderr, "ecrt_master_create_domain() failed\n");
        ecrt_release_master(master);
        return 4;
    }

    ec_slave_config_t *sc = ecrt_master_slave_config(
        master,
        ATLAS_SERVO_ALIAS,
        ATLAS_SERVO_POSITION,
        ATLAS_SERVO_VENDOR_ID,
        ATLAS_SERVO_PRODUCT_CODE);
    if (!sc) {
        fprintf(stderr, "ecrt_master_slave_config() failed\n");
        ecrt_release_master(master);
        return 5;
    }

    if (ecrt_slave_config_pdos(sc, EC_END, atlas_controller_syncs)) {
        fprintf(stderr, "ecrt_slave_config_pdos() failed\n");
        ecrt_release_master(master);
        return 6;
    }

    atlas_offsets_t off = {0};
    const ec_pdo_entry_reg_t regs[] = {
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6040, 0x00, &off.control_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6071, 0x00, &off.target_torque},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6041, 0x00, &off.status_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6064, 0x00, &off.position},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x606C, 0x00, &off.velocity},
        {}
    };

    if (ecrt_domain_reg_pdo_entry_list(domain, regs)) {
        fprintf(stderr, "ecrt_domain_reg_pdo_entry_list() failed\n");
        ecrt_release_master(master);
        return 7;
    }

    if (ecrt_master_activate(master)) {
        fprintf(stderr, "ecrt_master_activate() failed\n");
        ecrt_release_master(master);
        return 8;
    }

    uint8_t *pd = ecrt_domain_data(domain);
    if (!pd) {
        fprintf(stderr, "ecrt_domain_data() returned NULL\n");
        ecrt_release_master(master);
        return 9;
    }

    printf("controller offsets: cw=%u torque=%u status=%u pos=%u vel=%u\n",
           off.control_word, off.target_torque, off.status_word,
           off.position, off.velocity);

    struct timespec next;
    if (clock_gettime(CLOCK_MONOTONIC, &next)) {
        perror("clock_gettime");
        ecrt_release_master(master);
        return 10;
    }

    for (long k = 0; k < cycles; ++k) {
        add_ns(&next, ATLAS_PERIOD_NS);
        const int sleep_rc = clock_nanosleep(
            CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        if (sleep_rc && sleep_rc != EINTR) {
            fprintf(stderr, "clock_nanosleep: %s\n", strerror(sleep_rc));
            break;
        }

        ecrt_master_receive(master);
        ecrt_domain_process(domain);

        const int32_t raw_position = EC_READ_S32(pd + off.position);
        const int32_t raw_velocity = EC_READ_S32(pd + off.velocity);
        const uint16_t status = EC_READ_U16(pd + off.status_word);

        const double position = raw_position / ATLAS_POSITION_SCALE;
        const double velocity = raw_velocity / ATLAS_VELOCITY_SCALE;
        const double t = (double) k * 0.001;
        const double target_position = 0.45 * sin(2.0 * 3.141592653589793 * 0.25 * t);

        /*
         * A deliberately small PD controller. The command is a synthetic
         * torque unit used only by the fake plant; it is not a CiA-402 drive
         * scaling and must not be copied to real hardware unchanged.
         */
        const double torque_cmd =
            1800.0 * (target_position - position) - 40.0 * velocity;

        EC_WRITE_U16(pd + off.control_word, 0x000f);
        EC_WRITE_S16(pd + off.target_torque, clamp_i16(torque_cmd, 3000.0));

        if (k % 500 == 0) {
            printf("k=%ld target=%+.4f pos=%+.4f vel=%+.4f torque=%d status=0x%04x\n",
                   k, target_position, position, velocity,
                   (int) EC_READ_S16(pd + off.target_torque), status);
        }

        ecrt_domain_queue(domain);
        ecrt_master_send(master);
    }

    EC_WRITE_S16(pd + off.target_torque, 0);
    ecrt_domain_queue(domain);
    ecrt_master_send(master);
    ecrt_release_master(master);
    return 0;
}
~~~

控制循环严格保持一条稳定主线：

~~~text
receive
→ domain_process
→ read process image
→ control
→ write process image
→ domain_queue
→ master_send
~~~

所有 PDO offset 在 activate 之前注册。周期里不遍历对象树、不查字符串、不创建 request，只做 base pointer + offset 的固定访问。

周期等待使用 CLOCK_MONOTONIC + TIMER_ABSTIME。相对 sleep 会把本周期执行时间累积到下一周期，而绝对时刻能让周期相位围绕既定时间线推进。

这里的 ±3000 torque command 是教学用合成单位，不是任何真实 CiA-402 伺服器的电流或力矩标度。

## plant_sim.c —— 反向 PDO 的虚拟从站

~~~c
#define _POSIX_C_SOURCE 200809L

#include "virtual_servo.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    unsigned int control_word;
    unsigned int target_torque;
    unsigned int status_word;
    unsigned int position;
    unsigned int velocity;
} atlas_offsets_t;

static void add_ns(struct timespec *t, long ns)
{
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) {
        t->tv_nsec -= 1000000000L;
        ++t->tv_sec;
    }
}

static int32_t to_i32(double value, double scale)
{
    const double scaled = value * scale;
    if (scaled > 2147483647.0) return INT32_MAX;
    if (scaled < -2147483648.0) return INT32_MIN;
    return (int32_t) llround(scaled);
}

int main(int argc, char **argv)
{
    const long cycles = argc > 1 ? strtol(argv[1], NULL, 10) : 8000;
    if (cycles <= 0) {
        fprintf(stderr, "cycles must be positive\n");
        return 2;
    }

    ec_master_t *master = ecrt_request_master(0);
    if (!master) {
        fprintf(stderr, "plant: ecrt_request_master(0) failed\n");
        return 3;
    }

    ec_domain_t *domain = ecrt_master_create_domain(master);
    if (!domain) {
        fprintf(stderr, "plant: ecrt_master_create_domain() failed\n");
        ecrt_release_master(master);
        return 4;
    }

    ec_slave_config_t *sc = ecrt_master_slave_config(
        master,
        ATLAS_SERVO_ALIAS,
        ATLAS_SERVO_POSITION,
        ATLAS_SERVO_VENDOR_ID,
        ATLAS_SERVO_PRODUCT_CODE);
    if (!sc) {
        fprintf(stderr, "plant: ecrt_master_slave_config() failed\n");
        ecrt_release_master(master);
        return 5;
    }

    if (ecrt_slave_config_pdos(sc, EC_END, atlas_plant_syncs)) {
        fprintf(stderr, "plant: ecrt_slave_config_pdos() failed\n");
        ecrt_release_master(master);
        return 6;
    }

    atlas_offsets_t off = {0};
    const ec_pdo_entry_reg_t regs[] = {
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6040, 0x00, &off.control_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6071, 0x00, &off.target_torque},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6041, 0x00, &off.status_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6064, 0x00, &off.position},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x606C, 0x00, &off.velocity},
        {}
    };

    if (ecrt_domain_reg_pdo_entry_list(domain, regs)) {
        fprintf(stderr, "plant: PDO registration failed\n");
        ecrt_release_master(master);
        return 7;
    }

    if (ecrt_master_activate(master)) {
        fprintf(stderr, "plant: activate failed\n");
        ecrt_release_master(master);
        return 8;
    }

    uint8_t *pd = ecrt_domain_data(domain);
    if (!pd) {
        fprintf(stderr, "plant: domain data is NULL\n");
        ecrt_release_master(master);
        return 9;
    }

    /*
     * Synthetic one-axis dynamics:
     *     J*qdd + b*qd = tau
     * This process is only for exercising PDO direction, process-image
     * offsets and cyclic exchange. It is not a motor/drive model.
     */
    const double dt = 0.001;
    const double inertia = 0.05;
    const double damping = 0.22;
    double position = 0.0;
    double velocity = 0.0;

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);

    for (long k = 0; k < cycles; ++k) {
        add_ns(&next, ATLAS_PERIOD_NS);
        const int sleep_rc = clock_nanosleep(
            CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        if (sleep_rc && sleep_rc != EINTR) {
            fprintf(stderr, "plant clock_nanosleep: %s\n", strerror(sleep_rc));
            break;
        }

        ecrt_master_receive(master);
        ecrt_domain_process(domain);

        const uint16_t control_word = EC_READ_U16(pd + off.control_word);
        const int16_t target_torque = EC_READ_S16(pd + off.target_torque);
        const double torque_nm = target_torque / 1000.0;

        const double acceleration =
            (torque_nm - damping * velocity) / inertia;
        velocity += acceleration * dt;
        position += velocity * dt;

        const uint16_t synthetic_status =
            control_word ? 0x0027u : 0x0040u;

        EC_WRITE_U16(pd + off.status_word, synthetic_status);
        EC_WRITE_S32(pd + off.position,
                     to_i32(position, ATLAS_POSITION_SCALE));
        EC_WRITE_S32(pd + off.velocity,
                     to_i32(velocity, ATLAS_VELOCITY_SCALE));

        if (k % 500 == 0) {
            printf("plant k=%ld pos=%+.4f vel=%+.4f tau=%+.3f\n",
                   k, position, velocity, torque_nm);
        }

        ecrt_domain_queue(domain);
        ecrt_master_send(master);
    }

    ecrt_release_master(master);
    return 0;
}
~~~

plant 仍然使用同一套 ecrt API。区别只在 PDO 方向和业务逻辑：它读取 controller 写出的 target torque，积分一个最小二阶机械系统，然后把 position、velocity 和 synthetic status 写回 process image。

简化模型只有：

~~~text
J*qdd + b*qd = tau
~~~

所以这个进程的目标是验证数据闭环，不是模拟驱动器电流环、编码器、减速器或 CiA-402 状态机。

## run_fake.sh —— 自动完成 FakeEtherCAT 启动顺序

~~~bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build}"
FAKE_HOME="${FAKE_EC_HOMEDIR:-/tmp/atlas-fake-ethercat}"
SHIM_DIR="$BUILD_DIR/fake-lib-shim"

: "${FAKE_EC_SO:?Set FAKE_EC_SO to the full path of libfakeethercat.so.1}"

CONTROLLER="$BUILD_DIR/atlas_ethercat_controller"
PLANT="$BUILD_DIR/atlas_ethercat_plant"

if [[ ! -x "$CONTROLLER" || ! -x "$PLANT" ]]; then
    echo "Build the project first: cmake -S . -B build && cmake --build build -j"
    exit 2
fi

mkdir -p "$SHIM_DIR"
ln -sfn "$(realpath "$FAKE_EC_SO")" "$SHIM_DIR/libethercat.so.1"

export LD_LIBRARY_PATH="$SHIM_DIR:${LD_LIBRARY_PATH:-}"
export FAKE_EC_HOMEDIR="$FAKE_HOME"
export FAKE_EC_PREFIX="/atlas"
rm -rf "$FAKE_EC_HOMEDIR"
mkdir -p "$FAKE_EC_HOMEDIR"

warm_pid=""
plant_pid=""

cleanup() {
    [[ -n "$warm_pid" ]] && kill "$warm_pid" 2>/dev/null || true
    [[ -n "$plant_pid" ]] && kill "$plant_pid" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/3] Start a first controller instance so its output PDOs exist."
FAKE_EC_NAME=atlas-controller-bootstrap "$CONTROLLER" 30000 &
warm_pid=$!
sleep 1

echo "[2/3] Start the plant with swapped PDO directions."
FAKE_EC_NAME=atlas-plant "$PLANT" 8000 &
plant_pid=$!
sleep 1

echo "[3/3] Restart controller so it discovers the plant input variables."
kill "$warm_pid" 2>/dev/null || true
wait "$warm_pid" 2>/dev/null || true
warm_pid=""

FAKE_EC_NAME=atlas-controller "$CONTROLLER" 5000

wait "$plant_pid" || true
plant_pid=""
echo "Fake EtherCAT closed loop finished."
~~~

这个启动顺序来自 FakeEtherCAT 的发现机制。控制应用第一次 activate 时先创建自己的 output PDO；plant 随后创建反向 PDO；最后重启 controller，使其重新 activate 并发现 plant 创建的输入变量。脚本把这三个阶段自动化。

## 一次周期里数据到底在哪里

两个进程都各自拥有一块 Domain process image：

~~~text
Controller process image
  [control][torque][status][position][velocity]
        |        ^        ^        ^
        |        |        |        |
        +-------- RtIPC process-data exchange -------+
                                                     |
Plant process image                                  |
  [control][torque][status][position][velocity] <----+
~~~

这不是两个进程把同一个 C struct mmap 到同一地址。

FakeEtherCAT 根据 PDO mapping，把各自 Domain byte buffer 的对应区间注册为 RtIPC producer 或 consumer。domain_process() 负责 receive-side 更新，domain_queue() 负责 transmit-side 更新。

真实 Master 与 Fake 的上层语义因此能保持一致：

~~~text
application sees process image

Fake path:
process image ↔ RtIPC

Real path:
process image ↔ Domain datagram ↔ EtherCAT frame ↔ slave
~~~

## 为什么 offset 仍然必须由 Domain 注册

即使是 fake，也没有硬编码：

~~~text
byte 0 = control
byte 2 = torque
byte 4 = position
~~~

程序仍调用 ecrt_domain_reg_pdo_entry_list()，由 EtherCAT API 返回每个 PDO entry 的 offset。

后续切换真实 Master 时，控制算法仍旧是：

~~~text
read pd + off.position
write pd + off.target_torque
~~~

应用不需要知道 FMMU、datagram 或 frame 的具体布局。

## 为什么用 atlas_offsets_t，而不是字符串 Map

示例把五个 offset 聚合进一个小 struct。

如果改成：

~~~text
unordered_map<string, unsigned>
~~~

然后周期中按 "position"、"velocity" 查 hash table，会平白增加 hash、字符串比较、pointer chasing、动态容器生命周期，而运行时根本没有这种动态需求。

EtherCAT 控制程序更典型的分工是：

~~~text
配置阶段：
    可以使用复杂描述、解析 ESI、生成映射

周期阶段：
    编译成固定 offset、固定数组、连续 process image
~~~

这和 IgH 自身 Domain/FMMU/activate 的设计完全一致。

## 为什么 process image 是 uint8_t 指针，而不是 ServoPdo struct

真实 PDO 可能不是自然 C struct 布局：

- entry 可能非字节对齐；
- 多个从站会拼接；
- 厂商 PDO 顺序不同；
- C struct 有 padding/alignment；
- host ABI 不等于总线布局。

所以 IgH 提供 EC_READ/EC_WRITE 宏，通过 byte buffer + offset 访问字段，而不是要求应用直接 cast 一个业务 struct。

## 构建与运行

Linux 下安装好 IgH userspace library 后：

~~~bash
cd examples/ethercat/closed_loop
cmake -S . -B build -DETHERCAT_ROOT=/usr/local
cmake --build build -j
~~~

Fake 模式：

~~~bash
export FAKE_EC_SO=/usr/local/lib/libfakeethercat.so.1
bash scripts/run_fake.sh
~~~

正常运行时 controller 与 plant 每 500 周期打印一次。关键不是输出格式，而是观察因果闭环：

~~~text
controller torque changes
    ↓
plant reads torque
    ↓
plant state changes
    ↓
controller reads new position/velocity
    ↓
PD error changes
    ↓
next torque changes
~~~

如果 position 永远为 0，说明 PDO producer/consumer 没真正接起来。

## 这个工程证明了什么

它能验证：

- ecrt 用户态 API 的对象组织；
- PDO/SyncManager direction；
- PDO entry registration；
- process-image offset；
- cyclic receive/process/queue/send；
- 双应用 process-data exchange；
- 一条 1 kHz 闭环程序的基本形状。

它不能证明：

- NIC 与 driver 的时序；
- EtherCAT frame/datagram 的线行为；
- WKC 故障检测；
- 从站 AL 状态转换；
- CoE/SDO 真实往返；
- Distributed Clocks；
- 真机 CiA-402；
- 实时 worst-case latency。

这些边界必须保留，否则 fake 跑通很容易被误解成“EtherCAT 真机已经完成”。

下一步进入 [真实网卡与从站部署](real-hardware-deployment.md)，把 Fake 层没有覆盖的 NIC、WKC、AL state、DC 与安全停机补齐。
