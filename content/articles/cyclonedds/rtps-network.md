# RTPS 到 UDP：xmsg、xpack、iovec 与 sendmsg 怎样组成网络数据面

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 为什么不直接创建一块大 Packet Buffer

Cyclone DDS 把发送组织成 xmsg 与 xpack。Payload 往往已经存在于 serdata，如果只是为了在前面添加 RTPS header 就复制整份 payload，会放大大消息成本。

更适合的表示是 scatter/gather：

~~~text
iovec[0] -> RTPS header
iovec[1] -> INFO_TS
iovec[2] -> DATA header
iovec[3] -> serialized payload
~~~

然后一次 sendmsg 发送多个片段。

## xmsg 与 xpack 的职责

~~~text
xmsg
= 一组需要保持关系的 RTPS submessage

xpack
= 当前准备发送的 transport packet
= 收集多个 xmsg 的 iovec
~~~

当 packet 满、显式 flush 或 heartbeat 策略要求立即发送时，xpack 进入真正 transport。

## ddsi_xpack_send_real

固定 ddsi_xmsg.c 会验证 iovec 数量上限，然后根据 destination mode 对单 locator、addrset 或全 unicast 集合执行发送。发送对象不仅包含 payload，还携带目的地址集合与本次 packet 的引用生命周期。

## UDP transport 最终到 ddsrt_sendmsg

ddsi_udp.c 将 locator 转成 sockaddr，并构造：

~~~c
ddsrt_msghdr_t msg = {
  .msg_name = &dstaddr.x,
  .msg_namelen = ...,
  .msg_iov =
    (ddsrt_iovec_t *) msgfrags->iov,
  .msg_iovlen = msgfrags->niov
};
~~~

随后：

~~~c
rc = ddsrt_sendmsg(
  conn->m_sockext.sock,
  &msg,
  sendflags,
  &nsent);
~~~

到这里才真正跨入 OS socket。

## Linux 的数据拷贝边界要分层说

Cyclone DDS 使用 iovec 可以避免在用户态把多个片段拼成一个连续 buffer；但这不等于网卡零拷贝。Linux UDP 仍会进入 socket/network stack，通常形成 skb、经过路由/qdisc/driver，再由 NIC DMA。

应用层 scatter/gather、kernel copy、DMA 是不同层级的 copy。

## 接收方向也是 recvmsg

ddsi_udp.c 构造接收 iovec 后调用 ddsrt_recvmsg。收到数据以后还要解析 packet info、目的接口、PCAP tracing 等信息。

## 为什么接收 Buffer 不是每包 malloc

官方 write-to-take 文档说明 receive buffer 内部采用大块内存与 bump allocator 思路：正常无分片、无乱序时，包处理结束后可以整体复用；只有数据被 defrag/reorder 长期引用时，相应 buffer 才延长寿命。

~~~text
common path
快速 bump + reset

exception path
fragment / out-of-order
延长 buffer ownership
~~~

## 机器人网络调优真正要拆哪些段

至少应分开观察 serialization、WHC、xmsg/xpack、socket send、receive thread wakeup、defrag/reorder、RHC insertion、executor wakeup 与 callback scheduling。一个单独的 DDS latency 平均值无法说明瓶颈属于用户态协议还是内核网络栈。
