"""Static closure checks for the pinned Seastar shard-per-core runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "8df8212e53577e1d8477a5c901457cd61d88afc7"

ARTICLE_NAMES = (
    "overview",
    "reactor-shard-per-core",
    "scheduling-groups-vruntime",
    "smp-message-queue",
    "future-continuation-task",
    "sharded-foreign-ptr",
    "cross-shard-memory-reclaim",
)

REQUIRED = {
    "overview": ("shard-per-core", "SMP", "Future", "cross-CPU"),
    "reactor-shard-per-core": ("run_some_tasks", "need_preempt", "poller", "pure_poll"),
    "scheduling-groups-vruntime": ("_vruntime", "_shares", "account_runtime", "need_preempt"),
    "smp-message-queue": (
        "spsc_queue",
        "queue_length",
        "batch_size",
        "pending_fifo",
        "_completed_fifo",
        "process_queue",
        "service-group semaphore",
        "get_units",
        "process_completions",
        "maybe_wakeup",
        "_sleeping",
        "systemwide_memory_barrier",
        "schedule(this)",
        "Round-trip Credit",
        "Owner-Shard",
        "origin",
        "target",
    ),
    "future-continuation-task": ("continuation_base", "public task", "run_and_dispose", "nodiscard"),
    "sharded-foreign-ptr": ("sharded<Service>", "foreign_ptr", "smp::submit_to", "owner shard"),
    "cross-shard-memory-reclaim": ("xcpu_freelist", "free_cross_cpu", "drain_cross_cpu_freelist", "MPSC"),
}

PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|本轮任务|本次任务|任务进度|当前进度|"
    r"Agent|Reviewer|ChatGPT|Codex|审计报告|复审结论|质量门禁|"
    r"旧版导航|新版导航|这里改为|此处改为|为了方便阅读",
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


def main() -> int:
    errors: list[str] = []
    count = 0
    folder = ROOT / "content" / "articles" / "seastar"

    for slug in ARTICLE_NAMES:
        path = folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        count += 1
        source = path.read_text(encoding="utf-8-sig")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        folded = source.casefold()
        for term in REQUIRED[slug]:
            if term.casefold() not in folded:
                errors.append(f"{path.relative_to(ROOT)}: required mechanism absent: {term}")

        in_fence = False
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and PROCESS_LANGUAGE.search(line):
                errors.append(f"{path.relative_to(ROOT)}:{lineno}: editorial/process prose: {line.strip()}")
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["seastar"])
        if order != ARTICLE_NAMES:
            errors.append(f"seastar article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["seastar"])
        if guides:
            errors.append(f"seastar guide list should be empty: {guides}")
        pin = literal_assignment("PROJECTS")["seastar"][1]
        if pin != PINNED:
            errors.append(f"seastar project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"SEASTAR_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"SEASTAR_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("SEASTAR_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
