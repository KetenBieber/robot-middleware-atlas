# Linux RAW Socket 路径：SOEM 怎样绕过 TCP/IP，把 EtherCAT Frame 直接交给网卡

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

IgH 在 Linux 内核里接近 `net_device`。SOEM 作为用户态 library，必须先回答：

> 不写 kernel master，用户态怎样直接发送 EtherType 为 EtherCAT 的二层帧？

答案在 `oshw/linux/nicdrv.c`。

## ecx_init 很薄

固定 `src/ec_main.c`：

~~~c
int ecx_init(ecx_contextt *context, const char *ifname)
{
   ecx_initmbxpool(context);
   return ecx_setupnic(&context->port, ifname, FALSE);
}
~~~

它只做两件事：

```text
初始化 mailbox pool
初始化 NIC backend
```

真正 Linux-specific 的工作全部进入 OSHW。

## ecx_setupnic 建立 packet socket

`ecx_setupnic()` 保存 interface、socket 和 buffer stack 的关系，并把 socket 绑定到指定 NIC。

其目标不是建立 TCP/UDP connection，而是得到一个可直接收发 Ethernet frame 的 packet socket。

从架构上看：

```text
SOEM EtherCAT frame bytes
        ↓
send()/recv()
        ↓
Linux packet socket
        ↓
network device
```

因此没有：

```text
IP header
UDP header
TCP connection
port number
```

EtherCAT 在这里就是 Ethernet Layer 2 payload。

## Ethernet Header 预先写进每个 TX slot

固定源码：

~~~c
for (i = 0; i < EC_MAXBUF; i++)
{
   ec_setupheader(&(port->txbuf[i]));
   port->rxbufstat[i] = EC_BUF_EMPTY;
}
~~~

而 `ec_setupheader()`：

~~~c
bp->da0 = htons(0xffff);
bp->da1 = htons(0xffff);
bp->da2 = htons(0xffff);
bp->sa0 = htons(priMAC[0]);
bp->sa1 = htons(priMAC[1]);
bp->sa2 = htons(priMAC[2]);
bp->etype = htons(ETH_P_ECAT);
~~~

为什么初始化时就填？

因为这些字段对每一个周期帧基本不变。

没必要每 1 ms：

```text
重新写 destination MAC
重新写 source marker
重新写 EtherType
```

周期里只需要在 Ethernet header 后面重写 EtherCAT datagram 内容。

这又是一种 cold-path prepare / hot-path reuse。

## SOEM 的 source MAC 不是普通主机身份概念

`ec_options.h.in` 甚至专门说明：

> Primary source MAC 并不是 NIC 本身的 MAC；EtherCAT 不依赖普通 MAC addressing，这里主要用于区分冗余路径。

所以不能套普通 TCP/IP 的 mental model：

```text
source MAC == 本机网卡真实 MAC
```

在 SOEM 冗余实现里，它还承担 route marker。

## ecx_outframe 非阻塞地 send

固定实现核心：

~~~c
lp = (*stack->txbuflength)[idx];
(*stack->rxbufstat)[idx] = EC_BUF_TX;

rval = send(*stack->sock, (*stack->txbuf)[idx], lp, 0);

if (rval == -1)
{
   (*stack->rxbufstat)[idx] = EC_BUF_EMPTY;
}
~~~

注意状态先变成：

```text
EC_BUF_TX
```

再 send。

因为从这一刻开始，这个 slot 代表一个等待返回的 wire transaction。

## recv 使用 MSG_DONTWAIT

接收底层：

~~~c
bytesrx = recv(*stack->sock, (*stack->tempbuf), lp, MSG_DONTWAIT);
~~~

这意味着单次 socket read 不会无期限睡住。

真正的“等待到 timeout”逻辑在更上层 `ecx_waitinframe_red()` 中循环并使用 timer/ppoll。

因此要区分：

```text
raw recv primitive
    non-blocking

waitinframe
    bounded blocking with timeout
```

## 用户态 raw socket 对实时性的意义

优点：

- 不需要自定义 EtherCAT kernel module；
- Library 很容易移植和嵌入应用；
- 协议逻辑都在用户态，调试方便。

代价：

- syscall；
- Linux packet path；
- scheduler；
- socket receive path；
- page/cache 行为；

都会进入端到端 jitter。

所以：

> SOEM 可以用于实时系统，不等于普通 Linux raw socket 自动提供硬实时保证。

后面 OSAL/实时并发篇会继续分析 SCHED_FIFO、priority inheritance mutex 和应用线程设计。
