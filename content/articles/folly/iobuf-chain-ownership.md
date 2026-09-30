# IOBuf：网络 Buffer 为什么要把 Data Window、Storage Ownership 与 Chain 分开

固定源码版本：`c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8`。

真实网络 Buffer 经常要 prepend header、append payload、scatter/gather、拼接分片、clone 同一 storage，甚至接入外部 allocator。单纯 `std::vector<uint8_t>` 很难同时优雅表达这些需求。

Folly 的 `IOBuf` 把问题拆成三层。

## Storage

~~~cpp
uint8_t* buf_;
size_t capacity_;
~~~

表示整块底层可用内存。

## Data Window

~~~cpp
uint8_t* data_;
size_t length_;
~~~

表示当前有效数据区域。

~~~text
buffer start
|---- headroom ----|==== data ====|---- tailroom ----|
                   ^
                  data_
~~~

只要 headroom 足够，prepend protocol header 可以只移动 `data_` 和 `length_`，不需要重新分配整包。

## Circular IOBuf Chain

每个 IOBuf 还有 `next_` / `prev_`，形成循环双向链。

~~~text
[header]
↔ [payload fragment A]
↔ [payload fragment B]
↔ [external buffer]
~~~

链式表达天然适合 writev/sendmsg 一类 scatter/gather I/O。

单节点时 `next_ == this && prev_ == this`，因此链操作不用到处处理 null special case。

## Object Ownership 与 Storage Ownership 不同

链头通常由 `unique_ptr<IOBuf>` 持有对象 lifetime，但多个 IOBuf clone 可以共享同一底层 storage。

所以：

~~~text
IOBuf object ownership
!=
buffer storage ownership
~~~

## SharedInfo 是 Storage Control Block

SharedInfo 保存：

~~~cpp
FreeFunction freeFn;
void* userData;
std::atomic<uint32_t> refcount;
bool externallyShared;
StorageType storageType;
~~~

它类似专门面向 buffer 的 shared control block。Clone 可以创建新的 IOBuf metadata，但共享底层 payload，不发生大块 memcpy。

## unlink/pop 为什么返回 unique_ptr

链结构改变会改变对象 ownership。`unlink()` 把节点拆出后把 ownership 显式交给调用者。

这比裸指针 remove 更清楚：数据结构修改和 ownership transfer 在 API 类型中同时可见。

## Shared Refcount 不等于并发可写

Atomic refcount 只保护 storage lifetime，不保护 payload mutation。多个 clone 指向同一 storage 时，写操作仍需要 unshare/copy-on-write 或明确 single writer。

这是所有 zero-copy/shared-buffer 系统都必须区分的边界。

## 为什么还需要 Coalesce

链式 Buffer 减少复制，但并非所有下游 API 都接受 scatter/gather，所以系统仍需要在必要时把 chain 合并成 contiguous storage。

真正的决策是：

~~~text
copy cost
vs
fragment / chain complexity
~~~

## 对机器人与 GPU Pipeline 的启发

图像、点云、Tensor 也应该区分 storage identity、data view、ownership/reclaim policy 和 transport metadata，而不是全部塞进一个裸 vector。

这和 iceoryx2、UCX、Holoscan 的 Buffer 生命周期可以直接对照。

## 可迁移原则

1. Storage capacity 与有效 data window 分开。
2. Buffer object lifetime 与 underlying storage lifetime 分开。
3. 大 payload 优先考虑 chain/scatter-gather，而不是频繁 memcpy。
4. Ownership transfer 应通过 API 类型显式表达。
5. Zero-copy 往往把 copy cost 转换成更复杂的 lifetime protocol。