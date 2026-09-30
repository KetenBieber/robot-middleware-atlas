# Zenoh 阅读基础：发布、订阅与查询共享同一片数据空间

假设一台移动机器人每 10 毫秒产生一次位姿，前方相机持续输出图像，诊断工具偶尔询问电池和电机温度，边缘服务器还要保存部分历史数据。它们的时间行为不同：控制器需要持续收到新状态，诊断工具只在需要时发问，存储服务则关心稍后还能不能找到数据。

这些功能却在描述同一件事：机器人有哪些数据，这些数据叫什么。Zenoh 最值得先理解的地方，是让发布订阅和查询回复围绕同一套层级名字工作，再由运行时决定数据应留在本机，还是穿过一个或多个网络连接。保存历史值还需要应用或存储插件实际接收并写入数据；仅仅打开 Session 不会自动把 Sample 变成持久记录。

源码基线固定为 Zenoh 提交 [`9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5`](https://github.com/eclipse-zenoh/zenoh/tree/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5)。Zenoh 的基本模型由 Key Expression、publish/subscribe、query/reply 与 Session 组成；路由表、Face、WireExpr、`Arc` 和异步任务则负责把这些上层动作落实为具体运行时状态。

## 机器人数据首先需要一套稳定的名字

可以先给数据取一组不依赖机器地址的名字：

```text
robot/17/pose
robot/17/battery
robot/17/camera/front
robot/17/diagnostics/motor
```

这些不是 IP 地址，也不是某个进程里的 C++ 变量名。它们描述数据在机器人系统中的逻辑位置。相机进程从车载计算机迁移到另一台主机后，`robot/17/camera/front` 仍可以保持不变；但新主机仍须加入可达拓扑，发布端和接收端也仍须通过配置、发现或 Router 建立路由关系。名字稳定不等于连接自动建立。

朴素系统也可以分别使用 topic 名、RPC 服务名和数据库路径。但随着机器人跨进程、跨设备、跨边缘部署，同一份数据会在三套系统中拥有三种名字，桥接程序还要维护发现、权限和生命周期。Zenoh 的统一命名减少的是这种集成边界，并不自动决定一致性、访问权限或存储策略。

因此，把 Zenoh 简单等同为“另一个 topic 总线”会遗漏一半设计动机。它确实支持持续发布订阅，但同一命名空间还可以承载临时查询和其他数据访问方式。

## Key Expression 既能指向一个 key，也能描述一组 key

Zenoh 把这套名字称为 key；能够包含通配规则的表达式叫作 **Key Expression**，本质上是“能够描述一组数据地址的表达式”。在这个固定提交中，表达式必须是规范形式的 UTF-8、按 `/` 分段、不能有空段、不能以 `/` 开始或结束，也不能包含 `//` 或 `# $ ?`；构造器 `keyexpr::new` 拒绝非规范输入，`autocanonize` 会先原地规范化再验证。约束来自 [`keyexpr` 定义和构造器](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L32-L90)。因此，来自配置文件或机器人 ID 的字符串不能只靠“看起来像路径”就直接拼接进 key。

先看一个具体 key：

```text
robot/17/joint/1/state
```

它指向 17 号机器人 1 号关节的状态。若监控程序希望观察 17 号机器人的任意一个关节，可以写：

```text
robot/17/joint/*/state
```

`*` 匹配一个路径片段。若要覆盖更深的层级，可以写：

```text
robot/**/temperature
```

`**` 可以跨越多个 `/` 分隔的片段，所以它能描述机器人命名空间下不同深度的温度数据。

这时，表达式就不再只是普通字符串，而是在表示一组可能的具体 key。源码中常见的 `intersects` 和 `includes` 都来自集合关系。

### 相交表示两组 key 至少有一个共同成员

```text
robot/*/pose
robot/17/*
```

两个表达式并不相等，却都能匹配 `robot/17/pose`，因此它们相交。发布与订阅、查询与 Queryable 的匹配通常关心这种关系。

### 包含表示一个表达式覆盖另一个表达式的全部范围

```text
robot/**  includes  robot/17/pose
```

左边覆盖右边表示的具体 key。这个判断不能用普通字符串前缀代替：`robot/1` 虽然是 `robot/17` 的文本前缀，却不是它的路径父级。

因此，Zenoh 路由不能只做一次 `unordered_map<string, subscribers>` 精确查表。它还要按照统一的 keyexpr 规则计算表达式关系。后续组件地图会说明 Resource 如何组织这些名字，Publisher 路由章则会追踪匹配结果怎样变成一组目的地。

## 四种常用动作拥有不同的时间语义

同一套名字不表示所有操作都变成同一种消息。对初学者而言，先区分四个动作最重要：

| 应用意图 | Zenoh 动作 | 时间上的直觉 |
|---|---|---|
| 写入一份新数据 | `put` 或 Publisher | 现在产生一份值 |
| 持续观察后续更新 | Subscriber | 从声明以后接收匹配数据 |
| 临时询问已有答案 | `get` 或 Querier | 发出问题，接收零到多条回复 |
| 声明自己能够回答 | Queryable | 收到匹配问题后产生回复 |

发布订阅适合持续数据流。例如里程计不断向 `robot/17/pose` 写新位姿，控制器订阅这个 key 以获得后续更新。

查询更像一次面向数据空间的提问。诊断程序可以查询：

```text
robot/17/**/temperature
```

电池模块、电机模块和历史存储都可能声明了匹配的 Queryable。一次 `get` 因此可能没有回复，也可能收到来自多个位置的多条回复。它不是默认只对应一个服务端的 request/response socket。

“零条回复”和“查询超时”也不是同一个概念。前者可能表示匹配分支正常结束但没有数据，后者表示调用者等到期限仍未观察到完整收口。具体怎样记录分支、回复和结束信号，会在 Query 生命周期章中沿源码展开。

## Session 是应用进入数据空间的入口

Zenoh 应用通常从下面这行开始：

```rust
let session = zenoh::open(config).await?;
```

可以按执行顺序拆开 Rust 语法：

- `zenoh::open(config)` 创建一次打开操作，其中 `config` 描述节点模式、监听和连接等配置；
- 对一般 Future，`.await` 会把控制权交给执行器；若 Future 暂时不能完成，当前任务可以让出执行机会，但这不会新建一条操作系统线程。这个 Builder 是固定提交中的例外：`OpenBuilder::into_future` 先同步调用 `self.wait()`，再把结果放进已就绪的 `Ready` Future，所以首次 `zenoh::open(config).await` 的初始化可能直接阻塞正在执行该语句的线程。这个结论来自 [`OpenBuilder::wait` 与 `IntoFuture`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/session.rs#L135-L166)，不能仅凭 `.await` 外观推断是否会 yield；
- `?` 表示失败时立即把错误返回给上层，成功时取出 Session；
- `let session =` 给得到的 Session 句柄起一个局部名字。

固定版本的 [`Session` 文档与定义](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L693-L760) 明确把它描述为 Zenoh 的主要组件：它持有 Zenoh runtime，并允许声明 Publisher、Subscriber、Querier 和 Queryable。

Session 不是一条 TCP connection。它更像应用进入这片数据空间的一张工作台：应用在上面声明实体，而它背后可以共享一个 Runtime，并维护本地路由和一个或多个 transport。Session 打开成功，也不能单独证明某个特定远端此刻可达。

最小对象图先保留到这一层即可：

```text
Application
    |
    v
Session
    +-- Publisher / Subscriber
    `-- Querier / Queryable
             |
             v
       Zenoh routing
          /      \
     local        remote connection(s)
```

在后面的源码中，Session 会继续展开成内部状态、任务控制器和本地路由入口，但现在只需记住：它管理的是一组实体和运行时关系，不是单个远端 socket。

## 第一段发布代码只完成打开、声明和写入

一个持续发布关节状态的最小片段如下：

```rust
let session = zenoh::open(config).await?;

let publisher = session
    .declare_publisher("robot/17/joint/1/state")
    .await?;

publisher.put(payload).await?;
```

第一行已经解释过。第二段调用 [`Session::declare_publisher()`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L1199-L1228)，它先产生一个 Builder。可以暂时把 Builder 理解成“尚未提交的配置单”：key expression、拥塞控制、优先级等选项先在局部对象中收集，最后的 `.await?` 才请求创建 Publisher。

第三行通过已经声明的 Publisher 写入 payload。对于持续高频数据，这样做的意义是复用已经固定的 key 与发送配置，而不是每次重新描述同一实体。低频或一次性写入也可以直接使用 Session 上的 `put`。

到这里不要急着把 `.await` 翻译成“后台线程发送”。Rust 的异步操作只有被执行器持续 poll 才会推进；等待期间能否让出、内部是否还有同步步骤，要看具体 Builder 和调用链。语言设计章会把 Future、task、executor 和阻塞边界拆开解释。

## Subscriber 把后续样本交给处理逻辑

订阅端可以声明一组感兴趣的 key：

```rust
let subscriber = session
    .declare_subscriber("robot/*/pose")
    .await?;

while let Ok(sample) = subscriber.recv_async().await {
    handle_pose(sample);
}
```

`while let Ok(sample)` 的普通中文含义是：只要异步接收继续成功返回 Sample，就执行一次循环；当接收端关闭或返回错误时，模式不再匹配，循环结束。

Publisher 的表达式和 Subscriber 的表达式相交时，这份数据才有机会到达该订阅者。若双方位于同一本地运行时，路径可以停留在本地路由；若位于不同节点，路由结果还要进入相应 transport。

一条消息的第一版路径可以压缩为：

```text
publisher.put(payload)
  -> Publisher 保存的 key 与策略
  -> Session 的本地入口
  -> Zenoh 路由选择本地或远端目的地
  -> Subscriber handler / receive channel
```

这一页故意不在箭头之间塞入所有内部类。下一章会先给完整组件地图；Publisher 路由章再逐层加入 Face、Resource、WireExpr、Primitives、Mux 和 Transport，并解释每多一层究竟隔离了什么变化。

## Rust 类型把对象寿命写进了接口

后续源码会大量出现“拥有、借用、共享”。它们不是语言课里的孤立术语，而是在回答 payload、Session 和后台任务能活多久。

| 阅读标签 | 普通中文直觉 | 需要警惕的问题 |
|---|---|---|
| 拥有 | 当前值负责让对象活着，并最终释放它 | 所有权转移以后，旧变量不能继续当作有效 owner |
| 借用 | 暂时查看或修改，但不负责释放 | 借用不能比被借对象活得更久 |
| 共享 | 多个实体或任务共同延长同一个对象寿命 | 引用计数安全不等于内部字段自动线程安全 |

例如，复制一个 Session handle 通常不会复制整套网络运行时；多个 handle 会共享内部 Session 对象。Publisher 离开作用域时，应撤销自己的实体语义，却不能顺便销毁仍被 Subscriber 或其他 Session handle 使用的 Runtime。

Rust 常用 `Arc<T>` 表达线程安全的共享所有权，用 `Weak<T>` 表达不负责保活的回指，用 `RwLock<T>` 协调共享状态读写。这里先记住设计目的即可：避免仍在使用的对象提前释放，也避免后台任务反过来永久保活整个 Session。它们的语法、锁 guard、`Send/Sync` 以及 C++ `shared_ptr/weak_ptr` 对照，集中放在 Rust/C++ 设计章中。

## 接收者跟不上时必须选择数据语义

假设相机以 60 Hz 产生图像，而视觉算法只能处理 20 Hz。系统无法同时满足“每帧都处理”“内存有上限”“延迟永不增长”三个目标，必须选择策略：

- 让生产路径等待，压力可能向上游传播；
- 使用有界 FIFO，容量用完后拒绝或丢弃新工作；
- 使用 Ring，让新帧覆盖旧帧，优先保证新鲜度；
- 为关键命令使用不同的可靠与确认设计，而不是照搬相机策略。

Zenoh 中需要分清三个独立决策面：应用 handler 的交付队列、transport transmission pipeline 的拥塞控制，以及消息携带的 Reliability 标记。它们不是同一只队列，也不能相互替代。

在本文固定提交中，[`PublicationBuilder::reliability`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/publisher.rs#L146-L165) 的文档明确说明：设置 `Reliable` 本身不会触发 wire retransmission；该值会作为消息标记，并可帮助运行时选择更合适的传输链路，例如为 reliable 数据选择 TCP、为 best-effort 数据选择 UDP。因此，代码里出现 `Reliable` 不能直接推出端到端不丢、不重或不乱，更不能证明应用处理了每条 Sample。

对机器人状态流，过长积压可能比丢弃更危险，因为控制器真正关心的是最新状态；对命令、事件和审计数据，静默覆盖又可能不可接受。队列与交付策略必须跟随数据语义，而不是由一个全局默认决定。

## 统一命名带来能力，也带来实现成本

Zenoh 的优势可以沿同一条主线理解：Key Expression 让一条声明覆盖一组数据；发布订阅与查询共享名字，减少跨系统桥接；本地和远端路径可以进入同一路由模型；连接上还可以压缩反复出现的长 key。

这些能力也带来明确代价：

- 通配表达式匹配比精确 topic 查表更复杂；
- 声明或拓扑变化后，缓存的路由答案必须正确失效；
- 一次 Query 可能扇出到多个回答者，需要明确的完成与超时语义；
- 应用队列、transport 拥塞和链路行为会共同影响最终交付；
- Session、实体、异步任务和 transport 共享运行时，关闭不只是释放一个局部变量；
- Rust 核心与 C/C++ 应用之间还要维护 FFI、所有权和错误边界。

Zenoh 因此并不天然适合每一个系统。若需求只是单机、固定拓扑和少量精确 topic，更小的总线可能更容易部署和验证；若需要跨边缘路由、统一查询、动态数据空间和多种链路，Zenoh 的额外结构才更有价值。

它也不是硬实时调度器。异步任务、路由匹配、缓存失效、内存分配和操作系统网络栈都会影响尾延迟。用于控制闭环时，应测量数据年龄、最坏延迟、队列水位和丢弃位置，而不只看平均吞吐量。

## 后续章节按问题选择，而不是继续背名词

第一次读到这里，只需保留四个认识：Zenoh 围绕 Key Expression 组织数据；发布订阅与查询共享名字但时间语义不同；Session 是一组实体进入运行时的入口而非单连接；过载行为取决于多个独立层次。

接下来可以按问题进入对应章节：

| 当前想解决的问题 | 后续页面 |
|---|---|
| 完整系统有哪些组件，它们怎样分层 | 《功能与组件地图》 |
| `zenoh::open()` 创建了哪些对象，关闭时怎样收口 | 《Session 启动与运行时装配》 |
| 一次 `put` 怎样匹配目的地并进入 transport | 《Publisher 声明与数据路由》 |
| Resource、Face、WireExpr 与 route cache 怎样配合 | 《Resource Tree 与路由缓存》 |
| `get` 为什么可能多回复，什么时候真正完成 | 《Query、Reply 与 Final 生命周期》 |
| `Arc`、`Weak`、Builder、RAII 与 C++ FFI 怎样对应 | 《Rust/C++ 设计映射》 |
| rmw_zenoh 怎样把 ROS 2 语义映射到 Zenoh | 《真实项目案例：rmw_zenoh》 |

进入任何一条源码链时，都先问三个问题：数据现在在哪里，谁让相关对象继续活着，下一步由谁执行。Face、锁、队列和异步任务会逐步加入这张图，但读者始终可以回到这三个问题，不必一次记住整个 Zenoh 内部世界。
