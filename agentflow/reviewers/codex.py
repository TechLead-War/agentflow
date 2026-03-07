from __future__ import annotations
import os
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
- Performance: anything obviously wasteful?
- Security: injection, unsafe operations, hardcoded secrets
- Integration: will this break existing code?

Respond with EXACTLY one of:

1. If the code is good enough to merge:
   LGTM

2. If changes are needed:
   FEEDBACK:
   - Issue description (file:line if applicable) — severity: blocker|suggestion
   - ...

Only use "blocker" for things that would cause bugs or break the build.
Use "suggestion" for style, naming, or minor improvements.
Do NOT block on suggestions alone — if only suggestions remain, say LGTM.
"""


class CodexReviewer(BaseReviewer):
    """Code reviewer using OpenAI API (GPT-4o / o3-mini)."""

    async def review(self, task_spec: str, diff: str, round_num: int,
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
