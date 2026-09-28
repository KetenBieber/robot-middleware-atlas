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
THREAD_EXAMPLES = {
    "content/articles/cyber/cpp-implementation-lab.md":
        "## 附录：可运行的 Node、Reader 与 Writer 最小版",
}
EXAMPLES = {
    "content/articles/lcm/c-abi-cpp-design-lab.md":
        "### 用完整的 C++17 实验理解 trampoline，再进入真实模板",
    "content/articles/lcm/foundations.md":
        "### 一个能运行的 callback trampoline 实验",
    "content/articles/lcm/provider-vtable.md": "### 先写一个能运行的 C++17 缩小版",
    "content/articles/lcm/udpm-publish-protocol.md":
        "### 用独立 C++17 实验预测每一片的 offset",
    "content/articles/lcm/subscription-dispatch.md": "### 可以编译运行的订阅删除实验",
    "content/articles/yarp/cpp-design-lab.md": "## 从 `M × C` 个专用函数走向两条动态分派边界",
    "content/articles/ecal/cpp-design-lab.md": "## 用一个可运行的弱句柄实验建立所有权模型",
}
C_EXAMPLES = {
    "content/articles/lcm/receive-reassembly.md":
        "### 追问：为什么队列尾巴不是普通指针，而是二级指针？",
    "content/articles/lcm/provider-vtable.md":
        "### 我们先用 C 写一个最小运行时分派，而不是背 vtable 的定义",
}
EXTRA_CPP_EXAMPLES = [
    ("content/articles/lcm/subscription-dispatch.md",
     "### 再追问一步：准入计数并没有记住“是哪一条消息”"),
    ("content/articles/lcm/receive-reassembly.md",
     "### 让重复片真正触发一次“假的重组成功”"),
]
FILE_CPP_EXAMPLES = [
    "examples/ecal/closed_loop/wire_codec_test.cpp",
]


def extract(path: Path, section: str, language: str = "cpp") -> str:
    document = path.read_text(encoding="utf-8-sig")
    offset = document.find(section + "\n")
    if offset < 0:
        raise ValueError(f"missing example section {section}: {path}")
    section_start = offset + len(section) + 1
    rest = document[section_start:]
    for fence in ("~~~", "```"):
        match = re.search(
            rf"^{re.escape(fence)}{language}\s*\n(.*?)^{re.escape(fence)}\s*$",
            rest,
            re.MULTILINE | re.DOTALL,
        )
        if match is not None:
            return match.group(1)
    raise ValueError(f"missing standalone {language} fence: {path}")


def supports_cpp_threads(compiler: str) -> bool:
    probe = """#include <mutex>\n#include <thread>\nint main(){std::mutex m; std::thread t([]{}); t.join(); std::lock_guard<std::mutex> g(m); (void)g;}\n"""
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-thread-probe-") as directory:
            executable = Path(directory) / ("probe.exe" if sys.platform == "win32" else "probe")
            result = subprocess.run(
                [compiler, "-std=c++17", "-pthread", "-x", "c++", "-", "-o", str(executable)],
                input=probe, text=True, capture_output=True, timeout=20, check=False,
            )
            if result.returncode != 0:
                return False
            result = subprocess.run(
                [str(executable)], text=True, capture_output=True, timeout=10, check=False,
            )
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    compiler = shutil.which("g++")
    c_compiler = shutil.which("gcc")
    if not compiler or not c_compiler:
        print("CPP_EXAMPLES_ERROR=g++ and gcc are required")
        return 1

    failures: list[str] = []
    blocked: list[str] = []
    cpp_threads_available = supports_cpp_threads(compiler)
    samples = [
        (relative, section, "cpp", compiler, "c++17", False)
        for relative, section in EXAMPLES.items()
    ] + [
        (relative, section, "cpp", compiler, "c++17", False)
        for relative, section in EXTRA_CPP_EXAMPLES
    ] + [
        (relative, section, "cpp", compiler, "c++17", True)
        for relative, section in THREAD_EXAMPLES.items()
    ] + [
        (relative, section, "c", c_compiler, "c11", False)
        for relative, section in C_EXAMPLES.items()
    ]
    for index, (relative, section, language, executable_compiler, standard, requires_threads) in enumerate(samples):
        path = ROOT / relative
        try:
            source = extract(path, section, language)
            if requires_threads and not cpp_threads_available:
                blocked.append(
                    f"{relative}: current C++ compiler lacks usable std::thread/std::mutex"
                )
                print(
                    f"TEACHING_EXAMPLE_BLOCKED={index + 1}:{standard}:{relative}:thread-toolchain"
                )
                continue
            with tempfile.TemporaryDirectory(prefix="atlas-cxx-") as directory:
                executable = Path(directory) / ("teaching.exe" if sys.platform == "win32" else "teaching")
                command = [
                    executable_compiler, f"-std={standard}", "-O0", "-Wall", "-Wextra",
                    "-Werror", "-pedantic",
                ]
                if requires_threads:
                    command.append("-pthread")
                command += [
                    "-x", language if language == "c" else "c++", "-", "-o", str(executable),
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
    for relative in FILE_CPP_EXAMPLES:
        path = ROOT / relative
        try:
            with tempfile.TemporaryDirectory(prefix="atlas-project-cxx-") as directory:
                executable = Path(directory) / ("project_example.exe" if sys.platform == "win32" else "project_example")
                command = [
                    compiler, "-std=c++17", "-O0", "-Wall", "-Wextra", "-Werror",
                    "-pedantic", str(path), "-I", str(path.parent), "-o", str(executable),
                ]
                result = subprocess.run(command, text=True, capture_output=True, timeout=35, check=False)
                if result.returncode != 0:
                    raise RuntimeError(f"compile returned {result.returncode}:\n{result.stderr}")
                result = subprocess.run([str(executable)], text=True, capture_output=True, timeout=10, check=False)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"program exited {result.returncode}: {result.stdout}\n{result.stderr}"
                    )
            print(f"PROJECT_EXAMPLE_PASS={relative}")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            failures.append(f"{relative}: {error}")

    print(f"TEACHING_EXAMPLES_TESTED={len(samples) - len(blocked)}")
    print(f"PROJECT_EXAMPLES_TESTED={len(FILE_CPP_EXAMPLES)}")
    print(f"TEACHING_EXAMPLES_BLOCKED={len(blocked)}")
    print(f"CPP_EXAMPLES_FAILED={len(failures)}")
    for item in blocked:
        print("TEACHING_BLOCKED:", item)
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
