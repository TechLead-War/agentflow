from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentflow.models import ResearchBrief
from agentflow.repo_analysis import RepoAnalysis, analyze_repository
from agentflow.researcher import _find_relevant_files, _parse_researcher_response


class ResearchBriefTests(unittest.TestCase):
    def test_round_trip_dict(self):
        brief = ResearchBrief(
            current_state="Uses Flask with SQLite.",
            recommended_approach="Migrate to FastAPI with async SQLAlchemy.",
            constraints_and_risks="Breaking change for all API consumers.",
            rationale="FastAPI is faster and has native async support.",
            relevant_files=["app/main.py", "app/models.py"],
            sources=["https://fastapi.tiangolo.com"],
        )
        d = brief.to_dict()
        restored = ResearchBrief.from_dict(d)

        self.assertEqual(restored.current_state, brief.current_state)
        self.assertEqual(restored.recommended_approach, brief.recommended_approach)
        self.assertEqual(restored.constraints_and_risks, brief.constraints_and_risks)
        self.assertEqual(restored.rationale, brief.rationale)
        self.assertEqual(restored.relevant_files, brief.relevant_files)
        self.assertEqual(restored.sources, brief.sources)

    def test_to_prompt_context_includes_all_sections(self):
        brief = ResearchBrief(
            current_state="Current state.",
            recommended_approach="Recommended approach.",
            constraints_and_risks="Risks.",
            rationale="Rationale.",
            relevant_files=["a.py"],
        )
        ctx = brief.to_prompt_context()
        self.assertIn("CURRENT STATE:", ctx)
        self.assertIn("RECOMMENDED APPROACH:", ctx)
        self.assertIn("CONSTRAINTS & RISKS:", ctx)
        self.assertIn("RATIONALE:", ctx)
        self.assertIn("a.py", ctx)


class FindRelevantFilesTests(unittest.TestCase):
    def test_finds_files_mentioned_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "app/models.py": "class User: pass\n",
                "app/views.py": "from .models import User\n",
                "app/utils.py": "def helper(): pass\n",
            }
            for rel, content in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            analysis = analyze_repository(root.as_posix(), tracked_files=list(files))
            result = _find_relevant_files(
                "refactor app/models.py to add email field",
                analysis, root.as_posix(), max_files=5,
            )

            self.assertIn("app/models.py", result)

    def test_import_neighbors_ranked_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "pkg/__init__.py": "",
                "pkg/core.py": "VALUE = 1\n",
                "pkg/api.py": "from .core import VALUE\n",
                "pkg/unrelated.py": "X = 2\n",
            }
            for rel, content in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            analysis = analyze_repository(root.as_posix(), tracked_files=list(files))
            result = _find_relevant_files(
                "change pkg/core.py VALUE handling",
                analysis, root.as_posix(), max_files=5,
            )

            # core.py should be first, api.py (its importer) should rank higher than unrelated.py
            self.assertEqual(result[0], "pkg/core.py")
            if "pkg/api.py" in result and "pkg/unrelated.py" in result:
                self.assertLess(result.index("pkg/api.py"), result.index("pkg/unrelated.py"))


class ParseResearcherResponseTests(unittest.TestCase):
    def test_parses_valid_json(self):
        import json
        data = {
            "current_state": "State.",
            "recommended_approach": "Approach.",
            "constraints_and_risks": "Risks.",
            "rationale": "Rationale.",
        }
        brief = _parse_researcher_response(json.dumps(data))
        self.assertEqual(brief.current_state, "State.")
        self.assertEqual(brief.recommended_approach, "Approach.")

    def test_parses_json_in_markdown_fences(self):
        import json
        data = {
            "current_state": "State.",
            "recommended_approach": "Approach.",
            "constraints_and_risks": "Risks.",
            "rationale": "Rationale.",
        }
        text = f"```json\n{json.dumps(data)}\n```"
        brief = _parse_researcher_response(text)
        self.assertEqual(brief.current_state, "State.")

    def test_falls_back_to_raw_text(self):
        brief = _parse_researcher_response("Just some plain text research notes.")
        self.assertIn("plain text", brief.recommended_approach)
        self.assertIn("not structured", brief.rationale)


if __name__ == "__main__":
    unittest.main()
