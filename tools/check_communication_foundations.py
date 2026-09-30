"""Static closure checks for the two-view Communication & Runtime Design handbook."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "docs" / "communication-foundations"

PAGES = (
    "ownership-address-space",
    "threads-memory-order",
    "concurrent-queues-progress",
    "thread-dataflow-lab",
    "processes-shared-memory",
    "queues-backpressure",
    "network-distributed",
    "heterogeneous-memory",
    "atlas-mapping",
    "industry-runtime-cases",
)
REQUIRED = {
    "ownership-address-space": ("payload", "ownership", "地址空间", "PointerOffset", "memory domain"),
    "threads-memory-order": ("acquire", "release", "condition variable", "Ring", "False Sharing"),
    "concurrent-queues-progress": ("MPSC", "MPMC", "lock-free", "wait-free", "sequence", "ABA", "reclamation"),
    "thread-dataflow-lab": ("Bounded", "SPSC", "MPSC", "eventfd", "Executor", "Robot"),
    "processes-shared-memory": ("共享物理页", "offset", "loan", "进程死亡"),
    "queues-backpressure": ("Backpressure", "Data Age", "Drop Old", "WCET"),
    "network-distributed": ("serialization", "Discovery", "Routing", "business"),
    "heterogeneous-memory": ("GPU VRAM", "CUDA IPC", "Pinned Memory", "RDMA", "Memory Domain"),
    "atlas-mapping": ("iceoryx2", "Fast DDS", "Cyclone DDS", "UCX"),
    "industry-runtime-cases": (
        "Apollo Planning",
        "soem_interface",
        "rmw_cyclonedds",
        "EventBasedScheduler",
        "BlockMemoryPool",
    ),
}

SCENARIO_PAGES = (
    "scenario-latest-state-vs-event",
    "scenario-streaming-pipeline",
    "scenario-control-safety",
    "scenario-thread-runtime",
    "scenario-large-payload-ipc",
    "scenario-distributed-gpu",
    "scenario-mechanism-matrix",
)
SCENARIO_REQUIRED = {
    "scenario-latest-state-vs-event": ("State", "Event", "Snapshot", "Apollo", "latest"),
    "scenario-streaming-pipeline": ("Data Age", "Drop Old", "BlockMemoryPool", "Holoscan", "Little"),
    "scenario-control-safety": ("Emergency Stop", "deadline", "priority inversion", "EtherCAT", "watchdog"),
    "scenario-thread-runtime": ("per-worker", "work stealing", "eventfd", "SCHED_FIFO", "Cyber"),
    "scenario-large-payload-ipc": ("Shared Memory", "descriptor", "generation", "eventfd", "iceoryx2"),
    "scenario-distributed-gpu": ("Memory Domain", "CUDA IPC", "UCX", "Rendezvous", "Completion"),
    "scenario-mechanism-matrix": ("场景", "机制", "Emergency Stop", "GPU Tensor", "EtherCAT"),
}
ATLAS_PAGES = {
    "mechanism-selection-atlas": (
        "Queue Topology",
        "Backpressure",
        "Wakeup",
        "Scheduler",
        "Ownership",
        "IPC / Transport",
        "Memory Allocation",
    ),
}

HUBS = {
    "local-runtime": (
        "ownership-address-space",
        "threads-memory-order",
        "queues-backpressure",
        "concurrent-queues-progress",
        "thread-dataflow-lab",
    ),
    "ipc-distributed": (
        "processes-shared-memory",
        "network-distributed",
    ),
    "heterogeneous-data-plane": (
        "heterogeneous-memory",
    ),
    "mapping-and-cases": (
        "atlas-mapping",
        "industry-runtime-cases",
    ),
}

TOP_VIEWS = ("problem-driven-design", "solution-space")

FENCE = re.compile(r"^\s*(?:\x60{3}|~~~)")
PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|"
    r"任务进度|当前进度|工作轮次|Agent|Reviewer|ChatGPT|Codex|"
    r"审计报告|复审结论|质量门禁|quality\s+gate",
    re.I,
)
MALFORMED_TEX_ESCAPE = re.compile(r"\\\\(?:times|text|approx)")


def check_markdown(slug: str, required: tuple[str, ...], errors: list[str]) -> bool:
    path = FOLDER / f"{slug}.md"
    if not path.is_file():
        errors.append(f"missing page: {path.relative_to(ROOT)}")
        return False

    source = path.read_text(encoding="utf-8-sig")
    if not source.startswith("# "):
        errors.append(f"{path.relative_to(ROOT)}: missing h1")

    normalized_source = source.casefold()
    for term in required:
        if term.casefold() not in normalized_source:
            errors.append(f"{path.relative_to(ROOT)}: required concept absent: {term}")

    opened = None
    token = None
    for lineno, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("~~~"):
            this_token = "~~~"
        elif stripped.startswith(chr(96) * 3):
            this_token = chr(96) * 3
        else:
            continue
        if opened is None:
            opened = lineno
            token = this_token
        elif this_token == token:
            opened = None
            token = None
    if opened is not None:
        errors.append(f"{path.relative_to(ROOT)}:{opened}: unclosed code fence")

    in_fence = False
    fence_token = None
    for lineno, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("~~~"):
            this_token = "~~~"
        elif stripped.startswith(chr(96) * 3):
            this_token = chr(96) * 3
        else:
            this_token = None
        if this_token is not None:
            if not in_fence:
                in_fence = True
                fence_token = this_token
            elif this_token == fence_token:
                in_fence = False
                fence_token = None
            continue
        if not in_fence and PROCESS_LANGUAGE.search(line):
            errors.append(
                f"{path.relative_to(ROOT)}:{lineno}: production-process narration: {line.strip()}"
            )
        if not in_fence and MALFORMED_TEX_ESCAPE.search(line):
            errors.append(
                f"{path.relative_to(ROOT)}:{lineno}: doubled LaTeX command escape: {line.strip()}"
            )
        if not in_fence and line.strip() == "]":
            errors.append(f"{path.relative_to(ROOT)}:{lineno}: stray closing bracket")
    return True


def check_hierarchy(errors: list[str]) -> None:
    index = FOLDER / "index.rst"
    if not index.is_file():
        errors.append("missing communication-foundations/index.rst")
        return

    index_text = index.read_text(encoding="utf-8-sig")
    for view in TOP_VIEWS:
        if view not in index_text:
            errors.append(f"top-level design view missing from index: {view}")

    for hub in HUBS:
        if hub in index_text:
            errors.append(
                f"deep solution hub must not be a top-level sibling of the two design views: {hub}"
            )

    problem = FOLDER / "problem-driven-design.rst"
    solution = FOLDER / "solution-space.rst"
    if not problem.is_file():
        errors.append("missing problem-driven-design.rst")
    else:
        text = problem.read_text(encoding="utf-8-sig")
        for slug in SCENARIO_PAGES:
            if slug not in text:
                errors.append(f"problem-driven-design.rst: scenario child missing: {slug}")
        for hub in HUBS:
            if hub in text:
                errors.append(
                    f"problem-driven-design.rst: deep mechanism hub should be cross-linked, not parented: {hub}"
                )

    if not solution.is_file():
        errors.append("missing solution-space.rst")
    else:
        text = solution.read_text(encoding="utf-8-sig")
        if "mechanism-selection-atlas" not in text:
            errors.append("solution-space.rst: mechanism-selection-atlas missing")
        for hub in HUBS:
            if hub not in text:
                errors.append(f"solution-space.rst: solution hub missing: {hub}")

    for hub, children in HUBS.items():
        hub_path = FOLDER / f"{hub}.rst"
        if not hub_path.is_file():
            errors.append(f"missing solution hub: {hub_path.relative_to(ROOT)}")
            continue
        hub_text = hub_path.read_text(encoding="utf-8-sig")
        for child in children:
            if child not in hub_text:
                errors.append(f"{hub_path.relative_to(ROOT)}: child navigation missing: {child}")

    expected_children = [child for children in HUBS.values() for child in children]
    if sorted(expected_children) != sorted(PAGES):
        errors.append("solution parent/child map does not cover deep mechanism PAGES exactly once")


def main() -> int:
    errors: list[str] = []
    foundation_count = 0
    scenario_count = 0
    atlas_count = 0

    for slug in PAGES:
        if check_markdown(slug, REQUIRED[slug], errors):
            foundation_count += 1

    for slug in SCENARIO_PAGES:
        if check_markdown(slug, SCENARIO_REQUIRED[slug], errors):
            scenario_count += 1

    for slug, required in ATLAS_PAGES.items():
        if check_markdown(slug, required, errors):
            atlas_count += 1

    check_hierarchy(errors)

    print(f"COMM_FOUNDATIONS_PAGES={foundation_count}/{len(PAGES)}")
    print(f"COMM_SCENARIO_PAGES={scenario_count}/{len(SCENARIO_PAGES)}")
    print(f"COMM_ATLAS_PAGES={atlas_count}/{len(ATLAS_PAGES)}")
    print(f"COMM_TOP_LEVEL_VIEWS={len(TOP_VIEWS)}")
    print(f"COMM_FOUNDATIONS_ERRORS={len(errors)}")
    for error in errors:
        print("COMM_FOUNDATIONS_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
