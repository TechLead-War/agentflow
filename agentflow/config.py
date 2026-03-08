from __future__ import annotations
import os
import shutil
import logging
import yaml
from dataclasses import dataclass, field
from pathlib import Path


GLOBAL_DIR = Path.home() / ".agentflow"
GLOBAL_CONFIG = GLOBAL_DIR / "config.yaml"
PROJECT_CONFIG = ".agentflow.yaml"
logger = logging.getLogger(__name__)

DEFAULTS = {
    "reviewer": "codex",
    "agent": "claude",
    "max_rounds": 5,
    "max_parallel": 4,
    "branch_prefix": "tmp/af",
    "cleanup_branches": True,
    "notify": True,
    "claude_model": "claude-sonnet-4-20250514",
    "codex_model": "o3-mini",
    "planner_model": "claude-sonnet-4-20250514",
    "context_files": [],
    "agent_timeout_sec": 300,
    "prompt_strategy": "auto",
    "review_consistency": 1,
}

_INT_FIELDS = {"max_rounds", "max_parallel", "agent_timeout_sec", "review_consistency"}
_BOOL_FIELDS = {"cleanup_branches", "notify"}
_ALLOWED_AGENT = {"claude", "codex"}
_ALLOWED_REVIEWER = {"claude", "codex", "human"}
_ALLOWED_PROMPT_STRATEGY = {
    "auto", "zero_shot", "few_shot", "chain_of_thought",
    "self_consistency", "tree_of_thoughts",
}


@dataclass
class Config:
    reviewer: str = "codex"
    agent: str = "claude"
    max_rounds: int = 5
    max_parallel: int = 4
    branch_prefix: str = "tmp/af"
    cleanup_branches: bool = True
    notify: bool = True
    claude_model: str = "claude-sonnet-4-20250514"
    codex_model: str = "o3-mini"
    planner_model: str = "claude-sonnet-4-20250514"
    context_files: list[str] = field(default_factory=list)
    agent_timeout_sec: int = 300
    prompt_strategy: str = "auto"
    review_consistency: int = 1


def _coerce_config_value(key: str, value, default):
    """Best-effort coercion with fallback to defaults for invalid values."""
    if value is None:
        return default

    if key in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        logger.warning("Invalid bool for config '%s': %r. Using default %r.", key, value, default)
        return default

    if key in _INT_FIELDS:
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid int for config '%s': %r. Using default %r.", key, value, default)
            return default

        if number < 1:
            logger.warning("Config '%s' must be >= 1. Got %r, using default %r.", key, value, default)
            return default
        return number

    if key == "context_files":
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        logger.warning("Invalid context_files value %r. Using default %r.", value, default)
        return default

    if key == "agent":
        normalized = str(value).strip().lower()
        if normalized in _ALLOWED_AGENT:
            return normalized
        logger.warning("Invalid agent '%s'. Using default '%s'.", value, default)
        return default

    if key == "reviewer":
        normalized = str(value).strip().lower()
        if normalized in _ALLOWED_REVIEWER:
            return normalized
        logger.warning("Invalid reviewer '%s'. Using default '%s'.", value, default)
        return default

    if key == "prompt_strategy":
        normalized = str(value).strip().lower()
        if normalized in _ALLOWED_PROMPT_STRATEGY:
            return normalized
        logger.warning("Invalid prompt_strategy '%s'. Using default '%s'.", value, default)
        return default

    if key == "branch_prefix":
        text = str(value).strip()
        return text or default

    return value


def ensure_global_dir():
    """Create ~/.agentflow/ and default config if they don't exist."""
    GLOBAL_DIR.mkdir(exist_ok=True)
    if not GLOBAL_CONFIG.exists():
        with open(GLOBAL_CONFIG, "w") as f:
            yaml.dump(DEFAULTS, f, default_flow_style=False)


def load_config(project_dir: str = ".") -> Config:
    """Load config: defaults → global → project-local (each layer overrides)."""
    ensure_global_dir()
    merged = dict(DEFAULTS)

    # Global config
    if GLOBAL_CONFIG.exists():
        with open(GLOBAL_CONFIG) as f:
            global_cfg = yaml.safe_load(f) or {}
        merged.update({k: v for k, v in global_cfg.items() if v is not None})

    # Project-local config
    project_cfg_path = Path(project_dir) / PROJECT_CONFIG
    if project_cfg_path.exists():
        with open(project_cfg_path) as f:
            project_cfg = yaml.safe_load(f) or {}
        merged.update({k: v for k, v in project_cfg.items() if v is not None})

    normalized = {}
    for key in Config.__dataclass_fields__:
        normalized[key] = _coerce_config_value(key, merged.get(key), DEFAULTS.get(key))

    return Config(**normalized)


def set_config_value(key: str, value: str):
    """Update a value in the global config."""
    if key not in Config.__dataclass_fields__:
        raise ValueError(f"Unknown config key: {key}")

    ensure_global_dir()
    cfg = {}
    if GLOBAL_CONFIG.exists():
        with open(GLOBAL_CONFIG) as f:
            cfg = yaml.safe_load(f) or {}

    cfg[key] = _coerce_config_value(key, value, DEFAULTS.get(key))

    with open(GLOBAL_CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)


def check_api_keys() -> tuple[bool, bool]:
    """Return (has_anthropic, has_openai) via API keys or installed CLIs."""
    has_claude_cli = bool(shutil.which("claude"))
    has_codex_cli = bool(shutil.which("codex"))
    return (
        bool(os.environ.get("ANTHROPIC_API_KEY")) or has_claude_cli,
        bool(os.environ.get("OPENAI_API_KEY")) or has_codex_cli,
    )
