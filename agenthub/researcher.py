"""Researcher: deep codebase analysis + LLM synthesis for complex tasks.

When the complexity gate flags a task as complex, this module:
1. Identifies and deep-reads the most relevant files in the repo
2. Calls an LLM to synthesize a research brief: current state, recommended
   approach, constraints/risks, and rationale
3. Returns a ResearchBrief that feeds into the planner for smarter task splits
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from .config import Config
from .models import ResearchBrief
from .prompts import PromptBuilder
from .repo_analysis import RepoAnalysis

logger = logging.getLogger(__name__)

RESEARCHER_TIMEOUT_SEC = int(os.environ.get("AGENTHUB_RESEARCHER_TIMEOUT_SEC", "180"))

# Maximum characters to read per file during deep-read.
_MAX_CHARS_PER_FILE = 5000
# Maximum total characters across all deep-read files.
_MAX_TOTAL_CHARS = 50000


async def research(
    prompt: str,
    analysis: RepoAnalysis,
    config: Config,
    repo_path: str,
) -> ResearchBrief:
    """Produce a research brief by deep-reading code and calling an LLM."""

    max_files = config.research_max_files
    relevant_files = _find_relevant_files(prompt, analysis, repo_path, max_files)
    code_context = _read_files(relevant_files, repo_path)

    user_message = _build_user_message(prompt, analysis, code_context, relevant_files)
    raw_brief = await _call_researcher(user_message, config, repo_path)

    brief = _parse_researcher_response(raw_brief)
    brief.relevant_files = relevant_files

    return brief


def _find_relevant_files(
    prompt: str,
    analysis: RepoAnalysis,
    repo_path: str,
    max_files: int = 10,
) -> list[str]:
    """Score and rank files by relevance to the prompt."""
    prompt_lower = prompt.lower()
    prompt_words = set(re.findall(r"[a-z_][a-z0-9_]*", prompt_lower))

    scored: list[tuple[float, str]] = []

    for path in analysis.tracked_files:
        score = 0.0

        # Direct path mention in prompt
        if path.lower() in prompt_lower:
            score += 10.0

        # Module-style mention (e.g. "agenthub.models")
        module = path.replace("/", ".").replace(".py", "")
        if module.lower() in prompt_lower:
            score += 8.0

        # Path component overlap with prompt words
        path_parts = set(re.findall(r"[a-z_][a-z0-9_]*", path.lower()))
        overlap = prompt_words & path_parts
        score += len(overlap) * 2.0

        # High fan-in bonus (shared/core files are more relevant)
        importers = analysis.imported_by.get(path, set())
        if len(importers) >= 2:
            score += min(len(importers), 5) * 0.5

        # Import neighborhood: if a directly-mentioned file imports this one
        for mentioned_path in analysis.tracked_files:
            if mentioned_path.lower() in prompt_lower:
                if path in analysis.imports.get(mentioned_path, set()):
                    score += 4.0
                if mentioned_path in analysis.imports.get(path, set()):
                    score += 3.0

        # Penalty for test files, build artifacts, configs (less relevant for research)
        if "/test" in path or path.startswith("test"):
            score *= 0.3
        if path.endswith((".json", ".yaml", ".yml", ".toml", ".cfg", ".ini")):
            score *= 0.5

        if score > 0:
            scored.append((score, path))

    # Sort by score descending, take top N
    scored.sort(key=lambda x: -x[0])
    return [path for _, path in scored[:max_files]]


def _read_files(file_paths: list[str], repo_path: str) -> str:
    """Read file contents for the research context, with size caps."""
    parts: list[str] = []
    total_chars = 0

    for rel_path in file_paths:
        if total_chars >= _MAX_TOTAL_CHARS:
            break

        full_path = Path(repo_path) / rel_path
        if not full_path.is_file():
            continue

        try:
            content = full_path.read_text(errors="replace")
        except OSError:
            continue

        # Truncate individual files
        if len(content) > _MAX_CHARS_PER_FILE:
            content = content[:_MAX_CHARS_PER_FILE] + f"\n... (truncated, {len(content)} chars total)"

        remaining = _MAX_TOTAL_CHARS - total_chars
        if len(content) > remaining:
            content = content[:remaining] + "\n... (total context limit reached)"

        parts.append(f"--- {rel_path} ---\n{content}")
        total_chars += len(content)

    return "\n\n".join(parts)


def _build_user_message(
    prompt: str,
    analysis: RepoAnalysis,
    code_context: str,
    relevant_files: list[str],
) -> str:
    """Build the user message for the researcher LLM call."""
    sections = []

    sections.append(f"USER TASK:\n{prompt}")
    sections.append(f"{analysis.to_prompt_context()}")

    if code_context:
        sections.append(f"RELEVANT CODE (deep read):\n{code_context}")

    return "\n\n".join(sections)


async def _call_researcher(user_message: str, config: Config, repo_path: str) -> str:
    """Call a researcher LLM backend and return its raw response."""

    claude_bin = shutil.which("claude")
    codex_bin = shutil.which("codex")
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if not any((claude_bin, codex_bin, has_anthropic_key, has_openai_key)):
        raise RuntimeError(
            "No researcher backend available. Install claude/codex CLI or set API keys."
        )

    researcher_prompt = PromptBuilder.build_researcher_prompt()
    model = config.research_model or config.planner_model
    errors: list[str] = []

    if claude_bin:
        try:
            return await _research_with_claude_cli(
                claude_bin, researcher_prompt, user_message, model, repo_path,
            )
        except Exception as e:
            errors.append(f"claude CLI: {e}")

    if codex_bin:
        try:
            return await _research_with_codex_cli(
                codex_bin, researcher_prompt, user_message, repo_path,
            )
        except Exception as e:
            errors.append(f"codex CLI: {e}")

    if has_anthropic_key:
        try:
            return await _research_with_claude_api(
                researcher_prompt, user_message, model,
            )
        except Exception as e:
            errors.append(f"anthropic API: {e}")

    if has_openai_key:
        try:
            return await _research_with_openai_api(
                researcher_prompt, user_message, config.codex_model,
            )
        except Exception as e:
            errors.append(f"openai API: {e}")

    raise RuntimeError("All researcher backends failed: " + " | ".join(errors))


async def _research_with_claude_cli(
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
        proc.communicate(), timeout=RESEARCHER_TIMEOUT_SEC,
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude researcher failed (exit {proc.returncode}): {err[:500]}")

    return text


async def _research_with_codex_cli(
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
        timeout=RESEARCHER_TIMEOUT_SEC,
    )
    text = stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Codex researcher failed (exit {proc.returncode}): {err[:500]}")

    return text


async def _research_with_claude_api(
    system_prompt: str, user_message: str, model: str,
) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.content else ""


async def _research_with_openai_api(
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
        max_tokens=4096,
    )
    return response.choices[0].message.content or ""


def _parse_researcher_response(text: str) -> ResearchBrief:
    """Extract and parse JSON research brief from LLM response."""
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
            return ResearchBrief.from_dict(data)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(stripped[start:end])
            if isinstance(data, dict):
                return ResearchBrief.from_dict(data)
        except json.JSONDecodeError:
            pass

    # Fallback: treat the entire response as a plain-text brief
    logger.warning("Researcher output was not valid JSON. Using raw text as brief.")
    return ResearchBrief(
        current_state="",
        recommended_approach=stripped[:2000],
        constraints_and_risks="",
        rationale="(Researcher output was not structured — raw text used as approach.)",
    )
