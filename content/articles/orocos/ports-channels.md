# Port 与 ChannelElement：消息现在在哪里，读端拿到哪一份

机械臂关节状态以 2 kHz 发布，而控制器以 1 kHz 读取。最朴素的想法是把每次测量放进无界队列：这样样本不会丢。但消费速度只有生产速度一半，队列每秒增加 1000 项；十秒后控制器处理的是十秒以前的角度，机器人仍能稳定运行却在追赶旧目标。若改成单个共享变量，空间固定但必须定义读写同步，并接受中间样本会被覆盖。

RTT 的 Port 把类型化端点和具体存储拆开。InputPort/OutputPort 表达端点；连接建立时 ConnFactory 按 ConnPolicy 组装 ChannelElement 链，链中的 ChannelDataElement 或 ChannelBufferElement 决定样本怎样保存。所有实现细节锁定到 [orocos-toolchain/rtt commit 600102e8be9c81905b20930e32d43b28244ab173](https://github.com/orocos-toolchain/rtt/tree/600102e8be9c81905b20930e32d43b28244ab173)。

## 先把端口、连接和样本分开

~~~text
组件 A: OutputPort<JointState>
    -> ConnInputEndpoint
    -> [ChannelDataElement / ChannelBufferElement]
    -> ConnOutputEndpoint
    -> 组件 B: InputPort<JointState>
~~~

上图是本地连接的角色图，实际方向按写端和读端命名可能容易混淆。OutputPort 将样本交给自己的写端点；ConnFactory 生成中间 storage element 与输入端点；InputPort 从 read endpoint 读取。链路创建在 [ConnFactory::createConnection](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L448-L525)。

Port 的模板参数 JointState 让连接两端类型在编译期匹配。ConnPolicy 是运行期值对象：它描述数据存储 DATA/BUFFER/CIRCULAR_BUFFER、锁策略、缓冲容量、PUSH/PULL、是否初始化样本、连接是否 mandatory、buffer policy 与 transport 等。类型边界不能代替运行期语义；同一对端口可以连接成单值状态，也可以连接为 FIFO。

## 从 write 到真实存储

OutputPort::write(const T&) 的输入是调用者栈上的一个样本。若启用了 keeps_last_written_value 或 keeps_next_written_value，它先复制到输出端的 sample DataObject；随后通过端点把样本同步写入连接。摘录保留这一关键顺序：

~~~cpp
// 固定提交源码摘录：OutputPort<T>::write()，rtt/OutputPort.hpp
WriteStatus write(const T& sample)
{
    if (keeps_last_written_value || keeps_next_written_value)
    {
        keeps_next_written_value = false;
        has_initial_sample = true;
        this->sample->Set(sample);
    }
    has_last_written_value = keeps_last_written_value;

    WriteStatus result = NotConnected;
    if (connected()) {
        result = getEndpoint()->getWriteEndpoint()->write(sample);
    }
    return result;
}
~~~

源码摘录省略 trace 记录和断连日志，控制流见 [OutputPort::write](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/OutputPort.hpp#L239-L263)。sample 是 const 引用，所以函数入口不必先创建一份临时 T；但 storage 写入、端口保持最近值、分叉到多个连接和接收端赋值都可能复制 T。一个含 vector 的 JointState 即使调用方传引用，沿途仍可能分配。

OutputPort 的 endpoint 多连接写入时持有 outputs_lock，并按连接逐个调用下游 ChannelElement::write(sample)；因此慢连接会占用当前写线程，扇出成本随连接数增长。共享锁保护的是连接列表稳定性，不能让任意 T::operator=() 变成实时有界。见 [MultipleOutputsChannelElement::write](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ChannelElement.hpp#L292-L326)。

ConnFactory::buildDataStorage() 根据 ConnPolicy 决定创建 DATA 或 FIFO storage。DATA 生成 DataObjectLockFree/Locked/UnSync，再包装为 ChannelDataElement；BUFFER 与 CIRCULAR_BUFFER 生成 BufferLockFree/Locked/UnSync，再包装为 ChannelBufferElement。LOCK_FREE 也会按编译配置回退到 LOCKED，不能只看部署策略字符串就认定运行二进制真的无锁。源码见 [ConnFactory::buildDataStorage](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L150-L205)。

### 直接读 FIFO 的写入与三态读取

单纯把 BUFFER 称作“队列”，还不足以分析数据年龄。固定提交的 `ChannelBufferElement<T>` 把队列 Push/Pop 和上次样本指针 `last_sample_p` 分别管理：

~~~cpp
        virtual WriteStatus write(param_t sample)
        {
            if (!buffer->Push(sample)) return WriteFailure;
            return this->signal() ? WriteSuccess : NotConnected;
        }

        /** Pops and returns the first element of the FIFO
         *
         * @return false if the FIFO was empty, and true otherwise
         */
        virtual FlowStatus read(reference_t sample, bool copy_old_data)
        {
            value_t *new_sample_p;
            if ( (new_sample_p = buffer->PopWithoutRelease()) ) {
                if(last_sample_p)
                    buffer->Release(last_sample_p);

                sample = *new_sample_p;

                // In the PerOutputPort or Shared buffer policy case this buffer element may be read by multiple readers.
                // ==> We cannot store the last_sample and release immediately.
                // ==> WriteShared buffer connections will never return OldData.
                if (policy.buffer_policy != PerOutputPort && policy.buffer_policy != Shared)
                    last_sample_p = new_sample_p;
                else
                    buffer->Release(new_sample_p);

                return NewData;
            }
            if (last_sample_p) {
                if(copy_old_data)
                    sample = *(last_sample_p);
                return OldData;
            }
            return NoData;
        }
~~~

`write(sample)` 先调用底层 buffer 的 `Push`；若缓冲区已满且底层策略不接收，本层直接返回 `WriteFailure`。写入成功还须调用 `signal()`，由连接链通知下游读端；若连接已断开，则返回 `NotConnected`。所以 `WriteSuccess` 既不是“对端已经读取”，也不是“业务计算完成”，它只表达这次连接链写入和信号成功。

`read(sample, copy_old_data)` 先通过 `PopWithoutRelease()` 得到新样本指针。成功时归还上一份保留指针、复制样本数据，并视连接策略决定是自己保存这一份供下次报告 `OldData`，还是马上释放。`PerOutputPort` 和 `Shared` 情况允许多个读者共享 buffer，固定实现选择立即释放本次样本；这些连接因而不会通过此 `last_sample_p` 返回 OldData。若当前没有新样本但仍保留历史指针，则返回 OldData；`copy_old_data=false` 时可以只返回状态而不覆盖输出参数。没有历史指针才是 NoData。控制器若忽略这三个结果的区别，会把传感器停更后的旧样本误当最新观测。

### `ConnPolicy` 决定保存一份值还是一段历史

在建立连接时，`ConnFactory::buildDataStorage` 按 `ConnPolicy::DATA` 与 `BUFFER/CIRCULAR_BUFFER` 选择不同存储对象，再在各自分支依据 `lock_policy` 选择实现。下面是固定版本真实模板函数：

~~~cpp
        static base::ChannelElement<T>* buildDataStorage(ConnPolicy const& policy, const T& initial_value = T())
        {
            if (policy.type == ConnPolicy::DATA)
            {
                typename base::DataObjectInterface<T>::shared_ptr data_object;
                switch (policy.lock_policy)
                {
#ifndef OROBLD_OS_NO_ASM
                case ConnPolicy::LOCK_FREE:
                    data_object.reset( new base::DataObjectLockFree<T>(initial_value, policy) );
                    break;
#else
                case ConnPolicy::LOCK_FREE:
                    RTT::log(Warning) << "lock free connection policy is unavailable on this system, defaulting to LOCKED" << RTT::endlog();
#endif
                case ConnPolicy::LOCKED:
                    data_object.reset( new base::DataObjectLocked<T>(initial_value) );
                    break;
                case ConnPolicy::UNSYNC:
                    data_object.reset( new base::DataObjectUnSync<T>(initial_value) );
                    break;
                }
                return new ChannelDataElement<T>(data_object, policy);
            }
            else if (policy.type == ConnPolicy::BUFFER || policy.type == ConnPolicy::CIRCULAR_BUFFER)
            {
                typename base::BufferInterface<T>::shared_ptr buffer_object;
                switch (policy.lock_policy)
                {
#ifndef OROBLD_OS_NO_ASM
                case ConnPolicy::LOCK_FREE:
                    buffer_object.reset(new base::BufferLockFree<T>(policy.size, initial_value, policy));
                    break;
#else
                case ConnPolicy::LOCK_FREE:
                    RTT::log(Warning) << "lock free connection policy is unavailable on this system, defaulting to LOCKED" << RTT::endlog();
#endif
                case ConnPolicy::LOCKED:
                    buffer_object.reset(new base::BufferLocked<T>(policy.size, initial_value, policy));
                    break;
                case ConnPolicy::UNSYNC:
                    buffer_object.reset(new base::BufferUnSync<T>(policy.size, initial_value, policy));
                    break;
                }
                return new ChannelBufferElement<T>(buffer_object, policy);
            }
            return NULL;
        }
~~~

DATA 创建 `ChannelDataElement<T>`，只保留当前样本；BUFFER/CIRCULAR_BUFFER 则创建 `ChannelBufferElement<T>`，按 `policy.size` 分配所需存储。`LOCK_FREE`、`LOCKED` 和 `UNSYNC` 是与“单槽还是多槽”正交的另一个维度，并且不支持汇编原子操作的构建会把 LOCK_FREE 退回带锁方案。`LOCK_FREE` 不代表赋值任意 `T` 都有固定执行时间；对含 `std::vector` 或堆所有权成员的类型，复制与析构仍可能进入 allocator。做 1 kHz 关节状态闭环前，应同时定义队列深度、过载处理、读旧值的行为和实际样本复制成本，而非只选择一个写着 lock-free 的枚举。
## DATA 与 BUFFER 在负载下表现不同

如果 2 kHz 发布关节状态，1 kHz 消费，DATA 连接只保留最新值。中间样本被覆盖，消费者每次可能得到间隔两倍的样本，但队列不会持续积压。若控制只依赖最新状态，可用采样时间戳判断数据是否过期；不能把“没有队列延迟”误当成“必定读到每个测量”。

BUFFER 是有容量的 FIFO。当容量为 4、生产者快于消费者，4 个槽位满后普通 BUFFER 的新写入失败，WriteFailure 可从 OutputPort::write 返回；旧样本仍在，读者仍可按时间顺序追赶，但控制命令越来越旧。CIRCULAR_BUFFER 满后从最旧端腾出位置保存新项；读者跳过历史以维持较新数据，丢弃计数可以观察。具体实现见 [BufferLockFree::Push](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/BufferLockFree.hpp#L192-L242) 与 [ConnPolicy.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/ConnPolicy.hpp#L50-L88)。

| storage | 满载或更新行为 | 可观察控制后果 |
|---|---|---|
| DATA | 最新写覆盖先前状态 | 中间测量不再可读，消费延迟低 |
| BUFFER | 有界 FIFO，满时拒绝新项 | 旧状态积压，写端收到失败 |
| CIRCULAR_BUFFER | 保留最近容量项，丢弃旧项 | 读端跳过历史，保持相对新鲜 |

FlowStatus 用来区分“现在没有值”和“有值但不是新值”。ChannelDataElement 调用 DataObject::Get；数据第一次被读出后状态变为 OldData，再次读可能复制同一个旧样本；从未写过或 clear 后返回 NoData。InputPort::read(sample, copy_old_data) 默认会复制 OldData；copy_old_data=false 时仍可返回 OldData，但不改写 sample。类型和值关系见 [FlowStatus.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/FlowStatus.hpp#L48-L67) 和 [InputPort::read](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/InputPort.hpp#L136-L150)。

~~~cpp
// 教学最小例子：把旧测量转成可观察故障，而非一直当作新值
JointState state;
const RTT::FlowStatus status = state_in.read(state, false);
if (status == RTT::NewData) {
    control(state);
} else if (status == RTT::OldData) {
    reportStaleSample();
} else {
    enterSafeOutput();
}
~~~

readNewest() 适合只需要当前值、且连接是 queue 时使用。源码先 read 一次，若 NewData 则反复 read(sample,false)，直到队列没有更多 NewData，再返回 NewData。积压为 k 个样本时，它最多做 k 次读取与样本赋值，时间 O(k)；并非固定时间函数。控制器用它追最新队列，可以少追旧数据，但大突发会把 drain 成本带进周期。实现见 [InputPort::readNewest](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/InputPort.hpp#L152-L166)。

## PUSH/PULL 决定哪一侧主动，不等于网络协议语义

对本地 PerConnection PUSH 连接，ConnFactory 在输入端路径安装数据存储，写端调用它时立即 Push；本地 PerConnection PULL 则把 storage 放在 OutputPort 一侧，读端请求时从这个 storage 拉取。PerInputPort 强制 PUSH 并共享输入侧 buffer；PerOutputPort 强制 PULL 并共享输出侧 storage。ConnFactory 的两支 buildChannelInput/buildChannelOutput 实现了这类放置规则，见 [ConnFactory.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L219-L283) 与 [对应输出构造](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/internal/ConnFactory.hpp#L301-L364)。

ConnPolicy 文档指出 push/pull 对多进程通信有意义；本地存储位置规则不能直接推导成跨进程复制行为。若用了远端 port/transport，序列化、协议缓存和传输线程还会加入数据路径。PULL 也不是“自动更省 CPU”：读取端发起拉取后，取样工作可能进入读线程与其截止时间。

## Lock-free storage 仍然会复制样本

固定版本 DataObjectLockFree 使用一组环连的 DataBuf，读写指针和 read_counter/write_lock 协调读写访问。读取先给候选槽位增加 reader 计数，再确认 read_ptr 未被替换，之后用 CAS 将 NewData 改为 OldData，最后把槽位中的 T 复制到调用者变量；写端在候选槽位拿到写权后复制 T，并更新读写指针。可参考 [DataObjectLockFree::Get/Set](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/DataObjectLockFree.hpp#L197-L308)。

原子字段解决的是共享槽位身份与读写重叠的一部分同步问题。它不保护同一个 JointState 对象内部不遵守同步协议的其他成员，也不免除赋值运算成本。该实现用 oro_atomic_* 与 CAS 抽象，不是 C++ std::atomic memory_order API；其平台内存屏障与构建选项应按当前后端审计。若 T::operator=() 在复制 vector 时调用 allocator，lock-free 指针算法仍会走动态分配。

BufferLockFree 也不是“不复制队列”。Push 从预分配 TsPool 取 Item，将传入样本赋值给 Item；Pop 取出 Item 指针，ChannelBufferElement 再把 Item 赋值给 read() 的目标样本，最后释放上一次读取的槽位。无分配结论只覆盖框架在初始化后从池中取槽的行为，不能保证 T 的赋值、构造和析构无分配。用固定容量样本并在 configure 阶段提供 data_sample，才能压低这部分不确定成本。

## 所有权与断连不等于同步调用已经结束

ConnFactory 通过 ChannelElementBase::shared_ptr 连接链条；端点内部还使用 boost intrusive_ptr。ChannelDataElement 持有 DataObjectInterface::shared_ptr，ChannelBufferElement 持有 BufferInterface::shared_ptr。一个 Port 端点并不单独持有“所有消息”；共享/多输入输出 policy 会改变 storage 放在哪个连接一侧。断连应通过连接管理器和 ChannelElement 链完成，而不是直接 delete 中间节点。

写入返回 WriteSuccess 的含义是连接 buffer 对所有 mandatory channel 接收了写入，并不代表另一个进程或组件已经读到样本。源码 [ChannelElement.hpp](https://github.com/orocos-toolchain/rtt/blob/600102e8be9c81905b20930e32d43b28244ab173/rtt/base/ChannelElement.hpp#L100-L125) 明确说明这一点；FlowStatus 与 WriteStatus 是两个方向不同的状态。WriteSuccess 不能用作机器人设备已执行命令的确认。

工程上，disconnect 必须阻止未来访问已失效连接；在途 write/read 的同步规则要按 ConnFactory、PortConnectionLock 与具体 ChannelElement 实现核对。本提交的链路由 shared/intrusive pointer 管理 element 寿命，但应用仍需保证不在组件析构后持有旧 Port/OperationCaller。公开方法调用者也不应把旧样本引用留到下一轮覆盖之后。

## 选择策略与最小复刻

给状态估计器到控制器的姿态连接，可选 DATA 并携带 monotonic timestamp；控制器拒绝年龄超过阈值的样本。给不会丢的离散安全事件，可用固定容量 BUFFER，订阅端检查 WriteFailure 和 dropped counter，容量按最大突发与停顿时间估算。对高速观测流可用 circularBuffer 与 readNewest，但必须测量最大 drain 长度。

最小复刻先写一个类型化 OutputPort/InputPort 接口；连接建立时根据策略创建单值 ChannelDataElement 或定长队列 ChannelBufferElement；写端写入后读端返回 NoData/OldData/NewData；再增加普通 Buffer 满载拒绝和 circular 覆盖旧样本；最后加入多连接 fanout、PUSH/PULL、连接断开与可观测丢弃计数。复刻时为每次写、读、覆盖、满载和断连记录样本序号、线程与时间戳，才能检查数据是否仍新鲜。

