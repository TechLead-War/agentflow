from __future__ import annotations

from ..models import Task, TaskComplexity
from .strategies import PromptStrategy, select_agent_strategy
from . import templates


class PromptBuilder:
    """Composes prompts using the appropriate strategy for each pipeline stage.

    This is the single entry point for all prompt generation in agentflow.
    It applies the correct prompt engineering pattern (zero-shot, few-shot,
    chain-of-thought, tree-of-thoughts) based on the task context.
    """

    @staticmethod
    def build_planner_prompt(
        strategy: PromptStrategy = PromptStrategy.CHAIN_OF_THOUGHT,
    ) -> str:
        """Build the system prompt for the task planner.

        Default strategy is CoT because task decomposition benefits from
        systematic step-by-step reasoning about dependencies and scope.
        """
        parts = [templates.PLANNER_SYSTEM]

        if strategy == PromptStrategy.TREE_OF_THOUGHTS:
            parts.append(templates.PLANNER_TOT_REASONING)
        elif strategy == PromptStrategy.FEW_SHOT:
            parts.append(templates.PLANNER_COT_REASONING)
            parts.append(templates.PLANNER_FEW_SHOT_EXAMPLE)
        elif strategy in (
            PromptStrategy.CHAIN_OF_THOUGHT,
            PromptStrategy.SELF_CONSISTENCY,
            PromptStrategy.AUTO,
        ):
            parts.append(templates.PLANNER_COT_REASONING)

        # Zero-shot gets just the system prompt + output format

        parts.append(templates.PLANNER_OUTPUT_FORMAT)
        parts.append(templates.PLANNER_GUARDRAIL)

        return "\n".join(parts)

    @staticmethod
    def build_agent_prompt(
        task: Task,
        feedback: str | None,
        round_num: int,
        strategy_override: PromptStrategy | None = None,
    ) -> str:
        """Build the full prompt for a coding agent.

        Auto-selects the best strategy based on task complexity:
        - Architecture → Tree of Thoughts (explore designs)
        - Algorithm/Refactor → Chain of Thought (step-by-step)
        - Feature → Zero-shot (direct implementation)
        - Bugfix/Test → Few-shot (examples guide the pattern)
        - Feedback rounds → Always CoT (reason about fixes)
        """
        strategy = select_agent_strategy(
            task.complexity, round_num, strategy_override,
        )

        parts = []

        # ── Role prompting ──
        # Assigns a persona matched to the task type. Research shows role
        # assignment improves output quality for domain-specific tasks.
        role = templates.AGENT_ROLE.get(task.complexity.value)
        if role:
            parts.append(role)

        # ── Task specification ──
        parts.append(f"\n# Task: {task.title}")
        parts.append(f"\n{task.spec}")

        # ── Rationale (always include if available) ──
        if task.rationale:
            parts.append(
                f"\n# Context & Rationale\n"
                f"Understand WHY this change is needed before implementing:\n"
                f"{task.rationale}\n"
                f"Use this reasoning to make informed decisions. If the task says to "
                f"change a value, consider what the rationale tells you about edge cases, "
                f"related code, and downstream effects."
            )

        # ── Files list ──
        if task.files:
            parts.append(
                f"\nFiles to create or modify:\n"
                + "\n".join(f"  - {f}" for f in task.files)
            )

        parts.append(templates.AGENT_SYSTEM_UNDERSTANDING)
        parts.append(templates.AGENT_QUALITY_BAR)

        # ── Strategy-specific sections ──
        if round_num > 1 and feedback:
            # Feedback rounds: always use CoT to reason through fixes
            parts.append(templates.AGENT_FEEDBACK_COT)
            parts.append(
                f"\n# Review Feedback (Round {round_num - 1})\n"
                f"The reviewer returned an 8-check review of your previous implementation. "
                f"Fix every check marked `fail`. Preserve checks that already passed. "
                f"If the decision is `reject`, revisit the approach rather than making a cosmetic patch.\n\n"
                f"{feedback}"
            )
        elif round_num == 1:
            # First round: apply strategy-specific reasoning prefix
            if strategy == PromptStrategy.TREE_OF_THOUGHTS:
                parts.append(templates.AGENT_TOT_PREFIX)
            elif strategy == PromptStrategy.CHAIN_OF_THOUGHT:
                parts.append(templates.AGENT_COT_PREFIX)
            elif strategy == PromptStrategy.FEW_SHOT:
                # Pick the right few-shot example based on task type
                if task.complexity == TaskComplexity.BUGFIX:
                    parts.append(templates.AGENT_FEW_SHOT_BUGFIX)
                elif task.complexity == TaskComplexity.TEST:
                    parts.append(templates.AGENT_FEW_SHOT_TEST)
            # Zero-shot: no extra prefix — just the instructions

            parts.append(templates.AGENT_INSTRUCTIONS_ROUND1)

        # ── Guardrail ──
        parts.append(templates.AGENT_GUARDRAIL)

        return "\n".join(parts)

    @staticmethod
    def build_review_prompt(
        strategy: PromptStrategy = PromptStrategy.CHAIN_OF_THOUGHT,
    ) -> str:
        """Build the system prompt for code reviewers.

        Default strategy is CoT because systematic review catches more bugs
        than a quick skim. Self-consistency uses the same CoT prompt but
        runs multiple passes at the execution level.
        """
        parts = [templates.REVIEWER_SYSTEM]

        if strategy in (
            PromptStrategy.CHAIN_OF_THOUGHT,
            PromptStrategy.SELF_CONSISTENCY,
            PromptStrategy.AUTO,
        ):
            parts.append(templates.REVIEWER_COT_SECTION)

        parts.append(templates.REVIEWER_OUTPUT_FORMAT)
        parts.append(templates.REVIEWER_GUARDRAIL)

        return "\n".join(parts)

    @staticmethod
    def build_researcher_prompt(
        strategy: PromptStrategy = PromptStrategy.CHAIN_OF_THOUGHT,
    ) -> str:
        """Build the system prompt for the researcher.

        Default strategy is CoT because research benefits from systematic
        step-by-step reasoning about the codebase and approaches.
        """
        parts = [templates.RESEARCHER_SYSTEM]

        if strategy in (
            PromptStrategy.CHAIN_OF_THOUGHT,
            PromptStrategy.SELF_CONSISTENCY,
            PromptStrategy.AUTO,
        ):
            parts.append(templates.RESEARCHER_COT_REASONING)

        parts.append(templates.RESEARCHER_OUTPUT_FORMAT)
        parts.append(templates.RESEARCHER_GUARDRAIL)

        return "\n".join(parts)

    @staticmethod
    def build_merge_prompt(branch: str, base: str, title: str) -> str:
        """Build the prompt for merge conflict resolution.

        Always uses CoT (embedded in the template) because merge conflicts
        require careful reasoning about both sides' intent.
        """
        return templates.MERGER_PROMPT_TEMPLATE.format(
            branch=branch, base=base, title=title,
        )

    @staticmethod
    def build_validator_prompt(
        strategy: PromptStrategy = PromptStrategy.CHAIN_OF_THOUGHT,
    ) -> str:
        """Build the system prompt for the post-merge validator.

        Default strategy is CoT because holistic validation benefits from
        systematic step-by-step reasoning about completeness and correctness.
        """
        parts = [templates.VALIDATOR_SYSTEM]

        if strategy in (
            PromptStrategy.CHAIN_OF_THOUGHT,
            PromptStrategy.SELF_CONSISTENCY,
            PromptStrategy.AUTO,
        ):
            parts.append(templates.VALIDATOR_COT_REASONING)

        parts.append(templates.VALIDATOR_OUTPUT_FORMAT)
        parts.append(templates.VALIDATOR_GUARDRAIL)

        return "\n".join(parts)
