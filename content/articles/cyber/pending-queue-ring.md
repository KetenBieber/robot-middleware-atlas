# 有界消息缓存：`pending_queue_size=3` 的精确语义

相机每秒送来 60 帧，而一个重计算组件每秒只能处理 10 帧。如果接收线程等组件处理完再接下一帧，计算长尾就向上阻塞通信；如果给组件一只无界队列，处理到的图像又会越来越旧。因此先不追完整消息链，而要确定 Cyber RT 在两者之间放置的有界缓存究竟保留什么、淘汰什么，以及落后的消费者醒来时从哪里继续。

先把四个对象压缩成一句话：Component 是业务对象，Reader 是 channel 的接收端点，DataVisitor 保存“这个消费者读到哪里”的私有视图，CRoutine 是 Scheduler 以后会恢复执行的逻辑任务。`pending_queue_size` 是 Reader/Component 输入配置中的逻辑消息容量；它限制这只 DataVisitor 最多保留多少条待处理历史，而不是操作系统 socket 缓冲大小。

这样安排有一个明确原因。后面读到 `DataDispatcher::Dispatch()`、`DataVisitor::TryFetch()` 和 scheduler wakeup 时，所有逻辑都建立在“数据已经怎样存进缓存”之上。如果连 `pending_queue_size` 的准确语义都不知道，就无法判断慢消费者会积压、阻塞、丢旧还是丢新，也无法评估控制器拿到的数据有多老。

为方便从本篇独立阅读：`shared_ptr<T>` 是共享拥有消息对象的 C++ 句柄，复制句柄不会深复制消息；callback（回调）是消息到来后由框架调用的函数；CacheBuffer 是固定槽位的循环缓存，DataVisitor 是消费者私有的读取视图，CRoutine 是可被 Scheduler 暂停/恢复的任务。稍后说“环形缓冲”时，指写入位置绕回复用固定数组，而不是一个会自行增长的 `std::queue`。

本文继续固定 Apollo 提交 `d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa`，只阅读三个最小单元：`CacheBuffer`、`ChannelBuffer::Fetch()` 和 `DataVisitor::TryFetch()`。

本篇的 `text` 块是作者手算的状态表或时序示意，不是源码；C++ 块会明确标为固定提交摘录或教学实现。DAG 是有向无环图，这里指 Apollo 用来描述模块/组件及其依赖的配置图。

## 先确定数据契约：状态流要新鲜，事件流要完整

先看具体约束：相机以 60 Hz 发布图像（约每 16.7 ms 一帧），规划器以 10 Hz 消费（每 100 ms 才运行一次）。若规划器要估计“此刻障碍物在哪里”，它通常更需要最新状态；排队处理旧帧会让决策越来越滞后。反过来，若消息表示急停按钮按下或交易已提交，跳过中间消息就可能改变业务结果。本章讨论的是有限历史、允许覆盖的状态型输入，不是保证每个事件必达的日志队列。

无界 FIFO 在生产速率每秒比消费速率多 50 条时，一秒后就约积压 50 条，内存与数据年龄继续增长；共享 destructive pop 队列会让先运行的消费者取走消息，其他消费者看不到；单一 latest 槽则可能覆盖短暂脉冲。Cyber 的折中是每读者独立游标、缓存容量有限、写满覆盖旧数据，落后的读者跳向新数据。

## callback 之前的数据暂存边界

消息进入 Cyber 后，写入路径与业务回调路径并不必然处于同一操作系统线程；具体接收线程取决于传输路径。这里称写入侧为 producer，稍后读取的一侧为 consumer，payload 是消息内容。若接收路径直接等待 Processor，慢算法会把等待向上游传播；若只发“有消息”通知却不保存 payload，Processor 被提醒后仍无数据可读。

因此数据与唤醒必须分离：

```text
producer / 接收路径（线程随传输路径而异）
  -> 把 shared_ptr<Message> 放进有界 CacheBuffer
  -> 发出“有更新”事件

Processor 工作线程
  -> 被唤醒
  -> DataVisitor 按自己的游标从 CacheBuffer 取消息
  -> 执行 callback
```

缓存先落数据，通知后发生。即使多次通知被合并，只要缓存仍非空，消费者恢复后仍能读到消息。

为什么必须“有界”？摄像头可以 60 Hz 产生图像，规划器可能只有 10 Hz；如果 producer 永远快于 consumer，无界队列只会把延迟变成越来越大的内存占用。机器人控制通常更关心当前状态而不是完整历史，因此 Cyber 选择固定容量和覆盖最旧项。

## `size + 1` 环形缓冲的不变量

`CacheBuffer<T>` 的构造函数会多分配一个元素。先直接看构造和判满这两段**固定提交源码摘录**：

```cpp
explicit CacheBuffer(uint64_t size) {
  capacity_ = size + 1;
  buffer_.resize(capacity_);
}

bool Full() const {
  return capacity_ - 1 == tail_ - head_;
}
```

如果组件 DAG 配置把 `pending_queue_size` 写成 3，`vector` 会有 4 个槽位，但可保留消息上限仍是 3。多出的一个位置用来区分满和空：若只用 `head == tail` 表示空，那么写满并绕回时也会得到同一个条件；保留一个空槽后，`tail_ - head_ == capacity_ - 1` 才表示逻辑满。

环形缓冲不是会不断增长的队列，而是一段固定数组：逻辑序号走到末尾后通过取模重新使用前面的物理槽。这种结构避免稳态扩容，但槽位被复用前必须先定义旧消息如何淘汰。

这是环形缓冲常见技巧。如果只有两个取模后的下标，当 `head == tail` 时既可能表示“什么都没有”，也可能表示“刚好绕一圈写满”。保留一个逻辑空位后，两个状态便能区分。

Cyber 的 `head_`、`tail_` 不是不断在 `0..capacity-1` 内回绕的物理下标，而是单调递增的逻辑序号。访问 vector 时才通过取模函数映射：

```text
physical_index = logical_sequence % capacity
```

固定源码把三种数值分开暴露：`Head()` 返回最早有效的序号，`Tail()` 返回最近写入序号，`at(pos)` 才通过私有 `GetIndex()` 把逻辑序号映射为 vector 下标。注意这里的 `CacheBuffer::at()` 是项目自定义函数，内部对 `vector` 使用 `operator[]`；它只做取模，不验证逻辑序号是否有效，也不提供 `std::vector::at()` 的越界检查。正常的 DataVisitor 游标由 `Fetch()` 检查是否追上 producer 或落后于保留窗口；这不是对任意外部/损坏游标的通用验证，特别是大于 `Tail()+1` 的游标不属于有效调用约定。不能把这个方法当作安全的任意序号查询。下面直接看这些边界函数的**固定提交源码摘录**：

~~~cpp
uint64_t Head() const { return head_ + 1; }
uint64_t Tail() const { return tail_; }
const T& at(const uint64_t& pos) const { return buffer_[GetIndex(pos)]; }
uint64_t GetIndex(const uint64_t& pos) const { return pos % capacity_; }
~~~

所以不要把私有成员 head_ 和公开方法 Head() 当成同一个数：前者是最早有效序号之前的边界，后者才是 ring 当前保留的最早序号。

这样做的好处是容易比较消费者是否落后。若 consumer 想读逻辑序号 17，而 buffer 最老可读序号已经是 23，仅比较单调序号就能判断 17 已被覆盖；若只保存物理下标，两者都可能等于 1，信息已经丢失。

“单调”在这里有一个隐含的进程寿命前提：三个序号都是 `uint64_t`。`tail_ - head_` 的无符号减法在回绕处仍按模 $2^{64}$ 运算，但 `cursor < Head()` 这样的普通大小比较并不回绕安全，`tail_ + 1` 也会在最大值处变成 0。现实消息速率下走满 64 位几乎不可达，复刻时仍应把“计数器在进程寿命内不回绕、容量远小于计数空间”写成不变量，或采用 epoch / 回绕安全比较；构造参数也必须避免 `size + 1` 溢出。

## `std::vector<T>` 的存储与构造成本

`buffer_.resize(capacity_)` 会在构造阶段一次性建立固定数量的 `T` 槽位。稳定运行中的 `Fill()` 不调用 `push_back()`，因此不会因为元素数量增长而反复扩容或搬迁整段存储。

当 `T` 是 `std::shared_ptr<Message>` 时，每个槽位只保存一个小型智能指针对象。写槽通常改变引用计数和指针值，不会复制整个 protobuf payload。要区分“逻辑淘汰”和“物理覆盖”：满环写入时，`Fill()` 写在 `head_` 对应的边界槽，随后让 `head_` 前进；原最老消息从可读区间退出，但它所在的物理槽可能还保留着那份 `shared_ptr`，直到后续一轮写入才真的覆盖它。只有发生物理赋值覆盖时，旧 `shared_ptr` 才释放自己的强引用；若那时它已经是消息的最后一个所有者，对象才会析构。

这带来两个不同层次的性能结论：

```text
覆盖一个槽位
  -> 不复制 Message payload
  -> 仍会修改 shared_ptr 控制块的原子引用计数
  -> 最后一个引用消失时可能触发 Message 析构
```

所以“缓存写入是 O(1)”只描述算法复杂度，不表示执行时间完全固定。`shared_ptr` 的引用计数是原子更新；CPU 以 cache line（缓存行，一组一起装入处理器缓存的相邻字节）搬运内存，多核反复修改同一控制块时，该缓存行可能在核心之间来回转移（cache-line bouncing）。再加上对象析构和自定义 allocator，仍可能形成抖动。

## `Fill()` 的三条路

下面直接看 `Fill()` 的**固定提交源码摘录**，行尾注释用于点明动作、不是上游原注释：

```cpp
void Fill(const T& value) {
  if (fusion_callback_) {
    fusion_callback_(value);              // 多输入组件的主输入走融合路径
  } else if (Full()) {
    buffer_[GetIndex(head_)] = value;     // 写入当前边界槽，逻辑头随后前移
    ++head_;
    ++tail_;
  } else {
    buffer_[GetIndex(tail_ + 1)] = value; // 未满时扩展逻辑尾部
    ++tail_;
  }
}
```

单输入情况下只有后两条路。未满时，新消息写到 `tail_ + 1` 对应的物理槽，再推进尾边界。已满时，代码不会等待 consumer，也不会返回“队列满”；它把新值写到 `head_` 对应的逻辑边界槽，再让头尾边界同时前进。最老逻辑序号因此立即失效，但最老消息原来所在的物理槽不一定在这次写入中被覆盖。

`fusion_callback_` 是多输入组件留下的扩展点。主输入 M0 到来时，它可以不进入普通 M0 ring，而是立即与其他输入的最新值组成 tuple。这个分支会在多输入专章展开；当前只需知道单输入缓存没有隐藏的融合动作。

## 用 4 个物理槽手算 5 条消息

设逻辑容量是 3，内部 `capacity_ = 4`。初始 `head_ = 0`、`tail_ = 0`，可读逻辑区间可以理解为 `(head_, tail_]`：头边界本身不保存当前最老消息。

```text
vector physical slots: [0] [1] [2] [3]
logical sequence:        seq % 4
```

依次写入 A、B、C：

| 写入后 | head | tail | 保留的逻辑序号 | 物理槽内容 |
|---|---:|---:|---|---|
| A | 0 | 1 | 1 | `[ ] [A] [ ] [ ]` |
| B | 0 | 2 | 1, 2 | `[ ] [A] [B] [ ]` |
| C | 0 | 3 | 1, 2, 3 | `[ ] [A] [B] [C]` |

此时 `tail - head == 3`，逻辑容量已满。写入 D 时，`GetIndex(head_)` 是物理槽 0；D 写入后头尾同时加一：

| 写入后 | head | tail | 保留的逻辑序号 | 物理槽内容 |
|---|---:|---:|---|---|
| D | 1 | 4 | 2, 3, 4 | `[D] [A] [B] [C]` |

物理槽里的 A 尚未被覆盖，但它已经落在可读逻辑区间之外。ring 的有效性由逻辑边界决定，不能通过“某个槽里看起来还有旧指针”判断数据仍可读取。

再写 E 时复用物理槽 1：

| 写入后 | head | tail | 保留的逻辑序号 | 物理槽内容 |
|---|---:|---:|---|---|
| E | 2 | 5 | 3, 4, 5 | `[D] [E] [B] [C]` |

此时可读的是 C(seq 3)、D(seq 4)、E(seq 5)。物理顺序是槽 3、0、1，而逻辑顺序仍连续。`GetIndex()` 把两者连接起来。

这个例子说明覆盖策略不是“总覆盖数组下标最小的槽”，也不是在这次写入中直接改写最老消息所在的物理格。它先写到边界槽 0，让最老逻辑序号 1 失效；旧 A 仍暂存在槽 1，下一次写 E 时才由新值替换。也就是说，ring 的可读容量是 3，但满载后物理槽中可能短暂持有 4 份 `shared_ptr`，其中一份指向已经逻辑淘汰的消息。这不会让可读历史超过配置深度，却会把最后一次引用释放推迟到后续 producer 写入。

## `ChannelBuffer::Fetch()` 的非破坏性读取

传统队列通常由所有消费者共享头指针：一个消费者 pop 后，其他消费者再也看不到该元素。Cyber 的读取由 `ChannelBuffer::Fetch()` 和 visitor 私有游标完成，缓存本身不执行 destructive pop。

下面是固定提交中 `ChannelBuffer::Fetch()` 的关键判断摘录：

```cpp
if (*index == 0) {
  *index = buffer_->Tail();
} else if (*index == buffer_->Tail() + 1) {
  return false;
} else if (*index < buffer_->Head()) {
  auto interval = buffer_->Tail() - *index;
  // log dropped interval
  *index = buffer_->Tail();
}
m = buffer_->at(*index);
```

这里的 `index` 由调用者传入，`ChannelBuffer` 不在内部保存全局读头。单输入 `DataVisitor<M0>::TryFetch()` 持有 `next_msg_index_`。下面是固定提交源码摘录：

```cpp
bool TryFetch(std::shared_ptr<M0>& m0) {
  if (buffer_.Fetch(&next_msg_index_, m0)) {
    ++next_msg_index_;
    return true;
  }
  return false;
}
```

两段代码合起来才得到准确语义。

`next_msg_index_ == 0` 表示 visitor 尚未成功读取过。第一次 fetch 会直接把游标放到当前 `Tail()`，即读取当时最新消息，而不是从缓存中仍保留的最老历史开始。

若游标等于 `Tail() + 1`，消费者已经追上 producer，当前没有新数据。

必须区分两个容易混淆的名字：`head_` 是私有淘汰边界，`Head()` 返回 `head_ + 1`，即最早仍有效的逻辑序号。因此有效区间可以写成 `(head_, tail_]`，也可以写成 `[Head(), Tail()]`。Fetch 比较的是公开的 `Head()`：游标小于最早有效序号时记录丢失并跳到 `Tail()`；游标等于 `Head()` 时正好指向最早仍保留的消息，可以正常读取。

## 首次读取的 latest 语义

继续用 A、B、C 的例子。若 visitor 在三条消息都写入后第一次执行，`next_msg_index_` 仍是 0，`Fetch()` 会把它设为 tail 3，返回 C。A、B 即使还在 ring 里也不会交给这个 visitor。

```text
buffer holds: A, B, C
first TryFetch:
  next = 0
  next <- tail = 3
  return C
  next <- 4
```

这符合“组件开始执行时先处理最新状态”的倾向，却不同于“订阅之后保证按序交付每条消息”。如果业务必须重放所有事件，仅增大 `pending_queue_size` 也不够，因为初始游标语义和溢出跳转都优先最新值。

这一点对调试也很重要。看到 ring 中存在三条消息，不代表新启动的 Component 会依次执行三次 `Proc()`；它可能只从尾部开始。

## 慢消费者恢复：私有边界、公开 Head 与物理槽位

容量为 3、物理容量为 4 时，依次写入 A 到 G。写满后的私有状态是 head_=4, tail_=7；访问器给出 Head()=5, Tail()=7，所以仍有效的是 seq 5、6、7。物理槽仍可能留下无效旧内容：D(seq 4) 在槽 0，但“槽里还看得到 D”不等于它仍属于有效窗口。

```text
consumer next cursor: 4
buffer retains: 5, 6, 7
Fetch compares cursor 4 with public Head() 5
  -> records skipped interval
  -> index = tail = 7
  -> returns newest item 7
```

consumer 不会先处理 5 和 6，而是一次跳到 7。这比单纯的 overwrite-oldest 更激进：它不仅让生产者覆盖旧槽，也让已经落后的消费者主动放弃 ring 中尚存的部分历史。

~~~text
写满后的状态：head=4, tail=7, 物理槽=[D(seq4), E(seq5), F(seq6), G(seq7)]
有效逻辑区间：(4, 7] = {5, 6, 7}

visitor next cursor = 4
  4 < Head()(5) -> true
  index <- Tail()(7)
  at(7) -> slot[7 % 4] -> G(seq7)
~~~

这段回放把四个量分开了：私有 `head_` 是最早有效序号之前的边界，公开 `Head()` 是最早有效序号，`Tail()` 是最新序号，`at(pos)` 才把逻辑序号映射到物理槽。若绕过 Fetch 直接对旧序号 4 调用 `at(4)`，确实会访问槽 0 中过期的 D；但真实 Fetch 先比较 `4 < Head()(5)` 并改读 tail=7。教学版直接检查 `cursor < head_ + 1`，真实版检查 `index < Head()`，两者表达的是同一边界规则，而不是两种不同算法。

对姿态、速度、障碍物列表一类状态数据，这能缩短恢复后的数据年龄。对“门被打开”“订单已提交”一类每条都必须处理的事件，这种语义会破坏正确性。

## 读写两侧的锁边界

`CacheBuffer::Fill()` 本身不在函数内部获取 mutex（互斥锁：同一时刻只允许一个持锁线程进入被保护区）。上层 `DataDispatcher::Dispatch()` 在写每只 buffer 前取得 `buffer->Mutex()`；`ChannelBuffer::Fetch()` 读取时也取得同一只缓存 mutex。

```text
消息写入线程（取决于 transport）         Processor OS thread
      |                                        |
      | lock CacheBuffer mutex                 | lock same mutex
      | Fill(shared_ptr)                       | Fetch(index, shared_ptr)
      | unlock                                 | unlock
```

这使 head、tail 和槽位赋值形成简单临界区，容易验证，不需要为每个字段设计复杂内存序。代价是 producer 和 consumer 会在同一 mutex 上竞争；同一 channel 注册多只 DataVisitor 时，Dispatcher 还要串行获取多只独立 buffer mutex。

为什么不用一只全局锁？每个 DataVisitor 的缓存独立加锁后，一个组件正在 fetch 不会阻塞另一个组件读取自己的 ring。Dispatcher 仍需逐只写入，但锁竞争被限制在具体 consumer 的 buffer 上。

为什么不直接用 lock-free ring？这里“无锁”是指不靠 mutex 把所有操作串行化；严格术语中的 lock-free 只保证系统整体持续有操作取得进展，并不保证某一个线程有等待时间上限。无锁结构可以减少互斥等待，却要解决槽位覆盖与 `shared_ptr` 生命周期、单调序号发布、读写内存序和落后游标的并发一致性。对小临界区，mutex 的可读性和正确性可能比理论上的无锁更有价值。是否成为实际瓶颈仍需结合消息频率、consumer 数量和临界区耗时判断。

## 时间和空间复杂度只是第一层性能结论

单次 `Fill()` 与 `Fetch()` 的索引计算是 `O(1)`，buffer 存储是 `O(P)`，`P` 为逻辑容量。构造后 vector 不再扩容，内存上界明确。

但端到端 Dispatch 不是 `O(1)`。若同一类型、同一 channel 注册了 `B` 只 visitor buffer，Dispatcher 要逐只执行 weak pointer lock、mutex lock 和 shared pointer assignment，整体约为 `O(B)`。

缓存局部性也有两面。vector 槽位连续，遍历和取模定位简单；消息 payload 通常位于另一块堆内存，通过 shared pointer 间接访问。`shared_ptr` 控制块还可能被 producer 和多个 consumer 核心共同修改，形成 cache-line bouncing。

固定容量避免了正常写入时的 vector 扩容，却不能保证没有任何分配：transport 反序列化可能先分配 `Message`，多输入融合会分配 tuple，最后一个引用释放还可能触发复杂析构。

## queue depth 与控制数据年龄

设 producer 周期为 `Tp`，逻辑容量为 `P`。若消费者顺序处理且没有溢出，单由队列历史造成的最老样本年龄接近：

```text
queue_age ~= (P - 1) * Tp
```

100 Hz 传感器的 `Tp = 10 ms`。`P = 10` 时，缓存历史窗口约 90 ms；即使每条消息都还“有效”，对低时延闭环也可能已经太老。

1 kHz 编码器的 `Tp = 1 ms`。`P = 10` 对应约 9 个控制周期。若控制器的目标是当前状态而不是重放轨迹，深队列会把短暂过载变成长时间追赶。

`P = 1` 最接近 keep-last：新值很快覆盖旧值，恢复时优先看到当前状态。它减少数据年龄，却更容易丢失短脉冲事件，也无法吸收 producer 的瞬时 burst。

所以 queue depth 不能用“越大越安全”来选择。它同时定义了内存上界、可吸收突发长度、最大历史窗口和过载后恢复策略。

## 与其他四种队列语义比较

理解设计时，最好问它没有选择什么。

阻塞有界队列在满时让 producer 等待，适合不能丢数据、且允许背压传播的任务；用于 transport 接收线程时，慢 consumer 可能阻止整个数据入口继续工作。

drop-new 队列保留已有历史，拒绝新消息。它适合已排队事件比新事件更重要的工作，但会让状态型控制器持续处理旧世界。

无界队列保留全部历史，短时 burst 友好，持续过载时内存和时延没有上限。

单槽 latest value 不保留历史，读取者永远看到最新状态。它的新鲜度最好，却无法顺序处理任何中间变化。

Cyber 的实现位于“有限历史”和“latest value”之间：正常时可顺序读一段，溢出或首次读取时主动追最新。这种混合语义正是机器人状态流的偏好，而不是普适消息可靠性语义。

## 最小 ring 实现及其不变量

可以先不用协程和 transport，只实现一个单线程泛型 ring。比类名更重要的是写清以下不变量：

```text
1. physical capacity = logical capacity + 1
2. valid logical sequence lies in (head_boundary, tail]
3. tail - head_boundary never exceeds logical capacity
4. full write advances both boundaries and preserves newest P items
5. each consumer owns its next logical sequence
6. a lagging consumer never reads a slot under a new logical identity
7. overflow recovery policy is explicit: jump newest, oldest retained, or fail
```

下面是教学用最小 ring 实现，不是 Apollo 源码；它只覆盖单线程语义，后文再讨论如何加锁：

```cpp
// 教学最小例子：调用者保证序号在进程寿命内不回绕
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

template <typename T>
class LatestBiasedRing {
 public:
  explicit LatestBiasedRing(std::size_t n)
      : slots_(CheckedPhysicalCapacity(n)) {}

  void Fill(const T& value) {
    if (tail_ - head_ == slots_.size() - 1) {
      slots_[Index(head_)] = value;
      ++head_;
      ++tail_;
    } else {
      slots_[Index(tail_ + 1)] = value;
      ++tail_;
    }
  }

  bool Fetch(std::uint64_t& cursor, T& out) const {
    if (tail_ == 0) return false;
    if (cursor == 0) {
      cursor = tail_;
    } else if (cursor == tail_ + 1) {
      return false;
    } else if (cursor < head_ + 1) {
      cursor = tail_;
    }
    out = slots_[Index(cursor)];
    ++cursor;
    return true;
  }

 private:
  static std::size_t CheckedPhysicalCapacity(std::size_t n) {
    if (n == 0 || n == std::numeric_limits<std::size_t>::max()) {
      throw std::invalid_argument("ring capacity must be in [1, SIZE_MAX-1]");
    }
    return n + 1;
  }

  std::size_t Index(std::uint64_t seq) const {
    return seq % slots_.size();
  }
  std::vector<T> slots_;
  std::uint64_t head_ = 0;
  std::uint64_t tail_ = 0;
};
```

为缩小接口，这个教学版本在 `Fetch()` 成功时直接递增 cursor；固定源码把读取与递增分在 `ChannelBuffer::Fetch()` 和 `DataVisitor::TryFetch()` 两层，语义相同但职责位置不同。给教学版本加 mutex 时，`Fill()` 和整个 `Fetch()` 必须由同一把锁保护，不能只锁槽位赋值而把 head、tail 和 cursor 判断留在锁外。

先用 A、B、C、D、E 的表格验证每次边界和槽位，再加入 mutex；最后用两个线程制造 consumer 落后，确认 cursor 跳转不会读到被复用槽中的旧身份。

等这个最小单元正确后，再把它包进 `ChannelBuffer`，给它加 channel id；再由 `DataVisitor` 持有 cursor；再由 Dispatcher 按 channel 扇出写入。这样的实现顺序能让每一层只增加一个新问题。

## 从存储语义过渡到消息分发

现在已经知道：DataVisitor 拥有怎样的缓存，`Fill()` 对新消息做什么，`TryFetch()` 又如何决定返回哪一条。紧接着的[分发章节](dispatcher-notifier.md)会说明 Dispatcher 如何扇出写入、Notifier 为什么只发事件；之后的[执行面章节](croutine-wakeup.md)再追 Scheduler 如何选中协程。这样，数据保存、事件传播和 CPU 执行不会被混成一条含糊的“消息队列”。

当 Receiver 调用 `Dispatch(msg)` 时，数据会先进入这里解释过的固定容量 ring；Notifier 只负责让 Processor 再次检查它；CRoutine 恢复后，`TryFetch()` 使用这里解释过的私有游标取消息。过载时 `Proc()` 看到的是最新值而非完整历史，也正是由本章两段源码共同决定的。
