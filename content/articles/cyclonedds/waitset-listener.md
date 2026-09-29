# WaitSet 与 Listener：谁真正唤醒应用线程

固定源码：e54e991f75a3e67f8e628da3171122e36ea5b872。

## 两种通知模型不能混为一谈

DDS 常见两种应用消费方式：

~~~text
Listener
middleware 主动调用 callback

WaitSet
application thread 主动阻塞等待 condition
~~~

两者都依赖 Entity status/condition，但线程语义完全不同。

## WaitSet 内部就是 Mutex + Condition Variable + Attachment Array

dds_create_waitset() 初始化：

~~~c
ddsrt_mutex_init(&waitset->wait_lock);
ddsrt_cond_etime_init(&waitset->wait_cond);

waitset->nentities = 0;
waitset->ntriggered = 0;
waitset->entities = NULL;
~~~

attachment 使用动态数组；attach 时通过 realloc 扩容。这里没有复杂 lock-free queue，因为 attachment 集合通常小、变更频率低，而 wait 时需要稳定遍历。

## attach 为什么还要注册 Observer

dds_waitset_attach() 最终调用 entity observer registration。被观察 Entity 状态变化时，observer callback 进入 WaitSet：

~~~c
static void dds_waitset_observer(
  struct dds_waitset *ws,
  dds_entity_t observed)
{
  ddsrt_mutex_lock(&ws->wait_lock);

  /* move observed entity
     into triggered prefix */

  ddsrt_cond_etime_broadcast(
    &ws->wait_cond);

  ddsrt_mutex_unlock(&ws->wait_lock);
}
~~~

因此 WaitSet 不是主动轮询每个 Reader，它让 Entity 状态变化反向唤醒 WaitSet。

## triggered prefix 是一个很实用的数组技巧

WaitSet entities 数组前 ntriggered 个槽位表示当前已触发实体。某个后部元素触发时，通过 swap 移入前缀。

优点是：

~~~text
trigger
O(n) 找目标 + O(1) swap

wait 返回
直接遍历 [0, ntriggered)
~~~

对于 attachment 数量通常几十以内的 ROS 2 executor waitset，这种结构比维护多层动态容器更直接。

## 真正阻塞在哪里

dds_waitset_wait_impl() 的核心条件：

~~~c
while (ws->nentities > 0 &&
       ws->ntriggered == 0 &&
       !dds_handle_is_closed(
         &ws->m_entity.m_hdllink))
{
  if (!ddsrt_cond_etime_waituntil(
        &ws->wait_cond,
        &ws->wait_lock,
        abstimeout))
    break;
}
~~~

所以应用线程最终睡在 condition variable，而不是在 UDP socket 上。

Receive thread 将样本推进 Reader/RHC，Reader condition 变化，再经过 observer 唤醒 application wait thread。

## 为什么关闭 WaitSet 要 Broadcast

如果一个线程无限等待，而另一个线程删除 WaitSet，单纯释放对象会造成悬挂等待。

固定源码 interrupt path：

~~~c
ddsrt_mutex_lock(&ws->wait_lock);
ddsrt_cond_etime_broadcast(
  &ws->wait_cond);
ddsrt_mutex_unlock(&ws->wait_lock);
~~~

关闭协议首先让阻塞线程重新获得执行机会，看到 handle 已 closed 后退出。

## Listener 的线程必须单独追

Listener 是 callback 模型。callback 可能由 delivery path 触发，而 delivery path 可能来自：

- receive thread synchronous delivery；
- user delivery queue thread；
- local delivery；
- PSMX callback path。

所以不要形成“DDS listener 都在某个固定 listener thread”这种错误模型。

## WaitSet 为什么特别适合 ROS 2 Executor

ROS 2 executor 要同时等待 subscription、guard condition、service、client 和 event。把这些都映射为一个 WaitSet attachments 集合，就能使用同一 blocking primitive 睡眠，再按 triggered indices 选择 ready entity。

后面的 rmw_cyclonedds 案例会直接看到这条映射。
