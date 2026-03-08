from __future__ import annotations
import asyncio
import json
import os
import shutil
from pathlib import Path
from .models import Task, TaskComplexity
from .config import Config
from .assignment import assign
from . import git_ops

PLANNER_TIMEOUT_SEC = int(os.environ.get("AGENTFLOW_PLANNER_TIMEOUT_SEC", "180"))


PLANNER_PROMPT = """\
You are a technical task planner. Given a codebase file structure and a user request,
break the work into independent, atomic coding tasks.

Rules:
- Each task must be implementable independently by a single coding agent
- If two tasks modify the same file, make one depend on the other OR restructure
  so they touch different files
- Be specific in the spec — the coding agent needs exact instructions
- List ALL files that will be created or modified
- Minimize dependencies between tasks — prefer independent tasks
- Each task must be self-contained
- For EACH task, include a "rationale" explaining WHY this change is needed,
  what evidence or reasoning supports it, and what problem it solves. The coding
  agent will use this to make smarter decisions (e.g., if the task says "set limit
  to 10", the rationale should explain why 10 is the right value, what breaks
  without it, or what user behavior/data supports it).

For each task, classify complexity as one of:
  architecture — system design, new modules, major structural changes
  algorithm    — math-heavy, data structures, complex logic
  feature      — adding functionality with clear requirements
  bugfix       — fixing a specific broken behavior
  refactor     — restructuring without changing behavior
  test         — writing tests

Output ONLY valid JSON in this exact format:
{
  "tasks": [
    {
      "id": "kebab-case-id",
      "title": "Short descriptive title",
      "spec": "Detailed implementation instructions. Be specific about what to change, where, and how.",
      "rationale": "WHY this change is needed. What evidence, reasoning, or context supports this approach. What problem does it solve and what happens without it.",
      "files": ["path/to/file1.ext", "path/to/new_file.ext"],
      "depends_on": [],
      "complexity": "feature"
    }
  ]
}

Do NOT include any text before or after the JSON.
"""


async def plan(prompt: str, config: Config, repo_path: str) -> list[Task]:
    """Break a user prompt into structured tasks using an AI planner."""

    # Gather codebase context
    file_tree = git_ops.get_file_tree(cwd=repo_path)
    context_content = _read_context_files(config.context_files, repo_path)

    user_message = f"CODEBASE FILES:\n{file_tree}\n\n"
    if context_content:
        user_message += f"KEY FILES:\n{context_content}\n\n"
    user_message += f"USER REQUEST:\n{prompt}"

    # Detect available providers for assignment (CLI or API)
    has_anthropic = bool(shutil.which("claude")) or bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(shutil.which("codex")) or bool(os.environ.get("OPENAI_API_KEY"))

    raw_tasks = await _call_planner(user_message, config, repo_path)

    # Assign agents and reviewers based on complexity
    tasks: list[Task] = []
    for raw in raw_tasks:
        complexity = TaskComplexity(raw.get("complexity", "feature"))
        agent, reviewer = assign(complexity, has_anthropic, has_openai)

        task = Task(
            id=raw["id"],
            title=raw["title"],
            spec=raw["spec"],
            rationale=raw.get("rationale", ""),
            files=raw.get("files", []),
            depends_on=raw.get("depends_on", []),
            complexity=complexity,
            agent=agent,
            reviewer=reviewer,
            max_rounds=config.max_rounds,
        )
        tasks.append(task)

    return tasks


async def _call_planner(user_message: str, config: Config, repo_path: str) -> list[dict]:
    """Call a planner backend and parse its response into task dicts."""

    claude_bin = shutil.which("claude")
    codex_bin = shutil.which("codex")
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if not any((claude_bin, codex_bin, has_anthropic_key, has_openai_key)):
        raise RuntimeError(
            "No planner backend available. Install claude/codex CLI or set API keys."
        )

    errors: list[str] = []

    if claude_bin:
        try:
            return await _plan_with_claude_cli(claude_bin, user_message, config.planner_model, repo_path)
        except Exception as e:
            errors.append(f"claude CLI: {e}")

    if codex_bin:
        try:
            return await _plan_with_codex_cli(codex_bin, user_message, repo_path)
        except Exception as e:
            errors.append(f"codex CLI: {e}")

    if has_anthropic_key:
        try:
            return await _plan_with_claude(user_message, config.planner_model)
        except Exception as e:
            errors.append(f"anthropic API: {e}")

    if has_openai_key:
        try:
            return await _plan_with_openai(user_message, config.codex_model)
        except Exception as e:
            errors.append(f"openai API: {e}")

    raise RuntimeError("All planner backends failed: " + " | ".join(errors))


async def _plan_with_claude_cli(
    claude_bin: str,
    user_message: str,
    model: str,
    repo_path: str,
) -> list[dict]:
    full_prompt = f"{PLANNER_PROMPT}\n\n{user_message}"

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

    stdout, stderr = await _communicate_with_timeout(
        proc,
        timeout_sec=PLANNER_TIMEOUT_SEC,
        label="Claude planner",
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude planner failed (exit {proc.returncode}): {err[:500]}")

    return _parse_planner_response(text)


async def _plan_with_codex_cli(codex_bin: str, user_message: str, repo_path: str) -> list[dict]:
    full_prompt = f"{PLANNER_PROMPT}\n\n{user_message}"

    proc = await asyncio.create_subprocess_exec(
        codex_bin,
        "exec",
        "--full-auto",
        "-",
        cwd=repo_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await _communicate_with_timeout(
        proc,
        input_data=full_prompt.encode("utf-8"),
        timeout_sec=PLANNER_TIMEOUT_SEC,
        label="Codex planner",
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex planner failed (exit {proc.returncode}): {err[:500]}")

    return _parse_planner_response(text)


async def _plan_with_claude(user_message: str, model: str) -> list[dict]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=PLANNER_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text if response.content else ""
    return _parse_planner_response(text)


async def _plan_with_openai(user_message: str, model: str) -> list[dict]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "user", "content": user_message},
        ],
        max_tokens=4096,
    )

    text = response.choices[0].message.content or ""
    return _parse_planner_response(text)


def _parse_planner_response(text: str) -> list[dict]:
    """Extract JSON task list from planner response."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                raise ValueError(f"Planner returned invalid JSON:\n{text[:500]}")
        else:
            raise ValueError(f"Planner returned no JSON:\n{text[:500]}")

    tasks = data.get("tasks", [])
    if not tasks:
        raise ValueError("Planner returned no tasks.")

    return tasks


def _read_context_files(context_files: list[str], repo_path: str) -> str:
    """Read context files (README, CLAUDE.md, etc.) for the planner."""
    parts = []

    # Auto-detect common context files if none specified
    if not context_files:
        candidates = ["README.md", "CLAUDE.md", ".agentflow.yaml", "ARCHITECTURE.md"]
        context_files = [f for f in candidates if (Path(repo_path) / f).exists()]

    for filename in context_files:
        filepath = Path(repo_path) / filename
        if filepath.exists():
            content = filepath.read_text(errors="replace")[:2000]
            parts.append(f"--- {filename} ---\n{content}")

    return "\n\n".join(parts)


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    *,
    timeout_sec: int,
    label: str,
    input_data: bytes | None = None,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(proc.communicate(input=input_data), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        raise RuntimeError(f"{label} timed out after {timeout_sec}s")
