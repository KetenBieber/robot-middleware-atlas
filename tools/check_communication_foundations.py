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
    "threads-memory-order": ("acquire", "release", "condition variable", "Ring Buffer", "False Sharing"),
    "processes-shared-memory": ("共享物理页", "offset", "Pool + Loan", "进程死亡"),
    "queues-backpressure": ("Backpressure", "Data Age", "Drop Old", "WCET"),
    "network-distributed": ("serialization", "Discovery", "Routing", "business ACK"),
    "heterogeneous-memory": ("GPU VRAM", "CUDA IPC", "Pinned Memory", "RDMA", "Memory domain"),
    "atlas-mapping": ("iceoryx2", "Fast DDS", "Cyclone DDS", "UCX"),
}
FENCE = re.compile(r"^\s*~~~")
PROCESS_LANGUAGE = re.compile(
    r"下一步|接下来|本专题|这个专题|推荐阅读|"
    r"后面(?:再|继续|研究|文章|专题)|未来(?:整套|将会|会逐渐)|"
    r"值得[^。；]*?(?:学习|研究)|最适合作为[^。；]*?实例|"
    r"更适合作为[^。；]*?研究对象|应该串起来读|对 Atlas 的意义"
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
        for lineno, line in enumerate(source.splitlines(), 1):
            if FENCE.match(line) is None:
                continue
            opened = lineno if opened is None else None
        if opened is not None:
            errors.append(f"{path.relative_to(ROOT)}:{opened}: unclosed code fence")

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
