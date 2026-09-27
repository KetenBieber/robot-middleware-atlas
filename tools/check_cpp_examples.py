"""Compile and execute standalone C11 and C++17 teaching examples from the blog.

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
    "content/articles/lcm/subscription-dispatch.md": "### 可以编译运行的订阅删除实验",
    "content/articles/yarp/cpp-design-lab.md": "## 从 `M × C` 个专用函数走向两条动态分派边界",
    "content/articles/ecal/cpp-design-lab.md": "## 用一个可运行的弱句柄实验建立所有权模型",
}
C_EXAMPLES = {
    "content/articles/lcm/provider-vtable.md":
        "### 我们先用 C 写一个最小运行时分派，而不是背 vtable 的定义",
}


def extract(path: Path, section: str, language: str = "cpp") -> str:
    document = path.read_text(encoding="utf-8-sig")
    offset = document.find(section + "\n")
    if offset < 0:
        raise ValueError(f"missing example section {section}: {path}")
    section_start = offset + len(section) + 1
    rest = document[section_start:]
    match = re.search(
        rf"^~~~{language}\s*\n(.*?)^~~~\s*$", rest, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise ValueError(f"missing standalone C++ fence: {path}")
    return match.group(1)


def main() -> int:
    compiler = shutil.which("g++")
    c_compiler = shutil.which("gcc")
    if not compiler or not c_compiler:
        print("CPP_EXAMPLES_ERROR=g++ and gcc are required")
        return 1

    failures: list[str] = []
    samples = [
        (relative, section, "cpp", compiler, "c++17")
        for relative, section in EXAMPLES.items()
    ] + [
        (relative, section, "c", c_compiler, "c11")
        for relative, section in C_EXAMPLES.items()
    ]
    for index, (relative, section, language, executable_compiler, standard) in enumerate(samples):
        path = ROOT / relative
        try:
            source = extract(path, section, language)
            with tempfile.TemporaryDirectory(prefix="atlas-cxx-") as directory:
                executable = Path(directory) / ("teaching.exe" if sys.platform == "win32" else "teaching")
                command = [
                    executable_compiler, f"-std={standard}", "-O0", "-Wall", "-Wextra",
                    "-Werror", "-pedantic", "-x", language if language == "c" else "c++",
                    "-", "-o", str(executable),
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
            print(f"TEACHING_EXAMPLE_PASS={index + 1}:{standard}:{relative}")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            failures.append(f"{relative}: {error}")
    print(f"TEACHING_EXAMPLES_TESTED={len(samples)}")
    print(f"CPP_EXAMPLES_FAILED={len(failures)}")
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
