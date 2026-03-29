from __future__ import annotations

import json
import unittest

from agentflow.models import REVIEW_CHECKS, ReviewDecision
from agentflow.prompts.guardrails import parse_review_output


class ReviewGuardrailsTests(unittest.TestCase):
    def test_parse_review_output_accepts_structured_keep_review(self):
        payload = {
            "summary": "Change is solid.",
            "decision": "keep",
            "checks": [
                {
                    "id": check_id,
                    "status": "not_applicable" if check_id == "metric_improvement" else "pass",
                    "details": f"{question} looks good.",
                }
                for check_id, question in REVIEW_CHECKS
            ],
        }

        result, errors = parse_review_output(json.dumps(payload))

        self.assertEqual(errors, [])
        self.assertTrue(result.approved)
        self.assertEqual(result.decision, ReviewDecision.KEEP)
        self.assertEqual(len(result.checks), len(REVIEW_CHECKS))

    def test_parse_review_output_accepts_structured_retry_review(self):
        payload = {
            "summary": "Needs fixes before merge.",
            "decision": "retry",
            "checks": [
                {
                    "id": check_id,
                    "status": "fail" if check_id in {"run_build", "logic_edge_cases"} else "pass",
                    "details": f"Review note for {check_id}.",
                }
                for check_id, _ in REVIEW_CHECKS
            ],
        }

        result, errors = parse_review_output(json.dumps(payload))

        self.assertFalse(result.approved)
        self.assertEqual(result.decision, ReviewDecision.RETRY)
        self.assertEqual(len(result.checks), len(REVIEW_CHECKS))
        self.assertIn("Decision: retry", result.feedback)
        self.assertIn("Does it run/build correctly?", result.feedback)
        self.assertFalse(any("Missing review check" in error for error in errors))

    def test_parse_review_output_falls_back_to_legacy_feedback(self):
        result, errors = parse_review_output("FEEDBACK:\n- Missing import causes crash.")

        self.assertFalse(result.approved)
        self.assertEqual(result.decision, ReviewDecision.RETRY)
        self.assertIn("Missing import causes crash.", result.feedback)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
