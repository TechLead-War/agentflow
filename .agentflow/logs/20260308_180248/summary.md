# agentflow run 20260308_180248
Prompt: test prompt

## Results
  ✗ planning — failed
    error: All planner backends failed: claude CLI: Claude planner failed (exit 1):  | codex CLI: Codex planner failed (exit 1): WARNING: proceeding, even though we could not update PATH: Operation not permitted (os error 1)

thread 'reqwest-internal-sync-runtime' (2812155) panicked at /Users/runner/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/system-configuration-0.6.1/src/dynamic_store.rs:154:1:
Attempted to create a NULL object.
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

thread '<unnamed>' (2812154) panicked at /Users/runner/.cargo/registry/src/index.crates.io-1949cf8c6b5b557

0 merged, 1 failed