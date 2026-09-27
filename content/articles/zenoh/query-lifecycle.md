# Query 生命周期：双路分发、回复聚合与 Final 屏障

仓库调度器想一次查询所有仍在线的机器人电池状态，例如 `robot/*/battery`。最朴素的请求/响应 socket 会默认“一次请求对应一个服务端、收到一个包就结束”；但这里可能有十几台机器人分别回 Reply，也可能某台离线、一条远端分支卡住。读者会观察到只拿到第一台设备、等待永远不结束，或晚到的电量值混进下一次请求。发布订阅处理的是“有数据就推送”，Query/Queryable 处理的则是“发出一次请求，接收零到多条回复，然后明确结束”。中间件必须回答：请求应该送到哪些处理者、多个处理者的回复如何汇入同一个接收端、怎样判断所有分支都已结束、超时与关闭时由谁回收状态。

Zenoh 没有把 Query 简化成一次 RPC。一个查询可以同时命中本进程 Queryable 和远端节点，也可以从多个方向返回多条 Reply。因此，真正的核心不是请求报文，而是一个扇出后再汇聚的生命周期协议。

## 参与对象与完整数据流

先把公开对象与内部状态放在一条链上：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
应用
  ├─ declare_queryable("robot/**")
  │      └─ QueryableState{id, key_expr, complete, origin, callback}
  │
  └─ get("robot/arm/state")
         └─ QueryState{qid, callback, consolidation, nb_final, ...}
                ├─ 本地 handle_query() ──> Queryable callback
                └─ Primitives::send_request() ──> routing Face
                                                     ├─ 方向 A
                                                     └─ 方向 B

Reply ───────────────────────────────> QueryState callback
ResponseFinal ── 每条生产路径一次 ───> nb_final 减一
nb_final == 0 ───────────────────────> 删除 QueryState，关闭接收端
```

这里有两种“状态”，不能混为一谈。

- `QueryableState` 是服务提供方的长期注册信息，表示“谁能回答哪个 key expression”。
- `QueryState` 是请求方的一次性在途状态，表示“某个 qid 还在等哪些完成信号”。

一次 Query 的标识是 `qid`。它的作用和网络协议中的 request id 相同：回复可以乱序到达，但只要携带相同 qid，就能找到原始接收回调。

## Queryable 声明把回调注册为实体

公开 API 使用 builder 收集配置：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let queryable = session
    .declare_queryable("robot/**")
    .complete(true)
    .await?;

while let Ok(query) = queryable.recv_async().await {
    // 读取 query.selector()，再通过 query.reply(...) 回答
}
```

内部路径可缩写为：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
QueryableBuilder::wait / IntoFuture
  -> handler.into_handler()
       得到 Callback<Query> 与 Receiver<Query>
  -> declare_nonwild_prefix()
  -> Session::declare_queryable_inner()
       [SessionState 写锁]
       1. 检查 primitives 是否存在
       2. 分配 entity id
       3. 插入 Arc<QueryableState>
       [释放写锁]
       4. 非 SessionLocal 时发送 DeclareQueryable
       5. 更新 matching 状态
  -> Queryable{WeakSession, id, receiver, SyncGroup}
```

`handler.into_handler()` 解释了为什么同一个声明既能以回调方式使用，也能以 `recv_async()` 方式使用：两种接口最终都被规约为一个内部 `Callback<Query>`。默认接收器只是在 callback 中把 Query 投递到本地队列。

核心状态近似如下：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
struct QueryableState {
    id: EntityId,
    key_expr: OwnedKeyExpr,
    complete: bool,
    origin: Locality,
    callback: Callback<Query>,
}
```

`Arc<QueryableState>` 允许分发代码在持有 Session 读锁时克隆一个稳定引用，随后释放锁，再执行用户回调。这样，回调即使重新声明或取消实体，也不会在同一把锁上发生自死锁。

`complete` 并不表示“每次查询一定成功”。它参与路由目标选择：调用方要求 `AllComplete` 时，只选择覆盖条件满足且标记为 complete 的 Queryable；`BestMatching` 则会在匹配集合中选择更合适的目标。

源码位置：[`QueryableBuilder` 执行声明](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/queryable.rs#L221-L248)，[`QueryableState` 与声明实现](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L2094-L2190)。

## 发起 Query 时先建立接收状态

请求方的典型代码是：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let replies = session
    .get("robot/arm/state")
    .timeout(Duration::from_millis(200))
    .await?;

while let Ok(reply) = replies.recv_async().await {
    match reply.result() {
        Ok(sample) => consume(sample),
        Err(error) => report(error),
    }
}
```

`await` 得到的不是单条 Reply，而是 Reply 接收器。请求仍在后台继续，直到所有 Final 到达或超时任务关闭它。

内部建立顺序非常重要：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Session::query(...)
  1. 规范化 selector、target、consolidation、timeout
  2. Auto consolidation 转为具体策略
  3. 原子分配 qid
  4. 检查 primitives；若已关闭则立即报错
  5. 创建取消通知与 timeout task
  6. 计算 nb_final
  7. 在 state.queries[qid] 插入 QueryState
  8. 按 destination 分发
       Remote/Any -> primitives.send_request(...)
       Local/Any  -> handle_query(local=true, ...)
```

固定提交的 [`Session::query`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L2690-L2856) 在 SessionState 写锁内分配 qid、启动 timeout task 并插入 `QueryState`；释放写锁后才发送远端 request 或调用本地 `handle_query`。timeout task 即使立刻变为可运行，也必须等这把写锁释放，因此不会在 map 插入前误删状态。原子 `fetch_add(SeqCst)` 负责给并发 query 分配不同序号；Reply 能否找到对象依靠 `queries` map 与其锁，不是依靠原子计数传递 QueryState 内存。

必须先插入 `QueryState`，再允许 Reply 返回。若顺序反过来，同进程快速路径可能在请求状态创建之前就返回 Reply，接收端只能把它当成未知 qid 丢弃。

`QueryState` 可抽象为：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
struct QueryState {
    nb_final: usize,
    reception_mode: ConsolidationMode,
    replies: HashMap<OwnedKeyExpr, Reply>, // Latest 等策略可能使用
    callback: Callback<Reply>,
    // 还包含 selector、取消通知等字段
}
```

## `nb_final` 是生产路径计数而不是回复计数

当 `Locality::Any` 同时启用本地与远端分发时，`nb_final` 初始化为 2；只走一个方向时初始化为 1。

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let nb_final = if destination == Locality::Any { 2 } else { 1 };
```

这不是“最多等待两条 Reply”。每个方向都可以产生任意数量的 Reply，但必须恰好产生一个 Final：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
本地路径: Reply, Reply, Final ─┐
                               ├─ nb_final: 2 -> 1 -> 0 -> 完成
远端路径: Reply, Final ────────┘
```

Final 因此是控制消息，不是空 Reply。少一个 Final，请求会一直存活到超时；多一个 Final，第二个信号会落入 unknown-query 路径。复刻实现时，应把“数据条数”与“生产者完成数”放在两个独立变量中。

## 本地分发先复制回调，再释放锁

`handle_query` 在 Session 状态读锁下筛选候选者：

1. Queryable 的 `origin` 与请求来源相容；
2. 本地或远端方向符合 Queryable 的 locality；
3. target 为 `AllComplete` 时，Queryable 必须标为 complete；
4. Queryable key expression 与查询 key expression 相交。

实现不会在锁内直接调用 callback，而是克隆轻量的 `(entity_id, callback)` 列表：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let callbacks = {
    let state = self.0.state.read();
    state.queryables.values()
        .filter(|q| matches_query(q, &request))
        .map(|q| (q.id, q.callback.clone()))
        .collect::<Vec<_>>()
}; // 读锁在这里释放

for (id, callback) in callbacks {
    callback.call(make_query(id, shared_inner.clone()));
}
```

代码是结构化示意，但准确表达了锁边界。把用户代码放到内部写锁或路由表锁内执行，会造成三个问题：慢回调阻塞所有声明变更；回调重入 Session 时死锁；无法给锁等待时间建立稳定上界。

对应源码：[`Session::handle_query`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L2969-L3043)。

## `QueryInner::drop` 把对象生命周期变成 Final

每个匹配 callback 收到一个 `Query`，这些 Query 共享同一个 `Arc<QueryInner>`：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
pub struct Query {
    inner: Arc<QueryInner>,
}

impl Drop for QueryInner {
    fn drop(&mut self) {
        self.primitives.send_response_final(ResponseFinal {
            rid: self.request_id,
            ext_qos: self.qos,
            ext_tstamp: None,
        });
    }
}
```

上面省略了版本细节，但保留了机制：只有最后一个 `Query` clone 被释放，`QueryInner` 才析构并发送 Final。这是 RAII——资源离开生命周期时自动执行完成动作。

该设计允许 callback 把 Query 移入异步任务：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
move |query: Query| {
    tokio::spawn(async move {
        let value = slow_database_lookup().await;
        query.reply(query.key_expr(), value).await.ok();
        // task 结束，最后一个 Query 被释放，随后自动发送 Final
    });
}
```

如果 Final 在 callback 返回时就发送，上述异步 Reply 会变成“完成后的迟到回复”。绑定 `QueryInner` 析构后，异步任务持有 Query 就等价于声明“这条处理分支仍活着”。

代价也很明确：泄漏一个 Query clone 会延迟 Final，直到请求超时或 Session 关闭。没有任何 Queryable 匹配时，局部 `QueryInner` 会在 `handle_query` 返回时直接析构，因此零匹配仍能立即结束，不会悬挂。

对应源码：[`Query` 与 `QueryInner::drop`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/queryable.rs#L116-L160)。

以下固定提交中的析构体展示完成信号从哪发出；它的调用时刻取决于最后一个 `Arc<QueryInner>` owner 何时释放。

**代码身份：固定提交源码摘录；eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5，`zenoh/src/api/queryable.rs`，`QueryInner::drop`，L151–L158。**
```rust
impl Drop for QueryInner {
    fn drop(&mut self) {
        self.primitives.send_response_final(&mut ResponseFinal {
            rid: self.qid,
            ext_qos: self.qos.into(),
            ext_tstamp: None,
        });
    }
}
```

## 远端路由为每个方向重写 qid

源 Face 上的 `src_qid` 只在该 Face 的命名空间内唯一。路由节点向多个下游 Face 扇出时，为每个方向分配新的 `dst_qid`：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
来源 Face: Request(src_qid=7)
  -> key expression 解析与 query route 计算
  -> 目标 Face A: pending_queries[41] = Arc<Query{src_face, src_qid=7}>
  -> 目标 Face B: pending_queries[93] = Arc<Query{src_face, src_qid=7}>
  -> 分别发送 Request(dst_qid=41 / 93)
```

返回路径执行逆映射：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Face A: Response(dst_qid=41)
  -> pending_queries[41]
  -> 改写为 src_qid=7
  -> 发回来源 Face
```

这样每条连接只需管理自己的小型 id 空间，路由器也不必要求全网共享一个请求编号生成器。

扇出完成判定再次利用 `Arc`。所有 pending entry 共享一个 `Arc<Query>`；某方向收到 Final 后删除 entry 并取消该方向的清理任务。当最后一个方向完成时，`Arc::into_inner` 才能取得唯一所有权，并向来源发送唯一的上游 Final。

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Arc<Query> strong refs
  来源临时引用       -- 路由建立后释放
  pending A          -- A Final 后删除
  pending B          -- B Final 后删除，成为最后一个
                              |
                              +-> 上游 ResponseFinal(src_qid)
```

这相当于用引用计数实现 fan-out/fan-in 屏障，省去了手工维护 `remaining_branches`。风险是任何 pending entry 泄漏都会阻止上游 Final，所以每个方向还必须有 timeout cleanup，并在 Face 或 Transport 关闭时清空 pending map。

对应源码：[`route_query` 扇出](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/queries.rs#L202-L370)，[`QueryCleanup`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/queries.rs#L437-L523)，[`回复与 Final 汇聚`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/queries.rs#L552-L686)。

## Consolidation 决定 Reply 何时交付

多个方向可能对同一个 key 返回多次。Consolidation 用来规定接收端保留和交付哪些版本：

- `None`：每条 Reply 到达后立即交付，延迟最低，调用方自己去重。
- `Monotonic`：只交付时间戳不倒退的版本。
- `Latest`：按 key 暂存最新版本，完成或超时时再统一交付。
- `Auto`：在发起请求时根据 selector 等信息变成一个具体策略，而不是一直保留“自动”状态。

`Latest` 会增加 `O(K)` 内存，其中 `K` 是出现过的不同 key 数；它也改变可观察时序——数据可能已经到达网络栈，却要等 Final 才进入用户 callback。排查“回复延迟”时，必须区分网络延迟和 consolidation 延迟。

## 超时是另一条必须汇聚的终止路径

Session 为每个 Query 启动 timeout task。到期后，它以 qid 从 `queries` map 删除状态；若使用 `Latest`，先刷新缓存 Reply，再向接收端报告 timeout 并结束。

远端路由层还有 per-direction timeout。两者职责不同：

- 路由方向超时：回收某个下游 Face 的 pending entry，使扇出屏障能够继续汇聚；
- 请求端超时：回收整个 `QueryState`，规定调用者最多等待多久。

晚到 Reply 或 Final 找不到 qid 时，只能记录并丢弃，不能重新创建状态。否则旧请求的迟到数据可能污染一个恰好复用了相同编号的新请求。

## 失败路径与回收责任

| 失败位置 | 调用方看到的结果 | 回收者 |
|---|---|---|
| Session 已关闭 | 发起请求立即失败 | `primitives()` 检查，不插入状态 |
| 没有匹配 Queryable | 零条 Reply，正常结束 | 本地 `QueryInner::drop` 或路由空方向分支 |
| 单个下游不发 Final | 该方向超时，最终仍可结束 | `QueryCleanup` |
| 整体等待超时 | 已缓存 Reply 按策略刷新，并报告 timeout | Session timeout task |
| Reply 在超时后到达 | unknown qid，丢弃 | 接收入口 |
| Session 关闭 | receiver 关闭、在途状态被释放 | Session close 状态机 |

一个可靠的复刻实现，应为表中每一行写出唯一的状态拥有者。若同一 pending entry 同时由 timeout、transport close 和显式取消并发删除，删除操作必须是幂等的，并且只有成功取得该 entry 的路径有权发送最终完成信号。

## 数据结构与性能边界

设一次请求命中 `Q` 个本地 Queryable、扇出到 `F` 个远端 Face、产生 `R` 条 Reply：

| 操作 | 时间复杂度 | 主要成本 |
|---|---:|---|
| 本地候选筛选 | 朴素上界 `O(Q)` | key-expression 相交判断、callback clone |
| 远端方向建立 | `O(F)` 加路由计算 | pending map 插入、qid 分配、timeout task |
| 单条 Reply 定位 | 平均 `O(1)` | qid hash lookup、callback 调用 |
| `Latest` 合并 | 平均每条 `O(1)` | key 哈希与保留值内存 |
| Final 汇聚 | 每方向 `O(1)` | pending 删除、取消 token、`Arc` 引用计数 |

高扇出 Query 的主要风险不是单条报文大小，而是 pending entry 和 timeout task 的峰值。工程指标至少应包含：在途 Query 数、每 Query 扇出数、pending map 峰值、超时率、迟到 Reply 数以及 callback 队列水位。

## 最小复刻的实现顺序

可以按以下顺序实现一个保留 Zenoh 核心语义的查询子系统：

1. 用 `HashMap<Qid, QueryState>` 建立单路径请求与 Reply 回调；
2. 加入显式 `Final`，把“完成”从 Reply 数量中分离；
3. 加入 Queryable registry，并保证锁外执行用户 callback；
4. 用共享 `QueryInner` 的最后析构发送本地 Final；
5. 为每个下游分配局部 qid，建立 pending 反向映射；
6. 用共享聚合对象或显式计数器实现多方向 Final 屏障；
7. 加入 per-direction timeout、请求端 timeout 与迟到消息丢弃；
8. 最后实现 `Latest` 等 consolidation 策略。

这条顺序先固定生命周期不变量，再增加路由和便利 API。Query 系统最难修复的错误通常不是匹配错一个 key，而是某条失败路径没有发 Final，导致状态永久留在 map 中。

