# 项目案例：ROS 2 `rmw_zenoh` 如何映射 DDS 风格接口到 Zenoh

假设巡检机器人上的激光雷达以 20 Hz 发布扫描，ROS Executor 负责控制回调，远端诊断程序还要查询节点图。若直接在 Zenoh 到包回调里调用 ROS 用户函数，网络接收任务就会承担任意业务耗时；当控制回调执行 40 ms 时，后续扫描会在接收路径积压，节点也失去 ROS wait-set 的执行顺序。`rmw_zenoh` 在中间加上 `SubscriptionData` 队列与 `rmw_wait`，把“消息到了”转换成“ROS 实体可取”，再由 Executor 调用用户回调。

[`ros2/rmw_zenoh`](https://github.com/ros2/rmw_zenoh) 是 ROS 2 的 Zenoh RMW 实现，使用 zenoh-cpp 把 ROS Node、Publisher、Subscription、Service 和 Client 映射到 Session、Pub/Sub、Queryable 与 Query。它展示了怎样在不改变上层 ROS API 的前提下替换通信语义。

本文源码坐标固定到 rolling 提交 [`3b5b9bf4`](https://github.com/ros2/rmw_zenoh/tree/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe)。RMW 能力会随 ROS 发行版分支演进，部署时要用目标发行版分支重新核对 QoS、事件、共享内存和 Router 配置，不能把 rolling 的实现状态直接套到旧版二进制。

## 适配目标

RMW 层必须同时实现：ROS graph discovery、topic 数据、QoS、服务请求响应、wait set、序列化消息、GID 与时间戳。Zenoh 原生抽象并不与 DDS/ROS 一一同名，因此核心工作是语义映射，而不是包装函数。

## 从仓库结构建立阅读顺序

官方仓库将实现放在 `rmw_zenoh_cpp`，Zenoh 依赖由 `zenoh_cpp_vendor` 管理，系统级行为测试位于 `test_rmw_zenoh_cpp`。阅读时不要从巨大的 RMW 导出函数列表逐个跳转，而应沿实体生命周期推进：

```text
rmw_init / context Data
  -> Zenoh Session + graph cache + wait infrastructure
rmw_create_node
  -> NodeData + NN liveliness token
rmw_create_publisher / subscription
  -> PublisherData / SubscriptionData + MP/MS token
rmw_publish -> CDR + attachment -> Zenoh put
Zenoh callback -> owned queue -> guard/wait -> rmw_take
rmw_destroy_* -> stop ingress -> undeclare entity/token -> release storage
rmw_shutdown -> Session close
```

Context 对应一个共享 Session，而不是每个 Node 建一条连接。Node 在 Zenoh 中没有直接对应物，所以创建 Node 主要建立 RMW 状态和 `NN` liveliness token；Publisher、Subscription、Service、Client 才创建数据通信实体。

### 固定源码入口

| 功能 | 源码入口 | 阅读目标 |
|---|---|---|
| RMW C API | [`rmw_zenoh.cpp`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/rmw_zenoh.cpp#L886-L1024) | 参数校验、identifier、opaque data 与创建订阅；销毁见 [rmw_destroy_subscription](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/rmw_zenoh.cpp#L1028-L1066) |
| Publisher | [`rmw_publisher_data.cpp`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_publisher_data.cpp#L249-L265) | 对象状态；序列号、attachment 与发布路径见 [publish methods](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_publisher_data.cpp#L589-L708) |
| Subscription | [`rmw_subscription_data.cpp`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L701-L838) | Zenoh callback 到拥有型 Message；队列、history 与 WaitSet 通知见 [add_new_message](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L1114-L1180) |
| Graph | [`graph_cache.cpp`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/graph_cache.cpp#L346-L434) | token put 的解析和索引更新；删除路径见 [parse_del](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/graph_cache.cpp#L581-L660) |
| 设计协议 | [`docs/design.md`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/docs/design.md#L350-L415) | key、token、attachment、订阅队列与 take 语义 |
| 默认 Session | [`DEFAULT_RMW_ZENOH_SESSION_CONFIG.json5`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/config/DEFAULT_RMW_ZENOH_SESSION_CONFIG.json5) | Router、scouting、SHM 与队列配置 |

阅读时应先选一条 RMW 生命周期链，例如 create publisher → publish → graph query → destroy，而不是在一个巨型 C API 文件中按函数名顺序阅读。

## 总体对象图

```text
rcl/rclcpp
  -> rmw_zenoh_cpp
       ├─ Context / NodeData
       ├─ PublisherData / SubscriptionData
       ├─ ClientData / ServiceData
       ├─ Zenoh Session
       ├─ liveliness tokens for ROS graph
       └─ waitables / queues / guards
            -> zenoh router
```

默认部署使用独立 Router 和 Session 配置；官方 README 说明节点默认通过 Router gossip 获取发现信息，并允许用环境变量覆盖两类 JSON5 配置。

Router 主要用于发现和跨主机通信，并不意味着同主机数据都经过中心 broker。Context 中多个实体共享 Session，降低连接数量，却也意味着 Session 关闭是所有实体共同依赖的生命周期屏障。

### Context 的依赖图

```text
rmw_context_t
  -> ContextImpl
       |-- shared Zenoh Session
       |-- GraphCache + liveliness subscriber
       |-- wait/event infrastructure
       |-- Nodes
             |-- PublisherData / SubscriptionData
             `-- ClientData / ServiceData
```

正确关闭必须从叶子向根：先阻止实体 callback 和新请求，再销毁 Zenoh entity/liveliness token，随后清 Node/graph waitable，最后关闭 Session。若先 close Session，实体析构中的 undeclare 可能失败；若先释放 SubscriptionData，Zenoh callback 仍可能使用旧 `this`。

## C ABI 入口如何进入 C++ 对象

RMW 对上暴露 C 结构体与函数。一个创建入口的结构可抽象为：

```cpp
extern "C" rmw_publisher_t* rmw_create_publisher(
    const rmw_node_t* node,
    const rosidl_message_type_support_t* type_support,
    const char* topic,
    const rmw_qos_profile_t* qos,
    const rmw_publisher_options_t* options) {
  if (!ValidateNode(node) || topic == nullptr || qos == nullptr) return nullptr;
  try {
    auto data = PublisherData::make(NodeDataFrom(node), topic,
                                    type_support, *qos, *options);
    auto handle = AllocateRmwPublisher();
    handle->implementation_identifier = kImplementationIdentifier;
    handle->data = data.release();
    return handle;
  } catch (...) {
    SetRmwErrorFromCurrentException();
    return nullptr;
  }
}
```

这是结构等价代码，不是仓库逐字摘录。关键不变量有三个：所有入口检查 implementation identifier；C++ 异常不越过 C ABI；只有 PublisherData 完整创建后才把所有权提交给公开 handle。

`handle->data` 是类型擦除指针。销毁函数必须用相同 allocator、相同真实类型恢复它，并先销毁 Zenoh entity 与 liveliness token，最后释放 RMW handle。部分构造失败不能留下已发布的 graph token。

### 创建 Publisher 是跨三层的提交事务

```text
验证 node / type support / topic / QoS
  -> 规范化 ROS name 与 type hash
  -> 声明 Zenoh publisher/advanced publisher
  -> 声明 MP liveliness token
  -> 构造 PublisherData
  -> allocator 分配 rmw_publisher_t/name
  -> 最后写 handle->data 并返回
```

任一步失败都要逆序撤销。尤其不能先发布 MP token，再因 RMW handle 分配失败留下“图上存在、进程内没有对象”的幽灵 Publisher。可用局部 RAII 对象保存 Zenoh entity/token 和 allocator allocation，最后 `release()` 提交所有权。

销毁则先把公开 handle 从新调用路径隔离，停止发布并撤销 token，再释放 PublisherData、topic name 和 handle。RMW allocator 可能由调用方提供，不能用普通 `delete/free` 混用；创建和销毁必须使用同一 allocator 契约。

### C ABI 的错误状态也是线程局部协议

RMW 函数通常返回空指针或 `rmw_ret_t`，并设置详细错误。边界 catch 要把 `std::bad_alloc`、Zenoh error 和参数错误映射成稳定 RMW 错误，而不是让异常穿过 C。错误字符串的寿命和线程局部存储也要遵守 rcutils 约定；不能返回临时 `std::string::c_str()`。

## Topic 映射

ROS Publisher 映射为 Zenoh publisher/put，Subscription 映射为 subscriber callback。RMW 还要把 ROS 名称、类型哈希、QoS 和节点命名空间编码进 key expression 或附件，使同名但类型不兼容的实体不会被误认为有效匹配。

收到 Sample 后不能直接调用任意 rclcpp 用户回调；RMW 将数据放入 subscription queue，并触发 ROS wait set。这样 Zenoh 接收线程与 ROS Executor 分离，符合上层执行模型。

官方设计给出的数据 key 形式是：

```text
<domain_id>/<fully_qualified_name>/<type_name>/<type_hash>
```

把 `ROS_DOMAIN_ID` 放入 key 可隔离共享 Zenoh 基础设施上的不同 ROS domain；把类型名和类型哈希放入 key，可阻止同名 topic 的不兼容类型直接通信。字符转义必须是规范化的：若发布端和订阅端对 `/`、`%` 或空名称采用不同编码，逻辑上相同的 ROS 名字也无法匹配。

Publish 除 CDR payload 外还携带 attachment：8 字节 sequence、8 字节源时间戳、1 字节 GID 长度以及当前 16 字节 GID。所有多字节字段使用明确的小端编码，不能 `memcpy` 整个 C++ struct，因为 padding、对齐和宿主端序不属于稳定 wire format。

```cpp
void EncodeI64LE(std::vector<std::byte>& out, std::int64_t value) {
  const auto bits = static_cast<std::uint64_t>(value);
  for (unsigned shift = 0; shift != 64; shift += 8) {
    out.push_back(static_cast<std::byte>((bits >> shift) & 0xff));
  }
}
```

这个小函数看似低级，却比直接序列化结构体更能保证跨编译器和跨架构一致。

### Publish 的完整数据动作

```text
ROS message
  -> rosidl typesupport serialize to CDR
  -> build attachment(sequence, source time, GID)
  -> choose ordinary buffer or Zenoh SHM buffer
  -> zenoh Publisher::put
  -> return RMW status
```

serialized publish 从调用者接收已经编码的 CDR，应避免再次 decode/encode；typed publish 则需要 typesupport。两条路径最终必须产生相同 wire payload 与 attachment，否则 rosbag、intra-process bridge 或原始序列化 API 会出现行为差异。

sequence 分配是 PublisherData 的跨线程状态，应使用同一 mutex 或原子 fetch-add，并定义溢出行为。source timestamp 是采样/调用时刻，不等于 Zenoh 接收时间；订阅侧 `rmw_message_info_t` 应区分 source 与 received timestamps。

## Subscription 队列与 ROS Wait Set

固定实现的 `create_subscription_endpoint()` 让回调捕获 `weak_ptr<SubscriptionData>` 和拥有型 endpoint；回调开始时 `weak_ptr::lock()` 临时取得强引用，若对象已析构就直接返回。它从借用的 `Sample` 读取 attachment、payload 与 key，构造 `unique_ptr<Message>`，再调用 `add_new_message()`。这条真实路径见 [endpoint 回调与 `AdvancedSubscriber` 声明](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L701-L838)；Message 持有 Payload、Attachment 和接收时间的字段见 [Message 定义](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.hpp#L45-L59)。

`add_new_message()` 在 `mutex_` 下检查关闭状态、按 QoS depth 丢弃最旧元素、记录 publisher sequence、把唯一拥有 Message 的指针压入 deque。随后它同步触发 RMW 注册的新数据通知 callback，再锁 WaitSet 的 condition mutex、设置 `triggered` 并 `notify_one`。下面是该提交从入队之后的连续源码摘录：

```cpp
message_queue_.emplace_back(std::move(msg));

data_callback_mgr_.trigger_callback();
if (wait_set_data_ != nullptr) {
  std::lock_guard<std::mutex> wait_set_lock(wait_set_data_->condition_mutex);
  wait_set_data_->triggered = true;
  wait_set_data_->condition_variable.notify_one();
}
```

源码位置：[`SubscriptionData::add_new_message`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L1114-L1180)。这段展示了一个需要认真阅读的锁边界：函数进入时已经持有订阅自己的 `mutex_`，因此实际代码在调用 `data_callback_mgr_.trigger_callback()` 时并没有先释放队列锁。`DataCallbackManager` 在自己的 `event_mutex_` 下同步调用注册的 `rmw_event_callback_t`，通常用于通知上层实体已就绪；它不是直接执行 ROS subscription 的业务消息回调。这个次序意味着通知 callback 应保持短小且不重入 `SubscriptionData` 操作，否则它可能等待自己当前持有的 `mutex_`。源码事实见 [`DataCallbackManager::trigger_callback`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/event.cpp#L81-L90)。

```text
Zenoh receive task
  -> validate key/attachment
  -> create owned Message
  -> lock subscription queue
  -> apply history/depth policy
  -> enqueue Message
  -> synchronously invoke RMW on-new-data notification (still holding subscription mutex)
  -> set WaitSet predicate and notify (condition mutex)
  -> unlock subscription mutex

ROS executor thread
  -> rmw_wait marks subscription ready
  -> rmw_take removes one Message
  -> deserialize or return serialized payload
```

这里的真实 RMW 通知 callback 与 ROS Executor 的业务回调有不同职责：前者提示“有新数据可取”，后者稍后通过 `rmw_take` 取出 Message 再运行。把任意用户业务直接放在 Zenoh receive callback，会让网络接收任务承担业务执行时间；真实代码通过队列隔离了这段工作，但它同步触发的 RMW 通知函数仍须满足上述短小、不重入契约。`KEEP_LAST(depth)` 的队列条数有界，空间近似 `O(depth × message_size)`；`KEEP_ALL` 若没有系统级资源上限，就可能在消费者停顿时无限增长。

### WaitSet 注册与消息到达存在竞态

`rmw_wait` 不能只做“检查队列为空 → 注册 condition → 睡眠”，因为消息可能恰好在检查后、注册前到达，callback 看不到 waiter，执行器就会错过本次通知。这个实现把订阅的“队列是否为空”和“附加当前 WaitSet 指针”放在 `SubscriptionData::mutex_` 下检查；若尚无消息，保存 `wait_set_data_`，若已有消息则直接报告 ready，具体见 [`queue_has_data_and_attach_condition_if_not`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L917-L928)。

缩小模型如下：

```cpp
bool SubscriptionData::AttachIfEmpty(WaitCondition& condition) {
  std::lock_guard lock(queue_mutex_);
  if (!queue_.empty() || shutting_down_) return false;
  wait_condition_ = &condition;
  return true;
}

void SubscriptionData::Add(Message message) {
  WaitCondition* condition = nullptr;
  {
    std::lock_guard lock(queue_mutex_);
    ApplyHistoryPolicy(queue_, std::move(message));
    condition = std::exchange(wait_condition_, nullptr);
  }
  if (condition) condition->Trigger();
}
```

这只说明局部结构，不是该仓库源码的重写。真实实现的锁顺序是 callback 持 `SubscriptionData::mutex_` 后获取 `condition_mutex`；`rmw_wait` 则重置 predicate、释放 `condition_mutex` 后才逐个访问实体，避免把反向锁顺序放进这条路径。若你复刻时希望把 RMW 通知 callback 移到订阅锁外，需要用条件谓词或 generation 保留新消息状态，不能只在 `unlock()` 后发一个可能丢失的裸通知。实体锁仍只保护队列、关闭状态与 WaitSet 指针等不变量；WaitSet 的 predicate/通知由另一把 mutex 保护。

固定实现用一个被 `condition_mutex` 保护的 `triggered` 谓词补上“先检查/附加、后真正等待”之间的竞态。`rmw_wait` 先把标志清零，再检查并附加各类实体；如果期间有订阅变为 ready，生产回调会在同一 `condition_mutex` 下置 `triggered=true`。随后等待端持锁调用带谓词的 `condition_variable.wait`，谓词已为真时不睡，虚假唤醒时则重新检查。源码中这套协议的设计注释在 [`rmw_wait_set_data.hpp`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_wait_set_data.hpp#L22-L52)，等待处在 [`rmw_wait`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/rmw_zenoh.cpp#L2235-L2273)。

```cpp
wait_set_data->condition_variable.wait(
  lock, [wait_set_data]() { return wait_set_data->triggered; });
```

`wait(lock, predicate)` 等价于循环检查谓词；普通 `wait` 会先原子释放 mutex 并阻塞，线程因通知或虚假唤醒返回后重新锁住 mutex 再继续。超时版本还可能因期限到达返回，但谓词仍决定数据是否真的 ready。[C++ 条件变量规范](https://eel.is/c%2B%2Bdraft/thread.condition.condvar)明确规定这些步骤。Linux 上，线程竞争同步状态且需要真正睡眠时，标准库实现通常用 futex 把用户态原子状态与内核阻塞/唤醒衔接；无竞争路径仍可在用户态完成，而且 C++ 标准不要求底层一定是 futex。[Linux `futex(2)` 手册](https://man7.org/linux/man-pages/man2/futex.2.html)描述了这种“先在用户态尝试、确需等待才进内核”的机制。

因此一次消息就绪至少有五个不同时间点：Sample 已转成队列拥有的 Message；通知函数已把谓词改为 true 并发出 notify；等待线程从内核等待中解除并变为可运行；OS 调度器稍后给它 CPU 且它重新取得 condition mutex；`rmw_wait` 返回并由 Executor 后续 `rmw_take`，业务 callback 才开始。`notify_one()` 不会替线程分配 CPU，也不会直接调用 ROS 业务 callback。`rmw_wait` 最终还会 detach WaitSet 指针；detach 与入队使用同一订阅 mutex，以避免生产者继续访问已注销的条件对象。

### History 策略需要时间与内存两份指标

`KEEP_LAST(d)` 限制条数，但消息大小可变时内存仍约为 `Σsize(message_i)`，不能只报 depth。覆盖最旧消息适合状态流，却要增加 lost/change 事件统计；`KEEP_ALL` 需要系统级 resource limit 和拒绝语义。队列中的最老 source timestamp 比队列长度更能反映 Executor 是否正在消费陈旧数据。

## Service 映射为 Queryable

官方设计文档说明 Service Server 使用 `Session::declare_queryable`，Client 使用 `Session::get`。请求附件携带 sequence、时间戳和 client GID；回复把相同 sequence 带回，用于关联并发请求。

```text
rmw_send_request
  -> allocate sequence
  -> Session::get(service key, request + attachment)
  -> Queryable callback
  -> rmw_take_request
  -> user service executes
  -> rmw_send_response / Query::reply
  -> ClientData queue
  -> rmw_take_response
```

Zenoh Query 可以多回复，而 ROS Service 期望特定请求-响应关联；适配层必须限制和过滤语义，并处理 timeout 与迟到 Reply。

Client 的 sequence 必须在线程安全的单调计数器中生成。仅按 sequence 查找请求还不够，不同 Client 可能出现相同值，因此响应 attachment 同时带 client GID。等待项可以用 `(client_gid, sequence)` 作为复合键，平均查找 `O(1)`；超时路径和回复路径要竞争同一个完成状态，确保 promise/guard 只完成一次。

Server 端 `Query` 对象必须活到 `rmw_send_response`。如果 `rmw_take_request` 只返回裸指针而不保存拥有型 ZenohQuery，异步执行服务时就会悬空。适配层需要让 pending-request 表持有 Query，并在回复、取消或关闭时删除。

### Server 端把一次请求拆成“数据”和“回复能力”

`rmw_take_request` 与 `rmw_send_response` 不是同一次函数调用。前者在 Executor 线程中取走请求，用户回调运行一段时间之后，后者才发送响应。因此，Server 不能只把反序列化后的 request 放进队列；它还必须保存能够向原 Query 回复的拥有型句柄。可以把队列元素理解为下面的结构等价代码：

```cpp
struct RequestId {
  std::array<std::uint8_t, 16> client_gid;
  std::int64_t sequence;

  bool operator==(const RequestId&) const = default;
};

struct PendingServerRequest {
  RequestId id;                         // ROS 侧可见的关联键
  std::vector<std::byte> cdr_request;   // 请求正文，拥有自己的存储
  std::shared_ptr<OwnedZenohQuery> query; // 以后执行 reply 所需的能力
  std::chrono::steady_clock::time_point received_at;
};
```

这里有三个容易被忽略的 C++ 设计点。

第一，`std::span<std::byte>` 只是一段借用视图，不能跨越 Zenoh callback 保存；入队对象要拥有 payload，或者持有能够延长底层 Sample 生命周期的 owning handle。第二，`steady_clock` 适合计算本地超时，因为系统时钟校准不会让时间倒退；wire attachment 里的 source timestamp 则属于跨进程可观察时间，二者不能混用。第三，`shared_ptr` 并非天然正确：它只解决对象寿命，不解决“只能回复一次”。唯一完成权仍要由状态机约束。

```text
RECEIVED -> TAKEN -> REPLIED
    |          |        |
    +----------+--------+--> CANCELLED / SHUTDOWN
```

`rmw_take_request` 只把 `RECEIVED` 改为 `TAKEN`；`rmw_send_response` 以 `(client_gid, sequence)` 查表，并用锁内状态转换取得一次性的 reply 权。真正调用 `query->reply()` 应放在锁外，否则网络发送可能阻塞整个 Service 队列。若发送失败，条目是否允许重试必须预先定义，不能依赖异常发生后“再决定”。

### Client 端让回复与超时竞争同一个状态

Client 不必为每个请求创建一条线程。更紧凑的结构是：一个原子 sequence 产生器、一张 in-flight 表、一个收到 Zenoh Reply 的 callback，以及由 `rmw_wait` 消费的 completed 队列。

```cpp
enum class Completion : std::uint8_t { waiting, reply_ready, timed_out, closed };

struct InFlight {
  Completion state{Completion::waiting};  // 受 ClientData mutex 保护
  std::vector<std::byte> reply;
  std::chrono::steady_clock::time_point deadline;
};

using InFlightMap = std::unordered_map<RequestId, InFlight, RequestIdHash>;
```

收到回复和发现超时都执行同一种临界区操作：查找条目、确认仍是 `waiting`、转换状态、把 ready 事件移入完成队列。只有赢得状态转换的一方在解锁后触发 WaitSet。这样迟到回复只会被记录或丢弃，不会第二次唤醒已经超时的请求。

哈希表平均查找是 `O(1)`，但容量仍等于并发未完成请求数 `R`，内存近似为 `R × (键 + 状态 + allocator 开销)`；若每个条目还保存请求副本用于重试，则要再加 `Σrequest_size`。超时扫描若每次遍历整张表是 `O(R)`，高并发场景可用按 deadline 排序的小根堆把下一次到期查询降为 `O(1)`、插入和删除变为 `O(log R)`。是否值得增加这套结构，要由最大并发请求量决定。

## Liveliness 实现 Graph

ROS graph 需要知道 Node、Publisher、Subscriber、Service、Client 是否存在。rmw_zenoh 使用 liveliness token 发布实体存在性，订阅 token 变化维护 graph cache。对象析构时撤销 token，异常断连则依靠 Zenoh liveliness 收敛。

优秀之处是复用 Zenoh 原生分布式存在性，而不是复制 DDS discovery；代价是必须设计稳定 key schema，并处理 Router、Session 与 ROS daemon 的启动顺序。

Graph cache 要支持两阶段初始化：先用 `liveliness_get` 获取当前快照，再订阅后续变化。若二者之间存在空窗，恰好上线或离线的实体会永久遗漏。实现还要把匹配计数变化转成 ROS graph guard condition，使 `rclcpp` 查询和事件机制及时醒来。

官方 token 会编码 domain、session、node/entity id、实体种类、namespace、名称、类型和 QoS 等信息。这提高了无中心解析能力，也让 key schema 成为协议 ABI：字段顺序或 escaping 改变必须考虑混合版本部署。

### Graph 不是实体列表，而是一份最终一致的派生索引

收到 token 后，GraphCache 通常不会只保存原始字符串。ROS API 会按 node、namespace、topic、type 查询，因此缓存需要把一次事件投影到多种索引：

```text
entity_id -> EntityInfo
(node_name, namespace) -> entity_ids
topic_name -> publisher_ids / subscription_ids / type_names
service_name -> service_ids / client_ids / type_names
```

原始实体表是事实源，其他表是派生索引。新增和删除必须在同一个写临界区内更新，否则查询线程可能看到“entity 已存在、topic 索引尚未存在”的半完成状态。一个简单实现可以用单个 `std::mutex` 保证一致性；只有确认 graph 查询成为热点后，才值得改成读写锁或不可变快照。Graph 更新频率通常远低于消息频率，优先保证不变量比过早拆锁更重要。

Liveliness 事件还可能发生重排：删除通知可能先于本地观察到的创建通知，快照查询与持续订阅也可能重叠。因此 add/remove 操作必须幂等，不能把“删除未知实体”当作缓存损坏。最小规则如下：

```text
add(id, metadata): 不存在则插入；内容相同则无动作；内容变化则原子替换索引
remove(id):        存在则删除全部索引；不存在则记诊断并无动作
```

如果传输层能提供 incarnation/session identity，应把它纳入实体键，防止进程重启后复用 entity id，使旧删除事件误删新实体。若没有全序版本号，系统只能承诺最终一致，而不能承诺每一瞬间的 graph 查询都是全局强一致。

初始化时更稳妥的顺序是“先建立持续订阅，再取得快照，最后去重合并”，这样不会在快照结束与订阅开始之间留下事件空窗。代价是同一实体可能从快照和订阅各出现一次，所以幂等更新是协议的一部分，而非清理重复日志的便利函数。只有实体集合或匹配计数真正变化时，才递增 graph generation 并触发 guard condition；否则重复事件会导致 Executor 无意义唤醒。

## QoS 是映射表，不是全支持声明

`TRANSIENT_LOCAL` 可借助 AdvancedPublisher cache 和 AdvancedSubscriber 历史查询近似；可靠性、history、depth、拥塞控制也需组合 Zenoh 选项。并非每个 DDS 派生 QoS 都有直接对应物。

文章阅读所对应的官方设计明确列出一些受限项，例如只支持自动 liveliness，并标注 deadline、lifespan 等能力的实现状态。由于 rolling 会持续演进，部署时必须以目标 ROS 发行版分支的设计文档和测试为准，不能把当前 rolling 能力反推到旧发行版。

QoS 兼容判断也不能照搬 Zenoh 的“都可以通信”。ROS graph 需要报告 offered/requested compatibility，适配层必须保留上层语义，即使底层 transport 技术上能够传数据。

### 逐项建立语义映射矩阵

实现者应给每项策略标注“直接、组合、近似、不支持”，并写出不满足条件时的公开行为。下面是阅读设计时应采用的分析框架，不是跨所有发行版不变的能力声明：

| ROS QoS 语义 | 可能使用的 Zenoh 机制 | 映射类别 | 必须额外保存的状态 |
|---|---|---|---|
| `VOLATILE` | 普通 publisher/subscriber | 直接 | 无历史状态 |
| `KEEP_LAST(depth)` | RMW 接收队列裁剪 | 组合 | depth、丢弃计数、当前字节量 |
| `KEEP_ALL` | 不主动覆盖 | 近似 | 全局资源上限与耗尽策略 |
| `TRANSIENT_LOCAL` | advanced publisher cache + 历史查询 | 组合 | cache 深度、发布者寿命、补历史完成状态 |
| `RELIABLE` | 可靠路径及重传相关选项 | 组合 | 丢失检测、兼容性报告；具体能力按版本核对 |
| `BEST_EFFORT` | 非可靠/低开销路径 | 直接或近似 | 丢包统计 |
| deadline | 本地定时器与事件上报 | 组合 | 每实体最近收发时间、deadline generation |
| lifespan | 接收时按 source time 过滤 | 近似 | source timestamp 与时钟假设 |
| manual liveliness | 无等价原语时拒绝或降级 | 不支持/近似 | 明确的兼容性结果 |

“组合”意味着底层的一次 declare 还不够。例如 `TRANSIENT_LOCAL` 至少包含发布端缓存、晚加入订阅者发起历史查询、历史与实时流去重、以及“历史回放已经追平”的边界。若实时样本在历史查询期间到达，只把两路数据拼接会乱序或重复；实现需要 sequence/dedup window，或者明确不保证 DDS 风格的全序。

`KEEP_LAST` 也不仅是 `deque.size() > depth` 就弹出头部。depth 为零是否合法、覆盖是否产生 message-lost 事件、单条超大消息是否受字节预算限制、多个 take 线程是否允许，都属于可观察语义。映射矩阵的价值就是迫使实现者把这些隐藏条件写成代码分支和测试条件。

## 共享内存路径与退化策略

官方设计中的共享内存路径在达到阈值后从 Session 取得 SHM provider，直接在共享缓冲区内进行 CDR 序列化，再把 buffer 所有权移动给 Zenoh。这样避免常规序列化缓冲区到 SHM 的额外 memcpy。

provider 尚未就绪或池耗尽时，发布必须能够退回普通序列化和网络路径，而不是让整个 Publisher 永久失败。由此可见“零复制”是有条件优化：类型支持、构建 feature、Session 配置、payload 大小、内存域和池容量都必须满足。

移动 SHM buffer 后，源对象进入有效但不再拥有数据的状态。C++ 代码不能继续保存其 data pointer；订阅侧借用内存也必须在 loan/return 或 Sample 生命周期内完成。

## C++ 设计重点

RMW 是 C ABI，内部却使用 C++ RAII 对象。创建函数必须把部分构造失败转成 `rmw_ret_t`，并逆序释放 Zenoh entity、queue、guard condition 和 allocator 内存。Opaque C handle 的 `data` 指针是类型擦除边界，所有入口都要验证 implementation identifier。

回调捕获不能让 SubscriptionData 与 Session 形成引用环；关闭时先阻止 callback 入队，再撤销 Zenoh 实体，最后销毁 waitable storage。

### 回调捕获与对象销毁的具体推导

假设 `SubscriptionData` 拥有 Zenoh subscriber，而 subscriber 的 callback 又捕获 `shared_ptr<SubscriptionData>`，就形成强引用环：只有 callback 销毁才释放 Data，只有 Data 析构才 undeclare subscriber。解决方法通常是让 callback 捕获 `weak_ptr`，进入时临时 `lock()`：

```cpp
std::weak_ptr<SubscriptionData> weak_self = self;
auto callback = [weak_self](const zenoh::Sample& sample) {
  auto self = weak_self.lock();
  if (!self) return;                 // 销毁已经开始或完成
  self->OnSample(sample);            // 进入后仍需检查 closing flag
};
```

固定源码中的关闭路径需要沿两个 owner 看。第一，Zenoh callback 捕获 `weak_ptr<SubscriptionData>`；若 `lock()` 成功，当前 callback 的局部 `shared_ptr` 让对象至少活到该 callback 返回。第二，`rmw_destroy_subscription()` 从 `NodeData` 的 `subs_` 表删除这份共享所有权并释放 C handle，但它本身没有调用 `SubscriptionData::shutdown()`，也没有 join 某条 Zenoh 接收线程；实现可在 [`rmw_destroy_subscription`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/rmw_zenoh.cpp#L1028-L1066) 与 [`NodeData::delete_sub_data`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_node_data.cpp#L223-L238) 对照。

当最后一个 `shared_ptr` 离开作用域时，`SubscriptionData` 析构函数才调用 `shutdown()`。该函数在 `mutex_` 下将 `is_shutdown_` 置位、移出 subscriber handles，随后释放锁，再注销 discovery/event callback、撤销 liveliness token 与 Zenoh subscriber，并最终 reset Session；见 [`~SubscriptionData` 与 `shutdown`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L537-L547) 和 [shutdown 实现](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L841-L906)。因而，已成功 `weak_ptr::lock()` 的 callback 不会与 `SubscriptionData` 析构并行，避免 UAF；但工程推导是：`rmw_destroy_subscription()` 返回不等于“所有在途 callback 已完成”，因为 callback 的临时强引用可以延后析构。调用者仍须按 ROS Executor/WaitSet 的实体生命周期约束先停止并解绑使用方，不能把 C handle 的释放当成 callback drain 屏障。

另一个要核对的边界是 `wait_set_data_` 是原始指针，依靠同一把 `mutex_` attach/detach；订阅 shutdown 本身不负责 join ROS Executor。等待返回时的 detach 路径见 [`detach_condition_and_queue_is_empty`](https://github.com/ros2/rmw_zenoh/blob/3b5b9bf424443f9800dd148b5f1cc2053bbc37fe/rmw_zenoh_cpp/src/detail/rmw_subscription_data.cpp#L931-L937)。因此关闭/析构代码应证明 WaitSet 已经 detach 后才释放其 `rmw_wait_set_data_t`，而不能仅凭 `shared_ptr` 保护了 SubscriptionData 就推断 WaitSet 指针同样安全。

本提交的 `shutdown()` 先把 subscriber handles 移到局部变量，再在 `mutex_` 之外调用 Zenoh undeclare。这使任何可能等待 callback 或执行内部清理的调用都不会持有订阅队列锁；从源码只能确认它避免了持 `mutex_` 撤销实体，不能把它扩大解释成 RMW destroy API 会等待所有远端或 OS 线程退出。

## 设计取舍

Router-centric 默认拓扑减少 multicast 依赖、便于跨网段与 ACL；代价是 Router 成为重要基础设施。Vendored zenoh-cpp 保证特性与 ABI 组合，却增加构建时间和升级审核。使用 system Zenoh 更灵活，但调用方必须保证 features 与版本相容。

另一个取舍是完整 graph cache：它满足 ROS 工具链的全图查询，却抵消 Zenoh 尽量少做全局发现的一部分优势。实体数为 `N` 时，缓存空间至少为 `O(N)`；按 topic、node 和类型建立多个索引会进一步用内存换查询速度。

### 容量与时延预算

适配器的瓶颈往往不在 Zenoh API 调用本身，而在边界上的复制、排队和唤醒。可用一张预算表约束部署：

| 资源 | 近似上界 | 超限后的可见后果 |
|---|---|---|
| Subscription 历史 | `Σ(depth_i × max_sample_i)` | 覆盖、拒绝或进程内存增长 |
| 未完成 Client 请求 | `R × metadata + Σreply_buffer` | timeout 管理变慢、内存增长 |
| Service 待处理请求 | `Q × (request + owned query)` | 服务延迟增长，Query 长期占用 |
| GraphCache | `O(N entities + index entries)` | 创建/销毁抖动、查询锁竞争 |
| SHM pool | 并发在途大消息总字节数 | 回退到普通路径或发布失败 |

单条消息从网络到用户回调的延迟可以粗分为：

```text
T = T_transport + T_validate + T_queue_wait + T_deserialize + T_executor_wait
```

其中 `T_queue_wait` 与 `T_executor_wait` 通常最容易被平均值掩盖。评估时至少记录 p50/p99、队列最老消息年龄、覆盖数、WaitSet 唤醒到 take 的时间，而不能只报吞吐。共享内存主要削减 payload copy 与 allocation，并不会消除 Executor 排队。

## 缺点与不适用边界

`rmw_zenoh` 的价值是保留 ROS 2 上层接口，同时使用 Zenoh 的数据空间、查询和 liveliness 原语；这不意味着两套语义天然等价。适配层越完整，自己维护的状态就越多：Subscription history、Service pending query、Client in-flight、GraphCache、QoS 兼容结果和 WaitSet generation 都可能成为新的竞态或容量热点。

Router-centric 默认部署减少了 multicast 依赖，却让 Router 的启动、配置和恢复成为系统可用性的一部分。Session 共享减少连接数量，但 Context 级故障会同时影响多个 Node 和实体。共享内存能够减少大 payload 复制，却需要池容量、loan 生命周期和普通路径回退，不能被当成无条件零复制。

它也不适合用来掩盖应用层实时性问题。ROS Executor 仍决定用户 callback 何时运行；`KEEP_LAST` 仍可能交付已经陈旧的数据；底层可靠路径也不能证明控制命令在 deadline 内被业务线程处理。安全控制需要额外的序列号、数据年龄、超时、安全状态和端到端确认。

最后，rolling 的设计状态不能代表所有 ROS 发行版。若系统依赖特定 QoS、事件或 SHM 行为，应固定 ROS 发行版、`rmw_zenoh` commit、zenoh-cpp feature 集与 Router 配置，并用该组合的上层 RMW 行为测试验证，而不是只证明两个 Zenoh endpoint 能互通。

## 可迁移的语义适配方法

这个项目最值得迁移的是“语义适配表”。先逐项写出上层可观察保证，再判断底层原语属于直接提供、组合实现、近似实现还是不支持；随后为组合项明确附加状态、完成条件和销毁顺序。

第二个方法是把异步 transport 与上层执行器隔开。接收 callback 只验证、取得数据所有权并更新 ready generation，真正的用户 callback 仍由 Executor 线程运行。这个边界同样适用于把设备中断、网络库或共享内存队列接入自研调度器。

第三个方法是让 C ABI 成为明确的提交边界：C++ RAII 对象先完整构造，最后才把 opaque pointer 提交给 C handle；销毁时先阻止新入口和 callback，再逆序撤销外部实体，最后用原 allocator 释放 handle。不要让异常、临时字符串或半构造对象跨过 ABI。

第四个方法是把“只完成一次”建模为状态机。Service reply 与关闭、Client reply 与 timeout、WaitSet attach 与数据到达都不是普通函数调用顺序，而是多个执行上下文竞争同一个完成权。共享指针只能延长寿命，不能替代状态转换。

## 在真实 ROS 2 工作空间中接入

这不是单独运行 Zenoh pub/sub 示例，而是让真实 ROS 2 节点经过 RMW。部署时先固定 ROS 发行版与对应 `rmw_zenoh` 分支，再使用该分支随附的 Router 和 JSON5 配置；不要混用 rolling 的二进制、旧发行版的配置与另一版本的 zenoh-cpp。

```text
终端 A：启动该发行版提供的 rmw_zenoh router
终端 B：设置 RMW_IMPLEMENTATION=rmw_zenoh_cpp，运行 talker
终端 C：设置相同实现与 domain，运行 listener
终端 D：运行 ros2 node/topic/service 命令观察 GraphCache 的上层投影
```

学习时按四个层次观察，而不是看到消息打印就结束：

1. **进程层**：一个 Context 是否只建立预期数量的 Session，Router 退出后节点怎样报告和恢复。
2. **图层**：`ros2 node list`、`topic info -v` 看到的实体是否与 liveliness token 一致，实体退出后多久消失。
3. **数据层**：topic key、type hash、attachment sequence、source/received timestamp 是否贯通到 `rmw_message_info_t`。
4. **执行层**：Zenoh callback 只负责入队，用户 callback 是否始终由 ROS Executor 线程执行；队列积压时哪项 QoS 生效。

接入自己的包时，先用默认配置完成单机 volatile topic，再逐项开启可靠性、transient local、服务和 SHM。一次同时改变 RMW、Router、QoS 和网络配置，会让语义错误与部署错误无法区分。配置文件属于运行时接口：应随应用版本化，并记录 Router mode、listen/connect endpoint、scouting、队列和 SHM 参数。

### 一个可观察的最小开发任务

最小案例可由三个节点组成：传感器节点以固定频率发布带递增序号的大消息；处理节点使用有限 `KEEP_LAST` 队列并暴露“查询最后序号”的 Service；监控节点订阅 graph 与统计 topic。然后依次制造慢消费者、晚加入订阅者、Service 超时、Router 重启和 SHM 池不足。

实现目标不是做故障演示，而是把每个现象对应回内部对象：慢消费者对应 `SubscriptionData` history；晚加入对应 advanced history query；Service 超时对应 ClientData in-flight 状态；Router 重启对应 Session 与 GraphCache 收敛；SHM 不足对应 buffer acquisition 的普通路径回退。完成这一映射后，使用者才真正知道配置改变了哪段源码行为。

## 最小复刻：从 C ABI 建立可等待的 topic 闭环

不要从全部 RMW 函数开始。第一版只实现 Context、serialized Publisher、serialized Subscription、单实体 WaitSet 和对称销毁，让一条消息能够从 C ABI 进入 Zenoh，再回到 `rmw_take_serialized_message()` 风格的出口。

一个便于保持依赖方向的文件树可以是：

```text
mini_rmw_zenoh/
  include/mini_rmw/
    context.hpp          Session 与关闭屏障
    key_codec.hpp        ROS 名称和类型映射
    attachment.hpp       sequence / timestamp / GID 编解码
    message.hpp          拥有型 CDR 消息
    subscription.hpp     history、generation 与 take
    wait_set.hpp         attach、ready 与 shutdown
    publisher.hpp        sequence 与 put
  src/
    c_api_init.cpp       C handle 校验和异常边界
    c_api_publisher.cpp
    c_api_subscription.cpp
    c_api_wait.cpp
  tests/
    topic_lifecycle.cpp
    wait_race.cpp
    shutdown_race.cpp
```

`context.hpp` 不应该反向依赖 C handle。内部核心先使用普通 C++ 类型表达所有权，最外层文件再负责类型擦除：

```cpp
struct ContextData {
  std::shared_ptr<ZenohSession> session;
  std::mutex lifecycle_mutex;
  bool closing{false};
};

struct OwnedMessage {
  std::vector<std::byte> cdr;
  std::int64_t sequence{};
  std::int64_t source_time_ns{};
  std::array<std::uint8_t, 16> publisher_gid{};
  std::chrono::steady_clock::time_point received_at;
};
```

`std::vector<std::byte>` 表示队列元素拥有 CDR 存储，Zenoh callback 返回后仍可安全 take。`system_clock` 可能因时间校准跳变，不能用来计算本地排队时长；因此 wire source time 与本机 `steady_clock` 接收时刻必须分字段保存。

Subscription 的最小状态要把队列、关闭标志和 wait generation 放在同一同步边界：

```cpp
class SubscriptionData {
 public:
  bool Enqueue(OwnedMessage message);
  std::optional<OwnedMessage> Take();
  bool AttachIfNotReady(WaitSetState& wait, std::uint64_t generation);
  void Close();

 private:
  std::mutex mutex_;
  std::deque<OwnedMessage> queue_;
  std::size_t depth_{10};
  std::uint64_t generation_{0};
  bool closing_{false};
  WaitSetState* waiter_{nullptr};  // 非拥有，只在注册协议约束期内有效
};
```

这里故意没有让 `WaitSetState*` 变成 `shared_ptr`。WaitSet 与实体之间是一次等待期间的临时注册关系，不应因为 subscription 保存强引用就延长整个 Executor 的寿命。裸指针只是表达非拥有关系，安全性来自 attach/detach 协议：实体锁内注册，状态变化后取走指针，锁外触发；WaitSet 销毁前必须从所有实体解绑。

C ABI handle 只在内部对象完整构造后获得所有权：

```cpp
extern "C" rmw_subscription_t* mini_create_subscription(/* ... */) noexcept {
  try {
    auto data = std::make_unique<SubscriptionData>(/* ... */);
    auto handle = AllocateSubscriptionWithRmwAllocator();
    handle->implementation_identifier = kMiniIdentifier;
    handle->data = data.release();  // 最后一步才提交
    return handle;
  } catch (const std::bad_alloc&) {
    SetError("allocation failed");
  } catch (const std::exception& e) {
    SetError(e.what());
  } catch (...) {
    SetError("unknown C++ exception");
  }
  return nullptr;
}
```

`noexcept` 与 catch-all 共同保护 ABI：异常不能穿过 C 调用者。`unique_ptr` 在提交前负责失败回滚；`release()` 之后，destroy 函数必须恢复相同真实类型，并使用创建 handle 时的 allocator 对称释放。生产实现还要避免直接保存 `e.what()` 的临时地址，而应复制进 RMW/rcutils 规定的错误存储。

先实现单 Context/Session 与最简单 volatile topic；再加入 CDR、key 规范化和 attachment；随后建立 Subscription 队列与 rmw_wait；再实现 graph token/cache；之后做 Service 的 pending Query 生命周期；最后才逐项映射 QoS、事件和共享内存。

更具体的代码增量应保持每一步都能被上层调用：

1. 定义 `ContextData`、implementation identifier、allocator 契约和 create/destroy 对称性，只开放 init/shutdown。
2. 定义规范化 key 与显式 little-endian attachment codec，用 serialized message 打通单向 topic。
3. 加入 `SubscriptionData` owning queue、generation counter 与 WaitSet 注册协议，再接 typed deserialize。
4. 用 liveliness token 表示 NN/MP/MS 等实体，建立单事实表与派生索引，并把真实变化连接到 graph guard。
5. 实现 Client in-flight 与 Service owned-query 两张状态表，先保证一次完成和关闭，再增加并发。
6. 为每项 QoS 填写直接/组合/近似/不支持矩阵，让兼容判断与数据路径读取同一份归一化策略。
7. 最后加入 advanced history、SHM、事件统计和性能优化；这些路径都必须保留普通路径作为语义基线。

每新增一种实体，都要同时回答五个问题：谁拥有它；回调在哪个线程进入；哪个锁保护状态；销毁怎样阻止新工作并等待旧工作；失败到第几步需要撤销哪些资源。答不出其中任意一项，就还没有完成工业级生命周期设计。

每一步都要运行上层 RMW conformance 行为，而不是只运行 Zenoh pub/sub 示例。最低完成标准包括：错误 implementation identifier 被拒绝；销毁后 callback 不再入队；相同 topic 不同 type hash 不匹配；wait 能被数据与 shutdown 唤醒；请求超时和回复只完成一次；QoS 不支持项被明确报告；SHM 失败能退化到普通路径。
