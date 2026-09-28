"""Structural closure checks for the SOEM design-reading course."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTICLE_ROOT = ROOT / "content" / "articles" / "soem"
GUIDE_ROOT = ROOT / "content" / "guides" / "soem"

ARTICLE_NAMES = (
    "overview",
    "architecture-map",
    "context-fixed-arrays",
    "linux-raw-socket",
    "frame-buffer-index",
    "datagram-primitives",
    "slave-discovery",
    "pdo-sm-fmmu",
    "iomap-cyclic",
    "mailbox-coe",
    "distributed-clocks",
    "redundancy",
    "realtime-osal",
    "fault-recovery",
    "soem-vs-igh",
    "design-recap",
)

GUIDE_NAMES = (
    "case-study-official-ec-sample",
    "case-study-leggedrobotics-soem-interface",
    "case-study-elfin-robot-ros2",
    "case-study-ipe-ros2-control",
)

PINNED_SOEM = "304d1c05eab77dc0d426f1a5cf09c8cc7dc03713"
LEGGED_PIN = "6e8ab4d62bc9204dcd25454e19abfa9318bfed6f"
ELFIN_PIN = "fddb7d0813ce4bac9a11fd3dd8f9e8828a0926cb"
IPE_PIN = "6a7ee41910d681e01c261f5b50abb69f2a94caaa"

BANNED_PUBLIC = (
    "MockBackend",
    "deterministic-mock",
    "examples/soem/closed_loop",
    "--soem-enter-op",
    "--soem-enable-tutorial-layout",
    "ATLAS_WITH_SOEM",
)

REQUIRED = {
    "architecture-map": ("ecx_contextt", "ecx_portt", "OSAL", "OSHW"),
    "context-fixed-arrays": ("slavelist", "grouplist", "EC_MAXBUF"),
    "linux-raw-socket": ("raw socket", "ecx_setupnic", "ecx_outframe"),
    "frame-buffer-index": ("ecx_getindex", "rxbufstat", "ecx_inframe"),
    "datagram-primitives": ("ecx_APRD", "ecx_srconfirm", "ecx_setupdatagram"),
    "slave-discovery": ("ecx_config_init", "Auto Increment", "slavelist"),
    "pdo-sm-fmmu": ("SyncManager", "FMMU", "IOmap"),
    "iomap-cyclic": ("ecx_send_processdata_group", "ecx_receive_processdata_group", "idxstack"),
    "mailbox-coe": ("ecx_SDOread", "mailbox", "ticket"),
    "distributed-clocks": ("ecx_configdc", "ecx_dcsync0", "DCtime"),
    "redundancy": ("ecx_init_redundant", "ecx_waitinframe_red", "secondary"),
    "realtime-osal": ("CLOCK_MONOTONIC", "SCHED_FIFO", "PTHREAD_PRIO_INHERIT"),
    "fault-recovery": ("expected WKC", "ecx_reconfig_slave", "ecx_recover_slave"),
    "soem-vs-igh": ("IgH", "IOmap", "Domain", "ec_datagram_t"),
    "design-recap": ("Context", "IOmap", "Frame", "WKC"),
    "case-study-official-ec-sample": (
        PINNED_SOEM,
        "ecx_config_map_group",
        "ecx_mbxhandler",
        "ecx_reconfig_slave",
        "ecx_recover_slave",
    ),
    "case-study-leggedrobotics-soem-interface": (
        LEGGED_PIN,
        "EthercatBusBase.cpp",
        "ecx_config_map_group",
        "ecx_send_processdata",
        "ecx_receive_processdata",
        "outputsWKC * 2 + inputsWKC",
    ),
    "case-study-elfin-robot-ros2": (
        ELFIN_PIN,
        "elfin_ethercat_manager.cpp",
        "ec_send_processdata",
        "ec_receive_processdata",
        "ec_reconfig_slave",
        "ec_recover_slave",
        "CLOCK_REALTIME",
    ),
    "case-study-ipe-ros2-control": (
        IPE_PIN,
        "ethercat_common.c",
        "ecat_motor_master.c",
        "ec_send_processdata",
        "ec_receive_processdata",
        "CiA",
        "ros2_control",
    ),
}


def configured(name: str, project: str) -> tuple[str, ...]:
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        value = None
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                value = node.value
        if value is not None:
            mapping = ast.literal_eval(value)
            return tuple(mapping[project])
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def main() -> int:
    errors: list[str] = []
    public_paths = [
        *(ARTICLE_ROOT / f"{name}.md" for name in ARTICLE_NAMES),
        *(GUIDE_ROOT / f"{name}.md" for name in GUIDE_NAMES),
    ]

    present = 0
    for path in public_paths:
        if not path.is_file():
            errors.append(f"missing SOEM page: {path.relative_to(ROOT)}")
            continue

        present += 1
        source = path.read_text(encoding="utf-8-sig")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing H1")

        if path.parent == ARTICLE_ROOT and path.stem != "overview":
            if PINNED_SOEM not in source and "SOEM v2.0.0" not in source:
                errors.append(f"{path.relative_to(ROOT)}: missing SOEM v2.0.0 baseline")

        for term in REQUIRED.get(path.stem, ()):
            if term not in source:
                errors.append(
                    f"{path.relative_to(ROOT)}: required evidence absent: {term}"
                )

        for banned in BANNED_PUBLIC:
            if banned in source:
                errors.append(
                    f"{path.relative_to(ROOT)}: self-demo leaked into public SOEM course: {banned}"
                )

    configured_guides = set(GUIDE_NAMES)
    extra_guides = sorted(
        path.stem for path in GUIDE_ROOT.glob("*.md")
        if path.stem not in configured_guides
    )
    if extra_guides:
        errors.append(f"unreviewed SOEM public guide files remain: {extra_guides}")

    try:
        articles = configured("ARTICLE_ORDER", "soem")
        if articles != ARTICLE_NAMES:
            errors.append(f"SOEM article order drifted: {articles}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
        errors.append(f"SOEM article navigation: {exc}")

    try:
        guides = configured("GUIDE_ORDER", "soem")
        if guides != GUIDE_NAMES:
            errors.append(f"SOEM guide order drifted: {guides}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
        errors.append(f"SOEM guide navigation: {exc}")

    examples = ROOT / "examples" / "soem"
    if examples.exists():
        leaked = [path for path in examples.rglob("*") if path.is_file()]
        if leaked:
            errors.append(
                "self-authored SOEM example files remain public: "
                + ", ".join(str(path.relative_to(ROOT)) for path in leaked)
            )

    # .internal is intentionally local-only and ignored by Git.  When the
    # research index exists, validate it as an additional authoring-time gate;
    # CI/public builds must not require private research material.
    case_index = ROOT / ".internal" / "research" / "soem" / "case-index.md"
    if case_index.is_file():
        case_text = case_index.read_text(encoding="utf-8-sig")
        for term in (
            PINNED_SOEM,
            LEGGED_PIN,
            ELFIN_PIN,
            IPE_PIN,
            "samples/ec_sample/ec_sample.c",
            "EthercatBusBase.cpp",
            "elfin_ethercat_manager.cpp",
            "ethercat_common.c",
            "ipe_three_mode_ros2_control_hardware.cpp",
        ):
            if term not in case_text:
                errors.append(f"SOEM case index missing: {term}")

    print(f"SOEM_PUBLIC_PAGES={present}/{len(public_paths)}")
    print(f"SOEM_ARTICLES={len(ARTICLE_NAMES)}")
    print(f"SOEM_REAL_CASES={len(GUIDE_NAMES)}")
    print("SOEM_SELF_DEMO_FILES=0")
    print(f"SOEM_CLOSURE_ERRORS={len(errors)}")
    for error in errors:
        print("SOEM_CLOSURE_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
