# Blackboard：为什么机器人共享状态不一定应该放进消息队列

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

机器人中有一类数据和“消息”不同：

~~~text
当前模式
当前温度
当前控制增益
当前机器人状态
最新标定参数
~~~

业务通常不关心过去发了多少条，而关心现在值是多少。

这就是 Blackboard 更自然的场景。

## Blackboard Creator 先定义 Key → Value Layout

官方 example：

~~~rust
let service = node
    .service_builder(
        &"My/Funk/ServiceName".try_into()?)
    .blackboard_creator::<BlackboardKey>()
    .add::<i32>(key_0, 3)
    .add::<f64>(key_1, 1.1)
    .create()?;
~~~

这不是运行时不断插入任意对象的通用 HashMap。

Service 创建阶段已经固定：

~~~text
key
value type
initial value
shared-memory layout
~~~

因此 Reader/Writer 后续可以直接定位共享 entry。

## Metadata 与 Data 分离

Reader 查 entry 时，先进入 Blackboard management map：

~~~rust
.map
.__internal_get(
    key_mem,
    key_eq_func,
)
~~~

得到 entry metadata，再取：

~~~rust
let offset =
    entry.offset.load(Ordering::Relaxed);
~~~

最后：

~~~text
shared data base
+
offset
=
UnrestrictedAtomic<ValueType>
~~~

所以又一次出现 Atlas 的核心模式：

~~~text
metadata
保存 offset/type

payload/state
放共享 memory
~~~

## Reader 为什么返回 EntryHandle

~~~rust
let entry_handle =
    reader.entry::<i32>(&key)?;
~~~

创建 handle 时只做一次：

~~~text
key lookup
type check
offset lookup
pointer translation
~~~

后续 entry_handle.get 不必每次重新 hash key。

这对高频读取很重要。

## Writer 为什么只能有一个 EntryHandleMut

固定源码：

~~~rust
match unsafe {
    (*atomic).acquire_producer()
} {
    None =>
        HandleAlreadyExists,

    Some(producer) =>
        ...
}
~~~

也就是说每个 entry 同时只允许一个 producer handle。

这正好符合 Blackboard 常见语义：

~~~text
single writer
multiple readers
~~~

避免多个 Writer 并发覆盖同一状态造成不可推理竞争。

## UnrestrictedAtomic 为什么叫 Unrestricted

普通语言原子类型通常只支持有限大小或特定类型。

这里：

~~~rust
pub struct UnrestrictedAtomic<T: Copy> {
    mgmt: UnrestrictedAtomicMgmt,
    data:
        [UnsafeCell<MaybeUninit<T>>;
         NUMBER_OF_CELLS],
}
~~~

它允许 T 只要 Copy，就能通过多 cell 机制提供单 producer、多 reader 的原子读取语义。

## 双 Cell 的核心思想

初始化：

~~~text
cell[0] = initial value
cell[1] = uninitialized

write_cell = 1
~~~

Producer 写：

~~~text
write cell[write_cell % 2]
↓
Release increment write_cell
~~~

Reader 再根据 write_cell 选择一个稳定 cell 复制。

因此更新不是 Writer 正在覆盖、Reader 同时读同一内存，而是尽量让写和读落在不同 cell。

## Memory Ordering 是真的存在于源码里

固定源码：

~~~rust
self.mgmt
    .write_cell
    .fetch_add(
        1,
        Ordering::Release,
    );
~~~

注释明确说明：

~~~text
先完成 data write
再推进 write_cell
~~~

这样 Reader 看到新的 generation 时，前面的 payload 写已经被发布。

这正对应 Communication Foundations 的 release/acquire 理论。

## Reader 的 get 为什么带 Generation Counter

EntryHandle::get：

~~~rust
let generation_counter =
    (*self.atomic)
        .__internal_get_write_cell();

BlackboardValue {
    value: (*self.atomic).load(),
    generation_counter,
}
~~~

于是应用可以调用 is_up_to_date 判断自上次读取以后是否发生更新。

它不是消息 sequence queue。

它更像：

~~~text
latest-value register
+
generation
~~~

## 为什么源码允许一种瞬时误判

源码注释明确说明 generation counter 可能在真正 copy value 前后发生变化。

因此 freshness 判断可能瞬时保守，但设计目标是后续检查仍不会永久漏掉真实更新。

这是一种明确的并发 tradeoff：

- 避免把 read path 变成重锁；
- 接受瞬时 freshness 判断并非绝对同步快照；
- 后续再次检查仍能发现更新。

## Writer 的两种更新

简单：

~~~rust
entry_handle_mut
    .update_with_copy(value);
~~~

底层：

~~~rust
self.producer.store(value);
~~~

如果 Value 较大，可以：

~~~rust
let value =
    entry_handle_mut.loan_uninit();

value.value_mut().write(...);

entry_handle_mut =
    unsafe {
        value.assume_init_and_update()
    };
~~~

这让应用直接写当前 write cell，再一次性 publish generation。

## Blackboard 与 Pub/Sub 的根本区别

Pub/Sub：

~~~text
sample 1
sample 2
sample 3
~~~

语义是事件序列。

Blackboard：

~~~text
key → current value
~~~

语义是共享状态。

如果 Reader 慢：

~~~text
Pub/Sub
可能积压 sample

Blackboard
直接看到最新值
~~~

因此 Blackboard 天然控制 data age，不保留完整历史。

## Event 可以和 Blackboard 组合

每个 EntryHandle 暴露 entry_id。

所以可以搭：

~~~text
Blackboard
保存 current state

Event
告诉 Reader
哪个 entry 更新了

WaitSet
阻塞等待多个 entry notification
~~~

这是一种非常典型的高性能架构：

> 状态留在共享内存，通知走轻量事件。

## 官方 Example 的真实结构

Creator：

~~~text
create blackboard
↓
define two entries
↓
Writer
↓
periodically update
~~~

Opener：

~~~text
open existing blackboard
↓
Reader
↓
cache EntryHandle
↓
periodically get latest state
~~~

没有 sample queue，也没有 serialization。

## 对具身智能 Runtime 的意义

Blackboard 很适合：

~~~text
robot mode
calibration
latest localization
runtime parameters
health state
model metadata
~~~

但不适合：

~~~text
必须保留每一帧的日志
必须严格重放全部事件
~~~

“消息”与“状态”是两种不同的数据模型。

真正优秀的 runtime 应该允许二者并存，而不是把所有信息都强行塞进 topic FIFO。
