"""Structural regression gate for the pinned middleware and EtherCAT master courses.

This verifies the editorial source tree, not the correctness of upstream code.
Run deep source review separately before claiming that a mechanism is verified.
"""

from __future__ import annotations

from pathlib import Path

from build_sphinx_sources import ARTICLE_ORDER, GUIDE_ORDER, PENDING_SOURCE, PROJECTS


ROOT = Path(__file__).resolve().parents[1]
ARTICLES = ROOT / "content" / "articles"
GUIDES = ROOT / "content" / "guides"


def main() -> int:
    problems: list[str] = []
    articles_checked = 0
    guides_checked = 0

    for project, (_name, upstream_commit) in PROJECTS.items():
        article_dir = ARTICLES / project
        guide_dir = GUIDES / project
        expected_articles = set(ARTICLE_ORDER[project])
        expected_guides = set(GUIDE_ORDER[project])

        for project_dir, expected, kind in (
            (article_dir, expected_articles, "article"),
            (guide_dir, expected_guides, "guide"),
        ):
            existing = {p.stem for p in project_dir.glob("*.md")}
            for slug in sorted(expected - existing):
                problems.append(f"MISSING {project}/{kind}/{slug}")
            for slug in sorted(existing - expected):
                problems.append(f"UNLISTED {project}/{kind}/{slug}")

        for slug in sorted(expected_articles):
            path = article_dir / f"{slug}.md"
            if not path.is_file():
                continue
            articles_checked += 1
            content = path.read_text(encoding="utf-8-sig")
            if not content.startswith("# "):
                problems.append(f"MISSING_H1 {path.relative_to(ROOT)}")
            if upstream_commit != PENDING_SOURCE and upstream_commit[:8] not in content:
                problems.append(f"MISSING_UPSTREAM_COMMIT {path.relative_to(ROOT)}")
            if "\n## " not in content:
                problems.append(f"MISSING_SECTION {path.relative_to(ROOT)}")

        guides_checked += sum(
            (guide_dir / f"{slug}.md").is_file() for slug in expected_guides
        )

    print(f"BASELINE_PROJECTS={len(PROJECTS)}")
    print(f"BASELINE_ARTICLES={articles_checked}")
    print(f"BASELINE_GUIDES={guides_checked}")
    print(f"BASELINE_PROBLEMS={len(problems)}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
