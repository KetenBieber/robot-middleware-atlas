"""Static closure checks for the shared communication foundations course."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "ownership-address-space",
    "threads-memory-order",
    "processes-shared-memory",
    "queues-backpressure",
    "network-distributed",
    "heterogeneous-memory",
    "atlas-mapping",
)
REQUIRED = {
    "ownership-address-space": ("payload", "ownership", "地址空间", "PointerOffset", "memory domain"),
    "threads-memory-order": ("acquire", "release", "condition variable", "Ring", "False Sharing"),
    "processes-shared-memory": ("共享物理页", "offset", "loan", "进程死亡"),
    "queues-backpressure": ("Backpressure", "Data Age", "Drop Old", "WCET"),
    "network-distributed": ("serialization", "Discovery", "Routing", "business"),
    "heterogeneous-memory": ("GPU VRAM", "CUDA IPC", "Pinned Memory", "RDMA", "Memory Domain"),
    "atlas-mapping": ("iceoryx2", "Fast DDS", "Cyclone DDS", "UCX"),
}
FENCE = re.compile(r"^\s*(?:\x60{3}|~~~)")
PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|"
    r"任务进度|当前进度|工作轮次|Agent|Reviewer|ChatGPT|Codex|"
    r"审计报告|复审结论|质量门禁|quality\s+gate",
    re.I,
)
MALFORMED_TEX_ESCAPE = re.compile(r"\\\\(?:times|text|approx)")


def main() -> int:
    errors: list[str] = []
    count = 0
    folder = ROOT / "docs" / "communication-foundations"

    for slug in PAGES:
        path = folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue

        count += 1
        source = path.read_text(encoding="utf-8-sig")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")

        normalized_source = source.casefold()
        for term in REQUIRED[slug]:
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

    index = folder / "index.rst"
    if not index.is_file():
        errors.append("missing communication-foundations/index.rst")
    else:
        index_text = index.read_text(encoding="utf-8-sig")
        for slug in PAGES:
            if slug not in index_text:
                errors.append(f"foundation navigation missing: {slug}")

    print(f"COMM_FOUNDATIONS_PAGES={count}/{len(PAGES)}")
    print(f"COMM_FOUNDATIONS_ERRORS={len(errors)}")
    for error in errors:
        print("COMM_FOUNDATIONS_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
