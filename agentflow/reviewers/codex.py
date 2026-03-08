from __future__ import annotations
import asyncio
import logging
import os
import shutil
from .base import BaseReviewer
from ..models import ReviewResult
from ..prompts import PromptBuilder, PromptStrategy

logger = logging.getLogger(__name__)


class CodexReviewer(BaseReviewer):
    """Code reviewer using Codex CLI with API fallback.

    Supports self-consistency: when consistency_passes > 1, runs multiple
    review passes and takes the majority vote on approval.
    """

    def __init__(self, consistency_passes: int = 1):
        self.consistency_passes = consistency_passes

    async def review(self, task_spec: str, diff: str, round_num: int,
                     previous_feedback: str | None = None) -> ReviewResult:
        if self.consistency_passes > 1:
            return await self._review_with_consistency(
                task_spec, diff, round_num, previous_feedback,
            )
        return await self._single_review(task_spec, diff, round_num, previous_feedback)

    async def _review_with_consistency(
        self,
        task_spec: str,
        diff: str,
        round_num: int,
        previous_feedback: str | None,
    ) -> ReviewResult:
        """Self-consistency: run N review passes, majority vote on approval."""
        results = await asyncio.gather(
            *(
                self._single_review(task_spec, diff, round_num, previous_feedback)
                for _ in range(self.consistency_passes)
            ),
            return_exceptions=True,
        )

        valid_results = [r for r in results if isinstance(r, ReviewResult)]
        if not valid_results:
            raise RuntimeError("All self-consistency review passes failed")

        approvals = sum(1 for r in valid_results if r.approved)
        majority_approved = approvals > len(valid_results) / 2

        if majority_approved:
            return ReviewResult(approved=True, feedback="")

        all_feedback = [r.feedback for r in valid_results if not r.approved and r.feedback]
        merged = "\n---\n".join(all_feedback) if all_feedback else ""
        logger.info(
            "Self-consistency: %d/%d approved, using aggregated feedback",
            approvals, len(valid_results),
        )
        return ReviewResult(approved=False, feedback=merged)

    async def _single_review(self, task_spec: str, diff: str, round_num: int,
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
        review_prompt = PromptBuilder.build_review_prompt(PromptStrategy.CHAIN_OF_THOUGHT)
        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        full_prompt = f"{review_prompt}\n\n{user_content}"

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

        review_prompt = PromptBuilder.build_review_prompt(PromptStrategy.CHAIN_OF_THOUGHT)
        client = AsyncOpenAI()
        model = os.environ.get("AGENTFLOW_CODEX_MODEL", "o3-mini")

        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": review_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=2000,
        )

        text = response.choices[0].message.content or ""
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
