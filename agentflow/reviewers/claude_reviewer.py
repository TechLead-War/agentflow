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


class ClaudeReviewer(BaseReviewer):
    """Code reviewer using Claude CLI with API fallback."""

    async def review(self, task_spec: str, diff: str, round_num: int,
                     previous_feedback: str | None = None) -> ReviewResult:
        claude_bin = shutil.which("claude")
        if claude_bin:
            return await self._review_cli(claude_bin, task_spec, diff, round_num, previous_feedback)
        return await self._review_api(task_spec, diff, round_num, previous_feedback)

    async def _review_cli(
        self,
        claude_bin: str,
        task_spec: str,
        diff: str,
        round_num: int,
        previous_feedback: str | None = None,
    ) -> ReviewResult:
        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        full_prompt = f"{REVIEW_PROMPT}\n\n{user_content}"
        args = [
            claude_bin,
            "-p", full_prompt,
            "--output-format", "text",
            "--max-turns", "1",
            "--dangerously-skip-permissions",
        ]

        model = os.environ.get("AGENTFLOW_CLAUDE_MODEL", "claude-sonnet-4-20250514")
        if model:
            args.extend(["--model", model])

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Claude reviewer failed (exit {proc.returncode}): {err[:500]}")

        return self._parse_response(text)

    async def _review_api(self, task_spec: str, diff: str, round_num: int,
                          previous_feedback: str | None = None) -> ReviewResult:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
        model = os.environ.get("AGENTFLOW_CLAUDE_MODEL", "claude-sonnet-4-20250514")

        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        response = await client.messages.create(
            model=model,
            max_tokens=2000,
            system=REVIEW_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )

        text = response.content[0].text if response.content else ""
        return self._parse_response(text)

    def _parse_response(self, text: str) -> ReviewResult:
        stripped = text.strip()
        lines = stripped.split("\n")
        for line in lines:
            if line.strip().upper() == "LGTM":
                return ReviewResult(approved=True, feedback="")

        if "FEEDBACK:" in stripped.upper():
            idx = stripped.upper().index("FEEDBACK:")
            feedback = stripped[idx + len("FEEDBACK:"):].strip()
            return ReviewResult(approved=False, feedback=feedback)

        return ReviewResult(approved=False, feedback=stripped)
