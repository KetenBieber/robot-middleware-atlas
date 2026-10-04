"""Static closure checks for the pinned YARP middleware course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "91710eb45baf5d9cb62dd5a0cb3c3a00f42481b9"

ARTICLE_NAMES = (
    "foundations",
    "architecture-map",
    "portcore-architecture",
    "write-fanout",
    "protocol-carrier",
    "read-rpc",
    "close-lifecycle",
    "cpp-design-lab",
    "design-recap",
)

GUIDE_NAMES = (
    "use-environment",
    "use-ports-rpc",
    "use-operations",
    "case-study-icub-navigation",
)

ARTICLE_REQUIRED = {
    "foundations": (
        "PortCore",
        "Name Server",
        "Carrier",
        "PortWriter",
        "background",
        "interrupt",
        "close",
    ),
    "architecture-map": (
        "PortCore",
        "PortCoreOutputUnit",
        "PortCoreInputUnit",
        "Protocol",
        "Carrier",
        "Name Server",
        "PortReport",
    ),
    "portcore-architecture": (
        "PortCore",
        "Unit",
        "Name Server",
        "Carrier",
        "PortReport",
        "close",
        "registry",
    ),
    "write-fanout": (
        "sendHelper",
        "PortCoreOutputUnit",
        "Tracker",
        "sending",
        "background",
        "callback",
        "close",
    ),
    "protocol-carrier": (
        "Protocol",
        "Carrier",
        "OutputProtocol",
        "InputProtocol",
        "beginRead",
        "endRead",
        "ACK",
        "Modifier",
    ),
    "read-rpc": (
        "PortReader",
        "ConnectionReader",
        "readBlock",
        "RPC",
        "Strict",
        "beginRead",
        "interrupt",
    ),
    "close-lifecycle": (
        "closeMain",
        "interrupt",
        "join",
        "PortCoreOutputUnit",
        "PortCoreInputUnit",
        "finished",
        "callback",
    ),
    "cpp-design-lab": (
        "PortWriter",
        "PortReader",
        "BufferedPort",
        "RAII",
        "PImpl",
        "所有权",
        "close",
    ),
    "design-recap": (
        "PortCore",
        "Carrier",
        "Name Server",
        "PortCoreOutputUnit",
        "所有权",
        "关闭",
    ),
}

GUIDE_REQUIRED = {
    "use-environment": (
        "YARP",
        "yarpserver",
        "Port",
    ),
    "use-ports-rpc": (
        "Port",
        "BufferedPort",
        "RPC",
    ),
    "use-operations": (
        "connect",
        "disconnect",
        "interrupt",
    ),
    "case-study-icub-navigation": (
        "iCub",
        "YARP",
        "Port",
    ),
}

PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|本轮任务|本次任务|任务进度|当前进度|"
    r"Agent|Reviewer|ChatGPT|Codex|审计报告|复审结论|质量门禁|"
    r"旧版导航|新版导航|这里改为|此处改为|为了方便阅读|推荐先读",
    re.I,
)


def literal_assignment(name: str):
    path = ROOT / "tools" / "build_sphinx_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def check_page(
    path: Path,
    required: tuple[str, ...],
    *,
    require_pin: bool,
    errors: list[str],
) -> bool:
    if not path.is_file():
        errors.append(f"missing page: {path.relative_to(ROOT)}")
        return False

    source = path.read_text(encoding="utf-8-sig")
    rel = path.relative_to(ROOT)

    if not source.startswith("# "):
        errors.append(f"{rel}: missing h1")

    if require_pin and PINNED not in source:
        errors.append(f"{rel}: missing pinned commit")

    folded = source.casefold()
    for term in required:
        if term.casefold() not in folded:
            errors.append(f"{rel}: required mechanism absent: {term}")

    in_fence = False
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and PROCESS_LANGUAGE.search(line):
            errors.append(
                f"{rel}:{lineno}: editorial/process prose: {line.strip()}"
            )

    if in_fence:
        errors.append(f"{rel}: unclosed code fence")

    return True


def main() -> int:
    errors: list[str] = []
    article_count = 0
    guide_count = 0

    article_folder = ROOT / "content" / "articles" / "yarp"
    for slug in ARTICLE_NAMES:
        if check_page(
            article_folder / f"{slug}.md",
            ARTICLE_REQUIRED[slug],
            require_pin=True,
            errors=errors,
        ):
            article_count += 1

    guide_folder = ROOT / "content" / "guides" / "yarp"
    for slug in GUIDE_NAMES:
        if check_page(
            guide_folder / f"{slug}.md",
            GUIDE_REQUIRED[slug],
            require_pin=False,
            errors=errors,
        ):
            guide_count += 1

    try:
        article_order = tuple(literal_assignment("ARTICLE_ORDER")["yarp"])
        if article_order != ARTICLE_NAMES:
            errors.append(f"yarp article navigation mismatch: {article_order}")

        guide_order = tuple(literal_assignment("GUIDE_ORDER")["yarp"])
        if guide_order != GUIDE_NAMES:
            errors.append(f"yarp guide navigation mismatch: {guide_order}")

        pin = literal_assignment("PROJECTS")["yarp"][1]
        if pin != PINNED:
            errors.append(f"yarp project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"YARP_STATIC_ARTICLES={article_count}/{len(ARTICLE_NAMES)}")
    print(f"YARP_STATIC_GUIDES={guide_count}/{len(GUIDE_NAMES)}")
    print(f"YARP_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("YARP_STATIC_ERROR:", error)

    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
