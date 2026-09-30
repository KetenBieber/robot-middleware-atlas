from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


FORBIDDEN_ARTIFACT_PARTS = {
    ".internal",
    ".qa-debug",
    "legacy_custom_frontend",
    "legacy_previous_build",
    "reconstruction_audit",
    "sphinx_reconstruction_review",
    "test_results.md",
    "ecal_pubsub_spec",
    "lcm_compat",
    "zenoh_spec",
}


TARGET_ATTRIBUTE = re.compile(
    r"""<(?:a|link|img|script)\b[^>]*?\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE | re.DOTALL,
)


def iter_targets(source_text: str):
    for match in TARGET_ATTRIBUTE.finditer(source_text):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw:
            yield raw


def local_target(source_relative: str, raw: str) -> str | None:
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None

    decoded = unquote(parsed.path).replace("\\", "/")
    if decoded.startswith("/"):
        # Preserve the old checker's behavior for root-absolute links: they
        # point outside the generated site tree and therefore cannot match a
        # site-local path.
        return f"__outside_site__{decoded}"

    source_parent = posixpath.dirname(source_relative)
    target = posixpath.normpath(posixpath.join(source_parent, decoded))
    if raw.endswith("/"):
        target = posixpath.join(target, "index.html")
    return target


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    if not root.is_dir():
        print(f"site directory does not exist: {root}", file=sys.stderr)
        return 2

    # Scan the tree once.  The old implementation called resolve()+exists()
    # for every local href/src; Sphinx pages repeat navigation links heavily,
    # causing hundreds of thousands of filesystem lookups on Windows.
    entries = sorted(root.rglob("*"))
    files = [path for path in entries if path.is_file()]
    html_files = [path for path in files if path.suffix.lower() == ".html"]
    existing = {
        path.relative_to(root).as_posix()
        for path in entries
    }

    missing: list[tuple[Path, str, str]] = []
    forbidden: list[Path] = []
    for path in files:
        relative = path.relative_to(root)
        normalized = relative.as_posix().lower()
        if any(part in normalized for part in FORBIDDEN_ARTIFACT_PARTS):
            forbidden.append(relative)

    checked = 0
    for source in html_files:
        source_relative = source.relative_to(root).as_posix()
        source_text = source.read_text(encoding="utf-8")
        for raw in iter_targets(source_text):
            target = local_target(source_relative, raw)
            if target is None:
                continue
            checked += 1
            if target not in existing:
                missing.append((source.relative_to(root), raw, target))

    print(f"HTML_COUNT={len(html_files)}")
    print(f"LOCAL_REFERENCES={checked}")
    print(f"LOCAL_MISSING={len(missing)}")
    print(f"FORBIDDEN_ARTIFACTS={len(forbidden)}")
    for source, raw, target in missing[:50]:
        print(f"MISSING {source}: {raw} -> {target}")
    for path in forbidden[:50]:
        print(f"FORBIDDEN {path}")
    return 1 if missing or forbidden else 0


if __name__ == "__main__":
    raise SystemExit(main())
