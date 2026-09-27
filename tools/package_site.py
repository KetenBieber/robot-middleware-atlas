"""Build a portable ZIP of the already compiled public Sphinx site.

Source checkouts, internal audit records and Sphinx doctrees never belong in
the deployment bundle. Compile and check links before running this script.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DIST = ROOT / "dist"
ARCHIVE = DIST / "robot-middleware-atlas-site.zip"


def main() -> None:
    if not (SITE / "index.html").is_file():
        raise SystemExit("site/index.html not found; compile the website first")

    files = sorted(
        path for path in SITE.rglob("*")
        if path.is_file()
        and ".doctrees" not in path.relative_to(SITE).parts
        and path.name != ".buildinfo"
    )
    pages = sum(path.suffix.lower() == ".html" for path in files)
    if pages == 0:
        raise SystemExit("No compiled HTML pages; refusing to package the site")

    DIST.mkdir(parents=True, exist_ok=True)
    with ZipFile(ARCHIVE, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, arcname=path.relative_to(SITE).as_posix())

    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    (DIST / f"{ARCHIVE.name}.sha256").write_text(
        f"{digest}  {ARCHIVE.name}\n", encoding="utf-8"
    )
    print(f"DEPLOYMENT_ARCHIVE={ARCHIVE}")
    print(f"HTML_PAGES={pages}")
    print(f"PACKAGED_FILES={len(files)}")
    print(f"ARCHIVE_BYTES={ARCHIVE.stat().st_size}")
    print(f"SHA256={digest}")


if __name__ == "__main__":
    main()
