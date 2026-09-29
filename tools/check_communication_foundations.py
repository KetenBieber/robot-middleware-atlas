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
