from __future__ import annotations
import asyncio
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from .models import Task, TaskStatus, AgentType, RunState, ReviewResult
from .config import Config
from .prompts import PromptBuilder, PromptStrategy
from .state import save_state, update_task_status, log_round
from . import git_ops
from .agents import ClaudeAgent, CodexAgent
from .reviewers import CodexReviewer, ClaudeReviewer, HumanReviewer


def _get_agent(agent_type: AgentType):
    if agent_type == AgentType.CLAUDE:
        return ClaudeAgent()
    return CodexAgent()


def _get_reviewer(reviewer_type: AgentType | str, consistency_passes: int = 1):
    if isinstance(reviewer_type, str):
        reviewer_type = AgentType(reviewer_type) if reviewer_type != "human" else None
        if reviewer_type is None:
            return HumanReviewer()

    if reviewer_type == AgentType.CLAUDE:
        return ClaudeReviewer(consistency_passes=consistency_passes)
    return CodexReviewer(consistency_passes=consistency_passes)


async def run_workers(
    tasks: list[Task],
    config: Config,
    state: RunState,
    max_parallel: int | None = None,
):
    """Run a batch of tasks in parallel, each with its own agent-reviewer loop."""
    limit = max_parallel or config.max_parallel
    semaphore = asyncio.Semaphore(limit)

    async def bounded_worker(task: Task):
        async with semaphore:
            await _run_single_worker(task, config, state)

    results = await asyncio.gather(
        *(bounded_worker(task) for task in tasks),
        return_exceptions=True,
    )

    # Catch any unhandled exceptions from gather so they don't break the flow
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            task = tasks[i]
            if task.status not in (TaskStatus.FAILED, TaskStatus.ESCALATED,
                                   TaskStatus.APPROVED, TaskStatus.MERGED):
                task.status = TaskStatus.FAILED
                task.error = f"Unexpected error: {result}"
                save_state(state)


async def _run_single_worker(task: Task, config: Config, state: RunState):
    """The core feedback loop for a single task."""
    repo_path = state.repo_path
    base_branch = state.base_branch
    branch = f"{config.branch_prefix}-{task.id}"
    task.branch = branch

    # Create branch and worktree
    worktree_dir = str(Path(tempfile.gettempdir()) / f"agentflow-{task.id}")
    try:
        git_ops.create_branch(branch, base=base_branch, cwd=repo_path)
        git_ops.create_worktree(branch, worktree_dir, cwd=repo_path)
        task.worktree_path = worktree_dir
    except git_ops.GitError as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        save_state(state)
        return

    agent = _get_agent(task.agent)
    reviewer_type = config.reviewer if config.reviewer == "human" else task.reviewer
    reviewer = _get_reviewer(reviewer_type, consistency_passes=config.review_consistency)

    feedback = None
    previous_diff = None
    timeout = getattr(config, 'agent_timeout_sec', 300)

    try:
        for round_num in range(1, task.max_rounds + 1):
            task.current_round = round_num

            # --- AGENT PHASE ---
            update_task_status(state, task.id, TaskStatus.AGENT_WORKING, round_num=round_num)

            # Resolve prompt strategy from config (auto, or explicit override)
            strategy_override = _get_strategy_override(config)
            prompt = PromptBuilder.build_agent_prompt(
                task, feedback, round_num, strategy_override,
            )

            try:
                agent_output = await asyncio.wait_for(
                    agent.run(prompt, worktree_dir),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                task.status = TaskStatus.ESCALATED
                task.error = f"Agent timed out after {timeout}s (round {round_num}). Branch preserved for manual review."
                save_state(state)
                return
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = f"Agent error (round {round_num}): {e}"
                save_state(state)
                return

            # Commit any changes the agent made
            git_ops.commit_all(
                f"agentflow: {task.id} round {round_num}",
                cwd=worktree_dir,
            )

            # Log agent output
            log_round(repo_path, state.run_id, task.id, round_num, "agent", agent_output)

            # --- REVIEW PHASE ---
            update_task_status(state, task.id, TaskStatus.REVIEWING, round_num=round_num)

            diff = git_ops.get_diff(branch, base=base_branch, cwd=repo_path)

            if not diff.strip():
                # Agent made no changes
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          "No changes detected. Skipping review.")
                if round_num == 1:
                    # Handle no-op tasks where the requested change already exists.
                    if _agent_output_indicates_noop_complete(agent_output):
                        task.status = TaskStatus.MERGED
                        task.error = None
                        task.feedback = "No changes required; task already satisfied."
                        # Nothing to merge for no-op tasks.
                        task.branch = ""
                        save_state(state)
                        return

                    task.status = TaskStatus.FAILED
                    task.error = "Agent produced no changes."
                    save_state(state)
                    return

                # On subsequent rounds with no diff against base, treat as done.
                task.status = TaskStatus.MERGED
                task.error = None
                task.feedback = "No additional changes required."
                task.branch = ""
                save_state(state)
                return

            # --- STALE FEEDBACK DETECTION ---
            # If the diff hasn't meaningfully changed from last round, the agent
            # is stuck in a loop. Auto-approve to avoid wasting cycles.
            if previous_diff and round_num > 1:
                similarity = SequenceMatcher(None, previous_diff, diff).ratio()
                if similarity > 0.95:
                    log_round(repo_path, state.run_id, task.id, round_num, "review",
                              f"Stale loop detected (diff similarity: {similarity:.0%}). "
                              f"Auto-approving to prevent spin.")
                    task.status = TaskStatus.APPROVED
                    task.feedback = (
                        f"Auto-approved: agent made no meaningful progress after round "
                        f"{round_num - 1}. Diff similarity {similarity:.0%}."
                    )
                    save_state(state)
                    return
            previous_diff = diff

            try:
                result = await reviewer.review(
                    task_spec=task.spec,
                    diff=diff,
                    round_num=round_num,
                    previous_feedback=feedback,
                )
            except Exception as e:
                # Reviewer failed — treat as approved to avoid blocking
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          f"Reviewer error: {e}. Auto-approving.")
                result = ReviewResult(approved=True, feedback=f"Reviewer error: {e}")

            log_round(repo_path, state.run_id, task.id, round_num, "review",
                      f"{'LGTM' if result.approved else 'FEEDBACK:'}\n{result.feedback}")

            if result.approved:
                task.status = TaskStatus.APPROVED
                task.feedback = None
                save_state(state)
                return

            # --- CHECK: only blockers should cause another round ---
            if round_num > 1 and not _has_blockers(result.feedback):
                # Only suggestions remain on round 2+, good enough to merge
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          "Only suggestions remain after round 1. Auto-approving.")
                task.status = TaskStatus.APPROVED
                task.feedback = result.feedback
                save_state(state)
                return

            # --- FEEDBACK LOOP ---
            feedback = result.feedback
            update_task_status(
                state, task.id, TaskStatus.ITERATING,
                round_num=round_num, feedback=feedback,
            )

        # Exhausted all rounds without approval — escalate gracefully
        task.status = TaskStatus.ESCALATED
        task.error = (
            f"Not approved after {task.max_rounds} rounds. "
            f"Branch '{branch}' preserved for manual review."
        )
        save_state(state)

    finally:
        # Clean up worktree (branch stays for merging)
        git_ops.remove_worktree(worktree_dir, cwd=repo_path)
        git_ops.prune_worktrees(cwd=repo_path)


def _get_strategy_override(config: Config) -> PromptStrategy | None:
    """Resolve prompt strategy override from config."""
    raw = config.prompt_strategy
    if raw == "auto":
        return None  # Let PromptBuilder auto-select based on complexity
    try:
        return PromptStrategy(raw)
    except ValueError:
        return None


def _has_blockers(feedback: str) -> bool:
    """Check if the feedback contains any blocker-severity issues."""
    if not feedback:
        return False
    text = feedback.lower()
    # Look for explicit blocker markers
    if "blocker" in text:
        return True
    # Look for severity indicators that suggest blocking issues
    blocking_phrases = (
        # Crash / break indicators
        "will crash", "will break", "will fail", "would crash", "would break",
        "would fail", "causes crash", "causes failure",
        # Compilation / syntax
        "syntax error", "compilation error", "compile error", "won't compile",
        "does not compile", "parse error",
        # Runtime errors
        "runtime error", "throws exception", "unhandled exception",
        "stack overflow", "infinite loop", "deadlock",
        # Security
        "security vulnerability", "sql injection", "xss", "path traversal",
        "command injection", "insecure",
        # Reference errors
        "missing import", "undefined variable", "undefined function",
        "undeclared", "not defined", "name error", "reference error",
        "module not found", "import error",
        # Type errors
        "type error", "type mismatch", "wrong type", "incompatible type",
        # Null / bounds
        "null pointer", "nil pointer", "none type", "nonetype",
        "index out of", "out of bounds", "key error", "index error",
        # Logic errors
        "incorrect logic", "wrong result", "incorrect result",
        "logic error", "off-by-one", "data loss", "data corruption",
        "race condition",
        # Explicit severity
        "must fix", "critical", "severity: blocker", "breaking change",
        "this won't work", "this doesn't work", "this is broken",
        "fundamentally wrong", "completely wrong",
    )
    return any(phrase in text for phrase in blocking_phrases)


def _agent_output_indicates_noop_complete(agent_output: str) -> bool:
    """Heuristic to detect when the agent intentionally made no edits."""
    text = (agent_output or "").lower()
    markers = (
        "already satisfied",
        "already implemented",
        "already present",
        "already exists",
        "no changes needed",
        "no changes necessary",
        "no file edits were necessary",
        "task is already completed",
    )
    return any(marker in text for marker in markers)
