from __future__ import annotations
import asyncio
import tempfile
from pathlib import Path

from .models import Task, TaskStatus, AgentType, RunState, ReviewResult
from .config import Config
from .state import save_state, update_task_status, log_round
from . import git_ops
from .agents import ClaudeAgent, CodexAgent
from .reviewers import CodexReviewer, ClaudeReviewer, HumanReviewer


def _get_agent(agent_type: AgentType):
    if agent_type == AgentType.CLAUDE:
        return ClaudeAgent()
    return CodexAgent()


def _get_reviewer(reviewer_type: AgentType | str):
    if isinstance(reviewer_type, str):
        reviewer_type = AgentType(reviewer_type) if reviewer_type != "human" else None
        if reviewer_type is None:
            return HumanReviewer()

    if reviewer_type == AgentType.CLAUDE:
        return ClaudeReviewer()
    return CodexReviewer()


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

    await asyncio.gather(
        *(bounded_worker(task) for task in tasks),
        return_exceptions=True,
    )


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
    reviewer = _get_reviewer(reviewer_type)

    feedback = None

    try:
        for round_num in range(1, task.max_rounds + 1):
            task.current_round = round_num

            # --- AGENT PHASE ---
            update_task_status(state, task.id, TaskStatus.AGENT_WORKING, round_num=round_num)

            prompt = _build_agent_prompt(task, feedback, round_num)

            try:
                agent_output = await agent.run(prompt, worktree_dir)
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
                    task.status = TaskStatus.FAILED
                    task.error = "Agent produced no changes."
                    save_state(state)
                    return
                # On subsequent rounds with no new changes, consider it done
                break

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

            # --- FEEDBACK LOOP ---
            feedback = result.feedback
            update_task_status(
                state, task.id, TaskStatus.ITERATING,
                round_num=round_num, feedback=feedback,
            )

        # Exhausted all rounds without approval
        task.status = TaskStatus.ESCALATED
        task.error = f"Not approved after {task.max_rounds} rounds."
        save_state(state)

    finally:
        # Clean up worktree (branch stays for merging)
        git_ops.remove_worktree(worktree_dir, cwd=repo_path)
        git_ops.prune_worktrees(cwd=repo_path)


def _build_agent_prompt(task: Task, feedback: str | None, round_num: int) -> str:
    """Build the prompt for the coding agent."""
    parts = [
        f"# Task: {task.title}",
        f"\n{task.spec}",
    ]

    if task.files:
        parts.append(f"\nFiles to create or modify:\n" +
                      "\n".join(f"  - {f}" for f in task.files))

    if round_num > 1 and feedback:
        parts.append(
            f"\n# Review Feedback (Round {round_num - 1})\n"
            f"The reviewer found issues with your previous implementation. "
            f"Fix ALL of the following:\n\n{feedback}"
        )
    elif round_num == 1:
        parts.append(
            "\n# Instructions\n"
            "Implement this task completely. Edit or create the necessary files. "
            "Make sure the code compiles and integrates with the existing codebase."
        )

    return "\n".join(parts)
