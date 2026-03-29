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


def assign(
    complexity: TaskComplexity,
    has_anthropic: bool,
    has_openai: bool,
    config_agent: str = "",
    config_reviewer: str = "",
) -> tuple[AgentType, AgentType]:
    """Pick agent and reviewer based on config, then complexity matrix as fallback.

    If the user explicitly configured an agent/reviewer in config.yaml, respect
    that choice. The complexity matrix is only used when no explicit config exists.
    """
    # Start with the complexity-based matrix
    agent, reviewer = ASSIGNMENT_MATRIX[complexity]

    # If only one API is available, use it for both roles
    if has_anthropic and not has_openai:
        return AgentType.CLAUDE, AgentType.CLAUDE
    if has_openai and not has_anthropic:
        return AgentType.CODEX, AgentType.CODEX

    # User's explicit config overrides the matrix
    if config_agent and config_agent != "auto":
        try:
            agent = AgentType(config_agent)
        except ValueError:
            pass

    if config_reviewer and config_reviewer not in ("auto", "human"):
        try:
            reviewer = AgentType(config_reviewer)
        except ValueError:
            pass

    return agent, reviewer
