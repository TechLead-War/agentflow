from __future__ import annotations
import os
import shutil
import yaml
from dataclasses import dataclass, field
from pathlib import Path


GLOBAL_DIR = Path.home() / ".agentflow"
GLOBAL_CONFIG = GLOBAL_DIR / "config.yaml"
PROJECT_CONFIG = ".agentflow.yaml"

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

    return Config(**{k: v for k, v in merged.items() if k in Config.__dataclass_fields__})


def set_config_value(key: str, value: str):
    """Update a value in the global config."""
    ensure_global_dir()
    cfg = {}
    if GLOBAL_CONFIG.exists():
        with open(GLOBAL_CONFIG) as f:
            cfg = yaml.safe_load(f) or {}

    # Type coercion
    if value.lower() in ("true", "false"):
        cfg[key] = value.lower() == "true"
    elif value.isdigit():
        cfg[key] = int(value)
    else:
        cfg[key] = value

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
