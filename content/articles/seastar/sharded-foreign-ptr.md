# Sharded 与 foreign_ptr：对象可以跨 Core 移动，但它的析构责任不能随便移动

固定源码版本：`8df8212e53577e1d8477a5c901457cd61d88afc7`。

Shard-per-core 最容易被误解成：

~~~text
每个 core 有一份对象
~~~

但真正重要的是：

> 每份 mutable object 有明确 owner shard，外部 core 通过消息调用 owner。

Seastar 的 `sharded<Service>` 与 `foreign_ptr` 分别解决“服务实例 ownership”和“跨 shard 指针 lifetime”。

## sharded<Service> 是 Per-shard Object Table

内部：

~~~cpp
struct entry {
    shared_ptr<Service> service;
};
std::vector<entry> _instances;
~~~

`start()` 会在每个逻辑 core 构造本地 Service instance。

当前 shard 访问：

~~~text
local()
→ _instances[this_shard_id()]
~~~

业务代码通常只直接操作自己的 local service。

## invoke_on 为什么不是直接拿远端指针

`invoke_on` 最终依赖 `smp::submit_to` 的 owner-shard RPC：request 与 completion 分别走一条 SPSC，service-group semaphore 一直占用到 completion 回到 origin，work item 也最终回 origin 删除。也就是说，“把 computation 移到 owner”本身有一套完整 transport/backpressure/lifetime 协议，见 [SMP Message Queue：Owner-Shard、双向 SPSC 与跨核 Round-trip Backpressure](smp-message-queue.md)。

跨 shard：

~~~text
invoke_on(shard B, func)
↓
smp::submit_to(B)
↓
B obtains local Service&
↓
func executes on B
~~~

调用者移动的是 computation/message，不是把 B 的 mutable object pointer 当成普通共享指针在 A 上直接操作。

这就是 owner-computes。

## 为什么普通 shared_ptr 也不能随便跨 Core

Seastar 的 `shared_ptr/lw_shared_ptr` 为性能并不使用跨核 atomic reference count。

更重要的是对象 destructor 可能会：

- 从 shard-local container unlink；
- free shard-local allocator memory；
- 修改 owner Reactor 的 local state。

所以即便 pointer value 能被另一个 core 看见，也不表示“在哪个 core 析构都安全”。

## foreign_ptr 记住 Owner CPU

核心字段：

~~~cpp
PtrType _value;
unsigned _cpu;
~~~

构造时记录 `this_shard_id()`。

对象本身可以 move 到别的 shard，但 owner identity 跟着它一起走。

## Destructor 为什么可能发一条 SMP Message

如果 foreign_ptr 在非 owner shard 被销毁：

~~~text
current shard != _cpu
↓
smp::submit_to(_cpu, lambda)
↓
owner shard sets wrapped pointer = {}
↓
real destructor runs there
~~~

所以 pointer 的物理持有位置和 destruction execution context 是两个不同概念。

## 为什么 foreign_ptr 是 Move-only

复制一个跨 shard ownership wrapper 需要回 owner shard安全增加底层 reference count。

这不是一个普通本地 copy。

因此默认禁止 copy；显式 `copy()` 返回 future，并通过 owner shard 完成复制。

API 类型把跨核成本暴露出来，而不是伪装成便宜的 C++ copy。

## release() 为什么危险

`release()` 把内部 pointer 交给调用者，但源码明确警告：

> caller must destroy it on owner shard

这说明 raw pointer 并不会携带 execution ownership。

一旦脱离 wrapper，协议责任就落到程序员身上。

## 对 GPU / Device Handle 的类比

同样原则也适用于：

- CUDA context-bound resource；
- device-local allocator object；
- thread-affine GUI/resource handle；
- io_uring owner ring request；
- reactor-local timer。

一个 handle 能跨线程保存，不意味着它的销毁/回收可以在任意 execution context 执行。

## 可迁移原则

1. Pointer ownership 还应包含“谁负责执行 destructor/reclaim”。
2. 跨线程对象访问应优先移动 computation 到 owner，而不是暴露裸远端 pointer。
3. 如果 copy 需要跨核同步，API 不应伪装成普通廉价 copy。
4. Move-only wrapper 很适合表达 unique execution ownership。
5. Resource lifetime 应记录 allocation/creation domain，而不仅是内存地址。