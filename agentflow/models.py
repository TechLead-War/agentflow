from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


class TaskStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"
    AGENT_WORKING = "agent_working"
    REVIEWING = "reviewing"
    ITERATING = "iterating"
    APPROVED = "approved"
    MERGING = "merging"
    MERGED = "merged"
    ESCALATED = "escalated"
    FAILED = "failed"


class AgentType(Enum):
    CLAUDE = "claude"
    CODEX = "codex"


class TaskComplexity(Enum):
    ARCHITECTURE = "architecture"
    ALGORITHM = "algorithm"
    FEATURE = "feature"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TEST = "test"


@dataclass
class Task:
    id: str
    title: str
    spec: str
    rationale: str = ""
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    complexity: TaskComplexity = TaskComplexity.FEATURE
    agent: AgentType = AgentType.CLAUDE
    reviewer: AgentType = AgentType.CODEX
    status: TaskStatus = TaskStatus.PENDING
    current_round: int = 0
    max_rounds: int = 3
    branch: str = ""
    worktree_path: str = ""
    feedback: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "spec": self.spec,
            "rationale": self.rationale,
            "files": self.files,
            "depends_on": self.depends_on,
            "complexity": self.complexity.value,
            "agent": self.agent.value,
            "reviewer": self.reviewer.value,
            "status": self.status.value,
            "current_round": self.current_round,
            "max_rounds": self.max_rounds,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "feedback": self.feedback,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d["id"],
            title=d["title"],
            spec=d["spec"],
            rationale=d.get("rationale", ""),
            files=d.get("files", []),
            depends_on=d.get("depends_on", []),
            complexity=TaskComplexity(d.get("complexity", "feature")),
            agent=AgentType(d.get("agent", "claude")),
            reviewer=AgentType(d.get("reviewer", "codex")),
            status=TaskStatus(d.get("status", "pending")),
            current_round=d.get("current_round", 0),
            max_rounds=d.get("max_rounds", 3),
            branch=d.get("branch", ""),
            worktree_path=d.get("worktree_path", ""),
            feedback=d.get("feedback"),
            error=d.get("error"),
        )


@dataclass
class Batch:
    tasks: list[Task]
    order: int


@dataclass
class ReviewResult:
    approved: bool
    feedback: str = ""


@dataclass
class RunState:
    run_id: str
    started_at: str
    prompt: str
    tasks: list[Task]
    base_branch: str = "main"
    repo_path: str = ""
    status: str = "running"
    finished_at: Optional[str] = None

    @classmethod
    def create(cls, prompt: str, tasks: list[Task], repo_path: str, base_branch: str) -> RunState:
        now = datetime.now()
        return cls(
            run_id=now.strftime("%Y%m%d_%H%M%S"),
            started_at=now.isoformat(),
            prompt=prompt,
            tasks=tasks,
            base_branch=base_branch,
            repo_path=repo_path,
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "prompt": self.prompt,
            "tasks": [t.to_dict() for t in self.tasks],
            "base_branch": self.base_branch,
            "repo_path": self.repo_path,
            "status": self.status,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RunState:
        return cls(
            run_id=d["run_id"],
            started_at=d["started_at"],
            prompt=d["prompt"],
            tasks=[Task.from_dict(t) for t in d["tasks"]],
            base_branch=d.get("base_branch", "main"),
            repo_path=d.get("repo_path", ""),
            status=d.get("status", "running"),
            finished_at=d.get("finished_at"),
        )
