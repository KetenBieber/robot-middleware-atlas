from __future__ import annotations

import sys
from html.parser import HTMLParser
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


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.targets: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = "href" if tag in {"a", "link"} else "src" if tag in {"img", "script"} else None
        if attribute is None:
            return
        value = dict(attrs).get(attribute)
        if value:
            self.targets.append((tag, value))


def iter_html(root: Path):
    yield from sorted(path for path in root.rglob("*.html") if path.is_file())


def local_target(source: Path, raw: str) -> Path | None:
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    target = (source.parent / unquote(parsed.path)).resolve()
    if raw.endswith("/"):
        target /= "index.html"
    return target


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    if not root.is_dir():
        print(f"site directory does not exist: {root}", file=sys.stderr)
        return 2

    html_files = list(iter_html(root))
    missing: list[tuple[Path, str, Path]] = []
    forbidden: list[Path] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        normalized = relative.as_posix().lower()
        if any(part in normalized for part in FORBIDDEN_ARTIFACT_PARTS):
            forbidden.append(relative)
    checked = 0
    for source in html_files:
        parser = LinkParser()
        parser.feed(source.read_text(encoding="utf-8"))
        for _tag, raw in parser.targets:
            target = local_target(source, raw)
            if target is None:
                continue
            checked += 1
            if not target.exists():
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
