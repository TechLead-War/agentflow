from __future__ import annotations
import asyncio
import shutil
from .base import BaseAgent

# Per-turn budget: ~30-60s for Claude Code tool use. Generous default.
_SECONDS_PER_TURN = int(os.environ.get("AGENTHUB_AGENT_SECS_PER_TURN", "90"))


class ClaudeAgent(BaseAgent):
    """Coding agent that uses the Claude CLI (claude code)."""

    async def run(self, prompt: str, working_dir: str, max_turns: int = 0) -> str:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError(
                "Claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code"
            )

        args = [
            claude_bin,
            "-p", prompt,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        if max_turns > 0:
            args.extend(["--max-turns", str(max_turns)])

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timeout = self.max_turns * _SECONDS_PER_TURN
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            raise

        output = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Claude agent failed (exit {proc.returncode}): {err[:500]}")

        return output
