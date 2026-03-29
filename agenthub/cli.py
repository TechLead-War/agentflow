from __future__ import annotations
import sys
import asyncio
from datetime import datetime
from pathlib import Path


USAGE = """\
AgentHub — automated coding agents with built-in code review

Usage:
  agenthub "your task description"    Run a task
  agenthub --prompt-file <path>       Run a task from a prompt file
  agenthub -                          Run a task from stdin
  agenthub status                     Show progress of current/last run
  agenthub log                        Show logs from last run
  agenthub resume                     Resume an interrupted run
  agenthub retry                      Retry escalated tasks with fresh rounds
  agenthub clean                      Clear state from previous runs
  agenthub config <key> <value>       Set a configuration value

Examples:
  agenthub "add input validation to the signup form"
  agenthub --prompt-file /tmp/prompt.txt
  cat /tmp/prompt.txt | agenthub -
  agenthub "refactor auth to use JWT, add rate limiting, write tests"
  agenthub config reviewer claude
  agenthub config max_rounds 5
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


async def _run_validation_phase(state, config, prompt, repo_path):
    """Shared validation logic used by run, resume, and retry.

    Returns the final ValidationResult (or None if skipped/errored).
    On failure: saves work to a safe branch, rolls back base branch.
    """
    from .validator import validate as run_validation, create_fix_task
    from .worker import run_workers
    from .merger import merge_all
    from .state import save_state, log_validation
    from .models import TaskStatus
    from . import git_ops
    import logging as _logging

    state.phase = "validating"
    state.validation_attempt = 1
    save_state(state)

    validation_result = None
    try:
        validation_result = await run_validation(prompt, state, config)
    except Exception as val_err:
        _logging.getLogger(__name__).warning(
            "Validation error, skipping: %s", val_err,
        )

    if validation_result is not None:
        log_validation(repo_path, state.run_id, 1, validation_result.to_dict())

    if validation_result is not None and not validation_result.passed:
        # --- AUTO-FIX ATTEMPT ---
        fix_task = create_fix_task(validation_result, prompt, config)
        state.tasks.append(fix_task)
        state.phase = "running"
        fix_task.status = TaskStatus.QUEUED
        save_state(state)

        await run_workers([fix_task], config, state)

        state.phase = "merging"
        save_state(state)
        await merge_all(state, config)

        # Validate again (attempt 2)
        state.phase = "validating"
        state.validation_attempt = 2
        save_state(state)

        validation_result_2 = None
        try:
            validation_result_2 = await run_validation(prompt, state, config)
        except Exception as val_err:
            _logging.getLogger(__name__).warning(
                "Second validation error: %s", val_err,
            )

        if validation_result_2 is not None:
            log_validation(repo_path, state.run_id, 2, validation_result_2.to_dict())

        if validation_result_2 is None or not validation_result_2.passed:
            # Save merged work to a safe branch before rolling back
            safe_branch = f"{config.branch_prefix}-validation-failed-{state.run_id}"
            git_ops.run_git(["branch", safe_branch], cwd=repo_path, check=False)

            # Roll back base branch to clean state
            if state.base_sha:
                git_ops.run_git(["reset", "--hard", state.base_sha], cwd=repo_path)

            state.status = "validation_failed"
            state.phase = "validation_failed"
            save_state(state)
            return validation_result_2 or validation_result
        else:
            return validation_result_2

    return validation_result


async def _cmd_run(prompt: str):
    from .config import load_config, check_api_keys
    from .planner import plan
    from .repo_analysis import analyze_repository
    from .scheduler import schedule
    from .worker import run_workers
    from .merger import merge_all
    from .notifier import notify
    from .progress import show_live_progress
    from .state import load_state, save_state, log_plan, log_research, log_summary
    from .models import RunState, TaskStatus, GateResult
    from . import git_ops

    repo_path = git_ops.get_repo_root(".")
    git_ops.ensure_initial_commit(cwd=repo_path)
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
    base_sha = git_ops.run_git(["rev-parse", "HEAD"], cwd=repo_path)

    # Initialize state immediately so `agenthub status` works from second 0.
    state = RunState.create(prompt, [], repo_path, base_branch)
    state.base_sha = base_sha
    state.status = "running"
    state.phase = "initializing"
    save_state(state)

    # Start the live display NOW — it covers the entire lifecycle.
    live = show_live_progress(state)

    try:
        try:
            with live:
                # --- PHASE: PLANNING ---
                state.phase = "planning"
                save_state(state)

                try:
                    repo_analysis = analyze_repository(repo_path)

                    # --- PHASE: GATE + RESEARCH ---
                    research_brief = None
                    gate_result = None
                    if config.research_enabled:
                        from .gate import classify_complexity
                        from .researcher import research as run_research

                        state.phase = "gating"
                        save_state(state)

                        try:
                            gate_result = await classify_complexity(prompt, repo_analysis, config)
                        except Exception as gate_err:
                            import logging as _logging
                            _logging.getLogger(__name__).warning(
                                "Complexity gate failed, skipping research: %s", gate_err,
                            )
                            gate_result = GateResult(
                                is_complex=False, confidence=0.0,
                                reason=f"Gate error: {gate_err}", signals=[],
                            )

                        if gate_result.is_complex:
                            state.phase = "researching"
                            save_state(state)

                            try:
                                research_brief = await run_research(
                                    prompt, repo_analysis, config, repo_path,
                                )
                            except Exception as res_err:
                                import logging as _logging
                                _logging.getLogger(__name__).warning(
                                    "Research phase failed, proceeding without brief: %s",
                                    res_err,
                                )

                        log_research(
                            repo_path, state.run_id,
                            gate_result.to_dict(),
                            research_brief.to_dict() if research_brief else None,
                        )

                    tasks = await plan(
                        prompt, config, repo_path,
                        analysis=repo_analysis,
                        research_brief=research_brief,
                    )
                except Exception as e:
                    state.status = "failed"
                    state.phase = "failed"
                    state.finished_at = datetime.now().isoformat()
                    save_state(state)
                    log_summary(
                        repo_path,
                        state.run_id,
                        f"# AgentHub run {state.run_id}\n"
                        f"Prompt: {prompt}\n\n"
                        f"## Results\n"
                        f"  \u2717 planning \u2014 failed\n"
                        f"    error: {e}\n\n"
                        f"0 merged, 1 failed",
                    )
                    # Live display will show "failed" phase on next refresh
                    print(f"\nPlanning failed: {e}")
                    sys.exit(1)

                # Attach planned tasks — status table now shows them.
                state.tasks = tasks
                state.phase = "scheduling"
                save_state(state)

                plan_data = {
                    "prompt": prompt,
                    "analysis": repo_analysis.to_dict(),
                    "gate": gate_result.to_dict() if gate_result else None,
                    "research_brief": research_brief.to_dict() if research_brief else None,
                    "tasks": [t.to_dict() for t in tasks],
                }
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

                # --- PHASE: VALIDATING ---
                merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
                final_validation = None
                if config.validation_enabled and merged:
                    final_validation = await _run_validation_phase(
                        state, config, prompt, repo_path,
                    )

                if state.status != "validation_failed":
                    state.status = "completed"
                    state.phase = "completed"

                state.finished_at = datetime.now().isoformat()
                save_state(state)

        except KeyboardInterrupt:
            state.status = "interrupted"
            state.phase = "interrupted"
            state.finished_at = datetime.now().isoformat()
            save_state(state)
            print("\nInterrupted. Run 'agenthub resume' to continue.")
            sys.exit(1)
        except Exception as e:
            state.status = "failed"
            state.phase = "failed"
            state.finished_at = datetime.now().isoformat()
            save_state(state)
            log_summary(
                repo_path,
                state.run_id,
                f"# AgentHub run {state.run_id}\n"
                f"Prompt: {prompt}\n\n"
                f"## Results\n"
                f"  \u2717 run \u2014 failed\n"
                f"    error: {e}\n\n"
                f"0 merged, 1 failed",
            )
            print(f"\nRun failed: {e}")
            sys.exit(1)

        # --- SUMMARY ---
        merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
        failed = [t for t in state.tasks if t.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)]

        summary_lines = [
            f"# AgentHub run {state.run_id}",
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

        safe_branch = f"{config.branch_prefix}-validation-failed-{state.run_id}"

        if state.validation_attempt > 0:
            if state.status == "validation_failed":
                summary_lines.append(f"\n## Validation")
                summary_lines.append(f"  FAILED after {state.validation_attempt} attempt(s)")
                if final_validation and final_validation.issues:
                    summary_lines.append(f"  Issues:")
                    for issue in final_validation.issues:
                        summary_lines.append(f"    - {issue}")
                if final_validation and final_validation.summary:
                    summary_lines.append(f"  Summary: {final_validation.summary}")
                summary_lines.append(f"  Base branch rolled back to pre-run state.")
                summary_lines.append(f"  Safe branch with all merged work: {safe_branch}")
            else:
                summary_lines.append(f"\n## Validation")
                summary_lines.append(f"  Passed (attempt {state.validation_attempt})")

        summary = "\n".join(summary_lines)

        log_summary(repo_path, state.run_id, summary)

        # Print final results (outside live display)
        status_label = "complete" if state.status == "completed" else "VALIDATION FAILED"
        print(f"\n{'=' * 50}")
        print(f"  AgentHub {status_label} \u2014 {state.run_id}")
        print(f"{'=' * 50}")
        for t in state.tasks:
            icon = "\u2713" if t.status == TaskStatus.MERGED else "\u2717"
            print(f"  {icon} {t.id:30s}  {t.current_round} rounds   {t.status.value}")
        print(f"\n  {len(merged)} merged, {len(failed)} failed")
        if state.status == "validation_failed":
            print(f"\n  VALIDATION FAILED")
            if final_validation and final_validation.issues:
                print(f"  Issues found:")
                for issue in final_validation.issues:
                    print(f"    - {issue}")
            if final_validation and final_validation.summary:
                print(f"  Summary: {final_validation.summary}")
            print(f"\n  Your branch has been rolled back to its pre-run state.")
            print(f"  The merged work is saved on: {safe_branch}")
            print(f"  To inspect it: git checkout {safe_branch}")
        print(f"  Logs: .agenthub/logs/{state.run_id}/")
        print(f"{'=' * 50}")

        # --- NOTIFY ---
        if config.notify:
            notify(state)
    finally:
        # Restore stash no matter how the run exits.
        if stashed:
            git_ops.stash_pop(cwd=repo_path)


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

    log_dir = Path(repo_path) / ".agenthub" / "logs" / state.run_id
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

    # --- VALIDATION ---
    merged_tasks = [t for t in state.tasks if t.status == TaskStatus.MERGED]
    if config.validation_enabled and merged_tasks:
        final_validation = await _run_validation_phase(
            state, config, state.prompt, repo_path,
        )
        if state.status == "validation_failed":
            safe_branch = f"{config.branch_prefix}-validation-failed-{state.run_id}"
            print(f"\n  VALIDATION FAILED")
            if final_validation and final_validation.issues:
                for issue in final_validation.issues:
                    print(f"    - {issue}")
            print(f"\n  Branch rolled back. Merged work saved on: {safe_branch}")
            state.finished_at = datetime.now().isoformat()
            save_state(state)
            if config.notify:
                notify(state)
            return

    state.status = "completed"
    state.finished_at = datetime.now().isoformat()
    save_state(state)

    if config.notify:
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

    # --- Clean up stale git state from previous run ---
    # Must remove worktrees BEFORE deleting branches (git refuses to delete
    # a branch that has an active worktree).
    import shutil
    import tempfile

    print("Cleaning up stale branches and worktrees...")
    git_ops.prune_worktrees(cwd=repo_path)

    retry_ids = {t.id for t in escalated}
    for t in escalated:
        # Remove worktree if it still exists
        worktree_dir = str(Path(tempfile.gettempdir()) / f"agenthub-{t.id}")
        if Path(worktree_dir).exists():
            git_ops.remove_worktree(worktree_dir, cwd=repo_path)
            # If git worktree remove failed, force-remove the directory
            if Path(worktree_dir).exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)

        # Force-delete branch (unmerged branches need -D, not -d)
        if t.branch:
            git_ops.delete_branch(t.branch, cwd=repo_path, force=True)
        # Also try the expected branch name in case t.branch was already cleared
        expected_branch = f"{config.branch_prefix}-{t.id}"
        git_ops.delete_branch(expected_branch, cwd=repo_path, force=True)

        # Reset task state for a fresh run
        t.status = TaskStatus.PENDING
        t.current_round = 0
        t.max_rounds = config.max_rounds
        t.error = None
        t.feedback = None
        t.branch = ""
        t.worktree_path = ""
        # Strip dependencies on tasks not in the retry set (already completed)
        t.depends_on = [d for d in t.depends_on if d in retry_ids]

    git_ops.prune_worktrees(cwd=repo_path)

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

            # --- VALIDATION ---
            merged_in_retry = [t for t in state.tasks if t.status == TaskStatus.MERGED]
            final_validation = None
            if config.validation_enabled and merged_in_retry:
                final_validation = await _run_validation_phase(
                    state, config, state.prompt, repo_path,
                )

            if state.status != "validation_failed":
                state.status = "completed"
                state.phase = "completed"

            state.finished_at = datetime.now().isoformat()
            save_state(state)

    except KeyboardInterrupt:
        state.status = "interrupted"
        state.phase = "interrupted"
        save_state(state)
        print("\nInterrupted. Run 'agenthub retry' again to continue.")
        sys.exit(1)

    # Summary
    merged = [t for t in state.tasks if t.status == TaskStatus.MERGED]
    still_escalated = [t for t in state.tasks if t.status == TaskStatus.ESCALATED]
    failed = [t for t in state.tasks if t.status == TaskStatus.FAILED]

    status_label = "retry complete" if state.status == "completed" else "VALIDATION FAILED"
    print(f"\n{'=' * 50}")
    print(f"  AgentHub {status_label}")
    print(f"{'=' * 50}")
    for t in escalated:
        icon = "\u2713" if t.status in (TaskStatus.MERGED, TaskStatus.APPROVED) else "\u2717"
        print(f"  {icon} {t.id:30s}  {t.current_round} rounds   {t.status.value}")
    print(f"\n  {len(merged)} total merged, {len(still_escalated)} still escalated, {len(failed)} failed")
    if state.status == "validation_failed":
        safe_branch = f"{config.branch_prefix}-validation-failed-{state.run_id}"
        print(f"\n  VALIDATION FAILED")
        if final_validation and final_validation.issues:
            for issue in final_validation.issues:
                print(f"    - {issue}")
        print(f"\n  Branch rolled back. Merged work saved on: {safe_branch}")
        print(f"  To inspect it: git checkout {safe_branch}")
    print(f"{'=' * 50}")

    if config.notify:
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
    from .config import set_config_value, load_config, Config
    from . import git_ops

    try:
        repo_path = git_ops.get_repo_root(".")
    except git_ops.GitError:
        repo_path = "."

    if len(sys.argv) < 4:
        # Show current config
        config = load_config(repo_path)
        for field_name in config.__dataclass_fields__:
            print(f"  {field_name}: {getattr(config, field_name)}")
        return

    key = sys.argv[2]
    value = sys.argv[3]
    if key not in Config.__dataclass_fields__:
        valid = ", ".join(sorted(Config.__dataclass_fields__.keys()))
        print(f"Error: Unknown config key '{key}'.")
        print(f"Valid keys: {valid}")
        sys.exit(1)

    set_config_value(key, value)
    print(f"  {key} = {value}")
