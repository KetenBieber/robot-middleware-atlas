# Pipe 与 HWM：Backpressure 怎样进入对象状态机

固定源码版本：`46493370217ac135246617fa2f6ac819d8b61bfc`。

`ypipe` 解决低层 SPSC 数据移动；`pipe_t` 再加入双向 endpoint、active state、HWM/LWM、peer command、message boundary 与 termination。它已经不是一个普通 Queue。

## pipepair：双向通信由两条单向 channel 组成

~~~text
pipe A -- ypipe1 --> pipe B
pipe A <-- ypipe2 -- pipe B
~~~

每个 endpoint 分别保存 `_in_pipe`、`_out_pipe` 与 `_peer`。这种组合让每个方向仍保持单 writer / 单 reader。

## check_write() 先检查状态，再检查 HWM

~~~cpp
if (unlikely (!_out_active || _state != active))
    return false;

const bool full = !check_hwm ();
if (unlikely (full)) {
    _out_active = false;
    return false;
}
~~~

拒绝写入可能来自 lifecycle，也可能来自 backpressure。达到 HWM 后 endpoint 会主动进入 inactive，而不是不断 spin。

## 为什么不直接读 queue.size()

pipe 维护 `_msgs_read`、`_msgs_written`、`_peers_msgs_read`。逻辑上 outstanding 约等于 `msgs_written - peers_msgs_read`。writer 不需要每次跨线程读取 reader 的容器内部大小。

## LWM：为什么 reader 不每 pop 一条就通知 writer

~~~cpp
if (_lwm > 0 && _msgs_read % _lwm == 0)
    send_activate_write (_peer, _msgs_read);
~~~

Reader 消费到一定批量后才把进度反馈给 peer，这样避免每条消息都产生跨线程 command。

于是形成：

~~~text
HWM: 什么时候暂停 writer
LWM: 消费到什么程度再恢复 writer
~~~

这是一种 hysteresis，避免状态在满/不满边界上高频抖动。

## read side 也是 active/inactive 状态机

如果底层 pipe 读空，`_in_active=false`。新数据发布时，writer 会通过 activate_read command 让 peer 恢复 active。

~~~text
active -> empty -> inactive -> new data/activate_read -> active
~~~

## 与 condition_variable 的共同结构

Producer/Consumer 中是 predicate 不成立 → sleep → notify → re-check；libzmq 中则是 HWM/empty → endpoint inactive → activate_read/write command → active。

共同原则是：**条件不成立时停止无效工作，只有状态变化达到阈值时才唤醒另一侧。**

## HWM 是过载语义

pipe 只提供“现在还能不能写”的容量状态；上层 socket pattern 再决定 block、EAGAIN、drop 或 route 到其他 pipe。因此 backpressure 是分层的。

对机器人系统同样如此：控制命令、遥测、日志不应该共享一个无界 `std::queue<Message>`。它们应该有不同 capacity、HWM/LWM 和 overload policy。HWM 本质上定义的是系统过载时怎样退化，而不只是性能参数。
