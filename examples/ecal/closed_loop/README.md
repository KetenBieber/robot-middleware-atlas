# eCAL 三进程闭环工程

本目录按本地固定 eCAL 提交 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 的 Core API 编写。它不是上游示例，而是把 discovery、Publisher、Subscriber callback、relay 和最终验收连成一条可运行链。

> 注意：CMake 中的三个 eCAL executable 必须先 `add_executable` 再配置 target 属性；项目页会解释这一构建图。本目录的 CMake 文件由闭环检查同步校验。

数据链：

```text
atlas_ecal_source
  -> /atlas/raw
  -> atlas_ecal_relay
  -> /atlas/processed
  -> atlas_ecal_observer
```

先在一个已经安装 eCAL CMake package 的环境中：

```bash
cmake -S examples/ecal/closed_loop -B build/ecal-closed-loop
cmake --build build/ecal-closed-loop --parallel
```

`atlas_wire_codec_test` 不依赖 eCAL runtime，可单独验证文本 codec。

运行时先启动 observer，再启动 relay，最后启动 source。relay 在发布 raw Subscriber 之前先等待 processed Publisher 发现 observer；source 也先等待 raw Publisher 发现 relay，减少“端点还没有完成 soft-state discovery”对正常路径测试的干扰。

observer 只有在 10 秒内看到 20 条连续 sequence 且 payload 全部为 `processed:raw` 时返回 0。这个结果只能证明这一次业务观测闭环成功，不能证明 transport 可靠、无丢包或具备实时 deadline 保证。
