from __future__ import annotations
import sys
import asyncio
from datetime import datetime
from pathlib import Path


USAGE = """\
agentflow — automated coding agents with built-in code review

Usage:
  agentflow "your task description"    Run a task
  agentflow --prompt-file <path>       Run a task from a prompt file
  agentflow -                          Run a task from stdin
  agentflow status                     Show progress of current/last run
  agentflow log                        Show logs from last run
  agentflow resume                     Resume an interrupted run
  agentflow config <key> <value>       Set a configuration value

Examples:
  agentflow "add input validation to the signup form"
  agentflow --prompt-file /tmp/prompt.txt
  cat /tmp/prompt.txt | agentflow -
  agentflow "refactor auth to use JWT, add rate limiting, write tests"
  agentflow config reviewer claude
  agentflow config max_rounds 3
"""


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
    elif cmd == "status":
        _cmd_status()
    elif cmd == "log":
        _cmd_log()
    elif cmd == "resume":
        asyncio.run(_cmd_resume())
    elif cmd == "config":
        _cmd_config()
    elif cmd in ("--prompt-file", "-f"):
        if len(sys.argv) < 3:
            print("Error: Missing prompt file path.\n")
            print(USAGE)
            sys.exit(1)
        prompt = _read_prompt_file(sys.argv[2])
        asyncio.run(_cmd_run(prompt))
    elif cmd == "-":
        prompt = _read_prompt_stdin()
        asyncio.run(_cmd_run(prompt))
    else:
        # Everything else is a task prompt
        prompt = " ".join(sys.argv[1:])
        asyncio.run(_cmd_run(prompt))


def _read_prompt_file(path_str: str) -> str:
    path = Path(path_str)
    if not path.exists():
        print(f"Error: Prompt file not found: {path}")
        sys.exit(1)
    if not path.is_file():
        print(f"Error: Prompt path is not a file: {path}")
        sys.exit(1)

    try:
        prompt = path.read_text(errors="replace")
    except OSError as e:
        print(f"Error: Failed to read prompt file: {e}")
        sys.exit(1)

    if not prompt.strip():
        print(f"Error: Prompt file is empty: {path}")
        sys.exit(1)

    return prompt


def _read_prompt_stdin() -> str:
    if sys.stdin.isatty():
        print("Error: No stdin input provided. Pipe a prompt or use --prompt-file.\n")
        print(USAGE)
        sys.exit(1)

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Error: Stdin prompt is empty.")
        sys.exit(1)

    return prompt


async def _cmd_run(prompt: str):
    from .config import load_config, check_api_keys
    from .planner import plan
    from .scheduler import schedule
    from .worker import run_workers
    from .merger import merge_all
    from .notifier import notify
    from .progress import show_live_progress
    from .state import save_state, log_plan, log_summary
    from .models import RunState, TaskStatus
    from . import git_ops

    repo_path = git_ops.get_repo_root(".")
    config = load_config(repo_path)

    # Check prerequisites
    has_anthropic, has_openai = check_api_keys()
    if not has_anthropic and not has_openai:
        print("Error: Install claude/codex CLI or set ANTHROPIC_API_KEY/OPENAI_API_KEY.")
        sys.exit(1)

    # Warn if repo is dirty (but don't block)
    if not git_ops.is_clean(cwd=repo_path):
        print("Warning: Working tree has uncommitted changes. Stashing them.")
        git_ops.stash(cwd=repo_path)
        stashed = True
    else:
        stashed = False

    base_branch = git_ops.get_current_branch(cwd=repo_path)

    # Initialize state early so `agentflow status` works immediately.
    state = RunState.create(prompt, [], repo_path, base_branch)
    state.status = "planning"
    save_state(state)
    print(f"Run {state.run_id} started. Use 'agentflow status' to check progress.")

    # --- PLAN ---
    print(f"Planning...")
    try:
        tasks = await plan(prompt, config, repo_path)
    except Exception as e:
        state.status = "failed"
        state.finished_at = datetime.now().isoformat()
        save_state(state)
        log_summary(
            repo_path,
            state.run_id,
            f"# agentflow run {state.run_id}\n"
            f"Prompt: {prompt}\n\n"
            f"## Results\n"
            f"  ✗ planning — failed\n"
            f"    error: {e}\n\n"
            f"0 merged, 1 failed",
        )
        print(f"Planning failed: {e}")
        if stashed:
            git_ops.stash_pop(cwd=repo_path)
        sys.exit(1)

    # Attach planned tasks and save immediately so status shows them.
    state.tasks = tasks
    state.status = "running"
    save_state(state)

    plan_data = {"prompt": prompt, "tasks": [t.to_dict() for t in tasks]}
    log_plan(repo_path, state.run_id, plan_data)

    # Show plan summary
    print(f"\n  {len(tasks)} task(s) planned:\n")
    for t in tasks:
        agent_label = t.agent.value
        reviewer_label = t.reviewer.value
        print(f"    {t.id:30s}  agent={agent_label:6s}  reviewer={reviewer_label:6s}  [{t.complexity.value}]")
    print()

    # --- SCHEDULE ---
    batches = schedule(tasks)

    # --- EXECUTE ---
    live = show_live_progress(state)

    try:
        with live:
            for batch in batches:
                # Mark batch tasks as queued
                for task in batch.tasks:
                    task.status = TaskStatus.QUEUED
                save_state(state)

                # Update display
                if hasattr(live, 'update'):
                    live.update(live.renderable if hasattr(live, 'renderable') else None)

                await run_workers(batch.tasks, config, state)

                if hasattr(live, 'update'):
                    live.update(live.renderable if hasattr(live, 'renderable') else None)

    except KeyboardInterrupt:
        print("\nInterrupted. Run 'agentflow resume' to continue.")
        state.status = "interrupted"
        save_state(state)
        if stashed:
            git_ops.stash_pop(cwd=repo_path)
        sys.exit(1)

    # --- MERGE ---
    print("\nMerging approved branches...")
    await merge_all(state, config)

    # --- SUMMARY ---
    state.status = "completed"
    state.finished_at = datetime.now().isoformat()
    save_state(state)

    merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
    failed = [t for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)]

    summary_lines = [
        f"# agentflow run {state.run_id}",
        f"Prompt: {prompt}",
        f"",
        f"## Results",
    ]

    for t in state.tasks:
        icon = "✓" if t.status == TaskStatus.MERGED else "✗"
        summary_lines.append(f"  {icon} {t.id} — {t.status.value} (rounds: {t.current_round})")
        if t.error:
            summary_lines.append(f"    error: {t.error}")

    summary_lines.append(f"\n{len(merged)} merged, {len(failed)} failed")
    summary = "\n".join(summary_lines)

    log_summary(repo_path, state.run_id, summary)

    # Print results
    print("\n" + "=" * 50)
    print(f"  agentflow complete")
    print("=" * 50)
    for t in state.tasks:
        icon = "✓" if t.status == TaskStatus.MERGED else "✗"
        print(f"  {icon} {t.id:30s}  {t.current_round} rounds   {t.status.value}")
    print(f"\n  Logs: .agentflow/logs/{state.run_id}/")
    print("=" * 50)

    # Restore stash if we stashed
    if stashed:
        git_ops.stash_pop(cwd=repo_path)

    # --- NOTIFY ---
    notify(state)


def _cmd_status():
    from .progress import show_status
    from . import git_ops

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        repo_path = "."

    show_status(repo_path)


def _cmd_log():
    from .state import load_state
    from . import git_ops

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        repo_path = "."

    state = load_state(repo_path)
    if state is None:
        print("No run logs found.")
        return

    log_dir = Path(repo_path) / ".agentflow" / "logs" / state.run_id
    summary = log_dir / "summary.md"

    if summary.exists():
        print(summary.read_text())
    else:
        print(f"Run {state.run_id} — {state.status}")
        print(f"Logs at: {log_dir}")

        # List task logs
        for task in state.tasks:
            task_dir = log_dir / task.id
            if task_dir.exists():
                files = sorted(task_dir.iterdir())
                print(f"\n  {task.id}:")
                for f in files:
                    print(f"    {f.name}")


async def _cmd_resume():
    from .config import load_config
    from .scheduler import schedule
    from .worker import run_workers
    from .merger import merge_all
    from .notifier import notify
    from .state import load_state, save_state
    from .models import TaskStatus
    from . import git_ops

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        print("Not in a git repository.")
        sys.exit(1)

    state = load_state(repo_path)
    if state is None:
        print("No run to resume.")
        sys.exit(1)

    if state.status == "completed":
        print("Last run already completed. Start a new one.")
        sys.exit(0)

    config = load_config(repo_path)

    # Find incomplete tasks
    incomplete = [t for t in state.tasks if t.status not in (
        TaskStatus.MERGED, TaskStatus.APPROVED, TaskStatus.FAILED, TaskStatus.ESCALATED
    )]

    if not incomplete:
        # Just need to merge
        print("All tasks done. Merging...")
        await merge_all(state, config)
    else:
        print(f"Resuming {len(incomplete)} incomplete task(s)...")
        batches = schedule(incomplete)

        for batch in batches:
            await run_workers(batch.tasks, config, state)

        await merge_all(state, config)

    state.status = "completed"
    state.finished_at = datetime.now().isoformat()
    save_state(state)

    notify(state)
    print("Resume complete.")


def _cmd_config():
    from .config import set_config_value, load_config

    if len(sys.argv) < 4:
        # Show current config
        config = load_config(".")
        for field_name, field_val in config.__dataclass_fields__.items():
            print(f"  {field_name}: {getattr(config, field_name)}")
        return

    key = sys.argv[2]
    value = sys.argv[3]
    set_config_value(key, value)
    print(f"  {key} = {value}")
