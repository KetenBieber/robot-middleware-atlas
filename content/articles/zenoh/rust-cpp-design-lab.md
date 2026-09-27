# Zenoh Rust/C++ 设计映射：所有权、异步实体与 FFI 边界

Zenoh 核心使用 Rust，而许多机器人应用通过 C 或 C++ API 接入。读源码时最容易出现两个断层：Rust 的 `Arc`、生命周期和异步任务看懂了语法，却不知道它们解决哪个运行时问题；C++ 侧会声明 Publisher，却看不见句柄如何映射到 Rust 实体。本章把两侧放到同一张生命周期图中。

先把语言名词放到一个具体场景里。假设 C++ 相机节点声明了一个 Publisher，连续发布图像，然后让 Publisher 离开作用域：

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
{
  Publisher camera = session.DeclarePublisher("robot/camera/front");
  camera.Put(frame);
}  // camera 在这里析构
```

表面上只是一个局部变量消失，底层却必须同时回答四个问题：

1. C++ 对象析构后，Rust 侧的 Publisher 实体由谁撤销；
2. 这次撤销能否顺便销毁 Session，还是 Session 仍被其他实体使用；
3. 已经提交给异步任务的 `frame` 是否仍然有效；
4. 声明或撤销失败时，C ABI 怎样报告错误而不让 Rust panic 穿过语言边界。

本章后面的 `Arc`、`Weak`、Builder、RAII 和 FFI 都是在回答这四个问题。第一次阅读时，可以先把对象分成四层，不必立刻记住所有 Rust 类型：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
C++ RAII handle       负责“这个应用实体何时结束”
        |
C opaque handle       负责跨语言传递，但不暴露 Rust 内存布局
        |
Rust Publisher        负责声明、put 与撤销语义
        |
shared SessionInner   被多个 Publisher/Subscriber 和后台任务共同使用
```

最关键的区分是：Publisher 的寿命和 Session 的寿命不是一回事。局部 Publisher 析构，应撤销它自己的声明；它不能因为自己结束，就把其他订阅者仍在使用的网络运行时一并关闭。

本文固定 Zenoh 源码为 [`9fcd9cb5`](https://github.com/eclipse-zenoh/zenoh/tree/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5)。语言机制要沿真实运行时对象阅读：

| 学习边界 | 源码入口 | 重点问题 |
|---|---|---|
| Session 打开 | [`session.rs`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L860-L906) | SessionInner 与 Runtime 谁拥有谁 |
| Builder 执行 | [`builders/session.rs`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/builders/session.rs) | 同步 wait 与 Future 怎样提交操作 |
| Publisher 声明 | [`declare_publisher_inner`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/session.rs#L1581) | entity ID、key expression 与声明状态 |
| Query 生命周期 | [`queryable.rs`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/api/queryable.rs#L116-L160) | Drop 怎样触发 Final，clone 如何延后完成 |
| 路由快照 | [`resource.rs`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/zenoh/src/net/routing/dispatcher/resource.rs#L225-L319) | Arc Route、Weak matches 与 cache 失效 |
| 任务关闭 | [`zenoh-task`](https://github.com/eclipse-zenoh/zenoh/blob/9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5/commons/zenoh-task/src/lib.rs#L30-L146) | stop token、task tracking 与等待边界 |

一条声明和一条数据写入不是同一种路径：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
declare publisher:
Builder -> SessionInner entity table -> primitives declaration
  -> routing Resource/Face state -> Publisher handle

put payload:
Publisher -> Session primitives -> WireExpr/resource lookup
  -> immutable Route snapshot -> destination Faces -> transport tasks
```

声明路径允许更新表、失效缓存和分配实体；高频 put 路径应尽量复用已经发布的不可变路由结果。这是理解后续 Arc、锁和 Builder 选择的主线。

## 一次实体声明跨越哪些边界

**图示身份：概念、状态或调用链示意，不是源码。**
```text
C++ Publisher RAII object
  -> C ABI opaque handle
  -> Rust FFI wrapper
  -> Publisher entity
  -> Arc<SessionInner>
  -> routing declarations / transport runtime
```

应用句柄析构不等于立即销毁整个 Session。Publisher 只撤销自己的声明；Session、路由表和 transport 可能仍被其他实体共享。核心设计问题因此是“共享运行时 + 独立实体寿命”，而不是一条 socket 对应一个对象。

图中的每一条箭头还代表不同所有权：C++ Publisher 唯一拥有 C handle；C handle 唯一拥有 Rust FFI wrapper；Publisher entity 通常共享 SessionInner；SessionInner 再共享或引用 Runtime。关闭 Publisher 只切断自己的声明，不能擅自停止 Runtime。若把这些关系全部换成 shared_ptr/Arc，虽然不易悬空，却会失去“谁负责撤销声明”的唯一责任。

## `Arc` 只提供共享寿命，不提供可变性

**代码身份：教学最小例子；非上游源码摘录。**
```rust
struct Session {
    inner: Arc<SessionInner>,
}

impl Clone for Session {
    fn clone(&self) -> Self {
        Self { inner: Arc::clone(&self.inner) }
    }
}
```

克隆 Session 只增加原子引用计数，不复制路由表或网络连接。`Arc<T>` 允许多个线程共同拥有 `T`，但不能直接通过共享引用修改普通字段。可变状态仍需放入 `Mutex`、`RwLock`、原子变量或消息通道。

对应到 C++，它接近 `std::shared_ptr<SessionInner>`，但 Rust 类型系统还要求跨线程类型满足 `Send`/`Sync`。C++ 的 shared_ptr 只保证控制块的引用计数并发安全，不保证被指对象的字段线程安全。

### 逐句解释 `Arc::clone`

`Arc<SessionInner>` 是一个拥有型智能指针。`Arc::clone(&self.inner)` 通过共享引用读取控制块并原子增加 strong count，返回另一个 owner。它不调用 `SessionInner::clone()`，也不复制内部 socket、路由表或任务。

当最后一个 strong Arc 被 drop，`SessionInner` 析构；Weak count 仍可让控制块继续存在，直到最后一个 Weak 也被 drop。`Arc::strong_count()` 只能作为瞬时诊断，不能据此做“若 count == 1 就无锁修改”的并发决策，因为检查后其他线程仍可 clone。

`Arc<T>` 实现 `Send`/`Sync` 需要 T 满足相应约束。若 SessionInner 内含 `Rc`、裸的非线程安全 FFI 句柄或 `Cell`，编译器会阻止跨线程 spawn。不要用 `unsafe impl Send` 绕过错误；那是在手工承诺内部同步已经完整，必须逐字段证明。

### `Mutex<T>` 与 `RwLock<T>` 的 guard 是借用令牌

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let mut entities = inner.entities.write().await;
entities.insert(id, state);
drop(entities); // 明确结束可变借用和锁
```

guard 的析构释放锁，它同时携带对被保护 T 的借用，因此安全 Rust 无法把内部引用保存到 guard 之外。显式 `drop` 不是释放 entities 本身，而是缩短临界区。若需要锁外使用，复制 ID、Arc 或不可变 plan，而不是返回指向表项的引用。

## `Weak` 用于回指，不用于成功保证

异步任务常需回到 Session，却不应因此把 Session 永久保活：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let session = Arc::downgrade(&inner);
runtime.spawn(async move {
    if let Some(session) = session.upgrade() {
        session.process_event(event).await;
    }
});
```

`upgrade()` 返回 `Option<Arc<T>>`，迫使代码处理对象已经关闭的分支。它与 C++ `weak_ptr::lock()` 同构。成功升级只保证当前异步操作期间内存存在；执行逻辑还要检查 close 状态，避免关闭开始后继续创建声明。

Weak 常用于 parent 回指、缓存边和后台观察任务。它不是避免所有环的魔法：如果 SessionInner 持有 task join handle，而 task capture 强 Arc<SessionInner>，仍形成逻辑保活环。更好的任务捕获是 Weak 或只捕获任务所需的独立 Arc<State>，关闭时由 TaskController 发停止信号并 join。

## Builder 把配置阶段与生效阶段分离

Zenoh API 常返回 builder，最终通过 `.await` 或同步解析使操作生效：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let publisher = session
    .declare_publisher("robot/arm/state")
    .congestion_control(CongestionControl::Drop)
    .await?;
```

Builder 的价值不是链式调用好看，而是让一组相关选项在提交前保持局部一致。若每个 setter 都立即修改全局路由状态，中途失败会留下半配置实体。

Builder 通常消费自身并返回新类型或最终实体。消费 `self` 可防止同一声明被重复提交；C++ 若复刻这种语义，可以让 `Build() &&` 只允许在右值 builder 上调用，或在运行期记录 `built_` 并拒绝二次提交。

### `self`、`&self` 与 `&mut self` 对 API 的含义

这三个写法先不要理解成语法表。它们回答的是“调用函数时，函数拿走对象、暂时查看对象，还是暂时独占修改对象”。用同一个 Builder 对照最容易看清：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
let builder = session.declare_publisher("robot/arm/state");
let builder = builder.priority(Priority::DataHigh); // 旧值被 move，新值返回
let publisher = builder.wait()?;                    // 提交时消费 builder
// builder 到这里已经不能再使用
```

如果 `wait()` 只接收 `&self`，同一个 Builder 就可以被重复提交，代码还要在运行期额外判断“是否已经 build”。消费 `self` 则让这类非法状态在安全 Rust 中难以表达。下面再逐项看三种 receiver 的准确含义：

- `fn build(self)` 取得 builder 所有权，调用后原变量被 move，编译器阻止再次使用；
- `fn option(mut self, value)` 修改局部 builder 并返回它，形成链式配置；
- `fn inspect(&self)` 只借用，调用后 builder 仍可使用；
- `fn set(&mut self)` 独占借用一段时间，但不消费对象。

这比运行期 `built_` 布尔值更强：非法二次提交在编译期无表示。C++ 的 `Build() &&` 只能约束值类别，调用者仍可 `std::move(builder).Build()` 后继续访问 moved-from 对象，所以实现仍应让 moved-from 状态可析构且明确失败。

Builder 的最终提交还需要事务：分配 entity ID、规范化 KeyExpr、插入 Session 表、发送声明，任一步失败都要撤销前一步。Rust 的 `?` 会提前返回并 drop 局部 RAII guard，但已经写入共享表的动作必须由显式 rollback guard 或“先局部构造、最后一次插表”处理。

## RAII 句柄必须定义析构的阻塞语义

一个直觉化的 C++ 包装如下：

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
class Publisher final {
 public:
  Publisher(Publisher&& other) noexcept
      : handle_(std::exchange(other.handle_, nullptr)) {}

  Publisher& operator=(Publisher&& other) noexcept {
    if (this != &other) {
      Reset();
      handle_ = std::exchange(other.handle_, nullptr);
    }
    return *this;
  }

  Publisher(const Publisher&) = delete;
  Publisher& operator=(const Publisher&) = delete;
  ~Publisher() { Reset(); }

 private:
  void Reset() noexcept;
  z_owned_publisher_t* handle_ = nullptr;
};
```

移动构造使用 `std::exchange` 同时取走指针并把源对象清空。移动赋值必须先释放当前实体，否则覆盖指针会泄漏声明。

困难在 `Reset()`：撤销声明可能需要向异步运行时发送命令。如果析构等待网络确认，作用域退出时间不可预测；若只入队，析构返回并不表示远端已经观察到撤销。工业接口通常需要把“快速 RAII 清理”和“显式等待关闭”区分开，并在文档中声明保证级别。

### 移动赋值也可能阻塞

`Publisher& operator=(Publisher&&)` 中的 Reset 会先关闭目标已有实体，所以移动赋值不是纯指针操作。若 Reset 会等待运行时，它不能在实时线程执行。更稳妥的接口提供 `Close(timeout)` 返回结果，析构只做 noexcept 的本地兜底撤销。

若 C handle 是内联 opaque storage 而不是指针，C++ wrapper 仍应使用官方 `move`/`drop` 函数，不能 `memcpy`。Rust 类型可能具有地址敏感状态或内部所有权，字节复制会制造两个 owner。绑定层的 C 类型布局应以对应 zenoh-c 版本为准；本文代码展示所有权模型，不替代实际头文件。

### C++ Builder 的单次提交

**代码身份：教学最小例子；非上游源码摘录。**
```cpp
class PublisherBuilder final {
public:
  PublisherBuilder(Session& session, std::string key)
      : session_(&session), key_(std::move(key)) {}

  PublisherBuilder&& DropOnCongestion() && {
    congestion_ = Congestion::Drop;
    return std::move(*this);
  }

  Result<Publisher> Build() && {
    if (!session_) return Error::AlreadyConsumed;
    Session* session = std::exchange(session_, nullptr);
    return session->DeclarePublisher(key_, congestion_);
  }

private:
  Session* session_;
  std::string key_;
  Congestion congestion_{Congestion::Block};
};
```

`&&` ref-qualifier 表示函数只能在右值对象上调用；setter 返回 `PublisherBuilder&&` 保持链式临时对象。`std::exchange` 在提交前清空 session 指针，即使 DeclarePublisher 返回错误，同一个 builder 也不能再次提交。它仍只是 C++ 近似：裸 Session 指针要求 builder 不越过 Session 寿命，更强设计可保存 weak session handle。

## FFI 使用不透明句柄隔离 Rust 布局

C ABI 不应暴露 Rust 结构体布局：

**代码身份：教学最小例子；非上游源码摘录。**
```c
typedef struct z_owned_publisher_t z_owned_publisher_t;

int z_publisher_put(z_owned_publisher_t *publisher,
                    const uint8_t *data, size_t len);
void z_publisher_drop(z_owned_publisher_t *publisher);
```

实现侧可以把句柄看成 Box 管理的 Rust 对象，但创建和释放必须来自同一套 API。调用者不能用 `free()` 释放 Rust 分配的对象，也不能复制不透明句柄的字节来制造第二个 owner。

FFI 函数应先校验空指针和长度，再用 `slice::from_raw_parts` 建立临时借用。该 slice 只在调用期间有效；若数据进入异步队列，Rust 侧必须复制或取得明确的共享缓冲区所有权。

### `unsafe` 应压缩在最小边界

**代码身份：教学最小例子；非上游源码摘录。**
```rust
#[no_mangle]
pub unsafe extern "C" fn z_publisher_put(
    publisher: *mut z_owned_publisher_t,
    data: *const u8,
    len: usize,
) -> i32 {
    std::panic::catch_unwind(|| {
        let publisher = unsafe { publisher.as_mut() }
            .ok_or(Error::NullHandle)?;
        if data.is_null() && len != 0 {
            return Err(Error::NullData);
        }
        if len > MAX_PAYLOAD {
            return Err(Error::TooLarge);
        }
        let bytes = unsafe { std::slice::from_raw_parts(data, len) };
        publisher.put_copy(bytes)
    })
    .map_or(ERR_PANIC, result_to_code)
}
```

`unsafe extern "C"` 表示调用者必须满足原始指针契约；函数内部仍应尽快检查并转成安全引用。`from_raw_parts` 即使 len 为零也要求指针满足其有效性规则，生产实现可为零长度单独使用空 slice，避免把 NULL 传入该函数。

长度检查还要防 `pointer + len` 地址空间溢出、对齐要求和可访问内存。Rust 无法验证来自 C 的地址实际映射了 len 字节，这仍属于调用者契约。

`catch_unwind` 防止普通 Rust panic 穿过 C ABI，但它不是内存错误恢复，也只能捕获 unwind 模式下的 panic。panic=abort 时进程仍会终止；closure 捕获类型还需满足 unwind safety 或显式包装。FFI 导出函数应尽量在进入复杂 Rust 逻辑前完成参数验证，并把 panic 转为稳定错误码。

### 借用 put 与拥有 put 必须是不同 API

同步 `put_borrowed(span)` 可以保证函数返回前已复制或消费数据；异步 `put_owned(vector)` 则把 buffer 所有权移动给运行时。若一个 API 接收裸指针却悄悄异步保存，调用者无法知道何时可以复用内存。

C ABI 可用显式释放回调表达零拷贝所有权转移：调用者提交 pointer、len 和 deleter context，Rust 在最后一个发送任务完成后调用 deleter。这样能减少复制，却引入跨语言线程回调、allocator 匹配和关闭期回收问题；除非 payload 足够大且性能收益已测量，否则复制到 Rust owned buffer 更容易证明安全。

## Rust 借用与 C++ view 的对应关系

| Rust | C++ 近似物 | 共同限制 |
|---|---|---|
| `&[u8]` | `std::span<const std::byte>` | 不拥有数据，不能超过源缓冲区寿命 |
| `Vec<u8>` | `std::vector<std::byte>` | 拥有连续缓冲区，移动便宜 |
| `Arc<[u8]>` | `shared_ptr<const Buffer>` | 跨任务共享，承担原子计数成本 |
| `Cow<'a, [u8]>` | 借用或按需复制的封装 | API 与实现复杂度更高 |

Rust 编译器能验证纯 Rust 借用，跨入 C ABI 后这些保证消失。FFI 包装器必须把未检查的指针迅速转换为范围最小的安全类型，不能让裸指针渗透到路由核心。

`Cow<'a, [u8]>` 的 `Borrowed` 分支不复制，`Owned` 分支拥有 Vec。调用 `to_mut()` 时若当前为 Borrowed 会发生写时复制。它适合“多数只读、少数需要改写”的封装，不意味着异步任务可以随意保存 Borrowed；任务若超出 `'a`，编译器会要求转成 owned 或让 Future 也受该生命周期约束。

## 异步取消不是简单销毁 Future

一个 query 可能同时等待多个 reply。调用方超时后，需要区分：

**图示身份：概念、状态或调用链示意，不是源码。**
```text
停止等待结果
  != 远端已停止计算
  != 路由表已删除 query 状态
  != 网络中不再有迟到 reply
```

因此 query 状态常需唯一 ID、完成标志、接收端和超时任务。完成路径、超时路径、Session 关闭路径都可能争夺“谁负责最后清理”。可用原子状态机保证仅一次完成：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
enum QueryState { Open, Completed, Cancelled }
```

如果状态转换还伴随删除 map 项、唤醒 waiter 和发送取消消息，单个原子枚举仍不够；这些动作需要在锁内确定唯一负责人，再在锁外执行可能阻塞的通知。

可以把终态竞争收敛到一个函数：

**代码身份：教学最小例子；非上游源码摘录。**
```rust
fn finish_query(inner: &QueryInner, reason: FinishReason) -> Option<FinishPlan> {
    let mut state = inner.state.lock().unwrap();
    if !matches!(state.phase, Phase::Open) {
        return None;
    }
    state.phase = Phase::Finished(reason);
    Some(FinishPlan {
        waiter: state.waiter.take(),
        route_ids: std::mem::take(&mut state.route_ids),
    })
}
```

锁内只选择唯一完成者并移出资源；返回的 FinishPlan 在锁外唤醒 waiter、清理 route 或发送取消。`Option` 的 None 表示另一路径已经完成。`take()` 用默认空值替换字段并取得所有权，避免复制，也使第二个完成者没有资源可重复释放。

Drop 不能 `.await`，所以异步撤销通常只能向 runtime 投递命令或触发 cancellation token。若必须确认远端清理，应提供显式 async close；RAII Drop 只保证本地不再使用实体，并尽力启动撤销。

## 有界通道把过载变成显式策略

异步任务之间若使用无界队列，短时流量峰值会变成长期内存增长。容量为 `C` 的队列把内存上界约束为 `O(C × 平均消息大小)`，但满队列时必须选择：等待、丢新、丢旧或报错。

对控制状态，丢旧保留最新值可能合理；对命令流，静默丢弃通常不可接受；对传感器录制，反压可能最终阻塞数据源。拥塞策略属于业务语义，不能藏在容器默认行为里。

## 锁不能跨越 `.await`

**代码身份：错误示例；不是上游源码。**
```rust
// 危险形态：等待期间一直持有路由表锁
let mut tables = self.tables.write().await;
network.send(update).await;
tables.mark_sent();
```

网络发送可能等待容量或 I/O，持锁期间其他声明和路由计算全部停顿。更好的结构是锁内计算不可变计划，解锁后执行 I/O，再用短临界区提交结果；提交时若版本已变化，则重新计算或丢弃过期结果。

这与 C++ 的“锁内复制 shared_ptr，锁外调用回调”是同一条原则：临界区只维护共享不变量，不包住未知时长的外部工作。

## 最小 Session 与实体表

下面的缩小模型保留 Zenoh 最关键的关系：多个实体共享 SessionInner，声明由表唯一拥有元数据，句柄 Drop 发起撤销，数据路径使用不可变发送计划。

**代码身份：教学最小例子；非上游源码摘录。**
```rust
type EntityId = u64;

#[derive(Clone)]
struct RoutePlan {
    generation: u64,
    destinations: Arc<[Destination]>,
}

struct PublisherState {
    key: KeyExpr,
    plan: Arc<RoutePlan>,
}

struct SessionState {
    closing: bool,
    next_id: EntityId,
    publishers: HashMap<EntityId, PublisherState>,
    generation: u64,
}

struct SessionInner {
    state: RwLock<SessionState>,
    outbound: mpsc::Sender<Outbound>,
    tasks: TaskController,
}

struct Publisher {
    session: Weak<SessionInner>,
    id: EntityId,
}
```

SessionState 把“closing、ID 分配、实体表和路由版本”放在同一锁域，便于声明事务维护一致性。Publisher 只持 Weak，因而应用遗留的实体句柄不会阻止 Session 关闭；每次 put 先 upgrade，再检查实体仍存在。

### 声明事务

**代码身份：教学最小例子；非上游源码摘录。**
```rust
impl SessionInner {
    async fn declare_publisher(
        self: &Arc<Self>,
        key: KeyExpr,
    ) -> Result<Publisher> {
        let (id, declaration) = {
            let mut state = self.state.write().await;
            if state.closing {
                return Err(Error::Closed);
            }

            let id = state.next_id;
            state.next_id = state.next_id.checked_add(1)
                .ok_or(Error::IdExhausted)?;
            let plan = compute_route_plan(&state, &key)?;
            state.publishers.insert(id, PublisherState {
                key: key.clone(),
                plan: Arc::new(plan),
            });
            (id, Declaration::Publisher { id, key })
        };

        if self.outbound.send(Outbound::Declare(declaration)).await.is_err() {
            let mut state = self.state.write().await;
            state.publishers.remove(&id);
            return Err(Error::RuntimeStopped);
        }

        Ok(Publisher { session: Arc::downgrade(self), id })
    }
}
```

锁内验证 close、分配 ID、计算初始计划并插表；锁外 await outbound 容量。发送失败时重新加锁回滚。这个简化版本仍有竞态：插表后 Session close 可能先移除实体，失败回滚再 remove 只是幂等；更复杂的是声明已成功入队但确认未知，远端可能短暂看到一个本地已回滚实体。工业协议需要 declaration sequence/epoch，让撤销和迟到声明能够排序。

ID 使用 `checked_add`，不能让溢出静默复用仍在使用的 ID。KeyExpr clone 的成本取决于表示；若内部是 Arc/共享规范化字符串，clone 较轻，否则声明路径会复制。声明是控制面，可以接受一定分配，但仍需限制 key 长度和实体总数。

### put 路径只借用不可变计划

**代码身份：教学最小例子；非上游源码摘录。**
```rust
impl Publisher {
    async fn put(&self, payload: Bytes) -> Result<()> {
        let session = self.session.upgrade().ok_or(Error::Closed)?;
        let plan = {
            let state = session.state.read().await;
            let publisher = state.publishers.get(&self.id)
                .ok_or(Error::Undeclared)?;
            Arc::clone(&publisher.plan)
        };

        session.outbound.send(Outbound::Put { plan, payload })
            .await
            .map_err(|_| Error::RuntimeStopped)
    }
}
```

读锁内只查实体并 clone Arc<RoutePlan>，锁外等待有界 channel。路由更新会创建新 plan 并替换表中 Arc；已经开始的 put 可继续使用旧快照。这给出清晰语义：发送看到某个完整路由版本，而不是在 destinations 被原地修改时遍历半成品。

如果 put 选择 `CongestionControl::Drop`，应使用 try_send 并把 Full 映射为可观测 drop；若选择 Block，await 时间属于调用延迟。机器人控制线程通常不能直接 await 无界时间，应在上层设置 deadline 或使用非阻塞策略并监控丢弃。

### Drop 发起幂等撤销

**代码身份：教学最小例子；非上游源码摘录。**
```rust
impl Drop for Publisher {
    fn drop(&mut self) {
        if let Some(session) = self.session.upgrade() {
            session.tasks.spawn_detached(undeclare(session, self.id));
        }
    }
}
```

示例表达意图，但真实 Drop 不能假定 runtime 一定接受新任务：关闭中 spawn 可能失败。更稳妥的是同步从本地表标记撤销，并向一个仍由 Session close 管理的内部队列 try_send；若队列已关，Session close 自己清全部实体。undeclare 必须按 ID 幂等，避免显式 close 与 Drop 重复撤销。

## FFI 句柄的完整状态机

一个跨语言 owned handle 至少有 Empty、Live、Closing/Consumed 三种逻辑状态。C++ move 从源对象取走 Live 变成 Empty；显式 close 消费 Live；析构对 Empty 无操作，对 Live 执行兜底 drop。C 函数不能接受两个按字节复制出来的 Live 句柄。

可以让 C ABI 使用 out-parameter 创建，避免在错误时返回半初始化值：

**代码身份：教学最小例子；非上游源码摘录。**
```c
int z_declare_publisher(const z_session_t *session,
                        const char *key, size_t key_len,
                        z_owned_publisher_t *out);
int z_publisher_close(z_owned_publisher_t *publisher,
                      uint64_t timeout_ms);
void z_drop_publisher(z_owned_publisher_t *publisher);
```

函数进入时先把 out 初始化为空，全部成功后才写入 owned value。close 成功或失败后是否消费 handle必须固定；常见选择是“close 发起后句柄总被消费，返回码只表示确认结果”，否则调用者很难判断是否还要 drop。drop 必须接受 empty 并保持幂等。

回调句柄还要解决 C++ 对象稳定地址。把 `this` 作为 void* 注册后，wrapper 不能随意 move；可把 CallbackState 放进 `unique_ptr`，移动 wrapper 只移动 unique_ptr，堆对象地址不变。注销后等待 active callback 为零，再释放 state，和 LCM 的回调桥接遵循同一原则。

## 数据结构、复杂度与内存预算

设 Session 实体数为 `E`、资源节点数为 `R`、Face 数为 `F`、每条路由目标数为 `D`、outbound 容量为 `C`、平均 payload 为 `S`：

| 路径 | 典型成本 | 内存主项 | 边界条件 |
|---|---:|---:|---|
| entity HashMap 查找 | 平均 `O(1)`，最坏 `O(E)` | `O(E)` | 哈希碰撞、ID 溢出 |
| KeyExpr/Resource 匹配 | 与表达式长度和 matches 图相关 | Resource + Weak edges | `**` 密度放大相交计算 |
| route cache 命中 | 近似 `O(1)` 取 Arc + `O(D)` 扇出 | `O(R×contexts×routes)` | 声明抖动导致频繁失效 |
| Arc clone/drop | `O(1)` 原子操作 | 控制块 | 热点 cache line 竞争 |
| outbound 入队 | `O(1)` 摊销 | `O(C×S)` 或 shared payload | Full 时必须有策略 |
| Query | `O(fanout + replies)` | pending map + reply queue | 无 Final/超时、迟到回复 |
| Session close | `O(E+F+tasks)` 加等待 | 关闭快照 | task 不响应取消 |

`Arc<[u8]>` 或 Bytes 可以让多个 destination 共享 payload，避免 D 次完整复制；但每个 transport 仍可能添加 frame、压缩或复制进 socket buffer。route snapshot 降低锁竞争，却允许旧快照暂时保留 destination 与 writer，拓扑剧烈变化时要观察旧版本内存和回收延迟。

Rust allocator、HashMap 扩容、async channel 和 task spawn 都可能分配。Zenoh 适合高性能数据分发，不等于普通 API 路径具有硬实时上界。硬实时控制环应把网络中间件放在非实时边界，通过预分配 mailbox 与控制线程交换固定大小状态。

## 可迁移的设计能力

Zenoh 最值得迁移的组合是：Builder 把配置与提交分离；Arc/Weak 表达共享 runtime 与非拥有回指；不可变 Route 快照让高频读路径脱离写锁；有界 channel 把过载显式化；TaskController 给异步任务共同关闭边界；FFI 只暴露 opaque owned/loaned handle。

迁移到 C++ 时，不要机械翻译每个 Arc 为 shared_ptr。先标出真正所有权根、回指、借用和唯一实体责任，再选择 `unique_ptr`、`shared_ptr<const Plan>`、`weak_ptr` 或 span。迁移到其他 Rust 系统时，也不能把类型安全误当成协议正确：路由 version、query 唯一完成和 close 顺序仍需显式状态机。

## 最小复刻路线

1. 实现只支持精确 key 的单线程 Session 和本地 Pub/Sub；
2. 用 Arc<SessionInner> 与 Weak<Entity> 分离共享运行时和实体寿命；
3. 加入 Builder，并让声明采用“局部构造、最后提交、失败回滚”；
4. 增加固定容量 outbound channel 与 Block/Drop 两种明确策略；
5. 引入 Resource tree、Face mapping 和不可变 RoutePlan；
6. 加入 route generation 与声明变更失效；
7. 实现 Query pending map、Reply/Final、timeout 和唯一完成者；
8. 最后增加 C ABI 的 owned/loaned handle、panic 屏障和 C++ RAII wrapper。

每一步必须证明 close 后不接收新声明、旧 handle 安全失败、有界队列不会暗中增长、锁不跨 I/O await、Drop 与显式 close 不会重复释放、FFI 借用不越过调用边界。

## 性能与工程取舍

`Arc` clone 是原子操作，不应在每个 payload 字节处理层反复发生；可在批次或实体级保存强引用。`RwLock` 在读多写少时有优势，但路由更新若频繁，写者等待和缓存行竞争仍可能成为瓶颈。route cache 用内存换匹配计算，声明变化时又必须失效缓存。

Rust 消除了大量释放后使用问题，却不会自动消除协议状态错误、死锁、优先级反转和无界排队。C++ 包装器增加易用性，同时也可能隐藏异步关闭与借用期限；最好的绑定应把所有权做成类型，把阻塞与复制做成显式选项。

## 最小复刻的完成标准

教学实现应能让多个实体共享 Session；Publisher 可移动不可复制；FFI 句柄只有一个释放者；借用 payload 不越过调用边界；query 的成功、超时和关闭只完成一次；队列容量与满载策略可配置；任何锁都不跨越网络等待。达到这些条件，才复刻了 Zenoh 语言层设计的核心约束。

还应能指出四个线性化点：实体声明何时对 put 可见；路由更新何时替换旧 plan；query 哪一步取得唯一完成权；Session close 哪一步使新操作必然失败。若只能描述最终结果而说不清这些时刻，并发设计仍不可复刻。
