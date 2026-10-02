"""Static closure checks for the fifteen public LCM pages.

This checks source-document structure and local Markdown links only.
It does not claim the upstream LCM/Drake runtime or generated HTML is tested.
Run separately from tools/check_cpp_examples.py and make html-full.
"""

from __future__ import annotations

import ast
import re
import sys

from build_sphinx_sources import rewrite_generated_project_links
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTICLE_NAMES = (
    "overview", "foundations", "architecture-map", "provider-vtable",
    "udpm-publish-protocol", "receive-reassembly", "subscription-dispatch",
    "cpp-binding-lifetime-quiescence", "types-and-eventlog",
    "c-abi-cpp-design-lab", "design-recap",
)
GUIDE_NAMES = (
    "use-environment", "use-pubsub-types", "closed-loop-project",
    "use-operations", "case-study-drake",
)
PAGES = tuple(ROOT / "content" / "articles" / "lcm" / (x + ".md")
              for x in ARTICLE_NAMES) + tuple(
    ROOT / "content" / "guides" / "lcm" / (x + ".md") for x in GUIDE_NAMES
)
EXAMPLE_FILES = (
    ROOT / "examples/lcm/closed_loop/CMakeLists.txt",
    ROOT / "examples/lcm/closed_loop/types/joint_state_t.lcm",
    ROOT / "examples/lcm/closed_loop/src/sender.cpp",
    ROOT / "examples/lcm/closed_loop/src/receiver.cpp",
)
PROJECT_GUIDE = ROOT / "content/guides/lcm/closed-loop-project.md"
LEGACY_BADGE = re.compile(
    r"^(?:仓库与提交：|符号：|对应的上游实现如下：|"
    r"\*\*(?:代码身份|图示身份|固定源码摘录))", re.MULTILINE
)
MARKDOWN_LINK = re.compile(r"\]\(([^)]+\.md)(?:#[^)]*)?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
REQUIRED = {
    "overview": ("publish", "handle", "provider", "ring"),
    "foundations": ("userdata", "void", "trampoline"),
    "architecture-map": ("lcm_publish", "recv_thread", "subscription"),
    "provider-vtable": ("vtable", "lcm_create", "publish"),
    "udpm-publish-protocol": ("LC02", "LC03", "transmit_lock"),
    "receive-reassembly": ("inbufs_filled", "ringbuf", "notify_pipe"),
    "subscription-dispatch": ("callback_scheduled", "marked_for_deletion"),
    "cpp-binding-lifetime-quiescence": ("userdata", "channel_buf", "grace period", "pre-entry", "subscriptions", "quiescence"),
    "types-and-eventlog": ("fingerprint", "timestamp", "event"),
    "c-abi-cpp-design-lab": ("RAII", "trampoline", "callback"),
    "design-recap": ("Provider", "EventLog", "shutdown"),
    "use-environment": ("Provider URL", "handle"),
    "use-pubsub-types": ("schema", "subscribe", "callback"),
    "closed-loop-project": ("joint_state_t", "atlas_sender", "atlas_receiver", "handleTimeout"),
    "use-operations": ("回放", "故障", "关闭"),
    "case-study-drake": ("Context", "LcmSubscriberSystem", "Simulator"),
}


def article_order() -> tuple[str, ...]:
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "ARTICLE_ORDER"
                   for t in node.targets):
                order = ast.literal_eval(node.value)
                return tuple(order["lcm"])
    raise RuntimeError("ARTICLE_ORDER missing from build_sphinx_sources.py")


def guide_order() -> tuple[str, ...]:
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "GUIDE_ORDER":
                order = ast.literal_eval(node.value)
                return tuple(order["lcm"])
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "GUIDE_ORDER"
                   for t in node.targets):
                order = ast.literal_eval(node.value)
                return tuple(order["lcm"])
    raise RuntimeError("GUIDE_ORDER missing from build_sphinx_sources.py")


def main() -> int:
    errors: list[str] = []
    link_count = 0
    source_count = 0
    known = set(PAGES)
    for path in PAGES:
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        source_count += 1
        rel = path.relative_to(ROOT)
        source = path.read_text(encoding="utf-8-sig")
        # Source files sit in different content trees, but publication
        # flattens articles and guides into docs/generated/lcm.
        # Verify that the builder rewrites GitHub source-relative crosslinks.
        generated = rewrite_generated_project_links(source, "lcm")
        for generated_match in MARKDOWN_LINK.finditer(generated):
            generated_target = generated_match.group(1)
            if generated_target.startswith(("../../articles/lcm/", "../../guides/lcm/")):
                errors.append(
                    f"{rel}: generated page retained cross-tree path: {generated_target}"
                )

        if not source.startswith("# "):
            errors.append(f"{rel}: missing h1")
        if LEGACY_BADGE.search(source):
            errors.append(f"{rel}: legacy audit/provenance badge remains")
        for term in REQUIRED[path.stem]:
            if term not in source:
                errors.append(f"{rel}: required mechanism absent: {term}")
        opened = None
        for lineno, line in enumerate(source.splitlines(), 1):
            m = FENCE.match(line)
            if not m:
                continue
            kind = m.group(1)
            if opened is None:
                opened = (kind, lineno)
            elif opened[0] == kind:
                opened = None
            else:
                errors.append(f"{rel}:{lineno}: crossed code fences")
        if opened is not None:
            errors.append(f"{rel}:{opened[1]}: unclosed code fence")

        for m in MARKDOWN_LINK.finditer(source):
            link = m.group(1)
            if link.startswith(("http:", "https:", "/")):
                continue
            target = (path.parent / link).resolve()
            # Articles and guides are copied to the same docs/generated/lcm
            # directory. Allow unique co-located output slugs when source
            # locations still live under separate content trees.
            if not target.is_file() and "/" not in link and "\\" not in link:
                alternatives = [page for page in PAGES if page.name == link]
                if len(alternatives) == 1:
                    target = alternatives[0]
            link_count += 1
            if not target.is_file():
                errors.append(f"{rel}: missing linked file: {link}")
            elif target not in known:
                # A link to another project can be legitimate, but flag unexpected
                # content references for explicit editorial review.
                if not target.is_relative_to(ROOT / "content"):
                    errors.append(f"{rel}: link leaves content tree: {link}")

    try:
        order = article_order()
        if set(order) != set(ARTICLE_NAMES) or len(order) != len(ARTICLE_NAMES):
            errors.append(f"LCM navigation does not contain ten unique articles: {order}")
        if order[:2] != ("overview", "foundations"):
            errors.append("LCM must introduce foundations after the user-facing overview")
    except (SyntaxError, OSError, RuntimeError, ValueError) as exc:
        errors.append(f"navigation: {exc}")
    try:
        guides = guide_order()
        if set(guides) != set(GUIDE_NAMES) or len(guides) != len(GUIDE_NAMES):
            errors.append(f"LCM navigation does not contain five unique guides: {guides}")
        if guides[1:4] != ("use-pubsub-types", "closed-loop-project", "use-operations"):
            errors.append(
                "LCM guide path must move from typed pub/sub to the full project "
                "before operations"
            )
    except (SyntaxError, OSError, RuntimeError, ValueError) as exc:
        errors.append(f"guide navigation: {exc}")

    examples_present = 0
    for example in EXAMPLE_FILES:
        if not example.is_file():
            errors.append(f"missing reproducible LCM example: {example.relative_to(ROOT)}")
            continue
        examples_present += 1
    if examples_present == len(EXAMPLE_FILES):
        schema = EXAMPLE_FILES[1].read_text(encoding="utf-8")
        sender = EXAMPLE_FILES[2].read_text(encoding="utf-8")
        receiver = EXAMPLE_FILES[3].read_text(encoding="utf-8")
        if "int64_t sequence" not in schema or "joint_count" not in schema:
            errors.append("LCM example lacks sequence or joint-count validation fields")
        if 'publish("ATLAS_JOINT_STATE"' not in sender:
            errors.append("LCM example sender does not publish expected channel")
        if "handler.received != 20" not in receiver or "handler.gaps != 0" not in receiver:
            errors.append("LCM example receiver must fail on incomplete delivery")
        project_source = PROJECT_GUIDE.read_text(encoding="utf-8-sig")
        for example in EXAMPLE_FILES:
            snippet = example.read_text(encoding="utf-8").strip()
            if snippet not in project_source:
                errors.append(
                    "LCM project guide drifted from example file: "
                    f"{example.relative_to(ROOT)}"
                )

    print(f"LCM_STATIC_PAGES={source_count}/{len(PAGES)}")
    print(f"LCM_LOCAL_LINKS_CHECKED={link_count}")
    print(f"LCM_EXAMPLE_ASSETS={examples_present}/{len(EXAMPLE_FILES)}")
    print(f"LCM_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("LCM_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
