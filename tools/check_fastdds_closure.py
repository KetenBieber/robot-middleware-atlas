"""Static closure checks for the Fast DDS source course and real-world cases."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from build_sphinx_sources import rewrite_generated_project_links

ROOT = Path(__file__).resolve().parents[1]
PINNED_FASTDDS = "39303846fb8534ef69fa65f9fa4bcc9e6a7c995a"
PINNED_RMW = "a88ce42dc66203a9b617a05daf0f5019870fc3c8"

ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "participant-endpoint-lifecycle",
    "discovery-pdp-edp",
    "discovery-server",
    "qos-matching",
    "writer-reader-creation",
    "write-cachechange",
    "writerhistory-reliability",
    "readerhistory-fragments",
    "transport-network",
    "flowcontroller-async",
    "datasharing-vs-shm",
    "loan-zero-copy",
    "waitset-listener",
    "threads-events-close",
    "fastdds-vs-cyclonedds",
)
GUIDE_NAMES = (
    "case-study-delivery-mechanisms",
    "case-study-rmw-fastrtps",
)

PAGES = tuple(
    ROOT / "content" / "articles" / "fastdds" / (name + ".md")
    for name in ARTICLE_NAMES
) + tuple(
    ROOT / "content" / "guides" / "fastdds" / (name + ".md")
    for name in GUIDE_NAMES
)

REQUIRED = {
    "overview": ("DataWriterImpl", "CacheChange", "Data Sharing", "WaitSet"),
    "architecture-map": ("RTPSParticipantImpl", "WriterHistory", "ReaderProxy", "NetworkFactory"),
    "participant-endpoint-lifecycle": ("RecursiveTimedMutex", "RTPSDomain::createParticipant", "removeRTPSWriter"),
    "discovery-pdp-edp": ("PDP", "EDPSimple", "WriterProxyData"),
    "discovery-server": ("PDPClient", "PDPServer", "Discovery Server"),
    "qos-matching": ("max_blocking_time", "History", "Data Sharing"),
    "writer-reader-creation": ("DataWriterImpl", "DataReaderImpl", "IPayloadPool"),
    "write-cachechange": ("perform_create_new_change", "SerializedPayload_t", "add_pub_change"),
    "writerhistory-reliability": ("WriterHistory", "ReaderProxy", "Heartbeat"),
    "readerhistory-fragments": ("WriterProxy", "DATAFRAG", "completed_change"),
    "transport-network": ("ReceiverResource", "MessageReceiver", "SharedMemTransport"),
    "flowcontroller-async": ("std::unordered_map", "std::map", "max_bytes_per_period"),
    "datasharing-vs-shm": ("SharedMemTransportDescriptor", "data_sharing", "DataSharingPayloadPool"),
    "loan-zero-copy": ("loan_sample", "check_and_remove_loan", "is_plain_ctx"),
    "waitset-listener": ("WaitSetImpl", "Notifier", "condition_variable"),
    "threads-events-close": ("ResourceEvent", "TimedEvent", "m_network_Factory.Shutdown"),
    "fastdds-vs-cyclonedds": ("Cyclone DDS", "CacheChange_t", "FlowController"),
    "case-study-delivery-mechanisms": ("delivery_mechanisms", "SharedMemTransportDescriptor", "loan_sample"),
    "case-study-rmw-fastrtps": ("rmw_publish", "write_w_timestamp", "rmw_wait", "Fast DDS WaitSet"),
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
        if path.parent.name == "fastdds" and path.parent.parent.name == "articles":
            if PINNED_FASTDDS not in source:
                errors.append(f"{rel}: missing pinned Fast DDS commit")

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

        generated = rewrite_generated_project_links(source, "fastdds")
        if "../../articles/fastdds/" in generated or "../../guides/fastdds/" in generated:
            errors.append(f"{rel}: generated page retained cross-tree link")

    rmw_case = ROOT / "content/guides/fastdds/case-study-rmw-fastrtps.md"
    if rmw_case.is_file() and PINNED_RMW not in rmw_case.read_text(encoding="utf-8-sig"):
        errors.append("rmw_fastrtps case missing pinned RMW commit")

    official_case = ROOT / "content/guides/fastdds/case-study-delivery-mechanisms.md"
    if official_case.is_file() and PINNED_FASTDDS not in official_case.read_text(encoding="utf-8-sig"):
        errors.append("delivery_mechanisms case missing pinned Fast DDS commit")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["fastdds"])
        if order != ARTICLE_NAMES:
            errors.append(f"Fast DDS article navigation mismatch: {order}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"article navigation: {error}")

    try:
        guides = tuple(literal_assignment("GUIDE_ORDER")["fastdds"])
        if guides != GUIDE_NAMES:
            errors.append(f"Fast DDS guide navigation mismatch: {guides}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"guide navigation: {error}")

    examples = ROOT / "examples" / "fastdds"
    if examples.exists():
        leaked = [path for path in examples.rglob("*") if path.is_file()]
        if leaked:
            errors.append(
                "self-authored Fast DDS example files remain public: "
                + ", ".join(str(path.relative_to(ROOT)) for path in leaked)
            )

    print(f"FASTDDS_STATIC_PAGES={count}/{len(PAGES)}")
    print(f"FASTDDS_ARTICLES={len(ARTICLE_NAMES)}")
    print(f"FASTDDS_REAL_CASES={len(GUIDE_NAMES)}")
    print(f"FASTDDS_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("FASTDDS_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
