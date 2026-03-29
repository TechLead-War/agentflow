# agentflow

Automated coding agents with built-in code review. Give it a task in plain English, walk away, get pinged when it's done.

```bash
agentflow "add rate limiting to all API endpoints"
```

It reads your codebase, splits the work into subtasks, spins up coding agents on separate branches, has each one reviewed by a second AI, iterates on feedback until approved, merges everything, cleans up the branches, and notifies you.

## Install

```bash
cd ~/tools/agentflow   # or wherever you cloned it
pip install -e .
```

Set your API keys:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

Both are optional. If you only have one, agentflow uses that for both coding and review. If you have both, it cross-reviews (Claude codes + Codex reviews, or vice versa) for better results.

## Usage

```bash
# Give it a task
agentflow "refactor the auth module to use JWT tokens"

# Live progress updates in your terminal as it works.
# Walk away — you'll get a macOS notification when it's done.
```

That's the main workflow. A few other commands exist:

```bash
# Reconnect to a running session (if you closed the terminal)
agentflow status

# See what happened in the last run
agentflow log

# Pick up an interrupted run
agentflow resume

# Re-run escalated tasks with fresh rounds
agentflow retry

# Change a default
agentflow config max_rounds 5
agentflow config reviewer claude
```

## How it works

1. **Planner** reads your codebase, builds an internal import graph, and breaks the task into independent subtasks
2. **Scheduler** figures out which subtasks can run in parallel and which depend on others
3. **Workers** (one per subtask) each run on a temporary git branch:
   - A coding agent implements the subtask
   - A reviewer checks the diff and gives feedback
   - The coding agent iterates until the reviewer says LGTM
   - Max 5 rounds by default — after that it escalates for human review
   - Run `agentflow retry` to re-attempt escalated tasks
4. **Merger** squash-merges approved branches back into your working branch
5. **Cleanup** deletes all temporary branches
6. **Notifier** pings you

### Who does what

The planner classifies each subtask and picks the best AI for the job:

| Subtask type | Codes | Reviews |
|---|---|---|
| Architecture, algorithms, refactors | Claude | Codex |
| Features, bugfixes, tests | Codex | Claude |

Cross-review means the reviewer has a different perspective than the coder. Catches more issues.

### Task statuses

Each subtask moves through these statuses during a run:

| Status | Meaning |
|---|---|
| `pending` | Task created, not yet scheduled |
| `queued` | Scheduled in a batch, waiting for a worker slot |
| `agent_working` | Coding agent is implementing the task |
| `reviewing` | Reviewer is checking the diff |
| `iterating` | Agent received feedback, working on fixes |
| `approved` | Reviewer said LGTM, ready to merge |
| `merging` | Being merged into the base branch |
| `merged` | Successfully merged — done |
| `escalated` | Not approved after max rounds, timed out, or hit a merge conflict. Branch preserved for manual review. Run `agentflow retry` to re-attempt. |
| `failed` | Unrecoverable error (agent crash, no changes produced) |

### Review approach

The reviewer acts as a **spec compliance checker**, not a traditional code reviewer. It only checks two things:

1. Does the diff implement every requirement in the task spec?
2. Will the code crash (missing import, syntax error)?

It does not flag style, naming, edge cases not in the spec, performance, or "better" approaches. If the spec is satisfied, the reviewer approves. This keeps the feedback loop tight and avoids unnecessary escalations.

## Configuration

Global defaults live at `~/.agentflow/config.yaml` (auto-created on first run). You can override per-project by adding `.agentflow.yaml` to your repo root.

```yaml
# ~/.agentflow/config.yaml
reviewer: codex              # codex | claude | human
agent: claude                # codex | claude
max_rounds: 5               # feedback iterations before escalating
max_parallel: 4              # concurrent agents
branch_prefix: tmp/af        # temp branch naming
cleanup_branches: true       # delete branches after merge
agent_timeout_sec: 300       # seconds before agent times out (escalates)
prompt_strategy: auto        # auto | zero_shot | few_shot | chain_of_thought | tree_of_thoughts
review_consistency: 1        # number of review passes (majority vote when >1)
claude_model: claude-sonnet-4-20250514
codex_model: o3-mini
```

Project-level overrides:

```yaml
# your-repo/.agentflow.yaml
reviewer: claude
max_rounds: 5
context_files:
  - README.md
  - ARCHITECTURE.md
```

## Logs

Every run is logged to `.agentflow/logs/` in your project directory. Each task gets a folder with the full history: what the agent did, what the reviewer said, how many rounds it took.

```
.agentflow/logs/
  20260307_143022/
    plan.json
    fix-granger/
      round-1-agent.md
      round-1-review.md
      round-2-agent.md
      round-2-review.md    # LGTM
      result.json
    add-evaluation/
      ...
    summary.md
```

Add `.agentflow/` to your `.gitignore`.

## Requirements

- Python 3.11+
- git
- At least one of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- macOS, Linux, or WSL (for notifications, macOS gets native banners)

## Limitations

- Won't push to remote or open PRs — it only works locally
- Large tasks (50+ files changed) may hit context limits — break them up
- If the planner misjudges dependencies, you might get merge conflicts — the merger tries to resolve them automatically but may flag for human help
