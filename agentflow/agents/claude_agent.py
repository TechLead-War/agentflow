from __future__ import annotations
import asyncio
import shutil
from .base import BaseAgent


class ClaudeAgent(BaseAgent):
    """Coding agent that uses the Claude CLI (claude code)."""

    async def run(self, prompt: str, working_dir: str) -> str:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError(
                "Claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code"
            )

        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "-p", prompt,
            "--output-format", "text",
            "--max-turns", "10",
            "--dangerously-skip-permissions",
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Claude agent failed (exit {proc.returncode}): {err[:500]}")

        return output
