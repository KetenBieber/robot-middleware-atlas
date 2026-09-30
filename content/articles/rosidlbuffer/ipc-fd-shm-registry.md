# CUDA VMM IPC：epoll、eventfd、SCM_RIGHTS 与共享 Registry

固定源码版本：d7cd9642d77a1d64fd85f25ba0bf96e108401900。

CUDA backend 的跨进程路径几乎把 Linux IPC 的关键原语串在了一起：

~~~text
CUDA VMM exported FD
→ Unix-domain socket
→ SCM_RIGHTS
→ subscriber import
→ shared IPC metadata
→ host-wide endpoint registry
→ local caches
~~~

理解这条链以后，很多 DMA-BUF、camera、graphics、accelerator runtime 都会变得熟悉。

## 为什么不能“把 fd 数字发过去”

FD 是当前进程 file descriptor table 的索引。

Publisher 的 17 和 Subscriber 的 17 没有必然关系。

真正需要传递的是 fd 背后的 kernel object reference。

Linux 提供：

~~~text
sendmsg / recvmsg
+
SOL_SOCKET / SCM_RIGHTS
~~~

内核会为接收进程创建新的 local fd，指向同一个可共享内核资源。

所以 SCM_RIGHTS 是 capability transfer，而不是 integer serialization。

## 为什么每个 VMM block 有 socket identity

Publisher 给 block 建类似：

~~~text
cuda_vmm_<pid>_<block_id>
~~~

的 Unix-domain endpoint。

Descriptor 携带这个 identity。

Subscriber 第一次遇到 block：

~~~text
connect
→ receive exported VMM fd
→ cuMemImportFromShareableHandle
→ map to local CUDA VA
→ cache
~~~

后续命中 import cache，不再做完整 handshake。

## 为什么不能一个 block 一个 blocking thread

如果 100 个 block 各自一个线程阻塞 accept：

~~~text
100 blocks
→ 100 server threads
~~~

资源规模会和 pool 容量绑定。

固定 CudaVmmIPCManager 内部只有一个 FDDispatcher：

~~~cpp
int epoll_fd_;
int event_fd_;
std::unordered_map<int, int> socket_to_fd_;
std::mutex map_mutex_;
std::thread dispatcher_thread_;
std::atomic<bool> running_;
~~~

多个 server socket 统一进入一个 epoll Reactor。

## epoll 把什么成本交给内核

不用 epoll 时：

~~~text
for every socket:
    ask whether it is ready
~~~

epoll 模型：

~~~text
register interest once
↓
kernel maintains readiness
↓
epoll_wait returns ready fd set
~~~

这对大量 mostly-idle block socket 很合适。

## socket_to_fd_ 为什么是 unordered_map

epoll 返回 server socket fd。

dispatcher 需要做：

~~~text
server_socket
→ exported CUDA fd to serve
~~~

这是高频点查、无需顺序，因此 hash map 很自然。

## add_socket 为什么先 dup

固定实现先：

~~~cpp
int duped_fd = dup(fd_to_serve);
~~~

这样 dispatcher 对 exported resource 拥有独立 fd lifetime。

否则原 owner close 以后，保存下来的整数可能被 OS 重用于别的 file，形成极难调试的 stale-fd bug。

原则是：

~~~text
如果长期对象要独立持有一个 fd capability
→ dup ownership
→ 独立 close
~~~

## eventfd 为什么是 Reactor shutdown 的关键

dispatcher 可能永远睡在 epoll_wait。

只做：

~~~text
running = false
~~~

并不会让 kernel 立刻唤醒它。

固定实现把 eventfd 也加入 epoll。

stop 时：

~~~text
running=false
↓
write(eventfd)
↓
epoll_wait wakes
↓
dispatcher observes shutdown
↓
join
~~~

这是非常干净的 wakeup channel。

## eventfd 与 condition_variable 的角色不同

condition_variable 适合同进程线程共享内存谓词。

eventfd 是真正的 fd，可以进入：

~~~text
epoll/select/poll
~~~

因此当一个 Reactor 同时管理 socket、device fd、timerfd 等 OS 对象时，eventfd 更容易把“控制线程要求 wakeup”整合进同一个 wait set。

## handle_client 如何传 CUDA capability

server accept client 后构造 ancillary control message：

~~~cpp
cmsg_level = SOL_SOCKET;
cmsg_type = SCM_RIGHTS;
~~~

然后 sendmsg。

这一步以后 Subscriber 拿到的是自己进程里的新 fd。

再调用 CUDA import API，得到本进程的 VMM handle/VA。

所以完整跨进程过程是：

~~~text
GPU allocation
→ export kernel capability
→ transfer capability through AF_UNIX
→ import capability
→ map local VA
~~~

## import cache 为什么 key 是 pid + block_id

每个 Publisher process 都可能从 block_id=0 开始。

因此 block_id 不是全主机唯一。

组合：

~~~text
publisher pid
+
publisher-local block_id
~~~

形成 stable cache namespace。

## 为什么 mapping cache 命中仍然要检查 UID

CachedImport 说明：

~~~text
这个 allocation mapping 仍然存在
~~~

但不说明：

~~~text
这还是同一代 logical payload
~~~

所以 cache hit 以后仍然比较 shared IPCMetadata UID。

这区分：

~~~text
mapping validity
vs
data-generation validity
~~~

二者是不同层。

## per-block shared metadata 为什么用 shm_open/mmap

VMM FD 只负责 GPU allocation import。

还需要一小块 CPU-visible cross-process state：

~~~text
refcount
generation UID
publish timestamp
~~~

固定实现为每个 block 创建共享 metadata segment。

这正好体现 data/control split：

~~~text
large payload:
GPU VMM

small ownership state:
POSIX shared memory
~~~

## HostEndpointManager 为什么还有 host-wide registry

per-block metadata 只能回答 block lifetime。

优化路径判断需要回答 endpoint capability：

~~~text
remote GID belongs to which process?
same host?
which CUDA device?
which Linux uid?
IPC capable?
~~~

因此需要另一套 endpoint registry。

不要把“共享内存”当成一种统一用途；同一个系统里可以存在：

~~~text
payload memory
block lifetime metadata
endpoint discovery registry
~~~

三种完全不同共享区。

## 为什么 registry 仍需要 inter-process synchronization

共享 page 只让 bytes 可见。

多个进程同时写 registry slot 仍然有 race。

固定实现配 process-shared semaphore 保护 registry mutation/scan，然后把结果复制到本地 unordered_map cache。

这就是：

~~~text
slow/control shared state
→ synchronized shm registry

fast/local lookup
→ process-local hash map
~~~

## GID 为什么复制成 std::array

如果 cache key 保存 borrowed pointer 指向 RMW 临时结构，生命周期很危险。

固定 GidKey：

~~~text
copy fixed-size bytes into std::array
→ value semantics
→ stable equality/hash
~~~

这是把 external API handle 转成内部拥有 key 的常见做法。

## 为什么使用 abstract Unix socket

Linux abstract AF_UNIX address 不需要 filesystem socket file。

优点：

~~~text
no pathname file creation
no stale socket file cleanup
namespace belongs to kernel
~~~

很适合这种进程临时 capability service。

## FDDispatcher 的 shutdown 顺序

正确关闭：

~~~text
running=false
↓
write eventfd
↓
join dispatcher thread
↓
remove sockets / close duplicated FDs
↓
close eventfd
↓
close epoll fd
~~~

不能先 close epoll/socket 再让 worker 自己“慢慢退出”，否则 thread 可能同时访问已经失效的 fd。

shutdown order 本身就是 ownership proof。

## 和其他工业系统的对应

DMA-BUF、Wayland、V4L2/DRM/GBM 都大量使用 fd-backed buffer 与 Unix FD passing。

所以这里学到的不是 ROS 特有知识，而是 Linux accelerator IPC 的通用范式。

## 一个可迁移 Reactor 模板

~~~cpp
class FdDispatcher {
    int epoll_fd;
    int wake_eventfd;

    std::unordered_map<int, Resource> resources;
    std::mutex resources_mutex;

    std::thread thread;
    std::atomic<bool> running;
};
~~~

事件循环：

~~~text
epoll_wait
↓
wake_eventfd?
    handle control/shutdown
else
    lookup resource by fd
    handle readiness
~~~

这个模板可以直接扩展到 device fd、timerfd、network socket、IPC server。

## 本篇最重要的 OS 结论

1. FD 是进程局部索引；跨进程要传 kernel object capability。
2. SCM_RIGHTS 是 Linux FD capability transfer。
3. epoll 能把很多 blocking endpoints 收敛成一个 Reactor。
4. eventfd 是唤醒/关闭 Reactor 的一等控制通道。
5. shared memory 只共享 bytes，不自动提供互斥。
6. host-wide control state 应转成本地 cache，避免每帧进入跨进程锁。
