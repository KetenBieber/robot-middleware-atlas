# Endpoint Locality 与 Fallback：优化路径为什么必须是条件性的

固定源码版本：d7cd9642d77a1d64fd85f25ba0bf96e108401900。

zero-copy 最大的工程误区之一，是把“系统支持 CUDA IPC”理解成“所有消息都应该走 CUDA IPC”。

真实系统必须逐 endpoint 判断。固定 backend 把这件事做成明确的 capability state machine。

## EndpointLocality 不是展示字段，而是 data-path 输入

源码把 endpoint 分成：

~~~text
UNDEFINED
INTRA_PROCESS
INTER_PROCESS_SAME_HOST
INTER_HOST
~~~

这个状态直接决定 storage 是否有资格共享。

## on_creating_endpoint 与 on_discovering_endpoint 的职责

本端 endpoint 创建时，HostEndpointManager 向 host-wide registry 登记：

~~~text
GID
endpoint type
process instance
device id
Linux uid
IPC capability
~~~

remote endpoint 被发现以后，再形成资格判断：

~~~text
remote supports CUDA backend?
AND
local VMM pool IPC-capable?
AND
locality compatible?
AND
device compatible?
AND
Linux uid compatible?
~~~

任一条件不满足，都应该走 fallback。

## 为什么 same process 不等于“应该走 IPC”

同进程最直接的数据路径是共享同一个 CUDA-backed Buffer ownership。

如果同进程却绕进 VMM IPC descriptor/event path，会增加：

~~~text
descriptor construction
IPC event restrictions
extra synchronization
extra bookkeeping
~~~

所以 locality 越近，越应该优先选择更短的 ownership path。

## 为什么 same host 还不够

一台机器可以有多块 GPU。

当前实现对同主机跨进程还要求：

~~~text
remote device id == local device id
~~~

因为：

~~~text
same host
!=
same accelerator memory domain
~~~

未来即使支持 peer-access/NVLink，也应该把这种 capability 显式建模，而不是把 same-host 当万能条件。

## 为什么 Linux uid 也进入优化决策

GPU allocation、FD 与共享内存都跨越进程安全边界。

固定实现要求 same uid，本质是在定义 local trust boundary。

所以 data-path planner 不只看性能拓扑，还要看：

~~~text
hardware capability
process locality
security identity
~~~

## 为什么 shared registry 还要复制进 local cache

每帧进入 process-shared registry + semaphore 会把控制面锁带进高频数据面。

因此 HostEndpointManager 把共享事实整理成本地：

~~~text
GID → CachedEndpointInfo
~~~

steady state 只做进程内 hash lookup。

这是典型：

~~~text
slow synchronized discovery
→ local materialized facts
→ fast data-path lookup
~~~

## 为什么 backend 还有第二层 decision cache

HostEndpointManager 缓存事实：

~~~text
locality / device / uid / ipc capability
~~~

CUDA backend 再缓存策略结果：

~~~text
GID → final CUDA IPC yes/no
~~~

事实 cache 与 policy cache 即使 key 相同，也不应该机械合并。

以后 policy 增加 size threshold、QoS、security rule、device load 时，这两层可以独立失效和重算。

## optimized backend 为什么必须允许“不适用”

如果 endpoint 已知不能走 CUDA IPC，backend 不应该硬造无效 descriptor。

更合理的 contract：

~~~text
optimized path unavailable
→ upper layer chooses fallback
~~~

这样 CUDA backend 是 optional acceleration，而不是 message correctness 的前提。

## descriptor publish 为什么是 ownership transition

Publisher 在生成 descriptor 前会 finalize writer，使 producer completion event 已经存在。

逻辑：

~~~text
producer stream writes
↓
write event recorded
↓
descriptor may be published
~~~

如果顺序反过来，Subscriber 可能先获得 storage reference，却拿不到正确 producer fence。

## zero-copy 不能只数 memcpy

有些路径 copy 少，却增加：

~~~text
descriptor setup
CUDA event import
synchronization
kernel crossings
cache lookups
~~~

所以正确性能模型至少要统计：

~~~text
copy count
synchronization count
CPU blocking time
GPU wait time
descriptor/control-plane cost
~~~

“0 copy”不是端到端 latency 的同义词。

## import 失败应该分类

真实失败包括：

~~~text
stale generation UID
different CUDA device
FD/socket import failure
event import failure
endpoint capability changed
publisher exited
~~~

这些恢复语义不同。

生产 runtime 最好区分：

~~~text
capability mismatch
transient IPC failure
stale descriptor
peer failure
programming error
~~~

否则所有问题最后只剩一条“CUDA backend failed”。

## fallback 成本必须可观测

从 CUDA path 回 CPU 可能引入：

~~~text
D2H copy
CPU allocation
serialization
network/RMW transfer
H2D copy
stream synchronization
~~~

建议记录：

~~~text
selected backend
fallback reason
converted bytes
D2H/H2D count
conversion latency
~~~

这样 p99 抖动才有可解释性。

## fallback correctness 与 real-time correctness 不同

假设 normal path：

~~~text
2 ms handoff
~~~

fallback path：

~~~text
12 ms D2H + serialize + H2D
~~~

业务 payload 仍然正确，但控制 deadline 可能已经失效。

因此 capability degradation 可以触发：

~~~text
alert
rate reduction
disable noncritical branch
degraded mode
fault policy
~~~

而不是静默继续。

## 一个具身机器人拓扑

~~~text
Camera GPU frame publisher
├─ VLM encoder: same process GPU
├─ mapping process: same host same GPU
├─ telemetry: same host CPU
└─ remote visualization laptop
~~~

合理 data path：

~~~text
VLM       → direct local CUDA
mapping   → VMM IPC
telemetry → CPU conversion
remote    → serialization/network
~~~

同一 logical topic 完全可以 per-endpoint 使用不同 physical path。

## 与 UCX 的类比

UCX 根据：

~~~text
memory type
message size
transport capability
endpoint lanes
~~~

选择 protocol/path。

这里根据：

~~~text
locality
device
uid
backend support
VMM IPC capability
~~~

选择 direct/IPC/fallback。

共同原则是：

> semantic operation 保持稳定，physical data path 根据 capability 动态选择。

## 更通用的 Capability Descriptor

自研 VLA runtime 可以描述：

~~~text
host identity
process identity
security uid
device type/id
supported memory domains
export/import handle types
supported completion fences
max buffer size/alignment
transport capabilities
~~~

Path planner 再选择：

~~~text
direct share
same-host IPC
RDMA
staging copy
serialization
~~~

这比 hard-coded if(cuda) 更可扩展。

## 本篇最重要的五点

1. zero-copy 必须 per-endpoint 决策，不应该是全局模式。
2. locality、device 与 security identity 都属于 data-path capability。
3. 控制面 facts 应 materialize 成本地 fast-path decision。
4. fallback 保证功能 correctness，但未必保证 real-time deadline。
5. 生产系统必须观测当前真实 data path，而不是只观测最终 FPS。
