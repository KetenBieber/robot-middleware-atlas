# Zenoh 功能与组件地图：统一数据空间、路由与查询

凌晨的仓库里，移动底盘每 20 ms 发布一次位姿，机械臂控制器订阅关节状态，监控进程偶尔查询某个设备的当前诊断值；边缘网关断线重连后，还要把同一批数据转发到远端。若把这些功能从零写成一个 `topic -> socket` 字典，第一版很容易只覆盖精确 topic：控制器能收到 `robot/arm/state`，却不能声明 `robot/**` 这类集合；查询需要另建一套请求地址和超时表；网关收到消息后又无法判断该消息从哪个邻居进入、应当避开哪个出口。拓扑变化时，旧转发表仍可能把新消息送往已经断开的 Face，读者看到的就是诊断值停更、同一状态重复转发或故障切换后的首帧延迟。

Zenoh 把发布订阅、查询和存储能力放到共同的 Key Expression 命名空间中，但统一名称并不会自动解决路由和生命周期问题。下面从一次位姿声明、一次数据转发和一次多目标查询依次走过 API、Resource、Face 与 Route；再回到异步任务和关闭，说明这些对象分别保存什么状态。这里的故障时间线是用来推导模块职责的教学场景；固定提交中实际执行的调用点会在相应章节给出。

## 使用场景与非目标

Zenoh 适合机器人、边缘计算和跨网络数据空间：同一 key expression 支持发布订阅、查询和存储，Router/Peer/Client 模式可适应设备、局域网与广域拓扑，声明和 route cache 又为高频数据路径提供复用。

它不是简单 topic map，也不会让所有查询天然只有一个回复。Key expression 是集合表达式，路由依赖来源 Face 与拓扑，Query 可以扇出、多回复并以 Final 收敛。把它强行理解为 DDS topic 或 HTTP request 会遗漏核心语义。

## 功能域

| 功能 | 核心组件 | 关键问题 |
|---|---|---|
| 应用会话 | Session、Publisher、Subscriber、Queryable | 实体注册与生命周期 |
| 数据命名 | KeyExpr、WireExpr、Resource | 通配关系、字符串压缩、共享前缀 |
| 路由边界 | Face、Mux、DeMux | 每连接映射、ingress/egress policy |
| 路由计算 | Tables、Hat、Route cache | 拓扑变化、缓存失效、目的地去重 |
| 查询汇聚 | QueryState、pending query、Final | 多回复、扇出/汇聚、timeout |
| 任务与传输 | Runtime、TaskController、Transport | 模式编排、取消和关闭 |

## 从机器人与边缘需求反推组件

| 需求 | 首先进入的组件 | 关键限制 |
|---|---|---|
| 跨设备、边缘与云统一命名数据 | KeyExpr、Session、Router/Peer/Client | 通配匹配与拓扑策略进入路由成本 |
| 高频状态发布订阅 | Publisher、Resource、Route cache | 声明稳定时命中快，抖动时频繁失效 |
| 查询多个服务或存储 | Queryable、pending query、Final | 零到多回复，不等价于单 RPC |
| 存储历史并按 key 查询 | storage/query path | 一致性、选择器和结果合并仍需定义 |
| 在连接内压缩重复 key 前缀 | WireExpr、Face mapping | scope 只能在所属 Face 解释 |
| 按来源实施路由与访问策略 | Face、ingress/egress policy、Hat | cache key 必须包含来源上下文 |
| 有界地处理网络过载 | async channel、CongestionControl | Block/Drop 对状态与命令含义不同 |
| 可控关闭整个异步运行时 | TaskController、Session close、Transport | Drop 不可 await，显式 close 才能确认 |

Zenoh 的能力来自统一数据空间，但也意味着“key 名字”同时进入匹配、路由、查询和安全策略。调试时不能只看 publisher/subscriber 是否声明成功，还要看 ingress Face、映射恢复、Resource 命中、Route 版本和 outbound queue。

## 固定源码入口

本系列固定源码为 [`9fcd9cb5`](https://github.com/eclipse-zenoh/zenoh/tree/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5)：

| 层 | 固定提交中的路径与符号 | 首先追踪的动作 |
|---|---|---|
| Session API | [`Session::new` 与 `Session::init`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L864-L904) | Runtime、SessionInner、local Face 的建立顺序 |
| Builder | [`OpenBuilder::wait`/`IntoFuture`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/session.rs#L135-L166) | 配置如何在 wait/await 处真正提交 |
| Publisher | [`Session::declare_publisher_inner`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L1581-L1649)；[`PublisherBuilder::wait`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/publisher.rs#L476-L503)；[`PublicationBuilder::wait`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/publisher.rs#L243-L269) | entity ID、key、声明与样本提交 |
| Data routing | [`route_data`/`get_data_route`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/pubsub.rs#L198-L281) | Face mapping、ingress filter、route cache 和目的地 |
| Queryable/Query | [`Session::handle_query`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L2969-L3043)；[`QueryInner::drop`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/queryable.rs#L116-L160) | 回调、Reply 和 Final 屏障 |
| Resource/cache | [`Resource` 与 `Routes<T>`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/resource.rs#L225-L319)；[`make_resource`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/resource.rs#L586-L647) | 共享前缀、相交关系与版本失效 |
| Query routing | [`Face::route_query`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/queries.rs#L202-L370)；[`QueryCleanup`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/queries.rs#L437-L523) | fan-out、pending、timeout 和 Final |
| Task close | [`TaskController`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/commons/zenoh-task/src/lib.rs#L30-L146)；[`Runtime::close_inner`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/mod.rs#L1307-L1325) | 任务归属、取消、等待与底层资源关闭 |

源码阅读要从 API 穿过 Session primitives 到 routing tables，再沿 Face/Mux 到 transport。比如 `Publisher::put` 最终由 Face 进入 [`route_data`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/face.rs#L671-L682)：函数先从来源 Face 的 mapping 还原 WireExpr、检查 ingress filter，再取 route 并逐个检查目的地；因此只读 Builder 会看不到来源上下文，只读 dispatcher 又会失去实体声明与撤销语义。后续章节沿这条真实路径逐步展开。

## 端到端路径

```text
Session::put/get
  -> Primitives / local Face
  -> WireExpr 还原 RoutingExpr
  -> Resource tree 定位与 Route cache
  -> destination Faces
  -> Mux / Transport
```

Query 在这条路径上额外建立 qid 映射、每方向 pending entry 和 Final 屏障。

### 声明路径与数据路径

```text
声明：
PublisherBuilder await
  -> Session 分配 entity ID
  -> local primitives declare
  -> Resource/Face context 更新
  -> 失效受影响 Route
  -> 返回 Publisher handle

数据：
Publisher put
  -> key/WireExpr 解析
  -> Resource + source context
  -> 命中不可变 Route
  -> 遍历 destination Faces
  -> Mux/Transport 有界发送
```

声明是控制面，可以承担集合相交与缓存失效；put 是数据面，应复用结果。若应用每条消息都临时声明/撤销 Publisher，就把低频成本搬进热路径，还会制造全局失效抖动。

### Query 比 Pub/Sub 多出的状态

```text
get(selector)
  -> 分配本地 qid / pending state
  -> route_query 扇出到 N 个方向
  -> 每方向 qid 映射
  -> Reply* 零到多次
  -> 每方向 Final / timeout / cancel
  -> 唯一完成者唤醒调用方并清 pending
```

查询的空间和关闭成本与未完成 fan-out 数相关。一个分支不发 Final 时必须由 timeout 收敛；迟到 Reply 要识别已完成 qid 并丢弃，不能重新创建状态。

### 固定源码：从来源 Face 把 WireExpr 还原，再重用 Route

如果只把路由写成 `HashMap<KeyExpr, Vec<Face>>`，有两个问题立刻暴露：收到的 wire scope 只在当前连接有效，且同一 key 从不同来源进入时，出站过滤和避免回环的答案可能不同。Zenoh 数据分发首先用当前来源 `FaceState` 解释 WireExpr，再检查 ingress policy，并把完整 RoutingExpr 交给 Route 计算。固定提交中的入口代码是：

~~~rust
pub fn route_data(
    tables_ref: &Arc<TablesLock>,
    src_face: &FaceState,
    msg: &mut Push,
    reliability: Reliability,
    consume: bool,
) {
    let rtables = zread!(tables_ref.tables);
    let tables = &*rtables;
    let Some(prefix) =
        rtables
            .data
            .get_mapping(src_face, &msg.wire_expr.scope, msg.wire_expr.mapping)
    else {
        tracing::error!(
            "{} Route data with unknown scope {}!",
            src_face,
            msg.wire_expr.scope
        );
        return;
    };

    tracing::trace!(
        "{} Route data for res {}{}",
        src_face,
        prefix.expr(),
        msg.wire_expr.suffix.as_ref()
    );

    let expr = RoutingExpr::new(prefix, msg.wire_expr.suffix.as_ref());

    #[cfg(feature = "stats")]
    let payload_observer = super::stats::PayloadObserver::new(msg, Some(&expr), tables);
    #[cfg(feature = "stats")]
    payload_observer.observe_payload(zenoh_stats::Rx, src_face, msg);

    if !tables.ingress_filter(src_face) {
        return;
    }
~~~

`get_mapping(src_face, scope, mapping)` 成功才拿到这条连接已声明过的资源前缀；未知 scope 直接记录错误并返回，不应把任意整数当全局 Resource ID。`RoutingExpr` 将共享前缀与本次消息 suffix 组合成待路由表达式；接下来执行 `ingress_filter(src_face)`，这一步之前不应按普通全局 key 查缓存，否则同样的字符串可能绕过来源特定的准入规则。

下一层的缓存命中同样离不开来源上下文。固定提交的 `get_data_route` 实际按路由 region 汇集候选出口，并用目标 Face ID 去重：

~~~rust
fn get_data_route(
    tables: &Tables,
    src_face: &FaceState,
    expr: &RoutingExpr,
    node_id: NodeId,
) -> Arc<Route> {
    let compute_route = || {
        let mut builder = RouteBuilder::<Direction>::new();

        for (region, _) in tables.hats.iter() {
            let route = get_hat_data_route(tables, src_face, expr, node_id, &region);

            for dir in route.iter() {
                builder.insert(dir.dst_face.id, || dir.clone());
            }
        }
        Arc::new(builder.build())
    };
    let node_id = tables.hats[src_face.region].map_routing_context(&tables.data, src_face, node_id);
    match expr
        .resource()
        .as_ref()
        .and_then(|res| res.ctx.as_ref())
        .map(|ctx| &ctx.data_routes)
    {
        Some(data_routes) => get_or_set_route(
            data_routes,
            tables.data.routes_version,
            &src_face.region,
            node_id,
            compute_route,
        ),
        None => compute_route(),
    }
}
~~~

真正的缓存入口是 `get_or_set_route(data_routes, routes_version, src_face.region, node_id, compute_route)`：键不只是 key-expression，还含 region 与映射后的路由上下文，并且必须符合当前拓扑 `routes_version`。Resource 有可用 context 时才走缓存；临时表达式未定位到带 context 的 Resource 时仍可直接计算。这样在稳定拓扑下避免高频逐帧扫描所有声明，拓扑变动又能通过版本让旧 route 不再命中。

图上的“缓存命中”也不能理解为 transport 写出成功。获取 Route 以后还要遍历方向、检查 egress policy、按目标 Face 重写 WireExpr 并投递到 Mux/Transport；慢链路会产生另外一层有界队列和拥塞行为。完整的逐目标分支放在[Publisher 与数据路由](publisher-routing.md)，此处先固定两个边界：**来源 Face 决定消息怎样被解释，Route 版本决定旧的转发答案能不能继续使用。**
## 核心对象与所有权

```text
Runtime
  |-- transport manager / task controller
  `-- router tables

Session --Arc--> SessionInner
  |-- declared Publisher / Subscriber / Queryable entities
  `-- local Face -> Primitives

Tables
  |-- Resource tree --Weak matches--> related Resources
  |-- Face registry / per-Face mappings
  `-- versioned Arc<Route> caches

Transport <-> Mux/DeMux <-> Face <-> routing Tables
```

`Arc` 表示跨异步任务共享寿命，`Weak` 表示匹配/回指但不阻止回收，不可变 `Arc<Route>` 表示可安全共享的路由快照。引用计数只解决内存寿命；Session close 仍需撤销实体、停止任务和关闭 transport。

### 所有权与关闭约束

| 对象 | 主要 owner | 非拥有/共享关系 | 关闭要求 |
|---|---|---|---|
| Runtime | 进程/打开路径 | Session、transport tasks 共享 | 阻止新工作，取消并等待全部任务 |
| SessionInner | Session/实体通过 Arc 共享 | 后台任务宜持 Weak 或受 TaskController 管理 | 先撤销实体，再断 transport |
| Publisher/Subscriber/Queryable state | Session entity table | 应用 RAII handle 引用 ID/Session | Drop/close 幂等 undeclare |
| Resource | Tables/tree | matches 使用 Weak，Route snapshot 引用结果 | 删除时不能被强环保活 |
| Route | cache entry/Arc snapshot | put/query 任务临时共享 | 发布后不可变，版本失效后不再命中 |
| Face | routing tables/transport relation | mapping、policy 与 pending state | 断开时撤销声明、qid 和 mapping |
| Query pending | 来源与出站 map | Reply/Final/timeout 竞争 | 只有一个最终清理者 |

最容易形成逻辑环的是“Session 持 task handle，task capture 强 Arc<SessionInner>”。即使内存安全，最后一个外部 Session drop 后任务仍让运行时活着。应由 TaskController 明确拥有并取消任务，或让 task 捕获 Weak 并在 upgrade 失败时退出。

## KeyExpr、WireExpr 与 Resource 的分工

KeyExpr 表达数据集合，例如 `robot/*/state` 或 `robot/**`；Resource tree 按 `/` 共享前缀并保存声明、匹配边和路由上下文；WireExpr 在连接范围内用 scope/id 压缩重复前缀。

收到 WireExpr 后必须借助 ingress Face 的 mapping 恢复完整 RoutingExpr。映射是每 Face 状态，不能拿 A 连接的 scope 去解释 B 连接数据。Face 关闭时还要撤销 mapping 与声明并失效相关 route。

通配符让一次声明覆盖大量资源，但会增加 key-expression 相交判断、matches 图边数和缓存失效扇出。性能评估必须改变通配符密度，而不是只压测固定精确 key。

### 三种名称不能混用

- KeyExpr 是应用语义集合，可含 `*`/`**`；
- Resource 是路由表中共享前缀和上下文的节点，不等于每个 KeyExpr 都有独立字符串对象；
- WireExpr 是某条 Face 上的压缩表示，scope ID 只在连接局部有效。

安全边界要求先用 ingress Face mapping 还原 WireExpr，再做权限和路由。若先把未验证 scope 当全局 Resource ID，攻击者或协议错误可能引用另一连接的资源。

Resource tree 节点数 `R` 不等于声明数。共享前缀能减少重复字符串，但通配相交会增加 matches 边 `E_m`；内存预算要同时包含节点、边、每 Face context 和 Route cache。

## 声明期与数据期的成本交换

声明 Publisher/Subscriber/Queryable 时，系统更新 Resource、Face context 和路由表，并清除受影响缓存。数据到达时则尽量只定位 Resource、命中 `Arc<Route>`、遍历目的 Face 并转发。

这是典型的 read-optimized 设计：低频控制面承担匹配和失效，高频数据面复用结果。若声明频繁抖动，缓存重算和锁竞争会变成主成本；稳定拓扑下，命中路径接近常数查找加目的地遍历。

### 线程/任务与锁矩阵

| 路径 | 执行上下文 | 需要共享的状态 | 不应跨越的等待 |
|---|---|---|---|
| Session declare/undeclare | 应用 async task | entity table、Resource、声明状态 | 网络 send await 不持 table 写锁 |
| put 路由 | 应用/内部 task | Resource 与 Route snapshot | outbound channel/transport await 在锁外 |
| transport receive | transport task | Face mapping、routing tables | 用户 callback 与长路由计算 |
| route cache miss | routing worker/task | Tables/Hat/version | 昂贵计算不占全局写锁 |
| Query Reply/Final | 多个入站 task | pending map、完成状态 | waiter 通知与外部发送在锁外 |
| Session close | 管理 task | entities、TaskController、transport | 等待任务时不持它们退出所需的锁 |

Rust async 锁 guard 若跨 `.await`，不仅延长临界区，还可能使 Future 不满足 Send 或形成关闭死锁。正确结构是锁内取不可变计划/移动待处理资源，drop guard 后 await，再以 generation 检查是否仍可提交。

## Face 是策略与信任边界

Face 代表路由器看到的一侧连接关系，保存远端角色、资源映射和 Primitives。Ingress policy 可拒绝不允许的声明或数据，egress policy 决定哪些目的地可见，路由还要避免把数据无意义地送回来源。

同一 key 从不同来源进入可能得到不同 Route，因此缓存 key 需要包含来源 region/node，而不只是 Resource。安全、拓扑防环和角色策略都是“来源相关”的原因。

策略检查不能只发生在首次声明。拓扑、身份或配置变化后，旧 Route snapshot 必须因 version/policy epoch 失效；否则数据面可能继续使用变更前允许的目的地。版本号因此既是性能机制，也是安全更新边界。


## Region 是 Face 创建时确定的路由身份

Zenoh 的 `Region` 不应理解成运行中可以随意改写的标签。固定提交里，unicast transport 建立时，Runtime 先根据本地模式、远端角色、gateway 配置和握手阶段得到的 remote bound 计算 `Region` 与 `Bound`：

~~~text
TransportPeer + gateway config + remote bound
                  |
                  v
         compute_region_of()
             |          |
             v          v
           Region     Bound
~~~

随后 `Gateway::new_transport_unicast()` 把这两个结果交给 `FaceStateBuilder::new()`，写入新建 Face：

~~~rust
let builder = FaceStateBuilder::new(
    fid,
    zid,
    region,
    remote_bound,
    mux.clone(),
    tables.hats.map_ref(|hat| hat.new_face()),
);
~~~

因此，一个 Face 的 region 是 transport/Face 创建事务的一部分，而不是数据转发时每条消息重新计算的动态属性。数据路径才能直接使用 `tables.hats[src_face.region]` 选择对应 HAT。

`compute_region_of()` 同时考虑 local `WhatAmI`、remote `WhatAmI`、gateway south preset/custom subregion，以及双方的 `Bound`。自定义 gateway 还可以按 zid、interface、mode、region name 把远端放入具体的 `Region::South { id, mode }`。所以 Region 是 routing domain/HAT ownership 维度，不是物理网卡编号或机器人业务分组。

### 已建立 Face 不做 in-place region migration

固定生产源码里没有常规运行时的 `face.region = new_region` 路径。这个选择是必要的，因为 Face 已经挂着大量依赖 region 的状态：

~~~text
Face
├── region
├── HAT face contexts
├── resource mappings
├── remote subscribers/queryables
├── interests
└── pending queries
~~~

如果只改 region 字段，而不迁移这些状态，旧 HAT 与新 HAT 会同时持有不一致的声明、mapping 和 route ownership。

所以固定实现采取更清晰的生命周期：

~~~text
transport / Face creation
        |
 compute Region
        |
 register Face in owner HAT
        |
       ...
        |
 transport close
        |
 unregister old Face completely
        |
 optional reconnect
        |
 create a new Face
        |
 compute Region again
~~~

如果 gateway 配置在两次连接之间改变，重连产生的新 Face 会重新经过 `compute_region_of()`；旧 Face 不做半状态迁移。

### routing topology 变化不等于 Face region 变化

另一类变化是 router link-state 或 link weight 改变。此时 Face 仍属于原 region，但该 region 的 HAT 内部 topology tree 会重算：

~~~text
OAM_LINKSTATE / link-weight update
          |
          v
    compute_trees()
          |
  +-------+-------+
  v       v       v
pubsub  query    token
 tree    tree     tree
 change  change   change
          |
          v
 disable_all_routes()
          |
 routes_version changes
~~~

固定源码的 `do_compute_trees()` 在更新 pub/sub、query 和 token tree 后统一调用 `disable_all_routes()`。因此应区分：

~~~text
Face.region
  = 连接属于哪个 routing domain / HAT

routing tree
  = 该 domain 当前应该经过哪个 successor
~~~

前者主要在 Face 生命周期边界确定；后者可以随 link-state 动态重算。

### RegionMap 把每个 routing domain 变成独立 HAT

Gateway 初始化时会根据配置预先建立 North、Local 和各类 South/custom subregion，并为每个 region 创建对应 HAT。运行时不是一套全局路由算法，而更接近：

~~~text
RegionMap<Hat>
   ├── North
   ├── South(Client)
   ├── South(Peer)
   ├── South(Router)
   ├── custom South #N
   └── Local
~~~

Face 创建时 `partition_mut(&region)` 明确区分 owner HAT 与其他 HAT，使跨 region 声明传播和注销有清晰的所有权边界。

由此可以得到四条不变量：Face 与 Region 一起创建；HAT ownership 由 `Face.region` 决定；拓扑树变化通过 route recompute + version invalidation 生效；重新分类通过重建 Face，而不是修改活跃 Face 的 region 标签。

## Route cache 与失效

局部声明变化可沿 Resource 的 `matches` 精确清除相关缓存；拓扑/Face 大变化则递增全局 version。旧缓存下一次访问发现版本不匹配，再惰性重算。全局失效动作接近 `O(1)`，代价是变化后的首批请求出现重算抖动。

cache miss 采用读 miss、锁外计算、写锁 double-check。允许两个线程偶尔重复计算，避免把昂贵 Hat 路由算法放进长写锁。Route 发布后按不可变对象共享，更新使用新快照而不是原地修改目的列表。

double-check 的关键不是“再看一次 map”这句代码，而是比较同一 cache key 与 generation。线程 A 锁外计算期间拓扑可能变化；即使 cache 仍为空，A 的结果也可能基于旧 version，不能直接发布。计划对象应记录输入 epoch，提交时不匹配就丢弃或重算。

## Pub/Sub 与 Query 的不同状态

Pub/Sub 数据通常按 Route 扇出后结束，拥塞策略决定等待或丢弃。Query 还需为每个出站方向分配/映射 qid，保存 pending 状态，汇聚零到多个 Reply，并在所有分支 Final、超时或取消后恰好一次完成。

Query clone 若延长了完成 guard 寿命，Final 会被推迟。超时只表示请求方不再等待，不一定让远端计算立即停止；取消、迟到回复和 Session close 都要与正常 Final 竞争唯一清理权。

## 异步背压与关闭

任务间有界 channel 把内存上界固定为 `O(capacity × item_size)`，满载时必须选择 block、drop new、drop old 或 error。状态流与命令/查询不能盲目共用同一策略。

Rust lock 不能跨未知时长 `.await`：锁内建立不可变发送计划，锁外等待队列/网络，再用版本检查决定结果是否仍有效。关闭时先阻止新声明和发送，再取消任务、关闭 channel/transport、清 pending query，最后回收 Resource 与 Runtime。

有界 channel 的容量只给出空间上界，不给出时间上界。Block 可能让 producer await 任意久；Drop 维持调用延迟却损失数据；DropOld 需要可替换队列并明确序号；Error 把决策推给应用。状态、命令、query reply 和路由控制消息通常需要不同队列或不同策略，不能共享一个“默认拥塞模式”。

## 章节顺序

《阅读基础》先解释 Rust 所有权与 KeyExpr；《Session 启动链》建立运行时；《Publisher 数据路径》讲声明与路由；《Query 生命周期》讲扇出和完成；《Resource Tree》解释性能核心；《异步背压与关闭》收束任务。

把章节转成七个连续源码任务：

1. 从 OpenBuilder 的 wait/await 进入 `Session::init`，画出 Runtime、SessionInner 和 local Face；
2. 从 declare Publisher 追踪 entity ID、KeyExpr、primitives declaration 与失败回滚；
3. 从 put 进入 Resource lookup、cache key、Hat 计算和 destination Face；
4. 从 WireExpr receive 反向确认 ingress Face mapping 和 policy；
5. 从 get 追踪 qid fan-out、Reply、Final、timeout 与唯一完成者；
6. 人为改变声明/Face，验证局部与全局 route cache 如何失效；
7. 从 Session close 追踪实体撤销、TaskController、channel、transport 与 Runtime。

每一步都记录 Arc/Weak 的所有权意义、锁 guard 生命周期、await 边界和错误终点。只记 Rust 类型名无法解释协议状态。

## 优秀设计与代价

不可变 `Arc<Route>` 让读路径便宜，版本化失效让全局拓扑变化接近 O(1)；代价是首次访问重算和旧快照延迟释放。RAII Final 简化完成计数，但泄漏 Query clone 会延迟请求结束。通配表达式强大，却会放大 match graph 和失效扇出。

统一数据空间减少 Pub/Sub、Query 和 Storage 的命名割裂，却使路由器承担更丰富的集合匹配与状态。Rust 所有权消除大量 UAF，但不会自动避免死锁、无界队列、协议状态错误和关闭期间的迟到任务。

### 模式与工程取舍

- Builder：配置聚合后一次提交；避免半配置，但最终 await 仍需事务回滚；
- Shared Immutable Snapshot：`Arc<Route>` 让热路径锁外扇出；代价是旧版本延迟释放；
- Weak Graph：matches/回指不拥有 Resource；避免环，但使用前必须 upgrade 并处理失败；
- Cache Aside + Epoch：miss 时计算、版本变化惰性失效；更新便宜，但首个请求承担抖动；
- Actor/Pipeline：async channel 分隔任务；隔离执行，但背压和关闭传播必须显式；
- RAII Completion Guard：Query 分支 Drop 贡献 Final；减少遗漏，但 clone 泄漏会推迟收敛；
- Policy Boundary：Face 承载 ingress/egress 决策；来源相关路由更准确，但 cache 维度增加。

## 可迁移设计

在 C++ 中可用 `shared_ptr<const Route>`、`weak_ptr<Resource>`、版本 epoch、显式 pending map 和 stop token 复刻。应复制的是不变量，不是机械翻译 Rust 类型。

对应的不变量包括：Route 发布后不可变；matches 边不拥有节点；Face mapping 只在所属连接解释；pending query 只有一个最终完成者；任何全局 version 变化都使旧 cache 不可命中；关闭后弱引用升级失败或状态检查拒绝新工作。

## 性能与故障检查

关键变量是 Resource 数、平均深度、通配符比例、Face 数、Route 目的数、声明变更率、cache hit ratio、query fan-out、reply 数、channel 容量与 transport RTT。应观察 miss 重算时间、写锁等待、旧 Route 内存、pending query 数、最老队列项和关闭耗时。

故障测试覆盖非法/无法恢复的 WireExpr、Face 在转发中关闭、声明风暴、version wrap/变化、重复 Final、无 Final 分支、超时后迟到 Reply、队列满载和 Runtime close 时任务仍持 Arc。

## 性能与可行性预算

设 Resource 节点数 `R`、matches 边数 `E_m`、Face 数 `F`、单 Route 目的数 `D`、声明变更率 `u`、数据率 `f`、队列容量 `C`、payload `S`：

| 路径 | 主要成本 | 空间主项 |
|---|---|---|
| 精确 Resource lookup | 与 key 段数/树深相关 | tree `O(R)` |
| 通配相交与 matches | 取决于表达式和相关边 | `O(E_m)` |
| route cache hit | 查找 + `O(D)` 扇出 | 每 context 的 Arc<Route> |
| cache invalidation | 局部边遍历或全局 epoch `O(1)` | 旧 Route 暂存 |
| 声明风暴 | `u × match/invalidate/recompute` | 新旧状态并存 |
| outbound queue | 入队摊销常数 | `O(C×S)` 或共享 payload |
| Query | fan-out + reply 数 | pending state + reply buffers |

稳定拓扑下应关注 cache hit、destination 扇出和 transport；动态拓扑下应关注写锁、重算、旧快照与任务 churn。只在精确 key、单 Face、无声明变化条件下测得的吞吐无法代表边缘网络部署。

机器人应用还要测数据年龄、最老队列项、query completion 尾延迟和 close 最坏时间。网络吞吐足够而 route 变更后首帧延迟突增，仍可能影响故障切换与控制安全。

## 最小复刻路线

先实现只支持精确 key 的 Session 与本地 Pub/Sub；加入 Resource tree；再引入 Face 和每连接 mapping；随后实现不可变 Route cache 与版本失效；再添加 `*`/`**` 相交图；之后实现 Query pending/Final；最后接异步有界 pipeline、transport 和多角色拓扑。

每阶段的完成条件是：实体析构撤销声明；Face mapping 不跨连接误用；缓存失效后不返回旧目的地；删除 Resource 不形成强引用环；Query 超时/Final 只完成一次；channel 满载行为可观测；Runtime close 后没有后台任务继续使用已关闭 transport。
