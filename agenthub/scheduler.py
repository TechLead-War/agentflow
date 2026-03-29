from __future__ import annotations
import logging
from collections import defaultdict
from .models import Task, Batch

logger = logging.getLogger(__name__)


def schedule(tasks: list[Task]) -> list[Batch]:
    """
    Build execution batches from tasks based on dependencies and file overlaps.

    Tasks within the same batch can run in parallel.
    Batches execute sequentially.

    Uses a star-pattern for file overlap dependencies (all overlapping tasks
    depend on a single anchor) rather than chaining, which maximizes parallelism
    while still preventing merge conflicts on the same file.
    """
    if not tasks:
        return []

    # Build dependency graph
    deps = {t.id: set(t.depends_on) for t in tasks}

    # Detect file overlaps and add implicit dependencies (star pattern)
    _add_file_overlap_deps(tasks, deps)

    # Remove transitive dependencies to keep the graph minimal
    _transitive_reduce(deps)

    # Log dependency stats
    _log_dependency_stats(tasks, deps)

    # Topological sort into batches (Kahn's algorithm)
    return _topo_batch(tasks, deps)


def _add_file_overlap_deps(tasks: list[Task], deps: dict[str, set[str]]):
    """Add dependencies for file overlaps using a star pattern.

    Instead of chaining A→B→C when all touch the same file (forcing serial
    execution), uses a star pattern: B→A, C→A. This lets B and C run in
    parallel after A completes. The merger handles any remaining conflicts.
    """
    # Group tasks by file they touch
    file_to_tasks: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        for f in task.files:
            file_to_tasks[f].append(task.id)

    # For each file with multiple tasks, make all depend on the first (anchor)
    for filepath, task_ids in file_to_tasks.items():
        if len(task_ids) < 2:
            continue

        anchor = task_ids[0]  # First task listed becomes the anchor

        for tid in task_ids[1:]:
            # Skip if there's already an explicit dependency in either direction
            if _has_path(deps, anchor, tid) or _has_path(deps, tid, anchor):
                continue

            # Star pattern: all non-anchor tasks depend on the anchor
            deps[tid].add(anchor)


def _has_path(deps: dict[str, set[str]], source: str, target: str) -> bool:
    """Check if there's a dependency path from source to target (BFS)."""
    visited = set()
    queue = [source]
    while queue:
        current = queue.pop(0)
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        queue.extend(deps.get(current, set()))
    return False


def _transitive_reduce(deps: dict[str, set[str]]):
    """Remove transitive (redundant) dependencies.

    If A→B→C exists, then A→C is redundant and can be removed.
    This keeps the dependency graph minimal without losing ordering guarantees.
    """
    for tid in list(deps.keys()):
        direct = set(deps[tid])
        redundant = set()

        for dep in direct:
            # Check if this dep is reachable through OTHER deps
            other_deps = direct - {dep} - redundant
            for other in other_deps:
                if _has_path(deps, other, dep):
                    redundant.add(dep)
                    break

        if redundant:
            deps[tid] -= redundant
            logger.debug(
                "Removed %d transitive dep(s) from task '%s': %s",
                len(redundant), tid, redundant,
            )


def _log_dependency_stats(tasks: list[Task], deps: dict[str, set[str]]):
    """Log metrics about the dependency graph for observability."""
    total_tasks = len(tasks)
    total_deps = sum(len(d) for d in deps.values())
    independent = sum(1 for d in deps.values() if not d)

    # Calculate serial depth (longest path)
    serial_depth = _max_depth(deps)

    logger.info(
        "Scheduler: %d tasks, %d dependencies, %d independent, "
        "serial depth %d, parallelism ratio %.1f%%",
        total_tasks, total_deps, independent,
        serial_depth,
        (independent / total_tasks * 100) if total_tasks else 0,
    )

    # Warn if too many tasks are serialized
    if total_tasks > 2 and serial_depth > total_tasks * 0.7:
        logger.warning(
            "High serial depth (%d/%d tasks). Consider restructuring tasks "
            "to reduce file overlap and explicit dependencies.",
            serial_depth, total_tasks,
        )


def _max_depth(deps: dict[str, set[str]]) -> int:
    """Calculate the longest dependency chain (serial depth)."""
    memo: dict[str, int] = {}

    def depth(tid: str) -> int:
        if tid in memo:
            return memo[tid]
        task_deps = deps.get(tid, set())
        if not task_deps:
            memo[tid] = 1
            return 1
        reachable = [dep for dep in task_deps if dep in deps]
        if not reachable:
            memo[tid] = 1
            return 1
        d = 1 + max(depth(dep) for dep in reachable)
        memo[tid] = d
        return d

    all_ids = set(deps.keys())
    # Also include IDs referenced as deps but not in the main set
    for d in deps.values():
        all_ids.update(d)

    if not all_ids:
        return 0

    return max(depth(tid) for tid in deps.keys()) if deps else 0


def _topo_batch(tasks: list[Task], deps: dict[str, set[str]]) -> list[Batch]:
    """Topological sort into parallel batches."""
    task_map = {t.id: t for t in tasks}
    remaining = set(t.id for t in tasks)
    completed: set[str] = set()
    batches: list[Batch] = []
    order = 0

    # Treat deps referencing tasks outside the set as already satisfied
    external = set()
    for d in deps.values():
        external.update(d - remaining)
    completed.update(external)

    while remaining:
        # Find tasks with all dependencies satisfied
        ready = []
        for tid in remaining:
            task_deps = deps.get(tid, set())
            if task_deps.issubset(completed):
                ready.append(tid)

        if not ready:
            # Circular dependency — break it by picking one
            stuck = next(iter(remaining))
            logger.warning(
                "Circular dependency detected involving task '%s'. "
                "Breaking cycle to proceed.",
                stuck,
            )
            ready = [stuck]

        batch_tasks = [task_map[tid] for tid in ready]
        batches.append(Batch(tasks=batch_tasks, order=order))
        order += 1

        for tid in ready:
            remaining.discard(tid)
            completed.add(tid)

    return batches
