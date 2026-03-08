"""Prompt guardrails: injection detection, output validation, and sanitization.

These guardrails protect the pipeline at two boundaries:
1. INPUT: User prompts are scanned for injection patterns before being sent to LLMs
2. OUTPUT: LLM responses are validated against expected formats before being used
"""

from __future__ import annotations

import json
import logging
import re

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
    required_fields = {"id", "title", "spec"}
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
        if "files" in task and not isinstance(task["files"], list):
            errors.append(f"Task '{task_label}': 'files' must be a list")

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


def validate_review_output(text: str) -> tuple[bool, str]:
    """Validate reviewer output format.

    Returns:
        (is_valid_format, normalized_content)
        is_valid_format is True if the output matches LGTM or FEEDBACK: format.
    """
    stripped = text.strip()
    if not stripped:
        return False, "Empty review output"

    # Check for LGTM
    lines = stripped.split("\n")
    for line in lines:
        if line.strip().upper() == "LGTM":
            return True, "LGTM"

    # Check for FEEDBACK:
    if "FEEDBACK:" in stripped.upper():
        return True, stripped

    # Ambiguous format — still usable but not the expected format
    return False, stripped
