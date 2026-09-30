# Stream Handle 生命周期：RAII、CUDA Event 与后台 Recycler

固定源码版本：d7cd9642d77a1d64fd85f25ba0bf96e108401900。

GPU buffer 生命周期最容易被 CPU 思维误导。CPU 函数返回和 C++ 对象析构，并不能证明 GPU kernel 已经停止访问底层 storage。

固定 CUDA backend 的 ReadHandle、WriteHandle 与 BufferRecycler 正是在桥接两条不同时间轴：

~~~text
CPU object lifetime
vs
GPU asynchronous access lifetime
~~~

## 为什么裸 device pointer 不够

如果 Buffer 只提供 mutable pointer，runtime 无法知道：

- 当前访问是读还是写；
- 哪条 CUDA stream 在使用；
- producer 什么时候真正写完；
- consumer 什么时候真正读完；
- 是否存在第二个 writer；
- storage 什么时候可以 recycle。

所以真正缺失的是 access protocol，而不是 pointer API。

## WriteHandle 是一次独占写阶段

WriteHandle 是 move-only，并持有：

~~~text
device pointer
write event
CUDA stream
shared HandleState
~~~

HandleState 有：

~~~text
Unset
→ InUse
→ Finalized
~~~

写 handle 获取时会拒绝已有 active writer，也会拒绝在已有 read event 时重新开启写阶段。

因此同一个 logical generation 的写权限是显式排他的。

## WriteHandle 析构记录 event，而不是同步 CPU

finalize/release 会：

~~~text
cudaEventRecord(write_event, producer_stream)
↓
state = Finalized
~~~

它没有 cudaDeviceSynchronize。

所以 CPU 不需要停下来等待 GPU，只是把 producer completion 记录进 GPU dependency graph。

## ReadHandle 如何保证读发生在写之后

ReadHandle 构造时：

~~~text
producer write event exists
↓
cudaStreamWaitEvent(reader_stream, write_event)
↓
later work on reader stream is ordered after producer
~~~

这是 GPU-side ordering，不是 CPU blocking wait。

因此不同 Operator/线程仍然可以快速 enqueue 后续 kernel。

## 为什么 ReadHandle 析构还要记录 reader event

只等待 producer 完成仍然不够。

如果 consumer kernel 尚未结束，而 CPU ReadHandle 已析构，pool 若立即 reuse storage 就会 use-after-recycle。

所以 ReadHandle release：

~~~text
create CUDA event
↓
record event on reader stream
↓
append event to Buffer read_events
~~~

每个 reader 都留下自己的 completion evidence。

## read_events_ 为什么使用 vector

一个 Buffer 可能被多个 reader stream 消费。

系统需要：

~~~text
iterate all outstanding events
query completion
destroy finished event
~~~

reader 数通常与有限 fan-out/in-flight 数量一致，不是大规模 associative workload。

vector 的连续遍历比复杂树结构更合适。

## 为什么仍然需要 mutex

不同 CPU thread 可能同时：

- 创建/销毁 ReadHandle；
- 获取 WriteHandle；
- reap completed events。

read_events 容器必须有一致性保护。

这里选择普通 mutex 很合理。

这也是源码阅读应该形成的判断：

> 并发不等于所有结构都应该 lock-free；先看 contention、临界区大小和访问频率。

## ReadHandle const pointer 与 WriteHandle mutable pointer 的意义

接口把访问权限编码进类型：

~~~text
ReadHandle  → const pointer
WriteHandle → mutable pointer
~~~

这不能替代所有运行时检查，但让大部分调用路径天然遵循 read/write discipline。

更关键的是两类 handle 可以记录不同 completion event。

## promoted_buffer_ 为什么挂在 Handle 上

当输入不是 CUDA-backed，而调用者又要求 CUDA access，系统可能先创建 promoted CUDA Buffer。

Handle 持有 promoted_buffer 的 shared ownership，确保临时转换结果至少活到访问阶段结束。

原则是：

> 转换产生的临时 storage 应绑定到实际使用它的 lease/token，而不是依赖外部猜测寿命。

## CudaBuffer 析构为什么还不能直接归还 pool

析构时会收集：

~~~text
producer write event
+
all outstanding reader events
~~~

如果直接在业务线程里 cudaEventSynchronize，析构可能产生不可预测阻塞。

所以固定实现把真正等待交给后台 BufferRecycler。

## BufferRecycler 的容器选择

核心：

~~~cpp
std::deque<PendingWork> queue_;
std::mutex mutex_;
std::condition_variable cv_;
std::thread thread_;
bool running_;
~~~

Recycler 只需要：

~~~text
push_back
pop_front
~~~

所以 deque 比 vector 更适合 FIFO cleanup queue，因为 pop_front 不需要整体搬移。

这和 GXF StagingQueue 使用 vector-backed fixed ring 并不矛盾：

~~~text
StagingQueue:
fixed capacity + predictable storage

Recycler:
dynamic deferred-cleanup FIFO
~~~

访问模式不同，容器自然不同。

## Recycler thread 真正执行什么

~~~text
wait until queue non-empty
↓
move front PendingWork
↓
wait every CUDA event
↓
destroy events
↓
reset device pointer
↓
custom deleter executes
↓
block returns to pool / remote refcount decrements
~~~

因此资源释放有两阶段：

~~~text
CPU ownership ends
→ deferred cleanup enqueued

GPU ownership ends
→ physical storage may recycle
~~~

## custom deleter 为什么是 ownership policy

同一个 CudaBuffer 可以包：

~~~text
publisher-owned VMM pool block
remote imported VMM mapping
ordinary CUDA allocation
~~~

不同来源的 release semantics 不一样。

Publisher block 的 deleter 可以把 block 还给 pool。

Imported block 的 deleter 可以减少共享 IPC refcount。

因此 storage-specific release policy 被封装在 custom deleter，而不是硬编码进 CudaBuffer。

## imported Buffer 为什么不拥有 Publisher allocation

Subscriber 只拥有：

~~~text
local imported mapping
+
remote-use reference
~~~

真正 allocation/reuse 决定权仍在 Publisher pool。

所以 local destruction 最重要的动作是：

~~~text
finish local GPU readers
↓
release remote-use reference
~~~

这是跨进程 ownership separation。

## process-lifetime Recycler 暴露了 static destruction 问题

固定实现刻意避免在普通 static destruction 阶段销毁 Recycler，因为 CUDA runtime/driver 的卸载顺序可能已经开始。

这说明全局 runtime object 有另一类生命周期问题：

~~~text
C++ static destruction order
vs
GPU runtime destruction order
~~~

生产系统最好显式设计 shutdown，而不是期待全局析构自动按正确顺序发生。

## 异步 RAII 与普通 RAII 的区别

普通 RAII 常被理解为：

~~~text
destructor
→ physical resource released
~~~

异步 RAII 更准确是：

~~~text
destructor
→ ownership transferred to deferred cleanup
→ physical release happens after completion
~~~

RAII 仍然有效，只是析构语义从同步 free 变成 ownership handoff。

## 一个 fan-out 例子

~~~text
Producer stream P writes frame F
↓
event W

Reader A:
stream A waits W
uses F
records event RA

Reader B:
stream B waits W
uses F
records event RB

CPU owners disappear
↓
Recycler waits RA and RB
↓
F may return to pool
~~~

只要任一个 reader 仍在 GPU 上使用，storage 就不能 reuse。

## 与 Holoscan 的对应

Holoscan 的 stream-aware deallocation 同样要告诉 allocator 最后是哪条 CUDA stream 在使用 Tensor。

这里通过 ReadHandle event 收集多个 reader completion。

API 形式不同，但底层不变量相同：

~~~text
storage reuse
must happen after
last asynchronous device access
~~~

## 与 UCX request completion 的对应

UCX nonblocking send：

~~~text
API returns
!=
buffer safe to reuse
~~~

CUDA kernel launch：

~~~text
API returns
!=
buffer safe to reuse
~~~

两者都需要 completion token。

所以异构 runtime 可以统一理解为：

> asynchronous API return is not ownership release.

## 对自研具身 Runtime 的抽象

~~~cpp
class ReadLease {
    const void * ptr;
    CompletionFence producer_ready;
    CompletionFence consumer_done;
};

class WriteLease {
    void * ptr;
    CompletionFence write_done;
};
~~~

Pool 只在所有 lease/fence 完成以后重用 block。

CompletionFence 的具体实现可以是 CUDA event、NPU fence、DMA fence 或 UCX request。

## 本篇最重要的五点

1. C++ owner 生命周期与 GPU access 生命周期是两套时间轴。
2. Read/Write Handle 把访问权限、stream 与 completion 绑定。
3. zero-copy fan-out 必须追踪每个异步 reader 的完成。
4. background recycler 把阻塞 wait 从业务线程移出。
5. 异步 RAII 的析构经常表示 ownership handoff，而不是物理释放已经完成。
