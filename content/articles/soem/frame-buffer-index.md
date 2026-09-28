# Frame Buffer 与 Index：SOEM 怎样用固定 slot 处理“发出去的帧可能乱序回来”

固定源码：SOEM v2.0.0，提交 `304d1c05eab77dc0d426f1a5cf09c8cc7dc03713`。

这一篇是 SOEM 最值得学的数据结构之一。

如果同时发出多个 EtherCAT frame：

```text
TX: A → B → C
```

返回并不应该被代码假设成永远：

```text
RX: A → B → C
```

SOEM 因此不能只有一个“当前等待帧”变量。

## port 内部直接预分配所有 slot

`ecx_portt`：

~~~c
ec_bufT rxbuf[EC_MAXBUF];
int rxbufstat[EC_MAXBUF];
int rxsa[EC_MAXBUF];

ec_bufT txbuf[EC_MAXBUF];
int txbuflength[EC_MAXBUF];

uint8 lastidx;
pthread_mutex_t getindex_mutex;
pthread_mutex_t rx_mutex;
~~~

每个 `idx` 同时标识：

```text
txbuf[idx]
rxbuf[idx]
rxbufstat[idx]
```

它就是一个固定容量 transaction slot。

## ecx_getindex 像固定 ring 上找空槽

固定实现：

~~~c
pthread_mutex_lock(&(port->getindex_mutex));

idx = port->lastidx + 1;
if (idx >= EC_MAXBUF)
{
   idx = 0;
}

cnt = 0;
while ((port->rxbufstat[idx] != EC_BUF_EMPTY) &&
       (cnt < EC_MAXBUF))
{
   idx++;
   cnt++;
   if (idx >= EC_MAXBUF)
   {
      idx = 0;
   }
}

port->rxbufstat[idx] = EC_BUF_ALLOC;
port->lastidx = idx;

pthread_mutex_unlock(&(port->getindex_mutex));
~~~

这里不是：

```text
malloc frame
push queue
```

而是：

```text
从 lastidx 往后扫描
→ 找 EMPTY slot
→ 标 ALLOC
→ 返回 index
```

## slot state 是事务生命周期

最少可以看到：

```text
EMPTY
  ↓ getindex
ALLOC
  ↓ outframe
TX
  ↓ receive other requested order
RCVD
  ↓ consumer asks this idx
COMPLETE
  ↓ release
EMPTY
```

因此 `rxbufstat[]` 的名字虽然带 rx，但实际上承担整个 transaction state table。

## ecx_inframe 怎样处理乱序

固定注释已经把算法讲得很清楚：

> 如果读到的是请求的 index，就完成当前 slot；如果读到其他正在等待的 index，把它放到对应 buffer，之后请求那个 index 时直接取。

核心：

~~~c
idxf = ecp->index;

if (idxf == idx)
{
   memcpy(rxbuf,
          &(*stack->tempbuf)[ETH_HEADERSIZE],
          (*stack->txbuflength)[idx] - ETH_HEADERSIZE);

   (*stack->rxbufstat)[idx] = EC_BUF_COMPLETE;
}
else
{
   if (idxf < EC_MAXBUF &&
       (*stack->rxbufstat)[idxf] == EC_BUF_TX)
   {
      rxbuf = &(*stack->rxbuf)[idxf];

      memcpy(rxbuf,
             &(*stack->tempbuf)[ETH_HEADERSIZE],
             (*stack->txbuflength)[idxf] - ETH_HEADERSIZE);

      (*stack->rxbufstat)[idxf] = EC_BUF_RCVD;
   }
}
~~~

例如：

```text
等待 A
recv 得到 C
    → C slot = RCVD
    → A 继续等待

之后请求 C
    → 不必再 recv
    → 直接使用 rxbuf[C]
```

## 为什么 frame index 必须写进 Datagram header

`ecx_setupdatagram()`：

~~~c
datagramP->command = com;
datagramP->index = idx;
datagramP->ADP = htoes(ADP);
datagramP->ADO = htoes(ADO);
~~~

所以 wire 上的 EtherCAT datagram index 与本地 slot index 直接相连。

SOEM 不需要再做：

```text
遍历所有 in-flight object
比较地址/type/size
```

而是：

```text
wire index → array index
```

这和 IgH receive parser 的 queue scan 是非常鲜明的对比。

## 为什么这里需要 mutex

SOEM 2.0 的 Linux port 有：

```text
getindex_mutex
tx_mutex
rx_mutex
```

其中 getindex mutex 防止多个线程同时拿到同一个 frame slot。

rx mutex 则保护 socket receive 与把乱序帧写入对应 rx slot 的过程。

所以“固定数组”不等于“天然 lock-free”。

它解决的是：

```text
allocation / ownership / lookup upper bound
```

并发一致性仍需要协议和锁。

## 复杂度

拿 slot：

最坏要扫描 `EC_MAXBUF` 个状态：

```text
O(EC_MAXBUF)
```

但 `EC_MAXBUF` 是构建期固定小上限，因此在工程上是 bounded scan。

返回帧匹配：

```text
O(1)
```

直接用 index 定位。

这是典型实时系统思路：

> 不一定追求抽象意义上的最优复杂度，而是追求小而明确的最坏上界。
