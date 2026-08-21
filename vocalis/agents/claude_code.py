"""Claude Code connector: drive the `claude` CLI agent by voice.

Requires the Claude Code CLI installed and authenticated (``claude`` on
PATH). Instructions are executed with ``claude -p`` (non-interactive print
mode); stdout is streamed as the task output.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord


class ClaudeCodeAgent(AgentConnector):
    name = "claude-code"
    description = "Claude Code CLI bridge - full coding agent, driven by voice"
    capabilities = ["coding", "shell", "file-editing", "long-running"]

    def __init__(self, event_bus=None, cli: str = "claude", timeout_s: float = 1200.0) -> None:
        super().__init__(event_bus)
        self.cli = cli
        self.timeout_s = timeout_s

    async def health_check(self) -> bool:
        return shutil.which(self.cli) is not None

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        if shutil.which(self.cli) is None:
            raise RuntimeError(
                "claude CLI not found on PATH - install Claude Code first"
            )
        yield f"Dispatching to Claude Code: {instruction[:60]}..."
        yield 0.1

        proc = await asyncio.create_subprocess_exec(
            self.cli,
            "-p",
            instruction,
            "--output-format",
            "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        chunks: list[str] = []
        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"claude-code exceeded {self.timeout_s}s watchdog") from None
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                chunks.append(text)
                record.output = "\n".join(chunks)
                yield f"working: {text[:80]}"

        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited with code {proc.returncode}: {record.output[-400:]}")
        record.output = "\n".join(chunks)
        record.artifacts.append("claude-session")
        yield 1.0
