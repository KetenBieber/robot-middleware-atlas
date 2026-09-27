# 异步背压与关闭协议：队列、任务和资源的终止顺序

中间件在正常数据流上工作，并不意味着它在慢消费者、网络中断和进程退出时也正确。Zenoh 把这类问题分布在三个层次：本地 handler 队列决定回调来不及消费时怎么办，网络 QoS 决定传输拥塞时怎么办，TaskController 与 close 状态机决定后台工作如何停止。

这三层经常被笼统称为“背压”，但它们的作用位置和失败语义不同。把它们分开，才能判断一条数据究竟阻塞在应用队列、路由发送还是可靠传输窗口。

本文的源码版本固定为 `eclipse-zenoh/zenoh@9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5`；真实代码段均直接从工作区内的本地 checkout `source-audit/zenoh` 核对与摘录。教学最小例子和真实源码会分别标明，不依赖外部代码链接才能阅读。

## 本地 handler 把 callback 适配成 Receiver

Zenoh 的 Subscriber、Queryable 和 Reply 接口既支持 callback，也支持接收器。接收器形式在内部仍然是 callback，只是 callback 将事件写入一个 channel：

```text
中间件分发线程
  -> Callback<T>
       -> channel.send(T)
            -> 应用 recv_async()
```

这意味着“默认接收队列”不是网络 socket 缓冲，而是进程内、位于中间件 callback 与应用消费代码之间的一层。

使用接收器可以让应用按自己的异步任务节奏读取，但也引入容量问题。若生产速度长期高于消费速度，系统只能在三种行为中选择：阻塞生产者、丢弃旧数据或丢弃新数据。无限队列只是把选择推迟为最终内存耗尽。

## FIFO channel 用阻塞保留每个事件

FIFO handler 使用有界 channel。概念代码如下：

```rust
fn fifo_handler<T>(capacity: usize) -> (Callback<T>, Receiver<T>) {
    let (tx, rx) = flume::bounded(capacity);
    let callback = Callback::from(move |value| {
        tx.send(value).ok();
    });
    (callback, rx)
}
```

当队列满时，`send` 等待空间。它提供完整、按序的事件流，但阻塞会沿 callback 调用栈反向传播：

固定提交的 `FifoChannel::into_handler` 将 sender 移入 Zenoh 执行的回调闭包，同时把 receiver 交给应用。实际的有界入队不是异步等待，而是同步调用 `send`：


```rust
fn into_handler(self) -> (Callback<T>, Self::Handler) {
    let (sender, receiver) = flume::bounded(self.capacity);
    (
        Callback::from(move |t| {
            if let Err(error) = sender.send(t) {
                tracing::error!(%error)
            }
        }),
        FifoChannelHandler(receiver),
    )
}
```

`move` 将 sender 的所有权转交分发侧，应用持有 `FifoChannelHandler(receiver)`。`flume::bounded` 限制容量，`sender.send(t)` 在队列满时同步阻塞，等待直接传回执行当前 callback 的线程；若 receiver 已经析构则返回错误并记录日志。这种阻塞不会因为 Zenoh 在其他位置使用 async runtime 而自动消失。

```text
应用消费慢
  -> FIFO 满
  -> callback 阻塞
  -> 当前 dispatch task 无法继续
  -> 同一执行上下文中的其他实体延迟上升
```

因此，有界 FIFO 的容量不是越大越好。容量大可以吸收更长的短时突发，但会增加最坏排队延迟和内存。若每条消息平均占 `S` 字节、容量为 `N`，只计算 payload 就至少需要约 `N × S`，还未包括对象、Arc 和 channel 元数据。

适合 FIFO 的场景是命令、事务事件、状态变更日志等“每一条都有意义”的数据。若应用无法跟上，阻塞本身就是需要暴露的系统异常。

## Ring channel 用有限历史保持实时性

Ring handler 将固定容量视为循环窗口。满载后，新数据继续进入，同时覆盖或淘汰旧数据：

```text
capacity = 3

写入 A B C -> [A B C]
写入 D     -> [B C D]   // A 被淘汰
```

它牺牲完整历史，换取生产路径不因慢消费者长期阻塞。传感器姿态、实时位姿和最新健康状态通常更适合这种语义，因为应用恢复后最关心的是最新值，而不是逐条处理已经过时的一秒历史。

环形队列仍需同步保护其读写索引，固定版本实现通过互斥保护 ring 状态。锁持有时间应只覆盖索引移动与槽位替换，不能在锁内运行用户 callback 或等待异步 I/O。

固定实现还将样本存储与唤醒通知分离。下面是 `RingChannel::into_handler` 连续源码：


```rust
fn into_handler(self) -> (Callback<T>, Self::Handler) {
    let (sender, receiver) = flume::bounded(1);
    let inner = Arc::new(RingChannelInner {
        ring: std::sync::Mutex::new(RingBuffer::new(self.capacity)),
        not_empty: receiver,
    });
    let receiver = RingChannelHandler {
        ring: Arc::downgrade(&inner),
    };
    (
        Callback::from(move |t| match inner.ring.lock() {
            Ok(mut g) => {
                // Eventually drop the oldest element.
                g.push_force(t);
                drop(g);
                let _ = sender.try_send(());
            }
            Err(e) => tracing::error!("{}", e),
        }),
        receiver,
    )
}
```

`g.push_force(t)` 修改真正存放 Sample 的有限 ring；容量为 1 的 flume channel 只发送空值 `()` 作为轻量通知，无法一对一记录所有样本。`drop(g)` 先释放 ring mutex，随后 `try_send(())` 才尝试通知，通知槽已满时可以合并提示，但新的 Sample 仍保留在 ring 中。消费方先从 ring 取样，取不到时再等待通知，然后重新检查 ring。`RingChannelHandler` 中的 `Weak` 不使消费端单方面延长内部 ring 的生命周期。

FIFO 与 Ring 的选择是业务语义，而不是单纯性能开关：

| 语义 | 队列满时 | 优点 | 代价 |
|---|---|---|---|
| FIFO | 阻塞生产路径 | 不丢事件、保持顺序 | 延迟向上游扩散，可能拖住 dispatch |
| Ring | 淘汰旧事件 | 队列容量有界、优先保留较新数据 | 不能重放完整历史，也不保证端到端延迟存在硬上界 |

## callback 必须遵守执行上下文契约

直接 callback 通常运行在 Zenoh 的分发或异步任务上下文中。它不是自动获得一个独占线程。下面的回调会把数据库慢查询直接加到分发延迟中：

```rust
session.declare_subscriber("robot/**")
    .callback(|sample| {
        slow_blocking_database_write(sample); // 不合适
    })
    .await?;
```

更稳定的边界是让 callback 只做轻量验证、时间戳记录和入队，把慢工作交给受控 worker：

```rust
.callback(move |sample| {
    if work_tx.try_send(sample).is_err() {
        dropped_samples.fetch_add(1, Ordering::Relaxed);
    }
})
```

这里必须明确 `try_send` 失败语义。若业务不能丢，就不能静默忽略失败；应改用可阻塞队列、持久日志或让发布方得到明确过载信号。

## 三层流量控制不能互相替代

Zenoh 的流量控制至少包含三层：

```text
应用 handler queue
  管理 callback -> 应用消费者

CongestionControl / Priority
  管理一条发送路径在拥塞时的调度与丢弃倾向

Reliability marker
  表示可靠性偏好；本版本不会因为这个字段直接在网络上重传数据

实际 transport（例如 TCP 或 UDP）
  具有协议自身的流控、重传或丢包语义
```

固定提交 `PublicationBuilder::reliability` 的原始源码直接限定了这个字段的语义：


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

`Reliability` 在这里是发布/路由用的标记，可能参与选择合适链路；它不触发 Zenoh 自动重传，更不能证明接收方业务 callback 已执行。`#[zenoh_macros::unstable]` 说明这个方法也并非所有默认构建都可调用。即使实际承载是 TCP，机械臂安全停机命令仍需要业务序号、期限和执行确认；本地 Ring handler 仍可能覆盖旧 Sample。本地队列、拥塞策略、可靠性标记、实际传输协议和业务确认必须分层分析。

## TaskController 给后台任务确定归属

异步系统容易产生“启动后没人负责停止”的 detached task。Zenoh 使用 `TaskController` 将任务归属到 Session 或 Runtime：

对应的上游实现如下：
```rust
#[derive(Clone)]
pub struct TaskController {
    tracker: TaskTracker,
    token: CancellationToken,
}
```

`tracker` 记录被跟踪的后台任务，`token` 是协作取消的根信号。`Clone` 共享它们的状态，却没有自动建立一个阻止新任务注册的生命周期门闩。它支持两类任务：

- `spawn_abortable`：用派生 cancellation token 包装 future。取消后，future 在下一次 `await` 或 yield 点退出。
- `spawn`：只把 future 纳入 tracker，不替它自动监听取消。任务本身必须观察 token，或保证在有限时间内结束。

`spawn` 与关闭实现必须连在一起读。固定提交的 `spawn` 不检查 tracker 是否已关闭：

```rust
pub fn spawn<F, T>(&self, future: F) -> JoinHandle<T>
where
    F: Future<Output = T> + Send + 'static,
    T: Send + 'static,
{
    #[cfg(feature = "tracing-instrument")]
    let future = tracing::Instrument::instrument(future, tracing::Span::current());

    self.tracker.spawn(future)
}
```

```rust
pub async fn terminate_all_async(&self) {
    self.tracker.close();
    self.token.cancel();
    self.tracker.wait().await
}
```

`TaskTracker::close` 把跟踪器标记为关闭，使 `wait()` 能够在全部跟踪任务结束后返回，**它并不禁止未来注册任务**。本地 `Cargo.lock` 锁定 `tokio-util 0.7.19`，而上面 `spawn` 只是调用 `self.tracker.spawn(future)`，完全没有 admission gate。`token.cancel()` 将取消状态传播给子 token 并通知等待者，但任务仍需被执行器 poll 后才会观察信号并退出；`wait().await` 等待的是任务实际完成，而不只是取消命令已发出。

例如关闭方完成 `close → cancel → wait` 时 tracker 恰好为空，`wait` 返回后，另一个仍持有 Controller 克隆的生产者调用 `spawn`。普通 future 依然能注册，却已经越过这次关闭屏障，也不会自动观察 token。若业务要求“关闭之后不得再产生任务”，必须由 **Controller 外层的生命周期门闩** 保证新任务的准入和实际登记与关闭操作互斥；仅先检查一个 atomic bool 再调用 `spawn`，仍存在检查与登记之间的竞争窗口。这种门闩是推荐实现，而不是本文固定 Zenoh 源码已有的保证。

关闭的三步依然有先后含义：若先等待再取消，休眠或阻塞于可取消 I/O 的任务可能始终没有退出理由；若取消后不等待，仍持有 Session、Transport 或 Resource 引用的任务可能越过关闭边界存活。`spawn_abortable` 的 future 由取消包装器驱动退出，普通 `spawn` 则要求任务主动观察取消或保证有限时间内结束。

源码位置：[`TaskController`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/commons/zenoh-task/src/lib.rs#L30-L146)。

## 协作式取消只在任务主动让出时生效

Cancellation token 不是强制杀线程。下面的任务即使 token 已取消，也可能永久不退出：

```rust
controller.spawn(async move {
    loop {
        blocking_cpu_work_without_await();
    }
});
```

正确任务要么使用 abortable 包装，要么显式选择取消分支：

```rust
controller.spawn(async move {
    loop {
        tokio::select! {
            _ = session_token.cancelled() => break,
            item = input.recv_async() => process(item?).await,
        }
    }
});
```

因此，“由 TaskController 跟踪”只保证关闭方知道要等待谁；能否及时结束，还取决于每个任务是否遵守取消契约。

## Session close 以 `primitives.take()` 划定关闭时刻

Session 关闭状态可以概括为：

```text
OPEN
  |
  | CloseBuilder::into_future
  v
CLOSING
  | [SessionState 写锁]
  |  1. primitives.take()
  |  2. take queryables/subscribers/resources/queries/listeners maps
  | [释放写锁]
  |  3. 在锁外析构 callback-bearing entities
  |  4. 可选等待 SyncGroup 中的活动 callback
  |  5. 终止 Session tasks
  |  6. 关闭自有 Runtime，或仅向共享 Runtime 发送 session close
  v
CLOSED
```

`primitives` 是 Session 向下层发送声明、数据和请求的接口。`take()` 在 state 写锁内取走这一入口；随后通过 `state.primitives()?` **新取得**发送接口的操作才会观察到 closed。此前已经取得接口的在途操作不能仅凭这一行认定全部退出，因此后面仍需移出实体注册表、可选等待正在执行的 callback，并结束被跟踪的任务。

重复 close 时，第二次 `take()` 得到 `None`，于是快速返回。这使关闭具有幂等性，不会重复发送 transport close 或再次撤销相同实体。

## 带 callback 的对象必须在锁外析构

关闭代码把 registry maps 移到局部变量，再释放 Session 写锁。原始实现并非用单个 `entities` 容器抽象全部状态，而是逐个 `take` 出保存 callback、查询和匹配监听器的 map：

```rust
async fn close_inner(&self, close_args: SessionCloseArgs) {
    let primitives = zwrite!(self.0.state).primitives.take();

    // defer the cleanup of internal data structures by taking them out of the locked state
    // this is needed because callbacks may contain entities which need to acquire the
    // lock to be dropped, so callback must be dropped without the lock held
    // Do this step before closing runtime and transport to prevent new callbacks from being called
    // while closing
    {
        let mut state = zwrite!(self.0.state);
        let _queryables = std::mem::take(&mut state.queryables);
        let _subscribers = std::mem::take(&mut state.subscribers);
        let _liveliness_subscribers = std::mem::take(&mut state.liveliness_subscribers);
        let _local_resources = std::mem::take(&mut state.local_resources);
        let _remote_resources = std::mem::take(&mut state.remote_resources);
        let _queries = std::mem::take(&mut state.queries);
        let _matching_listeners = std::mem::take(&mut state.matching_listeners);
        let _transport_event_listeners = std::mem::take(&mut state.transport_events_listeners);
        let _link_event_listeners = std::mem::take(&mut state.link_event_listeners);
        drop(state);
    }
    // after this point, no callbacks can be present in session anymore,
    // since all existing ones have been dropped and no new ones can be created since primitives have been taken out of session state

    if close_args.wait_callbacks {
        self.0.callbacks_drop_sync_group.wait_async().await;
    }

    let Some(primitives) = primitives else {
        return;
    };

    if let Some(r) = self.0.runtime.static_runtime() {
        // session created by plugins never have a copy of static_runtime, so the code below will run only inside zenohd
        info!(zid = %self.zid(), "close session");
        self.0.task_controller.terminate_all_async().await;
        let closee = r.get_closee();
        closee.close_inner(()).await;
    } else {
        self.0.task_controller.terminate_all_async().await;
        primitives.send_close();
    }
}
```

这样做并非风格偏好。一个 Subscriber 或 Queryable 析构时可能自动 undeclare，而 undeclare 又会尝试获取 Session state lock；若对象在原写锁作用域内析构，就会重入同一把非重入锁并死锁。

注意 `drop(state)` 只释放写锁，`_subscribers` 等局部对象仍活到这层大括号退出才析构。`callbacks_drop_sync_group.wait_async()` 是对仍在执行的相关 callback 的可选等待；`task_controller.terminate_all_async()` 则等待这一个 Controller 跟踪的任务，它们不是同一套计数。关闭的普适原则是：锁内只断开注册表和状态可达性，锁外再执行析构、callback 等待和网络 I/O。

源码位置：[`CloseBuilder` 与超时桥接](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/close.rs#L35-L137)，[`Session close`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L3704-L3763)。

## 自有 Runtime 与共享 Runtime 的关闭边界不同

普通 `open()` 创建的 Session 可以拥有一个 static Runtime；插件环境中的 Session 也可以挂到共享 DynamicRuntime。两者关闭含义不同：

```text
Session owns static Runtime
  -> terminate Session tasks
  -> Runtime::close_inner()
       关闭 transport、routing resources 和 Runtime tasks

Session shares DynamicRuntime
  -> terminate Session tasks
  -> primitives.send_close()
       只移除该 Session，不关闭其他插件仍在使用的 Runtime
```

这是所有权驱动的关闭设计：只有拥有底层生命周期的对象才有权终止底层。把共享 Runtime 当成 Session 私有资源，会让一个插件的退出意外中断同进程其他插件。

## Runtime close 从生产任务向底层资源推进

自有 Runtime 的关闭顺序为：

对应的上游实现如下：

```rust
async fn close_inner(&self, _: ()) {
    tracing::trace!("Runtime::close()");
    // TODO: Plugins should be stopped
    // TODO: Check this whether is able to terminate all spawned task by Runtime::spawn
    self.task_controller.terminate_all_async().await;
    self.manager.close().await;
    // clean up to break cyclic reference of self.state to itself
    self.transport_handlers.write().unwrap().clear();
    // TODO: the call below is needed to prevent intermittent leak
    // due to not freed resource Arc, that apparently happens because
    // the task responsible for resource clean up was aborted earlier than expected.
    // This should be resolved by identifying corresponding task, and placing
    // cancellation token manually inside it.
    let mut tables = self.router.tables.tables.write().unwrap();
    tables.data.root_res.close();
    tables.data.faces.clear();
}
```

执行顺序是先等待由 Runtime `task_controller` 跟踪的任务，再执行 `TransportManager::close().await`，随后清空 `transport_handlers` 打断可能的强引用环；最后持有 routing tables 写锁关闭 Resource 树并清空 Faces。源码中的 TODO 提醒：插件停止过程和通过 `Runtime::spawn` 创建的任务尚有需要确认的边界；不能仅凭本函数就断言系统中一切潜在后台任务均已终止。

这遵循“先停生产者，再拆消费者状态”的方向。若先释放 Resource tree，而 transport callback 仍可能进入 DeMux，它会访问已经拆除的路由对象。

源码位置：[`Runtime::close_inner`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/runtime/mod.rs#L1307-L1325)。

## 显式 close 比依赖 Drop 更可诊断

最后一个逻辑 Session handle 析构时，`Session::drop` 会尝试同步关闭，但错误只能记录到日志。显式调用：

```rust
session.close().await?;
```

可以把 timeout 和关闭错误返回给调用方，也能让上层按照“停止业务输入 → 关闭 Session → 停止进程”的顺序编排。工业系统不应把关键清理完全寄托于进程退出时的析构，因为日志、网络 flush 和异步任务都可能没有足够时间完成。

## 性能与故障观测指标

| 位置 | 需要记录的指标 | 指标异常通常表示 |
|---|---|---|
| FIFO/Ring handler | queue depth、enqueue wait、overwrite/drop | 消费者慢或容量策略错误 |
| callback | 执行 p50/p99、并发数 | 用户代码阻塞 dispatch |
| Reliability 标记 | 发布标记值、最终选中的传输链路 | 可靠性偏好与实际链路选择的差异；标记本身不是重传次数 |
| 具体 transport | send queue、drop、具体协议自己的重传/流控指标 | 链路拥塞或相应协议的成本 |
| TaskController | tracked tasks、cancel-to-exit latency | 任务未观察 token 或 I/O 不可取消 |
| Session close | close duration、active callbacks、pending queries | 生命周期泄漏或 Final 丢失 |
| Runtime close | live transports/faces/resources after close | teardown 顺序或引用环问题 |

关闭超时必须携带剩余任务类型、qid、Face id 或 Resource expr 等上下文。只有“close timeout”一行日志，无法区分是慢 callback、丢失 Final、不可取消 I/O，还是引用环。

## 最小复刻的关闭契约

一个最小实现至少需要以下不变量：

1. 所有后台任务注册到一个明确 owner，禁止裸 `spawn` 后丢失 handle；
2. owner 必须使用独立的生命周期门闩，保证关闭开始后不能再登记新任务或创建实体；`TaskTracker::close` 自身不负责拒绝注册；
3. 先设置 closed/取走发送接口，再移出实体 registry；
4. 用户 callback、实体析构和网络 close 都在内部状态锁外执行；
5. 取消与等待分开：先发取消信号，再等待 tracker 归零；
6. 重复 close 不重复产生外部副作用；
7. close 有可配置上界，并能报告仍未结束的对象。

这些规则比某个特定 async runtime 更重要。即便用 C++ 的 `std::jthread`、stop token 和 condition variable 重写，也应保留相同的所有权与终止顺序。
