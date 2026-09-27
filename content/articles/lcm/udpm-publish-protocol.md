# UDPM 发送协议：一条控制消息为何能变成几十枚 IP 分片？

上一章我们已经知道：`lcm::LCM` 先把业务对象编码成连续字节，公共 `lcm_publish()` 再通过 provider 的函数表调用 `lcm_udpm_publish()`。现在换回使用者视角。你正在给机器人发送两类数据：一条每 2 ms 更新的 64 字节关节状态，以及一份偶尔更新、约 900 KiB 的点云。两者在业务代码里看起来只是两次相同的调用：

~~~cpp
lcm::LCM bus("udpm://239.255.76.67:7667?ttl=1");
bus.publish("JOINT_STATE", &joint_state);
bus.publish("CLOUD", &point_cloud);
~~~

为什么第一条通常很快发出去，第二条却可能占住另一条发布线程，而且抓包时你可能同时看见 **LCM 自己的分片**和**IP 层自己的分片**？如果只说“LCM 支持 UDP 应用层分片”，还远远不能解释这种行为。

我们沿着发送端逐步追问：它究竟怎样判断该不该分片；为什么使用 `iovec`；两个线程如何争抢同一个序列号；一次本地发送成功为什么不能代表远端已经收到？全文以 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864` 为准。

## 从用户的一个误判开始：LCM 分片和 IP 分片不是同一层

先回顾 IP 层的限制。以不带额外选项、MTU 为 1500 的普通 IPv4 以太网为例，一个不需要 IP 分片的 UDP 数据报最多容纳约 `1500 - 20 - 8 = 1472` 字节的 UDP payload。这只是便于理解的典型值；真实上限还取决于 IP 版本、报头选项、隧道和路径 MTU。

你可能猜想：LCM 会把所有超过 1472 字节的消息切成更小的 LC03 包，彻底避开 IP 分片。**固定版本的源码不支持这个普遍结论。** `udpm_util.h` 明确区分平台：

~~~c
#ifdef __APPLE__
#define LCM_SHORT_MESSAGE_MAX_SIZE 1435
#define LCM_FRAGMENT_MAX_PAYLOAD 1423
#else
#define LCM_SHORT_MESSAGE_MAX_SIZE 65499
#define LCM_FRAGMENT_MAX_PAYLOAD 65487
#endif
~~~

这两个宏定义有一个很容易忽略的含义：LCM 判断的是 `channel + '\0' + 已编码的 payload`，而不是加上 LCM header 后的总 UDP 长度。短包的 LC02 header 占 8 字节；长包的 LC03 header 占 20 字节，所以非 Apple 的两个最大值分别加上对应 header，恰好都是 65,507 字节——IPv4 UDP payload 的理论上限，而不是普通以太网“不发生 IP 分片”的上限。

<escape>**例子：**</escape> 假设 channel 为 `"POSE"`（5 字节，包括末尾 NUL），编码 payload 有 5000 字节。

| 构建配置 | LCM 的判断 | 交给 socket 的 UDP 数据报 |
|---|---|---|
| 非 Apple，短包阈值 65499 | `5005 ≤ 65499`，使用 LC02 | 一枚 `8 + 5 + 5000 = 5013` 字节的 UDP payload |
| Apple，短包阈值 1435 | `5005 > 1435`，使用 LC03 | 4 枚分别包含不同数据切片的 UDP 数据报 |

在上述 MTU 1500 的普通 IPv4 链路上，非 Apple 配置发送的那一枚 5013 字节 UDP 数据报仍可能被 IP 层分成多枚 IP fragments。Apple 配置的 LC03 包，每枚约 1443 字节或更短，才与这种链路的常见 MTU 更匹配。这里比较的是**固定版本的编译分支**，不是声称所有 Apple 设备或所有 Linux 网络都一定使用同一种 MTU。

由此我们得到第一个设计判断：**LC03 解决的是 LCM 如何跨多个 UDP 数据报重组一条业务消息；它是否同时避免 IP 分片，取决于编译时的常量与实际网络 MTU。**

## 第二问：如果我们自己写发送端，应该先写什么？

最直接的实现只有一次系统调用：

~~~cpp
// 教学伪代码：此时还没有 LCM wire header。
sendto(socket, bytes, size, 0, &destination, address_length);
~~~

当消息足够短，它完全合理。但当一条消息大到超出 UDP 单报文最大长度时，内核甚至无法接受这一整份 payload；即使没有超出最大长度，经过小 MTU 链路也可能有较大的 IP 重组失败风险。

因此我们需要在真正调用 socket 之前，先做一个**与操作系统无关的纯计算**：确定走 LC02 还是 LC03；如果是 LC03，第几片放哪一段数据？先算明白，再把每段交给 `sendmsg()`。这样传输失败与分片下标错误就不会被混在同一团代码中。

固定源码 `lcm_udpm_publish()` 的入口先检查 channel 长度，然后选择分支，核心条件只有：

~~~c
int channel_size = strlen(channel);
int payload_size = channel_size + 1 + datalen;

if (payload_size <= LCM_SHORT_MESSAGE_MAX_SIZE) {
    /* LC02：一个 UDP 数据报 */
} else {
    /* LC03：多个 UDP 数据报 */
}
~~~

这是根据真实代码改写的**教学骨架**，不是一段可独立编译的原函数。下面我们先只复刻其中的分片算术，随后再回到 header、锁和 socket。

### 用独立 C++17 实验预测每一片的 offset

编译命令：`g++ -std=c++17 -Wall -Wextra -Werror -pedantic planner.cpp -o planner`。这份程序不打开 socket；它专门验证“相同消息在两种编译常量下，实际会形成多少片、每片从原始 payload 取哪些字节”。

~~~cpp
#include <cassert>
#include <cstddef>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

struct Fragment {
    std::size_t number;
    std::size_t offset;    // 在原始 data 中的起点，不包括 channel
    std::size_t length;    // 当前分片内的 data 字节数
    bool has_channel;      // 只有第一片携带 channel
};

struct Plan {
    bool short_message;
    std::vector<Fragment> fragments;
};

Plan plan(std::size_t data_size, const std::string& channel,
          std::size_t short_limit, std::size_t fragment_payload) {
    const std::size_t channel_bytes = channel.size() + 1; // 包括 '\0'
    if (channel_bytes > short_limit || channel_bytes > fragment_payload)
        throw std::invalid_argument("channel exceeds packet budget");
    if (data_size > std::numeric_limits<std::size_t>::max() - channel_bytes)
        throw std::overflow_error("payload size overflow");
    const std::size_t total = channel_bytes + data_size;

    if (total <= short_limit)
        return {true, {{0, 0, data_size, true}}};

    // 与 LCM 的整数除法 + !!余数完全同义，避免向上取整时溢出。
    const std::size_t count = total / fragment_payload
                            + (total % fragment_payload != 0);
    if (count > 65535)
        throw std::length_error("fragment counter cannot represent count");

    const std::size_t first = fragment_payload - channel_bytes;
    if (first > data_size)
        throw std::logic_error("invalid first fragment");

    Plan result{false, {{0, 0, first, true}}};
    result.fragments.reserve(count);
    std::size_t offset = first;
    while (offset < data_size) {
        const std::size_t remaining = data_size - offset;
        const std::size_t length =
            remaining < fragment_payload ? remaining : fragment_payload;
        result.fragments.push_back(
            {result.fragments.size(), offset, length, false});
        offset += length;
    }
    assert(offset == data_size);
    assert(result.fragments.size() == count);
    return result;
}

int main() {
    const Plan apple = plan(5000, "POSE", 1435, 1423);
    assert(!apple.short_message && apple.fragments.size() == 4);
    assert(apple.fragments[0].length == 1418);
    assert(apple.fragments[1].offset == 1418);
    assert(apple.fragments[3].length == 736);

    const Plan other = plan(5000, "POSE", 65499, 65487);
    assert(other.short_message && other.fragments.size() == 1);

    const Plan other_big = plan(70000, "POSE", 65499, 65487);
    assert(!other_big.short_message && other_big.fragments.size() == 2);
    assert(other_big.fragments[0].length == 65482);
    assert(other_big.fragments[1].offset == 65482);
    assert(other_big.fragments[1].length == 4518);

    std::cout << "apple=" << apple.fragments.size()
              << " non_apple=" << other.fragments.size()
              << " non_apple_big=" << other_big.fragments.size() << '\n';
}
~~~

预期输出为 `apple=4 non_apple=1 non_apple_big=2`。注意实验里 `Fragment` 只描述 payload 在原始数据中的**切片视图**，它没有为每片复制一个新的 `vector<byte>`。第一片还要额外插入 channel，后续片不再重复它；这正是为什么 `offset` 不应把 header 或 channel 的长度也算进去。

把第一个 5000 字节样本展开：

~~~text
Apple profile: LC03，fragment_payload = 1423，channel_bytes = 5

分片 0: [20 字节 LC03 header][5 字节 channel][data 0 .. 1417]
分片 1: [20 字节 LC03 header][data 1418 .. 2840]
分片 2: [20 字节 LC03 header][data 2841 .. 4263]
分片 3: [20 字节 LC03 header][data 4264 .. 4999]

单个 UDP payload 长度：1443、1443、1443、756 字节
原始 data 长度：      1418 + 1423 + 1423 + 736 = 5000 字节
~~~

如果第一片没收到，接收端即使拿到其余三个片段，也没有这条消息的 channel，不能直接交付给按 channel 注册的订阅者。反过来，如果你把 `fragment_offset` 错误地算成包含 channel 的偏移，重组后的字节序列就会整体错位：**我们要保护的不是某个 C++ 类型，而是“每个原始字节在重组后的位置不变”这个不变量。**

到这里，分片算术已经确定。下一步再去看真实 LC02/LC03 header、分散内存的 `iovec`、跨线程的 `transmit_lock`，读者才知道每个字段和每把锁是在保护什么。
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
- 同一个 provider 的发送线程不能把另一条短消息的发送调用插进正在发送的长消息分片序列。

`msghdr`、`iovec` 和短消息 header 都是每次调用各自的栈上局部变量，**不是这把锁保护的共享对象**。锁维护的是 provider 级序列号和一整条应用层分片消息的发送事务；它不保证 IP/网络层的到达顺序，也不跨不同 LCM 实例串行化发送。

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
LCM 的两层分片必须分别讨论。**应用层 LC03 分片**将一条大消息拆成多个有序号、有字节偏移的 UDP 数据报；而**IP 分片**是网络栈在单枚 UDP 数据报超过链路允许的 IP 包大小时，再把它拆成多个 IP packets。这两层互不等价，也不自动互相替代。

回到固定版本：非 Apple 构建下，`LCM_SHORT_MESSAGE_MAX_SIZE=65499`、`LCM_FRAGMENT_MAX_PAYLOAD=65487`。因此 LC02 单报文和一枚 LC03 数据报的 UDP payload 都可能接近 65 KiB。普通 MTU 1500 的 IPv4 以太网可能再次将它们分割成多个 IP fragments。Apple 分支的 1435/1423 阈值能让单枚 LCM 数据报控制在约 1443 字节，但也不能脱离 VPN、隧道或具体路径 MTU 宣称“永不产生 IP 分片”。

~~~text
一条 70000 字节的业务 payload，非 Apple 固定配置：

LCM 自己的分片：
    LC03 #0：65482 字节 data + 5 字节 channel + 20 字节 header
    LC03 #1：4518  字节 data + 20 字节 header

IP 层对每一枚 UDP 数据报再作自己的判断：
    若可承载整枚报文  -> 直接发出
    若超过该路径 MTU  -> 产生 IP fragments
~~~

两层各有一个失败边界。任意一枚 UDP 数据报丢掉某个 IP fragment，该枚 UDP 数据报就不能交付到 LCM；任意一枚 LC03 数据报无法交付，整条业务消息就不能通过完整重组。开头看到的“发送端调用两次 sendmsg，抓包却有几十枚数据包”正是这两层叠加的结果，而不代表 `sendmsg` 在用户态被重复调用了几十次。

LC03 的直接收益是让 LCM 自己识别同一条业务消息的序号、总片数和数据偏移，而不是单纯代替 IP 重组。接收侧用 fragment store 记录还没收齐的业务消息，并在资源压力下淘汰旧项；固定版本没有单独的重组超时扫描。实际部署时，应该结合 MTU、路由、隧道和丢包模式验证配置，不应只看 LCM header 的最大可表示长度。

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


## 附录：固定版本 `lcm_udpm_publish()` 的完整控制流

下面保留本章前面逐步拆过的原始函数，方便读者从 `channel_size` 一路走到最后的 `return`。它来自本文固定版本的 UDPM 实现，并非教学复刻。请特别留意 LC02 与 LC03 在错误返回上的差异，以及 `transmit_lock` 的加锁范围。

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
