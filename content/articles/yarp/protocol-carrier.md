# Protocol 与 Carrier：握手、消息边界和数据变换

Carrier 决定连接如何说话，Protocol 保存一次连接正在进行到哪一步。二者配合把任意字节流变成具有 Route、消息边界与确认语义的 YARP 连接。

本文中的 YARP 源码固定于 `robotology/yarp` 提交 `91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9`。

## 功能需求决定 Protocol 与 Carrier 分离

PortCore 需要一套稳定的“读一条消息、写一条消息、关闭连接”接口，但底层可能是 TCP、本地直连、UDP、多播或插件提供的协议。固定提交中的 built-in prototype 包含这些主线实现。若 PortCore 为每种传输写 `switch`，每增加一种 Carrier 都要修改核心状态机。

YARP 将职责拆开：

```text
Protocol
  负责：连接生命周期、Route、读写阶段、ack、modifier 组合
    |
    `-- Carrier
          负责：识别 header、握手细节、frame 编解码、底层能力
                |
                `-- TwoWayStream
                      负责：实际字节输入、输出和 interrupt/close
```

Protocol 是一条连接的协调对象，Carrier 描述这条连接采用的线格式与握手规则，TwoWayStream 提供字节流。这里的所有权不是图上的抽象箭头：`Protocol` 构造时接管传入的 `TwoWayStream*`，内部 `ShiftStream` 持有它；关闭 Protocol 会关闭 stream，并释放它创建的 Carrier 对象。后文会回到析构代码核对这件事。

**固定提交源码摘录：**

```cpp
Protocol::Protocol(TwoWayStream* stream) :
        messageLen(0),
        pendingAck(false),
        active(true),
        delegate(nullptr),
        recv_delegate(nullptr),
        send_delegate(nullptr),
        need_recv_delegate(false),
        need_send_delegate(false),
        recv_delegate_fail(false),
        send_delegate_fail(false),
        route("null", "null", "tcp"),
        writer(nullptr),
        ref(nullptr),
        envelope(""),
        port(nullptr),
        pendingReply(false)
{
    shift.takeStream(stream);
    reader.setProtocol(this);
}

Protocol::~Protocol()
{
    closeHelper();
}
```

源码身份：固定提交中的 `Protocol::Protocol(TwoWayStream*)` 与 `Protocol::~Protocol()`。成员 `writer` 和 `ref` 是当前消息相关指针，不拥有应用 Writer/Portable；相反，stream 经 `shift.takeStream()` 转交，Protocol 的构造声明也明确 Protocol 成为 stream owner。每条连接需要自己的 Protocol，避免并发修改这些连接状态。

## 核心接口的 C++ 形状

Carrier 是有虚函数的基类：主动端需要从名称选出 Carrier，被动端需要先读固定长度的 header，再让各 Carrier 判别它。`create()` 为每条连接产生独立的运行实例。真实实现使用原始指针，因此还必须追查谁 delete；下面先看提交中的接口，而不是用智能指针示例代替它：

**固定提交源码摘录：**

```cpp
class Carrier : public Connection
{
public:
    virtual ~Carrier();
    virtual Carrier* create() const = 0;
    virtual bool checkHeader(const Bytes& header) = 0;
    virtual void setParameters(const Bytes& header) = 0;
    virtual void getHeader(Bytes& header) const override = 0;
    virtual bool canAccept() const = 0;
    virtual bool canOffer() const = 0;
    virtual bool sendHeader(ConnectionState& proto) = 0;
    virtual bool expectReplyToHeader(ConnectionState& proto) = 0;
    virtual bool write(ConnectionState& proto, SizedWriter& writer) = 0;
    virtual bool expectIndex(ConnectionState& proto) = 0;
    virtual bool sendAck(ConnectionState& proto) = 0;
    virtual bool expectAck(ConnectionState& proto) = 0;
};
```

源码身份：固定提交中的 `Carrier::create`、`Carrier::checkHeader`、`Carrier::write` 等接口节选，省去了与本章数据路径无关的地址、文本模式和 socket 操作接口。`ConnectionState&` 是 Protocol 暴露给 Carrier 的流、Route 与帧状态接口；Carrier 不拥有另一份业务消息。

这里返回 `Carrier*` 而非 `unique_ptr`。把 prototype 原样返回并复用于连接，会让连接共享可变握手状态；YARP 的工厂选择实际调用 `create()`，Protocol 接管返回指针并在关闭时 `delete`。因此阅读此代码时不能把普通的栈生命周期或 `unique_ptr` RAII 自动套到真实对象上。

抽象类的虚析构保证 Protocol 通过基类指针销毁派生对象时会进入派生析构；实际 Protocol 的三个 Carrier 指针正是 raw owning pointer，而其 stream 则由 ShiftStream 单独持有。

## 主动端与被动端握手

```text
active side                         passive side
Protocol::open(Route)
  -> choose carrier by name
  -> connect stream
  -> send carrier header ---------> read header
  -> send sender/route spec ------> choose carrier by bytes
                                    parse route
  <----------- response/header ---- respondToHeader
  -> optional index/ack exchange
  -> OPEN                          -> OPEN
```

握手任一步失败后，调用方都应停止在这条连接上发业务帧；发起端 `Protocol::open(route)` 在 send header 或等待应答失败时直接返回 false，被动端同样在识别 header/响应失败时返回 false。是否立即释放 stream 由外层连接 Unit 的失败清理完成，不能假设每个 `open()` 分支都会自行关闭。

主动端从 Route 的 carrier name 选择 Carrier；被动端读取协议标识后逐个询问 registry prototype。下面是两条入口在固定版 Protocol 中的真实分支：

**固定提交源码摘录：**

```cpp
bool Protocol::open(const std::string& name)
{
    if (name.empty()) {
        return false;
    }
    Route r = getRoute();
    r.setToName(name);
    setRoute(r);
    bool ok = expectHeader();
    if (!ok) {
        return false;
    }
    return respondToHeader();
}

bool Protocol::open(const Route& route)
{
    setRoute(route);
    setCarrier(route.getCarrierName());
    if (delegate == nullptr) {
        return false;
    }
    bool ok = sendHeader();
    if (!ok) {
        return false;
    }
    return expectReplyToHeader();
}
```

出处：YARP 固定提交中的 `Protocol::open(const std::string&)` 与 `Protocol::open(const Route&)`。服务端先从对端字节推断协议并回复；发起端先从 Route 设 Carrier、发 header，再等对端响应。失败时函数直接返回 false，后续消息不能依赖这条连接已握手成功。两端的方法顺序不同，不能把上图理解成两端同时执行同一个状态机。

握手状态最好显式表达：

**教学代码（不是固定提交源码摘录）：**

```cpp
enum class ProtocolState {
  Created,
  HeaderSent,
  HeaderAccepted,
  Open,
  Closing,
  Closed,
  Failed,
};
```

用多个 `bool sent_header_ / received_reply_ / ok_` 会产生大量无效组合。枚举使每个方法能检查允许的前置状态，也让故障日志说明失败发生在哪个阶段。

网络一次 `read()` 不保证拿到完整 header。`ReadExact(n)` 必须循环处理短读、EOF、interrupt 和错误；对端声明的可变长度必须先与最大值比较，再分配内存。握手还未认证时尤其不能相信任意长度字段。

## Carrier prototype registry

主动端按名称查 prototype；被动端先读 8 字节，再按 header 逐个调用 `checkHeader()`。固定版 registry 内部实际是 `std::vector<Carrier*>`，按名称和按 header 都线性扫描；发现匹配项后返回 `create()` 新建的裸指针，而不是 prototype 本身：

**固定提交源码摘录：**

```cpp
Carrier* Carriers::Private::chooseCarrier(const std::string& name,
                                          bool load_if_needed,
                                          bool return_template)
{
    auto pos = name.find('+');
    if (pos != std::string::npos) {
        return chooseCarrier(name.substr(0, pos), load_if_needed, return_template);
    }

    for (auto& delegate : delegates) {
        Carrier& c = *delegate;
        if (name == c.getName()) {
            if (!return_template) {
                return c.create();
            }
            return &c;
        }
    }

    if (load_if_needed) {
        if (NetworkBase::registerCarrier(name.c_str(), nullptr)) {
            return Carriers::Private::chooseCarrier(name, false);
        }
    }
    return nullptr;
}

Carrier* Carriers::Private::chooseCarrier(const Bytes& header,
                                          bool load_if_needed)
{
    for (auto& delegate : delegates) {
        Carrier& c = *delegate;
        if (c.checkHeader(header)) {
            return c.create();
        }
    }
    if (load_if_needed && scanForCarrier(header)) {
        return Carriers::Private::chooseCarrier(header, false);
    }
    return nullptr;
}
```

出处：YARP 固定提交中的 `Carriers::Private::chooseCarrier` 两个重载。摘录省去错误日志，保留名称规范化、插件按需注册、线性扫描与 fresh instance 创建分支。返回 `&c` 只发生在 `return_template` 请求 prototype 的特殊分支；普通名称选择和 header 匹配都调用 `create()`。

Prototype 也有明确释放者。进程级 `Carriers` 清理时删除它注册的原型；单连接的 Protocol 清理时则删除 `create()` 产生的实例。这两层 raw pointer 的 owner 不能混淆：

**固定提交源码摘录：**

```cpp
void Carriers::clear()
{
    for (auto& delegate : mPriv->delegates) {
        delete delegate;
        delegate = nullptr;
    }
    mPriv->delegates.clear();
}
```

出处：同一固定提交中的 `Carriers::clear`。清 registry 会销毁 prototype；它不替代 `Protocol::closeHelper()` 对每个活跃连接 Carrier 实例的释放。若插件动态库在 prototype 或连接对象仍活着时卸载，析构与虚函数调用都可能落到失效的动态库代码中。

TCP prototype 把每个实例需要的 ack 配置复制到新对象；握手 header 把 TCP 类型与 ack 位编码进去。下面的实现还说明 `checkHeader()` 是对已经读入的八字节做识别，并非先接收任意长度的协议数据：

**固定提交源码摘录：**

```cpp
yarp::os::Carrier* yarp::os::impl::TcpCarrier::create() const
{
    return new TcpCarrier(requireAckFlag);
}

bool yarp::os::impl::TcpCarrier::checkHeader(const yarp::os::Bytes& header)
{
    int spec = getSpecifier(header);
    if (spec % 16 == getSpecifierCode()) {
        if (((spec & 128) != 0) == requireAckFlag) {
            return true;
        }
    }
    return false;
}
```

出处：同一固定提交中的 `TcpCarrier::create` 与 `TcpCarrier::checkHeader`。Prototype 与运行时连接实例是两个对象；Protocol 持有 factory 返回的实例。名称路径与 header 路径都随已注册 prototype 数线性增长，扫描量受 carrier 种类影响，而非当前机器人连接数。

按需插件注册意味着未知 Carrier 会尝试动态加载；部署者可以按安全边界配置插件来源与允许列表。卸载时要先确保所有 Protocol 实例和正在执行的虚函数都已退出，否则一个仍被调用的 vtable 可能落在已卸载库里。固定代码片段展示了按需注册，但没有提供热卸载屏障，因此不能从 registry 存在反推出运行期卸载安全。

## `beginRead/endRead` 定义一条输入消息

TCP 只交付有序字节，没有“这一条机器人消息到此结束”的标记。YARP 因此让 Carrier 先解释连接上的 index，再由 Protocol 把解出的长度交给 `StreamConnectionReader`，最后由输入线程调用业务 Reader。固定源码中的顺序如下：

**固定提交源码摘录：**

```cpp
ConnectionReader& Protocol::beginRead()
{
    getRecvDelegate();
    if (delegate != nullptr) {
        bool ok = false;
        while (!ok) {
            ok = expectIndex();
            if (!ok) {
                if (!is().isOk()) {
                    ok = true;
                }
            }
        }
        respondToIndex();
    }
    return reader;
}

void Protocol::endRead()
{
    reader.flushWriter();
    sendAck();
}
```

出处：YARP 固定提交中的 `Protocol::beginRead/endRead`。`beginRead()` 创建/复用的是 Protocol 成员里的 Reader，不是为每个消息分配的新对象；输入 Unit 在该线程上调用配置的 `PortReader::read()`，返回后 `endRead()` 先刷新回复 writer，再调用 Carrier 侧 ack。循环中的 `is().isOk()` 是“底层输入流仍可用”的判断：一次帧头不符合预期时，代码可能继续尝试下一次 `expectIndex()`，直到成功或 stream 失败。

Carrier 负责把输入 index 翻译成这次消息的 payload 长度。默认二进制实现先读 8 字节整数并要求其值为 10，再读 10 字节描述符及每个输入/输出 block 的长度，累加后写入 Protocol 的 `messageLen`：

**固定提交源码摘录：**

```cpp
bool AbstractCarrier::defaultExpectIndex(ConnectionState& proto)
{
    char buf[8];
    Bytes header((char*)&buf[0], sizeof(buf));
    yarp::conf::ssize_t r = proto.is().readFull(header);
    if ((size_t)r != header.length()) {
        return false;
    }
    int len = interpretYarpNumber(header);
    if (len < 0) {
        return false;
    }
    if (len != 10) {
        return false;
    }

    char buf2[10];
    Bytes indexHeader((char*)&buf2[0], sizeof(buf2));
    r = proto.is().readFull(indexHeader);
    if ((size_t)r != indexHeader.length()) {
        return false;
    }
    int inLen = (unsigned char)(indexHeader.get()[0]);
    int outLen = (unsigned char)(indexHeader.get()[1]);

    int total = 0;
    NetInt32 numberSrc;
    Bytes number((char*)&numberSrc, sizeof(NetInt32));
    for (int i = 0; i < inLen; i++) {
        yarp::conf::ssize_t l = proto.is().readFull(number);
        if ((size_t)l != number.length()) {
            return false;
        }
        int x = NetType::netInt(number);
        total += x;
    }
    for (int i2 = 0; i2 < outLen; i2++) {
        yarp::conf::ssize_t l = proto.is().readFull(number);
        if ((size_t)l != number.length()) {
            return false;
        }
        int x = NetType::netInt(number);
        total += x;
    }
    proto.setRemainingLength(total);
    return true;
}
```

出处：YARP 固定提交中的 `AbstractCarrier::defaultExpectIndex`。上面删去调试输出，保留完整长度读取和累加逻辑。原函数把网络来的每个 int32 长度直接加到有符号 `int total`，没有对负长度或累计上限做校验；复刻时应用 checked arithmetic，并在读 header 时就施加最大 frame size。`Protocol::expectIndex()` 成功后调用 `reader.reset(is(), &getStreams(), getRoute(), messageLen, ...)`；这把总长度保存成 Reader 的计数，并不等同于已经拦截每次读取。

这里必须区分“记下本帧还剩多少字节”和“拒绝越界读取”。固定提交的 `StreamConnectionReader::expectBlock()` 会在成功读入后扣减 `messageLen`，但没有先检查请求长度不超过它：

**固定提交源码摘录：**

```cpp
bool StreamConnectionReader::expectBlock(Bytes& b)
{
    if (!isGood()) {
        return false;
    }
    yAssert(in != nullptr);
    size_t len = b.length();
    if (len == 0) {
        return true;
    }
    if (len > 0) {
        yarp::conf::ssize_t rlen = in->readFull(b);
        if (rlen >= 0) {
            messageLen -= len;
            return true;
        }
    }
    err = true;
    return false;
}
```

出处：同一固定提交中的 `StreamConnectionReader::expectBlock(Bytes&)`。`isGood()` 只检查 reader active/valid/error 状态，也不比较 `len` 与 `messageLen`。因此若收到长度为 12 的 index，应用 schema 却请求 16 字节，底层 `readFull` 可能继续吞掉下一帧的 4 个字节，然后 `messageLen` 变成负数；下一次 `beginRead()` 会从 frame 中间读 index，导致连接失步，后续消息不能按原边界解析。YARP 在这一对象里维护了长度信息，但这段代码没有把长度信息落实为 block 上界。写缩小版时，应在任何读操作之前检查 `requested <= remaining`，超界立即标记协议错误并关闭或丢弃当前连接。

一个有限 Reader 可以这样实现边界：

**教学代码（不是固定提交源码摘录）：**

```cpp
class LimitedReader final : public ConnectionReader {
 public:
  LimitedReader(InputStream& stream, std::size_t frame_size)
      : stream_(stream), remaining_(frame_size) {}

  bool expectBlock(char* dst, std::size_t size) override {
    if (size > remaining_) {
      error_ = true;
      return false;
    }
    if (!ReadExact(stream_, dst, size)) {
      error_ = true;
      return false;
    }
    remaining_ -= size;
    return true;
  }

 private:
  InputStream& stream_;       // 借用，寿命由 Protocol 保证
  std::size_t remaining_;
  bool error_ = false;
};
```

在下面的复刻代码中，`remaining_` 才是真正执行的帧预算：每个读取请求先与剩余字节比较，通过后才访问 stream。`InputStream&` 仍是借用引用，Reader 只能在 Protocol 持有 stream 的读帧期间使用。这个差异是必须显式说明的：推荐实现的安全性质不能反推到上游当前提交。

`endRead()` 还要决定剩余字节如何处理：完全消费、丢弃至帧尾，还是把未读视为协议错误。当前 `Protocol::endRead()` 先刷新可能存在的 RPC 回复，再进入 Carrier 的 `sendAck()`；它本身没有把 `messageLen` 非零转为丢弃或拒绝动作。若应用只读取 schema 的前半部分，就必须继续核对输入线程与 Carrier 如何处理未读 bytes；不能把 ack 当作“帧所有字段已验证”的证明。

## 写路径是 PortWriter 与 Carrier 的双重分派

应用对象实现 `PortWriter::write(ConnectionWriter&)`，Carrier/Protocol 提供具体 ConnectionWriter。一次写入的结构是：

```text
PortWriter dynamic type
  -> write(ConnectionWriter provided by Protocol)
       -> appendInt/appendBlock/convertTextMode...
            -> Carrier framing/index
                 -> OutputStream
```

第一次动态分派选择业务序列化，第二次选择连接协议。这样 Bottle、Image 或生成类型不需要知道 TCP framing，Carrier 也不需要知道业务字段。

扇出时同一个 `PortWriter const&` 可能被多个连接调用。`write()` 必须逻辑只读且可重复；若它在第一次调用后移动走内部 buffer，第二个订阅者会得到空消息。需要一次性编码时，应由 PortCore 建立不可变字节快照，再供多个 Protocol 使用。

接下来是连接层真正发送这份编码 buffer 的入口。`SizedWriter` 包含一组已序列化的 block；Protocol 先让 Modifier 按需初始化，检查连接 active，再把“一条 packet”边界交给 Carrier。写完成后，它可以读取 RPC reply，最后执行 carrier-specific ack 等待：

**固定提交源码摘录：**

```cpp
bool Protocol::write(SizedWriter& writer)
{
    writer.stopWrite();
    if (!getConnection().isActive()) {
        return false;
    }
    this->writer = &writer;
    bool replied = false;
    yCAssert(PROTOCOL, delegate != nullptr);
    getStreams().beginPacket();
    bool ok = delegate->write(*this, writer);
    getStreams().endPacket();
    PortReader* reply = writer.getReplyHandler();
    if (reply != nullptr) {
        if (!delegate->supportReply()) {
            yCInfo(PROTOCOL, "connection %s does not support replies (try \"tcp\" or \"text_ack\")",
                   getRoute().toString().c_str());
        }
        if (ok) {
            reader.reset(is(), &getStreams(), getRoute(), messageLen,
                         delegate->isTextMode(), delegate->isBareMode());
            replied = reply->read(reader);
        }
    }
    expectAck();
    this->writer = nullptr;
    return replied;
}
```

出处：YARP 固定提交中的 `Protocol::write(SizedWriter&)`。被 Carrier 写出的对象是 Protocol 临时借用的 `writer`；Protocol 不在此函数内复制它。注意该函数的 `bool` 是 `replied`，不是普通 one-way 写入的“stream 写成功”值；Carrier 写入的 `ok` 在 RPC reply 分支决定是否读回复。

Base Carrier 如何把 block 写到底层输出流，可从 `AbstractCarrier::write()` 看到：

**固定提交源码摘录：**

```cpp
bool AbstractCarrier::write(ConnectionState& proto, SizedWriter& writer)
{
    bool ok = sendIndex(proto, writer);
    if (!ok) {
        return false;
    }
    writer.write(proto.os());
    proto.os().flush();
    return proto.os().isOk();
}
```

出处：同一固定提交中的 `AbstractCarrier::write`。Carrier 先写连接特有 index，之后才把业务 block 送到 `OutputStream` 并 flush。stream 可写成功只说明本机写路径没有报告错误；它不能证明远端业务对象已读取或执行。

默认 Carrier 的 index 也不是一个孤立的 payload 长度整数。它先标出固定 10 字节的描述区，其中记录业务 writer 的 block 数和一个输出 block；随后逐块写入 32 位长度，最后写入长度为 0 的回复块描述：

**固定提交源码摘录：**

```cpp
bool AbstractCarrier::defaultSendIndex(ConnectionState& proto, SizedWriter& writer)
{
    writeYarpInt(10, proto);
    int len = (int)writer.length();
    char lens[] = {(char)len, (char)1, (char)-1, (char)-1, (char)-1,
                   (char)-1, (char)-1, (char)-1, (char)-1, (char)-1};
    Bytes b(lens, 10);
    OutputStream& os = proto.os();
    os.write(b);
    NetInt32 numberSrc;
    Bytes number((char*)&numberSrc, sizeof(NetInt32));
    for (int i = 0; i < len; i++) {
        NetType::netInt((int)writer.length(i), number);
        os.write(number);
    }
    NetType::netInt(0, number);
    os.write(number);
    return os.isOk();
}
```

出处：YARP 固定提交中的 `AbstractCarrier::defaultSendIndex`。这里只为排版把 `char lens` 初始化器折成多行；控制流和数据相同。Wire 上先出现 8 字节 YARP integer `10`，然后 10 字节索引描述，之后是 block lengths，最后由 `writer.write(proto.os())` 发送数据 blocks。若第一块报告 8 字节、第二块报告 4 字节，index 中保存的是两段长度，而不是把 C++ 对象布局直接写进 socket。

## Modifier 是一条可选的数据变换支路

Carrier name 可带 `send`/`recv` 参数。Protocol 将基础连接行为保存在 `delegate`，并可另外保存一个发送侧 `send_delegate` 和一个接收侧 `recv_delegate`。固定类只有各一个 modifier 指针，因此不能把它描述成任意多项、任意排序的通用装饰链。发送侧 modifier 的工作是产生要序列化的 Writer 或改写 reply；接收侧 modifier 则可变换收到的数据。

`beginWrite()` 会延迟检查 sender qualifier。下列函数是实际的创建与失败处理：

**固定提交源码摘录：**

```cpp
bool Protocol::getSendDelegate()
{
    if (send_delegate != nullptr) {
        return true;
    }
    if (!need_send_delegate) {
        return true;
    }
    if (send_delegate_fail) {
        return false;
    }
    Bottle b(getSenderSpecifier());
    std::string tag = b.find("send").asString();
    send_delegate = Carriers::chooseCarrier(tag);
    if (send_delegate == nullptr) {
        fprintf(stderr, "Need carrier \"%s\", but cannot find it.\n", tag.c_str());
        send_delegate_fail = true;
        close();
        return false;
    }
    if (!send_delegate->modifiesOutgoingData()) {
        fprintf(stderr, "Carrier \"%s\" does not modify outgoing data as expected.\n", tag.c_str());
        send_delegate_fail = true;
        close();
        return false;
    }
    if (!send_delegate->configure(*this)) {
        fprintf(stderr, "Carrier \"%s\" could not configure the send delegate.\n", tag.c_str());
        send_delegate_fail = true;
        close();
        return false;
    }
    return true;
}
```

出处：YARP 固定提交中的 `Protocol::getSendDelegate`，删去注释。Modifier 必须存在、声明自己修改 outgoing data，并成功用本 Protocol 配置；否则连接被关闭，错误不会静默降级成普通 TCP。对应的 `getRecvDelegate()` 检查 `modifiesIncomingData()` 并独立配置另一指针。

一个实际单 modifier 路径可以表示为：

```text
PortWriter -> sender delegate modifies outgoing data -> SizedWriter
           -> base Carrier framing -> OutputStream
```

当前 Unit 的 `sendHelper()` 先用 sender delegate 取得修改后的 `PortWriter`，再调用这个 Writer 的 `write(BufferedConnectionWriter&)`；Protocol 后续用基础 Carrier 输出 framing。若 modifier 返回引用而非拥有型值，原始 Writer、modifier 内部缓冲与 Unit 当前任务必须共同保持有效，直到序列化完成。具体哪些数据可复用、是否复制，取决于 modifier 实现，不能从 `Carrier*` 抽象接口推导出零拷贝。

若自己设计多个压缩、监视、认证步骤，需要显式设计排序和逆向恢复、缓冲边界与失败清理。发送端的“压缩后认证”要求接收端以“验证后解压”恢复，且任何一步失败都应拒绝业务 Reader；这是扩展协议时的设计问题，并非本提交对任意 Modifier 组合给出的保证。

## ACK 到底确认了什么

`tcp` Carrier 的 `requireAckFlag` 为 true；注册表也提供 `fast_tcp`，它把同一 TCP Carrier 配成不要求 ACK。需要 ACK 的配置继承 AbstractCarrier 的默认实现。发送方在 `Protocol::write()` 的尾部调用 `expectAck()`；接收方处理完一条消息后，`InputUnit` 调用 `Protocol::endRead()`，后者 flush 可能的 reply，再经 `sendAck()` 写 ACK。因而 ACK 至少晚于接收线程调用业务 `PortReader::read()` 返回。它仍不等于硬件已经完成运动：Reader 可以只是把目标值放入自己的控制队列。

这里的“业务 Reader 返回”不是抽象推演。`PortCoreInputUnit::run()` 在当前 input worker 上将接收的 reader 交给 `PortCore::readBlock()`；后者在 callback 锁保护下同步调用应用 `PortReader::read()`，回到 input loop 后再调用 `ip->endRead()`。若应用 Reader 里等待马达动作完成，ACK 就会连带等待；若它只把 setpoint 放入内部队列，ACK 只确认这一段同步处理结束。

**固定提交源码摘录：**

```cpp
if (ip->getReceiver().acceptIncomingData(br)) {
    ConnectionReader* cr = &(ip->getReceiver().modifyIncomingData(br));
    yarp::os::impl::PortDataModifier& modifier = getOwner().getPortModifier();
    modifier.inputMutex.lock();
    if (modifier.inputModifier != nullptr) {
        if (modifier.inputModifier->acceptIncomingData(*cr)) {
            cr = &(modifier.inputModifier->modifyIncomingData(*cr));
            modifier.inputMutex.unlock();
            man.readBlock(*cr, id, os);
        } else {
            modifier.inputMutex.unlock();
            skipIncomingData(*cr);
        }
    } else {
        modifier.inputMutex.unlock();
        man.readBlock(*cr, id, os);
    }
}
```

出处：YARP 固定提交中的 `PortCoreInputUnit::run` 的数据命令分支，略去 envelope 与其他 Port 命令分支。再往下一层：

**固定提交源码摘录：**

```cpp
if (m_reader != nullptr && !m_interrupted) {
    m_interruptable = false;
    bool haveOutputs = (m_outputCount != 0);
    if (m_logNeeded && haveOutputs) {
        ConnectionRecorder recorder;
        recorder.init(&reader);
        lockCallback();
        result = m_reader->read(recorder);
        unlockCallback();
        recorder.fini();
        sendHelper(recorder, PORTCORE_SEND_LOG);
    } else {
        lockCallback();
        result = m_reader->read(reader);
        unlockCallback();
    }
    m_interruptable = true;
} else {
    Bottle b;
    result = b.read(reader);
}
return result;
```

出处：同一固定提交中的 `PortCore::readBlock` 的核心分支，省去日志和 tracing。OS 线程从 socket stream 读到消息并不代表 callback 已开始；线程先解析命令和 index，再同步进入 Reader。Reader 卡住时，这一 input worker 无法开始读同一连接的下一帧；callback 锁还会阻止其他受同一 callback 锁串行化的调用。Reader 返回后，InputUnit 才执行 `endRead()`，Carrier 才有机会发 ACK。

**固定提交源码摘录：**

```cpp
bool AbstractCarrier::defaultSendAck(ConnectionState& proto)
{
    if (proto.getConnection().requireAck()) {
        writeYarpInt(0, proto);
    }
    return true;
}

bool AbstractCarrier::defaultExpectAck(ConnectionState& proto)
{
    if (proto.getConnection().requireAck()) {
        char buf[8];
        Bytes header((char*)&buf[0], sizeof(buf));
        yarp::conf::ssize_t hdr = proto.is().readFull(header);
        if ((size_t)hdr != header.length()) {
            return false;
        }
        int len = interpretYarpNumber(header);
        if (len < 0) {
            return false;
        }
        size_t len2 = proto.is().readDiscard(len);
        if ((size_t)len != len2) {
            return false;
        }
    }
    return true;
}
```

出处：YARP 固定提交中的 `defaultSendAck/defaultExpectAck`，省去诊断日志。默认 ACK 的 YARP 整数值为 0；读端先读 8 字节，再按编码长度丢弃剩余 ACK 内容。固定 `Protocol::write()` 会调用 `expectAck()`，但没有检查其返回值；若输入 EOF 导致 stream 本身错误，后续 `isOk()` 可以观察到；若读到完整但不合法的 ACK，Carrier 返回 false 而 stream 仍保持正常，这个返回值在该函数中没有被向上传递。因此不能把这行函数调用解读成公开 `Port::write()` 一定会以失败结束。

ack 读取本身可能阻塞。`Protocol::setTimeout()` 将 timeout 设置到 output 与 input stream；中断则通过 `Protocol::interrupt()` 打断输入 stream。是否启用 ACK、超时是多少、底层 Carrier 怎样定义 ACK 都改变尾延迟。工程上要把发送已进入本地 stream、ACK 已由对端协议写回、业务控制动作执行完成分开记录，而不能把它们都叫“发送成功”。

中断与销毁是不同状态转换。Port 关闭时必须先让可能阻塞在 read/ack 上的 worker 有机会退出；`interrupt()` 尝试结束等待，但不释放 Carrier 对象。之后 `closeHelper()` 才关闭 owned stream 并 delete 基础 Carrier 和两个 modifier：

**固定提交源码摘录：**

```cpp
void Protocol::interrupt()
{
    if (!active) {
        return;
    }
    if (pendingAck) {
        sendAck();
    }
    shift.interruptInputStream();
    active = false;
}

void Protocol::closeHelper()
{
    active = false;
    if (pendingAck) {
        sendAck();
    }
    shift.close();
    if (delegate != nullptr) {
        delegate->close();
        delete delegate;
        delegate = nullptr;
    }
    if (recv_delegate != nullptr) {
        recv_delegate->close();
        delete recv_delegate;
        recv_delegate = nullptr;
    }
    if (send_delegate != nullptr) {
        send_delegate->close();
        delete send_delegate;
        send_delegate = nullptr;
    }
}
```

出处：YARP 固定提交中的 `Protocol::interrupt` 与 `Protocol::closeHelper`。`interrupt()` 先处理 pending ack，然后打断输入 stream 并把 active 置 false；最终 close 负责关闭 stream 和销毁所拥有的三类 Carrier。`PortCoreOutputUnit::closeMain()` 的真实关停顺序如下：

**固定提交源码摘录：**

```cpp
void PortCoreOutputUnit::closeMain()
{
    if (finished) {
        return;
    }

    if (running) {
        std::shared_ptr<OutputProtocol> localOp = op;
        if (localOp) {
            localOp->interrupt();
        }

        closing = true;
        phase.post();
        activate.post();
        join();
    }

    closeBasic();
    running = false;
    closing = false;
    finished = true;
}
```

出处：同一固定提交中的 `PortCoreOutputUnit::closeMain`，删去诊断日志和解释性注释。局部 `shared_ptr` 保活 Protocol 直到 interrupt 调用结束；`join()` 等 worker 离开 `sendHelper` 与 Carrier 虚调用；随后 `closeBasic()` 关闭 Protocol 并重置 Unit 的 `shared_ptr`。该 join 是线程执行边界，不能只把 `closing=true` 写上就卸载 Carrier 插件。另一个生命周期反例是若只 reset `op` 而 worker 仍在调用 `delegate->write()`，Protocol/Carrier 可能在虚调用中途析构；此版本通过 interrupt、唤醒和 join 再 close 的顺序避免该悬空调用。

## 字节序、结构布局与版本

稳定协议不能直接发送 C++ struct：padding、`bool` 宽度、enum 底层类型和宿主端序都可能变化。固定字段应使用明确宽度与编码函数：

**教学代码（不是固定提交源码摘录）：**

```cpp
void WriteU32BE(OutputStream& out, std::uint32_t value) {
  std::array<std::byte, 4> bytes{
    std::byte((value >> 24) & 0xff),
    std::byte((value >> 16) & 0xff),
    std::byte((value >> 8) & 0xff),
    std::byte(value & 0xff),
  };
  out.write(bytes);
}
```

协议演进至少需要 magic、版本或可识别 header，并给未知字段/版本明确行为。默默按旧布局解析新 header 会比立即拒绝更危险。对设备 NWC/NWS 的业务协议，集中在 Thrift/IDL 中生成两端代码也比在客户端与服务端分别手写 VOCAB 更易保持一致。

## 协议测试应使用 golden bytes 与故障注入

最小测试包括：合法握手字节、未知 header、截断 header、超长 Route、每个字段的大小端、空 payload、多帧连续读取、对端不 ack、modifier 初始化失败以及握手中途断开。

内存 TwoWayStream 可在不启 socket 的情况下执行 golden byte 测试；随后再验证真实 TCP 的分段读取，因为一次 `read()` 不保证返回完整 header。

## 性能边界

Protocol 的固定成本相对大 payload 较小，但 modifier 可能引入整帧缓存、压缩和额外复制。评估时分别记录 logical payload、wire bytes、encode time、frame copies 和 ack latency。压缩降低带宽不一定降低端到端延迟。

对小消息，虚调用、header 和系统调用占比更高，合批可能提高吞吐但增加等待延迟；对大图像，复制与压缩占主导，scatter/gather 和专用 Carrier 更重要。一个统一 benchmark 数字无法代表这两种负载。

连接数为 `N` 时，每连接独立 Protocol/Carrier 的状态空间为 `O(N)`。若每个 OutputUnit 还有线程和栈，规模瓶颈可能先来自执行单元而不是 framing 算法。需要同时观察连接数、线程数、队列深度、短写次数和 p99 ACK 时间。

## 结构带来的收益、局限与可迁移思想

从职责和变化点看，Carrier 接口隔离传输策略，registry prototype 经 `create()` 产生每连接实例；PortWriter 与 ConnectionWriter 的双分派隔离业务序列化和 wire framing。Protocol 只有一个发送 modifier 槽和一个接收 modifier 槽，因此这里并不存在任意多层的通用 Decorator 链。这样的分离让 PortCore 不必随 Carrier 种类膨胀，但也把配置错误推到运行期。

局限是动态插件和字符串配置把部分错误推迟到运行期；发送与接收各一个 modifier 的配置仍需核对相互匹配；借用型 Reader 生命周期依赖约定；同步 ACK 和每连接执行单元可能放大慢节点影响。

可迁移原则是：协议状态使用显式枚举；识别函数无副作用且有输入上界；每连接拥有独立可变状态；帧 Reader 强制剩余预算；wire format 不发送原生 struct；插件只在所有实例销毁后卸载；确认语义精确说明完成点。

## 最小复刻顺序

第一版只实现固定 magic、长度前缀和 `LimitedReader` 的单一 TCP Carrier。第二版加入 Carrier registry 与 prototype clone。第三版增加主动/被动握手状态机和 Route。第四版加入一种 modifier 并验证发送/接收逆序。最后再实现 ACK、timeout、interrupt 和插件加载。

完成标准包括：短读可正确拼成 header；超长 frame 在分配前被拒绝；错误 schema 不能越过帧尾；未知 Carrier 只消耗有限字节；握手失败后不能发送 payload；中断能退出 ACK 等待；并发连接不共享可变 Carrier；关闭后没有 vtable 指向已卸载动态库。


