# Cyber RT 使用教程：环境、构建与诊断基线

本篇涉及的 Cyber RT 行为统一以 Apollo 固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 为准。

Cyber RT 最稳定的使用方式是在 Apollo 提供的容器或完整源码工作区中开发。它不是一个只需链接单个系统库的轻量 SDK；Bazel targets、protobuf、运行时配置和 `cyber/setup.bash` 共同构成环境。

官方快速开始也将 Apollo Docker 环境作为前提，并通过 Bazel 构建组件。[Apollo Cyber RT 快速开始](https://apollo.baidu.com/docs/apollo/10.x/md_cyber_2docs_2cyber__quick__start__cn.html)

## 环境分层

```text
host OS
  -> Apollo development container
       -> /apollo source workspace
       -> Bazel output: /apollo/bazel-bin/...
       -> cyber/setup.bash
       -> mainboard / cyber_launch / cyber_monitor / cyber_recorder
```

所有终端都应进入相同容器，并在运行前加载环境：

```bash
cd /apollo
source cyber/setup.bash
export GLOG_alsologtostderr=1
```

`setup.bash` 设置运行时查找路径、配置路径与网络变量。一个终端能运行、另一个终端找不到 channel 时，先比较两边是否都加载了同一份脚本。

## 验证源码与工具

```bash
cd /apollo
test -d cyber
which mainboard
which cyber_launch
which cyber_monitor
```

随后构建 Cyber 示例或整个 Apollo 工作区。完整工作区常用：

```bash
cd /apollo
bash apollo.sh build
```

开发单个 target 时优先使用精确 Bazel label，缩短反馈周期：

```bash
bazel build -c opt //cyber/examples/...
```

实际 label 以当前 checkout 的 `BUILD` 文件为准。升级 Apollo 后不要假设旧版本示例路径完全相同。

## 建立终端分工

推荐固定四个终端角色：

| 终端 | 用途 |
|---|---|
| A | 启动 `mainboard` 或 `cyber_launch` |
| B | 运行 writer / 数据源 |
| C | 运行 `cyber_monitor` 或 listener |
| D | 运行 `cyber_recorder`、检查日志与配置 |

这样能区分组件启动失败、没有发布者、订阅回调未执行和工具环境未加载。

## 用 cyber_monitor 检查拓扑与流量

```bash
cyber_monitor
```

只观察一个 channel：

```bash
cyber_monitor -c /apollo/test
```

monitor 能看到 channel 和类型，并以流量变化辅助判断数据是否到达。[官方开发工具文档](https://apollo.baidu.com/docs/apollo/latest/md_cyber_2doxy-docs_2source_2CyberRT__Developer__Tools.html)

若 channel 不出现，按顺序检查：进程是否存活、Node/Writer 是否创建成功、channel 拼写、protobuf 类型是否一致、两端网络配置。若 channel 出现但无频率，再检查 writer 循环和 `cyber::OK()`。

## 记录与回放建立可复现输入

记录所有 channel：

```bash
cyber_recorder record -a -o tutorial.record
```

只记录目标 channel：

```bash
cyber_recorder record -c /apollo/test -o tutorial.record
```

查看与回放：

```bash
cyber_recorder info -f tutorial.record
cyber_recorder play -f tutorial.record
```

将组件测试输入固化为 record，可把“传感器当前有没有数据”与“组件处理是否正确”分离。回放时记录 rate、begin/end 等参数，保证问题可复现。

## 多机通信基线

两台主机必须使用可互达地址，并在各自环境中设置本机 `CYBER_IP`，而不是都保留 `127.0.0.1`：

```bash
export CYBER_IP=192.168.10.6
source cyber/setup.bash
```

先验证路由、防火墙和相同网段，再验证 Cyber topology。官方 FAQ 说明同进程、跨进程和跨主机可分别选择 INTRA、SHM 与 RTPS；不要把 SHM 问题和 RTPS 网络问题混在一次排查中。[Cyber RT FAQ](https://apollo.baidu.com/docs/apollo/latest/md_cyber_2docs_2cyber__faqs.html)

## 最小环境验收

- 两个终端加载同一 `setup.bash`；
- 示例 target 能在 `bazel-bin` 中找到；
- writer 运行后 `cyber_monitor` 能看到目标 channel 与频率；
- recorder 能生成非空文件并成功回放；
- 停止 writer 后监控频率归零，进程能响应 Ctrl+C 并退出。
