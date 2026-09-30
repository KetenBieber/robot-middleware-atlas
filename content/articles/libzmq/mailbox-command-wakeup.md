# Mailbox：从 condition_variable 走到 Event Loop Wakeup

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

这一页只研究一个问题：**I/O thread 正在等网络 fd 时，另一个线程怎样安全地让它执行 stop、attach、activate_read 等动作？**

## 先从标准 C++ Producer/Consumer 出发

~~~cpp
std::mutex m;
std::condition_variable cv;
std::queue<Command> q;
~~~

这里 queue 保存真实 command，mutex 保护 queue，condition_variable 负责睡眠/唤醒。问题是 I/O thread 不只等待 queue，它还要同时等待 socket readable/writable、listener、timer。线程不能同时阻塞在 `cv.wait()` 和 `epoll_wait()`。

因此 libzmq 把“有 command 到达”也做成 poller 可观察事件。

## mailbox_t 的四个成员

~~~cpp
typedef ypipe_t<command_t, command_pipe_granularity> cpipe_t;
cpipe_t _cpipe;
signaler_t _signaler;
mutex_t _sync;
bool _active;
~~~

职责分别是：

~~~text
_cpipe     真正保存 command_t
_signaler  只负责 wakeup
_sync      多 sender 的发送侧串行化
_active    reader 当前是否主动消费
~~~

## 为什么还有 mutex

`ypipe` 的前提是 single writer / single reader，但 mailbox 外部可能是 many senders / one receiver。因此发送入口用 mutex 把 MPSC 拓扑降维成 one logical writer：

~~~text
thread A --\
thread B ----> sender mutex -> SPSC ypipe -> receiver
thread C --/
~~~

重点不是“lock-free 到底纯不纯”，而是：先把 topology 变简单，再让底层结构利用这个前提。

## send()：先发布事实，再做通知

~~~cpp
void mailbox_t::send (const command_t &cmd_)
{
    _sync.lock ();
    _cpipe.write (cmd_, false);
    const bool ok = _cpipe.flush ();
    _sync.unlock ();
    if (!ok)
        _signaler.send ();
}
~~~

真实顺序是：lock sender side → command 写入 cpipe → flush → unlock → 如果 reader sleeping 才 signal。command 在 queue 中，signal 只是叫醒等待者。

这与 condition_variable 的核心原则完全一样：**共享状态是真值，notification 只是 wakeup。**

## recv()：能直接读就不进内核等待

mailbox 先尝试 `_cpipe.read()`；只有队列读空才把 `_active=false`，再调用 signaler.wait。醒来后重新进入 active 状态并读取 cpipe。

~~~text
Active --queue empty--> Passive/Sleeping --signal--> Active
~~~

## 为什么 mailbox fd 要进入 poller

~~~cpp
_mailbox_handle = _poller->add_fd (_mailbox.get_fd (), this);
_poller->set_pollin (_mailbox_handle);
~~~

这样跨线程 command、网络 fd 和 timer 进入同一个事件循环，不需要两套阻塞机制。

## in_event() 为什么 drain

~~~cpp
command_t cmd;
int rc = _mailbox.recv (&cmd, 0);
while (rc == 0 || errno == EINTR) {
    if (rc == 0)
        cmd.destination->process_command (cmd);
    rc = _mailbox.recv (&cmd, 0);
}
~~~

一次 wakeup 会尽量把当前 command 批量 drain 完，避免每条 command 都支付一次 kernel wakeup。

## 与 condition_variable 的对应

~~~text
std::queue + mutex + condition_variable
                 ↓
ypipe + sender mutex + signaler fd + poller
~~~

区别主要在等待边界：只等一个 predicate 时 condition_variable 很自然；同时等 socket/timer/command 时，fd-based signaler + poller 更自然。
