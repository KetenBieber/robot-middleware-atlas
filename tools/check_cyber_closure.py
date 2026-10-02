"""Static closure checks for public Cyber RT pages and tutorial assets.

This verifies documentation structure, same-project links, guide navigation,
reproducible example assets, and source-code synchronization between the
published closed-loop project guide and examples/cyber/closed_loop.

It does not claim that the Apollo Bazel workspace has been built locally.
Run tools/check_cpp_examples.py separately for dependency-free C++ experiments.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from build_sphinx_sources import rewrite_generated_project_links

ROOT = Path(__file__).resolve().parents[1]

ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "dag-to-component",
    "node-reader-writer",
    "pending-queue-ring",
    "dispatcher-notifier",
    "multi-input-fusion",
    "registry-publication-quiescence",
    "croutine-wakeup",
    "processor-context-switch",
    "message-to-proc",
    "class-loader-abi",
    "cpp-type-runtime",
    "cpp-implementation-lab",
    "design-recap",
)
GUIDE_NAMES = (
    "use-environment",
    "use-pubsub",
    "closed-loop-project",
    "use-component-operations",
    "case-study-apollo-planning",
)
PAGES = tuple(
    ROOT / "content" / "articles" / "cyber" / (name + ".md")
    for name in ARTICLE_NAMES
) + tuple(
    ROOT / "content" / "guides" / "cyber" / (name + ".md")
    for name in GUIDE_NAMES
)

EXAMPLE_FILES = (
    ROOT / "examples/cyber/closed_loop/BUILD",
    ROOT / "examples/cyber/closed_loop/status.proto",
    ROOT / "examples/cyber/closed_loop/status_transform_component.h",
    ROOT / "examples/cyber/closed_loop/status_transform_component.cc",
    ROOT / "examples/cyber/closed_loop/status_source.cc",
    ROOT / "examples/cyber/closed_loop/status_observer.cc",
    ROOT / "examples/cyber/closed_loop/status_transform.dag",
)
PROJECT_GUIDE = ROOT / "content/guides/cyber/closed-loop-project.md"

LEGACY_BADGE = re.compile(
    r"^(?:仓库与提交：|符号：|对应的上游实现如下：|"
    r"\*\*(?:代码身份|图示身份|固定源码摘录))",
    re.MULTILINE,
)
MARKDOWN_LINK = re.compile(r"\]\(([^)]+\.md)(?:#[^)]*)?\)")
FENCE = re.compile(r"^\s*(```|~~~)")

REQUIRED = {
    "overview": ("Node", "DataVisitor", "Processor", "Proc"),
    "architecture-map": ("DataDispatcher", "DataNotifier", "CRoutine"),
    "dag-to-component": ("ModuleController", "ComponentBase", "Initialize"),
    "node-reader-writer": ("CreateReader", "CreateWriter", "Receiver"),
    "pending-queue-ring": ("CacheBuffer", "ChannelBuffer", "pending_queue_size"),
    "dispatcher-notifier": ("DataDispatcher", "DataNotifier", "weak_ptr"),
    "multi-input-fusion": ("AllLatest", "SetFusionCallback", "M0"),
    "registry-publication-quiescence": ("AtomicHashMap", "publication", "quiescence", "RemoveCRoutine", "SetFusionCallback"),
    "croutine-wakeup": ("NotifyProcessor", "DATA_WAIT", "state_"),
    "processor-context-switch": ("Processor::Run", "Resume", "Context"),
    "message-to-proc": ("Receiver", "Dispatcher", "Process", "Proc"),
    "class-loader-abi": ("ClassLoader", "factory", "ComponentBase"),
    "cpp-type-runtime": ("shared_ptr", "weak_ptr", "override"),
    "cpp-implementation-lab": ("mini_node.cc", "condition_variable", "join"),
    "design-recap": ("shutdown", "WCET", "registry"),
    "use-environment": ("setup.bash", "CYBER_IP"),
    "use-pubsub": ("CreateWriter", "CreateReader", "pending_queue_size"),
    "closed-loop-project": ("StatusTransformComponent", "status_source", "status_observer"),
    "use-component-operations": ("DAG", "CYBER_REGISTER_COMPONENT", "mainboard"),
    "case-study-apollo-planning": ("PlanningComponent", "LocalView", "ADCTrajectory"),
}


def literal_assignment(name: str):
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def main() -> int:
    errors: list[str] = []
    link_count = 0
    page_count = 0
    known = set(PAGES)

    for path in PAGES:
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        page_count += 1
        rel = path.relative_to(ROOT)
        source = path.read_text(encoding="utf-8-sig")
        generated = rewrite_generated_project_links(source, "cyber")

        if not source.startswith("# "):
            errors.append(f"{rel}: missing h1")
        if LEGACY_BADGE.search(source):
            errors.append(f"{rel}: legacy audit/provenance badge remains")
        for term in REQUIRED[path.stem]:
            if term not in source:
                errors.append(f"{rel}: required mechanism absent: {term}")

        opened = None
        for lineno, line in enumerate(source.splitlines(), 1):
            match = FENCE.match(line)
            if match is None:
                continue
            kind = match.group(1)
            if opened is None:
                opened = (kind, lineno)
            elif opened[0] == kind:
                opened = None
            else:
                errors.append(f"{rel}:{lineno}: crossed code fences")
        if opened is not None:
            errors.append(f"{rel}:{opened[1]}: unclosed code fence")

        for match in MARKDOWN_LINK.finditer(generated):
            generated_target = match.group(1)
            if generated_target.startswith(("../../articles/cyber/", "../../guides/cyber/")):
                errors.append(
                    f"{rel}: generated page retained cross-tree path: {generated_target}"
                )

        for match in MARKDOWN_LINK.finditer(source):
            link = match.group(1)
            if link.startswith(("http:", "https:", "/")):
                continue
            target = (path.parent / link).resolve()
            if not target.is_file() and "/" not in link and "\\" not in link:
                alternatives = [page for page in PAGES if page.name == link]
                if len(alternatives) == 1:
                    target = alternatives[0]
            link_count += 1
            if not target.is_file():
                errors.append(f"{rel}: missing linked file: {link}")
            elif target not in known and not target.is_relative_to(ROOT / "content"):
                errors.append(f"{rel}: link leaves content tree: {link}")

    try:
        sections = literal_assignment("CYBER_ARTICLE_SECTIONS")
        flattened = [slug for _, slugs in sections for slug in slugs]
        expected = [name for name in ARTICLE_NAMES if name != "overview"]
        if flattened != expected:
            errors.append(f"Cyber article navigation mismatch: {flattened}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as error:
        errors.append(f"article navigation: {error}")

    try:
        guides = tuple(literal_assignment("GUIDE_ORDER")["cyber"])
        if guides != GUIDE_NAMES:
            errors.append(f"Cyber guide navigation mismatch: {guides}")
        if guides[1:4] != (
            "use-pubsub",
            "closed-loop-project",
            "use-component-operations",
        ):
            errors.append(
                "Cyber guides must move from pub/sub to the closed-loop project "
                "before component operations"
            )
    except (OSError, SyntaxError, ValueError, RuntimeError) as error:
        errors.append(f"guide navigation: {error}")

    example_count = 0
    for example in EXAMPLE_FILES:
        if not example.is_file():
            errors.append(
                f"missing Cyber closed-loop asset: {example.relative_to(ROOT)}"
            )
        else:
            example_count += 1

    if example_count == len(EXAMPLE_FILES) and PROJECT_GUIDE.is_file():
        project_source = PROJECT_GUIDE.read_text(encoding="utf-8-sig")
        for example in EXAMPLE_FILES:
            snippet = example.read_text(encoding="utf-8").strip()
            if snippet not in project_source:
                errors.append(
                    "Cyber project guide drifted from example file: "
                    f"{example.relative_to(ROOT)}"
                )
        source = EXAMPLE_FILES[4].read_text(encoding="utf-8")
        observer = EXAMPLE_FILES[5].read_text(encoding="utf-8")
        component = EXAMPLE_FILES[3].read_text(encoding="utf-8")
        if "CreateWriter<atlas::cyber_demo::Status>" not in source or "/atlas/status/raw" not in source:
            errors.append("Cyber source does not publish the expected input channel")
        if "CreateWriter<atlas::cyber_demo::Status>" not in component or "/atlas/status/processed" not in component:
            errors.append("Cyber component does not publish the expected output channel")
        if "observer.received() != 20" not in observer or "observer.gaps() != 0" not in observer:
            errors.append("Cyber observer must fail incomplete delivery")
    elif not PROJECT_GUIDE.is_file():
        errors.append("missing Cyber closed-loop project guide")

    print(f"CYBER_STATIC_PAGES={page_count}/{len(PAGES)}")
    print(f"CYBER_LOCAL_LINKS_CHECKED={link_count}")
    print(f"CYBER_EXAMPLE_ASSETS={example_count}/{len(EXAMPLE_FILES)}")
    print(f"CYBER_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("CYBER_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
