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
  agentflow retry                      Retry escalated tasks with fresh rounds
  agentflow clean                      Clear state from previous runs
  agentflow config <key> <value>       Set a configuration value

Examples:
  agentflow "add input validation to the signup form"
  agentflow --prompt-file /tmp/prompt.txt
  cat /tmp/prompt.txt | agentflow -
  agentflow "refactor auth to use JWT, add rate limiting, write tests"
  agentflow config reviewer claude
  agentflow config max_rounds 5
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
    elif cmd == "retry":
        asyncio.run(_cmd_retry())
    elif cmd == "clean":
        _cmd_clean()
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
    from .state import load_state, save_state, log_plan, log_summary
    from .models import RunState, TaskStatus
    from . import git_ops

    repo_path = git_ops.get_repo_root(".")
    config = load_config(repo_path)

    # Check prerequisites
    has_anthropic, has_openai = check_api_keys()
    if not has_anthropic and not has_openai:
        print("Error: Install claude/codex CLI or set ANTHROPIC_API_KEY/OPENAI_API_KEY.")
        sys.exit(1)

    # Detect and clean stale runs
    prev_state = load_state(repo_path)
    if prev_state and prev_state.status not in ("completed", "failed"):
        print(f"Clearing previous run {prev_state.run_id} ({prev_state.status}).")

    # Warn if repo is dirty (but don't block)
    if not git_ops.is_clean(cwd=repo_path):
        print("Warning: Working tree has uncommitted changes. Stashing them.")
        git_ops.stash(cwd=repo_path)
        stashed = True
    else:
        stashed = False

    base_branch = git_ops.get_current_branch(cwd=repo_path)

    # Initialize state immediately so `agentflow status` works from second 0.
    state = RunState.create(prompt, [], repo_path, base_branch)
    state.status = "running"
    state.phase = "initializing"
    save_state(state)

    # Start the live display NOW — it covers the entire lifecycle.
    live = show_live_progress(state)

    try:
        with live:
            # --- PHASE: PLANNING ---
            state.phase = "planning"
            save_state(state)

            try:
                tasks = await plan(prompt, config, repo_path)
            except Exception as e:
                state.status = "failed"
                state.phase = "failed"
                state.finished_at = datetime.now().isoformat()
                save_state(state)
                log_summary(
                    repo_path,
                    state.run_id,
                    f"# agentflow run {state.run_id}\n"
                    f"Prompt: {prompt}\n\n"
                    f"## Results\n"
                    f"  \u2717 planning \u2014 failed\n"
                    f"    error: {e}\n\n"
                    f"0 merged, 1 failed",
                )
                # Live display will show "failed" phase on next refresh
                if stashed:
                    git_ops.stash_pop(cwd=repo_path)
                print(f"\nPlanning failed: {e}")
                sys.exit(1)

            # Attach planned tasks — status table now shows them.
            state.tasks = tasks
            state.phase = "scheduling"
            save_state(state)

            plan_data = {"prompt": prompt, "tasks": [t.to_dict() for t in tasks]}
            log_plan(repo_path, state.run_id, plan_data)

            # --- PHASE: SCHEDULING ---
            batches = schedule(tasks)
            state.total_batches = len(batches)
            save_state(state)

            # --- PHASE: RUNNING ---
            state.phase = "running"
            save_state(state)

            for i, batch in enumerate(batches, 1):
                state.current_batch = i
                # Mark batch tasks as queued
                for task in batch.tasks:
                    task.status = TaskStatus.QUEUED
                save_state(state)

                await run_workers(batch.tasks, config, state)

            # --- PHASE: MERGING ---
            state.phase = "merging"
            save_state(state)

            await merge_all(state, config)

            # --- PHASE: COMPLETED ---
            state.status = "completed"
            state.phase = "completed"
            state.finished_at = datetime.now().isoformat()
            save_state(state)

    except KeyboardInterrupt:
        state.status = "interrupted"
        state.phase = "interrupted"
        save_state(state)
        if stashed:
            git_ops.stash_pop(cwd=repo_path)
        print("\nInterrupted. Run 'agentflow resume' to continue.")
        sys.exit(1)

    # --- SUMMARY ---
    merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
    failed = [t for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)]

    summary_lines = [
        f"# agentflow run {state.run_id}",
        f"Prompt: {prompt}",
        f"",
        f"## Results",
    ]

    for t in state.tasks:
        icon = "\u2713" if t.status == TaskStatus.MERGED else "\u2717"
        summary_lines.append(f"  {icon} {t.id} \u2014 {t.status.value} (rounds: {t.current_round})")
        if t.error:
            summary_lines.append(f"    error: {t.error}")

    summary_lines.append(f"\n{len(merged)} merged, {len(failed)} failed")
    summary = "\n".join(summary_lines)

    log_summary(repo_path, state.run_id, summary)

    # Print final results (outside live display)
    print(f"\n{'=' * 50}")
    print(f"  agentflow complete \u2014 {state.run_id}")
    print(f"{'=' * 50}")
    for t in state.tasks:
        icon = "\u2713" if t.status == TaskStatus.MERGED else "\u2717"
        print(f"  {icon} {t.id:30s}  {t.current_round} rounds   {t.status.value}")
    print(f"\n  {len(merged)} merged, {len(failed)} failed")
    print(f"  Logs: .agentflow/logs/{state.run_id}/")
    print(f"{'=' * 50}")

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


async def _cmd_retry():
    from .config import load_config
    from .scheduler import schedule
    from .worker import run_workers
    from .merger import merge_all
    from .notifier import notify
    from .progress import show_live_progress
    from .state import load_state, save_state, log_summary
    from .models import TaskStatus
    from . import git_ops

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        print("Not in a git repository.")
        sys.exit(1)

    state = load_state(repo_path)
    if state is None:
        print("No run to retry.")
        sys.exit(1)

    # Find escalated tasks
    escalated = [t for t in state.tasks if t.status == TaskStatus.ESCALATED]

    if not escalated:
        print("No escalated tasks to retry.")
        sys.exit(0)

    config = load_config(repo_path)

    print(f"Retrying {len(escalated)} escalated task(s)...")
    retry_ids = {t.id for t in escalated}
    for t in escalated:
        # Reset task state for a fresh run
        t.status = TaskStatus.PENDING
        t.current_round = 0
        t.max_rounds = config.max_rounds
        t.error = None
        t.feedback = None
        # Strip dependencies on tasks not in the retry set (already completed)
        t.depends_on = [d for d in t.depends_on if d in retry_ids]
        # Clean up old branch so worker creates a fresh one
        if t.branch:
            try:
                git_ops.delete_branch(t.branch, cwd=repo_path)
            except Exception:
                pass  # Branch may already be gone
            t.branch = ""

    state.status = "running"
    state.phase = "running"
    state.finished_at = None
    save_state(state)

    live = show_live_progress(state)

    try:
        with live:
            batches = schedule(escalated)
            state.total_batches = len(batches)

            for i, batch in enumerate(batches, 1):
                state.current_batch = i
                for task in batch.tasks:
                    task.status = TaskStatus.QUEUED
                save_state(state)

                await run_workers(batch.tasks, config, state)

            # Merge any newly approved tasks
            state.phase = "merging"
            save_state(state)

            await merge_all(state, config)

            state.status = "completed"
            state.phase = "completed"
            state.finished_at = datetime.now().isoformat()
            save_state(state)

    except KeyboardInterrupt:
        state.status = "interrupted"
        state.phase = "interrupted"
        save_state(state)
        print("\nInterrupted. Run 'agentflow retry' again to continue.")
        sys.exit(1)

    # Summary
    merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
    still_escalated = [t for t in state.tasks if t.status == TaskStatus.ESCALATED]
    failed = [t for t in state.tasks if t.status == TaskStatus.FAILED]

    print(f"\n{'=' * 50}")
    print(f"  agentflow retry complete")
    print(f"{'=' * 50}")
    for t in escalated:
        icon = "\u2713" if t.status in (TaskStatus.MERGED, TaskStatus.APPROVED) else "\u2717"
        print(f"  {icon} {t.id:30s}  {t.current_round} rounds   {t.status.value}")
    print(f"\n  {len(merged)} total merged, {len(still_escalated)} still escalated, {len(failed)} failed")
    print(f"{'=' * 50}")

    notify(state)


def _cmd_clean():
    from .state import load_state, get_state_dir
    from . import git_ops
    import shutil

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        repo_path = "."

    state = load_state(repo_path)
    if state is None:
        print("Nothing to clean.")
        return

    state_dir = get_state_dir(repo_path)
    state_file = state_dir / "state.json"

    print(f"Clearing run {state.run_id} (status: {state.status})")

    # Remove state file
    if state_file.exists():
        state_file.unlink()

    # Remove logs if --logs flag passed
    if len(sys.argv) > 2 and sys.argv[2] == "--logs":
        log_dir = state_dir / "logs" / state.run_id
        if log_dir.exists():
            shutil.rmtree(log_dir)
            print(f"Removed logs: {log_dir}")

    print("Done. Ready for a new run.")


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
