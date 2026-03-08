from __future__ import annotations
import asyncio
import os
import shutil
from .base import BaseReviewer
from ..models import ReviewResult

REVIEW_PROMPT = """\
You are a senior engineer reviewing a code change. Be rigorous but fair.

You will receive:
1. TASK: what the code should accomplish
2. DIFF: the actual code changes
3. ROUND: which review iteration this is

Review for:
- Correctness: does the code actually implement the task?
- Bugs: edge cases, off-by-one errors, null/nil handling
- Security: injection, unsafe operations, hardcoded secrets
- Integration: will this break existing code?

IMPORTANT RULES:
- Mark each issue as "blocker" or "suggestion"
- "blocker" = will cause bugs, crash, break the build, or security vulnerability
- "suggestion" = style, naming, minor improvements, nice-to-have
- If ONLY suggestions remain and no blockers, you MUST say LGTM
- On ROUND 2+: be MORE lenient. The agent already addressed previous feedback.
  Only flag NEW blockers. Do NOT re-raise suggestions or style nits.
  If the core functionality works correctly, say LGTM.
- Do NOT ask for unnecessary changes like adding comments, docstrings, type hints,
  error handling for impossible cases, or renaming variables for style preference.
- Focus on: does it work? Is it correct? Will it break anything?

Respond with EXACTLY one of:

1. If the code is good enough to merge:
   LGTM

2. If changes are needed (blockers only):
   FEEDBACK:
   - Issue description (file:line if applicable) — severity: blocker|suggestion
   - ...
"""


class CodexReviewer(BaseReviewer):
    """Code reviewer using Codex CLI with API fallback."""

    async def review(self, task_spec: str, diff: str, round_num: int,
                     previous_feedback: str | None = None) -> ReviewResult:
        codex_bin = shutil.which("codex")
        if codex_bin:
            return await self._review_cli(codex_bin, task_spec, diff, round_num, previous_feedback)
        return await self._review_api(task_spec, diff, round_num, previous_feedback)

    async def _review_cli(
        self,
        codex_bin: str,
        task_spec: str,
        diff: str,
        round_num: int,
        previous_feedback: str | None = None,
    ) -> ReviewResult:
        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        full_prompt = f"{REVIEW_PROMPT}\n\n{user_content}"

        proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "exec",
            "--full-auto",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate(input=full_prompt.encode("utf-8"))
        text = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Codex reviewer failed (exit {proc.returncode}): {err[:500]}")

        return self._parse_response(text)

    async def _review_api(self, task_spec: str, diff: str, round_num: int,
                          previous_feedback: str | None = None) -> ReviewResult:
        from openai import AsyncOpenAI

        client = AsyncOpenAI()
        model = os.environ.get("AGENTFLOW_CODEX_MODEL", "o3-mini")

        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=2000,
        )

        text = response.choices[0].message.content or ""
        return self._parse_response(text)

    def _parse_response(self, text: str) -> ReviewResult:
        stripped = text.strip()
        # Check for LGTM anywhere in the response
        lines = stripped.split("\n")
        for line in lines:
            if line.strip().upper() == "LGTM":
                return ReviewResult(approved=True, feedback="")

        # If "FEEDBACK:" is present, extract it
        if "FEEDBACK:" in stripped.upper():
            idx = stripped.upper().index("FEEDBACK:")
            feedback = stripped[idx + len("FEEDBACK:"):].strip()
            return ReviewResult(approved=False, feedback=feedback)

        # Ambiguous response — treat as feedback
        return ReviewResult(approved=False, feedback=stripped)
