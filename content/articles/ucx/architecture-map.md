# UCX 架构地图：Context、Worker、Endpoint、Request 怎样连成一张对象图

固定源码版本：8a6b06fb880accbb933a79cda893883872c68d9d（UCX v1.22.0）。

先不要从函数名记忆 UCX。把运行时还原成对象关系，会发现最重要的四个 UCP 对象分别对应四种寿命：进程级资源、执行上下文、peer 连接、一次在途操作。

~~~text
ucp_context_t
  |
  +-- transport resources / memory domains / config
  |
  +-- ucp_worker_t
       |
       +-- uct_worker_t
       +-- ucp_worker_iface_t* ifaces[]
       +-- request pool
       +-- endpoint list / maps
       |
       +-- ucp_ep_t  ---- lane[0] -> uct_ep_t
       |             ---- lane[1] -> uct_ep_t
       |             ---- lane[N] -> uct_ep_t
       |
       +-- ucp_request_t ...
~~~

## Worker 为什么是最值得看的结构体

固定源码中的 ucp_worker_t 不是一个薄句柄。删去与主题无关字段后，仍能看到非常明确的运行时骨架：

~~~c
typedef struct ucp_worker {
    ucs_async_context_t  async;
    ucp_context_h        context;
    uct_worker_h         uct;

    ucs_mpool_t          req_mp;
    ucs_mpool_t          rkey_mp;

    ucs_list_link_t      all_eps;
    ucp_worker_iface_t   **ifaces;
    unsigned             num_ifaces;

    ucs_queue_head_t     rkey_ptr_reqs;
    ucp_tag_match_t      tm;

    ucp_ep_h             mem_type_ep[UCS_MEMORY_TYPE_LAST];

    ucp_worker_rkey_config_hash_t rkey_config_hash;
    UCS_PTR_MAP_T(ep)             ep_map;
    UCS_PTR_MAP_T(request)        request_map;

    ucp_ep_config_arr_t  ep_config;
    ucp_rkey_config_arr_t rkey_config;
} ucp_worker_t;
~~~

这里没有 C++ STL，但设计问题与 STL 容器选择完全同构。高频、短寿命 request 用 memory pool，而不是每次 malloc/free；需要按 ID 找回 endpoint/request 的对象用 pointer map/hash；需要 FIFO 推进的 rkey_ptr request 用 queue；所有 endpoint 用 intrusive list 维护。**数据结构是按访问模式选的，而不是统一塞进一个 map。**

这也是研究 C 系统代码时很有价值的一点：STL 只是容器实现之一，真正要学的是 workload。若自己写 C++ runtime，同样的问题会对应 object pool、unordered_map、deque/list、flat vector 等不同选择。

## Endpoint 不是“一条 socket”

ucp_ep_t 表示一个 peer，但 peer 到 peer 并不只绑定一个 transport。Endpoint config 会保存多条 lane；一条 lane 关联一个 UCT endpoint，并标注它适合哪些语义。RMA、high-bandwidth RMA、atomics、tag offload、wireup、keepalive 可以落在不同 lane 上。

因此“连接建立成功”并不等于“选出一个 socket”。更准确的说法是：UCX 根据本地和对端 capability 建立一张 **peer-specific communication plan**，之后 protocol selection 再在这张 plan 上挑实际协议。

## Request 是协议状态机的承载体

一次发送若立即完成，可以直接返回成功；若不能立即完成，UCP 需要保存 datatype iterator、chosen protocol、stage、completion、callback 等状态。ucp_request_t 就是这个在途状态机的载体。

Request 被池化还有第二层意义：高频控制/感知 pipeline 中，一秒可能发出成千上万次小操作。若每一次都走通用 heap allocator，不但平均开销上升，尾延迟也更难控制。对象池把“动态生命周期”与“每次向操作系统申请内存”分开。

所以 UCX 的对象图本质上围绕三个问题组织：**资源发现归 Context，执行与 progress 归 Worker，peer capability 归 Endpoint，短期协议状态归 Request。** 这四种寿命分开以后，transport 与 protocol 才能在不污染应用 API 的情况下替换。

## 不要只画“包含关系”，还要画 Ownership

上面的对象树看起来像：

~~~text
Context
└── Worker
    └── Endpoint
        └── Request
~~~

但真正写程序时，最容易出错的不是“谁在谁下面”，而是：

~~~text
谁拥有谁？
谁只是暂时引用？
谁可以比谁先销毁？
异步 callback 里还藏着谁的指针？
~~~

更接近运行时的图应该是：

~~~text
Application
    │ owns
    ▼
UCP Context
    │ resource/config lifetime
    ├──────────────┐
    ▼              ▼
Worker A        Worker B
    │ owns progress domain
    │
    ├── Endpoint(peer X)
    ├── Endpoint(peer Y)
    │
    └── Request pool
          │
          ├── active request R0
          ├── active request R1
          └── free request slots
~~~

Request 又可能暂时进入 protocol progress、transport pending queue 和 completion callback，所以 Request 的实际生命周期不是“API 调用开始到 API 调用返回”，而是：

~~~text
obtain from pool
↓
initialize protocol state
↓
issue or enqueue pending
↓
progress several times
↓
completion
↓
user callback / status observed
↓
return to pool
~~~

这和线程通信中“slot 被 Producer 写完以后还不能马上复用”完全同构。

## 为什么 Context、Worker、Endpoint 不应该揉成一个 Connection

假设自己设计一个 C++ 通信库，很容易先把 peer、设备、pending queue、registration cache 和 socket 全塞进一个 Connection 对象。

一个对象看起来直观，但如果有 100 个 peer，就要问：

~~~text
是否重复做 100 次 transport discovery？
是否重复维护 100 份 registration cache？
是否需要 100 个 progress thread？
~~~

UCX 的拆分依据其实是**变化频率与共享范围**：

| 状态 | 典型变化频率 | 适合的 owner |
| --- | --- | --- |
| transport / MD / config | 进程启动后较稳定 | Context |
| progress、request pool、iface | 执行域级 | Worker |
| 对端 capability / lane plan | peer 生命周期 | Endpoint |
| 一次发送/接收的 stage | 每次操作 | Request |

于是多个 Endpoint 可以复用 Worker 的 iface、request pool 和 progress machinery；多个 Worker 又可以复用 Context 的资源发现结果。

这是一条非常通用的系统设计原则：

> **把寿命相近、变化频率相近、共享边界一致的状态放在一起。**

## Worker 里的容器为什么不能统一成一种 Map

Worker 里同时存在 pool、list、queue、array、hash/map。它们不是“C 项目没用 STL”，而是访问模式不同。

### Request pool：生命周期高频，查找不是主要操作

如果一秒创建几十万次短 request，反复 malloc/free 会把 allocator 的锁、metadata 和 cache miss 带进热路径。

所以 request 使用对象池。若自己用 C++ 重写，对应思路更接近：

~~~text
std::pmr
slab allocator
object pool
freelist
~~~

而不是默认把每个 Request 放进通用 map。

### Endpoint list：主要操作之一是遍历

如果常见操作是：

~~~text
flush all endpoints
close all endpoints
walk all peers
~~~

intrusive list 很自然：节点已经嵌在 Endpoint 内，不需要额外 wrapper allocation。

### ep_map / request_map：协议消息带 ID，需要反查对象

这里的访问模式正相反：

~~~text
wire/control message carries id
↓
find endpoint/request
~~~

因此 hash/map 才合适。

### ifaces[]：资源索引是稠密整数

如果索引本身已经是 0..N-1，数组直接寻址最自然：

~~~text
ifaces[rsc_index]
~~~

这就是“先看访问模式，再选容器”的具体案例。

## Endpoint Config 是把昂贵决策变成可复用状态

如果每次发送都重新问：

~~~text
哪个 NIC 可达？
对端支持哪个 Memory Domain？
哪个 lane 支持 RMA？
哪个 lane 适合高带宽？
~~~

热路径会不断重复控制面工作。

UCX 的 Endpoint config 先把 peer-specific capability 编译成一张通信计划：

~~~text
peer
↓ wireup
lane[0]
lane[1]
...
role -> lane index
~~~

之后 protocol selection 只在这组已经证明可用的 lane 上继续选择。

这种“启动/配置阶段做复杂搜索，运行阶段只走索引”的思想同样适用于机器人 topic routing、EtherCAT PDO mapping、GPU kernel plan 和 sensor graph。

## Request Pool 解决 Allocation，不自动解决 Ownership

对象池只解决 storage 从哪里来，并不自动证明 request 何时可以回池。

仍然要问：

~~~text
transport pending queue 是否仍持有它？
completion callback 是否仍会访问它？
用户是否保存了 request handle？
~~~

如果旧 Request R0 仍被 callback 引用，却已经回池并被复用为 R1：

~~~text
old R0 pointer
     │ stale callback
     ▼
same address now stores R1
~~~

地址相同，但逻辑身份已经变化。这和 lock-free 数据结构的 ABA、共享内存 stale descriptor 属于同一类生命周期问题。

## 多线程程序真正需要先决定 Worker 拓扑

程序组织上，比“有多少 Endpoint”更重要的是“有多少 Worker，以及谁拥有它”：

~~~text
方案 1
all application threads
        ↓
one MULTI Worker

方案 2
many producers
        ↓
bounded MPSC submit queue
        ↓
one communication thread
        ↓
one SINGLE Worker

方案 3
pipeline A → Worker A
pipeline B → Worker B
pipeline C → Worker C
~~~

三种方案分别把共享状态放在不同位置：

| 方案 | 共享状态 | Progress owner | 主要风险 |
| --- | --- | --- | --- |
| 单 MULTI Worker | 最大 | 多线程共同访问 | lock contention、ownership 复杂 |
| 单 owner Worker | 应用 queue 集中 | 一条通信线程 | submit backlog、CPU 预算 |
| 多 Worker shard | Worker 内共享较少 | 每个 shard | state/resource duplication |

所以“Worker 是 progress domain”应该进一步翻译成：

> **Worker 是需要被明确分配线程所有权、CPU 预算和关闭责任的执行域。**

## Shutdown 必须按依赖图反向收束

如果依赖方向是：

~~~text
Context
↓
Worker
↓
Endpoint
↓
Request
~~~

关闭应该反向解除依赖：

~~~text
1. 禁止新 request
2. drain / cancel / fail active request
3. flush / close Endpoint
4. destroy Endpoint
5. stop Worker progress / wakeup
6. destroy Worker
7. destroy Context
~~~

这和共享内存 pool、线程池、Cyber Component、GPU stream teardown 都遵循同一原则：

> **先关入口，再收束在途工作，最后释放承载它们的长期 owner。**

## 把这张对象图迁移成通用 Runtime 模板

即使完全不用 UCX，也可以借用它的寿命分层：

~~~text
RuntimeContext
  全局设备、allocator、配置

ExecutionDomain / Worker
  thread、queue、event loop、object pool

Peer / Channel
  对端能力、route、cached plan

Operation / Request
  一次在途任务的状态机
~~~

这个模板可以出现在 CAN/EtherCAT gateway、GPU inference runtime、自研 IPC、camera pipeline 或 network service 中。

UCX 架构图真正值得学习的不是四个 typedef，而是**按寿命、共享范围和执行责任拆对象**的方法。
