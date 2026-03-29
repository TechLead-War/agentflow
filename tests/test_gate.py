from __future__ import annotations

import unittest

from agentflow.gate import _heuristic_classify
from agentflow.repo_analysis import RepoAnalysis


def _empty_analysis() -> RepoAnalysis:
    return RepoAnalysis(tracked_files=[], python_files=[], imports={}, imported_by={})


class GateHeuristicTests(unittest.TestCase):
    def test_simple_bugfix_classified_as_simple(self):
        result = _heuristic_classify("fix the bug in login form", _empty_analysis())
        self.assertFalse(result.is_complex)
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_add_test_classified_as_simple(self):
        result = _heuristic_classify("add a test for the user model", _empty_analysis())
        self.assertFalse(result.is_complex)

    def test_typo_classified_as_simple(self):
        result = _heuristic_classify("fix typo in README", _empty_analysis())
        self.assertFalse(result.is_complex)

    def test_architecture_classified_as_complex(self):
        result = _heuristic_classify(
            "redesign the authentication architecture to use OAuth2",
            _empty_analysis(),
        )
        self.assertTrue(result.is_complex)
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_performance_classified_as_complex(self):
        result = _heuristic_classify(
            "optimize the database query performance for the dashboard",
            _empty_analysis(),
        )
        self.assertTrue(result.is_complex)

    def test_migration_classified_as_complex(self):
        result = _heuristic_classify(
            "migrate the API framework from Flask to FastAPI",
            _empty_analysis(),
        )
        self.assertTrue(result.is_complex)

    def test_security_classified_as_complex(self):
        result = _heuristic_classify(
            "harden security on all API endpoints",
            _empty_analysis(),
        )
        self.assertTrue(result.is_complex)

    def test_no_signals_returns_low_confidence(self):
        result = _heuristic_classify("update the color of the button", _empty_analysis())
        self.assertLessEqual(result.confidence, 0.5)

    def test_high_fan_in_file_boosts_complexity(self):
        analysis = RepoAnalysis(
            tracked_files=["core/models.py", "api/views.py", "cli/main.py"],
            python_files=["core/models.py", "api/views.py", "cli/main.py"],
            imports={
                "api/views.py": {"core/models.py"},
                "cli/main.py": {"core/models.py"},
            },
            imported_by={
                "core/models.py": {"api/views.py", "cli/main.py", "tests/test.py"},
            },
        )
        result = _heuristic_classify("refactor the entire core/models.py module", analysis)
        self.assertTrue(result.is_complex)
        self.assertTrue(any("high_fan_in" in s for s in result.signals))


if __name__ == "__main__":
    unittest.main()
