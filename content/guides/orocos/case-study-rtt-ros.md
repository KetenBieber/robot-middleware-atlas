# 项目案例：`rtt_ros_integration` 如何连接实时组件与 ROS 生态

[`orocos/rtt_ros_integration`](https://github.com/orocos/rtt_ros_integration) 将 RTT 的 TaskContext、Port、Operation 和 typekit 接入 ROS 的 topic、parameter、clock、tf、actionlib 与部署体系。它的核心价值是保持实时组件内部边界，同时把非实时 ROS 功能放在适配层。

先用一个控制任务说明它解决的问题。假设关节控制器必须每 1 ms 计算一次力矩，而操作者希望继续使用 ROS 的消息、录包和可视化工具。直接在控制循环里调用 `ros::Publisher::publish()` 很方便，却会把序列化、动态内存和网络阻塞带进高优先级线程。`rtt_ros_integration` 的做法是让控制器只读写 RTT Port，再由桥接插件在非实时线程与 ROS topic 交换数据：

```text
ROS 工具与节点 ←→ 非实时 transport 适配层 ←→ 有界 RTT Port ←→ 实时控制组件
```

因此，本文研究的不是“怎样在 Orocos 里调用一个 ROS API”，而是更重要的工程问题：怎样连接两套线程、生命周期和类型系统，同时不让 ROS 的不确定性悄悄进入实时闭环。

几个会反复出现的名词可以先这样理解：

| 名词 | 直观含义 |
|---|---|
| `TaskContext` | 一个可部署组件，拥有 Port、Operation、属性和生命周期 |
| Port | 组件之间传递类型化数据的端点，不等同于网络 socket |
| Operation | 可被其他组件调用的命令接口，类似带线程策略的成员函数 |
| Activity | 驱动组件执行的线程或周期调度策略 |
| typekit | 让 RTT 的运行时部署器认识某个 C++ 类型的插件 |
| transport plugin | 让已注册类型能够通过 ROS topic 等协议传输的插件 |

这里分析的是 ROS 1 集成仓库。ROS 2 对应项目是 [`orocos/rtt_ros2_integration`](https://github.com/orocos/rtt_ros2_integration)，包名和底层通信实现不同，但“typekit + transport plugin + deployment boundary”的系统设计仍然延续。使用时必须按目标 ROS 版本选择仓库，不能把 ROS 1 的 catkin、roscpp 和 parameter server 代码直接搬到 ROS 2。

本文源码坐标固定到提交 [`d58546d8`](https://github.com/orocos/rtt_ros_integration/tree/d58546d81152898e21efe79f400c164e5d944c90)。核心阅读入口如下：

| 层次 | 固定提交源码 | 关键问题 |
|---|---|---|
| ROS node 运行时 | [`ros_plugin.cpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_rosnode/src/ros_plugin.cpp#L49-L90) | `ros::init`、master 检查和 AsyncSpinner 由谁启动 |
| Topic transport | [`rtt_rostopic_ros_msg_transporter.hpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L62-L75) | sender/receiver ChannelElement 怎样创建和传递样本 |
| 发布执行体 | [`rtt_rostopic_ros_publish_activity.cpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic_ros_publish_activity.cpp#L36-L71) | 为什么实时写 Port 不直接执行 `ros::Publisher::publish` |
| ConnPolicy 工厂 | [`rtt_rostopic.cpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic.cpp#L2-L45) | data、buffer、latched、unbuffered 如何编码 |
| transport 生成模板 | [`ros_package_transport.cpp.in`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/templates/typekit/ros_package_transport.cpp.in#L5-L29) | ROS message package 怎样生成 RTT TransportPlugin |
| 行为用例 | [`transport_tests.cpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/tests/rtt_roscomm_tests/test/transport_tests.cpp) | import、createStream、latched topic、disconnect 的真实用法 |

## 仓库按功能拆分

| 子包 | 功能 | 位于实时路径吗 |
|---|---|---|
| `rtt_ros` | CMake、插件导入和公共集成基础 | 主要服务构建与部署 |
| `rtt_roscomm` | RTT Port 与 ROS topic 之间的 transport | 桥接边界；需区分实时与非实时执行体 |
| `rtt_rosnode` | 初始化 ROS node、master 与 spinner | 非实时运行时 |
| `rtt_rosparam` | RTT Property 与 ROS 参数服务互相复制 | 非实时配置路径 |
| `rtt_rosclock` | ROS 时间与 RTT 时间适配 | 时间语义边界 |
| `rtt_tf` / `rtt_actionlib` | 坐标变换与长任务协议 | 通常位于非实时协调层 |
| `rtt_rosdeployment` | 启动和部署支持 | 进程管理路径 |
| message typekits | 让 ROS message 进入 RTT 动态类型系统 | 类型基础设施 |

这不是一个巨型 bridge 类，而是按 ROS 功能拆为插件。使用者只加载所需能力，避免所有依赖进入实时进程。

## 开发一个可桥接组件

沿着一次数据流自顶向下看，第一步不是创建 ROS Publisher，而是写一个完全不知道 ROS 网络存在的控制组件。下面是用于解释边界的**教学组件**，不是目标仓库中的原样文件：

```cpp
// 教学最小例子：组件只依赖 RTT Port；本例不是固定提交源码
class TorqueController final : public RTT::TaskContext {
 public:
  explicit TorqueController(const std::string& name)
      : RTT::TaskContext(name),
        command_in_("command"),
        state_out_("state") {
    addPort(command_in_);
    addPort(state_out_);
  }

 private:
  bool configureHook() override {
    command_in_.setDataSample(command_);
    state_out_.setDataSample(state_);
    return PreallocateWorkspace();
  }

  void updateHook() override {
    if (command_in_.readNewest(command_) == RTT::NewData) {
      ApplyCommand(command_);
    }
    ReadState(state_);
    state_out_.write(state_);
  }

  RTT::InputPort<sensor_msgs::JointState> command_in_;
  RTT::OutputPort<sensor_msgs::JointState> state_out_;
  sensor_msgs::JointState command_;
  sensor_msgs::JointState state_;
};

ORO_CREATE_COMPONENT(TorqueController)
```

代码表达三个边界：Port 类型在编译期固定；组件只认识消息值，不认识 ROS node/topic；`ORO_CREATE_COMPONENT` 把类注册到 RTT 插件系统，让 Deployer 能按名字实例化。

按 C++ 执行顺序拆开这段代码：

1. `final` 表示不再允许其他类继承 `TorqueController`，避免插件实例通过未预期的覆写改变生命周期语义；
2. 构造函数的初始化列表先构造基类，再用字符串名字构造两个 Port；Port 名会成为部署脚本引用的稳定接口；
3. `addPort(command_in_)` 注册的是已有成员的引用，`TaskContext` 不接管这个成员的所有权，所以 Port 成员必须与组件活得一样久；
4. `override` 让编译器检查函数确实覆写 RTT 生命周期钩子，签名写错时不会悄悄变成普通成员函数；
5. `private` Port 仍可被 RTT 部署器连接，因为构造阶段已经注册；C++ 访问控制限制的是源码直接访问，不是 RTT 的运行时接口；
6. `ORO_CREATE_COMPONENT` 导出插件入口。部署器依靠这个入口按类型名创建对象，而不是在自己的代码里直接 `new TorqueController`。

这也解释了为什么构造函数只注册接口，不连接网络、不分配大型缓冲区：对象刚被插件系统创建时，属性尚未设置，其他组件也可能尚不存在。可能失败的工作应放入 `configureHook()`，周期计算才放入 `updateHook()`。

`sensor_msgs::JointState` 内部包含动态数组和字符串。仅把它设为 data sample 不代表所有运行期写入都无分配；configure 阶段还要为 names/position/velocity/effort 预留预期上限，并拒绝超过上限的消息或切换到固定容量内部类型。

### Port 读写的三个状态

`InputPort::read` 不是返回 `bool`，而是返回 `RTT::FlowStatus`：

| 状态 | 含义 | 控制器常见处理 |
|---|---|---|
| `NoData` | 从未收到可用样本 | 保持安全默认值或拒绝 start |
| `NewData` | 自上次 read 后有新样本 | 验证并提交新 command |
| `OldData` | 当前还能读到最后样本，但没有更新 | 按数据年龄决定保持或超时 |

`readNewest()` 会跳过 buffer 中更旧的项，适合“只关心当前目标”的状态流；逐项 `read()` 适合必须消费每个边沿的事件流。选择哪一个是业务语义，不是微小 API 偏好。仅检查 `NewData` 而没有单调时钟 watchdog，会让最后一个命令在输入断线后永久有效。

`setDataSample()` 给连接建立阶段提供一个样本，RTT storage 可据此预分配，但 C++ 对象内部的 `std::vector`/`std::string` 容量仍需单独规划。下面两种“预分配”不同：

```text
Port data sample      -> 连接层知道 T 的样本形状，可准备 channel storage
message.reserve(N)    -> T 内部动态容器获得容量，避免 N 范围内再次分配
```

若收到长度超过 `N` 的 ROS message，赋值仍可能重新分配。硬实时组件更稳妥的边界是：ROS adapter 验证上限并转换成 `FixedJointState<N>`，实时 Port 只传固定容量值类型；这样 ROS ABI 和动态内存不会进入控制核心。

## Port 到 Topic 的映射

RTT `InputPort<Msg>`/`OutputPort<Msg>` 通过 ROS transport 连接到 topic。Port 仍保留 ConnPolicy 和 FlowStatus 语义，transport element 负责 ROS 编码与回调边界。

```text
RTT OutputPort<Msg>
  -> ChannelElement / ROS transport
  -> ros publisher
  -> ROS topic

ROS callback
  -> transport buffer
  -> RTT InputPort<Msg>
```

不能假设 ROS callback 线程等于组件 Activity。适配层应把数据写入 Port，让 ExecutionEngine 在组件线程消费。

部署脚本中的典型结构是加载 ROS 集成与消息 typekit，再把 Port 流式连接到 topic：

```text
import("rtt_ros")
ros.import("rtt_sensor_msgs")
loadComponent("controller", "my_pkg::TorqueController")

stream("controller.command", ros.comm.topic("/joint_command"))
stream("controller.state",   ros.comm.topic("/joint_state"))
```

具体脚本 API 需以所用发行版为准，但设计上 `stream` 在部署期创建 ChannelElement/ROS transport，而不是改动组件 C++。同一组件可以改为进程内 RTT 连接、CORBA/MQueue 或 ROS topic，算法代码保持不变。

ROS subscriber queue 与 RTT BUFFER 是两层队列：

```text
ROS callback queue (capacity R)
  -> transport callback
  -> RTT connection buffer (capacity O)
  -> Activity reads at period T
```

最坏积压不是简单取 `max(R, O)`；消息可能先在 ROS 层等待，再在 RTT 层等待。端到端最大年龄受两层容量、到达率、callback 调度和 Activity 周期共同影响。状态控制通常应尽早丢旧保新，而事件流需要端到端确认。

### `ConnPolicy` 是连接行为的值对象

固定源码 [`rtt_rostopic.cpp::topic/topicLatched/topicBuffer/topicUnbuffered`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic.cpp#L2-L45) 没有直接创建 Publisher，而是提供几个返回 `RTT::ConnPolicy` 的小工厂。上方代码按其字段组合重画普通 topic 与 BUFFER 两种策略，不是原文摘录。

```cpp
// 教学最小例子：按固定源码中的字段组合构造 ROS topic policy，不是上游源码摘录
RTT::ConnPolicy topic(const std::string& name) {
  RTT::ConnPolicy cp = RTT::ConnPolicy::data();
  cp.transport = protocol_id;
  cp.name_id = name;
  cp.init = false;
  cp.pull = false;
  return cp;
}

RTT::ConnPolicy topicBuffer(const std::string& name, int size) {
  RTT::ConnPolicy cp = RTT::ConnPolicy::buffer(size);
  cp.transport = protocol_id;
  cp.name_id = name;
  cp.init = false;
  cp.pull = false;
  return cp;
}
```

`transport` 选择 ROS protocol plugin；`name_id` 成为 topic；`init` 对应 latched publisher；`pull=false` 表示数据由写入或 callback 推送。`topicLatched()` 与 `topicUnbuffered()` 只是另外两组字段组合。它们把部署意图封装成普通值，因此 C++、脚本和测试最终都走同一条 `createStream(policy)` 路径。

需要区分 `DATA` 与 `BUFFER`。DATA 语义通常只保留最近值，适合状态；BUFFER 按容量保存多项，适合不能无条件覆盖的事件。`policy.size <= 0` 在 ROS advertise/subscribe 处会退到最小 queue size 1，但 RTT storage 是否存在、容量多少仍由连接种类决定。只看 ROS 命令行显示的 queue size，无法推出 RTT 端的完整积压。

### `RosMsgTransporter<T>` 把运行时类型请求还原成模板实例

每个消息类型注册一个 `RTT::types::TypeTransporter`。框架调用 `createStream(port, policy, is_sender)` 时，模板参数 `T` 已由 typekit 确定，运行时布尔值再选择发布或订阅方向：

```text
is_sender = true
  -> RosPubChannelElement<T>
  -> UNBUFFERED: 直接返回 publisher channel
  -> DATA/BUFFER: ConnFactory::buildDataStorage<T>(policy)
                  -> connectTo(publisher channel)

is_sender = false
  -> RosSubChannelElement<T>
  -> ROS callback 写入下游 RTT channel/storage
```

固定源码 [`RosMsgTransporter<T>::createStream`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L266-L307) 首先拒绝 `policy.pull`，再检查 `ros::ok()`。这两个检查把 ROS transport 的不支持策略和未初始化状态在 stream 创建期暴露出来。sender 方向创建 publisher channel；UNBUFFERED 直接返回该 channel，DATA/BUFFER 则先由 `ConnFactory::buildDataStorage<T>(policy)` 建存储，再连到 publisher channel。receiver 方向创建 subscriber channel；收到样本后 callback 通过 `getOutput()->write(...)` 交给已经连接的下游元素，见 [`RosSubChannelElement::newData`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L255-L264)。

模板继承关系也值得拆开：

```cpp
// 教学结构草图：展示模板类型与非模板发布接口的职责拆分，不是固定提交摘录
template<class T>
class RosPubChannelElement
    : public RTT::base::ChannelElement<T>,
      public RosPublisher { /* ... */ };
```

`ChannelElement<T>` 让对象参与 RTT 的类型化连接链；非模板 `RosPublisher` 只暴露 `virtual void publish()`，使进程级 Activity 能把不同 `T` 的 publisher 放进同一个 `std::set<RosPublisher*>`。这些角色分别见固定提交的 [`RosPublisher`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_publish_activity.hpp#L46-L53) 与 [`RosPubChannelElement<T>`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L62-L76)。这是“模板负责数据类型，虚函数负责异构调度”的组合，而不是二选一。

### 发布侧通过共享 Activity 隔离实时线程

固定源码的 `RosPubChannelElement::signal()` 调用缓存的 `act->trigger()`，本身不调用 roscpp；其 `publish()` 反复从上游 `ChannelElement` 读取 NewData，再调用 `ros_pub.publish()`。实现见 [`signal/publish/write`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L164-L192)。`RosPublishActivity` 构造时请求 `ORO_SCHED_OTHER`、最低优先级和零周期，并在 `loop()` 中遍历 publisher；线程行为见 [`RosPublishActivity.cpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic_ros_publish_activity.cpp#L36-L56)。触发只让这条 Activity 有工作机会，仍需等操作系统调度线程获得 CPU。

```text
RTT component Activity
  -> OutputPort::write
  -> RTT DATA/BUFFER storage
  -> RosPubChannelElement::signal
  -> RosPublishActivity::trigger

RosPublishActivity thread
  -> for each registered publisher
  -> while upstream has NewData
  -> ros::Publisher::publish(adapter::toRos(sample))
```

这条路径的优点是 roscpp 序列化、socket 和潜在分配不在实时组件线程执行。只有显式 `topicUnbuffered()` 才让 publish 位于写入 TaskContext 的线程，源码也直接警告该模式可能不具备实时安全性。

代价同样来自源码。所有 publisher 共用 `RosPublishActivity`；[`loop()`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/rtt_rostopic_ros_publish_activity.cpp#L42-L47) 在持有 `publishers_lock` 时遍历并调用每个 `publish()`。一个积压严重或耗时的 publisher 会推迟其他 topic，add/remove 也要等这次遍历放锁。例如 A topic 的 ROS publish 若因大消息耗时 8 ms，B topic 即使只有一个短消息也要等 A 返回。若输出通道很多或消息很大，应测每轮 drain 时间、高水位和 topic 间干扰；需要更强隔离时可按优先级或故障域拆执行上下文。

`RosPublishActivity::Instance()` 返回 `boost::shared_ptr`，静态成员只保存 `weak_ptr`，而每个 publisher 的 `act` 成员持有强引用；最后一个持有者消失后，Activity 才有机会析构并 stop。源头的持有关系见 [`RosPublishActivity.hpp`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_publish_activity.hpp#L63-L94) 及 [`RosPubChannelElement`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/include/rtt_roscomm/rtt_rostopic_ros_msg_transporter.hpp#L65-L75)。源码注释明确说明首次创建非线程安全；并发首次建 stream 可能创建竞争实例，部署应串行创建，或在实现中对初始化加锁。

### 订阅侧由 ROS Spinner 线程写入 RTT 连接

固定提交 [`loadRTTPlugin`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_rosnode/src/ros_plugin.cpp#L49-L90) 在尚未初始化时调用 `ros::init`，检查 master 并在检查成功后 `ros::start`；之后读取 `~spinner_threads` 并启动静态 `ros::AsyncSpinner`。源码注释称 0 表示由 ROS 按处理器数决定线程数。订阅 callback 因而运行在 ROS spinner 线程，而不是所连接的 TaskContext Activity：

```text
ROS AsyncSpinner thread
  -> RosSubChannelElement<T>::newData(const RosType&)
  -> RosMessageAdapter<T>::fromRos
  -> downstream ChannelElement<T>::write
  -> RTT InputPort becomes NewData

component Activity
  -> InputPort::read/readNewest
  -> updateHook consumes data
```

callback 并不直接调用组件 `updateHook()`，但它确实从非实时线程进入 RTT 连接存储。连接实现必须支持这个生产者/消费者并发模型；消息复制、动态数组分配和 buffer overwrite 也发生在这条边界附近。

默认 `RosMessageAdapter<T>` 让 `OrocosType` 与 `RosType` 都等于 `T`，`toRos/fromRos` 返回 `const&`。特化可以让内部固定容量类型与 ROS message 不同，但返回引用时必须保证被引用对象的寿命覆盖当前调用，不能返回函数局部临时对象。若转换需要分配，应该把它明确留在非实时发布/订阅适配层。

## Typekit 的作用

RTT 动态部署需要在运行时认识消息类型，只有 C++ 模板实例还不够。typekit 注册构造、复制、序列化和反射信息，使 Deployer 能按名字创建和连接 ROS message Port。

这里体现静态类型与动态类型系统的接缝：组件源码使用 `InputPort<std_msgs::Float64>`，部署器通过 type name 和 plugin registry 操作同一类型。

一条 ROS message 要进入动态部署至少经过：

```text
.msg definition
  -> ROS C++ generated struct + serialization traits
  -> RTT typekit registers type name / construction / members
  -> ROS transport plugin registers protocol id and marshalling
  -> Deployer imports plugin
  -> stream() verifies both Port types and creates connection
```

只有 typekit 而没有 transport，Deployer 可以认识和操作类型，却不能通过 ROS topic 传输；只有 transport 而没有类型注册，脚本无法按名字构造和检查 Port。把两者混称为“序列化库”会遗漏动态类型系统的职责。

Typekit 插件跨越动态库 ABI。消息生成器版本、编译器 ABI、RTT 版本和 `OROCOS_TARGET` 都要匹配。插件能被 `dlopen` 也不代表类型布局一定兼容，因此构建与部署环境必须成套版本化。

### 生成的 TransportPlugin 怎样找到具体类型

仓库模板 `ros_package_transport.cpp.in` 生成一个继承 `RTT::types::TransportPlugin` 的类，并用 `ORO_TYPEKIT_PLUGIN(...)` 导出。核心接口是：

```cpp
// 教学最小例子：省略生成器扩展的消息匹配代码，示意 plugin 注册接口；不是上游源码
bool registerTransport(std::string name, RTT::types::TypeInfo* ti) {
  // 生成器按 name 匹配包内消息：
  // ti->addProtocol(protocol_id, new RosMsgTransporter<Message>());
  return false;
}
```

固定模板 [`ROS@ROSPACKAGE@Plugin::registerTransport`](https://github.com/orocos/rtt_ros_integration/blob/d58546d81152898e21efe79f400c164e5d944c90/rtt_roscomm/src/templates/typekit/ros_package_transport.cpp.in#L5-L29) 生成 `TransportPlugin` 子类；模板占位片段由生成器展开后，`registerTransport()` 按名字把 type info 接到具体 `RosMsgTransporter<T>`。因此部署脚本里的动态名字最终落回编译期确定的 C++ 类型。若 message package 加了新类型但未重新生成/安装 transport plugin，源码能够包含该消息并不代表 Deployer 能 stream 它。

`RosMessageAdapter<T>` 又提供第二个扩展点：RTT Port 类型可以是 `T`，ROS wire 类型可以是另一个 `RosType`。例如仓库对 `std::string` 与 `std_msgs/String` 的兼容 transport，使测试能够用 `OutputPort<std_msgs::String>` 发布、再从 `InputPort<std::string>` 读取。适配器让类型转换与 ChannelElement 状态机分离，是 Adapter Pattern 的直接实现。

## 构建系统为何特殊

官方仓库说明 RTT 可为 `gnulinux`、`xenomai` 等不同 `OROCOS_TARGET` 构建，因此使用 `orocos_*` CMake macros 生成组件、插件和 typekit；普通 catkin target 不能替代 target-specific 导出。

这是 ABI 管理需求，不是历史语法偏好。错误地把不同 OROCOS_TARGET 产物混装，可能在插件加载时才失败。

官方推荐的 CMake 形状包括：

```cmake
# 教学最小例子：以 catkin 包装 RTT target；参数需匹配本机工具链
find_package(catkin REQUIRED COMPONENTS rtt_ros rtt_sensor_msgs)
include_directories(${catkin_INCLUDE_DIRS} ${USE_OROCOS_INCLUDE_DIRS})

orocos_component(torque_controller src/torque_controller.cpp)
target_link_libraries(torque_controller
  ${catkin_LIBRARIES} ${USE_OROCOS_LIBRARIES})

orocos_install_headers(DIRECTORY include/${PROJECT_NAME})
orocos_generate_package(DEPENDS rtt_ros rtt_sensor_msgs)
```

`orocos_component` 不只是 `add_library` 别名，它把 target-specific 输出位置、插件元数据和 RTT 链接约定带入构建。`orocos_generate_package` 生成供其他 RTT 包发现的信息。普通 catkin target 若绕过这些步骤，可能编译成功却无法被 Deployer 搜索或导入。

`package.xml` 的 `<rtt_ros><plugin_depend>...</plugin_depend></rtt_ros>` 还描述运行时插件依赖。`ros.import("my_pkg")` 会递归导入这些依赖；普通 `import()` 在某些工作区组合中只能加载当前包，缺少 typekit 时错误会延后到连接阶段。

## 实时与非实时边界

ROS parameter server、日志、XMLRPC 和普通 callback 不具备硬实时保证。正确结构是在 configure/deployment 阶段读取参数，在非实时适配线程接收 ROS 数据，通过有界 Port 送入实时 Activity。实时 updateHook 不直接调用参数服务或 ROS 网络 API。

参数同步也要区分启动配置与运行期调参。启动时可把 ROS 参数复制到 RTT Property，验证完整配置后进入 Running；运行期 dynamic_reconfigure 若直接修改多个 Property，会让 updateHook 看到半更新组合。更安全的方式是非实时侧构建完整不可变配置，在周期边界原子交换快照。

`rtt_rosclock` 提供时钟适配，但 ROS time 可能暂停、跳变或由仿真 `/clock` 驱动。控制器的 deadline 和 watchdog 通常应使用单调时钟，消息时间戳才使用 ROS time；把两者混用会在仿真暂停或时间回拨时破坏超时判断。

## Operation 与 ROS Service 的执行语义

`rtt_roscomm` 还可把 RTT Operation 暴露为 ROS service，或让 OperationCaller 调用外部 service。接口名字可以对应，但线程语义不会自动相同：

```text
ROS service callback thread
  -> transport adapter
  -> RTT Operation
       |-- ClientThread: 在 callback 线程执行
       `-- OwnThread: 排入组件 ExecutionEngine 并等待/异步完成
```

ClientThread 可能让非实时 ROS 线程直接进入组件状态；OwnThread 则可能使 ROS service callback 等待实时线程队列。二者都需要超时、取消和队列上限。高优先级 Activity 不应同步调用可能阻塞的外部 ROS service。

## 生命周期组合

启动顺序通常是：初始化 ROS node 服务；导入消息 typekit 与 transport；加载组件；设置 Property；创建 Port/topic stream；configure；设置 Activity；start。关闭按逆序进行：先拒绝 ROS 新请求和 topic ingress，再 stop/join Activity，断开 stream，cleanup 组件，最后关闭 ROS node。

若先销毁 ROS node，transport ChannelElement 仍可能在 stop/cleanup 中访问 publisher；若先卸载 typekit，共存组件的 Port 类型操作表会悬空。动态插件系统的卸载必须晚于所有实例与连接。

## 从组件源码到可部署控制进程

一个最小工程应把四类产物分开：组件 C++、消息/typekit 依赖、部署脚本和 ROS launch。组件库可以在没有 ROS master 的情况下被 RTT 加载；部署层决定是否建立 ROS stream。

### 第一步：组件只声明稳定 Port 与生命周期 Hook

`TorqueController` 的 `configureHook()` 完成尺寸、单位、参数和内存上限检查；`startHook()` 要确认必需输入已有安全初值；`updateHook()` 只做有界计算；`stopHook()` 使 actuator 输出进入安全值；`cleanupHook()` 才释放 configure 阶段资源。不要把网络连接成功当作 `startHook()` 的唯一安全条件，ROS publisher 存在并不说明命令是新鲜的。

组件构造函数注册接口，但不做可能失败的重资源操作。这样 Deployer 可以先实例化完整对象图、设置 Property，再统一 configure；构造异常不会留下半注册的插件实例。

### 第二步：构建包声明静态和动态两类依赖

```cmake
# 教学最小例子：声明链接期与包导入期依赖
find_package(catkin REQUIRED COMPONENTS
  rtt_ros rtt_roscomm rtt_sensor_msgs sensor_msgs)

orocos_component(torque_controller src/torque_controller.cpp)
target_link_libraries(torque_controller
  ${catkin_LIBRARIES} ${USE_OROCOS_LIBRARIES})

orocos_generate_package(
  DEPENDS rtt_ros rtt_roscomm rtt_sensor_msgs)
```

链接依赖让 C++ 符号可解析，`package.xml` 中的 `plugin_depend` 则让 `ros.import("my_pkg")` 知道先装载哪些 RTT 插件。缺少前者通常在链接或 `dlopen` 时报错；缺少后者可能直到 `stream()` 才报告找不到 type/transport。二者不能互相替代。

若使用自定义 `my_msgs`，应由 `ros_generate_rtt_typekit(my_msgs)` 生成 typekit/transport，并让控制包依赖生成后的 `rtt_my_msgs`。修改 `.msg` 后必须一起重建生成包和所有直接使用该 C++ 布局的组件。

### 第三步：部署脚本建立执行与通信策略

```python
# 教学最小部署脚本：展示装配顺序，不承诺跨发行版命令拼写不变
import("rtt_ros")
ros.import("my_robot_control")
ros.import("rtt_sensor_msgs")

loadComponent("controller", "my_robot_control::TorqueController")
setActivity("controller", 0.001, 90, ORO_SCHED_RT)

# DATA 连接用于只取最新命令；BUFFER 用于需要保留的状态事件。
stream("controller.command", ros.comm.topic("/joint_command"))
stream("controller.state", ros.comm.topicBuffer("/joint_state", 4))

configureComponent("controller")
startComponent("controller")
```

具体 helper 名会随安装版本变化；固定提交的 Service 注册名是 `ros.comm.topic`、`topicLatched`、`topicBuffer`、`topicUnbuffered`，旧 README 还出现过 `bufferedConnection` 一类名称。关键不是记住字符串，而是理解脚本必须明确五件事：组件类型、Activity 周期/优先级、topic 名、连接种类/容量、生命周期推进顺序。

如果 configure 之前就创建 stream，transport 可以准备 data sample；如果组件只有 configure 后才知道数组上限，则应先设置 Property，再 configure，再连接，或给 Port 在 configure 前提供保守最大样本。顺序要由组件契约规定，不能依赖偶然可用。

### 第四步：ROS launch 只负责进程级参数

仓库提供的 `rtt_ros/launch/deployer.launch` 可以接收 `DEPLOYER_ARGS`、`LOG_LEVEL`、`OROCOS_TARGET` 与 `RTT_COMPONENT_PATH`。launch 文件启动 Deployer 并传入 `.ops`，而不是把每条 Port 连接重新编码一遍。这样同一组件库可有 hardware、simulation、replay 三套部署脚本。

```text
roslaunch
  -> target-specific deployer executable
  -> load .ops
  -> import plugins/typekits
  -> instantiate TaskContext graph
  -> create ROS streams
  -> configure/start Activities
```

运行时诊断要按这一层级定位：找不到组件先查 `OROCOS_TARGET/RTT_COMPONENT_PATH`；组件可加载但类型未知，查 typekit/plugin_depend；类型已知但 stream 创建失败，查 ROS node 初始化、transport 和 ConnPolicy；Port 有数据但控制器无响应，再查 FlowStatus、Activity 和生命周期状态。

### 参数在 configure 边界完成提交

`rtt_rosparam` 为 TaskContext 增加 `rosparam` Service，提供 `get/set/getAll/setAll` 以及多种 ROS 名称解析策略。宏生成了 string、数值、bool、vector 与 Eigen vector 的操作族。它便于在部署脚本中把 parameter server 值复制到 RTT Property，但这些操作内部调用 XMLRPC、构造动态容器，不应从高优先级 `updateHook()` 执行。

安全流程是：非实时部署线程读取全部参数到 Property，`configureHook()` 验证组合不变量并预分配，成功后才 start。运行期调参则先在非实时侧构造完整 `ConfigSnapshot`，验证后在周期边界交换版本；逐字段远程写 Property 会产生控制器可见的半更新状态。

## 优秀设计与工程取舍

集成让 ROS 工具链和消息生态服务于 RTT 控制组件，但引入两套生命周期、时间和线程模型。ROS queue size 与 RTT ConnPolicy 是两层缓冲；只配置其中一层不足以推导端到端丢弃与延迟。

ROS message 直接作为实时组件 Port 类型减少转换代码，却把可变长容器和 ROS ABI 带入实时核心。另一设计是内部使用固定容量控制类型，在非实时桥接组件中转换 ROS message；代码更多，但内存上界与依赖边界更清楚。

仓库覆盖参数、clock、tf、actionlib 等众多能力，但加载越多插件，实时进程的依赖、全局状态和故障面越大。按需导入包是架构约束，不只是启动优化。

其中最值得复用的设计，是没有让 `TaskContext` 同时承担控制算法、ROS node 和部署配置三种责任。组件声明稳定能力，typekit 解决运行时类型，transport 解决跨系统传输，部署脚本决定具体连接。分层会增加插件数量，却让同一控制器可以在进程内测试、连接 ROS，或替换成其他 transport，而不必改动控制算法。

共享 `RosPublishActivity` 则是一次明确的折中：它用较少线程集中隔离 roscpp 调用，适合 topic 数量有限的系统；代价是多个 publisher 彼此影响。这里没有“永远正确”的线程模型，只有是否与消息大小、发布频率和故障隔离需求匹配。

## 缺点与不适用边界

1. **桥接不能把 ROS 1 变成硬实时系统。** TCPROS、XMLRPC、参数服务器和普通 callback queue 没有确定的最坏执行时间；适配层只能把不确定性隔离在边界外。
2. **两套队列容易制造隐藏延迟。** ROS callback queue 与 RTT BUFFER 都可能积压数据，只观察其中一个容量无法判断命令实际年龄。
3. **直接使用 ROS message 会把动态容器带进控制核心。** `std::vector` 和 `std::string` 在容量变化时可能分配内存；需要硬上界时应在非实时 adapter 转换成固定容量类型。
4. **动态插件扩大 ABI 风险。** ROS 消息生成器、编译器、RTT、`OROCOS_TARGET` 和 typekit 必须成套匹配；“动态库能够加载”并不足以证明类型布局兼容。
5. **进程级共享发布线程可能形成 convoy。** 一个大消息或堵塞 publisher 会拖慢其他 topic，不能假定不同输出天然隔离。
6. **生命周期组合复杂。** 组件、stream、spinner、ROS node 和 typekit 的关闭顺序错误时，callback 可能访问已经销毁的实例。

因此，这套集成适合需要 ROS 工具生态、但又希望把高优先级控制组件保持在 RTT 执行模型中的机器人。它不适合作为安全认证控制链的唯一通信保障，也不应让高速电机闭环跨越普通 ROS 网络。最内层硬实时控制应留在有界、经过测量的 RTT 或现场总线路径，ROS 更适合监督、任务协调、记录和可视化。

## 数据结构、测试与性能证据

测试应分别测 ROS callback→RTT Port 入队、Activity 读取、组件计算和 RTT Port→ROS publish 四段延迟，并记录两层 queue high-water mark。功能测试还要覆盖消息 typekit 未加载、topic 类型错误、ROS master/node 不可用、仿真时间回拨、service 超时和关闭时 callback 在途。

实时结论只适用于组件内部有界路径。ROS 1 TCPROS/XMLRPC、普通 roscpp callback queue 和 parameter server 不能因此获得硬实时保证。桥接层可以隔离不确定性，不能把它消除。

复杂度主要由消息大小和积压量决定。若单条消息序列化大小为 `S`，ROS 队列和 RTT 队列容量分别为 `R`、`O`，仅缓存数据的空间上界近似为 `O(S × (R + O))`，还没有计入中间转换对象。发布 Activity 一轮若需要处理 `P` 个 publisher，第 `i` 个积压 `Nᵢ` 条消息，工作量近似为 `O(ΣNᵢ)`；因此平均频率正常并不能排除突发积压导致的长尾延迟。

## 可迁移的桥接设计方法

桥接实时与非实时系统时，用类型化 Port 作为隔离面：外部 callback 只做验证、转换和有界入队，实时线程只读取已经准备好的值；所有动态发现、参数、日志和网络重连留在非实时侧。这样才能对实时核心单独建立 WCET 和资源上界。

进一步可以抽出五条与具体中间件无关的规则：

1. 先定义内部实时类型，再编写外部消息 adapter，不让 wire type 主导控制核心；
2. 给每一层队列写清容量、覆盖策略和超时语义，端到端判断数据年龄；
3. 用单向数据交接跨越线程边界，不让 callback 直接调用周期算法；
4. 把类型注册、传输实现和业务组件分开，使错误能在部署阶段准确定位；
5. 关闭时先阻止新输入，再停止执行体，最后卸载实例和类型插件。

## 最小复刻：建立单向实时到 ROS 的桥

最小工程可以先只包含这些文件：

```text
mini_rtt_ros_bridge/
├── CMakeLists.txt
├── package.xml
├── include/mini_bridge/torque_controller.hpp
├── src/torque_controller.cpp
├── config/controller.ops
└── launch/controller.launch
```

先写只含两个 RTT Port 的组件并用进程内连接验证；生成/加载一个 ROS message typekit；再实现单向 OutputPort→topic；随后实现 callback→有界 InputPort；加入 deployment script 和 target-specific CMake；最后才接参数、service、clock 和动态重配置。

每一步只增加一个新边界：

```text
阶段 1  TaskContext -> RTT Port -> RTT Port
阶段 2  OutputPort -> RosPubChannelElement -> ROS topic
阶段 3  ROS callback -> bounded RTT connection -> InputPort
阶段 4  .ops 明确 Activity、ConnPolicy、容量和生命周期
阶段 5  launch 只选择进程级配置与 OROCOS_TARGET
```

不要一开始同时接 parameter、tf 和 actionlib。若最简单的单向 topic 已经出现抖动，同时存在五套插件只会让所有权和线程来源更难追踪。

完成标准包括：组件不包含 ROS node API；缺少 typekit 时启动明确失败；ROS callback 不直接执行 updateHook；两层队列策略可解释；Running 阶段无非预期分配；ROS 断线不破坏实时 Activity；关闭后无 callback 访问已卸载组件或插件。
