from __future__ import annotations
import json
import os
from pathlib import Path
from .models import Task, TaskComplexity
from .config import Config
from .assignment import assign, AgentType
from . import git_ops


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

    # Detect available API keys for assignment
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))

    # Call planner (always uses Claude for planning — it's better at decomposition)
    raw_tasks = await _call_planner(user_message, config)

    # Assign agents and reviewers based on complexity
    tasks: list[Task] = []
    for raw in raw_tasks:
        complexity = TaskComplexity(raw.get("complexity", "feature"))
        agent, reviewer = assign(complexity, has_anthropic, has_openai)

        task = Task(
            id=raw["id"],
            title=raw["title"],
            spec=raw["spec"],
            files=raw.get("files", []),
            depends_on=raw.get("depends_on", []),
            complexity=complexity,
            agent=agent,
            reviewer=reviewer,
            max_rounds=config.max_rounds,
        )
        tasks.append(task)

    return tasks


async def _call_planner(user_message: str, config: Config) -> list[dict]:
    """Call the AI planner and parse its response into raw task dicts."""

    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))

    if has_anthropic:
        return await _plan_with_claude(user_message, config.planner_model)
    elif has_openai:
        return await _plan_with_openai(user_message, config.codex_model)
    else:
        raise RuntimeError(
            "No API keys found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
        )


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
