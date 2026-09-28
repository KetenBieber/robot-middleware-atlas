# 真实案例：ETH RSL soem_interface 怎样把 SOEM 包成机器人可复用的 C++ EtherCAT 总线层

案例仓库：[`leggedrobotics/soem_interface`](https://github.com/leggedrobotics/soem_interface)

本页固定案例提交：`6e8ab4d62bc9204dcd25454e19abfa9318bfed6f`。

核心源码：[`soem_interface_rsl/src/soem_interface_rsl/EthercatBusBase.cpp`](https://github.com/leggedrobotics/soem_interface/blob/6e8ab4d62bc9204dcd25454e19abfa9318bfed6f/soem_interface_rsl/src/soem_interface_rsl/EthercatBusBase.cpp)。

这个项目来自 ETH Zurich Robotic Systems Lab。README 对它的定位很直接：它为同一 EtherCAT 总线上的一个或多个设备提供 C++ 接口，底层通信由 SOEM 负责。

这里真正值得看的不是“怎么调 SOEM API”，而是：

> 当 SOEM 只是一个 C Library 时，机器人软件会在它上面再长出怎样的对象模型？

## 第一层：SOEM Context 被包进一个 Bus 对象

`EthercatBusBase` 不把 `ecx_contextt` 暴露给每个设备类，而是在实现对象里集中持有 Context、IOmap、互斥锁和从站集合。

上层对象关系变成：

~~~text
EthercatBusBase
├── one SOEM context
├── IOmap
├── contextMutex
└── vector<shared_ptr<EthercatSlaveBase>>
~~~

SOEM 自身用固定 `slavelist[]` 描述物理 EtherCAT 拓扑；这个项目则额外用 `std::vector` 保存业务层 Slave 对象，并按 EtherCAT address 排序。

这正好说明一个很重要的设计原则：

> SOEM hot path 使用固定容量结构，不意味着业务层也必须拒绝 STL。配置期完全可以使用 `vector/shared_ptr` 建模设备；关键是周期热路径不要不断重分配和重建映射。

## 第二层：startup 把“发现—设备配置—IOmap 编译”拆成三段

真实启动逻辑不是一行 `ecx_config_init()`。

它先检查 NIC，再初始化 SOEM Context，然后重复执行 slave detect，直到发现的设备数满足业务层已经注册的 Slave 数量。

之后才进入正式配置：

~~~text
ecx_init
→ ecx_detect_slaves with retry
→ ecx_config_init
→ PRE_OP
→ each EthercatSlaveBase::startup()
→ ecx_config_map_group
→ PDO size validation
→ clear IOmap
~~~

这一结构比 SOEM sample 更接近真实机器人系统：Master 发现“线上有什么”，业务层同时知道“我期望什么设备”。两者必须做一致性检查。

## 第三层：为什么在 config_map 后再次检查 PDO size

项目不是只相信“SOEM mapping 成功”。

每个业务 Slave 提供自己期望的 RxPDO/TxPDO size，然后与 SOEM `slavelist[address].Obytes/Ibytes` 比较。

于是形成：

~~~text
device driver declares expected process image
                ↕
SOEM reports actual mapped process image
~~~

不匹配就拒绝继续。

这是非常值得机器人驱动复用的思想，因为最危险的错误之一不是总线完全断开，而是：

~~~text
总线正常
WKC 正常
但 application 对 PDO layout 的理解错了
~~~

## 第四层：updateWrite / updateRead 把 IOmap 与设备对象分开

项目没有让所有 Device 类直接调用 SOEM。

写方向先让每个 slave 把业务命令写进自己的 PDO buffer，再统一发送。

真实源码里的关键调用只有很短一行：

~~~cpp
ecx_send_processdata(&ecatContext_);
~~~

读方向先统一接收过程数据，然后做 WKC 检查，最后才让各 Slave 从已经更新的 IOmap 解析自己的反馈。

关键接收同样很短：

~~~cpp
wkc_ = ecx_receive_processdata(&ecatContext_, EC_TIMEOUTRET);
~~~

于是一次机器人控制周期在对象层表现成：

~~~text
all slave commands
→ updateWrite() into IOmap
→ SOEM send

next cycle
→ SOEM receive
→ WKC validation
→ all slave updateRead() from IOmap
~~~

这就是我们前面“SOEM process image 是协议层和设备驱动之间 ABI”的具体工程化版本。

## 第五层：为什么 Context 外面又加一把 std::mutex

几乎所有直接访问 SOEM Context 的调用都受 `contextMutex_` 保护。

原因是这个项目不只存在过程数据线程，还可能同时发生：

- SDO；
- state query；
- bus diagnosis；
- shutdown；
- DC configuration。

虽然 SOEM 2.0 自己在 frame index、RX 和 mailbox pool 内部已经有局部锁，但这不代表任意高层 API 组合天然线程安全。

这个项目选择更保守的策略：

~~~text
one context
→ one coarse application mutex
~~~

优点是 ownership 很清楚；代价是如果把慢 SDO 和 process-data 放在同一把锁上，可能放大周期阻塞。因此真实 RT 系统仍需要进一步区分 data-plane 与 control-plane 调用。

## 第六层：WKC 不是只打印一次 warning

`updateRead()` 每周期计算：

~~~text
expected = outputsWKC * 2 + inputsWKC
~~~

如果实际 WKC 太低，项目会累计连续异常次数。

只有健康接收时才进入各 Slave 的 `updateRead()`。

这说明一个非常实用的数据有效性原则：

> 先验证这一周期 process image 是否可信，再让设备驱动解析它。

而不是让所有设备先读 stale memory，最后再打印一句“WKC 不对”。

## 第七层：Bus Monitoring 被故意放在另一条逻辑链

项目还有 `doBusMonitoring()`，用于：

- 读取 AL state；
- 打印 AL status code；
- 标记 lost slave；
- 分周期读取 ESC error counters。

尤其值得注意的是 error-counter diagnosis 没有一次性扫所有 slave，而是逐次处理一个设备。这说明作者已经意识到：

> 诊断本身也会制造额外 EtherCAT datagram，不能无条件把所有诊断工作塞进一个控制周期。

## 第八层：这个案例如何验证我们前面的 SOEM 设计结论

源码拆解里的概念，在这个真实项目里一一出现：

| SOEM 设计 | soem_interface 中的工程用法 |
| --- | --- |
| `ecx_contextt` | 被封装进一个 Bus runtime |
| application IOmap | Bus 统一拥有，Slave 只解析自己的区间 |
| `slavelist[]` | 与业务 `EthercatSlaveBase` address 对齐 |
| split-phase process data | `updateWrite()` / `updateRead()` |
| WKC | 作为 process image 有效性的门控 |
| blocking control API | 通过 Context mutex 与周期路径协调 |
| diagnostics | 独立 Bus Monitoring 逻辑 |

## 一个版本边界

这个案例仓库使用的是它自己携带/维护的较老 SOEM 接口版本，函数签名与我们固定研究的 SOEM v2.0.0 并不完全相同。

因此这里学习的是：

~~~text
SOEM 被真实机器人 C++ 软件怎样包起来
~~~

而不是把这个项目里的旧 API 原样复制到 SOEM 2.0 新工程中。

SOEM 2.0 应继续以本专题固定的 `304d1c05` 源码为 API 真值。