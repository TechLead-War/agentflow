from __future__ import annotations
from .models import Task, Batch


def schedule(tasks: list[Task]) -> list[Batch]:
    """
    Build execution batches from tasks based on dependencies and file overlaps.

    Tasks within the same batch can run in parallel.
    Batches execute sequentially.
    """
    if not tasks:
        return []

    # Build dependency graph
    task_map = {t.id: t for t in tasks}
    explicit_deps = {t.id: set(t.depends_on) for t in tasks}

    # Detect file overlaps and add implicit dependencies
    _add_file_overlap_deps(tasks, explicit_deps)

    # Topological sort into batches (Kahn's algorithm)
    return _topo_batch(tasks, explicit_deps)


def _add_file_overlap_deps(tasks: list[Task], deps: dict[str, set[str]]):
    """If two tasks touch the same file and aren't already ordered, make them sequential."""
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a.id == b.id:
                continue

            # Check file overlap
            files_a = set(a.files)
            files_b = set(b.files)
            overlap = files_a & files_b

            if not overlap:
                continue

            # Already have a dependency path? Skip.
            if b.id in deps[a.id] or a.id in deps[b.id]:
                continue

            # Add dependency: later task depends on earlier task
            deps[b.id].add(a.id)


def _topo_batch(tasks: list[Task], deps: dict[str, set[str]]) -> list[Batch]:
    """Topological sort into parallel batches."""
    task_map = {t.id: t for t in tasks}
    remaining = set(t.id for t in tasks)
    completed: set[str] = set()
    batches: list[Batch] = []
    order = 0

    while remaining:
        # Find tasks with all dependencies satisfied
        ready = []
        for tid in remaining:
            task_deps = deps.get(tid, set())
            if task_deps.issubset(completed):
                ready.append(tid)

        if not ready:
            # Circular dependency — break it by picking one
            ready = [next(iter(remaining))]

        batch_tasks = [task_map[tid] for tid in ready]
        batches.append(Batch(tasks=batch_tasks, order=order))
        order += 1

        for tid in ready:
            remaining.discard(tid)
            completed.add(tid)

    return batches
