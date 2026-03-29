from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentflow.repo_analysis import analyze_repository


class RepoAnalysisTests(unittest.TestCase):
    def test_analyze_repository_resolves_relative_and_absolute_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "pkg/__init__.py": "",
                "pkg/util.py": "VALUE = 1\n",
                "pkg/service.py": (
                    "from . import util\n"
                    "from .sub.worker import run\n"
                    "import pkg.util\n"
                ),
                "pkg/sub/__init__.py": "",
                "pkg/sub/worker.py": "from .. import util\n",
            }
            for relative_path, content in files.items():
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            analysis = analyze_repository(root.as_posix(), tracked_files=list(files))

            self.assertEqual(
                analysis.imports["pkg/service.py"],
                {"pkg/sub/worker.py", "pkg/util.py"},
            )
            self.assertEqual(
                analysis.imports["pkg/sub/worker.py"],
                {"pkg/util.py"},
            )

    def test_apply_task_dependencies_adds_import_based_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "pkg/__init__.py": "",
                "pkg/core.py": "VALUE = 1\n",
                "pkg/api.py": "from .core import VALUE\n",
                "pkg/other.py": "VALUE = 2\n",
            }
            for relative_path, content in files.items():
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            analysis = analyze_repository(root.as_posix(), tracked_files=list(files))
            tasks = [
                {
                    "id": "change-core",
                    "title": "Update core",
                    "spec": "Change pkg/core.py",
                    "files": ["pkg/core.py"],
                    "depends_on": [],
                },
                {
                    "id": "change-api",
                    "title": "Update api",
                    "spec": "Change pkg/api.py",
                    "files": ["pkg/api.py"],
                    "depends_on": [],
                },
                {
                    "id": "change-other",
                    "title": "Update other",
                    "spec": "Change pkg/other.py",
                    "files": ["pkg/other.py"],
                    "depends_on": [],
                },
            ]

            messages = analysis.apply_task_dependencies(tasks)

            self.assertEqual(tasks[0]["depends_on"], [])
            self.assertEqual(tasks[1]["depends_on"], ["change-core"])
            self.assertEqual(tasks[2]["depends_on"], [])
            self.assertTrue(
                any("change-api" in message and "change-core" in message for message in messages)
            )


if __name__ == "__main__":
    unittest.main()
