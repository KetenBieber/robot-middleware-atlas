# Accelerator IPC 闭环：从 CUDA VMM 到 DMA-BUF，拆开 Handle、FD、Generation 与 Lifetime

固定源码版本：`d7cd9642d77a1d64fd85f25ba0bf96e108401900`。

同机异构计算最容易出现一种“看起来已经 zero-copy，实际上生命周期还没闭环”的设计：

~~~text
Publisher owns accelerator buffer
↓
message carries pointer / fd / handle
↓
Subscriber receives it
↓
both processes access same storage
~~~

真正困难的部分都藏在箭头里：虚拟地址只属于当前进程；FD 数字只属于当前进程的 descriptor table；同一物理 block 会被反复复用；descriptor 可能晚到；Subscriber 可能还没 import，Publisher 已经观察到 refcount 为 0；GPU kernel 的 API 已返回，却仍可能继续访问 storage。

rosidl_buffer_backends 同时提供 CUDA VMM backend 和 Qualcomm dma-buf backend，正好可以比较两种 accelerator IPC 如何围绕同一组不变量采用不同的数据结构和 OS 机制。

## 1. 跨进程 Buffer 至少有四种不同 Identity

### Logical payload identity

例如 frame 173、tensor generation 9912。它回答“这一次语义上是哪一份数据”。CUDA backend 用 `ipc_uid` / shared `uid` 表示 generation。

### Physical allocation identity

CUDA pool 中可以用 `(publisher pid, block_id)` 标识长期存在、会反复复用的 allocation。

### Kernel object capability

CUDA VMM export 和 dma-buf 都会暴露 FD-backed kernel object。FD 不是 payload，而是当前进程 descriptor table 中对 kernel object 的 capability reference。

### Process-local virtual address

Publisher 和 Subscriber 可以把同一 allocation 映射到完全不同的 VA。因此：

> shared storage != shared virtual address。

## 2. `block_id` 与 `uid` 为什么必须分开

CUDA pool 的 block 同时保存：

~~~cpp
CUmemGenericAllocationHandle handle;
CUdeviceptr va;
size_t size;
int exported_fd;
uint32_t block_id;
IPCMetadata* ipc_meta;
uint64_t current_uid;
~~~

`block_id` 是稳定物理 slot identity，`uid` 是当前逻辑 generation。

~~~text
block 7 / uid 1001 → frame A
recycle
block 7 / uid 1002 → frame B
~~~

迟到的旧 descriptor 如果只带 `(pid, block_id)`，就会把 frame A 错认成 frame B。generation check 本质上与 ECS generation、slab handle generation、ABA counter 是同一种防 stale-reference 机制。

## 3. 为什么 raw FD 不能直接写进 DDS 消息

Publisher 中 `fd=17` 与 Subscriber 中 `fd=17` 没有任何天然关系。它们只是两个进程各自 descriptor table 的索引。

真正要传递的是 FD 背后的 kernel object reference。Linux 的标准机制是：

~~~text
AF_UNIX socket
+ sendmsg/recvmsg
+ SCM_RIGHTS
~~~

发送后 Subscriber 可能得到 `fd=31`，但它与 Publisher 的 `fd=17` 引用同一个 kernel object。

## 4. CUDA VMM 把 Descriptor Channel 与 Capability Channel 分开

注册 block 时，CUDA backend 建立与 `(pid, block_id)` 对应的 Unix socket：

~~~cpp
ss << "cuda_vmm_" << getpid() << "_" << block->block_id;
int server_socket = create_fd_server_socket(socket_path);
dispatcher_->add_socket(server_socket, block->exported_fd);
registered_blocks_[block->block_id] = {server_socket, socket_path};
~~~

普通 RMW descriptor 只携带：

~~~text
pid
block_id
block_size
socket_path
uid
CUDA IPC event handle
~~~

真正的 VMM FD 在 Subscriber 首次 import 时才通过 `SCM_RIGHTS` 获取。

所以它实际上有两条 plane：

~~~text
message/control plane
  small descriptor metadata

kernel capability plane
  FD transfer over Unix socket
~~~

## 5. CUDA FDDispatcher 为什么使用 `epoll`

如果每个长期 VMM block 都有一个 server socket，采用“一 socket 一线程”会让 OS thread 数随着 pool block 数增长。

固定实现使用一个 dispatcher thread：

~~~text
many server sockets
        ↓
      epoll
        ↓
one dispatcher thread
~~~

核心事件循环：

~~~cpp
int n = epoll_wait(epoll_fd_, events, MAX_EVENTS, 1000);

for (int i = 0; i < n; ++i) {
  int fd = events[i].data.fd;
  if (fd == event_fd_) {
    read(event_fd_, &val, sizeof(val));
    continue;
  }
  handle_client(fd, fd_to_serve);
}
~~~

这就是标准 Reactor：很多 readiness source 被收敛到一个 OS thread。

## 6. `eventfd` 为什么是独立控制通道

Dispatcher 可能睡在 `epoll_wait()`。如果其他线程要 stop、remove resource 或改变 reactor 状态，需要一个与真实 client traffic 无关的 wakeup source。

因此：

~~~text
data events   → server sockets
control event → eventfd
              ↓
            epoll
~~~

这个模式可以直接迁移到 device fd、timerfd、network socket、runtime shutdown 和 hot reload。

## 7. Subscriber 为什么用 `(pid, block_id)` 做 Import Cache Key

稳定 pool block 会被多次 publish。第一次 import 需要：

~~~text
Unix socket connect
SCM_RIGHTS
cuMemImportFromShareableHandle
cuMemAddressReserve
cuMemMap
cuMemSetAccess
~~~

后续同一物理 block 只需 cache lookup 与 generation check。

因此稳定 physical identity 的价值不仅是 zero-copy，还能把昂贵 import/setup 从“每帧一次”摊薄成“每 block 一次”。

## 8. Cache Hit 为什么仍然必须检查 `uid`

固定实现：

~~~cpp
auto it = cache.find(ImportCacheKey{pid, block_id});
if (it != cache.end()) {
  IPCMetadata* meta = it->second.ipc_meta;
  meta->refcount.fetch_add(1, std::memory_order_acq_rel);
  check_uid_staleness(meta, block_id, pid, expected_uid);
  return {it->second.va, meta};
}
~~~

cache hit 只证明 physical allocation 已映射到本进程，不证明 descriptor 指向的 logical generation 仍有效。

所以必须同时维护：

~~~text
mapping validity
+
generation validity
~~~

## 9. 为什么先 `refcount++`，再验证 Generation

如果先检查 generation，再增加 remote refcount，中间存在一个 reuse race：Publisher 可能看到 `refcount==0`，并在 Subscriber 宣告 ownership 前复用 block。

固定实现选择：

~~~text
refcount++
↓
check generation
↓ stale?
refcount-- rollback
~~~

它表达的是“先声明我可能正在使用，再确认这一代是否仍合法”。

## 10. 为什么 `refcount==0` 仍不等于安全复用

存在一个典型窗口：

~~~text
T0 Publisher sends descriptor
T1 descriptor is still in RMW/queue
T2 local Buffer returns block to pool
T3 Subscriber has not imported yet
~~~

此时 `refcount==0`，但未来仍可能有 Subscriber 根据已经在路上的 descriptor 发起 import。

所以 shared metadata 还有：

~~~cpp
std::atomic<uint64_t> publish_timestamp_us;
~~~

真正 reuse gate 是：

~~~text
refcount == 0
AND
publish grace period elapsed
~~~

三种机制分别负责：

~~~text
refcount → 已经 import 的 active users
grace    → descriptor 已发出但尚未 import 的窗口
uid      → 最终 stale detection
~~~

## 11. `free_blocks_` 为什么只是“候选可复用集合”

CUDA pool 的 `free()` 只做：

~~~cpp
free_blocks_[block->size].push_back(block);
~~~

下一次 `allocate()` 仍然要调用 `is_block_ready()`。因此 free list 的准确语义不是“现在可以用”，而是“allocator ownership 已归还，等待 lifetime eligibility 检查”。

这种 deferred reuse 在 CUDA、DMA、RDMA request pool 中都很常见。

## 12. Descriptor 本质上是一次 Publish Transaction

CUDA path 顺序为：

~~~text
register capability endpoint
↓
assign generation uid
↓
fill physical identity + generation
↓
finalize producer write handle
↓
export completion event
~~~

因此 descriptor 不是单纯 metadata，而是：

> storage identity + generation + completion dependency 的组合票据。

## 13. Zero-copy 还必须共享 Completion

共享同一 allocation 只回答“数据在哪里”，没有回答“什么时候可以安全读取”。

Producer kernel 在 stream P 写数据后，CUDA backend 导出 write event：

~~~cpp
cudaIpcGetEventHandle(&event_handle, write_event);
~~~

Subscriber：

~~~cpp
cudaIpcOpenEventHandle(&imported_event, event_handle);
imported_buffer.set_write_event(imported_event, true);
~~~

后续 ReadHandle 在 consumer stream 建立 wait。

完整 zero-copy contract 因而是：

~~~text
shared storage
+
shared completion/fence
~~~

只有 pointer/handle 没有 fence，仍然可能形成 device-side data race。

## 14. Subscriber Deleter 为什么只释放 Remote Lease

imported Buffer 的 deleter：

~~~cpp
auto deleter = [meta](uint8_t*) {
  if (meta) {
    meta->refcount.fetch_sub(1, std::memory_order_release);
  }
};
~~~

Subscriber 不拥有 Publisher 的 physical allocation。它拥有的是 local imported mapping 与一个 remote-use lease。

因此跨进程 ownership 被明确拆开：

~~~text
Publisher pool → physical reuse authority
Subscriber    → temporary remote-use lease
~~~

## 15. Qualcomm DMA-BUF Backend：同一问题的另一种实现

QC backend 使用：

~~~text
rpcmem / dma-buf
+ mmap
+ Unix socket
+ SCM_RIGHTS
~~~

虽然 accelerator API 不同，但 FD capability transfer、bounded retention、fallback 和 shutdown 问题仍然存在。

## 16. `unordered_map + deque` 为什么适合 FdBroker

核心成员：

~~~cpp
std::unordered_map<uint64_t, Entry> table_;
std::deque<uint64_t> order_;
static constexpr size_t kCapacity = 64;
~~~

`unordered_map` 负责 `uid → duplicated fd` 的均摊 O(1) lookup；`deque` 负责 O(1) `push_back/pop_front` 的 FIFO eviction order。

这是“associative lookup + bounded retention order”的组合，而不是为了炫技使用多个 STL。

## 17. Broker 为什么必须 `dup(fd)`

Publisher 注册 buffer 时：

~~~cpp
int dup_fd = ::dup(fd);
table_[uid] = {dup_fd, dmabuf_size};
~~~

原始 Buffer 即使析构并 close 自己的 fd，Broker 仍持有独立 kernel reference。

所以：

~~~text
Buffer object lifetime
can end

Broker capability lifetime
can continue
~~~

## 18. Rolling Window 其实就是 Lifetime / Backpressure Policy

Broker 只保留最近 64 个 uid：

~~~cpp
order_.push_back(uid);
while (order_.size() > kCapacity) {
  uint64_t old = order_.front();
  order_.pop_front();
  close(table_[old].dup_fd);
  table_.erase(old);
}
~~~

这明确拒绝“无限保存 capability history”。慢 Subscriber 请求已经淘汰的 uid 时必须 miss/fallback。

对于 live sensor data，这往往比无限保留 dma-buf 更符合实时系统语义。

## 19. CUDA 与 QC 为什么选择不同 Server Topology

CUDA VMM：

~~~text
stable pool block
→ per-block socket identity
→ one epoll dispatcher multiplexes many sockets
~~~

QC dma-buf：

~~~text
process-wide broker
→ subscriber sends uid
→ unordered_map lookup
→ send one fd
~~~

原因来自资源 identity/reuse pattern，而不是某一种网络编程模型“更高级”。

CUDA block 长期稳定并被反复换 generation，所以 `(pid, block_id)` 是自然 stable key；QC path 更像 publish uid 进入 bounded rolling window，一个 process-level broker 做 uid lookup 更直接。

## 20. QC 为什么使用 Per-Connection Thread

Broker accept 后：

~~~cpp
active_connections_.fetch_add(1);
std::thread([this, conn_fd]() {
  handle_connection(conn_fd);
}).detach();
~~~

优点是实现直接，连接 handler 可以独立阻塞。代价是 burst 下线程创建、stack 和 scheduler 成本会增长。

因此还需要 `active_connections_` 做 shutdown barrier。

如果 capability request 变成高频 hot path，更可能考虑固定 worker pool、epoll reactor 或 io_uring；但对低频 control/setup path，简单线程模型可能已经足够。

## 21. 为什么 Lookup 时要“锁内 dup，锁外 sendmsg”

QC handler 的关键结构：

~~~cpp
int tmp_fd = -1;
{
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = table_.find(uid);
  tmp_fd = ::dup(it->second.dup_fd);
}

sendmsg(conn_fd, ..., tmp_fd);
~~~

如果 unlock 后还拿着 table 内原 fd，另一个线程可能 eviction 并 close；如果把 `sendmsg()` 也放在 mutex 内，慢客户端又会延长全局临界区。

先 `dup()` 出独立 kernel reference，同时解决 lifetime race 与 long critical section。

## 22. `SCM_RIGHTS` 之后为什么可以立刻 Close `tmp_fd`

成功发送后，内核已经为接收进程创建自己的 file reference。

~~~text
Broker table dup_fd
        │
        ├─ dup → tmp_fd
        │          │
        │          └─ SCM_RIGHTS
        │               ↓
        │        Subscriber received_fd
        │
        └─ later eviction close
~~~

三个 descriptor 可以具有不同数字，但引用同一个 kernel object。

## 23. 为什么必须检查 `MSG_CTRUNC`

SCM_RIGHTS 位于 ancillary/control message。control buffer 太小时，普通 payload byte 可能收到，但 FD 被内核截断。

QC backend 明确检查：

~~~cpp
if (msg.msg_flags & MSG_CTRUNC) {
  // capability lost → fallback
}
~~~

所以 `recvmsg() > 0` 并不足以证明 capability transfer 成功。

## 24. `mmap` 与 CUDA VMM Import 的共同抽象

QC：

~~~text
received fd
→ mmap
→ local VA
~~~

CUDA：

~~~text
received fd
→ cuMemImportFromShareableHandle
→ cuMemAddressReserve
→ cuMemMap
→ cuMemSetAccess
→ local GPU VA
~~~

抽象状态机完全一致：

~~~text
receive capability
→ import resource
→ establish local address-space representation
→ set access
→ construct local Buffer/Lease
~~~

## 25. 一帧 CUDA Tensor 的完整生命周期

### T0：Pool 分配

~~~text
allocate(request)
→ size-class lookup
→ ready block or new VMM block
~~~

得到 stable `block_id` 与 publisher-local VA。

### T1：Producer 写

GPU stream P 在 block 上异步写入。

### T2：发布 Generation

~~~text
assign_uid(block)
→ shared uid
→ publish_timestamp
~~~

### T3：记录 Producer Completion

WriteHandle finalize，在 stream P 上 record CUDA event。

### T4：发送 Descriptor

~~~text
(pid, block_id, uid, socket_path, event_handle)
~~~

payload 仍留在 accelerator memory。

### T5：Subscriber Import

cache miss 时：

~~~text
mmap IPCMetadata
→ refcount++
→ generation check
→ SCM_RIGHTS receive fd
→ driver import/map
→ import cache
~~~

cache hit 时只保留 lease + generation validation。

### T6：Consumer Wait Producer

consumer stream wait imported producer event。

### T7：Consumer GPU Work

ReadHandle 记录自己的 consumer completion。

### T8：释放 Remote Lease

GPU access 完成后，Subscriber owner 最终触发 `refcount--`。

### T9：Publisher Reuse

block 虽已回 free list，但只有满足：

~~~text
refcount == 0
AND grace elapsed
~~~

才允许下一 generation 复用。

## 26. 状态应该分别属于哪一层

| 状态 | 层级 | 目的 |
| --- | --- | --- |
| `block_id` | allocator | stable physical slot |
| `uid` | lifetime protocol | stale-generation detection |
| exported FD | kernel capability | share physical resource |
| socket path | IPC control plane | capability acquisition |
| import cache | process-local runtime | amortize mapping/import |
| refcount | ownership | active remote users |
| grace timestamp | race protection | descriptor in-flight window |
| CUDA event | device synchronization | producer/consumer happens-before |
| endpoint capability cache | path selection | optimized vs fallback |
| free list | allocator index | reuse candidates |
| recycler | async lifetime | wait device completion before physical reuse |

一个实现如果只说“支持 zero-copy”，却没有明确这些状态放在哪里，通常生命周期还没有真正闭环。

## 27. 五类最常见的设计错误

### 错误一：把 VA 当跨进程 Identity

解决方案是 exportable capability + local import/map。

### 错误二：Stable Slot 没有 Generation

会出现 late descriptor / ABA / stale payload。

### 错误三：只看 Refcount

descriptor 仍在路上时 refcount 可能还是 0；需要 grace/handshake 与 generation。

### 错误四：只共享 Storage，不共享 Completion

Consumer 可能在 Producer kernel 写完前读取；需要 event/fence。

### 错误五：Broker 保留无限历史

慢/死 Subscriber 会让 FD 与 allocation retention 无界；需要 bounded window 与显式 miss/fallback policy。

## 28. 可迁移的 Accelerator IPC 数据结构

~~~cpp
struct ResourceKey {
  ProcessId owner;
  SlotId slot;
};

struct Descriptor {
  ResourceKey resource;
  Generation generation;
  CapabilityEndpoint endpoint;
  CompletionFence producer_done;
  Shape shape;
  DType dtype;
  Deadline deadline;
};

struct SharedLifetime {
  std::atomic<int> remote_users;
  std::atomic<Generation> generation;
};
~~~

Publisher 需要：

~~~text
ResourcePool
+ CapabilityBroker
+ GenerationPublisher
+ CompletionExporter
~~~

Subscriber 需要：

~~~text
ImportCache
+ GenerationValidator
+ CompletionImporter
+ Lease
~~~

这套结构可以用于 Camera DMA→GPU preprocess、vision encoder→VLA policy、NPU detector→CPU planner、multi-process simulator 或 tensor server。

## 29. Reactor 与 Per-Connection Thread 怎么选

| 需求 | Reactor / epoll | per-connection thread |
| --- | --- | --- |
| endpoint 很多 | 更合适 | thread 数可能膨胀 |
| connection 很低频 | 稍复杂 | 简单直接 |
| handler 会阻塞 | 需要 worker pool | 容易表达 |
| 统一 shutdown wakeup | eventfd 很自然 | 需要追踪 accept/handler |
| tail latency 可预测性 | 更易集中控制 | 受 thread scheduling 影响 |

真正决定选型的是 connection rate、并发连接数、handler blocking time、latency target 和 shutdown semantics。

## 30. Import Cache 的性能收益如何估计

每帧重新 import 的固定成本可以写成：

~~~text
T_setup = socket connect
        + SCM_RIGHTS
        + driver import
        + VA reserve/map
        + permission setup
~~~

稳定 pool 有 B 个 block，长期处理 F 帧，且 `F >> B` 时，setup count 可以接近 B，而不是 F。

所以 zero-copy 性能收益不只来自减少 memcpy，还来自 stable identity 对 setup cost 的 amortization。

## 31. Pool Size 同时决定显存和 Control-plane State

pool 太小会造成 buffer shortage / producer stall；pool 太大不仅占更多显存，还意味着更多 exported FD、server socket、import-cache entry 与 lifetime metadata。

因此 pool capacity 同时控制：

~~~text
in-flight concurrency
memory footprint
IPC control-plane state size
~~~

## 32. 对实时机器人更重要的是 Data Age

一条 zero-copy path 如果 pool exhaustion 导致等待 20 ms，可能比稳定 4 ms 的 copy path 更差。

应该同时监控：

~~~text
copy bytes
import-cache hit rate
pool free blocks
remote refcount
oldest outstanding generation age
fallback rate
FD broker miss rate
p99 handoff latency
end-to-end observation age
~~~

“zero-copy=true”不是性能结论。

## 33. Failure Policy 必须由业务语义决定

Camera frame 的 stale/miss 可以 drop 并等待更新帧；Control command 的 capability miss 不能静默跳过；Recorder 可以接受 CPU-copy fallback。

同一个 Buffer Backend 提供 mechanism，业务 Runtime 仍然必须定义 policy。

## 34. 跨平台后仍然不变的设计变量

CUDA VMM 可以换成 DMA-BUF、ROCm IPC、Level Zero IPC、NPU vendor handle 或 RDMA MR/rkey；Linux SCM_RIGHTS 也可以换成其他 OS 的 capability transfer。

但以下问题不会消失：

~~~text
resource identity
generation
capability transfer
local import/mapping
ownership/lease
completion fence
bounded retention
fallback
shutdown
~~~

这些才是能迁移到不同机器人计算平台的设计知识。

## 35. 一个 VLA Runtime 的完整 Data Plane

~~~text
Camera / Sensor
      ↓
Accelerator Buffer Pool
  stable slots
      ↓
Producer kernel
      ↓
completion fence
      ↓
Descriptor
  slot + generation
  capability endpoint
  fence
  timestamp/deadline
      ↓
Runtime Router
      ↓
┌───────────────────────────────┐
│ same process                  │
│ direct ownership handoff      │
├───────────────────────────────┤
│ same host / same accelerator  │
│ capability broker + import    │
├───────────────────────────────┤
│ unsupported endpoint          │
│ explicit CPU/staging fallback │
└───────────────────────────────┘
      ↓
Consumer Lease
      ↓
consumer completion
      ↓
remote lease release
      ↓
Pool reuse gate
~~~

一套异构 Runtime 必须同时回答六个问题：数据在哪里、谁能访问、什么时候能访问、谁拥有、什么时候能复用、失败以后走哪条路。六个问题都闭环，zero-copy 才是可部署的系统机制，而不是 benchmark 技巧。