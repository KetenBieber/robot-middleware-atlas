# LCM 端到端最小工程

本工程不是上游源码。C++ API、`lcm-gen` 和 CMake helper 已按本地固定提交 `ad0c54cee0ec048ef12357c34349ec1443158864` 核对；真实编译需要安装 LCM 和其 CMake package。它不控制真实机器人，也不承诺 UDP 可靠交付。

```bash
cmake -S examples/lcm/closed_loop -B build/lcm-closed-loop \
  -DCMAKE_PREFIX_PATH=/your/lcm/install
cmake --build build/lcm-closed-loop --parallel
```

接收端只有在 12 秒内收齐 20 条连续序号且数据校验通过时才退出并返回 0。否则返回非零状态；本测试是本地链路的一次性检查，不能据此声称 UDP 可可靠交付。

先在终端 A 运行 `atlas_receiver`，再在终端 B 运行 `atlas_sender`。两者默认使用 `udpm://239.255.76.67:7667?ttl=0`，也可把相同 URL 作为两个进程的首个命令行参数。TTL 0 限制在本机，仍要求网络栈支持 multicast。

发送端以 50 ms 周期发送 20 条三关节状态。接收端最多等待 12 秒，显示业务序号、原始字节长度和间断次数。先完成正常通信，再逐一尝试：① 发送后才启动接收端；② 让双方端口不同；③ 在 callback 中人为睡眠 300 ms，对比订阅队列配额 4 时的新消息丢弃；④ 修改 schema 后尝试解码旧日志，观察 fingerprint。

必须记录发布/接收时间、网络环境和配置，不能把某次成功直接宣称为低延迟或可靠交付。配套阅读：[总入口](../../../content/articles/lcm/overview.md)、[UDP 发送](../../../content/articles/lcm/udpm-publish-protocol.md)、[接收缓存](../../../content/articles/lcm/receive-reassembly.md)、[订阅分发](../../../content/articles/lcm/subscription-dispatch.md)、[类型日志](../../../content/articles/lcm/types-and-eventlog.md)。
