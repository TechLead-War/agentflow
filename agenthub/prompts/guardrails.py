"""Prompt guardrails: injection detection, output validation, and sanitization.

These guardrails protect the pipeline at two boundaries:
1. INPUT: User prompts are scanned for injection patterns before being sent to LLMs
2. OUTPUT: LLM responses are validated against expected formats before being used
"""

from __future__ import annotations

import json
import logging
import re

from ..models import (
    REVIEW_CHECKS,
    ReviewCheck,
    ReviewCheckStatus,
    ReviewDecision,
    ReviewResult,
)

logger = logging.getLogger(__name__)


# ─── INPUT GUARDRAILS ───────────────────────────────────────────────────────

# Common prompt injection patterns. These are checked against user input
# and flagged as warnings. We warn but don't block — the guardrail text
# in system prompts is the primary defense.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"ignore\s+(all\s+)?above\s+instructions",
    r"disregard\s+(all\s+)?previous",
    r"you\s+are\s+now\s+a",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*you\s+are",
    r"forget\s+(everything|all)",
    r"override\s+(system|instructions)",
    r"pretend\s+you\s+are",
    r"act\s+as\s+if\s+you\s+have\s+no\s+restrictions",
    r"jailbreak",
    r"DAN\s+mode",
    r"do\s+anything\s+now",
    r"developer\s+mode",
]

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def sanitize_input(user_prompt: str) -> tuple[str, list[str]]:
    """Scan user input for prompt injection patterns.

    Returns:
        (original_prompt, list_of_warnings)
        The prompt is returned unchanged — we warn but don't modify.
        Warnings are logged and can be surfaced to the user.
    """
    warnings = []

    for pattern in _compiled_patterns:
        match = pattern.search(user_prompt)
        if match:
            warnings.append(
                f"Potential prompt injection detected: '{match.group()}'"
            )

    if warnings:
        logger.warning(
            "Prompt injection warning(s) in user input: %s",
            "; ".join(warnings),
        )

    return user_prompt, warnings


# ─── OUTPUT GUARDRAILS ──────────────────────────────────────────────────────

def validate_planner_output(text: str) -> tuple[list[dict], list[str]]:
    """Validate and parse planner JSON output.

    Performs structural validation beyond basic JSON parsing:
    - Required fields present on each task
    - Field types are correct
    - Complexity values are valid

    Returns:
        (tasks_list, validation_errors)
        If validation_errors is non-empty, tasks may be partially valid.
    """
    errors: list[str] = []
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # remove opening fence line
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # Parse JSON
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                return [], [f"Invalid JSON in planner output: {text[:200]}"]
        else:
            return [], [f"No JSON found in planner output: {text[:200]}"]

    tasks = data.get("tasks", [])
    if not tasks:
        return [], ["Planner returned no tasks"]

    # Validate each task
    required_fields = {"id", "title", "spec", "files", "depends_on"}
    valid_complexities = {
        "architecture", "algorithm", "feature", "bugfix", "refactor", "test",
    }

    for i, task in enumerate(tasks):
        task_label = task.get("id", f"index-{i}")

        # Required fields
        missing = required_fields - set(task.keys())
        if missing:
            errors.append(f"Task '{task_label}' missing required fields: {missing}")

        # Type validation
        if "files" in task:
            if not isinstance(task["files"], list):
                errors.append(f"Task '{task_label}': 'files' must be a list")
            elif not task["files"]:
                errors.append(f"Task '{task_label}': 'files' should list at least one path")

        if "depends_on" in task and not isinstance(task["depends_on"], list):
            errors.append(f"Task '{task_label}': 'depends_on' must be a list")

        # Complexity validation
        complexity = task.get("complexity", "feature")
        if complexity not in valid_complexities:
            errors.append(
                f"Task '{task_label}': invalid complexity '{complexity}', "
                f"expected one of {valid_complexities}"
            )

    return tasks, errors


def parse_review_output(text: str) -> tuple[ReviewResult, list[str]]:
    """Parse structured reviewer output, with legacy fallback."""
    stripped = text.strip()
    if not stripped:
        return (
            ReviewResult(
                approved=False,
                feedback="Reviewer returned empty output.",
                decision=ReviewDecision.RETRY,
                summary="Reviewer returned empty output.",
                raw_output=text,
            ),
            ["Empty review output"],
        )

    normalized = _strip_markdown_fences(stripped)
    data, json_errors = _extract_json_object(normalized)
    if data:
        result, errors = _parse_structured_review(data, raw_output=text)
        if result.checks:
            return result, errors
        json_errors.extend(errors)

    legacy_result = _parse_legacy_review_output(stripped)
    if legacy_result:
        return legacy_result, json_errors or ["Used legacy review output parser."]

    return (
        ReviewResult(
            approved=False,
            feedback=stripped,
            decision=ReviewDecision.RETRY,
            summary="Reviewer output was not in the expected structured format.",
            raw_output=text,
        ),
        json_errors or ["Reviewer output was not in the expected structured format."],
    )


def validate_review_output(text: str) -> tuple[bool, str]:
    """Validate reviewer output format."""
    result, errors = parse_review_output(text)
    if result.checks or result.decision != ReviewDecision.RETRY or not errors:
        return True, result.to_log_text()
    return False, result.feedback or result.to_log_text()


def _strip_markdown_fences(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _extract_json_object(text: str) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data, []
        return None, ["Review output JSON must be an object."]
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
                if isinstance(data, dict):
                    return data, []
                return None, ["Review output JSON must be an object."]
            except json.JSONDecodeError:
                errors.append(f"Invalid JSON in review output: {text[:200]}")
        else:
            errors.append(f"No JSON found in review output: {text[:200]}")
    return None, errors


def _parse_structured_review(data: dict, *, raw_output: str) -> tuple[ReviewResult, list[str]]:
    errors: list[str] = []
    summary = str(data.get("summary", "")).strip()
    decision = _normalize_review_decision(data.get("decision"))
    if decision is None:
        errors.append("Review output missing valid 'decision' (keep|retry|reject).")
        decision = ReviewDecision.RETRY

    checks_data = data.get("checks")
    checks = _normalize_review_checks(checks_data, errors)

    fail_count = sum(1 for check in checks if check.status == ReviewCheckStatus.FAIL)
    if fail_count == 0 and decision != ReviewDecision.KEEP:
        errors.append("Structured review has no failed checks but decision is not 'keep'.")
    if fail_count > 0 and decision == ReviewDecision.KEEP:
        errors.append("Structured review cannot use decision 'keep' when checks failed.")

    approved = decision == ReviewDecision.KEEP and fail_count == 0 and bool(checks)
    feedback = _format_review_feedback(checks, summary, decision)

    return ReviewResult(
        approved=approved,
        feedback=feedback,
        decision=decision,
        summary=summary,
        checks=checks,
        raw_output=raw_output,
    ), errors


def _normalize_review_checks(checks_data, errors: list[str]) -> list[ReviewCheck]:
    if not isinstance(checks_data, list):
        errors.append("Review output missing 'checks' list.")
        return []

    checks_by_id: dict[str, ReviewCheck] = {}
    canonical_ids = {check_id for check_id, _ in REVIEW_CHECKS}

    for index, item in enumerate(checks_data):
        if not isinstance(item, dict):
            errors.append(f"Review check at index {index} must be an object.")
            continue

        raw_id = item.get("id")
        check_id = str(raw_id).strip() if raw_id is not None else ""
        if not check_id and index < len(REVIEW_CHECKS):
            check_id = REVIEW_CHECKS[index][0]
        if check_id not in canonical_ids:
            errors.append(f"Unknown review check id '{check_id or index}'.")
            continue

        status = _normalize_review_status(item.get("status"))
        if status is None:
            errors.append(f"Review check '{check_id}' has invalid status '{item.get('status')}'.")
            continue

        details = str(item.get("details", "")).strip()
        question = dict(REVIEW_CHECKS)[check_id]
        checks_by_id[check_id] = ReviewCheck(
            id=check_id,
            question=question,
            status=status,
            details=details,
        )

    ordered_checks: list[ReviewCheck] = []
    for check_id, question in REVIEW_CHECKS:
        check = checks_by_id.get(check_id)
        if check is None:
            errors.append(f"Missing review check '{check_id}'.")
            continue
        if not check.details:
            check.details = "No reviewer details provided."
        ordered_checks.append(check)

    if len(checks_by_id) != len(REVIEW_CHECKS):
        extra = set(checks_by_id) - {check_id for check_id, _ in REVIEW_CHECKS}
        if extra:
            errors.append(f"Unexpected review checks: {sorted(extra)}")

    return ordered_checks


def _normalize_review_status(value) -> ReviewCheckStatus | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    aliases = {
        "pass": ReviewCheckStatus.PASS,
        "passed": ReviewCheckStatus.PASS,
        "ok": ReviewCheckStatus.PASS,
        "yes": ReviewCheckStatus.PASS,
        "fail": ReviewCheckStatus.FAIL,
        "failed": ReviewCheckStatus.FAIL,
        "no": ReviewCheckStatus.FAIL,
        "not_applicable": ReviewCheckStatus.NOT_APPLICABLE,
        "not-applicable": ReviewCheckStatus.NOT_APPLICABLE,
        "na": ReviewCheckStatus.NOT_APPLICABLE,
        "n/a": ReviewCheckStatus.NOT_APPLICABLE,
    }
    return aliases.get(normalized)


def _normalize_review_decision(value) -> ReviewDecision | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    aliases = {
        "keep": ReviewDecision.KEEP,
        "approve": ReviewDecision.KEEP,
        "approved": ReviewDecision.KEEP,
        "lgtm": ReviewDecision.KEEP,
        "retry": ReviewDecision.RETRY,
        "revise": ReviewDecision.RETRY,
        "fix": ReviewDecision.RETRY,
        "reject": ReviewDecision.REJECT,
    }
    return aliases.get(normalized)


def _parse_legacy_review_output(text: str) -> ReviewResult | None:
    stripped = text.strip()
    lines = stripped.split("\n")
    for line in lines:
        if line.strip().upper() == "LGTM":
            return ReviewResult(
                approved=True,
                feedback="",
                decision=ReviewDecision.KEEP,
                summary="Legacy reviewer approval.",
                raw_output=text,
            )

    if "FEEDBACK:" in stripped.upper():
        idx = stripped.upper().index("FEEDBACK:")
        feedback = stripped[idx + len("FEEDBACK:"):].strip()
        return ReviewResult(
            approved=False,
            feedback=feedback,
            decision=ReviewDecision.RETRY,
            summary="Legacy reviewer requested changes.",
            raw_output=text,
        )

    return None


def _format_review_feedback(
    checks: list[ReviewCheck],
    summary: str,
    decision: ReviewDecision,
) -> str:
    lines = [f"Decision: {decision.value}"]
    if summary:
        lines.append(f"Summary: {summary}")
    for index, check in enumerate(checks, 1):
        detail = check.details or "No reviewer details provided."
        lines.append(
            f"{index}. {check.question} [{check.status.value}] {detail}"
        )
    return "\n".join(lines)
