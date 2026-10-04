"""Static closure checks for the pinned libzmq message-runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "46493370217ac135246617fa2f6ac819d8b61bfc"

ARTICLE_NAMES = (
    "overview",
    "msg-storage-refcount",
    "mailbox-command-wakeup",
    "ypipe-yqueue-spsc",
    "pipe-hwm-backpressure",
    "socket-command-owner",
    "command-seqnum-quiescence",
    "inproc-endpoint-registry",
    "io-thread-poller",
    "session-stream-engine",
    "zmtp-handshake-mechanism",
    "tcp-reconnect-state-machine",
    "monitor-event-observability",
    "fq-lb-dist-schedulers",
    "dealer-router-routing",
    "pubsub-trie-distributor",
    "proxy-device-runtime",
    "linger-termination-protocol",
    "context-reaper-lifecycle",
)

REQUIRED = {
    "overview": ("socket_base_t", "pipe_t", "session_base_t", "mailbox_t"),
    "msg-storage-refcount": (
        "msg_t_size", "max_vsm_size", "add_refs", "rm_refs", "dist_t"
    ),
    "mailbox-command-wakeup": (
        "mailbox_t",
        "signaler",
        "ypipe",
        "mutex",
        "_c == NULL",
        "lost wakeup",
        "compare_exchange_strong",
        "memory_order_acq_rel",
        "ctx_t::_slots",
        "process_command",
        "PASSIVE",
        "doorbell",
        "mailbox_safe_t",
    ),
    "ypipe-yqueue-spsc": ("ypipe", "yqueue", "_c", "flush"),
    "pipe-hwm-backpressure": (
        "HWM",
        "_out_active",
        "_in_active",
        "_msgs_written",
        "_msgs_read",
        "_peers_msgs_read",
        "_lwm",
        "compute_lwm",
        "activate_write",
        "activate_read",
        "write_activated",
        "read_activated",
        "set_hwms_boost",
        "ypipe_conflate_t",
        "rollback",
        "delimiter",
        "term_req_sent2",
    ),
    "socket-command-owner": (
        "socket_base_t", "command_t", "process_commands", "owner"
    ),
    "command-seqnum-quiescence": (
        "_sent_seqnum", "_processed_seqnum", "inc_seqnum",
        "process_seqnum", "find_endpoint", "TERM_ACK", "Reaper"
    ),
    "inproc-endpoint-registry": (
        "endpoint_t", "_pending_connections", "multimap",
        "find_endpoint", "connect_pending", "inc_seqnum"
    ),
    "io-thread-poller": ("io_thread_t", "poller", "mailbox", "in_event"),
    "session-stream-engine": (
        "session_base_t", "stream_engine_base_t", "engine_ready", "encoder"
    ),
    "zmtp-handshake-mechanism": (
        "greeting", "mechanism_t", "handshaking", "mechanism_ready", "ZMTP"
    ),
    "tcp-reconnect-state-machine": (
        "EINPROGRESS", "SO_ERROR", "reconnect_ivl_max",
        "add_reconnect_timer", "create_engine"
    ),
    "monitor-event-observability": (
        "_monitor_socket", "_monitor_sync", "ZMQ_EVENT_CONNECTED",
        "ZMQ_EVENT_PIPES_STATS", "ZMQ_LINGER"
    ),
    "fq-lb-dist-schedulers": (
        "fq_t",
        "lb_t",
        "dist_t",
        "multipart",
        "array_item_t<1>",
        "array_item_t<2>",
        "active prefix",
        "_dropping",
        "reverse_match",
        "matching ⊆ active ⊆ eligible",
        "add_refs",
        "rm_refs",
        "has_pipe",
        "Generation",
        "transaction",
    ),
    "dealer-router-routing": (
        "DEALER",
        "ROUTER",
        "routing",
        "_out_pipes",
        "_anonymous_pipes",
        "_current_out",
        "_current_in",
        "_prefetched_msg",
        "ZMQ_ROUTER_MANDATORY",
        "ZMQ_ROUTER_HANDOVER",
        "ZMQ_CONNECT_ROUTING_ID",
        "EHOSTUNREACH",
        "EAGAIN",
        "get_peer_state",
        "_terminate_current_in",
        "Rename-before-Retire",
        "target-specific readiness",
    ),
    "pubsub-trie-distributor": (
        "PUB",
        "SUB",
        "trie",
        "subscription",
        "_refcnt",
        "generic_mtrie_t<pipe_t>",
        "std::set<value_t *>",
        "_min",
        "_count",
        "_live_nodes",
        "ZMQ_XPUB_VERBOSE",
        "ZMQ_XPUB_VERBOSER",
        "ZMQ_XPUB_MANUAL",
        "ZMQ_XPUB_NODROP",
        "ZMQ_INVERT_MATCHING",
        "_manual_subscriptions",
        "_pending_pipes",
        "_more_send",
        "snapshot",
        "reverse control plane",
    ),
    "proxy-device-runtime": (
        "proxy_burst_size",
        "ZMQ_RCVMORE",
        "ZMQ_POLLOUT",
        "PAUSE",
        "STATISTICS",
        "capture",
        "complete-message",
        "Two-phase Readiness",
        "poller_receive_blocked",
        "poller_send_blocked",
        "poller_both_blocked",
        "frontend_equal_to_backend",
        "request_processed",
        "reply_processed",
        "900 kB",
        "critical path",
        "ZMQ_REP",
        "Dependency-directed Waiting",
        "null-object dereference risk",
    ),
    "linger-termination-protocol": (
        "ZMQ_LINGER",
        "_pending",
        "_terminating_pipes",
        "_term_acks",
        "_sent_seqnum",
        "_processed_seqnum",
        "waiting_for_delimiter",
        "delimiter_received",
        "term_req_sent1",
        "term_req_sent2",
        "term_ack_sent",
        "send_reap",
        "_destroyed",
        "check_destroy",
        "unregister_endpoints",
        "send_disconnect_msg",
        "process_pipe_term_ack",
        "RETIRE",
        "DRAIN",
        "QUIESCE",
        "DETACH",
        "RECLAIM",
        "process_destroy",
    ),
    "context-reaper-lifecycle": (
        "Reaper", "_slots", "_empty_slots", "send_reap",
        "start_reaping", "send_done"
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
    folder = ROOT / "content" / "articles" / "libzmq"

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
            errors.append(f"{path.relative_to(ROOT)}: missing pinned commit")

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
        order = tuple(literal_assignment("ARTICLE_ORDER")["libzmq"])
        if order != ARTICLE_NAMES:
            errors.append(f"libzmq article navigation mismatch: {order}")

        guides = tuple(literal_assignment("GUIDE_ORDER")["libzmq"])
        if guides:
            errors.append(f"libzmq guide list should be empty: {guides}")

        pin = literal_assignment("PROJECTS")["libzmq"][1]
        if pin != PINNED:
            errors.append(f"libzmq project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"LIBZMQ_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"LIBZMQ_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("LIBZMQ_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
