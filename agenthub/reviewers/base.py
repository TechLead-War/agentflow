from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from ..prompts import parse_review_output
from ..models import ReviewResult

logger = logging.getLogger(__name__)


class BaseReviewer(ABC):
    """Interface for code reviewers."""

    @abstractmethod
    async def review(self, task_spec: str, diff: str, round_num: int,
                     previous_feedback: str | None = None) -> ReviewResult:
        """
        Review a code diff against the task specification.

        Args:
            task_spec: The original task description.
            diff: Git diff of the changes.
            round_num: Current review round (1-based).
            previous_feedback: Feedback from the previous round, if any.

        Returns:
            ReviewResult with approved=True or feedback text.
        """
        ...

    def _parse_review_text(self, text: str) -> ReviewResult:
        result, errors = parse_review_output(text)
        if errors:
            logger.warning("Review output parse warnings: %s", errors)
        return result
