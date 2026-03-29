"""Post-merge validator: holistic check of all merged changes.

After all task branches are merged, this module:
1. Gets the full combined diff against the original base state
2. Optionally runs build/test commands if the repo has them
3. Calls an LLM to verify delivery, breakage, and regressions
4. Returns a ValidationResult

If validation fails, `create_fix_task()` produces a single fix task
that can be run through the existing agent→review→merge loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .models import (
    AgentType,
    RunState,
    Task,
    TaskComplexity,
    ValidationResult,
)
from .prompts import PromptBuilder
from . import git_ops

logger = logging.getLogger(__name__)

VALIDATOR_TIMEOUT_SEC = int(os.environ.get("AGENTFLOW_VALIDATOR_TIMEOUT_SEC", "180"))
_TEST_RUNNER_TIMEOUT_SEC = 60
_MAX_DIFF_CHARS = 30000
_MAX_BUILD_OUTPUT_CHARS = 5000


async def validate(
    prompt: str,
    state: RunState,
    config: Config,
) -> ValidationResult:
    """Run holistic validation on all merged changes."""
    repo_path = state.repo_path

    # Get combined diff of everything that changed since run start
    diff = _get_combined_diff(state)
    if not diff.strip():
        return ValidationResult(
            passed=True,
            summary="No changes to validate (empty diff).",
        )

    # Optionally run build/tests
    build_output = _detect_and_run_tests(repo_path)

    # Call LLM validator
    user_message = _build_user_message(prompt, diff, build_output)
    raw_response = await _call_validator(user_message, config, repo_path)

    result = _parse_validator_response(raw_response)
    result.build_output = build_output

    return result


def create_fix_task(
    validation_result: ValidationResult,
    original_prompt: str,
    config: Config,
) -> Task:
    """Create a single fix task from a validation failure report."""
    issues_text = "\n".join(f"- {issue}" for issue in validation_result.issues)

    spec = (
        f"The following changes were made to fulfill this request:\n"
        f'"{original_prompt}"\n\n'
        f"Post-merge validation found these issues:\n"
        f"{issues_text}\n\n"
        f"Validation summary: {validation_result.summary}\n\n"
        f"Fix ALL of the issues listed above. Do not introduce unrelated changes. "
        f"Focus only on correcting the specific problems identified by the validator."
    )

    reviewer_type = AgentType.CODEX
    if config.reviewer != "human":
        try:
            reviewer_type = AgentType(config.reviewer)
        except ValueError:
            pass

    return Task(
        id="validation-fix",
        title="Fix validation issues",
        spec=spec,
        rationale=(
            "Post-merge validation detected issues with the combined changes. "
            "This fix task addresses the specific problems found by the validator."
        ),
        files=[],
        depends_on=[],
        complexity=TaskComplexity.BUGFIX,
        agent=AgentType(config.agent),
        reviewer=reviewer_type,
        max_rounds=config.max_rounds,
    )


def _get_combined_diff(state: RunState) -> str:
    """Get the full diff of all changes since the run started."""
    repo_path = state.repo_path

    if state.base_sha:
        return git_ops.run_git(
            ["diff", state.base_sha, "HEAD"],
            cwd=repo_path, check=False,
        )

    # Fallback: diff against base branch (less accurate if base moved)
    return git_ops.run_git(
        ["diff", state.base_branch, "HEAD"],
        cwd=repo_path, check=False,
    )


def _detect_and_run_tests(repo_path: str) -> str:
    """Auto-detect and run test commands. Returns output or empty string."""
    root = Path(repo_path)

    # Detect test runner
    command: list[str] | None = None

    # Python: pytest
    has_pytest = (
        (root / "pytest.ini").exists()
        or (root / "tests").is_dir()
        or (root / "pyproject.toml").exists()
    )
    if has_pytest and shutil.which("python"):
        command = ["python", "-m", "pytest", "--tb=short", "-q"]

    # Node: npm test
    if command is None and (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                command = ["npm", "test"]
        except (json.JSONDecodeError, OSError):
            pass

    # Make: make test
    if command is None and (root / "Makefile").exists():
        try:
            makefile = (root / "Makefile").read_text(errors="replace")
            if "\ntest:" in makefile or makefile.startswith("test:"):
                command = ["make", "test"]
        except OSError:
            pass

    # Rust: cargo test
    if command is None and (root / "Cargo.toml").exists():
        command = ["cargo", "test"]

    # Go: go test
    if command is None and (root / "go.mod").exists():
        command = ["go", "test", "./..."]

    if command is None:
        return ""

    logger.info("Running tests: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=_TEST_RUNNER_TIMEOUT_SEC,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        if len(output) > _MAX_BUILD_OUTPUT_CHARS:
            output = output[:_MAX_BUILD_OUTPUT_CHARS] + "\n... (truncated)"
        return output
    except subprocess.TimeoutExpired:
        return "Test runner timed out."
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.warning("Test runner error: %s", e)
        return f"Test runner error: {e}"


def _build_user_message(prompt: str, diff: str, build_output: str) -> str:
    """Assemble the user message for the validator LLM call."""
    truncated_diff = diff[:_MAX_DIFF_CHARS]
    if len(diff) > _MAX_DIFF_CHARS:
        truncated_diff += f"\n... (diff truncated, {len(diff)} chars total)"

    sections = [
        f"ORIGINAL USER REQUEST:\n{prompt}",
        f"FULL COMBINED DIFF OF ALL CHANGES:\n{truncated_diff}",
    ]

    if build_output:
        sections.append(f"BUILD/TEST OUTPUT:\n{build_output}")
    else:
        sections.append("BUILD/TEST OUTPUT:\nNo build/test runner detected.")

    return "\n\n".join(sections)


async def _call_validator(user_message: str, config: Config, repo_path: str) -> str:
    """Call a validator LLM backend and return its raw response."""

    claude_bin = shutil.which("claude")
    codex_bin = shutil.which("codex")
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if not any((claude_bin, codex_bin, has_anthropic_key, has_openai_key)):
        raise RuntimeError(
            "No validator backend available. Install claude/codex CLI or set API keys."
        )

    validator_prompt = PromptBuilder.build_validator_prompt()
    model = config.planner_model
    errors: list[str] = []

    if claude_bin:
        try:
            return await _validate_with_claude_cli(
                claude_bin, validator_prompt, user_message, model, repo_path,
            )
        except Exception as e:
            errors.append(f"claude CLI: {e}")

    if codex_bin:
        try:
            return await _validate_with_codex_cli(
                codex_bin, validator_prompt, user_message, repo_path,
            )
        except Exception as e:
            errors.append(f"codex CLI: {e}")

    if has_anthropic_key:
        try:
            return await _validate_with_claude_api(
                validator_prompt, user_message, model,
            )
        except Exception as e:
            errors.append(f"anthropic API: {e}")

    if has_openai_key:
        try:
            return await _validate_with_openai_api(
                validator_prompt, user_message, config.codex_model,
            )
        except Exception as e:
            errors.append(f"openai API: {e}")

    raise RuntimeError("All validator backends failed: " + " | ".join(errors))


async def _validate_with_claude_cli(
    claude_bin: str,
    system_prompt: str,
    user_message: str,
    model: str,
    repo_path: str,
) -> str:
    full_prompt = f"{system_prompt}\n\n{user_message}"
    args = [
        claude_bin,
        "-p", full_prompt,
        "--output-format", "text",
        "--max-turns", "1",
        "--dangerously-skip-permissions",
    ]
    if model:
        args.extend(["--model", model])

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=VALIDATOR_TIMEOUT_SEC,
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude validator failed (exit {proc.returncode}): {err[:500]}")

    return text


async def _validate_with_codex_cli(
    codex_bin: str,
    system_prompt: str,
    user_message: str,
    repo_path: str,
) -> str:
    full_prompt = f"{system_prompt}\n\n{user_message}"
    proc = await asyncio.create_subprocess_exec(
        codex_bin, "exec", "--full-auto", "-",
        cwd=repo_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=full_prompt.encode("utf-8")),
        timeout=VALIDATOR_TIMEOUT_SEC,
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex validator failed (exit {proc.returncode}): {err[:500]}")

    return text


async def _validate_with_claude_api(
    system_prompt: str, user_message: str, model: str,
) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.content else ""


async def _validate_with_openai_api(
    system_prompt: str, user_message: str, model: str,
) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=2000,
    )
    return response.choices[0].message.content or ""


def _parse_validator_response(text: str) -> ValidationResult:
    """Extract and parse JSON validation result from LLM response."""
    stripped = text.strip()

    # Strip markdown code fences
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # Try direct JSON parse
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return ValidationResult.from_dict(data)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(stripped[start:end])
            if isinstance(data, dict):
                return ValidationResult.from_dict(data)
        except json.JSONDecodeError:
            pass

    # Fallback: treat as failed validation
    logger.warning("Validator output was not valid JSON. Treating as failure.")
    return ValidationResult(
        passed=False,
        issues=["Validator output was not in expected format."],
        summary=stripped[:500],
        raw_output=text,
    )
