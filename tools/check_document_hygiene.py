"""Regression checks for public documentation structure and retired legacy pages."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RETIRED_TOP_LEVEL = (
    ROOT / "docs" / "industrial.rst",
    ROOT / "docs" / "experimental.rst",
)

RETIRED_RECAPS = (
    ("cyclonedds", "design-recap"),
    ("fastdds", "design-recap"),
    ("iceoryx2", "design-recap"),
    ("ucx", "design-recap"),
)

PROJECTS = (
    "libuv",
    "nginx",
    "cyber",
    "orocos",
    "yarp",
    "ecal",
    "lcm",
    "ros1",
    "rmwzenoh",
    "cyclonedds",
    "fastdds",
    "zenoh",
    "iceoryx2",
    "ucx",
    "rosidlbuffer",
    "holoscan",
    "ethercat",
    "soem",
)


def main() -> int:
    errors: list[str] = []

    for path in RETIRED_TOP_LEVEL:
        if path.exists():
            errors.append(f"retired navigation page returned: {path.relative_to(ROOT)}")

    index = (ROOT / "docs" / "index.rst").read_text(encoding="utf-8-sig")
    for legacy in ("industrial", "experimental"):
        if legacy in index:
            errors.append(f"docs/index.rst still references retired category: {legacy}")

    implementations = ROOT / "docs" / "implementations.rst"
    if not implementations.is_file():
        errors.append("docs/implementations.rst missing")
    else:
        source = implementations.read_text(encoding="utf-8-sig")
        for project in PROJECTS:
            needle = f"generated/{project}/index"
            if needle not in source:
                errors.append(f"implementations navigation missing: {needle}")

    for project, slug in RETIRED_RECAPS:
        source = ROOT / "content" / "articles" / project / f"{slug}.md"
        generated = ROOT / "docs" / "generated" / project / f"{slug}.md"
        if source.exists():
            errors.append(f"retired source article returned: {source.relative_to(ROOT)}")
        if generated.exists():
            errors.append(
                f"stale generated article survived regeneration: {generated.relative_to(ROOT)}"
            )

    print(f"DOC_HYGIENE_PROJECTS={len(PROJECTS)}")
    print(f"DOC_HYGIENE_ERRORS={len(errors)}")
    for error in errors:
        print("DOC_HYGIENE_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
