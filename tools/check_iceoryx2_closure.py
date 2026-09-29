"""Static closure checks for the iceoryx2 zero-copy IPC course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "135d09dd8b29f321f1725920d434864c4e512378"
ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "service-abstraction",
    "shared-memory-pointer-offset",
    "pool-allocator-layout",
    "service-discovery-config",
    "publisher-loan",
    "zero-copy-connection",
    "subscriber-receive-reclaim",
    "fanout-backpressure-history",
    "event-notifier-listener",
    "thread-safety-event-reactor",
    "request-response-streaming",
    "blackboard-shared-state",
    "dead-node-recovery",
    "iceoryx2-vs-existing-shm",
)
GUIDE_NAMES = ("official-examples-lab",)
REQUIRED = {
    "overview": ("PointerOffset", "ZeroCopyConnection", "reclaim"),
    "architecture-map": ("Node", "Service", "DataSegment", "CAL"),
    "service-abstraction": ("SharedMemory", "Connection", "Monitoring", "Reactor"),
    "shared-memory-pointer-offset": ("ShmPointer", "PointerOffset", "register_and_translate_offset"),
    "pool-allocator-layout": ("UniqueIndexSet", "bucket_size", "PointerOffset", "AllocationStrategy"),
    "service-discovery-config": ("StaticConfig", "DynamicConfig", "ServiceHash"),
    "publisher-loan": ("loan_uninit", "SampleMut::send", "max_loaned_samples"),
    "zero-copy-connection": ("ZeroCopySender", "ZeroCopyReceiver", "PointerOffset"),
    "subscriber-receive-reclaim": ("receive", "release_offset", "reclaim"),
    "fanout-backpressure-history": ("RetryUntilDelivered", "DiscardData", "safe overflow", "history"),
    "event-notifier-listener": ("Notifier", "Listener", "EventActivation", "epoll"),
    "thread-safety-event-reactor": ("ipc_threadsafe", "WaitSet", "Reactor", "epoll"),
    "request-response-streaming": ("ChannelId", "RequestId", "PendingResponse", "ActiveRequest"),
    "blackboard-shared-state": ("UnrestrictedAtomic", "generation", "EntryHandle", "Blackboard"),
    "dead-node-recovery": ("NodeState", "DeadNodeView", "VersionMismatch"),
    "iceoryx2-vs-existing-shm": ("eCAL", "Fast DDS", "Cyclone", "PointerOffset"),
}
GUIDE_REQUIRED = {
    "official-examples-lab": (
        "publish_subscribe_publisher",
        "event_multiplexing_wait",
        "request_response_server",
        "blackboard_creator",
        "NodeState::Dead",
    ),
}
FENCE = re.compile(r"^\s*~~~")
PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|"
    r"任务进度|当前进度|工作轮次|Agent|Reviewer|ChatGPT|Codex|"
    r"审计报告|复审结论|质量门禁|quality\s+gate",
    re.I,
)
MALFORMED_TEX_ESCAPE = re.compile(r"\\\\(?:times|text|approx)")


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
        if not in_fence and MALFORMED_TEX_ESCAPE.search(line):
            errors.append(
                f"{path.relative_to(ROOT)}:{lineno}: doubled LaTeX command escape: {line.strip()}"
            )
        if not in_fence and line.strip() == "]":
            errors.append(f"{path.relative_to(ROOT)}:{lineno}: stray closing bracket")


def main() -> int:
    errors: list[str] = []
    article_count = 0
    guide_count = 0
    folder = ROOT / "content" / "articles" / "iceoryx2"

    for slug in ARTICLE_NAMES:
        path = folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        article_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        for term in REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required mechanism absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    guide_folder = ROOT / "content" / "guides" / "iceoryx2"
    for slug in GUIDE_NAMES:
        path = guide_folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing guide: {path.relative_to(ROOT)}")
            continue
        guide_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        for term in GUIDE_REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required lab concept absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["iceoryx2"])
        if order != ARTICLE_NAMES:
            errors.append(f"iceoryx2 article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["iceoryx2"])
        if guides != GUIDE_NAMES:
            errors.append(f"iceoryx2 guide navigation mismatch: {guides}")
        project_pin = literal_assignment("PROJECTS")["iceoryx2"][1]
        if project_pin != PINNED:
            errors.append(f"iceoryx2 project pin mismatch: {project_pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ICEORYX2_STATIC_PAGES={article_count}/{len(ARTICLE_NAMES)}")
    print(f"ICEORYX2_STATIC_GUIDES={guide_count}/{len(GUIDE_NAMES)}")
    print(f"ICEORYX2_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ICEORYX2_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
