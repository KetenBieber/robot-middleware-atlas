"""Static closure checks for the pinned nginx runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "b74b5c961e687c76489482b44cedff63acd18c84"

ARTICLE_NAMES = (
    "overview",
    "worker-epoll-accept",
    "timer-rbtree",
    "posted-event-queue",
    "connection-pool-lifecycle",
    "memory-pool-slab",
)

REQUIRED = {
    "overview": (
        "master",
        "worker",
        "free_connections",
        "rbtree",
        "posted",
    ),
    "worker-epoll-accept": (
        "ngx_process_events_and_timers",
        "epoll_wait",
        "instance",
        "accept mutex",
        "SO_REUSEPORT",
        "EPOLLEXCLUSIVE",
        "accept_mutex off",
        "ngx_trylock_accept_mutex",
        "ngx_enable_accept_events",
        "ngx_accept_disabled",
        "ngx_get_connection",
        "free_connections",
        "worker_connections",
        "ee.data.ptr",
        "rev->instance != instance",
        "ngx_close_listening_sockets",
        "stale event",
        "generation",
    ),
    "timer-rbtree": (
        "ngx_event_timer_rbtree",
        "ngx_rbtree_min",
        "NGX_TIMER_LAZY_DELAY",
        "300",
        "ngx_rbtree_insert_timer_value",
        "ngx_msec_int_t",
        "49",
        "timer_set",
        "timedout",
        "cancelable",
        "ngx_event_no_timers_left",
        "ngx_rbtree_next",
        "ngx_event_del_timer",
        "ngx_rbtree_data",
        "wrap",
        "remove",
        "handler",
        "shutdown",
    ),
    "posted-event-queue": (
        "ngx_posted_events",
        "ngx_posted_next_events",
        "ngx_post_event",
        "intrusive",
    ),
    "connection-pool-lifecycle": (
        "free_connections",
        "reusable_connections_queue",
        "ngx_drain_connections",
        "instance",
        "generation",
    ),
    "memory-pool-slab": (
        "ngx_palloc_small",
        "pool->current",
        "ngx_slab_alloc",
        "NGX_SLAB_PAGE_MASK",
        "cleanup",
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
    folder = ROOT / "content" / "articles" / "nginx"

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
        order = tuple(literal_assignment("ARTICLE_ORDER")["nginx"])
        if order != ARTICLE_NAMES:
            errors.append(f"nginx article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["nginx"])
        if guides:
            errors.append(f"nginx guide list should be empty in first closure: {guides}")
        pin = literal_assignment("PROJECTS")["nginx"][1]
        if pin != PINNED:
            errors.append(f"nginx project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"NGINX_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"NGINX_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("NGINX_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
