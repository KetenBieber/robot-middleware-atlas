"""Static closure checks for the rosidl::Buffer / CUDA backend course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "d7cd9642d77a1d64fd85f25ba0bf96e108401900"

ARTICLE_NAMES = (
    "overview",
    "buffer-backend-contract",
    "cuda-vmm-pool",
    "ipc-fd-shm-registry",
    "stream-handles-lifetime",
    "accelerator-ipc-protocols",
    "endpoint-locality-fallback",
)
GUIDE_NAMES = ("case-study-isaac-ros-5-migration",)

REQUIRED = {
    "overview": (
        "CudaMemoryPool",
        "CudaVmmIPCManager",
        "HostEndpointManager",
        "SCM_RIGHTS",
        "ReadHandle",
    ),
    "buffer-backend-contract": (
        "ipc_decision_cache_",
        "to_cpu()",
        "fallback",
        "Descriptor",
    ),
    "cuda-vmm-pool": (
        "lower_bound",
        "swap-and-pop",
        "free_blocks_",
        "IPCMetadata",
        "grace",
    ),
    "ipc-fd-shm-registry": (
        "epoll",
        "eventfd",
        "SCM_RIGHTS",
        "unordered_map",
        "shared",
    ),
    "stream-handles-lifetime": (
        "WriteHandle",
        "ReadHandle",
        "BufferRecycler",
        "std::deque",
        "cudaEvent",
    ),
    "accelerator-ipc-protocols": (
        "SCM_RIGHTS",
        "block_id",
        "Generation",
        "FdBroker",
        "MSG_CTRUNC",
        "epoll",
        "dma-buf",
        "Data Age",
    ),
    "endpoint-locality-fallback": (
        "INTRA_PROCESS",
        "INTER_PROCESS_SAME_HOST",
        "Linux uid",
        "fallback",
        "capability",
    ),
}

GUIDE_REQUIRED = {
    "case-study-isaac-ros-5-migration": (
        "2026-09-21",
        "NITROS",
        "rosidl::Buffer",
        "CUDA buffer backend",
        "fallback",
    ),
}

FENCE = re.compile(r"^\s*~~~")
PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|"
    r"任务进度|当前进度|工作轮次|Agent|Reviewer|ChatGPT|Codex|"
    r"审计报告|复审结论|质量门禁|quality\s+gate",
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


def main() -> int:
    errors: list[str] = []
    article_count = 0
    guide_count = 0

    article_folder = ROOT / "content" / "articles" / "rosidlbuffer"
    for slug in ARTICLE_NAMES:
        path = article_folder / f"{slug}.md"
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

    guide_folder = ROOT / "content" / "guides" / "rosidlbuffer"
    for slug in GUIDE_NAMES:
        path = guide_folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing guide: {path.relative_to(ROOT)}")
            continue
        guide_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        for term in GUIDE_REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required case concept absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["rosidlbuffer"])
        if order != ARTICLE_NAMES:
            errors.append(f"rosidlbuffer article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["rosidlbuffer"])
        if guides != GUIDE_NAMES:
            errors.append(f"rosidlbuffer guide navigation mismatch: {guides}")
        project_pin = literal_assignment("PROJECTS")["rosidlbuffer"][1]
        if project_pin != PINNED:
            errors.append(f"rosidlbuffer project pin mismatch: {project_pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ROSIDLBUFFER_STATIC_PAGES={article_count}/{len(ARTICLE_NAMES)}")
    print(f"ROSIDLBUFFER_STATIC_GUIDES={guide_count}/{len(GUIDE_NAMES)}")
    print(f"ROSIDLBUFFER_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ROSIDLBUFFER_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
