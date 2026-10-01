# Nodelet 与进程内通信：ROS1 怎样绕开大消息序列化

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）；Nodelet runtime 固定到 nodelet_core `5ed9cabe9388d48a8e228f682d005f313b0a7e89`。

ROS1 最明显的性能问题之一，是相机、点云等大消息跨进程时必须经过 serialization、socket 和 deserialization。Nodelet 的思路不是把 TCP 优化到极致，而是直接改变部署边界：让多个逻辑组件进入同一个进程和地址空间。

## 1. 为什么同进程以后问题本质变了

跨进程：

~~~text
Process A address space
   Message object
      |
   serialize
      |
   bytes
      |
   kernel/socket
      |
Process B address space
   deserialize
      |
   Message object
~~~

同进程：

~~~text
one address space

Publisher
   |
boost::shared_ptr<Message>
   |
Subscriber
~~~

同一个 raw pointer 在不同进程没有意义；在同一个进程里，共享同一 C++ object 是可行的。

Nodelet 的 no-copy 机会来自“地址空间相同”这个前提，而不是来自一个特殊 wire protocol。

## 2. roscpp 本身已经拥有 intra-process link

Nodelet 并没有自己重写 pub/sub。

roscpp 已经有：

~~~text
IntraProcessSubscriberLink
IntraProcessPublisherLink
~~~

Publication 在 publish 前检查 subscriber 类型：

~~~cpp
bool nocopy = false;
bool serialize = false;

if (m.type_info && m.message)
{
  p->getPublishTypes(
      serialize,
      nocopy,
      *m.type_info);
}
else
{
  serialize = true;
}
~~~

如果存在远端 transport subscriber，就必须准备 SerializedMessage；如果 compatible subscriber 都在当前进程，则可沿 message object path 交付。

因此 Nodelet 真正做的是：

> 把本来会运行成多个 ROS node process 的 component 装进同一个 roscpp process，使已有 intra-process delivery path 生效。

## 3. 为什么不能只把所有类 new 在 main() 里

当然可以手写：

~~~cpp
int main()
{
  Camera camera;
  Filter filter;
  Planner planner;
  ...
}
~~~

但这样部署拓扑、组件类型和进程二进制完全绑定。

Nodelet 需要达到：

~~~text
stable manager process
+
runtime-selectable component plugins
~~~

所以引入 pluginlib。

## 4. Loader 为什么使用 pluginlib::ClassLoader

固定源码：

~~~cpp
typedef pluginlib::ClassLoader<Nodelet>
    Loader;

boost::shared_ptr<Loader> loader(
    new Loader(
      "nodelet",
      "nodelet::Nodelet"));

create_instance_ =
    boost::bind(
      &Loader::createInstance,
      loader,
      boost::placeholders::_1);

refresh_classes_ =
    boost::bind(
      &Loader::refreshDeclaredClasses,
      loader);
~~~

Manager 只依赖 `nodelet::Nodelet` 抽象。

具体：

~~~text
camera_pkg/Driver
image_proc/Rectify
pointcloud/Filter
...
~~~

在运行时由 class loader 创建。

这里动态装载解决的是“部署可组合性”，不是通信本身。

## 5. Nodelet 是对象，不是 OS process

Loader 保存：

~~~cpp
typedef boost::ptr_map<
    std::string,
    ManagedNodelet>
    M_stringToNodelet;

M_stringToNodelet nodelets_;
~~~

map 的 key 是逻辑 Nodelet name，value 是当前 manager process 里真正构造出来的对象。

这意味着：

~~~text
多个 Nodelet
  share:
    process
    virtual address space
    heap
    loaded libraries
    worker pool
~~~

它们并没有 Linux process isolation。

## 6. ManagedNodelet 为什么同时拥有对象和 callback queues

固定源码：

~~~cpp
struct ManagedNodelet : boost::noncopyable
{
  detail::CallbackQueuePtr st_queue;
  detail::CallbackQueuePtr mt_queue;

  NodeletPtr nodelet;

  detail::CallbackQueueManager*
      callback_manager;

  ManagedNodelet(
      NodeletPtr nodelet,
      detail::CallbackQueueManager* cqm)
    : st_queue(
        new detail::CallbackQueue(
          cqm, nodelet))
    , mt_queue(
        new detail::CallbackQueue(
          cqm, nodelet))
    , nodelet(std::move(nodelet))
    , callback_manager(cqm)
  {
    callback_manager->addQueue(
        st_queue, false);

    callback_manager->addQueue(
        mt_queue, true);
  }

  ~ManagedNodelet()
  {
    callback_manager->removeQueue(st_queue);
    callback_manager->removeQueue(mt_queue);
  }
};
~~~

这里体现一个非常重要的 lifecycle 原则：

> 可执行 work queue 必须和拥有它的业务对象一起管理。

如果 unload 只 `delete Nodelet`，worker thread 里已经排队的 callback 仍可能访问已析构对象。

ManagedNodelet 把“组件 + 执行入口”绑成一个生命周期单元。

## 7. 为什么 callback_manager_ 必须比 nodelets_ 活得久

`Loader::Impl`：

~~~cpp
boost::shared_ptr<
    detail::CallbackQueueManager>
    callback_manager_; // Must outlive nodelets_

typedef boost::ptr_map<
    std::string,
    ManagedNodelet>
    M_stringToNodelet;

M_stringToNodelet nodelets_;
~~~

ManagedNodelet 析构时要调用：

~~~cpp
callback_manager->removeQueue(...)
~~~

所以 manager 若先析构，Nodelet 后析构就会触发悬空指针。

字段的 ownership 顺序直接决定 shutdown 是否安全。

这类“某个 scheduler 必须活得比所有 scheduled object 久”的约束，在 Executor、ThreadPool、event loop 中普遍存在。

## 8. load() 怎样把 plugin 接进 Runtime

核心路径：

~~~cpp
p = impl_->create_instance_(type);

ManagedNodelet* mn =
    new ManagedNodelet(
      std::move(p),
      impl_->callback_manager_.get());

impl_->nodelets_.insert(
    const_cast<std::string&>(name),
    mn);

mn->nodelet->init(
    name,
    remappings,
    my_argv,
    mn->st_queue.get(),
    mn->mt_queue.get());
~~~

步骤是：

~~~text
plugin type
   |
ClassLoader
   |
Nodelet object
   |
ManagedNodelet
   |
register ST/MT queues
   |
Nodelet::init
   |
onInit
~~~

只有 callback queue 先建立并交给 Nodelet，`onInit()` 创建的 subscriber/timer 才能进入正确 execution domain。

## 9. Nodelet::init 为什么有四个 NodeHandle

固定源码：

~~~cpp
private_nh_.reset(
    new ros::NodeHandle(
      name,
      remapping_args));

nh_.reset(
    new ros::NodeHandle(
      ros::names::parentNamespace(name),
      remapping_args));

mt_private_nh_.reset(
    new ros::NodeHandle(
      name,
      remapping_args));

mt_nh_.reset(
    new ros::NodeHandle(
      ros::names::parentNamespace(name),
      remapping_args));

private_nh_->setCallbackQueue(st_queue);
nh_->setCallbackQueue(st_queue);

mt_private_nh_->setCallbackQueue(mt_queue);
mt_nh_->setCallbackQueue(mt_queue);

inited_ = true;

this->onInit();
~~~

两个维度组合：

~~~text
namespace:
  normal
  private

execution:
  single-thread queue
  multi-thread queue
~~~

于是 Nodelet developer 可以选择不同 NodeHandle，把不同 callback 放到不同并发语义。

## 10. CallbackQueueManager 不是简单 ThreadPool

构造时：

~~~cpp
tg_.create_thread(
    boost::bind(
      &CallbackQueueManager::managerThread,
      this));

size_t num_threads =
    getNumWorkerThreads();

thread_info_.reset(
    new ThreadInfo[num_threads]);

for (size_t i = 0;
     i < num_threads;
     ++i)
{
  tg_.create_thread(
      boost::bind(
        &CallbackQueueManager::workerThread,
        this,
        &thread_info_[i]));
}
~~~

存在：

~~~text
one manager thread
+
N worker threads
~~~

Nodelet queue 有 work 时先进入 waiting collection；manager thread 再决定放到哪个 worker queue。

这比所有 Nodelet 共用一个普通 global CallbackQueue 更细。

## 11. threaded queue 和 single-thread queue 怎样区别调度

manager thread 里：

~~~cpp
if (info->threaded)
{
  ti = getSmallestQueue();
}
else
{
  boost::mutex::scoped_lock lock(
      info->st_mutex);

  if (info->in_thread == 0)
  {
    ti = getSmallestQueue();

    info->thread_index =
        ti - thread_info_.get();
  }
  else
  {
    ti =
        &thread_info_[
          info->thread_index];
  }

  ++info->in_thread;
}
~~~

multi-thread queue：

~~~text
new work
 -> least-loaded worker
~~~

single-thread queue：

~~~text
if no work in progress:
  choose least-loaded worker

if already executing:
  keep assigning to same worker
~~~

这保持 single-thread callback queue 的串行执行约束，同时允许不同 Nodelet 之间并行。

## 12. getSmallestQueue 为什么是简单负载均衡

~~~cpp
size_t size = ti.calling;

if (size == 0)
{
  return &ti;
}

if (size < smallest)
{
  smallest = size;
  smallest_index = i;
}
~~~

调度依据是每个 worker 的 pending/calling 数量，而不是 callback WCET。

所以：

~~~text
worker A:
  1 x 100 ms task

worker B:
  2 x 1 ms tasks
~~~

按 queue count 看，A 看起来更空，实际完成时间可能更差。

这说明 Nodelet CallbackQueueManager 是通用并发调度，不是 duration-aware 或 realtime scheduler。

## 13. unload 为什么不能忽略 callback lifetime

`Loader::unload`：

~~~cpp
Impl::M_stringToNodelet::iterator it =
    impl_->nodelets_.find(name);

if (it != impl_->nodelets_.end())
{
  impl_->nodelets_.erase(it);
  return true;
}
~~~

看起来只是 map erase，但 `boost::ptr_map` 拥有 ManagedNodelet，erase 触发对象销毁，ManagedNodelet 析构又把 ST/MT queues 从 CallbackQueueManager 移除。

所以容器 ownership 是 shutdown protocol 的一部分，而不仅是“方便查找”。

## 14. no-copy 为什么仍然需要 shared_ptr

假设 Publisher 函数：

~~~cpp
void publishFrame()
{
  Message msg;
  pub.publish(msg);
}
~~~

函数返回后栈对象结束，但 Subscriber callback 可能稍后才运行。

如果 intra-process link 只保存裸指针：

~~~text
Publisher returns
 -> object destroyed
 -> queued Subscriber dereferences dangling pointer
~~~

shared ownership 让 message object 活到最后一个使用者结束。

这就是“zero-copy”仍然需要严格 lifetime protocol 的原因。

## 15. Nodelet 的 zero-copy 边界在哪里

即使 transport 层共享同一个 Message object，业务 pipeline 仍可能发生：

- OpenCV format conversion；
- image resize；
- point cloud filtering 生成新 buffer；
- GPU upload/download；
- mutable subscriber 触发 copy；
- allocator 重新分配。

所以更准确的说法是：

> Nodelet 允许 ROS intra-process delivery 避免 transport serialization/copy；它不保证整条算法 pipeline 零内存搬运。

## 16. 同进程优化换掉了什么故障隔离

Nodelet 的代价非常直接：

~~~text
普通 ROS nodes:
  process A crash
  process B may survive

Nodelet manager:
  one plugin segfault
  whole manager process may die
~~~

同时还共享：

~~~text
heap / allocator
worker CPU budget
process address space
loaded symbols
global library state
~~~

因此 Nodelet 不是无条件“高级做法”，而是：

~~~text
copy / serialization cost
        vs
fault isolation
~~~

之间的部署选择。

## 17. 相机 pipeline 为什么特别适合观察这个取舍

例如：

~~~text
camera driver
  -> rectify
  -> resize
  -> detector
~~~

每帧 6 MB、30 Hz。

拆成 4 个独立进程时，大 payload 可能重复跨进程序列化。

装入同一个 Nodelet manager 后：

~~~text
shared message object
  -> component callbacks
~~~

transport-level memory bandwidth 显著下降。

但 detector 崩溃也可能带走 camera driver。

这就是性能优化如何改变 failure domain。

## 18. Nodelet 与共享内存 IPC 的根本区别

Nodelet：

~~~text
same virtual address space
raw pointer/shared_ptr is meaningful
process lifetime shared
~~~

共享内存 Runtime：

~~~text
different virtual address spaces
same physical/shared pages
raw pointer generally not portable
need offset/handle
need cross-process reclaim
~~~

所以不能把 `boost::shared_ptr` 直接放进 shared memory，就认为跨进程 zero-copy 完成了。

像 iceoryx2 这样的系统还必须解决：

~~~text
offset addressing
loan
fan-out ownership
borrow
release
dead process cleanup
~~~

Nodelet 是理解这些机制非常好的前置：它先展示了“只要避免跨地址空间，no-copy 会变得多简单”，然后共享内存告诉我们跨进程时还缺哪些协议。
