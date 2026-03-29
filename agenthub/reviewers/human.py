from __future__ import annotations
from .base import BaseReviewer
from ..models import REVIEW_CHECKS, ReviewCheck, ReviewCheckStatus, ReviewDecision, ReviewResult


class HumanReviewer(BaseReviewer):
    """Interactive reviewer that shows the diff and asks for human input."""

    async def review(self, task_spec: str, diff: str, round_num: int,
                     previous_feedback: str | None = None) -> ReviewResult:
        print("\n" + "=" * 60)
        print(f"  HUMAN REVIEW — Round {round_num}")
        print("=" * 60)
        print(f"\nTASK: {task_spec[:200]}...")
        print(f"\nDIFF ({len(diff)} chars):")
        print("-" * 40)

        # Show a reasonable amount of the diff
        if len(diff) > 3000:
            print(diff[:3000])
            print(f"\n... ({len(diff) - 3000} more chars, see full diff in logs)")
        else:
            print(diff)

        print("-" * 40)
        print("\nScore each review check: p = pass, f = fail, n = not applicable.")

        checks: list[ReviewCheck] = []
        for check_id, question in REVIEW_CHECKS:
            while True:
                try:
                    answer = input(f"{question} (p/f/n): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nSkipping review (treating as retry).")
                    return ReviewResult(
                        approved=False,
                        feedback="Human review interrupted.",
                        decision=ReviewDecision.RETRY,
                        summary="Human review interrupted.",
                    )

                status_map = {
                    "p": ReviewCheckStatus.PASS,
                    "pass": ReviewCheckStatus.PASS,
                    "f": ReviewCheckStatus.FAIL,
                    "fail": ReviewCheckStatus.FAIL,
                    "n": ReviewCheckStatus.NOT_APPLICABLE,
                    "na": ReviewCheckStatus.NOT_APPLICABLE,
                }
                status = status_map.get(answer)
                if status is None:
                    print("Enter p, f, or n.")
                    continue

                try:
                    details = input("Details: ").strip()
                except (EOFError, KeyboardInterrupt):
                    details = ""

                checks.append(
                    ReviewCheck(
                        id=check_id,
                        question=question,
                        status=status,
                        details=details or "No details provided.",
                    )
                )
                break

        while True:
            try:
                answer = input(
                    "\nDecision? (k = keep / r = retry / x = reject / s = skip-as-keep): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nSkipping review (treating as keep).")
                return ReviewResult(
                    approved=True,
                    decision=ReviewDecision.KEEP,
                    summary="Skipped by human reviewer.",
                    checks=checks,
                    raw_output="Skipped by human reviewer.",
                )

            if answer in ("k", "keep"):
                try:
                    summary = input("Summary: ").strip()
                except (EOFError, KeyboardInterrupt):
                    summary = ""
                return ReviewResult(
                    approved=True,
                    feedback="",
                    decision=ReviewDecision.KEEP,
                    summary=summary or "Approved by human reviewer.",
                    checks=checks,
                    raw_output="Human keep decision.",
                )
            elif answer in ("s", "skip"):
                return ReviewResult(
                    approved=True,
                    feedback="",
                    decision=ReviewDecision.KEEP,
                    summary="Skipped by human reviewer.",
                    checks=checks,
                    raw_output="Skipped by human reviewer.",
                )
            elif answer in ("r", "retry", "x", "reject"):
                try:
                    summary = input("Summary: ").strip()
                except (EOFError, KeyboardInterrupt):
                    summary = ""
                decision = ReviewDecision.RETRY if answer in ("r", "retry") else ReviewDecision.REJECT
                feedback = ReviewResult(
                    approved=False,
                    decision=decision,
                    summary=summary or "Changes requested by human reviewer.",
                    checks=checks,
                    raw_output=f"Human {decision.value} decision.",
                ).to_log_text()
                return ReviewResult(
                    approved=False,
                    feedback=feedback,
                    decision=decision,
                    summary=summary or "Changes requested by human reviewer.",
                    checks=checks,
                    raw_output=f"Human {decision.value} decision.",
                )
            else:
                print("Enter k, r, x, or s.")
