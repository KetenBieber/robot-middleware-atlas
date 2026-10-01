"""Static closure checks for the pinned ROS1 communication-runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "30483a9f218f1545eec16d3934bf3cb042e2cb5b"
SERIALIZATION_PIN = "a1a194271bfb35b97a09553f2782585cf7fec9db"
NODELET_PIN = "5ed9cabe9388d48a8e228f682d005f313b0a7e89"

ARTICLE_NAMES = (
    "overview",
    "master-discovery",
    "topic-connection",
    "tcpros-transport",
    "serialization-message",
    "callback-queue-spinner",
    "service-rpc-runtime",
    "nodelet-intra-process",
    "limitations-and-ros2-transition",
)

REQUIRED = {
    "overview": (
        "ROS Master", "requestTopic", "SubscriptionQueue",
        "CallbackQueue", "Spinner", "intra-process"
    ),
    "master-discovery": (
        "RegistrationManager", "NodeRef", "Registrations",
        "registerSubscriber", "publisherUpdate", "ps_lock"
    ),
    "topic-connection": (
        "publisherUpdate", "negotiateConnection", "requestTopic",
        "TransportTCP", "PublisherLink", "md5sum"
    ),
    "tcpros-transport": (
        "TransportTCP", "Connection", "read_filled_",
        "PollManager", "tcp_nodelay", "Publication"
    ),
    "serialization-message": (
        "Serializer", "serializationLength", "serializeMessage",
        "SerializedMessage", "lazy serialization", "deserializer"
    ),
    "callback-queue-spinner": (
        "CallbackQueue", "SubscriptionQueue", "callAvailable",
        "callOne", "AsyncSpinner", "TryAgain", "condition"
    ),
    "nodelet-intra-process": (
        "ManagedNodelet", "CallbackQueueManager", "pluginlib",
        "IntraProcess", "shared_ptr", "onInit"
    ),
    "limitations-and-ros2-transition": (
        "RMW", "Executor", "QoS", "wait set",
        "Nodelet", "TCPROS", "CallbackQueue"
    ),
    "service-rpc-runtime": (
        "ServiceManager", "lookupService", "ServiceServerLink",
        "ServiceClientLink", "ServicePublication", "call_queue_",
        "condition_variable", "persistent",
    ),

}

PROCESS_LANGUAGE = re.compile(
    r"用户提出|用户要求|你的要求|本轮任务|本次任务|任务进度|当前进度|"
    r"Agent|Reviewer|ChatGPT|Codex|审计报告|复审结论|质量门禁|"
    r"旧版导航|新版导航|这里改为|此处改为|为了方便阅读",
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


def main() -> int:
    errors: list[str] = []
    count = 0
    folder = ROOT / "content" / "articles" / "ros1"

    for slug in ARTICLE_NAMES:
        path = folder / f"{slug}.md"
        if not path.is_file():
            errors.append(f"missing page: {path.relative_to(ROOT)}")
            continue

        count += 1
        source = path.read_text(encoding="utf-8-sig")

        if not source.startswith("# "):
            errors.append(f"{path.relative_to(ROOT)}: missing h1")
        if PINNED not in source:
            errors.append(f"{path.relative_to(ROOT)}: missing ros_comm pinned commit")

        if slug == "serialization-message" and SERIALIZATION_PIN not in source:
            errors.append(
                f"{path.relative_to(ROOT)}: missing roscpp_core pinned commit"
            )
        if slug == "nodelet-intra-process" and NODELET_PIN not in source:
            errors.append(
                f"{path.relative_to(ROOT)}: missing nodelet_core pinned commit"
            )

        folded = source.casefold()
        for term in REQUIRED[slug]:
            if term.casefold() not in folded:
                errors.append(
                    f"{path.relative_to(ROOT)}: "
                    f"required mechanism absent: {term}"
                )

        in_fence = False
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and PROCESS_LANGUAGE.search(line):
                errors.append(
                    f"{path.relative_to(ROOT)}:{lineno}: "
                    f"editorial/process prose: {line.strip()}"
                )
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["ros1"])
        if order != ARTICLE_NAMES:
            errors.append(f"ros1 article navigation mismatch: {order}")

        guides = tuple(literal_assignment("GUIDE_ORDER")["ros1"])
        if guides:
            errors.append(f"ros1 guide list should be empty: {guides}")

        pin = literal_assignment("PROJECTS")["ros1"][1]
        if pin != PINNED:
            errors.append(f"ros1 project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ROS1_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"ROS1_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ROS1_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
