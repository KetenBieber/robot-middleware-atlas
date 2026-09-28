# IgH EtherCAT Master：先把一条 1 kHz 控制周期在主站里跑通


> 本专题已固定到 EtherLab / IgH EtherCAT Master ``stable-1.6``，版本 1.6.13，提交 ``61cc654f5b721ddd54df0f58bdd34106d91c5359``。本页仍然承担阅读地图作用；涉及具体对象、锁、线程与调用方向时，后续章节将直接贴这个固定提交中的连续源码并逐层核对。

## 为什么 EtherCAT 主站值得单独拆成一个源码专题

如果只从应用 API 看 EtherCAT，控制循环可能只有几步：收包、更新过程数据、读取输入、计算控制量、写输出、排队、发包。真正的工程难度却全部藏在这些动作之间。

假设机器人关节控制器以 1 kHz 运行，每个周期只有 1 ms。控制线程真正关心的是：本周期读到的编码器值来自哪一次总线交换；写进 PDO 的目标转矩会在哪一个帧里离开网卡；多个从站的数据怎样映射成连续 process image；Working Counter 异常或从站掉出 OP 时哪一层先发现；SDO/CoE、扫描与 DC 校时是否侵入实时数据面；以及 `send/receive` 背后到底有没有动态分配、等待或不可控的锁竞争。

这就是为什么 EtherCAT 源码不能只读成“协议解析器”。对机器人控制而言，它是一套把 **控制线程时间语义**、**过程数据内存布局**、**EtherCAT datagram**、**从站状态机** 与 **NIC 数据路径** 接在一起的运行时。

## 先建立最小对象图：不要一上来钻进网卡驱动

源码阅读会从应用可见对象开始，而不是按照目录顺序翻文件。第一版对象图只保留六个概念：

```text
control application
        |
        | ecrt_* API
        v
     Master
      /   \
     /     \
 Domain   Slave Config
   |           |
process      PDO / mailbox /
 image       state requests
   |
Datagram queue / FSM
        |
      Device
        |
       NIC
        |
 EtherCAT slaves
```

最重要的分界是 **Domain/process image** 与 **datagram** 不在同一个抽象层。process image 面向控制算法：应用希望看到“某个固定 offset 就是关节 3 的实际位置”；datagram 面向协议：它关心命令、逻辑或物理地址、长度、Working Counter，以及这些字节怎样装进 Ethernet frame。

如果把两层混在一起，应用每个周期都得理解帧格式；如果完全隔离又没有映射关系，主站就不知道 process image 的哪一段必须进入哪个 datagram。后续源码阅读的核心问题之一，就是这条映射怎样在配置期建立，并在周期路径上以尽量低的成本被重复使用。

## 第一条主线：从 public API 追一整个周期

源码到位后，第一轮只追控制线程真正会反复调用的入口。预期搜索锚点包括：

```c
ecrt_master_receive(...);
ecrt_domain_process(...);

/* read PDO input from process image */
/* run control law */
/* write PDO output into process image */

ecrt_domain_queue(...);
ecrt_master_send(...);
```

这段代码本身并不难，难的是把每一步的隐藏状态全部展开。

### receive 之后，数据在哪里

首先确认 `ecrt_master_receive` 到底从哪里取得已返回的帧：是直接消费 device 层已经接收的 buffer，还是还存在额外队列；帧与先前发送的 datagram 怎样匹配；Working Counter 在哪里更新；超时 datagram 怎样从 in-flight 状态退出。

随后进入 `ecrt_domain_process`。如果 receive 已经拿到返回字节，为什么还需要 domain_process？这里要从固定源码判断 Domain 是否承担 datagram 完成状态聚合、Working Counter 状态汇总、process image 可见性更新或其他职责，并确认这些动作在哪个执行上下文发生。

### queue 之后，为什么还没有真正发包

`ecrt_domain_queue` 与 `ecrt_master_send` 的分离非常值得读。最朴素的实现是“某个 Domain 一 queue 就立即发网卡”，但这样多个 Domain 很难在同一个周期统一组织发送顺序、frame packing 与链路时刻。更合理的设计通常会把“准备本周期过程数据”与“统一提交链路”分开。

这只是待源码验证的设计推导；真正结论会绑定固定提交的函数体、对象字段和下一跳。

## 第二条主线：Domain 与 process image 到底解决了什么

控制程序最自然的想法是把各从站 PDO 看成结构化状态：位置、速度、转矩、status word、control word。但 EtherCAT 从站实际暴露的 PDO entry 不会天然按某个 C/C++ 结构体排列，不同从站、SyncManager 与 PDO assignment 最终都要被映射到一块确定的字节区域。

所以这部分源码必须回答四个问题：

1. PDO entry 注册时谁计算逻辑 offset 与 bit position；
2. Domain 的 process image 内存由谁申请、何时确定大小；
3. 一个 Domain 会生成一个还是多个 datagram，边界由什么决定；
4. Master activate 以后是否还允许修改映射，若不能，冻结点在哪里。

这里会重点区分 **配置期数据结构** 与 **周期期数据结构**。链表很适合配置阶段动态收集 PDO、FMMU、slave config 等对象，但 1 kHz 路径不希望每帧重新遍历复杂配置拓扑；offset table、预构建映射或可复用 datagram 则可能把成本前移。具体使用了什么结构、为什么这样用，必须从本地固定源码逐个对象说明。

## 第三条主线：datagram 才是主站数据面的最小协议工作单元

进入 datagram 层以后，阅读重点从对象关系切换成状态机。一个 datagram 至少要回答：它当前是空闲、已排队、已发送、已接收还是超时；index/sequence 怎样让返回帧找到原请求；payload 指向 Domain 内存还是自己的 buffer；Working Counter 的期望值怎样建立；多个 datagram 怎样装进一个 Ethernet frame；发送失败时状态怎样回滚或向上层报告。

这一层也最适合讲 C 数据结构：intrusive list、枚举状态、缓冲区所有权、对象复用以及边界检查。实时系统常识告诉我们“周期路径里频繁 malloc/free 很危险”，但文章不会把这个常识直接当成 IgH 的实现事实；我们会实际找出分配点、复用点和最大容量决定方式。

## 第四条主线：从站状态机为什么不能塞进周期 PDO 代码

EtherCAT 从站并不是上电后永久处于 OP。扫描、SII、AL 状态切换、PDO/SyncManager/FMMU 配置、邮箱协议、掉线恢复都需要自己的控制流程。

最朴素的实现是每个 1 ms 周期先交换 PDO，然后顺手扫描所有 slave、必要时做 SDO、切状态或重配置。问题是这些管理动作的时延和分支远大于稳定过程数据交换；如果某个邮箱事务还需要等待响应，更不能阻塞整个实时循环。

因此后续会专门追 Master FSM、Slave FSM、配置 FSM 与邮箱 FSM：长事务怎样被拆成“一次只推进一步”的非阻塞状态；由谁周期性推进；它们怎样申请或复用 datagram；以及管理流量和 process-data datagram 如何共享 Master 数据面而不互相破坏时间预算。

## 第五条主线：Device/NIC 边界决定主站实时性真正能走到多远

用户态控制算法很快，不代表网卡数据面就是确定的。最终必须追到 Device 抽象和实际 NIC 路径，回答 tx/rx buffer 谁拥有、发送接收在哪个上下文发生、是否存在额外队列和锁、link state 怎样反馈、in-flight datagram 在 shutdown 时怎样收束。

这一步会特别区分“平均 1 kHz 能跑”与“1 kHz worst-case 可推理”。如果发送路径穿过普通网络栈、软中断、驱动 ring 和不可控队列，那么实时预算必须把这些调度边界一并算进去；如果固定版本存在专用 EtherCAT device 路径，也要解释它具体减少了什么不确定性，而不是笼统写成‘更实时’。

## 第六条主线：Distributed Clocks 不是一个单独的校时函数

DC 的目标不是单纯让 Linux 主机时间更准，而是让多个从站在足够小的相位偏差下共同采样和输出。后续会按“测量 → 选择参考 → offset/drift 调整 → 周期同步 datagram → application time”追完整链路，并把 Sync0/Sync1 与控制周期的关系放回 observation age 和 actuation age 中解释。

## 源码到位后会拆成哪些文章

专题计划按认知顺序展开，而不是按目录顺序：

1. **architecture-map**：Master、Domain、Slave Config、Datagram、FSM、Device 的对象骨架与所有权；
2. **master-lifecycle**：request/create、activate、deactivate/release，找出配置期到周期期的冻结边界；
3. **domain-process-image**：PDO 注册、offset/bit position、process image 与 datagram 映射；
4. **cyclic-send-receive**：完整跑一轮 receive → process → queue → send；
5. **datagram-frame**：datagram 状态机、frame packing、Working Counter 与 timeout；
6. **slave-fsm-mailbox**：扫描、配置、AL 状态以及 CoE/SDO 等邮箱事务；
7. **device-nic-runtime**：设备抽象、NIC 发送接收、buffer 与执行上下文；
8. **distributed-clocks**：参考时钟、漂移校正、Sync0/Sync1 与控制相位；
9. **realtime-concurrency**：锁、链表、内存分配、内核上下文和关闭竞态；
10. **design-recap**：把主站压缩成最小可实现模型，区分 EtherCAT 协议复杂度与 Linux 实时工程复杂度。

固定源码进入本地以后，第一篇正式源码深读会从 **Master 生命周期 + Domain/process image** 开始，因为它们决定后面所有周期数据结构为什么能在实时路径上保持稳定。