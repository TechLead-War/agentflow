from __future__ import annotations
import json
import os
from pathlib import Path
from .models import RunState, Task, TaskStatus


AGENTHUB_DIR = ".agenthub"
STATE_FILE = "state.json"


def get_state_dir(repo_path: str) -> Path:
    d = Path(repo_path) / AGENTHUB_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_log_dir(repo_path: str, run_id: str) -> Path:
    d = get_state_dir(repo_path) / "logs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_task_log_dir(repo_path: str, run_id: str, task_id: str) -> Path:
    d = get_log_dir(repo_path, run_id) / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_state(state: RunState):
    """Atomically save run state to disk."""
    state_dir = get_state_dir(state.repo_path)
    state_file = state_dir / STATE_FILE
    tmp_file = state_dir / f"{STATE_FILE}.tmp"

    with open(tmp_file, "w") as f:
        json.dump(state.to_dict(), f, indent=2)

    os.replace(tmp_file, state_file)


def load_state(repo_path: str) -> RunState | None:
    """Load run state from disk. Returns None if no state exists."""
    state_file = Path(repo_path) / AGENTHUB_DIR / STATE_FILE
    if not state_file.exists():
        return None

    with open(state_file) as f:
        data = json.load(f)

    return RunState.from_dict(data)


def update_task_status(state: RunState, task_id: str, status: TaskStatus,
                       round_num: int | None = None, feedback: str | None = None,
                       error: str | None = None):
    """Update a single task's status and persist."""
    for task in state.tasks:
        if task.id == task_id:
            task.status = status
            if round_num is not None:
                task.current_round = round_num
            if feedback is not None:
                task.feedback = feedback
            if error is not None:
                task.error = error
            break
    save_state(state)


def log_round(repo_path: str, run_id: str, task_id: str,
              round_num: int, phase: str, content: str):
    """Write a log file for a specific round and phase."""
    log_dir = get_task_log_dir(repo_path, run_id, task_id)
    filename = f"round-{round_num}-{phase}.md"
    with open(log_dir / filename, "w") as f:
        f.write(content)


def log_plan(repo_path: str, run_id: str, plan_data: dict):
    """Write the plan output."""
    log_dir = get_log_dir(repo_path, run_id)
    with open(log_dir / "plan.json", "w") as f:
        json.dump(plan_data, f, indent=2)


def log_research(repo_path: str, run_id: str, gate_data: dict, brief_data: dict | None):
    """Write the research gate result and optional brief."""
    log_dir = get_log_dir(repo_path, run_id)
    with open(log_dir / "research.json", "w") as f:
        json.dump({"gate": gate_data, "brief": brief_data}, f, indent=2)


def log_validation(repo_path: str, run_id: str, attempt: int, result_data: dict):
    """Write a validation result to the run log directory."""
    log_dir = get_log_dir(repo_path, run_id)
    with open(log_dir / f"validation-{attempt}.json", "w") as f:
        json.dump(result_data, f, indent=2)


def log_summary(repo_path: str, run_id: str, summary: str):
    """Write the final summary."""
    log_dir = get_log_dir(repo_path, run_id)
    with open(log_dir / "summary.md", "w") as f:
        f.write(summary)
