# EtherCAT Fake Closed Loop

This teaching project exercises the IgH EtherCAT userspace API without a physical EtherCAT master or servo. It is designed for Linux and the upstream `libfakeethercat` + RtIPC facility.

The two processes model one virtual joint:

```text
controller
  Tx: control word + target torque
  Rx: status + position + velocity
       |
       | RtIPC via libfakeethercat
       v
plant_sim
  Rx: control word + target torque
  integrates J*qdd + b*qd = tau
  Tx: status + position + velocity
```

The simulator deliberately swaps `EC_DIR_OUTPUT` and `EC_DIR_INPUT`, exactly as the upstream FakeEtherCAT documentation requires for back-to-back process-data emulation.

## Build

Install an IgH userspace library first, then:

```bash
cmake -S . -B build -DETHERCAT_ROOT=/usr/local
cmake --build build -j
```

You may instead pass explicit paths:

```bash
cmake -S . -B build \
  -DECRT_INCLUDE_DIR=/usr/local/include \
  -DETHERCAT_LIBRARY=/usr/local/lib/libethercat.so
```

## Run with libfakeethercat

Set the actual installed fake library path and run the helper:

```bash
export FAKE_EC_SO=/usr/local/lib/libfakeethercat.so.1
bash scripts/run_fake.sh
```

The helper follows the upstream discovery sequence: first create controller output variables, start the simulator, then restart the controller so both sides can discover the opposite-direction RtIPC PDO variables.

A healthy run prints controller and plant state every 500 cycles. Position should move toward the controller's sinusoidal target rather than remain permanently zero.

## Scope

This program validates application-side object setup, PDO registration, process-image offsets, cyclic `receive/process/queue/send`, and fake shared-memory exchange. It does **not** validate NIC timing, Working Counter failures, AL state transitions, Distributed Clocks, a real CiA-402 state machine, or a real drive's scaling.

The vendor/product IDs and PDO objects in this project are synthetic teaching values. Do not copy them into a physical drive configuration.
