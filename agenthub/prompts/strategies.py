from __future__ import annotations

from enum import Enum

from ..models import TaskComplexity


class PromptStrategy(Enum):
    """Prompt engineering strategies available in the pipeline.

    ZERO_SHOT:        Direct instruction, no examples. Best for clear, simple tasks.
    FEW_SHOT:         Instruction + concrete examples. Best for pattern-following tasks.
    CHAIN_OF_THOUGHT: Step-by-step reasoning. Best for complex logic and debugging.
    SELF_CONSISTENCY:  Multiple reasoning paths, majority vote. Used at execution level
                      (reviewer runs N times, aggregates). The prompt itself uses CoT.
    TREE_OF_THOUGHTS: Explore multiple approaches, evaluate, select best. Best for
                      architectural decisions with multiple valid solutions.
    AUTO:             Auto-select based on task complexity and round number.
    """

    ZERO_SHOT = "zero_shot"
    FEW_SHOT = "few_shot"
    CHAIN_OF_THOUGHT = "chain_of_thought"
    SELF_CONSISTENCY = "self_consistency"
    TREE_OF_THOUGHTS = "tree_of_thoughts"
    AUTO = "auto"


# Maps task complexity to the optimal prompt strategy for the coding agent.
# Rationale:
#   Architecture → ToT: multiple design options should be explored
#   Algorithm    → CoT: step-by-step reasoning prevents logic errors
#   Refactor     → CoT: must reason about what to preserve vs change
#   Feature      → Zero-shot: clear requirements, direct implementation
#   Bugfix       → Few-shot: debugging examples guide the approach
#   Test         → Few-shot: test patterns are best shown by example
AGENT_STRATEGY_MAP: dict[TaskComplexity, PromptStrategy] = {
    TaskComplexity.ARCHITECTURE: PromptStrategy.TREE_OF_THOUGHTS,
    TaskComplexity.ALGORITHM:    PromptStrategy.CHAIN_OF_THOUGHT,
    TaskComplexity.REFACTOR:     PromptStrategy.CHAIN_OF_THOUGHT,
    TaskComplexity.FEATURE:      PromptStrategy.ZERO_SHOT,
    TaskComplexity.BUGFIX:       PromptStrategy.FEW_SHOT,
    TaskComplexity.TEST:         PromptStrategy.FEW_SHOT,
}


def select_agent_strategy(
    complexity: TaskComplexity,
    round_num: int,
    override: PromptStrategy | None = None,
) -> PromptStrategy:
    """Select the best prompt strategy for a coding agent.

    On feedback rounds (round > 1), always uses CoT so the agent
    reasons through what to fix rather than blindly patching.
    On round 1, uses the complexity-based mapping unless overridden.
    """
    if override and override != PromptStrategy.AUTO:
        return override
    if round_num > 1:
        return PromptStrategy.CHAIN_OF_THOUGHT
    return AGENT_STRATEGY_MAP.get(complexity, PromptStrategy.ZERO_SHOT)
