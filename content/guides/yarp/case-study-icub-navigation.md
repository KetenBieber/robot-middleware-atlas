# 项目案例：Robotology Navigation 如何用 YARP 组织机器人导航模块

[`robotology/navigation`](https://github.com/robotology/navigation) 提供基于 YARP 的自主导航模块和设备，并依赖 `icub-main`。它体现了 Robotology 生态常见结构：算法被包装成 YARP module/device，通过 Port、RPC 和配置文件与整套 iCub 软件连接。

先把本文研究的对象说清楚。这里不是试图用一篇文章讲完 iCub 的视觉、机械臂、语音和全身控制，也不是把整个 Navigation 仓库逐文件翻译一遍。本文选择其中最小但完整的 `robotGoto` 闭环：上层给出“移动到地图中的某个位置”，模块读取机器人位姿和激光数据，持续计算底盘速度，直到到达、暂停或失败。把这条闭环看懂以后，再加入全局路径规划器、真实激光雷达或真实底盘，只是在相同接口上替换模块。

可以先用一句话记住各层职责：

```text
客户端说“去哪里” → robotGoto 决定“这一周期怎么走” → baseControl 决定“轮子怎么动”
```

文章会沿着一次导航任务真正发生的顺序向下阅读，而不是按文件名罗列类：

1. 先认清系统解决的问题，以及哪些能力不属于 `robotGoto`；
2. 再看官方 XML 怎样把定位、激光、控制和客户端组装成运行图；
3. 从客户端的一次 `gotoTarget...()` 调用一路追到控制线程；
4. 拆开 C++ 对象的所有权、线程生命周期、状态共享和关闭顺序；
5. 最后评估性能与缺点，并抽出一套可以自己实现的最小架构。

本文源码坐标固定到提交 [`496e04a9`](https://github.com/robotology/navigation/tree/496e04a9997e6d57d32209858237a8b3f69dfa67)。这个坐标很重要：仓库从早期独立 `RFModule` 逐步演化为可由 `yarpdev` 装载的 device/plugin，若把旧教程的进程结构直接套到当前源码，会看错对象所有权和关闭路径。

| 阅读层次 | 固定提交入口 | 要回答的问题 |
|---|---|---|
| 部署图 | [`robotGotoExample1.xml`](https://github.com/robotology/navigation/blob/496e04a9997e6d57d32209858237a8b3f69dfa67/app/robotGotoExamples/scripts/robotGotoExample1.xml) | 哪些进程、device 和连接共同构成导航闭环 |
| 客户端 | [`robotGotoExample/main.cpp`](https://github.com/robotology/navigation/blob/496e04a9997e6d57d32209858237a8b3f69dfa67/src/tutorials/robotGotoExample/main.cpp) | 应用怎样通过 `INavigation2D` 发目标和读状态 |
| device 外壳 | [`robotGotoDev.h`](https://github.com/robotology/navigation/blob/496e04a9997e6d57d32209858237a8b3f69dfa67/src/navigationDevices/robotGotoDevice/robotGotoDev.h) | `DeviceDriver`、接口实现、RPC reader 怎样组合 |
| 控制线程 | [`robotGotoCtrl.cpp`](https://github.com/robotology/navigation/blob/496e04a9997e6d57d32209858237a8b3f69dfa67/src/navigationDevices/robotGotoDevice/robotGotoCtrl.cpp) | 传感器读取、状态机、速度计算和输出怎样串联 |
| 插件构建 | [`CMakeLists.txt`](https://github.com/robotology/navigation/blob/496e04a9997e6d57d32209858237a8b3f69dfa67/src/navigationDevices/robotGotoDevice/CMakeLists.txt) | `robotGotoDev` 怎样注册成 YARP 动态 device |

## 功能拆分

导航系统通常包含定位、地图、规划、控制与人机命令。连续状态用 streaming Port，低频控制和配置用 RPC，硬件访问通过 YARP device/`PolyDriver` 抽象。

对初次接触机器人导航的读者，可以把这几个词翻译成更日常的分工：

| 模块 | 它回答的问题 | 在本文闭环中的输入与输出 |
|---|---|---|
| 定位 | “机器人现在在哪里、朝向哪里？” | 传入地图坐标中的 `x/y/theta` |
| 激光测距 | “周围哪里有障碍？” | 传入一圈距离测量 |
| 目标跟踪 | “为了靠近目标，这一刻应该往哪走？” | 输出线速度、平移方向和角速度 |
| 底盘控制 | “如何把期望速度变成轮子或执行器动作？” | 接收速度，输出电机控制与里程计 |
| 客户端 | “任务要去哪里，当前完成了吗？” | 发送目标、暂停、恢复、停止并读取状态 |

`robotGoto` 主要承担第三项。它不是完整的地图构建系统，也不负责直接驱动某一种电机；它把外部目标、当前位姿和障碍数据变成底盘速度。这个边界解释了为什么源码中既能看到导航状态机，又看不到具体电机厂商的 CAN 指令。

```text
localization stream -> navigation module -> velocity command stream
map/service RPC ----^         |
operator RPC -----------------+
                           PolyDriver -> mobile base device
```

这种接口分类比“全部用 Bottle”更重要：流数据强调新鲜度，命令强调一次请求的结果，设备接口强调可替换驱动。

这里的 **streaming Port** 可以理解为持续流动的数据管道，例如定位每隔几十毫秒产生一帧新位姿；旧数据通常很快失去价值。**RPC** 则更像一次有返回值的函数调用，例如“接受这个目标了吗”。**device 接口** 是 C++ 抽象层，让同一套导航代码既能面对模拟底盘，也能面对真实硬件。三者不是三种写法偏好，而是分别服务于三种不同的数据寿命。

## 从官方示例还原完整模块图

先让一条任务完整跑一遍：操作者要求机器人前往 `office_map` 中的 `(1.0, 0.0)`；`navigation2DClient` 把目标发给服务端；`robotGotoDev` 保存目标；它的控制线程每 10 ms 询问一次当前位置和激光扫描，根据状态机计算速度；`baseControl` 把笛卡尔速度换成轮速；模拟底盘运动后又产生新里程计。下一周期读取到新的位置，这个闭环继续运行，直到状态变为 `goal_reached`。

项目的组件名虽然多，但每个名字都能放回这条故事：

| 组件 | 角色 | 主要输入 | 主要输出 | 暂时去掉会怎样 |
|---|---|---|---|---|
| `navigation2DClient` | 应用侧代理，把远程服务伪装成本地 C++ 接口 | 应用的目标与状态查询 | 发往 server 的请求 | 应用必须自己理解 Port 和远程协议 |
| `navigation2DServer` | 网络包装层，把远程请求转成 device 接口调用 | client 请求 | 对 `robotGotoDev` 的虚函数调用 | `robotGotoDev` 只能在同一进程内被直接调用 |
| `robotGotoDev` | 局部导航 device，保存任务并拥有控制线程 | 目标、定位、激光 | 底盘笛卡尔速度与导航状态 | 系统没有“朝目标走”的决策者 |
| `baseControl` | 底盘运动学层，把机体速度变成具体轮速 | 线速度、方向、角速度 | 电机命令与里程计 | 导航算法必须知道轮子数量和安装方式 |
| `fakeMotionControl` | 模拟的控制板 device | 轮速命令 | 模拟执行器状态 | 示例需要真实硬件才能运行 |
| `localization2DServer` + `odomLocalizer` | 把里程计整理成统一定位接口 | 底盘里程计 | 地图/机器人位姿 | robotGoto 不知道自己在哪里 |
| `Rangefinder2DWrapper` + `fakeLaser` | 把模拟激光包装成统一测距接口 | 仿真世界与机器人位姿 | 激光距离数组 | 仍可朝目标走，但无法感知局部障碍 |
| `map2DServer` + `transformServer` | 提供地图对象与坐标系关系 | 地图文件、坐标变换 | 地图与 frame 查询 | 多地图、多坐标系的语义无法统一 |

这里的 **device** 是实现某种能力的 C++ 对象，例如定位器或激光；**wrapper** 是协议翻译层，把进程内 device 接口暴露为网络服务，通常不负责导航决策；**client** 是反向代理，把网络服务重新呈现为本地接口。`yarpdev` 则是能够根据配置装载这些 device 插件的通用进程，因此无需为每个设备都重写一个 `main()`。

项目的 `robotGotoExample1` 不只启动一个导航进程，而是组合多个可替换模块。当前提交中的真实 XML 使用 `fakeMotionControl + baseControl` 模拟底盘，而不是把整个底盘模型藏在 robotGoto 内部：

```text
fakeMotionControl <-------------------- baseControl
                                             ^
                                             | /robotGoto/control:o (UDP)
navigation2DServer
  `-- subdevice robotGotoDev ----------------+
           | localization2DClient
           v
localization2DServer -- subdevice odomLocalizer
           ^
           | /baseControl/odometry:o
baseControl+------------------------> fakeLaser location input
                                      |
                                      v
                         Rangefinder2DWrapper

map2DServer + transformServer 提供地图/坐标基础设施
navigation2DClient -> navigation2DServer -> robotGotoDev
```

`fakeMotionControl` 暴露控制板 device，`baseControl` 仍执行底盘运动学和里程计；因此替换真实硬件时，导航控制器不需要改写目标接口。`localization2DServer` 把 `odomLocalizer` 包成定位服务，Rangefinder wrapper 把 fakeLaser 包成测距 device。模块不是按进程数量随意拆分，而是沿设备契约拆分。

部署图还揭示了两种组合方式。`navigation2DServer --subdevice robotGotoDev` 是进程内装饰：server wrapper 和算法 device 在同一 `yarpdev` 进程中，通过 C++ 接口调用；`/robotGoto/control:o -> /baseControl/control:i` 是进程间 Port 连接，通过 UDP carrier 传输。前者避免手写远程协议，后者保留可重连的模块边界。

Carrier 是 YARP 对具体传输方式的称呼，例如 TCP 或 UDP。Port 名描述“逻辑上连接谁”，Carrier 决定“字节具体怎样传”。把两者分开后，同一组模块可以不改业务代码，只在部署时选择更可靠或更低延迟的传输方式。

`robotPathPlanner` 再叠加一层全局规划：它产生 waypoint 序列，由 robotGoto 负责局部跟踪和避障。全局路径与局部速度控制分离后，两者可独立替换，也能分别设置较慢与较快的更新周期。当前案例首先聚焦最小 `robotGoto` 闭环，避免在尚未理解 device/server 边界前同时引入路径规划。

## 客户端通过 INavigation2D 而不是手写 Port

官方 README 推荐应用创建 `navigation2DClient`，再 view 为 `yarp::dev::INavigation2D`。典型 C++ 结构如下：

**教学代码（不是固定提交源码摘录）：**

```cpp
yarp::os::Property options;
options.put("device", "navigation2DClient");
options.put("local", "/mission/navigationClient");
options.put("navigation_server", "/robotPathPlanner");
options.put("map_locations_server", "/map2DServer");

yarp::dev::PolyDriver driver(options);
yarp::dev::Nav2D::INavigation2D* navigation = nullptr;
if (!driver.isValid() || !driver.view(navigation) || navigation == nullptr) {
    return false;
}

yarp::dev::Nav2D::Map2DLocation target;
target.map_id = "office_map";
target.x = 1.0;
target.y = 0.0;
target.theta = 0.0;
if (!navigation->gotoTargetByAbsoluteLocation(target)) {
    return false;
}
```

这段代码值得按执行顺序读，而不是只记 API 名称：

1. `Property` 是一张字符串配置表，先说明要创建哪一种 device，以及本地客户端和远端服务的名字；
2. `PolyDriver driver(options)` 根据 `device=navigation2DClient` 在运行时寻找并创建对应插件；
3. `driver.view(navigation)` 不会再创建一个导航对象，只是向已经打开的 driver 请求一个 `INavigation2D` 接口视图；
4. `Map2DLocation` 把地图名、位置和朝向绑在同一个值对象里，避免只传两个坐标却忘记它们属于哪张地图；
5. `gotoTargetByAbsoluteLocation()` 把目标交给服务端。返回 `true` 只说明请求在接口语义上被接受，不代表机器人已经走到目标。

配置 key 会随具体设备版本变化，部署应以对应版本文档为准；这里展示的是设计结构。`PolyDriver` 是 owner，`navigation` 是从 driver 借出的接口指针，不能比 driver 活得更久。`view()` 成功不转移所有权，关闭时只需要 `driver.close()`，不能 delete 接口指针。

“owner”和“借用指针”是后文所有生命周期分析的基础。owner 负责创建和销毁资源；借用指针只是临时获得使用权。可以把 `PolyDriver` 想成图书馆，`INavigation2D*` 想成借书证：借书证能让你访问馆藏，却不能在图书馆关闭后继续使用，更不能由借书证去拆掉图书馆。

应用依赖 `INavigation2D`，不依赖 robotGoto 的 RPC VOCAB，也不依赖具体移动底盘。client device 内部负责连接多个服务器 Port，把分布式模块组合成一个本地 C++ 接口。

### 官方客户端示例的适用范围

真实 `robotGotoExample/main.cpp` 的 `gotoLoc()` 先循环等待 `navigation_status_idle`，发送目标后又每秒轮询状态、当前位置和当前目标，直到 reached、aborted 或 failing。这很好地展示了接口，却还不是生产任务执行器：两个 `do ... while (1)` 都没有总体 deadline，网络调用失败的返回值也没有逐次进入状态机，主线程在 `Time::delay()` 期间不能处理取消。

把它升级为工程任务客户端时，应明确区分四种结果：

```text
transport failure   : 本次 RPC 没有可靠完成，不能推断服务端是否执行
command rejected    : 服务端收到请求，但参数或状态不允许
command accepted    : 目标已进入导航状态机，并不代表到达
terminal navigation : goal_reached / aborted / failing / client timeout
```

可把 `PolyDriver` 和接口借用封装成一个不可复制 owner。下面是根据接口关系整理的**推荐封装**，不是仓库逐字源码：

**教学代码（不是固定提交源码摘录）：**

```cpp
class NavigationClient {
 public:
  explicit NavigationClient(const Property& options) {
    if (!driver_.open(options) || !driver_.view(api_) || api_ == nullptr) {
      throw std::runtime_error("cannot open navigation2DClient");
    }
  }

  NavigationClient(const NavigationClient&) = delete;
  NavigationClient& operator=(const NavigationClient&) = delete;

  ~NavigationClient() { driver_.close(); }

  INavigation2D& api() { return *api_; }

 private:
  PolyDriver driver_;       // owner 必须先声明
  INavigation2D* api_{};    // 借用视图，不 delete
};
```

这个封装使用了 RAII：对象构造成功就表示资源可用，对象离开作用域便自动清理资源。两行 `= delete` 明确禁止复制，因为两个 `NavigationClient` 若误以为自己都拥有同一个底层连接，就可能重复关闭资源。

成员按声明的逆序析构：先处理后声明的 `api_`，再销毁先声明的 `driver_`。这里 `api_` 只是一个不拥有资源的地址，不需要 `delete`；真正的底层 client 由 `driver_` 关闭。显式 `close()` 便于记录关闭错误，但析构仍要在提前 `return` 或异常时兜底。若应用需要异步取消，则由单独任务状态机定时轮询，不应让后台线程长期保存一个可能先于 driver 失效的裸接口指针。

## 当前实现的三层生命周期

上一节站在应用开发者的位置，只看见一个本地接口。现在沿着这次调用进入服务端，才能解释为什么仓库里同时出现 server、device 和 thread 三类对象。

当前源码不是一个 `RFModule::updateModule()` 包办所有工作，而是三层对象：

```text
yarpdev process
  -> navigation2DServer wrapper
       -> robotGotoDev : DeviceDriver + INavigation2DTargetActions
                         + INavigation2DControlActions
            |-- rpcPort + robotGotoRPCHandler
            `-- gotoThread : PeriodicThread(10 ms)
                  |-- localization2DClient / ILocalization2D*
                  |-- rangefinder2DClient / IRangefinder2D*
                  |-- velocity/status/gui BufferedPorts
                  `-- obstacle handler + navigation state machine
```

外层 `navigation2DServer` 负责把远程 client 协议映射成 `INavigation2D` 调用；`robotGotoDev` 是接口门面和生命周期 owner；`GotoThread` 才是周期控制执行体。理解这三层后，`gotoTargetByAbsoluteLocation()` 的调用链才完整：

```text
user application
  -> navigation2DClient proxy
  -> YARP request/reply
  -> navigation2DServer
  -> robotGotoDev::gotoTargetByAbsoluteLocation
  -> GotoThread::setNewAbsTarget
  -> 下一次 GotoThread::run 计算速度
  -> /robotGoto/control:o
  -> baseControl
```

### `DeviceDriver::open` 是资源事务

源码中的 `robotGotoDev::open(Searchable&)` 先复制并解析配置，然后 `new GotoThread(0.010, p)`、`start()`，最后打开 RPC Port 并注册 reader。`PeriodicThread::start()` 会调用 `threadInit()`；真正的定位 client、激光 client 和输出 Ports 都在那里创建。因此一次看似简单的 `open` 跨越两个对象和一个新线程。

可以把理想的不变量写成：只有当控制线程初始化成功、RPC Port 打开、reader 安装完成时，device 才对外可见；任一步失败都必须逆序释放已经提交的资源。下面是突出事务边界的**教学等价代码**，不是固定提交中的原文：

**教学代码（不是固定提交源码摘录）：**

```cpp
bool RobotGotoDevice::open(Searchable& config) {
  auto thread = std::make_unique<GotoThread>(10ms, config);
  if (!thread->start()) return false;          // threadInit 失败也不提交

  Port rpc;
  if (!rpc.open(name_ + "/rpc")) {
    thread->stop();                            // 撤销已启动线程
    return false;
  }

  rpc_handler_.setInterface(this);
  rpc.setReader(rpc_handler_);
  thread_ = std::move(thread);                 // 最后提交 owner
  rpc_port_ = std::move(rpc);
  return true;
}
```

这是教学上的安全形状，不是仓库逐字代码。固定提交仍使用裸 `GotoThread*`，而且线程启动后若 RPC Port 打开失败，`open()` 直接返回；这一失败分支没有在局部显式停止线程。它说明“主路径能运行”不等于构造事务已经完备，也是复刻时应优先修正的缺点。

所谓“资源事务”，就是把启动过程看成数据库事务：中途任何一步失败，都要撤销前面已经完成的步骤；只有所有步骤成功，才把临时对象提交给成员。`std::unique_ptr` 在这里不仅是为了少写一次 `delete`，更重要的是它把“尚未提交的线程由局部变量负责清理”写进了类型系统。

### `PeriodicThread` 把初始化、周期和析构分开

`GotoThread` 继承 `yarp::os::PeriodicThread`，三个覆写函数分别承担不同责任：

| 回调 | 执行时机 | 当前源码中的职责 |
|---|---|---|
| `threadInit()` | worker 启动前 | 解析配置、打开输出 Port、创建定位/激光 client、取得借用接口 |
| `run()` | 每 10 ms 左右 | 拉取定位与激光、推进状态机、限幅、发布速度与状态 |
| `threadRelease()` | worker 退出后 | 关闭 PolyDriver、interrupt/close Ports、删除 obstacle handler |

这与 `RFModule` 的进程级周期不同：`yarpdev` 可以装载多个 device，而每个内部 device 自己拥有 worker。`run()` 的 10 ms 是期望周期，不是硬实时保证；定位 RPC、激光读取、日志和 Port 写入都会占用这段预算。

### 关闭必须先停止控制线程

当前 `robotGotoDev::close()` 先 interrupt/close RPC Port，再调用 `gotoThread->stop()`，随后 delete。`stop()` 的关键作用是等待 `run()` 离开，之后 `threadRelease()` 才能安全销毁它使用的 Port 和 driver。依赖顺序可以写成：

```text
拒绝新 RPC
  -> 等待正在执行的 reader 返回
  -> stop/join GotoThread
  -> threadRelease 关闭 sensor clients 与 output ports
  -> 删除 GotoThread
  -> device close 完成
```

如果先关闭 `m_pLoc` 或 `m_pLas` 再 stop，控制线程可能正在通过借用的 `m_iLoc`、`m_iLaser` 调用虚函数，形成 use-after-close。`PolyDriver` 是接口实现的 owner，`I*` 指针只是借用视图；这个所有权规律同时适用于客户端示例中的 `INavigation2D*`。

## `ResourceFinder` 与可部署配置

生命周期解决了“对象何时存在”，下一步要解决“这些对象连接到谁”。YARP 没有把所有名字编译进程序，而是让配置文件和命令行共同描述对象图，因此相同二进制可以进入模拟环境或真实机器人部署。

模块不把 Port 名、机器人名、地图路径和周期写死，而是从 context/INI/命令行合并配置。最终解析值应在启动时打印；两个模块的 Port 名约定由部署脚本或 `yarp connect --persist` 建立。

配置驱动提高复用，但字符串错误会推迟到运行时。生产部署应在连接前验证必需 key、范围和文件存在性。

当前 `robotGotoDev` 接收的是 `Searchable&`，随后用 `Property::fromString(config.toString())` 复制成自己的属性树。配置组直接决定源码分支：

| 配置组 | 进入的对象 | 影响 |
|---|---|---|
| `ROBOTGOTO_GENERAL` | `robotGotoDev` 与 `GotoThread` | device 名称、本地 Port 前缀、自动连接 |
| `LOCALIZATION` | `GotoThread` | robot/map frame、远端 localization server |
| `LASER` | `GotoThread` | rangefinder client 的远端 Port |
| `ROBOT_GEOMETRY` | 控制计算与避障器 | 半径、激光相对底盘的位姿 |
| `ROBOT_TRAJECTORY` | 控制状态机 | 全向属性、增益、速度上限、目标容差 |
| `OBSTACLES_*` | 避障器 | 急停、避障与检测距离策略 |
| `RETREAT_OPTION` | preparing 状态 | 导航前是否执行短时退让动作 |

配置不是“若干调参数字”，而是构造对象图的输入。比如缺少 `LASER` 会让 `threadInit()` 失败；错误的 laser pose 不会使 Port 连接失败，却会让避障几何整体偏移。工程上应把配置分成 schema 校验、设备连通性校验和运行时健康校验三层，而不是用一个 `bool open()` 混合所有错误。

## 传感器读取、Streaming Port 与缓冲策略

系统已经启动并完成连接后，控制线程开始重复同一件事：取得最新传感器快照、计算动作、发布结果。这里最容易被忽略的不是控制公式，而是数据是否足够新，以及一次计算究竟混用了哪些时刻的数据。

当前 `GotoThread::run()` 并不直接从定位和激光 `BufferedPort` 读取，而是在每个周期调用：

```text
ILocalization2D::getCurrentPosition -> m_localization_data
IRangefinder2D::getLaserMeasurement -> m_laser_data
```

具体 client device 内部可能仍通过 YARP Port 通信，但控制器依赖的是类型化设备接口。这一层隐藏了 wire protocol，也意味着一次 getter 的时延会进入 10 ms 控制周期。若远端 server 阻塞 30 ms，`PeriodicThread` 无法用接口抽象把它变回 10 ms；需要在 client 层缓存快照、设置超时，或把 I/O 与控制计算拆成两个线程。

定位和速度状态通常只关心最新值，BufferedPort 默认低延迟行为合理；路径事件或任务队列若不能丢，应选择 strict 或应用级确认。慢可视化消费者不应迫使控制输出积压旧速度命令。

每条状态消息需要 envelope timestamp 和 sequence。只看 Port active 无法判断定位是否已经陈旧。

控制循环应把“没有新消息”和“已有消息但过期”分开。可以维护最后接收时间：

**教学代码（不是固定提交源码摘录）：**

```cpp
if (auto* pose = pose_port_.read(false)) {
  latest_pose_ = *pose;
  last_pose_time_ = yarp::os::Time::now();
}
if (yarp::os::Time::now() - last_pose_time_ > pose_timeout_) {
  SendZeroVelocity();
  state_ = State::LocalizationLost;
}
```

`read(false)` 是非阻塞读取，返回的指针属于 BufferedPort 内部缓冲；必须在下一次 read 或 Port 关闭前复制需要的数据。把该指针保存到成员并跨周期使用会依赖未保证的缓冲寿命。

固定提交采用失败次数 `m_loc_timeout_counter` 和 `m_las_timeout_counter`，成功时清零、失败时递增到 `TIMEOUT_MAX`。这种设计成本低，却把实际时间隐式绑定到调度周期：阈值 300 在稳定 10 ms 周期下约为 3 秒，周期超限或动态改频后就不再代表 3 秒。更清晰的状态应保存最后成功的单调时钟时间。下面的 `SensorSnapshot` 是**推荐数据结构**，固定提交并没有这个类型：

**教学代码（不是固定提交源码摘录）：**

```cpp
struct SensorSnapshot {
  Map2DLocation pose;
  std::vector<LaserMeasurementData> scan;
  std::chrono::steady_clock::time_point pose_ok_at;
  std::chrono::steady_clock::time_point scan_ok_at;
  bool pose_valid{};
  bool scan_valid{};
};
```

控制决策读取一次一致快照，不能先用新 pose 计算目标方向、再用上一周期 scan 做避障却不标注时间差。若两种数据不要求严格同步，也要定义允许的最大年龄和最大 skew。

速度命令应带独立 watchdog。即使导航模块崩溃，baseControl 也应在命令超时后输出零速度，不能把“最后一条前进命令”永久保持。

## RPC 作为管理面

传感器和速度属于高频数据面；设置目标、暂停和查询状态属于低频管理面。把两者分开，可以避免一个耗时的管理请求阻塞连续数据，也让“最新一帧可以覆盖旧帧”和“命令不可随便丢弃”拥有不同策略。

启动导航、设置目标、暂停和查询状态适合 RPC。Handler 应快速验证请求，把长任务转成内部状态机，并立即返回接受/拒绝；不要在 InputUnit 线程同步等待整段导航完成。

类型化 Thrift 接口比自由格式 Bottle 更适合稳定命令协议，因为生成代码固定方法和参数类型。Bottle 适合交互调试与兼容命令。

Bottle 是 YARP 的动态值容器，可以像列表一样混装字符串、整数和浮点数，调试时很方便，但字段含义主要靠双方约定。Thrift 则先写接口定义，再生成类型化代码；编译器能够更早发现参数数量或类型不匹配。这里的 InputUnit 是读取并分发输入请求的执行单元，可以把它理解为 RPC 请求进入模块后的处理线程。

项目示例允许向 `/robotGoto/rpc` 发送 `gotoAbs 1.0 0.0`。RPC handler 应把命令解析成内部事件：

```text
RPC thread: validate map/x/y/theta -> enqueue SetGoal -> reply accepted
GotoThread cycle: consume SetGoal -> local controller state transition
```

这样状态机只有一个写线程，避免 RPC callback 与 `run()` 同时改 path、goal 和 controller state。事件队列必须有界；重复 SetGoal 可以覆盖旧目标，而 Stop/Emergency 事件需要保留更高优先级。

### 当前接口调用暴露出的共享状态边界

固定提交中的 `robotGotoDev` 把 `INavigation2D` 方法直接转发给 `GotoThread`。例如目标被装成 `yarp::sig::Vector` 后，`setNewAbsTarget()` 立即改写 `m_target_data`、`m_status` 和 retreat 字段；与此同时 `GotoThread::run()` 正在读取这些字段。源码中的 `m_mutex` 只在 `run()` 开头 `wait()`、末尾 `post()`，这些 setter/getter 并未取得同一信号量。

在 C++ 内存模型中，“写入几个 double 和 enum 很快”不能消除 data race；未同步读写本身就是未定义行为。最小修正有两种：

```text
方案 A：所有 setter/getter 取得同一 mutex
  优点：改动小、语义直观
  代价：RPC 线程可能被完整控制周期阻塞；锁内不能调用远端接口

方案 B：RPC 只写有界 CommandQueue，控制线程独占状态机
  优点：状态只有一个写线程，Stop/SetGoal 可以定义优先级
  代价：命令“接受”与“生效”分离，需要 sequence 和执行结果状态
```

导航任务更适合方案 B。可将事件定义为 `std::variant<SetAbsoluteGoal, SetRelativeGoal, Stop, Pause, Resume>`；RPC 路径只做范围检查和入队，`run()` 在读取传感器前消费事件。队列满时不能静默丢弃 Stop；可以给 Stop 单独的原子闩锁，或使用按优先级覆盖的 mailbox。

`robotGotoRPCHandler` 还提供自由格式 Bottle 命令来修改增益和避障开关，而标准 `INavigation2D` 是类型化接口。两者分别属于诊断/调参与稳定业务 API。若调参字段也会被控制线程读取，同样必须经过同步提交，最好把一组相关增益构造成不可变 `ControllerConfig` 快照后一次替换，避免只更新到一半。

## 控制周期从输入到速度输出

到这里，配置、对象和输入都已经就位，才轮到控制算法。`run()` 每次只完成一个很短的闭环步：读当前世界、决定当前动作、发布这一次动作。机器人是否最终到达目标，是许多个周期共同推进状态机的结果。

`GotoThread::run()` 的逻辑顺序比单个控制公式更重要：

```text
读取定位与激光
  -> 控制输出先清零
  -> 计算目标距离、目标方向 beta、终点姿态误差 gamma
  -> 检测路径障碍
  -> 按 NavigationStatusEnum 推进状态机
  -> 仅在 moving 状态执行速度限幅
  -> 发布控制 Bottle、状态和 GUI 数据
```

这条顺序建立了一个安全基线：每周期先 `m_control_out.zero()`，只有明确状态分支才产生非零速度。它比“沿用上一周期输出再局部修改”更不容易在遗漏分支时继续运动。

### 坐标和角度计算

当前位姿为 `(x_r, y_r, θ_r)`，目标为 `(x_g, y_g, θ_g)`。源码先计算：

```text
distance   = sqrt((x_g - x_r)^2 + (y_g - y_r)^2)
beta_world = atan2(y_g - y_r, x_g - x_r)
beta_robot = normalize(beta_world - θ_r)
gamma      = normalize(θ_g - θ_r)
```

`beta_robot` 决定机器人朝向目标的偏差，`gamma` 只在位置已经进入容差后用于终点朝向。非完整约束底盘在 `|beta_robot|` 过大时先原地旋转；全向底盘则可以把 `beta_robot` 作为平移方向。距离、角度和目标姿态不是同一个误差，混成一个 PID 会掩盖底盘运动学约束。

所谓非完整约束底盘，最直观的例子是汽车：它能向前后行驶和转向，却不能保持车头不变直接横移。麦克纳姆轮等全向底盘可以横移，因此相同目标误差会对应不同控制策略。源码中的分支不是算法风格差异，而是在服从底盘真实的运动能力。

`atan2(dy, dx)` 的作用是根据目标相对位置求出世界坐标系中的方向，而且能正确区分四个象限。随后减去机器人自身朝向，才得到“目标在机器人左边还是右边”。`normalize` 再把角度折回约定范围，例如 `[-180°, 180°)`；否则机器人从 `179°` 转到 `-179°` 时，算法可能误判为还要旋转接近一整圈。

源码角度使用 degree，三角函数输入则必须转换成 radian。`setNewRelTarget()` 明确用 `DEG2RAD` 构造二维旋转，而控制输出中的 `angular_vel` 又以 degree/s 发给 baseControl。这种单位契约没有被 C++ 类型系统编码，复刻时可以引入 `Radians`、`Degrees`、`MetersPerSecond` 的强类型，至少也要让变量名携带 `_deg`、`_rad`。

### 状态机决定完成条件

主要状态及动作如下：

| 状态 | 进入原因 | 周期动作 | 退出条件 |
|---|---|---|---|
| `idle` | 尚无任务或 Stop | 输出保持零 | 收到目标 |
| `preparing_before_move` | 新目标，可选退让 | 按固定方向/速度短时移动 | retreat 时间结束 |
| `moving` | 正常跟踪 | 位置控制、姿态控制、限幅 | 到达、障碍、Pause |
| `waiting_obstacle` | 急停区域有障碍 | 零速度并等待 | 障碍消失或等待超时 |
| `paused` | Pause | 零速度 | 定时恢复或 Resume |
| `goal_reached` | 位置与姿态满足容差 | 零输出、等待新目标 | 新目标或 Stop |
| `failing` | 障碍长期不消失等 | 零输出、报告失败 | 外部重新下发任务 |

`weak_angle` 表示目标没有显式终点朝向；到达位置后可直接完成。这个布尔值实际改变状态机完成条件，因此它不是普通 UI 标记，而应与目标一起在同一次命令提交中原子更新。

### 速度限幅不是简单 clamp

源码只有速度非零时才施加最小速度：正值限制到 `[min, max]`，负值限制到 `[-max, -min]`，零值保持零。这样可以克服电机静摩擦，又不会把“停车”提升成最小运动速度。对应结构是：

**教学代码（不是固定提交源码摘录）：**

```cpp
double SaturateSigned(double value, double min_abs, double max_abs) {
  if (value > 0.0) return std::clamp(value, min_abs, max_abs);
  if (value < 0.0) return std::clamp(value, -max_abs, -min_abs);
  return 0.0;
}
```

但 `min_abs > max_abs`、NaN 或单位错误仍需在配置阶段拒绝。每周期发现负参数再 `fabs` 虽能继续运行，却会把配置故障变成悄悄修正，降低可诊断性。

### Wire 输出和安全边界

`sendOutput()` 构造的控制 Bottle 顺序是：模式 `2`、平移方向、线速度、角速度、最后一个常量字段，并附加 `Stamp` envelope。这个隐式字段顺序是 robotGoto 与 baseControl 之间的协议；没有 schema 时，双方必须同步维护索引、类型与单位。

更关键的是，固定提交只在 `m_status == navigation_status_moving` 时写控制 Port。若当前周期从 moving 转为 `goal_reached` 或 `waiting_obstacle`，本周期算出的零速度不会从该分支发出。系统安全因此依赖 baseControl 对旧速度命令设有 watchdog，或者需要修改为在状态转换时显式发送一次零命令。中间件连接正常不等于运动安全；“最后命令保持多久”必须是端到端契约。

## Device 抽象

`PolyDriver` 根据配置创建具体设备实现，导航模块依赖移动底盘接口而非厂商类。这是运行时 Abstract Factory：同一算法可连接仿真设备或真实底盘。

代价是设备名、接口查询和插件 ABI 都在运行时验证。获取接口失败时 configure 必须回滚已经打开的 Ports 和 driver。

官方模块还展示了 server/client device 组合：server 侧实现 `INavigation2D` 的方法并连接定位、地图和底盘；client device 自动建立远端连接，把 RPC/streaming 协议隐藏在接口后。这是 Proxy + Abstract Factory：调用看似本地虚函数，实际可能跨进程。

虚函数返回成功只表示远端接受或完成了接口定义的动作。长时间导航通常需要继续轮询 navigation status 或订阅状态，而不是把一次 `gotoTarget...()` 的 bool 理解为已经到达。

## 部署与启动顺序

官方示例建议通过 yarpmanager 运行 XML 应用，并逐个启动模块，最后连接 Ports；`yarpviz` 可检查实际拓扑。这个建议反映了动态系统的现实：Name Server 注册、设备初始化和依赖连接不是同一时刻完成。

生产部署不应只靠固定 sleep。更可靠的启动器应等待指定 Port/设备接口可用，执行健康检查，再建立连接；失败时指出缺失依赖而不是让模块进入空转。连接恢复后还要重新验证时间戳，不能立刻使用断线前的旧定位。

## 数据结构与性能边界

当前 robotGoto 是反应式目标跟踪与 APF 风格避障，不是采样 `K` 条候选轨迹的局部规划器。姿态与目标控制为 `O(1)`；激光向量有 `L` 个测量时，障碍检测和势场聚合通常为 `O(L)`，并保存 `O(L)` 的 `LaserMeasurementData`。若接入更复杂的 trajectory rollout，复杂度才可能上升为 `O(L × K)`，不能把这一成本错误归到当前实现。

APF 是人工势场：可以把目标想成吸引机器人、障碍物想成排斥机器人的“虚拟力”。它计算快，但可能陷入局部极小值。trajectory rollout 则一次模拟多条短期候选运动轨迹并评分，通常获得更丰富的运动选择，也会多出与候选数量 `K` 成比例的计算量。`O(1)` 表示工作量不随激光束数增长，`O(L)` 表示激光束翻倍时，这部分工作大致也翻倍。

单周期墙上预算可以写成：

```text
Tcycle = Tlocalization_get + Tlaser_get + O(L) obstacle work
       + O(1) state/control + Tport_write + Tlogging
```

10 ms period 要求正常和高分位 `Tcycle` 都受控；只测平均值会漏掉远端 client 超时和日志抖动。`std::vector<LaserMeasurementData>` 由 client 填充时可能扩容，若要求更稳定的分配行为，可按传感器最大束数预留容量，并确认 API 是否复用调用方存储。

多模块设计增加序列化、调度和跨进程复制，却提供故障隔离和可替换性。将定位、全局规划、局部控制全部塞进一个进程可降低通信开销，但会放大崩溃影响，并使仿真替换和独立调试更困难。

## 设计取舍

YARP 的动态名字和 Carrier 让实验室模块可灵活重连，但拓扑正确性依赖 Name Server 与配置；Bottle 快速迭代却缺少强 schema；每连接执行单元简化隔离却增加线程和 fan-out 成本。

Robotology Navigation 仓库同时包含稳定主模块和标为开发/测试阶段的模块。选择组件时必须区分成熟路径与实验模块，并核对依赖版本；能在 yarpmanager 中启动不等于已经满足真实底盘的安全、实时和故障恢复要求。

这些取舍背后有一条一致思路：Robotology 更看重研究系统中模块可替换、可观察、可在不同进程重新连接，而不是把每一次函数调用都压到最低延迟。对需要频繁替换定位器、传感器和控制算法的实验平台，这种选择很有价值；对资源极小、拓扑固定的嵌入式控制器，它的动态性和序列化成本可能反而多余。

## 缺点与不适用边界

理解一个项目不能只总结“设计得好”的部分。固定提交中的 `robotGoto` 有清晰的模块边界，但也有几项在复刻时必须正面处理的限制：

1. **它是局部目标跟踪器，不是完整自主导航栈。** 模块能够朝目标运动并做反应式避障，却不等于拥有复杂地图上的全局最优路径、动态重规划和交通规则。需要绕过大范围障碍时，应在上层加入 `robotPathPlanner` 或其他规划器。
2. **共享状态的同步边界不够严格。** RPC setter 与控制线程可能同时读写目标和状态。实验环境中“多数时候能跑”不能消除 C++ data race；更稳妥的实现应使用单写者状态机和命令 mailbox。
3. **运动安全依赖下游。** 非 moving 状态并不总能保证当前周期显式发送零速度，因此 baseControl 的命令超时保护是系统级必需条件，而不是可选优化。
4. **周期线程不是硬实时线程。** 远端 getter、日志、内存分配和网络抖动都可能让 10 ms 周期超限。需要硬实时保证的最内层电机控制不应直接依赖这条普通网络路径。
5. **字符串配置和 Bottle 协议把部分错误推迟到运行时。** 拼错 Port 名、交换字段顺序或混用角度单位，都可能通过编译。部署前需要 schema、连通性和单位检查。
6. **动态分布式结构有固定成本。** 多进程隔离带来序列化、复制、线程调度和运维复杂度。若系统只有一个传感器、一个固定底盘且从不替换算法，更小的静态进程内结构可能更合适。

因此，`robotGoto` 适合教学、研究验证、实验室移动机器人以及需要替换设备实现的原型系统。若直接用于高速平台、人员密集环境或具备认证要求的工业车辆，还需要独立安全控制器、确定性通信、冗余状态估计和经过验证的停车链路。

## 可迁移的设计方法

离开 YARP 的具体类名后，这个案例仍留下七条可以带到其他机器人中间件中的方法：

1. **先画数据寿命，再选通信形式。** 高频状态允许覆盖旧值，任务命令需要确认，硬件能力适合稳定接口；不要因为同一个中间件能传所有数据，就把所有数据做成同一种消息。
2. **让业务算法依赖能力接口，而不是厂商驱动。** `INavigation2D`、`ILocalization2D` 和 `IRangefinder2D` 把“能做什么”放在“由谁实现”之前，使模拟器与真实设备能够替换。
3. **把启动写成可回滚事务。** 所有局部资源成功后再提交给长期成员，失败时按逆序撤销，避免半启动对象继续对外可见。
4. **让周期线程成为状态的唯一写者。** 其他线程只提交不可变命令；这样比给每个字段零散加锁更容易推导状态机。
5. **把时间写进数据契约。** 保存采样时间、接收时间和序号，用实际 age 判断超时，而不是只靠“连续失败多少个周期”。
6. **把零速度视为消息，而不是没有消息。** 停车必须显式、可确认，并由下游 watchdog 在发布者失联时兜底。
7. **先纯计算，后接中间件。** 控制核心输入快照并返回动作，不直接打开 Port；adapter 才负责 I/O、线程和时间。这样核心行为更容易阅读，也更容易迁移到 ROS 2、Cyber RT 或自研总线。

## 可复用模块骨架

把上面的原则落到 C++ 对象，可以先得到下面这组边界：

```text
NavigationDevice            对外实现 INavigation2D，拥有全部资源
  ├─ CommandMailbox         接收 SetGoal / Stop / Pause / Resume
  ├─ SensorAdapter          从 YARP device 接口生成带时间的快照
  ├─ GotoWorker             唯一周期线程，推进状态机
  ├─ ControllerCore         纯计算：snapshot + goal -> control output
  └─ VelocityPublisher      发布速度和强制零速
```

它们的所有权可以直接写进类型：

**教学代码（不是固定提交源码摘录）：**

```cpp
class NavigationDevice final : public DeviceDriver,
                               public INavigation2DTargetActions,
                               public INavigation2DControlActions {
 public:
  bool open(Searchable& config) override;
  bool close() override;

 private:
  std::unique_ptr<GotoWorker> worker_;  // 独占并负责 stop/join
  PolyDriver localization_driver_;      // 拥有定位 client
  ILocalization2D* localization_{};      // 借用，不 delete
  PolyDriver laser_driver_;             // 拥有激光 client
  IRangefinder2D* laser_{};              // 借用，不 delete
  CommandMailbox commands_;             // device 与 worker 的同步边界
};
```

`std::unique_ptr<GotoWorker>` 表达“恰好一个 device 拥有 worker”；两个 `I*` 裸指针在这里有意表达非拥有关系。裸指针本身不危险，危险的是没有写清谁保证它的寿命。成员顺序也不是排版问题：关闭时应先停止使用借用接口的 `worker_`，再关闭拥有实现对象的两个 `PolyDriver`。

最小数据模型也应把命令、传感器和输出分开：

**教学代码（不是固定提交源码摘录）：**

```cpp
struct SetGoal { Map2DLocation goal; std::uint64_t command_id; };
struct Stop {};
struct Pause {};
struct Resume {};
using Command = std::variant<SetGoal, Stop, Pause, Resume>;

struct ControlInput {
  SensorSnapshot sensors;
  std::optional<Command> command;
  std::chrono::steady_clock::time_point now;
};

struct ControlOutput {
  double linear_mps{};
  double direction_deg{};
  double angular_degps{};
  NavigationStatusEnum status{navigation_status_idle};
  bool publish_zero_now{};
};
```

`std::variant` 表示命令在任一时刻只可能是列出的某一种类型，访问时必须显式处理各分支；它比一个包含许多可选字段的“大命令结构体”更难形成非法组合。`std::optional<Command>` 表示这一周期可以没有新命令，不需要再约定某个魔法整数代表“无事件”。字段名携带单位，则是在没有强单位类型时最低成本的防错措施。

## 最小复刻：从纯控制核心组装导航闭环

新算法最好保留 `INavigation2DTargetActions` 与 `INavigation2DControlActions` 契约，只替换内部 planner/controller。当前最小模拟闭环是 `fakeMotionControl → baseControl → odomLocalizer/fakeLaser → robotGotoDev`，而不是旧版文档里的单一 fakeMobileBaseTest。

从空类开始可按下面的依赖顺序实现：

1. 先写纯 `ControllerCore`：输入 pose、scan snapshot、goal 和 config，输出 `ControlOutput + next state`，不包含 Port 或 `Time::now()`。
2. 写 `GotoWorker : PeriodicThread`，只负责采集接口快照、消费命令 mailbox、调用 core、发布带时间戳的输出。
3. 写 `NavigationDevice : DeviceDriver + INavigation2D*Actions`，把 API 请求验证后变成事件，不直接修改 core 状态。
4. 用 `yarp_prepare_plugin` 注册 device，让 `navigation2DServer --subdevice` 装载；此时 server/client wire 协议由 YARP wrapper 提供。
5. 在 XML 中部署 fakeMotionControl、baseControl、定位和激光 wrapper，显式连接速度与里程计 Port。
6. 最后替换真实 controlboard、localizer 与 rangefinder 配置，算法和客户端代码保持不变。

第一步的纯核心不需要知道 YARP。它只接收一份值类型输入，因此可以写成普通 C++ 类：

**教学代码（不是固定提交源码摘录）：**

```cpp
class ControllerCore {
 public:
  explicit ControllerCore(ControllerConfig config)
      : config_(std::move(config)) {}

  ControlOutput step(const ControlInput& input) {
    applyCommand(input.command);
    if (!fresh(input.sensors, input.now)) {
      return stopped(NavigationStatusEnum::navigation_status_failing);
    }
    return advanceStateMachine(input.sensors, input.now);
  }

 private:
  ControllerConfig config_;
  GoalState goal_;
  NavigationStatusEnum state_{navigation_status_idle};
};
```

`const ControlInput&` 表示函数只借用本周期输入且不修改它；`config_`、`goal_` 和 `state_` 是跨周期保留的核心状态。`step()` 先消费命令，再检查数据新鲜度，最后推进状态机。无论以后使用 YARP Port、ROS 2 subscription 还是共享内存，纯核心都不需要变化。

第二步才让 worker 连接中间件：

**教学代码（不是固定提交源码摘录）：**

```cpp
void GotoWorker::run() {
  const auto now = std::chrono::steady_clock::now();
  SensorSnapshot snapshot = sensors_.readLatest(now);
  std::optional<Command> command = commands_.takeNext();
  ControlOutput output = core_.step({std::move(snapshot),
                                     std::move(command), now});
  velocity_.publish(output);  // publish_zero_now 也必须形成显式输出
}
```

局部变量 `snapshot` 保证本周期使用同一份传感器视图；`std::move` 表示允许把向量等内部存储转交给下一层，减少不必要复制，但移动后的局部对象不能再假定保留原值。middleware adapter 管理线程、时间和 I/O，core 只负责可推导的状态转换。

验证至少覆盖定位超时、激光断流、目标被替换、Stop 抢占、Port 重连、地图坐标系不一致和 close 期间仍有 RPC。完成标准是算法可在 fake 与真实 device 间只换配置，外部 client 不修改代码。

完成后的最小系统应能回答六个具体问题：目标由谁拥有、命令何时生效、传感器多旧就必须停车、哪个线程能够修改状态、零速度如何抵达底盘、关闭时谁先退出。能从代码中无歧义地回答这些问题，才算真正复刻了架构；仅仅让机器人在空旷场地向前移动，还没有复刻它的工程边界。
