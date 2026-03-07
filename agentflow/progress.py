from __future__ import annotations
import time
from datetime import datetime
from .models import RunState, TaskStatus, Task
from .state import load_state


# Status display characters and colors
STATUS_STYLES = {
    TaskStatus.PENDING:       ("dim", "waiting"),
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

PROGRESS_CHARS = ("█", "░")


def render_progress_bar(current: int, total: int, width: int = 10) -> str:
    if total == 0:
        return PROGRESS_CHARS[1] * width
    filled = int(width * current / total)
    return PROGRESS_CHARS[0] * filled + PROGRESS_CHARS[1] * (width - filled)


def show_live_progress(state: RunState):
    """Show a live-updating progress display using Rich."""
    try:
        from rich.live import Live
        from rich.table import Table
        from rich.console import Console
        from rich.text import Text

        console = Console()

        def build_table() -> Table:
            # Reload state from disk for latest updates
            current_state = load_state(state.repo_path) or state

            table = Table(
                title=f"agentflow — {current_state.run_id} — {len(current_state.tasks)} tasks",
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

            for task in current_state.tasks:
                style, label = STATUS_STYLES.get(task.status, ("", str(task.status.value)))

                # Icon
                if task.status == TaskStatus.MERGED:
                    icon = "[green]✓[/]"
                elif task.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                    icon = "[red]✗[/]"
                elif task.status in (TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING, TaskStatus.ITERATING):
                    icon = "[cyan]●[/]"
                else:
                    icon = "[dim]○[/]"

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
            merged = sum(1 for t in current_state.tasks if t.status == TaskStatus.MERGED)
            failed = sum(1 for t in current_state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED))
            active = sum(1 for t in current_state.tasks if t.status in (
                TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING, TaskStatus.ITERATING))

            elapsed = ""
            try:
                start = datetime.fromisoformat(current_state.started_at)
                delta = datetime.now() - start
                minutes = int(delta.total_seconds() // 60)
                seconds = int(delta.total_seconds() % 60)
                elapsed = f"{minutes}m {seconds}s"
            except (ValueError, TypeError):
                pass

            table.caption = (
                f"{elapsed} elapsed — "
                f"{merged} merged — {active} active — {failed} failed"
            )

            return table

        return Live(build_table(), refresh_per_second=1, console=console)

    except ImportError:
        return _FallbackProgress(state)


class _FallbackProgress:
    """Simple fallback when rich is not installed."""

    def __init__(self, state: RunState):
        self.state = state

    def __enter__(self):
        print(f"agentflow — {self.state.run_id} — {len(self.state.tasks)} tasks")
        print("-" * 50)
        return self

    def __exit__(self, *args):
        pass

    def update(self, renderable=None):
        current = load_state(self.state.repo_path) or self.state
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
        live = show_live_progress(state)
        if hasattr(live, 'get_renderable'):
            console.print(live.get_renderable())
        else:
            # It's a Live object, just render the table once
            from rich.table import Table
            table = live.renderable if hasattr(live, 'renderable') else None
            if table:
                console.print(table)
            else:
                _print_simple_status(state)
    except ImportError:
        _print_simple_status(state)


def _print_simple_status(state: RunState):
    """Plain text status output."""
    print(f"\nagentflow — {state.run_id} — {state.status}")
    print(f"Prompt: {state.prompt[:80]}...")
    print("-" * 50)

    for task in state.tasks:
        _, label = STATUS_STYLES.get(task.status, ("", task.status.value))
        icon = "✓" if task.status == TaskStatus.MERGED else "✗" if task.status in (
            TaskStatus.FAILED, TaskStatus.ESCALATED) else "●" if task.status in (
            TaskStatus.AGENT_WORKING, TaskStatus.REVIEWING) else "○"
        print(f"  {icon} {task.id:30s}  round {task.current_round}/{task.max_rounds}  {label}")

        if task.error:
            print(f"    error: {task.error[:80]}")

    merged = sum(1 for t in state.tasks if t.status == TaskStatus.MERGED)
    failed = sum(1 for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED))
    print(f"\n{merged} merged — {failed} failed")
