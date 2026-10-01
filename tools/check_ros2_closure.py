"""Static closure checks for the pinned ROS2 communication-runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PINNED = "cfdb3b7dcea4a503c0acaa304d033636beeb1dba"
RCL_PIN = "cbaee7c905e3276bb7a629eb1e18d8d3c781f194"
RMW_DDS_COMMON_PIN = "e26ba1079886c5598614172db0b27526aa6af07d"
CYCLONE_RMW_PIN = "e370e09ca76fc811e42ff07bd3a5e3b92f18c51e"
FAST_RMW_PIN = "da0c2d3120e9b5ea85cca251ea1f48ca8d0a93d7"
ROS1_PIN = "30483a9f218f1545eec16d3934bf3cb042e2cb5b"

ARTICLE_NAMES = (
    "overview",
    "publish-rcl-rmw-dds",
    "discovery-graph-cache",
    "qos-contract-mapping",
    "executor-waitset",
    "receive-take-callback",
    "service-client-server-runtime",
    "action-protocol-composition",
    "action-goal-state-executor",
    "intra-process-manager",
    "loaned-message-zero-copy",
    "ros1-vs-ros2-architecture",
    "service-rpc-comparison",
    "latency-budget-comparison",
    "copy-serialization-comparison",
    "scheduling-comparison",
    "backlog-qos-comparison",
    "discovery-failure-comparison",
    "control-chain-case-study",
)

REQUIRED = {
    "overview": (
        "rclcpp", "rcl_publish", "rmw", "Executor", "GraphCache", "IntraProcessManager",
    ),
    "publish-rcl-rmw-dds": (
        "do_inter_process_publish", "rcl_publish", "rmw_publish",
        "dds_write", "DataWriter", "SerializedData",
    ),
    "discovery-graph-cache": (
        "GraphCache", "ParticipantEntitiesInfo", "associate_writer",
        "SPDP", "SEDP", "DDS",
    ),
    "qos-contract-mapping": (
        "rmw_qos_profile_t", "Reliability", "Durability",
        "Deadline", "Liveliness", "dds_qset_reliability",
    ),
    "executor-waitset": (
        "WaitSet", "rcl_wait", "AnyExecutable",
        "CallbackGroup", "MutuallyExclusive", "GuardCondition",
    ),
    "receive-take-callback": (
        "take_type_erased", "rcl_take", "rmw_take_with_info",
        "SUBSCRIPTION_TAKE_FAILED", "DDS Reader", "MessageInfo",
    ),
    "intra-process-manager": (
        "IntraProcessManager", "SplittedSubscriptions", "take_shared_subscriptions",
        "take_ownership_subscriptions", "shared_timed_mutex", "unique_ptr",
    ),
    "loaned-message-zero-copy": (
        "LoanedMessage", "can_loan_messages", "rcl_borrow_loaned_message",
        "rcl_return_loaned_message", "DDS_HAS_SHM", "RMW_RET_UNSUPPORTED",
    ),
    "ros1-vs-ros2-architecture": (
        "ROS Master", "RMW", "QoS", "WaitSet",
        "Executor", "Nodelet", "IntraProcessManager",
    ),
    "latency-budget-comparison": (
        "SubscriptionQueue", "wait_for_work", "rmw_take_with_info",
        "DDS Reader History", "CallbackGroup", "Age",
    ),
    "copy-serialization-comparison": (
        "getPublishTypes", "IntraProcessManager", "LoanedMessage",
        "dds_write", "DataWriter", "inter_process_publish_needed",
    ),
    "scheduling-comparison": (
        "CallbackQueue", "callAvailable", "WaitSet",
        "execute_any_executable", "CallbackGroup", "SingleThreadedExecutor",
    ),
    "backlog-qos-comparison": (
        "SubscriptionQueue", "rmw_qos_profile_t", "KEEP_LAST",
        "Reliability", "Durability", "Executor",
    ),
    "discovery-failure-comparison": (
        "ROS Master", "requestTopic", "GraphCache",
        "ParticipantEntitiesInfo", "DDS Domain", "QoS",
    ),
    "control-chain-case-study": (
        "queue_size", "KEEP_LAST", "CallbackGroup",
        "SingleThreadedExecutor", "Age", "stale-data",
    ),
    "service-client-server-runtime": (
        "rmw_create_service", "rmw_create_client", "sequence_number",
        "pending_requests_", "execute_service", "execute_client",
        "rmw_take_request", "rmw_take_response",
    ),
    "service-rpc-comparison": (
        "ServiceServerLink", "call_queue_", "request_id",
        "pending_requests_", "CallbackQueue", "WaitSet", "persistent",
    ),
    "action-protocol-composition": (
        "send_goal", "cancel_goal", "get_result",
        "feedback", "status", "GoalUUID",
        "pending_goal_responses", "result_requests_",
    ),
    "action-goal-state-executor": (
        "GOAL_STATE_ACCEPTED", "GOAL_STATE_EXECUTING", "GOAL_STATE_CANCELING",
        "rcl_action_transition_goal_state", "goal_results_", "result_requests_",
        "next_ready_event", "Waitable",
    ),

}

EXTRA_PINS = {
    "publish-rcl-rmw-dds": (RCL_PIN, CYCLONE_RMW_PIN, FAST_RMW_PIN),
    "discovery-graph-cache": (RMW_DDS_COMMON_PIN, CYCLONE_RMW_PIN),
    "qos-contract-mapping": (RMW_DDS_COMMON_PIN, CYCLONE_RMW_PIN),
    "executor-waitset": (RCL_PIN,),
    "receive-take-callback": (RCL_PIN, CYCLONE_RMW_PIN),
    "loaned-message-zero-copy": (CYCLONE_RMW_PIN, FAST_RMW_PIN),
    "ros1-vs-ros2-architecture": (ROS1_PIN,),
    "latency-budget-comparison": (ROS1_PIN, RCL_PIN),
    "copy-serialization-comparison": (ROS1_PIN, CYCLONE_RMW_PIN, FAST_RMW_PIN),
    "scheduling-comparison": (ROS1_PIN, RCL_PIN),
    "backlog-qos-comparison": (ROS1_PIN, RMW_DDS_COMMON_PIN),
    "discovery-failure-comparison": (ROS1_PIN, RMW_DDS_COMMON_PIN),
    "control-chain-case-study": (ROS1_PIN,),
    "service-client-server-runtime": (RCL_PIN,),
    "service-rpc-comparison": (ROS1_PIN, RCL_PIN),
    "action-protocol-composition": (RCL_PIN,),
    "action-goal-state-executor": (RCL_PIN,),

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
    folder = ROOT / "content" / "articles" / "ros2"

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
            errors.append(f"{path.relative_to(ROOT)}: missing rclcpp pinned commit")

        for pin in EXTRA_PINS.get(slug, ()):
            if pin not in source:
                errors.append(
                    f"{path.relative_to(ROOT)}: missing supporting pin {pin}"
                )

        folded = source.casefold()
        for term in REQUIRED[slug]:
            if term.casefold() not in folded:
                errors.append(
                    f"{path.relative_to(ROOT)}: required mechanism absent: {term}"
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
        order = tuple(literal_assignment("ARTICLE_ORDER")["ros2"])
        if order != ARTICLE_NAMES:
            errors.append(f"ros2 article navigation mismatch: {order}")

        guides = tuple(literal_assignment("GUIDE_ORDER")["ros2"])
        if guides:
            errors.append(f"ros2 guide list should be empty: {guides}")

        pin = literal_assignment("PROJECTS")["ros2"][1]
        if pin != PINNED:
            errors.append(f"ros2 project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"ROS2_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"ROS2_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("ROS2_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
