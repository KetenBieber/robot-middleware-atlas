"""Static closure checks for the NVIDIA Holoscan runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "66a9609ac37515405561b9b8dbdee8e57f41ab11"
GXF_PINNED = "daf1810358301f642374dfb3d725be349bba5ec0"

ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "flowgraph-containers",
    "event-based-scheduler",
    "gxf-event-runtime-internals",
    "gxf-entity-executor-router",
    "operator-materialization-lifecycle",
    "conditions-connectors-backpressure",
    "allocator-cuda-memory",
    "cuda-stream-event-propagation",
    "distributed-ucx-runtime",
)
GUIDE_NAMES = (
    "case-study-endoscopy-tool-tracking",
    "case-study-ultrasound-segmentation",
)

REQUIRED = {
    "overview": (
        "Operator",
        "Condition",
        "EventBasedScheduler",
        "MemoryAvailableCondition",
        "UCX",
    ),
    "architecture-map": (
        "ThreadPool",
        "GXF",
        "CudaStreamPool",
        "stop_execution",
    ),
    "flowgraph-containers": (
        "succ_",
        "pred_",
        "NodeTypeCompare",
        "ordered_nodes_",
        "cached_cyclic_roots_",
    ),
    "event-based-scheduler": (
        "enable_queue_stealing",
        "internal_event_shard_count",
        "SCHED_FIFO",
        "READY",
    ),
    "gxf-event-runtime-internals": (
        "TimedJobList",
        "UniqueEventList",
        "priority_queue",
        "condition_variable",
        "compare_exchange_strong",
    ),
    "gxf-entity-executor-router": (
        "syncInbox",
        "syncOutbox",
        "execution_mutex",
        "StagingQueue",
        "ExpiringMessageAvailableSchedulingTerm",
    ),
    "operator-materialization-lifecycle": (
        "initialize_base",
        "initialize_graph_entity",
        "GXFWrapper",
        "EntityExecutor::executeEntity",
        "syncInbox",
        "syncOutbox",
        "EntityGroup",
    ),
    "conditions-connectors-backpressure": (
        "DoubleBufferReceiver",
        "DownstreamMessageAffordableCondition",
        "Simpson",
        "MemoryAvailableCondition",
    ),
    "allocator-cuda-memory": (
        "BlockMemoryPool",
        "StreamOrderedAllocator",
        "RMMAllocator",
        "CudaStreamPool",
        "set_deallocation_stream",
    ),
    "cuda-stream-event-propagation": (
        "CudaStreamId",
        "CudaStreamHandle",
        "receive_cuda_stream",
        "set_cuda_stream",
        "cudaEventRecord",
        "cudaStreamWaitEvent",
        "propagate_stream_to_entity_memory_buffers",
        "set_deallocation_stream",
        "execution dependency",
    ),
    "distributed-ucx-runtime": (
        "UcxTransmitter",
        "UcxSerializationBuffer",
        "CodecRegistry",
        "network_connection_timeout",
    ),
}

GUIDE_REQUIRED = {
    "case-study-endoscopy-tool-tracking": (
        "LSTMTensorRTInferenceOp",
        "BlockMemoryPool",
        "CudaStreamPool",
        "rdma",
    ),
    "case-study-ultrasound-segmentation": (
        "input_on_cuda",
        "BlockMemoryPool",
        "CudaStreamPool",
        "UnboundedAllocator",
    ),
}

FENCE = re.compile(r"^\s*~~~")
PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|"
    r"任务进度|当前进度|工作轮次|Agent|Reviewer|ChatGPT|Codex|"
    r"审计报告|复审结论|质量门禁|quality\s+gate",
    re.I,
)


def literal_assignment(name: str):
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def check_fences(path: Path, source: str, errors: list[str]) -> None:
    opened = None
    for lineno, line in enumerate(source.splitlines(), 1):
        if FENCE.match(line) is None:
            continue
        opened = lineno if opened is None else None
    if opened is not None:
        errors.append(f"{path.relative_to(ROOT)}:{opened}: unclosed code fence")


def check_public_prose(path: Path, source: str, errors: list[str]) -> None:
    in_fence = False
    for lineno, line in enumerate(source.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and PROCESS_LANGUAGE.search(line):
            errors.append(
                f"{path.relative_to(ROOT)}:{lineno}: process/editorial narration: {line.strip()}"
            )


def main() -> int:
    errors: list[str] = []
    article_count = 0
    guide_count = 0

    article_folder = ROOT / "content" / "articles" / "holoscan"
    for slug in ARTICLE_NAMES:
        path = article_folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        article_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        if slug.startswith("gxf-") and GXF_PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing public GXF pinned commit")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        for term in REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required mechanism absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    guide_folder = ROOT / "content" / "guides" / "holoscan"
    for slug in GUIDE_NAMES:
        path = guide_folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing guide: {path.relative_to(ROOT)}")
            continue
        guide_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        for term in GUIDE_REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required case concept absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["holoscan"])
        if order != ARTICLE_NAMES:
            errors.append(f"holoscan article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["holoscan"])
        if guides != GUIDE_NAMES:
            errors.append(f"holoscan guide navigation mismatch: {guides}")
        project_pin = literal_assignment("PROJECTS")["holoscan"][1]
        if project_pin != PINNED:
            errors.append(f"holoscan project pin mismatch: {project_pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"HOLOSCAN_STATIC_PAGES={article_count}/{len(ARTICLE_NAMES)}")
    print(f"HOLOSCAN_STATIC_GUIDES={guide_count}/{len(GUIDE_NAMES)}")
    print(f"HOLOSCAN_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("HOLOSCAN_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
