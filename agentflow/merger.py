from __future__ import annotations
import tempfile
from pathlib import Path
from .models import Task, TaskStatus, RunState
from .config import Config
from .prompts import PromptBuilder
from .state import log_round, save_state
from . import git_ops


async def merge_all(state: RunState, config: Config):
    """Merge all approved task branches into the base branch."""
    repo_path = state.repo_path
    base_branch = state.base_branch

    # Make sure we're on the base branch
    current = git_ops.get_current_branch(cwd=repo_path)
    if current != base_branch:
        git_ops.checkout(base_branch, cwd=repo_path)

    approved = [t for t in state.tasks if t.status == TaskStatus.APPROVED]

    for task in approved:
        if not task.branch:
            continue

        task.status = TaskStatus.MERGING
        save_state(state)

        ready, no_changes, message = await _refresh_branch_before_merge(task, state, config)
        if not ready:
            task.status = TaskStatus.ESCALATED
            task.error = message or (
                f"Pre-merge refresh failed for branch {task.branch}. "
                "Manual review needed."
            )
            save_state(state)
            continue

        if no_changes:
            task.status = TaskStatus.MERGED
            task.error = None
            if message:
                task.feedback = message
            save_state(state)
            continue

        clean = git_ops.merge_branch(task.branch, cwd=repo_path, squash=True)

        if clean:
            task.status = TaskStatus.MERGED
            task.error = None
        else:
            # Conflict — try to resolve with an agent
            resolved = await _resolve_conflict(task, state, config)
            if resolved:
                task.status = TaskStatus.MERGED
                task.error = None
            else:
                task.status = TaskStatus.ESCALATED
                task.error = f"Merge conflict on branch {task.branch}. Manual resolution needed."

        save_state(state)

    # Cleanup branches
    if config.cleanup_branches:
        _cleanup_branches(state, config)


async def _resolve_conflict(task: Task, state: RunState, config: Config) -> bool:
    """Attempt to auto-resolve a merge conflict using a coding agent."""
    from .agents import ClaudeAgent, CodexAgent

    repo_path = state.repo_path
    if config.agent == "codex":
        agent = CodexAgent()
    else:
        agent = ClaudeAgent()

    prompt = PromptBuilder.build_merge_prompt(
        branch=task.branch,
        base=state.base_branch,
        title=task.title,
    )

    try:
        # Need to attempt the merge again to get conflict markers
        git_ops.run_git(["merge", "--squash", task.branch], cwd=repo_path, check=False)

        await agent.run(prompt, repo_path)
        unresolved = git_ops.run_git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=repo_path,
            check=False,
        )
        if unresolved.strip():
            git_ops.run_git(["merge", "--abort"], cwd=repo_path, check=False)
            git_ops.run_git(["reset", "--merge"], cwd=repo_path, check=False)
            return False

        git_ops.commit_all(f"agentflow: resolve conflict for {task.id}", cwd=repo_path)
        return True
    except Exception:
        git_ops.run_git(["merge", "--abort"], cwd=repo_path, check=False)
        git_ops.run_git(["reset", "--merge"], cwd=repo_path, check=False)
        return False


async def _refresh_branch_before_merge(
    task: Task,
    state: RunState,
    config: Config,
) -> tuple[bool, bool, str | None]:
    """Rebase an approved branch onto the latest base and re-review it.

    Returns:
        (ready_to_merge, no_changes_remaining, message)
    """
    repo_path = state.repo_path
    base_branch = state.base_branch
    branch = task.branch
    worktree_dir = Path(tempfile.gettempdir()) / f"agentflow-mergecheck-{task.id}"

    try:
        if worktree_dir.exists():
            git_ops.remove_worktree(str(worktree_dir), cwd=repo_path)
            if worktree_dir.exists():
                import shutil
                shutil.rmtree(worktree_dir, ignore_errors=True)
            git_ops.prune_worktrees(cwd=repo_path)

        git_ops.create_worktree(branch, str(worktree_dir), cwd=repo_path)

        rebased = git_ops.rebase_onto(base_branch, cwd=str(worktree_dir))
        if not rebased:
            return (
                False,
                False,
                f"Branch '{branch}' could not be rebased onto '{base_branch}'. "
                "Manual resolution needed before merge.",
            )

        diff = git_ops.get_diff(branch, base=base_branch, cwd=repo_path)
        merge_review_round = task.current_round + 1 if task.current_round > 0 else 1

        if not diff.strip():
            message = (
                f"Branch '{branch}' has no remaining diff after rebasing onto "
                f"'{base_branch}'. Changes are already present in the base branch."
            )
            log_round(repo_path, state.run_id, task.id, merge_review_round, "merge-review", message)
            return True, True, message

        reviewer = _get_merge_reviewer(task, config)
        result = await reviewer.review(
            task_spec=task.spec,
            diff=diff,
            round_num=merge_review_round,
            previous_feedback=task.feedback,
        )
        log_round(
            repo_path,
            state.run_id,
            task.id,
            merge_review_round,
            "merge-review",
            result.to_log_text(),
        )

        if result.approved:
            task.feedback = None
            return True, False, None

        task.feedback = result.feedback
        decision = result.decision.value
        return (
            False,
            False,
            f"Pre-merge review returned '{decision}' after rebasing '{branch}' onto "
            f"'{base_branch}'. Branch preserved for manual review.",
        )
    except Exception as e:
        return (
            False,
            False,
            f"Pre-merge refresh failed for branch '{branch}': {e}. "
            "Branch preserved for manual review.",
        )
    finally:
        git_ops.remove_worktree(str(worktree_dir), cwd=repo_path)
        git_ops.prune_worktrees(cwd=repo_path)


def _get_merge_reviewer(task: Task, config: Config):
    """Match the normal reviewer-selection logic for pre-merge re-review."""
    from .models import AgentType
    from .reviewers import ClaudeReviewer, CodexReviewer, HumanReviewer

    reviewer_type = config.reviewer if config.reviewer == "human" else task.reviewer

    if isinstance(reviewer_type, str):
        normalized = reviewer_type.strip().lower()
        if normalized == "human":
            return HumanReviewer()
        reviewer_type = AgentType(normalized)

    if reviewer_type == AgentType.CLAUDE:
        return ClaudeReviewer(consistency_passes=config.review_consistency)
    return CodexReviewer(consistency_passes=config.review_consistency)


def _cleanup_branches(state: RunState, config: Config):
    """Delete all temporary branches created during the run."""
    repo_path = state.repo_path

    for task in state.tasks:
        # Preserve branches for failed/escalated tasks so they can be inspected.
        if task.branch and task.status == TaskStatus.MERGED:
            git_ops.delete_branch(task.branch, cwd=repo_path, force=True)

    git_ops.prune_worktrees(cwd=repo_path)
