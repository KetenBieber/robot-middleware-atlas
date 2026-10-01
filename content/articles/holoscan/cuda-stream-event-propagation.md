# Holoscan CUDA Stream 传播：GPU 异步依赖如何穿过 Operator 边界

固定源码版本：Holoscan SDK v4.6.0 `66a9609ac37515405561b9b8dbdee8e57f41ab11`。

CPU 消息中间件里，我们常常默认：

~~~text
消息已经交给 consumer
≈
消息数据已经可安全读取
~~~

在 GPU pipeline 里，这个等式不成立。

上游 Operator 可以在 CUDA stream A 上 launch kernel，然后 CPU 立刻返回。此时 Tensor 指针已经能够被下游拿到，但 GPU 也许仍在写那块显存。

因此 accelerator-native runtime 必须同时传播两种东西：

~~~text
payload storage
+
execution dependency
~~~

这篇文章从固定源码追踪 Holoscan 如何把 `CudaStreamId`、`CudaStreamHandle`、CUDA event、输出 Entity 和 allocator lifetime 串成一套协议。

## 1. 第一性原理：指针可见不等于数据完成

考虑最简单的两个 Operator：

~~~text
Preprocess
   |
   | Tensor*
   v
Inference
~~~

如果 Preprocess 做：

~~~cpp
preprocess_kernel<<<grid, block, 0, stream_a>>>(tensor);
emit(tensor);
~~~

CUDA kernel launch 对 CPU 是异步的。于是时间线可能是：

~~~text
CPU:
launch A ---- emit pointer ---- return

GPU stream A:
          [------- still writing tensor -------]

CPU downstream:
                 receive pointer
                 launch consumer?
~~~

下游真正需要的不是“Tensor 地址”，而是：

~~~text
Tensor 地址
+
“上游写什么时候完成”的 happens-before 关系
~~~

如果只传 storage，不传 dependency，就会形成典型的异步 data race。

## 2. 最粗暴的修法为什么是 cudaDeviceSynchronize()

最容易想到：

~~~cpp
preprocess_kernel<<<..., stream_a>>>(tensor);
cudaDeviceSynchronize();
emit(tensor);
~~~

这样当然能保证数据完成，但代价是把整个 device 上的并行 work 都变成 CPU 等待点。

原本可以是：

~~~text
CPU: launch A -> 做别的调度工作
GPU:     A ---------->
~~~

加入 device-wide barrier 后变成：

~~~text
CPU: launch A -> [阻塞等整个 device] -> emit
GPU:     A ---------->
~~~

如果每个 Operator 都这么做，stream concurrency 基本被抹掉。

正确目标不是“所有 GPU 工作都结束”，而只是：

> consumer 的 stream 在使用这块数据之前，必须等待 producer 的那条必要依赖。

这正是 CUDA event + stream wait 适合表达的关系。

## 3. Holoscan 把 execution dependency 变成消息的一部分

Holoscan/GXF Entity 不只可以装 Tensor，还可以挂一个 `CudaStreamId`：

~~~text
GXF Entity
├── Tensor / VideoBuffer / Message
├── Metadata
├── Timestamp
└── CudaStreamId
       |
       +--> stream_cid
~~~

这里保存的不是裸 `cudaStream_t`，而是 GXF `CudaStream` component 的 cid。

为什么要多这一层？

因为 runtime 不只需要一个 CUDA handle，还要能够：

- 找到它所属的 GXF component；
- 确认它是 Holoscan 管理的 stream；
- 维护 stream→handle 的反向映射；
- 在 output Entity 上重新附着 stream identity；
- 让 allocator 和下游 Operator 使用同一个 lifecycle token。

所以关系更准确地写成：

~~~text
cudaStream_t
    ^
    |
CudaStreamHandle
    |
component cid
    ^
    |
CudaStreamId in message
~~~

## 4. receive_impl() 在业务代码拿消息时顺手提取 stream metadata

固定版本的 `GXFInputContext::receive_impl()` 在解析消息 metadata 后，会处理 Entity 上的 stream：

~~~cpp
// Handle any streams found in the entity
{
  PROF_SCOPED_EVENT(op_->id(), event_receive_streams);
  retrieve_cuda_streams(entity, input_name);
}
~~~

`retrieve_cuda_streams()` 再进入 `CudaObjectHandler`：

~~~cpp
gxf_result_t GXFInputContext::retrieve_cuda_streams(
    nvidia::gxf::Entity& message,
    const std::string& input_name) {
  auto context = gxf_context();
  auto object_handler = gxf_cuda_object_handler();

  if (object_handler == nullptr) {
    return GXF_FAILURE;
  }

  auto result =
      object_handler->streams_from_message(
          context, message, input_name);

  if (result != GXF_SUCCESS) {
    return result;
  }
  return GXF_SUCCESS;
}
~~~

因此业务代码后面调用 `receive_cuda_stream()` 时，不需要重新扫描原始消息。stream identity 已经被抽取进当前 Operator 的 handler 状态。

## 5. streams_from_message() 如何从 cid 恢复成可用 CUDA stream

真正的映射发生在 `CudaObjectHandler::streams_from_message()`：

~~~cpp
const auto maybe_cuda_stream_id =
    message.get<nvidia::gxf::CudaStreamId>();

if (maybe_cuda_stream_id) {
  const auto& cuda_stream_id =
      maybe_cuda_stream_id.value();

  auto& id_vector =
      received_cuda_stream_ids_[input_key];

  id_vector.emplace_back(*(cuda_stream_id.get()));

  const auto maybe_cuda_stream_handle =
      CudaStreamHandle::Create(
          context,
          cuda_stream_id->stream_cid);

  if (maybe_cuda_stream_handle) {
    auto& handle_vector =
        received_cuda_stream_handles_[input_key];

    auto& stream_handle =
        maybe_cuda_stream_handle.value();

    handle_vector.push_back(stream_handle);

    auto maybe_stream = stream_handle->stream();
    if (maybe_stream) {
      stream_to_stream_handle_[maybe_stream.value()] =
          stream_handle;
    }
  }
}
~~~

可以看到 runtime 同时维护了三层状态：

~~~text
received_cuda_stream_ids_
    message-level identity

received_cuda_stream_handles_
    runtime-owned GXF handle

stream_to_stream_handle_
    raw cudaStream_t -> managed handle
~~~

这个反向 map 后面非常重要：`set_cuda_stream(cudaStream_t)` 必须靠它判断一个裸 stream 是否真的属于当前 runtime 管理范围。

## 6. receive_cuda_stream() 的语义不是“取一下上游 cudaStream_t”

公开 API 看起来很轻：

~~~cpp
cudaStream_t stream =
    op_input.receive_cuda_stream("in");
~~~

但固定实现最终进入：

~~~cpp
auto stream = object_handler->get_cuda_stream(
    execution_context_->context(),
    input_name,
    allocate,
    sync_to_default);

return stream;
~~~

真正复杂的部分在 `get_cuda_stream_handle()`。

默认路径会尽量得到一个当前 Operator 的 **internal stream**，并把输入依赖汇聚到它。

## 7. allocate=true：为当前 Operator 分配内部 stream，再让所有输入等价汇入它

当需要独立 internal stream 时，源码先从 stream pool 分配或复用 `"_internal"`：

~~~cpp
auto maybe_stream =
    allocate_internal_stream(context, "_internal");

if (maybe_stream.has_value()) {
  output_stream = maybe_stream.value();

  int gpu_id = output_stream->dev_id();
  cudaSetDevice(gpu_id);

  if (received_iter !=
      received_cuda_stream_handles_.end()) {
    const auto& stream_handle_vec =
        received_iter->second;

    synchronize_streams(
        stream_handle_vec,
        output_stream,
        sync_to_default);
  }

  return output_stream;
}
~~~

语义就是：

~~~text
input stream A --+
input stream B --+----> internal stream C
input stream D --+
~~~

后续当前 Operator 在 C 上 launch kernel，就能依赖所有必要的 producer。

这不是 CPU join，而是 GPU dependency merge。

## 8. 不额外分配 stream 时，会选一个输入 stream 作为 internal stream

另一条路径会找到第一个有效输入 stream，把它作为当前 internal stream：

~~~cpp
for (size_t i = 0; i < vec_size; ++i) {
  const auto& stream_handle = stream_handle_vec[i];

  if (stream_handle.has_value()) {
    output_stream = stream_handle.value();

    if (i + 1 < vec_size) {
      std::vector<std::optional<CudaStreamHandle>>
          remaining_streams(
              stream_handle_vec.begin() + i + 1,
              stream_handle_vec.end());

      synchronize_streams(
          std::move(remaining_streams),
          output_stream,
          sync_to_default);
    }

    break;
  }
}
~~~

假设三条输入：

A --+
B --+----> Fusion
C --+
C ----/
~~~

可以选择 A 作为 Fusion 的执行 stream，再让 B、C 的 completion 成为 A 的前置依赖：

B --event--+
            +--> A continues -> Fusion kernel
C --event--+
C --event--/
~~~

如果 A 本来就是 producer stream，那么 A 自己不需要额外同步。

## 9. 真正的同步原语就是 cudaEventRecord + cudaStreamWaitEvent

固定版本最终不是调用 `cudaDeviceSynchronize()`，而是：

~~~cpp
if (!event_created_) {
  cudaEvent_t event;
  cudaEventCreateWithFlags(
      &cuda_event_,
      cudaEventDisableTiming);

  event_created_ = true;
}

for (auto& cuda_stream : cuda_streams) {
  if (cuda_stream == target_cuda_stream) {
    continue;
  }

  cudaEventRecord(
      cuda_event_,
      cuda_stream);

  cudaStreamWaitEvent(
      target_cuda_stream,
      cuda_event_);
}
~~~

这两句构造的关系是：

~~~text
producer stream
   |
   | previous kernels
   v
cudaEventRecord(E)
   |
   | E
   v
cudaStreamWaitEvent(target, E)
   |
   v
consumer work on target
~~~

注意 CPU 不需要等 event 完成。

CPU 只是把：

~~~text
“target stream 后面的 work 必须等 E”
~~~

提交给 CUDA runtime。

所以这个机制保留了 host-side asynchronous scheduling。

## 10. 同一个 stream 为什么是最低成本依赖路径

源码会直接跳过：

~~~cpp
if (cuda_stream == target_cuda_stream) {
  continue;
}
~~~

因为同一 CUDA stream 天然保证提交顺序：

~~~text
stream X:
producer kernel
      |
      v
consumer kernel
~~~

没有必要再插一对 event/wait。

这给 GPU pipeline 一个很重要的性能直觉：

> 如果连续 Operator 没有并发隔离需求，沿用同一 stream 往往是最低同步成本的路径。

不是 stream 越多越快。增加 stream 只有在能够创造有效并行时才有意义；否则还会增加 dependency bookkeeping。

## 11. receive_cuda_stream() 与 receive_cuda_streams() 不是同一个抽象层

`receive_cuda_stream()` 是“帮我选一个可执行 stream，并处理必要依赖”的高层 API。

而 `receive_cuda_streams()` 更像“把每条消息携带的 stream 原样给我”：

~~~cpp
auto streams =
    object_handler->get_cuda_streams(
        execution_context_->context(),
        input_name);

return streams;
~~~

后者不会自动完成完整的 stream 选择策略。

因此适用关系可以写成：

~~~text
普通 Operator:
receive_cuda_stream()
→ runtime 帮你完成 dependency merge

高级手动调度:
receive_cuda_streams()
→ 你拿到多个 stream
→ 自己决定目标 stream 和同步关系
~~~

如果使用后一种模式却忘了同步，就等于绕过了 runtime 为你提供的 happens-before 保障。

## 12. 为什么 set_cuda_stream() 不能接受任意裸 cudaStream_t

`OutputContext::set_cuda_stream()` 最终调用：

~~~cpp
auto gxf_result =
    object_handler->add_stream(
        stream,
        output_name);
~~~

而 handler 对裸 stream 的处理是反向查询：

~~~cpp
auto it =
    stream_to_stream_handle_.find(stream);

if (it != stream_to_stream_handle_.end()) {
  const auto& stream_handle = it->second;

  emitted_cuda_stream_cids_.insert_or_assign(
      output_port_name,
      stream_handle.cid());

  return GXF_SUCCESS;
}

return GXF_FAILURE;
~~~

所以任意用户自己 `cudaStreamCreate()` 得到的 handle，并不能天然传播。

原因不是 Holoscan “不喜欢外部 stream”，而是它缺少 runtime identity：

~~~text
只有 cudaStream_t
但没有
CudaStreamHandle / component cid / ownership mapping
~~~

没有 cid，就没法在输出 Entity 中构造正确 `CudaStreamId`。

因此可传播 stream 必须来自：

- 输入消息中已经被 runtime 恢复的 stream；
- `ExecutionContext::allocate_cuda_stream*()` 分配的 runtime-managed stream。

## 13. receive_cuda_stream() 为什么很多时候不需要再显式 set_cuda_stream()

这里有一个容易误解的细节。

`CudaObjectHandler` 会记录 `"_internal"` stream。输出端查询 stream cid 时，如果用户没有显式指定某个 output port，会回退到 internal stream：

~~~cpp
auto allocated_iter =
    allocated_cuda_stream_handles_.find("_internal");

if (allocated_iter !=
    allocated_cuda_stream_handles_.end()) {
  auto stream_handle = allocated_iter->second;
  return stream_handle.cid();
}

auto received_iter =
    received_cuda_stream_handles_.find("_internal");

if (received_iter !=
        received_cuda_stream_handles_.end() &&
    !received_iter->second.empty()) {
  auto stream_handle = received_iter->second[0];

  if (stream_handle.has_value()) {
    return stream_handle.value().cid();
  }
}
~~~

因此常规模式：

~~~text
receive()
receive_cuda_stream()
launch work on returned stream
emit()
~~~

可以自动把 internal stream 继续传播到 output。

而显式 `set_cuda_stream()` 更适合：

- root Operator 自己通过 ExecutionContext 分配 stream；
- 使用 `receive_cuda_streams()` 手动选择目标 stream；
- 一个 Operator 有多个输出且不同 output 需要不同 stream；
- 算法主动切换到了另一个 Holoscan-managed stream。

这比“每次 emit 前必须 set_cuda_stream”更准确。

## 14. emit() 如何把 stream identity 真正塞回消息

输出时，`emit_impl()` 先取得当前 output 对应的 stream cid：

~~~cpp
auto maybe_stream_cid =
    object_handler->get_output_stream_cid(
        output_name);

stream_found =
    maybe_stream_cid.has_value();

if (stream_found) {
  stream_cid = maybe_stream_cid.value();
}
~~~

如果创建一个新 Entity，会把 `CudaStreamId` 加进去：

~~~cpp
if (stream_found) {
  auto stream_result =
      add_stream_id_to_entity(
          gxf_entity.value(),
          stream_cid,
          true,
          true);

  if (stream_result != GXF_SUCCESS) {
    throw std::runtime_error(...);
  }
}
~~~

于是消息边界实际传递的是：

~~~text
Entity
├── payload
└── CudaStreamId(stream_cid)
~~~

下游再次经过 `streams_from_message()`，就能把这个 cid 恢复成 handle 和 raw CUDA stream。

依赖由此沿图一跳一跳传播。

## 15. 转发旧 Entity 时，为什么要更新而不是盲目再加一个 CudaStreamId

一个 Operator 可能直接转发收到的 GXF Entity，但已经在自己的 stream 上对 payload 做了新处理。

此时旧 Entity 里已有上游 `CudaStreamId`。

固定源码会更新 existing component：

~~~cpp
if (replace_existing && !is_new_entity) {
  auto existing_stream_id =
      get_cuda_stream_id(gxf_entity);

  if (existing_stream_id) {
    existing_stream_id.value()->stream_cid =
        stream_cid;
    return GXF_SUCCESS;
  }
}

const auto maybe_stream_id =
    add_cuda_stream_id(gxf_entity);

maybe_stream_id.value()->stream_cid =
    stream_cid;
~~~

为什么？

假设：

~~~text
A writes on stream A
↓
B receives same Entity
↓
B modifies tensor on stream B
↓
B forwards same Entity
~~~

如果仍保留“A 是最后 producer”的 dependency token，下游只等 A 就不够了。

因此转发后的消息必须表达 **最近一次有效 producer dependency**。

## 16. execution dependency 为什么还必须进入 MemoryBuffer

到这里我们只保证了“下游什么时候能读”。

但还有第二类问题：

> allocator 什么时候能重新使用这块显存？

考虑：

~~~text
Tensor refcount on CPU reaches zero
          |
          v
allocator sees buffer free
          |
          v
reuse block
~~~

可 GPU 可能还在某条 stream 上访问这个 buffer。

固定实现由 `propagate_stream_to_entity_memory_buffers()` 完成这件事：

~~~cpp
auto maybe_stream_handle =
    gxf::CudaStreamHandle::Create(
        gxf_ctx,
        stream_cid);

auto stream_result =
    maybe_stream_handle.value()->stream();

cudaStream_t cuda_stream =
    stream_result.value();

void* stream_ptr =
    static_cast<void*>(cuda_stream);

auto tensors =
    gxf_entity.findAllHeap<nvidia::gxf::Tensor>();

if (tensors) {
  for (auto tensor_handle : tensors.value()) {
    auto tensor_ptr = tensor_handle.value();

    tensor_ptr->memory_buffer().setStream(
        stream_ptr);
  }
}
~~~

VideoBuffer 也执行同样处理。

因此同一个 stream metadata 同时承担两类协议：

~~~text
consumer ordering:
“下游在什么时候可以使用数据？”

memory lifetime:
“allocator 在什么时候可以安全复用 storage？”
~~~

这就是 GPU zero-copy 真正困难的地方。

## 17. Sink Operator 为什么特别容易出现 use-after-recycle

普通 transform Operator 会继续 emit，于是 runtime 有机会把新的 stream dependency 写回输出 Entity。

Sink 没有输出。

假设：

~~~text
upstream Tensor
  最后记录 stream A
      |
      v
sink receive
      |
      +--> 在 stream B launch async kernel
      |
      +--> compute() 立刻返回
      |
      v
CPU 引用释放
      |
      v
allocator 只知道 A
      |
      v
buffer 被重新复用
      |
      v
B 仍在读旧内容
~~~

于是 Holoscan Tensor 提供了：

~~~cpp
bool set_deallocation_stream(cudaStream_t stream);
~~~

它只对由 Holoscan/GXF allocator 管理、拥有底层 MemoryBuffer 的 Tensor 有意义。

这一操作更新的是“最后使用这块 storage 的 GPU timeline”。

所以对 sink 来说：

~~~text
receive dependency
!=
最终 deallocation dependency
~~~

如果 sink 在新的 stream 上继续异步使用 buffer，就必须让 allocator 知道新的 last-use stream。

## 18. 多输入 Fusion 的完整时序

现在把所有机制放到一个机器人感知例子里。

两个传感器预处理：

~~~text
Camera preprocess (stream A) --+
                                +--> Fusion
LiDAR preprocess  (stream B) --+
~~~

Fusion 调用高层 stream API 后，可以得到 internal stream C。

GPU dependency：

~~~text
stream A: camera kernel ---- event EA --+
                                        +----> stream C waits ----> fusion kernel
stream B: lidar kernel ----- event EB --+
~~~

CPU 则可以很快完成：

~~~text
receive messages
→ enqueue event/wait dependency
→ launch fusion kernel
→ return scheduler
~~~

CPU 没有在这里等待 A、B、C 真正完成。

这才是“异步 GPU pipeline”的核心。

## 19. Fan-out zero-copy 为什么又会引出读写 discipline

假设同一个 Tensor 被共享给两个下游：

~~~text
          B
          ^
          |
A --------+--------> same Tensor storage
          |
          v
          D
~~~

storage zero-copy 以后，B 和 D 可能拿的是同一底层 buffer。

如果两者只读：

~~~text
B read
D read
~~~

只需要正确 dependency。

如果 B 原地写、D 同时读：

~~~text
B write
D read
~~~

即使 stream dependency 都合法，仍然存在逻辑 data race，因为两条 consumer 分支之间没有定义谁应该先于谁。

因此：

> zero-copy 只消除了 memcpy，不会自动提供共享可变数据的一致性协议。

runtime 仍需要 ownership、copy-on-write、immutable message 或显式跨分支同步中的某一种策略。

## 20. 默认 stream 的同步也不是 device-wide barrier

固定实现还允许把 target stream 的 event 接到 CUDA default stream：

~~~cpp
if (sync_to_default_stream) {
  cudaEventRecord(
      cuda_event_,
      target_cuda_stream);

  cudaStreamWaitEvent(
      cudaStreamDefault,
      cuda_event_);
}
~~~

它表达的仍然是特定依赖边，而不是：

~~~cpp
cudaDeviceSynchronize();
~~~

两者的差别很重要：

~~~text
event/wait:
约束必要的先后关系
其余 GPU work 仍可并行

device synchronize:
host 等待 device 上相关 work 完成
并行和调度自由度大幅下降
~~~

实时 pipeline 分析时，不能把所有“同步”都当成同一种成本。

## 21. 把一帧 GPU Tensor 的完整 dependency path 跑一遍

一条典型链可以写成：

~~~text
Producer kernel on stream A
    |
    v
emit Entity
├── Tensor storage
└── CudaStreamId(cid_A)
    |
    v
Receiver
    |
GXFInputContext::receive_impl()
    |
retrieve_cuda_streams()
    |
CudaObjectHandler::streams_from_message()
    |
cid_A
 -> CudaStreamHandle
 -> cudaStream_t A
 -> reverse mapping
    |
    v
receive_cuda_stream()
    |
select/reuse internal stream C
    |
cudaEventRecord(A, E)
cudaStreamWaitEvent(C, E)
    |
    v
Consumer kernel on C
    |
    v
emit()
    |
CudaStreamId updated to cid_C
MemoryBuffer stream updated to C
    |
    v
next Operator
~~~

这已经不是“CUDA stream metadata”这么简单，而是一条贯穿消息、执行和内存三层的协议。

## 22. 这套设计真正维护的五个不变量

一个 GPU message path 要正确，至少要同时守住：

~~~text
1. Storage validity
   consumer 使用期间，buffer 仍然有效

2. Producer completion
   consumer 开始真正依赖数据前，producer work 已建立 happens-before

3. Dependency identity
   CudaStreamId / Handle / raw stream 映射没有丢失或指向错误对象

4. Last-use lifetime
   allocator 不早于最后一次 GPU use 回收 storage

5. Mutation discipline
   zero-copy fan-out 时，不允许未同步的共享写读竞争
~~~

这五件事缺任何一件，都可能出现“偶尔错一帧”“高负载才崩”“显存看起来正常但结果随机”等最难定位的问题。

## 23. 对机器人 GPU Runtime 最值得迁移的设计原则

Holoscan 这一套机制揭示了 accelerator middleware 与普通 CPU pub/sub 的根本区别。

CPU 消息模型常能近似成：

~~~text
message delivery
≈
data readiness
~~~

GPU/异构系统更准确的是：

~~~text
message delivery
=
storage transfer/visibility
+
execution dependency transfer
+
memory lifetime transfer
~~~

所以所谓 GPU zero-copy 绝不能只讨论：

~~~text
“少了几次 cudaMemcpy？”
~~~

真正需要问的是：

~~~text
谁拥有 buffer？
producer work 在哪条 timeline 上结束？
consumer 在哪条 timeline 上开始？
跨 timeline 用什么 dependency primitive？
最后一个 GPU user 是谁？
allocator 根据什么判断可以 recycle？
fan-out 后谁有写权限？
~~~

Holoscan 的 `CudaStreamId`、`CudaStreamHandle`、event/wait、output propagation 和 `set_deallocation_stream`，本质上是在把这些问题变成 runtime 可追踪的协议。

这也是设计 VLA/VLM 推理流水线、相机预处理→TensorRT→后处理、GPU 点云链路或任何异构机器人 runtime 时最值得借鉴的一点：**不要只让数据跨模块流动，还要让“数据什么时候真的可用、什么时候真的可回收”的时间关系一起流动。**
