"""Complexity gate: fast classification of whether a task needs research.

Uses a two-tier approach:
1. Heuristic tier (always runs): regex/keyword matching, essentially free.
2. LLM tier (only if heuristics are inconclusive): a minimal, cheap LLM call.

Simple tasks (bugfix, test, typo) get zero overhead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

from .config import Config
from .models import GateResult
from .repo_analysis import RepoAnalysis

logger = logging.getLogger(__name__)

# ── Heuristic patterns ──────────────────────────────────────────────────────

_COMPLEX_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\barchitect(?:ure)?\b",
        r"\bredesign\b",
        r"\bmigrat(?:e|ion)\b",
        r"\bframework\b",
        r"\bsecurity\b",
        r"\bperformance\b",
        r"\boptimiz(?:e|ation)\b",
        r"\balgorithm\b",
        r"\brefactor\s+(?:the\s+)?entire\b",
        r"\brewrite\b",
        r"\bsystem[- ]?wide\b",
        r"\bcross[- ]?cutting\b",
        r"\bscalability\b",
        r"\bconcurrency\b",
        r"\bauth(?:entication|orization)\s+(?:system|layer|flow)\b",
        r"\bdatabase\s+(?:schema|migration|redesign)\b",
    ]
]

_SIMPLE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bfix\s+(?:\w+\s+)?(?:bug|typo|error|crash|issue)\b",
        r"\bfix\b.*\bbug\b",
        r"\badd\s+(?:\w+\s+)?test\b",
        r"\bwrite\s+(?:\w+\s+)?test\b",
        r"\brename\b",
        r"\bupdate\s+version\b",
        r"\badd\s+(?:\w+\s+)?(?:validation|check)\b",
        r"\btypo\b",
        r"\bsmall\s+change\b",
        r"\bminor\s+fix\b",
        r"\bremove\s+unused\b",
        r"\bupdate\s+(?:readme|docs|documentation)\b",
    ]
]

# Confidence thresholds
_HIGH_CONFIDENCE = 0.85
_LOW_CONFIDENCE = 0.4

GATE_TIMEOUT_SEC = int(os.environ.get("AGENTHUB_GATE_TIMEOUT_SEC", "30"))

_GATE_SYSTEM_PROMPT = """\
Classify this coding task as SIMPLE or COMPLEX.

SIMPLE: bugfix, test, small feature, rename, typo, documentation update, \
adding validation, removing unused code.

COMPLEX: architecture change, algorithm design, security hardening, \
performance optimization, framework migration, cross-cutting refactor, \
database redesign, new system/module design, concurrency work.

Output ONLY the word SIMPLE or COMPLEX."""


async def classify_complexity(
    prompt: str,
    analysis: RepoAnalysis,
    config: Config,
) -> GateResult:
    """Classify whether a prompt needs research before planning."""

    # Tier 1: heuristics
    result = _heuristic_classify(prompt, analysis)
    if result.confidence >= _HIGH_CONFIDENCE:
        logger.info(
            "Gate (heuristic): %s (confidence %.0f%%) — %s",
            "COMPLEX" if result.is_complex else "SIMPLE",
            result.confidence * 100,
            result.reason,
        )
        return result

    # Tier 2: LLM fallback for ambiguous prompts
    try:
        result = await _llm_classify(prompt, config)
        logger.info(
            "Gate (LLM): %s (confidence %.0f%%) — %s",
            "COMPLEX" if result.is_complex else "SIMPLE",
            result.confidence * 100,
            result.reason,
        )
        return result
    except Exception as e:
        logger.warning("Gate LLM fallback failed: %s. Defaulting to SIMPLE.", e)
        return GateResult(
            is_complex=False,
            confidence=0.5,
            reason=f"LLM gate failed ({e}), defaulting to simple.",
            signals=["llm_fallback_error"],
        )


def _heuristic_classify(prompt: str, analysis: RepoAnalysis) -> GateResult:
    """Fast keyword/pattern-based classification."""
    complex_signals: list[str] = []
    simple_signals: list[str] = []

    # Pattern matching
    for pattern in _COMPLEX_PATTERNS:
        match = pattern.search(prompt)
        if match:
            complex_signals.append(f"keyword:{match.group()}")

    for pattern in _SIMPLE_PATTERNS:
        match = pattern.search(prompt)
        if match:
            simple_signals.append(f"keyword:{match.group()}")

    # Prompt length as a weak complexity signal (long prompts tend to describe
    # complex work, but short prompts are NOT necessarily simple).
    word_count = len(prompt.split())
    if word_count > 150:
        complex_signals.append(f"long_prompt:{word_count}_words")

    # High fan-in files mentioned in prompt
    for path in analysis.python_files:
        importers = analysis.imported_by.get(path, set())
        if len(importers) >= 3 and _file_mentioned_in_prompt(path, prompt):
            complex_signals.append(f"high_fan_in:{path}({len(importers)}_importers)")

    # Score
    complex_score = len(complex_signals)
    simple_score = len(simple_signals)
    total = complex_score + simple_score

    if total == 0:
        return GateResult(
            is_complex=False,
            confidence=_LOW_CONFIDENCE,
            reason="No strong signals found.",
            signals=[],
        )

    if complex_score > 0 and simple_score == 0:
        confidence = min(0.6 + complex_score * 0.15, 0.95)
        return GateResult(
            is_complex=True,
            confidence=confidence,
            reason=f"Complex signals: {', '.join(complex_signals[:3])}",
            signals=complex_signals,
        )

    if simple_score > 0 and complex_score == 0:
        confidence = min(0.6 + simple_score * 0.15, 0.95)
        return GateResult(
            is_complex=False,
            confidence=confidence,
            reason=f"Simple signals: {', '.join(simple_signals[:3])}",
            signals=simple_signals,
        )

    # Mixed signals — low confidence, let LLM decide
    is_complex = complex_score > simple_score
    confidence = _LOW_CONFIDENCE
    return GateResult(
        is_complex=is_complex,
        confidence=confidence,
        reason=f"Mixed signals: {complex_score} complex, {simple_score} simple.",
        signals=complex_signals + simple_signals,
    )


def _file_mentioned_in_prompt(path: str, prompt: str) -> bool:
    """Check if a file path or its module name is mentioned in the prompt."""
    prompt_lower = prompt.lower()
    if path.lower() in prompt_lower:
        return True
    # Check module-style reference (e.g. "agenthub.models")
    module = path.replace("/", ".").replace(".py", "")
    return module.lower() in prompt_lower


async def _llm_classify(prompt: str, config: Config) -> GateResult:
    """Use a cheap LLM call to classify ambiguous prompts."""
    truncated = prompt[:500]

    claude_bin = shutil.which("claude")
    if claude_bin:
        return await _classify_with_claude_cli(claude_bin, truncated, config)

    codex_bin = shutil.which("codex")
    if codex_bin:
        return await _classify_with_codex_cli(codex_bin, truncated)

    if os.environ.get("ANTHROPIC_API_KEY"):
        return await _classify_with_claude_api(truncated, config)

    if os.environ.get("OPENAI_API_KEY"):
        return await _classify_with_openai_api(truncated, config)

    raise RuntimeError("No LLM backend available for gate classification.")


async def _classify_with_claude_cli(
    claude_bin: str, prompt: str, config: Config,
) -> GateResult:
    full_prompt = f"{_GATE_SYSTEM_PROMPT}\n\nTASK:\n{prompt}"
    model = config.research_model or config.planner_model

    args = [
        claude_bin, "-p", full_prompt,
        "--output-format", "text",
        "--max-turns", "1",
        "--dangerously-skip-permissions",
    ]
    if model:
        args.extend(["--model", model])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GATE_TIMEOUT_SEC)
    text = stdout.decode("utf-8", errors="replace").strip().upper()
    return _parse_llm_gate_response(text)


async def _classify_with_codex_cli(codex_bin: str, prompt: str) -> GateResult:
    full_prompt = f"{_GATE_SYSTEM_PROMPT}\n\nTASK:\n{prompt}"

    proc = await asyncio.create_subprocess_exec(
        codex_bin, "exec", "--full-auto", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(
        proc.communicate(input=full_prompt.encode("utf-8")),
        timeout=GATE_TIMEOUT_SEC,
    )
    text = stdout.decode("utf-8", errors="replace").strip().upper()
    return _parse_llm_gate_response(text)


async def _classify_with_claude_api(prompt: str, config: Config) -> GateResult:
    from anthropic import AsyncAnthropic

    model = config.research_model or config.planner_model
    client = AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=10,
        system=_GATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"TASK:\n{prompt}"}],
    )
    text = (response.content[0].text if response.content else "").strip().upper()
    return _parse_llm_gate_response(text)


async def _classify_with_openai_api(prompt: str, config: Config) -> GateResult:
    from openai import AsyncOpenAI

    model = config.codex_model
    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _GATE_SYSTEM_PROMPT},
            {"role": "user", "content": f"TASK:\n{prompt}"},
        ],
        max_tokens=10,
    )
    text = (response.choices[0].message.content or "").strip().upper()
    return _parse_llm_gate_response(text)


def _parse_llm_gate_response(text: str) -> GateResult:
    """Parse LLM response which should be just SIMPLE or COMPLEX."""
    is_complex = "COMPLEX" in text
    return GateResult(
        is_complex=is_complex,
        confidence=0.75,
        reason=f"LLM classified as {'COMPLEX' if is_complex else 'SIMPLE'}.",
        signals=["llm_classification"],
    )
