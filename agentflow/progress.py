from __future__ import annotations
from datetime import datetime
from .models import RunState, TaskStatus
from .state import load_state


# Status display characters and colors
STATUS_STYLES = {
    TaskStatus.PENDING:       ("dim", "pending"),
    TaskStatus.QUEUED:        ("dim", "queued"),
    TaskStatus.AGENT_WORKING: ("bold cyan", "agent working"),
    TaskStatus.REVIEWING:     ("bold yellow", "reviewing"),
    TaskStatus.ITERATING:     ("bold yellow", "iterating"),
    TaskStatus.APPROVED:      ("bold green", "approved"),
    TaskStatus.MERGING:       ("bold blue", "merging"),
    TaskStatus.MERGED:        ("green", "merged"),
    TaskStatus.ESCALATED:     ("bold red", "ESCALATED"),
    TaskStatus.FAILED:        ("bold red", "FAILED"),
}

# Phase display styles: (rich style, human label)
PHASE_STYLES = {
    "initializing": ("bold white", "Initializing"),
    "planning":     ("bold cyan", "Planning tasks..."),
    "scheduling":   ("bold cyan", "Scheduling batches"),
    "running":      ("bold green", "Running"),
    "merging":      ("bold blue", "Merging branches"),
    "completed":    ("bold green", "Completed"),
    "failed":       ("bold red", "Failed"),
    "interrupted":  ("bold yellow", "Interrupted"),
}

PROGRESS_CHARS = ("\u2588", "\u2591")


def _elapsed_str(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at)
        delta = datetime.now() - start
        minutes = int(delta.total_seconds() // 60)
        seconds = int(delta.total_seconds() % 60)
        return f"{minutes}m {seconds}s"
    except (ValueError, TypeError):
        return ""


def render_progress_bar(current: int, total: int, width: int = 10) -> str:
    if total == 0:
        return PROGRESS_CHARS[1] * width
    filled = int(width * current / total)
    return PROGRESS_CHARS[0] * filled + PROGRESS_CHARS[1] * (width - filled)


def build_status_table(state: RunState):
    """Build a Rich Table showing current run status."""
    from rich.table import Table
    from rich.text import Text

    task_count = len(state.tasks) if state.tasks else 0
    elapsed = _elapsed_str(state.started_at)
    phase_style, phase_label = PHASE_STYLES.get(
        state.phase, ("dim", state.phase)
    )

    # Build title: always show run_id + phase
    title = f"agentflow \u2014 {state.run_id}"

    # Build subtitle: prompt snippet + phase + elapsed
    prompt_snippet = state.prompt[:60].replace("\n", " ").strip()
    if len(state.prompt) > 60:
        prompt_snippet += "..."
    subtitle = f'[dim]"{prompt_snippet}"[/]'

    table = Table(
        title=title,
        caption=subtitle,
        show_header=True,
        header_style="bold",
        border_style="dim",
        pad_edge=False,
    )

    table.add_column("", width=2)
    table.add_column("Task", min_width=20)
    table.add_column("Round", width=8, justify="center")
    table.add_column("Progress", width=12)
    table.add_column("Status", min_width=15)
    table.add_column("Agent", width=8, justify="center")

    # --- Phase: initializing / planning (no tasks yet) ---
    if not state.tasks:
        if state.phase == "planning":
            # Check staleness
            try:
                age = (datetime.now() - datetime.fromisoformat(state.started_at)).total_seconds()
                if age > 300:
                    phase_label = "STALE \u2014 run 'agentflow clean'"
                    phase_style = "bold red"
            except (ValueError, TypeError):
                pass

            table.add_row(
                "[bold cyan]\u25cf[/]",
                "[bold cyan]Breaking down prompt into tasks[/]",
                "-", "",
                f"[{phase_style}]{phase_label}[/]",
                "-",
            )
        elif state.phase == "initializing":
            table.add_row(
                "[dim]\u25cb[/]",
                "[dim]Checking config & prerequisites[/]",
                "-", "",
                f"[{phase_style}]{phase_label}[/]",
                "-",
            )
        else:
            table.add_row(
                "[dim]...[/]", f"[dim]{phase_label}[/]",
                "-", "", f"[{phase_style}]{phase_label}[/]", "-",
            )

        table.caption = (
            f"{subtitle}\n"
            f"[{phase_style}]{phase_label}[/] \u2014 {elapsed} elapsed"
        )
        return table

    # --- Phase: scheduling / running / merging / completed (has tasks) ---
    for task in state.tasks:
        style, label = STATUS_STYLES.get(task.status, ("", str(task.status.value)))

        # Icon
        if task.status == TaskStatus.MERGED:
            icon = "[green]\u2713[/]"
        elif task.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            icon = "[red]\u2717[/]"
        elif task.status in (TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING, TaskStatus.ITERATING):
            icon = "[cyan]\u25cf[/]"
        elif task.status == TaskStatus.QUEUED:
            icon = "[dim]\u25cb[/]"
        else:
            icon = "[dim]\u25cb[/]"

        # Progress bar
        bar = render_progress_bar(task.current_round, task.max_rounds)

        # Round display
        round_str = f"{task.current_round}/{task.max_rounds}" if task.current_round > 0 else "-"

        table.add_row(
            icon,
            task.id,
            round_str,
            bar,
            f"[{style}]{label}[/]",
            task.agent.value,
        )

    # Footer stats
    merged = sum(1 for t in state.tasks if t.status == TaskStatus.MERGED)
    failed = sum(1 for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED))
    active = sum(1 for t in state.tasks if t.status in (
        TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING, TaskStatus.ITERATING))
    pending = sum(1 for t in state.tasks if t.status in (
        TaskStatus.PENDING, TaskStatus.QUEUED))

    # Batch info
    batch_str = ""
    if state.total_batches > 0:
        batch_str = f"batch {state.current_batch}/{state.total_batches} \u2014 "

    table.caption = (
        f"{subtitle}\n"
        f"[{phase_style}]{phase_label}[/] \u2014 {batch_str}"
        f"{elapsed} elapsed \u2014 "
        f"{task_count} tasks \u2014 "
        f"{merged} merged \u2014 {active} active \u2014 {pending} pending \u2014 {failed} failed"
    )

    return table


def show_live_progress(state: RunState):
    """Show a live-updating progress display using Rich.

    Returns a context manager that auto-refreshes the table from disk
    every second.
    """
    try:
        from rich.live import Live
        from rich.console import Console

        console = Console()

        class _AutoRefreshLive(Live):
            """Live display that re-reads state from disk on each refresh."""

            def __init__(self, run_state: RunState, **kwargs):
                self._run_state = run_state
                super().__init__(build_status_table(run_state), **kwargs)

            def get_renderable(self):
                current = load_state(self._run_state.repo_path) or self._run_state
                return build_status_table(current)

        return _AutoRefreshLive(state, refresh_per_second=1, console=console)

    except ImportError:
        return _FallbackProgress(state)


class _FallbackProgress:
    """Simple fallback when rich is not installed."""

    def __init__(self, state: RunState):
        self.state = state

    def __enter__(self):
        print(f"agentflow \u2014 {self.state.run_id} \u2014 {self.state.phase}")
        print(f"Prompt: {self.state.prompt[:80]}...")
        print("-" * 50)
        return self

    def __exit__(self, *args):
        pass

    def update(self, renderable=None):
        current = load_state(self.state.repo_path) or self.state
        phase_style, phase_label = PHASE_STYLES.get(
            current.phase, ("", current.phase)
        )
        print(f"\n[{phase_label}]")
        for task in current.tasks:
            _, label = STATUS_STYLES.get(task.status, ("", task.status.value))
            print(f"  {task.id:30s}  round {task.current_round}/{task.max_rounds}  {label}")


def show_status(repo_path: str = "."):
    """One-shot status display (for `agentflow status`)."""
    state = load_state(repo_path)
    if state is None:
        print("No active or recent agentflow run found.")
        return

    try:
        from rich.console import Console
        console = Console()
        table = build_status_table(state)
        console.print(table)
    except ImportError:
        _print_simple_status(state)


def _print_simple_status(state: RunState):
    """Plain text status output."""
    phase_style, phase_label = PHASE_STYLES.get(
        state.phase, ("", state.phase)
    )
    elapsed = _elapsed_str(state.started_at)

    print(f"\nagentflow \u2014 {state.run_id} \u2014 {phase_label}")
    print(f"Prompt: {state.prompt[:80]}...")
    print(f"Elapsed: {elapsed}")
    print("-" * 50)

    if not state.tasks:
        print(f"  {phase_label}")
        return

    for task in state.tasks:
        _, label = STATUS_STYLES.get(task.status, ("", task.status.value))
        icon = "\u2713" if task.status == TaskStatus.MERGED else "\u2717" if task.status in (
            TaskStatus.FAILED, TaskStatus.ESCALATED) else "\u25cf" if task.status in (
            TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING) else "\u25cb"
        print(f"  {icon} {task.id:30s}  round {task.current_round}/{task.max_rounds}  {label}")

        if task.error:
            print(f"    error: {task.error[:80]}")

    merged = sum(1 for t in state.tasks if t.status == TaskStatus.MERGED)
    failed = sum(1 for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED))
    print(f"\n{merged} merged \u2014 {failed} failed")
