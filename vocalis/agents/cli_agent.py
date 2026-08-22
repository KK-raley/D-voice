"""Generic CLI agent connector: bridge *any* terminal coding agent by voice.

Works with OpenAI Codex CLI, opencode, aider, Gemini CLI, ... anything that
runs as ``<program> [args...] "<instruction>"`` and streams progress to
stdout. Configure in ``~/.vocalis/config.toml``:

.. code-block:: toml

    [[cli_agents]]
    name = "codex"
    command = ["codex", "exec", "{instruction}"]

    [[cli_agents]]
    name = "opencode"
    command = ["opencode", "run", "{instruction}"]

The ``{instruction}`` placeholder receives the spoken instruction verbatim;
omit it to have the instruction appended as the final argument.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from collections.abc import AsyncIterator
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord


def build_args(command: list[str], instruction: str) -> list[str]:
    """Substitute ``{instruction}`` into the command template (or append)."""
    command = list(command)
    if not command:
        raise ValueError("cli agent command must not be empty")
    if any("{instruction}" in part for part in command):
        return [part.replace("{instruction}", instruction) for part in command]
    return command + [instruction]


class GenericCLIAgent(AgentConnector):
    """Drive an external CLI agent as a first-class vocalis citizen."""

    def __init__(
        self,
        name: str = "codex",
        command: list[str] | None = None,
        event_bus=None,
        timeout_s: float = 1800.0,
        description: str | None = None,
    ) -> None:
        super().__init__(event_bus)
        self.name = name
        self.command = command or ["codex", "exec", "{instruction}"]
        self.timeout_s = timeout_s
        self.description = description or f"{name} CLI bridge - voice-driven coding agent"
        self.capabilities = ["coding", "shell", "file-editing", "long-running"]

    def _exe(self) -> str:
        return self.command[0]

    async def health_check(self) -> bool:
        return shutil.which(self._exe()) is not None

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        exe = self._exe()
        if shutil.which(exe) is None:
            raise RuntimeError(
                f"'{exe}' not found on PATH - install the {self.name} CLI first"
            )
        args = build_args(self.command, instruction)
        yield f"Dispatching to {self.name}: {shlex.join(args)[:80]}..."
        yield 0.05

        proc = await asyncio.create_subprocess_exec(
            *args,
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
                raise RuntimeError(
                    f"{self.name} exceeded {self.timeout_s}s watchdog"
                ) from None
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                chunks.append(text)
                record.output = "\n".join(chunks)
                yield f"working: {text[:80]}"

        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.name} exited with code {proc.returncode}: {record.output[-400:]}"
            )
        record.output = "\n".join(chunks)
        yield 1.0
