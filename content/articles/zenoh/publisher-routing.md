# Publisher 数据路径：声明聚合、WireExpr 与 Route Cache

机械臂控制器以 500 Hz 发布 `robot/arm/joint/1/state`。最朴素的实现会在每个样本到达时扫描所有订阅者，再为每个出口临时拼完整 key；当网络里有数千个声明、同一网关上有多个出口时，扫描成本随声明数和目的 Face 数一起增长。若改用 `HashMap<String, Socket>`，又会丢掉通配订阅、来源 Face 的 scope 和经过 ACL 后的出口集合；重连后旧目的地还可能留在表里。Zenoh 把低频声明期计算与高频数据期转发分开：创建 Publisher 时登记实体/声明，样本到来时恢复 key、按来源求 Route，并为每个目的 Face 发送它能理解的 WireExpr。下面追踪这条真实路径，区分“声明了 Publisher”和“数据最终走哪条连接”。

这里的 KeyExpr 是分段的机器人数据名，也可以带 `*`/`**` 通配符，所以一个 Subscriber 可能匹配许多发布 key。Face 是路由层管理的网络连接或本地 Session 端点；Route 是“从当前来源可走到哪些下一跳”的方向集合。WireExpr 则把完整 KeyExpr 按某条 Face 的 mapping 压缩成 scope 与 suffix，另一端只能在同一连接上下文中还原它。

本文固定源码基线为 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5`；下文标出符号并直接摘录该提交的相关实现。

## 从声明建立 Publisher 实体并订阅远端匹配状态

Publisher 的公开声明会先建立本地实体；网络上的控制面消息则不是“把 Publisher 句柄发给对端”。控制面维护 key 与匹配 Subscriber 的拓扑状态，和后面承载 payload 的 Push 分开。此处的 `Interest` 是本端请求另一端报告匹配声明的协议消息；`CurrentFuture` 同时订阅已经存在和之后出现的匹配 Subscriber。

公开调用：

```rust
let publisher = session
    .declare_publisher("robot/pose")
    .congestion_control(CongestionControl::Drop)
    .priority(Priority::DataHigh)
    .reliability(Reliability::BestEffort)
    .await?;
```

例子中的 `.reliability(...)` 是 unstable API，代码要启用该 feature 才能照此编译；稳定 API 用户可删去这一行，其他三个 builder 配置仍能演示本节关注的声明流程。

Builder 在局部保存 key expression、encoding、priority、express、reliability 和 locality。只有 resolve 时才进入 Session 共享状态。

这避免每调用一个配置方法就获取 Session write lock，也让参数验证在提交实体前完成。

### Declare 的锁内事务

下面这条路径对应固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Session::declare_publisher_inner`：

```text
acquire SessionState write lock
  -> verify primitives exists, else SessionClosed
  -> allocate local entity id
  -> store owned key expression in PublisherState
  -> check aggregated publisher declarations
  -> check twin publisher on same key
  -> assign/reuse remote Interest id
  -> insert local publisher state
  -> if a new Interest is needed:
       retain primitives and selected key; release state write lock
       construct and send CurrentFuture Interest
     otherwise:
       reuse existing remote Interest; finish with local state only
```

关键边界是最后两步。网络/路由调用发生前显式 drop state guard。

固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中 `Session::declare_publisher_inner` 的核心区段如下：

```rust
        let mut state = zwrite!(self.0.state);
        if state.primitives.is_none() {
            return Err(SessionClosedError.into());
        }
        let id = self.0.runtime.next_id();

        let mut pub_state = PublisherState {
            id,
            remote_id: id,
            key_expr: key_expr.clone().into_owned(),
            destination,
        };

        let declared_pub = (destination != Locality::SessionLocal)
            .then(|| {
                match state
                    .aggregated_publishers
                    .iter()
                    .find(|s| s.includes(&key_expr))
                {
                    Some(join_pub) => {
                        if let Some(joined_pub) = state.publishers.values().find(|p| {
                            p.destination != Locality::SessionLocal
                                && join_pub.includes(&p.key_expr)
                        }) {
                            pub_state.remote_id = joined_pub.remote_id;
                            None
                        } else {
                            Some(join_pub.clone().into())
                        }
                    }
                    None => {
                        if let Some(twin_pub) = state.publishers.values().find(|p| {
                            p.destination != Locality::SessionLocal && p.key_expr == key_expr
                        }) {
                            pub_state.remote_id = twin_pub.remote_id;
                            None
                        } else {
                            Some(key_expr.clone())
                        }
                    }
                }
            })
            .flatten();

        state.publishers.insert(id, pub_state);

        if let Some(res) = declared_pub {
            let primitives = state.primitives()?;
            drop(state);
            primitives.send_interest(&mut Interest {
                id,
                mode: InterestMode::CurrentFuture,
                options: InterestOptions::KEYEXPRS + InterestOptions::SUBSCRIBERS,
                wire_expr: Some(res.to_wire(self).to_owned()),
                ext_qos: network::ext::QoSType::DEFAULT,
                ext_tstamp: None,
                ext_nodeid: interest::ext::NodeIdType::DEFAULT,
            });
        }
```

固定提交里协议模式的枚举把 Current、Future 和两者合并的 CurrentFuture 区分开：

```rust
pub enum InterestMode {
    Final,
    Current,
    Future,
    CurrentFuture,
}

impl InterestMode {
    pub fn is_future(&self) -> bool {
        self == &InterestMode::Future || self == &InterestMode::CurrentFuture
    }

    pub fn is_current(&self) -> bool {
        self == &InterestMode::Current || self == &InterestMode::CurrentFuture
    }
}
```

协议说明用两方时间线区分短暂枚举当前状态与订阅当前加未来状态：

对应的上游实现如下：
````text
A                   B
|     INTEREST      |
|------------------>| -- This is a DeclareInterest e.g. for subscriber declarations/undeclarations.
|                   |
|  DECL SUBSCRIBER  |
|<------------------| -- With interest_id field set
|  DECL SUBSCRIBER  |
|<------------------| -- With interest_id field set
|  DECL SUBSCRIBER  |
|<------------------| -- With interest_id field set
|                   |
|     DECL FINAL    |
|<------------------| -- With interest_id field set
|                   |
|  DECL SUBSCRIBER  |
|<------------------| -- With interest_id field not set
| UNDECL SUBSCRIBER |
|<------------------| -- With interest_id field not set
|                   |
|        ...        |
|                   |
| INTEREST FINAL    |
|------------------>| -- Mode: Final
|                   |    This stops the transmission of subscriber declarations/undeclarations.
|                   |
````

`Session::declare_publisher_inner` 把 publisher key expression 放进 Interest 的 `wire_expr`，并设置 `KEYEXPRS + SUBSCRIBERS`；因此对端返回的是匹配订阅拓扑声明，而不是样本 payload。路由层之后用这些声明算出下一跳。publisher 的 `put` 才开始数据面 Push。

`state` 是 Session 内部写 guard，保护 publisher map 与聚合规则；新的 `PublisherState` 先取得独立 local id，remote id 初始相同。若存在涵盖本 key 的 aggregated Publisher，或找到相同 key 的 twin publisher，就复用现存 `remote_id` 并跳过重复的 Interest；否则先把实体放进本地 map，再释放 guard、向 primitives 发送 `CurrentFuture` Interest。因而 API 的 Publisher handle、Session 里的实体记录和对远端表达的 Interest 请求是不同对象，不能说成“每个 handle 都直接发送一条 Publisher 声明”。

### Twin Publisher 共享远端 Interest

同一 Session 内可以创建多个相同 key 的 Publisher。如果每个都向网络发送独立 Interest，会扩大控制面状态。

Zenoh 可让 twin publishers 共享一个 remote id：

```text
Publisher local id 10 ----+
                          +-> one remote Interest id 7
Publisher local id 11 ----+
```

图中的远端编号是共享 Interest 的 id，不是 Publisher 的网络实体 id。

本地实体仍独立，drop 一个 Publisher 不应立即撤销 remote interest。只有最后一个代表消失时才发 Final Interest。

这是网络控制面的共享代表关系：remote interest 的寿命由本地代表数量决定。

固定提交中，`Publisher::undeclare_impl` 先撤销本句柄的 matching listener，再把实体 id 交给 Session；真正是否还需要远端 Interest 由 SessionState 中其他 Publisher 的 remote id 决定：

```rust
pub(crate) fn undeclare_publisher_inner(&self, pid: Id) -> ZResult<()> {
    let mut state = zwrite!(self.0.state);
    let Ok(primitives) = state.primitives() else {
        return Ok(());
    };
    if let Some(pub_state) = state.publishers.remove(&pid) {
        trace!("undeclare_publisher({:?})", pub_state);
        if pub_state.destination != Locality::SessionLocal {
            // Note: there might be several publishers on the same KeyExpr.
            // Before calling forget_publishers(key_expr), check if this was the last one.
            if !state.publishers.values().any(|p| {
                p.destination != Locality::SessionLocal && p.remote_id == pub_state.remote_id
            }) {
                drop(state);
                primitives.send_interest(&mut Interest {
                    id: pub_state.remote_id,
                    mode: InterestMode::Final,
                    // Note: InterestMode::Final options are undefined in the current protocol specification,
                    //       they are initialized here for internal use by local egress interceptors.
                    options: InterestOptions::SUBSCRIBERS,
                    wire_expr: None,
                    ext_qos: interest::ext::QoSType::DEFAULT,
                    ext_tstamp: None,
                    ext_nodeid: interest::ext::NodeIdType::DEFAULT,
                });
            }
        }
        Ok(())
    } else {
        Err(zerror!("Unable to find publisher").into())
    }
}
```

检查条件不是“这个 key 还有没有句柄”，而是“还有没有非 SessionLocal 的实体共享同一个 `remote_id`”。同一个 remote representative 可由 twin 或聚合声明复用，因此只有最后一个代表移除后才发送 `InterestMode::Final`，告诉路由层停止这组匹配 Subscriber 的声明通知。`drop(state)` 仍先于发送：修改本地 map 需要 Session 写锁，向 primitives 发送控制消息不在这把锁内执行。

### Aggregated Publisher 压缩 Interest 集合

配置可声明更宽的聚合 key expression。例如多个具体 Publisher：

```text
robot/arm/joint/1/state
robot/arm/joint/2/state
robot/arm/joint/3/state
```

可以由一个聚合表达式代表：

```text
robot/arm/joint/*/state
```

如果现有 aggregated publisher includes 新 key，新增具体 Publisher 无需再发一份重叠 Interest；已有通配 Interest 已覆盖这组 key 的 Subscriber 匹配状态。

聚合降低控制面 Interest 数量，却会为更大的 key expression 空间请求 Subscriber 状态，其中可能包含当前进程暂时不会发布的 key；这是控制状态压缩与匹配范围大小的权衡。

### Publisher Handle 保存 Session 与实体 ID

Publisher 通常持 WeakSession、local id 和默认发送策略。Drop/undeclare 时用 id 从 SessionState 移除自身，再判断对应 remote Interest 是否仍被 twin/aggregation 使用。

这里的 `WeakSession` 不能按标准库 `Weak<T>` 理解。固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 的 `Session::downgrade` 将一个额外 Arc 放进 `ManuallyDrop<Session>`，但不增加公开 Session handle 的逻辑计数；Publisher 因而不会阻止最后一个公开 Session handle 发起 close，Arc 内存则要等 close 清理内部引用环后才能回收。Publisher 的 put/undeclare 仍需检查 Session 是否关闭。

业务代码不应假设 Publisher handle 存活就代表 Session 仍 open；每次 put 仍可能返回 SessionClosed。

固定提交中 Session 的公开句柄计数与底层 Arc 引用数分开：

```rust
impl Session {
    #[cfg(not(feature = "internal"))]
    pub(crate) fn downgrade(&self) -> WeakSession {
        WeakSession {
            inner: ManuallyDrop::new(Session(self.0.clone())),
        }
    }

    #[zenoh_macros::internal]
    pub fn downgrade(&self) -> WeakSession {
        WeakSession {
            inner: ManuallyDrop::new(Session(self.0.clone())),
        }
    }
}

impl Clone for Session {
    fn clone(&self) -> Self {
        self.0.strong_counter.fetch_add(1, Ordering::Relaxed);
        Self(self.0.clone())
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        if self.0.strong_counter.fetch_sub(1, Ordering::Relaxed) == 1 {
            if let Err(error) = self.close().wait() {
                tracing::error!(error)
            }
        }
    }
}

pub struct WeakSession {
    inner: ManuallyDrop<Session>,
}

impl Clone for WeakSession {
    fn clone(&self) -> Self {
        self.inner.downgrade()
    }
}

impl Drop for WeakSession {
    fn drop(&mut self) {
        // SAFETY: Rust does not call drop on ManuallyDrop and all Session-allocated resources
        // except Arc<SessionInner>, will be released once last "strong" Session is dropped.
        unsafe { std::ptr::drop_in_place(&mut self.inner.0 as *mut _) };
    }
}
```

`Session::clone` 同时增加逻辑 strong counter 与 Arc；`Session::downgrade` 只克隆 Arc 并包进 `ManuallyDrop`，所以不会让最后一个公开 Session 延迟 close。销毁 WeakSession 时仅析构内部 Arc 字段，不运行 `Session::drop`，避免误减逻辑计数。`Ordering::Relaxed` 保证 counter 的原子增减，但不为 Session 其他字段建立内存顺序；那些字段仍由具体 Session 锁及关闭协议保护。这样 Publisher 可安全持有 SessionInner 的内存地址，同时 Session 生命周期不会被 Publisher handle 延长。

固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Publisher` 将声明状态放在对象里，并在 Drop 时走 undeclare：

```rust
pub struct Publisher<'a> {
    pub(crate) session: WeakSession,
    pub(crate) id: Id,
    pub(crate) key_expr: KeyExpr<'a>,
    pub(crate) encoding: Encoding,
    pub(crate) congestion_control: CongestionControl,
    pub(crate) priority: Priority,
    pub(crate) is_express: bool,
    pub(crate) destination: Locality,
    #[cfg(feature = "unstable")]
    pub(crate) reliability: Reliability,
    pub(crate) matching_listeners: Arc<Mutex<HashSet<Id>>>,
    pub(crate) undeclare_on_drop: bool,
    pub(crate) sync_group: SyncGroup,
}

impl Drop for Publisher<'_> {
    fn drop(&mut self) {
        if self.undeclare_on_drop {
            if let Err(error) = self.undeclare_impl() {
                error!(error);
            }
        }
    }
}
```

`WeakSession` 名称看似标准库 `Weak<T>`，但该项目用 `ManuallyDrop<Session>` 保存底层 Arc，同时 `Session::drop` 只按显式的 strong counter 决定最后一个公开 Session handle 是否开始 close。Publisher 因而能在 Session 已关闭后仍暂时存在；它不会延长公开 Session 生命周期，put/undeclare 必须检查关闭状态。Publisher Drop 自动调用 `undeclare_impl`，而 `undeclare_on_drop` 先被置为 false，避免 panic 路径中重复 undeclare。

`matching_listeners` 是另一个独立的共享状态：它保存通过此 Publisher 注册的匹配状态 listener id。`Arc` 让 builder 和 Publisher 句柄引用同一组 id，`Mutex` 只保护这组 HashSet；它并不保护整个 Publisher 或 SessionState。下面的 `zlock!` 临界区只把 id drain 到一个拥有型 `Vec`，离开 `let` 语句后 Mutex guard 已释放，随后才逐个进入 Session 撤销路径，避免同时持有两种锁。

这里 `Drop::drop` 只负责进入关闭流程；下面的短摘录说明实体 id 怎样返回 Session。`undeclare_on_drop` 必须先置为 false，因为后续清 listener 或 Session 操作可能失败，析构不能在展开时再重复执行同一操作：

对应的上游实现如下：
```rust
fn undeclare_impl(&mut self) -> ZResult<()> {
    // set the flag first to avoid double panic if this function panics
    self.undeclare_on_drop = false;
    let ids: Vec<Id> = zlock!(self.matching_listeners).drain().collect();
    for id in ids {
        self.session.undeclare_matches_listener_inner(id)?
    }
    self.session.undeclare_publisher_inner(self.id)
}
```

## 发布样本进入路由与构造下一跳

`publisher.put(payload)` 将调用时 payload 与 Publisher 默认策略组合成一条 Push；它的 body 是 Put 或 Delete：

```text
key expression
payload bytes
encoding
timestamp
congestion control
priority
express flag
reliability
```

然后通过 Session primitives 进入本地 routing Face。Payload 通常采用拥有或引用计数 buffer，避免异步发送时借用调用者已经释放的栈内存。

但一次本地 `put` 不必经过网络 Face 才能到达同 Session 订阅者。固定提交的 `Session::resolve_put` 先在 SessionState 读锁下取得 primitives 与匹配的本地 callback 列表，随后释放锁，再构造 Push。若目标包含 Remote，就把 Push 交给 primitives；若本地也有订阅者，则保留这份 Push 并同步调用本地 callback。下面两段均为 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中 `Session::resolve_put` 的固定源码摘录；中间省略的是时间戳、QoS 与 Push 字段构造。

```rust
        let state = zread!(self.0.state);
        let primitives = state.primitives()?;
        let wire_expr = key_expr.to_wire(self);
        let mut callbacks = SubscriberCallbacks::default();
        if destination != Locality::Remote {
            callbacks =
                state.subscriber_callbacks(true, SubscriberKind::Subscriber, &wire_expr, false);
        }
        drop(state);
```

读锁中的不变量很具体：从同一份 Session 状态取得尚可用的 primitives、把 key 编为本 Session 使用的 WireExpr、按目的范围收集本地订阅回调。`drop(state)` 在 Push 构造以及后续外部调用之前显式释放 guard；若 callback 重入 Session API，持有 guard 会造成自我等待；慢 callback 也会持续占用读锁，让需要写锁的实体声明/撤销被迫等待。

对应的上游实现如下：
```rust
        let has_local_callbacks = !callbacks.is_empty();
        if destination != Locality::SessionLocal {
            primitives.send_push_consume(
                &mut push,
                #[cfg(feature = "unstable")]
                reliability,
                #[cfg(not(feature = "unstable"))]
                Reliability::DEFAULT,
                !has_local_callbacks,
            );
        }
        if has_local_callbacks {
            #[cold]
            fn call_local(
                callbacks: SubscriberCallbacks,
                push: &mut Push,
                #[cfg(feature = "unstable")] reliability: Reliability,
                #[cfg(feature = "unstable")] timestamp_stack: Option<
                    zenoh_protocol::network::timestamp_stack::TimestampStack,
                >,
            ) {
                callbacks.call(
                    true,
                    push.ext_qos,
                    &mut push.payload,
                    #[cfg(feature = "unstable")]
                    reliability,
                    #[cfg(feature = "unstable")]
                    timestamp_stack,
                );
            }
            #[cfg(feature = "unstable")]
            {
                let rt = self.0.runtime.get_inner();
                push_ts_interception(
                    &mut push.ext_ts_stack,
                    || Some(rt),
                    zenoh_protocol::network::timestamp_stack::interception_point::RECEIVE,
                );
            }
            #[cfg(feature = "unstable")]
            let timestamp_stack = push.ext_ts_stack.as_ref().map(|ts| ts.ts_stack.clone());
            call_local(
                callbacks,
                &mut push,
                #[cfg(feature = "unstable")]
                reliability,
                #[cfg(feature = "unstable")]
                timestamp_stack,
            );
        }
```

`!has_local_callbacks` 是消费权限提示：没有本地回调时，primitives 可消费这条 Push；本地还要读取 payload 时，函数必须保留它。这里不是把业务 callback 丢进 Executor 队列；`call_local` 就在当前 `resolve_put` 调用栈中执行。若控制线程发布后，本地订阅 callback 又同步写磁盘 20 ms，500 Hz 发布周期 2 ms 会立即被超过，后续控制样本可能在应用队列中积压。把长活转入有界队列可以隔离发布线程，但必须明确定义满队列时阻塞还是丢弃。这个本地执行事实不能套用到后面由 Face 转发的远端路径。

远端路径再经过 Face 提供的 Primitives 实现进入同一个路由函数。固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中 `Face::send_push_consume` 的关键调用是：

```rust
    #[inline]
    fn send_push_consume(&self, msg: &mut Push, reliability: Reliability, consume: bool) {
        let _span = tracing::enabled!(tracing::Level::DEBUG).then(|| {
            tracing::debug_span!(
                "send_push",
                expr = %msg.wire_expr,
                is_reliable = bool::from(reliability),
                consume,
            )
            .entered()
        });
        route_data(&self.tables, &self.state, msg, reliability, consume);
    }
```

因此数据从 Session 对象转交到 Face 的 Primitives 后，`route_data` 同时能得到被共享保护的 Tables、来源 Face 状态、Push 可变借用、Reliability 参数和消费权限。`Face::send_push_consume` 只是普通同步调用，没有创建 task 或切换线程；`route_data` 在调用它的 OS 线程上继续执行，究竟是哪条线程由上游 transport/primitive 路径决定，这个函数没有设置线程 affinity。它也没有把 callback 交给业务 Executor；当前职责仍是解释来源表达式并完成下一跳选择。

### Source Face 解释 WireExpr

网络消息携带的 key 可能是完整字符串，也可能是 scope id + suffix。`route_data()` 先用 source Face 的 mapping 查 scope prefix：

```text
WireExpr(scope=17, suffix=/state)
source_face.mapping[17] = robot/arm/joint/1
full expression = robot/arm/joint/1/state
```

Scope 未知时不能猜测字符串，消息被记录错误并丢弃。Scope 的含义只在该 Face/连接上下文成立。

本地 Session 也通过 Face 接入，因此 local 与 remote push 可复用同一 routing entry。

### RoutingExpr 延迟构造完整表达式

热路径不一定立刻拼接完整 String。`route_data` 已从入站 Face 的 scope 表中得到 prefix，再把 prefix 和消息里的 suffix 交给 `RoutingExpr`。若每个 500 Hz 样本都马上拼接并验证完整 key，即使命中已有 Route 也会付出字符串分配与解析成本；`RoutingExpr` 因此先保存借用片段，等某一步确实需要完整表达式时才计算。

固定提交中的结构和关键方法如下。`OnceCell` 让同一个 `RoutingExpr` 第一次需要 Resource 或完整 key 时执行初始化，此后复用结果；`Cow` 可以是从 Resource 借来的 key，也可以是在没有精确 Resource 时新构造的 owned key。

```rust
pub(crate) struct RoutingExpr<'a> {
    prefix: &'a Arc<Resource>,
    suffix: &'a str,
    resource: OnceCell<Option<&'a Arc<Resource>>>,
    key_expr: OnceCell<Option<Cow<'a, keyexpr>>>,
}

impl Debug for RoutingExpr<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}{}", self.prefix, self.suffix)
    }
}

impl<'a> RoutingExpr<'a> {
    #[inline]
    pub(crate) fn new(prefix: &'a Arc<Resource>, suffix: &'a str) -> Self {
        let resource = if suffix.is_empty() {
            Some(prefix).into()
        } else {
            OnceCell::new()
        };
        RoutingExpr {
            prefix,
            suffix,
            resource,
            key_expr: OnceCell::new(),
        }
    }

    pub(crate) fn resource(&self) -> Option<&'a Arc<Resource>> {
        *self
            .resource
            .get_or_init(|| Resource::get_resource_ref(self.prefix, self.suffix))
    }

    fn compute_key_expr(&self) -> Option<Cow<'a, keyexpr>> {
        let full_expr = match self.resource().as_ref() {
            Some(res) => res
                .keyexpr()
                .ok_or_else(|| keyexpr::new("").unwrap_err())
                .map(Cow::Borrowed),
            None => [self.prefix.expr(), self.suffix]
                .concat()
                .try_into()
                .map(Cow::Owned),
        };
        if let Err(e) = &full_expr {
            tracing::warn!("Invalid KE reached the system: {}", e);
        }
        full_expr.ok()
    }

    pub(crate) fn key_expr(&self) -> Option<&keyexpr> {
        self.key_expr
            .get_or_init(|| self.compute_key_expr())
            .as_deref()
    }

    pub(crate) fn get_best_key(&self, sid: usize) -> WireExpr<'a> {
        match self.resource() {
            Some(res) => res.get_best_key("", sid),
            None => self.prefix.get_best_key(self.suffix, sid),
        }
    }
}
```

`'a` ties both slices to their owners: `prefix` is borrowed from the routing tables’ Resource mapping, while `suffix` is borrowed from the incoming Push. The `RoutingExpr` cannot outlive those inputs, so it is used while `route_data` still holds the Tables read guard and the message borrow. It does not store a pointer past either lifetime. If the whole key is exactly an existing Resource, `compute_key_expr` borrows its validated key; otherwise it concatenates prefix and suffix, validates the result, and owns it in `Cow`. `get_best_key(sid)` is separate: it asks the Resource tree or source prefix for the compact WireExpr that the destination Face with id `sid` understands.

只有 ingress filter、route miss 或其他确实需要完整表达式的步骤才支付拼接与验证成本。

因此 Route cache hit 可以跳过完整 key 的重新拼接；但借用表达式不能从持有 mapping 的锁保护范围逃逸。缓存中留下的是完整计算出的 `Route` 与拥有型目标 `WireExpr<'static>`，不是这个临时的 `RoutingExpr`。

### Ingress Filter 位于路由选择前

消息从 source Face 进入后先经过 ingress interceptor/filter。它可以基于 key、metadata、来源或策略拒绝/修改消息。

过滤发生在 route fan-out 前，可以避免为被拒绝消息生成/查询 route 和克隆到多个目的地。

固定提交中 `route_data()` 持有 Tables 读 guard 进行 mapping、ingress 和 route 读取；这些过滤判断是数据路径上的真实成本。它们不等于调用 ROS/应用层消息回调，也不能在过滤时等待网络或重新进入会竞争同一 Tables 锁的路径。

接下来这段固定提交源码展示：WireExpr 已经由来源 Face 的 mapping 恢复成 `RoutingExpr` 后，先过 ingress filter，再取得路由。来源为 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `route_data`。

```rust
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

    let expr = RoutingExpr::new(prefix, msg.wire_expr.suffix.as_ref());
    tracing::trace!(
        "{} Route data for res {}{}",
        src_face,
        prefix.expr(),
        msg.wire_expr.suffix.as_ref()
    );

    #[cfg(feature = "stats")]
    let payload_observer = super::stats::PayloadObserver::new(msg, Some(&expr), tables);
    #[cfg(feature = "stats")]
    payload_observer.observe_payload(zenoh_stats::Rx, src_face, msg);

    if !tables.ingress_filter(src_face) {
        return;
    }
    let send_push = |dst_face: &FaceState, msg: &mut Push, reliability: Reliability| {
        if dst_face.primitives.send_push(msg, reliability) {
            #[cfg(feature = "stats")]
            payload_observer.observe_payload(zenoh_stats::Tx, dst_face, msg);
        }
    };
    let route = get_data_route(&rtables, src_face, &expr, msg.ext_nodeid.node_id);
```

`msg` 是入站 Push 的可变借用，`expr` 暂存来源前缀与后缀的路由表达式，`route` 则是基于本次来源 Face 与 node context 得出的目的方向。此时尚未执行应用订阅者 callback；`send_push` 也只是闭包，真正发送发生在稍后逐出口改写消息之后。若 ingress 不允许，函数立即返回，Route 计算和多目的地复制都不会发生。

接下来是同一函数中从非空 Route 到实际出口发送的固定源码摘录。前面的代码已完成 ingress 判定与 route 查询；下面保留出口过滤、释放 Tables guard、消息克隆/改写和 Face send 的完整分支。

```rust
    if !route.is_empty() {
        treat_timestamp!(
            &rtables.data.hlc,
            msg.payload,
            rtables.data.drop_future_timestamp
        );

        let inter_region_filter = {
            let src_zid = tables.hats[src_face.region]
                .remote_node_id_to_zid(src_face, msg.ext_nodeid.node_id);
            move |dir: &Direction| {
                InterRegionFilter {
                    src: &src_face.region,
                    dst: &dir.dst_face.region,
                    src_zid: src_zid.as_ref(),
                    fwd_zid: Some(&src_face.zid),
                    dst_zid: Some(&dir.dst_face.zid),
                }
                .resolve(tables)
            }
        };

        if route.len() == 1 {
            let dir = route.iter().next().unwrap();

            if inter_region_filter(dir) && rtables.egress_filter(src_face, &dir.dst_face) {
                #[cfg(feature = "unstable")]
                let weak_runtime = rtables.data.runtime.clone();

                drop(rtables);
                let mut msg_clone;
                let mut msg = &mut *msg;
                if !consume {
                    msg_clone = msg.clone();
                    msg = &mut msg_clone;
                }

                msg.wire_expr = dir.wire_expr.clone();
                msg.ext_nodeid = ext::NodeIdType {
                    node_id: dir.node_id,
                };
                #[cfg(feature = "unstable")]
                {
                    let weak = weak_runtime.clone();
                    push_ts_interception(
                        &mut msg.ext_ts_stack,
                        move || weak.and_then(|w| w.upgrade()).map(|rt| rt.state),
                        zenoh_protocol::network::timestamp_stack::interception_point::ROUTE,
                    );
                }
                send_push(&dir.dst_face, msg, reliability);
            }
        } else {
            let dirs = route
                .iter()
                .filter(|dir| {
                    inter_region_filter(dir) && rtables.egress_filter(src_face, &dir.dst_face)
                })
                .collect::<Vec<&Direction>>();

            #[cfg(feature = "unstable")]
            let weak_runtime = rtables.data.runtime.clone();

            drop(rtables);

            for dir in dirs {
                let mut push = Push {
                    wire_expr: dir.wire_expr.clone(),
                    ext_qos: msg.ext_qos,
                    ext_tstamp: None,
                    ext_nodeid: ext::NodeIdType {
                        node_id: dir.node_id,
                    },
                    ext_ts_stack: msg.ext_ts_stack.clone(),
                    payload: msg.payload.clone(),
                };
                #[cfg(feature = "unstable")]
                {
                    let weak = weak_runtime.as_ref();
                    push_ts_interception(
                        &mut push.ext_ts_stack,
                        || weak.and_then(|w| w.upgrade()).map(|rt| rt.state),
                        zenoh_protocol::network::timestamp_stack::interception_point::ROUTE,
                    );
                }
                send_push(&dir.dst_face, &mut push, reliability);
            }
        }
    }
```

Route 的“下一跳”是 `Direction.dst_face`，不是最终订阅机器人；网关可先把消息交给相邻 router，再由下一台 router 重新做一次路由。每个 `Direction` 同时保存该 Face 可解析的 `wire_expr` 和转发时写入 `ext_nodeid` 的路由上下文。单出口只有在 `consume == true` 时才原地改写入站 Push；否则先 clone。多出口无论如何都逐目的地构造新的 Push，因为 scope、NodeId 和 timestamp stack 等扩展字段可能不同；`payload.clone()` 复制的是 buffer 句柄，底层字节是否复制取决于 payload 类型。

注意这里不是“持 Tables 锁发网包”。inter-region/egress 检查仍读取拓扑，所以先在 Tables 读 guard 下筛选；筛好方向后 `drop(rtables)`，再调用 `send_push`。Route 的 `Arc` 和 `dst_face` 的 `Arc` 让当前这次转发计划在并发 undeclare/断链后仍可访问，但不维持对端在线、不保证内核 socket 接收或业务 callback 已运行。send 返回 false 时固定实现不记 Tx stats，也不会由此触发应用层重试。

### get_data_route 查询版本化缓存

对每个 Resource，数据路由缓存的映射键是来源 `Region` 与经过 routing-context 映射后的 `NodeId`；缓存对象自己的 `version` 还必须等于当前 topology version。Resource 本身由外层 resource tree 选中，不会再作为 `Routes` 内部哈希键。若表达式没有 Resource context，固定实现不走此缓存而直接计算。命中时克隆 `Arc<Route>`：复制的是 Arc 句柄，route vector 和每个 destination 仍由不可变快照共享。

```text
lookup attached Resource's Routes cache
  -> cached version matches and (source Region, mapped NodeId) exists: clone Arc<Route>
  -> miss: acquire this cache's write lock
           check again in case another writer filled the slot
           compute route while the write guard is still held
           if cache epoch changed, clear old map; store Arc<Route>
```

下面摘录的是固定源码 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `get_data_route` 与 `get_hat_data_route`。它显示两个层次：先以 Resource 的合并缓存查整条 Route；miss 时再逐个 routing region 查询该 Resource 下的 HAT 缓存，合并并按目标 Face 去重。

```rust
#[inline]
fn get_hat_data_route(
    tables: &Tables,
    src_face: &FaceState,
    expr: &RoutingExpr,
    node_id: NodeId,
    region: &Region,
) -> Arc<Route> {
    let node_id = tables.hats[region].map_routing_context(&tables.data, src_face, node_id);
    let compute_route =
        || tables.hats[region].compute_data_route(&tables.data, &src_face.region, expr, node_id);
    match expr
        .resource()
        .as_ref()
        .and_then(|res| res.ctx.as_ref())
        .map(|ctx| &ctx.hats[region].data_routes)
    {
        Some(data_routes) => get_or_set_route(
            data_routes,
            tables.data.hats[region].routes_version,
            &src_face.region,
            node_id,
            compute_route,
        ),
        None => compute_route(),
    }
}

#[inline]
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
```

`compute_data_route` 的 Hat 实现会把表达式相交结果与当前拓扑匹配，产出若干 `Direction`。`RouteBuilder` 以 `dst_face.id` 去重；不同 Hat 的候选方向最后合并成一个快照。此处不能把 key 简化成“只有 key expression”：不同 `src_face.region` 或 `map_routing_context` 结果可能允许不同方向。

下一层不是抽象的“找订阅者”，而是 HAT 按其拓扑规则选择出边。下面展示 router HAT 的完整固定方法：局部 helper 先把订阅节点索引映射成树方向，再验证方向、图节点和 Face；外层方法遍历表达式的相交 Resource 并合并出口。peer、client、broker 各有自己的 `compute_data_route`，不能把这个算法推广成全部模式的共同规则。

```rust
    fn compute_data_route(
        &self,
        tables: &TablesData,
        src_region: &Region,
        expr: &RoutingExpr,
        node_id: NodeId,
    ) -> Arc<Route> {
        #[inline]
        fn insert_faces_for_subs(
            this: &Hat,
            route: &mut RouteBuilder<Direction>,
            expr: &RoutingExpr,
            tables: &TablesData,
            net: &Network,
            source: NodeId,
            subs: &HashSet<ZenohIdProto>,
        ) {
            if net.trees.len() > source as usize {
                for sub in subs {
                    if let Some(sub_idx) = net.get_idx(sub) {
                        if net.trees[source as usize].directions.len() > sub_idx.index() {
                            if let Some(direction) =
                                net.trees[source as usize].directions[sub_idx.index()]
                            {
                                if net.graph.contains_node(direction) {
                                    if let Some(face) = this.face(tables, &net.graph[direction].zid)
                                    {
                                        tracing::debug!(dst = %face, dst.has_subscriber = true);
                                        route.insert(face.id, || {
                                            let wire_expr = expr.get_best_key(face.id);
                                            Direction {
                                                dst_face: face.clone(),
                                                wire_expr: wire_expr.to_owned(),
                                                node_id: source,
                                            }
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            } else {
                tracing::trace!("Tree for node sid:{} not yet ready", source);
            }
        }

        let mut route = RouteBuilder::<Direction>::new();
        let Some(key_expr) = expr.key_expr() else {
            return Arc::new(route.build());
        };
        let matches = expr
            .resource()
            .as_ref()
            .and_then(|res| res.ctx.as_ref())
            .map(|ctx| Cow::from(&ctx.matches))
            .unwrap_or_else(|| Cow::from(Resource::get_matches(tables, key_expr)));

        for mres in matches.iter() {
            let mres = mres.upgrade().unwrap();
            let net = self.net();
            let router_source = if *src_region == self.region() {
                node_id
            } else {
                net.idx.index() as NodeId
            };
            insert_faces_for_subs(
                self,
                &mut route,
                expr,
                tables,
                net,
                router_source,
                &self.res_hat(&mres).router_subs,
            );
        }
        Arc::new(route.build())
    }
```

路由输入的表达式若对应 Resource context，代码复用声明期保存的 `matches` 弱引用；若没有 context，则调用 `Resource::get_matches` 即时计算。随后从每个匹配 Resource 的 `router_subs` 取候选订阅节点，router network tree 用 `router_source` 和订阅节点索引选出下一条 `direction`，再找方向对应的 Face。更内层的 `insert_faces_for_subs` 将 `FaceState`、该 Face 的最佳 WireExpr 和转发 NodeId 写进 `Direction`。这是为什么 Route 是“按来源上下文选出的下一跳集合”，而非 `key -> socket` 的静态表。

RouteBuilder 以 `face.id` 作为去重键；对同一出口不会重复发相同 Route entry。`face.clone()` 复制 Arc 而不复制 FaceState，route 持有这个 Arc 到本次缓存失效/使用结束；`wire_expr.to_owned()` 则把借用表达式转成可随 Route 缓存存活的 `WireExpr<'static>`。如果树尚未给 source 建立完整索引，helper 不产生这条方向；这会表现为当前 route 没有该出口，后续拓扑变更通过版本失效要求重算。

### Route cache 的写锁实际包住 miss 计算

`RwLock` 是读写锁：只读查询可以共享读 guard，更新要取得独占写 guard；同一时刻写 guard 会排除该缓存的其他读者和写者。它是同步锁而不是 async mutex，调用线程拿不到 guard 时不能通过 `.await` 让出；标准库实现可能短暂自旋，也可能让线程进入阻塞等待，后者才由 OS 调度器切换去运行其他 runnable 线程。这里尤其要分清两把锁：`route_data` 在 `TablesLock.tables` 上持有读 guard，而 `get_or_set_route` 再对某个 Resource 的 route cache 取得独立写 guard。miss 时并没有拿 Tables 写锁，但 route compute 确实在 Resource cache 写锁内。

```rust
pub(crate) fn get_or_set_route<T: Clone>(
    routes: &RwLock<Routes<T>>,
    version: RoutesVersion,
    region: &Region,
    node_id: NodeId,
    compute_route: impl FnOnce() -> T,
) -> T {
    if let Some(route) = routes.read().unwrap().get_route(version, region, node_id) {
        return route.clone();
    }
    let mut routes = routes.write().unwrap();
    // NOTE(regions): we supposedly re-read the routes here because they might've changed, but I'm
    // not sure this is true given that all callers would've acquired `TablesLock::tables`.
    if let Some(route) = routes.get_route(version, region, node_id) {
        return route.clone();
    }
    let route = compute_route();
    routes.set_route(version, region, node_id, route.clone());
    route
}
```

第一轮 `read()` miss 后读 guard 释放，代码才会取得 `write()`。第二轮检查解决的是竞争填充：若另一个线程在本线程等待期间先写好了同一项，当前线程复用它；若仍没有，当前线程持着独占 cache guard 调 `compute_route()` 再写入。因而同一个 Resource cache 上，冷启动时多个 500 Hz 流同时 miss 会在这把写锁前排队，route 计算不会并行进行。代价是每个 miss 对这个 Resource cache 的其他读查询也形成短暂排斥；收益是没有重复构造相同 snapshot，也不会发生“先检查 miss、后到的旧写覆盖新值”。

锁层次仍须分开理解：route_data 的 Tables 读 guard 保证本次路由计算看到一致的拓扑视图，并阻止声明线程同时修改 Tables；Resource cache 写 guard 只序列化该缓存的命中/填充。计算不是 Tables 的全局写事务，且因 Tables 读 guard 存在，同一时刻另一线程不能取得 Tables 写 guard去改这份拓扑。route_data 后续会先做出口筛选，再释放 Tables guard，最后调用 Face 的发送方法。`Arc<Route>` 留存了 `Arc<FaceState>`，所以并发撤销不会让当前调用悬空；但活着的 Face 对象不等于网络仍连通，发送仍可能失败。

## HAT 如何从表达式构造转发计划

可以把 Route 理解为：

```rust
struct RouteEntry {
    destination: Arc<FaceState>,
    wire_expr: DestinationWireExpr,
    context: RoutingContext,
}

type Route = Vec<RouteEntry>;
```

具体字段更复杂，但核心是已经完成匹配、去重和目的表达式计算的发送计划。

缓存 immutable Arc 对象比在热路径共享可变 vector 更容易并发读取。

源码中的最小真实下一跳对象并不含教学例子中的 `RoutingContext` 字段，而是固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `Direction`：

```rust
#[derive(Clone, Debug)]
pub(crate) struct Direction {
    pub(crate) dst_face: Arc<FaceState>,
    pub(crate) wire_expr: WireExpr<'static>,
    pub(crate) node_id: NodeId,
}

pub(crate) type Route = Vec<Direction>;
```

`dst_face` 是本 router 下一跳的 Face；`wire_expr` 是该 Face 的 scope 表里可解释的 key 表示；`node_id` 随 Push 写入扩展字段，让下一台 router 知道当前转发上下文。它不是机器人身份号，也不是最终 Subscriber ID。route 是 `Vec<Direction>`，构建完成后以 `Arc<Route>` 共享；向量内部不再由发送线程修改。

### 单目的地优化

只有一个 destination 时，可以把原消息的 WireExpr 改写为目标 Face 需要的表示，再直接发送。

在修改前必须确认当前代码拥有 message，不能修改其他调用者共享的对象。实现通过 consume/ownership 标记判断是否需要 clone。

```text
one destination + owned message
  -> mutate wire expr in place
  -> send

borrowed/shared message
  -> clone before mutation
```

Rust 所有权帮助编译器阻止多个可变引用，却仍需业务层知道消息 buffer 与 metadata 哪些部分能够共享。

### 多目的地必须分别改写

不同 Face 对同一 key 的 scope mapping 可能不同：

```text
Face A: scope 5 + /state
Face B: scope 91 + /joint/1/state
Face C: full key expression
```

因此不能只改写一份消息后发送给全部目的地。通常为每个出口构造/clone 对应 network message，同时尽量让大 payload buffer 使用 Arc/共享 bytes，避免 D 次完整复制。

复杂度约为 `O(D)`，D 为 destination faces 数。Metadata clone 与 payload clone 是否复制底层字节取决于 buffer 类型。

### Egress Filter 位于每个目的地前

Route 选择后，每个出口还可经过 egress filter/interceptor。Ingress 回答“允许这条来源消息进入路由核心”，Egress 回答“允许它发往这只目标 Face”。

访问控制、桥接策略和区域限制可分别作用于入口与出口。

过滤器改变时也可能改变有效 route，设计上要么把动态判断保留在每次发送，要么使 cache key/version 包含策略版本。

## 缓存怎样随拓扑变化失效

任何影响目的地集合的变化都必须失效相关 route：

- Subscriber declare/undeclare；
- Face connect/close；
- topology/region 改变；
- resource mapping 改变；
- routing hat 状态改变；
- 影响静态路由结果的策略变化。

漏掉一次失效会继续向已关闭 Face 发送，或看不见新 Subscriber。过度全局失效则正确但增加重算成本。

### 局部失效与全局版本

声明只影响某个 key expression 及其 intersects resource 时，可以清这些节点的 cache。

拓扑级变化影响面广时，递增 `routes_version`：旧 cache 不必立即遍历删除，下一次 lookup 发现 version 不符后 lazy clear/recompute。

```text
local declaration change -> invalidate resource + match neighbors
face/topology change      -> routes_version += 1
```

这是精准失效与低成本 epoch invalidation 的组合。

### 数据路径锁边界

概念流程：

```text
Tables read lock
  -> resolve source scope
  -> construct RoutingExpr
  -> read/cache route
  -> clone Arc<Route>
release Tables lock

for destination in route
  -> rewrite WireExpr
  -> egress filter
  -> Face primitives send
```

发送到 transport 前释放全局 Tables lock，避免慢 transport queue 阻塞所有拓扑/声明更新。

上述固定摘录来自 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中的 `route_data`。两个发送分支都在调用 `send_push` 前释放 `rtables` guard；多出口分支先收集通过 inter-region 与 egress 检查的方向，再锁外逐个构造 Push 并发送。锁外发送不代表发送一定成功，实际发送仍可能被 congestion control 或 transport 状态拒绝。

## 发送策略的边界与最小复刻

Publisher 有几项相邻配置，但它们进入实现的路径不同。固定提交 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5` 中 `Session::resolve_put` 把 Priority、CongestionControl 和 Express 写入 Push 的 QoS 扩展：

```rust
        let ext_qos = push::ext::QoSType::new(priority.into(), congestion_control, is_express);
        let mut push = Push {
            wire_expr: wire_expr.to_owned(),
            ext_qos,
            ..Push::from(match kind {
                SampleKind::Put => PushBody::Put(Put {
                    timestamp,
                    encoding: encoding.into(),
                    #[cfg(feature = "unstable")]
                    ext_sinfo: source_info.map(Into::into),
                    #[cfg(not(feature = "unstable"))]
                    ext_sinfo: None,
                    #[cfg(feature = "shared-memory")]
                    ext_shm: None,
                    ext_attachment: attachment.map(Into::into),
                    ext_unknown: vec![],
                    payload: payload.into(),
                }),
                SampleKind::Delete => PushBody::Del(Del {
                    timestamp,
                    #[cfg(feature = "unstable")]
                    ext_sinfo: source_info.map(Into::into),
                    #[cfg(not(feature = "unstable"))]
                    ext_sinfo: None,
                    ext_attachment: attachment.map(Into::into),
                    ext_unknown: vec![],
                }),
            })
        };
```

`Push::from` 的默认部分填入其他扩展默认值；显式代码把优先级、拥塞控制和 express 位打包进 QoS，并把输入 `ZBytes` 转为 payload 所有权。它们对队列及发送的最终影响发生在下游。

`Reliability` 则在该固定提交中是 unstable API 参数：在 `resolve_put` 调用 Primitives 时单独传入，之后 `route_data` 的 `send_push` 闭包再把它交给目标 Face 的 Primitives。Publisher builder 自己也明确记载，这个值本身不触发网络重传：

```rust
    /// Changes the [`Reliability`](crate::qos::Reliability) to apply when routing the data.
    ///
    /// **NOTE**: Currently `reliability` does not trigger any data retransmission on the wire. It
    ///   is rather used as a marker on the wire and it may be used to select the best link
    ///   available (e.g. TCP for reliable data and UDP for best effort data).
    #[zenoh_macros::unstable]
    #[inline]
    pub fn reliability(self, reliability: Reliability) -> Self {
        Self {
            publisher: self.publisher.reliability(reliability),
            ..self
        }
    }
```

因此不能把 `Reliability::Reliable` 误写成 Publisher 已提供端到端应用确认，也不能把它理解成此路由层的重发状态机；它是一个传给下层的传输标记，如何选链路或获得传输可靠性需继续看相应 transport 实现。

这些选项必须按实际阶段观察：样本构造时怎样形成 QoS 扩展、Face primitive 怎样排队、transport 怎样发帧，以及接收端怎样处理。只看到 API 名称，推不出消息已入队、已写入 socket、更推不出机器人控制回调已经执行。

### 热路径性能模型

```text
Tput = Session/entity lookup
     + Push construction
     + source scope resolve
     + ingress filter
     + route cache lookup/miss computation
     + D * (wire rewrite + egress filter + queue send)
```

Cache hit 时路由查找接近常数索引与 Arc clone；通配声明 churn 会提高 invalidation 和 miss 频率。多目的地成本至少随 D 线性增长。

Payload 大小时关键是 clone 是否共享 bytes；Key churn 高时关键是 resource 匹配与 route cache，而不只是网络吞吐。

### 最小复刻顺序

先实现精确 key 的 session-local pub/sub：`HashMap<Key, Vec<Subscriber>>`。第二步加入 Primitives/Face 边界。第三步实现 KeyExpr intersects。第四步加入 immutable Route cache 与全局 version。第五步实现 per-Face scope/suffix 和多目的改写。

每一步验证：

- 声明在锁内提交、通知在锁外发送；
- Twin publisher 只有最后一个 drop 才 undeclare；
- 新 Subscriber 出现后旧 cache 不再使用；
- Face close 后 route 不持有逻辑可发送状态；
- 多目的地 WireExpr 各自正确，payload 不做无谓深拷贝。

### 设计边界

Zenoh Publisher 将本地实体生命周期、远端匹配状态 Interest 的生命周期和每条数据路由分成三层。Twin/aggregation 压缩控制面，WireExpr 压缩连接上的 key，版本化 Route 把复杂表达式匹配结果缓存成不可变转发计划。

它的性能来自复用与延迟计算，正确性则依赖精确 cache invalidation、锁外 I/O 和每个 Face 独立表达式上下文。
