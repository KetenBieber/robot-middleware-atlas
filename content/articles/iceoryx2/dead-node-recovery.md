# Dead Node Recovery：进程被 SIGKILL 后，Shared Memory 谁来收尸

固定源码版本：135d09dd8b29f321f1725920d434864c4e512378（v0.10.0）。

## 共享内存系统最危险的错误不是“没收到消息”

而是：

~~~text
Process A
创建 SharedMemory / Publisher / Connection
↓
借出 Chunk
↓
突然 crash
~~~

OS 会回收进程私有 heap 和 file descriptor，但共享资源、其他进程中的 connection state 与 ownership metadata 不能靠 C++/Rust 析构自动闭环。

所以 crash recovery 必须是 runtime 的一等公民。

## NodeState 把死亡显式建模

固定源码：

~~~rust
pub enum NodeState<Service> {
    Alive(AliveNodeView<Service>),
    Dead(DeadNodeView<Service>),
    Inaccessible(UniqueNodeId),
    Undefined(UniqueNodeId),
}
~~~

这比简单 bool alive 更完整。

### Alive

monitoring 判断 owner process 仍存在。

### Dead

owner 已死亡，当前进程可以尝试接管 stale cleanup。

### Inaccessible

权限不足，不能确认生死。

### Undefined

资源缺失或状态不一致。

所以：

> 看不到 heartbeat 不能简单等价于“可以删除所有资源”。

## DeadNodeView 为什么存在

当 Node 已死，另一个活进程可以调用：

~~~rust
dead_node
    .try_remove_stale_resources()
~~~

Node 本身也提供：

~~~rust
try_cleanup_dead_nodes()
blocking_cleanup_dead_nodes(timeout)
~~~

这使 cleanup 不依赖已经死亡进程的析构逻辑。

## 为什么 Cleanup 还会失败

NodeCleanupFailure 包含：

~~~text
Interrupt
InternalError
InsufficientPermissions
VersionMismatch
ResourcesAlreadyCleanedUp
AnotherInstanceIsCleaningUpTheNode
~~~

这说明 cleanup 本身也是分布式 ownership 问题。

多个活进程可能同时发现同一个 dead node。

必须避免两个 Cleaner 同时删除同一套资源。

## Drop 路径仍然做正常清理

正常退出时，SharedNodeState drop 会根据配置执行 dead-node cleanup，并移除当前 node resources。

所以有两条生命周期：

~~~text
normal:
RAII drop

abnormal:
other process detects dead node
→ stale cleanup
~~~

生产级 IPC 两条都要有。

## Stale Cleanup 要清什么

逻辑上可能包括：

- node ownership record；
- service membership；
- Publisher/Subscriber endpoint slot；
- ZeroCopyConnection；
- shared-memory ownership metadata；
- outstanding used chunk state；
- event/monitoring resource。

并不是简单 unlink 一个 shm 文件。

## 为什么 VersionMismatch 是真实问题

如果旧版本进程留下的共享数据结构布局与新版本不同，新进程不能假装能安全解释。

所以 cleanup API 把 VersionMismatch 单独暴露。

这体现：

> shared memory 既是数据，也是 ABI。

升级生产系统时必须考虑跨版本遗留资源。

## Container / Namespace 场景为什么更难

进程存活检测在容器、PID namespace、权限隔离下可能不等同于裸主机 PID 查询。

因此 Monitoring 必须是可替换机制，而不能把某个 OS 假设散落在业务层。

## 对机器人系统的意义

感知或 VLA 进程崩溃后，系统最怕：

~~~text
共享 pool 永久耗尽
↓
其他健康模块也无法继续工作
~~~

stale cleanup 的目标就是防止局部崩溃把共享资源永久锁死。

所以 fault containment 也是高性能 IPC 的一部分。
