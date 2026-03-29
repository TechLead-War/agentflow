# Agentflow Architecture

## Purpose

`agentflow` is a local orchestration system for breaking a natural-language coding request into tasks, assigning those tasks to coding agents, reviewing the results, and merging approved work back into the current branch.

It is not a hosted service. It runs inside a local git repository, uses local state in `.agentflow/`, and relies on available LLM backends such as the `claude` CLI, the `codex` CLI, or their APIs.


## High-Level Flow

At runtime, the system follows this sequence:

1. Read the user prompt from the CLI.
2. Detect the current repository root and ensure the repo has an initial commit.
3. Stash local uncommitted changes if the worktree is dirty.
4. Build repository analysis from tracked files and Python import relationships.
5. Optionally run a complexity gate and a research phase before planning.
6. Ask the planner to split the work into tasks.
7. Build dependency-aware execution batches.
8. Run one worker loop per task inside temporary branches and worktrees.
9. Rebase approved branches onto the latest base branch, re-review them, then merge them.
10. Optionally validate the full merged result and, if needed, run one automatic validation-fix task.
11. Clean up merged branches/worktrees, write logs, notify the user, and restore the original stash.


## System Model

The system is centralized. There is no autonomous "manager agent" separate from the code. The orchestration logic lives in the Python runtime, mainly in `cli.py`, and it coordinates several specialized LLM-driven steps.

Those steps are:

- Complexity gate
- Researcher
- Planner
- Scheduler
- Coding workers
- Reviewers
- Merger
- Validator

The orchestration code owns state transitions, batching, retries, merge decisions, log writing, and cleanup.


## Entry Points

The CLI supports these user-facing modes:

- `agentflow "task"`
- `agentflow --prompt-file <path>`
- `agentflow -` for stdin
- `agentflow status`
- `agentflow log`
- `agentflow resume`
- `agentflow retry`
- `agentflow clean`
- `agentflow config <key> <value>`

`status` reads saved run state and renders current progress.

`log` prints the last summary or task log listing.

`resume` continues incomplete tasks from saved state.

`retry` only retries tasks that ended in `escalated`, resets their branches/worktrees, and reruns them.

`clean` removes saved run state and optionally deletes logs when called with `--logs`.


## Repository Preconditions

The system assumes it is running inside a git repository.

Before a run starts:

- It resolves the repo root using git.
- If the repository has no commits yet, it creates an empty initial commit.
- If the working tree is dirty, it stashes the changes instead of refusing to run.

At the end of the run, it attempts to restore the stash.


## Configuration

Configuration is loaded in three layers:

1. Built-in defaults
2. Global config at `~/.agentflow/config.yaml`
3. Project config at `.agentflow.yaml`

Current config surface:

- `reviewer`
- `agent`
- `max_rounds`
- `max_parallel`
- `branch_prefix`
- `cleanup_branches`
- `notify`
- `claude_model`
- `codex_model`
- `planner_model`
- `context_files`
- `agent_timeout_sec`
- `prompt_strategy`
- `review_consistency`
- `research_enabled`
- `research_model`
- `research_max_files`
- `validation_enabled`


## Runtime Phases

The progress UI tracks these phases:

- `initializing`
- `gating`
- `researching`
- `planning`
- `scheduling`
- `running`
- `merging`
- `validating`
- `validation_failed`
- `completed`
- `failed`
- `interrupted`

These phases are persisted in run state and are also used by the progress renderer.


## Complexity Gate

The gate decides whether a prompt is simple enough to skip research or complex enough to justify a deeper pre-planning pass.

It uses two tiers:

1. Heuristic classification
2. LLM fallback when heuristics are inconclusive

The heuristic layer looks for prompt signals such as architecture, migration, framework, security, performance, concurrency, large refactors, and system-wide changes. It also considers prompt length and whether the prompt directly mentions high fan-in files.

If heuristics are confident, the gate returns immediately.

If heuristics are mixed or weak, the system asks an LLM to classify the task as `SIMPLE` or `COMPLEX`.

If the gate itself fails, the system defaults to treating the task as simple and continues without research.


## Repository Analysis

Before planning, the system builds a local repository model from tracked files.

Today this analysis is Python-focused:

- It enumerates tracked files.
- It parses Python files with the AST module.
- It builds an internal import graph:
  - which file imports which file
  - which files have many importers

This analysis is used in three places:

1. To give the gate stronger signals.
2. To help the researcher focus on relevant files.
3. To give the planner and scheduler better dependency information.


## Research Phase

If the gate marks a task as complex and `research_enabled` is true, the system runs a researcher phase before planning.

The researcher does not implement code. It does two things:

1. Select relevant repository files using prompt/file relevance scoring and import-neighborhood signals.
2. Deep-read those files and ask an LLM to synthesize a structured brief.

The brief includes:

- current state
- recommended approach
- constraints and risks
- rationale
- relevant files
- optional sources field

Important current behavior:

- The researcher reads local repository files.
- It asks an LLM to synthesize a brief from that code context.
- It does not independently run builds/tests.
- It does not perform a dedicated external web-search step in the current codebase.

If researcher output is not valid JSON, the system falls back to storing the raw text as the recommended approach.

If the researcher fails entirely, planning still proceeds without a brief.


## Planner

The planner receives:

- the repository file tree
- selected context files such as `README.md`, `CLAUDE.md`, `.agentflow.yaml`, and `ARCHITECTURE.md` when present
- repository import analysis
- the optional research brief
- the user request

It returns structured tasks with:

- `id`
- `title`
- `spec`
- `rationale`
- `files`
- `depends_on`
- `complexity`

The planner output is validated structurally before use.

After task creation, repository analysis may add extra dependencies between tasks when one task edits an importer and another edits the imported module.


## Agent Assignment

Each task is assigned a coding agent and a reviewer based on its declared complexity.

Current default matrix:

- Architecture, algorithm, refactor:
  - coder: Claude
  - reviewer: Codex
- Feature, bugfix, test:
  - coder: Codex
  - reviewer: Claude

If only one provider is available, the same provider is used for both coding and review.


## Scheduler

The scheduler builds a task dependency graph from `depends_on`.

It then adds implicit file-overlap dependencies using a star pattern:

- the first task touching a file becomes the anchor
- later tasks touching the same file depend on that anchor

This reduces direct conflicts without fully serializing every overlapping task.

After that:

- transitive dependencies are removed
- simple dependency metrics are logged
- tasks are topologically sorted into batches

A task becomes runnable when all of its dependencies are already completed.

Tasks in the same batch run in parallel, limited by `max_parallel`.


## Worker Execution Model

Each task runs in its own temporary git branch and git worktree.

For each task, the worker loop:

1. Creates a branch from the base branch captured at run start.
2. Creates a temporary worktree under the system temp directory.
3. Runs the coding agent.
4. Commits agent changes.
5. Runs the reviewer.
6. Either approves, retries, rejects, or escalates.

The worktree is removed at the end of the task loop, but the branch is preserved until merge or escalation handling.


## Coding Agents

Two coding agent implementations exist:

- Claude agent
- Codex agent

Current backend behavior:

- Claude agent requires the `claude` CLI.
- Codex agent prefers the `codex` CLI and falls back to the OpenAI API.
- The Codex API fallback writes files by parsing structured file blocks from the model output.


## Coding Prompt Model

The coding prompt now has two important qualities:

1. On the first attempt, it tells the coder to understand the surrounding system before editing code.
2. It tells the coder that the output will be judged against the same 8 review checks used by the reviewer.

The first-round prompt explicitly instructs the coder to:

- understand the relevant system and adjacent files
- preserve contracts and integration points
- follow current repo/language/framework syntax
- avoid outdated or deprecated patterns
- aim to pass the full review bar on the first submission

Retry rounds receive the full prior review and are told to fix failed checks while preserving already-passing parts.


## Review Model

The review system is structured, not free-form.

Every review scores these 8 checks:

1. Run/build correctness
2. Task fit
3. Scope and regressions
4. Logic, edge cases, failure cases
5. Code/design quality
6. Approach justification
7. Metric improvement
8. Final keep/reject/retry judgment

The reviewer must return structured JSON with:

- a summary
- a decision: `keep`, `retry`, or `reject`
- all 8 checks in a fixed order

The system parses that into a typed `ReviewResult`.

Behavior by decision:

- `keep`: task is approved
- `retry`: feedback goes back to the coder for another round
- `reject`: task escalates for manual review

Important current limitation:

- The reviewer reasons from `task + diff`.
- It does not execute a real build, test suite, or benchmark command during per-task review.
- So checks like "does it run" and "did the metric improve" are still judgment-based at review time unless the diff itself makes the answer clear.
- A separate post-merge validator may run an auto-detected test command later, after merges are complete.


## Self-Consistency Review

When `review_consistency > 1`, the reviewer runs multiple passes and aggregates them.

If a majority approves, the change is approved.

If not, the system picks a representative non-approved structured result, preferring `reject` over `retry` when tied.


## Retry and Escalation Rules

A task escalates when:

- the coding agent times out
- the reviewer rejects the change
- the reviewer crashes
- the worker exhausts all retry rounds
- the retry loop appears stale
- merge refresh fails
- merge conflict resolution fails

The stale-loop detector compares the current diff to the previous diff. If similarity stays above 95 percent on later rounds, the task escalates instead of being auto-approved.

Escalated branches are preserved for manual inspection or later retry.


## No-Op Handling

The worker has explicit no-op handling.

If the first round produces no diff and the agent claims the task is already satisfied, the task is treated as complete without a merge branch.

If a later round produces no diff against base, the task is also treated as complete.

So a task can end in `merged` status without generating a final merge commit if no additional changes are needed.


## Merge Phase

The merger only considers tasks with `approved` status.

Current merge flow:

1. Switch to the base branch if needed.
2. For each approved branch:
   - reopen it in a temporary merge-check worktree
   - rebase it onto the latest base branch
   - compute its new diff against the latest base
   - run a fresh review on that rebased diff
3. If the rebased branch still passes review, squash-merge it.
4. If the squash merge conflicts, try agent-assisted conflict resolution.
5. If refresh, re-review, or conflict resolution fails, escalate the task and preserve the branch.

This closes the earlier safety gap where an approved branch could be merged against a stale base snapshot.

Important current limitation:

- The merge refresh reruns review, not a real build/test pipeline.
- So it protects against stale-diff logic and branch drift better than before, but it is not a full CI replacement.


## Post-Merge Validation

If `validation_enabled` is true, the system enters a validation phase after merge.

This validation path is shared by the main `run`, `resume`, and `retry` commands.

The validator works on the combined result of the whole run, not on one task at a time.

Current validation flow:

1. Compute the combined diff from the run's starting commit to the current `HEAD`.
2. Try to auto-detect a test/build command from the repository.
3. Ask a validator LLM to judge whether the final merged result fulfills the original request and whether the combined change looks safe.
4. If validation passes, the run completes.
5. If validation fails, create one synthetic `validation-fix` task and send it through the normal worker and merge loop.
6. Run validation one more time after that fix task is merged.
7. If the second validation still fails, save the merged result to a safe branch, hard-reset the base branch back to the run's starting commit, and mark the run as `validation_failed`.

Current test/build detection is heuristic and limited to a small set of common commands:

- `python -m pytest --tb=short -q`
- `npm test`
- `make test`
- `cargo test`
- `go test ./...`

Important current behavior:

- Validation is holistic: it sees the original request, the full combined diff, and any detected build/test output.
- There is only one automatic validation-fix round.
- If the first validator call itself errors, the run logs a warning and completes without blocking on validation.
- If the second validator call errors after a validation-fix attempt, the run saves work to a safe branch, rolls back the base branch, and ends as `validation_failed`.
- The safe branch name is `<branch_prefix>-validation-failed-<run_id>`.


## Conflict Resolution

If a squash merge conflicts, the merger asks a coding agent to resolve the conflict in the repository root.

The conflict-resolution prompt tells the agent to preserve both sides' intent and stage resolved files.

If unresolved conflict markers remain, the merge is aborted/reset and the task escalates.


## State and Logging

Runtime state is stored in `.agentflow/state.json`.

Logs are stored in `.agentflow/logs/<run_id>/`.

Persisted run state includes:

- the prompt
- task list and task statuses
- the base branch
- the base commit SHA captured at run start
- phase/status fields
- batch counters
- validation attempt counter
- finish time when present

Current log artifacts include:

- `plan.json`
- `research.json`
- `validation-1.json`
- `validation-2.json` when a second validation attempt happens
- task-level round logs
- `summary.md`

Task-level logs include agent output, review output, and merge-review output when applicable.


## Recovery Commands

### `resume`

`resume` reloads the last run and:

- merges immediately if all remaining tasks are already done
- otherwise reschedules incomplete tasks and continues execution

Important current behavior:

- `resume` does not rerun the planning or research phases.
- After resumed work is merged, it reruns the shared post-merge validation phase when validation is enabled.
- If that validation fails after the auto-fix attempt, the merged result is preserved on a safe branch and the base branch is rolled back.

### `retry`

`retry` only targets tasks in `escalated`.

For each escalated task it:

- removes stale worktrees
- deletes preserved task branches
- clears task runtime fields
- resets retry counters
- strips dependencies on tasks outside the retry set

Then it reruns those tasks and merges any newly approved branches.

Important current behavior:

- `retry` only re-executes escalated tasks plus merge.
- After that merge, it reruns the shared post-merge validation phase when validation is enabled.
- If that validation fails after the auto-fix attempt, the merged result is preserved on a safe branch and the base branch is rolled back.


## Notifications

At the end of a run, the notifier:

- sends a macOS notification when running on macOS
- always rings the terminal bell as a fallback


## Current Strengths

- Explicit orchestration instead of an opaque agent swarm
- Complexity-gated research before planning
- Import-aware planning hints and dependency augmentation
- Structured reviewer output with explicit keep/retry/reject decisions
- Retry, resume, and escalation paths
- Pre-merge rebase plus re-review against latest base
- Post-merge holistic validation with one automatic fix attempt
- Persistent local logs and recoverable state


## Current Limitations

The current code still has important boundaries:

- No remote push, PR creation, or hosted coordination
- No dedicated external web research step in the researcher
- No real build/test/benchmark execution inside the reviewer or merge refresh
- Post-merge validation is heuristic, limited to one auto-fix attempt, and can fail open on the first validator error
- Repository analysis is strongest for Python imports; other languages are not modeled as deeply
- Task overlap detection is file-level plus Python-import aware, not symbol-level
- Conflict resolution is best-effort and can still escalate


## Design Intent

The core design choice is to keep one deterministic orchestrator in control while using agents for bounded reasoning tasks:

- gate when research is worth the cost
- research the problem when needed
- plan from a brief instead of guessing blindly
- run isolated task workers
- use structured reviews as a quality gate
- integrate branches conservatively

That keeps the system inspectable and recoverable while still getting leverage from coding agents.
