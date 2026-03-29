from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentflow.config import Config
from agentflow.models import ValidationResult
from agentflow.validator import (
    _parse_validator_response,
    _detect_and_run_tests,
    create_fix_task,
)


class ParseValidatorResponseTests(unittest.TestCase):
    def test_parses_passing_json(self):
        data = {
            "passed": True,
            "issues": [],
            "summary": "All good.",
        }
        result = _parse_validator_response(json.dumps(data))
        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.summary, "All good.")

    def test_parses_failing_json(self):
        data = {
            "passed": False,
            "issues": ["Missing import in app.py", "Regression in tests"],
            "summary": "Two issues found.",
        }
        result = _parse_validator_response(json.dumps(data))
        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 2)

    def test_parses_json_in_markdown_fences(self):
        data = {"passed": True, "issues": [], "summary": "OK."}
        text = f"```json\n{json.dumps(data)}\n```"
        result = _parse_validator_response(text)
        self.assertTrue(result.passed)

    def test_falls_back_to_failure_on_invalid_output(self):
        result = _parse_validator_response("Some random text that is not JSON.")
        self.assertFalse(result.passed)
        self.assertTrue(len(result.issues) > 0)


class ValidationResultTests(unittest.TestCase):
    def test_round_trip_dict(self):
        result = ValidationResult(
            passed=False,
            issues=["Issue 1", "Issue 2"],
            summary="Needs fixes.",
            build_output="1 test failed",
        )
        d = result.to_dict()
        restored = ValidationResult.from_dict(d)

        self.assertEqual(restored.passed, result.passed)
        self.assertEqual(restored.issues, result.issues)
        self.assertEqual(restored.summary, result.summary)
        self.assertEqual(restored.build_output, result.build_output)


class CreateFixTaskTests(unittest.TestCase):
    def test_creates_fix_task_with_issues(self):
        result = ValidationResult(
            passed=False,
            issues=["Missing import in api.py", "Broken test in test_api.py"],
            summary="Two issues found.",
        )
        config = Config()
        task = create_fix_task(result, "add auth to API", config)

        self.assertEqual(task.id, "validation-fix")
        self.assertIn("Missing import in api.py", task.spec)
        self.assertIn("Broken test in test_api.py", task.spec)
        self.assertIn("add auth to API", task.spec)
        self.assertEqual(task.complexity.value, "bugfix")


class DetectTestRunnerTests(unittest.TestCase):
    def test_no_runner_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _detect_and_run_tests(tmp)
            self.assertEqual(result, "")

    def test_detects_pytest_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            (Path(tmp) / "tests" / "test_foo.py").write_text("def test_foo(): pass\n")
            # We don't actually run pytest here (might not be installed),
            # but verify detection doesn't crash
            result = _detect_and_run_tests(tmp)
            # Result is either test output or empty (if pytest not available)
            self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
