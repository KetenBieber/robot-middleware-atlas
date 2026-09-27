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
        "architecture-map",
        "provider-vtable",
        "udpm-publish-protocol",
        "receive-reassembly",
        "subscription-dispatch",
        "types-and-eventlog",
        "foundations",
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
    "cyber": ["use-environment", "use-pubsub", "use-component-operations", "case-study-apollo-planning"],
    "ecal": ["use-environment", "use-pubsub", "use-operations", "case-study-mqtt-bridge"],
    "zenoh": ["use-environment", "use-pubsub-query", "use-operations", "case-study-rmw-zenoh"],
    "lcm": ["use-environment", "use-pubsub-types", "use-operations", "case-study-drake"],
    "orocos": ["use-environment", "use-component-ports", "use-operations", "case-study-rtt-ros"],
    "yarp": ["use-environment", "use-ports-rpc", "use-operations", "case-study-icub-navigation"],
}

LEARNING_PATHS = {
    "cyber": {
        "intro": "overview",
        "map": "architecture-map",
        "source": "dag-to-component",
        "language": "cpp-type-runtime",
        "recap": "design-recap",
        "rebuild": "cpp-implementation-lab",
        "guide": "use-environment",
        "case": "case-study-apollo-planning",
    },
    "ecal": {
        "intro": "foundations",
        "map": "architecture-map",
        "source": "registration-soft-state",
        "language": "cpp-design-lab",
        "recap": "design-recap",
        "rebuild": "cpp-design-lab",
        "guide": "use-environment",
        "case": "case-study-mqtt-bridge",
    },
    "zenoh": {
        "intro": "foundations",
        "map": "architecture-map",
        "source": "session-runtime",
        "language": "rust-cpp-design-lab",
        "recap": "design-recap",
        "rebuild": "rust-cpp-design-lab",
        "guide": "use-environment",
        "case": "case-study-rmw-zenoh",
    },
    "lcm": {
        "intro": "overview",
        "map": "architecture-map",
        "source": "provider-vtable",
        "language": "foundations",
        "recap": "design-recap",
        "rebuild": "c-abi-cpp-design-lab",
        "guide": "use-environment",
        "case": "case-study-drake",
    },
    "orocos": {
        "intro": "foundations",
        "map": "architecture-map",
        "source": "taskcontext-lifecycle",
        "language": "cpp-design-lab",
        "recap": "design-recap",
        "rebuild": "cpp-design-lab",
        "guide": "use-environment",
        "case": "case-study-rtt-ros",
    },
    "yarp": {
        "intro": "foundations",
        "map": "architecture-map",
        "source": "portcore-architecture",
        "language": "cpp-design-lab",
        "recap": "design-recap",
        "rebuild": "cpp-design-lab",
        "guide": "use-environment",
        "case": "case-study-icub-navigation",
    },
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
        normalized[1:1] = ["", "```{contents} Contents", ":depth: 2", ":local:", "```", ""]
    return title, "\n".join(normalized).rstrip() + "\n"


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

阅读时从 public entry 出发，沿真实调用链标出对象所有权、线程切换、队列、锁和数据复制，再从关闭路径反向验证在途工作怎样收束。

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
    path = LEARNING_PATHS[project]
    learning_path = f"""
统一学习路径
------------

本专题按同一条工程认知链组织。先理解它解决的系统问题，再认识组件边界；随后沿真实源码调用链追踪数据、线程与所有权。读懂实现以后，再分别讨论语言机制、设计取舍、性能边界、可迁移思想和最小复刻。实际项目案例放在这八步之后，用来观察这些机制怎样进入完整机器人系统。

1. **功能介绍与需求分析**：:doc:`建立使用场景、核心保证与适用边界 <{path['intro']}>`。
2. **组件地图**：:doc:`先看清公开入口、核心对象和控制流关系 <{path['map']}>`。
3. **自顶向下源码实现**：:doc:`从第一条主调用链进入实现 <{path['source']}>`，再按下方源码目录顺序阅读后续模块。
4. **C/C++ 或 Rust 机制**：:doc:`把所有权、模板、RAII、ABI 与并发原语映射回设计目的 <{path['language']}>`。
5. **优秀设计与工程取舍**：:doc:`回看分层、接口和数据结构为什么这样组织 <{path['recap']}>`。
6. **缺点与性能边界**：仍在 :doc:`设计总结 <{path['recap']}>` 中检查容量、复杂度、尾延迟、故障和不适用条件。
7. **可迁移设计思想**：从 :doc:`设计总结 <{path['recap']}>` 提取能够带到其他运行时、驱动和机器人框架中的方法。
8. **最小复刻**：进入 :doc:`实现练习 <{path['rebuild']}>`，按依赖顺序重建最小闭环，并用关闭、过载和竞态不变量判断实现是否完整。
9. **实际开发与知名项目**：从 :doc:`环境与基础操作 <{path['guide']}>` 开始，最后进入 :doc:`真实项目案例 <{path['case']}>`。
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
