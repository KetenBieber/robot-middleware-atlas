# Session 启动链：Runtime、模式编排与本地 Routing Face

设想一台仓库机器人启动时要连接 Router，同时让诊断进程能够立即查询它的电池状态。朴素做法是先开网络监听、等 transport 连上后再注册本地 Publisher 和 Queryable；这会留出一个窗口：远端的声明先到，当前进程的实体表和路由入口还不存在，系统只能延迟处理或丢弃这次状态变化。固定版本把 Runtime 构造、本地 Session/Face 初始化、网络启动排成可追踪的顺序，读者可以沿这条链确认对象何时真正可接收消息。

下面讨论的“线程阻塞”与“异步任务让出”不是同一件事。`Future` 是一份可轮询的未完成计算；执行器调用 `poll` 推进它，返回 `Pending` 时任务可以先挂起，让执行器去运行其他任务。操作系统调度的是执行器所在的线程，而不是每一个 Rust Future。若普通同步函数在执行器线程中等待，操作系统会把这条线程置为阻塞，线程上的其他任务也暂时不能运行。

## Session 的主要对象图

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Session
  `-- Arc<SessionInner>
        +-- GenericRuntime
        +-- RwLock<SessionState>
        |     +-- primitives
        |     +-- publishers/subscribers
        |     +-- queryables/queries
        |     `-- local resource declarations
        +-- Session TaskController
        `-- callback drop synchronization

Runtime
  `-- Arc<RuntimeState>
        +-- Gateway
        |     `-- TablesLock -> Resource tree / Faces / Routes
        +-- TransportManager
        +-- Runtime TaskController
        `-- handlers/configuration
```

Session state 管理应用实体；Runtime state 管网络、路由和拓扑。一个插件环境中的 Runtime 可以服务多个 Session，因此二者关闭时机不能简单绑定成同一个 Arc。

## OpenBuilder 的表面异步

公开 API 返回 builder：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let session = zenoh::open(config).await?;
```

固定版本中 [`OpenBuilder`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/session.rs#L135-L166) 同时实现同步 `Wait` 和 `IntoFuture`。它的 `into_future()` 会在返回 `Ready` 之前直接执行 `self.wait()`；因此 Session 打开期间的同步等待发生在调用这条语句的线程上，不能把这个 `.await` 当成必然让出线程的 I/O 等待。

因此 `.await` 是统一 API 外形，不足以证明初始化工作不会阻塞调用线程。阅读 async Rust 必须继续查看 Future 构造方式，而不是只看调用处有 `.await`。

## Runtime Build 与 Start 分离

`Session::new` 先调用 `RuntimeBuilder::build().await`，再调用 `Session::init(...).await`，最后才执行 `runtime.start().await`；对应调用顺序位于 [`Session::new`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L1451-L1480)。`RuntimeBuilder::build` 装配 Gateway、TransportManager 和 RuntimeState，`Runtime::start` 再按节点角色启动网络编排。概念顺序如下：

把关键控制流缩到最短，固定提交 `Session::new` 的连续源码是：

**代码身份：固定提交源码摘录；eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5，`zenoh/src/api/session.rs`，`Session::new`，L1470–L1479。**
```rust
            let mut runtime = runtime.build().await?;
            let session = Self::init(
                runtime.clone().into(),
                aggregated_subscribers,
                aggregated_publishers,
            )
            .await;
            runtime.start().await?;
            Ok(session)
```

先有 `Runtime`，再把 clone/转换后的 Runtime owner 交给 `Session::init`；`init` 返回 Session 后才 start listener、connect 和 scouting。若 `start` 返回错误，`?` 让错误向上返回，因此调用者拿不到半启动的 Session handle。由这一顺序可以推导出“先准备本地路由接收方、再开放网络入口”的设计理由；这是根据调用顺序作出的工程解释，不是提交中的作者注释。

**图示身份：概念、状态或调用链示意，不是源码。**
```text
RuntimeBuilder::build
  -> validate Config
  -> create HLC/time state
  -> create routing Gateway and Tables
  -> create TransportManager
  -> assemble RuntimeState
  -> connect handlers with WeakRuntime
  -> initialize routing hats/admin/plugins

Runtime::start
  -> bind listeners
  -> connect configured endpoints
  -> start scouting/autoconnect tasks
  -> wait configured readiness conditions
```

分阶段让 Session 能在真正网络事件发生前注册本地 routing face。`RuntimeBuilder::build` 在 [`runtime/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/mod.rs#L729-L860) 是 async builder，会装配 Gateway、TransportManager、RuntimeState 并等待 transport manager builder；“build 在 start 前”描述的是对象阶段，不代表 build 阶段完全没有异步等待。模式分发由 [`Runtime::start`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/orchestrator.rs#L168-L174) 执行。

## Session::init 建立本地 Face

[`Session::init`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L864-L904) 创建 SessionState 与 SessionInner，向 Runtime 注册 connectivity handler，再通过 Runtime/Gateway 创建一组指向本地 Session 的 primitives。`Session::new` 在随后才启动 listener/connect/scouting，因此网络入口打开时本地 Face 已经能承接早到的远端声明。

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Runtime routing core
  <-> local Face
       <-> WeakSession primitives
            <-> SessionState entities/callbacks
```

之后 Session 把 primitives 存入 state。Publisher/Subscriber 声明只有在 primitives 存在时才能进入路由核心。

`WeakSession` 这个名字容易造成错误直觉：固定提交里它不是 `std::sync::Weak`。它内部用 `ManuallyDrop<Session>` 持有同一份 `Arc<SessionInner>`，但不增加“公开 Session handle”专用的 `strong_counter`；因此它不阻止最后一个公开 Session 触发 close，却仍会让 Arc 内存暂时存活。源码注释说明 primitives 需要在 close 操作期间可用，所以实现容许 Session 内部存在引用环，再由 [`Session::close`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L3704-L3763) 清理该环。具体布局、drop 与设计注释见 [`WeakSession`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L801-L847)。这里必须分开“何时认为 Session 应关闭”和“Arc 何时能回收内存”。

## 启动顺序保护早到网络事件

整体顺序为：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
build Runtime objects
  -> init Session and local Face
  -> store Session primitives
  -> start Runtime listeners/connect/scouting
  -> return Session
```

若先启动 transport，再创建本地 Face，远端 declaration 可能在窗口期到达，却没有完整 Session/Gateway 状态可处理。

这是“先装消费者，再打开生产入口”的通用并发规则。类似原则也适用于启动接收线程前先建队列与 callback registry。

## Runtime 模式决定网络编排

Zenoh node 可以是 client、peer 或 router。三种模式共享 routing core，却采用不同连接策略。

### Client

Client 通常向已知 peer/router 建 north-bound transport。配置显式 endpoint 时直接连接；启用 scouting 时可发现候选节点。其分支顺序与“无 peer 且 scouting 关闭时报错”的具体行为见固定实现 [`start_client`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/orchestrator.rs#L176-L245)。

若 multicast scouting 关闭且没有配置 peer，client 没有任何连接目标，启动返回错误。

Client 还可能限制同时存在的 north-bound transport 数，避免无意形成复杂转发拓扑。

### Peer

Peer 可以监听入站连接、主动连接配置端点并参与 scouting。它既能作为数据端点，也能在特定拓扑中转发；具体入口为 [`start_peer`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/orchestrator.rs#L247-L287)。

配置可以要求等待发现/连接条件后再认为 start 成功。超时有时是错误，有时只记录 warning 后继续，必须按具体 return condition 区分。

### Router

Router 主要承担跨连接路由，通常启动 listener、连接已知 peer、开启 scouting，并等待配置的启动延迟/条件；分支实现在 [`start_router`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/orchestrator.rs#L288-L324)。

它需要更完整的 routing tables、Face 和 hat/region 状态，资源规模与连接数更相关。

## Listener 与 Connect 是两类方向

**图示身份：概念、状态或调用链示意，不是源码。**
```text
listener: 等远端主动连接当前节点
connector: 当前节点主动连接远端 endpoint
```

两者都由 TransportManager 创建 transport，随后触发 Runtime transport event handler。启动成功条件可以要求至少一个 listener 成功、至少一个 endpoint connected，或允许后台持续 retry。

错误处理要区分：立即配置错误、单 endpoint 失败、全局 timeout 和允许重试的暂时失败。

## Scouting 是发现候选传输端点

Runtime 内建 scouting task 可：

- 监听并响应 scout；
- 主动发送 scout；
- 根据 Hello 自动连接匹配节点；
- 同时运行 responder 与 autoconnect。

任务由 Runtime TaskController 管理，所以 Runtime close 能统一取消。

公开独立 `zenoh::scout()` 不创建完整 Runtime。它建立自己的 socket/task 和 cancellation token，drop 时终止 scout task。二者生命周期不要混为一谈。

## TaskController 表达任务归属

直接 `tokio::spawn` 后丢掉 JoinHandle，会让关闭无法知道任务是否仍访问 state。这里的 task 是执行器管理的 Future；多个 task 可以轮流占用同一 OS 线程，task 进入等待后由执行器重新安排，而不是每个 task 都拥有一条线程。`TaskController` 通过 `TaskTracker` 和 `CancellationToken` 将任务归属到 owner，具体字段与 `spawn[_abortable]`/终止路径见 [固定提交的 `TaskController`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/commons/zenoh-task/src/lib.rs#L30-L146)：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Session TaskController
  -> query timeout
  -> session-local background work

Runtime TaskController
  -> scouting
  -> reconnect
  -> transport/runtime loops
```

Owner close 时先禁止/取消新任务，再等待现有任务终止。Session 可以先关自己的 timeout task，而共享 Runtime 继续服务其他 Session。

## Transport 建立后创建 Face

TransportManager 的新 unicast transport callback 最终会到 [`Gateway::new_transport_unicast`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/gateway.rs#L264-L355)：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
upgrade WeakRuntime
  -> notify optional transport handlers
  -> determine remote role/region/bounds
  -> Gateway::new_transport_unicast
       -> create FaceState
       -> create interceptors
       -> create Mux and DeMux
       -> register face with routing hat
       -> accumulate declarations to send
  -> release table/control locks
  -> send declarations
```

Face 将一条 transport connection 纳入路由拓扑。它保存该邻居的 resource mappings、interceptors 和 pending request state。

最能说明锁边界的是函数末尾的固定提交代码：Face 和 routing hat 已经更新表之后，先释放 Tables 写锁与 control lock，再逐项发送累积的声明。

**代码身份：固定提交源码摘录；eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5，`zenoh/src/net/routing/gateway.rs`，`Gateway::new_transport_unicast`，L343–L347。**
```rust
        drop(wtables);
        drop(ctrl_lock);
        for (p, m) in declares {
            m.with_mut(|m| p.send_declare(m));
        }
```

这些锁保护的是表内拓扑更新，不覆盖声明发送。若 `send_declare` 阻塞或回入路由逻辑，全局写锁仍被持有就会把其他声明/Face 变更一起挡住。这里的动机是从源码可见的锁释放顺序得出的工程推导。

## Control Lock 与 Tables Lock

Face 创建会修改多组路由状态。`ctrl_lock` 串行拓扑控制操作，Tables 写锁保护资源树和 Face map。源码中这些锁保护的是路由拓扑不变量；网络发送放到释放锁之后执行，避免队列等待或回调重入时把全局表锁一起占住。

声明消息在释放这些锁后发送。原因是发送可能进入 transport queue、触发 callback，甚至间接回入 routing core。持全局路由锁执行外部操作会构造重入死锁。

通用模式为：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let commands = {
    let _control = ctrl_lock.lock();
    let mut tables = tables.write();
    update_state_and_build_commands(&mut tables)
};
send_commands(commands); // no routing locks held
```

## Mux 与 DeMux 接入消息流

Mux 是 routing-to-transport：路由结果通过目标 Face 的 Mux 发送。

DeMux 是 transport-to-routing：收到 wire message 时，根据消息类型调用声明、push、request、response 等路由入口。

**图示身份：概念、状态或调用链示意，不是源码。**
```text
remote transport
  -> RuntimeSession::handle_message
  -> DeMux
  -> Face route method
  -> Tables/Resource/Route
```

Link up/down 和 transport close 也会触发路由状态变更与 cache invalidation。

## Session Clone 与逻辑 Strong Counter

`Session` 外层由 Arc 共享，但项目还维护逻辑 strong counter，区分公开 Session clone 与内部 Arc 引用。固定实现中 `Clone` 用 `fetch_add(1, Ordering::Relaxed)`，`Drop` 用 `fetch_sub(1, Ordering::Relaxed)` 判断是否为最后一个公开 handle；内存仍由 `Arc` 控制块管理，这个额外计数只决定何时调用 `close()`，并不保护 `SessionState` 的并发访问。定义见 [`SessionInner`、`Clone` 与 `Drop`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L693-L795)。

原因是内部 callback/task 可能需要 Arc 保证内存安全，却不应因此阻止“最后一个用户 Session handle”触发 close。

**图示身份：概念、状态或调用链示意，不是源码。**
```text
Arc strong count = public handles + internal implementation refs
logical counter  = public handles that own open session semantics
```

最后一个业务 handle drop 时执行 close；内部 Weak/Arc 的存在不改变用户可见生命周期。

这与 C++ 插件中“对象引用计数”和“库租约计数”类似：一个引用计数无法表达两种不同业务含义。

## 启动失败的清理

可能失败的阶段包括配置解析、listener bind、peer connect、scout socket、plugin/admin init 和 start condition timeout。

一旦 Runtime/Session state 已创建，失败返回前要关闭已启动 task/transport，并清理本地 Face。Rust RAII 能释放内存，但后台 task 和 socket 的有序 async close 仍需显式执行。

Builder/Start 最好返回 guard，只有所有阶段成功后 commit 为 Running；失败时 guard drop/rollback 关闭已经登记的资源。

## 启动的性能与可用性语义

`open()` 延迟可能包含：

- 配置解析与插件装配；
- listener bind；
- DNS/endpoint connect；
- scouting interface bind；
- 等待 peer/start condition；
- 固定启动 delay。

Open 返回成功的含义也取决于模式和配置：Runtime 对象已启动，不一定已经连到所有期望远端。应用需要 matching listener 或 connectivity status 区分“Session 可用”和“目标数据路径已经匹配”。

## 最小 Runtime 复刻

第一版只实现 session-local routing：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
struct SessionInner {
    state: RwLock<SessionState>,
    tasks: TaskController,
    closing: AtomicBool,
}
```

第二版增加 `Primitives` trait 和 local Face，让 Session 不直接访问 router。第三版加入一条 loopback transport 与 Mux/DeMux。第四版才实现 client listener/connect/scouting 状态机。

每一步验证：锁内只提交状态，锁外调用 primitives；Weak 回调升级失败安全返回；Close 能取消并等待 owner tasks。

## Session 启动设计结论

Zenoh 将对象构建、Session 本地 Face 初始化和网络 Runtime 启动分开，使 transport 事件出现前路由消费者已经就绪。Client/Peer/Router 共享底层对象，却由 orchestrator 采用不同 listener、connect 和 scouting 策略。

最值得迁移的能力是：公开 handle 生命周期与内部内存引用分离、后台任务归属于明确 owner、全局路由锁内只生成命令而不执行外部 I/O。
