# agenthub

Automated coding agents with built-in code review. Give it a task in plain English, walk away, get pinged when it's done.

```bash
agenthub "add rate limiting to all API endpoints"
```

It reads your codebase, splits the work into subtasks, spins up coding agents on separate branches, has each one reviewed by a second AI, iterates on feedback until approved, validates the merged result, and notifies you.

## Install

```bash
cd ~/tools/agenthub   # or wherever you cloned it
pip install -e .
```

Set up at least one backend — either an installed CLI or an API key:

```bash
# CLI backends (no API key needed if the CLI is already authenticated)
# Install claude: https://docs.anthropic.com/en/docs/claude-code
# Install codex:  https://github.com/openai/codex

# API key backends
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

If you only have one backend, agenthub uses it for both coding and review. If you have both, it cross-reviews (Claude codes + Codex reviews, or vice versa) for better results.

## Usage

```bash
# Give it a task
agenthub "refactor the auth module to use JWT tokens"

# Read the task from a file
agenthub --prompt-file /tmp/prompt.txt
agenthub -f /tmp/prompt.txt

# Read from stdin
cat /tmp/prompt.txt | agenthub -

# Live progress updates in your terminal as it works.
# Walk away — you'll get a notification when it's done.
```

Other commands:

```bash
# Show progress of current/last run
agenthub status

# See what happened in the last run
agenthub log

# Pick up an interrupted run
agenthub resume

# Re-run escalated tasks with fresh rounds
agenthub retry

# Clear state from previous runs (keeps logs)
agenthub clean

# Clear state and remove current run's log directory
agenthub clean --logs

# View current config
agenthub config

# Change a default
agenthub config max_rounds 5
agenthub config reviewer claude
```

## How it works

1. **Planner** reads your codebase, builds an internal import graph, and breaks the task into independent subtasks
2. **Complexity gate** classifies the task. If it looks complex (architecture, algorithms), a research phase runs first to deep-read relevant files and produce a brief that feeds into planning
3. **Scheduler** figures out which subtasks can run in parallel and which depend on others
4. **Workers** (one per subtask) each run on a temporary git branch:
   - A coding agent implements the subtask
   - A reviewer checks the diff against an 8-check rubric and gives feedback
   - The coding agent iterates until the reviewer approves
   - Max rounds default to 5 — after that the task escalates for human review
   - Run `agenthub retry` to re-attempt escalated tasks
5. **Merger** squash-merges approved branches back into your working branch
6. **Validator** (when enabled) runs a holistic check on all merged changes — diffs the combined result against the original state, auto-detects and runs tests if available (pytest, npm test, make test, cargo test, go test), and calls an LLM to verify delivery and catch regressions. If validation fails, it creates a fix task, runs it through the agent→review→merge loop, and validates again. If the second attempt also fails, the merged work is saved to a safe branch (`<branch_prefix>-validation-failed-<run_id>`, e.g. `tmp/ah-validation-failed-abc123`) and the base branch is rolled back to its pre-run state
7. **Cleanup** (when `cleanup_branches` is enabled) deletes temporary branches for merged tasks only — branches for failed or escalated tasks are preserved so you can inspect them
8. **Notifier** sends a macOS notification (with sound) or a terminal bell

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
| `escalated` | Not approved after max rounds, timed out, or hit a merge conflict. Branch preserved for manual review. Run `agenthub retry` to re-attempt. |
| `failed` | Unrecoverable error (agent crash, no changes produced) |

### Review approach

The reviewer evaluates each diff against an 8-check rubric:

| Check | Question |
|---|---|
| `run_build` | Does it run/build correctly? |
| `task_fit` | Does it solve the actual requested task? |
| `scope_regressions` | Did it stay within scope and avoid regressions? |
| `logic_edge_cases` | Is the logic correct, including edge cases and failure cases? |
| `code_quality` | Is the code/design quality acceptable? |
| `approach_justified` | Is the chosen approach justified versus alternatives? |
| `metric_improvement` | Did the target metric actually improve? |
| `change_decision` | Should we keep, reject, or retry this change? |

Each check gets a pass, fail, or not-applicable result. The reviewer makes a final decision: keep (approve), retry (send feedback), or reject (escalate).

## Configuration

Global defaults live at `~/.agenthub/config.yaml` (auto-created on first run). You can override per-project by adding `.agenthub.yaml` to your repo root.

```yaml
# ~/.agenthub/config.yaml
reviewer: codex              # codex | claude | human
agent: claude                # codex | claude
max_rounds: 5               # feedback iterations before escalating
max_parallel: 4              # concurrent agents
branch_prefix: tmp/ah        # temp branch naming
cleanup_branches: true       # delete branches after merge
notify: true                 # send notification when done
agent_timeout_sec: 0         # seconds before agent times out (0 = no limit)
reviewer_timeout_sec: 0      # seconds before reviewer times out (0 = no limit)
agent_max_turns: 0           # max agent conversation turns (0 = no limit)
context_files: []            # extra files to include in agent context
prompt_strategy: auto        # auto | zero_shot | few_shot | chain_of_thought | self_consistency | tree_of_thoughts
review_consistency: 1        # number of review passes (majority vote when >1)
research_enabled: true       # run complexity gate + research for complex tasks
research_model: ""           # model for research phase (empty = use planner_model)
research_max_files: 10       # max files to deep-read during research
validation_enabled: true     # run post-merge validation
claude_model: claude-sonnet-4-20250514
codex_model: o3-mini
planner_model: claude-sonnet-4-20250514
```

Project-level overrides:

```yaml
# your-repo/.agenthub.yaml
reviewer: claude
max_rounds: 5
context_files:
  - README.md
  - ARCHITECTURE.md
```

## Logs

Every run is logged to `.agenthub/logs/` in your project directory. Each run gets a folder with the plan, research output, per-task round logs, validation results, and a summary.

```
.agenthub/logs/
  20260307_143022_000000/
    plan.json                    # task breakdown and repo analysis
    research.json                # complexity gate result + research brief
    fix-granger/
      round-1-agent.md           # what the coding agent did
      round-1-review.md          # what the reviewer said
      round-2-agent.md
      round-2-review.md          # approved
    add-evaluation/
      ...
    validation-1.json            # first validation result
    validation-2.json            # second attempt (if first failed)
    summary.md                   # final run summary
```

Add `.agenthub/` to your `.gitignore`.

## Requirements

- Python 3.11+
- git
- At least one of: `claude` CLI, `codex` CLI, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- macOS, Linux, or WSL (for notifications, macOS gets native banners)

## Limitations

- Won't push to remote or open PRs — it only works locally
- Large tasks (50+ files changed) may hit context limits — break them up
- If the planner misjudges dependencies, you might get merge conflicts — the merger tries to resolve them automatically but may flag for human help
