# YARP 阅读基础：从两个机器人进程开始理解 Port、名字服务与 Carrier

第一次接触 YARP 时，最容易遇到的困难不是 API 太多，而是几个看似熟悉的词同时出现：Port、Contact、Route、Name Server、Protocol、Carrier。若直接背这些类名，它们很快会混在一起。更自然的办法，是先看一个机器人程序为什么需要它们。

假设机器人上有两个独立进程：相机驱动持续产生图像，视觉程序读取图像并检测障碍物。开发者希望相机程序只声明“我提供图像”，视觉程序只声明“我需要图像”，至于两个进程是否位于同一台计算机、使用 TCP 还是共享内存、视觉程序何时启动，都不要写死在算法代码里。

YARP 为这个问题提供的核心抽象叫作 **Port**。可以先把它理解成“一个有名字、可以在运行期接线的通信端口”：

```text
camera process                              vision process

  /camera/image  ------------------------>  /vision/image
       Port             connection               Port
```

这张图只够解释“看起来怎么用”，还不能解释程序怎样运行。真正的数据路径还要经过名字解析、连接建立、协议握手、消息编码以及接收回调；这些层次共同决定 Port 最终怎样把数据送进接收进程。

源码基线固定为 YARP 仓库 `robotology/yarp` 的提交 `91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`，类、函数和运行行为均以这一提交为准。

## YARP 解决的是运行期接线问题

在一个很小的程序里，发送者可以直接知道接收者的 IP 地址和端口：

```text
connect("192.168.1.42", 5000)
```

机器人系统很快会让这种写法失效。相机驱动可能从开发机移到车载计算机，记录器可能临时加入，某条连接可能为了调试改用文本协议，另一条连接则需要高吞吐二进制传输。若地址、协议和拓扑都写进业务代码，每次部署变化都要重新修改和编译算法。

YARP 把问题拆成三个决定：

1. **模块叫什么**：例如 `/camera/image`，这是逻辑名字；
2. **模块现在在哪里**：例如主机、端口号和可用的传输方式；
3. **这一次连接怎样传数据**：例如使用哪种 Carrier，怎样握手和划分消息边界。

这三个决定分别落到 Name Server、Contact/Route 和 Carrier 一带。这样，算法代码依赖稳定的逻辑名字，部署系统负责把名字接成实际连接。

这种设计很适合实验室机器人、iCub 一类多模块研究平台，以及需要频繁替换设备和算法的系统。它的代价也很明确：连接可以动态改变，意味着不少错误只能在运行期暴露；每条连接还带有自己的协议和生命周期状态，系统规模变大以后，线程、缓冲和关闭顺序都必须认真分析。

## 从四行代码建立第一个运行时模型

最小的发送端看起来很简单：


```cpp
yarp::os::Network yarp;
yarp::os::Port output;

output.open("/camera/image");
output.write(image);
output.close();
```

不要把这四行理解为“创建一个 socket、写一次、关掉 socket”。每一行背后承担的职责不同：

- `Network yarp` 初始化进程级的 YARP 网络环境和必要的插件能力；
- `Port output` 只构造一个尚未启动的 C++ 门面对象；
- `open()` 让端口获得可用地址、启动监听，并按配置向名字服务注册；
- `write()` 把同一个逻辑消息交给当前所有输出连接；
- `close()` 停止读写、关闭连接并等待后台线程退出。

固定提交的 `Port` 类注释把它定位为维护动态输入、输出连接集合的“小型服务器”。一次 `Port::write()` 会把数据发送到所有输出连接，而不是天然只对应一个接收者。下面的公共声明摘录给出其边界：


```cpp
class YARP_os_API Port : public UnbufferedContactable
{
public:
    Port();
    ~Port() override;

    bool open(const std::string& name) override;
    void close() override;
    void interrupt() override;

    bool write(const PortWriter& writer,
               const PortWriter* callback = nullptr) const override;
    bool read(PortReader& reader, bool willReply = false) override;
};
```

先看 C++ 表面含义。`Port` 继承 `UnbufferedContactable`，所以调用者面对的是统一的可连接端口接口。成员函数后的 `override` 表示它们覆盖基类中的虚函数：编译器会检查函数签名是否真的与基类契约匹配。析构函数同样是虚函数，这保证通过基类指针释放派生端口时，派生对象能够完整析构。

再看参数里的 `const PortWriter&`。这里没有复制待发送对象，`Port` 暂时借用一个只读引用。这个签名本身并不保证零拷贝，也不保证引用在后台线程中一直有效；它只说明进入 `write()` 时没有转移 C++ 对象所有权。真正是否复制、何时完成编码，要继续看同步写、后台写和 `onCompletion()` 的约定。

## Port 是门面，PortCore 才是运行时内核

公开类 `Port` 故意保持小巧。它没有暴露连接表、监听线程和协议对象，而是通过内部实现转发到 `PortCoreAdapter`。固定提交中 `Port::needImplementation()` 的关键部分如下：


```cpp
void* Port::needImplementation() const
{
    if (implementation != nullptr) {
        return implementation;
    }
    Port* self = const_cast<Port*>(this);
    self->implementation = new yarp::os::impl::PortCoreAdapter(*self);
    yCAssert(PORT, self->implementation != nullptr);
    self->owned = true;
    return self->implementation;
}

#define IMPL() (*reinterpret_cast<yarp::os::impl::PortCoreAdapter*>(needImplementation()))
```

这段代码第一次读起来会有些别扭，可以分四步理解。

第一，`implementation` 最初是空指针。只有某个操作真正需要运行时内核时，`needImplementation()` 才分配 `PortCoreAdapter`。这是一种延迟创建：仅仅构造一个 `Port`，不会马上启动网络资源。

第二，`owned` 记录当前 `Port` 是否负责销毁这个实现对象。YARP 还支持 `sharedOpen()`，两个门面可能引用同一个实现；因此“指向对象”和“拥有对象”不能画等号。

第三，`const_cast<Port*>(this)` 去掉了 `this` 的只读限定。这是因为 `needImplementation()` 被声明为 `const`，却要惰性写入实现指针。它让 `const write()` 能延迟初始化内部状态，但也提高了阅读门槛：这里的逻辑 const 表示“对外观察到的端口身份不变”，不代表对象内存一字不改。

第四，`reinterpret_cast` 和宏是较老式的类型隐藏手法。它把无类型指针重新解释为具体实现。好处是公开头文件不需要暴露 `PortCoreAdapter`，从而降低编译依赖并稳定 ABI；缺点是类型安全主要依赖类内部约定，现代代码通常更倾向于 `std::unique_ptr<Impl>` 形式的 PImpl。

于是，第一个重要对象关系可以画成：

```text
application code
      |
      v
    Port                    小而稳定的公开门面
      |
      | implementation
      v
PortCoreAdapter             把公开 API 适配到内部内核
      |
      v
   PortCore                 连接集合、监听、发送、接收、关闭状态
```

这就是 Facade 与 PImpl 风格设计真正解决的问题：调用者不必理解内部连接状态才能使用端口，库作者也能在不大幅改变公开类布局的情况下演进实现。后面的 `PortCore` 章节会继续追踪这层转发，而不是停在“这里用了门面模式”这个标签上。

## 名字、地址和连接路线是三个不同概念

假设视觉模块希望连接 `/camera/image`。这个字符串易于人类理解，却不足以让操作系统建立连接：socket 最终仍需要主机名、端口和传输协议。

YARP 用 `Contact` 表示“怎样到达一个网络参与者”。固定提交的 `Contact` 构造函数声明已经揭示了它保存的信息：


```cpp
Contact(const std::string& name = std::string(),
        const std::string& carrier = std::string(),
        const std::string& hostname = std::string(),
        int port = -1);
```

可以把一个 Contact 想成通讯录条目：

```text
logical name : /camera/image
host         : robot-pc.local
port         : 10042
carrier      : tcp
```

其中有些字段可以暂时未知。只有逻辑名字时，YARP 可以向 Name Server 查询；已经知道完整地址时，也可以绕过名字注册直接打开连接。

`Route` 表示的不是一个端点，而是两个端点之间的连接意图。它保存来源名字、目标名字、目标 `Contact` 和 Carrier 名。固定提交把公开对象压成 PImpl 指针，再由实现对象持有这些值：


```cpp
class Route::Private
{
public:
    Private(std::string fromName,
            std::string toName,
            Contact toContact,
            std::string carrierName) :
            fromName(std::move(fromName)),
            toName(std::move(toName)),
            toContact(std::move(toContact)),
            carrierName(std::move(carrierName))
    {
    }

    std::string fromName;
    std::string toName;
    Contact toContact;
    std::string carrierName;
};
```

`Route` 的构造函数把输入名字复制到 PImpl 中，并将尚未解析的目标 `Contact` 初始化为空值：

接着看 `Route::Route(const std::string&, const std::string&, const std::string&)` 的真实实现：

```cpp
Route::Route(const std::string& fromName,
             const std::string& toName,
             const std::string& carrierName) :
        mPriv(new Private(fromName,
                          toName,
                          Contact(),
                          carrierName))
{
}
```

读取接口返回 PImpl 中字符串的 const 引用，不复制字符串，也不转移所有权：

接着看 `Route::getFromName()` 的真实实现：

```cpp
const std::string& Route::getFromName() const
{
    return mPriv->fromName;
}
```

因此，下列三个值不能混用：

```text
Port name : /camera/image
Contact   : tcp://robot-pc.local:10042 以及它关联的逻辑名
Route     : /camera/image --tcp--> /vision/image
```

Name Server 主要参与从逻辑名字到可达 Contact 的解析和注册。连接建立以后，普通图像 payload 通常由两端直接传输，不再绕经 Name Server。由此可以推出一个非常实用的故障边界：名字服务暂时失效会妨碍新建连接和重连，却不必然让已经建立的端到端数据流立即中断。

## Carrier 决定连接怎样说话

知道目标地址仍然不够。双方还要约定怎样识别连接、怎样划分一条消息、是否等待确认，以及如何关闭。YARP 把这组协议行为封装为 Carrier。

可以先用一个不完全但好记的类比：

- Port 名像联系人名字；
- Contact 像电话号码和所在网络；
- Route 像“从谁打给谁”；
- Carrier 像这通连接使用的通话规则。

这个类比的边界也要说清：Carrier 不只是一个字符串标签。真正的 Carrier 实现参与连接头识别、客户端与服务端握手、消息 framing、确认、modifier 以及中断和关闭。选择不同 Carrier，可能改变可靠性、拷贝次数、延迟和可观测行为。

一条已经建立的发送路径大致如下：

```text
Port::write(message)
  -> PortCore 遍历当前输出连接
     -> 每条连接对应一个 OutputUnit
        -> Protocol 保存这条连接的会话状态
           -> Carrier 实现握手与消息边界
              -> TwoWayStream / socket 搬运字节
```

`OutputUnit` 的存在很重要。一个 Port 可以连接多个接收者，每个接收者可能使用不同协议、具有不同速度或处于不同错误状态。YARP 为每条连接保留独立执行状态，从而避免所有协议状态揉成一个大对象；相应代价是连接数增加时，连接对象、队列、线程栈和调度成本也会增加。

## 应用对象通过 PortWriter 变成连接字节

Port 不可能预先知道所有机器人消息类型。图像、关节状态、激光点云和控制命令的字段完全不同，但它们都需要进入同一套连接系统。YARP 用一个很小的虚接口解决这个变化点。

固定提交中 `PortWriter` 的核心契约是：


```cpp
class PortWriter
{
public:
    virtual ~PortWriter();
    virtual bool write(ConnectionWriter& writer) const = 0;
    virtual void onCompletion() const;
    virtual void onCommencement() const;
};
```

这里有三个值得慢慢拆开的 C++ 概念。

`virtual` 表示动态分派。`PortCore` 手里只需要一个 `PortWriter&`，运行时仍会调用真实消息类重写的 `write()`。因此新增 `JointState` 或 `ImageFrame` 不需要修改 PortCore。

函数末尾的 `= 0` 表示纯虚函数。`PortWriter` 只定义“可写对象必须提供什么能力”，自身不能直接实例化。它是协议边界，而不是一个装数据的基类。

参数 `ConnectionWriter&` 把消息结构和传输编码分开。消息知道字段以什么顺序写出，ConnectionWriter 知道怎样把这些字段交给当前连接。一个简单的关节状态可以这样实现：

**教学最小例子（机器人关节状态的自定义编码，不是 YARP 原始类）：**

```cpp
class JointState final : public yarp::os::Portable {
public:
    std::int32_t sequence{};
    double position{};

    bool write(yarp::os::ConnectionWriter& writer) const override {
        writer.appendInt32(sequence);
        writer.appendFloat64(position);
        return !writer.isError();
    }

    bool read(yarp::os::ConnectionReader& reader) override {
        sequence = reader.expectInt32();
        position = reader.expectFloat64();
        return !reader.isError();
    }
};
```

`Portable` 同时继承 `PortReader` 和 `PortWriter`。固定提交直接表达了这个关系：


```cpp
class Portable : public PortReader, public PortWriter
{
public:
    bool read(ConnectionReader& reader) override = 0;
    bool write(ConnectionWriter& writer) const override = 0;
};
```

这不是“继承用于复用数据成员”，因为接口几乎没有业务状态。它是在类型系统里声明：这个对象既懂得从连接读取自己，也懂得把自己写入连接。

上面的代码还隐藏着一个重要限制：发送方与接收方必须对字段顺序和类型达成一致。发送端先写 `int32` 再写 `float64`，接收端就必须按相同顺序读取。C++ 类名相同不会自动带来跨进程 schema，也不会自动解决字段新增、版本兼容和字节序问题；这些契约仍需要生成类型系统或明确的 wire 规则来维护。

## 一次 write 可能多次调用消息对象

很多读者会自然地认为：应用调用一次 `Port::write(message)`，那么 `message.write(connection)` 也只执行一次。`PortWriter` 的源码注释明确提醒，这个假设不成立：根据连接数量、协议组合和缓存复用情况，`write()` 可能不调用、调用一次，也可能调用多次。

原因可以从扇出路径看出来：

```text
                       -> TCP connection A -> ConnectionWriter A
Port::write(message)  -> TCP connection B -> ConnectionWriter B
                       -> text carrier C   -> ConnectionWriter C
```

若 A 与 B 使用完全相同的编码，框架可能复用已序列化缓冲；若 C 使用不同格式，就需要重新让对象写出数据。因此 `PortWriter::write()` 应当像一个只读操作：它不能第一次执行时把成员移动走，也不能依赖“只会调用一次”的副作用。

这也解释了 `write()` 为什么是 `const`。`const` 不能绝对禁止所有外部副作用，但它在接口层表达了一个关键意图：序列化不应消耗逻辑消息。对同一个对象进行多连接编码时，各连接应得到一致内容。

## 同步写与后台写的区别首先是生命周期

`Port` 默认通信会耦合发送者和接收者的时序。调用者等待到约定的写阶段完成后再继续，最容易理解的数据生命周期是：

```text
stack message exists
  -> Port::write borrows it
  -> encoding / sending completes
  -> Port::write returns
  -> caller may modify or destroy message
```

当调用 `enableBackgroundWrite(true)` 后，`write()` 的目标是尽快返回。此时后台执行单元可能仍需要消息内容：

```text
application thread                    output worker

Port::write(message)
  -> submit work --------------------> later serialize(message)
  -> return
```

如果后台只保存了栈对象的裸引用，而调用者在返回后立即销毁或修改对象，就会出现悬空引用或数据竞争。因此后台发送必须通过内部缓存、完成回调或其他明确协议，把消息的有效期延长到最后一条连接完成使用。`onCommencement()` 与 `onCompletion()` 正是这类生命周期契约的一部分。

“后台写”只把等待从调用线程移到别处，不会创造额外网络带宽。慢接收者仍然存在，区别只是它导致调用者阻塞、连接忙、数据排队还是跳过发送。后续发送路径章节会沿 `PortCore::sendHelper()` 和 `PortCoreOutputUnit` 精确区分这些状态。

## open 不只是把名字放进注册表

`Port::open()` 比公开接口长得多，因为它要完成一段生命周期，而不是一次简单赋值。删去前面的环境变量、Name 配置和 NestedContact 校验后，固定提交中的连续核心路径如下：

```text
检查 Network 是否初始化
  -> 解析名字、环境变量与 Contact
  -> 必要时向 Name Server 注册，取得可用地址
  -> PortCore::listen(address, ...)
  -> PortCore::start()
  -> 端口进入 active 状态
```

接着看 `Port::open(const Contact&, bool, const char*)` 的真实实现：

```cpp
    PortCoreAdapter& core = IMPL();

    core.openable();

    if (NetworkBase::localNetworkAllocation() && contact2.getPort() <= 0) {
        yCDebug(PORT, "local network allocation needed");
        local = true;
    }

    bool success = true;
    Contact address(contact2.getName(),
                    contact2.getCarrier(),
                    contact2.getHost(),
                    contact2.getPort());
    address.setNestedContact(contact2.getNested());

    core.setReadHandler(core);
    if (contact2.getPort() > 0 && !contact2.getHost().empty()) {
        registerName = false;
    }

    std::string ntyp = getType().getNameOnWire();
    if (ntyp.empty()) {
        NestedContact nc;
        nc.fromString(n);
        if (!nc.getTypeName().empty()) {
            ntyp = nc.getTypeName();
        }
    }
    if (ntyp.empty()) {
        ntyp = getType().getName();
    }
    if (!ntyp.empty()) {
        NestedContact nc;
        nc.fromString(contact2.getName());
        nc.setTypeName(ntyp);
        contact2.setNestedContact(nc);
        if (getType().getNameOnWire() != ntyp) {
            core.promiseType(Type::byNameOnWire(ntyp.c_str()));
        }
    }

    if (registerName && !local) {
        address = NetworkBase::registerContact(contact2);
    }

    core.setControlRegistration(registerName);
    success = (address.isValid() || local) && (fakeName == nullptr);

    if (success) {
        NestedContact nc;
        nc.fromString(address.getName());
        if (!nc.getNestedName().empty()) {
            if (nc.getCategory() == "+1") {
                addOutput(nc.getNestedName());
            }
        }
    }

    std::string blame = "invalid address";
    if (success) {
        success = core.listen(address, registerName);
        blame = "address conflict";
        if (success) {
            success = core.start();
            blame = "manager did not start";
        }
    }
    if (success) {
        address = core.getAddress();
        if (registerName && local) {
            contact2.setSocket(address.getCarrier(),
                               address.getHost(),
                               address.getPort());
            contact2.setName(address.getRegName());
            Contact newName = NetworkBase::registerContact(contact2);
            core.resetPortName(newName.getName());
            address = core.getAddress();
        } else if (core.getAddress().getRegName().empty() && !registerName) {
            core.resetPortName(core.getAddress().toURI(false));
            core.setName(core.getAddress().getRegName());
        }

        if (address.getRegName().empty()) {
            yCIInfo(PORT, core.getName(),
                   "Anonymous port active at %s",
                   address.toURI().c_str());
        } else {
            yCIInfo(PORT, core.getName(),
                   "Port %s active at %s",
                   address.getRegName().c_str(),
                   address.toURI().c_str());
        }
    }
```

这段摘录按上游顺序连续保留了有效 `Contact`、Name Server 注册、listener 建立、管理线程启动和本地随机端口二次注册；前面的配置解析以及后续 fake port/失败日志未包含在摘录范围内。这里的 `registerContact()` 不是业务 payload 代理：它在 open 阶段解析或发布端口地址，payload 后续由连接两端直接传输。

这条顺序解释了几个常见现象。只构造 `Port` 并不会产生可连接端点；名字注册成功也不等于监听线程一定启动成功；若 `Network` 尚未初始化，`open()` 会直接失败。定位启动故障时，应区分配置解析、名字注册、地址冲突、监听建立和管理线程启动，而不是笼统地归因于“YARP 没连上”。

同时，源码允许同一 Port 多次安全调用 `open()`：如果旧内核已经打开，它会创建新内核、迁移应该保留的 reader 和配置、关闭旧内核，再切换实现。这种便利提高了内部生命周期复杂度，也说明 `open()` 不是无副作用的轻量 getter。

## interrupt、close 与析构承担不同责任

通信线程可能阻塞在读取、写入或等待确认上。只设置一个 `closing = true` 标志并不能唤醒这些线程，所以 YARP 把“打断阻塞”和“完成关闭”分成不同动作。

接下来对照固定版本的实际代码：

```cpp
void Port::close()
{
    if (!owned) {
        return;
    }

    PortCoreAdapter& core = IMPL();
    core.finishReading();
    core.finishWriting();
    core.close();
    core.join();
    core.active = false;
}

void Port::interrupt()
{
    IMPL().interrupt();
}
```

这段代码是固定提交的 `Port::close()` 与 `Port::interrupt()`。它给出了清晰的关闭顺序：先让正在等待的读写收口，再关闭内部资源，然后 `join()` 等待工作线程真正退出。只有在这些步骤完成后，Port 才把自己标记为不再 active。

`interrupt()` 的目标更窄：让阻塞操作尽快返回，端口之后还可能通过 `resume()` 恢复。`close()` 则结束当前生命周期。析构函数最后也会调用 `close()`，但显式关闭更利于处理错误、记录超时并控制各组件的退出顺序。

这里的 `owned` 检查再次提醒我们：某个对象能调用方法，不等于它拥有底层资源。`sharedOpen()` 产生的非拥有门面不应替另一个 Port 销毁共享内核。

## 控制面与数据面需要分开观察

把 YARP 放进机器人系统后，至少有两条不同的健康链路：

```text
控制面：Port name -> Name Server -> Contact -> connect / reconnect

数据面：PortWriter -> PortCore -> OutputUnit -> Protocol / Carrier
       -> stream -> InputUnit -> PortReader
```

控制面正常，只能说明名字可解析、连接可能建立；它不能证明相机还在产生新图像。数据面仍然可能因为生产者停止、接收回调阻塞或连接积压而失去新鲜度。反过来，Name Server 短暂不可用时，已经建立的端到端连接仍可能继续传输。

因此，机器人系统的监控不应只有“端口名是否存在”。至少还应记录最后一条消息时间戳、序列号是否连续、每连接发送状态、丢弃数量以及回调耗时。名字服务回答“模块在哪里”，不能回答“控制环当前拿到的数据有多旧”。

## 性能边界来自扇出、编码和慢连接

设一个输出 Port 当前连接 `C` 个接收者。一次发送至少需要遍历这些连接，所以管理成本不会优于 `O(C)`。若不同连接不能复用相同编码，还要分别承担序列化和传输成本：

```text
一次逻辑 write 的成本

  O(C)                         遍历连接
  + Σ encode(message, format)  为不同 wire 格式编码
  + Σ enqueue_or_write         每条连接排队或写入
  + optional acknowledgements  协议确认与往返等待
```

对于小控制指令，锁竞争、线程唤醒和确认往返可能占主要部分；对于大图像，序列化、内存复制和网络带宽通常更显著。一个 Port 同时连接记录器、可视化器和控制器时，最慢连接是否拖慢其他连接，取决于同步/后台写、连接单元和缓冲策略，不能只由“用了 TCP”推断。

YARP 提供灵活的通信基础设施，却不是硬实时调度器。动态连接、虚函数分派和协议插件本身不必然很慢，但它们也没有消除内存分配、操作系统调度、page fault 和用户回调超时。对 1 kHz 控制环，应把硬实时计算与网络分发分开，使用预分配的最新值快照跨越边界，并单独测量最坏数据年龄和抖动，而不是只看平均吞吐量。

## 适用场景与不适用边界

YARP 的优势集中在运行期组合能力：逻辑名字让模块位置可变，Port 统一了常见通信入口，Carrier 把协议变化从业务对象中剥离，Reader/Writer 接口又让自定义 C++ 数据能进入同一条通路。这些特性非常适合需要频繁替换模块、连接真实设备并进行交互式调试的机器人研发环境。

它并不自动提供以下保证：

- 仅靠 C++ 类型名完成跨版本 schema 演进；
- 在所有 Carrier 下都实现相同的可靠性和延迟；
- 慢消费者出现时自动选择正确的丢弃或背压策略；
- 把应用优先级一直传递为操作系统实时线程优先级；
- 让所有动态插件天然满足 ABI 与安全要求；
- 为硬实时控制环给出可证明的最坏执行时间。

这些不是简单的“缺点列表”，而是架构选择的另一面。动态性越强，运行期验证和可观测性就越重要；协议越可插拔，兼容矩阵和部署约束就越需要明确。

## 带着一张图进入后续源码

读完本章，只需要牢牢记住下面这条主线：

```text
逻辑名字
  -> Name Server 解析 Contact
  -> Route 描述 from / to / carrier
  -> Port 作为应用门面
  -> PortCore 管理连接集合和生命周期
  -> InputUnit / OutputUnit 隔离单条连接
  -> Protocol 保存连接会话状态
  -> Carrier 实现握手、framing 与传输语义
  -> PortReader / PortWriter 连接应用对象与 wire bytes
```

后续阅读按运行顺序展开：先看组件地图和 `PortCore` 对象关系，再跟踪 `Port::write()` 如何扇出到各个 OutputUnit，接着进入 Carrier 握手和接收回调，最后处理并发关闭、C++ 所有权和最小复刻。

当再次看到 `Port` 时，不要只把它理解为“一个 topic 端点”。更准确的心智模型是：它是一扇稳定的公开门，门后由 PortCore 管理一组会动态变化、协议各异、生命周期相互独立的连接。
