# TaskContext 生命周期：状态转换怎样管理设备资源

把驱动器接入机械臂时，构造一个 C++ 对象并不代表设备已经能安全输出。驱动库可能尚未加载，参数可能不合法，编码器也可能未连接。若构造函数直接打开设备并启动线程，部署器遇到第二个设备失败时就难回滚；若只有 configured/running 两个布尔量，运行中失败后也不清楚设备句柄应保留还是释放。

Orocos RTT 用 TaskCore 的状态与 hook 为资源划出边界，再由 TaskContext 补上 Service、Port、Peer 和 Activity。固定版本为 [Orocos RTT commit 600102e8be9c81905b20930e32d43b28244ab173](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。

## 从一个失败配置的例子开始

最朴素的组件一边配置一边改成员：

~~~cpp
// 错误示例：中途失败后成员只初始化了一半
bool configure() {
    device_ = openDevice();
    if (!device_.connected()) return false;
    model_ = loadModel();
    if (!model_) return false;
    configured_ = true;
    return true;
}
~~~

如果模型加载失败，device_ 已经打开；部署器看到 false 却不知道关闭它。多加一个 device_open_ 标志，只会把回滚责任扩散到更多布尔组合。更易推理的做法是在局部 RAII 对象中完成准备，所有步骤成功后 move 到成员；失败时局部对象析构，已取得的资源自动释放。

RTT 的 configureHook 由 TaskCore::configure() 调用，但框架不能替用户 hook 回滚任意外部资源。若 hook 在 PreOperational 分配后抛异常，TaskCore::exception() 只有在此前状态至少 Stopped 时才调用 cleanupHook；未提交的局部资源应由 RAII 自己释放，不能依赖 cleanupHook 一定会运行。

## 状态机让调用者知道下一步是否合法

TaskCore 状态包括 PreOperational、Stopped、Running、RunTimeError、Exception 与 FatalError。正常路径如下：

~~~text
PreOperational --configure 成功--> Stopped --start 成功--> Running
       ^                                  |                    |
       |                                  +---- cleanup <--- stop
       +-------------------------------------------------------+
~~~

固定实现中 TaskCore::configure() 仅接受 Stopped 或 PreOperational。它将目标状态设为 Stopped，调用 configureHook；hook 返回 true 时状态变为 Stopped，返回 false 时状态与目标态回到 PreOperational。异常则进入 exception 逻辑。见 [TaskCore::configure 与 cleanup](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L96-L149)。

“hook 成功再提交状态”不表示框架能撤销 hook 中已经完成的副作用。configureHook 若先 open 设备、再验证连接、最后失败，应在 hook 内用局部 unique_ptr/RAII guard 收回已打开资源，或确保 cleanupHook 能处理部分初始化状态。unique_ptr 代表单一拥有者，离开作用域自动 delete；裸指针本身没有这种保证。

TaskCore::start() 只允许从 Stopped 开始。它先设置 mTargetState=Running，再调用 startHook；hook 成功后提交 Running，并根据 TriggerOnStart 触发一次 Activity。失败时返回 Stopped；异常进入 Exception。startHook 适合使能设备和确认连接，不适合做耗时且不能取消的完整设备发现。生命周期函数并非整个都由一个全局 mutex 串行保护：应用应由单一管理者顺序调用 configure/start/stop/cleanup，避免两个管理线程基于同一旧状态同时转换。

## TaskContext 的对象关系与真实所有权

TaskContext 是组件公共门面。构造函数调用 TaskCore 建立 ExecutionEngine，再建立提供服务的 Service、请求服务的 ServiceRequester 和 Activity；setup() 将 configure、start、stop、cleanup、trigger 注册为默认 ClientThread Operations，并启动默认 Activity。源码见 [TaskContext::TaskContext/setup](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L70-L117)。

~~~text
TaskContext
  ├─ TaskCore: 当前状态、目标状态、ExecutionEngine
  ├─ Service: 生命周期 Operation、组件公开接口
  ├─ ServiceRequester: 依赖的外部 Service
  ├─ 派生组件成员: Port、属性、设备与业务状态
  ├─ our_act: 当前 Activity 的 shared_ptr
  └─ peers: TaskContext* 按别名索引
~~~

这幅图要按所有权而不是缩进读取。TaskCore 以 raw ExecutionEngine* ee 建立执行引擎，并在析构中删除；TaskContext 的 our_act 是 ActivityInterface::shared_ptr。ActivityInterface 持有 RunnableInterface* runner，Runnable 持有的又是非拥有 ActivityInterface* owner_act；绑定本身不会拥有 Runnable。组件生命周期必须覆盖 Activity 的工作周期，不能让线程仍可能访问 Engine 时先析构组件。

TaskContext::setActivity() 是所有权转移 API，不是把栈上的 Activity 借给组件。头文件明确说明成功后 Activity 由 TaskContext 拥有，调用者不应继续使用传入指针；实现停止新旧 Activity、把 Engine 绑定到新 Activity、将 new_act 放入 boost::shared_ptr，再启动它。它拒绝 Running 状态替换，也拒绝从当前 Activity 自己的线程替换。见 [TaskContext.hpp 的 setActivity 文档](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.hpp#L119-L141) 与 [TaskContext::setActivity](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L338-L373)。

~~~cpp
// 错误示例：成功的 setActivity 会接管对象，却传入栈地址
RTT::Activity local_activity(&controller.engine());
controller.setActivity(&local_activity); // 组件析构 Activity 时会尝试释放栈对象
~~~

正确做法是传入由 new 创建且准备转移所有权的 Activity，或由部署封装提供等效工厂；调用者不能把 shared ownership 和独立 delete 混在一起。boost::shared_ptr 的控制块管理强引用与最终析构，传入裸指针转为 shared_ptr 后，若调用者仍手动 delete 就会双重释放。

Peer 与连接也不是一种关系。Peer map 记录名称到 TaskContext* 的非拥有导航指针，创建 Peer 不自动创建数据 Port；Port channel 要显式连接。TaskContext 保存互相引用的 users 列表，并在析构时从 peer 两侧移除以免残留悬空指针。析构注释明确不在此处调用 disconnect()，因为派生类中的 Port 成员可能已经销毁；部署层应在销毁组件前断开 Port。见 [Peer 与 disconnect](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L234-L306) 与 [析构函数](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/TaskContext.cpp#L119-L147)。

## Hook 的职责与失败可观察性

| Hook | 典型责任 | 失败时需要说明 |
|---|---|---|
| configureHook | 检查参数、打开设备、建立固定容量数据结构 | 一半完成如何 RAII 回滚 |
| startHook | 连接检查、使能设备、复位周期状态 | 使能成功但后续失败怎样失能 |
| updateHook | 每个实际 Engine 周期读样本、计算、写输出 | OldData 多久转安全态 |
| errorHook | RunTimeError 时执行有界降级 | 重试次数与总耗时上限 |
| stopHook | 停止普通输出并进入设备安全状态 | 函数多久返回，超时如何处置 |
| cleanupHook | 释放配置阶段长期资源 | 只能在线程不再使用资源后调用 |

这张表是设计建议；真实 hook 调用条件需看 TaskCore/ExecutionEngine。例如 updateHook 只会在当前状态和目标状态均为 Running 时调用；RunTimeError 走 errorHook。未处理异常使 TaskCore 先记录旧状态，再进入 Exception；若旧状态至少 Running 会调用 stopHook；若旧状态至少 Stopped 且初始状态要求 PreOperational，会调用 cleanupHook。详见 [TaskCore::exception](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L163-L184) 与 [ExecutionEngine::processHooks](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L360-L392)。

运行期故障例子：编码器断线但 updateHook 继续用 OldData，控制器把过去的关节位置当作现在状态，可能继续输出错误力矩。不能只设置一个 error 标志；要校验时间戳、切安全输出，并给独立设备 watchdog 兜底。RunTimeError 可显式 recover，但这不会自动判断外设数据已恢复；监督层仍需验证前置条件。

## stop、stopHook 和析构的边界不同

TaskContext::stop() 先检查正在运行状态，然后进入 TaskCore::stop()。TaskCore 把目标态设为 Stopped，通过 Engine::stopTask() 建立更新同步点；该方法 stop Activity 等当前 step 返回，再 start Activity；只有同步成功后才执行 stopHook 并写入 Stopped。此操作不销毁线程，组件以后还可再次 start。见 [TaskCore::stop](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/TaskCore.cpp#L232-L255) 与 [ExecutionEngine::stopTask](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L401-L409)。

析构另有路径。TaskContext::~TaskContext() 先对 our_act 调 stop，再清 Service、ServiceRequester、Peer 关系；它明确不调用 stop() 或 cleanup()，责任留给派生类/部署管理者，因为基类析构期间不能安全调用已销毁派生对象的虚 hook。随后 TaskCore::~TaskCore() 删除其 ExecutionEngine，但也不调用 cleanupHook。若设备关闭在 cleanupHook，部署器要显式执行组件 stop 和 cleanup，再 disconnect，再析构组件；停止失败时不要继续拆 Activity 或卸载插件。

Activity::stop() 可能超时返回 false；Activity 析构函数还调用 terminate 等待底层线程，以防线程继续访问 Activity 成员。析构一个组件不能证明外部线程已退出，关闭协调必须从上游停掉输入源和命令生产者开始。

## 从零复刻这笔资源事务

先实现状态枚举与唯一管理线程，只允许明确转换；再让每个 hook 有独立失败注入点；为 configure 阶段资源使用局部 RAII，成功后才提交成员；最后接入 Activity、Engine、Port 与 Operation。观察这些时间线：open 成功但模型加载失败时句柄是否释放；stop 期间 callback 是否还能访问设备；cleanup 前周期是否确已跨过同步点；Peer/Port 是否先断开再析构；组件失败后状态和设备实际状态是否一致。

TaskContext 的 Service 反射、PluginLoader、动态类型和远端 Port 会增加对象寿命与 ABI 依赖；先把基本资源事务闭合，再逐层接入。

