"""Static closure checks for the Cyclone DDS source course and real-world cases."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from build_sphinx_sources import rewrite_generated_project_links

ROOT = Path(__file__).resolve().parents[1]
PINNED_CYCLONE = "e54e991f75a3e67f8e628da3171122e36ea5b872"
PINNED_RMW = "19478b0a9aa523af62023812d05bfcb4295e8efb"

ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "entity-lifecycle",
    "discovery-spdp-sedp",
    "qos-matching",
    "writer-reader-creation",
    "write-path",
    "whc-reliability",
    "rtps-network",
    "receive-reorder",
    "rhc-read-take",
    "waitset-listener",
    "threads-async-close",
    "psmx-loans",
)
GUIDE_NAMES = (
    "case-study-ddsperf",
    "case-study-rmw-cyclonedds",
)

PAGES = tuple(
    ROOT / "content" / "articles" / "cyclonedds" / (name + ".md")
    for name in ARTICLE_NAMES
) + tuple(
    ROOT / "content" / "guides" / "cyclonedds" / (name + ".md")
    for name in GUIDE_NAMES
)

REQUIRED = {
    "overview": ("DDSc", "DDSI", "Writer History Cache", "WaitSet"),
    "architecture-map": ("ddsi_writer", "proxy_writer", "ddsi_domaingv"),
    "entity-lifecycle": ("AVL", "pin", "dds_delete"),
    "discovery-spdp-sedp": ("SPDP", "SEDP", "proxy_writer"),
    "qos-matching": ("Reliability", "Durability", "Deadline"),
    "writer-reader-creation": ("ddsi_new_writer", "ddsi_new_reader", "RHC"),
    "write-path": ("dds_write", "serdata", "ddsi_write_sample_gc"),
    "whc-reliability": ("WHC", "ACKNACK", "HEARTBEAT"),
    "rtps-network": ("xmsg", "xpack", "ddsrt_sendmsg"),
    "receive-reorder": ("recvmsg", "defrag", "reorder"),
    "rhc-read-take": ("dds_rhc_default", "instances", "circular"),
    "waitset-listener": ("dds_waitset_wait", "condition variable", "observer"),
    "threads-async-close": ("SENDQ_MAX", "sendq", "ddsi_start"),
    "psmx-loans": ("PSMX", "loan", "DDS_PSMX_FEATURE_SHARED_MEMORY"),
    "case-study-ddsperf": ("ddsperf", "writer_batching", "dds_waitset_wait"),
    "case-study-rmw-cyclonedds": ("rmw_publish", "dds_write_ts", "dds_waitset_wait"),
}

FENCE = re.compile(r"^\s*~~~")


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

    for path in PAGES:
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue

        count += 1
        source = path.read_text(encoding="utf-8-sig")
        rel = path.relative_to(ROOT)

        if not source.startswith("# "):
            errors.append(f"{rel}: missing h1")
        if path.parent.name == "cyclonedds" and path.parent.parent.name == "articles":
            if PINNED_CYCLONE not in source:
                errors.append(f"{rel}: missing pinned Cyclone commit")
        for term in REQUIRED[path.stem]:
            if term not in source:
                errors.append(f"{rel}: required mechanism absent: {term}")

        opened = None
        for lineno, line in enumerate(source.splitlines(), 1):
            if FENCE.match(line) is None:
                continue
            if opened is None:
                opened = lineno
            else:
                opened = None
        if opened is not None:
            errors.append(f"{rel}:{opened}: unclosed code fence")

        generated = rewrite_generated_project_links(source, "cyclonedds")
        if "../../articles/cyclonedds/" in generated or "../../guides/cyclonedds/" in generated:
            errors.append(f"{rel}: generated page retained cross-tree link")

    rmw_case = ROOT / "content/guides/cyclonedds/case-study-rmw-cyclonedds.md"
    if rmw_case.is_file() and PINNED_RMW not in rmw_case.read_text(encoding="utf-8-sig"):
        errors.append("rmw_cyclonedds case missing pinned RMW commit")

    ddsperf_case = ROOT / "content/guides/cyclonedds/case-study-ddsperf.md"
    if ddsperf_case.is_file() and PINNED_CYCLONE not in ddsperf_case.read_text(encoding="utf-8-sig"):
        errors.append("ddsperf case missing pinned Cyclone commit")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["cyclonedds"])
        if order != ARTICLE_NAMES:
            errors.append(f"Cyclone DDS article navigation mismatch: {order}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"article navigation: {error}")

    try:
        guides = tuple(literal_assignment("GUIDE_ORDER")["cyclonedds"])
        if guides != GUIDE_NAMES:
            errors.append(f"Cyclone DDS guide navigation mismatch: {guides}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"guide navigation: {error}")

    print(f"CYCLONEDDS_STATIC_PAGES={count}/{len(PAGES)}")
    print(f"CYCLONEDDS_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("CYCLONEDDS_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
