# 订阅与分发：正则缓存、队列配额和回调期删除

机械臂状态 `ARM_STATE` 同时送给三个订阅者：控制器 1 ms 内必须看到新状态，记录器可以稍慢，可视化 callback 偶尔需要 15 ms 绘图。若初学者为每个订阅复制一份 payload，再让所有 callback 在同一个循环里无界执行，慢图形会让控制状态晚到，消息副本还会按订阅数和积压一起增长。Provider 交给核心层的是一条完整消息：channel、payload 视图和接收时间。核心层随后要决定哪些 subscription 能看见它、哪些订阅已经积压过多，以及 callback 执行期间取消订阅是否安全。

本文固定分析 `lcm-proj/lcm@ad0c54cee0ec048ef12357c34349ec1443158864`，重点符号是 `lcm_subscribe()`、`lcm_try_enqueue_message()`、`lcm_unsubscribe()` 与 `lcm_dispatch_handlers()`。它们展示一个小型事件分发器必须处理的三个问题：匹配成本、过载隔离和可重入生命周期。

下文讨论的锁是核心对象 `lcm_t::mutex`：它保护 `handlers_all`、`handlers_map`、subscription 的排队计数和延迟删除标记。`handle_mutex` 只把同一实例的 `lcm_handle()` 串成一个分发者；provider 的 `lcm_udpm_t::mutex` 则保护接收缓冲队列与 ring，二者不要混成一把锁。固定版本的 `lcm_handle()` 持有 `handle_mutex` 调用 provider 的 `handle`，而 callback 由这个调用栈同步执行。

## subscription 的状态

对应的上游实现如下：

```c
struct _lcm_subscription_t {
    char *channel;
    lcm_msg_handler_t handler;
    void *userdata;
    lcm_t *lcm;
    GRegex *regex;

    int callback_scheduled;
    int marked_for_deletion;

    int max_num_queued_messages;
    int num_queued_messages;
};
```

这段定义揭示两个不同的所有权约定：`channel` 和 `regex` 是 subscription 自己创建并由 `lcm_handler_free()` 释放的；`handler` 是函数地址；`userdata` 只借给 LCM 使用，不复制、不析构。末尾两个计数保护消息准入，两个标记则保护 callback 遍历期间的节点生命期。

userdata 的寿命由调用者负责。LCM 不知道它指向栈、堆还是静态对象；`unsubscribe()` 返回不等于已经没有 callback 正在使用这个指针。若该订阅正在 dispatch，固定实现只设置延迟删除标记并返回，物理回收要等这一轮 dispatch 结束；跨线程释放 userdata 前必须停止并 join 唯一 handle 线程，或另行建立能够等待在途 callback 结束的屏障。下面逐行看订阅节点装配与取消的真实控制流。

## 两级订阅容器

`lcm_t` 同时维护：

```text
handlers_all:  [subscription*, ...]
handlers_map:  actual channel -> [matching subscription*, ...]
```

`handlers_all` 是权威集合。`handlers_map` 是按已经出现过的具体 channel 建立的匹配缓存。

例如订阅：

```text
"POSE_.*"
"POSE_FRONT"
"LIDAR_.*"
```

第一次收到 `POSE_FRONT` 时，核心遍历全部 subscription 做 regex match，并缓存两个结果。后续同名消息直接查询 hash table。

这避免每条消息都执行所有正则，但缓存大小随实际出现过的 channel 名数量增长。若外部可以发送无限随机 channel，必须考虑 channel cache 的内存上界。

## subscribe 的装配顺序

`lcm_subscribe()` 先通知 provider，再分配核心 subscription：

```text
provider->subscribe(channel)
  -> allocate subscription
  -> duplicate channel
  -> compile ^pattern$
  -> append handlers_all
  -> update all existing handlers_map entries
```

正则通过 `^` 与 `$` 锚定，确保匹配整个 channel。调用者传入的 channel 字符串本身可以包含正则语法。

provider subscribe 成功、但后续正则编译失败时，需要对应的 provider rollback。固定提交摘录如下；输入是调用者传入的 channel、handler 与 userdata，函数先进入 provider，再创建核心节点，最后把节点登记到两级容器。


```c
lcm_subscription_t *lcm_subscribe(lcm_t *lcm, const char *channel, lcm_msg_handler_t handler,
                                  void *userdata)
{
    dbg(DBG_LCM, "registering %s handler %p\n", channel, handler);

    if (lcm->provider && lcm->vtable->subscribe) {
        if (0 != lcm->vtable->subscribe(lcm->provider, channel)) {
            return NULL;
        }
    }

    // create and populate a new message handler struct
    lcm_subscription_t *subscription = (lcm_subscription_t *) calloc(1, sizeof(lcm_subscription_t));
    subscription->channel = strdup(channel);
    subscription->handler = handler;
    subscription->userdata = userdata;
    subscription->callback_scheduled = 0;
    subscription->marked_for_deletion = 0;
    subscription->max_num_queued_messages = lcm->default_max_num_queued_messages;
    subscription->num_queued_messages = 0;
    subscription->lcm = lcm;

    char *regexbuf = g_strdup_printf("^%s$", channel);
    GError *rerr = NULL;
    subscription->regex =
        g_regex_new(regexbuf, (GRegexCompileFlags) 0, (GRegexMatchFlags) 0, &rerr);
    g_free(regexbuf);
    if (rerr) {
        fprintf(stderr, "%s: %s\n", __FUNCTION__, rerr->message);
        dbg(DBG_LCM, "%s: %s\n", __FUNCTION__, rerr->message);
        g_error_free(rerr);
        free(subscription);
        return NULL;
    }
    g_rec_mutex_lock(&lcm->mutex);
    g_ptr_array_add(lcm->handlers_all, subscription);
    g_hash_table_foreach(lcm->handlers_map, map_add_handler_callback, subscription);
    g_rec_mutex_unlock(&lcm->mutex);

    return subscription;
}
```

输入 `channel` 被复制到 `subscription->channel`，handler 和 userdata 则按值保存地址；`regexbuf` 是临时字符串，在编译后释放，编译出的 `GRegex` 由节点持有。成功时 mutex 把“加入权威数组并更新已有 channel 缓存”作为一个事务提交。错误路径值得特别检查：provider hook 已经成功后，正则失败分支只 `free(subscription)`，没有释放 `subscription->channel`，也没有调用 provider 的 `unsubscribe`。因此 TCPQ 可能继续向服务器声明一个核心层已拒绝的 channel，且本地 channel 副本泄漏。复刻时应把后半段装配写成事务：任何失败都按已获取资源的逆序释放，并撤销之前成功的 provider 操作。

## 新订阅反向更新缓存

如果 `handlers_map` 已缓存 `POSE_FRONT`，之后新增 `POSE_.*` 订阅，不能等下一次 channel 首见才更新。代码遍历 map，对所有已知 channel 检查新 regex 并追加匹配项。

因此复杂度分配为：

```text
subscribe: O(number of known concrete channels)
first message of new channel: O(number of subscriptions)
steady message dispatch: O(1) lookup + O(matches)
```

这是典型的读多写少优化。机器人数据面消息远多于运行期 subscribe 操作时，代价分配合理。

## lcm_try_enqueue_message 只预留资格

Provider 完整接收一条消息后，先调用核心层 `lcm_try_enqueue_message()`。输入是目标 channel；函数查已缓存的匹配列表，在同一把核心 mutex 下逐个检查该订阅的待处理计数并为有空间者预留资格。以下是固定版本的完整函数：


```c
int lcm_try_enqueue_message(lcm_t *lcm, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);
    GPtrArray *handlers = lcm_get_handlers(lcm, channel);
    int num_keepers = 0;
    for (unsigned int i = 0; i < handlers->len; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        if (subscription->num_queued_messages < subscription->max_num_queued_messages ||
            subscription->max_num_queued_messages <= 0) {
            subscription->num_queued_messages++;
            num_keepers++;
        }
    }
    g_rec_mutex_unlock(&lcm->mutex);
    return num_keepers > 0;
}
```

这里的 `num_keepers` 是“至少一个订阅有空间”的资格判断，不是 payload 副本数。若它为 0，UDPM provider 释放当前 buffer；若大于 0，接收线程把唯一的完整消息加入 filled list。

这不是传统的“每 subscription 一条 payload queue”。`num_queued_messages` 是资格计数，实际 buffer 位于 provider 的全局 FIFO。分发到该消息时，只调用计数仍大于零的 handler。

## 单副本与订阅者隔离的折中

一份 payload 对多个 handler 的设计节省内存：

```text
one MessageBuffer
  +-> subscription A pending count
  +-> subscription B pending count
  `-> subscription C full, no count increment
```

但全局 FIFO 仍意味着 handler 执行顺序串行。A 很慢时，B 即使有独立配额，也要等待 A callback 返回。

配额隔离的是“为某订阅者保留多少历史”，不是 CPU 执行隔离。需要并行时，callback 应快速转交到应用自有队列；转交时必须复制或 decode，因为 provider payload 在 handle 返回后失效。

## 分发时如何冻结迭代边界，却在锁外执行业务

此时 provider 已经接收完整消息，核心层也知道这条具体 channel 应交给哪些 handler。新的矛盾是：业务 callback 可能很慢，还可能在内部执行 `unsubscribe()`，因此不能一直持有核心订阅表的互斥锁；但如果解锁后让 callback 直接删除正在遍历的 handler，又会发生迭代器失效和悬空指针。

一个朴素的错误版本是 `for (handler : vector) handler(message)`。假设 A、B 两只订阅收到同一帧图像；A 回调中取消 B 并释放它的对象，for 循环下一次访问 B 的指针就可能触发 use-after-free。给整轮循环加锁也不是完整答案，因为用户代码可能反过来调用订阅接口、执行文件 I/O，或与接收线程争同一把锁。

LCM 采用**冻结当前迭代边界、逻辑删除、最后集中回收**三阶段。下面给出固定提交的完整连续 `lcm_dispatch_handlers()`，没有把重要的锁域和回收分支藏进教学伪代码：

~~~c
int lcm_dispatch_handlers(lcm_t *lcm, lcm_recv_buf_t *buf, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);

    GPtrArray *handlers = lcm_get_handlers(lcm, channel);

    // ref the handlers to prevent them from being destroyed by an
    // lcm_unsubscribe.  This guarantees that handlers 0-(nhandlers-1) will not
    // be destroyed during the callbacks.  Store nhandlers in a local variable
    // so that we don't iterate over handlers that are added during the
    // callbacks.
    int nhandlers = handlers->len;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        subscription->callback_scheduled = 1;
    }

    // now, call the handlers.
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        if (!subscription->marked_for_deletion && subscription->num_queued_messages > 0) {
            subscription->num_queued_messages--;
            g_rec_mutex_unlock(&lcm->mutex);
            subscription->handler(buf, channel, subscription->userdata);
            g_rec_mutex_lock(&lcm->mutex);
        }
    }

    // unref the handlers and check if any should be deleted
    GList *to_remove = NULL;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        subscription->callback_scheduled = 0;
        if (subscription->marked_for_deletion)
            to_remove = g_list_prepend(to_remove, subscription);
    }
    // actually delete handlers marked for deletion
    for (; to_remove; to_remove = g_list_delete_link(to_remove, to_remove)) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) to_remove->data;
        g_ptr_array_remove(lcm->handlers_all, subscription);
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        lcm_handler_free(subscription);
    }
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
}
~~~

第一段在核心 mutex 下得到 channel 的缓存 handler 数组，保存 `nhandlers`，再给这批 subscription 标记 `callback_scheduled=1`。这个标志不是引用计数，也不是“当前就在执行”的状态，而是通知 `unsubscribe()`：本轮迭代仍可能解引用这个对象，所以现在只能逻辑删除，不能移动数组槽位或 `free`。`nhandlers` 冻结数量而非完整副本；在 callback 中新增的订阅不会意外插入本次消息的遍历范围。

第二段逐个判断 `marked_for_deletion` 和配额 `num_queued_messages`。对有资格的节点，先扣掉配额，再 **释放 `lcm->mutex` 执行 `subscription->handler(buf, channel, userdata)`**，完成后重新加锁。这样业务 callback 能够修改订阅表；但本次处理仍发生在调用 `lcm_handle()` 的应用线程上。A 耗时 20 ms，则同一条消息的 B 仍必须等这 20 ms，并不会因独立配额就获得独立线程。真正的 CPU 隔离需要业务 callback 快速复制或 decode 数据后，把工作交给另一个有界应用队列。

第三段解除所有本轮迭代标志，将 `marked_for_deletion` 的对象收集到 `to_remove`，然后从 `handlers_all`、所有 channel 缓存及堆内存中一次性移除。这一安全点保护的是核心内部 `lcm_subscription_t` 节点，不拥有用户提供的 `userdata`。外部管理线程若取消订阅后马上释放 userdata，而 handle 线程仍在执行该回调，依然可能发生悬空访问；需要先停 handle 循环并等待它退出。

这里同时解释了为什么 `lcm_handle()` 用独立 `handle_mutex` 串行化完整处理，又用 `in_handle` 禁止递归 handle：核心允许 callback 调用部分订阅管理函数，但不允许同一 handle 过程递归消费下一条消息来绕过当前批次的回收边界。
## callback 内 unsubscribe 使用延迟删除

如果 handler 在自身 callback 中调用 unsubscribe，立即从数组删除并 free 会让 dispatch 循环持有悬空指针。LCM 检查：


```c
if (subscription->callback_scheduled) {
    subscription->marked_for_deletion = 1;
    return success;
}
```

当前批次结束后，dispatch 清除 scheduled 标记，收集 marked subscription，再统一从 `handlers_all` 和所有 channel cache 删除并 free。

这是 deferred reclamation：逻辑删除立即生效，物理回收推迟到已知没有迭代者的安全点。这里保护的是 C 核心的 `lcm_subscription_t`。下面的固定实现还显示了另一个边界：当前批次中的 unsubscribe 只设置标记并跳过 provider hook。此时输入是被取消的节点；核心 mutex 覆盖标记与容器操作，provider 私有状态并未因此同步改变。


```c
int lcm_unsubscribe(lcm_t *lcm, lcm_subscription_t *subscription)
{
    g_rec_mutex_lock(&lcm->mutex);

    int foundit = 0;

    // when called from within an lcm_handle callback, tampering with the
    // handlers_map will throw off the indices in lcm_dispatch_handlers, so
    // check for the dispatch sentinels and skip the body of this function if
    // they're present.
    if (!subscription || subscription->marked_for_deletion)
        goto done;
    if (subscription->callback_scheduled) {
        subscription->marked_for_deletion = 1;
        foundit = 1;
        goto done;
    }

    // remove the handler from the master list
    foundit = g_ptr_array_remove(lcm->handlers_all, subscription);

    if (lcm->provider && lcm->vtable->unsubscribe) {
        lcm->vtable->unsubscribe(lcm->provider, subscription->channel);
    }

    if (foundit) {
        // remove the handler from all the lists in the hash table
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        if (!subscription->callback_scheduled)
            lcm_handler_free(subscription);
        else
            subscription->marked_for_deletion = 1;
    }

done:
    g_rec_mutex_unlock(&lcm->mutex);

    return foundit ? 0 : -1;
}
```

`callback_scheduled` 分支在设置 `marked_for_deletion` 后直接跳到 `done`，所以这个路径不调用 `vtable->unsubscribe`。这对没有取消 hook 的 UDPM 不明显；TCPQ provider 会保留服务器侧订阅，直到 provider 被重建或销毁。于是“核心不再调用 callback”与“传输层停止接收/转发”是两个不同状态，不能把当前 callback 内 unsubscribe 的成功返回理解成两边都已完成。

C++ wrapper 还有一层更短的寿命：`lcm::LCM::unsubscribe()` 调用 C API 后马上从自己的 `subscriptions` vector 擦除并 `delete` C++ wrapper。该 wrapper 同时保存 `channel_buf` 和成员 handler；成员回调 trampoline 把 `channel_buf` 以 `const std::string&` 传入用户函数。因此若 C++ handler 自行 unsubscribe 后还读 `channel`，引用就指向已释放的 wrapper 成员。工程上可把 unsubscribe 请求投递到 handle loop，在当前 callback 返回后处理；关闭时则先停止并 join handle 线程，再取消剩余订阅。

C++ 中可以用 `shared_ptr<Subscription>` 简化对象寿命，但仍需要定义“当前消息是否继续调用刚取消的订阅”。内存安全不会自动给出事件语义。

把一次 dispatch 从查表直到回收连起来看，才能看到锁保护的状态和对象何时安全释放。`lcm_udpm_handle()` 已从 provider 队列取出一条消息，然后在 provider 锁外调用核心 `lcm_dispatch_handlers()`；因此回调运行在线程调用 `lcm_handle()` 的调用栈中。下面从构造接收视图处摘取连续语句，输入 `lcmb` 是刚从 filled queue 取出的唯一完整消息 buffer。

对应的上游实现如下：

```c
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
```

`rbuf` 是栈上视图，`data` 借用 `lcmb` 中的 payload；这段函数在调用 `lcm_dispatch_handlers()` 时没有持有 provider mutex，callback 因而不会长时间阻塞接收线程队列锁，但仍阻塞当前 `lcm_handle()` 调用。所有 callback 返回后 provider 才重新加锁，释放 payload 对 ring 的占用并把 `lcmb` 节点放回空闲链表。回调保存 `rbuf.data` 到返回之后会读到过期存储。


```c
int lcm_dispatch_handlers(lcm_t *lcm, lcm_recv_buf_t *buf, const char *channel)
{
    g_rec_mutex_lock(&lcm->mutex);

    GPtrArray *handlers = lcm_get_handlers(lcm, channel);

    // ref the handlers to prevent them from being destroyed by an
    // lcm_unsubscribe.  This guarantees that handlers 0-(nhandlers-1) will not
    // be destroyed during the callbacks.  Store nhandlers in a local variable
    // so that we don't iterate over handlers that are added during the
    // callbacks.
    int nhandlers = handlers->len;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);
        subscription->callback_scheduled = 1;
    }

    // now, call the handlers.
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        if (!subscription->marked_for_deletion && subscription->num_queued_messages > 0) {
            subscription->num_queued_messages--;
            g_rec_mutex_unlock(&lcm->mutex);
            subscription->handler(buf, channel, subscription->userdata);
            g_rec_mutex_lock(&lcm->mutex);
        }
    }

    // unref the handlers and check if any should be deleted
    GList *to_remove = NULL;
    for (int i = 0; i < nhandlers; i++) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) g_ptr_array_index(handlers, i);

        subscription->callback_scheduled = 0;
        if (subscription->marked_for_deletion)
            to_remove = g_list_prepend(to_remove, subscription);
    }
    // actually delete handlers marked for deletion
    for (; to_remove; to_remove = g_list_delete_link(to_remove, to_remove)) {
        lcm_subscription_t *subscription = (lcm_subscription_t *) to_remove->data;
        g_ptr_array_remove(lcm->handlers_all, subscription);
        g_hash_table_foreach(lcm->handlers_map, map_remove_handler_callback, subscription);
        lcm_handler_free(subscription);
    }
    g_rec_mutex_unlock(&lcm->mutex);

    return 0;
}
```

入口先在 `lcm->mutex` 下取得 channel 的匹配数组并固定 `nhandlers`，再把这一批订阅标成 scheduled，保证 callback 中新增 handler 不会插入当前遍历。随后每个有待处理资格的订阅先减计数，再暂时解锁执行业务 callback；provider 的 `lcm_buf_t` 在这期间仍由当前 handle 调用持有，所以所有 handler 共用同一份 payload view。callback 返回后重新加锁，把删除标记清除出 authoritative list 与所有 channel cache，最后才释放 C subscription 节点。这只保护核心 subscription；`userdata` 和 C++ wrapper 的所有权仍由应用负责，且该 mutex 不会使跨线程 `lcm_destroy()` 等待外部 handle loop。

## 取消订阅对当前消息的语义

回调 A 若取消尚未执行的回调 B，B 的 `marked_for_deletion` 会变为 true。dispatch 到 B 时检查标记并跳过。

因此取消可以影响同一条消息中排在后面的 handler。实际顺序来自匹配数组插入顺序，不应被业务当作强协议保证。

需要事务式 fan-out 的系统可以先对 handler 强引用快照，再规定当前消息一定投递给快照中的全部订阅；LCM 选择了更直接的即时逻辑删除。

## handle_mutex 与递归限制

同一 `lcm_t` 的所有 `handle()` 由 `handle_mutex` 串行化。另有 `in_handle` 断言禁止 callback 递归调用 handle。

如果允许递归，内层 handle 可能：

- 处理下一条 provider buffer；
- 改变 subscription pending count；
- 删除外层循环仍标记 scheduled 的对象；
- 让 callback 调用栈深度由消息链无界增长。

禁止递归把事件循环保持为明确的一层。callback 可以 publish，因为发布不进入同一分发状态机。

## 正则与缓存的线程安全

订阅增删、首次 channel 缓存建立、pending count 和删除标记都受 `lcm->mutex` 保护。该锁是递归 mutex，因为内部帮助函数可能在已经持锁时再次进入 `lcm_get_handlers()`。

递归锁降低函数组合难度，却也可能掩盖层次不清和意外重入。自行实现时可以采用非递归 mutex，并明确区分：

```text
GetHandlers()        // acquires lock
GetHandlersLocked()  // caller already owns lock
```

这样锁契约更容易静态审查，也能发现同线程重复获取导致的设计问题。

## 队列上限的语义

`max_num_queued_messages <= 0` 表示不设上限。默认值为 30，可按 subscription 修改。

上限为 N 的真实含义是：最多 N 条尚未被该 subscription 消费的 provider 消息为它保留投递资格。达到上限后，新消息对该订阅者被丢弃，旧消息继续等待。

这是 drop-newest 策略，而许多控制系统更希望 drop-oldest、只保留最新状态。LCM 的全局单副本 FIFO 很难为不同订阅者同时执行不同的 payload 淘汰策略；若需要 latest-only，应在 callback 后的应用队列中实现覆盖槽。

## 分发复杂度

已知 channel 的单条消息成本近似为：

```text
O(1) hash lookup
+ O(H) pending-count admission
+ O(H) callback scan
+ sum(callback execution time)
```

其中 H 是匹配 subscription 数。两次 O(H) 通常远小于业务 callback，但大量通配订阅会扩大每条消息成本。

全局 mutex 在 admission 和 dispatch bookkeeping 阶段竞争。callback 在锁外，因此锁持有时间不直接包含业务 WCET。

## 可复刻的数据结构


```cpp
struct Subscription {
  Regex pattern;
  Callback callback;
  std::size_t pending = 0;
  std::size_t limit = 30;
  bool dispatching = false;
  bool remove_requested = false;
};

class SubscriptionIndex {
  std::vector<std::shared_ptr<Subscription>> all_;
  std::unordered_map<std::string,
      std::vector<std::weak_ptr<Subscription>>> cache_;
};
```

使用 weak pointer 的 channel cache 可避免缓存反向延长已取消订阅寿命。dispatch 先锁定一组 shared pointer 快照，再释放 index mutex 执行 callback。

仍需单独实现 pending quota，因为 shared pointer 只解决内存寿命，不解决过载策略。

## 分发层的设计结论

LCM 订阅层通过两级索引把正则成本移到首次 channel 和订阅变更，通过每订阅者计数限制积压，再用锁外 callback 与延迟删除解决可重入生命周期。

最值得复用的不是 GLib 容器，而是四条规则：权威集合与热路径缓存分离；payload 单副本但配额按订阅者统计；永不持内部锁调用用户代码；取消订阅的逻辑生效与物理回收分开处理。
