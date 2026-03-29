from __future__ import annotations
import asyncio
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from .models import Task, TaskStatus, AgentType, ReviewDecision, RunState
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
        normalized = reviewer_type.strip().lower()
        if normalized == "human":
            return HumanReviewer()
        try:
            reviewer_type = AgentType(normalized)
        except ValueError:
            reviewer_type = AgentType.CODEX

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
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    semaphore = asyncio.Semaphore(limit)

    async def bounded_worker(task: Task):
        async with semaphore:
            await _run_single_worker(task, config, state)

    results = await asyncio.gather(
        *(bounded_worker(task) for task in tasks),
        return_exceptions=True,
    )

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

    worktree_dir = str(Path(tempfile.gettempdir()) / f"agenthub-{task.id}")
    try:
        if Path(worktree_dir).exists():
            git_ops.remove_worktree(worktree_dir, cwd=repo_path)
            if Path(worktree_dir).exists():
                import shutil
                shutil.rmtree(worktree_dir, ignore_errors=True)
            git_ops.prune_worktrees(cwd=repo_path)

        git_ops.delete_branch(branch, cwd=repo_path, force=True)
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
    previous_incremental = None
    agent_timeout = config.agent_timeout_sec
    reviewer_timeout = config.reviewer_timeout_sec
    max_turns = config.agent_max_turns

    try:
        for round_num in range(1, task.max_rounds + 1):
            task.current_round = round_num

            # --- AGENT PHASE ---
            update_task_status(state, task.id, TaskStatus.AGENT_WORKING, round_num=round_num)

            strategy_override = _get_strategy_override(config)
            prompt = PromptBuilder.build_agent_prompt(
                task, feedback, round_num, strategy_override,
            )

            pre_agent_sha = git_ops.run_git(
                ["rev-parse", "HEAD"], cwd=worktree_dir,
            ).strip()

            try:
                if agent_timeout and agent_timeout > 0:
                    agent_output = await asyncio.wait_for(
                        agent.run(prompt, worktree_dir, max_turns=max_turns),
                        timeout=agent_timeout,
                    )
                else:
                    agent_output = await agent.run(prompt, worktree_dir, max_turns=max_turns)
            except asyncio.TimeoutError:
                task.status = TaskStatus.ESCALATED
                task.error = f"Agent timed out after {agent_timeout}s (round {round_num}). Branch preserved for manual review."
                save_state(state)
                return
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = f"Agent error (round {round_num}): {e}"
                save_state(state)
                return

            git_ops.commit_all(
                f"agenthub: {task.id} round {round_num}",
                cwd=worktree_dir,
            )

            log_round(repo_path, state.run_id, task.id, round_num, "agent", agent_output)

            # --- REVIEW PHASE ---
            update_task_status(state, task.id, TaskStatus.REVIEWING, round_num=round_num)

            diff = git_ops.get_diff(branch, base=base_branch, cwd=repo_path)

            if not diff.strip():
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          "No changes detected. Skipping review.")
                if round_num == 1:
                    if _agent_output_indicates_noop_complete(agent_output):
                        task.status = TaskStatus.MERGED
                        task.error = None
                        task.feedback = "No changes required; task already satisfied."
                        task.branch = ""
                        save_state(state)
                        return

                    task.status = TaskStatus.FAILED
                    task.error = "Agent produced no changes."
                    save_state(state)
                    return

                task.status = TaskStatus.MERGED
                task.error = None
                task.feedback = "No additional changes required."
                task.branch = ""
                save_state(state)
                return

            # --- STALE FEEDBACK DETECTION ---
            # Compare INCREMENTAL changes (what the agent changed this round),
            # not total diff against base. Prevents false positives when the
            # reviewer requests small fixes to a large correct change.
            post_agent_sha = git_ops.run_git(
                ["rev-parse", "HEAD"], cwd=worktree_dir,
            ).strip()

            if pre_agent_sha != post_agent_sha:
                incremental = git_ops.run_git(
                    ["diff", pre_agent_sha, post_agent_sha],
                    cwd=worktree_dir, check=False,
                )
            else:
                incremental = ""

            if previous_incremental is not None and round_num > 1:
                if not incremental.strip():
                    log_round(repo_path, state.run_id, task.id, round_num, "review",
                              "Agent made no incremental changes this round. "
                              "Escalating — agent could not address reviewer feedback.")
                    task.status = TaskStatus.ESCALATED
                    task.feedback = (
                        f"Escalated: agent made no incremental changes on round "
                        f"{round_num} despite reviewer feedback."
                    )
                    task.error = (
                        f"Stale loop detected on round {round_num} (zero incremental changes). "
                        "Branch preserved for manual review."
                    )
                    save_state(state)
                    return

                similarity = SequenceMatcher(None, previous_incremental, incremental).ratio()
                if similarity > 0.95:
                    log_round(repo_path, state.run_id, task.id, round_num, "review",
                              f"Stale loop detected (incremental diff similarity: {similarity:.0%}). "
                              f"Agent is repeating the same changes. Escalating.")
                    task.status = TaskStatus.ESCALATED
                    task.feedback = (
                        f"Escalated: agent repeated the same incremental changes on round "
                        f"{round_num}. Incremental diff similarity {similarity:.0%}."
                    )
                    task.error = (
                        f"Stale retry loop detected on round {round_num} "
                        f"(incremental similarity {similarity:.0%}). "
                        "Branch preserved for manual review."
                    )
                    save_state(state)
                    return

            previous_incremental = incremental

            try:
                if reviewer_timeout and reviewer_timeout > 0:
                    result = await asyncio.wait_for(
                        reviewer.review(
                            task_spec=task.spec,
                            diff=diff,
                            round_num=round_num,
                            previous_feedback=feedback,
                        ),
                        timeout=reviewer_timeout,
                    )
                else:
                    result = await reviewer.review(
                        task_spec=task.spec,
                        diff=diff,
                        round_num=round_num,
                        previous_feedback=feedback,
                    )
            except asyncio.TimeoutError:
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          f"Reviewer timed out after {reviewer_timeout}s. Escalating.")
                task.status = TaskStatus.ESCALATED
                task.error = (
                    f"Reviewer timed out after {reviewer_timeout}s on round {round_num}. "
                    "Branch preserved for manual review."
                )
                save_state(state)
                return
            except Exception as e:
                log_round(repo_path, state.run_id, task.id, round_num, "review",
                          f"Reviewer error: {e}. Escalating for manual review.")
                task.status = TaskStatus.ESCALATED
                task.error = (
                    f"Reviewer error on round {round_num}: {e}. "
                    "Branch preserved for manual review."
                )
                save_state(state)
                return

            log_round(repo_path, state.run_id, task.id, round_num, "review",
                      result.to_log_text())

            if result.approved:
                task.status = TaskStatus.APPROVED
                task.feedback = None
                save_state(state)
                return

            if result.decision == ReviewDecision.REJECT:
                task.status = TaskStatus.ESCALATED
                task.error = (
                    f"Reviewer rejected the change on round {round_num}. "
                    "Branch preserved for manual review."
                )
                task.feedback = result.feedback
                save_state(state)
                return

            # --- FEEDBACK LOOP ---
            feedback = result.feedback
            update_task_status(
                state, task.id, TaskStatus.ITERATING,
                round_num=round_num, feedback=feedback,
            )

        task.status = TaskStatus.ESCALATED
        task.error = (
            f"Not approved after {task.max_rounds} rounds. "
            f"Branch '{branch}' preserved for manual review."
        )
        save_state(state)

    finally:
        git_ops.remove_worktree(worktree_dir, cwd=repo_path)
        git_ops.prune_worktrees(cwd=repo_path)


def _get_strategy_override(config: Config) -> PromptStrategy | None:
    """Resolve prompt strategy override from config."""
    raw = config.prompt_strategy
    if raw == "auto":
        return None
    try:
        return PromptStrategy(raw)
    except ValueError:
        return None


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
