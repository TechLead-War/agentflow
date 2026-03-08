from __future__ import annotations
from .models import Task, TaskStatus, RunState
from .config import Config
from .prompts import PromptBuilder
from .state import save_state
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

        clean = git_ops.merge_branch(task.branch, cwd=repo_path, squash=True)

        if clean:
            task.status = TaskStatus.MERGED
        else:
            # Conflict — try to resolve with an agent
            resolved = await _resolve_conflict(task, state, config)
            if resolved:
                task.status = TaskStatus.MERGED
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


def _cleanup_branches(state: RunState, config: Config):
    """Delete all temporary branches created during the run."""
    repo_path = state.repo_path

    for task in state.tasks:
        # Preserve branches for failed/escalated tasks so they can be inspected.
        if task.branch and task.status == TaskStatus.MERGED:
            git_ops.delete_branch(task.branch, cwd=repo_path, force=True)

    git_ops.prune_worktrees(cwd=repo_path)
