"""Static closure checks for the pinned Folly runtime/data-structure course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "c8ad483c91ef9cfc4cd1e41bb6bc5f575bf935c8"

ARTICLE_NAMES = (
    "overview",
    "spsc-cacheline-cursor",
    "mpmc-ticket-turnsequencer",
    "eventbase-atomic-notification",
    "iobuf-chain-ownership",
    "f14-cache-friendly-hash",
    "concurrent-hashmap-shards-hazptr",
    "rcu-grace-period-reclamation",
    "hhwheel-timer",
    "cpu-thread-pool",
)

REQUIRED = {
    "overview": ("ProducerConsumerQueue", "MPMCQueue", "IOBuf", "HHWheelTimer", "CPUThreadPoolExecutor"),
    "spsc-cacheline-cursor": ("remoteIndexCache", "localIndex", "hardware_destructive_interference_size", "Acquire / Release"),
    "mpmc-ticket-turnsequencer": ("pushTicket_", "popTicket_", "TurnSequencer", "FUTEX", "Stride"),
    "eventbase-atomic-notification": ("Armed", "kQueueArmedTag", "AtomicNotificationQueue", "maxReadAtOnce"),
    "iobuf-chain-ownership": ("SharedInfo", "next_", "prev_", "refcount", "Storage"),
    "f14-cache-friendly-hash": ("kCapacity", "tag", "SIMD", "outboundOverflowCount", "prefetch"),
    "concurrent-hashmap-shards-hazptr": ("ShardBits", "alignas(64)", "hazard", "seqlock", "contains"),
    "rcu-grace-period-reclamation": ("rcu_domain", "Grace Period", "target = curr + 2", "half_sync", "Hazard Pointer"),
    "hhwheel-timer": ("WHEEL_BUCKETS", "Intrusive", "Bitmap", "Cascade"),
    "cpu-thread-pool": ("BlockingQueue", "ThrottledLifoSem", "Poison Task", "OS Thread Priority"),
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
    folder = ROOT / "content" / "articles" / "folly"

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
        order = tuple(literal_assignment("ARTICLE_ORDER")["folly"])
        if order != ARTICLE_NAMES:
            errors.append(f"folly article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["folly"])
        if guides:
            errors.append(f"folly guide list should be empty: {guides}")
        pin = literal_assignment("PROJECTS")["folly"][1]
        if pin != PINNED:
            errors.append(f"folly project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"FOLLY_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"FOLLY_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("FOLLY_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
