# 从 UDP 数据报到订阅回调：LCM 的接收、重组与缓冲区归还
上一章我们发现：`subscribe()` 并不会自动创建一个帮我们执行 callback 的 worker。真正调用业务函数的是 `lcm.handle()` 所在的应用线程。那么，一个实际使用中的疑问就来了：**我没有调用 `handle()` 的这段时间，网络上新来的 UDP 消息都去了哪里？**

先模拟一次很普通的机器人实验。底盘每 1 ms 发布一条位姿，GUI 上同时订阅这条消息；GUI 的 callback 偶尔为了保存截图运行 15 ms。你在日志里看到消息时间戳连续到达、画面却一阵阵更新，随后开始出现丢帧。第一反应可能是“UDP 不可靠”，但这个现象也可能完全由自己写的 callback 引起。

## 如果我们自己写接收器，第一版会怎样？

大多数人第一次写 socket 都会得到以下教学伪代码：

~~~cpp
while (running) {
    Packet packet = recv_one_datagram(socket);
    if (auto message = decode_or_reassemble(packet))
        user_callback(*message);
}
~~~

假设 t=0 ms 收到第一帧，`user_callback` 用了 15 ms。接收循环直到 t=15 ms 才再次 `recv`。期间网卡与内核仍可能接收 UDP 数据报，但用户态没有及时从 socket 缓冲区取走它们；如果内核缓冲区被写满，后续数据报就被丢弃。**给 callback 加锁或者改成另一个函数都不能改变接收线程被它占住 15 ms 的事实。**

所以第一次改造必须把两种工作分开：接收线程负责尽快从 socket 搬走数据、检查协议并重组长消息；应用线程决定什么时候取出一条完整消息并运行 callback。二者之间必须有一个传递数据所有权的队列。

~~~text
                         内核空间         进程内存
网络报文 ──> UDP socket 接收缓冲 ──recv──> receiver thread
                                                  |
                                              协议校验
                                                  |
                                           短消息/分片重组
                                                  |
                                            完整消息队列
                                                  |
应用线程调用 handle() ──等待通知──取出消息────────+
           |
           +──匹配订阅──运行 callback──归还消息缓冲
~~~

此时业务 callback 可以慢，但它不直接占住 socket 接收线程。然而**两线程不等于无限吞吐**。如果输入频率为 1000 Hz，而每条回调平均耗费 15 ms，单条应用线程最多每秒处理约 66 条；剩余消息仍然积压，最终依赖队列容量和订阅配额发生丢弃。对控制器来说，必须同时考虑“网络收到”与“业务实际处理”之间的数据年龄。

## 队列里有消息后，handle 为什么会醒来？

初学者可能会给每条完整消息都向 pipe 写一个字节，把 pipe 当作计数器。但如果应用线程正在运行耗时 callback，接收线程还在以 1 kHz 入队，通知 pipe 也会很快积压。我们真正需要传递的是一个条件：**队列从空变成非空了，可以来取。**

固定源码中的 receiver 在取得 provider mutex 后，只在 `inbufs_filled` 为空时写通知字节，随后把消息描述符入队：

~~~c
g_rec_mutex_lock(&lcm->mutex);

if (lcm_buf_queue_is_empty(lcm->inbufs_filled))
    if (lcm_internal_pipe_write(lcm->notify_pipe[1], "+", 1) < 0)
        perror("write to notify");

lcm_buf_enqueue(lcm->inbufs_filled, lcmb);

g_rec_mutex_unlock(&lcm->mutex);
~~~

这里有两个重要细节。首先，检查空队列、通知、入队处于**同一把 mutex 的锁域**，应用侧不会在这三个动作中间拿走一个不完整的队列状态。其次，通知不是 payload，也不是新建一个 callback：pipe 可读只意味着应用线程现在可以进入 `handle()` 的下一步。

对应的 `lcm_udpm_handle()` 先读走一个通知字节，再持锁从 `inbufs_filled` 取一条完整消息；如果队列仍非空，它会往 pipe **写回一个字节**，保证下一次调用 `handle()` 仍能发现有数据可处理。所以在稳定态下，队列可能有很多消息，而 pipe 里只需要维护“还有工作可做”的可读状态。

~~~text
初始：       queue=[]             pipe=空
收到 M1：    queue=[M1]           pipe="+"
收到 M2：    queue=[M1,M2]        pipe="+"（无需重复通知）
handle M1：  queue=[M2]           pipe 读走后重新写回 "+"
handle M2：  queue=[]             pipe 被读空
~~~

注意 `notify_pipe` 解决的是应用线程的**等待与唤醒**，不是停止接收线程的信号；后者使用独立的 `thread_msg_pipe`。接下来读 provider 的数据结构时，读者就能理解为什么一个看似很小的 UDP provider 需要两条队列、两个 pipe、接收线程、mutex 和 ring allocator，而不是一只 socket 就够了。

源码版本为 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`。本章后面的原始代码将沿消息实际走过的路径继续展开：先建立接收资源，再等待 socket 与退出事件，最后处理 LC02 短包、LC03 分片以及缓冲区归还。以上 `recv_one_datagram` 是帮助推导的伪代码，不是该提交的函数。
## 先让接收端拥有可复用的状态

每个 UDPM provider 都要保留接收 socket、两条 buffer 队列、ring buffer、接收线程、两条通知 pipe 和未完成分片表。notify_pipe 通知应用线程有完整消息可取；thread_msg_pipe 单独用于通知 receiver 退出。前者随 provider 建立，后者与接收资源一起延迟创建。它们的方向不同，不能合并成一个含糊的“事件 fd”。

下面先看真实的 `lcm_udpm_t`，把收包线程、缓存队列和两条 pipe 放回同一个对象里：


~~~c
typedef struct _lcm_provider_t lcm_udpm_t;
struct _lcm_provider_t {
    SOCKET recvfd;
    SOCKET sendfd;
    struct sockaddr_in dest_addr;

    lcm_t *lcm;

    udpm_params_t params;

    /* size of the kernel UDP receive buffer */
    int kernel_rbuf_sz;
    int warned_about_small_kernel_buf;

    /* Packet structures available for sending or receiving use are
     * stored in the *_empty queues. */
    lcm_buf_queue_t *inbufs_empty;
    /* Received packets that are filled with data are queued here. */
    lcm_buf_queue_t *inbufs_filled;

    /* Memory for received small packets is taken from a fixed-size ring buffer
     * so we don't have to do any mallocs */
    lcm_ringbuf_t *ringbuf;

    GRecMutex mutex; /* Must be locked when reading/writing to the above three queues */

    int thread_created;
    GThread *read_thread;
    int notify_pipe[2];      // pipe to notify application when messages arrive
    int thread_msg_pipe[2];  // pipe to notify read thread when to quit

    GMutex transmit_lock;  // so that only thread at a time can transmit

    /* synchronization variables used only while allocating receive resources
     */
    int creating_read_thread;
    GCond create_read_thread_cond;
    GMutex create_read_thread_mutex;
    GMutex *p_create_read_thread_mutex;

    /* other variables */
    lcm_frag_buf_store *frag_bufs;

    uint32_t udp_rx;             // packets received and processed
    uint32_t udp_discarded_bad;  // packets discarded because they were bad
                                 // somehow
    double udp_low_watermark;    // least buffer available
    int32_t udp_last_report_secs;

    uint32_t msg_seqno;  // rolling counter of how many messages transmitted
};
~~~

这个结构体本身不创建线程；它保存后来由 _setup_recv_parts() 建立的资源。进程提供地址空间和打开的资源，线程则是在这个进程里独立运行、会被内核调度的执行流。UDPM receiver 与调用 handle 的线程共享同一个 lcm_udpm_t、队列和内存，所以线程边界能隔离业务耗时，却不会自动复制这些对象；两条执行流访问共享队列时仍必须遵守同一把 mutex。两个队列里放的是 lcm_buf_t 描述符：inbufs_empty 提供可重用的空壳，inbufs_filled 暂存已经完整、等业务线程处理的消息。消息 payload 并不一定来自同一个分配器，因此描述符还要记录 ring buffer 所有者。

接着沿 `lcm_buf_t` 和 `lcm_frag_buf_t` 的字段，把描述符、payload 与分片中间状态分开：


~~~c
typedef struct _lcm_buf {
    char channel_name[LCM_MAX_CHANNEL_NAME_LENGTH + 1];
    int channel_size;  // length of channel name

    int64_t recv_utime;  // timestamp of first datagram receipt
    char *buf;           // pointer to beginning of message.  This includes
                         // the header for unfragmented messages, and does
                         // not include the header for fragmented messages.

    int data_offset;         // offset to payload
    int data_size;           // size of payload
    lcm_ringbuf_t *ringbuf;  // the ringbuffer used to allocate buf.  NULL if
                             // not allocated from ringbuf

    int packet_size;  // total bytes received
    int buf_size;     // bytes allocated

    struct sockaddr from;  // sender
    socklen_t fromlen;
    struct _lcm_buf *next;
} lcm_buf_t;

typedef struct _lcm_frag_buf {
    char channel[LCM_MAX_CHANNEL_NAME_LENGTH + 1];
    struct sockaddr_in from;
    char *data;
    uint32_t data_size;
    uint16_t fragments_remaining;
    uint32_t msg_seqno;
    int64_t last_packet_utime;
    lcm_frag_key_t key;
} lcm_frag_buf_t;
~~~

lcm_buf_t::next 让这些描述符连成单链表。队列不只是一个 head 指针：tail 保存“下一个可写入的位置”，初始指向 head，添加节点后改指向新节点的 next。下面是队列数据结构和实际入队/出队代码：


~~~c
typedef struct _lcm_buf_queue {
    lcm_buf_t *head;
    lcm_buf_t **tail;
    int count;
} lcm_buf_queue_t;

lcm_buf_queue_t *lcm_buf_queue_new(void)
{
    lcm_buf_queue_t *q = (lcm_buf_queue_t *) malloc(sizeof(lcm_buf_queue_t));

    q->head = NULL;
    q->tail = &q->head;
    q->count = 0;
    return q;
}

lcm_buf_t *lcm_buf_dequeue(lcm_buf_queue_t *q)
{
    lcm_buf_t *el;

    el = q->head;
    if (!el)
        return NULL;

    q->head = el->next;
    el->next = NULL;
    if (!q->head)
        q->tail = &q->head;
    q->count--;

    return el;
}

void lcm_buf_enqueue(lcm_buf_queue_t *q, lcm_buf_t *el)
{
    *(q->tail) = el;
    q->tail = &el->next;
    el->next = NULL;
    q->count++;
}
~~~

tail 的类型是二级指针：它不是指向“最后一个节点”，而是指向一个链表指针槽位。空队列时该槽位就是 q->head；加节点后，槽位变成刚加节点的 next。这样入队不用从头扫描尾节点，出队也只动 head；如果出队后 head 为空，tail 必须重新指向 q->head。这个实现不自带线程安全：没有 lcm->mutex，producer 与 handle consumer 同时改 head、tail 或 count 时会产生 C data race；两个入队者可能覆盖同一指针槽，导致一条消息从链表丢失。LCM 在调用这些函数的外围锁住共享 queue，而不是靠链表函数本身解决并发。

### 追问：为什么队列尾巴不是普通指针，而是二级指针？

前面已经解决“为什么要有接收线程和应用线程”。可是这两条线程之间仍有一个具体成本：如果每收到一条 datagram 都申请一个全新的消息对象，处理完再释放，接收线程会不断承担分配开销。因此真实 UDPM 把描述符与描述符所指向的 payload 分开，借助两条队列重复利用描述符。

先别看 socket。下面是一个**独立可编译的 C11 教学程序**，只保留实际链表的 `head`、`tail`、`count` 和三个节点，让一条消息完整经历“空闲描述符 → 已填充描述符 → callback 完成 → 归还”。

~~~c
#include <assert.h>
#include <stdio.h>

typedef struct Node {
    int id;
    struct Node *next;
} Node;

typedef struct {
    Node *head;
    Node **tail;       /* 指向下一个能够写入 Node* 的槽位 */
    unsigned count;
} Queue;

static void init(Queue *q) {
    q->head = NULL;
    q->tail = &q->head;
    q->count = 0;
}

static void push(Queue *q, Node *n) {
    assert(n->next == NULL);
    *(q->tail) = n;
    q->tail = &n->next;
    ++q->count;
}

static Node *pop(Queue *q) {
    Node *n = q->head;
    if (n == NULL) return NULL;
    q->head = n->next;
    n->next = NULL;
    if (q->head == NULL) q->tail = &q->head;
    --q->count;
    return n;
}

int main(void) {
    Node a = {1, NULL}, b = {2, NULL}, c = {3, NULL};
    Queue empty, filled;
    init(&empty);
    init(&filled);
    push(&empty, &a);
    push(&empty, &b);
    push(&empty, &c);
    assert(empty.count == 3 && empty.tail == &c.next);

    Node *receiving = pop(&empty);
    assert(receiving == &a && receiving->next == NULL);
    push(&filled, receiving);

    receiving = pop(&empty);
    assert(receiving == &b);
    push(&filled, receiving);
    assert(filled.count == 2 && filled.tail == &b.next);

    Node *handling = pop(&filled);
    assert(handling == &a && filled.head == &b);
    push(&empty, handling);
    assert(empty.head == &c && empty.tail == &a.next);

    handling = pop(&filled);
    assert(handling == &b && filled.head == NULL);
    assert(filled.tail == &filled.head);
    push(&empty, handling);
    printf("empty=%u filled=%u\n", empty.count, filled.count);
}
~~~

使用 `gcc -std=c11 -Wall -Wextra -Werror -pedantic -O0 queue.c -o queue` 编译，预期输出 `empty=3 filled=0`。这里的“空闲”是描述符尚未承载待分发消息，不代表没有分配过内存。

现在再看最难懂的 `Node **tail`。它保存的**不是尾节点的地址，而是下一次应该改写的指针变量的地址**：

~~~text
初始： head = NULL; tail = &head

push(A):
    head -> A -> NULL
                  ^
    tail = &A.next

push(B):
    head -> A -> B -> NULL
                       ^
    tail = &B.next

pop(A):
    head -> B -> NULL
                  ^
    tail = &B.next

pop(B):
    head = NULL; tail = &head    // 这一步不可省略
~~~

如果只保存尾节点地址，空队列入队时要修改 `head`，非空时要修改 `tail->next`，就必须写两条分支。二级指针把这两种情况统一成 `*(q->tail) = n`，但也引入了一条维护不变量：**每次弹出最后一条消息，都要把 tail 重新指回 head 的地址**。否则 tail 会悬挂在已出队节点的 next 字段上；以后复用该节点，新的入队就可能把两条原本独立的链表接在一起。

这仍然不是无锁队列。教学程序只有一个执行线程；真实的 receiver 与 handle 线程在访问 `head/tail/count` 时必须由外围 `lcm->mutex` 串行化。普通指针赋值即使在某个 CPU 上一次就能写完，也不意味着它符合 C 语言的跨线程同步规则。

### 队列里放的是描述符，不等于 payload 内存归谁所有

真实的 `inbufs_empty` 与 `inbufs_filled` 保存的是 `lcm_buf_t` 描述符。receiver 从空闲队列取出描述符后，再为当前 datagram 绑定 ring 或 heap 内存；完整消息入 filled 队列；`lcm_udpm_handle` 将其出队，构造临时 `lcm_recv_buf_t` 交给同步业务 callback；callback 返回后才释放 payload，并归还同一个描述符：

~~~text
empty --pop--> lcm_buf_t
                  |
                  | ring/heap 上取得 payload
                  | recvmsg + 解析/重组
                  v
filled <--push-- lcm_buf_t
   |
   | handle 线程 pop
   v
lcm_recv_buf_t 临时借用 lcm_buf_t.buf
   |
   | 同步调用匹配的 callback
   v
释放 payload；把 lcm_buf_t 重新 push 到 empty
~~~

固定源码里，`lcm_udpm_handle` 在 dispatch 返回后执行：

~~~c
g_rec_mutex_lock(&lcm->mutex);
lcm_buf_free_data(lcmb, lcm->ringbuf);
lcm_buf_enqueue(lcm->inbufs_empty, lcmb);
g_rec_mutex_unlock(&lcm->mutex);
~~~

因此，callback 可以同步读取 `rbuf.data`，却不能只保存这根指针就返回，让另一个线程日后使用：那时指向的内存已经被释放或允许复用。需要异步处理的机器人应用，应在 callback 返回前复制必要字节，或将内容转移到自己拥有寿命的业务消息中。

下一问自然出现：为什么 `lcm_buf_free_data` 还需要收到一个 `ringbuf` 参数？因为分配这段 payload 的旧 ring 可能已经满了，receiver 切换了新 ring，而等待处理的旧消息还引用着旧 ring。释放 payload 的时候必须认出它究竟来自哪一代分配器，不能把“当前 ring”误当成“所有消息的 ring”。
lcm_buf_t::buf 是一个裸指针，单看它并不能判断该调用 ring deallocate 还是 free()；ringbuf 字段正是分配来源标签，空值表示 buffer 由堆分配。lcm_frag_buf_t::data 则暂时由分片项拥有，完整消息形成后才转给 lcm_buf_t。这里不是 shared_ptr，没有引用计数：每个阶段都要明确只有一个负责释放它的对象。

接收时先从 empty queue 取一个描述符，再给它分配最大 UDP datagram 区域。描述符池空了就补一批；ring 空间不够时，源码创建更大的新 ring，但把仍被旧消息使用的 ring 暂时留存，并在每个描述符上记录实际分配来源：


~~~c
lcm_buf_t *lcm_buf_allocate_data(lcm_buf_queue_t *inbufs_empty, lcm_ringbuf_t **ringbuf)
{
    lcm_buf_t *lcmb = NULL;
    // first allocate a buffer struct for the packet metadata
    if (lcm_buf_queue_is_empty(inbufs_empty)) {
        // allocate additional buffer structs if needed
        int i;
        for (i = 0; i < LCM_DEFAULT_RECV_BUFS; i++) {
            lcm_buf_t *nbuf = (lcm_buf_t *) calloc(1, sizeof(lcm_buf_t));
            lcm_buf_enqueue(inbufs_empty, nbuf);
        }
    }

    lcmb = lcm_buf_dequeue(inbufs_empty);
    assert(lcmb);

    // allocate space on the ringbuffer for the packet data.
    // give it the maximum possible size for an unfragmented packet
    lcmb->buf = lcm_ringbuf_alloc(*ringbuf, LCM_MAX_UNFRAGMENTED_PACKET_SIZE);
    if (lcmb->buf == NULL) {
        // ringbuffer is full.  allocate a larger ringbuffer

        // Can't free the old ringbuffer yet because it's in use (i.e., full)
        // Must wait until later to free it.
        assert(lcm_ringbuf_used(*ringbuf) > 0);
        dbg(DBG_LCM, "Orphaning ringbuffer %p\n", *ringbuf);

        unsigned int old_capacity = lcm_ringbuf_capacity(*ringbuf);
        unsigned int new_capacity = (unsigned int) (old_capacity * 1.5);
        // replace the passed in ringbuf with the new one
        *ringbuf = lcm_ringbuf_new(new_capacity);
        lcmb->buf = lcm_ringbuf_alloc(*ringbuf, 65536);
        assert(lcmb->buf);
        dbg(DBG_LCM, "Allocated new ringbuffer size %u\n", new_capacity);
    }
    // save a pointer to the ringbuf, in case it gets replaced by another call
    lcmb->ringbuf = *ringbuf;

    // zero the last byte so that strlen never segfaults
    lcmb->buf[65535] = 0;
    return lcmb;
}
~~~

### 为什么要换一整代 ring，而不是把旧内存直接覆盖？

把 `inbufs_empty` 和 `inbufs_filled` 弄明白之后，接收器还有一个更棘手的问题。假设应用线程正在执行第 1 条消息的 callback，暂时还没有释放它的 payload；receiver 却继续收到第 2、3、4 条消息。假如环形缓冲区写指针简单地绕回起点，第 4 条就可能覆盖 callback 仍在读取的第 1 条。**写指针到达尾部，不意味着起点那段内存已经可以写。**

真实的 `ringbuffer.c` 因此不是简单的 `data[write++ % capacity]`。它在同一块连续内存里保存一个个变长的分配记录，每条记录前面都有自己的管理头部：

~~~c
struct _lcm_ringbuf_rec {
    int32_t magic;
    lcm_ringbuf_rec_t *prev;
    lcm_ringbuf_rec_t *next;
    unsigned int length;
    char buf[];
};
~~~

`buf[]` 是 C 的 flexible array member，说明 payload 紧跟在记录头部后面，并不是一个独立分配的数组。`lcm_ringbuf_alloc(ring, len)` 会先把需求加上这个头部的大小，再向上对齐到 32 字节。如果 payload 只需要 3 字节，实际占用仍要包含记录头部、对齐填充；所以 `ring->used` 计算的是**已分配块的总占用**，不能拿它直接当作业务消息字节数。

这块内存里有两种合法的存放形态：

~~~text
形态一：已分配记录处于连续区间
ring.data
| 可用 | 头部+A | 头部+B | 可用 ... |
         ^head              ^tail

形态二：写入越过 ring 末端后从开头继续
ring.data
| 头部+C | 可用 ... | 头部+A | 头部+B |
  ^tail                        ^head
~~~

这里的 `head/tail` 是**分配记录的链表头尾**，不是上面 `inbufs_filled` 消息描述符队列的头尾。两个容器虽然使用相似名字，却在维护不同的资源：队列决定“下一条处理什么消息”，ring 记录决定“这段 payload 内存何时可以重新使用”。

看 `lcm_ringbuf_alloc` 的真正决策：如果 `head` 在 `tail` 前面，下一块只能放在当前 tail 与 head 之间；如果 head 在 tail 前面，则先尝试 tail 之后直到 ring 末尾，放不下才尝试从开头到 head 之前。两个区域都放不下就返回 `NULL`。这里不能为了追求吞吐直接覆盖 head，因为 head 可能仍由正在等待处理的消息借用。

### 一条回调没结束时，旧 ring 为什么必须留下？

假设 ring R1 里还有两块正在使用的 payload。receiver 要求再分配一个最大 datagram 缓冲，却发现 R1 没有足够的连续空间。这时 `lcm_buf_allocate_data` 会将当前 ring 指针换成更大的 R2，但**不会释放 R1**；每只 `lcm_buf_t` 仍在 `ringbuf` 字段里记着自己分配时所属的 R1 或 R2：

~~~text
当前 ring: R1
  A.buf -> R1[记录 A]    callback 正在借用
  B.buf -> R1[记录 B]    等待分发

R1 分配失败：
  current_ring -> R2
  C.buf -> R2[记录 C]    新消息

处理 A、B 时：
  free_data(A) -> 从 R1 解除 A 的占用
  free_data(B) -> 从 R1 解除 B 的占用
                  R1 的 used 终于变成 0 -> 释放 R1

R2 仍是 current_ring，不随 A/B 释放
~~~

这就是 `lcm_buf_free_data(lcmb, current_ring)` 的两个参数各自负责的事情。`lcmb->ringbuf` 表示**当前消息实际来源**；`current_ring` 只用来判断该来源是不是已经被淘汰的上一代。当来源非空时，对**来源**执行 `lcm_ringbuf_dealloc`；如果来源不是当前 ring，并且来源的 `used` 已经降为 0，才调用 `lcm_ringbuf_free`。若 `lcmb->ringbuf == NULL`，说明该 payload 独立来自 heap，应调用普通的 `free`。

这里也暴露了 ring allocator 的一项真实限制。`lcm_ringbuf_dealloc` 有明确断言：

~~~c
assert(rec == ring->head || rec == ring->tail);
~~~

它只允许释放**最早**或**最新**的分配记录，不能随意回收中间的一块。因此 ring allocator 并不是通用的任意顺序内存池。网络路径通常沿 FIFO 方向消费消息；最近一次分配失败、需要取消的临时块可以从 tail 方向退回。设计一个自己的异步分发器时，不能直接把这些 ring 指针交给任意顺序的多线程 worker，再假定它们可以随时释放。若业务需要乱序完成，应让 worker 使用独立所有权的缓冲池，或由统一回收器按分配顺序释放。

从机器人控制角度看，旧 ring 被保留也是一个**背压与内存占用信号**：慢 callback 持有旧 payload，接收线程仍要继续服务新流量，于是可能创建 R2、R3……。描述符队列的复用减少了分配次数，却不能由此推断系统存在固定内存上限；生产环境仍需要监控数据年龄、缓存占用和丢弃策略。
结构体描述符的补充并没有队列最大项数，ring 满后也会扩容；因此这部分适合减少频繁分配，却不能当成绝对内存上限。每个接收 buffer 会先分配 65536 字节，recvmsg 最多写入 65535 字节，最后一字节保留零值以保证字符串扫描有终点。短消息路径随后会把最近一次 ring 分配收缩到实际 datagram 长度；长消息的 payload 则另由分片 buffer 暂时保存。

## 第一次需要接收时才建立线程

只发布、不订阅的进程不必创建接收线程。第一次 handle 或其他接收入口触发 _setup_recv_parts() 时，它才打开组播 socket、加入组、创建队列和 ring、预置 buffer 描述符、创建退出 pipe 并启动线程。延迟初始化节省了 publish-only 进程的资源，但引入了一个并发问题：两个线程可能同时成为“第一次订阅者”。

GCond 是带条件谓词的等待机制。线程不能把“收到 signal”当成谓词已经成立，因为信号可能在检查之前发出，也可能发生虚假唤醒。正确的用法是：持有 mutex 检查共享状态；条件不满足时在循环中 wait；wait 原子地释放 mutex 并阻塞；醒来后重新取得 mutex，再检查谓词。LCM 用 creating_read_thread 表示创建过程是否结束，并用 thread-local 标记识别创建者的重入调用。

首次订阅时的竞态并非靠“先检查 thread_created”解决；看 `_setup_recv_parts()` 如何处理另一个线程已经在初始化的情况：


~~~c
    g_rec_mutex_lock(&lcm->mutex);

    if (lcm->creating_read_thread) {
        if (g_private_get(&CREATE_READ_THREAD_PKEY)) {
            g_rec_mutex_unlock(&lcm->mutex);
            return 0;
        }

        g_mutex_lock(lcm->p_create_read_thread_mutex);
        g_rec_mutex_unlock(&lcm->mutex);

        while (lcm->creating_read_thread) {
            g_cond_wait(&lcm->create_read_thread_cond, lcm->p_create_read_thread_mutex);
        }
        g_mutex_unlock(lcm->p_create_read_thread_mutex);
        g_rec_mutex_lock(&lcm->mutex);

        int result = lcm->thread_created ? 0 : -1;
        g_rec_mutex_unlock(&lcm->mutex);
        return result;
    } else if (lcm->thread_created) {
        g_rec_mutex_unlock(&lcm->mutex);
        return 0;
    }

    lcm->creating_read_thread = 1;
~~~

摘录从原函数中保留了关键控制流，省略 GLib 注释。此处先锁 lcm->mutex 读取和写入 creating_read_thread；等待者随后取得单独的 GMutex，再释放递归锁，避免创建者需要 lcm->mutex 收尾时被等待线程挡住。g_cond_wait() 返回后再检查谓词，所以通知只是让线程重新检查，并不等于 OS 已给它 CPU。

Linux 上，GLib 的条件变量实现会借助内核等待/唤醒设施（常见路径包含 futex），让等待线程在条件尚未满足时不占着 CPU 自旋；LCM 本身调用的是 GLib 的 g_cond_wait()，不是直接调用 futex。条件变化后，线程最多先成为 runnable，仍需等待内核调度。若把 while 改成 if，一个虚假唤醒就可能使第二个调用者在资源尚未建立时继续运行。

thread-local 标记是每条 OS 线程各自的一份状态：创建者再次进入 setup 时可以识别自己，直接返回；否则它会等待自己负责结束的初始化，永远等不到自己继续执行。GRecMutex 允许同一线程嵌套加锁，而普通 GMutex 不允许；GCond 等待函数需要普通 mutex，于是源码另外使用 create_read_thread_mutex，并在睡眠前释放 provider 的递归锁。资源建立完成后，创建者在状态锁保护下发布结果并广播。创建期间的线程只能在该同步协议下读取 thread_created；它们不能根据“线程对象已分配”推断 socket、queue 与退出 pipe 都已就绪。部分创建失败进入统一清理，再将 creating_read_thread 清零并唤醒等待者，避免永久卡在初始化状态。

## 接收线程等 socket 与退出 pipe

接收线程不能只阻塞在 recvmsg()。若 destroy 只设置一个普通标志，线程仍可能睡在没有新报文的 socket 上，关闭操作便无法等它退出。LCM 用 select() 同时等待接收 socket 与独立的退出 pipe；pipe 字节进入内核缓冲区后，阻塞在 select() 的线程才会变为 runnable。

实际接收线程用 `select()` 同时等待网络数据和退出 pipe，相关循环如下：


~~~c
    int got_complete_message = 0;

    while (!got_complete_message) {
        // wait for either incoming UDP data, or for an abort message
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(lcm->recvfd, &fds);
        FD_SET(lcm->thread_msg_pipe[0], &fds);
        SOCKET maxfd = MAX(lcm->recvfd, lcm->thread_msg_pipe[0]);

        if (select(maxfd + 1, &fds, NULL, NULL, NULL) <= 0) {
            perror("udp_read_packet -- select:");
            continue;
        }

        if (FD_ISSET(lcm->thread_msg_pipe[0], &fds)) {
            // received an exit command.
            dbg(DBG_LCM, "read thread received exit command\n");
            if (lcmb) {
                // lcmb is not on one of the memory managed buffer queues.  We could
                // either put it back on one of the queues, or just free it here.  Do the
                // latter.
                //
                // Can also just free its lcm_buf_t here.  Its data buffer is
                // managed either by the ring buffer or the fragment buffer, so
                // we can ignore it.
                free(lcmb);
            }
            return NULL;
        }

        // there is incoming UDP data ready.
        assert(FD_ISSET(lcm->recvfd, &fds));

        if (!lcmb) {
            g_rec_mutex_lock(&lcm->mutex);
            lcmb = lcm_buf_allocate_data(lcm->inbufs_empty, &lcm->ringbuf);
            g_rec_mutex_unlock(&lcm->mutex);
        }
        struct iovec vec;
        vec.iov_base = lcmb->buf;
        vec.iov_len = 65535;

        struct msghdr msg;
        memset(&msg, 0, sizeof(struct msghdr));
        msg.msg_name = &lcmb->from;
        msg.msg_namelen = sizeof(struct sockaddr);
        msg.msg_iov = &vec;
        msg.msg_iovlen = 1;
#ifdef MSG_EXT_HDR
        // operating systems that provide SO_TIMESTAMP allow us to obtain more
        // accurate timestamps by having the kernel produce timestamps as soon
        // as packets are received.
        char controlbuf[64];
        msg.msg_control = controlbuf;
        msg.msg_controllen = sizeof(controlbuf);
        msg.msg_flags = 0;
#endif
        sz = recvmsg(lcm->recvfd, &msg, 0);

        if (sz < 0) {
            perror("udp_read_packet -- recvmsg");
            lcm->udp_discarded_bad++;
            continue;
        }

        if (sz < sizeof(lcm2_header_short_t)) {
            // packet too short to be LCM
            lcm->udp_discarded_bad++;
            continue;
        }

        lcmb->fromlen = msg.msg_namelen;
        int got_utime = 0;
#ifdef SO_TIMESTAMP
        struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
        /* Get the receive timestamp out of the packet headers if possible */
        while (!lcmb->recv_utime && cmsg) {
            if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_TIMESTAMP) {
                struct timeval *t = (struct timeval *) CMSG_DATA(cmsg);
                lcmb->recv_utime = (int64_t) t->tv_sec * 1000000 + t->tv_usec;
                got_utime = 1;
                break;
            }
            cmsg = CMSG_NXTHDR(&msg, cmsg);
        }
#endif
        if (!got_utime)
            lcmb->recv_utime = g_get_real_time();

        lcm2_header_short_t *hdr2 = (lcm2_header_short_t *) lcmb->buf;
        uint32_t rcvd_magic = ntohl(hdr2->magic);
        if (rcvd_magic == LCM2_MAGIC_SHORT)
            got_complete_message = _recv_short_message(lcm, lcmb, sz);
        else if (rcvd_magic == LCM2_MAGIC_LONG)
            got_complete_message = _recv_message_fragment(lcm, lcmb, sz);
        else {
            dbg(DBG_LCM, "LCM: bad magic\n");
            lcm->udp_discarded_bad++;
            continue;
        }
    }
~~~

短消息不会长期占满 ring；在 udp_read_packet() 得到完整消息后，它把最后一次预分配收缩到实际 datagram 长度：


~~~c
    if (lcmb->ringbuf) {
        g_rec_mutex_lock(&lcm->mutex);
        lcm_ringbuf_shrink_last(lcmb->ringbuf, lcmb->buf, sz);
        g_rec_mutex_unlock(&lcm->mutex);
    }

    return lcmb;
~~~

以上固定摘录保留了接收循环的连续主要分支，包括可用时读取 SO_TIMESTAMP ancillary data、否则退回 g_get_real_time() 的逻辑。recvmsg() 是系统调用：线程从用户态进入内核，由内核从 UDP socket 接收缓冲取出数据，再复制到用户态 lcmb->buf；msg_name 同时把发送方地址写进 lcmb->from。线程阻塞在 select() 时，内核可把它从 runnable 集合移出并调度别的线程，执行流切换涉及保存和恢复线程上下文；fd 就绪后它先变回 runnable，之后还要等 CPU。这个阻塞/切换避免它空等时耗掉 CPU，但不是 callback 已经开始的意思。短消息可立即完成，长消息则要等 _recv_message_fragment() 完成重组；未知 magic 和过短数据报被计入 bad packet 并丢弃。

select() 返回只表示 fd 已经可读，不表示 callback 开始运行。线程恢复后才调用 recvmsg()；此时它以 lcm->mutex 保护 ring 分配和 buffer 元数据，但解析 fragment store 的工作由唯一接收线程串行完成，并没有给每次 hash-table 操作再套一把锁。析构时必须先 join 这条线程，随后才能销毁 fragment store。

接收线程处理好一条完整消息后，在同一把 lcm->mutex 下完成“检查队列是否为空、写入一个通知字节、把描述符入队”：

固定源码中检查空队列、发送通知与入队的锁域如下：


~~~c
static void *recv_thread(void *user)
{
#ifdef G_OS_UNIX
    // Mask out all signals on this thread.
    sigset_t mask;
    sigfillset(&mask);
    pthread_sigmask(SIG_SETMASK, &mask, NULL);
#endif

    lcm_udpm_t *lcm = (lcm_udpm_t *) user;

    while (1) {
        lcm_buf_t *lcmb = udp_read_packet(lcm);
        if (!lcmb)
            break;

        /* If necessary, notify the reading thread by writing to a pipe.  We
         * only want one character in the pipe at a time to avoid blocking
         * writes, so we only do this when the queue transitions from empty to
         * non-empty. */
        g_rec_mutex_lock(&lcm->mutex);

        if (lcm_buf_queue_is_empty(lcm->inbufs_filled))
            if (lcm_internal_pipe_write(lcm->notify_pipe[1], "+", 1) < 0)
                perror("write to notify");

        /* Queue the packet for future retrieval by lcm_handle (). */
        lcm_buf_enqueue(lcm->inbufs_filled, lcmb);

        g_rec_mutex_unlock(&lcm->mutex);
    }
    dbg(DBG_LCM, "read thread exiting\n");
    return NULL;
}
~~~

pipe 中不是“一条消息对应一个字节”的计数器。它表达的是队列从空变为非空，之后消费者每次只取一条消息；若队列里仍有数据，消费者在锁内再写回一个字节，维持可读状态。因队列状态与读写通知字节共享同一把锁，消费者不会观察到“空队列却已经消费了仍有效的唯一通知”这种中间状态。通知字节被写入内核 pipe 缓冲后，阻塞在 pipe read 的线程才可能 runnable；之后还需内核调度、lcm_handle() 取队列并调用分发函数，callback 才运行。

这条通知 pipe 在 POSIX 上是真正的匿名 pipe；Windows 兼容层把它实现为一对本机 TCP socket。两者都提供可等待的内核缓冲字节流，均不是共享内存。pipe 也不同于 semaphore：这里靠“队列是否为空”的状态维持通知，不要求 pipe 字节数等于消息数。

## 收到 UDP 报文，不代表它可以直接当协议头读取

我们从最朴素的 socket 接收实现出发，容易写出这种危险的步骤：`recvmsg()` 返回多少字节都不看，直接把缓冲区强转为 `lcm2_header_long_t*`，然后读取偏移和分片数量。即使真正收到的 datagram 只有 8 字节，也可能被恶意或损坏的报文标成 LC03；如果没有先要求 `sz >= sizeof(lcm2_header_long_t)`，接下来的字段访问就会越过报文有效区域。

固定版本的 `udp_read_packet()` 先检查 `sz < sizeof(lcm2_header_short_t)`，随后只根据 magic 选择 `_recv_short_message` 或 `_recv_message_fragment`。LC03 的真实 header 更长，`_recv_message_fragment()` 中直接把传入的缓冲区转换成 long header 并读取字段，且在首片用 `strlen(channel)` 查找 NUL。**不要把前面的短 header 长度检查误认为已经验证长 header 和首片字符串边界。**

~~~text
收到一枚 datagram
   |
   +-- sz < 8 ----------- 丢弃
   |
   +-- magic == LC02 ---- 仍须验证 channel NUL 处于报文范围内
   |
   +-- magic == LC03 ---- 必须先验证 sz >= 20
                              |
                         首片还必须验证
                         channel NUL 位于当前 datagram 内
                              |
                         才能扣减 frag_size 并 memcpy
~~~

这是阅读固定源码时应当显式识别的**不可信输入边界**，不是说原实现已经完成了图中的全部校验。自己实现或加固 receiver 时，应先保证长 header 完整，再用长度受限的查找（例如 `memchr(data_start, '\0', available)`）确认首片的 channel 确实在当前 datagram 内，最后才使用 channel 长度更新 `frag_size`。不能让未终止的 channel 字符串把 `strlen` 带出接收缓冲，也不能等到 `memcpy` 前才第一次验证数据报边界。这个检查与后面避免 `offset + fraglen` 无符号溢出的范围检查是两个相互独立的不变量。


## LC03 的 key、分片计数和边界检查

短消息可用一个 UDP datagram 装下；LC03 长消息由多片组成。接收方需要把同一个源地址和 msg_seqno 的片放进同一个未完成项。若只按序号索引，两个发送者恰好使用相同序号时会把不同消息混在一起；固定代码把 IPv4 地址、UDP 端口和消息序号一起作为 key。

下面是 _recv_message_fragment() 的固定源码。它也揭示了此版本的一个重要约束：字段 fragments_remaining 只减计数，没有按 fragment_no 设置 bitmap 去重。


~~~c
static int _recv_message_fragment(lcm_udpm_t *lcm, lcm_buf_t *lcmb, uint32_t sz)
{
    lcm2_header_long_t *hdr = (lcm2_header_long_t *) lcmb->buf;

    uint32_t msg_seqno = ntohl(hdr->msg_seqno);
    uint32_t data_size = ntohl(hdr->msg_size);
    uint32_t fragment_offset = ntohl(hdr->fragment_offset);
    //    uint16_t fragment_no = ntohs (hdr->fragment_no);
    uint16_t fragments_in_msg = ntohs(hdr->fragments_in_msg);
    uint32_t frag_size = sz - sizeof(lcm2_header_long_t);
    char *data_start = (char *) (hdr + 1);

    // any existing fragment buffer for this message source?
    lcm_frag_key_t key;
    key.from = (struct sockaddr_in *) &(lcmb->from);
    key.msg_seqno = msg_seqno;
    lcm_frag_buf_t *fbuf = lcm_frag_buf_store_lookup(lcm->frag_bufs, &key);

    // discard any stale fragments from previous messages
    if (fbuf && (fbuf->data_size != data_size)) {
        lcm_frag_buf_store_remove(lcm->frag_bufs, fbuf);
        dbg(DBG_LCM, "Dropping message (missing %d fragments)\n", fbuf->fragments_remaining);
        fbuf = NULL;
    }

    //    printf ("fragment %d/%d (offset %d/%d) seq %d packet sz: %d %p\n",
    //        ntohs(hdr->fragment_no) + 1, fragments_in_msg,
    //        fragment_offset, data_size, msg_seqno, sz, fbuf);

    if (data_size > LCM_MAX_MESSAGE_SIZE) {
        dbg(DBG_LCM, "rejecting huge message (%d bytes)\n", data_size);
        return 0;
    }

    // if this is the first packet, set some values
    char *channel = NULL;
    int channel_sz = 0;
    if (hdr->fragment_no == 0) {
        channel = (char *) (hdr + 1);
        channel_sz = strlen(channel);
        if (channel_sz > LCM_MAX_CHANNEL_NAME_LENGTH) {
            dbg(DBG_LCM, "bad channel name length\n");
            lcm->udp_discarded_bad++;
            return 0;
        }
        data_start += channel_sz + 1;
        frag_size -= (channel_sz + 1);
    }

    if (!fbuf) {
        fbuf = lcm_frag_buf_new(*((struct sockaddr_in *) &lcmb->from), msg_seqno, data_size,
                                fragments_in_msg, lcmb->recv_utime);
        lcm_frag_buf_store_add(lcm->frag_bufs, fbuf);
    }

    if (channel != NULL) {
        memcpy(fbuf->channel, channel, channel_sz + 1);
    }

    // fragment_offset and frag_size are both uint32_t
    // values taken straight off the wire. The most straightforward check
    // (fragment_offset + frag_size > fbuf->data_size) can wrap around when
    // fragment_offset is near UINT32_MAX, letting an invalid fragment slip
    // through and causing memcpy() below to write far outside fbuf->data.
    if (fragment_offset > fbuf->data_size || frag_size > fbuf->data_size - fragment_offset) {
        dbg(DBG_LCM, "dropping invalid fragment (off: %d, %d / %d)\n", fragment_offset, frag_size,
            fbuf->data_size);
        lcm_frag_buf_store_remove(lcm->frag_bufs, fbuf);
        return 0;
    }

    // copy data
    memcpy(fbuf->data + fragment_offset, data_start, frag_size);
    fbuf->last_packet_utime = lcmb->recv_utime;

    fbuf->fragments_remaining--;

    if (0 == fbuf->fragments_remaining) {
        // complete message received.  Is there a subscriber that still
        // wants it?  (i.e., does any subscriber have space in its queue?)
        if (!lcm_try_enqueue_message(lcm->lcm, fbuf->channel)) {
            // no... sad... free the fragment buffer and return
            lcm_frag_buf_store_remove(lcm->frag_bufs, fbuf);
            return 0;
        }

        // yes, transfer the message into the lcm_buf_t

        // deallocate the ringbuffer-allocated buffer
        g_rec_mutex_lock(&lcm->mutex);
        lcm_buf_free_data(lcmb, lcm->ringbuf);
        g_rec_mutex_unlock(&lcm->mutex);

        // transfer ownership of the message's payload buffer
        lcmb->buf = fbuf->data;
        fbuf->data = NULL;

        strcpy(lcmb->channel_name, fbuf->channel);
        lcmb->channel_size = strlen(lcmb->channel_name);
        lcmb->data_offset = 0;
        lcmb->data_size = fbuf->data_size;
        lcmb->recv_utime = fbuf->last_packet_utime;

        // don't need the fragment buffer anymore
        lcm_frag_buf_store_remove(lcm->frag_bufs, fbuf);

        return 1;
    }

    return 0;
}
~~~

以上固定摘录保留了实际分支、字段操作、边界检查、复制和所有权转移；为避开源注释中的外部链接，省略了只打印 Linux 内核接收缓冲区提示的条件编译块。调用者给 sz 的前提是 datagram 至少通过短 header 长度检查；长 header 在格式上更大，解析前还依赖该协议入口正确。data_size 有最大消息长度检查，fragment_offset 与当前片长度则用“先比较 offset，再比较 size 是否大于剩余范围”的方式避免无符号加法溢出。

计数法会在分片重复时出错。例如总片数为 3，片 0、片 0、片 2 到达后，计数依次变成 2、1、0；实际缺少片 1，代码仍会把 buffer 判为完整并交给 decoder。可观察到的结果是解码失败，或者解码器读到那段从未写入的 heap 内容。固定代码没有 fragment_no bitmap 去证明每个编号只计一次，因此它依赖网络不会重复这一更强假设。教学复刻面对不可信报文时，应验证总片数与编号范围，用 bitmap 只在某编号首次到达时增加 count，并规定重叠区间怎么处理。

### 让重复片真正触发一次“假的重组成功”

上面的结论不必凭想象。下面是一个 40 行的**独立 C++17 教学实验**，把固定实现的“只减剩余片数”与显式记录 fragment number 的方案并排运行。这里不需要真实 socket，因为要证明的错误完全发生在数据结构与状态转移层。

~~~cpp
#include <array>
#include <cassert>
#include <iostream>

struct CountOnly {
    int remaining = 3;
    void on_fragment(unsigned /*number*/) { --remaining; }
    bool complete() const { return remaining == 0; }
};

struct Bitmap {
    std::array<bool, 3> received{};
    int unique_count = 0;
    void on_fragment(unsigned number) {
        if (number >= received.size() || received[number]) return;
        received[number] = true;
        ++unique_count;
    }
    bool complete() const { return unique_count == 3; }
};

int main() {
    CountOnly naive;
    Bitmap checked;
    for (unsigned number : {0U, 0U, 2U}) {
        naive.on_fragment(number);
        checked.on_fragment(number);
    }
    assert(naive.complete());    // 错误地以为三个分片都到齐
    assert(!checked.complete()); // 编号 1 仍未收到
    checked.on_fragment(1U);
    assert(checked.complete());
    std::cout << "count-only: false complete; bitmap: verified" << '\n';
}
~~~

这份实验只演示**接收进度证明**，不是完整的安全重组器。实际网络输入还要检查同一片编号对应的 offset/length 是否一致、不同片是否恶意重叠、累加长度是否等于原始数据大小，以及单个来源占用多少未完成消息内存。即使加入 bitmap，若尚未限制这些边界，也不能宣称“恶意 UDP 数据报已被安全处理”。

还有一个很具体的边界条件：lcm_frag_buf_store_add() 在插入之前检查当前总字节数和现有条目数，故达到阈值时再添加一个刚好合法的新消息后，账面用量可以越过阈值一项。新片的 data 在入表前已经 malloc(data_size)，因此淘汰旧项和新分配可能短暂同时占内存。

看 `lcm_frag_buf_store_add()` 如何维护分片内存配额与淘汰旧项：


~~~c
void lcm_frag_buf_store_add(lcm_frag_buf_store *store, lcm_frag_buf_t *fbuf)
{
    while (store->total_size > store->max_total_size ||
           g_hash_table_size(store->frag_bufs) > store->max_n_frag_bufs) {
        // find and remove the least recently updated fragment buffer
        lcm_frag_buf_t *lru_fbuf = NULL;
        g_hash_table_foreach(store->frag_bufs, _find_lru_frag_buf, &lru_fbuf);
        if (lru_fbuf) {
            lcm_frag_buf_store_remove(store, lru_fbuf);
        }
    }
    g_hash_table_insert(store->frag_bufs, &fbuf->key, fbuf);
    store->total_size += fbuf->data_size;
}
~~~

循环条件是“当前已经超过”，不是“添加后会超过”。这使 store 的配置值成为淘汰触发线，而不是严格的分配前硬上限。连续发送最大大小的未完成消息时，峰值包含超限的新分配和被淘汰前的旧项；若安全边界要求硬限制，复刻实现应在分配前用溢出安全的 current <= max - requested 判定，并为半包设置 TTL。

哈希表只由 recv_thread 的调用链访问：查找、插入、淘汰与移除都在接收线程中顺序发生，没有通过 lcm->mutex 保护 fragment store。这个设计依赖单接收线程串行化这些操作；若未来改成多个接收 worker 并行解析，就必须为 store 增加独立锁或分片，并重新规定锁内是否允许分配、淘汰和 callback 通知。不能从“provider 有一把 mutex”推出“全部成员都受这把 mutex 保护”。

删除分片项还涉及值对象的生存期。固定提交创建 hash table 时把 value destroy callback 设为 lcm_frag_buf_destroy；移除 key 会同步销毁对应 fbuf。因此，在 data_size 不匹配的分支中，先 remove 再从 fbuf 读取剩余分片数，是一个条件性 use-after-free：只有启用了调试输出、并且 DBG_LCM 模式实际开启时，dbg 的参数表达式才会求值；这时读的是刚释放对象的字段。比如同一发送端的序号复用，但新消息长度改变，接收端会进入这个分支。正常构建也会丢弃旧消息重建；开启该诊断路径则可能打印错误数字或触发未定义行为。修复方式是先把 fragments_remaining 复制到局部变量再 remove，再打印局部值。


~~~c
    store->frag_bufs = g_hash_table_new_full(_lcm_frag_key_hash, _lcm_frag_key_equal, NULL,
                                             (GDestroyNotify) lcm_frag_buf_destroy);

void lcm_frag_buf_destroy(lcm_frag_buf_t *fbuf)
{
    free(fbuf->data);
    free(fbuf);
}

void lcm_frag_buf_store_remove(lcm_frag_buf_store *store, lcm_frag_buf_t *fbuf)
{
    store->total_size -= fbuf->data_size;
    g_hash_table_remove(store->frag_bufs, &fbuf->key);
}
~~~

## 完成之后，数据由谁释放

当最后一片让计数归零时，LCM 先确认至少一个匹配订阅仍有排队额度，然后释放当前 UDP datagram 的 ring 区域，再把 fbuf->data 指针交给 lcmb->buf。紧接着把源指针设成 NULL，使 lcm_frag_buf_store_remove() 销毁分片项时不会再释放 payload。随后该 lcm_buf_t 进入 filled queue，由 lcm_handle() 线程在 callback 返回后回收。

`lcm_udpm_handle()` 的真实收尾代码解释了 payload 为什么只能借给同步 callback：


~~~c
static int lcm_udpm_handle(lcm_udpm_t *lcm)
{
    int status;
    char ch;
    if (0 != _setup_recv_parts(lcm))
        return -1;

    /* Read one byte from the notify pipe.  This will block if no packets are
     * available yet and wake up when they are. */
    status = lcm_internal_pipe_read(lcm->notify_pipe[0], &ch, 1);
    if (status == 0) {
        fprintf(stderr, "Error: lcm_handle read 0 bytes from notify_pipe\n");
        return -1;
    } else if (status < 0) {
        fprintf(stderr, "Error: lcm_handle read: %s\n", strerror(errno));
        return -1;
    }

    /* Dequeue the next received packet */
    g_rec_mutex_lock(&lcm->mutex);
    lcm_buf_t *lcmb = lcm_buf_dequeue(lcm->inbufs_filled);

    if (!lcmb) {
        fprintf(stderr, "Error: no packet available despite getting notification.\n");
        g_rec_mutex_unlock(&lcm->mutex);
        return -1;
    }

    /* If there are still packets in the queue, put something back in the pipe
     * so that future invocations will get called. */
    if (!lcm_buf_queue_is_empty(lcm->inbufs_filled))
        if (lcm_internal_pipe_write(lcm->notify_pipe[1], "+", 1) < 0)
            perror("write to notify");
    g_rec_mutex_unlock(&lcm->mutex);

    lcm_recv_buf_t rbuf;
    rbuf.data = (uint8_t *) lcmb->buf + lcmb->data_offset;
    rbuf.data_size = lcmb->data_size;
    rbuf.recv_utime = lcmb->recv_utime;
    rbuf.lcm = lcm->lcm;

    if (lcm->creating_read_thread) {
        // special case:  If we're creating the read thread and are in
        // self-test mode, then only dispatch the self-test message.
        if (!strcmp(lcmb->channel_name, SELF_TEST_CHANNEL))
            lcm_dispatch_handlers(lcm->lcm, &rbuf, lcmb->channel_name);
    } else {
        lcm_dispatch_handlers(lcm->lcm, &rbuf, lcmb->channel_name);
    }

    g_rec_mutex_lock(&lcm->mutex);
    lcm_buf_free_data(lcmb, lcm->ringbuf);
    lcm_buf_enqueue(lcm->inbufs_empty, lcmb);
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
}
~~~

源码中的 lcm_recv_buf_t 是栈对象，rbuf.data 只是指向 lcmb 所持内存的借用视图，不转移所有权。分发结束后，lcm_buf_free_data() 按 lcmb->ringbuf 是否为空选择 ring deallocate 或 free()，然后描述符回到 empty queue。callback 把裸 payload 指针保存到另一个线程并在 callback 返回后读取，就是悬空引用；若要异步处理，必须在回调内复制字节，或 decode 成拥有自身内存的数据结构。

释放实现使用分配来源字段来选择 allocator：


~~~c
void lcm_buf_free_data(lcm_buf_t *lcmb, lcm_ringbuf_t *ringbuf)
{
    if (!lcmb->buf)
        return;
    if (lcmb->ringbuf) {
        lcm_ringbuf_dealloc(lcmb->ringbuf, lcmb->buf);

        // if the packet was allocated from an obsolete and empty ringbuffer,
        // then deallocate the old ringbuffer as well.
        if (lcmb->ringbuf != ringbuf && !lcm_ringbuf_used(lcmb->ringbuf)) {
            lcm_ringbuf_free(lcmb->ringbuf);
            dbg(DBG_LCM, "Destroying unused orphan ringbuffer %p\n", lcmb->ringbuf);
        }
    } else {
        free(lcmb->buf);
    }
    lcmb->buf = NULL;
    lcmb->buf_size = 0;
    lcmb->ringbuf = NULL;
}
~~~

ring buffer 因容量用尽而被替换时，仍被未归还消息引用的旧 ring 暂时成为 orphan；最后一条旧 buffer 归还时才释放它。只存 buf 裸指针不够，因为同一个描述符既可能指向 ring 分配，也可能指向 malloc 的完整重组 payload；ringbuf 字段就是释放时必须维持的不变量。若新写一套 allocator，却在所有路径上都用 free()，轻则破坏 ring 的队列内部状态，重则释放无效地址。

完整消息由接收线程搬到 filled queue，业务 callback 在调用 lcm_handle() 的线程执行。lcm_handle() 一次只消费一条消息，并同步运行该条匹配 handler；如果 callback 处理时间超过消息到达间隔，filled queue 和 subscription 计数会积压。换成额外工作线程虽能隔离业务耗时，但复制/移动 payload 后必须定义队列上限、丢弃策略与关闭 join 顺序。

## 先把生命周期写成执行时序，才能理解关闭顺序

使用者调用 `lcm_destroy()` 时，很容易只想到关 socket，却忘记另一条线程可能正从 `recvmsg()` 返回，应用线程也可能仍在 `handle()` 里面执行用户 callback。此时单独释放 `lcm_t` 或 provider 不是完整的关闭协议：线程、队列、payload 分配器和 userdata 分别持有不同的引用关系。

~~~text
控制线程                   receiver thread                 handle thread
request_stop
   |                       可能正阻塞 select/recv           可能正执行 callback
   | 唤醒接收线程                |
   +--------------------------->退出循环
   |                             |
   | join -----------------------+
   |
   |                  等 handle 线程完成当前 callback
   +---------------------------------------------------> return
   |
   | 取消剩余订阅、释放队列、分片表与 provider
   v
安全销毁业务 Handler 与 LCM
~~~

上面是面向应用的**建议停机协议**，不是说固定版本 `lcm_destroy()` 会自动等待所有外部应用线程。尤其要区分：provider 能管理自己创建的 receiver thread，但无法代替应用管理正在调用 `handle()` 的线程，更不能保证用户在另一个线程中保存的 callback userdata 依然有效。

有一项内存约束在正常收包时不明显，只有停机时才会暴露：当一条完整消息已经入队，描述符的 `buf` 可能来自当前 ring、已被替换的旧 ring，也可能来自独立 heap。释放描述符之前必须按**这条消息自己的来源字段**释放 payload；旧 ring 的已用量降到零，才能最终销毁那一整代分配器。若仅遍历队列后统一 `free(buf)`，不仅可能把 ring 内部地址错当成 heap 指针，还可能过早销毁仍被其他描述符使用的存储。

所以测试不仅应覆盖“正常收到一条 LC02”，还应制造：最后一片始终不来、接收线程已建立但没有任何消息、callback 中途请求停机、ring 满后更换代际以及关闭时仍存在 filled 描述符。每种场景都应记录最后一次回收是否成功，以及是否有线程仍持有被销毁的对象。

## 关闭是一条有失败分支的协议

正常关闭时 _destroy_recv_parts() 往退出 pipe 写一个字节，然后 g_thread_join() 等接收线程结束；只有 join 返回后，才关闭 socket 和 pipes、销毁分片表、释放两个队列和 ring。写字节会让 select() 的等待条件满足；线程实际执行退出分支后，join 才完成。


~~~c
static void _destroy_recv_parts(lcm_udpm_t *lcm)
{
    if (lcm->thread_created) {
        // send the read thread an exit command
        int wstatus = lcm_internal_pipe_write(lcm->thread_msg_pipe[1], "\0", 1);
        if (wstatus < 0) {
            perror(__FILE__ " write(destroy)");
        } else {
            g_thread_join(lcm->read_thread);
        }
        lcm->read_thread = NULL;
        lcm->thread_created = 0;
    }

    if (lcm->thread_msg_pipe[0] >= 0) {
        lcm_internal_pipe_close(lcm->thread_msg_pipe[0]);
        lcm_internal_pipe_close(lcm->thread_msg_pipe[1]);
        lcm->thread_msg_pipe[0] = lcm->thread_msg_pipe[1] = -1;
    }

    if (lcm->recvfd >= 0) {
        lcm_close_socket(lcm->recvfd);
        lcm->recvfd = -1;
    }

    if (lcm->frag_bufs) {
        lcm_frag_buf_store_destroy(lcm->frag_bufs);
        lcm->frag_bufs = NULL;
    }

    if (lcm->inbufs_empty) {
        lcm_buf_queue_free(lcm->inbufs_empty, lcm->ringbuf);
        lcm->inbufs_empty = NULL;
    }
    if (lcm->inbufs_filled) {
        lcm_buf_queue_free(lcm->inbufs_filled, lcm->ringbuf);
        lcm->inbufs_filled = NULL;
    }
    if (lcm->ringbuf) {
        lcm_ringbuf_free(lcm->ringbuf);
        lcm->ringbuf = NULL;
    }
}

static void lcm_udpm_destroy(lcm_udpm_t *lcm)
{
    dbg(DBG_LCM, "closing lcm context\n");
    _destroy_recv_parts(lcm);

    if (lcm->sendfd >= 0)
        lcm_close_socket(lcm->sendfd);

    lcm_internal_pipe_close(lcm->notify_pipe[0]);
    lcm_internal_pipe_close(lcm->notify_pipe[1]);

    g_rec_mutex_clear(&lcm->mutex);
    g_mutex_clear(&lcm->transmit_lock);
    if (lcm->p_create_read_thread_mutex) {
        g_mutex_clear(&lcm->create_read_thread_mutex);
        lcm->p_create_read_thread_mutex = NULL;
        g_cond_clear(&lcm->create_read_thread_cond);
    }
    free(lcm);
}
~~~

这里还存在一个需要严谨保留的失败分支：若 pipe 写入失败，固定代码打印错误，但跳过 join，继续关闭并释放接收资源。若此时接收线程仍在使用这些成员，就会出现释放后访问；因此不能把该路径描述为“错误也会安全等待线程”。安全改进应保证停止通知可重试，或在写失败后使用另一种可靠中断手段，并且只有确认 worker 已退出才释放其共享状态。另一方面，正常路径的 join 只等待 UDPM 接收线程，并不替应用调用者同步正在运行的 lcm_handle() / callback；程序应先停止并 join 自己的 handle loop，再销毁 provider 和 callback userdata。

## 从普通 vector 开始复刻

先别急着复刻 ring allocator。最小版本可以用两种明晰的数据对象：


~~~cpp
struct Partial {
    Endpoint source;
    std::uint32_t sequence;
    std::vector<std::byte> payload;
    std::vector<std::uint8_t> received;
    std::size_t received_count = 0;
    std::chrono::steady_clock::time_point last_update;
};

struct Complete {
    std::string channel;
    std::vector<std::byte> payload;
    std::int64_t received_time_us;
};
~~~

按以下顺序实现和观察：

1. 用单线程接收短 UDP 包，验证长度、magic、源地址和 channel 范围。
2. 增加 partial map，key 由 source endpoint 与 sequence 组成；使用 bitmap 处理重复、乱序片，并在收到超时或超限时记录明确原因。
3. 让接收 worker 只形成 complete message 并入有界队列，不运行业务 callback。
4. 给一个独立 handle loop 使用条件变量或 pipe 等待队列非空；记录入队时刻、callback 开始时刻和结束时刻，以区分通知延迟与业务耗时。
5. 关闭时先停止接收、唤醒阻塞 worker、join，再释放其队列和 partial map；停止 handle loop 后才能释放 callback 所需对象。
6. 只有复制成本确实成为测量瓶颈后，再引入环形分配器，并用 allocator tag 明确每段内存如何释放。

如果 1 kHz 的关节状态 callback 需要 3 ms，单一 handle 线程的理论服务率已经低于输入率，队列最终会积压；加线程只能暂时增加吞吐，不能让无界 backlog 成为正确设计。应选择明确的过载策略：丢旧保新、丢新保旧、按 channel 分配配额或使 producer 降速。对控制环而言，“处理完所有历史姿态”常常比丢掉旧样本更危险，因为控制器会追着过期状态执行。
