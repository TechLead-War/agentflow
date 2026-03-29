from __future__ import annotations
import subprocess
import os
from pathlib import Path


def run_git(args: list[str], cwd: str | None = None, check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class GitError(Exception):
    pass


def has_commits(cwd: str = ".") -> bool:
    """Check if the repository has any commits."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def ensure_initial_commit(cwd: str = "."):
    """Create an initial empty commit if the repo has none."""
    if not has_commits(cwd):
        run_git(["commit", "--allow-empty", "-m", "Initial commit (agentflow)"], cwd=cwd)


def get_repo_root(cwd: str = ".") -> str:
    """Get the root of the current git repository."""
    return run_git(["rev-parse", "--show-toplevel"], cwd=cwd)


def get_current_branch(cwd: str = ".") -> str:
    """Get the currently checked-out branch name."""
    if not has_commits(cwd):
        # On an empty repo, rev-parse fails; use symbolic-ref instead.
        return run_git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def is_clean(cwd: str = ".") -> bool:
    """Check if the working tree is clean (no uncommitted changes)."""
    status = run_git(["status", "--porcelain"], cwd=cwd, check=False)
    return len(status.strip()) == 0


def create_branch(name: str, base: str = "HEAD", cwd: str = "."):
    """Create a new branch from base."""
    run_git(["branch", name, base], cwd=cwd)


def delete_branch(name: str, cwd: str = ".", force: bool = False):
    """Delete a branch."""
    flag = "-D" if force else "-d"
    run_git(["branch", flag, name], cwd=cwd, check=False)


def create_worktree(branch: str, path: str, cwd: str = "."):
    """Create a git worktree for a branch at the given path."""
    run_git(["worktree", "add", path, branch], cwd=cwd)


def remove_worktree(path: str, cwd: str = "."):
    """Remove a git worktree."""
    run_git(["worktree", "remove", path, "--force"], cwd=cwd, check=False)


def prune_worktrees(cwd: str = "."):
    """Clean up stale worktree references."""
    run_git(["worktree", "prune"], cwd=cwd, check=False)


def get_diff(branch: str, base: str = "HEAD", cwd: str = ".") -> str:
    """Get the diff between a branch and base."""
    return run_git(["diff", f"{base}...{branch}"], cwd=cwd, check=False)


def get_diff_in_worktree(worktree_path: str) -> str:
    """Get the diff of uncommitted + committed changes in a worktree vs its base."""
    # First, get committed diff from branch point
    branch = get_current_branch(cwd=worktree_path)
    diff = run_git(["diff", "HEAD"], cwd=worktree_path, check=False)

    # Also get staged changes
    staged = run_git(["diff", "--cached"], cwd=worktree_path, check=False)

    return (diff + "\n" + staged).strip()


def commit_all(message: str, cwd: str = "."):
    """Stage all changes and commit."""
    run_git(["add", "-A"], cwd=cwd)
    # Check if there's anything to commit
    status = run_git(["status", "--porcelain"], cwd=cwd, check=False)
    if status.strip():
        run_git(["commit", "-m", message], cwd=cwd)


def merge_branch(branch: str, cwd: str = ".", squash: bool = True) -> bool:
    """Merge a branch into current branch. Returns True if clean, False if conflict."""
    try:
        if squash:
            run_git(["merge", "--squash", branch], cwd=cwd)
            # Commit the squash
            run_git(["commit", "-m", f"agentflow: merge {branch}"], cwd=cwd)
        else:
            run_git(["merge", "--no-ff", branch, "-m", f"agentflow: merge {branch}"], cwd=cwd)
        return True
    except GitError:
        # Conflict — abort the merge
        run_git(["merge", "--abort"], cwd=cwd, check=False)
        return False


def rebase_onto(base: str, cwd: str = ".") -> bool:
    """Rebase the current branch onto base. Returns True if clean, False if conflict."""
    try:
        run_git(["rebase", base], cwd=cwd)
        return True
    except GitError:
        run_git(["rebase", "--abort"], cwd=cwd, check=False)
        return False


def get_file_tree(cwd: str = ".", max_depth: int = 4) -> str:
    """Get a tree-like listing of tracked files."""
    files = run_git(["ls-files"], cwd=cwd, check=False)
    if not files:
        return ""

    lines = files.split("\n")
    # Truncate if too many files
    if len(lines) > 200:
        return "\n".join(lines[:200]) + f"\n... ({len(lines) - 200} more files)"
    return files


def get_changed_files(branch: str, base: str, cwd: str = ".") -> list[str]:
    """Get list of files changed between base and branch."""
    output = run_git(["diff", "--name-only", f"{base}...{branch}"], cwd=cwd, check=False)
    return [f for f in output.split("\n") if f.strip()]


def checkout(branch: str, cwd: str = "."):
    """Switch to a branch."""
    run_git(["checkout", branch], cwd=cwd)


def stash(cwd: str = ".") -> bool:
    """Stash changes. Returns True if anything was stashed."""
    output = run_git(["stash"], cwd=cwd, check=False)
    return "No local changes" not in output


def stash_pop(cwd: str = "."):
    """Pop stashed changes."""
    run_git(["stash", "pop"], cwd=cwd, check=False)
