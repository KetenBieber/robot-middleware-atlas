# 从 UDP 数据报到订阅回调：LCM 的接收、重组与缓冲区归还

机械臂控制进程每毫秒发布一次关节状态，同时接收相机标定和地图更新。最直接的接收循环会在 recvfrom() 后立刻解析并调用用户 callback。若地图 callback 偶尔做 20 ms 的文件写入，这个线程就有 20 个毫秒收不到 UDP；内核 socket 接收缓冲区填满后会丢包，控制端看到的 sequence gap 便不再是网络本身造成，而是自己的磁盘工作挡住了收包。

要修复它，先得把“收包”和“处理业务”拆开：网络接收线程尽可能快地从 socket 取数据、识别短消息或重组长消息，然后把完整消息放到进程内队列；调用 lcm_handle() 的应用线程再取出消息并同步运行 callback。拆开之后仍然有几个不同的时刻：UDP 数据报进入内核 socket 缓冲区、recvmsg() 将数据复制进用户态、分片成为完整消息、完整消息进入 inbufs_filled、通知 pipe 写入字节、handle 线程从内核阻塞中变为可运行、操作系统将 CPU 分给它、callback 才真正开始执行。把这些步骤都叫作“消息被唤醒”会掩盖积压究竟发生在哪一层。

本文按 lcm-proj/lcm 固定提交 ad0c54cee0ec048ef12357c34349ec1443158864 展开。文中的固定源码摘录均来自这个提交；它们用于解释真实控制流，不是教学代码。

## 先让接收端拥有可复用的状态

每个 UDPM provider 都要保留接收 socket、两条 buffer 队列、ring buffer、接收线程、两条通知 pipe 和未完成分片表。notify_pipe 通知应用线程有完整消息可取；thread_msg_pipe 单独用于通知 receiver 退出。前者随 provider 建立，后者与接收资源一起延迟创建。它们的方向不同，不能合并成一个含糊的“事件 fd”。

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：struct _lcm_provider_t

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

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_buf_t 与 lcm_frag_buf_t

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


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_buf_queue_t、lcm_buf_queue_new()、lcm_buf_dequeue()、lcm_buf_enqueue()

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

lcm_buf_t::buf 是一个裸指针，单看它并不能判断该调用 ring deallocate 还是 free()；ringbuf 字段正是分配来源标签，空值表示 buffer 由堆分配。lcm_frag_buf_t::data 则暂时由分片项拥有，完整消息形成后才转给 lcm_buf_t。这里不是 shared_ptr，没有引用计数：每个阶段都要明确只有一个负责释放它的对象。

接收时先从 empty queue 取一个描述符，再给它分配最大 UDP datagram 区域。描述符池空了就补一批；ring 空间不够时，源码创建更大的新 ring，但把仍被旧消息使用的 ring 暂时留存，并在每个描述符上记录实际分配来源：


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_buf_allocate_data()

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

结构体描述符的补充并没有队列最大项数，ring 满后也会扩容；因此这部分适合减少频繁分配，却不能当成绝对内存上限。每个接收 buffer 会先分配 65536 字节，recvmsg 最多写入 65535 字节，最后一字节保留零值以保证字符串扫描有终点。短消息路径随后会把最近一次 ring 分配收缩到实际 datagram 长度；长消息的 payload 则另由分片 buffer 暂时保存。

## 第一次需要接收时才建立线程

只发布、不订阅的进程不必创建接收线程。第一次 handle 或其他接收入口触发 _setup_recv_parts() 时，它才打开组播 socket、加入组、创建队列和 ring、预置 buffer 描述符、创建退出 pipe 并启动线程。延迟初始化节省了 publish-only 进程的资源，但引入了一个并发问题：两个线程可能同时成为“第一次订阅者”。

GCond 是带条件谓词的等待机制。线程不能把“收到 signal”当成谓词已经成立，因为信号可能在检查之前发出，也可能发生虚假唤醒。正确的用法是：持有 mutex 检查共享状态；条件不满足时在循环中 wait；wait 原子地释放 mutex 并阻塞；醒来后重新取得 mutex，再检查谓词。LCM 用 creating_read_thread 表示创建过程是否结束，并用 thread-local 标记识别创建者的重入调用。

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：_setup_recv_parts() 的并发初始化等待分支

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

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：udp_read_packet() 中等待输入与处理退出命令的循环

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


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：udp_read_packet() 的 ring-buffer 收缩分支

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

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：recv_thread()

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

## LC03 的 key、分片计数和边界检查

短消息可用一个 UDP datagram 装下；LC03 长消息由多片组成。接收方需要把同一个源地址和 msg_seqno 的片放进同一个未完成项。若只按序号索引，两个发送者恰好使用相同序号时会把不同消息混在一起；固定代码把 IPv4 地址、UDP 端口和消息序号一起作为 key。

下面是 _recv_message_fragment() 的固定源码。它也揭示了此版本的一个重要约束：字段 fragments_remaining 只减计数，没有按 fragment_no 设置 bitmap 去重。


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：_recv_message_fragment()

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

还有一个很具体的边界条件：lcm_frag_buf_store_add() 在插入之前检查当前总字节数和现有条目数，故达到阈值时再添加一个刚好合法的新消息后，账面用量可以越过阈值一项。新片的 data 在入表前已经 malloc(data_size)，因此淘汰旧项和新分配可能短暂同时占内存。

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_frag_buf_store_add()

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


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_frag_buf_store_new()、lcm_frag_buf_destroy()、lcm_frag_buf_store_remove()

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

对应的上游实现如下：

仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_udpm_handle()

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


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：lcm_buf_free_data()

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

## 关闭是一条有失败分支的协议

正常关闭时 _destroy_recv_parts() 往退出 pipe 写一个字节，然后 g_thread_join() 等接收线程结束；只有 join 返回后，才关闭 socket 和 pipes、销毁分片表、释放两个队列和 ring。写字节会让 select() 的等待条件满足；线程实际执行退出分支后，join 才完成。


仓库与提交：lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864  
符号：_destroy_recv_parts() 与 lcm_udpm_destroy()

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
