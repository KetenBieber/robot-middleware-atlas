"""Static closure checks for the pinned rmw_zenoh runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "3b5b9bf424443f9800dd148b5f1cc2053bbc37fe"

ARTICLE_NAMES = (
    "overview",
    "context-session-router",
    "graph-liveliness-cache",
    "publisher-subscription-dataflow",
    "subscription-waitset",
    "service-queryable-rpc",
    "qos-events-shm",
    "shutdown-dds-comparison",
)

REQUIRED = {
    "overview": (
        "GraphCache", "SubscriptionData", "ServiceData",
        "Zenoh Session", "liveliness", "WaitSet",
    ),
    "context-session-router": (
        "rmw_context_impl_s::Data", "NodeData", "zenoh::Session",
        "liveliness_get", "compare_exchange_strong", "nodes_",
    ),
    "graph-liveliness-cache": (
        "GraphCache::parse_put", "liveliness token", "type hash",
        "QoS", "guard condition", "snapshot",
    ),
    "publisher-subscription-dataflow": (
        "PublisherData::make", "SubscriptionData::add_new_message",
        "CDR", "attachment", "message_queue_", "weak_ptr",
    ),
    "subscription-waitset": (
        "rmw_wait_set_data_t", "condition_variable", "triggered",
        "queue_has_data_and_attach_condition_if_not", "rmw_take", "notify_one",
    ),
    "service-queryable-rpc": (
        "ServiceData", "Queryable", "rmw_request_id_t",
        "sequence_to_query_map_", "rmw_take_request", "Query::reply",
    ),
    "qos-events-shm": (
        "KEEP_LAST", "qos_to_keyexpr", "rmw_event.cpp",
        "unsupported", "shared-memory", "Buffer-aware",
    ),
    "shutdown-dds-comparison": (
        "weak_ptr", "undeclare", "Session close",
        "GraphCache", "rmw_request_id_t", "RMW",
    ),
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
    folder = ROOT / "content" / "articles" / "rmwzenoh"

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
            errors.append(f"{path.relative_to(ROOT)}: missing pinned rmw_zenoh commit")

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
                errors.append(
                    f"{path.relative_to(ROOT)}:{lineno}: editorial/process prose: {line.strip()}"
                )
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["rmwzenoh"])
        if order != ARTICLE_NAMES:
            errors.append(f"rmwzenoh article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["rmwzenoh"])
        if guides:
            errors.append(f"rmwzenoh guide list should be empty: {guides}")
        pin = literal_assignment("PROJECTS")["rmwzenoh"][1]
        if pin != PINNED:
            errors.append(f"rmwzenoh project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"RMWZENOH_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"RMWZENOH_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("RMWZENOH_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
