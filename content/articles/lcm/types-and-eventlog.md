# 类型指纹与事件日志：把机器人消息变成可验证、可回放的字节
前面的篇章已经能把机械臂关节状态通过 `publish("ARM_STATE", &state)` 发送到另一进程。第一次用起来时，你可能不会关心这条消息由几字节组成：定位端发布，控制端订阅，日志进程记录。一切都正常，直到几个月后，你给关节状态增加了一个字段，拿新程序回放旧实验，接收端突然不能解码。

这才出现两个原本被 API 隐藏的问题：**消息格式究竟由谁规定？日志又怎样保证明天还能读出今天的含义？** 如果我们把两者混成一件事，就很容易产生“既然录像完整，按当前 C++ 结构体直接转换不就行了吗”的错觉。

先不要急着研究 hash 算法。我们先从一条真正被发送的消息开始，把它在发送前、网络中和解码后的三个形态摆在一起：

~~~text
发布进程内                   网络 / 日志                      接收进程内
channel_to_port_t             wire bytes                   channel_to_port_t

channel = "arm"   ────────►  schema fingerprint ────────►  检查类型
port    = 7667                string length = 4              |
                              'a' 'r' 'm' '\0'               字段解码
                              port: 2 bytes                  |
                                                            v
                                                    新分配的 channel 字符串
                                                    port = 7667
~~~

这张图最关键的并不是字段顺序，而是**对象表示与通信表示彻底分开**。原来的 `channel` 是一个本进程的 `char*`，接收端不能复用它；发送时必须写出真正的字符串字节，接收时再分配自己的内存。文件只是保存中间这列 wire bytes，既不知道原进程的指针地址，也不应该依赖它。

## 第一次追问：为什么不把 C++ 结构体直接写进日志？

假设第一版记录器只是：

~~~cpp
// 错误的教学思路：把内存镜像当持久化协议。
file.write(reinterpret_cast<const char*>(&state), sizeof(state));
~~~

一开始，当 `state` 只包含几个浮点数、写入和读取都来自同一份程序时，它可能看起来“确实能用”。但有两个失败不需要换平台就能发生。

第一，加入 `std::string` 或 `std::vector` 后，记录器保存的是容器对象内部的指针和长度等本机状态，而不是它们指向的数据。第二，修改字段顺序或增加字段后，同样的字节偏移可以被解释为完全不同的业务量。最危险的是读出的数值仍可能落在正常范围内，错误不一定表现为崩溃。

所以我们真正需要的是一个与编译器内存布局无关的**字段级协议**。对于 `channel_to_port_t`，固定版本生成器写出的格式有精确的长度关系：

~~~text
偏移             字段                         字节数
[0, 8)           schema fingerprint          8
[8, 12)          "arm" 的长度（包含 \0）       4
[12, 16)         'a' 'r' 'm' '\0'            4
[16, 18)         port                        2

总长度：8 + 4 + 4 + 2 = 18 字节
~~~

注意这里的 18 字节属于 `channel="arm", port=7667` 这条**具体消息**，不属于 `sizeof(channel_to_port_t)`。换成 `channel="left_camera"`，编码长度就变了，而本地结构体 `sizeof` 完全可以不变。

对应的源码恰好给出三个相互独立的函数：`channel_to_port_t_encoded_size` 算出这次需要的容量；`channel_to_port_t_encode` 先写 8 字节 fingerprint，再调用 `__channel_to_port_t_encode_array`；后者先编码字符串，再编码端口。这些函数的拆分不是为了让 API 看起来丰富，而是为了让**申请容量、生成字节、恢复对象**能各自单独检查错误。

下面进入本地固定提交 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864` 的真实生成代码。先观察字段和长度，再讨论 fingerprint 为什么能够在新旧版本不兼容时提前拒绝解码。
## 从字段结构到确定的字节顺序

先看固定提交中一个真实生成类型。它只有字符串 channel 和 16 位端口号，结构体里的 C 指针布局不是网络格式：32 位与 64 位进程的指针宽度不同，结构体对齐也可能不同，因而不能拿 sizeof(struct) 当消息长度直接发送。



~~~c
typedef struct _channel_to_port_t channel_to_port_t;
struct _channel_to_port_t
{

    /**
     * LCM Type: string
     */
    char*      channel;
    int16_t    port;
};
~~~

结构体是接收程序使用的本机对象；生成的 encode/decode 函数负责把字段逐项转换成跨进程 bytes。字符串要存其长度及内容，整数要采用协议定义的字节序。接收端因此不依赖编译器如何摆放 char*，也无需在运行时加载 schema 文件。

类型指纹是 wire 消息开头的 64 位 schema 标识。它不是整条消息内容的 checksum，也不是身份认证 MAC；它回答的是“这个 decoder 所期待的字段结构，是否与这段 bytes 声称的类型相同”。生成代码首先计算并缓存一个基于 schema 的值：



~~~c
LCM_NO_EXPORT
uint64_t __channel_to_port_t_hash_recursive(const __lcm_hash_ptr *p)
{
    const __lcm_hash_ptr *fp;
    for (fp = p; fp != NULL; fp = fp->parent)
        if (fp->v == __channel_to_port_t_get_hash)
            return 0;

    __lcm_hash_ptr cp;
    cp.parent =  p;
    cp.v = __channel_to_port_t_get_hash;
    (void) cp;

    uint64_t hash = (uint64_t)0x11dde9fa42a43913LL
         + __string_hash_recursive(&cp)
         + __int16_t_hash_recursive(&cp)
        ;

    return (hash<<1) + ((hash>>63)&1);
}

LCM_NO_EXPORT
int64_t __channel_to_port_t_get_hash(void)
{
    if (!__channel_to_port_t_hash_computed) {
        __channel_to_port_t_hash = (int64_t)__channel_to_port_t_hash_recursive(NULL);
        __channel_to_port_t_hash_computed = 1;
    }

    return __channel_to_port_t_hash;
}
~~~

这个类型的静态基值与 string、int16_t 组件的 hash 合成最终 fingerprint。递归访问链用于避免嵌套类型的哈希遍历无限绕回。实际发出的值由生成文件固定；运行时不会把源 schema 字符串发到网络上。

接着，生成 encoder 先把 hash 作为一个 64 位字段编码，再依次编码真实字段。每个字段编码器接收剩余容量，并在失败时向上传回负值：



~~~c
LCM_NO_EXPORT
int channel_to_port_t_encode(void *buf, int offset, int maxlen, const channel_to_port_t *p)
{
    int pos = 0, thislen;
    int64_t hash = __channel_to_port_t_get_hash();

    thislen = __int64_t_encode_array(buf, offset + pos, maxlen - pos, &hash, 1);
    if (thislen < 0) return thislen; else pos += thislen;

    thislen = __channel_to_port_t_encode_array(buf, offset + pos, maxlen - pos, p, 1);
    if (thislen < 0) return thislen; else pos += thislen;

    return pos;
}
~~~

decode 先读出 fingerprint 并比较，再解码字段。新旧类型不匹配时它返回失败，而不是猜测“相似字段”或把旧字节塞进新 struct：

现在看 `channel_to_port_t_decode()` 怎样先比较 schema fingerprint，再决定是否解码字段：


~~~c
LCM_NO_EXPORT
int channel_to_port_t_decode(const void *buf, int offset, int maxlen, channel_to_port_t *p)
{
    int pos = 0, thislen;
    int64_t hash = __channel_to_port_t_get_hash();

    int64_t this_hash;
    thislen = __int64_t_decode_array(buf, offset + pos, maxlen - pos, &this_hash, 1);
    if (thislen < 0) return thislen; else pos += thislen;
    if (this_hash != hash) return -1;

    thislen = __channel_to_port_t_decode_array(buf, offset + pos, maxlen - pos, p, 1);
    if (thislen < 0) return thislen; else pos += thislen;

    return pos;
}

LCM_NO_EXPORT
int channel_to_port_t_decode_cleanup(channel_to_port_t *p)
{
    return __channel_to_port_t_decode_array_cleanup(p, 1);
}
~~~

字符串字段是动态内存。底层 string decoder 为它分配独立缓冲，生成的 cleanup 最终调用 free；因此调用者拿到的是有所有权的解码对象，而不是指向网络接收 buffer 的字符串视图。下面是实际通用 string decoder 的分配语句与释放函数：



~~~c
static inline int __string_decode_array(const void *_buf, int offset, int maxlen, char **p,
                                        int elements)
{
    int pos = 0, thislen;
    int element;

    for (element = 0; element < elements; element++) {
        int32_t length;

        // read length including \0
        thislen = __int32_t_decode_array(_buf, offset + pos, maxlen - pos, &length, 1);
        if (thislen < 0)
            return thislen;
        else
            pos += thislen;

        p[element] = (char *) malloc(length);
        thislen =
            __int8_t_decode_array(_buf, offset + pos, maxlen - pos, (int8_t *) p[element], length);
        if (thislen < 0)
            return thislen;
        else
            pos += thislen;
    }

    return pos;
}

static inline int __string_decode_array_cleanup(char **s, int elements)
{
    int element;
    for (element = 0; element < elements; element++)
        free(s[element]);
    return 0;
}
~~~

完成处理后必须调用对应 cleanup。若 callback 把 p->channel 指针放进长期对象后立刻 cleanup，长期对象会悬空；若从不 cleanup，频繁解码会不断泄漏字符串分配。这里的 malloc 失败也没有单独检查，面向不可信数据的复刻 decoder 应检查长度合法性和分配结果，并让失败路径清理此前已经成功构造的字段。

编码容量同样重要。生成的 encoded_size 先估算需要的字节数，调用者按该值分配 buffer 后再 encode。这个接口适合把 wire 长度安排在单块连续区域中，但长度总和自身仍受 int 范围约束；自定义生成器不能假设各字段大小相加永远不溢出。

对于控制消息，严格拒绝 fingerprint 不匹配通常比“尽力兼容”更安全：异常显式报错，代价是升级时要协调两侧类型。需要渐进升级时，可以新建 pose_v2_t、新旧 channel 并行一段时间，或设置一个显式 version 字段并实现逐版本转换；不能只给 struct 末尾加字段便期待旧 decoder 自动忽略它。

## C++ 发布时临时 bytes 的寿命

固定提交的 C++ 模板发布路径把 typed message 编码成临时 byte array，调用底层 publish 后立即释放：



~~~cpp
template <class MessageType>
inline int LCM::publish(const std::string &channel, const MessageType *msg)
{
    unsigned int datalen = msg->getEncodedSize();
    uint8_t *buf = new uint8_t[datalen];
    msg->encode(buf, 0, datalen);
    int status = this->publish(channel, buf, datalen);
    delete[] buf;
    return status;
}
~~~

这里的模板类型在编译期决定 encoder；底层 provider 收到的仍是 bytes、channel 和长度。临时数组归发布调用栈拥有，publish 返回后释放，所以 provider 必须在调用期间消费或复制数据，不能异步保存 buf 指针。现有写法每次发布都会分配并释放一块数组。RAII 是让对象析构函数自动释放它拥有的资源：std::vector<uint8_t> 可以在函数返回时自动释放 bytes，少掉手写 delete[]；但复用同一 scratch buffer 时，两个发布线程仍不能同时改写它。


~~~cpp
std::vector<std::uint8_t> buffer(msg->getEncodedSize());
msg->encode(buffer.data(), 0, static_cast<unsigned int>(buffer.size()));
return this->publish(channel, buffer.data(), static_cast<unsigned int>(buffer.size()));
~~~

这里每次调用仍会分配 vector 的存储，只是析构自动归还；若实测分配成本太高，可以让每个发布线程单独持有 scratch，或从受锁保护的池借用，不能让多线程共享一块可变数组。

## event log 保存的是 wire 数据，不是 C++ 对象

event log 为每条事件存放同步 magic、递增 event number、时间戳、channel 长度、payload 长度、channel bytes 与原始 payload。它不解码消息，也不需要知道 channel 对应哪个 schema。因此记录未知类型仍可成功；回放时，应用必须加载兼容的 decoder。

事件结构明确把两个长度定义成有符号 32 位：



~~~c
struct _lcm_eventlog_event_t {
    /**
     * A monotonically increasing number assigned to the message to identify it
     * in the log file.
     */
    int64_t eventnum;
    /**
     * Time that the message was received, in microseconds since the UNIX
     * epoch
     */
    int64_t timestamp;
    /**
     * Length of @c channel, in bytes
     */
    int32_t channellen;
    /**
     * Length of @c data, in bytes
     */
    int32_t datalen;

    /**
     * Channel that the message was received on
     */
    char *channel;
    /**
     * Raw byte buffer containing the message payload.
     */
    void *data;
};
~~~

writer 按固定顺序写字段，然后原样写 channel 与 payload：



~~~c
int lcm_eventlog_write_event(lcm_eventlog_t *l, lcm_eventlog_event_t *le)
{
    if (0 != fwrite32(l->f, MAGIC))
        return -1;

    le->eventnum = l->eventcount;

    if (0 != fwrite64(l->f, le->eventnum))
        return -1;
    if (0 != fwrite64(l->f, le->timestamp))
        return -1;
    if (0 != fwrite32(l->f, le->channellen))
        return -1;
    if (0 != fwrite32(l->f, le->datalen))
        return -1;

    if (le->channellen != (int32_t) fwrite(le->channel, 1, le->channellen, l->f))
        return -1;
    if (le->datalen != (int32_t) fwrite(le->data, 1, le->datalen, l->f))
        return -1;

    l->eventcount++;

    return 0;
}
~~~

eventnum 维护文件顺序；timestamp 记录事件时间，两者用途不同。即使两条事件具有相同时间戳，文件顺序仍能确定回放先后。eventlog 本身对 data 不做 decode/re-encode，所以日志忠实保留生成器输出的 fingerprint 与 payload bytes。文件 provider 组装待写 event 时从系统实时钟取得时间，并把 channel 与 payload 复制进一块连续分配：

继续看文件 provider 的 `lcm_logprov_publish()` 怎样复制消息并写入日志：


~~~c
static int lcm_logprov_publish(lcm_logprov_t *lcm, const char *channel, const void *data,
                               unsigned int datalen)
{
    if (lcm->log_mode == LCM_LOGPROV_READ_MODE) {
        dbg(DBG_LCM, "Called publish(), but lcm file provider is in read mode\n");
        return -1;
    }
    int channellen = strlen(channel);

    int64_t mem_sz = sizeof(lcm_eventlog_event_t) + channellen + 1 + datalen;

    lcm_eventlog_event_t *le = (lcm_eventlog_event_t *) malloc(mem_sz);
    memset(le, 0, mem_sz);

    le->timestamp = (int64_t) g_get_real_time();
    ;
    le->channellen = channellen;
    le->datalen = datalen;
    // log_write_event will handle le.eventnum.

    le->channel = ((char *) le) + sizeof(lcm_eventlog_event_t);
    strcpy(le->channel, channel);
    le->data = le->channel + channellen + 1;
    assert((char *) le->data + datalen == (char *) le + mem_sz);
    memcpy(le->data, data, datalen);

    lcm_eventlog_write_event(lcm->log, le);
    free(le);

    return 0;
}
~~~

因此日志 timestamp 是 provider 记录发布时读到的 realtime，不自动等于传感器采样时间；实际控制算法的 measurement timestamp 仍应保存在消息字段中。一个分配中相邻放置 event、channel 与 payload，避免这条同步写路径另行分配两个小块；它们只在 lcm_logprov_publish() 中被 eventlog writer 消费，写完一起 free。

这段代码还留下一个可复现的失败语义：它没有检查 lcm_eventlog_write_event() 的返回值。磁盘满或 fwrite 短写时，底层可能返回 -1，但 lcm_logprov_publish() 仍释放 event 并返回 0，发布者会以为记录成功。可靠的复刻应传播这个错误，并把失败事件数和磁盘状态暴露给实验记录流程；对闭环控制，还要明确记录失败是否允许继续执行。

## 读日志时要把长度字段当成不可信输入

文件不是天然可信的。日志可能因断电截断、磁盘损坏或人工修改而带有错误长度。固定读取函数检查 channel 长度在 1 到 999 之间，并拒绝负的 data 长度；但它没有给非负 data 长度设置应用级上限，也没有在分配前确认剩余文件足够长。



~~~c
lcm_eventlog_event_t *lcm_eventlog_read_next_event(lcm_eventlog_t *l)
{
    lcm_eventlog_event_t *le = (lcm_eventlog_event_t *) calloc(1, sizeof(lcm_eventlog_event_t));

    uint32_t magic = 0;
    int r;

    do {
        r = fgetc(l->f);
        if (r < 0) {
            free(le);
            return NULL;
        }
        magic = (magic << 8) | (uint32_t) r;
    } while (magic != MAGIC);

    if (0 != fread64(l->f, &le->eventnum) || 0 != fread64(l->f, &le->timestamp) ||
        0 != fread32(l->f, &le->channellen) || 0 != fread32(l->f, &le->datalen)) {
        free(le);
        return NULL;
    }

    // Sanity check the channel length and data length
    if (le->channellen <= 0 || le->channellen >= 1000) {
        fprintf(stderr, "Log event has invalid channel length: %d\n", le->channellen);
        free(le);
        return NULL;
    }
    if (le->datalen < 0) {
        fprintf(stderr, "Log event has invalid data length: %d\n", le->datalen);
        free(le);
        return NULL;
    }

    le->channel = (char *) calloc(1, le->channellen + 1);
    if (fread(le->channel, 1, le->channellen, l->f) != (size_t) le->channellen) {
        free(le->channel);
        free(le->data);
        free(le);
        return NULL;
    }

    le->data = calloc(1, le->datalen + 1);
    if (fread(le->data, 1, le->datalen, l->f) != (size_t) le->datalen) {
        free(le->channel);
        free(le->data);
        free(le);
        return NULL;
    }

    // Check that there's a valid event or the EOF after this event.
    int32_t next_magic;
    if (0 == fread32(l->f, &next_magic)) {
        if (next_magic != MAGIC) {
            fprintf(stderr, "Invalid header after log data\n");
            free(le->channel);
            free(le->data);
            free(le);
            return NULL;
        }
        fseeko(l->f, -4, SEEK_CUR);
    }
    return le;
}
~~~

这里的 datalen 不是任意大的无符号整数，而是 int32_t。读入后负值会被拒绝，因此可通过检查的协议范围是 0 到 2,147,483,647 字节，也就是不到 2 GiB，而不是任意多个 GiB。仍然很大的伪造长度会触发接近 2 GiB 的分配请求。更细的一处边界是 calloc(1, le->datalen + 1)：在常见的 int32_t 等同 32 位 int 的构建上，加法先以有符号 32 位完成；若 datalen 正好是 INT32_MAX，+1 会发生有符号溢出，转换成 calloc 的 size_t 之前就已经出了问题。这个结论依赖常见 ABI 的 int32_t typedef，不能误写成“所有平台对任意大正数的行为都相同”。

代码也没有检查 calloc 返回 NULL，就继续把指针交给 fread；对于确实失败的超大分配，这条路径可能在文件读取时崩溃。文件只剩几百字节但 header 声称有巨型 payload 时，程序也是先尝试分配，再由 fread 的短读分支发现截断。防御性 reader 应先将有符号长度验证为非负、检查显式 payload 上限与 size_t/int 算术，再检查当前文件剩余字节，最后分配；每一步失败都应返回可诊断错误，而不是只返回 EOF。

## 每个 event 的所有权由调用者接手

读取函数为 event、channel 和 data 分别分配内存；成功返回后，调用者负责调用配对的 free 函数。它清理三个分配并释放 event 本身：



~~~c
void lcm_eventlog_free_event(lcm_eventlog_event_t *le)
{
    if (le->data)
        free(le->data);
    if (le->channel)
        free(le->channel);
    memset(le, 0, sizeof(lcm_eventlog_event_t));
    free(le);
}
~~~

使用方式是逐条读取、处理、释放。若算法要把消息交给稍后运行的工作线程，工作项必须复制所需字段或接管完整 event 对象；保存 event->data 裸指针然后马上 free_event，会造成悬空访问。


~~~c
for (;;) {
    lcm_eventlog_event_t *event = lcm_eventlog_read_next_event(log);
    if (!event)
        break;

    process_bytes(event->channel, event->data, event->datalen);
    lcm_eventlog_free_event(event);
}
~~~

file provider 也遵守这个寿命：handle 线程先用当前 event 的 data 同步 dispatch callback，等 callback 返回后才 load_next_event；后者首先释放旧 event，再读下一条。故 callback 内 bytes 可同步读取，callback 返回后不得保存旧 data 指针。

先看 `load_next_event()` 怎样释放上一条事件并取得下一条：


~~~c
static int load_next_event(lcm_logprov_t *lr)
{
    if (lr->event)
        lcm_eventlog_free_event(lr->event);

    lr->event = lcm_eventlog_read_next_event(lr->log);
    if (!lr->event)
        return -1;

    return 0;
}
~~~

接着看 `lcm_logprov_handle()` 怎样安排当前事件和后续回放时间：


~~~c
static int lcm_logprov_handle(lcm_logprov_t *lr)
{
    lcm_recv_buf_t rbuf;

    if (!lr->event)
        return -1;

    char ch;
    int status = lcm_internal_pipe_read(lr->notify_pipe[0], &ch, 1);
    if (status == 0) {
        fprintf(stderr, "Error: lcm_handle read 0 bytes from notify_pipe\n");
        return -1;
    } else if (status < 0) {
        fprintf(stderr, "Error: lcm_handle read: %s\n", strerror(errno));
        return -1;
    }

    int64_t now = g_get_real_time();
    /* Initialize the wall clock if this is the first time through */
    if (lr->next_clock_time < 0)
        lr->next_clock_time = now;

    //    rbuf.channel = lr->event->channel,
    rbuf.data = (uint8_t *) lr->event->data;
    rbuf.data_size = lr->event->datalen;
    rbuf.recv_utime = lr->next_clock_time;
    rbuf.lcm = lr->lcm;

    if (lcm_try_enqueue_message(lr->lcm, lr->event->channel))
        lcm_dispatch_handlers(lr->lcm, &rbuf, lr->event->channel);

    int64_t prev_log_time = lr->event->timestamp;
    if (load_next_event(lr) < 0) {
        /* end-of-file reached.  This call succeeds, but next call to
         * _handle will fail */
        lr->event = NULL;
        if (lcm_internal_pipe_write(lr->notify_pipe[1], "+", 1) < 0) {
            perror(__FILE__ " - write(notify)");
        }
        return 0;
    }

    /* Compute the wall time for the next event */
    if (lr->speed > 0)
        lr->next_clock_time += (lr->event->timestamp - prev_log_time) / lr->speed;
    else
        lr->next_clock_time = now;

    if (lr->next_clock_time > now) {
        int wstatus = lcm_internal_pipe_write(lr->timer_pipe[1], &lr->next_clock_time, 8);
        if (wstatus < 0) {
            perror(__FILE__ " - write(timer_pipe)");
        }
    } else {
        int wstatus = lcm_internal_pipe_write(lr->notify_pipe[1], "+", 1);
        if (wstatus < 0) {
            perror(__FILE__ " - write(notify_pipe)");
        }
    }

    return 0;
}
~~~

handle 先从 notify pipe 读取一个字节；该 pipe 可读只表示当前 event 到了处理时机。它以当前 event 的 data 构造 rbuf，业务 handler 在 dispatch 中同步执行。handler 返回后，load_next_event() 才释放旧 event 并读下一条。若下一条事件的目标墙上时刻还没到，provider 把该时刻写入 timer pipe；timer 工作路径到点后再通知 handle。这里与网络接收路径共享同一种借用边界：rbuf.data 指向当前 event 所拥有的 bytes，换成异步 handler 时，必须改变所有权协议，不能直接复用这个指针。速度大于零时，事件间隔按 log timestamp 差值除以 speed 累加；速度不大于零时，下一条被安排为当前 realtime。由于代码用 g_get_real_time()，系统校时可能让相对等待变早或变晚；稳定回放的复刻可以用 monotonic clock 等待，同时把原始 event timestamp 作为消息时间保留下来。

若 callback 执行时间超过日志中的事件间隔，下一条 event 的目标墙上时刻可能在 callback 尚未返回时就已过去；timer pipe 随后很快再次通知，handle 会紧接着处理下一条。固定路径没有因业务变慢而丢弃旧 event 或限制追赶速度，因而回放时可能出现连续 callback、数据年龄不断增长的现象。对闭环控制来说，回放“所有消息”可能比按 deadline 丢旧样本更不真实。

## 回放时间不是自动等于传感器采样时间

日志事件的 timestamp 文档语义是消息收到时的 UNIX epoch 微秒；文件 provider 另用 g_get_real_time() 计算本次回放的 wall-clock 目标。两者都不等于传感器测量时刻。相机图像可能在曝光结束时采样，经过驱动和网络后才被 logger 记录。要做传感器融合，应同时保留 measurement timestamp 与记录/接收时间，并注明时钟域。


## 逐步复刻：先有稳定 wire，再做坏文件恢复

最小的教学实现可以先定义一个小消息和固定字段 wire 编码，再由日志层写 event header、channel 和 bytes：


~~~cpp
struct Pose {
    std::int64_t measurement_time_us;
    double x;
    double y;
    double yaw;
};

struct Event {
    std::uint64_t sequence;
    std::int64_t record_time_us;
    std::string channel;
    std::vector<std::byte> payload;
};
~~~

实现顺序应让每一步都能观察：先为固定字段定义字节序和 fingerprint，并用两种语言的 golden bytes 对照；再做单条事件写入/读取与配对释放；随后注入负长度、接近 INT32_MAX 的长度、被截断的 channel/payload 和错误 magic，确认 reader 会在分配前拒绝；最后增加 event 时间映射、seek 索引和回放速度。同步显示日志 eventnum、记录时间与消息内 measurement timestamp，才能分辨文件顺序、网络到达和传感器采样之间的差异。

这套拆分带来的工程取舍很清楚：生成代码让类型边界显式、跨语言字节布局稳定；严格 fingerprint 让不兼容升级尽早失败；event log 不依赖 decoder，因而能够保留任意 channel 的原始消息。但生成类型不是 schema 协商，日志长度字段也不是安全容量上限，回放时间更不是机器人传感器的真实采样时刻。
