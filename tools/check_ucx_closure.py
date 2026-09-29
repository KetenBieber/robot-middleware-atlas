"""Static closure checks for the OpenUCX course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "8a6b06fb880accbb933a79cda893883872c68d9d"
ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "context-worker-endpoint",
    "wireup-lane-selection",
    "tag-send-request",
    "protocol-selection",
    "progress-engine",
    "uct-transport-model",
    "memory-domain-types",
    "rendezvous-gpu-pipeline",
    "backpressure-thread-safety",
    "ucx-vs-message-middleware",
    "design-recap",
)
GUIDE_NAMES = ("official-hello-world-lab",)
REQUIRED = {
    "overview": ("UCP", "UCT", "memory type", "progress"),
    "architecture-map": ("ucp_context_t", "ucp_worker_t", "ucp_ep_t", "ucp_request_t"),
    "context-worker-endpoint": ("req_mp", "ifaces", "ep_config", "lane"),
    "wireup-lane-selection": ("ucp_wireup_select_lanes", "rma_lanes", "rkey_ptr_lane", "transport"),
    "tag-send-request": ("ucp_tag_send_nbx", "ucp_request_get_param", "UCP_OP_ID_TAG_SEND"),
    "protocol-selection": ("ucp_proto_select_param", "mem_type", "msg_length", "kHash"),
    "progress-engine": ("ucp_worker_progress", "uct_worker_progress", "eventfd", "UCS_THREAD_MODE_SINGLE"),
    "uct-transport-model": ("uct_iface_ops", "ep_put_zcopy", "ep_am_bcopy", "Memory Domain"),
    "memory-domain-types": ("reg_mem_types", "detect_mem_types", "CUDA", "registration"),
    "rendezvous-gpu-pipeline": ("RTS", "GET ZCOPY", "rkey_ptr", "pipeline"),
    "backpressure-thread-safety": ("UCS_ERR_NO_RESOURCE", "uct_ep_pending_add", "SERIALIZED", "MULTI"),
    "ucx-vs-message-middleware": ("iceoryx2", "DDS", "UCX", "GXF"),
    "design-recap": ("lane", "protocol selection", "memory type", "progress"),
}
GUIDE_REQUIRED = {
    "official-hello-world-lab": (
        "ucp_hello_world",
        "ucp_worker_progress",
        "ucp_worker_arm",
        "ucp_tag_send_nbx",
        "UCX_TLS",
    ),
}
FENCE = re.compile(r"^\s*~~~")
PROCESS_LANGUAGE = re.compile(
    r"下一步|接下来|本专题|这个专题|推荐阅读|专题源码阅读路径|"
    r"后面(?:再|继续|研究|文章|专题)|未来(?:整套|将会|会逐渐)|"
    r"值得[^。；]*?(?:学习|研究)|最适合作为[^。；]*?实例|"
    r"更适合作为[^。；]*?研究对象|应该串起来读|对 Atlas 的意义"
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
    folder = ROOT / "content" / "articles" / "ucx"

    for slug in ARTICLE_NAMES:
        path = folder / f"{slug}.md"
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

    guide_folder = ROOT / "content" / "guides" / "ucx"
    for slug in GUIDE_NAMES:
        path = guide_folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing guide: {path.relative_to(ROOT)}")
            continue
        guide_count += 1
        source = path.read_text(encoding="utf-8-sig")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")
        for term in GUIDE_REQUIRED[slug]:
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required lab concept absent: {term}")
        check_fences(path, source, errors)
        check_public_prose(path, source, errors)

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["ucx"])
        if order != ARTICLE_NAMES:
            errors.append(f"ucx article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["ucx"])
        if guides != GUIDE_NAMES:
            errors.append(f"ucx guide navigation mismatch: {guides}")
        project_pin = literal_assignment("PROJECTS")["ucx"][1]
        if project_pin != PINNED:
            errors.append(f"ucx project pin mismatch: {project_pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"UCX_STATIC_PAGES={article_count}/{len(ARTICLE_NAMES)}")
    print(f"UCX_STATIC_GUIDES={guide_count}/{len(GUIDE_NAMES)}")
    print(f"UCX_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("UCX_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
