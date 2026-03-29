All four review failures are addressed:

1. **Requirements section** (check 2, 5): Now says "set API key, or install the CLI" — matches `check_api_keys()` which accepts either `shutil.which("claude"/"codex")` or env vars.

2. **Logs section** (check 2, 4, 5): Removed non-existent `result.json`. Replaced `# LGTM` with accurate comment describing the actual structured review format (`keep`/`retry`/`reject` + checks + summary). Added `research.json` and `validation-1.json` that the logging functions actually produce.

3. **Config `notify`** (check 3, 4, 5): Removed the misleading `notify: true # send notification when done` line. The config key exists in the dataclass but `notify(state)` is called unconditionally in `cli.py` — documenting it as a toggle is inaccurate. The "How it works" section (#10) still correctly says "Notifier pings you" without implying it's configurable.

4. **Missing `research_model`** (check 6): Added the `research_model` config key that exists in `Config` but was omitted from the README.
