# UDPM 发送协议：LC02、LC03 与 scatter-gather I/O

假设移动机器人每 2 ms 发布一次 64 字节关节状态，另一个线程偶尔发布 900 KiB 点云。把两类数据直接拼进一个大 `struct` 再做一次 UDP `sendto()`，大数据报会超过常见链路 MTU，依赖 IP 层二次分片；任何一片丢失，整条 UDP 数据报都不可交付。LCM 的 UDPM provider 因此把短消息保持为一个 datagram，把较长 payload 切成带序号和偏移的应用层片段。这个协议能接收大 payload，却不提供大消息可靠交付。

LCM 的默认 URL 使用 UDP multicast：

```text
udpm://239.255.76.67:7667?ttl=0
```

`lcm_publish()` 到达 UDPM provider 后，根据 channel 加 payload 的长度选择两种线格式：短消息使用单个 LC02 数据报，大消息使用多枚 LC03 分片。发送代码集中在 `lcm_udpm_publish()`。

下面沿 LCM 的固定提交 `ad0c54cee0ec048ef12357c34349ec1443158864` 回放发送线程的完整分支：从长度检查与序号分配开始，一直追到 scatter-gather 系统调用以及错误返回，而不是把“支持分片”当成已经证明可靠交付。

## 发送函数的输入边界

Provider 接收四个参数：


```c
static int lcm_udpm_publish(lcm_udpm_t *lcm,
                            const char *channel,
                            const void *data,
                            unsigned int datalen);
```

`data` 已经是生成式编码器产生的字节序列。UDPM 不理解消息字段，只负责把 channel 和 bytes 封装到协议中。
下面先看固定提交中从输入到两个线格式分支的完整控制流。输入 `data` 与 `channel` 均由调用方提供；函数只在同步调用期间借用 payload。


```c
static int lcm_udpm_publish(lcm_udpm_t *lcm, const char *channel, const void *data,
                            unsigned int datalen)
{
    int channel_size = strlen(channel);
    if (channel_size > LCM_MAX_CHANNEL_NAME_LENGTH) {
        fprintf(stderr, "LCM Error: channel name too long [%s]\n", channel);
        return -1;
    }

    int payload_size = channel_size + 1 + datalen;
    if (payload_size <= LCM_SHORT_MESSAGE_MAX_SIZE) {
        // message is short.  send in a single packet

        g_mutex_lock(&lcm->transmit_lock);

        lcm2_header_short_t hdr;
        hdr.magic = htonl(LCM2_MAGIC_SHORT);
        hdr.msg_seqno = htonl(lcm->msg_seqno);

        struct iovec sendbufs[3];
        sendbufs[0].iov_base = (char *) &hdr;
        sendbufs[0].iov_len = sizeof(hdr);
        sendbufs[1].iov_base = (char *) channel;
        sendbufs[1].iov_len = channel_size + 1;
        sendbufs[2].iov_base = (char *) data;
        sendbufs[2].iov_len = datalen;

        // transmit
        int packet_size = datalen + sizeof(hdr) + channel_size + 1;
        dbg(DBG_LCM_MSG, "transmitting %d byte [%s] payload (%d byte pkt)\n", datalen, channel,
            packet_size);

        //        int status = writev (lcm->sendfd, sendbufs, 3);
        struct msghdr msg;
        msg.msg_name = (struct sockaddr *) &lcm->dest_addr;
        msg.msg_namelen = sizeof(lcm->dest_addr);
        msg.msg_iov = sendbufs;
        msg.msg_iovlen = 3;
        msg.msg_control = NULL;
        msg.msg_controllen = 0;
        msg.msg_flags = 0;
        int status = sendmsg(lcm->sendfd, &msg, 0);

        lcm->msg_seqno++;
        g_mutex_unlock(&lcm->transmit_lock);

        if (status == packet_size)
            return 0;
        else
            return status;
    } else {
        // message is large.  fragment into multiple packets

        int fragment_size = LCM_FRAGMENT_MAX_PAYLOAD;
        int nfragments = payload_size / fragment_size + !!(payload_size % fragment_size);

        if (nfragments > 65535) {
            fprintf(stderr, "LCM error: too much data for a single message\n");
            return -1;
        }

        // acquire transmit lock so that all fragments are transmitted
        // together, and so that no other message uses the same sequence number
        // (at least until the sequence # rolls over)
        g_mutex_lock(&lcm->transmit_lock);
        dbg(DBG_LCM_MSG, "transmitting %d byte [%s] payload in %d fragments\n", payload_size,
            channel, nfragments);

        uint32_t fragment_offset = 0;

        lcm2_header_long_t hdr;
        hdr.magic = htonl(LCM2_MAGIC_LONG);
        hdr.msg_seqno = htonl(lcm->msg_seqno);
        hdr.msg_size = htonl(datalen);
        hdr.fragment_offset = 0;
        hdr.fragment_no = 0;
        hdr.fragments_in_msg = htons(nfragments);

        // first fragment is special.  insert channel before data
        int firstfrag_datasize = fragment_size - (channel_size + 1);
        assert(firstfrag_datasize <= datalen);

        struct iovec first_sendbufs[3];
        first_sendbufs[0].iov_base = (char *) &hdr;
        first_sendbufs[0].iov_len = sizeof(hdr);
        first_sendbufs[1].iov_base = (char *) channel;
        first_sendbufs[1].iov_len = channel_size + 1;
        first_sendbufs[2].iov_base = (char *) data;
        first_sendbufs[2].iov_len = firstfrag_datasize;

        int packet_size = sizeof(hdr) + channel_size + 1 + firstfrag_datasize;
        fragment_offset += firstfrag_datasize;
        //        int status = writev (lcm->sendfd, first_sendbufs, 3);
        struct msghdr msg;
        msg.msg_name = (struct sockaddr *) &lcm->dest_addr;
        msg.msg_namelen = sizeof(lcm->dest_addr);
        msg.msg_iov = first_sendbufs;
        msg.msg_iovlen = 3;
        msg.msg_control = NULL;
        msg.msg_controllen = 0;
        msg.msg_flags = 0;
        int status = sendmsg(lcm->sendfd, &msg, 0);

        // transmit the rest of the fragments
        for (uint16_t frag_no = 1; packet_size == status && frag_no < nfragments; frag_no++) {
            hdr.fragment_offset = htonl(fragment_offset);
            hdr.fragment_no = htons(frag_no);

            int fraglen = MIN(fragment_size, datalen - fragment_offset);

            struct iovec sendbufs[2];
            sendbufs[0].iov_base = (char *) &hdr;
            sendbufs[0].iov_len = sizeof(hdr);
            sendbufs[1].iov_base = (char *) ((char *) data + fragment_offset);
            sendbufs[1].iov_len = fraglen;

            //            status = writev (lcm->sendfd, sendbufs, 2);
            msg.msg_iov = sendbufs;
            msg.msg_iovlen = 2;
            status = sendmsg(lcm->sendfd, &msg, 0);

            fragment_offset += fraglen;
            packet_size = sizeof(hdr) + fraglen;
        }

        // sanity check
        if (0 == status) {
            assert(fragment_offset == datalen);
        }

        lcm->msg_seqno++;
        g_mutex_unlock(&lcm->transmit_lock);
    }

    return 0;
}
```

短帧和长帧共用调用线程中的 `transmit_lock`。短帧构造 LC02 header 和三个 iovec，调用一次 `sendmsg()` 后推进序号并检查返回长度；长帧先计算片数，再持锁发送第一片和其余片。payload 不在 provider 中转为新的连续副本，片段 iovec 直接引用调用方 data。长帧循环在失败后停止，但函数仍走到末尾返回 0；源码没有实现 ACK 或重传，调用者也不能把返回 0 当成远端收到。

函数先验证 channel 长度，并计算协议 payload：


```c
int channel_size = strlen(channel);
int payload_size = channel_size + 1 + datalen;
```

`+1` 是 channel 末尾的 `\0`。线格式没有单独 channel-length 字段，接收端通过 NUL 终止符找到消息数据起点。

这要求 channel 本身不能包含 NUL，并必须受最大长度限制。解析端也必须在当前数据报边界内搜索终止符，不能用无界 `strlen()` 读取不可信网络数据。

## LC02 短消息布局

短消息 header 定义在 `udpm_util.h`：

```text
0               31 32              63
+-----------------+-----------------+
| magic "LC02"   | message seqno  |
+-----------------+-----------------+
| channel bytes ... | 0 | encoded message ...
+-----------------------------------------------
```

发送代码构造两个 32 位字段：


```c
lcm2_header_short_t hdr;
hdr.magic = htonl(LCM2_MAGIC_SHORT);
hdr.msg_seqno = htonl(lcm->msg_seqno);
```

`htonl` 把主机字节序转换为网络字节序。接收端必须使用 `ntohl` 恢复，不能假设所有机器人计算机都是 little-endian。

Magic `0x4c433032` 对应 ASCII `LC02`。它同时承担协议识别和格式版本分支：不是 LC02 或 LC03 的数据报会被统计为坏包并丢弃。

## iovec 避免用户态拼接

短消息没有先分配 `header + channel + payload` 连续数组。实现建立三个 `iovec`：


```c
struct iovec sendbufs[3];
sendbufs[0].iov_base = &hdr;
sendbufs[0].iov_len = sizeof(hdr);
sendbufs[1].iov_base = (char *) channel;
sendbufs[1].iov_len = channel_size + 1;
sendbufs[2].iov_base = (char *) data;
sendbufs[2].iov_len = datalen;
```

再由一次 `sendmsg()` 发送：


```c
struct msghdr msg = {0};
msg.msg_name = &lcm->dest_addr;
msg.msg_namelen = sizeof(lcm->dest_addr);
msg.msg_iov = sendbufs;
msg.msg_iovlen = 3;

int status = sendmsg(lcm->sendfd, &msg, 0);
```

scatter-gather I/O 的价值是避免额外用户态 memcpy。header 在栈上，channel 和 payload 仍位于调用者原缓冲区，内核按 iovec 视图组装 UDP 数据报。

这不是严格意义的零拷贝。内核仍需要读取这些页面并构建网络包，网卡路径也可能复制；生成式编码器通常已经把对象字段写入连续 payload。准确表述是“避免发送前的用户态拼接副本”。

## transmit_lock 保护序列号与分片连续性

整个短消息发送位于 `transmit_lock` 中：


```c
g_mutex_lock(&lcm->transmit_lock);
// build header and sendmsg
lcm->msg_seqno++;
g_mutex_unlock(&lcm->transmit_lock);
```

锁保护的不只是 socket。它还保证：

- 两个发布线程不会读取同一 `msg_seqno`；
- header 中的序列号和递增动作形成原子事务；
- 大消息的全部 LC03 分片不会被同一 provider 的另一条消息穿插；
- 共享 `msghdr` 相关局部协议状态保持一致。

短消息系统调用本身是线程安全的，但去掉这把锁会破坏 LCM 层序列语义。并发安全必须围绕跨字段不变量设计，不能只看单个 API 是否 thread-safe。

## 短消息返回值语义

代码计算预期 packet size，并比较 `sendmsg()` 返回值：


```c
if (status == packet_size)
    return 0;
else
    return status;
```

对 UDP，成功通常是完整数据报长度；失败返回 -1 并设置 errno。实现保留非预期短写结果，让调用者能看到异常。

对 LC02 短消息，返回 0 表示这次 `sendmsg()` 返回了预期 datagram 长度，不表示任何订阅者已经收到、解码或执行 callback。LC03 大消息的固定版本有更严重的返回值缺口：即使第一片或后续 `sendmsg()` 失败，循环会停止，但函数仍递增序号、解锁并无条件返回 0。因而调用者可能把只发出部分片段的命令误记为本地成功。发送端没有应用层确认；需要交付保证的控制命令必须另加 ack/sequence/deadline，并在改进实现中让 `lcm_udpm_publish()` 返回首个失败或明确的 partial-send 状态。

## LC03 大消息布局

超过单报文阈值后，发送端使用长 header：

```text
+----------------------+----------------------+
| magic "LC03"        | message seqno       |
+----------------------+----------------------+
| original data size   | fragment offset     |
+----------------------+-----------+----------+
| fragment number      | fragments in msg   |
+----------------------+-----------+----------+
| first fragment: channel\0 + data slice      |
| later fragments: data slice only            |
+---------------------------------------------+
```

字段职责分别是：

- `msg_seqno`：把同一发送者的分片归入一条消息；
- `msg_size`：完整消息数据长度，不含 channel；
- `fragment_offset`：当前片数据在完整 payload 中的位置；
- `fragment_no`：当前分片编号；
- `fragments_in_msg`：总分片数量。

发送者地址加 `msg_seqno` 才能唯一标识正在重组的消息。不同进程的序列号都可能从零开始，仅用 seqno 会错误合并来源。

## 分片数量与边界检查

代码用固定最大 fragment payload 计算数量：


```c
int fragment_size = LCM_FRAGMENT_MAX_PAYLOAD;
int nfragments = payload_size / fragment_size
               + !!(payload_size % fragment_size);
```

`!!remainder` 把非零余数转成 1，相当于整数向上取整。由于总分片数字段为 16 位，实现拒绝超过 65535 片的消息。

这里还应同时考虑整数溢出。`channel_size + 1 + datalen` 若在窄 `int` 中溢出，后续分支与分配都会错误。工业实现最好用 `size_t` 做加法，并在每次运算前检查上界，再安全收窄到协议字段类型。

## 第一分片携带 channel

第一片为 channel 预留空间：


```c
int firstfrag_datasize = fragment_size - (channel_size + 1);

first_sendbufs[0] = header;
first_sendbufs[1] = channel + NUL;
first_sendbufs[2] = first data slice;
```

后续分片只包含 header 与 data slice。这样避免每片重复 channel 字符串，也使接收端在第一片到达后才能知道消息应交给哪些订阅者。

其直接后果是：如果第一片丢失，即使其他所有片都到达，接收端也缺少 channel，无法完成分发。协议不会向发送端请求补发。

## fragment_offset 与 data 指针

第一片发送后，offset 增加实际数据字节数。循环处理余下分片：


```c
for (uint16_t frag_no = 1;
     packet_size == status && frag_no < nfragments;
     ++frag_no) {
    hdr.fragment_offset = htonl(fragment_offset);
    hdr.fragment_no = htons(frag_no);

    int fraglen = MIN(fragment_size,
                      datalen - fragment_offset);
    sendbufs[1].iov_base =
        (char *) data + fragment_offset;
    sendbufs[1].iov_len = fraglen;

    status = sendmsg(...);
    fragment_offset += fraglen;
}
```

`fragment_offset` 相对的是消息 data，不包含 channel。接收端可以直接把 slice 放到重组缓冲区的对应偏移。

使用 offset 而不只使用 fragment number，使最后一片变长/变短或未来改变片尺寸时仍可定位。但接收端必须验证 `offset + fraglen <= msg_size`，避免恶意报文越界写。

## 长消息锁覆盖全部系统调用

LC03 分支从第一片到最后一片一直持有 `transmit_lock`。若一条消息需要 N 个分片，其他发布线程等待时间至少包含 N 次 `sendmsg()`。

这给出一个简单上界模型：

```text
Tlock_hold ~= N * Tsendmsg + Tloop
N = ceil((channel + 1 + data) / fragment_payload)
```

大消息会造成发布端 head-of-line blocking。高频小状态与大图像若共享同一 LCM 实例和 UDPM provider，小消息可能被大消息分片发送阻塞。

可以按数据类别拆分实例或传输；更根本的方案是让大数据使用共享内存、TCP 或专用流媒体通道，而让 UDPM 保持小而及时的状态广播。

## UDP 分片放大丢包概率

若单个 UDP 数据报独立成功概率为 `p`，一条 N 片消息完整成功的概率近似为：

```text
Pcomplete = p^N
```

即使 `p = 0.999`：

```text
N = 1    -> 99.9%
N = 50   -> about 95.1%
N = 200  -> about 81.9%
```

真实网络丢包可能相关，突发拥塞会更糟。应用看到的是整条消息缺失，而不是部分 payload。

因此 LC03 是“让超过单报文阈值的数据能够传输”的机制，不是可靠大消息协议。它没有 ACK、NACK、重传窗口或前向纠错。

## 应用分片与 IP 分片的关系

LCM 主动把大消息切成受控大小的 UDP 数据报，目的是避免依赖 IP 层把一个超大数据报再次分片。IP 分片中任一 fragment 丢失同样会使整个 UDP datagram 作废，而且中间设备处理更不稳定。

应用层分片让 LCM 能识别消息并跟踪偏移；接收侧通过 fragment store 在新项到来时按 LRU 压力淘汰，但固定版本没有独立的重组超时扫描。每个 LCM fragment 仍应低于路径 MTU 的安全 payload。若设置过大，底层 IP 仍可能二次分片，形成两层失败放大。

## 序列号回绕

`msg_seqno` 是固定宽度整数，会自然回绕。接收端不能永久把“发送者 + seqno”视为全局唯一标识，只能在有限重组时间窗口内使用。

正确的重组表需要：

- 以来源地址和 seqno 为 key；
- 为每项保存创建/最后更新时间；
- 超时删除不完整项；
- 新第一片与旧残留冲突时重新初始化；
- 限制表项总数和单消息大小。

只要超时时间远小于序列号完整回绕周期，就能避免正常流量中的歧义。

## 多播 TTL 与部署范围

默认 URL 中 `ttl=0` 通常把流量限制在本机。提高 TTL 才允许路由器转发到更远网段，但网络设备是否允许 multicast 仍取决于基础设施配置。

TTL 不是安全边界，也不是订阅权限。任何能加入多播组并到达端口的进程都可能接收或注入报文。生产网络需要 VLAN、主机防火墙、隧道或应用层认证等额外措施。

## 发送路径的数据寿命

`sendmsg()` 返回前，iovec 指向的 header、channel 和 data 必须有效。调用是同步的，因此栈上 header 与调用者 payload 在函数返回前都仍存活。

Provider 没有把 `data` 指针保存到后台线程，所以 `lcm_publish()` 返回后调用者可以立即复用编码缓冲。若将来把发送改为异步队列，就必须：

- 复制 payload；或
- 转移拥有权；或
- 使用引用计数 buffer 并明确完成通知。

“把同步 send 改成后台线程”不是纯性能优化，它会改变 API 的所有权契约。

## 发布性能评估

短消息的用户态额外空间为常数级：栈上 header、三个 iovec 和 msghdr。没有与 payload 等长的拼接 buffer。

长消息仍使用原 payload 切片，不复制整条数据，但系统调用次数为 `O(N)`，持锁时间也随 N 增长。每片都有长 header，带来带宽开销：

```text
overhead ratio ~= N * sizeof(long_header)
                 / original_data_size
```

小 fragment 会增加 header 与 syscall 开销，大 fragment 会逼近 MTU 并增加 IP 分片风险。fragment size 是吞吐、可靠性和网络兼容性的共同参数。

## 最小协议实现

复刻发送端时应先实现短消息：


```cpp
struct ShortHeader {
  std::uint32_t magic_be;
  std::uint32_t seq_be;
};

int PublishShort(int fd, sockaddr_in destination,
                 std::string_view channel,
                 std::span<const std::byte> payload);
```

验证以下不变量：

- channel 长度受限且明确包含 NUL；
- 所有整数转成网络字节序；
- 预期 packet size 与 `sendmsg` 返回值一致；
- sequence increment 与 header 构造在同一锁域；
- API 返回只代表本机发送结果。

随后加入 LC03，并把分片计划先表示成纯函数：


```cpp
std::vector<FragmentView> PlanFragments(
    std::string_view channel,
    std::span<const std::byte> payload,
    std::size_t max_fragment_payload);
```

纯函数可以独立验证 offset、首片容量、末片长度和 65535 上限。网络循环只消费已经验证的计划，避免把算术、协议编码和系统调用错误混在一起。

## 发送协议的设计结论

LCM UDPM 的发送侧以很少的机制换取明确性能：短消息用一次 scatter-gather 系统调用，大消息用显式应用层分片，单把 mutex 维护序列号和分片事务。

它的优点是代码路径短、没有隐藏后台发送队列、buffer 生命周期简单。缺点是大消息锁占用、UDP 不可靠和缺少拥塞控制同样直接暴露。机器人系统应据此安排数据：小而新鲜的状态适合 UDPM，大而必须完整的数据需要更合适的传输策略。
