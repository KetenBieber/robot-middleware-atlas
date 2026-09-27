# YARP 使用教程：BufferedPort、Bottle 与 RPC

这一页实现一个最小状态发布器、监视器和控制 RPC。三者使用不同语义：状态流允许丢掉旧样本，控制请求必须得到明确回复，长任务则返回 job id 而不占住 RPC 线程。

## BufferedPort Publisher


```cpp
#include <yarp/os/BufferedPort.h>
#include <yarp/os/Bottle.h>
#include <yarp/os/Network.h>

int main() {
  yarp::os::Network network;
  if (!network.checkNetwork()) return 1;

  yarp::os::BufferedPort<yarp::os::Bottle> port;
  if (!port.open("/atlas/state:o")) return 2;

  auto& bottle = port.prepare();
  bottle.clear();
  bottle.addInt64(1);
  bottle.addString("alive");
  port.write();
  port.close();
}
```

`prepare()` 返回内部复用槽；每次先 clear。调用 write 后不能继续修改引用。

完整循环还应包含连接状态、序号和节拍：


```cpp
#include <chrono>
#include <cstdint>
#include <thread>

std::uint64_t sequence = 0;
while (!stopping.load()) {
  auto& msg = port.prepare();
  msg.clear();
  msg.addInt64(static_cast<std::int64_t>(sequence++));
  msg.addFloat64(read_temperature());

  yarp::os::Stamp stamp;
  stamp.update();
  port.setEnvelope(stamp);
  port.write();
  std::this_thread::sleep_for(std::chrono::milliseconds(20));
}
```

Bottle 适合原型、管理命令和异构字段调试，因为可以直接打印；它的缺点是字段位置与类型只存在于约定中。正式数据流应使用 IDL/Thrift 生成类型，编译器才能检查字段访问，类型签名也能参与连接兼容性判断。

## Subscriber


```cpp
yarp::os::BufferedPort<yarp::os::Bottle> port;
port.open("/atlas/state:i");
while (auto* bottle = port.read()) {
  std::cout << bottle->toString() << '\n';
}
```

read 返回的对象只保证到下一次 read 前有效；需要异步处理时复制为自有对象。

接收循环必须先处理关闭。`interrupt()` 会让阻塞的 `read()` 返回空指针，所以空指针既可能表示中断，也可能表示端口关闭：


```cpp
while (!stopping.load()) {
  const yarp::os::Bottle* msg = port.read();
  if (msg == nullptr) break;
  if (msg->size() < 2 || !msg->get(0).isInt64()) {
    ++malformed_messages;
    continue;
  }
  const auto seq = msg->get(0).asInt64();
  consume(seq, msg->get(1).asFloat64());
}
```

对 Bottle 做长度和类型检查不是多余防御。端口名是运行时连接点，调试工具、旧进程或错误模块都可能发来不同布局。未经检查的 `get(3)` 不是协议解析，只是把错误推迟到业务代码。

YARP 默认 BufferedPort 偏向低延迟，可能丢旧消息；`writeStrict()` 和 `setStrict()` 改为保序 FIFO，但慢消费者会增加延迟和内存。[Buffering Policies](https://yarp.it/latest/yarp_buffering.html)

## 连接


```bash
yarp connect /atlas/state:o /atlas/state:i tcp
```

UDP 是单向 streaming Carrier，不适合 request/reply RPC。[Carrier configuration](https://yarp.it/latest/group__carrier__config.html)

## RPC

服务端使用 RpcServer 或带 replier 的 Port，解析 Bottle 请求并写 Bottle 回复；客户端 RpcClient 执行 write(request, reply)。命令行快速测试：


```bash
yarp rpcserver /atlas/control
yarp rpc /atlas/control
```

输入请求后在 server 终端输入回复。生产协议优先使用 Thrift/IDL 生成类型化接口，避免字符串命令歧义。

RPC handler 不应在 Port 线程执行长任务；对长操作返回 job id，再通过状态 Port 查询。

### 一个有边界的 RPC 服务

继承 `PortReader` 可以把解析与业务执行分开。下面只接受 `set_limit <double>` 和 `status`，未知命令返回结构化错误：


```cpp
class ControlReader final : public yarp::os::PortReader {
public:
  explicit ControlReader(Controller& controller) : controller_(controller) {}

  bool read(yarp::os::ConnectionReader& connection) override {
    yarp::os::Bottle request;
    yarp::os::Bottle reply;
    if (!request.read(connection)) return false;

    const std::string command =
        request.size() > 0 && request.get(0).isString()
            ? request.get(0).asString() : std::string{};
    if (command == "set_limit" && request.size() == 2 &&
        request.get(1).isFloat64()) {
      const double value = request.get(1).asFloat64();
      if (value > 0.0 && value <= 5.0) {
        controller_.enqueue_limit(value); // 有界队列，不在 RPC 线程改控制状态
        reply.addString("ok");
      } else {
        reply.addString("error");
        reply.addString("limit_out_of_range");
      }
    } else if (command == "status") {
      const auto snapshot = controller_.snapshot();
      reply.addString("ok");
      reply.addString(snapshot.mode);
      reply.addInt64(snapshot.last_sequence);
    } else {
      reply.addString("error");
      reply.addString("unknown_or_malformed_command");
    }

    yarp::os::ConnectionWriter* writer = connection.getWriter();
    return writer != nullptr && reply.write(*writer);
  }
private:
  Controller& controller_;
};
```

服务端装配时让 reader 的生命周期覆盖端口：


```cpp
yarp::os::RpcServer rpc;
ControlReader reader(controller);
if (!rpc.open("/atlas/control")) return 1;
rpc.setReader(reader);
// 主线程等待退出信号；关闭时先 rpc.interrupt()，再 rpc.close()。
```

这段设计把 RPC 线程视为协议适配器：它验证请求并把短命令放进控制器自己的有界队列。若直接在 handler 中抓取控制器互斥锁并执行设备操作，一个慢设备就会阻塞所有后续请求，关闭也可能卡在回调内部。

### 客户端的失败语义


```cpp
yarp::os::RpcClient client;
client.open("/atlas/client");
yarp::os::Network::connect(client.getName(), "/atlas/control");

yarp::os::Bottle request, reply;
request.addString("status");
if (!client.write(request, reply)) {
  // 传输失败：不能假设服务端完全没有执行请求。
} else if (reply.get(0).asString() != "ok") {
  // 应用拒绝：连接正常，但请求无效或当前状态不允许。
}
```

RPC 超时后存在经典的不确定性：请求可能未到达，也可能已经执行但回复丢失。会产生副作用的命令应携带 request id，并由服务端缓存最近完成结果，使客户端重试保持幂等。`increment` 这类不可幂等命令尤其不能在未知结果后直接重发。

## 串起三个进程


```bash
# 终端 1
yarpserver
# 终端 2、3、4
./state_monitor
./control_server
./state_publisher
# 终端 5
yarp connect /atlas/state:o /atlas/state:i tcp
printf "status\n" | yarp rpc /atlas/control
```

先观察 sequence 连续增长，再故意让 monitor 睡眠：非 strict 状态流应保持较新的样本而跳号；strict 模式应保序但延迟增加。最后停止 Name Server，确认既有状态连接的实际行为，并验证此时新开的诊断端口无法注册。这个实验能同时建立控制面与数据面的边界感。

## 验收

- prepare/write 后不再修改 buffer；
- read 指针不跨下一次 read 使用；
- strict 与非 strict 在慢消费者下表现符合预期；
- TCP RPC 有 timeout，UDP 只用于流；
- 每条消息携带 sequence/envelope timestamp。
