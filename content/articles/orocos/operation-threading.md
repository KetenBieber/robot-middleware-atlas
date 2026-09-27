# Operation 线程模型：清故障命令如何进入组件 Engine

一台移动机器人控制器每毫秒更新一次电机目标。操作员从诊断界面点“复位驱动器”时，命令也要改同一组件的状态，却不能随时插入控制计算中途。直接公开 resetFault() 看似简单；管理线程可能与 updateHook 交错修改故障标志和积分器，最后屏幕显示复位成功，下一周期仍输出旧力矩。

Operation 给命令一个执行策略：在调用者线程运行，或在拥有组件的 ExecutionEngine 线程运行。后者减少调用方对组件状态的并发写入，但引入排队、参数寿命、返回等待和关闭问题。本文固定到 [orocos-toolchain/rtt commit 600102e8be9c81905b20930e32d43b28244ab173](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。

## ClientThread 直接使用调用者的栈

Port 表达连续样本；Operation 表达一次调用、参数和结果。把每帧 1 kHz 关节测量做成函数调用，需要为每个样本管理调用、返回与拥塞；把一次复位命令做成 Port，则需自行编码请求、确认、超时与重复命令标识。

~~~cpp
// 教学最小例子：将复位命令安排到组件所属 Engine
addOperation("resetFault", &DriveController::resetFault, this, RTT::OwnThread)
    .doc("Reset a recoverable drive fault");
~~~

OwnThread 表示方法在拥有 TaskContext 的 ExecutionEngine 线程执行。ClientThread 是默认策略，方法沿调用者的 C++ 调用栈运行。定义见 [OperationBase.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/OperationBase.hpp#L54-L60)；[Service::addOperation](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/Service.hpp#L387-L408) 默认使用 ClientThread，并创建、保存 Operation。

ClientThread 不经过 RTT Engine 队列，但多个调用方仍可能同时进入组件。若它写 updateHook 同时访问的目标值，必须使用互斥锁、不可变快照或其他同步协议。外部调用者的线程优先级也会进入目标函数；低优先级调用者如果持有控制线程需要的锁，高优先级周期线程仍可能被阻塞。

## OwnThread 用 invocation 对象跨过调用边界

OwnThread 调用不能只把“函数地址”放进队列。参数要在 caller 返回后仍可用，结果也要在目标函数结束后可收集。固定版本为每次调用克隆 LocalOperationCallerImpl 子对象，由 BindStorage 存放参数和结果状态，再把对象指针放进目标 Engine 消息队列：

~~~cpp
// 固定提交源码摘录：LocalOperationCallerImpl::do_send() 与 send_impl()，rtt/internal/LocalOperationCaller.hpp
SendHandle<Signature> do_send(shared_ptr cl) {
    ExecutionEngine* receiver = this->getMessageProcessor();
    cl->self = cl;
    if ( receiver && receiver->process( cl.get() ) ) {
        return SendHandle<Signature>( cl );
    } else {
        cl->dispose();
        return SendHandle<Signature>();
    }
}
SendHandle<Signature> send_impl( T1 a1 ) {
    shared_ptr cl = this->cloneRT();
    cl->store( a1 );
    return do_send(cl);
}
~~~

这是固定源码摘录，完整模板对不同参数数量有对应 overload，见 [LocalOperationCaller.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/LocalOperationCaller.hpp#L130-L160)。cloneRT() 用 RTT 实时 allocator 建立签名一致的 invocation；store() 放入参数；do_send() 先让 invocation 用 shared_ptr self 延长寿命，再把裸指针交给 Engine 队列。队列本身不拥有智能指针，self 和 SendHandle 内的 shared_ptr 才延长其生命。投递失败时 dispose() 解除 self 引用并返回空 handle。

这是 Boost shared_ptr，不是 std::shared_ptr。控制块记录强引用数与删除器；SendHandle 持有一个引用，排队中的 invocation 也通过 self 持有强引用。方法执行后 dispose() 清掉 self；handle 仍可读取结果。共享拥有解决“调用者返回后 invocation 被析构”的问题，但不等于所有参数都会被深拷贝，也不表示销毁 handle 会取消硬件动作。

## 引用参数为什么会悬空

BindStorage 为值参数和引用参数实例化不同存储。值存储拥有一份 T；引用存储 AStore<T&> 只保存 T*：

~~~cpp
// 固定提交源码摘录：AStore<T&>，rtt/internal/BindStorage.hpp
template<class T>
struct AStore<T&>
{
    T* arg;
    AStore() : arg( &NA<T&>::na() ) {}
    AStore(T& t) : arg(&t) {}
    AStore(AStore const& o) : arg(o.arg) {}

    T& get() { return *arg; }
    void operator()(T& a) { arg = &a; }
    operator T&() { return *arg;}
};
~~~

复制 AStore<T&> 只复制地址，不复制对象，见 [BindStorage.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/BindStorage.hpp#L64-L88)。若异步命令参数是 const Command&，局部 Command 在目标 Engine 执行前已经析构，工作线程就会解引用悬空地址。值参数也可能复制、扩容；若含 vector/string，执行时延和内存分配仍需核算。

~~~text
管理线程：创建 local Command -> send(local) -> 返回并析构 local
组件线程：稍后执行 invocation -> 读取 local 的旧地址
可观察结果：字段被复用后随机变化、崩溃或接受错误目标值
~~~

Operation 不会替应用延长引用参数对象的寿命。“invocation 被 shared_ptr 管理”只涵盖 invocation 对象自身。

## Engine 怎样执行并交回结果

ExecutionEngine::process(DisposableInterface*) 会在非 FatalError 且 Activity 存在时尝试把 invocation 指针加入消息队列，然后触发 Activity；队列容量由 ORONUM_EE_MQUEUE_SIZE 定为 100。满时 process 返回 false，因此 send 返回空 handle。入队成功不表示业务函数已经开始执行：Activity 还需在运行线程被调度后由 Engine 出队。

processMessages() 排空消息队列并调用 executeAndDispose()。目标 Engine 首次处理 invocation 时执行绑定的业务函数，将返回值或异常记录在 RStore；如果设有 caller Engine，则把它投递回 caller Engine；否则清掉 self。再次处理时已经 executed，只做收尾。源码见 [ExecutionEngine.cpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ExecutionEngine.cpp#L207-L230) 与 [LocalOperationCallerImpl::executeAndDispose](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/LocalOperationCaller.hpp#L95-L128)。

这版没有通用 CallState 类；状态在按签名实例化的 invocation 与 BindStorage::RStore 中。SendHandle 本身通过共享指针保留 invocation，collect() 查询结果；析构函数不发取消请求。RTT 使用 boost::function<Signature> 作为统一调用接口；OperationCallerBinder 把成员函数指针、传入的对象指针和参数占位符绑定为 boost::function。模板为每个实际签名实例化不同实现，Service 再用运行时名称暴露操作。绑定对象指针不会自动拥有目标组件；Service/Operation 生命周期不能越过组件对象析构。

## call 与 send 的差别

ClientThread 的 call() 直接执行存储的可调用对象。若是跨 Engine 的 OwnThread，call() 会先 send，再 collect，并返回结果；发送或收集失败抛 SendFailure。固定源码摘录：

~~~cpp
// 固定提交源码摘录：LocalOperationCallerImpl::call_impl()，rtt/internal/LocalOperationCaller.hpp
result_type call_impl()
{
    if ( this->isSend() ) {
        SendHandle<Signature> h = send_impl();
        if ( h.collect() == SendSuccess )
            return h.ret();
        else
            throw SendFailure;
    } else {
        if ( this->mmeth )
            return this->mmeth(); // ClientThread
        else
            return NA<result_type>::na();
    }
}
~~~

完整模板还有参数 overload 与可选 signalling 分支，见 [call_impl](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/LocalOperationCaller.hpp#L347-L368)。是否要排队由 [OperationCallerInterface::isSend()](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/OperationCallerInterface.hpp#L107-L124) 决定：只有 OwnThread 且目标 Engine 与 caller Engine 不同时才 send。同一 Engine 可内联，避免把工作排给自己再同步等待。

send() 立即返回 SendHandle；collectIfDone() 可不阻塞地检查是否完成，collect() 则可能等待 caller Engine 的谓词。目标 Engine 忙、消息积压或业务函数阻塞时，collect() 会等。这个版本没有 send 超时与取消接口。销毁 handle 只释放 handle 自己的强引用；排队 invocation 的 self 仍维持其生命，目标方法继续执行。放弃结果不等于取消命令。

## 有界队列仍然没有周期预算

100 个 invocation 堆在 Engine 队列时，后续 send 会失败而不是无界分配队列节点。但 processMessages() 会一直 drain 到空；若生产者持续补入消息，控制周期仍会被大量工作挤压。容量限制瞬时空间，不保证周期执行时间；该提交没有可配置的 per-cycle batch budget。

同步 OwnThread 的反例：GUI 调用 resetFault() 后在 collect() 等待；组件周期线程因驱动 API 阻塞两秒，GUI 也两秒后才返回。实时闭环不会因为使用 OwnThread 自动受保护。管理线程还可能持有普通 mutex，使高优先级周期线程发生优先级反转。

关闭前要停止新调用方、等在途命令完成或拒绝、确认 Activity 不再使用 Engine，再析构 Service 与组件。ExecutionEngine 析构会释放排队的 Disposable，但不是用户级取消协议，也不能安全中断正在运行的设备函数。管理层不能在还有同步调用等待时销毁其 Engine。

## 从零复刻的顺序

先写固定签名接口和直接 ClientThread 调用，明确目标对象是借用而不是自动拥有；再实现固定容量队列，按值保存参数与结果；随后定义唯一终态、满队列返回、异常和 wait predicate；再加入 send/collect；最后做关闭时拒绝新调用、为所有等待者给出终态，并只在函数提供安全检查点时实现协作取消。

最小系统应可观察：引用参数寿命短于排队时间时被拒绝或复制；第 101 个投递失败；Engine 忙时 collect 仍未完成；关闭前所有等待者都得到完成或失败结果；运行中的设备操作只在安全取消点结束。这样才能说明 Operation 改变了执行线程、参数寿命和调用者等待时序。

