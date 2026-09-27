"""Compile and execute standalone C++17 teaching examples from the blog.

Only examples documented as complete, dependency-free translation units belong
here. Partial fixed-upstream source excerpts are deliberately excluded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {
    "content/articles/lcm/provider-vtable.md": "### 先写一个能运行的 C++17 缩小版",
    "content/articles/yarp/cpp-design-lab.md": "## 从 `M × C` 个专用函数走向两条动态分派边界",
    "content/articles/ecal/cpp-design-lab.md": "## 用一个可运行的弱句柄实验建立所有权模型",
}


def extract(path: Path, section: str) -> str:
    document = path.read_text(encoding="utf-8-sig")
    offset = document.find(section + "\n")
    if offset < 0:
        raise ValueError(f"missing example section {section}: {path}")
    section_start = offset + len(section) + 1
    rest = document[section_start:]
    match = re.search(r"^~~~cpp\s*\n(.*?)^~~~\s*$", rest, re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError(f"missing standalone C++ fence: {path}")
    return match.group(1)


def main() -> int:
    compiler = shutil.which("g++")
    if not compiler:
        print("CPP_EXAMPLES_ERROR=g++ not available")
        return 1

    failures: list[str] = []
    for index, (relative, section) in enumerate(EXAMPLES.items()):
        path = ROOT / relative
        try:
            source = extract(path, section)
            with tempfile.TemporaryDirectory(prefix="atlas-cxx-") as directory:
                executable = Path(directory) / ("teaching.exe" if sys.platform == "win32" else "teaching")
                command = [
                    compiler, "-std=c++17", "-O0", "-Wall", "-Wextra",
                    "-Werror", "-pedantic", "-x", "c++", "-", "-o", str(executable),
                ]
                result = subprocess.run(
                    command, input=source, text=True, capture_output=True, timeout=35,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"compile returned {result.returncode}:\n{result.stderr}")
                result = subprocess.run(
                    [str(executable)], text=True, capture_output=True, timeout=10,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"program exited {result.returncode}: {result.stdout}\n{result.stderr}"
                    )
            print(f"CPP_EXAMPLE_PASS={index + 1}:{relative}")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            failures.append(f"{relative}: {error}")
    print(f"CPP_EXAMPLES_TESTED={len(EXAMPLES)}")
    print(f"CPP_EXAMPLES_FAILED={len(failures)}")
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
