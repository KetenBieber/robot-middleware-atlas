# 场景设计五：大图像/点云跨进程，怎样从 Copy IPC 走到 Shared-Memory Data Plane

> **首次阅读先抓三层：** Payload 放大数据、Descriptor 描述数据、Notification 只负责唤醒。Offset、Loan、Generation 都是围绕这三层解决跨进程地址和生命周期问题。

## 场景

~~~text
Camera Process
    ↓
Perception Process
~~~

每帧 1920×1080×RGBA8：

~~~text
≈ 8.3 MB/frame
~~~

60 FPS：

~~~text
≈ 500 MB/s raw payload
~~~

如果跨进程每次 serialize + copy 多遍，memory bandwidth 很快成为主要成本。

---

## 先建立进程、地址空间和共享内存的基本模型

### 每个进程都有自己的 Virtual Address Space

两个进程即使运行同一个程序，也有各自独立的虚拟地址空间。

例如 Process A 可能把某一物理页映射到：

~~~text
0x7000_0000 -> physical page P
~~~

Process B 则可能把同一物理页映射到：

~~~text
0x9200_0000 -> physical page P
~~~

底层 physical page 相同，但 virtual address 不同。

> **共享内存共享的是底层 pages，不代表两个进程里的 pointer value 必须相同。**

这就是为什么把一个进程中的裸指针整数值直接发给另一个进程通常没有意义。

---

## Payload、Descriptor、Notification 是三层不同东西

### Payload

真正的大数据，例如 8 MB image、point cloud、tensor bytes。

### Descriptor

描述 payload 在哪里、大小多少、属于哪个版本的小对象，例如：

~~~text
struct FrameDescriptor {
    uint32_t slot;
    uint32_t generation;
    uint32_t bytes;
    uint64_t timestamp_ns;
};
~~~

Descriptor 很小，可以放进 socket、pipe、ring queue 或其他控制通道。

### Notification

只负责告诉另一边：**共享状态可能变化了，值得醒来检查一下。**

例如 eventfd、futex wake、condition_variable、socket readiness。

因此常见结构是：

~~~text
Payload Pool
    存大数据

Descriptor Queue
    存 slot/generation/metadata

Notification
    负责 wakeup
~~~

这和线程 Producer/Consumer 中的 `queue = truth, condition_variable = wakeup` 是同一条设计原则。

---

## 为什么 Offset 能跨进程，而 Pointer 通常不能

假设 shared segment 的逻辑布局：

~~~text
base
 |
 +-- offset 0
 +-- offset 4096
 +-- offset 8192
~~~

Producer 只传：

~~~text
offset = 8192
~~~

Consumer 在自己的地址空间中计算：

~~~text
local_ptr = local_base + offset
~~~

于是两个进程虽然 `local_base` 不同，却都能定位到共享 segment 中同一个逻辑位置。

这就是 relative pointer / offset pointer 的基本思想。

---

## 一个真正可运行的 Linux SHM + Offset 示例

下面不是“伪共享内存”。它真的创建 POSIX shared memory、`fork()` 出第二个进程，用 pipe 只传一个小 Descriptor，再由 Child 用 `base + offset` 找到大 payload。

编译运行：

~~~text
g++ -std=c++17 shm_offset_demo.cpp -o shm_offset_demo
./shm_offset_demo
~~~

~~~cpp
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

struct FrameDescriptor {
    std::uint32_t offset;
    std::uint32_t bytes;
    std::uint32_t generation;
};

[[noreturn]] void die(const char* what) {
    std::perror(what);
    std::exit(1);
}

void write_all(int fd, const void* data, std::size_t bytes) {
    const char* p = static_cast<const char*>(data);
    while (bytes > 0) {
        const ssize_t n = ::write(fd, p, bytes);
        if (n < 0) {
            die("write");
        }
        p += n;
        bytes -= static_cast<std::size_t>(n);
    }
}

void read_all(int fd, void* data, std::size_t bytes) {
    char* p = static_cast<char*>(data);
    while (bytes > 0) {
        const ssize_t n = ::read(fd, p, bytes);
        if (n <= 0) {
            die("read");
        }
        p += n;
        bytes -= static_cast<std::size_t>(n);
    }
}

int main() {
    constexpr const char* kName = "/atlas_shm_offset_demo";
    constexpr std::size_t kSegmentBytes = 4096;
    constexpr std::uint32_t kPayloadOffset = 256;

    const int shm_fd =
        ::shm_open(kName, O_CREAT | O_RDWR, 0600);
    if (shm_fd < 0) {
        die("shm_open");
    }

    if (::ftruncate(shm_fd, kSegmentBytes) != 0) {
        die("ftruncate");
    }

    void* parent_base =
        ::mmap(nullptr,
               kSegmentBytes,
               PROT_READ | PROT_WRITE,
               MAP_SHARED,
               shm_fd,
               0);
    if (parent_base == MAP_FAILED) {
        die("mmap parent");
    }

    int descriptor_pipe[2];
    if (::pipe(descriptor_pipe) != 0) {
        die("pipe");
    }

    const pid_t pid = ::fork();
    if (pid < 0) {
        die("fork");
    }

    if (pid == 0) {
        ::close(descriptor_pipe[1]);

        // 不依赖 Parent 继承来的 mapping，Child 自己重新 mmap。
        ::munmap(parent_base, kSegmentBytes);
        ::close(shm_fd);

        const int child_fd = ::shm_open(kName, O_RDWR, 0600);
        if (child_fd < 0) {
            die("child shm_open");
        }

        void* child_base =
            ::mmap(nullptr,
                   kSegmentBytes,
                   PROT_READ | PROT_WRITE,
                   MAP_SHARED,
                   child_fd,
                   0);
        if (child_base == MAP_FAILED) {
            die("mmap child");
        }

        FrameDescriptor d{};
        read_all(descriptor_pipe[0], &d, sizeof(d));

        const char* payload =
            static_cast<const char*>(child_base) + d.offset;

        std::cout
            << "child_base=" << child_base
            << " offset=" << d.offset
            << " generation=" << d.generation
            << " payload=" << payload
            << "\n"
            << std::flush;

        ::munmap(child_base, kSegmentBytes);
        ::close(child_fd);
        ::close(descriptor_pipe[0]);
        _exit(0);
    }

    ::close(descriptor_pipe[0]);

    char* payload =
        static_cast<char*>(parent_base) + kPayloadOffset;
    const char message[] = "frame-42: shared payload";
    std::memcpy(payload, message, sizeof(message));

    FrameDescriptor d{
        kPayloadOffset,
        static_cast<std::uint32_t>(sizeof(message)),
        7
    };

    write_all(descriptor_pipe[1], &d, sizeof(d));
    ::close(descriptor_pipe[1]);

    ::waitpid(pid, nullptr, 0);

    ::munmap(parent_base, kSegmentBytes);
    ::close(shm_fd);
    ::shm_unlink(kName);
}
~~~

这段程序里两类数据非常明确：

~~~text
共享内存：
    真正 payload

匿名 pipe：
    FrameDescriptor(offset, bytes, generation)
~~~

Child 并没有接收 Parent 的裸指针；它只接收 offset，然后基于自己的 `child_base` 重新计算本地地址。这就是后面 iceoryx2 PointerOffset、共享内存 slot/descriptor 机制的最小原型。

---

> **首次阅读完成点：** 到这里已经把“跨进程不能传裸指针、Payload 与 Descriptor 分离、Offset 如何定位共享数据”跑通了。下面的 Pool、Loan、Refcount、Generation 是为了把这个最小原型变成长期运行的 Runtime。


## 为什么固定 Pool 比“每帧 mmap 一次”更自然

实时 data plane 希望把昂贵和不确定的资源操作尽量移出 hot path。

初始化阶段：

~~~text
allocate N large blocks
map/register
建立 metadata
~~~

运行阶段：

~~~text
loan slot
write payload
publish descriptor
consumer read
release slot
~~~

这样避免每帧 malloc/mmap/setup/free 带来的 allocator contention、page fault、fragmentation 与不可预测延迟。

---

## Loan 到底是什么意思

Loan 不是“某个 API 看起来像零拷贝”。

> **Loan 表示 Runtime 暂时把一个可写 slot 的独占使用权交给 Producer。**

状态可以写成：

~~~text
FREE
  | loan()
  v
WRITING
  | publish()
  v
READY
~~~

Producer 在 WRITING 期间可以直接填共享 payload；publish 后，Consumer 才应该观察这块内容。

所以 zero-copy 真正困难的是 ownership protocol，而不只是拿到一个 pointer。

---

## Refcount 为什么能支持 Fan-out

如果同一帧同时交给 Perception、Recorder、Visualizer，不能第一个 Consumer release 后就立即复用 slot。

一种直接方案：

~~~text
refcount = 3
Perception done  -> 2
Recorder done    -> 1
Visualizer done  -> 0
slot recyclable
~~~

但 atomic refcount 也会带来共享写、cache contention，并且慢 Consumer 会长期占住 buffer。

所以工业 Runtime 也会采用 per-consumer cursor、ownership bitmap，或者给慢支路单独复制。

---

## Generation 是怎样防 Stale Descriptor 的

只用 `slot=3` 不够，因为 slot 会循环复用。

~~~text
slot 3, generation 100
consumer holds old descriptor

slot recycled

slot 3, generation 101
new frame written
~~~

旧 Consumer 如果只记住 slot=3，就可能把 generation 101 的新数据误认成 generation 100。

所以更完整的 identity 是：

~~~text
(slot, generation)
~~~

这与 ABA 防护中的 version/tag 思想本质相同。

ABA 指“一个位置先从 A 变成 B，后来又变回 A；只比较当前值的人会误以为它从未变化”。Generation/Version 就是在 identity 里再加入一次复用次数，让“旧的 slot 3”和“重新复用后的 slot 3”不再看起来完全一样。

---

## Naive 方案：Socket + Serialization

~~~text
camera object
↓ serialize
temporary bytes
↓ kernel/socket copy
receiver bytes
↓ deserialize
new image object
~~~

优点：

~~~text
边界清楚
容易跨主机
crash isolation 简单
~~~

小消息时非常合理。

大 payload 高频时问题是 copy 与 allocator。

---

## 为什么 Shared Memory 自然出现

目标不是“让两个进程共享 C++ object”，而是：

~~~text
payload storage
只放一份在 shared physical pages
~~~

然后消息只传：

~~~text
slot id / offset
size
generation
metadata
~~~

这就是 descriptor-based data plane。

---


## Notification 为什么不能代替共享状态

错误理解：

~~~text
eventfd fired
→ 一定有一个完整 frame
~~~

正确：

~~~text
eventfd/wakeup
只表示“值得重新检查共享状态”
~~~

真实状态仍然在 descriptor queue/cursor。

这与 condition_variable predicate 规则相同。

---

## Crash Recovery 为什么跨进程必须额外设计

Thread crash 通常意味着整个 process 结束。

跨进程共享 pool 时：

~~~text
Consumer dies while holding slot
~~~

Producer/other consumers 还活着。

谁回收？

可能需要：

~~~text
process registry
heartbeat/liveness

这些词可以先按职责理解：process registry 记录“哪些参与者存在”；heartbeat/liveness 判断进程是否还活着；owner epoch 给每次进程启动一个新的 incarnation/version；lease 表示“所有权只在一段时间内有效”；central daemon 则把清理和仲裁集中到一个独立管理进程。并不是每套 SHM Runtime 都需要同时拥有这五种机制。
owner epoch
generation
lease
central daemon
~~~

这也是 SHM 比普通 in-process queue 难得多的地方。

---

## 工业方案空间

| 方案 | 数据路径 | 优点 | 适合 |
| --- | --- | --- | --- |
| Unix socket | copy/serialize | 简单 | 小消息 |
| memfd/mmap + custom descriptor | SHM | 可控 | 自研 runtime |
| iceoryx2 | chunk pool + ownership protocol | zero-copy/lifetime 完整 | large IPC |
| eCAL SHM | middleware-integrated SHM | 工程易用 | robotics/process IPC |
| Fast DDS Data Sharing/SHM | DDS semantics + local optimization | 保留 DDS API | DDS system |
| Cyclone DDS PSMX | plugin/offload path | 可扩展 | DDS local data plane |

不是所有系统都需要自研 SHM。

---

## 什么时候 Copy 反而更好

payload 小、频率低时：

~~~text
copy 2 KB
vs
复杂 loan/refcount/crash recovery
~~~

copy 可能更便宜、更安全。

另外对于安全/隔离边界，copy 还能减少共享可变状态。

zero-copy 应该由 payload size × frequency × latency budget 驱动，而不是作为目标本身。

---

## 一个机器人图像 IPC 推荐结构

~~~text
Camera Process
  loan slot from SHM pool
  DMA/copy into slot
  publish descriptor
  eventfd notify
        ↓
Perception Process
  wake
  pop descriptor
  validate generation
  borrow slot
  process
  release
~~~

Recorder 如果很慢：

~~~text
copy/secondary pool
~~~

避免它长期占主实时 pool。

---

## 指标

必须测：

~~~text
copy count
pool free slots
loan wait time
descriptor queue depth
oldest chunk age
consumer hold time
crash recovery time
stale descriptor count
~~~

只测 IPC throughput 不够。

---

## 什么时候继续升级到网络/UCX

如果 Producer/Consumer：

~~~text
不在同一主机
或
payload 已经在 GPU/NPU memory
~~~

普通 SHM 模型又不够。

下一场景进入 device/remote data plane。

深入机制：

- [Processes & Shared Memory](processes-shared-memory.md)
- [Heterogeneous Memory](heterogeneous-memory.md)
- [iceoryx2 实现专题](../generated/iceoryx2/index.rst)
