from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow.config import Config
from agentflow import git_ops
from agentflow.merger import _refresh_branch_before_merge
from agentflow.models import AgentType, ReviewDecision, ReviewResult, RunState, Task, TaskComplexity


class _FakeReviewer:
    def __init__(self):
        self.calls: list[dict] = []

    async def review(self, task_spec: str, diff: str, round_num: int, previous_feedback: str | None = None):
        self.calls.append(
            {
                "task_spec": task_spec,
                "diff": diff,
                "round_num": round_num,
                "previous_feedback": previous_feedback,
            }
        )
        return ReviewResult(
            approved=True,
            decision=ReviewDecision.KEEP,
            summary="Pre-merge review approved.",
        )


def _init_repo(root: Path):
    git_ops.run_git(["init"], cwd=root.as_posix())
    git_ops.run_git(["checkout", "-B", "main"], cwd=root.as_posix())
    git_ops.run_git(["config", "user.name", "Agentflow Tests"], cwd=root.as_posix())
    git_ops.run_git(["config", "user.email", "tests@example.com"], cwd=root.as_posix())


class MergerRefreshTests(unittest.TestCase):
    def test_rebase_onto_updates_feature_branch_to_latest_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)

            (root / "base.txt").write_text("base\n")
            git_ops.commit_all("initial", cwd=root.as_posix())

            git_ops.create_branch("feature", base="main", cwd=root.as_posix())
            git_ops.checkout("feature", cwd=root.as_posix())
            (root / "feature.txt").write_text("feature work\n")
            git_ops.commit_all("feature work", cwd=root.as_posix())
            feature_head_before = git_ops.run_git(["rev-parse", "feature"], cwd=root.as_posix())

            git_ops.checkout("main", cwd=root.as_posix())
            (root / "base2.txt").write_text("new base change\n")
            git_ops.commit_all("main update", cwd=root.as_posix())
            main_head = git_ops.run_git(["rev-parse", "main"], cwd=root.as_posix())

            git_ops.checkout("feature", cwd=root.as_posix())
            self.assertTrue(git_ops.rebase_onto("main", cwd=root.as_posix()))

            feature_head_after = git_ops.run_git(["rev-parse", "feature"], cwd=root.as_posix())
            merge_base = git_ops.run_git(["merge-base", "feature", "main"], cwd=root.as_posix())

            self.assertNotEqual(feature_head_before, feature_head_after)
            self.assertEqual(merge_base, main_head)

    def test_refresh_branch_before_merge_rebases_and_rereviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)

            (root / "base.txt").write_text("base\n")
            git_ops.commit_all("initial", cwd=root.as_posix())

            git_ops.create_branch("feature", base="main", cwd=root.as_posix())
            git_ops.checkout("feature", cwd=root.as_posix())
            (root / "feature.txt").write_text("feature work\n")
            git_ops.commit_all("feature work", cwd=root.as_posix())
            feature_head_before = git_ops.run_git(["rev-parse", "feature"], cwd=root.as_posix())

            git_ops.checkout("main", cwd=root.as_posix())
            (root / "base2.txt").write_text("new base change\n")
            git_ops.commit_all("main update", cwd=root.as_posix())
            main_head = git_ops.run_git(["rev-parse", "main"], cwd=root.as_posix())

            task = Task(
                id="feature-task",
                title="Feature task",
                spec="Add feature.txt with content.",
                files=["feature.txt"],
                depends_on=[],
                complexity=TaskComplexity.FEATURE,
                agent=AgentType.CODEX,
                reviewer=AgentType.CLAUDE,
                branch="feature",
                current_round=1,
            )
            state = RunState.create(
                prompt="implement feature task",
                tasks=[task],
                repo_path=root.as_posix(),
                base_branch="main",
            )
            config = Config()
            reviewer = _FakeReviewer()

            with mock.patch("agentflow.merger._get_merge_reviewer", return_value=reviewer):
                ready, no_changes, message = asyncio.run(
                    _refresh_branch_before_merge(task, state, config)
                )

            feature_head_after = git_ops.run_git(["rev-parse", "feature"], cwd=root.as_posix())
            merge_base = git_ops.run_git(["merge-base", "feature", "main"], cwd=root.as_posix())

            self.assertTrue(ready)
            self.assertFalse(no_changes)
            self.assertIsNone(message)
            self.assertNotEqual(feature_head_before, feature_head_after)
            self.assertEqual(merge_base, main_head)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertEqual(reviewer.calls[0]["round_num"], 2)
            self.assertIn("feature.txt", reviewer.calls[0]["diff"])


if __name__ == "__main__":
    unittest.main()
