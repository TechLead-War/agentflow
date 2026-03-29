from __future__ import annotations
import asyncio
import logging
import os
import shutil
from collections import Counter
from .base import BaseReviewer
from ..models import ReviewDecision, ReviewResult
from ..prompts import PromptBuilder, PromptStrategy

logger = logging.getLogger(__name__)


class ClaudeReviewer(BaseReviewer):
    """Code reviewer using Claude CLI with API fallback.

    Supports self-consistency: when consistency_passes > 1, runs multiple
    review passes and takes the majority vote on approval. This catches
    more bugs by leveraging the fact that correct review conclusions
    converge while incorrect ones diverge.
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

        # Filter out errors
        valid_results = [r for r in results if isinstance(r, ReviewResult)]
        if not valid_results:
            raise RuntimeError("All self-consistency review passes failed")

        # Majority vote on approval
        approvals = sum(1 for r in valid_results if r.approved)
        majority_approved = approvals > len(valid_results) / 2

        if majority_approved:
            for result in valid_results:
                if result.approved:
                    return result

        decisions = Counter(r.decision for r in valid_results if not r.approved)
        if decisions:
            top_count = max(decisions.values())
            candidates = [
                decision for decision, count in decisions.items()
                if count == top_count
            ]
            preferred = ReviewDecision.REJECT if ReviewDecision.REJECT in candidates else candidates[0]
            for result in valid_results:
                if not result.approved and result.decision == preferred:
                    return result

        logger.info(
            "Self-consistency: %d/%d approved, using first non-approved structured result",
            approvals, len(valid_results),
        )
        return valid_results[0]

    async def _single_review(self, task_spec: str, diff: str, round_num: int,
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
        review_prompt = PromptBuilder.build_review_prompt(PromptStrategy.CHAIN_OF_THOUGHT)
        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        full_prompt = f"{review_prompt}\n\n{user_content}"
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

        review_prompt = PromptBuilder.build_review_prompt(PromptStrategy.CHAIN_OF_THOUGHT)
        client = AsyncAnthropic()
        model = os.environ.get("AGENTFLOW_CLAUDE_MODEL", "claude-sonnet-4-20250514")

        user_content = f"TASK:\n{task_spec}\n\nDIFF:\n{diff[:12000]}\n\nROUND: {round_num}"
        if previous_feedback:
            user_content += f"\n\nPREVIOUS FEEDBACK (round {round_num - 1}):\n{previous_feedback}"

        response = await client.messages.create(
            model=model,
            max_tokens=2000,
            system=review_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        text = response.content[0].text if response.content else ""
        return self._parse_response(text)

    def _parse_response(self, text: str) -> ReviewResult:
        return self._parse_review_text(text)
