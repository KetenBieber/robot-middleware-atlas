# ROS1 消息序列化：C++ 对象为什么能稳定变成 wire bytes

固定源码版本：ros_comm `30483a9f218f1545eec16d3934bf3cb042e2cb5b`（Noetic）；序列化核心固定到 roscpp_core `a1a194271bfb35b97a09553f2782585cf7fec9db`。

跨进程 TCP 不能“发送一个 C++ 对象”。对象里的 `std::string`、`std::vector`、指针和 allocator 状态只对当前地址空间有意义。ROS1 必须把消息转换成与进程虚拟地址无关的线性 byte sequence。

## 1. 为什么 memcpy(message) 通常是错的

考虑：

~~~cpp
struct ImageLike
{
  std::string frame;
  std::vector<uint8_t> data;
};
~~~

对象本体并不内嵌整个字符串和整幅图。string/vector 通常保存长度、容量和指向 heap storage 的地址。

若直接：

~~~cpp
send(fd, &msg, sizeof(msg), 0);
~~~

跨进程复制的只是本进程里的指针数值。对端进程没有相同虚拟地址映射，这些地址没有意义。

所以序列化必须按**字段语义**递归编码，而不是复制 C++ object layout。

## 2. Serializer<T> 是编译期协议接口

roscpp_core 的默认模板：

~~~cpp
template<typename T>
struct Serializer
{
  template<typename Stream>
  inline static void write(
      Stream& stream,
      typename boost::call_traits<T>::param_type t)
  {
    t.serialize(stream.getData(), 0);
  }

  template<typename Stream>
  inline static void read(Stream& stream, T& t)
  {
    t.deserialize(stream.getData());
  }

  inline static uint32_t serializedLength(
      typename boost::call_traits<T>::param_type t)
  {
    return t.serializationLength();
  }
};
~~~

primitive、string、vector、array 和生成的 ROS message 都可以提供 specialization 或 compatible implementation。

它真正解决的是：

> 对具体 MessageType 的编码方式在编译期解析，hot path 不需要运行时反射或虚函数查表去逐字段解释 schema。

## 3. 为什么同一个字段协议要支持 length、write、read

真正发送可变长消息前，先要知道总 buffer 大小。

一种朴素实现是动态 append：

~~~text
write field A
buffer full -> realloc
write field B
buffer full -> realloc
...
~~~

这会产生重复扩容和 copy。

ROS serialization 使用三种 stream role：

~~~text
LStream
  只累计 serialization length

OStream
  向预分配 buffer 写 bytes

IStream
  从 buffer 读取
~~~

字段层面的 Serializer 逻辑保持一致，不同 Stream 决定“处理这个字段”是在计数、写入还是读取。

这是典型的策略复用：协议 schema 不重复，执行行为由 stream 类型决定。

## 4. serializeMessage 为什么先遍历一次长度

固定源码：

~~~cpp
template<typename M>
inline SerializedMessage serializeMessage(
    const M& message)
{
  SerializedMessage m;

  uint32_t len =
      serializationLength(message);

  m.num_bytes = len + 4;
  m.buf.reset(
      new uint8_t[m.num_bytes]);

  OStream s(
      m.buf.get(),
      static_cast<uint32_t>(m.num_bytes));

  serialize(
      s,
      static_cast<uint32_t>(m.num_bytes) - 4);

  m.message_start = s.getData();

  serialize(s, message);

  return m;
}
~~~

内存布局：

~~~text
m.buf
 |
 v
+----------------+----------------------------+
| uint32 length  | serialized message fields  |
+----------------+----------------------------+
                  ^
                  m.message_start
~~~

第一遍 `serializationLength` 换来一次精确分配，第二遍才真正写字段。

代价也很明确：复杂可变长消息要做一次 length traversal + 一次 encode traversal。

## 5. string/vector 为什么必须显式编码长度

跨进程 wire format 不能使用 C++ string 的 capacity 或结尾指针。

逻辑格式需要：

~~~text
uint32 byte_count
byte[byte_count]
~~~

vector 同理：

~~~text
uint32 element_count
element 0
element 1
...
~~~

固定大小 primitive 则可以用更直接的编码。

因此“ROS 消息字段”与“C++ 容器内存布局”是两个层次。

## 6. Message Traits 和 Serializer 为什么不能混为一谈

ROS1 还有：

~~~text
MD5Sum<T>
DataType<T>
Definition<T>
~~~

它们回答：

~~~text
这是什么 schema？
连接双方能不能互相解释？
~~~

Serializer 回答：

~~~text
这个具体对象怎样编码成 bytes？
~~~

前者属于**类型身份**，后者属于**wire representation**。

连接建立时交换 type/md5sum，一旦确认兼容，每帧 payload 不需要重复携带完整 schema。

## 7. Publisher 为什么没有一进入 API 就立刻 serialize

`Publisher::publish` 可以把 serialization 封装成延迟函数：

~~~cpp
publish(
  boost::bind(
    serializeMessage<M>,
    boost::ref(message)),
  m);
~~~

`TopicManager::publish` 先检查实际 delivery path：

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

if (!nocopy)
{
  m.message.reset();
  m.type_info = 0;
}

if (serialize || p->isLatching())
{
  SerializedMessage m2 = serfunc();

  m.buf = m2.buf;
  m.num_bytes = m2.num_bytes;
  m.message_start = m2.message_start;
}

p->publish(m);
~~~

如果所有 subscriber 都能使用同进程 C++ object path，`serfunc` 甚至不必执行。

这是**lazy serialization**：编码成本只在真的需要 wire bytes 时支付。

## 8. 为什么 latching 会改变 encoding policy

Latched publisher 要在未来新 Subscriber 加入时立刻发最后一条消息。

未来 Subscriber 可能是远程进程，所以不能只留下某个当前作用域中的 C++ object reference。固定实现因此需要在 latching 时保留可重放的 serialized representation。

这说明 storage lifetime 需求会反过来影响 encoding policy。

## 9. 接收端同样使用 lazy deserialization

网络线程收到完整 bytes 后，没有立即把它变成用户消息对象。

`Subscription::handleMessage` 创建或复用 `MessageDeserializer`，放入 `SubscriptionQueue`。Spinner 真正执行时：

~~~cpp
VoidConstPtr msg =
    i.deserializer->deserialize();

if (msg)
{
  SubscriptionCallbackHelperCallParams params;

  params.event =
      MessageEvent<void const>(
        msg,
        i.deserializer->getConnectionHeader(),
        i.receipt_time,
        i.nonconst_need_copy,
        MessageEvent<void const>::CreateFunction());

  i.helper->call(params);
}
~~~

这有两个直接效果：

- PollManager 网络线程不承担用户对象构造的 CPU 开销；
- 如果队列过载把旧消息丢掉，被丢的 SerializedMessage 不必完成完整反序列化。

对于高频传感器，这是很实际的分工。

## 10. 一个 serialized buffer 为什么能服务多个远端链接

`SerializedMessage` 的 buffer 使用 shared ownership。Publication 扇出到多个 SubscriberLink 时，可以复用同一份字段级 encoding 结果：

~~~text
C++ message
     |
     | serialize once
     v
SerializedMessage buffer
     |
     +--> TCP link A
     +--> TCP link B
     +--> TCP link C
~~~

这不等于真正端到端 zero-copy。每条 socket 仍要把数据送入自己的 kernel/network path。但至少避免了为每个 Subscriber 重新做一次完整字段序列化。

## 11. std::vector 的数据结构语义为什么重要

vector 在 C++ 进程内通常拥有连续 storage，所以 primitive vector 可以高效线性遍历；但 wire format 仍必须保存 element count。

这两个事实同时成立：

~~~text
C++ vector:
  contiguous local memory

ROS wire:
  explicit portable length + elements
~~~

连续存储有利于 cache/memcpy；显式长度保证跨语言、跨进程恢复结构。

中间件经常就在“本机数据结构效率”和“wire representation 可移植性”之间搭桥。

## 12. 为什么大图像把 serialization 成本放大

1920×1080 RGB 原始 payload 约：

~~~text
1920 * 1080 * 3
≈ 6.2 MB/frame
~~~

30 Hz 接近：

~~~text
~186 MB/s raw payload
~~~

再考虑：

- user buffer 生产；
- serialization read/write；
- socket kernel copy；
- receiver buffer；
- deserialization；
- 算法自己的图像转换；

内存带宽很容易成为系统成本的一大部分。

所以 Nodelet 的价值往往不是减少几个函数调用，而是避免大 payload 在 transport 层做不必要的 byte transform。

## 13. 反序列化发生在哪个线程为什么很重要

假设点云反序列化要 3 ms，算法要 5 ms。

两者都发生在同一串行 Spinner callback path 时：

~~~text
service time ≈ 8 ms
sustainable serial rate ≈ 125 Hz
~~~

输入如果达到 200 Hz，队列仍然会过载。

因此性能分析不能只测 network RTT，也不能只测 `serializeMessage()` 微基准。应该观察：

~~~text
capture timestamp
  -> serialization
  -> network
  -> SubscriptionQueue
  -> deserialization
  -> callback start
  -> callback end
~~~

## 14. 生成代码为什么把 schema 编译进类型

ROS message generator 最终生成 C++ message 类型和 serialization traits。这样运行时不需要拿着文本版 `.msg` 去解析每个字段。

变化过程是：

~~~text
.msg schema
   |
code generation
   v
C++ type + traits + serializer
   |
compile
   v
runtime hot path
~~~

这用 build-time code generation 换取运行时简单性。

代价是 schema 变化通常意味着重新生成、重新编译，并通过 MD5 identity 阻止不同定义静默互通。

## 15. 从源码作者视角看 serialization 的对象边界

可以把模块拆成四种对象：

~~~text
Message type
  业务字段

Traits
  schema identity

Serializer<T>
  field encoding rules

SerializedMessage
  encoded buffer ownership
~~~

如果把它们混成一个“大消息类”，类型身份、业务数据、编码策略和 buffer lifetime 会互相污染。

ROS1 的拆法虽然历史较久，但职责边界很清楚。

## 16. 序列化给 zero-copy 留下了什么问题

跨进程协议必须拥有：

~~~text
portable representation
+
independent lifetime
~~~

如果想避免 byte serialization，就必须用其他机制重新解决这两件事。

共享内存系统通常改成：

~~~text
shared data segment
+
offset/handle instead of process-local raw pointer
+
loan/borrow/reclaim protocol
~~~

Nodelet 则走另一条路：不跨地址空间，直接共享 C++ object。

这两个方案都不是“把 Serializer 再优化一点”，而是改变 payload ownership model。
