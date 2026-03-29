from __future__ import annotations
import asyncio
import shutil
import os
from .base import BaseAgent

_CODEX_TIMEOUT_SEC = int(os.environ.get("AGENTHUB_CODEX_TIMEOUT_SEC", "300"))


class CodexAgent(BaseAgent):
    """Coding agent that uses the Codex CLI or falls back to OpenAI API."""

    async def run(self, prompt: str, working_dir: str, max_turns: int = 0) -> str:
        codex_bin = shutil.which("codex")

        if codex_bin:
            return await self._run_cli(codex_bin, prompt, working_dir)
        else:
            return await self._run_api(prompt, working_dir)

    async def _run_cli(self, codex_bin: str, prompt: str, working_dir: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "exec",
            "--full-auto",
            "-",
            cwd=working_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=_CODEX_TIMEOUT_SEC,
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
            raise RuntimeError(f"Codex agent failed (exit {proc.returncode}): {err[:500]}")

        return output

    async def _run_api(self, prompt: str, working_dir: str) -> str:
        """Fallback: use OpenAI API to generate code, write files manually."""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise RuntimeError("Neither codex CLI nor openai package available.")

        client = AsyncOpenAI()

        import subprocess
        tree = subprocess.run(
            ["git", "ls-files"], cwd=working_dir,
            capture_output=True, text=True
        ).stdout[:3000]

        system_prompt = (
            "You are a coding agent. You will be given a task and a codebase. "
            "Implement the task by outputting the complete modified files. "
            "Format each file as:\n"
            "=== FILE: path/to/file ===\n"
            "<file content>\n"
            "=== END FILE ===\n"
        )

        response = await client.chat.completions.create(
            model=os.environ.get("AGENTFLOW_CODEX_MODEL", "o3-mini"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"CODEBASE FILES:\n{tree}\n\nTASK:\n{prompt}"},
            ],
            max_tokens=16000,
        )

        output = response.choices[0].message.content or ""
        self._write_files(output, working_dir)
        return output

    def _write_files(self, output: str, working_dir: str):
        """Parse the agent's output and write files to disk."""
        import re
        from pathlib import Path

        pattern = r"=== FILE: (.+?) ===\n(.*?)\n=== END FILE ==="
        matches = re.findall(pattern, output, re.DOTALL)
        root = Path(working_dir).resolve()

        for filepath, content in matches:
            rel_path = filepath.strip()
            if not rel_path:
                continue

            full_path = (root / rel_path).resolve()
            try:
                full_path.relative_to(root)
            except ValueError:
                continue

            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
