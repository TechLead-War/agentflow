from .models import AgentType, TaskComplexity

# Maps task complexity to (who codes, who reviews).
# Claude: better at complex reasoning, architecture, algorithms.
# Codex: faster at focused implementation, pattern-heavy code.
# Cross-review: reviewer is always different from coder.

ASSIGNMENT_MATRIX: dict[TaskComplexity, tuple[AgentType, AgentType]] = {
    TaskComplexity.ARCHITECTURE: (AgentType.CLAUDE, AgentType.CODEX),
    TaskComplexity.ALGORITHM:    (AgentType.CLAUDE, AgentType.CODEX),
    TaskComplexity.REFACTOR:     (AgentType.CLAUDE, AgentType.CODEX),
    TaskComplexity.FEATURE:      (AgentType.CODEX,  AgentType.CLAUDE),
    TaskComplexity.BUGFIX:       (AgentType.CODEX,  AgentType.CLAUDE),
    TaskComplexity.TEST:         (AgentType.CODEX,  AgentType.CLAUDE),
}


def assign(complexity: TaskComplexity, has_anthropic: bool, has_openai: bool) -> tuple[AgentType, AgentType]:
    """Pick agent and reviewer based on task complexity and available API keys."""
    agent, reviewer = ASSIGNMENT_MATRIX[complexity]

    # If only one API is available, use it for both roles
    if has_anthropic and not has_openai:
        return AgentType.CLAUDE, AgentType.CLAUDE
    if has_openai and not has_anthropic:
        return AgentType.CODEX, AgentType.CODEX

    return agent, reviewer
