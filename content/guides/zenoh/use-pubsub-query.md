# Zenoh 使用教程：Publisher、Subscriber 与 Queryable

Zenoh 用 Key Expression 统一发布订阅和查询。精确 key 表示一个资源，`*` 匹配单层，`**` 匹配多层。命名设计应先于代码，因为表达式宽度直接影响路由与权限范围。

## Key Expression 是路由协议的一部分

| 订阅表达式 | 匹配 | 不匹配 |
|---|---|---|
| `robot/arm/state` | 同一个精确 key | `robot/leg/state` |
| `robot/*/state` | `robot/arm/state` | `robot/a/left/state` |
| `robot/**` | `robot/arm/state`、更深后代 | 其他根 namespace |

Key 不只是字符串标签。路由缓存、Queryable 选择、ACL 和存储查询都依赖相同的相交/包含语义。重命名层级会改变权限与流量范围，应该像修改 wire schema 一样评审。

应用边界先把用户输入解析为合法 `KeyExpr`，不要把任意字符串一路传到发送热路径再处理错误。固定前缀与设备 id 的拼接还要防止空层级、保留字符和意外的 `**` 扩权。

## C++ Publisher

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
#include <chrono>
#include <thread>
#include <zenoh.hxx>

int main() {
  auto config = zenoh::Config::create_default();
  auto session = zenoh::Session::open(std::move(config));
  auto publisher = session.declare_publisher(
      zenoh::KeyExpr("robot/arm/state"));

  for (std::uint64_t seq = 0; ; ++seq) {
    publisher.put(zenoh::Bytes::serialize(
        std::string("seq=") + std::to_string(seq)));
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
}
```

zenoh-cpp 仍在演进，`Bytes`、callback 和 option 的精确名称应以所锁定 tag 的 examples 为准；CMake 和源代码必须锁定相同 release，不要混用 main 头文件与旧 zenoh-c 动态库。

### Payload 的所有权

把字符串序列化成 `Bytes` 是一次所有权边界。同步 API 若只在调用期间借用输入，可以接受 view；一旦后端可能排队异步发送，就必须让队列拥有 bytes 或共享不可变缓冲区。不能把局部 `std::span` 指向的栈内存交给后台任务。

业务协议至少写入 sequence、源时间戳和 schema/version：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
version:u16 | sequence:u64 | source_time_ns:i64 | payload...
```

多字节整数使用规定端序逐字段编码，不要 `memcpy` 一个带 padding 的 C++ struct。Zenoh 负责搬运 payload，不自动定义机器人消息的字段布局。

## Subscriber 与表达式

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
auto subscriber = session.declare_subscriber(
    zenoh::KeyExpr("robot/*/state"),
    [](const zenoh::Sample& sample) {
      std::cout << sample.get_keyexpr().as_string_view() << "\n";
    },
    zenoh::closures::none);
```

保持 subscriber handle 存活。callback 只做解析、指标和有界入队；慢任务不要直接占用接收执行上下文。

`Sample` 参数通常只保证在 callback 调用范围内有效。若 worker 在回调返回后继续处理，应把所需 key、时间和 payload 转成拥有型对象：

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
void on_sample(const zenoh::Sample& sample) {
  OwnedSample owned{
      std::string(sample.get_keyexpr().as_string_view()),
      copy_payload(sample),
      steady_clock_now()};
  if (!queue_.try_push(std::move(owned))) ++application_drops_;
}
```

这里复制 payload 是把后端借用转换为应用所有权。若目标版本提供可移动/共享的拥有型 bytes，可以移动以减少复制，但仍要确认它是否让 SHM loan 或后端 Session 长时间保活。

`robot/*/state` 不匹配任意深度；需要多层时使用 `robot/**`。过宽的 `**` 会增加流量、路由匹配与 ACL 暴露范围。

## Queryable 与 Get

Queryable 不是普通 subscriber。它收到 Query 后可以返回零到多条 Reply：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
server: declare_queryable("robot/arm/config")
  on query:
    query.reply("robot/arm/config", serialized_config)

client: session.get("robot/arm/config")
  while reply receiver open:
    consume reply
```

官方 `z_queryable` 与 `z_get` 示例提供当前版本的准确 API。[Zenoh C examples](https://github.com/eclipse-zenoh/zenoh-c/blob/main/examples/README.md)

调用方必须设置 timeout，并消费到 receiver 完成；服务方异步保存 Query 时会延迟 Final。不要把 Query 对象无限保存在容器中。

### Query 的完整状态机

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Open
  -> Reply(0..N) -> Open
  -> Final        -> Completed
  -> Timeout      -> TimedOut
  -> SessionClose -> Cancelled
```

Reply 是数据，Final 是完成信号，两者不能用“收到一条回复”合并。多个 Queryable 都可能回答同一个 selector，调用方要决定目标范围、合并策略和超时。超时后到达的迟到 Reply 必须关联旧 query id 并被丢弃，不能进入下一次请求。

服务方若要把 Query 交给异步 worker，需要一个有界 pending 表：

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
struct PendingQuery {
  QueryHandle query;                 // 拥有回复能力
  std::chrono::steady_clock::time_point deadline;
};

if (!pending.try_insert(id, PendingQuery{move_query(query), deadline})) {
  reply_busy(query);
}
```

表项在回复、超时或关闭时只能由一个路径取走。安全结构是锁内 `erase/take` 决定唯一负责人，锁外发送 reply 或完成通知，避免网络操作持有 pending map 互斥锁。

### FIFO 与 Ring handler

Zenoh 1.x C++ API 文档区分两类 stream handler：FIFO 满时阻塞，Ring 满时淘汰较旧消息为新消息腾位。[Zenoh C++ 1.0 migration](https://zenoh.io/docs/migration_1.0/c%2B%2B/)

| 数据 | 起点选择 | 仍需补充的机制 |
|---|---|---|
| 高频姿态/状态 | Ring 或 latest | sequence gap、最大 age |
| 不可丢命令 | 有界 FIFO | 超时、busy、应用 ACK |
| Query Reply | 有界 FIFO | Final、整体 timeout |
| 审计事件 | 持久通道 | 磁盘上限与恢复 |

FIFO 的“阻塞”不是可靠交付的同义词；它可能把慢消费者反压到接收/路由任务并放大尾延迟。Ring 的“保留最新”也不保证只剩一个样本，容量与淘汰计数仍要明确。

## Pub/Sub 与 Query 的组合

常见设备模式：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
robot/arm/state         高频实时发布
robot/arm/config        Queryable 返回当前配置
robot/arm/cmd           命令发布
robot/arm/alive         liveliness token
```

新消费者先查询当前配置或最新状态，再订阅增量更新。Storage 在概念上就是 Subscriber 加 Queryable：收到 publication 时保存，收到 Query 时返回匹配值。[Zenoh abstractions](https://zenoh.io/docs/manual/abstractions/)

## 过载语义

接收 handler 可选择 FIFO 或 ring。FIFO 满时可能把压力传回分发路径；ring 保留有限最新历史。状态流优先 ring/最新值，命令和审计事件需要可靠且可观察的队列或持久化机制。

同时区分 handler queue、CongestionControl 和 Reliability；修改其中一项不会自动改变另外两项。

三者位于不同层：handler queue 管应用交付积压，CongestionControl 决定发送路径拥塞时等待还是丢弃，Reliability 影响链路交付取向。再加上业务 ACK 才能表达“机器人已经执行命令”。调优时必须一次改变一个层次并记录 drop 来源。

## C++ 与 Rust 的同一生命周期

**图示身份：概念、状态或调用链示意，不是源码。**
```text
C++ Subscriber RAII handle
  -> C opaque owned handle
  -> Rust Subscriber entity
  -> Arc<SessionInner>
```

C++ 句柄析构撤销自己的实体，不应隐式关闭共享 Session。Rust `Arc` 让实体在 callback/任务间共享 Session 寿命，但 close 状态仍需单独检查；引用计数存在不等于协议仍允许创建新工作。

## 验收

- 精确 subscriber 只收到目标 key；
- `*` 与 `**` 用边界样例验证；
- Queryable 零回复、多回复和超时均能结束；
- callback 过载时 drop/block 行为可见；
- sequence gap、payload encoding 与端到端 age 有指标；
- 释放实体后 matching/liveliness 能收敛。
