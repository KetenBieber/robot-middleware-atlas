# Zenoh 使用教程：Router 配置、访问控制与故障排查

生产部署要把自动发现的实验拓扑变成显式、可审计的 Router 与 Client 配置。

## 最小显式拓扑

**图示身份：概念、状态或调用链示意，不是源码。**
```text
robot clients -> tcp/router-host:7447 <- edge/cloud clients
```

Router JSON5 配置固定监听 endpoint；Client 配置关闭不需要的 scouting，并列出 connect endpoints。启动日志应记录 zid、mode、监听和连接结果。

Zenoh 1.x 的配置意图可以写成下面这样；字段以部署版本附带的默认配置和 schema 为最终依据：

**示例身份：教学配置或命令；须按目标版本核对。**
```json5
// router.json5
{
  mode: "router",
  listen: { endpoints: ["tcp/0.0.0.0:7447"] },
}
```

**示例身份：教学配置或命令；须按目标版本核对。**
```json5
// robot-client.json5
{
  mode: "client",
  connect: { endpoints: ["tcp/router-host:7447"] },
  scouting: { multicast: { enabled: false } },
}
```

关闭 multicast scouting 后，显式 endpoint 就成为启动依赖。客户端应区分“Session 对象已创建”和“已连接预期 Router”；如果业务不能在离线状态运行，就在进入 Ready 前等待连接条件并设置总超时，而不是立即开始产生无处交付的命令。

## 拓扑状态机

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Starting -> SessionOpen -> Connecting -> Matched -> Ready
                              |             |
                              +-- timeout --+-> Degraded
Ready -- transport lost --> Reconnecting -- success --> Rematching --> Ready
Reconnecting -- deadline --> Offline/SafeStop
```

transport 重连成功不表示声明已经重新匹配，更不表示收到的数据是新鲜的。恢复完成条件至少包含：目标 router/peer 可达、关键 Publisher/Subscriber/Queryable 匹配、第一条有效样本通过 schema/sequence 检查。

## Key Expression 命名规范

推荐按组织/机器人/子系统/数据分类：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
factory/line-a/robot-07/arm/state
factory/line-a/robot-07/arm/cmd
factory/line-a/robot-07/arm/config
```

禁止把不稳定 IP 作为 key；租户和设备边界应出现在固定层级，方便 ACL 用 `factory/line-a/**` 限制。

命名表应同时给出所有者与语义：

| Key | 写入方 | 读取方 | 数据语义 |
|---|---|---|---|
| `.../arm/state` | arm driver | estimator/UI | 可丢旧状态 |
| `.../arm/cmd` | controller | arm driver | 需要应用确认 |
| `.../arm/config` | config owner | Query clients | 多回复/Final |
| `.../arm/alive` | arm process | supervisor | liveliness |

仅凭名字后缀不能获得可靠性；表格必须继续声明 payload schema、频率、最大尺寸、拥塞策略、权限和数据年龄上限。

## ACL 同时限制消息种类与方向

Zenoh ACL 可按 key expression、消息类型和 ingress/egress 建规则。规则中的消息包括 put/delete、subscriber 声明、query/reply、queryable 声明与 liveliness。[官方 Access Control](https://zenoh.io/docs/manual/access-control/)

安全配置采用默认拒绝，只开放确切 namespace 和必要动作。只允许 `put` 却忘记 `declare_publisher/subscriber` 相关控制消息，可能表现为连接存在但数据不可用；调试时记录命中的 rule 与 flow。

官方 1.x ACL 模型由 rules、subjects、policies 与 `default_permission` 组成，并规定冲突时显式 deny 高于显式 allow，显式 allow 高于默认值。[Zenoh Access Control](https://zenoh.io/docs/manual/access-control/)

分析一条数据路径时按两个方向展开：Publisher 产生的 put 在发送端属于 egress，在 Router 上先 ingress 后 egress，在 Subscriber 端属于 ingress。Router 同时匹配两个方向的规则，因此看似对称的 allow/deny 组合可能产生不同于端节点的结果。

ACL 不是身份认证本身。subject 可以依据接口、证书 common name 或用户名等身份属性匹配；如果对端没有可信认证，仅按来源接口授权的强度有限。凭据和证书应通过独立秘密管理注入，不要写入随网站或普通配置仓库分发的示例文件。

## Shared Memory 使用判断

同主机大 payload 可评估 SHM。双方配置都必须支持并在 Session 建立时完成 probing，否则可能回退网络路径。监控不能只看功能成功，还要确认实际 transport、fallback 次数、复制量和 buffer pool 使用率。

共享内存收益近似为避免一次或多次 `O(S)` payload copy，但会增加 buffer pool、loan 生命周期和崩溃回收状态。使用前测量：消息尺寸 `S`、频率、普通路径复制带宽、池容量、最大在途 loan 和慢消费者持有时间。

池耗尽策略必须显式：阻塞发布、回退普通 bytes、丢弃或报错。状态图像可能选择回退/丢弃，控制命令通常不能静默丢失。只有功能互通而没有 transport 观测，无法证明 SHM 实际启用。

## 故障定位四层

| 层 | 观察 |
|---|---|
| Session/transport | client 是否连接 router，是否重连 |
| 声明/matching | publisher、subscriber、queryable 是否匹配 |
| routing/ACL | keyexpr、方向、消息类型是否被过滤 |
| application | callback queue、encoding、超时与 Final |

Query 收到部分 Reply 后超时，通常要继续检查某个路由方向是否缺 Final；Pub/Sub 无数据则先区分未匹配、ACL 丢弃与消费者过载。

## 性能账本

| 路径 | 主要成本 | 放大因素 | 应观测 |
|---|---|---|---|
| keyexpr 声明 | 表达式解析与路由更新 | 通配符数量、拓扑变化 | 匹配收敛时间 |
| publication | 编码、路由 cache、目标发送 | payload、目标数、拥塞 | put 延迟、drop 原因 |
| subscriber | 解码、handler queue、callback | worker 服务率 | queue depth、oldest age |
| query | fan-out、回复合并、Final | Queryable 数、timeout | pending 数、首/末回复延迟 |
| SHM | pool 取得与 loan | 在途样本、慢读者 | pool 水位、fallback |

稳定拓扑吞吐和拓扑抖动吞吐要分开测试。频繁声明/撤销和大量 `**` 会触发匹配与路由缓存更新；只测长时间稳定 publisher 会漏掉这类控制面尖峰。

## 关闭顺序

停止业务生产，释放 Publisher/Subscriber/Queryable，取消并等待应用 worker，再显式关闭 Session。Router 关闭前先停止接入新 client，等待关键 client 迁移或重连。

全局静态 Session 不适合依赖进程 `atexit` 析构；将其放在 main 所有的服务对象中，明确调用 close。

一个服务对象可以把依赖方向固定下来：

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
class ZenohService {
public:
  void stop() noexcept {
    if (stopping_.exchange(true)) return;
    producer_.request_stop();
    producer_.join();

    queryable_.reset();               // 阻止新 Query
    subscriber_.reset();              // 阻止新 Sample callback
    pending_queries_.cancel_all();
    worker_queue_.close();
    workers_.join();

    publisher_.reset();
    session_.close();                  // 目标版本若返回结果，应记录失败
  }

  ~ZenohService() { stop(); }
private:
  // Session 先构造、最后析构；实体在声明顺序上位于其后。
};
```

这是结构代码，具体句柄是否提供 `reset/close` 以及返回类型以锁定的 zenoh-cpp tag 为准。关键不变量是：撤销接收实体后才销毁 callback 捕获的队列；取消 pending Query 后才关闭 Session；关闭幂等。

析构函数不适合无限等待网络。生产接口应给显式 `close(deadline)`，超时后记录未完成实体并进入受控强制取消；析构只做不抛异常的最后清理。

## 故障演练

1. Router 未启动时启动 Client，记录 Session 打开、连接超时与业务 Ready 的差异；
2. 满负载断开 Router，验证队列与 retry 水位有上界；
3. 恢复 Router，测 transport 重连、重新匹配和第一条新鲜样本的三个时间点；
4. 拒绝 `declare_subscriber`、`put`、`query` 和 `reply`，分别验证 ACL 诊断；
5. 让一个 Queryable 永不释放 Query，验证调用方 timeout 和 pending 清理；
6. 耗尽 SHM pool，验证阻塞、fallback 或 drop 与配置一致；
7. 在 callback 执行时请求 close，验证没有 use-after-free 或重复完成。

## 运行验收

- router 重启后 client 在目标时间内恢复匹配；
- 网络分区期间 queue 与 retry 不无界增长；
- ACL 拒绝能定位到 key、message type 与 flow；
- SHM 不可用时 fallback 行为符合部署策略；
- Query timeout 后 pending 状态归零，迟到 Reply 不污染新请求；
- close 返回后无活动 task、callback 和 transport handle。
