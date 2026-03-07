from __future__ import annotations
import sys
from .base import BaseReviewer
from ..models import ReviewResult


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

        while True:
            try:
                answer = input("\nLGTM? (y = approve / n = give feedback / s = skip): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nSkipping review (treating as LGTM).")
                return ReviewResult(approved=True)

            if answer in ("y", "yes", "lgtm"):
                return ReviewResult(approved=True)
            elif answer in ("s", "skip"):
                return ReviewResult(approved=True, feedback="Skipped by human")
            elif answer in ("n", "no"):
                try:
                    feedback = input("Feedback: ").strip()
                except (EOFError, KeyboardInterrupt):
                    feedback = "Human declined without feedback."
                return ReviewResult(approved=False, feedback=feedback)
            else:
                print("Enter y, n, or s.")
