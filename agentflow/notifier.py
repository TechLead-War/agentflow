from __future__ import annotations
import subprocess
import sys
from .models import RunState, TaskStatus


def notify(state: RunState):
    """Send a notification that the run is complete."""
    merged = sum(1 for t in state.tasks if t.status == TaskStatus.MERGED)
    failed = sum(1 for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED))
    total = len(state.tasks)

    title = "agentflow"
    if failed == 0:
        message = f"Done. {merged}/{total} tasks merged."
    else:
        message = f"Done. {merged} merged, {failed} need attention."

    # macOS notification
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}" sound name "Glass"'],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass

    # Terminal bell as fallback
    print(f"\a", end="", flush=True)
