# Cyber RT 端到端闭环工程

本目录不是 Apollo 上游源码。API、BUILD 宏、Component 注册和 DAG 形状按本地固定提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa` 对照；真实构建必须在相同或兼容的 Apollo Bazel workspace 中完成。

将本目录内容复制或挂载到 Apollo checkout 的：

```text
cyber/examples/atlas_closed_loop/
```

然后在 Apollo 根目录执行：

```bash
source cyber/setup.bash
bazel build //cyber/examples/atlas_closed_loop/...
```

终端 A 启动 Component：

```bash
mainboard -d cyber/examples/atlas_closed_loop/status_transform.dag
```

终端 B 启动严格 observer：

```bash
bazel run //cyber/examples/atlas_closed_loop:status_observer
```

终端 C 最后启动 source：

```bash
bazel run //cyber/examples/atlas_closed_loop:status_source
```

数据链为：

```text
/atlas/status/raw
  -> StatusTransformComponent::Proc
  -> /atlas/status/processed
  -> status_observer
```

source 发送 20 条带序号和源时间戳的 Status。Component 保留序号与时间戳，只把 `text` 改为 `processed:raw`。observer 只有收齐 20 条、序号连续且输出前缀正确时才返回 0。

这个返回值只能证明这一次实验的业务观测满足条件，不能推出 Cyber transport、调度或操作系统具有确定性实时保证。若要复现 backlog，应在 Component::Proc 中人为阻塞，再调整 Reader pending queue；若要研究多输入，继续改成 `Component<M0, M1>` 并明确触发输入与融合语义。

本仓库当前不是 Apollo 构建环境，因此该真实 Cyber 工程只做源码形状与文档闭环检查；当前机器可直接编译运行的机理实验位于 `content/articles/cyber/cpp-implementation-lab.md` 的 `mini_node.cc`。
