"""Static closure checks for the pinned Asio runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "8806a6803cde7054c3049d3666d3ec36786568c5"

ARTICLE_NAMES = (
    "overview",
    "scheduler-operation-queue",
    "epoll-reactor-descriptor-state",
    "strand-serialization",
    "work-lifetime-cancellation",
)

REQUIRED = {
    "overview": (
        "scheduler_operation",
        "Strand",
        "outstanding work",
        "Reactor",
    ),
    "scheduler-operation-queue": (
        "task_operation_",
        "Intrusive",
        "outstanding_work_",
        "Cancellation",
    ),
    "epoll-reactor-descriptor-state": (
        "descriptor_state",
        "op_queue",
        "Speculative",
        "EPOLLET",
        "Interrupter",
    ),
    "strand-serialization": (
        "waiting_queue_",
        "ready_queue_",
        "locked_",
        "logical owner",
        "invoker",
        "push_waiting_to_ready",
        "running_in_this_thread",
        "call_stack",
        "shutdown_",
        "dispatch",
        "post",
    ),
    "work-lifetime-cancellation": (
        "work_started",
        "executor_work_guard",
        "payload",
        "operation_aborted",
        "reclamation",
        "post_deferred_completions",
        "work_cleanup",
        "deregister_descriptor",
        "handler_work",
        "stop_all_threads",
    ),
}

PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|本轮任务|本次任务|任务进度|当前进度|"
    r"Agent|Reviewer|ChatGPT|Codex|审计报告|复审结论|质量门禁|"
    r"旧版导航|新版导航|这里改为|此处改为|为了方便阅读|推荐先读",
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
    folder = ROOT / "content" / "articles" / "asio"

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
                errors.append(
                    f"{path.relative_to(ROOT)}: required mechanism absent: {term}"
                )

        in_fence = False
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and PROCESS_LANGUAGE.search(line):
                errors.append(
                    f"{path.relative_to(ROOT)}:{lineno}: editorial/process prose: {line.strip()}"
                )
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["asio"])
        if order != ARTICLE_NAMES:
            errors.append(f"asio article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["asio"])
        if guides:
            errors.append(f"asio guide list should be empty: {guides}")
        pin = literal_assignment("PROJECTS")["asio"][1]
        if pin != PINNED:
            errors.append(f"asio project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ASIO_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"ASIO_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ASIO_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
