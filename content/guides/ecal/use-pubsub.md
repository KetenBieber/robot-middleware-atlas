# eCAL 使用教程：从 Core Publisher/Subscriber 到可解释的 callback 生命周期

这一页统一按本地固定 eCAL 提交 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 的 Core API 编写。先用最小 Publisher/Subscriber 把环境跑通，再沿 public facade 追到 Gate、Impl 和 callback 线程；完整三进程工程放在下一页。

## 先明确 public facade 并不是底层实现的唯一 owner

业务代码看见的是：

~~~text
CPublisher / CSubscriber
       |
       | public facade
       v
PublisherImpl / SubscriberImpl
       |
       v
Gate / Registration / Transport
~~~

固定 `CPublisher` 构造函数先创建 `shared_ptr<CPublisherImpl>`，随后把它注册进 `PubGate`；而 facade 自己保存的是 `weak_ptr<CPublisherImpl>`。`CSubscriber` 使用相同方向的设计。

这意味着：

- facade 不需要和 Gate 形成强引用环；
- Gate 的 registry 是实现对象的长期 strong owner；
- facade 每次 `Send()` / 设置 callback 时先 `weak_ptr::lock()` 临时取得强引用；
- facade 析构通过 Gate `Unregister()` 结束 registry 所有权。

这是很典型的 **Facade + Registry + weak handle** 组合。`weak_ptr` 不是为了“更快”，而是明确表达：公共句柄可以访问实现，但不能单独决定整个 runtime implementation 的寿命。

## 固定版本的 Initialize 形状

这一版主 API 是：

~~~cpp
if (!eCAL::Initialize("atlas_process")) {
  return 1;
}

{
  // Publisher / Subscriber 必须在 Finalize 前离开作用域
}

eCAL::Finalize();
~~~

不要把旧主版本的 `Initialize(argc, argv, ...)` 示例混进这套固定源码分析。命令行配置如何传入应按对应版本的 Configuration/部署方式处理。

## 最小 Publisher：直接使用 eCAL::core

~~~cpp
#include <ecal/ecal.h>
#include <ecal/pubsub/publisher.h>

#include <chrono>
#include <cstdint>
#include <string>
#include <thread>

int main() {
  if (!eCAL::Initialize("atlas_sender")) {
    return 1;
  }

  int result = 0;
  {
    const eCAL::SDataTypeInformation type{
        "atlas.status", "text", "sequence text"};
    eCAL::CPublisher publisher("/atlas/status", type);

    std::uint64_t sequence = 0;
    while (eCAL::Ok()) {
      const std::string payload = "seq=" + std::to_string(sequence++);
      if (!publisher.Send(payload)) {
        // 固定实现无订阅者时也会返回 false。
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }

  eCAL::Finalize();
  return result;
}
~~~

这里直接使用 Core `CPublisher::Send(const std::string&)`，不依赖额外 string message wrapper。

固定 `CPublisher::Send()` 有一个很容易误解的语义：如果 `GetSubscriberCount()==0`，它会刷新发送统计，但直接返回 `false`，因为没有真正向任何订阅者发送。因此：

~~~text
Send == false
不一定等于
本地 Publisher 对象失效
~~~

它也可能只是当前 soft-state 连接表还没有任何 Subscriber。

## 最小 Subscriber：callback 参数是 borrowed bytes

~~~cpp
#include <ecal/ecal.h>
#include <ecal/pubsub/subscriber.h>

#include <chrono>
#include <iostream>
#include <string_view>
#include <thread>

int main() {
  if (!eCAL::Initialize("atlas_receiver")) {
    return 1;
  }

  {
    const eCAL::SDataTypeInformation type{
        "atlas.status", "text", "sequence text"};
    eCAL::CSubscriber subscriber("/atlas/status", type);

    subscriber.SetReceiveCallback(
        [](const eCAL::STopicId& publisher_id,
           const eCAL::SDataTypeInformation&,
           const eCAL::SReceiveCallbackData& data) {
          const auto* bytes = static_cast<const char*>(data.buffer);
          const std::string_view payload(bytes, data.buffer_size);

          std::cout << publisher_id.topic_name
                    << " clock=" << data.send_clock
                    << " payload=" << payload << '\n';

          // payload 只是借用视图，不要保存到 callback 返回以后。
        });

    while (eCAL::Ok()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    subscriber.RemoveReceiveCallback();
  }

  eCAL::Finalize();
}
~~~

`SReceiveCallbackData::buffer` 是 `const void*`，它没有给业务永久所有权。若 worker 要在 callback 返回后继续使用数据，必须复制到自己拥有的对象，或者建立明确的底层 buffer lease 协议。

例如：

~~~cpp
std::string owned(
    static_cast<const char*>(data.buffer),
    data.buffer_size);
worker_queue.try_push(std::move(owned));
~~~

复制有成本，但所有权最容易证明。Zero-copy 方案必须额外处理 buffer 何时可复用、慢 worker 是否拖住共享内存槽以及关闭时谁归还 lease。

## callback 实际在哪个线程？

public Subscriber 不要求 main 线程手动 `handle()`。底层 UDP/TCP reader 或 SHM observer 取得数据后进入 SubGate，再进入 `CSubscriberImpl::ApplySample()`，最终调用用户 callback。

因此主线程和 callback 至少是两个独立执行上下文：

~~~text
main thread
  -> lifecycle / control loop

receive execution context
  -> layer receive
  -> SubGate
  -> SubscriberImpl::ApplySample
  -> user callback
~~~

这就是为什么 callback 与 main 同时读写普通变量时必须有 mutex/atomic；也解释了为什么 callback 内做磁盘 I/O、推理或同步 RPC 会把 transport delivery 延迟一起拉长。

## 固定实现的 receive callback mutex 是重要边界

`CSubscriberImpl::ApplySample()` 在入口获取 `m_receive_callback_mutex`，而且用户 callback 返回之前一直不释放。

`SetReceiveCallback()` 与 `RemoveReceiveCallback()` 也使用同一把非递归 mutex。

因此不要在 callback 内对同一个 Subscriber 直接调用 `RemoveReceiveCallback()` 或重新 `SetReceiveCallback()`：固定版本存在同线程重入自死锁路径。

更通用的运行时设计通常会：

~~~text
lock
  -> copy callback / owner snapshot
unlock
  -> invoke user code
~~~

但这只解决“不要持 registry lock 执行任意用户代码”。如果注销 API 还承诺返回后没有任何 in-flight callback，则还需要额外的 quiescence / in-flight 计数协议。

## callback 模式与 Read 模式不是同一种数据语义

固定 Subscriber 没有 callback 时，会把最新 payload 写入：

~~~text
std::string m_read_buf
std::mutex m_read_buf_mutex
std::condition_variable m_read_buf_cv
bool m_read_buf_received
~~~

下一条消息可以覆盖上一条，所以同步 Read 本质是 **latest-value mailbox**，不是历史 FIFO。

这很适合“我只需要最新状态”的控制/监控逻辑，却不适合“每个命令必须执行一次”的事件流。

## callback 后面应该放什么 STL？

不要默认使用无界 `std::queue`。

| 数据语义 | 更合适的结构 | 主要行为 |
|---|---|---|
| 最新状态 | `optional<T> + mutex + version` | 新值覆盖旧值，内存 O(S) |
| 有界事件队列 | `deque<T>` + capacity | 明确 drop-old/drop-new/block |
| 固定高频环 | `array<Slot,N>` + head/tail | 稳态少分配，但并发协议更复杂 |

例如状态流：

~~~cpp
class LatestSample {
 public:
  void publish(Sample sample) {
    std::lock_guard<std::mutex> lock(mutex_);
    latest_ = std::move(sample);
    ++version_;
  }

  std::optional<Sample> read_after(std::uint64_t& seen) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!latest_ || seen == version_) return std::nullopt;
    seen = version_;
    return *latest_;
  }

 private:
  std::mutex mutex_;
  std::optional<Sample> latest_;
  std::uint64_t version_{};
};
~~~

而事件流如果必须有历史完整性，就需要有界队列、sequence、overflow policy，必要时还要应用层 ACK。容器类型应该来自数据语义，而不是习惯。

## 为什么同 topic 可以有多个 Subscriber？

固定 `CSubGate` 使用：

~~~cpp
std::unordered_multimap<
    std::string,
    std::shared_ptr<CSubscriberImpl>>
~~~

`unordered_multimap` 直接表达一个 topic 名对应多个本进程 Subscriber。分发时它在 shared lock 下通过 `equal_range` 找出目标，然后复制成 `vector<shared_ptr<...>>` 快照，释放 Gate 锁后再调用 `ApplySample()`。

这把“registry 结构保护”和“任意用户 callback 执行”拆开，是中间件 registry 设计里非常值得迁移的模式。

## 从基础 API 进入完整工程

现在已经知道 Publisher/Subscriber 的外观和 callback 生命周期，下一步不要直接跳到 MQTT Bridge。先做[三进程 eCAL 闭环工程](closed-loop-project.md)：

~~~text
source -> /atlas/raw -> relay -> /atlas/processed -> observer
~~~

项目页会完整展开 `unordered_multimap / map / set / vector / CExpirationMap / CExpandingVector`，以及 Linux `shm_open/mmap/flock` 与 Windows `CreateFileMapping/MapViewOfFile` 的对应关系。

## 常见故障按层定位

| 现象 | 第一检查层 |
|---|---|
| Monitor 看不到进程 | Initialize / 配置 / 进程生命周期 |
| 有 Publisher/Subscriber 但 Send false | soft-state connection count |
| 同机通、跨机不通 | registration network / 网卡 / 防火墙 / transport |
| callback 频率越来越低 | callback WCET / 接收线程阻塞 / 下游 I/O |
| 大消息吞吐异常 | SHM 是否实际选中 / staging copy / zero-copy lease |
| 退出卡住或死锁 | callback under mutex / worker join / Finalize 顺序 |

## 关闭顺序

~~~text
停止业务 producer
  -> 不再接收新的应用任务
  -> RemoveReceiveCallback / 销毁 Subscriber
  -> drain/cancel 应用 worker queue
  -> join worker threads
  -> 销毁 Publisher/Subscriber facade
  -> eCAL::Finalize()
~~~

如果 worker 还会调用 eCAL API，就必须在 Finalize 前结束。若 callback 正在执行，还要考虑 in-flight callback，而不是只设置一个 stopping flag。

## 本页验收

- Publisher 和 Subscriber 都按固定 Core API 编写；
- callback 不保存 borrowed buffer；
- main/callback 跨线程状态有同步；
- callback 不在自身持有的 callback mutex 上做 Set/Remove 重入；
- 数据语义决定 latest-slot、bounded queue 或 ring，而不是默认无界 queue；
- Publisher/Subscriber 生命周期短于进程级 eCAL runtime。
