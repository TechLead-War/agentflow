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


class ReviewCheckStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class ReviewDecision(Enum):
    KEEP = "keep"
    RETRY = "retry"
    REJECT = "reject"


REVIEW_CHECKS: tuple[tuple[str, str], ...] = (
    ("run_build", "Does it run/build correctly?"),
    ("task_fit", "Does it solve the actual requested task?"),
    ("scope_regressions", "Did it stay within scope and avoid regressions?"),
    ("logic_edge_cases", "Is the logic correct, including edge cases and failure cases?"),
    ("code_quality", "Is the code/design quality acceptable?"),
    ("approach_justified", "Is the chosen approach justified versus alternatives?"),
    ("metric_improvement", "Did the target metric actually improve?"),
    ("change_decision", "Should we keep, reject, or retry this change?"),
)


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
class ReviewCheck:
    id: str
    question: str
    status: ReviewCheckStatus
    details: str = ""


@dataclass
class ReviewResult:
    approved: bool
    feedback: str = ""
    decision: ReviewDecision = ReviewDecision.KEEP
    summary: str = ""
    checks: list[ReviewCheck] = field(default_factory=list)
    raw_output: str = ""

    def to_log_text(self) -> str:
        lines = [
            f"DECISION: {self.decision.value}",
            f"APPROVED: {'yes' if self.approved else 'no'}",
        ]
        if self.summary:
            lines.append(f"SUMMARY: {self.summary}")
        if self.checks:
            lines.append("CHECKS:")
            for index, check in enumerate(self.checks, 1):
                line = f"{index}. {check.question} [{check.status.value}]"
                if check.details:
                    line += f" {check.details}"
                lines.append(line)
        elif self.feedback:
            lines.append("FEEDBACK:")
            lines.append(self.feedback)
        return "\n".join(lines)


@dataclass
class RunState:
    run_id: str
    started_at: str
    prompt: str
    tasks: list[Task]
    base_branch: str = "main"
    repo_path: str = ""
    status: str = "running"
    phase: str = "initializing"
    current_batch: int = 0
    total_batches: int = 0
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
            "phase": self.phase,
            "current_batch": self.current_batch,
            "total_batches": self.total_batches,
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
            phase=d.get("phase", "initializing"),
            current_batch=d.get("current_batch", 0),
            total_batches=d.get("total_batches", 0),
            finished_at=d.get("finished_at"),
        )
