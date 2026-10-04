"""Static closure checks for the pinned Seastar shard-per-core runtime course."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "8df8212e53577e1d8477a5c901457cd61d88afc7"

ARTICLE_NAMES = (
    "overview",
    "reactor-shard-per-core",
    "scheduling-groups-vruntime",
    "smp-message-queue",
    "future-continuation-task",
    "sharded-foreign-ptr",
    "cross-shard-memory-reclaim",
)

REQUIRED = {
    "overview": ("shard-per-core", "SMP", "Future", "cross-CPU"),
    "reactor-shard-per-core": (
        "allocate_reactor",
        "local_engine",
        "task_queue",
        "add_task",
        "run_tasks",
        "run_some_tasks",
        "preemption_monitor",
        "scheduler_need_preempt",
        "max_task_backlog",
        "task quota",
        "poll_once",
        "pure_poll",
        "try_sleep",
        "try_enter_interrupt_mode",
        "_sleeping",
        "try_systemwide_memory_barrier",
        "eventfd",
        "registration_task",
        "_finished_running_tasks",
        "shutdown",
    ),
    "scheduling-groups-vruntime": (
        "create_scheduling_group",
        "smp::invoke_on_all",
        "init_scheduling_group",
        "with_scheduling_group",
        "current_scheduling_group",
        "task_queue",
        "_reciprocal_shares_times_2_power_32",
        "to_vruntime",
        "account_runtime",
        "indirect_compare",
        "insert_active_task_queue",
        "_last_vruntime",
        "_waittime",
        "_starvetime",
        "queue_length",
        "time_spent_on_task_quota_violations",
        "scheduler_need_preempt",
        "max_task_backlog",
        "set_shares",
        "update_io_bandwidth",
        "destroy_scheduling_group",
        "hard real-time",
    ),
    "smp-message-queue": (
        "spsc_queue",
        "queue_length",
        "batch_size",
        "pending_fifo",
        "_completed_fifo",
        "process_queue",
        "service-group semaphore",
        "get_units",
        "process_completions",
        "maybe_wakeup",
        "_sleeping",
        "systemwide_memory_barrier",
        "schedule(this)",
        "Round-trip Credit",
        "Owner-Shard",
        "origin",
        "target",
    ),
    "future-continuation-task": (
        "continuation_base",
        "public task",
        "future_state",
        "_local_state",
        "_promise",
        "_future",
        "_task",
        "detach_promise",
        "run_and_dispose",
        "broken_promise",
        "report_failed_future",
        "result_unavailable",
        "then_impl",
        "inline",
        "schedule_urgent",
        "nodiscard",
        "Gate",
        "single-consumer",
    ),
    "sharded-foreign-ptr": (
        "sharded<Service>",
        "foreign_ptr",
        "smp::submit_to",
        "owner shard",
        "async_sharded_service",
        "track_deletion",
        "peering_sharded_service",
        "invoke_on",
        "invoke_on_all",
        "get_owner_shard",
        "destroy_on",
        "run_in_background",
        "make_foreign",
        "release()",
        "future<foreign_ptr>",
        "v = {}",
        "Execution-affine Smart Pointer",
        "Lifetime Safety ≠ Data-race Safety",
    ),
    "cross-shard-memory-reclaim": (
        "xcpu_freelist",
        "free_cross_cpu",
        "drain_cross_cpu_freelist",
        "MPSC",
        "object_cpu_id",
        "cpu_id_shift",
        "local_expected_cpu_id",
        "try_free_fastpath",
        "is_local_pointer",
        "do_foreign_free",
        "is_seastar_memory",
        "compare_exchange_weak",
        "memory_order_release",
        "memory_order_acquire",
        "exchange(nullptr",
        "cross_cpu_free_item",
        "live_cpus",
        "drain_cross_cpu_freelist_pollfn",
        "maybe_reclaim",
        "current_min_free_pages",
        "original_free_func",
        "cross_cpu_frees",
        "Adaptive Deferred Reclamation",
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
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} missing from build_sphinx_sources.py")


def main() -> int:
    errors: list[str] = []
    count = 0
    folder = ROOT / "content" / "articles" / "seastar"

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
                errors.append(f"{path.relative_to(ROOT)}: required mechanism absent: {term}")

        in_fence = False
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and PROCESS_LANGUAGE.search(line):
                errors.append(f"{path.relative_to(ROOT)}:{lineno}: editorial/process prose: {line.strip()}")
        if in_fence:
            errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    try:
        order = tuple(literal_assignment("ARTICLE_ORDER")["seastar"])
        if order != ARTICLE_NAMES:
            errors.append(f"seastar article navigation mismatch: {order}")
        guides = tuple(literal_assignment("GUIDE_ORDER")["seastar"])
        if guides:
            errors.append(f"seastar guide list should be empty: {guides}")
        pin = literal_assignment("PROJECTS")["seastar"][1]
        if pin != PINNED:
            errors.append(f"seastar project pin mismatch: {pin}")
    except (OSError, SyntaxError, ValueError, RuntimeError, KeyError) as error:
        errors.append(f"builder navigation: {error}")

    print(f"SEASTAR_STATIC_PAGES={count}/{len(ARTICLE_NAMES)}")
    print(f"SEASTAR_STATIC_ERRORS={len(errors)}")
    for error in errors:
        print("SEASTAR_STATIC_ERROR:", error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
