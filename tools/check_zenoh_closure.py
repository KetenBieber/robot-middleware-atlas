"""Static closure checks for the pinned Zenoh runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "9fcd9cb5d364192c3e8a27e66de76f4bc750d1d5"

ARTICLE_NAMES = (
    "foundations",
    "architecture-map",
    "session-runtime",
    "publisher-routing",
    "query-lifecycle",
    "resource-route-cache",
    "backpressure-close",
    "rust-cpp-design-lab",
    "design-recap",
)

REQUIRED = {
    "query-lifecycle": (
        "QueryState", "ResponseFinal", "pending_queries",
        "QueryCleanup", "finalize_pending_query", "Arc::into_inner",
        "queries_lock", "Face close",
    ),
    "resource-route-cache": (
        "ResourceContext", "matches", "RegionMap", "NodeIdMap",
        "routes_version", "get_or_set_route", "disable_data_routes",
        "disable_query_routes",
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
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def main() -> int:
    errors: list[str] = []
    folder = ROOT / "content" / "articles" / "zenoh"
    count = 0

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
            errors.append(f"{path.relative_to(ROOT)}: missing pinned Zenoh commit")

        folded = source.casefold()
        for term in REQUIRED.get(slug, ()):
            if term.casefold() not in folded:
                errors.append(
                    f"{path.relative_to(ROOT)}: required mechanism absent: {term}"
                )

        in_fence = False
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("~~~") or stripped.startswith(chr(96) * 3):
                in_fence = not in_fence
                continue
            if not in_fence and PROCESS_LANGUAGE.search(line):
                errors.append(
                    f"{path.relative_to(ROOT)}:{lineno}: "
                    f"editorial/process prose: {line.strip()}"
                )
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["zenoh"])
        if order != ARTICLE_NAMES:
            errors.append(f"zenoh article navigation mismatch: {order}")

        pin = literal_assignment("PROJECTS")["zenoh"][1]
        if pin != PINNED:
            errors.append(f"zenoh project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ZENOH_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"ZENOH_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ZENOH_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
