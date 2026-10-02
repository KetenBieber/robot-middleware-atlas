"""Static closure checks for public eCAL pages and tutorial assets."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from build_sphinx_sources import rewrite_generated_project_links

ROOT = Path(__file__).resolve().parents[1]

ARTICLE_NAMES = (
    "foundations",
    "architecture-map",
    "registration-soft-state",
    "publisher-discovery-send",
    "shm-memory-protocol",
    "subscriber-delivery",
    "callback-reentrancy-quiescence",
    "global-lifecycle",
    "cpp-design-lab",
    "design-recap",
)
GUIDE_NAMES = (
    "use-environment",
    "use-pubsub",
    "closed-loop-project",
    "use-operations",
    "case-study-mqtt-bridge",
)
PAGES = tuple(
    ROOT / "content" / "articles" / "ecal" / (name + ".md")
    for name in ARTICLE_NAMES
) + tuple(
    ROOT / "content" / "guides" / "ecal" / (name + ".md")
    for name in GUIDE_NAMES
)

EXAMPLE_FILES = (
    ROOT / "examples/ecal/closed_loop/CMakeLists.txt",
    ROOT / "examples/ecal/closed_loop/wire_codec.h",
    ROOT / "examples/ecal/closed_loop/wire_codec_test.cpp",
    ROOT / "examples/ecal/closed_loop/source.cpp",
    ROOT / "examples/ecal/closed_loop/relay.cpp",
    ROOT / "examples/ecal/closed_loop/observer.cpp",
)
PROJECT_GUIDE = ROOT / "content/guides/ecal/closed-loop-project.md"

MARKDOWN_LINK = re.compile(r"\]\(([^)]+\.md)(?:#[^)]*)?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
REQUIRED = {
    "foundations": ("Publisher", "Subscriber", "registration", "SHM"),
    "architecture-map": ("SubGate", "PublisherImpl", "shared_ptr", "SHM"),
    "global-lifecycle": ("Initialize", "Finalize", "CGlobals", "thread"),
    "registration-soft-state": ("CExpirationMap", "Registration", "timeout"),
    "publisher-discovery-send": ("DetermineTransportLayer", "m_payload_buffer", "SHM"),
    "shm-memory-protocol": ("CSyncMemoryFile", "zero-copy", "ACK"),
    "subscriber-delivery": ("CSubGate", "ApplySample", "condition_variable"),
    "callback-reentrancy-quiescence": ("self-deadlock", "quiescence", "in_flight", "m_event_id_callback", "CSubGate", "weak_ptr"),
    "cpp-design-lab": ("shared_ptr", "weak_ptr", "RAII"),
    "design-recap": ("SHM", "Registration", "ownership"),
    "use-environment": ("find_package", "Initialize"),
    "use-pubsub": ("CPublisher", "CSubscriber", "SetReceiveCallback"),
    "closed-loop-project": ("unordered_multimap", "CExpirationMap", "atlas_ecal_relay"),
    "use-operations": ("Protobuf", "Recorder", "replay"),
    "case-study-mqtt-bridge": ("MQTT", "eCAL", "bridge"),
}


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
    page_count = 0
    link_count = 0
    known = set(PAGES)

    for path in PAGES:
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        page_count += 1
        source = path.read_text(encoding="utf-8-sig")
        rel = path.relative_to(ROOT)

        if not source.startswith("# "):
            errors.append(f"{rel}: missing h1")
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

        generated = rewrite_generated_project_links(source, "ecal")
        for match in MARKDOWN_LINK.finditer(generated):
            target = match.group(1)
            if target.startswith(("../../articles/ecal/", "../../guides/ecal/")):
                errors.append(f"{rel}: generated page retained cross-tree link: {target}")

        for match in MARKDOWN_LINK.finditer(source):
            link = match.group(1)
            if link.startswith(("http:", "https:", "/")):
                continue
            target = (path.parent / link).resolve()
            if not target.is_file() and "/" not in link and "\\" not in link:
                candidates = [page for page in PAGES if page.name == link]
                if len(candidates) == 1:
                    target = candidates[0]
            link_count += 1
            if not target.is_file():
                errors.append(f"{rel}: missing linked file: {link}")
            elif target not in known and not target.is_relative_to(ROOT / "content"):
                errors.append(f"{rel}: link leaves content tree: {link}")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["ecal"])
        if order != ARTICLE_NAMES:
            errors.append(f"eCAL article navigation mismatch: {order}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as error:
        errors.append(f"article navigation: {error}")

    try:
        guides = tuple(literal_assignment("GUIDE_ORDER")["ecal"])
        if guides != GUIDE_NAMES:
            errors.append(f"eCAL guide navigation mismatch: {guides}")
        if guides[1:4] != ("use-pubsub", "closed-loop-project", "use-operations"):
            errors.append("eCAL guide path must move from pub/sub through the closed-loop project to operations")
    except (OSError, SyntaxError, ValueError, RuntimeError) as error:
        errors.append(f"guide navigation: {error}")

    example_count = 0
    for example in EXAMPLE_FILES:
        if not example.is_file():
            errors.append(f"missing eCAL example asset: {example.relative_to(ROOT)}")
        else:
            example_count += 1

    if PROJECT_GUIDE.is_file() and example_count == len(EXAMPLE_FILES):
        project = PROJECT_GUIDE.read_text(encoding="utf-8-sig")
        for example in EXAMPLE_FILES:
            snippet = example.read_text(encoding="utf-8").strip()
            if snippet not in project:
                errors.append(
                    "eCAL project guide drifted from example file: "
                    f"{example.relative_to(ROOT)}"
                )

        source = EXAMPLE_FILES[3].read_text(encoding="utf-8")
        relay = EXAMPLE_FILES[4].read_text(encoding="utf-8")
        observer = EXAMPLE_FILES[5].read_text(encoding="utf-8")
        if '"/atlas/raw"' not in source:
            errors.append("eCAL source does not publish /atlas/raw")
        if '"/atlas/raw"' not in relay or '"/atlas/processed"' not in relay:
            errors.append("eCAL relay must bridge raw to processed")
        if "received.load() != 20" not in observer or "gaps.load() != 0" not in observer:
            errors.append("eCAL observer must fail incomplete or gapped delivery")
    elif not PROJECT_GUIDE.is_file():
        errors.append("missing eCAL closed-loop project guide")

    print(f"ECAL_STATIC_PAGES={page_count}/{len(PAGES)}")
    print(f"ECAL_LOCAL_LINKS_CHECKED={link_count}")
    print(f"ECAL_EXAMPLE_ASSETS={example_count}/{len(EXAMPLE_FILES)}")
    print(f"ECAL_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ECAL_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
