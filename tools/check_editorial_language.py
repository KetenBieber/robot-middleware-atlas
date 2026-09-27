from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = [ROOT / "content", ROOT / "docs"]

# These expressions represent internal production/review language.  Natural
# technical uses such as "用户回调" are intentionally not banned.
FORBIDDEN = {
    "agent/reviewer identity": re.compile(r"\b(?:Agent|Reviewer|ChatGPT|Codex)\b", re.I),
    "audit/report framing": re.compile(
        r"reconstruction\s+(?:audit|report|level)|quality\s+gate|"
        r"(?:独立|第三轮|第二轮)?(?:审阅|复审|盲审)(?:结论|报告|状态|范围)?|审计报告",
        re.I,
    ),
    "completion labels": re.compile(
        r"\b(?:PASS|PARTIAL|UNKNOWN|Covered|Excluded)\b|"
        r"重建级别|复建级别|Evidence\s+Level|Audit\s+Status"
    ),
    "evidence labels": re.compile(
        r"(?<![A-Za-z])(?:SOURCE|INFERENCE|PROPOSED|MEASURED)(?![A-Za-z])"
    ),
    "personal request/process": re.compile(
        r"用户提出|用户要求|你的要求|符合.*要求|本轮任务|本次任务|任务进度|当前进度|工作轮次"
    ),
}


def source_files():
    for root in SEARCH_ROOTS:
        if root.is_dir():
            for pattern in ("*.md", "*.rst"):
                for path in root.rglob(pattern):
                    # Generated pages duplicate the source articles.
                    if root == ROOT / "docs" and (ROOT / "docs" / "generated") in path.parents:
                        continue
                    yield path


def main() -> int:
    violations: list[str] = []
    checked = 0
    for path in sorted(set(source_files())):
        checked += 1
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for category, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{number}: {category}: {line.strip()}"
                    )
    print(f"EDITORIAL_FILES={checked}")
    print(f"EDITORIAL_VIOLATIONS={len(violations)}")
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
