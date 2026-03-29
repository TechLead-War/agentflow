from __future__ import annotations
from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """Interface for coding agents that implement tasks."""

    @abstractmethod
    async def run(self, prompt: str, working_dir: str) -> str:
        """
        Execute a coding task in the given working directory.

        Args:
            prompt: Full task description + any feedback from previous rounds.
            working_dir: Path to the git worktree where the agent should work.

        Returns:
            Summary of what the agent did.
        """
        ...
