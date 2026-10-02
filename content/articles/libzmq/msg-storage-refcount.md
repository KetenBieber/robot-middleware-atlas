# msg_t：64 字节消息对象怎样同时兼顾小消息、扇出与零拷贝

固定源码版本：46493370217ac135246617fa2f6ac819d8b61bfc。

沿着 ZeroMQ 的发送链继续往下追，会碰到一个看起来很普通的类型：**msg_t**。Pipe、Distributor、Encoder、Session 之间真正流动的不是裸字节数组，而是这个固定大小的消息对象。

这篇只追一个问题：**PUB 把一帧较大的相机消息扇出到 8 条 Pipe 时，libzmq 会不会把 payload 复制 8 份？**

答案取决于消息表示。小消息和大消息走两种不同的存储策略；真正值得学习的是它怎样让两种策略共享同一个 64 字节 envelope，并把 ownership 写进消息类型本身。

## 1. 为什么普通 vector 很快不够

最直觉的第一版可能是：

~~~cpp
struct Message {
    std::vector<std::byte> payload;
    bool more;
};
~~~

单消费者时很好理解。一旦进入 fan-out：

~~~text
                 -> pipe A
Message --------> pipe B
                 -> pipe C
                 -> pipe D
~~~

就必须回答：

- 每个 Pipe 得到完整 payload 副本吗？
- 如果共享底层 buffer，最后一个消费者是谁？
- 某一条 Pipe 写失败时，原本应持有的引用由谁撤销？
- 十几个字节的控制帧也值得一次 heap allocation 吗？
- 应用传入外部 buffer 时，最终由谁释放？
- 一个消息 move 以后，源对象如何避免二次释放？

这些不是附加优化，而是消息对象最核心的所有权协议。

## 2. 固定 64 字节外壳

源码把消息对象大小固定下来：

~~~cpp
enum
{
    msg_t_size = 64
};

enum
{
    max_vsm_size =
      msg_t_size - (sizeof (metadata_t *) + 3 + 16 + sizeof (uint32_t))
};
~~~

内部 union 可以容纳多种表示。主要的两类可以先画成：

~~~text
small message
+-----------------------------------------------+
| msg_t 64 B                                    |
| metadata | type | flags | size | inline data  |
+-----------------------------------------------+

large message
+---------------------------+
| msg_t 64 B                |
| metadata | ... | content* |----+
+---------------------------+    |
                                 v
                      +-----------------------+
                      | content_t             |
                      | data*                 |
                      | size                  |
                      | free function         |
                      | hint                  |
                      | atomic refcount       |
                      +-----------------------+
                                 |
                                 v
                              payload
~~~

足够小的数据直接塞进 msg_t，本身就是 small-message optimization；超过阈值后才把 payload 放到外部 storage。

## 3. content_t 同时描述数据与释放规则

大消息控制块：

~~~cpp
struct content_t
{
    void *data;
    size_t size;
    msg_free_fn *ffn;
    void *hint;
    zmq::atomic_counter_t refcnt;
};
~~~

这几个字段实际上组成三件事：

~~~text
payload view
+
deallocation policy
+
shared ownership state
~~~

data/size 描述 payload，refcnt 支持多个 msg_t 共享同一内容；ffn/hint 允许 payload 来自调用者自己的 allocator、对象池或其他外部存储，而不要求 libzmq 猜应该怎样释放。

## 4. 小消息为什么直接内嵌

init_size() 先判断长度：

~~~cpp
int zmq::msg_t::init_size (size_t size_)
{
    if (size_ <= max_vsm_size) {
        _u.vsm.metadata = NULL;
        _u.vsm.type = type_vsm;
        _u.vsm.flags = 0;
        _u.vsm.size = static_cast<unsigned char> (size_);
        _u.vsm.group.sgroup.group[0] = '\0';
        _u.vsm.group.type = group_type_short;
        _u.vsm.routing_id = 0;
    } else {
        _u.lmsg.metadata = NULL;
        _u.lmsg.type = type_lmsg;
        _u.lmsg.flags = 0;
        _u.lmsg.content = NULL;

        if (sizeof (content_t) + size_ > size_)
            _u.lmsg.content =
              static_cast<content_t *> (
                malloc (sizeof (content_t) + size_));

        if (unlikely (!_u.lmsg.content)) {
            errno = ENOMEM;
            return -1;
        }

        _u.lmsg.content->data = _u.lmsg.content + 1;
        _u.lmsg.content->size = size_;
        _u.lmsg.content->ffn = NULL;
        _u.lmsg.content->hint = NULL;
        new (&_u.lmsg.content->refcnt)
          zmq::atomic_counter_t ();
    }
    return 0;
}
~~~

大消息还有一个值得借鉴的细节：控制块和 payload 使用一次 malloc，payload 紧跟在 content_t 后面，而不是 control block 与 payload 各做一次分配。

这样既少一次 allocator 往返，也改善了控制块与数据头部的局部性。

## 5. init() 怎样选择存储策略

更高一层的初始化：

~~~cpp
int zmq::msg_t::init (
  void *data_,
  size_t size_,
  msg_free_fn *ffn_,
  void *hint_,
  content_t *content_)
{
    if (size_ <= max_vsm_size) {
        const int rc = init_size (size_);

        if (rc != -1) {
            memcpy (data (), data_, size_);
            return 0;
        }
        return -1;
    }

    if (content_)
        return init_external_storage (
          content_, data_, size_, ffn_, hint_);

    return init_data (data_, size_, ffn_, hint_);
}
~~~

决策树是：

~~~text
size <= inline threshold?
   |
   +-- yes -> copy into msg_t itself
   |
   +-- no
       |
       +-- caller supplied content_t?
       |      -> external-storage representation
       |
       +-- otherwise
              -> large/constant representation
~~~

因此“zero-copy”不能被理解成任何入口都绝不 memcpy。小消息会主动复制进 inline storage。优化目标是让大 payload 避免不必要的重复复制，而不是追求绝对化口号。

## 6. copy() 为什么不等于 memcpy

真正的 copy() 在复制 64 字节表示之前先处理共享资源：

~~~cpp
const atomic_counter_t::integer_t
  initial_shared_refcnt = 2;

if (src_.is_lmsg () || src_.is_zcmsg ()) {
    if (src_.flags () & msg_t::shared)
        src_.refcnt ()->add (1);
    else {
        src_.set_flags (msg_t::shared);
        src_.refcnt ()->set (
          initial_shared_refcnt);
    }
}

if (src_._u.base.metadata != NULL)
    src_._u.base.metadata->add_ref ();

if (src_._u.base.group.type == group_type_long)
    src_._u.base.group.lgroup.content
      ->refcnt.add (1);

*this = src_;
~~~

可以把它理解成：

~~~text
copy msg_t shell
        |
        +-- VSM inline bytes
        |      payload lives in shell
        |
        +-- LMSG / zero-copy
               shell only copies pointer
               shared payload refcount++
~~~

shared flag 还有一个性能意义：单引用大消息一开始不必承担共享引用计数语义，第一次真正发生共享时才把 refcount 初始化为 2。

## 7. move() 如何避免源对象二次释放

移动路径：

~~~cpp
int zmq::msg_t::move (msg_t &src_)
{
    if (unlikely (!src_.check ())) {
        errno = EFAULT;
        return -1;
    }

    int rc = close ();
    if (unlikely (rc < 0))
        return rc;

    *this = src_;

    rc = src_.init ();
    if (unlikely (rc < 0))
        return rc;

    return 0;
}
~~~

它的语义是：

~~~text
target closes old resource
        |
        v
representation transferred
        |
        v
source becomes a fresh empty valid msg_t
~~~

所以源对象随后再 close 也不会释放已经交给目标的 payload。

## 8. close()：最后一个引用才释放大 payload

大消息关闭路径：

~~~cpp
if (_u.base.type == type_lmsg) {
    if (!(_u.lmsg.flags & msg_t::shared)
        || !_u.lmsg.content->refcnt.sub (1)) {

        _u.lmsg.content->refcnt
          .~atomic_counter_t ();

        if (_u.lmsg.content->ffn)
            _u.lmsg.content->ffn (
              _u.lmsg.content->data,
              _u.lmsg.content->hint);

        free (_u.lmsg.content);
    }
}
~~~

如果没有共享，当前对象就是唯一 owner；如果已经共享，则原子减引用，只有最后一个引用离开才执行真正释放。

metadata 与 long group 又维护各自的引用计数，这说明“msg_t 复制”其实是多个子资源生命周期的组合，而不仅是 payload 指针。

## 9. add_refs / rm_refs 为 fan-out 做批量引用管理

源码专门提供：

~~~cpp
void zmq::msg_t::add_refs (int refs_)
{
    zmq_assert (refs_ >= 0);
    zmq_assert (_u.base.metadata == NULL);

    if (!refs_)
        return;

    if (_u.base.type == type_lmsg
        || is_zcmsg ()) {
        if (_u.base.flags & msg_t::shared)
            refcnt ()->add (refs_);
        else {
            refcnt ()->set (refs_ + 1);
            _u.base.flags |= msg_t::shared;
        }
    }
}
~~~

VSM、constant message 和 delimiter 可以按固定表示复制；真正需要共享 payload 计数的是 long / zero-copy message。

这避免 fan-out 每发一条 Pipe 都重复执行完整 copy() 的通用逻辑。

## 10. dist_t 真正怎样扇出

没有匹配 Pipe 时直接释放：

这里讨论的是 payload ownership；而 `dist_t` 为什么能用 matching / active / eligible 三层前缀在 O(1) membership transition 下维护当前订阅集合、背压和 multipart generation，见 [FQ / LB / DIST：Active Prefix、Multipart 原子性与消息调度器](fq-lb-dist-schedulers.md)。

~~~cpp
if (_matching == 0) {
    int rc = msg_->close ();
    errno_assert (rc == 0);
    rc = msg_->init ();
    errno_assert (rc == 0);
    return;
}
~~~

VSM 走 inline copy：

~~~cpp
if (msg_->is_vsm ()) {
    for (pipes_t::size_type i = 0;
         i < _matching;) {
        if (!write (_pipes[i], msg_)) {
        } else {
            ++i;
        }
    }

    int rc = msg_->init ();
    errno_assert (rc == 0);
    return;
}
~~~

非 VSM 先一次性增加引用：

~~~cpp
msg_->add_refs (
  static_cast<int> (_matching) - 1);

int failed = 0;

for (pipes_t::size_type i = 0;
     i < _matching;) {
    if (!write (_pipes[i], msg_)) {
        ++failed;
    } else {
        ++i;
    }
}

if (unlikely (failed))
    msg_->rm_refs (failed);

const int rc = msg_->init ();
errno_assert (rc == 0);
~~~

假设有 4 个匹配订阅者：

~~~text
before fan-out
    payload ref = 1

add_refs(4 - 1)
    payload ref = 4

pipe A msg shell ----+
pipe B msg shell ----+----> one shared payload
pipe C msg shell ----+
pipe D msg shell ----+

one pipe write fails?
    rm_refs(1)
~~~

所以结论应精确表述为：

**大消息扇出不会为每个成功 Pipe 再复制一份 payload；各 Pipe 获得自己的消息表示，而 long/zero-copy payload 通过引用计数共享。小 VSM 的 payload 本就在 64 字节消息对象内部，因此复制表示时也会复制这些 inline bytes。**

## 11. 为什么不统一用 shared_ptr<vector<byte>>

统一智能指针当然更容易写，但它会把同一套成本强加给所有消息：

~~~text
tiny control frame
   -> heap allocation
   -> vector object
   -> shared ownership control
   -> atomic refcount traffic
~~~

libzmq 则按数据特点分流：

~~~text
tiny payload
   -> inline, no payload allocation

large owned payload
   -> one allocation: content + bytes

external payload
   -> pointer + custom free policy

fan-out large payload
   -> shared flag + atomic refcount
~~~

它不是“永远不用堆”，而是只在需要时付出 heap 与 shared ownership 成本。

## 12. 对机器人数据链的启发

机器人系统常同时存在两种 traffic：

~~~text
控制 / 状态
  tens of bytes
  high rate
  latency-sensitive

图像 / 点云 / tensor
  KB ~ MB
  bandwidth-sensitive
~~~

用一种 storage policy 处理两者通常会吃亏。

可以把 msg_t 的思想抽象成：

~~~text
MessageEnvelope
  timestamp
  type
  flags
  routing metadata
  storage variant
     |- Inline<N>
     |- HeapBlock
     |- SharedBlock
     |- BorrowedExternal
     |- DeviceBufferHandle
~~~

先固定 envelope，再让 payload storage policy 多态，比业务层到处传播裸 void* 更容易维护，也比让所有小控制帧都进入通用 allocator 更稳定。

## 13. 它与 Pipe/HWM 的接口边界

Pipe 的一个重要契约是：write() 失败时，调用者仍保留 message buffer ownership。

因为 msg_t 已经明确了 ownership，scheduler 才可以安全地：

~~~text
try pipe A
  full -> still own message

try pipe B
  succeeds -> representation transferred/copied

failed fan-out branch
  repair reference count
~~~

容量控制因此只需要关注“能不能接收”，不必顺便猜 payload 应不应该析构。

接着读 [Pipe 与 HWM](pipe-hwm-backpressure.md)，再沿 [Session 与 Stream Engine](session-stream-engine.md) 看同一个 msg_t 怎样进入网络 framing。
