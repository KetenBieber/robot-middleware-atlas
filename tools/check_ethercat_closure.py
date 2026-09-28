"""Static closure checks for the EtherCAT theory/source/practice topic.

This validates public pages, navigation, and source-to-guide synchronization.
It does not claim that libfakeethercat, RtIPC, a kernel master, a NIC, or a
physical slave ran on the current host.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ARTICLE_NAMES = (
    "overview",
    "theory-stack",
    "theory-frame-datagram",
    "theory-pdo-process-image",
    "theory-state-mailbox",
    "theory-distributed-clocks",
    "theory-realtime",
    "architecture-map",
    "master-lifecycle",
    "domain-process-image",
    "cyclic-send-receive",
    "datagram-frame",
    "slave-fsm-mailbox",
    "device-nic-runtime",
    "distributed-clocks",
    "realtime-concurrency",
    "design-recap",
)

GUIDE_NAMES = (
    "use-environment",
    "closed-loop-project",
    "real-hardware-deployment",
)

ARTICLE_ROOT = ROOT / "content" / "articles" / "ethercat"
GUIDE_ROOT = ROOT / "content" / "guides" / "ethercat"
PROJECT_GUIDE = GUIDE_ROOT / "closed-loop-project.md"

EXAMPLE_FILES = (
    ROOT / "examples/ethercat/closed_loop/CMakeLists.txt",
    ROOT / "examples/ethercat/closed_loop/include/virtual_servo.h",
    ROOT / "examples/ethercat/closed_loop/src/controller.c",
    ROOT / "examples/ethercat/closed_loop/src/plant_sim.c",
    ROOT / "examples/ethercat/closed_loop/scripts/run_fake.sh",
)

REQUIRED = {
    "theory-stack": ("PDO", "FMMU", "Datagram", "NIC"),
    "theory-frame-datagram": ("Working Counter", "LRW", "EtherCAT"),
    "theory-pdo-process-image": ("SyncManager", "FMMU", "process image"),
    "theory-state-mailbox": ("PREOP", "Mailbox", "CoE"),
    "theory-distributed-clocks": ("Sync0", "reference", "传播"),
    "theory-realtime": ("jitter", "data age", "1 kHz"),
    "architecture-map": ("ioctl", "mmap", "ec_master"),
    "master-lifecycle": ("ecrt_request_master", "ecrt_master_activate", "kmalloc"),
    "domain-process-image": ("ecrt_domain_reg_pdo_entry_list", "FMMU", "offset"),
    "cyclic-send-receive": ("ecrt_master_receive", "ecrt_domain_process", "ecrt_master_send"),
    "datagram-frame": ("ec_master_queue_datagram", "EC_DATAGRAM_QUEUED", "frame"),
    "slave-fsm-mailbox": ("ec_fsm_master_exec", "state", "SDO"),
    "device-nic-runtime": ("net_device", "sk_buff", "ndo_start_xmit"),
    "distributed-clocks": ("ecrt_master_application_time", "dc_ref_clock", "ecrt_master_sync_slave_clocks"),
    "realtime-concurrency": ("injection_seq_fsm", "smp_load_acquire", "Operation"),
    "design-recap": ("Process Image", "Datagram", "Device", "FSM"),
    "use-environment": ("libfakeethercat", "RtIPC", "LD_LIBRARY_PATH"),
    "closed-loop-project": ("atlas_ethercat_controller", "atlas_ethercat_plant", "TIMER_ABSTIME"),
    "real-hardware-deployment": ("MASTER0_DEVICE", "DEVICE_MODULES", "Working Counter"),
}

FENCE = re.compile(r"^\s*(``|~~~)")
FENCE = re.compile(r"^\\s*(" + re.escape(chr(96) * 3) + r"|~~~)")

def configured(name: str, project: str) -> tuple[str, ...]:
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        value = None
        target_name = None
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                value = node.value
                target_name = name
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                value = node.value
                target_name = name
        if target_name and value is not None:
            mapping = ast.literal_eval(value)
            return tuple(mapping[project])
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def check_fences(path: Path, source: str, errors: list[str]) -> None:
    opened: tuple[str, int] | None = None
    for lineno, line in enumerate(source.splitlines(), 1):
        match = FENCE.match(line)
        if not match:
            continue
        kind = match.group(1)
        if opened is None:
            opened = (kind, lineno)
        elif opened[0] == kind:
            opened = None
        else:
            errors.append(f"{path.relative_to(ROOT)}:{lineno}: crossed code fences")
    if opened is not None:
        errors.append(f"{path.relative_to(ROOT)}:{opened[1]}: unclosed code fence")


def main() -> int:
    errors: list[str] = []
    pages = [
        *(ARTICLE_ROOT / f"{name}.md" for name in ARTICLE_NAMES),
        *(GUIDE_ROOT / f"{name}.md" for name in GUIDE_NAMES),
    ]

    present = 0
    for path in pages:
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue
        present += 1
        source = path.read_text(encoding="utf-8-sig")
        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        for term in REQUIRED.get(path.stem, ()):
            if term not in source:
                errors.append(f"{path.relative_to(ROOT)}: required mechanism absent: {term}")
        check_fences(path, source, errors)

    try:
        articles = configured("ARTICLE_ORDER", "ethercat")
        if articles != ARTICLE_NAMES:
            errors.append(f"EtherCAT article order drifted: {articles}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
        errors.append(f"article navigation: {exc}")

    try:
        guides = configured("GUIDE_ORDER", "ethercat")
        if guides != GUIDE_NAMES:
            errors.append(f"EtherCAT guide order drifted: {guides}")
    except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
        errors.append(f"guide navigation: {exc}")

    assets = 0
    for path in EXAMPLE_FILES:
        if not path.is_file():
            errors.append(f"missing EtherCAT example asset: {path.relative_to(ROOT)}")
            continue
        assets += 1

    if assets == len(EXAMPLE_FILES) and PROJECT_GUIDE.is_file():
        guide = PROJECT_GUIDE.read_text(encoding="utf-8-sig")
        for path in EXAMPLE_FILES:
            snippet = path.read_text(encoding="utf-8").strip()
            if snippet not in guide:
                errors.append(
                    "EtherCAT project guide drifted from example file: "
                    f"{path.relative_to(ROOT)}"
                )

        header = EXAMPLE_FILES[1].read_text(encoding="utf-8")
        controller = EXAMPLE_FILES[2].read_text(encoding="utf-8")
        plant = EXAMPLE_FILES[3].read_text(encoding="utf-8")
        runner = EXAMPLE_FILES[4].read_text(encoding="utf-8")

        if "atlas_controller_syncs" not in header or "atlas_plant_syncs" not in header:
            errors.append("EtherCAT example lacks paired controller/plant SyncManager maps")
        if "EC_DIR_OUTPUT" not in header or "EC_DIR_INPUT" not in header:
            errors.append("EtherCAT example does not expose PDO direction inversion")
        for call in (
            "ecrt_master_receive",
            "ecrt_domain_process",
            "ecrt_domain_queue",
            "ecrt_master_send",
        ):
            if call not in controller or call not in plant:
                errors.append(f"EtherCAT example missing cyclic call on both sides: {call}")
        if "TIMER_ABSTIME" not in controller or "TIMER_ABSTIME" not in plant:
            errors.append("EtherCAT example must use absolute-time periodic waits")
        if "FAKE_EC_SO" not in runner or "libfakeethercat.so.1" not in runner:
            errors.append("EtherCAT fake runner must explicitly redirect to libfakeethercat")

    print(f"ETHERCAT_STATIC_PAGES={present}/{len(pages)}")
    print(f"ETHERCAT_EXAMPLE_ASSETS={assets}/{len(EXAMPLE_FILES)}")
    print(f"ETHERCAT_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ETHERCAT_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
