from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"
ARTICLES = CONTENT / "articles"
GUIDES = CONTENT / "guides"
DOCS = ROOT / "docs"
GENERATED = DOCS / "generated"


def write_if_changed(path: Path, text: str) -> None:
    """Keep unchanged generated file mtimes stable for incremental builds."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

PROJECTS = {
    "cyber": ("Apollo Cyber RT", "d53aa3da47a06a08e6d0cd175d5623a34fa0d6aa"),
    "orocos": ("Orocos RTT", "600102e8be9c81905b20930e32d43b28244ab173"),
    "yarp": ("YARP", "91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9"),
    "ecal": ("Eclipse eCAL", "1ec0ea2fe5e5e61e3e492be6128c27cc6026d717"),
    "zenoh": ("Eclipse Zenoh", "9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5"),
    "lcm": ("LCM", "ad0c54cee0ec048ef12357c34349ec1443158864"),
}

PROJECT_OVERVIEWS = {
    "cyber": """Cyber RT 是 Apollo 面向车载计算图的运行时：DAG 装载组件，Node 创建 Reader/Writer，Transport 接入进程内、共享内存与 RTPS 通道，DataVisitor 把消息缓存转换为可调度事件，CRoutine 与 Scheduler 再决定业务代码何时获得 CPU。

理解它不能停在“有 Component 和协程”这一层。真正的主线是一条消息怎样跨过 transport callback、Dispatcher、有限缓存、Notifier 和 Processor，最后进入 ``Component::Proc()``；每一次复制、锁竞争和非抢占执行都会落到感知—规划—控制链路的延迟预算里。""",
    "ecal": """eCAL 是发现驱动的高性能发布订阅中间件。稳定的 Publisher/Subscriber 门面背后，注册信息负责让端点相遇，PubGate/SubGate 管理匹配关系，SHM、UDP 与 TCP writer/reader 根据本机性、配置和订阅者能力组成实际数据路径。

它的关键不只是“支持多种传输”，而是同一次 Send 如何准备 payload、选择并驱动多条 writer，接收端又怎样把共享内存视图或网络报文交给回调。buffer rotation、确认等待、fragment、TCP 队列和注册过期共同决定慢消费者会阻塞、丢旧还是造成更长的数据年龄。""",
    "zenoh": """Zenoh 把发布订阅、查询和分布式数据空间统一在 key expression 之上。Session 是应用入口，Primitives 隔离会话与路由，Face 表示一侧连接关系，resource tree 与 route cache 把表达式匹配结果变成可复用的数据路径，transport pipeline 再完成 batch、frame 和 fragment。

因此它不能只被理解成另一套 topic pub/sub。要读懂一次 put 或 get，必须同时理解 key expression 的集合语义、声明如何改变路由表、消息如何在多个 Face 间改写 WireExpr，以及拥塞控制和可靠性如何沿异步任务与有限通道传播。""",
    "lcm": """LCM 用很小的 C 运行时完成低延迟消息分发。顶层 ``lcm_t`` 保存 provider vtable、订阅关系和 handle 状态；UDPM provider 用 LC02/LC03 报文、多播 socket、接收线程、重组表、有限 ring 与通知 pipe，把网络接收和用户 callback 分到两个执行上下文。

它的价值在于机制少而边界清楚：publish 可以一直追到 ``sendmsg/iovec``，receive 可以一直追到 fragment reassembly 与锁外 callback。相应代价也直接可见——多播没有端到端可靠性，分片放大丢包概率，队列容量和主线程调用 ``handle`` 的节奏决定数据是否及时。""",
    "orocos": """Orocos RTT 围绕实时组件建立明确的执行边界。TaskContext 暴露生命周期 hooks、Operation 和 Port；Activity 提供线程与周期，ExecutionEngine 统一处理消息、端口事件和组件更新；ConnPolicy 决定数据连接使用最新值还是有界缓冲，以及采用何种同步策略。

它最值得追踪的问题是“谁的线程执行这段代码”。ClientThread 与 OwnThread operation、周期与事件驱动 Activity、DATA 与 BUFFER policy 会给出完全不同的阻塞和数据年龄语义。所谓实时性最终取决于容器进度保证、hook 的最坏执行时间、OS priority/affinity 与关闭顺序能否形成闭环。""",
    "yarp": """YARP 把机器人网络抽象成 Port。PortCore 管理输入输出连接和生命周期，OutputUnit/InputUnit 把每条连接的执行状态隔离开，Protocol 与 Carrier 则把握手、framing、确认和具体传输协议从端口 API 中剥离。Name Server 把逻辑名字解析成可连接的 Contact。

阅读 YARP 的主线是一次 ``Port::write`` 如何序列化并扇出到多条连接，以及对端怎样经 InputUnit 进入 PortReader。同步写、后台写、不同 Carrier、慢连接与断开竞态会改变 buffer 所有权和调用者阻塞时间，也决定它更适合可靠数据流还是只关心最新状态的控制链路。""",
}

PROJECT_EXTRAS: dict[str, list[str]] = {}

ARTICLE_ORDER = {
    "cyber": [
        "overview",
        "architecture-map",
        "dag-to-component",
        "node-reader-writer",
        "pending-queue-ring",
        "dispatcher-notifier",
        "multi-input-fusion",
        "croutine-wakeup",
        "processor-context-switch",
        "message-to-proc",
        "class-loader-abi",
        "cpp-type-runtime",
        "cpp-implementation-lab",
        "design-recap",
    ],
    "lcm": [
        "overview",
        "foundations",
        "architecture-map",
        "provider-vtable",
        "udpm-publish-protocol",
        "receive-reassembly",
        "subscription-dispatch",
        "types-and-eventlog",
        "c-abi-cpp-design-lab",
        "design-recap",
    ],
    "ecal": [
        "foundations",
        "architecture-map",
        "registration-soft-state",
        "publisher-discovery-send",
        "shm-memory-protocol",
        "subscriber-delivery",
        "global-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
    "zenoh": [
        "foundations",
        "architecture-map",
        "session-runtime",
        "publisher-routing",
        "query-lifecycle",
        "resource-route-cache",
        "backpressure-close",
        "rust-cpp-design-lab",
        "design-recap",
    ],
    "orocos": [
        "foundations",
        "architecture-map",
        "taskcontext-lifecycle",
        "activity-execution-engine",
        "operation-threading",
        "ports-channels",
        "realtime-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
    "yarp": [
        "foundations",
        "architecture-map",
        "portcore-architecture",
        "write-fanout",
        "protocol-carrier",
        "read-rpc",
        "close-lifecycle",
        "cpp-design-lab",
        "design-recap",
    ],
}

CYBER_ARTICLE_SECTIONS = [
    ('从空目录建立系统全貌', ['architecture-map']),
    ('第一条消息之前：装配与通信端点',
     ['dag-to-component', 'node-reader-writer']),
    ('数据面：有界缓存、分发与输入组合',
     ['pending-queue-ring', 'dispatcher-notifier', 'multi-input-fusion']),
    ('执行面：从数据通知到真正获得 CPU',
     ['croutine-wakeup', 'processor-context-switch']),
    ('把整条消息链重新跑一遍', ['message-to-proc']),
    ('生命周期、ABI 与 C++ 机制',
     ['class-loader-abi', 'cpp-type-runtime']),
    ('从源码原则到最小实现',
     ['cpp-implementation-lab', 'design-recap']),
]

GUIDE_ORDER: dict[str, list[str]] = {
    "cyber": ["use-environment", "use-pubsub", "closed-loop-project", "use-component-operations", "case-study-apollo-planning"],
    "ecal": ["use-environment", "use-pubsub", "use-operations", "case-study-mqtt-bridge"],
    "zenoh": ["use-environment", "use-pubsub-query", "use-operations", "case-study-rmw-zenoh"],
    "lcm": ["use-environment", "use-pubsub-types", "closed-loop-project", "use-operations", "case-study-drake"],
    "orocos": ["use-environment", "use-component-ports", "use-operations", "case-study-rtt-ros"],
    "yarp": ["use-environment", "use-ports-rpc", "use-operations", "case-study-icub-navigation"],
}

PROJECT_STORIES = {
    "cyber": """从 :doc:`总览 <overview>` 中的一帧消息开始，先分清 Node、Reader/Writer、Component 和 Processor 分别属于通信、业务与执行哪一层。随后沿 :doc:`DAG 装配 <dag-to-component>`、:doc:`通信端点 <node-reader-writer>`、:doc:`数据分发 <dispatcher-notifier>` 和 :doc:`调度唤醒 <croutine-wakeup>` 追踪同一条消息如何从 transport 走到 Proc。

读完基础 API 后进入 :doc:`端到端闭环工程 <closed-loop-project>`：把 Proto、Bazel、Publisher、DAG Component、输出 Writer 与严格 Observer 放在同一条链里，再回到 :doc:`最小 C++ Runtime <cpp-implementation-lab>` 亲自编译 Node/Reader/Writer 的缩小版。最后用 :doc:`Apollo Planning 案例 <case-study-apollo-planning>` 检查这些机制如何进入真实规划流水线。""",
    "ecal": """把问题收敛到一次相机消息：先在 :doc:`入门 <foundations>` 中分清发布端与订阅端，再读 :doc:`注册和软状态 <registration-soft-state>`，理解它们如何发现彼此。只有端点已经匹配，:doc:`Publisher 发送 <publisher-discovery-send>` 中的 SHM、UDP 和 TCP 选择才有实际意义。

接着追 :doc:`共享内存与消息寿命 <shm-memory-protocol>`：同一帧何时被复制、哪一个槽位能被覆盖，为什么 zero-copy 不等于无条件零拷贝；最后沿 :doc:`Subscriber 交付 <subscriber-delivery>` 把网络线程与业务回调分开。写自己的版本之前，带着这些状态关系进入 :doc:`C++ 实现实验 <cpp-design-lab>` 和 :doc:`MQTT Bridge 案例 <case-study-mqtt-bridge>`。""",
    "lcm": """从 :doc:`机械臂消息总线的最小问题 <overview>` 开始，只保留“给出 channel 和一串字节”的 API。随后进入 :doc:`Provider 设计 <provider-vtable>`：先亲手写一个会失控的 switch，再理解 C 函数指针如何隔离传输实现。

再沿 :doc:`UDP 发送 <udpm-publish-protocol>`、:doc:`接收与分片重组 <receive-reassembly>` 和 :doc:`订阅分发 <subscription-dispatch>` 追踪同一条消息，找出谁在收包、谁在调用用户代码，以及取消订阅为何需要延迟回收。读完 typed pub/sub 后进入 :doc:`双进程闭环工程 <closed-loop-project>`，把 schema、CMake、sender、receiver 和退出验收逐文件连起来；最后用 :doc:`C ABI 与 C++ 实验 <c-abi-cpp-design-lab>` 将设计压缩到可写、可测试的最小系统，再看 :doc:`Drake 集成 <case-study-drake>`。""",
    "orocos": """先从 :doc:`1 ms 控制循环的失败 <foundations>` 出发：把设备、命令与日志塞进同一个线程为什么不够；然后沿 :doc:`TaskContext 生命周期 <taskcontext-lifecycle>` 给配置、启动、异常和释放划边界。

接着进入 :doc:`Activity 与 ExecutionEngine <activity-execution-engine>`，区分“任务可以运行”和“哪个 OS 线程真正执行”；再读 :doc:`Port 与 Channel <ports-channels>` 及 :doc:`Operation 线程模型 <operation-threading>`，理解样本与控制命令的不同时间语义。最后通过 :doc:`C++ 实验 <cpp-design-lab>` 验证对象寿命、虚接口和并发关闭，再对照 :doc:`RTT/ROS 集成 <case-study-rtt-ros>`。""",
    "yarp": """从 :doc:`为何需要带名字的 Port <foundations>` 开始：应用不应知道远端 socket 的每一个细节。进入 :doc:`PortCore 架构 <portcore-architecture>` 以后，先辨别 Port、连接 Unit、Protocol 和 Carrier 的所有权关系，再在 :doc:`Carrier 协议 <protocol-carrier>` 中追握手与 framing。

一份消息怎样送往多条连接，读 :doc:`写入与扇出 <write-fanout>`；回调与 RPC 怎样在对端发生，读 :doc:`读取与 RPC <read-rpc>`；为什么异步发送不能把 Writer 栈地址长期借出，继续读 :doc:`关闭与生命周期 <close-lifecycle>`。把虚接口、RAII、模板与异步对象关系映射回 :doc:`C++ 设计实验 <cpp-design-lab>`，最后进入 :doc:`iCub 案例 <case-study-icub-navigation>`。""",
    "zenoh": """先在 :doc:`Key Expression 入门 <foundations>` 中理解数据集合与单条消息，再沿 :doc:`Session 的建立 <session-runtime>` 追踪 API、内部实体和 Runtime；随后在 :doc:`Resource 与路由缓存 <resource-route-cache>` 中解释为什么通配匹配不应每帧重算。

有了对象与路由模型，再走一次 :doc:`Publisher 到 Transport <publisher-routing>`，随后进入 :doc:`Query、Reply 和 Final <query-lifecycle>`，亲自模拟两路回复、一条超时和最后的回收。完成 :doc:`背压与关闭 <backpressure-close>` 的故障回放以后，再读 :doc:`Rust/C++ 设计实验 <rust-cpp-design-lab>` 以及 :doc:`rmw_zenoh 案例 <case-study-rmw-zenoh>`。""",
}

INTERNAL_ONLY_SLUGS = {"reconstruction"}

PAGE_RE = re.compile(r"<!--\s*PAGE:\s*([^\s]+)\s*-->")
ADMONITIONS = {
    "NOTE": "note",
    "WARNING": "warning",
    "DANGER": "danger",
    "ANALYSIS": "important",
    "SOURCE": "seealso",
}


def convert_alerts(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = re.match(r"^>\s*\[!(\w+)\]\s*(.*)$", lines[i])
        if not match:
            out.append(lines[i])
            i += 1
            continue
        kind = ADMONITIONS.get(match.group(1).upper(), "note")
        body = [match.group(2)] if match.group(2) else []
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            body.append(re.sub(r"^>\s?", "", lines[i]))
            i += 1
        out.extend([f":::{kind}", *body, ":::"])
    return "\n".join(out)


def normalize_page(block: str) -> tuple[str, str]:
    block = convert_alerts(block.strip())
    title_match = re.search(r"^#\s+(.+)$", block, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled"
    lines = block.splitlines()
    normalized: list[str] = []
    inserted_contents = False
    for line in lines:
        meta = re.match(r"^(Goal|Tutorial level|Time|Source|Commit):\s*(.+)$", line)
        if meta:
            key, value = meta.groups()
            if key == "Source":
                normalized.append(f"**源码入口：** {value}")
            elif key == "Commit":
                normalized.append(f"**固定版本：** {value}")
        else:
            normalized.append(line)
    if not inserted_contents:
        normalized[1:1] = ["", "```{contents} 本页目录", ":depth: 2", ":local:", "```", ""]
    return title, "\n".join(normalized).rstrip() + "\n"


def rewrite_generated_project_links(page: str, project: str) -> str:
    """Keep source-relative links valid after articles/guides share one folder.

    content/articles/<project> and content/guides/<project> both become
    docs/generated/<project>. Rewrite only explicit same-project crosslinks.
    """
    pattern = re.compile(
        r"\]\(\.\./\.\./(?:articles|guides)/([^/]+)/"
        r"([^/)#]+\.md)(#[^)]*)?\)"
    )

    def replace(match: re.Match[str]) -> str:
        if match.group(1) != project:
            return match.group(0)
        return "](" + match.group(2) + (match.group(3) or "") + ")"

    return pattern.sub(replace, page)



def cyber_course_navigation() -> str:
    blocks = ["""先认识组件：消息触发与周期触发
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. toctree::
   :maxdepth: 2

   overview
"""]
    for title, slugs in CYBER_ARTICLE_SECTIONS:
        entries = '\n'.join(f'   {slug}' for slug in slugs)
        underline = '~' * 80
        blocks.append(
            f'''{title}
{underline}

.. toctree::
   :maxdepth: 2

{entries}
'''
        )
    return '\n'.join(blocks)


def cyber_learning_path() -> str:
    return '''
怎样使用这套课程
----------------

第一次阅读，可以先用短篇组件入门区分消息触发与周期触发，再进入架构骨架画出对象、线程、缓存和依赖关系，之后沿“装配与端点 → 有界缓存 → 数据分发 → 多输入融合 → 任务唤醒 → Processor 执行 → 完整消息链回放”连续前进。带源码深读时，每到一个对象都记录 owner、所在执行上下文、锁域、消息复制和关闭动作。动手复刻时，则先实现有界 ring 与单 worker，再逐步加入类型擦除、事件闩锁、协程和配置装配；不要从完整框架接口倒推一个不可运行的玩具。

源码章节分别深入端点创建、消息主链、数据面、任务状态和 Processor 执行，语言设计与最小复刻放在最后。简明入口与源码长文各自回答不同问题，不需要在多篇文章中反复重讲同一组基础术语。
'''


def write_project_index(
    project: str,
    pages: list[tuple[str, str]],
    guides: list[tuple[str, str]],
) -> None:
    if project == 'cyber':
        name, commit = PROJECTS[project]
        folder = GENERATED / project
        folder.mkdir(parents=True, exist_ok=True)
        guide_entries = '\n'.join(f'   {slug}' for slug, _ in guides)
        guide_navigation = f'''
实际开发与生产案例
------------------

.. toctree::
   :maxdepth: 2

{guide_entries}
'''
        text = f'''{name}
{'=' * len(name)}

固定源码版本：``{commit}``

{PROJECT_OVERVIEWS[project]}

本专题从机器人部署里的实际延迟和丢帧问题开始，再沿缓存、通知、调度和对象寿命逐步深入。没有明确称为固定源码的短代码是帮助推导的教学示例；文字图只是为讨论运行时关系服务。

{cyber_learning_path()}

源码工程课程
------------

{cyber_course_navigation()}

{guide_navigation}
'''
        write_if_changed(folder / 'index.rst', text)
        return

    name, commit = PROJECTS[project]
    folder = GENERATED / project
    folder.mkdir(parents=True, exist_ok=True)
    underline = "=" * len(name)
    source_slugs = [slug for slug, _ in pages] + PROJECT_EXTRAS.get(project, [])
    source_entries = "\n".join(f"   {slug}" for slug in source_slugs)
    guide_entries = "\n".join(f"   {slug}" for slug, _ in guides)
    source_navigation = ""
    if source_entries:
        source_navigation = f"""
源码解读
--------

.. toctree::
   :maxdepth: 2

{source_entries}
"""
    guide_navigation = ""
    if guide_entries:
        guide_navigation = f"""
使用教程
--------

.. toctree::
   :maxdepth: 2

{guide_entries}
"""
    if project == 'cyber':
        source_navigation = cyber_course_navigation()
    learning_path = f"""
从一个具体故障开始
------------------

{PROJECT_STORIES[project]}

这些文章中的短代码用于一步步推导机制；只有上下文明确说明取自固定上游版本时，才是项目原始源码。文字图呈现概念或运行时关系，而不是从源码自动生成的类图。
"""
    text = f"""{name}
{underline}

固定源码版本：``{commit}``

{PROJECT_OVERVIEWS[project]}

阅读源码时从 public entry 出发，沿真实调用链标出对象所有权、线程切换、队列、锁和数据复制，再回到关闭路径检查在途工作如何收束。
{learning_path}
{source_navigation}
{guide_navigation}
"""
    write_if_changed(folder / "index.rst", text)


def main() -> None:
    GENERATED.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[tuple[str, str]]] = {key: [] for key in PROJECTS}
    guide_grouped: dict[str, list[tuple[str, str]]] = {key: [] for key in PROJECTS}
    for path in sorted(CONTENT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        matches = list(PAGE_RE.finditer(text))
        for index, match in enumerate(matches):
            route = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title, page = normalize_page(text[match.end():end])
            if "/" in route:
                project, slug = route.split("/", 1)
                if project not in PROJECTS:
                    continue
                if slug in INTERNAL_ONLY_SLUGS:
                    continue
                folder = GENERATED / project
                folder.mkdir(parents=True, exist_ok=True)
                write_if_changed(folder / f"{slug}.md", page)
                grouped[project].append((slug, title))

    # New long-form articles use one source file per page.  Keeping them under
    # content/articles makes the authoring source unambiguous while generated/
    # remains disposable build input.
    for project in PROJECTS:
        article_dir = ARTICLES / project
        if not article_dir.is_dir():
            continue
        order = {slug: index for index, slug in enumerate(ARTICLE_ORDER.get(project, []))}
        paths = sorted(
            article_dir.glob("*.md"),
            key=lambda item: (order.get(item.stem, len(order)), item.stem),
        )
        for path in paths:
            slug = path.stem
            title, page = normalize_page(path.read_text(encoding="utf-8"))
            page = rewrite_generated_project_links(page, project)
            folder = GENERATED / project
            folder.mkdir(parents=True, exist_ok=True)
            write_if_changed(folder / f"{slug}.md", page)
            grouped[project].append((slug, title))

    # Practical guides are kept separate from source analysis while sharing
    # the same project landing page and navigation tree.
    for project in PROJECTS:
        guide_dir = GUIDES / project
        if not guide_dir.is_dir():
            continue
        order = {slug: index for index, slug in enumerate(GUIDE_ORDER.get(project, []))}
        paths = sorted(
            guide_dir.glob("*.md"),
            key=lambda item: (order.get(item.stem, len(order)), item.stem),
        )
        for path in paths:
            slug = path.stem
            title, page = normalize_page(path.read_text(encoding="utf-8"))
            page = rewrite_generated_project_links(page, project)
            folder = GENERATED / project
            folder.mkdir(parents=True, exist_ok=True)
            write_if_changed(folder / f"{slug}.md", page)
            guide_grouped[project].append((slug, title))

    for project, pages in grouped.items():
        write_project_index(project, pages, guide_grouped[project])

    source_total = sum(len(pages) for pages in grouped.values())
    guide_total = sum(len(pages) for pages in guide_grouped.values())
    print(f"generated {source_total} source pages and {guide_total} guide pages")


if __name__ == "__main__":
    main()
