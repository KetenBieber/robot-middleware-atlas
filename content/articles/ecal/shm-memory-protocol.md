# SHM 数据路径：双层头、Buffer 轮换与零拷贝锁域

共享内存常被简化为“Publisher 写一块内存，Subscriber 直接读”。真实实现还必须解决容量变化、跨进程互斥、新数据通知、慢读者、重复通知、对象销毁与可选确认。

eCAL 将这些职责拆到 `CMemoryFile`、`CSyncMemoryFile`、`CDataWriterSHM` 和 `CMemFileObserver`。本章从内存布局开始，追踪一次写入和一次读取。

本章固定 eCAL 源码为 commit `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717`。

## 先从 30 Hz、8 MB 图像的失败实现开始

假设相机每 33 ms 产生一帧 8 MB 图像，同一台机器上的感知进程要消费它，另一台机器上的监控进程也要订阅它。先不要把 eCAL 的类名带进来，单独看这个工程问题：发布者必须在下一帧到来前完成写入，订阅者不能读到半帧，慢回调不能把发布线程永久卡住，进程退出后旧的共享对象也不能让新进程误读。

最直接的实现通常有三个版本，但它们分别暴露了不同的边界。

1. **把指针通过 socket 发给订阅者。** 指针只在当前进程的虚拟地址空间中有意义；另一个进程即使拿到相同的数值，也可能没有映射到同一页，解引用会崩溃。就算两个对象恰好映射到了相同地址，发布者释放或复用对象后，订阅者保存的裸指针仍然会悬空。
2. **每帧通过 socket 发送 8 MB。** 这会把大块数据放回内核 socket 缓冲区和用户态缓冲区之间的复制路径。生产者一旦比消费者快，发送调用就会受到反压；把发送线程改成异步线程后，又必须额外设计队列、丢帧策略和关闭协议。
3. **只放一个共享槽位并立即通知。** 如果写线程先写 header、发 event，再执行 `memcpy`，读线程可能被唤醒后看到新序号，却读到一半新、一半旧的图像。即使把通知放到 `memcpy` 之后，慢读者仍可能持有旧数据时被下一帧覆盖。

因此，共享内存不是“把一个指针变成全局变量”，而是两个进程分别把同一个命名对象映射到自己的虚拟地址空间：Linux 固定实现用 `shm_open`/文件描述符找到对象，`ftruncate` 定容量，`mmap(..., MAP_SHARED, ...)` 建立映射；每个进程得到的虚拟地址可以不同，更新却对映射同一共享对象的进程可见。下面直接看 Linux memory-file backend 的分配与映射代码；`MAP_SHARED` 的操作系统语义可参考 [`mmap(2)`](https://man7.org/linux/man-pages/man2/mmap.2.html)。映射不等于所有页已预先加载：首次访问某页可能触发 page fault，严格实时路径要预触页并测量。


```cpp
bool AllocFile(const std::string& name_, const bool create_, SMemFileInfo& mem_file_info_)
{
  {
    const std::lock_guard<std::mutex> lock{eCAL::posix::GetUmaskCreationMutex()};
    const eCAL::posix::ScopedUmaskRestore scoped_umask{000};

    mem_file_info_.name = name_.size() ? ((name_[0] != '/') ? "/" + name_ : name_) : name_;
    if (create_)
    {
      mem_file_info_.memfile = ::shm_open(mem_file_info_.name.c_str(), O_CREAT | O_RDWR | O_EXCL,
                                          S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP | S_IROTH | S_IWOTH);
      if (mem_file_info_.memfile == -1 && errno == EEXIST)
      {
        mem_file_info_.exists = true;
        mem_file_info_.memfile = ::shm_open(mem_file_info_.name.c_str(), O_RDWR,
                                            S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP | S_IROTH | S_IWOTH);
      }
    }
    else
    {
      mem_file_info_.memfile = ::shm_open(mem_file_info_.name.c_str(), O_RDONLY,
                                          S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP | S_IROTH | S_IWOTH);
      mem_file_info_.exists = true;
    }
  }

  if (mem_file_info_.memfile == -1)
  {
    mem_file_info_.memfile = 0;
    mem_file_info_.name = "";
    mem_file_info_.exists = false;
    return(false);
  }
  mem_file_info_.size = 0;
  return(true);
}

bool MapFile(const bool create_, SMemFileInfo& mem_file_info_)
{
  if (mem_file_info_.mem_address == nullptr)
  {
    if (create_)
    {
      if (::ftruncate(mem_file_info_.memfile, mem_file_info_.size) != 0)
      {
        std::cerr << "ftruncate failed (memfile::os::MapFile): " << mem_file_info_.name
                  << " errno: " << strerror(errno) << std::endl;
      }
    }

    int prot = PROT_READ;
    if (create_) prot |= PROT_WRITE;

    mem_file_info_.mem_address = ::mmap(nullptr, mem_file_info_.size, prot,
                                        MAP_SHARED, mem_file_info_.memfile, 0);
    if (mem_file_info_.mem_address == MAP_FAILED)
    {
      mem_file_info_.mem_address = nullptr;
      std::cerr << "mmap failed (memfile::os::MapFile): " << mem_file_info_.name
                << " errno: " << strerror(errno) << std::endl;
      return(false);
    }
  }
  return(true);
}
```

创建方用 `shm_open(O_CREAT | O_EXCL)` 争取新名字；若名字已存在，则重新打开既有对象。随后 `MapFile()` 为创建者设置长度、按读写权限选择 `prot`，最后以 `MAP_SHARED` 将文件描述符映射进当前进程。进程 A 的 `mem_address` 与进程 B 的地址可以不同，因为两者持有的是各自虚拟地址空间里的映射；共享的是内核管理的 backing object。`GetUmaskCreationMutex()` 只在本进程内序列化临时修改 umask，不能协调另一个进程同时改 umask。还有一个需要读代码才能看到的错误边界：`ftruncate()` 失败时这里只打印日志，仍继续 `mmap()` 并可能返回成功；因此后续写入失败不一定发生在 sample writer 层，容量建立本身就可能没有按请求完成。

普通 `std::mutex` 默认只保证共享同一进程地址空间的线程互斥，不能直接保护另一个进程，所以还需要可命名、可跨进程打开的同步对象。

通知也不能被理解成“队列里已经有一条消息”。event 更像一个提示：可能有新内容。读者醒来后仍要重新获取读锁、检查 header、比较 publisher clock，再决定这次是否真的产生了新 sample；重复通知、丢失通知和观察者关闭都必须由读取状态机收敛。

Linux 版本的 send event 把 `pthread_mutex_t`、`pthread_cond_t` 和一字节 `set` 放在 process-shared 映射中。`Set` 在 mutex 下写 `set=1` 后 signal；`Wait` 先检查谓词，若未置位就进入 `pthread_cond_wait` 循环，返回时再次检查并清除状态。条件变量不是存消息的队列；`pthread_cond_signal` 只通知一个等待者，调用返回也不表示那个线程已经运行。POSIX/Linux 的 `pthread_cond_wait` 在等待期间原子释放 mutex、阻塞线程，返回前重新取得 mutex；虚假唤醒也要求循环重查谓词。eCAL 调用 `pthread_cond_wait`，不直接调用 futex；Linux 上 futex 是用户态同步在竞争时请求内核睡眠/唤醒的机制，用户态状态和内核调度仍是两层，可分别参考 [`pthread_cond_wait(3)`](https://man7.org/linux/man-pages/man3/pthread_cond_wait.3.html) 与 [`futex(2)`](https://man7.org/linux/man-pages/man2/futex.2.html)。

下面先写一个教学最小模型。它不是 eCAL 原始源码，而是把必须存在的状态压缩成一个槽位，帮助我们看清提交顺序。真实实现随后会把槽位封装到 `CMemoryFile`/`CSyncMemoryFile`，把通知交给 named event，把多个槽位交给 `CDataWriterSHM`。

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
// 教学示例：只展示“锁内完成提交，解锁后通知”的数据流。
struct SharedSlot {
  std::mutex process_local_only; // 真实跨进程实现不能直接使用它
  uint64_t sequence = 0;
  uint64_t size = 0;
  std::array<std::byte, 8 * 1024 * 1024> bytes;
};

void Write(SharedSlot& slot, ByteView input) {
  std::scoped_lock lock(slot.process_local_only);
  std::memcpy(slot.bytes.data(), input.data, input.size);
  slot.size = input.size;
  ++slot.sequence;              // header 与 payload 在同一临界区提交
  // unlock 后才 signal send_event
}

bool Read(SharedSlot& slot, std::vector<std::byte>& local) {
  std::scoped_lock lock(slot.process_local_only);
  const auto n = slot.size;
  const auto seq = slot.sequence;
  if (n > slot.bytes.size()) return false; // 先校验边界
  local.assign(slot.bytes.begin(), slot.bytes.begin() + n);
  // 解锁后 callback(local)，不把业务 WCET 带入临界区
  return seq != 0;
}
```

这个模型故意保留了几个缺陷：锁只是进程内的、只有一个槽位、没有 resize、没有 ACK。它的价值在于先固定一个不变量：**读者只能看到已经完成 header/payload 写入的 sample，通知不能早于这个提交点。** 如果把 signal 放到 `memcpy` 前面，具体失败就是读线程在 `sequence` 已更新而 `size`/payload 尚未完成时运行 callback；如果把 callback 放在锁内，40 ms 的算法回调会让发布线程在下一帧到来时等待同一个锁；如果不轮换槽位，写线程最终只能覆盖读线程正在复制的那一帧。后面的每个 eCAL 对象，都是在解决这些最小模型暴露出来的一个缺陷，而不是凭空增加抽象层。

固定版本源码中，这些职责的对应关系是：`CDataWriterSHM::Write` 选择并轮换 `CSyncMemoryFile`；`CSyncMemoryFile::Write/SyncContent` 在命名互斥保护下写入 sample header 与 payload，再发出通知；`CMemFileObserver::Observe` 等待 event、重新获取读权限并检查 clock。copy 模式先复制到 observer 本地 vector，zero-copy 模式则把映射视图和读锁的有效期延长到同步 callback 结束；下文会直接摘录这几段调用。

理解了这个失败—修复链条，再回头看下面的双层 Header，就能知道每个字段为什么存在：第一层描述映射容器能容纳多少字节，第二层描述这一条消息的大小、序号和发布者时钟；前者服务内存管理，后者服务消息一致性。两层都不是装饰字段，任何一层不可信都可能把读指针推进到映射边界之外。

## 两层 Header 与 Payload

共享映射的布局为：

```text
offset 0
+-----------------------------------+
| CMemoryFile::SInternalHeader      |
| int_hdr_size                      |
| cur_data_size                     |
| max_data_size                     |
+-----------------------------------+ <- GetReadAddress()
| SMemFileHeader                    |
| hdr_size                          |
| data_size                         |
| publisher id / clock / time/hash |
| zero_copy                         |
| ack_timeout_ms                    |
+-----------------------------------+
| user payload bytes                |
+-----------------------------------+
```

第一层由通用 memory-file 管理器使用，描述映射容量和当前有效区。第二层由 eCAL pub/sub 使用，描述一条业务 sample。

`CMemoryFile::GetReadAddress()` 返回 internal header 后的位置；Observer 还要再跳过 `SMemFileHeader::hdr_size` 才得到 payload。

## InternalHeader 与 SampleHeader 的职责

InternalHeader 解决“这块共享对象当前可容纳多少字节”，即 allocator/container 问题。

SampleHeader 解决“这里存的是哪一条 Publisher 消息”，即 messaging 问题。

分层后，同一个 `CMemoryFile` 基础设施可以服务不同上层内容；pub/sub 不需要重复实现 mapping、resize 和 named mutex。

但两层 size 都必须相互校验：

```text
sample_header.hdr_size <= cur_data_size
sample_header.data_size <= cur_data_size - hdr_size
cur_data_size <= max_data_size
```

只信任任一 header 都可能越界访问损坏或版本不兼容的映射。

## Native struct layout 的 ABI 风险

`SInternalHeader` 对 packing 有显式处理，而 `SMemFileHeader` 依赖本地 C++ 结构布局。字段间 padding、bool 大小和 alignment 可能受编译器与平台影响。

共享内存两端若使用相同发行版和 ABI，原生布局速度快、代码直接。跨编译器、跨架构或独立实现互通时，它不等同于稳定 wire format。

可移植重建应显式编码固定宽度字段：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct WireHeaderV1 {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t header_size;
  std::uint64_t payload_size;
  std::uint64_t publisher_clock;
  // fixed endian and reserved bytes
};
```

## 命名对象建立跨进程同步

普通 `std::mutex` 只能在共享同一进程地址空间的线程间工作。eCAL SHM 使用可由多个进程按名称打开的对象：

- memory file：共享数据；
- named mutex：保护读写；
- send event：通知新 sample；
- ack event：可选确认读取。

名称通过 Publisher registration 告诉 Subscriber。发现信息因此是打开数据面资源的目录。这里要避免把 Linux 和 Windows 的实现说成同一种“named mutex/event”：Linux 固定实现把 `pthread_mutex_t`、`pthread_cond_t` 和状态字节放在共享对象中，并设置 `PTHREAD_PROCESS_SHARED`；普通 memfile 配置没有把该 mutex 设为 robust，不能承诺 owner death 后由内核自动修复。Windows 则使用命名 kernel mutex 和 auto-reset event，等待可返回 `WAIT_ABANDONED`，但是否按 abandoned-owner 路径恢复还取决于 recoverable 配置。下面会按平台直接对照同步操作代码。

Linux send event 不是逐消息计数器：共享结构里是一个 `set` 字节，`Set` 把它置 1 并 signal，`Wait` 在 mutex 与谓词下检查后消费为 0；同一次 Wait 之前多次 Set 会合并。Windows auto-reset event 也不会累积无限个未处理样本。命名对象还带来清理问题：Linux 共享内存名称可能在异常退出后残留，映射建立也不等于所有页已驻留；第一次触页可能发生 page fault，这是 OS 层推导而非 eCAL 的实时保证。

Linux 实现把状态字节和 pthread 同步对象放在共享映射中。`PTHREAD_PROCESS_SHARED` 让不同进程映射到不同虚拟地址的线程也能使用同一把 mutex/condvar；真正等待时，pthread 在原子地释放 mutex 的同时把当前线程阻塞，返回前重新取得 mutex。固定提交的 event 原语如下：


```cpp
struct alignas(8) named_event
{
  pthread_mutex_t mtx;
  pthread_cond_t  cvar;
  uint8_t         set;
};
typedef struct named_event named_event_t;

bool named_event_initialize(named_event_t* evt)
{
  pthread_mutexattr_t shmtx;
  pthread_mutexattr_init(&shmtx);
  pthread_mutexattr_setpshared(&shmtx, PTHREAD_PROCESS_SHARED);

  pthread_condattr_t shattr;
  pthread_condattr_init(&shattr);
  pthread_condattr_setpshared(&shattr, PTHREAD_PROCESS_SHARED);
  pthread_condattr_setclock(&shattr, CLOCK_MONOTONIC);

  pthread_mutex_init(&evt->mtx, &shmtx);
  pthread_cond_init(&evt->cvar, &shattr);
  pthread_mutexattr_destroy(&shmtx);
  pthread_condattr_destroy(&shattr);
  evt->set = 0;
  return true;
}

void named_event_set(named_event_t* evt_)
{
  pthread_mutex_lock(&evt_->mtx);
  evt_->set = 1;
  pthread_cond_signal(&evt_->cvar);
  pthread_mutex_unlock(&evt_->mtx);
}

bool named_event_wait(named_event_t* evt_, struct timespec* ts_)
{
  pthread_mutex_lock(&evt_->mtx);
  if (evt_->set)
  {
    evt_->set = 0;
    pthread_mutex_unlock(&evt_->mtx);
    return true;
  }

  int ret(0);
  while ((ret == 0) && (evt_->set == 0))
  {
    if (ts_)
      ret = pthread_cond_timedwait(&evt_->cvar, &evt_->mtx, ts_);
    else
      ret = pthread_cond_wait(&evt_->cvar, &evt_->mtx);
  }
  if (ret == 0) evt_->set = 0;
  pthread_mutex_unlock(&evt_->mtx);
  return (ret == 0);
}
```

mutex 同时保护谓词 `set`，所以既不会丢掉“已经发生”的状态，也不会把一次 event 变成无限消息队列；两次 `Set` 在一次 `Wait` 前合并成 `set = 1`。`while` 而不是 `if` 很重要：条件变量允许虚假唤醒，wait 返回只意味着线程重新拿到了 mutex 并应重查谓词，不代表有新 sample。pthread 再将竞争等待映射到 Linux 内核等待机制（常见实现会使用 futex）；这仍只是让线程从 blocked 转为 runnable 的必要条件，CPU 何时调度到 observer 是另一阶段。此实现也没有配置 robust mutex：持锁进程崩溃后的恢复不能由 `PTHREAD_PROCESS_SHARED` 单独保证。

## CDataWriterSHM 管理多个独立文件

Publisher 的 SHM writer 保存 `std::vector<std::shared_ptr<CSyncMemoryFile>>`。每项代表独立命名 memory file，不是一个 mmap 内的环形数组。`CDataWriterSHM::Write()` 选择当前项后推进普通 `size_t m_write_idx`，多文件时强制 full write，再按 vector 大小回绕：

```text
write #0 -> buffer 0
write #1 -> buffer 1
write #2 -> buffer 2
write #3 -> buffer 0
```

多文件让 Subscriber 仍在读 file 0 时，Publisher 有机会写 file 1。它用额外映射、event 与句柄换取读写并行；每个唯一 memfile 有自己的 `CMemFileObserver` 线程，pool 另有清理线程。`CMemFileThreadPool` 不是固定大小的通用消费线程池。buffer 数量、writer 轮换与 observer 创建的真实过程分别由 `CDataWriterSHM::SetBufferCount/Write` 和 `CMemFileThreadPool::ObserveFile` 展开。

先看 buffer 数量如何转化成实际文件对象，再看每个写调用怎样推进索引：

接着看 `Write` 的真实实现：

```cpp
bool CDataWriterSHM::PrepareWrite(const SWriterAttr& attr_)
{
  bool ret_state(false);
  m_write_idx %= m_memory_file_vec.size();
  ret_state |= m_memory_file_vec[m_write_idx]->CheckSize(attr_.len);
  return ret_state;
}

bool CDataWriterSHM::Write(CPayloadWriter& payload_, const SWriterAttr& attr_)
{
  const bool force_full_write(m_memory_file_vec.size() > 1);
  const bool sent = m_memory_file_vec[m_write_idx]->Write(payload_, attr_, force_full_write);
  m_write_idx++;
  m_write_idx %= m_memory_file_vec.size();
  return sent;
}

bool CDataWriterSHM::SetBufferCount(size_t buffer_count_)
{
  if (m_memory_file_vec.size() == buffer_count_) return true;
  if (buffer_count_ < 1) return false;

  SSyncMemoryFileAttr memory_file_attr = {};
  memory_file_attr.min_size        = m_attributes.memfile_min_size_bytes;
  memory_file_attr.reserve         = m_attributes.memfile_reserve_percent;
  memory_file_attr.timeout_open_ms = PUB_MEMFILE_OPEN_TO;
  memory_file_attr.timeout_ack_ms  = m_attributes.acknowledge_timeout_ms;

  size_t memory_file_size(0);
  if (!m_memory_file_vec.empty())
    memory_file_size = m_memory_file_vec[0]->GetSize();
  else
    memory_file_size = memory_file_attr.min_size;

  m_memory_file_vec.clear();
  while (m_memory_file_vec.size() < buffer_count_)
  {
    auto sync_memfile = std::make_shared<CSyncMemoryFile>(
      m_memfile_base_name, memory_file_size, memory_file_attr, m_memfile_map);
    if (sync_memfile->IsCreated())
      m_memory_file_vec.push_back(sync_memfile);
    else
    {
      m_memory_file_vec.clear();
      return false;
    }
  }
  return true;
}
```

`SetBufferCount()` 先从已有第一个文件取容量（或使用最小容量），然后清空旧 vector，再逐个创建新 `CSyncMemoryFile`。这不是先建好新集合再原子交换的事务：中途创建失败会清空 vector 并返回 false，原 buffer 集合已经不在。正常重建要靠外层发现状态变化并重新发布 registration。热路径的 `PrepareWrite/Write` 直接读写普通 `m_write_idx`，片段中没有互斥；若同一 `CDataWriterSHM` 被两个业务线程并发 Write，索引递增本身就构成 data race，两个调用也可能错误地落到同一 memfile。上游需要调用者串行化，或复刻版须把索引分配纳入明确同步；增加文件数只能提供读写轮换余量，不能修复并发写者。

若发布频率远高于 callback，Publisher 最终仍会绕回被占用文件。文件数 B 只提供有限轮换余量，不是保存 B 条待消费消息的 FIFO：每个文件上仍是一个当前 sample，event 也只是合并通知。慢消费者可观察到 clock 跳跃/中间帧丢失；若正好轮到被锁占用的文件，写端还会等待或进入容量/重建失败路径。

## PrepareWrite 检查容量并重建

当前 payload 超过 memfile 容量时，writer 根据 payload 与 reserve 百分比申请更大映射：

```text
required = sample_header + payload
new_capacity = required * (1 + reserve_percent)
```

Reserve 减少大小逐步增长时频繁重建。代价是额外共享内存常驻。

重建可能改变 memory-file identity/list。Publisher 随后需要重新发送 registration，让 Subscriber observer 打开新对象。Resize 因而不是纯本地内存操作，而是控制面变化。

## 写端临界区

`CSyncMemoryFile` 的一次写入按以下顺序：

```text
acquire named mutex
  -> obtain write address
  -> write SMemFileHeader
  -> copy/write payload
  -> update current size
release named mutex
signal send event
optional: wait ACK events
```

通知必须在内容完整并解锁后发生。若先 signal，Subscriber 可能醒来并看到半写 header；若持锁 signal 后继续长操作，Observer 醒来却只能等待 mutex。

Header 和 payload 必须在同一临界区提交，形成一条原子可见 sample。

写端真正拿到的是 `CPayloadWriter&`、消息元数据和 `force_full_write`。调用位置从 `CDataWriterSHM::Write` 选中的单个同步 memfile 进入；下面是固定提交 `1ec0ea2fe5e5e61e3e492be6128c27cc6026d717` 中 `CSyncMemoryFile::Write()` 的连续源码摘录，省略范围为零。


```cpp
bool CSyncMemoryFile::Write(CPayloadWriter& payload_, const SWriterAttr& data_, bool force_full_write_/* = false*/)
{
  if (!m_created)
  {
    Logging::Log(Logging::log_level_error, m_base_name + "::CSyncMemoryFile::Write - FAILED (m_created == false)");
    return false;
  }

  // store acknowledge timeout parameter
  m_attr.timeout_ack_ms = data_.acknowledge_timeout_ms;
  if (m_attr.timeout_ack_ms < 0) m_attr.timeout_ack_ms = 0;

  // write header and payload into the memory file
#ifndef NDEBUG
  Logging::Log(Logging::log_level_debug4, m_base_name + "::CSyncMemoryFile::Write");
#endif

  // create user file header
  struct SMemFileHeader memfile_hdr;
  // set data size
  memfile_hdr.data_size         = static_cast<uint64_t>(data_.len);
  // set header id
  memfile_hdr.id                = static_cast<uint64_t>(data_.id);
  // set header clock
  memfile_hdr.clock             = static_cast<uint64_t>(data_.clock);
  // set header time
  memfile_hdr.time              = static_cast<int64_t>(data_.time);
  // set header hash
  memfile_hdr.hash              = static_cast<uint64_t>(data_.hash);
  // set zero copy
  memfile_hdr.options.zero_copy = static_cast<unsigned char>(data_.zero_copy);
  // set acknowledge timeout
  memfile_hdr.ack_timout_ms     = static_cast<int64_t>(data_.acknowledge_timeout_ms);

  // acquire write access
  bool write_access = m_memfile.GetWriteAccess(static_cast<int>(m_attr.timeout_open_ms));

  // maybe it's locked by a zombie or a crashed process
  // so we try to recreate a new one
  if (!write_access)
  {
#ifndef NDEBUG
    Logging::Log(Logging::log_level_debug2, m_base_name + "::CSyncMemoryFile::Write::GetWriteAccess - FAILED");
#endif

    // try to recreate the memory file
    if (!Recreate(m_memfile.MaxDataSize())) return false;

    // then try to get access again
    write_access = m_memfile.GetWriteAccess(static_cast<int>(m_attr.timeout_open_ms));
    // still no chance ? hell .... we give up
    if (!write_access)
    {
      Logging::Log(Logging::log_level_error, m_base_name + "::CSyncMemoryFile::Write::GetWriteAccess - FAILED FINALLY");
      return false;
    }
  }

  // now write content
  bool written(true);
  size_t wbytes(0);

  // write the user file header
  written &= m_memfile.WriteBuffer(&memfile_hdr, memfile_hdr.hdr_size, wbytes) > 0;
  wbytes += memfile_hdr.hdr_size;
  // write the buffer
  if (data_.len > 0)
  {
    written &= m_memfile.WritePayload(payload_, data_.len, wbytes, force_full_write_) > 0;
  }
  // release write access
  m_memfile.ReleaseWriteAccess();

  // and fire the publish event for local subscriber
  if (written) SyncContent();

  if (written)
  {
#ifndef NDEBUG
    Logging::Log(Logging::log_level_debug4, m_base_name + "::CSyncMemoryFile::Write - SUCCESS : " + std::to_string(data_.len) + " Bytes written");
#endif
  }
  else
  {
    Logging::Log(Logging::log_level_error, m_base_name + "::CSyncMemoryFile::Write - FAILED (written == false)");
  }

  // return success
  return written;
}
```

这一函数先把 `SWriterAttr` 变成共享内存中的 `SMemFileHeader`，再拿跨进程写访问权。若有限等待失败，它尝试 `Recreate()` 并重试；仍失败就不触碰 payload。取得锁后，头和 payload 都经 `CMemoryFile` 写入同一 mapping，随后显式 `ReleaseWriteAccess()`，只有 `written` 为真才进入 `SyncContent()`。所以 event 通知不是提交本身：有效次序是“写数据—解锁—设置 send event—可选等待 ACK”。这里依次发生的是用户态函数调用、跨进程锁等待/获得、共享映射写入和 event 状态变化；observer 被通知后仍要竞争 OS 调度并重新取读锁，不能把 `SetEvent` 当成 callback 已运行。

还要继续向下一层读 `CMemoryFile::WritePayload()`：它会调用传入 writer 的 `WriteFull` 或 `WriteModified`。固定提交在底层 writer 返回失败时会记日志，但仍返回 payload 长度；因此本函数 `written` 的判断并不能证明任意自定义 `CPayloadWriter` 已完整写入。对普通 buffer writer 可检查其 `memcpy` 路径；对自定义 writer 这是一处错误传播边界，不能据此声称“通知必定代表完整 payload”。

## Copy 模式与 Zero-copy 模式

写端抽象 `CPayloadWriter` 用虚函数 `WriteFull(target,size)` 和 `WriteModified(target,size)` 把“payload 怎么写进目标内存”交给具体 writer。`CSyncMemoryFile::Write()` 接收基类引用，运行时根据真实派生对象做虚函数分派；默认 `CBufferPayloadWriter` 只是对原缓冲区执行 `memcpy`，自定义 writer 才能把序列化过程直接写到 mapping。用虚接口换来的好处是同步内存文件不需要为每种消息类型模板化；代价是 writer 必须遵守同步调用期的源/目标有效期与返回值契约。若 `WriteModified` 只写了一部分却返回 false，底层当前实现仍有错误传播缺口；下面的源码摘录直接展示基类接口与默认 buffer writer 的实现。

“零拷贝”至少有发布端和接收端两个观察方向。Publisher `CPublisherImpl::Write()` 先计算 `allow_zero_copy`：SHM zero-copy 配置打开且 UDP/TCP 都没有活动发送层时，才跳过成员 `m_payload_buffer` staging；网络层活动会令这个条件为 false。随后 `SWriterAttr.zero_copy` 仍从 SHM 配置写入共享 header，因此“publisher 是否绕过 staging”与“observer 是否把映射指针借给 callback”不是同一条件。下面的 `CPublisherImpl::Write()` 与 `CSyncMemoryFile::Write()` 摘录分别展示这两个开关落点。

Copy 接收模式下，Publisher 将 writer 数据写入 SHM mapping；observer 再复制到本地 `receive_buffer`，释放 named mutex 后调用 reader callback。若 `CSubscriberImpl` 没有注册 receive callback，它还会把 observer 传入的数据复制到自己的 `m_read_buf` 单槽中，供同步 `Read()` 取走。因此“接收端 copy 一次”要说明统计边界：observer 到 reader layer 一次，Read slot 又是另一段所有权转移。

```text
publisher bytes -> shared mapping -> subscriber local buffer -> callback
```

SHM header 的 `options.zero_copy` 非零时，observer 在 `GetReadAddress()` 后直接把 mapping payload 指针同步传给 `m_data_callback`：

```text
publisher/shared mapping -> callback view
```

这省去 observer 到本地 `receive_buffer` 的复制，但该 buffer 的 named mutex 仍被持有，直到整条同步调用返回。调用链会继续进入 `CSubGate`，再到 `CSubscriberImpl::ApplySample()`；如果设置了应用 callback，该 subscriber 还在 `m_receive_callback_mutex` 和 `m_connection_map_mtx` 临界区内执行用户代码。于是这次图像推理的 WCET 会占用 observer 线程、该 subscriber 的输入串行锁、连接 map 锁和跨进程 memfile 锁。回调只借用 payload，返回后映射即可被写端复用；保存指针供异步线程使用会读到之后覆盖的数据。

## Observer 的读取状态机

`CMemFileObserver::Observe()` 大致执行：

```text
wait send event, periodically wake for shutdown
try lock named mutex with timeout
read and validate header
compare publisher clock with last seen

if zero_copy:
  callback(mapped payload) while lock remains held
else:
  copy payload to local vector
  unlock
  callback(local buffer)

update last_sample_clock
if ACK enabled: signal ack event
```

Clock 去重用于避免重复 event 或同一 buffer 被多次观察。先确认 header/payload 边界，再使用其中 size。

等待线程真正如何分支，可以直接看固定提交的连续源码。输入是上一节写入并通知的 memfile；调用发生在 `CMemFileObserver::Start()` 创建的 observer `std::thread` 中。下面摘录从 wait/获取读锁一直到 callback、ACK 与循环退出；它保留了这段范围内的全部控制流。注意源码注释称 wait 为 20 ms，但传给 `gWaitForEvent` 的实参实际是 500 ms。


```cpp
void CMemFileObserver::Observe(const int timeout_)
{
  // internal clock sample update checking
  uint64_t last_sample_clock(0);

  // buffer to store memory file content
  std::vector<char> receive_buffer;

  // Boolean that tells whether the SHM file has new data that we have NOT already accessed
  bool has_unprocessed_data = false;

  // runs as long as there is no timeout and no external stop request
  while(std::chrono::steady_clock::now() - std::chrono::steady_clock::time_point(m_time_of_last_life_signal) < std::chrono::milliseconds(timeout_)
         && !m_do_stop)
  {
    if (!has_unprocessed_data)
    {
      // Only wait for the new-data-event, if we haven't processed the data, yet
      // check for memory file update event from shm writer (20 ms)
      has_unprocessed_data = gWaitForEvent(m_event_snd, 500);

      if (has_unprocessed_data)
      {
        // We got a signal from the publisher! It is alive! So we reset the time since the last live signal
        m_time_of_last_life_signal = std::chrono::steady_clock::now();
      }
    }

    // If we have unprocessed data, we try to access (and process!) it
    if(has_unprocessed_data)
    {
      // last chance to stop ..
      if(m_do_stop) break;

      // try to open memory file (timeout 5 ms)
      if(m_memfile.GetReadAccess(5))
      {
        // We have gotten access! Now the data qualifies as processed, so next loop we will wait for the signal for new data, again.
        has_unprocessed_data = false;

        // read the file header
        SMemFileHeader mfile_hdr;
        ReadFileHeader(mfile_hdr);

        // check for new content
        if (mfile_hdr.clock <= last_sample_clock)
        {
          // release access and leave
          m_memfile.ReleaseReadAccess();
        }
        else
        {
          const bool zero_copy_allowed = mfile_hdr.options.zero_copy != 0;
          bool post_process_buffer(false);
          // -------------------------------------------------------------------------
          // zero copy mode
          // -------------------------------------------------------------------------
          // That means we call the user callback (ApplySample) from within the opened memory file.
          // So we do not waste time by copying the payload in an intermediate buffer
          // but the file keeps opened and blocked until the callback returns.
          // Other subscriber can not access the content this time !
          // -------------------------------------------------------------------------
          if (zero_copy_allowed)
          {
            if (m_data_callback)
            {
              const char* data_buf = nullptr;
              if (mfile_hdr.data_size > 0)
              {
                // acquire memory file payload pointer (no copying here)
                const void* buf(nullptr);
                if (m_memfile.GetReadAddress(buf, mfile_hdr.data_size) > 0)
                {
                  // calculate user payload address
                  data_buf = static_cast<const char*>(buf) + mfile_hdr.hdr_size;
                  // call user callback function
                  m_data_callback(data_buf, mfile_hdr.data_size, (long long)mfile_hdr.id, (long long)mfile_hdr.clock, (long long)mfile_hdr.time, (size_t)mfile_hdr.hash);
                }
              }
              else
              {
                // call user callback function
                m_data_callback(data_buf, mfile_hdr.data_size, (long long)mfile_hdr.id, (long long)mfile_hdr.clock, (long long)mfile_hdr.time, (size_t)mfile_hdr.hash);
              }
            }
          }
          // -------------------------------------------------------------------------
          // buffered mode
          // -------------------------------------------------------------------------
          // we copy the data into the receive buffer (standard mode for eCAL < 5.10)
          // and close the file immediately
          // -------------------------------------------------------------------------
          else
          {
            // need to resize the buffer especially if data_size = 0, otherwise it might contain stale data.
            receive_buffer.resize((size_t)mfile_hdr.data_size);

            // read payload
            // if data length == 0, there is no need to further read data
            // we just flag to process the empty buffer
            if (mfile_hdr.data_size != 0)
            {
              m_memfile.Read(receive_buffer.data(), (size_t)mfile_hdr.data_size, mfile_hdr.hdr_size);
            }

            post_process_buffer = true;
          }

          // store clock
          last_sample_clock = mfile_hdr.clock;

          // release access
          m_memfile.ReleaseReadAccess();

          // process receive buffer if buffered mode read some data in
          if (post_process_buffer)
          {
            // add sample to data reader (and call user callback function)
            if (m_data_callback) m_data_callback(receive_buffer.data(), receive_buffer.size(), (long long)mfile_hdr.id, (long long)mfile_hdr.clock, (long long)mfile_hdr.time, (size_t)mfile_hdr.hash);
          }

          // send acknowledge event
          if (mfile_hdr.ack_timout_ms != 0)
          {
            gSetEvent(m_event_ack);
          }
        }
      }
    }
  }

#ifndef NDEBUG
  // log it
  if(m_do_stop)
  {
    eCAL::Logging::Log(Logging::log_level_debug2, std::string("CMemFileObserver " + m_memfile.Name() + " stopped"));
  }
  else
  {
    eCAL::Logging::Log(Logging::log_level_debug2, std::string("CMemFileObserver " + m_memfile.Name() + " timeout"));
  }
#endif

  // mark as stopped
  m_is_observing = false; //-V1020
}
```

流程中的“通知”至少有五个不同关口。首先 Publisher 已把 header 与 payload 写进共享映射并释放写锁；随后 Linux event 的共享状态字节被置为 1，condvar 被 signal；这只让等待谓词成立，并不保证 observer 已运行。若 observer 当时 blocked，库/运行库在底层等待上让线程变为 runnable；Linux 调度器何时给它 CPU 是另一个时刻。线程获得 CPU 后还要在 5 ms 内取得 memfile 读权、检查 clock；通过检查后才会调用 `m_data_callback`。copy 分支在释放共享锁后才回调；zero-copy 分支先取映射地址并在锁内同步回调，所以 callback 的返回才允许 writer/其他 reader 复用这一 memfile。`m_data_callback` 返回后才 set ACK；ACK 表示 observer 已走到这个 ACK 点，不是应用算法完成或命令已执行。

这段还解释了为什么 event 不是 FIFO：若第 10 帧和第 11 帧在 observer 读取前都写入同一个 memfile，两次 Set 合并为一个待处理状态，observer 读取的是当前 header，再用 `last_sample_clock` 拒绝不比上一帧新的内容。它不会恢复第 10 帧。`has_unprocessed_data` 则用于读锁暂时拿不到时保留一次重试机会；5 ms 获取失败并不把线程“唤醒”为业务执行，它只是让循环再次尝试。

## Observer 线程粒度

该设计可为每个 observed memfile 建立一个 observer/thread。优点是每个 Publisher buffer 有独立等待上下文，代码直接。

如果有 M 个 memory files，线程数量、栈内存和 scheduler 唤醒可能近似 `O(M)`。大量 topic/Pub 时，这比 payload copy 更早成为规模瓶颈。

可扩展替代是让一组 worker 使用可等待事件集合或平台 reactor 管理多个 memfile，但跨平台 named events 是否可统一等待会影响实现复杂度。

Observer callback 的 C++ 捕获方式也决定关闭顺序。固定源码 `CSHMReaderLayer::SetConnectionParameter()` 构造回调 `[this, topic_info]`：`topic_info` 按值复制，捕获的是当时这份 topic 描述；`this` 只保存一个裸地址，不增加 reader layer 的引用计数。这个 `std::function` 被交给按 memfile 名复用 Observer 的 pool。observer 线程调用 callback 时，调用栈会经过 `CSHMReaderLayer::OnNewShmFileContent()`；因此 reader layer 必须活到该 observer 停止并 join。`CGlobals::Finalize()` 对 SHM 的顺序是先 `CMemFileThreadPool::Stop()`（join observers），再 reset reader layer，才闭合这条裸 `this` 依赖。下文的 callback 与 `Stop/Finalize` 源码摘录会展开这条所有权关系。如果某个替代实现只清空 Observer map 或 detach 线程就释放 layer，`this` 会变成悬空地址。

registration receiver 在拿到 publisher 发来的 memfile list 后进入 `CSHMReaderLayer::SetConnectionParameter()`。这个函数为每个 memfile event 创建 callback，再交给由 `CGlobals` 共享拥有的 pool；pool 按文件名复用已有 observer，或者建立并启动新 observer：

接着看 `CMemFileThreadPool::ObserveFile` 的真实实现：

```cpp
void CSHMReaderLayer::SetConnectionParameter(SReaderLayerPar& par_)
{
  for (const auto& memfile_name : par_.parameter.layer_par_shm.memory_file_list)
  {
    if (m_memfile_thread_pool)
    {
      const std::string process_id = std::to_string(m_attributes.process_id);
      const std::string memfile_event = memfile_name + "_" + process_id;

      Payload::TopicInfo topic_info;
      topic_info.topic_name = par_.topic_name;
      topic_info.host_name  = par_.host_name;
      topic_info.topic_id   = par_.topic_id;
      topic_info.process_id = par_.process_id;

      auto data_callback = [this, topic_info](const char* buf_, size_t len_, long long id_,
                                               long long clock_, long long time_, size_t hash_)->size_t
      {
        return OnNewShmFileContent(topic_info, buf_, len_, id_, clock_, time_, hash_);
      };
      m_memfile_thread_pool->ObserveFile(memfile_name, memfile_event,
                                         m_attributes.registration_timeout_ms, data_callback);
    }
  }
}

bool CMemFileThreadPool::ObserveFile(const std::string& memfile_name_,
                                     const std::string& memfile_event_,
                                     int timeout_observation_ms,
                                     const MemFileDataCallbackT& callback_)
{
  if (!m_created || memfile_name_.empty()) return false;
  const std::lock_guard<std::mutex> lock(m_observer_pool_sync);

  auto observer_it = m_observer_pool.find(memfile_name_);
  if (observer_it != m_observer_pool.end())
  {
    auto& observer = observer_it->second;
    if (observer->IsObserving())
      observer->ResetTimeout();
    else
    {
      observer->Stop();
      observer->Start(timeout_observation_ms, callback_);
    }
    return true;
  }

  auto observer = std::make_shared<CMemFileObserver>(m_memfile_map);
  observer->Create(memfile_name_, memfile_event_);
  observer->Start(timeout_observation_ms, callback_);
  m_observer_pool[memfile_name_] = observer;
  return true;
}
```

捕获列表里只有 `topic_info` 是按值拥有的副本；`this` 仍是非拥有指针。`std::function` 包装这个 lambda 时同样不会替 reader layer 续命。另一方面，pool map 保存 `shared_ptr<CMemFileObserver>`，因此每个 observer 至少由 pool 强持有，清理或 Stop 从 map 移除后才会进入析构。新线程由 `Start()` 启动；如果同一个 memfile 的 registration 仍活跃，pool 只刷新 timeout，不创建第二个 worker。

关闭代码把 worker 的线程边界闭合在 `join()`：

接着看 `CMemFileThreadPool::Stop` 的真实实现：

```cpp
bool CMemFileObserver::Start(const int timeout_, const MemFileDataCallbackT& callback_)
{
  if (!m_created || m_is_observing) return false;
  m_data_callback = callback_;
  m_is_observing = true;
  m_thread = std::thread(&CMemFileObserver::Observe, this, timeout_);
  return true;
}

bool CMemFileObserver::Stop()
{
  if (!m_created) return false;
  if (m_is_observing)
  {
    m_do_stop = true;
    gSetEvent(m_event_snd);
  }
  if (m_thread.joinable()) m_thread.join();
  return true;
}

void CMemFileThreadPool::Stop()
{
  if (!m_created) return;
  {
    const std::lock_guard<std::mutex> lock(m_do_cleanup_mtx);
    m_do_cleanup = false;
    m_do_cleanup_cv.notify_one();
  }
  if (m_cleanup_thread.joinable()) m_cleanup_thread.join();

  const std::lock_guard<std::mutex> lock(m_observer_pool_sync);
  for (auto& observer : m_observer_pool) observer.second->Stop();
  m_observer_pool.clear();
  m_created = false;
}
```

`Stop()` 先让清理线程离开它的条件变量等待并 join，再持 `m_observer_pool_sync` 停止并 join 每个 observer，最后才清 pool。observer callback 若正执行一个 40 ms 的图像处理调用，Stop 必须等 callback 返回、Observe 线程退出，join 才能完成；这就是同步关闭的实际代价。Signal 只让 event wait 有机会返回，join 才是“线程已不再访问 reader layer”的确认点。`CGlobals::Finalize()` 随后 reset reader layer，顺序刚好满足 `[this]` 捕获的寿命要求。

这里的 `std::function` 提供固定的“可调用对象接口”：pool 不需要知道 callable 是普通函数、lambda 还是函数对象，就能用统一的 `operator()` 调用。它按值保存 lambda target，但不会递归拥有捕获指针指向的对象；复制 `std::function` 也只复制这个裸 `this` 地址和 topic 值，不会自动续住 layer 生命周期。类型擦除省下的是调用者对具体 callable 类型的依赖，所需的对象寿命仍由 Stop/Join 顺序负责。

`Stop()` 是 Signal → Join → Destroy：先设置 `m_do_stop` 并 set send event，让正在 event wait 的 observer 有机会从等待返回；再 `join()`，等待它从当前控制流退出；join 后才能关闭本地 mapping、event handle 与 callback。它不能中断已经开始的用户 callback，所以若 callback 永久阻塞，Finalization 的 join 也会卡住。该 observer 是 OS 线程；set event、等待线程变 runnable、调度器分配 CPU、退出 wait、恢复 C++ 调用栈是分开的阶段。固定源码创建处见 `CMemFileObserver::Start/Stop`。

## Zero-copy 把业务 WCET 传给 Publisher

假设 callback 执行 40 ms，Buffer 数为 1：

```text
t0 subscriber locks and enters callback
t1 publisher tries next write -> waits same named mutex
t0+40ms subscriber returns/unlocks
publisher can write
```

此时 Publisher latency 包含 Subscriber 的业务执行时间。即使发布线程本身优先级更高，也无法绕过跨进程 mutex。

Buffer 数增加到 3 后，Publisher 可先写其他两块，但持续高频发布最终仍绕回。因此 zero-copy 适合 callback 极短或上层能够 loan/retain buffer 并用更明确所有权协议的场景。

## Copy 模式隔离临界区

Copy 模式在锁内只执行 `memcpy(S)`，随后 callback 在锁外运行。Publisher 最坏等待接近一次复制加调度抖动，而不是完整算法 WCET。

对于中等 payload，复制成本可能比不可控尾延迟更划算。是否 zero-copy 不能只看平均带宽，应比较：

```text
copy time p99
versus
callback WCET + mutex contention p99
```

“少一次复制”不是自动等于“实时性更好”。

## ACK 的目的

没有 ACK 时，send event 只表示 Publisher 发布了新内容，不知道哪些 Subscriber 已经观察。

启用 ACK 后，每个订阅进程关联 ack event。`CMemFileObserver::Observe()` 在自己的 `m_data_callback` 返回后才设置 ACK event；Publisher 的 `SyncContent()` 逐个等待当前 snapshot 中的 event，共用一个总 timeout budget。这个确认只表示 observer 已走到 callback 返回后的 ACK 点，不表示 SubGate 一定找到并接受了 Subscriber，更不表示应用异步排队后的业务计算已完成。

```text
Publisher signal send
  -> Subscriber A reads -> signal ACK A
  -> Subscriber B reads -> signal ACK B
Publisher returns after A+B or timeout
```

ACK 可以减少发布端在未等待读者前就继续返回的情况，但它不是保留每条历史样本的 FIFO，也会把慢/故障 Subscriber 变成 Publisher 阻塞来源。observer callback 如果只将 payload 推入业务队列然后返回，ACK 会在队列入队之后立即发送；队列里的算法可能尚未开始执行。

## ACK timeout 的语义

多个 ACK 使用同一个开始时刻时，总等待预算近似为 configured timeout，而不是每个订阅者都重新等待完整 timeout。

源码注释声称超时后直到 registration 重新确认之前不再等待该 ACK，但同一固定提交的 `CSyncMemoryFile::SyncContent()` 先将 `m_event_handle_map` 复制到局部 `event_handle_map_snapshot`，随后只对 snapshot 的 `event_ack_is_invalid` 赋值，没有写回权威 map。因而不能把注释描述的“后续 publish 永久跳过”当成已被这段代码实现的事实；按可见控制流，局部赋值不会改变下一次 `SyncContent()` 复制到的状态。下方源码摘录直接展示这处差异。

## SHM 所有权与指针有效期

Zero-copy callback 得到的是映射地址借用：

```text
valid after header validation and lock acquisition
valid during callback
invalid for application use after callback/unlock
```

保存裸指针到后台线程会在 Publisher 重写或 memfile 重建后读到变化/无效内存。

若 API 要允许异步零拷贝，需要显式 loan handle：handle 持有 buffer lease，析构时 release；Publisher 只能复用 lease count 为零的 buffer。这个协议比 callback 借用复杂得多。

## 内存预算

设每个 Publisher 配 B 个 buffer，容量 C，有 P 个 Publisher：

```text
SHM payload capacity ~= P * B * C
```

还要加 header、named object、mapping page 对齐和 observer 本地 copy buffer。

Reserve 使 C 高于当前 payload；大小曾出现过峰值后，memfile 可能长期保持峰值容量。监控应报告 configured、allocated 和 current data size，而不只报告消息大小。

## 故障路径

需要明确处理：

- Publisher 在持锁时崩溃，named mutex 是否支持 owner-death recovery；
- Subscriber 仍观察旧 memfile，Publisher 已 resize/rename；
- event 到达但 header 版本/size 不合法；
- ACK Subscriber 退出但 registration timeout 尚未收敛；
- Finalize 时 observer 正在 callback；
- 重建共享对象时其他进程仍持旧 mapping。

共享内存省去网络复制，却把跨进程生命周期变成主要复杂度。

## 可复刻的最小 SHM 设计

第一版只用单 buffer + copy read：

**教学/复刻示例（不是固定提交源码摘录）：**

```cpp
struct Header {
  uint32_t magic;
  uint16_t version;
  uint16_t header_size;
  uint64_t capacity;
  uint64_t payload_size;
  uint64_t sequence;
};
```

写端锁内写 header/payload，解锁后 signal；读端锁内验证并复制，解锁后 callback。

第二版加入 B 个 slot 和 round-robin。第三版加入 registration 发布 slot names。第四版才增加 ACK。最后在测得 copy 是瓶颈后，设计带 lease 的 loan API，而不是简单把 callback 移进锁内。

## SHM 设计结论

eCAL SHM 以 memory file 承载 bytes，以 named mutex 提交一致 sample，以 event 通知 observer，以多 buffer 缓解读写冲突，并可用 ACK 加强读取确认。

它的主要优势是同机大数据吞吐；主要风险是原生 ABI、每 memfile observer 成本、resize 控制面传播，以及 zero-copy 将用户 callback 纳入跨进程临界区。真正的性能评估必须同时测复制时间、锁等待和 callback 尾延迟。
